"""Disposable ledger and real-process contention tests for the M3a boundary."""
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import sqlite3

import pytest

from comic_automation.database import dal
from comic_automation.web import state_dir as sd
from comic_automation.web.config import load_console_config
from comic_automation.web.jobs.store import (
    CLAIM_BUDGET_MS, JobRefused, JobStore, LockBusy, ProcessIdentity, VerifiedRequest, validate_job_id,
)
from web_support import private_dir


def fake_registry(parameters):
    return VerifiedRequest("fixture", "reviewer", {"fixture": "not executable"},
                           kind=parameters.get("kind", "task"), readonly=parameters.get("readonly", True))


@pytest.fixture
def ledger(tmp_path):
    state = sd.open_state_dir(private_dir(tmp_path / "state"), environ={})
    clock = [1000]
    store = JobStore(state, verifier=fake_registry, watcher_observation=lambda: False, clock_ms=lambda: clock[0])
    try:
        yield state, store, clock
    finally:
        state.close()


RUNNER = ProcessIdentity(123, 133_000_000_000_000_001, 1)


def test_admission_persists_readback_digest_nonce_and_event(ledger):
    state, store, _ = ledger
    row = store.admit({})
    assert validate_job_id(row["job_id"]) == row["job_id"]
    assert len(row["nonce"]) == 32 and row["state"] == "admitted" and row["state_version"] == 1
    command = state.path / "jobs" / row["job_id"] / "command.json"
    assert hashlib.sha256(command.read_bytes()).hexdigest() == row["command_digest"]
    assert json.loads(command.read_bytes())["nonce"] == row["nonce"]
    assert row["claim_deadline_ms"] == 1000 + CLAIM_BUDGET_MS
    assert [event["event"] for event in store.events(row["job_id"])] == ["admitted"]


def test_missing_verifier_refuses_before_preparing_a_command(ledger):
    state, _, _ = ledger
    with pytest.raises(JobRefused, match="TRUSTED_VERIFIER_REQUIRED"):
        JobStore(state).admit({})
    assert not (state.path / "jobs").exists()


@pytest.mark.parametrize("field", ["argv", "cmd", "command"])
def test_direct_service_rejects_raw_command_fields(ledger, field):
    _, store, _ = ledger
    with pytest.raises(JobRefused, match="COMMAND_NOT_ACCEPTED"):
        store.admit({field: []})


def test_only_one_task_and_no_artifact_for_a_known_conflict(ledger):
    state, store, _ = ledger
    row = store.admit({})
    with pytest.raises(JobRefused, match="JOB_ACTIVE"):
        store.admit({})
    assert [p.name for p in (state.path / "jobs").iterdir()] == [row["job_id"]]


@pytest.mark.parametrize("watcher", [None, True])
def test_writers_refuse_a_present_or_unknown_watcher(ledger, watcher):
    state, _, clock = ledger
    store = JobStore(state, verifier=fake_registry, watcher_observation=lambda: watcher, clock_ms=lambda: clock[0])
    with pytest.raises(JobRefused, match="WATCHER_UNKNOWN"):
        store.admit({"readonly": False})


def test_readonly_task_can_coexist_with_a_watcher_but_not_another_task(ledger):
    state, store, clock = ledger
    service = store.admit({"kind": "service", "readonly": False})
    task = store.admit({})
    assert service["kind"] == "service" and task["kind"] == "task"
    with pytest.raises(JobRefused):
        store.admit({})
    with pytest.raises(JobRefused):
        store.admit({"kind": "service"})


def test_writing_task_excludes_a_watcher_start(ledger):
    _, store, _ = ledger
    store.admit({"readonly": False})
    with pytest.raises(JobRefused):
        store.admit({"kind": "service"})


def test_claim_is_committed_before_return_and_keeps_admission_after_close(ledger):
    _, store, clock = ledger
    row = store.admit({})
    with store.claim(row["job_id"], row["nonce"], RUNNER):
        claimed = store.get(row["job_id"])
        assert claimed["state"] == "claimed" and claimed["state_version"] == 2
        assert claimed["runner_created_at"] == str(RUNNER.created_at)
        with pytest.raises(LockBusy):
            store.claim(row["job_id"], row["nonce"], RUNNER)
    with pytest.raises(JobRefused, match="CLAIM_FENCED"):
        store.claim(row["job_id"], row["nonce"], RUNNER)
    clock[0] += CLAIM_BUDGET_MS * 2
    assert store.expire_admissions() == []
    with pytest.raises(JobRefused):
        store.admit({})


def test_wrong_nonce_and_late_claim_never_enter_the_tool_side(ledger, tmp_path):
    _, store, clock = ledger
    row = store.admit({})
    marker = tmp_path / "tool-side"
    for nonce, instant in [("0" * 32, 1000), (row["nonce"], row["claim_deadline_ms"])]:
        clock[0] = instant
        with pytest.raises(JobRefused, match="CLAIM_FENCED"):
            with store.claim(row["job_id"], nonce, RUNNER):
                marker.write_text("must never happen")
    assert not marker.exists()
    assert store.expire_admissions() == [row["job_id"]]
    assert store.expire_admissions() == []
    assert [e["event"] for e in store.events(row["job_id"])] == ["admitted", "admission_expired"]


def test_uncertain_rows_block_every_class_and_cannot_expire(ledger):
    _, store, clock = ledger
    row = store.admit({})
    updated = store.hold_uncertain(row["job_id"], row["nonce"], 1, actor="operator", reason="inconclusive")
    assert updated["state_version"] == 2
    clock[0] += CLAIM_BUDGET_MS * 10
    assert store.expire_admissions() == []
    for request in ({"kind": "service"}, {}, {"readonly": False}):
        with pytest.raises(JobRefused, match="JOB_UNCERTAIN"):
            store.admit(request)
    with pytest.raises(JobRefused, match="UNCERTAIN_FENCED"):
        store.hold_uncertain(row["job_id"], row["nonce"], 1, actor="operator", reason="stale")


def test_clock_rollback_cannot_expire_or_claim(ledger):
    _, store, clock = ledger
    row = store.admit({})
    clock[0] -= 1
    with pytest.raises(JobRefused, match="CLOCK_MOVED_BACKWARD"):
        store.expire_admissions()
    with pytest.raises(JobRefused, match="CLOCK_MOVED_BACKWARD"):
        store.claim(row["job_id"], row["nonce"], RUNNER)
    assert store.get(row["job_id"])["state"] == "admitted"


@pytest.mark.parametrize("sql", ["UPDATE job_events SET actor='edited'", "DELETE FROM job_events"])
def test_events_are_append_only_even_through_direct_sql(ledger, sql):
    state, store, _ = ledger
    row = store.admit({})
    with dal.connection_scope(state.database) as db, pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with dal.transaction(db):
            db.execute(sql)
    assert len(store.events(row["job_id"])) == 1


def test_failed_event_rolls_back_claim_and_releases_its_runner_lock(ledger):
    state, store, _ = ledger
    row = store.admit({})
    with dal.connection_scope(state.database) as db, dal.transaction(db):
        db.execute("CREATE TRIGGER reject_claim BEFORE INSERT ON job_events WHEN NEW.event='claimed' "
                   "BEGIN SELECT RAISE(ABORT,'injected event failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        store.claim(row["job_id"], row["nonce"], RUNNER)
    assert store.get(row["job_id"])["state"] == "admitted"
    assert len(store.events(row["job_id"])) == 1
    with dal.connection_scope(state.database) as db, dal.transaction(db):
        db.execute("DROP TRIGGER reject_claim")
    with store.claim(row["job_id"], row["nonce"], RUNNER):
        assert store.get(row["job_id"])["state"] == "claimed"


def test_future_schema_refuses_existing_and_new_store(ledger):
    state, store, _ = ledger
    with dal.connection_scope(state.database) as db, dal.transaction(db):
        db.execute("UPDATE console_meta SET value='2' WHERE key='schema_version'")
    with pytest.raises(JobRefused, match="UNSUPPORTED_SCHEMA"):
        JobStore(state)
    with pytest.raises(JobRefused, match="UNSUPPORTED_SCHEMA"):
        store.admit({})


def test_admission_os_lock_is_required_even_with_an_idle_database(ledger):
    _, store, _ = ledger
    store.lock_timeout = 0
    owner = store._lock(Path("admission.lock"))
    try:
        with pytest.raises(LockBusy):
            store.admit({})
    finally:
        sd._unlock(owner)
        owner.close()
    assert store.admit({})["state"] == "admitted"


def test_readback_failure_cannot_create_an_admitted_row(ledger, monkeypatch):
    state, store, _ = ledger
    read = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda p: b"corrupt" if p.name == "command.json" else read(p))
    with pytest.raises(JobRefused, match="COMMAND_READBACK_MISMATCH"):
        store.admit({})
    with dal.connection_scope(state.database, readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_failed_fsync_leaves_no_runnable_row_and_orphans_are_not_reused(ledger, monkeypatch):
    state, store, _ = ledger
    monkeypatch.setattr("comic_automation.web.jobs.store.secrets.randbits", lambda size: 1)
    sync = os.fsync
    def fail(fd):
        raise OSError("injected fsync failure")
    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError, match="injected"):
        store.admit({})
    orphan = next((state.path / "jobs").iterdir())
    prepared = (orphan / "command.json.tmp").read_bytes()
    monkeypatch.setattr(os, "fsync", sync)
    with pytest.raises(FileExistsError):
        store.admit({})
    assert (orphan / "command.json.tmp").read_bytes() == prepared
    with dal.connection_scope(state.database, readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_clock_rollback_between_preparation_and_commit_refuses(ledger):
    state, _, _ = ledger
    instants = iter((2000, 1999))
    store = JobStore(state, verifier=fake_registry, clock_ms=lambda: next(instants))
    with pytest.raises(JobRefused, match="CLOCK_MOVED_BACKWARD"):
        store.admit({})


@pytest.mark.parametrize("after,limit", [(-1, 1), (2**63, 1), (True, 1), (0, 0), (0, 1001), (0, True)])
def test_event_pages_are_bounded_at_the_service_entry(ledger, after, limit):
    _, store, _ = ledger
    row = store.admit({})
    with pytest.raises(JobRefused, match="INVALID_EVENT_PAGE"):
        store.events(row["job_id"], after=after, limit=limit)


@pytest.mark.parametrize("budget", [-1, float("nan"), float("inf"), True, 61])
def test_lock_wait_budget_must_be_finite_and_bounded(ledger, budget):
    state, _, _ = ledger
    with pytest.raises(JobRefused, match="INVALID_LOCK_TIMEOUT"):
        JobStore(state, lock_timeout=budget)


@pytest.mark.parametrize("identity", [ProcessIdentity(True, 1, 1), ProcessIdentity(2**32, 1, 1),
    ProcessIdentity(1, 1.5, 1), ProcessIdentity(1, 0, 1), ProcessIdentity(1, 2**64, 1), ProcessIdentity(1, 1, 2**32)])
def test_process_identities_require_exact_bounded_integers(ledger, identity):
    _, store, _ = ledger
    row = store.admit({})
    with pytest.raises(JobRefused, match="INVALID_PROCESS_IDENTITY"):
        store.claim(row["job_id"], row["nonce"], identity)


@pytest.mark.parametrize("value", ["../x", "0" * 25, "8" + "0" * 25, "a" * 26, "0" * 26 + "/x"])
def test_invalid_job_ids_never_become_paths(ledger, value):
    _, store, _ = ledger
    with pytest.raises(JobRefused, match="INVALID_JOB_ID"):
        store.claim(value, "0" * 32, RUNNER)


def _child_store(root, identity, now):
    path = Path(root)
    state = sd.StateDir(path, load_console_config(path / "console.toml", environ={}), identity, None)
    return JobStore(state, verifier=fake_registry, watcher_observation=lambda: False, clock_ms=lambda: now)


def _admit_process(root, identity, barrier, results):
    barrier.wait(timeout=60)
    try:
        row = _child_store(root, identity, 1000).admit({})
        results.put(("admitted", row["job_id"]))
    except JobRefused as exc:
        results.put(("refused", str(exc)))
    except BaseException as exc:
        results.put(("error", repr(exc)))


def _race_process(root, identity, row, action, barrier, results):
    store = _child_store(root, identity, row["claim_deadline_ms"] - (action == "claim"))
    barrier.wait(timeout=60)
    try:
        if action == "claim":
            with store.claim(row["job_id"], row["nonce"], ProcessIdentity(os.getpid(), 100_000_001, 1)):
                results.put(("claimed", True))
        else:
            results.put(("expired", bool(store.expire_admissions())))
    except JobRefused:
        results.put(("fenced", False))


def _duplicate_runner(root, identity, row, barrier, release, results, markers):
    store = _child_store(root, identity, 1000)
    barrier.wait(timeout=60)
    try:
        owner = ProcessIdentity(os.getpid(), 100_000_000 + os.getpid(), 1)
        with store.claim(row["job_id"], row["nonce"], owner):
            (Path(markers) / str(os.getpid())).write_text("past committed claim")
            results.put("claimed")
            release.wait(timeout=20)
    except JobRefused:
        results.put("fenced")


def _join(processes):
    for process in processes:
        process.join(timeout=20)
    for process in processes:
        if process.is_alive():
            process.terminate()  # only our disposable fixture child
            process.join(timeout=5)


def test_twenty_concurrent_request_processes_admit_exactly_one(ledger):
    state, store, _ = ledger
    ctx = mp.get_context("spawn")
    barrier, results = ctx.Barrier(21), ctx.Queue()
    children = [ctx.Process(target=_admit_process, args=(str(state.path), state.identity, barrier, results))
                for _ in range(20)]
    try:
        for process in children:
            process.start()
        barrier.wait(timeout=60)
        received = [results.get(timeout=60) for _ in children]
        assert sum(item[0] == "admitted" for item in received) == 1, received
        assert sum(item[0] == "refused" for item in received) == 19, received
    finally:
        _join(children)
        results.close()
    assert all(process.exitcode == 0 for process in children)


def test_claim_and_expiry_processes_have_one_winner(ledger):
    state, store, _ = ledger
    row = store.admit({})
    ctx = mp.get_context("spawn")
    barrier, results = ctx.Barrier(3), ctx.Queue()
    children = [ctx.Process(target=_race_process,
                 args=(str(state.path), state.identity, row, action, barrier, results)) for action in ("claim", "expire")]
    try:
        for process in children:
            process.start()
        barrier.wait(timeout=60)
        received = [results.get(timeout=60) for _ in children]
        assert sum(bool(item[1]) for item in received) == 1, received
    finally:
        _join(children)
        results.close()
    final = store.get(row["job_id"])
    assert final["state"] in ("claimed", "admission_expired")
    assert len(store.events(row["job_id"])) == 2


def test_two_real_runner_processes_cannot_both_pass_the_claim(ledger, tmp_path):
    state, store, _ = ledger
    row = store.admit({})
    markers = tmp_path / "markers"
    markers.mkdir()
    ctx = mp.get_context("spawn")
    barrier, release, results = ctx.Barrier(3), ctx.Event(), ctx.Queue()
    children = [ctx.Process(target=_duplicate_runner,
        args=(str(state.path), state.identity, row, barrier, release, results, str(markers))) for _ in range(2)]
    try:
        for process in children:
            process.start()
        barrier.wait(timeout=60)
        received = [results.get(timeout=30) for _ in children]
        assert sorted(received) == ["claimed", "fenced"]
        assert len(list(markers.iterdir())) == 1
    finally:
        release.set()
        _join(children)
        results.close()
    assert all(process.exitcode == 0 for process in children)
