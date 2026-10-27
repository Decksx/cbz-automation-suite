"""The console's WSGI application: routing, security checks, JSON errors.

Design: ``docs/web_console_v1_design.md`` §9 (security) and §10 (API). This
layer stays thin: it checks the request, finds the handler and maps errors.
Behaviour belongs in service modules, so the transport can change later
without touching it (§3).

Every request passes these checks, in this order:

1. **Host** must be ``127.0.0.1:<port>`` or ``localhost:<port>`` (else 421).
   This runs first, even for static files, because it is what defeats DNS
   rebinding.
2. **Authentication.** ``/login``, ``/``, ``/static/...`` and
   ``/api/launcher/hello`` are open; ``POST /api/bootstrap`` takes only the
   launcher secret; ``GET /api/health`` takes the launcher secret or a
   session; everything else needs a session: the cookie *and* the
   ``X-Session-Key`` browser key (else 401).
3. **Every method except GET, HEAD and OPTIONS**, bootstrap aside, needs a
   same-origin ``Origin``, ``Sec-Fetch-Site: same-origin`` when sent,
   ``Content-Type: application/json`` and the session's CSRF token, then a
   JSON body of at most 64 KiB with no command field.

The checks run before routing, so an unknown path behind a missing CSRF token
still answers 403, not 404. That is what lets the browser smoke test prove the
guard without a test-only endpoint in production.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import parse_qs

from . import security
from .config import ConsoleConfig
from .security import MAX_BODY_BYTES, RequestRefused, SecretStore, Session

STATIC_DIR = Path(__file__).resolve().parent / "static"
SERVER_VERSION = "web-console-m2"
log = logging.getLogger("comic_automation.web")

# A fixed table, not mimetypes: on Windows mimetypes reads the registry, where
# .js is sometimes text/plain, and with nosniff the browser then refuses to run
# the module.
_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}

_STATUS_TEXT = {
    200: "OK", 303: "See Other", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
    405: "Method Not Allowed", 409: "Conflict", 411: "Length Required", 413: "Payload Too Large",
    415: "Unsupported Media Type", 421: "Misdirected Request", 422: "Unprocessable Entity",
    500: "Internal Server Error",
}


@dataclass
class Request:
    """What a handler sees: the parsed request plus the checks' results."""

    method: str
    path: str
    query: dict[str, list[str]]
    environ: Mapping[str, Any]
    session: Session | None = None
    body: Any = None


@dataclass
class Response:
    status: int
    body: bytes = b""
    content_type: str = "application/json"
    headers: list[tuple[str, str]] = field(default_factory=list)


def json_response(payload: Any, status: int = 200) -> Response:
    return Response(status, json.dumps(payload).encode("utf-8"))


def error_response(status: int, code: str, message: str, details: dict | None = None) -> Response:
    return json_response({"error": code, "message": message, "details": details or {}}, status)


Handler = Callable[[Request], Response]


class ConsoleApp:
    """The WSGI callable. One instance per server run.

    ``port`` is the port actually bound (the Host and Origin checks need it),
    so the server sets it after binding; tests construct the app directly.
    """

    def __init__(self, *, config: ConsoleConfig, secrets: SecretStore, port: int,
                 started_at: str, git_sha: str | None, static_dir: Path = STATIC_DIR):
        self.config = config
        self.secrets = secrets
        self.port = port
        self.started_at = started_at
        self.git_sha = git_sha
        self.static_dir = static_dir
        self.routes: dict[tuple[str, str], Handler] = {
            ("GET", "/api/launcher/hello"): self.launcher_hello,
            ("POST", "/api/bootstrap"): self.bootstrap,
            ("GET", "/login"): self.login,
            ("GET", "/api/session"): self.session_info,
            ("GET", "/api/health"): self.health,
            ("GET", "/api/config"): self.config_report,
        }

    # ------------------------------------------------------------ WSGI entry
    def __call__(self, environ: dict, start_response: Callable) -> Iterable[bytes]:
        try:
            response = self.handle(environ)
        except Exception:
            # The traceback goes to console.log, never to the browser.
            log.exception("Unhandled error in %s %s", environ.get("REQUEST_METHOD"), environ.get("PATH_INFO"))
            response = error_response(500, "INTERNAL", "Internal error; see console.log.")
        headers = [("Content-Type", response.content_type), ("Content-Length", str(len(response.body))),
                   *security.SECURITY_HEADERS, *response.headers]
        if response.content_type == "application/json" or response.status == 303:
            headers.append(("Cache-Control", "no-store"))
        start_response(f"{response.status} {_STATUS_TEXT.get(response.status, '')}".strip(), headers)
        if environ.get("REQUEST_METHOD", "").upper() == "HEAD":
            return [b""]  # same headers, including Content-Length, no body
        return [response.body]

    def handle(self, environ: Mapping[str, Any]) -> Response:
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/") or "/"
        req = Request(method, path, parse_qs(environ.get("QUERY_STRING", "")), environ)

        if not security.host_ok(environ, self.port):
            return error_response(421, "HOST_REFUSED", "Unexpected Host header.")

        if method in ("GET", "HEAD") and (path == "/" or path.startswith("/static/")):
            return self.static(path)

        try:
            self.authenticate(req)
            if method not in security.SAFE_METHODS and (method, path) != ("POST", "/api/bootstrap"):
                security.check_state_change(environ, self.port, req.session)
                req.body = self.read_json(environ)
                security.refuse_command_fields(req.body)
        except RequestRefused as refused:
            return error_response(refused.status, refused.code, refused.message)

        handler = self.routes.get(("GET" if method == "HEAD" else method, path))
        if handler is None:
            if any(p == path for (_, p) in self.routes):
                return error_response(405, "METHOD_NOT_ALLOWED", f"{method} is not supported here.")
            return error_response(404, "NOT_FOUND", "No such endpoint.")
        return handler(req)

    # --------------------------------------------------------------- checks
    def authenticate(self, req: Request) -> None:
        if req.path in ("/login", "/api/launcher/hello"):
            return
        if (req.method, req.path) == ("POST", "/api/bootstrap"):
            if not self.secrets.check_launcher_secret(req.environ.get("HTTP_X_LAUNCHER_SECRET")):
                raise RequestRefused(401, "UNAUTHENTICATED", "Launcher secret required.")
            return
        req.session = self.secrets.session_for(security.cookie_value(req.environ),
                                               req.environ.get("HTTP_X_SESSION_KEY"))
        if req.session is None:
            if (req.method, req.path) in (("GET", "/api/health"), ("HEAD", "/api/health")) and self.secrets.check_launcher_secret(
                req.environ.get("HTTP_X_LAUNCHER_SECRET")
            ):
                return
            raise RequestRefused(401, "UNAUTHENTICATED", "Open the console from the CBZ Console shortcut.")

    @staticmethod
    def read_json(environ: Mapping[str, Any]) -> Any:
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            raise RequestRefused(400, "BAD_REQUEST", "Bad Content-Length.") from None
        if length > MAX_BODY_BYTES:
            raise RequestRefused(413, "BODY_TOO_LARGE", f"Request bodies are limited to {MAX_BODY_BYTES} bytes.")
        if length <= 0:
            return None
        raw = environ["wsgi.input"].read(length)
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RequestRefused(400, "BAD_JSON", "The body is not valid JSON.") from None

    # ------------------------------------------------------------- handlers
    def launcher_hello(self, req: Request) -> Response:
        """Prove to the launcher that this server holds the launcher secret.

        Open by design: the answer is an HMAC of the caller's random challenge,
        which reveals nothing usable. The launcher sends its secret only after
        checking it.
        """
        challenge = req.environ.get("HTTP_X_LAUNCHER_CHALLENGE", "")
        if len(challenge) < security.MIN_CHALLENGE_LENGTH or len(challenge) > 256:
            return error_response(400, "BAD_CHALLENGE", "Send X-Launcher-Challenge (32 to 256 characters).")
        return json_response({"proof": security.hello_proof(self.secrets.launcher_secret, challenge)})

    def bootstrap(self, req: Request) -> Response:
        return json_response({"code": self.secrets.issue_bootstrap_code(),
                              "expires_in": security.BOOTSTRAP_TTL_SECONDS})

    def login(self, req: Request) -> Response:
        code = (req.query.get("code") or [None])[0]
        if not self.secrets.consume_bootstrap_code(code):
            return error_response(401, "LOGIN_CODE_REFUSED",
                                  "This sign-in link has been used or has expired. Open the console from its shortcut again.")
        session = self.secrets.create_session()
        # 303 so a reload of the resulting page is a plain GET of "/", and the
        # code leaves the address bar (history is not relied on, §9). The
        # browser key rides in the fragment, which never reaches a server or
        # log; the page moves it to sessionStorage and clears it from the URL.
        # Clear-Site-Data drops anything left on this origin from before,
        # including a service worker another program might have planted while
        # it held the port.
        return Response(303, b"", "text/plain", [
            ("Location", f"/#k={session.browser_key}"),
            ("Set-Cookie", security.session_cookie_header(session)),
            ("Clear-Site-Data", '"cache", "storage"'),
        ])

    def session_info(self, req: Request) -> Response:
        s = req.session
        return json_response({"operator": s.operator, "csrf_token": s.csrf_token, "server_version": SERVER_VERSION,
                              "git_sha": self.git_sha, "started_at": self.started_at})

    def health(self, req: Request) -> Response:
        # Jobs (M3) and watcher status (M6) do not exist yet; report them honestly.
        return json_response({"ok": True, "jobs_running": 0, "watcher_state": "unknown"})

    def config_report(self, req: Request) -> Response:
        return json_response({"values": self.config.report()})

    def static(self, path: str) -> Response:
        rel = "index.html" if path == "/" else path[len("/static/"):]
        root = os.path.realpath(self.static_dir)
        not_found = error_response(404, "NOT_FOUND", "No such file.")
        if "\0" in rel:
            return not_found
        target = os.path.realpath(os.path.join(root, *rel.split("/")))
        # Containment on the resolved path: "..", backslashes and absolute or
        # drive-letter segments ("C:") all land outside root and get a 404.
        # commonpath raises for a different drive, which is outside too.
        try:
            inside = os.path.commonpath([root, target]) == root
        except ValueError:
            inside = False
        if not inside or not os.path.isfile(target):
            return not_found
        ctype = _STATIC_TYPES.get(os.path.splitext(target)[1].lower(), "application/octet-stream")
        with open(target, "rb") as handle:
            return Response(200, handle.read(), ctype, [("Cache-Control", "no-cache")])
