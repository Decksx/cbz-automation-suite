"""Bounded read DTOs and security through the actual ConsoleApp dispatch."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from comic_automation.web.jobs import views
from comic_automation.web.jobs.history import JobHistory
from comic_automation.web.jobs.store import CLAIM_BUDGET_MS, JobRefused
from comic_automation.web.jobs.supervisor import Supervisor
from test_web_job_store import ledger  # noqa: F401 (shared private ledger fixture)
from web_support import HOST, call, login, make_app, post_headers


def expire(store, clock, count):
    rows = []
    for _ in range(count):
        rows.append(store.admit({}))
        clock[0] += CLAIM_BUDGET_MS
        store.expire_admissions()
    return rows


def route_app(tmp_path, store):
    app = make_app(tmp_path)
    service = views.JobViews(store)
    app.jobs = SimpleNamespace(read_jobs=service.list, read_job=service.detail)
    return app, login(app)[0]


def test_pages_are_newest_first_and_do_not_duplicate_when_new_jobs_arrive(ledger):
    _, store, clock = ledger
    rows = expire(store, clock, 4)
    reader = views.JobViews(store)
    first = reader.list(limit=2)
    assert [r['job_id'] for r in first['jobs']] == [r['job_id'] for r in rows[::-1][:2]]
    expire(store, clock, 1)
    older = reader.list(before=int(first['next']), limit=2)
    assert [r['job_id'] for r in older['jobs']] == [rows[1]['job_id'],rows[0]['job_id']]
    assert older['next'] is None


def test_empty_and_last_pages_have_no_cursor_and_large_ids_are_exact(ledger):
    _, store, clock = ledger
    reader = views.JobViews(store)
    assert reader.list() == {'jobs':[], 'next':None}
    row, = expire(store, clock, 1)
    with store._home(), store._write() as db:
        db.execute('UPDATE jobs SET admission_seq=? WHERE job_id=?',(2**63-1,row['job_id']))
    assert reader.list()['jobs'][0]['admission_seq'] == str(2**63-1)
    assert reader.list(before=2**63-1)['jobs'] == []


@pytest.mark.parametrize('options', [{'limit':0},{'limit':101},{'limit':True},{'before':0},
                                     {'before':True},{'before':'1'},{'before':2**63}])
def test_direct_list_bounds_refuse_before_storage(ledger, monkeypatch, options):
    _,store,_ = ledger
    monkeypatch.setattr(store,'_home',lambda **kw:pytest.fail('invalid request reached storage'))
    with pytest.raises(JobRefused,match='INVALID_JOB_PAGE'):
        views.JobViews(store).list(**options)


@pytest.mark.parametrize('options',[{'after':-1},{'after':True},{'limit':True},{'limit':1001},{'after':2**63}])
def test_direct_event_bounds_refuse(ledger, options):
    _,store,clock = ledger
    row, = expire(store,clock,1)
    with pytest.raises(JobRefused,match='INVALID_EVENT_PAGE'):
        views.JobViews(store).detail(row['job_id'],**options)


def test_event_pages_and_dtos_keep_private_payloads_out(ledger):
    _, store, clock = ledger
    row, = expire(store, clock, 1)
    secret = r'C:\private\credential=secret'
    with store._home(),store._write() as db:
        db.execute('UPDATE jobs SET outcome=?,tool_id=? WHERE job_id=?',(secret,secret,row['job_id']))
        store._event(db,row,'unknown-'+secret,secret,clock[0],{'argv':[secret],'note':secret})
    reader = views.JobViews(store)
    page = reader.detail(row['job_id'],limit=1)
    assert [event['event'] for event in page['events']] == ['admitted']
    second = reader.detail(row['job_id'],after=int(page['next']),limit=2)
    assert [event['event'] for event in second['events']] == ['admission_expired','other']
    assert second['next'] is None
    assert second['job']['tool_id'] == 'unknown' and second['job']['outcome'] is None
    encoded = json.dumps([reader.list(),page,second])
    for forbidden in (secret,'seal_json','command_digest','details_json','runner_created_at','actor'):
        assert forbidden not in encoded


def test_detail_rejects_a_concurrent_wal_commit(ledger, monkeypatch):
    _,store,clock = ledger
    row, = expire(store,clock,1)
    original = views._summary
    def concurrent(record):
        with sqlite3.connect(store.database) as db:
            db.execute('UPDATE jobs SET updated_ms=updated_ms+1 WHERE job_id=?',(row['job_id'],))
        return original(record)
    monkeypatch.setattr(views,'_summary',concurrent)
    with pytest.raises(JobRefused,match='JOB_VIEW_CHANGED'):
        views.JobViews(store).detail(row['job_id'])


def test_cold_jobs_keep_summary_and_events_without_opening_commands(ledger,monkeypatch):
    state,store,clock = ledger
    row, = expire(store,clock,1)
    history = JobHistory(store)
    history.initialize()
    assert history.archive(keep=0,age_ms=0)
    assert not (state.path/'jobs'/row['job_id']).exists()
    monkeypatch.setattr(JobHistory,'read_command',lambda *args:pytest.fail('read DTO opened a command'))
    reader = views.JobViews(store)
    assert reader.list()['jobs'][0]['job_id'] == row['job_id']
    assert [e['event'] for e in reader.detail(row['job_id'])['events']] == [
        'admitted','admission_expired','archived','archive_cleanup']


def test_supervisor_read_owns_the_operation_and_closing_refuses(ledger):
    state,store,clock = ledger
    row, = expire(store,clock,1)
    supervisor = Supervisor(state)
    assert supervisor.read_jobs()['jobs'][0]['job_id'] == row['job_id']
    assert supervisor.read_job(row['job_id'])['job']['nonce'] == row['nonce']
    supervisor.close()
    with pytest.raises(JobRefused,match='SUPERVISOR_NOT_READY'):
        supervisor.read_jobs()


def test_routes_have_auth_host_method_and_no_write_side_effects(ledger,tmp_path):
    _,store,clock = ledger
    row, = expire(store,clock,1)
    before = store.get(row['job_id']),store.events(row['job_id'])
    app,auth = route_app(tmp_path,store)
    for path in ('/api/jobs',f"/api/jobs/{row['job_id']}"):
        assert call(app,'GET',path).status == 401
        assert call(app,'GET',path,headers=auth,host='evil.example').status == 421
        assert call(app,'POST',path,headers=auth,body={}).status == 403
        csrf = call(app,'GET','/api/session',headers=auth).json['csrf_token']
        assert call(app,'POST',path,headers=post_headers(auth,csrf),body={}).status == 405
        response = call(app,'GET',path,headers=auth)
        assert response.status == 200 and response.headers['cache-control'] == 'no-store'
        head = call(app,'HEAD',path,headers=auth)
        assert head.status == 200 and head.body == b''
        assert head.headers['content-length'] == response.headers['content-length']
    assert before == (store.get(row['job_id']),store.events(row['job_id']))
    assert call(app,'GET',f"/api/jobs/{row['job_id']}/stop",headers=auth).status == 405


@pytest.mark.parametrize('query',['limit=0','limit=101','limit=','limit=01','limit=-1','limit=true',
                                 'limit=1&limit=2','before=0','before=9223372036854775808',
                                 'unknown=1','after=1'])
def test_http_bad_job_queries_are_400(ledger,tmp_path,query):
    _,store,_ = ledger
    app,auth = route_app(tmp_path,store)
    assert call(app,'GET','/api/jobs?'+query,headers=auth).status == 400


def test_not_found_and_storage_errors_are_redacted(ledger,tmp_path,monkeypatch):
    _,store,_ = ledger
    app,auth = route_app(tmp_path,store)
    assert call(app,'GET','/api/jobs/'+'0'*26,headers=auth).status == 404
    assert call(app,'GET','/api/jobs/not-a-job',headers=auth).status == 400
    assert call(app,'GET','/api/jobs/'+'0'*26+'?limit=1001',headers=auth).status == 400
    def fail(**kw):
        raise OSError(r'C:\private\password')
    monkeypatch.setattr(app.jobs,'read_jobs',fail)
    response = call(app,'GET','/api/jobs',headers=auth)
    assert response.status == 503 and 'private' not in response.body.decode()
    app.jobs = None
    assert call(app,'GET','/api/jobs',headers=auth).status == 503
