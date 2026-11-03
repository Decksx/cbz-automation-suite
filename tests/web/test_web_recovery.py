"""Recovery guard witnesses; native crash tests use only disposable private homes."""
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from comic_automation.database import dal
from comic_automation.web.jobs import recovery as rc, receipts, runner as r, win32 as w
from comic_automation.web.jobs.store import JobRefused, ProcessIdentity, _json
from test_web_job_store import ledger, RUNNER
from test_web_runner_receipts import finished, map_exit, publish
from test_web_runner import fixture, until


@pytest.fixture
def dead(monkeypatch):
    """Isolate ledger/receipt guards with typed native absence, not production proof."""
    monkeypatch.setattr(w, "observe_process", lambda identity: w.ProcessObservation("gone", identity))
    monkeypatch.setattr(w, "observe_job", lambda *a: w.JobObservation("absent", error=2))


def running(store):
    """Prepare a fake dead-owner row; typed native outcomes are injected separately."""
    row = store.admit({})
    claim = store.claim(row["job_id"], row["nonce"], RUNNER)
    try:
        return store.mark_running(claim, 2, ProcessIdentity(456, RUNNER.created_at + 1, RUNNER.session_id))
    finally:
        claim.close()


@pytest.mark.parametrize("runner,child,job,expected", [
    ("alive", "gone", "absent", "running"), ("gone", "gone", "absent", "lost"),
    ("unknown", "gone", "absent", "uncertain"), ("gone", "alive", "absent", "uncertain"),
    ("gone", "unknown", "absent", "uncertain"), ("gone", "gone", "present", "uncertain"),
    ("gone", "gone", "unknown", "uncertain")])
def test_recovery_matrix_retains_admission_without_complete_absence(ledger, monkeypatch, runner, child, job, expected):
    _, store, _ = ledger
    row = running(store)
    monkeypatch.setattr(w, "observe_process", lambda i: w.ProcessObservation(runner if i.pid == RUNNER.pid else child, i, 5))
    monkeypatch.setattr(w, "observe_job", lambda *a: w.JobObservation(job, 0 if job == "present" else None, 5))
    result = rc.reconcile(store, row["job_id"], row["nonce"], 3)
    assert result["state"] == expected
    if expected in ("running", "uncertain"):
        with pytest.raises(JobRefused): store.admit({})
    else:
        assert result["outcome"] == "unknown" and result["exit_code"] is None
        assert store.admit({})["job_id"] != row["job_id"]
    if expected == "uncertain":
        detail = json.loads(store.events(row["job_id"])[-1]["details_json"])
        assert detail["evidence"]["job"]["state"] == job


def test_prebirth_dead_claim_is_lost_without_inventing_a_child(ledger, dead):
    _, store, _ = ledger
    row = store.admit({})
    store.claim(row["job_id"], row["nonce"], RUNNER).close()
    assert rc.reconcile(store, row["job_id"], row["nonce"], 2)["state"] == "lost"


def test_pid_reuse_does_not_replace_descendant_proof(ledger, dead, monkeypatch):
    _, store, _ = ledger
    row = running(store)
    monkeypatch.setattr(w, "observe_process", lambda i: w.ProcessObservation("gone", dataclasses.replace(i, created_at=i.created_at+1)))
    monkeypatch.setattr(w, "observe_job", lambda *a: w.JobObservation("present", 0))
    evidence = rc.observe(row)
    assert evidence["runner"]["identity"]["created_at"] == str(RUNNER.created_at+1)
    assert rc.reconcile(store, row["job_id"], row["nonce"], 3)["state"] == "uncertain"


def test_native_query_exception_is_unknown_and_recorded(ledger, dead, monkeypatch):
    _, store, _ = ledger
    row = running(store)
    def denied(*a): raise PermissionError("fixture denial")
    monkeypatch.setattr(w, "observe_process", denied)
    result = rc.reconcile(store, row["job_id"], row["nonce"], 3)
    assert result["state"] == "uncertain"
    assert "PermissionError" in store.events(row["job_id"])[-1]["details_json"]


@pytest.mark.parametrize("change", ["nonce", "version", "bool_version"])
def test_recovery_and_recheck_fence_stale_requests(ledger, dead, change):
    _, store, _ = ledger
    row = running(store)
    nonce, version = ("f"*32 if change == "nonce" else row["nonce"]), (True if change == "bool_version" else 2 if change == "version" else 3)
    with pytest.raises(JobRefused, match="FENCED"):
        rc.reconcile(store, row["job_id"], nonce, version)
    store.hold_uncertain(row["job_id"], row["nonce"], 3, actor="fixture", reason="log unknown")
    with pytest.raises(JobRefused, match="FENCED"):
        rc.recheck(store, row["job_id"], nonce, version, actor="operator", note="acknowledge missing tail")


def test_orphan_temp_is_explicit_and_never_promoted_or_removed(ledger, dead):
    _, store, _ = ledger
    row = running(store)
    path = store.path / "jobs" / row["job_id"] / "exit.json.tmp"
    path.write_bytes(b"partial receipt")
    assert rc.reconcile(store, row["job_id"], row["nonce"], 3)["state"] == "uncertain"
    assert "ORPHAN_EXIT_RECEIPT_TEMP" in store.events(row["job_id"])[-1]["details_json"]
    assert path.read_bytes() == b"partial receipt" and not path.with_suffix("").exists()


def test_publisher_refuses_orphan_temp_with_specific_reason(finished):
    store, claim, _, _, _ = finished
    path = store.path / "jobs" / claim.job_id / "exit.json.tmp"
    path.write_bytes(b"partial receipt")
    with pytest.raises(JobRefused, match="ORPHAN_EXIT_RECEIPT_TEMP"): publish(finished)
    assert path.read_bytes() == b"partial receipt" and store.get(claim.job_id)["state"] == "running"


def test_dead_owner_receipt_import_is_idempotent_and_attributed(finished, dead):
    store, claim, _, _, _ = finished
    publish(finished)
    result = rc.reconcile(store, claim.job_id, claim.nonce, 3, outcome_mapper=map_exit)
    assert result["state"] == "succeeded" and result["exit_code"] == 0
    assert rc.reconcile(store, claim.job_id, claim.nonce, 3, outcome_mapper=map_exit) == result
    events = store.events(claim.job_id)
    assert [e["event"] for e in events].count("recovered_receipt") == 1
    assert events[-1]["actor"] == "reconciler"
    assert json.loads(events[-1]["details_json"])["receipt_sha256"]


@pytest.mark.parametrize("bad", ["malformed", "duplicate", "oversized", "nonce", "runner", "child", "job", "exit_bool", "ended_bool", "future", "stale", "state", "extra", "stop", "logs", "index", "index_hash", "incomplete_index", "segment", "command", "mapper", "temp"])
def test_untrusted_receipt_or_missing_durable_bytes_cannot_release(finished, dead, bad):
    store, claim, _, _, _ = finished
    publish(finished)
    root = store.path / "jobs" / claim.job_id
    doc = json.loads((root / "exit.json").read_bytes())
    mapper = map_exit
    if bad == "nonce": doc["nonce"] = "f"*32
    elif bad == "runner": doc["runner"]["created_at"] = str(int(doc["runner"]["created_at"])+1)
    elif bad == "child": doc["child"]["pid"] += 1
    elif bad == "job": doc["job_object_name"] += "foreign"
    elif bad == "exit_bool": doc["exit_code"] = False
    elif bad == "ended_bool": doc["ended_ms"] = True
    elif bad == "future": doc["ended_ms"] = 1001
    elif bad == "stale": doc["ended_ms"] = 999
    elif bad == "state": doc["state"] = "failed"
    elif bad == "extra": doc["extra"] = True
    elif bad == "stop": doc["stop"]["requested"] = True
    elif bad == "logs": doc["logs"]["complete"] = False
    elif bad == "index": (root / "log" / "index.json").write_bytes(b"{}")
    elif bad == "incomplete_index":
        path = root / "log" / "index.json"
        index = json.loads(path.read_bytes()); index["complete"] = False
        path.write_bytes(_json(index).encode())
        doc["logs"]["index_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    elif bad == "index_hash":
        path = root / "log" / "index.json"
        path.write_bytes(path.read_bytes()+b" ")
    elif bad == "segment": next((root / "log").glob("seg-*.log")).unlink()
    elif bad == "command": (root / "command.json").write_bytes(b"{}")
    elif bad == "mapper": mapper = None
    elif bad == "temp": (root / "exit.json.tmp").write_bytes(b"orphan")
    payload = _json(doc).encode()
    if bad == "malformed": payload = b"{"
    elif bad == "duplicate": payload = payload[:-1] + b',"version":1}'
    elif bad == "oversized": payload = b" " * (64*1024+1)
    (root / "exit.json").write_bytes(payload)
    result = rc.reconcile(store, claim.job_id, claim.nonce, 3, outcome_mapper=mapper)
    assert result["state"] == "uncertain" and result["exit_code"] is None
    with pytest.raises(JobRefused): store.admit({})


def test_uncertain_never_imports_even_a_valid_late_receipt_but_operator_can_acknowledge(finished, dead, monkeypatch):
    store, claim, _, _, _ = finished
    publish(finished)
    store.hold_uncertain(claim.job_id, claim.nonce, 3, actor="fixture", reason="log-only uncertainty")
    monkeypatch.setattr(receipts, "read_for_recovery", lambda *a: pytest.fail("automatic uncertain import"))
    unchanged = rc.reconcile(store, claim.job_id, claim.nonce, 4, outcome_mapper=map_exit)
    assert unchanged["state"] == "uncertain" and unchanged["state_version"] == 4
    result = rc.recheck(store, claim.job_id, claim.nonce, 4, actor="David", note="accept unknown tail")
    assert result["state"] == "lost" and result["exit_code"] is None and result["outcome"] == "unknown"
    detail = json.loads(store.events(claim.job_id)[-1]["details_json"])
    assert detail["note"] == "accept unknown tail" and detail["acknowledged_unknown_outcome"]


@pytest.mark.parametrize("actor,note", [("", "ack"), ("operator", ""), (" ", "ack")])
def test_recheck_requires_attributed_explicit_acknowledgement(ledger, dead, actor, note):
    _, store, _ = ledger
    row = running(store)
    store.hold_uncertain(row["job_id"], row["nonce"], 3, actor="fixture", reason="unknown")
    with pytest.raises(JobRefused, match="ACKNOWLEDGEMENT"):
        rc.recheck(store, row["job_id"], row["nonce"], 4, actor=actor, note=note)


def test_inconclusive_recheck_is_recorded_and_versions_replay(ledger, dead, monkeypatch):
    _, store, _ = ledger
    row = running(store)
    store.hold_uncertain(row["job_id"], row["nonce"], 3, actor="fixture", reason="unknown")
    monkeypatch.setattr(w, "observe_job", lambda *a: w.JobObservation("unknown", error=5))
    result = rc.recheck(store, row["job_id"], row["nonce"], 4, actor="operator", note="please recheck")
    assert result["state"] == "uncertain" and result["state_version"] == 5
    assert store.events(row["job_id"])[-1]["event"] == "operator_recheck"
    with pytest.raises(JobRefused, match="FENCED"):
        rc.recheck(store, row["job_id"], row["nonce"], 4, actor="operator", note="retry")


def test_receipt_import_reobserves_after_file_reads(finished, dead, monkeypatch):
    store, claim, _, _, _ = finished
    publish(finished)
    original = receipts.read_for_recovery
    def changed(*args):
        result = original(*args)
        monkeypatch.setattr(w, "observe_job", lambda *a: w.JobObservation("present", 0))
        return result
    monkeypatch.setattr(receipts, "read_for_recovery", changed)
    assert rc.reconcile(store, claim.job_id, claim.nonce, 3, outcome_mapper=map_exit)["state"] == "uncertain"


def test_recovery_commit_and_event_roll_back_together(ledger, dead, monkeypatch):
    _, store, _ = ledger
    row = running(store)
    def fail(*a, **k): raise OSError("event failed")
    monkeypatch.setattr(store, "_event", fail)
    with pytest.raises(OSError): rc.reconcile(store, row["job_id"], row["nonce"], 3)
    assert store.get(row["job_id"])["state"] == "running" and len(store.events(row["job_id"])) == 3


@pytest.mark.skipif(os.name != "nt", reason="Windows contained crash proof")
@pytest.mark.parametrize("stage", ["claim", "birth", "before_receipt", "receipt_temp", "receipt"])
def test_native_owner_crash_reconciles_at_explicit_handshakes(fixture, tmp_path, stage):
    ready, tool_ready, release = (tmp_path / name for name in ("owner-ready", "tool-ready", "release"))
    body = "import os;os.write(1,b'fixture\\n')"
    if stage == "birth":
        body = f"import pathlib,time\npathlib.Path({str(tool_ready)!r}).touch()\nwhile not pathlib.Path({str(release)!r}).exists(): time.sleep(.01)"
    store, row, _, _ = fixture(body)
    helper = Path(__file__).with_name("runner_job_fixture.py")
    host = subprocess.Popen([sys.executable, str(helper), str(store.path), row["job_id"], row["nonce"], stage, str(ready)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        deadline = time.monotonic()+15
        while not ready.exists():
            assert host.poll() is None, host.communicate(timeout=1)
            assert time.monotonic() < deadline
            time.sleep(.01)
        evidence = json.loads(ready.read_text())
        if stage == "birth":
            while not tool_ready.exists():
                assert time.monotonic() < deadline
                time.sleep(.01)
            assert w.observe_process(ProcessIdentity(**evidence["child"])).state == "alive"
        if stage == "receipt_temp":
            # This crash point deliberately pauses while publication owns
            # admission. Observe the fixture row read-only; mutation must wait
            # until owner death releases its byte lock.
            with dal.connection_scope(store.database, readonly=True) as db:
                current = dict(db.execute("SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)).fetchone())
            assert rc.observe(current)["runner"]["state"] == "alive"
        else:
            current = store.get(row["job_id"])
            assert rc.reconcile(store, row["job_id"], row["nonce"], current["state_version"], outcome_mapper=map_exit) == current
        host.kill()  # Only this disposable known fixture owner, never a production PID.
        host.communicate(timeout=5)
        deadline = time.monotonic()+8
        while w.observe_job(row["job_id"], evidence["runner"]["session_id"]).state != "absent":
            assert time.monotonic() < deadline
            time.sleep(.01)
        result = rc.reconcile(store, row["job_id"], row["nonce"], current["state_version"], outcome_mapper=map_exit)
        expected = "succeeded" if stage == "receipt" else "uncertain" if stage == "receipt_temp" else "lost"
        assert result["state"] == expected, result
        if stage == "receipt":
            assert rc.reconcile(store, row["job_id"], row["nonce"], current["state_version"], outcome_mapper=map_exit) == result
        if stage == "receipt_temp":
            path = store.path / "jobs" / row["job_id"] / "exit.json.tmp"
            assert path.exists() and "ORPHAN_EXIT_RECEIPT_TEMP" in store.events(row["job_id"])[-1]["details_json"]
            with pytest.raises(JobRefused): store.admit({})
            original = path.read_bytes()
            acknowledged = rc.recheck(store, row["job_id"], row["nonce"], result["state_version"],
                actor="fixture operator", note="accept unknown output after interrupted publication")
            assert acknowledged["state"] == "lost" and path.read_bytes() == original
        else:
            assert store.admit({})["job_id"] != row["job_id"]
    finally:
        release.touch()
        if host.poll() is None: host.kill()
        host.communicate(timeout=5)


@pytest.mark.parametrize("value", [None, "", "0:1", "1:0", "01:2", "1:02", "-1:2", "1:2:3", str(2**128)+":1"])
def test_runner_requires_parent_admission_identity(value):
    with pytest.raises(JobRefused, match="ADMISSION_STATE_IDENTITY"):
        r.admission_identity({} if value is None else {"CBZ_CONSOLE_STATE_IDENTITY": value})


def test_main_uses_parent_identity_instead_of_observed_current_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(r.state_dir, "state_dir_from_env", lambda: tmp_path)
    monkeypatch.setenv("CBZ_CONSOLE_STATE_IDENTITY", "123:456")
    def inspect(path, *, expected_identity):
        assert expected_identity == (123,456)
        raise JobRefused("witness stops before ledger open")
    monkeypatch.setattr(r.state_dir, "inspect_state_dir", inspect)
    with pytest.raises(JobRefused, match="witness"):
        r.main(["job", "nonce"])


@pytest.mark.skipif(os.name != "nt", reason="Windows runner")
def test_log_pump_start_failure_has_clear_post_birth_reason(fixture, monkeypatch):
    store, row, _, start = fixture("pass")
    def fail(self): raise RuntimeError("fixture start failed")
    monkeypatch.setattr(r.LogPump, "start", fail)
    owner = start()
    assert owner.process is not None and owner.reason == "LOG_PUMP_START_FAILED_AFTER_BIRTH:RuntimeError"
    until(owner, lambda: owner.done)
    final = store.get(row["job_id"])
    assert final["state"] == "uncertain" and final["child_pid"] == owner.process.identity.pid


@pytest.mark.skipif(os.name != "nt", reason="Windows runner")
def test_ledger_refresh_is_slower_than_native_polling(fixture, monkeypatch, tmp_path):
    now = [0.]
    release = tmp_path / "release-refresh"
    body = f"import pathlib,time\nwhile not pathlib.Path({str(release)!r}).exists(): time.sleep(.01)"
    store, row, _, start = fixture(body, clock=lambda: now[0])
    owner = start()
    try:
        until(owner, lambda: owner.running_saved)
        calls=[]; get=store.get
        monkeypatch.setattr(store, "get", lambda *a: (calls.append(now[0]),get(*a))[1])
        for tick in range(1,10):
            now[0]=tick/10
            owner.tick()
            assert owner.io is None, "ledger refresh scheduled before one second"
        assert calls == []
        now[0]=1.1
        until(owner, lambda: bool(calls))
        assert len(calls)==1
    finally:
        release.touch()
    until(owner, lambda: owner.done)


@pytest.mark.parametrize("field,value", [("state_version",4), ("nonce","f"*32),
    ("child_created_at","2"), ("runner_session_id",7), ("seal_digest","0"*64)])
def test_recovery_refences_the_row_after_native_queries(ledger, dead, monkeypatch, field, value):
    _, store, _ = ledger
    row = running(store)
    changed = []
    def query(*args):
        if not changed:
            changed.append(True)
            with dal.connection_scope(store.database) as db, dal.transaction(db):
                db.execute("UPDATE jobs SET " + field + "=? WHERE job_id=?", (value,row["job_id"]))
        return w.JobObservation("absent", error=2)
    monkeypatch.setattr(w, "observe_job", query)
    with pytest.raises(JobRefused, match="FENCED"):
        rc.reconcile(store,row["job_id"],row["nonce"],3)
    assert store.get(row["job_id"])["state"] == "running"


def test_foreign_recorded_job_name_cannot_release(ledger, dead):
    _,store,_ = ledger
    row = running(store)
    with dal.connection_scope(store.database) as db, dal.transaction(db):
        db.execute("UPDATE jobs SET job_object_name='Local\\foreign' WHERE job_id=?",(row["job_id"],))
    assert rc.reconcile(store,row["job_id"],row["nonce"],3)["state"] == "uncertain"


@pytest.mark.skipif(os.name != "nt", reason="Windows session namespace proof")
def test_wrong_session_native_absence_is_unknown(ledger):
    _,store,_ = ledger
    current = w.current_identity()
    row = store.admit({})
    old = ProcessIdentity(current.pid,1,current.session_id+1)
    claim = store.claim(row["job_id"],row["nonce"],old)
    try:
        store.mark_running(claim,2,ProcessIdentity(current.pid,2,current.session_id+1))
    finally:
        claim.close()
    result = rc.reconcile(store,row["job_id"],row["nonce"],3)
    assert result["state"] == "uncertain"
    detail=json.loads(store.events(row["job_id"])[-1]["details_json"])
    assert detail["evidence"]["runner"]["state"] == "gone"
    assert detail["evidence"]["job"]["state"] == "unknown"


def unrecorded(store, identity=RUNNER):
    """Prepare durable uncertainty after a claim without inventing child fields."""
    row=store.admit({})
    store.claim(row["job_id"],row["nonce"],identity).close()
    return store.hold_uncertain(row["job_id"],row["nonce"],2,actor="fixture runner",reason="pre-child persistence failed")


def test_explicit_unrecorded_child_recheck_uses_containment_absence(ledger, dead):
    _,store,_=ledger
    row=unrecorded(store)
    # Automatic reconciliation remains sticky even when no member survives.
    assert rc.reconcile(store,row["job_id"],row["nonce"],3)==row
    result=rc.recheck(store,row["job_id"],row["nonce"],3,actor="David",note="accept unknown outcome after owner death")
    assert result["state"]=="lost" and result["outcome"]=="unknown" and result["exit_code"] is None
    assert result["child_pid"] is None and result["child_created_at"] is None
    event=store.events(row["job_id"])[-1]
    detail=json.loads(event["details_json"])
    assert event["actor"]=="David" and detail["note"]=="accept unknown outcome after owner death"
    assert detail["evidence"]["child"]["state"]=="not_recorded"
    assert detail["evidence"]["job"]["state"]=="absent"
    assert detail["evidence"]["recorded"]["expected_job_name"]==w.job_name(row["job_id"])
    assert store.admit({})["job_id"]!=row["job_id"]


@pytest.mark.parametrize("runner,job",[("alive","absent"),("unknown","absent"),("gone","present"),("gone","unknown")])
def test_unrecorded_recheck_stays_uncertain_without_complete_proof(ledger,dead,monkeypatch,runner,job):
    _,store,_=ledger
    row=unrecorded(store)
    monkeypatch.setattr(w,"observe_process",lambda i:w.ProcessObservation(runner,i,error=5))
    monkeypatch.setattr(w,"observe_job",lambda *a:w.JobObservation(job,0 if job=="present" else None,error=5))
    result=rc.recheck(store,row["job_id"],row["nonce"],3,actor="operator",note="explicit recheck")
    assert result["state"]=="uncertain" and result["state_version"]==4
    detail=json.loads(store.events(row["job_id"])[-1]["details_json"])
    assert detail["evidence"]["job"]["state"]==job
    with pytest.raises(JobRefused):store.admit({})


@pytest.mark.parametrize("pid,created",[(None,"2"),(456,None)])
def test_partial_child_identity_is_not_unrecorded_proof(ledger,dead,pid,created):
    _,store,_=ledger
    row=unrecorded(store)
    with dal.connection_scope(store.database) as db,dal.transaction(db):
        db.execute("UPDATE jobs SET child_pid=?,child_created_at=?,job_object_name=? WHERE job_id=?",
                   (pid,created,w.job_name(row["job_id"]),row["job_id"]))
    result=rc.recheck(store,row["job_id"],row["nonce"],3,actor="operator",note="check partial identity")
    assert result["state"]=="uncertain"
    assert "PARTIAL_CHILD_IDENTITY" in store.events(row["job_id"])[-1]["details_json"]


@pytest.mark.skipif(os.name!="nt",reason="Windows namespace proof")
def test_unrecorded_recheck_cannot_use_another_sessions_absence(ledger):
    _,store,_=ledger
    current=w.current_identity()
    row=unrecorded(store,ProcessIdentity(current.pid,1,current.session_id+1))
    result=rc.recheck(store,row["job_id"],row["nonce"],3,actor="operator",note="check old session")
    assert result["state"]=="uncertain"
    detail=json.loads(store.events(row["job_id"])[-1]["details_json"])
    assert detail["evidence"]["runner"]["state"]=="gone"
    assert detail["evidence"]["job"]["state"]=="unknown"
    with pytest.raises(JobRefused):store.admit({})


@pytest.mark.skipif(os.name!="nt",reason="Windows named job proof")
@pytest.mark.parametrize("live",[False,True])
def test_unrecorded_recheck_retains_a_real_present_job_even_when_empty(ledger,tmp_path,live):
    _,store,_=ledger
    current=w.current_identity()
    row=unrecorded(store,ProcessIdentity(current.pid,1,current.session_id))
    release=tmp_path/"release-present"
    body=f"import pathlib,time\nwhile not pathlib.Path({str(release)!r}).exists():time.sleep(.01)" if live else "pass"
    child=w.launch(row["job_id"],Path(sys.executable),["-c",body],cwd=tmp_path,environment=dict(os.environ))
    try:
        if not live:
            assert child.wait_root(5)==0
            deadline=time.monotonic()+5
            while child.active_processes():
                assert time.monotonic()<deadline
                time.sleep(.01)
        result=rc.recheck(store,row["job_id"],row["nonce"],3,actor="operator",note="check actual object")
        assert result["state"]=="uncertain"
        detail=json.loads(store.events(row["job_id"])[-1]["details_json"])
        assert detail["evidence"]["job"]["state"]=="present"
        if live:
            assert detail["evidence"]["job"]["active_processes"]>=1
        else:
            assert detail["evidence"]["job"]["active_processes"]==0
    finally:
        release.touch()
        assert child.wait_root(5)==0
        deadline=time.monotonic()+5
        while child.active_processes():
            assert time.monotonic()<deadline
            time.sleep(.01)
        child.close()
    result=rc.recheck(store,row["job_id"],row["nonce"],4,actor="operator",note="acknowledge after object is gone")
    assert result["state"]=="lost" and result["child_pid"] is None


@pytest.mark.skipif(os.name!="nt",reason="Windows actual owner death")
@pytest.mark.parametrize("stage",["claim","birth"])
def test_unrecorded_uncertainty_after_real_owner_death_has_operator_escape(fixture,tmp_path,stage):
    ready,tool_ready,release=(tmp_path/name for name in ("owner-ready","tool-ready","release"))
    body=f"import pathlib,time\npathlib.Path({str(tool_ready)!r}).touch()\nwhile not pathlib.Path({str(release)!r}).exists():time.sleep(.01)"
    store,row,_,_=fixture(body)
    helper=Path(__file__).with_name("runner_job_fixture.py")
    host=subprocess.Popen([sys.executable,str(helper),str(store.path),row["job_id"],row["nonce"],stage,str(ready)],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        deadline=time.monotonic()+15
        while not ready.exists() or stage=="birth" and not tool_ready.exists():
            assert host.poll() is None,host.communicate(timeout=1)
            assert time.monotonic()<deadline
            time.sleep(.01)
        report=json.loads(ready.read_text())
        uncertain=store.hold_uncertain(row["job_id"],row["nonce"],2,actor="fixture",reason="failed before child identity commit")
        assert uncertain["child_pid"] is None
        host.kill() # This known disposable fixture owner only.
        host.communicate(timeout=5)
        deadline=time.monotonic()+8
        while w.observe_job(row["job_id"],report["runner"]["session_id"]).state!="absent":
            assert time.monotonic()<deadline
            time.sleep(.01)
        assert rc.reconcile(store,row["job_id"],row["nonce"],3)==uncertain
        result=rc.recheck(store,row["job_id"],row["nonce"],3,actor="operator",note="accept unknown crash outcome")
        assert result["state"]=="lost" and result["outcome"]=="unknown" and result["child_pid"] is None
        if report["child"]:
            assert w.observe_process(ProcessIdentity(**report["child"])).state=="gone"
    finally:
        release.touch()
        if host.poll() is None:host.kill()
        host.communicate(timeout=5)
