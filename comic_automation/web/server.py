"""Loopback-only threading WSGI server: startup checks, ``session.json``, shutdown.

Design: ``docs/web_console_v1_design.md`` §3, §4.2 and §9. Run it as
``python -m comic_automation.web.server``; the launcher
(``apps/web_console.py``) does that detached. Startup either completes or
refuses with one operator-readable line on stderr and exit code 2:

1. open and validate the state directory (:mod:`.state_dir`), which also takes
   the single-console lock;
2. bind ``127.0.0.1:<port>``. The host is a constant, never configuration;
3. generate this run's secrets and write ``session.json`` (port, launcher
   secret, pid), readable by the operator account only;
4. serve until interrupted, then remove ``session.json`` and release the lock.

``--init-state-dir`` creates the state directory (first run only) and exits.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import getpass
import json
import logging
import os
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Sequence
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer

from . import acl, security
from .app import ConsoleApp
from .config import LOOPBACK_HOST, REPO_ROOT
from .security import SecretStore
from .state_dir import StateDir, StateDirRefused, init_state_dir, open_state_dir, state_dir_from_env

log = logging.getLogger("comic_automation.web")
# Bounds what one stalled client can hold (§3: wsgiref is not hardened, so the
# loopback bind, the body cap and this timeout do that job).
REQUEST_TIMEOUT_SECONDS = 30
EXIT_REFUSED = 2


class _Server(ThreadingMixIn, WSGIServer):
    daemon_threads = True
    # Measured on OfficePC (2026-10-27, tests/web/test_web_server.py): while the
    # console listens, a second socket cannot bind 127.0.0.1:<port> even with
    # SO_REUSEADDR, and SO_EXCLUSIVEADDRUSE changed nothing measurable, so it is
    # not set. A wildcard 0.0.0.0:<port> bind can coexist either way, but loopback
    # traffic still reaches the more specific 127.0.0.1 bind.
    allow_reuse_address = False


class _Handler(WSGIRequestHandler):
    timeout = REQUEST_TIMEOUT_SECONDS

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        # The stdlib writes the raw request line to stderr. Route it to
        # console.log with /login query strings removed; headers (Cookie,
        # X-Launcher-Secret, X-CSRF-Token) are never logged at all.
        log.info("%s %s", self.address_string(), security.redact_request_line(format % args))

    def address_string(self) -> str:
        return self.client_address[0]


def _git_sha() -> str | None:
    try:
        # CREATE_NO_WINDOW: the server runs detached, so a console child would
        # otherwise flash a window on the operator's desktop.
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def write_session_file(state: StateDir, port: int, launcher_secret: str) -> None:
    """Write ``session.json`` (§4.2: it holds the launcher secret).

    The state directory is already private (checked at start-up), so the file
    inherits operator-only access from the moment it exists. The file's own
    protected ACL is applied to the empty file before the secret is written, as
    a second layer in case the directory is loosened later.
    """
    path = state.session_file
    tmp = path.with_suffix(".json.tmp")
    tmp.unlink(missing_ok=True)
    tmp.touch()
    try:
        acl.make_private(tmp, directory=False)
    except acl.AclError as exc:
        raise StateDirRefused(f"Could not restrict {tmp} to you: {exc}") from None
    tmp.write_text(json.dumps({"port": port, "launcher_secret": launcher_secret, "pid": os.getpid()}), encoding="utf-8")
    # os.replace keeps the source's ACL, so the restricted file is what lands.
    os.replace(tmp, path)


def _configure_logging(state: StateDir) -> None:
    handler = RotatingFileHandler(state.server_log, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def build_server(state: StateDir, port: int | None = None) -> tuple[_Server, ConsoleApp]:
    """Bind and wire everything; the caller runs ``serve_forever``.

    ``port`` overrides the configured one (tests pass 0 for an ephemeral port).
    """
    secrets = SecretStore(operator=getpass.getuser())
    started = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    app = ConsoleApp(config=state.config, secrets=secrets, port=0, started_at=started, git_sha=_git_sha())
    bind_port = state.config.port if port is None else port
    try:
        server = _Server((LOOPBACK_HOST, bind_port), _Handler)
    except OSError as exc:
        raise StateDirRefused(f"Cannot listen on {LOOPBACK_HOST}:{bind_port}: {exc}") from None
    server.set_app(app)
    app.port = server.server_address[1]
    write_session_file(state, app.port, secrets.launcher_secret)
    return server, app


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m comic_automation.web.server",
                                     description="Run the CBZ web console on 127.0.0.1.")
    parser.add_argument("--init-state-dir", action="store_true",
                        help="create the state directory (first run only) and exit")
    parser.add_argument("--port", type=int, default=None, help=argparse.SUPPRESS)  # tests: 0 = ephemeral
    args = parser.parse_args(argv)

    path = state_dir_from_env()
    try:
        if args.init_state_dir:
            init_state_dir(path)
            print(f"State directory ready: {path}")
            return 0
        state = open_state_dir(path)
    except StateDirRefused as exc:
        print(f"Web console refused to start: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    try:
        _configure_logging(state)
        server, app = build_server(state, args.port)
    except (StateDirRefused, OSError) as exc:
        # OSError too (console.log locked, session.json held open, disk full):
        # the launcher shows stderr's last line, so it must be a reason, not a
        # traceback.
        state.close()
        print(f"Web console refused to start: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    log.info("Console started on %s:%s (pid %s, git %s)", LOOPBACK_HOST, app.port, os.getpid(), app.git_sha)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        state.session_file.unlink(missing_ok=True)
        log.info("Console stopped")
        state.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
