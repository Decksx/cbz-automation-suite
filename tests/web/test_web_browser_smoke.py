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
import urllib.request

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from test_web_server import _reap, _start, _wait_for_session  # noqa: E402  (shared server start-up)
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
    cookie = next(c for c in context.cookies() if c["name"] == "cbz_console_session")
    assert cookie["httpOnly"] and cookie["sameSite"] == "Strict"
    # The cookie is invisible to page script; the browser key left the URL for sessionStorage.
    assert "cbz_console_session" not in page.evaluate("document.cookie")
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
    context.close()
