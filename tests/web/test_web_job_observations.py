"""Disposable ledger/log witnesses for D3's additive progress consumer."""
from contextlib import contextmanager
import sqlite3

import pytest

from comic_automation.database import dal
from comic_automation.web.jobs import logs as lg
from comic_automation.web.jobs.log_service import JobLogs
from comic_automation.web.jobs.observations import JobObservations
from comic_automation.web.jobs.store import JobRefused, ProcessIdentity
from test_web_log_service import service, written
from test_web_runner_receipts import finished, map_exit, publish


ONE = b"CBZ_PROGRESS 1/4 25% first\n"
TWO = b"CBZ_PROGRESS 2/4 50% second\n"


def ledger_rows(store):
    with dal.connection_scope(store.database, readonly=True) as db:
        return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
                for table in ("jobs", "job_events", "console_meta")}


@contextmanager
def active(service, **options):
    _, store, row, source = service
    with store.claim(row["job_id"], row["nonce"], ProcessIdentity(1, 123456, 1)) as claim:
        disk = source.create_writer(claim, **options)
        observations = JobObservations(store)
        observations.initialize()
        yield disk, observations, lambda **kw: observations.consume_once(row["job_id"], row["nonce"], **kw)


def test_snapshot_is_read_only_and_missing_logs_are_not_eof(service, monkeypatch):
    _, store, row, _ = service
    obs = JobObservations(store)
    obs.initialize()
    before = ledger_rows(store)
    initial = obs.snapshot(row["job_id"], row["nonce"])
    assert initial == dict(generation=0, cursor="0", proven_offset="0", log_status="unobserved",
                           eof=False, progress=None, gap_count=0, last_gap=None)
    with pytest.raises(lg.LogRefused):
        obs.consume_once(row["job_id"], row["nonce"])
    monkeypatch.setattr(JobLogs, "read_page", lambda *a, **kw: pytest.fail("reader parsed logs"))
    assert obs.snapshot(row["job_id"], row["nonce"]) == dict(initial, generation=1, log_status="unavailable")
    assert ledger_rows(store) == before
    with dal.connection_scope(store.database, readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 1


def test_bounded_catchup_survives_restart_without_repeating_progress(service, monkeypatch):
    _, store, row, _ = service
    with active(service) as (disk, obs, consume):
        disk.append(0, ONE + TWO)
        before = ledger_rows(store)
        calls = []
        read_page = JobLogs.read_page
        def bounded(source, **kw):
            calls.append(kw)
            # The observation consumer has released its admission ownership.
            with store._home(job_id=row["job_id"]):
                pass
            return read_page(source, **kw)
        monkeypatch.setattr(JobLogs, "read_page", bounded)
        first = consume(limit=1)
        assert first["generation"] == 1 and first["cursor"] == str(len(ONE))
        assert first["progress"] == dict(done=1, total=4, percent=25, label="first", cursor=str(len(ONE)))
        restarted = JobObservations(store)
        restarted.initialize()
        second = restarted.consume_once(row["job_id"], row["nonce"], limit=1)
        assert second["generation"] == 2 and second["progress"]["done"] == 2
        assert second["cursor"] == str(len(ONE + TWO)) and not second["eof"]
        assert consume(limit=1) == second
        assert calls == [dict(cursor="0", limit=1), dict(cursor=str(len(ONE)), limit=1),
                         dict(cursor=str(len(ONE + TWO)), limit=1)]
        assert ledger_rows(store) == before  # lifecycle, audit and clock are untouched


def test_partial_live_record_waits_and_unknown_tail_is_never_eof(service):
    with active(service) as (disk, _, consume):
        disk.append(0, ONE[:-1])
        first = consume()
        assert first["cursor"] == "0" and first["progress"] is None and not first["eof"]
        assert first["proven_offset"] == str(len(ONE) - 1) and first["log_status"] == "live"
        disk.append(len(ONE) - 1, b"\n")
        assert consume()["progress"]["done"] == 1
        disk.finish(len(ONE), complete=False)
        _, store, row, _ = service
        with store._home(job_id=row["job_id"]), store._write() as db:
            db.execute("UPDATE jobs SET state='failed' WHERE job_id=?", (row["job_id"],))
        last = consume()
        assert last["log_status"] == "unknown_tail" and not last["eof"]
        assert last["progress"]["done"] == 1


def test_fragments_and_gap_adjacent_tails_never_become_progress(service):
    with active(service) as (disk, _, consume):
        # Looks like progress only at the beginning of a continuation fragment.
        prefix = b"x" * lg.CHUNK_BYTES
        disk.append(0, prefix)
        first = consume(limit=1)
        assert first["cursor"] == str(len(prefix)) and first["progress"] is None
        disk.append(len(prefix), ONE)
        assert consume()["progress"] is None
        # Missing preceding byte means this apparent progress may be a line tail.
        start = len(prefix + ONE) + 5
        disk.append(start, TWO)
        missing = consume()
        assert missing["progress"] is None and missing["gap_count"] == 1
        assert missing["last_gap"] == dict(type="gap", **{"from": str(start - 5), "to": str(start)}, reason="dropped")
        disk.append(start + len(TWO), ONE)
        assert consume()["progress"]["done"] == 1


def test_retention_gap_and_exact_large_cursor_persist_without_recounting(service):
    with active(service) as (disk, obs, consume):
        total = 2**63 - 1
        disk.finish(total, complete=True)
        result = consume(limit=1)
        assert result["cursor"] == result["proven_offset"] == str(total)
        assert result["eof"] and result["log_status"] == "complete"
        assert result["gap_count"] == 1 and result["progress"] is None
        assert consume() == result
        _, store, row, _ = service
        assert JobObservations(store).snapshot(row["job_id"], row["nonce"]) == result


def test_real_retention_catchup_and_cold_archive(service):
    written(service, body=ONE + TWO, segment_bytes=len(ONE), keep=1)
    _, store, row, _ = service
    obs = JobObservations(store)
    obs.initialize()
    result = obs.consume_once(row["job_id"], row["nonce"])
    assert result["eof"] and result["gap_count"] == 1
    assert result["last_gap"]["reason"] == "retention"
    # The retained segment starts after a gap; its first apparent line is not trusted.
    assert result["progress"] is None


def test_cold_complete_progress_is_consumed_from_verified_members(service):
    written(service, body=ONE + TWO)
    _, store, row, _ = service
    obs = JobObservations(store)
    obs.initialize()
    result = obs.consume_once(row["job_id"], row["nonce"])
    assert result["eof"] and result["progress"]["done"] == 2
    assert obs.consume_once(row["job_id"], row["nonce"]) == result


def test_a_competing_consumer_cannot_rewind_or_duplicate_a_page(service, monkeypatch):
    _, store, row, _ = service
    with active(service) as (disk, obs, consume):
        disk.append(0, ONE + TWO)
        read_page = JobLogs.read_page
        other = JobObservations(store)
        def interleaved(source, **kw):
            monkeypatch.setattr(JobLogs, "read_page", read_page)
            other.consume_once(row["job_id"], row["nonce"])
            return read_page(source, **kw)
        monkeypatch.setattr(JobLogs, "read_page", interleaved)
        with pytest.raises(JobRefused, match="STALE_OBSERVATION"):
            consume(limit=1)
        current = obs.snapshot(row["job_id"], row["nonce"])
        assert current["generation"] == 1 and current["progress"]["done"] == 2
        assert current["cursor"] == str(len(ONE + TWO))
        assert consume() == current


def test_failed_observation_commit_keeps_cursor_retryable(service):
    _, store, row, _ = service
    with active(service) as (disk, obs, consume):
        disk.append(0, ONE)
        with store._home(), store._write() as db:
            db.execute("CREATE TRIGGER no_observation BEFORE INSERT ON job_observations "
                       "BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match="fixture failure"):
            consume()
        assert obs.snapshot(row["job_id"], row["nonce"])["generation"] == 0
        with store._home(), store._write() as db:
            db.execute("DROP TRIGGER no_observation")
        result = consume()
        assert result["generation"] == 1 and result["progress"]["done"] == 1


@pytest.mark.parametrize("phase", ["read", "commit"])
def test_nonce_mismatch_cannot_publish_observations(service, monkeypatch, phase):
    _, store, row, _ = service
    with active(service) as (disk, obs, consume):
        disk.append(0, ONE)
        if phase == "read":
            with pytest.raises(JobRefused, match="NONCE_MISMATCH"):
                obs.snapshot(row["job_id"], "f" * 32)
            return
        read_page = JobLogs.read_page
        def replaced(source, **kw):
            page = read_page(source, **kw)
            with store._home(), store._write() as db:
                db.execute("UPDATE jobs SET nonce=? WHERE job_id=?", ("f" * 32, row["job_id"]))
            return page
        monkeypatch.setattr(JobLogs, "read_page", replaced)
        with pytest.raises(JobRefused, match="NONCE_MISMATCH"):
            consume()
        with dal.connection_scope(store.database, readonly=True) as db:
            assert db.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 0


def test_regressing_log_proof_is_refused_without_advancing(service, monkeypatch):
    with active(service) as (disk, _, consume):
        disk.append(0, ONE[:-1])
        first = consume()
        read_page = JobLogs.read_page
        monkeypatch.setattr(JobLogs, "read_page", lambda *a, **kw:
                            dict(read_page(*a, **kw), proven_offset="0"))
        with pytest.raises(lg.LogRefused, match="OBSERVATION_LOG_REGRESSED"):
            consume()
        monkeypatch.setattr(JobLogs, "read_page", read_page)
        assert consume() == first


@pytest.mark.parametrize("limit", [0, 1001, True])
def test_consumer_cannot_raise_page_bounds(service, limit):
    _, store, row, _ = service
    with active(service) as (_, obs, consume):
        before = obs.snapshot(row["job_id"], row["nonce"])
        with pytest.raises(lg.LogRefused, match="INVALID_LOG_PAGE"):
            consume(limit=limit)
        assert obs.snapshot(row["job_id"], row["nonce"]) == before


@pytest.mark.parametrize("nonce", [None, "", "F" * 32, "f" * 31])
def test_invalid_nonce_is_refused_before_storage(service, nonce):
    _, store, row, _ = service
    with pytest.raises(JobRefused, match="INVALID_NONCE"):
        JobObservations(store).snapshot(row["job_id"], nonce)


def test_missing_job_is_not_an_empty_observation(service):
    _, store, row, _ = service
    missing = row["job_id"][:-1] + ("0" if row["job_id"][-1] != "0" else "1")
    obs = JobObservations(store)
    obs.initialize()
    with pytest.raises(JobRefused, match="JOB_NOT_FOUND"):
        obs.snapshot(missing, row["nonce"])


def test_unrelated_output_keeps_last_progress_but_advances_parse_cursor(service):
    with active(service) as (disk, _, consume):
        disk.append(0, ONE)
        first = consume()
        disk.append(len(ONE), b"ordinary output\n")
        second = consume()
        assert second["progress"] == first["progress"]
        assert second["cursor"] == str(len(ONE + b"ordinary output\n"))
        assert second["generation"] == first["generation"] + 1


def test_generation_exhaustion_refuses_without_advancing_parse_cursor(service):
    _, store, row, _ = service
    with active(service) as (disk, obs, consume):
        disk.append(0, ONE)
        consume()
        with store._home(), store._write() as db:
            db.execute("UPDATE job_observations SET generation=?", (2**63 - 1,))
        before = obs.snapshot(row["job_id"], row["nonce"])
        disk.append(len(ONE), TWO)
        with pytest.raises(JobRefused, match="OBSERVATION_GENERATION_EXHAUSTED"):
            consume()
        assert obs.snapshot(row["job_id"], row["nonce"]) == before


def test_unreadable_evidence_clears_historical_eof_without_inventing_loss(service, monkeypatch):
    with active(service) as (disk, obs, consume):
        disk.append(0, ONE)
        disk.finish(len(ONE), complete=True)
        before = consume()
        assert before["eof"]
        read_page = JobLogs.read_page
        def unreadable(*args, **kwargs):
            raise lg.LogRefused("MISSING_MAPPED_BYTES")
        monkeypatch.setattr(JobLogs, "read_page", unreadable)
        with pytest.raises(lg.LogRefused, match="MISSING_MAPPED_BYTES"):
            consume()
        _, store, row, _ = service
        unavailable = obs.snapshot(row["job_id"], row["nonce"])
        assert unavailable == dict(before, generation=before["generation"] + 1,
                                   log_status="unavailable", eof=False)
        with pytest.raises(lg.LogRefused):
            consume()
        assert obs.snapshot(row["job_id"], row["nonce"]) == unavailable
        monkeypatch.setattr(JobLogs, "read_page", read_page)
        assert consume() == dict(before, generation=unavailable["generation"] + 1)


def test_schema_v1_runner_keeps_its_version_fence_and_finalizes_after_additive_init(finished):
    # The claim and native child/pump predate the new table. store.py, receipts.py
    # and log_service.py are unchanged from the pre-D3 ee13ad9 baseline.
    store, claim, running, child, pump = finished
    before = ledger_rows(store)
    obs = JobObservations(store)
    obs.initialize()
    obs.initialize()
    result = obs.consume_once(claim.job_id, claim.nonce)
    assert result["eof"] and result["progress"] is None
    assert ledger_rows(store) == before
    publish(finished)
    final = store.finalize(claim, running["state_version"], child, pump, outcome_mapper=map_exit)
    assert final["state"] == "succeeded" and final["state_version"] == running["state_version"] + 1
    assert obs.snapshot(claim.job_id, claim.nonce) == result
    with dal.connection_scope(store.database, readonly=True) as db:
        assert db.execute("SELECT value FROM console_meta WHERE key='schema_version'").fetchone()[0] == "1"
