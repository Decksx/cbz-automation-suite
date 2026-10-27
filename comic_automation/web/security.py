"""Console security on loopback: secrets, sessions, request checks, redaction.

Design: ``docs/web_console_v1_design.md`` §9. The console binds 127.0.0.1
only, but it still treats every request as hostile, because any web page the
operator opens can make the browser send requests to localhost.

Three distinct secrets, each 256 bits from :mod:`secrets`, compared in constant
time and regenerated on every server start:

1. the **launcher secret**, written to ``session.json`` (operator-only ACL) and
   sent by the launcher as ``X-Launcher-Secret`` to ``POST /api/bootstrap``;
2. the **bootstrap code** that call returns: single use, valid 60 s, good for
   nothing except ``GET /login?code=``;
3. the **session id**, set by ``/login`` as an ``HttpOnly; SameSite=Strict``
   cookie, plus a per-session **CSRF token** returned by ``/api/session``.

The cookie alone is not enough. Cookies are not scoped to a port, so the
browser also sends this one to any other program listening on 127.0.0.1. That
program could replay it from outside the browser, forging Host and Origin. So
``/login`` also issues a **browser key**, delivered in the redirect's URL
fragment (``/#k=…``; fragments never reach a server or a log). The page keeps
it in ``sessionStorage`` and sends it as ``X-Session-Key`` on every API call.
A session needs both.

Each session's cookie has its own name (``cbz_console_<public id>``; the id is
not a secret). The browser key selects the session and that session's own
cookie must match. One shared cookie name would let a second sign-in overwrite
the cookie every open console tab relies on, while each tab keeps its own
browser key, so the older tabs would be locked out for good.

The launcher secret is never accepted as a session, and a session is never
accepted where the launcher secret is required (except ``/api/health``, which
takes either). Before the launcher sends its secret at all, it checks that the
server knows it: ``GET /api/launcher/hello`` answers a random challenge with an
HMAC under the launcher secret. So a process that took the port after the
console stopped cannot harvest the secret or a sign-in from a stale
``session.json``.
"""

from __future__ import annotations

import hmac
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from typing import Callable, Mapping

BOOTSTRAP_TTL_SECONDS = 60
MAX_BODY_BYTES = 64 * 1024
SESSION_COOKIE_PREFIX = "cbz_console_"
# A body naming any of these is refused outright (§9 "Input"): the browser never
# sends a command, the server always builds it from validated parameters.
COMMAND_FIELDS = frozenset({"argv", "cmd", "command"})
# Everything outside this set gets the Origin/CSRF checks. A denylist of
# state-changing methods would let an unusual verb skip them.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
HELLO_CONTEXT = b"cbz-console-launcher-hello:"
MIN_CHALLENGE_LENGTH = 32

SECURITY_HEADERS = (
    ("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
)


def new_secret() -> str:
    """256 bits, URL-safe."""
    return secrets.token_urlsafe(32)


def same(a: str | None, b: str | None) -> bool:
    """Constant-time equality that treats a missing value as a mismatch."""
    if not a or not b:
        return False
    return hmac.compare_digest(a.encode(), b.encode())


def hello_proof(secret: str, challenge: str) -> str:
    """HMAC-SHA256 of the launcher's challenge under the launcher secret.

    Domain-separated so the proof cannot be reused as any other value.
    """
    import hashlib

    return hmac.new(secret.encode(), HELLO_CONTEXT + challenge.encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Session:
    session_id: str
    csrf_token: str
    browser_key: str
    operator: str
    created_at: float
    public_id: str  # names this session's cookie; not a secret

    @property
    def cookie_name(self) -> str:
        return SESSION_COOKIE_PREFIX + self.public_id


@dataclass
class SecretStore:
    """All secrets for one server run. Thread-safe; nothing here is persisted
    except the launcher secret, which the server writes to ``session.json``."""

    operator: str
    clock: Callable[[], float] = time.time
    launcher_secret: str = field(default_factory=new_secret)
    _codes: dict[str, float] = field(default_factory=dict)
    _sessions: list[Session] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def check_launcher_secret(self, presented: str | None) -> bool:
        return same(presented, self.launcher_secret)

    def issue_bootstrap_code(self) -> str:
        code = new_secret()
        with self._lock:
            now = self.clock()
            # Drop expired codes so the table cannot grow without bound.
            self._codes = {c: exp for c, exp in self._codes.items() if exp > now}
            self._codes[code] = now + BOOTSTRAP_TTL_SECONDS
        return code

    def consume_bootstrap_code(self, presented: str | None) -> bool:
        """True once for a valid, unexpired code; the code is gone afterwards.

        Every stored code is compared, so the time taken does not depend on
        which code (if any) matched.
        """
        with self._lock:
            now = self.clock()
            hit = None
            for code, expires in self._codes.items():
                if same(presented, code) and expires > now:
                    hit = code
            if hit is None:
                return False
            del self._codes[hit]
            return True

    def create_session(self) -> Session:
        session = Session(new_secret(), new_secret(), new_secret(), self.operator, self.clock(),
                          secrets.token_hex(8))
        with self._lock:
            self._sessions.append(session)
        return session

    def session_for(self, cookies: Mapping[str, str], browser_key: str | None) -> Session | None:
        """The session whose browser key matches and whose own cookie is present and matches.

        ``cookies`` maps every cookie name the request carried to its value.
        """
        with self._lock:
            found = None
            for s in self._sessions:
                # Both halves compared for every session, so timing reveals neither.
                if same(browser_key, s.browser_key) & same(cookies.get(s.cookie_name), s.session_id):
                    found = s
            return found

    def all_secret_values(self) -> list[str]:
        """Every secret currently known, for the redaction tests."""
        with self._lock:
            return [self.launcher_secret, *self._codes, *(s.session_id for s in self._sessions),
                    *(s.csrf_token for s in self._sessions), *(s.browser_key for s in self._sessions)]


def request_cookies(environ: Mapping[str, str]) -> dict[str, str]:
    """Every cookie the request carried, by name; empty when absent or malformed."""
    raw = environ.get("HTTP_COOKIE")
    if not raw:
        return {}
    try:
        jar = SimpleCookie()
        jar.load(raw)
    except CookieError:
        return {}
    return {name: morsel.value for name, morsel in jar.items()}


def session_cookie_header(session: Session) -> str:
    return f"{session.cookie_name}={session.session_id}; HttpOnly; SameSite=Strict; Path=/"


def allowed_hosts(port: int) -> tuple[str, str]:
    return (f"127.0.0.1:{port}", f"localhost:{port}")


def host_ok(environ: Mapping[str, str], port: int) -> bool:
    """The Host header names this server on loopback; anything else is a DNS
    rebinding attempt or a misdirected request (421)."""
    return environ.get("HTTP_HOST", "").lower() in allowed_hosts(port)


class RequestRefused(Exception):
    """A request failed a security check. Carries the HTTP status and error code."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def check_state_change(environ: Mapping[str, str], port: int, session: Session) -> None:
    """Origin, fetch-metadata, content-type and CSRF checks for a state-changing request.

    Raise :class:`RequestRefused` on the first failure. Order matters only for
    which error is reported; all checks must pass.
    """
    origin = environ.get("HTTP_ORIGIN")
    # The Host check already ran, so Host is one of ours; the Origin must be the
    # same one. A missing Origin is refused: every browser sends one on POST.
    if not origin or origin.lower() != "http://" + environ.get("HTTP_HOST", "").lower():
        raise RequestRefused(403, "ORIGIN_REFUSED", "Requests must come from the console's own page.")
    site = environ.get("HTTP_SEC_FETCH_SITE")
    if site is not None and site != "same-origin":
        raise RequestRefused(403, "ORIGIN_REFUSED", "Cross-site requests are refused.")
    ctype = environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower()
    if ctype != "application/json":
        # Rules out form-based CSRF: an HTML form cannot send application/json.
        raise RequestRefused(415, "UNSUPPORTED_MEDIA_TYPE", "Send JSON (Content-Type: application/json).")
    if not same(environ.get("HTTP_X_CSRF_TOKEN"), session.csrf_token):
        raise RequestRefused(403, "CSRF_REFUSED", "Missing or wrong X-CSRF-Token.")


def refuse_command_fields(body: object) -> None:
    if isinstance(body, dict) and COMMAND_FIELDS & set(body):
        raise RequestRefused(422, "COMMAND_NOT_ACCEPTED", "The console builds commands itself; send parameters.")


_QUERY = re.compile(r"\?[^ \"]*")


def redact_request_line(line: str) -> str:
    """Drop every query string from a logged request line.

    Not just ``/login``'s: the raw request line is logged before decoding, so
    ``/%6Cogin?code=…`` would slip past a path-specific pattern while the
    server still decodes it to ``/login`` and consumes the code.
    """
    return _QUERY.sub("?<redacted>", line)
