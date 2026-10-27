"""Security layer through the in-process WSGI client (design §9, §12 "Security").

Each test names the guard it exercises; the PR records which tests fail when
each guard is bypassed.
"""

from __future__ import annotations

import pytest

from comic_automation.web.security import BOOTSTRAP_TTL_SECONDS, MAX_BODY_BYTES, SESSION_COOKIE
from web_support import ORIGIN, PORT, call, login, make_app, post_headers


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


# --------------------------------------------------------------- Host check
@pytest.mark.parametrize("host", ["evil.example:8765", "127.0.0.1:9999", "192.168.42.117:8765", "", "127.0.0.1"])
def test_a_foreign_host_gets_421_even_for_static_files(app, host):
    assert call(app, "GET", "/", host=host).status == 421
    assert call(app, "GET", "/api/session", host=host).status == 421


def test_localhost_spelling_is_accepted(app):
    assert call(app, "GET", "/", host=f"localhost:{PORT}").status == 200


# ----------------------------------------------------------- authentication
def test_api_without_a_session_is_401(app):
    for path in ("/api/session", "/api/config", "/api/health", "/api/anything"):
        assert call(app, "GET", path).status == 401, path


def test_static_shell_needs_no_session(app):
    res = call(app, "GET", "/")
    assert res.status == 200 and b"CBZ Console" in res.body
    assert res.headers["content-type"].startswith("text/html")
    assert call(app, "GET", "/static/js/main.js").headers["content-type"].startswith("text/javascript")


def test_bootstrap_needs_the_launcher_secret(app):
    assert call(app, "POST", "/api/bootstrap").status == 401
    assert call(app, "POST", "/api/bootstrap", headers={"X-Launcher-Secret": "wrong"}).status == 401
    ok = call(app, "POST", "/api/bootstrap", headers={"X-Launcher-Secret": app.secrets.launcher_secret})
    assert ok.status == 200 and ok.json["expires_in"] == BOOTSTRAP_TTL_SECONDS


def test_login_sets_a_strict_httponly_cookie_and_redirects(app):
    code = call(app, "POST", "/api/bootstrap", headers={"X-Launcher-Secret": app.secrets.launcher_secret}).json["code"]
    res = call(app, "GET", f"/login?code={code}")
    assert res.status == 303 and res.headers["location"].startswith("/#k=")  # browser key in the fragment
    cookie = res.headers["set-cookie"]
    for flag in ("HttpOnly", "SameSite=Strict", "Path=/"):
        assert flag in cookie
    assert cookie.startswith(SESSION_COOKIE + "=")


def test_a_bootstrap_code_works_once(app):
    code = call(app, "POST", "/api/bootstrap", headers={"X-Launcher-Secret": app.secrets.launcher_secret}).json["code"]
    assert call(app, "GET", f"/login?code={code}").status == 303
    assert call(app, "GET", f"/login?code={code}").status == 401


def test_wrong_and_missing_codes_are_refused(app):
    assert call(app, "GET", "/login?code=nope").status == 401
    assert call(app, "GET", "/login").status == 401


def test_an_expired_code_is_refused(tmp_path):
    now = [1000.0]
    app = make_app(tmp_path, clock=lambda: now[0])
    code = call(app, "POST", "/api/bootstrap", headers={"X-Launcher-Secret": app.secrets.launcher_secret}).json["code"]
    now[0] += BOOTSTRAP_TTL_SECONDS + 1
    assert call(app, "GET", f"/login?code={code}").status == 401


def test_the_launcher_secret_is_not_a_session(app):
    secret = app.secrets.launcher_secret
    assert call(app, "GET", "/api/session", headers={"Cookie": f"{SESSION_COOKIE}={secret}"}).status == 401
    assert call(app, "GET", "/api/session", headers={"X-Launcher-Secret": secret}).status == 401
    # ...except for the health check, which the launcher needs before it has a session.
    assert call(app, "GET", "/api/health", headers={"X-Launcher-Secret": secret}).json["ok"] is True


def test_a_session_is_not_a_launcher_secret(app):
    auth, _ = login(app)
    assert call(app, "POST", "/api/bootstrap", headers=auth).status == 401


def test_session_endpoint_reports_operator_and_csrf(app):
    auth, csrf = login(app)
    body = call(app, "GET", "/api/session", headers=auth).json
    assert body["operator"] == "tester" and body["csrf_token"] == csrf and body["git_sha"] == "abc123"


def test_config_endpoint_reports_sources(app):
    auth, _ = login(app)
    values = call(app, "GET", "/api/config", headers=auth).json["values"]
    assert values["server.port"]["source"] == "default"


# ------------------------------------------------- state-changing requests
# No M2 endpoint changes state, so these POST to a path with no handler: the
# guard must answer before routing, and only a fully valid request reaches 404.
def test_a_fully_valid_post_passes_every_guard(app):
    auth, csrf = login(app)
    assert call(app, "POST", "/api/nothing", headers=post_headers(auth, csrf), body={}).status == 404


def test_post_without_origin_is_403(app):
    auth, csrf = login(app)
    headers = post_headers(auth, csrf)
    del headers["Origin"]
    assert call(app, "POST", "/api/nothing", headers=headers, body={}).json["error"] == "ORIGIN_REFUSED"


@pytest.mark.parametrize("origin", ["http://evil.example", "http://127.0.0.1:9999", "null", "https://127.0.0.1:8765"])
def test_post_with_a_foreign_origin_is_403(app, origin):
    auth, csrf = login(app)
    res = call(app, "POST", "/api/nothing", headers=post_headers(auth, csrf, Origin=origin), body={})
    assert res.status == 403


def test_cross_site_fetch_metadata_is_403(app):
    auth, csrf = login(app)
    res = call(app, "POST", "/api/nothing", headers=post_headers(auth, csrf, **{"Sec-Fetch-Site": "cross-site"}), body={})
    assert res.status == 403


@pytest.mark.parametrize("csrf_header", [None, "", "wrong"])
def test_missing_or_wrong_csrf_is_403(app, csrf_header):
    auth, csrf = login(app)
    headers = post_headers(auth, csrf)
    if csrf_header is None:
        del headers["X-CSRF-Token"]
    else:
        headers["X-CSRF-Token"] = csrf_header
    assert call(app, "POST", "/api/nothing", headers=headers, body={}).json["error"] == "CSRF_REFUSED"


def test_another_sessions_csrf_token_is_refused(app):
    auth, _ = login(app)
    _, other_csrf = login(app)
    assert call(app, "POST", "/api/nothing", headers=post_headers(auth, other_csrf), body={}).status == 403


@pytest.mark.parametrize("ctype", ["application/x-www-form-urlencoded", "multipart/form-data; boundary=x", "text/plain", ""])
def test_non_json_content_types_are_415(app, ctype):
    auth, csrf = login(app)
    res = call(app, "POST", "/api/nothing", headers=post_headers(auth, csrf, **{"Content-Type": ctype}), body="a=1")
    assert res.status == 415


def test_an_oversized_body_is_413(app):
    auth, csrf = login(app)
    res = call(app, "POST", "/api/nothing", headers=post_headers(auth, csrf), body=b"[" + b"0," * MAX_BODY_BYTES + b"0]")
    assert res.status == 413


@pytest.mark.parametrize("field", ["argv", "cmd", "command"])
def test_a_command_field_is_422(app, field):
    auth, csrf = login(app)
    res = call(app, "POST", "/api/nothing", headers=post_headers(auth, csrf), body={field: ["calc.exe"]})
    assert (res.status, res.json["error"]) == (422, "COMMAND_NOT_ACCEPTED")


def test_malformed_json_is_400(app):
    auth, csrf = login(app)
    assert call(app, "POST", "/api/nothing", headers=post_headers(auth, csrf), body="{nope").status == 400


def test_unauthenticated_post_is_401_before_anything_else(app):
    res = call(app, "POST", "/api/nothing", headers={"Origin": ORIGIN, "Content-Type": "application/json"}, body={})
    assert res.status == 401


# ------------------------------------------------------------------ headers
def test_security_headers_on_every_response(app):
    auth, _ = login(app)
    for res in (call(app, "GET", "/"), call(app, "GET", "/api/session", headers=auth),
                call(app, "GET", "/api/session"), call(app, "GET", "/nope")):
        csp = res.headers["content-security-policy"]
        assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "base-uri 'none'" in csp
        assert res.headers["x-content-type-options"] == "nosniff"
        assert res.headers["referrer-policy"] == "no-referrer"


def test_api_responses_are_not_cached(app):
    auth, _ = login(app)
    assert call(app, "GET", "/api/session", headers=auth).headers["cache-control"] == "no-store"
    assert call(app, "GET", "/api/session").headers["cache-control"] == "no-store"


# ------------------------------------------------------------- static files
@pytest.mark.parametrize("path", ["/static/../app.py", "/static/..\\app.py", "/static/js/../../app.py",
                                  "/static/C:/Windows/win.ini", "/static/js",
                                  "/static/nothing.js", "/static/a\0b"])
def test_static_files_cannot_escape_their_folder(app, path):
    assert call(app, "GET", path).status == 404


def test_head_works_for_static_files_without_a_body(app):
    res = call(app, "GET", "/")
    head = call(app, "HEAD", "/")
    assert head.status == 200 and head.body == b"" and head.headers["content-length"] == str(len(res.body))


# ------------------------------------------------- the browser key (2nd half)
def test_the_cookie_alone_is_not_a_session(app):
    """Cookies are not port-scoped: another local listener could replay this one."""
    auth, _ = login(app)
    assert call(app, "GET", "/api/session", headers={"Cookie": auth["Cookie"]}).status == 401
    assert call(app, "GET", "/api/session", headers={"X-Session-Key": auth["X-Session-Key"]}).status == 401


def test_a_browser_key_from_another_session_is_refused(app):
    first, _ = login(app)
    second, _ = login(app)
    mixed = {"Cookie": first["Cookie"], "X-Session-Key": second["X-Session-Key"]}
    assert call(app, "GET", "/api/session", headers=mixed).status == 401


def test_login_clears_old_site_data_and_keeps_the_key_out_of_the_query(app):
    code = call(app, "POST", "/api/bootstrap", headers={"X-Launcher-Secret": app.secrets.launcher_secret}).json["code"]
    res = call(app, "GET", f"/login?code={code}")
    assert res.headers["clear-site-data"] == '"cache", "storage"'
    assert res.headers["location"].startswith("/#k=") and "?" not in res.headers["location"]


# ------------------------------------------------------- launcher hello
def test_hello_proves_the_secret_without_revealing_it(app):
    from comic_automation.web.security import hello_proof

    challenge = "c" * 40
    res = call(app, "GET", "/api/launcher/hello", headers={"X-Launcher-Challenge": challenge})
    assert res.status == 200
    assert res.json == {"proof": hello_proof(app.secrets.launcher_secret, challenge)}
    assert app.secrets.launcher_secret not in res.body.decode()


@pytest.mark.parametrize("challenge", [None, "", "short"])
def test_hello_needs_a_long_challenge(app, challenge):
    headers = {} if challenge is None else {"X-Launcher-Challenge": challenge}
    assert call(app, "GET", "/api/launcher/hello", headers=headers).status == 400


def test_hello_is_still_host_checked(app):
    assert call(app, "GET", "/api/launcher/hello", headers={"X-Launcher-Challenge": "c" * 40},
                host="evil.example:8765").status == 421


# --------------------------------------------------- unusual HTTP methods
@pytest.mark.parametrize("method", ["PROPFIND", "MKCOL", "PATCH", "PURGE"])
def test_methods_outside_get_head_options_get_the_csrf_checks(app, method):
    auth, _ = login(app)
    headers = {**auth, "Content-Type": "application/json"}  # no Origin, no CSRF
    assert call(app, method, "/api/session", headers=headers, body={}).status == 403


def test_redaction_drops_every_query_string():
    from comic_automation.web.security import redact_request_line

    for line in ('"GET /login?code=SECRET HTTP/1.1" 303 0', '"GET /%6Cogin?code=SECRET HTTP/1.1" 303 0',
                 '"GET /LOGIN?code=SECRET HTTP/1.1" 401 0', '"GET /anything?x=SECRET HTTP/1.1" 404 0'):
        assert "SECRET" not in redact_request_line(line)


def test_unknown_api_paths_and_wrong_methods(app):
    auth, _ = login(app)
    assert call(app, "GET", "/api/nope", headers=auth).status == 404
    assert call(app, "GET", "/api/bootstrap", headers=auth).status == 405
