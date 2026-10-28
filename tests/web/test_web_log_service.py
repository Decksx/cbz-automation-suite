"""Real private homes, matching claims and ledger-pinned cold log pages."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import zipfile

import pytest

from comic_automation.database import dal
from comic_automation.web import state_dir as sd, acl
from comic_automation.web.jobs import archives as ar, logs as lg
from comic_automation.web.jobs.history import JobHistory
from comic_automation.web.jobs.log_service import JobLogs
from comic_automation.web.jobs.store import JobStore, ProcessIdentity, LockBusy
from test_web_job_store import fake_registry
from web_support import private_dir


@pytest.fixture
def service(tmp_path):
    state = sd.open_state_dir(private_dir(tmp_path / "state"), environ={})
    store = JobStore(state, verifier=fake_registry, watcher_observation=lambda: False, clock_ms=lambda: 1000)
    row = store.admit({})
    logs = JobLogs(store, row["job_id"])
    yield state, store, row, logs
    state.close()


def written(service, body=b"first\nsecond\n", *, retain_hot=False, monkeypatch=None, **options):
    state, store, row, logs = service
    with store.claim(row["job_id"], row["nonce"], ProcessIdentity(1, 123456, 1)) as claim:
        disk = logs.create_writer(claim, **options)
        disk.append(0, body)
        disk.finish(len(body), complete=True)
    with dal.connection_scope(store.database) as db, dal.transaction(db):
        db.execute("UPDATE jobs SET state='failed' WHERE job_id=?", (row["job_id"],))
    history = JobHistory(store)
    history.initialize()
    if retain_hot:
        monkeypatch.setattr(history, "_cleanup_batch", lambda *args: [])
    name = history.archive(keep=0)
    return history.directory / name


def test_matching_claim_is_required_and_close_does_not_grant_new_writer(service):
    _, store, row, logs = service
    with store.claim(row["job_id"], row["nonce"], ProcessIdentity(1, 123456, 1)) as claim:
        nonce = claim.nonce
        claim.nonce = "f" * 32
        with pytest.raises(lg.LogRefused, match="MATCHING"):
            logs.create_writer(claim)
        claim.nonce = nonce
        disk = logs.create_writer(claim)
        disk.append(0, b"one\n")
        assert logs.read_page()["records"][0]["text"] == "one\n"
    with pytest.raises(lg.LogRefused, match="MATCHING"):
        logs.create_writer(claim)
    with pytest.raises(lg.LogRefused, match="LOG_CLAIM_CLOSED"):
        disk.append(4, b"late\n")


def test_cold_pages_use_durable_member_pins_without_whole_batch_hash(service, monkeypatch):
    state, store, row, logs = service
    batch = written(service)
    assert not logs.directory.exists()
    # Prove the new API never falls back to the old full-batch digest reader.
    monkeypatch.setattr(ar, "_hash", lambda *args, **kwargs: pytest.fail("whole batch hash during paging"))
    first = logs.read_page(limit=1)
    assert first["records"][0]["text"] == "first\n" and first["next_cursor"] == "6"
    second = logs.read_page(cursor=first["next_cursor"])
    assert second["records"][0]["text"] == "second\n" and second["eof"]
    with dal.connection_scope(store.database, readonly=True) as db:
        pins = db.execute("SELECT * FROM job_archive_members WHERE job_id=?", (row["job_id"],)).fetchall()
    assert {p["member"] for p in pins} == {"command.json", "log/index.json", "log/seg-0000000000000000.log"}


def test_member_is_verified_once_per_page_instead_of_once_per_line(service, monkeypatch):
    written(service, body=b"line\n" * 1000)
    calls = []
    original = ar.read_pinned_member
    def counted(*args):
        calls.append(args[2])
        return original(*args)
    monkeypatch.setattr(ar, "read_pinned_member", counted)
    output = service[3].read_page()
    assert len(output["records"]) == 1000
    assert calls == ["log/index.json", "log/seg-0000000000000000.log"]


def test_cold_page_lookahead_does_not_thrash_adjacent_segments(service, monkeypatch):
    written(service, body=b"line\n" * 1000, segment_bytes=4096)
    calls = []
    original = ar.read_pinned_member
    def counted(*args):
        calls.append(args[2])
        return original(*args)
    monkeypatch.setattr(ar, "read_pinned_member", counted)
    output = service[3].read_page()
    assert len(output["records"]) == 1000 and output["eof"]
    assert calls == ["log/index.json", "log/seg-0000000000000000.log", "log/seg-0000000000001000.log"]


def rewrite_member(path, name, change):
    with zipfile.ZipFile(path) as batch:
        files = {n: batch.read(n) for n in batch.namelist()}
    files[name] = change(files[name])
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as batch:
        for name, body in files.items():
            batch.writestr(name, body)


@pytest.mark.parametrize("member", ["index.json", "seg-0000000000000000.log"])
def test_tampered_cold_member_refuses_even_with_hot_copy(service, monkeypatch, member):
    batch = written(service, retain_hot=True, monkeypatch=monkeypatch)
    rewrite_member(batch, service[2]["job_id"] + "/log/" + member,
                   lambda data: bytes([data[0] ^ 1]) + data[1:])
    with pytest.raises(ar.ArchiveRefused, match="digest mismatch"):
        service[3].read_page()
    assert service[3].directory.exists()


def test_hot_corruption_outside_requested_slice_is_not_hidden(service, monkeypatch):
    written(service, retain_hot=True, monkeypatch=monkeypatch)
    path = service[3].directory / "seg-0000000000000000.log"
    path.write_bytes(b"first\ncorrupt\n")
    with pytest.raises(lg.LogRefused, match="DISAGREEMENT"):
        service[3].read_page(limit=1)


def test_pinned_member_evidence_is_sql_immutable(service):
    written(service)
    for sql in ("UPDATE job_archive_members SET sha256='changed'", "DELETE FROM job_archive_members"):
        with dal.connection_scope(service[1].database) as db, pytest.raises(sqlite3.IntegrityError, match="immutable"):
            with dal.transaction(db):
                db.execute(sql)


def test_old_archive_without_member_evidence_refuses_fast_path(service):
    # Model an old schema's batch by dropping only the new fixture table's
    # deletion trigger. Production APIs never delete or backfill these rows.
    written(service)
    with dal.connection_scope(service[1].database) as db, dal.transaction(db):
        db.execute("DROP TRIGGER archive_members_no_delete")
        db.execute("DELETE FROM job_archive_members")
    with pytest.raises(lg.LogRefused, match="NOT_PINNED"):
        service[3].read_page()
    assert JobHistory(service[1]).read_command(service[2]["job_id"])


@pytest.mark.skipif(os.name != "nt", reason="Windows permissions")
@pytest.mark.parametrize("target", ["index", "segment", "archive"])
def test_selected_log_paths_and_cold_batch_are_private(service, target):
    if target == "archive":
        path = written(service)
    else:
        _, store, row, logs = service
        with store.claim(row["job_id"], row["nonce"], ProcessIdentity(1, 123456, 1)) as claim:
            disk = logs.create_writer(claim)
            disk.append(0, b"private\n")
            disk.finish(8, complete=True)
        path = logs.directory / ("index.json" if target == "index" else "seg-0000000000000000.log")
    acl._icacls(str(path), "/grant", "*S-1-5-11:R")
    with pytest.raises(sd.StateDirRefused, match="not private"):
        service[3].read_page()


@pytest.mark.skipif(os.name != "nt", reason="native junction")
def test_log_subdirectory_junction_refuses_before_writer_access(service, tmp_path):
    import _winapi
    outside = tmp_path / "outside"
    outside.mkdir()
    _, store, row, logs = service
    with store.claim(row["job_id"], row["nonce"], ProcessIdentity(1, 123456, 1)) as claim:
        _winapi.CreateJunction(str(outside), str(logs.directory))
        with pytest.raises(sd.StateDirRefused, match="link or junction"):
            logs.create_writer(claim)
        logs.directory.rmdir()  # remove the fixture junction before claim cleanup
    assert list(outside.iterdir()) == []


def test_claim_runner_lock_creation_is_inside_admission_exclusion(service, monkeypatch):
    state, store, row, _ = service
    contender = JobStore(state, verifier=fake_registry, watcher_observation=lambda: False,
                         clock_ms=lambda: 1000, lock_timeout=0)
    entered, proceed = threading.Event(), threading.Event()
    original = store._lock
    claims, errors = [], []
    def paused(relative, **kwargs):
        if str(relative).endswith(".runner.lock"):
            entered.set()
            assert proceed.wait(5)
        return original(relative, **kwargs)
    monkeypatch.setattr(store, "_lock", paused)
    def claim():
        try:
            claims.append(store.claim(row["job_id"], row["nonce"], ProcessIdentity(1, 123456, 1)))
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=claim)
    thread.start()
    try:
        assert entered.wait(5)
        # Moving _lock outside _home removes this real OS exclusion. The hook
        # fixes the exact interleaving; no sleep or scheduler race is assumed.
        with pytest.raises(LockBusy):
            with contender._home():
                pass
    finally:
        proceed.set()
        thread.join(5)
        for claim in claims:
            claim.close()
    assert not thread.is_alive() and not errors


def test_codec_verification_compares_supplied_ledger_command_digest(tmp_path):
    job_id = "00000000000000000000000001"
    root, target = tmp_path / "job", tmp_path / "archive"
    root.mkdir()
    target.mkdir()
    command = b"{}"
    (root / "command.json").write_bytes(command)
    job = ar.ArchiveJob(job_id, "failed", hashlib.sha256(command).hexdigest(), root)
    receipt = ar.write_batch(target, "00000000000000000000000002", [job], 1000)
    other = ar.ArchiveJob(job_id, "failed", "f" * 64, root)
    with pytest.raises(ar.ArchiveRefused, match="ledger evidence"):
        ar.verify_batch(target / receipt.file_name, receipt.sha256, jobs={job_id: other})
