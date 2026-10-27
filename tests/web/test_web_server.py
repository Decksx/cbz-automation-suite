"""The real server on a socket (design §12 "API" smoke test and "Security").

Each test starts ``python -m comic_automation.web.server --port 0`` in a
subprocess with ``CBZ_WEB_CONSOLE_HOME`` pointing at a temporary directory, so
the real startup path runs: state-directory checks, bind, session.json.
Every started process is killed in a ``finally``, including ones the test
expected to refuse: a guard that stops refusing must fail the test, not leave
a stray server behind.
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.parse
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from comic_automation.web import acl
from web_support import private_dir

REPO = Path(__file__).resolve().parents[2]


def _start(state: Path, *extra: str) -> subprocess.Popen:
    env = {**os.environ, "CBZ_WEB_CONSOLE_HOME": str(state), "PYTHONUNBUFFERED": "1"}
    return subprocess.Popen([sys.executable, "-m", "comic_automation.web.server", "--port", "0", *extra],
                            cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _reap(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait(10)
    proc.stdout.close()
    proc.stderr.close()


@contextmanager
def started(state: Path, *extra: str):
    proc = _start(state, *extra)
    try:
        yield proc
    finally:
        _reap(proc)


def _wait_for_session(state: Path, proc: subprocess.Popen, timeout: float = 20) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"server exited {proc.returncode}: {proc.stderr.read()}")
        try:
            data = json.loads((state / "session.json").read_text(encoding="utf-8"))
            if data.get("pid") == proc.pid:
                return data
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    raise AssertionError("server did not write session.json")


def _req(port: int, method: str, path: str, headers: dict | None = None, body: bytes | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    res = conn.getresponse()
    data = res.read()
    conn.close()
    return res.status, dict((k.lower(), v) for k, v in res.getheaders()), data


@pytest.fixture
def server(tmp_path):
    state = private_dir(tmp_path / "web-console")
    with started(state) as proc:
        yield state, _wait_for_session(state, proc), proc


def _bootstrap(port: int, secret: str) -> tuple[str, str]:
    """(bootstrap code, this run's browser host), asked for on 127.0.0.1 like the launcher does."""
    status, _, body = _req(port, "POST", "/api/bootstrap", {"X-Launcher-Secret": secret})
    assert status == 200
    reply = json.loads(body)
    host = urllib.parse.urlsplit(reply["login_url"]).netloc
    assert host.endswith(f".localhost:{port}")
    return reply["code"], host


def _full_login(port: int, secret: str) -> tuple[str, dict, str]:
    """(bootstrap code, auth headers, csrf) through the real socket. The auth
    headers carry the browser host, since session routes answer only there."""
    code, host = _bootstrap(port, secret)
    status, headers, _ = _req(port, "GET", f"/login?code={code}", {"Host": host})
    assert status == 303
    auth = {"Host": host, "Cookie": headers["set-cookie"].split(";", 1)[0],
            "X-Session-Key": headers["location"][len("/#k="):]}
    status, _, body = _req(port, "GET", "/api/session", auth)
    assert status == 200
    return code, auth, json.loads(body)["csrf_token"]


def test_real_server_round_trip_and_loopback_bind(server):
    _, session, _ = server
    port = session["port"]
    assert port > 0
    status, _, body = _req(port, "GET", "/api/health", {"X-Launcher-Secret": session["launcher_secret"]})
    assert status == 200 and json.loads(body)["ok"] is True
    _, auth, csrf = _full_login(port, session["launcher_secret"])
    status, _, _ = _req(port, "POST", "/api/nothing", {**auth, "X-CSRF-Token": csrf,
                                                       "Origin": f"http://{auth['Host']}",
                                                       "Content-Type": "application/json"}, b"{}")
    assert status == 404  # every guard passed; there is simply no such endpoint
    # Nothing answers on a non-loopback address of this machine.
    import socket
    other = socket.gethostbyname(socket.gethostname())
    if not other.startswith("127."):
        with pytest.raises(OSError):
            socket.create_connection((other, port), timeout=2).close()


@pytest.mark.skipif(os.name != "nt", reason="Windows socket-sharing rules")
def test_no_other_socket_can_share_the_console_address(server):
    """Another socket cannot take over 127.0.0.1:<port> while the console holds
    it, even with SO_REUSEADDR. This is a measurement of the platform, which is
    why the server sets no extra socket option (see _Server). A wildcard
    0.0.0.0:<port> bind can coexist, but loopback requests still reach the
    console's more specific bind."""
    import socket

    _, session, _ = server
    intruder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        intruder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with pytest.raises(OSError):
            intruder.bind(("127.0.0.1", session["port"]))
    finally:
        intruder.close()


@pytest.mark.parametrize("path", ["/static/%2e%2e/app.py", "/static/%2E%2E/%2e%2e/web/app.py", "/static/..%5capp.py"])
def test_encoded_traversal_is_refused_by_the_real_server(server, path):
    """wsgiref decodes the path before the app sees it, so only a real socket
    tests the decoded form."""
    _, session, _ = server
    _, host = _bootstrap(session["port"], session["launcher_secret"])
    assert _req(session["port"], "GET", path, {"Host": host})[0] == 404


def test_secrets_never_reach_the_log(server):
    state, session, _ = server
    port = session["port"]
    code, auth, csrf = _full_login(port, session["launcher_secret"])
    _req(port, "POST", "/api/nothing", {**auth, "X-CSRF-Token": csrf, "Origin": f"http://{auth['Host']}",
                                        "Content-Type": "application/json"}, b"{}")
    _req(port, "GET", "/login?code=" + code, {"Host": auth["Host"]})    # a replayed code is logged too, redacted
    _req(port, "GET", "/%6Cogin?code=" + code, {"Host": auth["Host"]})  # and one spelled to dodge a path pattern
    time.sleep(0.3)
    log = (state / "console.log").read_text(encoding="utf-8")
    assert "/login?<redacted>" in log  # the requests were logged...
    for secret in (session["launcher_secret"], code, auth["Cookie"].split("=", 1)[1], auth["X-Session-Key"], csrf):
        assert secret not in log       # ...but none of their secrets


def test_session_file_is_private_to_the_operator(server):
    state, _, _ = server
    assert acl.privacy_problems(state / "session.json") == []
    if os.name == "nt":
        assert "D:P" in acl.sddl(state / "session.json")  # its own protected list, not just inherited


def test_a_second_console_on_the_same_state_dir_refuses(server):
    state, _, _ = server
    with started(state) as second:
        assert second.wait(20) == 2
        assert "already running" in second.stderr.read()


@pytest.mark.parametrize("toml, message", [
    ("[server]\nhost = '0.0.0.0'\n", "server.host must be 127.0.0.1"),
    ("[serverr]\n", "unknown section"),
])
def test_bad_config_refuses_startup_with_exit_2(tmp_path, toml, message):
    state = private_dir(tmp_path / "web-console")
    (state / "console.toml").write_text(toml)
    with started(state) as proc:
        assert proc.wait(20) == 2
        assert message in proc.stderr.read()
        assert not (state / "session.json").exists()


def test_missing_state_dir_refuses_and_init_creates_a_private_one(tmp_path):
    state = tmp_path / "web-console"
    with started(state) as proc:
        assert proc.wait(20) == 2 and "--init-state-dir" in proc.stderr.read()
    assert not state.exists()
    with started(state, "--init-state-dir") as init:
        assert init.wait(20) == 0
    assert state.is_dir() and acl.privacy_problems(state) == []


# ------------------------------------------------------------- the launcher
class _Impostor(BaseHTTPRequestHandler):
    """A program that took the console's port: says ok to everything, records headers."""

    seen: list[dict] = []

    def do_GET(self):  # noqa: N802 - stdlib name
        self._answer()

    def do_POST(self):  # noqa: N802
        self._answer()

    def _answer(self):
        type(self).seen.append(dict(self.headers))
        body = json.dumps({"ok": True, "proof": "0" * 64, "code": "stolen"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.mark.parametrize("login_url", [
    "http://127.0.0.1:{port}/login?code=x",          # the shared loopback origin, not this run's
    "http://evil.example:{port}/login?code=x",
    "https://abc.localhost:{port}/login?code=x",
    "http://abc.localhost:1/login?code=x",            # wrong port
    "http://abc.localhost:{port}/elsewhere",
    "",
])
def test_launcher_opens_only_this_runs_localhost_sign_in_url(monkeypatch, login_url):
    sys.path.insert(0, str(REPO / "apps"))
    import web_console

    monkeypatch.setattr(web_console, "_request", lambda *a, **k: {"login_url": login_url.format(port=5000)})
    with pytest.raises(web_console.LaunchError, match="unexpected sign-in address"):
        web_console.login_url({"port": 5000, "launcher_secret": "s"})
    monkeypatch.setattr(web_console, "_request", lambda *a, **k: {"login_url": "http://abc.localhost:5000/login?code=x"})
    assert web_console.login_url({"port": 5000, "launcher_secret": "s"}) == "http://abc.localhost:5000/login?code=x"


def test_launcher_never_sends_its_secret_to_an_impostor(tmp_path):
    sys.path.insert(0, str(REPO / "apps"))
    import web_console

    impostor = ThreadingHTTPServer(("127.0.0.1", 0), _Impostor)
    threading.Thread(target=impostor.serve_forever, daemon=True).start()
    try:
        session_file = tmp_path / "session.json"  # stale: the console that wrote it is gone
        session_file.write_text(json.dumps({"port": impostor.server_address[1], "launcher_secret": "S" * 43, "pid": 1}))
        _Impostor.seen = []
        assert web_console.live_console(session_file) is None
        assert _Impostor.seen, "the launcher should have tried the hello challenge"
        for headers in _Impostor.seen:
            assert "S" * 43 not in json.dumps(headers)
            assert "X-Launcher-Secret" not in headers
    finally:
        impostor.shutdown()
        impostor.server_close()


def test_launcher_reuses_a_real_console_and_ignores_proxy_settings(server, tmp_path, monkeypatch):
    sys.path.insert(0, str(REPO / "apps"))
    import web_console

    state, session, _ = server
    # A proxy that does not exist: if the launcher used it, the call would fail.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")
    # urlopen's shared opener reads proxy settings once and is cached, so an
    # earlier test can hide a regression. The launcher must not use it at all.
    import urllib.request

    def no_default_opener(*a, **k):
        raise AssertionError("the launcher used urllib's default (proxy-aware) opener")

    monkeypatch.setattr(urllib.request, "urlopen", no_default_opener)
    found = web_console.live_console(state / "session.json")
    assert found is not None and found["port"] == session["port"]
    url = web_console.login_url(found)
    parts = urllib.parse.urlsplit(url)
    assert parts.hostname.endswith(".localhost") and parts.port == session["port"] and parts.path == "/login"
