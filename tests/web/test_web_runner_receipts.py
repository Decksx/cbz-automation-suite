"""Live claims, disposable contained children and durable receipt fencing.

No production adapter or runner entry point is enabled by these witnesses.
Native conditions are observed directly; delays are bounded polling intervals,
never the assertion that a process or a log worker must already have settled.
"""
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import types

import pytest

from comic_automation.web import state_dir as sd
from comic_automation.database import dal
from comic_automation.web.jobs import receipts, win32
from comic_automation.web.jobs.log_service import JobLogs
from comic_automation.web.jobs.logs import LogPump
from comic_automation.web.jobs.store import JobStore, JobRefused, ProcessIdentity, _json
from test_web_job_store import ledger, RUNNER, fake_registry
from web_support import private_dir


def test_claimed_command_is_bound_to_every_immutable_ledger_field(ledger):
    _, store, _ = ledger
    row = store.admit({})
    with store.claim(row["job_id"], row["nonce"], RUNNER) as claim:
        command = store.read_command(claim, 2)
        assert command["seal"] == {"fixture": "not executable"}
        path = store.path / "jobs" / claim.job_id / "command.json"
        path.write_bytes(path.read_bytes() + b" ")
        with pytest.raises(JobRefused, match="COMMAND_DIGEST_CHANGED"):
            store.read_command(claim, 2)


@pytest.mark.parametrize("bad", ["version", "nonce", "pid", "creation", "session", "closed", "foreign"])
def test_live_exact_claim_fences_running_transition(ledger, bad):
    state, store, _ = ledger
    row = store.admit({})
    with store.claim(row["job_id"], row["nonce"], RUNNER) as claim:
        candidate = dataclasses.replace(claim)
        owner = store
        version = 2
        if bad == "version":
            version = 1
        elif bad == "nonce":
            candidate.nonce = "f" * 32
        elif bad in ("pid", "creation", "session"):
            fields = dict(pid=RUNNER.pid, created_at=RUNNER.created_at, session_id=RUNNER.session_id)
            fields[{"pid": "pid", "creation": "created_at", "session": "session_id"}[bad]] += 1
            candidate.runner = ProcessIdentity(**fields)
        elif bad == "closed":
            candidate._closed = True
        else:
            owner = JobStore(state)
        with pytest.raises(JobRefused):
            owner.mark_running(candidate, version, ProcessIdentity(321, 987654321, candidate.runner.session_id))
        assert store.get(row["job_id"])["state"] == "claimed"


def test_prebirth_refusal_is_versioned_and_releases_only_its_admission(ledger):
    _, store, _ = ledger
    row = store.admit({})
    with store.claim(row["job_id"], row["nonce"], RUNNER) as claim:
        refused = store.refuse_before_create(claim, 2, reason="fresh seal mismatch")
        assert refused["state"] == "refused" and refused["state_version"] == 3
        assert store.admit({})["job_id"] != row["job_id"]
        with pytest.raises(JobRefused):
            store.mark_running(claim, 3, ProcessIdentity(321, 987654321, 1))


def test_claim_lock_must_still_name_its_open_physical_file(ledger, monkeypatch):
    _, store, _ = ledger
    row = store.admit({})
    with store.claim(row["job_id"], row["nonce"], RUNNER) as claim:
        path = store.path / "locks" / "jobs" / f"{claim.job_id}.runner.lock"
        original = Path.stat
        own = original(path)
        def replaced(self, *args, **kwargs):
            if self == path:
                return types.SimpleNamespace(st_dev=own.st_dev, st_ino=own.st_ino + 1)
            return original(self, *args, **kwargs)
        monkeypatch.setattr(Path, "stat", replaced)
        with pytest.raises(JobRefused, match="LOCK_IDENTITY"):
            store.mark_running(claim, 2, ProcessIdentity(321, 987654321, 1))
        monkeypatch.undo()


def test_command_seal_digest_must_match_the_ledger(ledger):
    _, store, _ = ledger
    row = store.admit({})
    with store.claim(row["job_id"], row["nonce"], RUNNER) as claim:
        with dal.connection_scope(store.database) as db, dal.transaction(db):
            db.execute("UPDATE jobs SET seal_digest=? WHERE job_id=?", ("0" * 64, claim.job_id))
        with pytest.raises(JobRefused, match="COMMAND_LEDGER_MISMATCH"):
            store.read_command(claim, 2)


@pytest.mark.parametrize("operation", ["running", "refused"])
def test_transition_and_event_roll_back_together(ledger, monkeypatch, operation):
    _, store, _ = ledger
    row = store.admit({})
    with store.claim(row["job_id"], row["nonce"], RUNNER) as claim:
        def failure(*args, **kwargs):
            raise OSError("event commit failed")
        monkeypatch.setattr(store, "_event", failure)
        with pytest.raises(OSError):
            if operation == "running":
                store.mark_running(claim, 2, ProcessIdentity(321, 987654321, 1))
            else:
                store.refuse_before_create(claim, 2, reason="not created")
        current = store.get(row["job_id"])
        assert current["state"] == "claimed" and current["state_version"] == 2
        assert [e["event"] for e in store.events(row["job_id"])] == ["admitted", "claimed"]


@pytest.fixture
def finished(tmp_path, request):
    if os.name != "nt":
        pytest.skip("Windows contained receipt witnesses")
    state = sd.open_state_dir(private_dir(tmp_path / "state"), environ={})
    store = JobStore(state, verifier=fake_registry, clock_ms=lambda: 1000)
    row = store.admit({})
    claim = store.claim(row["job_id"], row["nonce"], win32.current_identity())
    child = pump = None
    try:
        disk = JobLogs(store, claim.job_id).create_writer(claim)
        code = getattr(request, "param", 0)
        child = win32.launch(claim.job_id, Path(sys.executable),
            ["-c", f"import os,sys;os.write(1,b'fixture output\\n');sys.exit({code})"],
            cwd=tmp_path, environment=dict(os.environ))
        pump = LogPump(child.stdout, disk).start()
        running = store.mark_running(claim, 2, child.identity)
        assert child.wait_root(5) == code
        deadline = time.monotonic() + 5
        while child.active_processes():
            assert time.monotonic() < deadline
            time.sleep(.01)
        result = pump.join(5)
        assert result.settled and result.complete and result.durable_offset == len(b"fixture output\n")
        yield store, claim, running, child, pump
    finally:
        if child is not None:
            child.close()
        if pump is not None:
            pump.join(5)
        claim.close()
        state.close()


def map_exit(code):
    return "succeeded" if code == 0 else "failed"


def publish(finished, *, outcome=None):
    store, claim, row, child, pump = finished
    return receipts.publish(store, claim, row["state_version"], child, pump, outcome_mapper=map_exit if outcome is None else lambda code: outcome)


@pytest.mark.parametrize("finished", [0, 7], indirect=True)
def test_durable_receipt_precedes_atomic_terminal_event(finished):
    store, claim, row, child, pump = finished
    outcome = "succeeded" if child.wait_root(0) == 0 else "failed"
    result = publish(finished, outcome=outcome)
    assert result["runner"]["created_at"] == str(claim.runner.created_at)
    assert result["child"]["created_at"] == str(child.identity.created_at)
    path = store.path / "jobs" / claim.job_id / "exit.json"
    assert json.loads(path.read_bytes()) == result
    assert store.get(claim.job_id)["state"] == "running"
    final = store.finalize(claim, 3, child, pump, outcome_mapper=map_exit)
    assert final["state"] == outcome and final["state_version"] == 4
    events = store.events(claim.job_id)
    assert [e["event"] for e in events] == ["admitted", "claimed", "running", outcome]
    assert json.loads(events[-1]["details_json"])["receipt_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(JobRefused):
        store.finalize(claim, 3, child, pump, outcome_mapper=map_exit)


@pytest.mark.parametrize("change", ["nonce", "command", "seal", "runner", "child", "session", "job",
                                   "exit", "zero", "logs", "offset", "extra", "ended_bool", "state", "future"])
def test_receipt_shape_and_binding_are_not_authority(finished, change):
    store, claim, _, child, pump = finished
    doc = publish(finished)
    if change == "nonce": doc["nonce"] = "f" * 32
    elif change == "command": doc["command_digest"] = "0" * 64
    elif change == "seal": doc["seal_digest"] = "0" * 64
    elif change == "runner": doc["runner"]["created_at"] = str(claim.runner.created_at + 1)
    elif change == "child": doc["child"]["pid"] += 1
    elif change == "session": doc["child"]["session_id"] += 1
    elif change == "job": doc["job_object_name"] += "-foreign"
    elif change == "exit": doc["exit_code"] = False  # False must not alias exit code 0.
    elif change == "zero": doc["descendants"]["active_processes"] = False
    elif change == "logs": doc["logs"]["complete"] = False
    elif change == "offset": doc["logs"]["durable_offset"] += 1
    elif change == "extra": doc["invented"] = True
    elif change == "ended_bool": doc["ended_ms"] = True
    elif change == "state": doc["state"] = "failed"
    else: doc["ended_ms"] = 1001
    (store.path / "jobs" / claim.job_id / "exit.json").write_bytes(_json(doc).encode())
    with pytest.raises(JobRefused):
        store.finalize(claim, 3, child, pump, outcome_mapper=map_exit)
    assert store.get(claim.job_id)["state"] == "running"


@pytest.mark.parametrize("bad", [b"", b"{", b"[]", b"x" * (64 * 1024 + 1)],
                         ids=["empty", "truncated", "array", "oversized"])
def test_malformed_or_oversized_receipt_cannot_finalize(finished, bad):
    store, claim, _, child, pump = finished
    (store.path / "jobs" / claim.job_id / "exit.json").write_bytes(bad)
    with pytest.raises(JobRefused):
        store.finalize(claim, 3, child, pump, outcome_mapper=map_exit)
    assert store.get(claim.job_id)["state"] == "running"


@pytest.mark.parametrize("bad", ["root", "descendants", "reader", "writer", "error", "eof", "command", "index"])
def test_persisted_booleans_never_replace_fresh_owned_proof(finished, monkeypatch, bad):
    store, claim, _, child, pump = finished
    publish(finished)
    if bad == "root": monkeypatch.setattr(child, "wait_root", lambda timeout=0: None)
    elif bad == "descendants": monkeypatch.setattr(child, "active_processes", lambda: 1)
    elif bad == "reader": monkeypatch.setattr(pump.reader, "is_alive", lambda: True)
    elif bad == "writer": monkeypatch.setattr(pump.writer, "is_alive", lambda: True)
    elif bad == "error": pump.error = "StateDirRefused"
    elif bad == "eof": pump.queue.complete = False
    else:
        path = store.path / "jobs" / claim.job_id / ("command.json" if bad == "command" else "log/index.json")
        path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(JobRefused):
        store.finalize(claim, 3, child, pump, outcome_mapper=map_exit)
    assert store.get(claim.job_id)["state"] == "running"
    monkeypatch.undo()  # cleanup sees the real finished worker status


@pytest.mark.parametrize("bad", ["root", "descendants", "reader", "writer"])
def test_publication_itself_requires_fresh_native_and_thread_proof(finished, monkeypatch, bad):
    store, claim, _, child, pump = finished
    if bad == "root": monkeypatch.setattr(child, "wait_root", lambda timeout=0: None)
    elif bad == "descendants": monkeypatch.setattr(child, "active_processes", lambda: 1)
    elif bad == "reader": monkeypatch.setattr(pump.reader, "is_alive", lambda: True)
    else: monkeypatch.setattr(pump.writer, "is_alive", lambda: True)
    with pytest.raises(JobRefused):
        publish(finished)
    assert not (store.path / "jobs" / claim.job_id / "exit.json").exists()
    monkeypatch.undo()


def test_receipt_survives_terminal_commit_failure_and_retry_is_once(finished, monkeypatch):
    store, claim, _, child, pump = finished
    publish(finished)
    path = store.path / "jobs" / claim.job_id / "exit.json"
    original = path.read_bytes()
    event = store._event
    monkeypatch.setattr(store, "_event", lambda *a, **k: (_ for _ in ()).throw(OSError("DB commit failed")))
    with pytest.raises(OSError):
        store.finalize(claim, 3, child, pump, outcome_mapper=map_exit)
    assert store.get(claim.job_id)["state"] == "running" and path.read_bytes() == original
    monkeypatch.setattr(store, "_event", event)
    assert store.finalize(claim, 3, child, pump, outcome_mapper=map_exit)["state"] == "succeeded"
    assert [e["event"] for e in store.events(claim.job_id)].count("succeeded") == 1


def test_late_valid_receipt_does_not_clear_uncertain(finished):
    store, claim, _, child, pump = finished
    publish(finished)
    store.hold_uncertain(claim.job_id, claim.nonce, 3, actor="runner", reason="ambiguous cleanup")
    with pytest.raises(JobRefused):
        store.finalize(claim, 4, child, pump, outcome_mapper=map_exit)
    with pytest.raises(JobRefused, match="UNCERTAIN"):
        store.admit({})


def test_publication_fsync_failure_never_publishes_or_finalizes(finished, monkeypatch):
    store, claim, _, _, _ = finished
    def failure(fd):
        raise OSError("disk unavailable")
    monkeypatch.setattr(receipts.os, "fsync", failure)
    with pytest.raises(OSError):
        publish(finished)
    assert not (store.path / "jobs" / claim.job_id / "exit.json").exists()
    assert store.get(claim.job_id)["state"] == "running"


def test_existing_receipt_is_not_replaced_and_stopped_mapping_is_closed(finished):
    store, claim, _, _, _ = finished
    with pytest.raises(JobRefused, match="INVALID_RECEIPT_RESULT"):
        publish(finished, outcome="stopped")
    publish(finished)
    path = store.path / "jobs" / claim.job_id / "exit.json"
    original = path.read_bytes()
    with pytest.raises(JobRefused, match="EXISTING_EXIT_RECEIPT"):
        publish(finished)
    assert path.read_bytes() == original


def test_missing_receipt_and_pending_stop_cannot_release_admission(finished):
    store, claim, _, child, pump = finished
    with pytest.raises(FileNotFoundError):
        store.finalize(claim, 3, child, pump, outcome_mapper=map_exit)
    with dal.connection_scope(store.database) as db, dal.transaction(db):
        db.execute("UPDATE jobs SET stop_requested_ms=999 WHERE job_id=?", (claim.job_id,))
    with pytest.raises(JobRefused, match="STOP_EVIDENCE_REQUIRED"):
        publish(finished)
    assert store.get(claim.job_id)["state"] == "running"


def test_outcome_mapper_is_required_fresh_and_receipt_cannot_select_success(finished):
    store, claim, _, child, pump = finished
    with pytest.raises(JobRefused, match="MAPPER_REQUIRED"):
        receipts.publish(store, claim, 3, child, pump)
    publish(finished)
    with pytest.raises(JobRefused, match="MAPPER_REQUIRED"):
        store.finalize(claim, 3, child, pump)
    with pytest.raises(JobRefused, match="MISMATCH"):
        store.finalize(claim, 3, child, pump, outcome_mapper=lambda code: "failed")
    assert store.get(claim.job_id)["state"] == "running"


def test_contradictory_ledger_seal_cannot_be_sealed_by_a_new_receipt(finished):
    store, claim, _, _, _ = finished
    with dal.connection_scope(store.database) as db, dal.transaction(db):
        db.execute("UPDATE jobs SET seal_digest=? WHERE job_id=?", ("0" * 64, claim.job_id))
    with pytest.raises(JobRefused, match="COMMAND_LEDGER_MISMATCH"):
        publish(finished)
