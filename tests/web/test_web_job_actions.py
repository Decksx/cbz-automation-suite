"""Direct and protected HTTP operator intent over disposable private ledgers."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import threading
from types import SimpleNamespace

import pytest

from comic_automation.web.jobs import actions, recovery, views, win32
from comic_automation.web.jobs.history import JobHistory
from comic_automation.web.jobs.store import CLAIM_BUDGET_MS, JobRefused, ProcessIdentity
from comic_automation.web.jobs.supervisor import Supervisor
from comic_automation.web import state_dir as sd
from test_web_job_store import ledger, RUNNER  # noqa: F401
from test_web_recovery import dead, running, unrecorded  # noqa: F401
from web_support import call, login, make_app, post_headers


def request_for(row, note=False):
    """Echo only identity/version plus an optional operator note."""
    result = {'nonce': row['nonce'], 'state_version': row['state_version']}
    if note:
        result['note'] = 'Accept an unknown outcome after fresh proof.'
    return result


def app_for(tmp_path, state, store):
    """Keep the real supervisor lifetime guard with the fixture's stable clock."""
    app = make_app(tmp_path)
    app.jobs = Supervisor(state)
    app.jobs.store = store
    auth, csrf = login(app)
    return app, auth, post_headers(auth, csrf)


def test_admitted_stop_is_a_terminal_refusal_and_a_runner_cannot_claim_it(ledger):
    state, store, _ = ledger
    row = store.admit({})
    result = actions.JobActions(store).stop(row['job_id'], row['nonce'], 1, actor='David')
    assert result['job']['state'] == 'refused' and result['job']['state_version'] == 2
    assert result['job']['outcome'] is None  # the internal refusal reason stays private
    stored = store.get(row['job_id'])
    assert stored['outcome'] == 'OPERATOR_STOP_BEFORE_TOOL'
    assert stored['runner_pid'] is None and stored['child_pid'] is None
    assert stored['stop_requested_ms'] is None
    assert not (state.path/'jobs'/row['job_id']/'stop.request').exists()
    event = store.events(row['job_id'])[-1]
    assert event['event'] == 'operator_stop' and event['actor'] == 'David' and event['state_version'] == 2
    with pytest.raises(JobRefused, match='CLAIM_FENCED'):
        store.claim(row['job_id'], row['nonce'], RUNNER)
    assert store.admit({})['job_id'] != row['job_id']


def test_stop_and_runner_claim_compete_for_one_admitted_generation(ledger):
    _, store, _ = ledger
    row = store.admit({})
    gate = threading.Barrier(2)
    def stop():
        gate.wait(timeout=5)
        try:
            return actions.JobActions(store).stop(row['job_id'], row['nonce'], 1, actor='operator')
        except JobRefused as exc:
            return str(exc)
    def claim():
        gate.wait(timeout=5)
        try:
            return store.claim(row['job_id'], row['nonce'], RUNNER)
        except JobRefused as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        stopping, claiming = pool.submit(stop), pool.submit(claim)
        stopped, claimed = stopping.result(timeout=10), claiming.result(timeout=10)
    try:
        final = store.get(row['job_id'])
        assert final['state_version'] == 2
        if final['state'] == 'refused':
            assert stopped['job']['state'] == 'refused' and claimed == 'CLAIM_FENCED'
        else:
            assert final['state'] == 'claimed' and stopped == 'ACTION_FENCED'
            assert claimed.job_id == row['job_id']
    finally:
        if not isinstance(claimed, str):
            claimed.close()


@pytest.mark.parametrize('state', ['admission_expired', 'refused', 'succeeded', 'failed', 'stopped', 'lost'])
def test_terminal_stop_is_idempotent_with_a_fresh_fence_and_no_new_event(ledger, state):
    _, store, _ = ledger
    row = store.admit({})
    with store._home(), store._write() as db:
        db.execute('UPDATE jobs SET state=? WHERE job_id=?', (state, row['job_id']))
    before = store.get(row['job_id']), store.events(row['job_id'])
    result = actions.JobActions(store).stop(row['job_id'], row['nonce'], 1, actor='operator')
    assert result['job']['state'] == state
    assert before == (store.get(row['job_id']), store.events(row['job_id']))


@pytest.mark.parametrize('state', ['claimed', 'running', 'uncertain'])
def test_current_stop_policy_has_no_intent_file_or_native_effect(ledger, state, monkeypatch):
    root, store, _ = ledger
    row = store.admit({})
    if state == 'claimed':
        store.claim(row['job_id'], row['nonce'], RUNNER).close()
    else:
        store.claim(row['job_id'], row['nonce'], RUNNER).close()
        if state == 'running':
            with store._home(), store._write() as db:
                db.execute("UPDATE jobs SET state='running' WHERE job_id=?", (row['job_id'],))
        else:
            store.hold_uncertain(row['job_id'], row['nonce'], 2, actor='fixture', reason='unknown logs')
    row = store.get(row['job_id'])
    before = row, store.events(row['job_id'])
    monkeypatch.setattr(win32, 'observe_process', lambda *a: pytest.fail('stop queried a native process'))
    monkeypatch.setattr(recovery, 'recheck', lambda *a, **kw: pytest.fail('stop performed recovery'))
    with pytest.raises(actions.NotStoppable) as refused:
        actions.JobActions(store).stop(row['job_id'], row['nonce'], row['state_version'], actor='operator')
    assert refused.value.reason == ('RECHECK_REQUIRED' if state == 'uncertain' else 'WAIT_ONLY')
    assert before == (store.get(row['job_id']), store.events(row['job_id']))
    assert not (root.path/'jobs'/row['job_id']/'stop.request').exists()


@pytest.mark.parametrize('method', ['stop', 'recheck', 'acknowledge'])
@pytest.mark.parametrize('change', ['nonce', 'version'])
def test_every_direct_mutation_fences_a_stale_generation(ledger, method, change):
    _, store, _ = ledger
    row = store.admit({})
    arguments = request_for(row, method != 'stop')
    arguments['version'] = arguments.pop('state_version')
    arguments[change] = 'f'*32 if change == 'nonce' else 2
    before = store.get(row['job_id']), store.events(row['job_id'])
    with pytest.raises(JobRefused, match='FENCED'):
        getattr(actions.JobActions(store), method)(row['job_id'], actor='operator', **arguments)
    assert before == (store.get(row['job_id']), store.events(row['job_id']))


@pytest.mark.parametrize('field,value', [('nonce', ''), ('nonce', 'F'*32), ('version', True),
    ('version', '1'), ('version', 0), ('version', 2**63), ('actor', ''), ('actor', ' '*2),
    ('actor', 'x'*257), ('actor', '\0'), ('actor', '\ud800')])
def test_bad_direct_action_refuses_before_storage(ledger, monkeypatch, field, value):
    _, store, _ = ledger
    monkeypatch.setattr(store, '_home', lambda **kw: pytest.fail('invalid action reached storage'))
    args = {'nonce': 'a'*32, 'version': 1, 'actor': 'operator'}
    args[field] = value
    with pytest.raises(JobRefused, match='INVALID_JOB_ACTION'):
        actions.JobActions(store).stop('0'*26, **args)


@pytest.mark.parametrize('method', ['recheck', 'acknowledge'])
@pytest.mark.parametrize('note', ['', ' ', None, True, 'x'*4097, '\0', '\ud800'])
def test_note_is_required_bounded_and_encodable_before_storage(ledger, monkeypatch, method, note):
    _, store, _ = ledger
    monkeypatch.setattr(store, '_home', lambda **kw: pytest.fail('invalid note reached storage'))
    with pytest.raises(JobRefused, match='INVALID_JOB_ACTION'):
        getattr(actions.JobActions(store), method)('0'*26, 'a'*32, 1, actor='operator', note=note)


def test_acknowledgement_is_private_note_only_and_never_releases_uncertainty(ledger):
    root, store, _ = ledger
    row = unrecorded(store)
    note = r'C:\private\credential=<script>secret</script>'
    result = actions.JobActions(store).acknowledge(row['job_id'], row['nonce'], 3, actor='David', note=note)
    assert store.get(row['job_id']) == row
    assert result['job']['state'] == 'uncertain' and result['job']['state_version'] == 3
    assert 'private' not in json.dumps(result) and 'David' not in json.dumps(result)
    event = store.events(row['job_id'])[-1]
    assert event['actor'] == 'David' and event['event'] == 'acknowledged'
    assert json.loads(event['details_json']) == {'note': note}
    assert views.JobViews(store).detail(row['job_id'])['events'][-1]['event'] == 'acknowledged'
    assert note not in json.dumps(views.JobViews(store).detail(row['job_id']))
    assert not (root.path/'jobs'/row['job_id']/'stop.request').exists()
    with pytest.raises(JobRefused):
        store.admit({})


def test_cold_history_acknowledgement_does_not_restore_a_job_directory(ledger):
    root, store, clock = ledger
    row = store.admit({})
    clock[0] += CLAIM_BUDGET_MS
    store.expire_admissions()
    row = store.get(row['job_id'])
    history = JobHistory(store)
    history.initialize()
    history.archive(keep=0, age_ms=0)
    assert not (root.path/'jobs'/row['job_id']).exists()
    actions.JobActions(store).acknowledge(row['job_id'], row['nonce'], row['state_version'],
                                         actor='operator', note='Reviewed historical result.')
    assert not (root.path/'jobs'/row['job_id']).exists()
    assert store.events(row['job_id'])[-1]['event'] == 'acknowledged'


@pytest.mark.parametrize('proof,expected', [('absent', 'lost'), ('present', 'uncertain'), ('unknown', 'uncertain')])
def test_recheck_uses_fresh_proof_and_fences_replay(ledger, dead, monkeypatch, proof, expected):
    _, store, _ = ledger
    row = unrecorded(store)
    calls = []
    def observe(*args):
        calls.append(proof)
        return win32.JobObservation(proof, 0 if proof == 'present' else None, error=5)
    monkeypatch.setattr(win32, 'observe_job', observe)
    result = actions.JobActions(store).recheck(row['job_id'], row['nonce'], 3, actor='David', note='Accept unknown outcome.')
    assert result['job']['state'] == expected and result['job']['state_version'] == 4
    assert result['job']['outcome'] == ('unknown' if expected == 'lost' else None)
    assert calls == [proof]
    event = store.events(row['job_id'])[-1]
    assert event['actor'] == 'David' and 'evidence' in json.loads(event['details_json'])
    assert 'evidence' not in json.dumps(result)
    with pytest.raises(JobRefused, match='FENCED'):
        actions.JobActions(store).recheck(row['job_id'], row['nonce'], 3, actor='David', note='Replay.')
    assert calls == [proof]


@pytest.mark.parametrize('method', ['stop_job', 'recheck_job', 'acknowledge_job'])
def test_supervisor_counts_action_lifetime_and_refuses_after_close(ledger, monkeypatch, method):
    state, store, _ = ledger
    supervisor = Supervisor(state)
    supervisor.store = store
    name = method.removesuffix('_job')
    def counted(self, *args, **kwargs):
        assert supervisor._active == 1
        assert supervisor.close(timeout=0) is False
        return {'counted': True}
    monkeypatch.setattr(actions.JobActions, name, counted)
    kwargs = {'actor': 'operator'} | ({'note': 'Reviewed.'} if name != 'stop' else {})
    assert getattr(supervisor, method)('0'*26, 'a'*32, 1, **kwargs) == {'counted': True}
    assert supervisor.wait_closed() is None
    with pytest.raises(JobRefused, match='SUPERVISOR_NOT_READY'):
        getattr(supervisor, method)('0'*26, 'a'*32, 1, **kwargs)


@pytest.mark.parametrize('action', ['stop', 'recheck', 'acknowledge'])
def test_action_http_auth_method_and_query_gates_precede_service(ledger, tmp_path, action):
    state, store, _ = ledger
    row = store.admit({})
    app, auth, protected = app_for(tmp_path, state, store)
    path = f"/api/jobs/{row['job_id']}/{action}"
    before = store.get(row['job_id']), store.events(row['job_id'])
    body = request_for(row, action != 'stop')
    assert call(app, 'POST', path, body=body).status == 401
    assert call(app, 'POST', path, host='evil.example', headers=protected, body=body).status == 421
    assert call(app, 'POST', path, headers=auth, body=body).status == 403
    assert call(app, 'POST', path, headers={**protected, 'Origin': 'http://evil.example'}, body=body).status == 403
    for method in ('GET', 'HEAD', 'OPTIONS'):
        assert call(app, method, path, headers=auth).status == 405
    assert call(app, 'POST', path+'?unused=', headers=protected, body=body).status == 400
    assert before == (store.get(row['job_id']), store.events(row['job_id']))


@pytest.mark.parametrize('body', [None, [], True, {}, {'nonce': 'a'*32},
    {'nonce': 'a'*32, 'state_version': True}, {'nonce': 'a'*32, 'state_version': '1'},
    {'nonce': 'a'*32, 'state_version': 1, 'actor': 'forged'},
    {'nonce': 'a'*32, 'state_version': 1, 'state': 'succeeded'}])
def test_strict_action_body_rejects_missing_extra_and_alias_fields(ledger, tmp_path, body):
    state, store, _ = ledger
    row = store.admit({})
    app, _, protected = app_for(tmp_path, state, store)
    result = call(app, 'POST', f"/api/jobs/{row['job_id']}/stop", headers=protected, body=json.dumps(body))
    assert result.status == 400
    assert store.get(row['job_id']) == row and len(store.events(row['job_id'])) == 1


def test_http_uses_session_actor_and_returns_only_safe_summary(ledger, tmp_path):
    state, store, _ = ledger
    row = store.admit({})
    app, auth, protected = app_for(tmp_path, state, store)
    path = f"/api/jobs/{row['job_id']}"
    reply = call(app, 'POST', path+'/acknowledge', headers=protected,
                 body={**request_for(row), 'note': 'private note'})
    assert reply.status == 200 and store.events(row['job_id'])[-1]['actor'] == 'tester'
    assert 'private' not in reply.body.decode() and 'seal' not in reply.body.decode()
    assert call(app, 'POST', path+'/stop', headers=protected, body=request_for(row)).status == 200
    assert store.events(row['job_id'])[-1]['actor'] == 'tester'
    assert call(app, 'POST', path+'/stop', headers=protected, body=request_for(row)).json['error'] == 'STALE'
    current = store.get(row['job_id'])
    assert call(app, 'POST', path+'/recheck', headers=protected,
                body=request_for(current, True)).json['error'] == 'INVALID_STATE'
    assert call(app, 'GET', path, headers=auth).json['events'][-1]['event'] == 'operator_stop'


def test_action_http_missing_policy_and_unavailable_errors_are_redacted(ledger, tmp_path, monkeypatch):
    state, store, _ = ledger
    row = running(store)
    app, _, protected = app_for(tmp_path, state, store)
    path = f"/api/jobs/{row['job_id']}/stop"
    result = call(app, 'POST', path, headers=protected, body=request_for(row))
    assert result.status == 409 and result.json['error'] == 'NOT_STOPPABLE'
    assert result.json['details'] == {'reason': 'WAIT_ONLY'}
    def fail(*args, **kwargs):
        raise OSError(r'C:\private\password=secret')
    monkeypatch.setattr(app.jobs, 'stop_job', fail)
    result = call(app, 'POST', path, headers=protected, body=request_for(row))
    assert result.status == 503 and 'private' not in result.body.decode()
    app.jobs = SimpleNamespace()
    assert call(app, 'POST', path, headers=protected, body=request_for(row)).status == 503


def test_missing_and_invalid_ids_have_safe_http_errors(ledger, tmp_path):
    state, store, _ = ledger
    app, _, protected = app_for(tmp_path, state, store)
    for job_id, expected in [('0'*26, 404), ('not-a-job', 400)]:
        assert call(app, 'POST', f'/api/jobs/{job_id}/stop', headers=protected,
                    body={'nonce': 'a'*32, 'state_version': 1}).status == expected


@pytest.mark.parametrize('raw', ['{"nonce":"a","nonce":"b","state_version":1}',
                               '{"nonce":"a","state_version":1,"state_version":2}'])
def test_duplicate_json_fields_refuse_before_service(ledger, tmp_path, raw):
    state, store, _ = ledger
    row = store.admit({})
    app, _, protected = app_for(tmp_path, state, store)
    reply = call(app, 'POST', f"/api/jobs/{row['job_id']}/stop", headers=protected, body=raw)
    assert reply.status == 400 and reply.json['error'] == 'BAD_JSON'
    assert store.get(row['job_id']) == row and len(store.events(row['job_id'])) == 1


def test_kind_and_state_display_labels_are_fixed_sets_even_for_a_corrupt_row(ledger):
    _, store, _ = ledger
    row = store.admit({})
    with store._home(), store._write() as db:
        db.execute('PRAGMA ignore_check_constraints=ON')
        db.execute('UPDATE jobs SET kind=?,state=? WHERE job_id=?',
                   ('private-kind-path', 'private-state-path', row['job_id']))
    result = views.JobViews(store).detail(row['job_id'])
    assert result['job']['kind'] == result['job']['state'] == 'unknown'
    assert 'private-' not in json.dumps(result)


@pytest.mark.parametrize('method', ['stop', 'acknowledge'])
def test_decision_and_event_own_admission_exclusion(ledger, monkeypatch, method):
    state, store, _ = ledger
    row = store.admit({})
    original = actions._row
    seen = []
    def guarded(*args):
        with open(state.path/'locks'/'admission.lock', 'a+b') as contender:
            acquired = sd._try_lock(contender)
            if acquired:
                sd._unlock(contender)
            assert not acquired, 'operator decision did not own admission exclusion'
        seen.append('owned')
        return original(*args)
    monkeypatch.setattr(actions, '_row', guarded)
    kwargs = {'actor': 'operator'} | ({'note': 'Reviewed.'} if method != 'stop' else {})
    getattr(actions.JobActions(store), method)(row['job_id'], row['nonce'], 1, **kwargs)
    assert seen == ['owned']


@pytest.mark.parametrize('method', ['stop', 'acknowledge'])
def test_event_failure_rolls_back_the_whole_action(ledger, monkeypatch, method):
    _, store, _ = ledger
    row = store.admit({})
    before = store.get(row['job_id']), store.events(row['job_id'])
    original = store._event
    def interrupted(*args):
        original(*args)
        raise OSError('fixture event failure')
    monkeypatch.setattr(store, '_event', interrupted)
    kwargs = {'actor': 'operator'} | ({'note': 'Reviewed.'} if method != 'stop' else {})
    with pytest.raises(OSError, match='fixture event failure'):
        getattr(actions.JobActions(store), method)(row['job_id'], row['nonce'], 1, **kwargs)
    assert before == (store.get(row['job_id']), store.events(row['job_id']))


@pytest.mark.parametrize('proof,expected', [('absent', 'lost'), ('present', 'uncertain'), ('unknown', 'uncertain')])
def test_protected_http_recheck_uses_current_session_and_keeps_proof_private(ledger, tmp_path, dead, monkeypatch, proof, expected):
    state, store, _ = ledger
    row = unrecorded(store)
    monkeypatch.setattr(win32, 'observe_job', lambda *a: win32.JobObservation(proof, error=5))
    app, _, protected = app_for(tmp_path, state, store)
    reply = call(app, 'POST', f"/api/jobs/{row['job_id']}/recheck", headers=protected,
                 body=request_for(row, True))
    assert reply.status == 200 and reply.json['job']['state'] == expected
    assert reply.json['job']['state_version'] == row['state_version']+1
    event = store.events(row['job_id'])[-1]
    assert event['actor'] == 'tester' and event['event'] == 'operator_recheck'
    for private in ('evidence', 'runner_pid', 'job_object_name', 'note', 'tester'):
        assert private not in reply.body.decode()


def test_boundary_note_and_actor_sizes_are_accepted_without_touching_job_state(ledger):
    _, store, _ = ledger
    row = store.admit({})
    actions.JobActions(store).acknowledge(row['job_id'], row['nonce'], 1, actor='a'*256, note='n'*4096)
    assert store.get(row['job_id']) == row
    assert json.loads(store.events(row['job_id'])[-1]['details_json'])['note'] == 'n'*4096


@pytest.mark.parametrize('method', ['stop', 'recheck', 'acknowledge'])
@pytest.mark.parametrize('job_id', ['../escape', '', '8'+'0'*25])
def test_direct_action_validates_canonical_id_before_any_path(ledger, monkeypatch, method, job_id):
    _, store, _ = ledger
    monkeypatch.setattr(store, '_home', lambda **kw: pytest.fail('invalid ID reached storage'))
    kwargs = {'actor': 'operator'} | ({'note': 'Reviewed.'} if method != 'stop' else {})
    with pytest.raises(JobRefused, match='INVALID_JOB_ID'):
        getattr(actions.JobActions(store), method)(job_id, 'a'*32, 1, **kwargs)


@pytest.mark.skipif(os.name != 'nt', reason='Windows session namespace proof')
def test_http_recheck_cannot_treat_another_sessions_job_absence_as_proof(ledger, tmp_path):
    state, store, _ = ledger
    current = win32.current_identity()
    row = unrecorded(store, ProcessIdentity(current.pid, 1, current.session_id+1))
    app, _, protected = app_for(tmp_path, state, store)
    reply = call(app, 'POST', f"/api/jobs/{row['job_id']}/recheck", headers=protected,
                 body=request_for(row, True))
    assert reply.status == 200 and reply.json['job']['state'] == 'uncertain'
    detail = json.loads(store.events(row['job_id'])[-1]['details_json'])
    assert detail['evidence']['job']['state'] == 'unknown'
    with pytest.raises(JobRefused):
        store.admit({})
