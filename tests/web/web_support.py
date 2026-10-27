"""Shared helpers for the web console tests: an in-process WSGI client and
builders for a console app and state directory in a temporary folder.

The client is deliberately small (design §12): it builds a WSGI environ,
calls the app, and returns status, headers and the decoded body, so every
security check is exercised exactly as a real request would hit it.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

from comic_automation.web.app import ConsoleApp
from comic_automation.web.config import load_console_config
from comic_automation.web.security import SESSION_COOKIE_PREFIX, SecretStore

PORT = 8765
LABEL = "testlabel"  # in-process apps get a fixed browser-host label
HOST = f"{LABEL}.localhost:{PORT}"
ORIGIN = f"http://{HOST}"


@dataclass
class Result:
    status: int
    headers: dict[str, str]
    raw_headers: list[tuple[str, str]]
    body: bytes

    @property
    def json(self) -> Any:
        return json.loads(self.body) if self.body else None


def call(app: ConsoleApp, method: str, path: str, *, headers: dict[str, str] | None = None,
         body: bytes | str | dict | None = None, host: str = HOST) -> Result:
    if isinstance(body, dict):
        body = json.dumps(body)
    if isinstance(body, str):
        body = body.encode()
    body = body or b""
    path, _, query = path.partition("?")
    environ: dict[str, Any] = {
        "REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query, "SERVER_NAME": "127.0.0.1",
        "SERVER_PORT": str(PORT), "wsgi.input": io.BytesIO(body), "CONTENT_LENGTH": str(len(body)),
        "wsgi.url_scheme": "http", "HTTP_HOST": host,
    }
    for name, value in (headers or {}).items():
        key = name.upper().replace("-", "_")
        environ[key if key in ("CONTENT_TYPE", "CONTENT_LENGTH") else "HTTP_" + key] = value
    captured: dict[str, Any] = {}

    def start_response(status: str, hdrs: list[tuple[str, str]]) -> None:
        captured["status"] = int(status.split()[0])
        captured["headers"] = hdrs

    out = b"".join(app(environ, start_response))
    return Result(captured["status"], {k.lower(): v for k, v in captured["headers"]}, captured["headers"], out)


def write_config(directory: Path, text: str = "") -> Path:
    path = directory / "console.toml"
    path.write_text(text, encoding="utf-8")
    return path


def make_app(tmp_path: Path, clock=None) -> ConsoleApp:
    config = load_console_config(write_config(tmp_path), environ={})
    secrets = SecretStore(operator="tester", **({"clock": clock} if clock else {}))
    return ConsoleApp(config=config, secrets=secrets, port=PORT, started_at="2026-10-27T00:00:00+00:00",
                      git_sha="abc123", origin_label=LABEL)


def login(app: ConsoleApp) -> tuple[dict[str, str], str]:
    """Bootstrap and log in like a browser.

    Returns (auth headers, csrf token). The auth headers carry both halves of
    a session: the cookie and the browser key from the redirect's fragment.
    """
    code = call(app, "POST", "/api/bootstrap", headers={"X-Launcher-Secret": app.secrets.launcher_secret}).json["code"]
    res = call(app, "GET", f"/login?code={code}")
    assert res.status == 303, res.body
    jar = SimpleCookie()
    jar.load(res.headers["set-cookie"])
    location = res.headers["location"]
    assert location.startswith("/#k=")
    (name, morsel), = jar.items()  # exactly one cookie, named for this session
    assert name.startswith(SESSION_COOKIE_PREFIX)
    auth = {"Cookie": f"{name}={morsel.value}", "X-Session-Key": location[len("/#k="):]}
    csrf = call(app, "GET", "/api/session", headers=auth).json["csrf_token"]
    return auth, csrf


def post_headers(auth: dict[str, str], csrf: str, **extra: str) -> dict[str, str]:
    headers = {**auth, "X-CSRF-Token": csrf, "Origin": ORIGIN, "Content-Type": "application/json"}
    headers.update(extra)
    return headers


def private_dir(path: Path, config: str | None = "") -> Path:
    """A state directory prepared the way the operator's --init-state-dir does.

    ``config`` is written as console.toml. The default, empty, keeps the
    example's network roots (\\\\tower, Z:) out of tests that are not about
    them: checking those costs a ~3 s name lookup per start. Pass None to leave
    the file absent, so the first open copies the example, as in production.
    """
    from comic_automation.web.state_dir import init_state_dir

    init_state_dir(path)
    if config is not None:
        (path / "console.toml").write_text(config, encoding="utf-8")
    return path
