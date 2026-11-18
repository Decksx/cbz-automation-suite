"""The failure report is bounded and cannot publish fixture credentials."""
import json
import importlib.util
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from browser_evidence import BrowserEvidence, PipeSummary, launch_summary


@pytest.mark.parametrize('url,origin,route', [
    ('http://secret-label.localhost:8123/login?code=SECRET#k=SECRET', 'loopback', '/login'),
    ('http://127.0.0.1:8123/private/SECRET?code=SECRET', 'loopback', 'other'),
    ('https://SECRET.example/static/SECRET.js?token=SECRET', 'other', '/static/*'),
    ('http://[invalid/SECRET', 'other', 'other'),
])
def test_request_evidence_drops_every_untrusted_url_component(url, origin, route):
    evidence = BrowserEvidence()
    request = SimpleNamespace(url=url, method='POST', headers={'secret': 'SECRET'}, post_data='SECRET')
    evidence.record('failed', request, 'net::ERR_NO_BUFFER_SPACE')
    evidence.record('failed', request, 'net::ERR_SECRET')
    evidence.record('response', request, 403)
    result = json.loads(evidence.report())
    assert 'SECRET' not in evidence.report()
    assert result['events'][0]['error'] == 'net::ERR_NO_BUFFER_SPACE'
    assert result['events'][0]['origin'] == origin and result['events'][0]['route'] == route
    assert result['events'][1]['error'] == 'other'
    assert result['events'][2]['status'] == 403
    assert 'headers' not in result['events'][0] and 'post_data' not in result['events'][0]


def test_request_evidence_has_a_bounded_tail_and_observes_every_context_page():
    evidence = BrowserEvidence()
    handlers = {}
    evidence.attach(SimpleNamespace(on=lambda name, callback: handlers.setdefault(name, callback)))
    assert set(handlers) == {'requestfailed', 'response'}
    request = SimpleNamespace(url='http://test.localhost/api/session', method='GET', failure='net::ERR_FAILED')
    handlers['requestfailed'](request)
    for index in range(150):
        handlers['response'](SimpleNamespace(request=request, status=200 + index % 2))
    assert len(evidence.events) == 128
    assert all(row['kind'] == 'response' for row in evidence.events)
    assert evidence.events[-1]['status'] == 201


def test_pipe_evidence_counts_bytes_and_split_errors_without_retaining_output():
    pipe = PipeSummary()
    chunks = [b'login?code=SECRET [WinError 10', b'055] BrokenPipeError SECRET', b'x' * 10000]
    for chunk in chunks:
        pipe.feed(chunk)
    assert pipe.snapshot() == {'bytes': sum(map(len, chunks)), 'winerror_codes': ['10055'],
                               'exception_types': ['BrokenPipeError']}
    assert 'SECRET' not in json.dumps(pipe.snapshot())
    assert len(pipe._tail) <= 64


def test_owned_server_pipes_are_drained_without_publishing_their_contents():
    process = subprocess.Popen([sys.executable, '-c',
        "import sys; sys.stdout.write('SECRET'*20000); sys.stdout.flush(); "
        "sys.stderr.write('SECRET [WinError 10055] BrokenPipeError'); sys.stderr.flush()"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    evidence = BrowserEvidence()
    try:
        evidence.watch_server(process, 1)
        completed = False
        try:
            process.wait(timeout=5)
            completed = True
        except subprocess.TimeoutExpired:
            pass
        assert completed, 'the owned child blocked on a full diagnostic pipe'
        summaries = evidence.servers[0][2]
        deadline = time.monotonic() + 2
        while summaries['stderr'].snapshot()['bytes'] < 40 and time.monotonic() < deadline:
            time.sleep(.01)
        result = json.loads(evidence.report())
        assert result['servers'][0]['stdout']['bytes'] == 120000
        assert result['servers'][0]['stderr']['winerror_codes'] == ['10055']
        assert 'SECRET' not in evidence.report()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()


def test_launch_evidence_uses_effective_switches_without_reporting_the_command():
    args = ['--user-data-dir=SECRET', '--disable-features=HttpsUpgrades,TcpPortRandomizationWin']
    assert launch_summary(args) == {'feature_arguments': 1, 'tcp_randomization_disabled': True,
                                    'https_upgrades_disabled': True, 'web_security_disabled': False}
    assert not launch_summary(['--disable-features=HttpsUpgrades'])['tcp_randomization_disabled']
    assert launch_summary(['--disable-web-security'])['web_security_disabled']
    assert launch_summary(args + ['--disable-features=Other'])['feature_arguments'] == 2


@pytest.mark.parametrize('failed,broken', [(False, False), (True, False), (True, True)])
def test_failure_hook_samples_only_failures_and_preserves_the_report(failed, broken):
    spec = importlib.util.spec_from_file_location('web_failure_hook', Path(__file__).with_name('conftest.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    def collect():
        calls.append('sample')
        if broken:
            raise OSError('SECRET')
        return 'safe evidence'
    report = SimpleNamespace(failed=failed, sections=[])
    item = SimpleNamespace(_browser_evidence=SimpleNamespace(report=collect))
    hook = module.pytest_runtest_makereport(item, None)
    next(hook)
    with pytest.raises(StopIteration):
        hook.send(SimpleNamespace(get_result=lambda: report))
    assert calls == (['sample'] if failed else [])
    assert len(report.sections) == int(failed)
    if failed:
        assert report.sections[0][0] == 'Redacted browser/server evidence'
        assert report.sections[0][1] == ('Evidence collection failed; original test failure is unchanged.'
                                        if broken else 'safe evidence')
    assert 'SECRET' not in repr(report.sections)
