"""The one real-browser smoke test (design §12), driving the installed Edge.

Covers what only a browser can prove: the bootstrap code exchange, the cookie
flags as the browser stores them, a CSRF-protected POST refused without its
header, a cross-origin fetch blocked by the browser, and a reused code
refused. The SSE resume check named in §12 waits for M3, which adds the stream.

Skipped when Playwright or Edge is unavailable; CI installs Playwright from
.github/requirements-ci.txt and windows-latest ships Edge, so no browser is
downloaded.
"""

from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys
import urllib.request

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from test_web_server import REPO, _reap, _start, _wait_for_session  # noqa: E402  (shared server start-up)
from web_support import private_dir  # noqa: E402


@pytest.fixture
def console(tmp_path):
    state = private_dir(tmp_path / "web-console")
    proc = _start(state)
    try:
        yield _wait_for_session(state, proc)
    finally:
        _reap(proc)


@pytest.fixture
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="msedge", headless=True)
        except Exception as exc:  # Edge missing on this machine
            pytest.skip(f"Edge not available: {exc}")
        yield b
        b.close()


_SW_PAGE = b"""<!doctype html><script>
navigator.serviceWorker.register('/sw.js', {scope: '/'}).then(() => navigator.serviceWorker.ready)
  .then(() => { document.title = 'registered'; });
</script>"""
# Answers every request on its origin with a marker, without contacting the server.
_SW_SCRIPT = b"""self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => e.respondWith(new Response('SERVICE-WORKER-MARKER',
  {headers: {'Content-Type': 'text/html'}})));"""


def _register_worker_on_fresh_port(context) -> int:
    """Play the earlier listener: serve a page that installs a service worker on
    127.0.0.1:<port>, let the browser register it, then free the port."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    class Earlier(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body, ctype = (_SW_SCRIPT, "text/javascript") if self.path == "/sw.js" else (_SW_PAGE, "text/html")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    earlier = ThreadingHTTPServer(("127.0.0.1", 0), Earlier)
    threading.Thread(target=earlier.serve_forever, daemon=True).start()
    port = earlier.server_address[1]
    page = context.new_page()
    page.goto(f"http://127.0.0.1:{port}/")
    page.wait_for_function("document.title === 'registered'")
    page.close()
    earlier.shutdown()
    earlier.server_close()
    return port


def test_a_service_worker_left_on_an_old_address_cannot_reach_the_console(browser, tmp_path):
    """codex-lead's R3 on #151. Browser state belongs to an origin, port
    included. A worker registered by whatever held a port earlier intercepts a
    console later started on that same fixed port (the control half of this
    test shows the risk is real). The default, a new port at every start,
    gives the console an origin no earlier listener has had."""
    context = browser.new_context()
    old_port = _register_worker_on_fresh_port(context)

    # Control: a console on the reused fixed port is intercepted before it sees /login.
    fixed = private_dir(tmp_path / "fixed")
    proc = _start(fixed, "--port", str(old_port))  # the later --port wins over _start's 0
    try:
        session = _wait_for_session(fixed, proc)
        assert session["port"] == old_port
        page = context.new_page()
        response = page.goto(_login_url(session))
        assert response.from_service_worker and "SERVICE-WORKER-MARKER" in page.content()
        page.close()
    finally:
        _reap(proc)

    # Default: a new port, so the old worker's origin never matches.
    fresh = private_dir(tmp_path / "fresh")
    (fresh / "console.toml").write_text("")  # the defaults, as a first run gets them
    proc = subprocess.Popen([sys.executable, "-m", "comic_automation.web.server"], cwd=REPO,
                            env={**os.environ, "CBZ_WEB_CONSOLE_HOME": str(fresh)},
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        session = _wait_for_session(fresh, proc)
        assert session["port"] != old_port
        page = context.new_page()
        response = page.goto(_login_url(session))
        assert not response.from_service_worker
        sync_api.expect(page.locator("#who")).to_have_text(f"Signed in: {getpass.getuser()}")
    finally:
        _reap(proc)
    context.close()


def _login_url(session: dict) -> str:
    port = session["port"]
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/bootstrap", method="POST",
                                 headers={"X-Launcher-Secret": session["launcher_secret"]})
    with urllib.request.urlopen(req, timeout=10) as res:
        return f"http://127.0.0.1:{port}/login?code={json.load(res)['code']}"


def test_browser_sign_in_csrf_cross_origin_and_code_reuse(console, browser):
    port = console["port"]
    url = _login_url(console)
    context = browser.new_context()
    page = context.new_page()

    # Bootstrap code -> session cookie -> the shell loads its session.
    page.goto(url)
    assert page.url == f"http://127.0.0.1:{port}/"
    sync_api.expect(page.locator("#who")).to_have_text(f"Signed in: {getpass.getuser()}")
    cookie = next(c for c in context.cookies() if c["name"].startswith("cbz_console_"))
    assert cookie["httpOnly"] and cookie["sameSite"] == "Strict"
    # The cookie is invisible to page script; the browser key left the URL for sessionStorage.
    assert "cbz_console_" not in page.evaluate("document.cookie")
    assert "#" not in page.url and page.evaluate("sessionStorage.getItem('cbz-console-key')")

    # The cookie alone is not a session. A same-origin POST without the CSRF
    # header is refused; with it, it passes every guard and reaches routing
    # (404: no such endpoint in M2).
    statuses = page.evaluate("""async () => {
        const key = {'X-Session-Key': sessionStorage.getItem('cbz-console-key')};
        const post = (headers) => fetch('/api/nothing', {method: 'POST', body: '{}',
            headers: {'Content-Type': 'application/json', ...key, ...headers}}).then(r => r.status);
        const csrf = (await (await fetch('/api/session', {headers: key})).json()).csrf_token;
        const cookieOnly = await fetch('/api/session').then(r => r.status);
        return [cookieOnly, await post({}), await post({'X-CSRF-Token': csrf})];
    }""")
    assert statuses == [401, 403, 404]  # cookie alone; no CSRF; everything

    # A page on another origin cannot read the API or post to it: the browser
    # blocks it (no CORS headers), before the server's own Origin check.
    other = context.new_page()
    other.goto(f"http://localhost:{port}/")
    blocked = other.evaluate(f"""async () => {{
        const results = [];
        for (const init of [{{credentials: 'include'}},
                            {{method: 'POST', credentials: 'include', body: '{{}}',
                              headers: {{'Content-Type': 'application/json'}}}}]) {{
            try {{ await fetch('http://127.0.0.1:{port}/api/session', init); results.push('allowed'); }}
            catch (e) {{ results.push('blocked'); }}
        }}
        return results;
    }}""")
    assert blocked == ["blocked", "blocked"]

    # The same login link a second time is refused.
    response = page.goto(url)
    assert response.status == 401

    # A second launch in the same browser (a new tab) must not lock out the
    # first tab, before or after the first tab reloads.
    first = context.new_page()
    first.goto(_login_url(console))
    sync_api.expect(first.locator("#who")).to_have_text(f"Signed in: {getpass.getuser()}")
    second = context.new_page()
    second.goto(_login_url(console))
    sync_api.expect(second.locator("#who")).to_have_text(f"Signed in: {getpass.getuser()}")
    session_status = """() => fetch('/api/session', {headers: {'X-Session-Key': sessionStorage.getItem('cbz-console-key')}})
                              .then(r => r.status)"""
    assert first.evaluate(session_status) == 200
    first.reload()
    sync_api.expect(first.locator("#who")).to_have_text(f"Signed in: {getpass.getuser()}")
    context.close()
