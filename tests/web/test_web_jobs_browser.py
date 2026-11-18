"""Installed-Edge Jobs screen over a disposable authenticated real server."""
import re

import pytest

from comic_automation.web import state_dir as sd
from comic_automation.web.jobs.store import CLAIM_BUDGET_MS, JobStore
from test_web_browser_smoke import browser, _login_url  # noqa: F401
from test_web_job_store import fake_registry
from test_web_server import _start, _wait_for_session, _reap
from web_support import private_dir

sync_api = pytest.importorskip('playwright.sync_api')


@pytest.fixture
def jobs_console(tmp_path):
    root = private_dir(tmp_path/'console')
    state = sd.open_state_dir(root,environ={})
    try:
        clock = [1795000000000]
        store = JobStore(state,verifier=fake_registry,watcher_observation=lambda:False,clock_ms=lambda:clock[0])
        rows = []
        for _ in range(3):
            rows.append(store.admit({}))
            clock[0] += CLAIM_BUDGET_MS
            store.expire_admissions()
    finally:
        state.close()
    process = _start(root)
    try:
        yield _wait_for_session(root,process),rows
    finally:
        _reap(process)


def test_real_job_history_details_keyboard_and_mobile_layout(jobs_console,browser):
    session,rows = jobs_console
    context = browser.new_context(viewport={'width':1280,'height':900})
    page = context.new_page()
    try:
        page.goto(_login_url(session))
        sync_api.expect(page.locator('#jobs-status')).to_have_text('Showing 3 jobs, newest first.')
        assert page.locator('#job-rows small').all_text_contents() == [r['job_id'] for r in rows[::-1]]
        page.locator('#job-rows button').first.focus()
        page.keyboard.press('Enter')
        sync_api.expect(page.locator('#job-detail-heading')).to_be_focused()
        sync_api.expect(page.locator('#job-facts')).to_contain_text(rows[-1]['job_id'])
        sync_api.expect(page.locator('#job-events li')).to_have_count(2)
        sync_api.expect(page.locator('#jobs-older')).to_be_disabled()
        assert page.locator('#jobs button').all_text_contents() == [
            'Refresh latest','Older jobs','fixture','fixture','fixture','Later events']
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        sync_api.expect(page.locator('#job-detail')).to_be_visible()
    finally:
        context.close()


def test_job_pages_loading_errors_and_untrusted_text(jobs_console,browser):
    session,rows = jobs_console
    context = browser.new_context()
    page = context.new_page()
    requests = []
    mode = ['normal']
    held = []
    injection = '<img src=x onerror="window.injected=true">'
    def handle(route):
        request = route.request
        requests.append(request.url.split('/api/jobs',1)[1])
        assert request.headers.get('x-session-key')
        if mode[0] == 'slow':
            held.append(route)
            return
        if mode[0] == 'error':
            return route.fulfill(status=503,json={'error':'JOB_VIEW_UNAVAILABLE','message':'Temporarily unavailable'})
        if '/api/jobs/' in request.url:
            if '?after=' in request.url:
                return route.fulfill(json={'job':dict(rows[-1]),
                    'events':[{'at_ms':1001,'event':'admission_expired'}], 'next':None})
            return route.fulfill(json={'job':dict(rows[-1],tool_id=injection),
                'events':[{'at_ms':1000,'event':injection}], 'next':'9'})
        older = '?before=' in request.url
        data = [dict(rows[0],tool_id='older')] if older else [dict(rows[-1],tool_id=injection)]
        return route.fulfill(json={'jobs':data,'next':None if older else '2'})
    page.route(re.compile(r'.*/api/jobs(?:/.*|\?.*)?$'),handle)
    try:
        page.goto(_login_url(session))
        sync_api.expect(page.locator('#jobs-status')).to_have_text('Showing 1 job, newest first.')
        assert page.locator('#job-rows button').inner_text() == injection
        assert page.locator('#job-rows img').count() == 0
        page.locator('#job-rows button').click()
        sync_api.expect(page.locator('#job-events li')).to_contain_text(injection)
        assert page.locator('#job-events img').count() == 0
        assert page.evaluate('window.injected === undefined')
        page.locator('#job-events-more').click()
        sync_api.expect(page.locator('#job-events li')).to_contain_text('admission expired')
        assert any(request.endswith('?after=9') for request in requests)
        sync_api.expect(page.locator('#job-events-more')).to_be_disabled()
        page.locator('#jobs-older').click()
        sync_api.expect(page.locator('#job-rows button')).to_have_text('older')
        assert '?before=2' in requests
        sync_api.expect(page.locator('#jobs-older')).to_be_disabled()
        mode[0] = 'error'
        page.locator('#jobs-refresh').click()
        sync_api.expect(page.locator('#jobs-notice')).to_contain_text('Temporarily unavailable')
        sync_api.expect(page.locator('#job-rows button')).to_have_text('older')
        sync_api.expect(page.locator('#jobs-refresh')).to_be_enabled()
        mode[0] = 'normal'
        page.locator('#jobs-refresh').click()
        sync_api.expect(page.locator('#jobs-notice')).to_be_hidden()
        assert len(requests) == 6  # initial, detail, later events, older, failed refresh, retry
        mode[0] = 'slow'
        page.locator('#jobs-refresh').click()
        sync_api.expect(page.locator('#jobs')).to_have_attribute('aria-busy','true')
        sync_api.expect(page.locator('#jobs-status')).to_have_text('Loading jobs…')
        sync_api.expect(page.locator('#jobs-refresh')).to_be_disabled()
        # Queued/programmatic events must not create another request while a
        # response is pending, even if an event reaches a disabled control.
        calls = page.evaluate("""() => {
            const fetch = window.fetch;
            let calls = 0;
            window.fetch = (...args) => { calls++; return fetch(...args); };
            document.querySelector('#jobs-refresh').dispatchEvent(new MouseEvent('click'));
            window.fetch = fetch;
            return calls;
        }""")
        assert calls == 0
        assert len(held) == 1
        held.pop().fulfill(json={'jobs':[],'next':None})
        sync_api.expect(page.locator('#jobs-empty')).to_be_visible()
        sync_api.expect(page.locator('#job-rows tr')).to_have_count(0)
        sync_api.expect(page.locator('#jobs-refresh')).to_be_enabled()
        assert len(requests) == 7
    finally:
        context.close()
