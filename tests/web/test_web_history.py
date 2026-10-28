"""Real private state, serialized runner cleanup and ledger-first retention."""
import hashlib
import json
from pathlib import Path
import sqlite3
import time

import pytest

from comic_automation.database import dal
from comic_automation.web import acl, state_dir as sd
from comic_automation.web.jobs import archives as ar
from comic_automation.web.jobs.history import JobHistory
from comic_automation.web.jobs.store import CLAIM_BUDGET_MS, JobStore, ProcessIdentity
from test_web_job_store import fake_registry
from web_support import private_dir


@pytest.fixture
def history(tmp_path):
    state = sd.open_state_dir(private_dir(tmp_path / "state"), environ={})
    clock = [1000]
    store = JobStore(state, verifier=fake_registry, watcher_observation=lambda: False, clock_ms=lambda: clock[0])
    service = JobHistory(store)
    try:
        yield state, store, service, clock
    finally:
        state.close()


def expired(store, clock, count=1):
    rows = []
    for _ in range(count):
        rows.append(store.admit({}))
        clock[0] += CLAIM_BUDGET_MS
        assert store.expire_admissions() == [rows[-1]["job_id"]]
    return rows


def test_claim_close_removes_only_the_lock_not_durable_admission(history):
    state, store, _, _ = history
    row = store.admit({})
    path = state.path / "locks" / "jobs" / f"{row['job_id']}.runner.lock"
    with store.claim(row["job_id"], row["nonce"], ProcessIdentity(1, 123456, 1)):
        assert path.exists()
    assert not path.exists()
    assert store.get(row["job_id"])["state"] == "claimed"


def test_cleanup_failure_preserves_the_body_error(history, monkeypatch):
    state, store, _, _ = history
    row = store.admit({})
    path = state.path / "locks" / "jobs" / f"{row['job_id']}.runner.lock"
    unlink = Path.unlink
    def fail(p, *args, **kwargs):
        if p == path:
            raise OSError("injected cleanup failure")
        return unlink(p, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail)
    with pytest.raises(ValueError, match="primary") as caught:
        with store.claim(row["job_id"], row["nonce"], ProcessIdentity(1, 123456, 1)):
            raise ValueError("primary")
    assert any("cleanup also failed" in note for note in caught.value.__notes__)
    assert path.exists() and store.get(row["job_id"])["state"] == "claimed"


@pytest.mark.skipif(sd.os.name != "nt", reason="native Windows junction")
def test_lock_parent_reparse_is_refused_before_any_lock_write(history, tmp_path):
    state, store, _, _ = history
    # locks/jobs is not held by the console; an outside junction here must be
    # rejected before the runner's lock file is created, not by later inspect.
    outside = tmp_path / "outside-locks"
    outside.mkdir()
    import _winapi
    _winapi.CreateJunction(str(outside), str(state.path / "locks" / "jobs"))
    with pytest.raises(sd.StateDirRefused, match="link or junction"):
        store._lock(Path("jobs") / "fixture.runner.lock", retry=False)
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(sd.os.name != "nt", reason="Windows permissions")
@pytest.mark.parametrize("operation", ["get", "events", "claim"])
def test_store_checks_the_selected_job_command_privacy(history, operation):
    state, store, _, _ = history
    row = store.admit({})
    command = state.path / "jobs" / row["job_id"] / "command.json"
    acl._icacls(str(command), "/grant", "*S-1-5-11:R")
    sd.inspect_state_dir(state.path, expected_identity=state.identity, environ={})  # no history walk
    with pytest.raises(sd.StateDirRefused, match="not private"):
        if operation == "claim":
            store.claim(row["job_id"], row["nonce"], ProcessIdentity(1, 123456, 1))
        else:
            getattr(store, operation)(row["job_id"])


def test_archive_requires_explicit_private_directory(history):
    state, store, service, clock = history
    row = expired(store, clock)[0]
    with pytest.raises(ar.ArchiveRefused, match="missing"):
        service.archive(keep=0)
    assert not service.directory.exists()
    assert (state.path / "jobs" / row["job_id"]).exists()
    service.initialize()
    assert service.archive(keep=0)


def test_count_retention_preserves_rows_events_and_verified_cold_reads(history):
    state, store, service, clock = history
    rows = expired(store, clock, 4)
    original = {r["job_id"]: service.read_command(r["job_id"]) for r in rows}
    service.initialize()
    name = service.archive(keep=1)
    assert name and len(list(service.directory.iterdir())) == 1
    assert [p.name for p in (state.path / "jobs").iterdir()] == [rows[-1]["job_id"]]
    for row in rows:
        assert service.read_command(row["job_id"]) == original[row["job_id"]]
        assert store.get(row["job_id"])["state"] == "admission_expired"
    assert [e["event"] for e in store.events(rows[0]["job_id"])] == [
        "admitted", "admission_expired", "archived", "archive_cleanup"]
    assert service.cleanup() == []


def test_age_retention_archives_even_below_the_count_limit(history):
    state, store, service, clock = history
    row = expired(store, clock)[0]
    service.initialize()
    assert service.archive(keep=200, age_ms=0)
    assert not (state.path / "jobs" / row["job_id"]).exists()


def test_batches_pack_by_bytes_instead_of_refusing_all_large_history(history, monkeypatch):
    state, store, service, clock = history
    rows = expired(store, clock, 3)
    sizes = [(state.path / "jobs" / row["job_id"] / "command.json").stat().st_size for row in rows]
    monkeypatch.setattr(ar, "MAX_BATCH_BYTES", max(sizes))
    service.initialize()
    names = [service.archive(keep=0) for _ in rows]
    assert len(set(names)) == 3 and all(names)
    assert service.archive(keep=0) is None


def test_batches_pack_member_count_instead_of_refusing_segmented_history(history, monkeypatch):
    state, store, service, clock = history
    rows = expired(store, clock, 3)
    for row in rows:
        (state.path / "jobs" / row["job_id"] / "fixture.log").write_bytes(b"small log")
    monkeypatch.setattr(ar, "MAX_MEMBERS", 3)
    service.initialize()
    names = [service.archive(keep=0) for _ in rows]
    assert len(set(names)) == 3 and all(names)
    assert service.archive(keep=0) is None
    assert all(service.read_command(row["job_id"]) for row in rows)


def test_active_and_held_runner_jobs_are_never_archived(history):
    state, store, service, clock = history
    service.initialize()
    row = store.admit({})
    assert service.archive(keep=0, age_ms=0) is None
    with store.claim(row["job_id"], row["nonce"], ProcessIdentity(1, 123456, 1)):
        # Models the terminal-commit-before-runner-finally boundary of M3c.
        with dal.connection_scope(state.database) as db, dal.transaction(db):
            db.execute("UPDATE jobs SET state='failed' WHERE job_id=?", (row["job_id"],))
        assert service.archive(keep=0, age_ms=0) is None
        assert (state.path / "jobs" / row["job_id"]).exists()
    assert service.archive(keep=0, age_ms=0)


@pytest.mark.parametrize("status", ["admitted", "claimed", "running", "uncertain"])
def test_every_admission_holding_state_is_skipped_by_retention(history, status):
    state, store, service, _ = history
    row = store.admit({})
    with dal.connection_scope(state.database) as db, dal.transaction(db):
        db.execute("UPDATE jobs SET state=? WHERE job_id=?", (status, row["job_id"]))
    service.initialize()
    assert service.archive(keep=0, age_ms=0) is None
    assert (state.path / "jobs" / row["job_id"]).exists()


def test_archive_event_failure_leaves_orphan_batch_and_hot_evidence(history):
    state, store, service, clock = history
    row = expired(store, clock)[0]
    service.initialize()
    with dal.connection_scope(state.database) as db, dal.transaction(db):
        db.execute("CREATE TRIGGER deny_archive BEFORE INSERT ON job_events WHEN NEW.event='archived' "
                   "BEGIN SELECT RAISE(ABORT,'injected event failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        service.archive(keep=0)
    assert len(list(service.directory.glob("*.zip"))) == 1
    assert (state.path / "jobs" / row["job_id"] / "command.json").exists()
    with dal.connection_scope(state.database, readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM job_archives").fetchone()[0] == 0
    assert service.cleanup() == []  # an unreferenced batch is never authority


def test_archive_lock_cleanup_cannot_mask_the_original_event_failure(history, monkeypatch):
    state, store, service, clock = history
    expired(store, clock)
    service.initialize()
    with dal.connection_scope(state.database) as db, dal.transaction(db):
        db.execute("CREATE TRIGGER deny_archive BEFORE INSERT ON job_events WHEN NEW.event='archived' "
                   "BEGIN SELECT RAISE(ABORT,'primary event failure'); END")
    close = store._close_runner_lock
    def cleanup_failure(*args):
        close(*args)
        raise OSError("secondary cleanup")
    monkeypatch.setattr(store, "_close_runner_lock", cleanup_failure)
    with pytest.raises(sqlite3.IntegrityError, match="primary event failure") as caught:
        service.archive(keep=0)
    assert any("secondary cleanup" in note for note in caught.value.__notes__)


def test_committed_pointer_allows_cleanup_after_restart(history, monkeypatch):
    state, store, service, clock = history
    rows = expired(store, clock, 2)
    service.initialize()
    cleanup = service._cleanup_batch
    def crash(*args):
        raise OSError("after pointer commit")
    monkeypatch.setattr(service, "_cleanup_batch", crash)
    with pytest.raises(OSError, match="pointer commit"):
        service.archive(keep=0)
    assert all((state.path / "jobs" / row["job_id"]).exists() for row in rows)
    monkeypatch.setattr(service, "_cleanup_batch", cleanup)
    restarted = JobHistory(store)
    assert set(restarted.cleanup()) == {row["job_id"] for row in rows}
    assert restarted.cleanup() == []


def test_partial_deletion_resumes_from_committed_verified_copy(history, monkeypatch):
    state, store, service, clock = history
    row = expired(store, clock)[0]
    original = service.read_command(row["job_id"])
    service.initialize()
    import comic_automation.web.jobs.history as module
    remove = module.shutil.rmtree
    target = state.path / "jobs" / row["job_id"]
    def partial(path, *args, **kwargs):
        if Path(path) == target:
            (target / "command.json").unlink()
            raise OSError("interrupted hot deletion")
        return remove(path, *args, **kwargs)
    monkeypatch.setattr(module.shutil, "rmtree", partial)
    with pytest.raises(OSError, match="interrupted"):
        service.archive(keep=0)
    monkeypatch.setattr(module.shutil, "rmtree", remove)
    assert service.read_command(row["job_id"]) == original
    assert service.cleanup() == [row["job_id"]]
    assert not target.exists()


def test_corrupt_committed_batch_never_removes_hot_files_or_falls_back(history, monkeypatch):
    state, store, service, clock = history
    row = expired(store, clock)[0]
    service.initialize()
    cleanup = service._cleanup_batch
    monkeypatch.setattr(service, "_cleanup_batch", lambda *args: [])
    name = service.archive(keep=0)
    monkeypatch.setattr(service, "_cleanup_batch", cleanup)
    with (service.directory / name).open("ab") as stream:
        stream.write(b"corrupt")
    with pytest.raises(ar.ArchiveRefused, match="digest mismatch"):
        service.cleanup()
    with pytest.raises(ar.ArchiveRefused, match="digest mismatch"):
        service.read_command(row["job_id"])
    assert (state.path / "jobs" / row["job_id"] / "command.json").exists()


@pytest.mark.skipif(sd.os.name != "nt", reason="Windows permissions")
def test_cold_reads_check_the_batch_permissions(history):
    state, store, service, clock = history
    row = expired(store, clock)[0]
    service.initialize()
    name = service.archive(keep=0)
    acl._icacls(str(service.directory / name), "/grant", "*S-1-5-11:R")
    assert store.get(row["job_id"])["state"] == "admission_expired"  # metadata does not read the batch
    with pytest.raises(sd.StateDirRefused, match="not private"):
        service.read_command(row["job_id"])


@pytest.mark.skipif(sd.os.name != "nt", reason="Windows permissions")
def test_archive_checks_empty_source_directory_permissions(history):
    state, store, service, clock = history
    row = expired(store, clock)[0]
    empty = state.path / "jobs" / row["job_id"] / "empty"
    empty.mkdir()
    acl._icacls(str(empty), "/grant", "*S-1-5-11:F")
    service.initialize()
    with pytest.raises(sd.StateDirRefused, match="not private"):
        service.archive(keep=0)
    assert empty.exists()


@pytest.mark.parametrize("sql", ["UPDATE job_archives SET batch_file='changed'", "DELETE FROM job_archives"])
def test_archive_pointers_are_immutable(history, sql):
    state, store, service, clock = history
    expired(store, clock)
    service.initialize()
    service.archive(keep=0)
    with dal.connection_scope(state.database) as db, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with dal.transaction(db):
            db.execute(sql)


def test_history_past_old_ceiling_has_bounded_get_checks_and_restart_cost(history, monkeypatch):
    state, store, service, clock = history
    first = store.admit({})
    clock[0] += CLAIM_BUDGET_MS
    store.expire_admissions()
    service.initialize()
    calls = []
    real = acl.privacy_problems
    monkeypatch.setattr(acl, "privacy_problems", lambda path: (calls.append(str(path)), real(path))[1])
    # Hold one connection so WAL sidecar presence is stable for both measurements.
    with dal.connection_scope(state.database) as db:
        store.get(first["job_id"])
        calls.clear()
        started = time.perf_counter()
        store.get(first["job_id"])
        small, small_checks = time.perf_counter() - started, len(calls)
        template = dict(db.execute("SELECT * FROM jobs WHERE job_id=?", (first["job_id"],)).fetchone())
        columns = [name for name in template if name != "admission_seq"]
        body = json.loads((state.path / "jobs" / first["job_id"] / "command.json").read_bytes())
        with dal.transaction(db):
            for n in range(1, 1001):
                job_id, nonce = f"{n:026d}", f"{n:032x}"
                payload = json.dumps({**body, "job_id": job_id, "nonce": nonce}, sort_keys=True, separators=(",", ":")).encode()
                root = state.path / "jobs" / job_id
                root.mkdir()
                (root / "command.json").write_bytes(payload)
                item = {**template, "job_id": job_id, "nonce": nonce,
                        "command_digest": hashlib.sha256(payload).hexdigest()}
                db.execute(f"INSERT INTO jobs({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                           tuple(item[name] for name in columns))
        assert sum(1 for _ in (state.path / "jobs").rglob("*")) > 2000
        calls.clear()
        started = time.perf_counter()
        store.get(first["job_id"])
        large, large_checks = time.perf_counter() - started, len(calls)
        assert large_checks <= small_checks + 3, (small_checks, large_checks)
        assert large < small * 3 + 0.25, (small, large)
        print(json.dumps({"small_get_ms": round(small * 1000, 3), "large_get_ms": round(large * 1000, 3),
                          "small_privacy_checks": small_checks, "large_privacy_checks": large_checks,
                          "terminal_jobs": 1001}))
    state.close()
    reopened = sd.open_state_dir(state.path, environ={})
    reopened.close()
    batches = []
    while True:
        name = service.archive(keep=200, batch_size=200)
        if name is None:
            break
        batches.append(name)
    assert len(batches) == 5
    assert len(list((state.path / "jobs").iterdir())) == 200
    assert service.read_command(first["job_id"])
