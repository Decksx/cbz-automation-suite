"""A shared publisher gets the exact bounded page only after durable catch-up."""
from contextlib import contextmanager
import sqlite3
import threading

import pytest

from comic_automation.web.jobs import log_service, logs
from comic_automation.web.jobs.log_service import JobLogs
from comic_automation.web.jobs.observations import JobObservations
from comic_automation.web.jobs.store import JobRefused
from test_web_job_observations import active, ledger_rows, ONE, TWO
from test_web_log_service import service, written


def test_single_read_page_is_published_after_commit_and_handle_release(service, monkeypatch):
    _, store, row, _ = service
    with active(service) as (disk, consumer, _):
        disk.append(0, ONE + TWO)
        before = ledger_rows(store)
        pages, handles = [], []
        read_page, shared_read = JobLogs.read_page, log_service.shared_read
        @contextmanager
        def tracked(path):
            with shared_read(path) as stream:
                handles.append(stream)
                yield stream
        def read(source, **kwargs):
            result = read_page(source, **kwargs)
            pages.append(result)
            return result
        monkeypatch.setattr(log_service, "shared_read", tracked)
        monkeypatch.setattr(JobLogs, "read_page", read)
        batch = consumer.consume_page(row["job_id"], row["nonce"], limit=1)
        assert len(pages) == 1 and batch.page == pages[0]
        assert handles and all(stream.closed for stream in handles)
        # Re-entering admission and reading the persisted snapshot after return
        # witnesses that publication is outside the commit's ownership.
        assert consumer.snapshot(row["job_id"], row["nonce"]) == batch.observation
        assert batch.page["cursor"] == "0" and batch.page["next_cursor"] == str(len(ONE))
        assert [r["text"] for r in batch.page["records"]] == [ONE.decode()]
        assert batch.observation["generation"] == 1
        assert batch.observation["progress"]["cursor"] == str(len(ONE))
        assert ledger_rows(store) == before


def test_restart_handoff_resumes_next_page_and_legacy_api_stays_snapshot_only(service):
    _, store, row, _ = service
    with active(service) as (disk, consumer, _):
        disk.append(0, ONE + TWO)
        first = consumer.consume_page(row["job_id"], row["nonce"], limit=1)
        restarted = JobObservations(store)
        second = restarted.consume_page(row["job_id"], row["nonce"], limit=1)
        assert first.page["next_cursor"] == second.page["cursor"] == str(len(ONE))
        assert second.page["records"][0]["text"] == TWO.decode()
        assert second.observation["generation"] == 2 and second.observation["progress"]["done"] == 2
        assert restarted.consume_once(row["job_id"], row["nonce"]) == second.observation
        empty = restarted.consume_page(row["job_id"], row["nonce"])
        assert empty.page["records"] == [] and not empty.page["eof"]
        assert empty.observation == second.observation


def test_partial_live_page_is_forwarded_without_inventing_a_record(service):
    _, _, row, _ = service
    with active(service) as (disk, consumer, _):
        disk.append(0, ONE[:-1])
        first = consumer.consume_page(row["job_id"], row["nonce"])
        assert first.page["records"] == [] and first.page["next_cursor"] == "0"
        assert first.observation["cursor"] == "0" and first.observation["progress"] is None
        disk.append(len(ONE) - 1, b"\n")
        second = consumer.consume_page(row["job_id"], row["nonce"])
        assert second.page["records"][0]["text"] == ONE.decode()
        assert second.observation["progress"]["done"] == 1


@pytest.mark.parametrize("retained", [False, True])
def test_cold_page_handoff_keeps_verified_records_and_gaps(service, retained):
    options = dict(segment_bytes=len(ONE), keep=1) if retained else {}
    written(service, body=ONE + TWO, **options)
    _, store, row, _ = service
    consumer = JobObservations(store)
    consumer.initialize()
    batch = consumer.consume_page(row["job_id"], row["nonce"])
    assert batch.page["eof"] and batch.observation["eof"]
    assert batch.page["next_cursor"] == batch.observation["cursor"] == str(len(ONE + TWO))
    if retained:
        assert batch.page["records"][0]["type"] == "gap"
        assert batch.page["records"][0]["reason"] == "retention"
        assert batch.observation["gap_count"] == 1 and batch.observation["progress"] is None
    else:
        assert [record["text"] for record in batch.page["records"]] == [ONE.decode(), TWO.decode()]
        assert batch.observation["progress"]["done"] == 2


@pytest.mark.parametrize("failure", ["read", "commit", "stale"])
def test_failed_batch_never_reaches_the_publisher(service, monkeypatch, failure):
    _, store, row, _ = service
    with active(service) as (disk, consumer, _):
        disk.append(0, ONE + TWO)
        initial = consumer.consume_page(row["job_id"], row["nonce"], limit=1).observation
        if failure == "read":
            def unreadable(*args, **kwargs):
                raise logs.LogRefused("LOG_EVIDENCE_UNAVAILABLE")
            monkeypatch.setattr(JobLogs, "read_page", unreadable)
            expected, message = logs.LogRefused, "LOG_EVIDENCE_UNAVAILABLE"
        elif failure == "commit":
            with store._home(), store._write() as db:
                db.execute("CREATE TRIGGER refuse_batch BEFORE INSERT ON job_observations "
                           "BEGIN SELECT RAISE(ABORT,'fixture batch commit'); END")
            expected, message = sqlite3.IntegrityError, "fixture batch commit"
        else:
            commit = consumer._commit
            def raced(job_id, nonce, before, after):
                # Another observation changes generation before this commit.
                with store._home(), store._write() as db:
                    db.execute("UPDATE job_observations SET generation=generation+1")
                return commit(job_id, nonce, before, after)
            monkeypatch.setattr(consumer, "_commit", raced)
            expected, message = JobRefused, "STALE_OBSERVATION"
        with pytest.raises(expected, match=message):
            consumer.consume_page(row["job_id"], row["nonce"], limit=1)
        after = consumer.snapshot(row["job_id"], row["nonce"])
        assert after["cursor"] == initial["cursor"] and after["progress"] == initial["progress"]
        if failure == "read":
            assert after["log_status"] == "unavailable" and not after["eof"]
        elif failure == "commit":
            assert after == initial
        else:
            assert after == dict(initial, generation=initial["generation"] + 1)


def test_producer_cannot_receive_a_page_while_commit_is_pending(service, monkeypatch):
    _, _, row, _ = service
    with active(service) as (disk, consumer, _):
        disk.append(0, ONE)
        commit = consumer._commit
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []
        def paused(*args):
            entered.set()
            assert release.wait(5)
            return commit(*args)
        def produce():
            try:
                results.append(consumer.consume_page(row["job_id"], row["nonce"]))
            except BaseException as error:
                errors.append(error)
        monkeypatch.setattr(consumer, "_commit", paused)
        thread = threading.Thread(target=produce)
        thread.start()
        try:
            assert entered.wait(5)
            thread.join(.1)  # explicit commit barrier, not a scheduling sleep
            assert thread.is_alive() and results == []
            assert consumer.snapshot(row["job_id"], row["nonce"])["cursor"] == "0"
        finally:
            release.set()
            thread.join(5)
        assert not thread.is_alive() and errors == []
        assert len(results) == 1 and results[0].observation["generation"] == 1


@pytest.mark.parametrize("prefix", ["π\n", "📚\n"])
def test_handoff_preserves_source_byte_offsets_for_utf8(service, prefix):
    _, _, row, _ = service
    encoded = prefix.encode()
    with active(service) as (disk, consumer, _):
        disk.append(0, encoded + ONE)
        first = consumer.consume_page(row["job_id"], row["nonce"], limit=1)
        assert first.page["records"][0]["text"] == prefix
        assert first.page["next_cursor"] == first.observation["cursor"] == str(len(encoded))
        second = consumer.consume_page(row["job_id"], row["nonce"], limit=1)
        assert second.page["cursor"] == str(len(encoded))
        assert second.observation["progress"]["cursor"] == str(len(encoded + ONE))
