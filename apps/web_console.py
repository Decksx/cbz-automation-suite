"""Desktop launcher for the CBZ web console (design §13).

The "CBZ Console" shortcut runs this under ``pythonw``. It:

1. reads ``session.json`` from the state directory. If a console proves it
   holds that launcher secret (``GET /api/launcher/hello``: an HMAC of a fresh
   random challenge) and then answers ``GET /api/health``, it is reused. The
   secret is never sent to a server that has not proved this, because after a
   reboot ``session.json`` is stale and another program may hold the port;
2. otherwise starts ``python -m comic_automation.web.server`` detached and
   waits for its ``session.json`` and health check;
3. asks the console for a single-use bootstrap code (``POST /api/bootstrap``
   with ``X-Launcher-Secret``) and opens the browser at the sign-in URL it
   returns, on this run's own ``<label>.localhost`` host.

If the server refuses to start (G: missing, another console running, bad
``console.toml``), the server's own reason is shown in a message box, since a
``pythonw`` process has no console to print to.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from comic_automation.web.security import hello_proof  # noqa: E402
from comic_automation.web.state_dir import state_dir_from_env  # noqa: E402

STARTUP_TIMEOUT_SECONDS = 20
HEALTH_TIMEOUT_SECONDS = 2
# After a refusal saying another console is running (two launches at once),
# how long to wait for that one to come up instead of showing an error.
RACE_WAIT_SECONDS = 10

# No proxy handler: urllib otherwise honours HTTP_PROXY and the Windows proxy
# settings even for 127.0.0.1, which would send the secret to the proxy.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class LaunchError(RuntimeError):
    """The console could not be reached or started; the message is for the operator."""


def _request(url: str, method: str, timeout: float, headers: dict[str, str]) -> dict:
    req = urllib.request.Request(url, method=method, headers=headers)
    with _opener.open(req, timeout=timeout) as response:
        return json.load(response)


def _proves_secret(port: int, secret: str) -> bool:
    """True if the server on ``port`` answers a fresh challenge with the right HMAC."""
    challenge = secrets.token_urlsafe(32)
    reply = _request(f"http://127.0.0.1:{port}/api/launcher/hello", "GET", HEALTH_TIMEOUT_SECONDS,
                     {"X-Launcher-Challenge": challenge})
    return hmac.compare_digest(str(reply.get("proof", "")), hello_proof(secret, challenge))


def _read_session(session_file: Path) -> dict | None:
    try:
        data = json.loads(session_file.read_text(encoding="utf-8"))
        return data if isinstance(data.get("port"), int) and data.get("launcher_secret") else None
    except (OSError, ValueError, AttributeError):
        return None


def live_console(session_file: Path) -> dict | None:
    """The session of a console that is up and owns ``session.json``, else None."""
    session = _read_session(session_file)
    if session is None:
        return None
    port, secret = session["port"], session["launcher_secret"]
    try:
        if not _proves_secret(port, secret):
            return None  # something else holds the port; it gets nothing
        health = _request(f"http://127.0.0.1:{port}/api/health", "GET", HEALTH_TIMEOUT_SECONDS,
                          {"X-Launcher-Secret": secret})
    except (OSError, urllib.error.URLError, ValueError):
        return None  # stale file from a console that has exited
    return session if health.get("ok") else None


def start_server(session_file: Path) -> dict:
    """Start the server detached and wait until it answers; raise LaunchError with its reason."""
    exe = Path(sys.executable)
    # The server must outlive this launcher and show no window. python.exe, not
    # pythonw.exe, so its stderr (the refusal reason) is captured to the log.
    python = exe.with_name("python.exe") if exe.name.lower() == "pythonw.exe" else exe
    log = Path(tempfile.gettempdir()) / "cbz-web-console-start.log"
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    with open(log, "wb") as out:
        proc = subprocess.Popen([str(python), "-m", "comic_automation.web.server"], cwd=REPO_ROOT,
                                stdin=subprocess.DEVNULL, stdout=out, stderr=out, creationflags=flags,
                                close_fds=True)
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            reason = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            last = reason[-1] if reason else f"The console exited with code {proc.returncode}."
            if "already running" in last:
                # Another launch won the race and is still starting; use it.
                race_deadline = time.monotonic() + RACE_WAIT_SECONDS
                while time.monotonic() < race_deadline:
                    session = live_console(session_file)
                    if session:
                        return session
                    time.sleep(0.25)
            raise LaunchError(last)
        session = live_console(session_file)
        if session and session.get("pid") == proc.pid:
            return session
        time.sleep(0.25)
    raise LaunchError(f"The console did not answer within {STARTUP_TIMEOUT_SECONDS} s. See {log}.")


def login_url(session: dict) -> str:
    port = session["port"]
    reply = _request(f"http://127.0.0.1:{port}/api/bootstrap", "POST", 10,
                     {"X-Launcher-Secret": session["launcher_secret"]})
    # The server names this run's browser host (<label>.localhost:<port>). Only
    # a server that passed the hello check gets here, but the URL is still
    # checked before a browser is pointed at it.
    url = urllib.parse.urlsplit(str(reply.get("login_url", "")))
    if not (url.scheme == "http" and url.hostname and url.hostname.endswith(".localhost")
            and url.port == port and url.path == "/login"):
        raise LaunchError("The console returned an unexpected sign-in address.")
    return url.geturl()


def _show_error(message: str) -> None:
    if os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "CBZ Console", 0x10)  # MB_ICONERROR
    else:  # pragma: no cover
        print(message, file=sys.stderr)


def main() -> int:
    session_file = state_dir_from_env() / "session.json"
    try:
        session = live_console(session_file) or start_server(session_file)
        webbrowser.open(login_url(session))
    except LaunchError as exc:
        _show_error(str(exc))
        return 1
    except (OSError, urllib.error.URLError, ValueError, KeyError) as exc:
        _show_error(f"Could not open the console: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
