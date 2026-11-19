"""Real private ledgers and bounded worker handshakes; no HTTP or native jobs."""
from contextlib import contextmanager
import threading
import time

import pytest

from comic_automation.web.jobs import stream_manager as streams
from comic_automation.web.jobs.log_service import JobLogs
from test_web_job_observations import active, ledger_rows, ONE
from test_web_log_service import service, written


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
                assert calls.count(dict(cursor="0", limit=1000)) == 1
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
            assert value.stats()["slots"] == 0 and value.stats()["readers"] == 1
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
            assert value.stats()["slots"] == 0 and value.stats()["readers"] == 1
            assert value.close(.01) is False
        finally:
            release.set()
        assert value.wait_closed(5)


def test_active_cadence_has_one_quarter_second_floor(service, monkeypatch):
    with active(service) as (disk, _, _):
        disk.append(0, b"a\n" * 2100)
        reads, original = [], JobLogs.read_page
        def counted(source, **kw):
            reads.append(time.monotonic())
            return original(source, **kw)
        monkeypatch.setattr(JobLogs, "read_page", counted)
        with manager(service) as value:
            member = subscribe(value, service)
            packets = [member.receive(5) for _ in range(3)]
            assert [packet.next_cursor for packet in packets] == ["2000", "4000", "4200"]
            assert all(b - a >= .24 for a, b in zip(reads, reads[1:]))


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
