"""Service-owned shared readers and bounded subscribers; no HTTP wiring yet.

The request side only reserves/receives immutable packets. A single worker per
job owns catch-up writes, fair replay reads and encoding, at a bounded cadence.
"""
from __future__ import annotations

from collections import deque
import logging
import math
import threading
import time

from . import logs
from .log_service import JobLogs
from .observations import JobObservations
from .stream_frames import EncodedPage, HEARTBEAT, StreamRefused, encode_page, error_frame

MAX_STREAMS = 8
MAX_QUEUE_PAGES = 4
ACTIVE_SECONDS = .25
IDLE_SECONDS = 1.0
HEARTBEAT_SECONDS = 15.0
log = logging.getLogger("comic_automation.web")


def _deadline(timeout):
    if timeout is not None and (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0):
        raise StreamRefused("INVALID_STREAM_WAIT")
    return None if timeout is None else time.monotonic() + timeout


def _unobserved(row):
    # Admission and synthesis must agree: these jobs never had a byte cursor
    # beyond zero, including a pre-claim refusal or expired admission.
    return row["state"] == "admitted" or row["state"] in {"refused", "admission_expired"} and row["runner_pid"] is None


class StreamClosed(RuntimeError):
    """An owned subscription closed; its fixed reason is safe for the wire."""


class Subscription:
    """Closeable byte iterator, including when closed before its first next()."""
    def __init__(self, manager, reader, token, cursor, session_valid):
        self.manager, self.reader, self.token = manager, reader, token
        self.cursor, self.session_valid = cursor, session_valid
        self.queue, self.pending = deque(), deque()
        self.signature = None
        self.closed, self.reason, self.error_sent = False, "", False

    def close(self):
        """Release the subscriber; a settling worker retains one capacity slot."""
        self.manager._detach(self, "CLIENT_CLOSED")

    def receive(self, timeout=HEARTBEAT_SECONDS) -> EncodedPage | None:
        """Wait without source/DB ownership, revalidating expiry while idle."""
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= HEARTBEAT_SECONDS:
            raise StreamRefused("INVALID_STREAM_WAIT")
        deadline = time.monotonic() + timeout
        while True:
            self.manager._validate_session(self)
            with self.manager.condition:
                if self.closed:
                    raise StreamClosed(self.reason)
                if self.queue:
                    packet = self.queue.popleft()
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.manager.condition.wait(min(ACTIVE_SECONDS, remaining))
        self.manager._validate_session(self)
        with self.manager.condition:
            if self.closed:
                raise StreamClosed(self.reason)
        return packet

    def __iter__(self):
        """Keep explicit close available even before iteration begins."""
        return self

    def __next__(self):
        """Deliver one encoded frame after auth, or an ID-free idle heartbeat."""
        try:
            self.manager._validate_session(self)
            with self.manager.condition:
                if self.closed:
                    raise StreamClosed(self.reason)
                if self.pending:
                    return self.pending.popleft()
            packet = self.receive()
            if packet is None:
                return HEARTBEAT
            with self.manager.condition:
                if self.closed:
                    raise StreamClosed(self.reason)
                self.pending.extend(packet.frames)
            return next(self)
        except StreamClosed:
            self.close()
            if not self.error_sent and self.reason in {
                    "LOG_UNAVAILABLE", "SLOW_SUBSCRIBER", "INVALID_LOG_CURSOR", "READER_START_FAILED"}:
                self.error_sent = True
                return error_frame(self.reason)
            raise StopIteration
        except BaseException:
            self.close()
            raise


class _Reader:
    def __init__(self, manager, key, snapshot):
        self.key, self.latest = key, snapshot
        self.members, self.pending = {}, deque()
        self.stop = False
        self.thread = threading.Thread(target=manager._run, args=(self,),
                                       name="cbz-log-reader-" + key[0], daemon=False)


class SharedLogReaders:
    """Eight service-local slots and one bounded, fair source reader per job.

    initialize is a service-start operation. subscribe requires a trusted,
    bounded in-memory session predicate; the HTTP layer must perform M2 auth
    before calling it and own the returned iterator's close on every path.
    """
    def __init__(self, store, *, queue_pages=MAX_QUEUE_PAGES):
        if type(queue_pages) is not int or not 1 <= queue_pages <= MAX_QUEUE_PAGES:
            raise StreamRefused("INVALID_STREAM_QUEUE")
        self.store, self.observations = store, JobObservations(store)
        self.queue_pages = queue_pages
        self.condition = threading.Condition()
        self.readers, self.members, self.reservations = {}, {}, set()
        self.ready, self.closed, self.polls = False, False, 0

    def initialize(self):
        """Explicit additive initialization under the owning service lifetime."""
        with self.condition:
            if self.closed:
                raise StreamRefused("STREAM_MANAGER_CLOSED")
            if self.ready:
                return
        self.observations.initialize()
        with self.condition:
            if self.closed:
                raise StreamRefused("STREAM_MANAGER_CLOSED")
            self.ready = True

    @staticmethod
    def _authorized(predicate):
        try:
            return predicate() is True
        except Exception:
            return False

    def _validate_session(self, member):
        if not member.closed and not self._authorized(member.session_valid):
            self._detach(member, "SESSION_INVALID")

    def _slots(self):
        # Caller holds condition. Each reader without subscribers still owns
        # one slot until its I/O unwinds, including failed/unknown thread starts.
        return len(self.members) + len(self.reservations) + sum(
            not reader.members for reader in self.readers.values())

    def subscribe(self, job_id, nonce, *, cursor="0", session_valid):
        """Reserve nonblocking before source work; release even failed opens."""
        if not callable(session_valid) or not self._authorized(session_valid):
            raise StreamRefused("SESSION_INVALID")
        JobObservations._validate(job_id, nonce)
        logs.decode_cursor(cursor)
        token = object()
        with self.condition:
            if self.closed or not self.ready:
                raise StreamRefused("STREAM_MANAGER_CLOSED" if self.closed else "STREAM_MANAGER_NOT_READY")
            if self._slots() >= MAX_STREAMS:
                raise StreamRefused("STREAM_LIMIT")
            self.reservations.add(token)
        try:
            row = self.store.get(job_id)
            if row["nonce"] != nonce:
                raise StreamRefused("OBSERVATION_NONCE_MISMATCH")
            snapshot = self.observations.snapshot(job_id, nonce)
            if _unobserved(row) and cursor != "0":
                raise StreamRefused("INVALID_LOG_CURSOR")
            if not self._authorized(session_valid):
                raise StreamRefused("SESSION_INVALID")
            with self.condition:
                if self.closed:
                    raise StreamRefused("STREAM_MANAGER_CLOSED")
                key = (job_id, nonce)
                reader = self.readers.get(key)
                if reader is not None and reader.stop:
                    raise StreamRefused("READER_CLOSING")
                new = reader is None
                if new:
                    reader = _Reader(self, key, snapshot)
                    self.readers[key] = reader
                member = Subscription(self, reader, token, cursor, session_valid)
                self.members[token] = reader.members[token] = member
                self.reservations.remove(token)
            if new:
                try:
                    reader.thread.start()
                except BaseException as error:
                    with self.condition:
                        self._stop_reader(reader, "READER_START_FAILED")
                        if isinstance(error, Exception) and reader.thread.ident is None and not reader.thread.is_alive():
                            if self.readers.get(key) is reader:
                                self.readers.pop(key)
                        self.condition.notify_all()
                    if isinstance(error, Exception):
                        raise StreamRefused("READER_START_FAILED") from error
                    raise
            return member
        finally:
            with self.condition:
                self.reservations.discard(token)
                self.condition.notify_all()

    def _detach(self, member, reason):
        with self.condition:
            if member.closed:
                return
            member.closed, member.reason = True, reason
            member.queue.clear()
            member.pending.clear()
            self.members.pop(member.token, None)
            member.reader.members.pop(member.token, None)
            if not member.reader.members:
                member.reader.stop = True
            self.condition.notify_all()

    def _stop_reader(self, reader, reason):
        # Caller holds condition; it is reentrant, and detach never performs I/O.
        for member in list(reader.members.values()):
            self._detach(member, reason)
        reader.stop = True

    def _run(self, reader):
        due = 0.0
        try:
            while True:
                with self.condition:
                    members = list(reader.members.values())
                for member in members:
                    self._validate_session(member)
                with self.condition:
                    if reader.stop or self.closed or not reader.members:
                        return
                    remaining = due - time.monotonic()
                    if remaining > 0:
                        self.condition.wait(min(ACTIVE_SECONDS, remaining))
                        continue
                    wanted = list(dict.fromkeys([reader.latest["cursor"]] +
                                               [member.cursor for member in reader.members.values()]))
                    # At most nine distinct cursors. Round-robin gives replay a
                    # turn without rewinding the durable progress consumer.
                    reader.pending = deque(cursor for cursor in reader.pending if cursor in wanted)
                    reader.pending.extend(cursor for cursor in wanted if cursor not in reader.pending)
                    cursor = reader.pending.popleft()
                    reader.pending.append(cursor)
                    self.polls += 1
                job_id, nonce = reader.key
                row = self.store.get(job_id)
                if row["nonce"] != nonce:
                    raise StreamRefused("OBSERVATION_NONCE_MISMATCH")
                if _unobserved(row):
                    page = dict(cursor="0", next_cursor="0", records=[], proven_offset="0", eof=False,
                                log_status="unobserved")
                elif cursor == reader.latest["cursor"]:
                    batch = self.observations.consume_page(job_id, nonce, allow_unobserved=True)
                    page, reader.latest = batch.page, batch.observation
                else:
                    # Historical replay is read-only. Its page may be behind
                    # current progress; never commit a rewound observation.
                    try:
                        page = JobLogs(self.store, job_id).read_page(
                            cursor=cursor, allow_unobserved=reader.latest["generation"] == 0)
                    except logs.LogRefused as error:
                        if str(error) == "INVALID_LOG_PAGE":
                            with self.condition:
                                for member in list(reader.members.values()):
                                    if member.cursor == cursor:
                                        self._detach(member, "INVALID_LOG_CURSOR")
                            due = time.monotonic() + ACTIVE_SECONDS
                            continue
                        raise
                packet = encode_page(page, reader.latest, row)
                with self.condition:
                    for member in list(reader.members.values()):
                        if member.cursor != packet.cursor or member.signature == packet.signature:
                            continue
                        if len(member.queue) >= self.queue_pages:
                            self._detach(member, "SLOW_SUBSCRIBER")
                            continue
                        member.queue.append(packet)
                        member.cursor, member.signature = packet.next_cursor, packet.signature
                    self.condition.notify_all()
                active = row["state"] in {"claimed", "running"} and bool(page["records"])
                due = time.monotonic() + (ACTIVE_SECONDS if active else IDLE_SECONDS)
        except Exception:
            log.exception("Shared log reader stopped for %s", reader.key[0])
            with self.condition:
                self._stop_reader(reader, "LOG_UNAVAILABLE")
        finally:
            # All source/DB scopes have ended before ownership can be released.
            with self.condition:
                # Cancellation also unwinds scopes, but bypasses Exception.
                # Do not leave slots/iterators alive after their worker exits.
                self._stop_reader(reader, "LOG_UNAVAILABLE")
                if self.readers.get(reader.key) is reader:
                    self.readers.pop(reader.key)
                self.condition.notify_all()

    def stats(self):
        """Bounded service diagnostics; polls count job attempts, not subscribers."""
        with self.condition:
            return {"slots": self._slots(), "readers": len(self.readers),
                    "reservations": len(self.reservations), "polls": self.polls}

    def close(self, timeout=2.0):
        """Stop and settle within the deadline; False requires retaining ownership."""
        deadline = _deadline(timeout)
        with self.condition:
            self.closed = True
            for member in list(self.members.values()):
                self._detach(member, "SERVER_SHUTDOWN")
            self.condition.notify_all()
        return self._wait_until(deadline)

    def wait_closed(self, timeout=None):
        """Wait for source readers and in-flight open reservations to settle."""
        return self._wait_until(_deadline(timeout))

    def _wait_until(self, deadline):
        with self.condition:
            while self.readers or self.reservations:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self.condition.wait(remaining)
            return True
