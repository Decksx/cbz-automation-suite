"""Real private ledgers and bounded worker handshakes; no HTTP or native jobs."""
from contextlib import contextmanager
import threading
import time
from types import SimpleNamespace

import pytest

from comic_automation.web.jobs import stream_manager as streams
from comic_automation.database import dal
from comic_automation.web.jobs.history import JobHistory
from comic_automation.web.jobs.log_service import JobLogs
from comic_automation.web.jobs.store import ProcessIdentity
from test_web_job_observations import active, ledger_rows, ONE
from test_web_log_service import service, written, rewrite_member


def eventually(predicate, timeout=5):
    """Bounded observation, with the condition itself deciding success."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("worker did not reach the expected boundary")
        threading.Event().wait(.01)


@contextmanager
def manager(service, monkeypatch=None, **options):
    value = streams.SharedLogReaders(service[1], **options)
    if monkeypatch is not None:
        monkeypatch.setattr(streams, "ACTIVE_SECONDS", .01)
        monkeypatch.setattr(streams, "IDLE_SECONDS", .025)
    value.initialize()
    try:
        yield value
    finally:
        assert value.close(5), "source worker outlived its private home"


def subscribe(value, service, **options):
    return value.subscribe(service[2]["job_id"], service[2]["nonce"],
                           session_valid=options.pop("session_valid", lambda: True), **options)


def test_eight_subscribers_share_one_read_and_one_encoded_packet(service, monkeypatch):
    with active(service) as (disk, _, _):
        disk.append(0, ONE)
        entered, release = threading.Event(), threading.Event()
        original, calls = JobLogs.read_page, []
        def paused(source, **kw):
            calls.append(kw)
            entered.set()
            assert release.wait(5)
            return original(source, **kw)
        monkeypatch.setattr(JobLogs, "read_page", paused)
        with manager(service) as value:
            try:
                members = [subscribe(value, service) for _ in range(8)]
                assert entered.wait(5)
                original_get, caller = value.store.get, threading.current_thread()
                def guarded_get(*args):
                    if threading.current_thread() is caller:
                        pytest.fail("ninth open did I/O")
                    return original_get(*args)
                monkeypatch.setattr(value.store, "get", guarded_get)
                with pytest.raises(streams.StreamRefused, match="STREAM_LIMIT"):
                    subscribe(value, service)
                release.set()
                packets = [member.receive(5) for member in members]
                assert packets[0] is not None and all(packet is packets[0] for packet in packets)
                # Later scheduled polls may run if the test thread is delayed;
                # they must not repeat the page that produced this packet.
                assert sum(call["cursor"] == "0" and call["limit"] == 1000 for call in calls) == 1
                assert len(calls) <= value.stats()["polls"] and value.stats()["readers"] == 1
                assert packets[0].next_cursor == str(len(ONE))
                # Delivery holds no admission scope or source handle.
                with service[1]._home(job_id=service[2]["job_id"]):
                    pass
            finally:
                release.set()


def test_close_before_iteration_is_idempotent_and_releases_slot_and_reader(service):
    with manager(service) as value:
        member = subscribe(value, service)
        member.close()
        member.close()
        with pytest.raises(StopIteration):
            next(member)
        eventually(lambda: value.stats()["readers"] == 0)
        assert value.stats()["slots"] == 0


def test_denied_session_does_no_store_work_and_failed_open_releases_reservation(service, monkeypatch):
    with manager(service) as value:
        def fail(*args):
            raise RuntimeError("private open failure")
        monkeypatch.setattr(value.store, "get", fail)
        with pytest.raises(streams.StreamRefused, match="SESSION_INVALID"):
            subscribe(value, service, session_valid=lambda: False)
        with pytest.raises(RuntimeError):
            subscribe(value, service)
        assert value.stats() == dict(slots=0, readers=0, reservations=0, polls=0)


def test_session_expiry_discards_pending_delivery_and_releases_idle_reader(service):
    valid = [True]
    with manager(service) as value:
        member = subscribe(value, service, session_valid=lambda: valid[0])
        assert next(member).startswith(b"event: state")
        valid[0] = False  # Progress/status frames are buffered, but must not escape.
        with pytest.raises(StopIteration):
            next(member)
        eventually(lambda: value.stats()["readers"] == 0)
        assert member.reason == "SESSION_INVALID" and value.stats()["slots"] == 0


def test_session_expiry_while_waiting_closes_without_new_source_page(service):
    valid = [True]
    with manager(service) as value:
        member = subscribe(value, service, session_valid=lambda: valid[0])
        assert member.receive(5)
        valid[0] = False
        with pytest.raises(streams.StreamClosed, match="SESSION_INVALID"):
            member.receive(5)
        assert not member.queue


def test_admitted_job_has_state_only_no_log_read_or_observation_write(service, monkeypatch):
    monkeypatch.setattr(JobLogs, "read_page", lambda *a, **kw: pytest.fail("admitted log read"))
    with manager(service) as value:
        before = ledger_rows(service[1])
        member = subscribe(value, service)
        packet = member.receive(5)
        assert packet.cursor == packet.next_cursor == "0"
        assert b'"log_status":"unobserved"' in b"".join(packet.frames)
        assert value.observations.snapshot(service[2]["job_id"], service[2]["nonce"])["generation"] == 0
        assert ledger_rows(service[1]) == before
        with pytest.raises(streams.StreamRefused, match="INVALID_LOG_CURSOR"):
            subscribe(value, service, cursor="1")


def test_full_queue_closes_slow_member_while_fast_member_keeps_every_byte(service, monkeypatch):
    with active(service) as (disk, _, _), manager(service, monkeypatch, queue_pages=1) as value:
        fast, slow = subscribe(value, service), subscribe(value, service)
        fast.receive(5)
        eventually(lambda: len(slow.queue) == 1)
        disk.append(0, ONE)
        packet = fast.receive(5)
        assert packet.next_cursor == str(len(ONE))
        eventually(lambda: slow.closed)
        assert slow.reason == "SLOW_SUBSCRIBER" and not slow.queue
        assert b"SLOW_SUBSCRIBER" in next(slow)
        with pytest.raises(StopIteration):
            next(slow)
        disk.append(len(ONE), b"later\n")
        assert fast.receive(5).next_cursor == str(len(ONE) + 6)
        assert value.stats()["slots"] == 1


def test_replay_is_fair_and_bad_cursor_closes_only_its_subscriber(service, monkeypatch):
    written(service, body=b"first\nsecond\n")
    with manager(service, monkeypatch) as value:
        first = subscribe(value, service)
        assert first.receive(5).next_cursor == "13"
        replay = subscribe(value, service, cursor="6")
        bad = subscribe(value, service, cursor="14")
        packet = replay.receive(5)
        assert packet.cursor == "6" and packet.next_cursor == "13"
        assert b"second" in packet.frames[0]
        eventually(lambda: bad.closed)
        assert bad.reason == "INVALID_LOG_CURSOR" and not first.closed and not replay.closed


def test_unknown_tail_is_not_eof_and_cold_reconnect_uses_source_ids(service, monkeypatch):
    with active(service) as (disk, _, _):
        disk.append(0, b"first\n")
        disk.finish(6, complete=False)
        with service[1]._home(job_id=service[2]["job_id"]), service[1]._write() as db:
            db.execute("UPDATE jobs SET state='failed' WHERE job_id=?", (service[2]["job_id"],))
        with manager(service, monkeypatch) as value:
            packet = subscribe(value, service).receive(5)
            assert b"id: 6\n" in packet.frames[0]
            assert b'"eof":false' in packet.frames[-1] and b"unknown_tail" in packet.frames[-1]


def test_source_failure_closes_with_fixed_error_not_private_exception(service, monkeypatch):
    written(service)
    def failed(*a, **kw):
        raise RuntimeError("PRIVATE-FILE-PATH")
    monkeypatch.setattr(JobLogs, "read_page", failed)
    with manager(service) as value:
        member = subscribe(value, service)
        frame = next(member)
        assert b"LOG_UNAVAILABLE" in frame and b"PRIVATE" not in frame
        with pytest.raises(StopIteration):
            next(member)
        assert value.stats()["slots"] == 0


def test_shutdown_retains_reader_ownership_until_blocked_source_settles(service, monkeypatch):
    written(service)
    entered, release = threading.Event(), threading.Event()
    original = JobLogs.read_page
    def blocked(source, **kw):
        entered.set()
        assert release.wait(5)
        return original(source, **kw)
    monkeypatch.setattr(JobLogs, "read_page", blocked)
    with manager(service) as value:
        try:
            member = subscribe(value, service)
            assert entered.wait(5)
            member.close()
            assert value.stats()["slots"] == value.stats()["readers"] == 1
            with pytest.raises(streams.StreamRefused, match="READER_CLOSING"):
                subscribe(value, service)
            assert value.close(.01) is False
            assert value.stats()["readers"] == 1
        finally:
            release.set()
        assert value.wait_closed(5)


def test_shutdown_retains_an_inflight_open_reservation(service, monkeypatch):
    entered, release, errors = threading.Event(), threading.Event(), []
    with manager(service) as value:
        original = value.store.get
        def blocked(*args):
            entered.set()
            assert release.wait(5)
            return original(*args)
        monkeypatch.setattr(value.store, "get", blocked)
        def opening():
            try:
                subscribe(value, service)
            except Exception as error:
                errors.append(str(error))
        thread = threading.Thread(target=opening)
        thread.start()
        try:
            assert entered.wait(5)
            assert value.close(.01) is False
            assert value.stats()["reservations"] == 1
        finally:
            release.set()
            thread.join(5)
        assert not thread.is_alive() and errors == ["STREAM_MANAGER_CLOSED"]
        assert value.wait_closed(5) and value.stats()["slots"] == 0


def test_thread_start_failure_releases_unstarted_reader_and_slots(service, monkeypatch):
    with manager(service) as value:
        def failed(*args):
            raise RuntimeError("cannot start")
        monkeypatch.setattr(threading.Thread, "start", failed)
        with pytest.raises(streams.StreamRefused, match="READER_START_FAILED"):
            subscribe(value, service)
        assert value.stats() == dict(slots=0, readers=0, reservations=0, polls=0)


def test_idle_cadence_and_admission_cost_do_not_scale_with_subscribers(service, monkeypatch):
    written(service)
    with manager(service) as value:
        original, homes, reads = value.store._home, [], []
        @contextmanager
        def counted(*a, **kw):
            homes.append(time.monotonic())
            with original(*a, **kw) as home:
                yield home
        monkeypatch.setattr(value.store, "_home", counted)
        original_read = JobLogs.read_page
        def read(source, **kw):
            reads.append(time.monotonic())
            return original_read(source, **kw)
        monkeypatch.setattr(JobLogs, "read_page", read)
        members = [subscribe(value, service) for _ in range(8)]
        eventually(lambda: len(reads) >= 3)
        with value.condition:
            polls = value.polls
            assert len(homes) <= 4 * polls + 2 * len(members)
        assert all(b - a >= .95 for a, b in zip(reads, reads[1:]))
        assert len(reads) <= 3  # Eight subscribers still have one ~1-second cadence.


@pytest.mark.parametrize("size", [0, 5, True, 1.0])
def test_queue_bound_is_not_caller_expandable(service, size):
    with pytest.raises(streams.StreamRefused, match="INVALID_STREAM_QUEUE"):
        streams.SharedLogReaders(service[1], queue_pages=size)


def test_manager_requires_service_initialization_and_refuses_after_close(service):
    value = streams.SharedLogReaders(service[1])
    with pytest.raises(streams.StreamRefused, match="NOT_READY"):
        subscribe(value, service)
    value.initialize()
    assert value.close()
    with pytest.raises(streams.StreamRefused, match="CLOSED"):
        subscribe(value, service)
    with pytest.raises(streams.StreamRefused, match="CLOSED"):
        value.initialize()


def test_truthy_auth_and_session_expiring_during_open_are_refused(service):
    with manager(service) as value:
        with pytest.raises(streams.StreamRefused, match="SESSION_INVALID"):
            subscribe(value, service, session_valid=lambda: 1)
        calls = []
        def expires():
            calls.append(1)
            return len(calls) == 1
        with pytest.raises(streams.StreamRefused, match="SESSION_INVALID"):
            subscribe(value, service, session_valid=expires)
        assert value.stats()["slots"] == value.stats()["readers"] == 0


def test_nonce_mismatch_and_noncanonical_cursor_do_not_leak_slots(service):
    with manager(service) as value:
        with pytest.raises(streams.StreamRefused, match="NONCE_MISMATCH"):
            value.subscribe(service[2]["job_id"], "0" * 32, session_valid=lambda: True)
        with pytest.raises(Exception, match="INVALID_LOG_CURSOR"):
            subscribe(value, service, cursor="00")
        assert value.stats()["slots"] == value.stats()["readers"] == 0


def test_reservation_limit_counts_concurrent_inflight_opens(service, monkeypatch):
    entered, release, errors, count = threading.Event(), threading.Event(), [], []
    with manager(service) as value:
        original = value.store.get
        def blocked(*args):
            with value.condition:
                count.append(1)
                if len(count) == 8:
                    entered.set()
            assert release.wait(5)
            return original(*args)
        monkeypatch.setattr(value.store, "get", blocked)
        def opening():
            try:
                subscribe(value, service)
            except Exception as error:
                errors.append(str(error))
        threads = [threading.Thread(target=opening) for _ in range(8)]
        for thread in threads:
            thread.start()
        try:
            assert entered.wait(5)
            assert value.stats()["reservations"] == 8
            with pytest.raises(streams.StreamRefused, match="STREAM_LIMIT"):
                subscribe(value, service)
            assert len(count) == 8
            assert value.close(.01) is False
        finally:
            release.set()
            for thread in threads:
                thread.join(5)
        assert all(not thread.is_alive() for thread in threads)
        assert errors == ["STREAM_MANAGER_CLOSED"] * 8 and value.wait_closed(5)


def test_started_thread_is_retained_if_start_raises_after_birth(service, monkeypatch):
    written(service)
    entered, release = threading.Event(), threading.Event()
    original_read, original_start = JobLogs.read_page, threading.Thread.start
    def blocked(source, **kw):
        entered.set()
        assert release.wait(5)
        return original_read(source, **kw)
    def started_then_failed(thread):
        original_start(thread)
        assert entered.wait(5)
        raise RuntimeError("failure after native thread birth")
    monkeypatch.setattr(JobLogs, "read_page", blocked)
    with manager(service) as value:
        monkeypatch.setattr(threading.Thread, "start", started_then_failed)
        try:
            with pytest.raises(streams.StreamRefused, match="READER_START_FAILED"):
                subscribe(value, service)
            assert value.stats()["slots"] == value.stats()["readers"] == 1
            assert value.close(.01) is False
        finally:
            release.set()
        assert value.wait_closed(5)


def test_active_cadence_has_one_quarter_second_floor(service, monkeypatch):
    # Freeze only the manager's clock. Slow real disk reads cannot accidentally
    # satisfy the cadence floor; every advance must come from its wait branch.
    clock, reads, waits = [100.0], [], []
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(streams, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    with active(service) as (disk, _, _):
        disk.append(0, b"a\n" * 2100)
        original = JobLogs.read_page
        def counted(source, **kw):
            reads.append(clock[0])
            if len(reads) == 3:
                entered.set()
                assert release.wait(5)
            return original(source, **kw)
        monkeypatch.setattr(JobLogs, "read_page", counted)
        with manager(service) as value:
            original_wait = value.condition.wait
            def wait(timeout=None):
                if threading.current_thread().name.startswith("cbz-log-reader-"):
                    waits.append(timeout)
                    clock[0] += timeout
                    return
                return original_wait(timeout)
            monkeypatch.setattr(value.condition, "wait", wait)
            try:
                member = subscribe(value, service)
                assert entered.wait(5)
                assert reads == [100.0, 100.25, 100.5]
                assert waits == [.25, .25]
                packets = [member.receive(5) for _ in range(2)]
                release.set()
                packets.append(member.receive(5))
                assert [packet.next_cursor for packet in packets] == ["2000", "4000", "4200"]
            finally:
                member.close()
                release.set()


def test_reconnect_inside_gap_emits_gap_before_bytes_and_progress_does_not_rewind(service, monkeypatch):
    with active(service) as (disk, _, _), manager(service, monkeypatch) as value:
        disk.append(10, b"tail\n")
        first = subscribe(value, service)
        packet = first.receive(5)
        first.close()
        eventually(lambda: value.stats()["readers"] == 0)
        progress = value.observations.snapshot(service[2]["job_id"], service[2]["nonce"])
        resumed = subscribe(value, service, cursor="5")
        packet = resumed.receive(5)
        assert packet.frames[0].startswith(b"event: gap\nid: 10\n")
        assert packet.frames[1].startswith(b"event: log\nid: 15\n")
        assert value.observations.snapshot(service[2]["job_id"], service[2]["nonce"]) == progress


def test_restart_does_not_deliver_old_buffered_frames(service):
    with manager(service) as first:
        old = subscribe(first, service)
        assert next(old).startswith(b"event: state")
        assert old.pending
        assert first.close(5)
    with manager(service) as second:
        fresh = subscribe(second, service)
        assert next(fresh).startswith(b"event: state")
        with pytest.raises(StopIteration):
            next(old)
        assert old.reason == "SERVER_SHUTDOWN" and not old.pending


def test_heartbeat_waits_fifteen_seconds_and_has_no_event_id(service):
    with manager(service) as value:
        member = subscribe(value, service)
        for _ in range(3):
            next(member)
        start = time.monotonic()
        assert next(member) == b": heartbeat\n\n"
        assert time.monotonic() - start >= 14.95


@pytest.mark.parametrize("timeout", [-1, True, float("nan"), "1"])
def test_invalid_wait_does_not_close_the_service(service, timeout):
    with manager(service) as value:
        with pytest.raises(streams.StreamRefused, match="INVALID_STREAM_WAIT"):
            value.close(timeout)
        assert value.closed is False
        with pytest.raises(streams.StreamRefused, match="INVALID_STREAM_WAIT"):
            value.wait_closed(timeout)
        member = subscribe(value, service)
        with pytest.raises(streams.StreamRefused, match="INVALID_STREAM_WAIT"):
            member.receive(timeout)
        assert not member.closed


def test_nonce_changed_after_open_closes_reader_without_relabeling_old_stream(service, monkeypatch):
    with manager(service, monkeypatch) as value:
        member = subscribe(value, service)
        assert member.receive(5)
        with service[1]._home(job_id=service[2]["job_id"]), service[1]._write() as db:
            db.execute("UPDATE jobs SET nonce=? WHERE job_id=?", ("0" * 32, service[2]["job_id"]))
        eventually(lambda: member.closed)
        assert member.reason == "LOG_UNAVAILABLE" and not member.queue


def test_session_expires_inside_a_wait_without_a_source_notification(service):
    valid, waiting, errors = [True], threading.Event(), []
    with manager(service) as value:
        def authorized():
            if threading.current_thread().name == "test-subscriber-wait":
                waiting.set()
            return valid[0]
        member = subscribe(value, service, session_valid=authorized)
        assert member.receive(5)
        def receive():
            try:
                member.receive(5)
            except streams.StreamClosed as error:
                errors.append(str(error))
        thread = threading.Thread(target=receive, name="test-subscriber-wait")
        thread.start()
        assert waiting.wait(5)
        valid[0] = False
        thread.join(2)
        assert not thread.is_alive() and errors == ["SESSION_INVALID"]


def test_abnormal_worker_exit_closes_subscribers_even_when_exception_is_not_caught(service, monkeypatch):
    written(service)
    class Cancelled(BaseException):
        pass
    cancelled = []
    with manager(service) as value:
        original = value._run
        def capture(reader):
            # Simulate an outer thread supervisor observing cancellation. The
            # reader itself must run its finally; no pytest thread warning.
            try:
                original(reader)
            except Cancelled:
                cancelled.append(True)
        def failed(*a, **kw):
            raise Cancelled()
        monkeypatch.setattr(value, "_run", capture)
        monkeypatch.setattr(JobLogs, "read_page", failed)
        member = subscribe(value, service)
        eventually(lambda: bool(cancelled))
        assert member.closed and member.reason == "LOG_UNAVAILABLE"
        assert value.stats()["slots"] == value.stats()["readers"] == 0


@pytest.mark.parametrize("state", ["admission_expired", "refused"])
def test_never_claimed_terminal_job_refuses_nonzero_cursor_without_retaining_a_slot(service, monkeypatch, state):
    with service[1]._home(job_id=service[2]["job_id"]), service[1]._write() as db:
        db.execute("UPDATE jobs SET state=? WHERE job_id=?", (state, service[2]["job_id"]))
    monkeypatch.setattr(JobLogs, "read_page", lambda *a, **kw: pytest.fail("unobserved source read"))
    with manager(service) as value:
        before = ledger_rows(service[1])
        with pytest.raises(streams.StreamRefused, match="INVALID_LOG_CURSOR"):
            subscribe(value, service, cursor="1")
        assert value.stats()["slots"] == value.stats()["readers"] == 0
        valid = subscribe(value, service)
        packet = valid.receive(5)
        assert packet.cursor == packet.next_cursor == "0"
        assert b'"log_status":"unobserved"' in packet.frames[-1]
        assert ledger_rows(service[1]) == before


def test_closing_readers_keep_capacity_until_twelve_job_probe_settles(service, monkeypatch):
    # Create independent terminal jobs, then hold every source read past close.
    # The ninth attempt must refuse before doing any identity/snapshot I/O.
    _, store, first, _ = service
    rows = [first]
    for number in range(12):
        if number:
            rows.append(store.admit({}))
        with store._home(job_id=rows[-1]["job_id"]), store._write() as db:
            db.execute("UPDATE jobs SET state='failed',runner_pid=1 WHERE job_id=?", (rows[-1]["job_id"],))
    release, entered, threads = threading.Event(), threading.Event(), []
    def blocked(source, **kw):
        threads.append(threading.current_thread())
        entered.set()
        assert release.wait(10)
        raise RuntimeError("private blocked source settled")
    monkeypatch.setattr(JobLogs, "read_page", blocked)
    with manager(service) as value:
        try:
            for row in rows[:8]:
                entered.clear()
                member = value.subscribe(row["job_id"], row["nonce"], session_valid=lambda: True)
                assert entered.wait(5)
                member.close()
            assert value.stats()["readers"] == value.stats()["slots"] == 8
            original = store.get
            def denied_io(*args):
                pytest.fail("capacity refusal did identity I/O")
            monkeypatch.setattr(store, "get", denied_io)
            for row in rows[8:]:
                with pytest.raises(streams.StreamRefused, match="STREAM_LIMIT"):
                    value.subscribe(row["job_id"], row["nonce"], session_valid=lambda: True)
            assert len(threads) == 8 and all(thread.is_alive() for thread in threads)
            assert value.close(.01) is False
            monkeypatch.setattr(store, "get", original)
        finally:
            release.set()
        assert value.wait_closed(5)
        assert value.stats()["slots"] == value.stats()["readers"] == 0


@pytest.mark.parametrize("terminal", [False, True])
def test_claimed_without_writer_delivers_state_and_later_logs_or_terminal_state(service, monkeypatch, terminal):
    _, store, row, source = service
    with store.claim(row["job_id"], row["nonce"], ProcessIdentity(1, 123456, 1)) as claim:
        if terminal:
            with store._home(job_id=row["job_id"]), store._write() as db:
                db.execute("UPDATE jobs SET state='failed' WHERE job_id=?", (row["job_id"],))
        with manager(service, monkeypatch) as value:
            before = ledger_rows(store)
            member = subscribe(value, service)
            packet = member.receive(5)
            assert packet.frames[0].startswith(b"event: state\n")
            assert (b'"state":"failed"' if terminal else b'"state":"claimed"') in packet.frames[0]
            assert b'"log_status":"unobserved"' in packet.frames[-1]
            assert packet.cursor == packet.next_cursor == "0"
            assert value.observations.snapshot(row["job_id"], row["nonce"])["generation"] == 0
            assert ledger_rows(store) == before
            bad = subscribe(value, service, cursor="1")
            eventually(lambda: bad.closed)
            assert bad.reason == "INVALID_LOG_CURSOR" and not member.closed
            if not terminal:
                disk = source.create_writer(claim)
                disk.append(0, ONE)
                packet = member.receive(5)
                if packet.next_cursor == "0":
                    # The worker may observe the valid empty index between
                    # create_writer and append. That status frame is ordered
                    # before the bytes, so consume it rather than assuming the
                    # test thread wins that production race.
                    assert b'"log_status":"live"' in packet.frames[-1]
                    assert b'"eof":false' in packet.frames[-1]
                    packet = member.receive(5)
                assert packet.next_cursor == str(len(ONE))
                assert packet.frames[0].startswith(b"event: log\n")


@pytest.mark.parametrize("empty", [False, True])
def test_observed_hot_index_disappearing_is_unavailable_even_at_zero_cursor(service, monkeypatch, empty):
    with active(service) as (disk, _, _), manager(service, monkeypatch) as value:
        if not empty:
            disk.append(0, ONE)
        member = subscribe(value, service)
        assert member.receive(5)
        assert value.observations.snapshot(service[2]["job_id"], service[2]["nonce"])["generation"] > 0
        (service[3].directory / "index.json").unlink()
        eventually(lambda: member.closed)
        assert member.reason == "LOG_UNAVAILABLE"
        snapshot = value.observations.snapshot(service[2]["job_id"], service[2]["nonce"])
        assert snapshot["log_status"] == "unavailable" and not snapshot["eof"]


@pytest.mark.parametrize("failure", ["corrupt_index", "missing_segment"])
def test_unobserved_fallback_never_hides_existing_broken_evidence(service, monkeypatch, failure):
    # Generation zero alone is insufficient: index validation and segment
    # reads still refuse even before the consumer has made its first snapshot.
    with active(service) as (disk, _, _):
        disk.append(0, ONE)
        directory = service[3].directory
        if failure == "corrupt_index":
            (directory / "index.json").write_bytes(b"invalid index")
        else:
            (directory / "seg-0000000000000000.log").unlink()
        with manager(service, monkeypatch) as value:
            member = subscribe(value, service)
            eventually(lambda: member.closed)
            assert member.reason == "LOG_UNAVAILABLE"
            assert b"LOG_UNAVAILABLE" in next(member)
            snapshot = value.observations.snapshot(service[2]["job_id"], service[2]["nonce"])
            assert snapshot["log_status"] == "unavailable" and not snapshot["eof"]


@pytest.mark.parametrize("evidence", ["command_only", "partial_log", "legacy"])
def test_archived_without_writer_requires_command_pins_and_no_log_pins(service, monkeypatch, evidence):
    _, store, row, _ = service
    with store.claim(row["job_id"], row["nonce"], ProcessIdentity(1, 123456, 1)):
        with store._home(job_id=row["job_id"]), store._write() as db:
            db.execute("UPDATE jobs SET state='failed' WHERE job_id=?", (row["job_id"],))
    history = JobHistory(store)
    history.initialize()
    assert history.archive(keep=0, age_ms=0)
    if evidence == "partial_log":
        # Even a declared segment without its index prevents no-log synthesis.
        with store._home(job_id=row["job_id"]), store._write() as db:
            db.execute("INSERT INTO job_archive_members VALUES(?,?,?,?)",
                       (row["job_id"], "log/seg-0000000000000000.log", 1, "0" * 64))
    elif evidence == "legacy":
        # Disposable fixture only: model the old archive format's absent pins,
        # using the same setup as the existing legacy log-service witness.
        with dal.connection_scope(store.database) as db, dal.transaction(db):
            db.execute("DROP TRIGGER archive_members_no_delete")
            db.execute("DELETE FROM job_archive_members")
    with manager(service, monkeypatch) as value:
        before = ledger_rows(store)
        member = subscribe(value, service)
        if evidence == "command_only":
            packet = member.receive(5)
            assert packet.frames[0].startswith(b"event: state\n")
            assert b'"state":"failed"' in packet.frames[0]
            assert b'"log_status":"unobserved"' in packet.frames[-1]
            assert b'"eof":false' in packet.frames[-1]
            assert packet.cursor == packet.next_cursor == "0"
            assert value.observations.snapshot(row["job_id"], row["nonce"])["generation"] == 0
            bad = subscribe(value, service, cursor="1")
            eventually(lambda: bad.closed)
            assert bad.reason == "INVALID_LOG_CURSOR" and not member.closed
        else:
            eventually(lambda: member.closed)
            assert member.reason == "LOG_UNAVAILABLE"
        assert ledger_rows(store) == before


@pytest.mark.parametrize("failure", ["missing_batch", "corrupt_index"])
def test_archived_declared_logs_never_become_unobserved(service, monkeypatch, failure):
    batch = written(service)
    if failure == "missing_batch":
        batch.unlink()
    else:
        rewrite_member(batch, service[2]["job_id"] + "/log/index.json", lambda body: b"corrupt")
    with manager(service, monkeypatch) as value:
        member = subscribe(value, service)
        eventually(lambda: member.closed)
        assert member.reason == "LOG_UNAVAILABLE"
        snapshot = value.observations.snapshot(service[2]["job_id"], service[2]["nonce"])
        assert snapshot["log_status"] == "unavailable" and not snapshot["eof"]


@pytest.mark.parametrize("cursor", ["0", "6"])
def test_runner_refused_job_keeps_prebirth_logs_and_accepts_resume(service, monkeypatch, cursor):
    # A trusted runner can create/finish its log before positively refusing
    # native birth. Terminal refused does not imply the job was never claimed.
    _, store, row, source = service
    body = b"first\nsecond\n"
    with store.claim(row["job_id"], row["nonce"], ProcessIdentity(1, 123456, 1)) as claim:
        disk = source.create_writer(claim)
        disk.append(0, body)
        disk.finish(len(body), complete=True)
        refused = store.refuse_before_create(claim, 2, reason="STOP_REQUESTED_BEFORE_BIRTH")
        assert refused["state"] == "refused" and refused["runner_pid"] == 1
        with manager(service, monkeypatch) as value:
            before = ledger_rows(store)
            member = subscribe(value, service, cursor=cursor)
            packet = member.receive(5)
            assert packet.cursor == cursor and packet.next_cursor == str(len(body))
            frames = b"".join(packet.frames)
            assert packet.frames[0].startswith(b"event: log\n")
            assert b'"text":"second\\n"' in frames and b'"state":"refused"' in frames
            assert (b'"text":"first\\n"' in frames) == (cursor == "0")
            assert b'"log_status":"complete"' in frames and not member.closed
            assert ledger_rows(store) == before


def test_historical_replay_of_lost_observed_index_closes_without_a_new_observation(service, monkeypatch):
    # Hold the first cadence wait so replay joins while its zero cursor is
    # still next in round-robin. Block any later producer consume separately:
    # producer-side loss handling must not mask a broken replay-only guard.
    initial_wait, replay_ready = threading.Event(), threading.Event()
    release_producer = threading.Event()
    producer_calls = []
    with active(service) as (disk, _, _), manager(service, monkeypatch) as value:
        disk.append(0, ONE)
        original_wait = value.condition.wait
        original_consume = value.observations.consume_page
        def pause_first_wait(timeout=None):
            if threading.current_thread().name.startswith("cbz-log-reader-") and not initial_wait.is_set():
                initial_wait.set()
                deadline = time.monotonic() + 10
                while not replay_ready.is_set() and time.monotonic() < deadline:
                    original_wait(.1)
                assert replay_ready.is_set(), "replay admission barrier timed out"
                return
            return original_wait(timeout)
        def block_later_producer(*args, **kwargs):
            producer_calls.append(True)
            if len(producer_calls) > 1:
                assert release_producer.wait(10), "producer cleanup barrier timed out"
            return original_consume(*args, **kwargs)
        monkeypatch.setattr(value.condition, "wait", pause_first_wait)
        monkeypatch.setattr(value.observations, "consume_page", block_later_producer)
        try:
            first = subscribe(value, service)
            assert initial_wait.wait(5)
            assert first.receive(5).next_cursor == str(len(ONE))
            before = value.observations.snapshot(service[2]["job_id"], service[2]["nonce"])
            assert before["generation"] > 0 and before["cursor"] != "0"
            (service[3].directory / "index.json").unlink()
            replay = subscribe(value, service, cursor="0")
            replay_ready.set()
            with value.condition:
                value.condition.notify_all()
            with pytest.raises(streams.StreamClosed, match="LOG_UNAVAILABLE"):
                replay.receive(5)
            assert first.closed and first.reason == "LOG_UNAVAILABLE"
            assert not replay.queue and len(producer_calls) == 1
            assert value.observations.snapshot(service[2]["job_id"], service[2]["nonce"]) == before
        finally:
            replay_ready.set()
            release_producer.set()
            with value.condition:
                value.condition.notify_all()
