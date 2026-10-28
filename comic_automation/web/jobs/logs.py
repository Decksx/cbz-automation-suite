"""Bounded raw-byte logs, durable coverage, nonblocking ingestion and paging.

Only the writer performs disk I/O. A bounded queue carries data plus logical
offsets; lost ranges are inferred between offsets, so alternating overflow
cannot allocate an unbounded pending-gap list. A compact atomic index replaces
the design's append-only gaps.jsonl: it is the durable coverage watermark.
Anything beyond it after a crash is an unknown tail, never successful EOF.
"""
from __future__ import annotations

from collections import deque
import codecs
import copy
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import threading
import time

from .log_io import shared_read, cancel_thread_io
from .store import JobRefused, validate_job_id

CHUNK_BYTES = 64 * 1024
QUEUE_CHUNKS = 256
SEGMENT_BYTES = 8 * 1024 * 1024
SERVICE_SEGMENTS = 4
TASK_SEGMENTS = 32
MAX_INDEX_BYTES = 64 * 1024
MAX_PAGE_BYTES = 256 * 1024
MAX_OFFSET = 2**63 - 1
_CURSOR = re.compile(r"(?:0|[1-9][0-9]{0,18})\Z")


class LogRefused(JobRefused):
    """Log evidence or an API bound could not be trusted."""


def decode_cursor(value: str) -> int:
    """Accept canonical decimal source-byte offsets, preserving JSON precision."""
    if not isinstance(value, str) or not _CURSOR.fullmatch(value) or int(value) > MAX_OFFSET:
        raise LogRefused("INVALID_LOG_CURSOR")
    return int(value)


def encode_cursor(value: int) -> str:
    """Serialize a bounded source offset without floating-point conversion."""
    if type(value) is not int or not 0 <= value <= MAX_OFFSET:
        raise LogRefused("INVALID_LOG_CURSOR")
    return str(value)


def _segment(start: int) -> str:
    return f"seg-{start:016x}.log"


def _number(value) -> bool:
    return type(value) is int and 0 <= value <= MAX_OFFSET


def validate_index(value: dict, job_id: str, nonce: str) -> dict:
    """Require bounded, complete, nonoverlapping half-open coverage to watermark."""
    if (not isinstance(value, dict) or type(value.get("version")) is not int or value.get("version") != 1 or value.get("job_id") != job_id
            or value.get("nonce") != nonce or not _number(value.get("offset"))
            or type(value.get("complete")) is not bool):
        raise LogRefused("INVALID_LOG_INDEX")
    segments, gaps = value.get("segments"), value.get("gaps")
    if not isinstance(segments, list) or len(segments) > TASK_SEGMENTS or not isinstance(gaps, list) or len(gaps) > TASK_SEGMENTS + 1:
        raise LogRefused("EXCESSIVE_LOG_MAP")
    spans = []
    for entry in segments:
        if (not isinstance(entry, dict) or not _number(entry.get("start")) or not _number(entry.get("end"))
                or not 0 < entry["end"] - entry["start"] <= SEGMENT_BYTES):
            raise LogRefused("INVALID_LOG_SEGMENT")
        spans.append((entry["start"], entry["end"]))
    for entry in gaps:
        if (not isinstance(entry, dict) or not _number(entry.get("from")) or not _number(entry.get("to"))
                or entry["from"] >= entry["to"] or entry.get("reason") not in ("dropped", "retention", "drain_timeout")):
            raise LogRefused("INVALID_LOG_GAP")
        spans.append((entry["from"], entry["to"]))
    position = 0
    for start, end in sorted(spans):
        if start != position:
            raise LogRefused("OVERLAPPING_OR_MISSING_LOG_COVERAGE")
        position = end
    if position != value["offset"]:
        raise LogRefused("INVALID_LOG_WATERMARK")
    return value


class LogQueue:
    """A short-lock queue; payload <=256*64KiB and loss metadata is one scalar."""
    def __init__(self, capacity: int = QUEUE_CHUNKS):
        if type(capacity) is not int or not 1 <= capacity <= QUEUE_CHUNKS:
            raise ValueError("Invalid log queue capacity")
        self.capacity, self.items = capacity, deque()
        self.condition = threading.Condition()
        self.offset, self.closed, self.complete = 0, False, False

    def offer(self, data: bytes) -> bool:
        """Advance offsets even when full; never wait for the consumer or disk."""
        if not isinstance(data, bytes) or not 0 < len(data) <= CHUNK_BYTES:
            raise ValueError("Invalid log chunk")
        with self.condition:
            if self.closed or self.offset + len(data) > MAX_OFFSET:
                raise LogRefused("LOG_INPUT_CLOSED_OR_EXHAUSTED")
            start = self.offset
            self.offset += len(data)
            accepted = len(self.items) < self.capacity
            if accepted:
                self.items.append((start, data))
            self.condition.notify()
            return accepted

    def finish(self, *, complete: bool) -> None:
        """Signal read completion; only a proven pipe EOF may set complete=True."""
        if type(complete) is not bool:
            raise ValueError("Invalid EOF proof")
        with self.condition:
            self.closed = True
            self.complete = bool(complete)
            self.condition.notify_all()

    def take(self):
        """Wait only for input, releasing the queue lock before any storage I/O."""
        with self.condition:
            self.condition.wait_for(lambda: self.items or self.closed)
            return self.items.popleft() if self.items else None


class LogDisk:
    """Single-writer segment store; callers retain the live runner claim.

    validate(path) is invoked before every access and supplies the private-home
    boundary in production. The standalone form is for disposable fixtures.
    An existing index is never reopened for writing: a dead runner's log is
    evidence for reconciliation, not an invitation to resume or overwrite it.
    """
    def __init__(self, directory: Path, job_id: str, nonce: str, *, service: bool = False,
                 validate=lambda path: None, segment_bytes: int = SEGMENT_BYTES, keep: int | None = None):
        validate_job_id(job_id)
        if not re.fullmatch(r"[0-9a-f]{32}", nonce):
            raise LogRefused("INVALID_LOG_NONCE")
        maximum = SERVICE_SEGMENTS if service else TASK_SEGMENTS
        self.keep = maximum if keep is None else keep
        if (type(segment_bytes) is not int or not 1 <= segment_bytes <= SEGMENT_BYTES
                or type(self.keep) is not int or not 1 <= self.keep <= maximum):
            raise ValueError("Invalid log retention")
        self.directory, self.validate, self.segment_bytes = Path(directory), validate, segment_bytes
        self.index_path = self.directory / "index.json"
        self.pending_delete = []
        self.snapshot = dict(version=1, job_id=job_id, nonce=nonce, offset=0,
                             complete=False, segments=[], gaps=[])
        self.validate(self.directory)
        self.directory.mkdir(exist_ok=True)
        self.validate(self.index_path)
        if any(self.directory.iterdir()):
            raise LogRefused("EXISTING_LOG_EVIDENCE")
        self._publish(self.snapshot)

    def _publish(self, candidate):
        validate_index(candidate, self.snapshot["job_id"], self.snapshot["nonce"])
        payload = json.dumps(candidate, separators=(",", ":"), allow_nan=False).encode()
        if len(payload) > MAX_INDEX_BYTES:
            raise LogRefused("LOG_INDEX_TOO_LARGE")
        temporary = self.directory / "index.tmp"
        self.validate(temporary)
        self.validate(self.index_path)
        with open(temporary, "wb") as stream:
            if stream.write(payload) != len(payload):
                raise OSError("Short log index write")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.index_path)
        self.snapshot = candidate

    @staticmethod
    def _gap(candidate, start, end, reason):
        if start == end:
            return
        gaps = candidate["gaps"]
        if gaps and gaps[-1]["to"] == start and gaps[-1]["reason"] == reason:
            gaps[-1]["to"] = end
        else:
            gaps.append(dict(**{"from": start, "to": end}, reason=reason))

    def _prune(self):
        while self.pending_delete:
            path = self.directory / _segment(self.pending_delete[0])
            self.validate(path)
            path.unlink(missing_ok=True)
            self.pending_delete.pop(0)

    def append(self, start: int, data: bytes) -> None:
        """Persist a chunk or its short-write gap; retry safely after I/O errors.

        The committed watermark fences a retry. Any uncommitted file suffix is
        truncated before retrying that portion, so a failed index publication
        cannot alias offsets or expose bytes whose fsync wasn't proved.
        """
        if not _number(start) or not isinstance(data, bytes) or not 0 < len(data) <= CHUNK_BYTES or start + len(data) > MAX_OFFSET:
            raise LogRefused("INVALID_LOG_CHUNK")
        if self.snapshot["complete"]:
            raise LogRefused("LOG_ALREADY_COMPLETE")
        self._prune()
        offset = self.snapshot["offset"]
        if start < offset:
            data, start = data[offset - start:], offset
        if not data:
            return
        candidate = copy.deepcopy(self.snapshot)
        if start > candidate["offset"]:
            self._gap(candidate, candidate["offset"], start, "dropped")
        while data:
            segments = candidate["segments"]
            current = segments[-1] if segments else None
            if current is None or current["end"] != start or current["end"] - current["start"] == self.segment_bytes:
                current = dict(start=start, end=start)
                segments.append(current)
            path = self.directory / _segment(current["start"])
            self.validate(path)
            count = min(len(data), self.segment_bytes - (current["end"] - current["start"]))
            mode = "r+b" if path.exists() else "w+b"
            with open(path, mode, buffering=0) as stream:
                stream.truncate(current["end"] - current["start"])
                stream.seek(current["end"] - current["start"])
                written = stream.write(data[:count])
                if type(written) is not int or not 0 <= written <= count:
                    raise OSError("Invalid log write count")
                os.fsync(stream.fileno())
            current["end"] += written
            if written == 0:
                segments.pop() if current["start"] == current["end"] else None
            if written < count:
                self._gap(candidate, start + written, start + count, "dropped")
            start += count
            data = data[count:]
            candidate["offset"] = start
            evicted = [current["start"]] if current["start"] == current["end"] else []
            while len(segments) > self.keep:
                evicted.append(segments.pop(0)["start"])
            # Discard the entire old prefix including its gaps. This bounds
            # both segment count and metadata under alternating data/loss.
            if evicted and segments and evicted[0] < segments[0]["start"]:
                floor = segments[0]["start"]
                candidate["gaps"] = [g for g in candidate["gaps"] if g["to"] > floor]
                candidate["gaps"].insert(0, {"from": 0, "to": floor, "reason": "retention"})
            self._publish(candidate)
            self.pending_delete.extend(evicted)
            self._prune()  # coverage/gap is durable before any closed file unlink
            candidate = copy.deepcopy(self.snapshot)

    def finish(self, total: int, *, complete: bool) -> None:
        """Commit known dropped suffix and EOF only after all queued writes settle."""
        if not _number(total) or total < self.snapshot["offset"] or type(complete) is not bool:
            raise LogRefused("INVALID_LOG_FINAL_OFFSET")
        self._prune()
        candidate = copy.deepcopy(self.snapshot)
        self._gap(candidate, candidate["offset"], total, "dropped")
        candidate.update(offset=total, complete=complete)
        self._publish(candidate)


@dataclass(frozen=True)
class PumpResult:
    """Join proof for M3c; incomplete cleanup must retain admission as uncertain."""
    settled: bool
    complete: bool
    read_offset: int
    durable_offset: int
    error: str | None


class LogPump:
    """Two daemon threads; the pipe reader never waits for storage recovery.

    This class owns neither the pipe handle nor the runner claim. M3c owns
    native cancellation/closure and maps unsettled cleanup to uncertainty.
    No worker callback may finalize a job or clear that uncertainty.
    """
    def __init__(self, source, disk: LogDisk, *, capacity: int = QUEUE_CHUNKS, retry_seconds: float = 0.1):
        if not isinstance(retry_seconds, (int, float)) or not math.isfinite(retry_seconds) or not 0.001 <= retry_seconds <= 5:
            raise ValueError("Invalid writer retry delay")
        self.source, self.disk = source, disk
        self.queue = LogQueue(capacity)
        self.retry_seconds, self.error = retry_seconds, None
        self.abort = threading.Event()
        self.reader = threading.Thread(target=self._read, name="cbz-log-reader", daemon=True)
        self.writer = threading.Thread(target=self._write, name="cbz-log-writer", daemon=True)

    def start(self):
        """Start storage consumer and pipe reader; return the pump for ownership."""
        self.writer.start()
        try:
            self.reader.start()
        except BaseException:
            self.abort.set()
            self.queue.finish(complete=False)
            self.writer.join(1)
            raise
        return self

    def _read(self):
        complete = False
        try:
            while not self.abort.is_set():
                data = self.source.read(CHUNK_BYTES)
                if not data:
                    complete = True
                    break
                self.queue.offer(data)
        except Exception as exc:
            self.error = type(exc).__name__
        finally:
            self.queue.finish(complete=complete)

    def _retry(self, operation):
        while not self.abort.is_set():
            try:
                operation()
                return True
            except OSError as exc:
                self.error = type(exc).__name__
                self.abort.wait(self.retry_seconds)
        return False

    def _write(self):
        try:
            while not self.abort.is_set():
                item = self.queue.take()
                if item is None:
                    self._retry(lambda: self.disk.finish(self.queue.offset, complete=self.queue.complete))
                    return
                if not self._retry(lambda: self.disk.append(*item)):
                    return
        except Exception as exc:
            self.error = type(exc).__name__

    def join(self, timeout: float = 5.0) -> PumpResult:
        """Bound the combined wait; timeout requests cancellation without claiming it worked."""
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 30:
            raise ValueError("Invalid log join timeout")
        deadline = time.monotonic() + timeout
        for thread in (self.reader, self.writer):
            thread.join(max(0, deadline - time.monotonic()))
        settled = not self.reader.is_alive() and not self.writer.is_alive()
        if not settled:
            self.abort.set()
            self.queue.finish(complete=False)
            for thread in (self.reader, self.writer):
                if thread.is_alive() and thread.native_id:
                    try:
                        cancel_thread_io(thread.native_id)
                    except OSError:
                        pass  # cancellation is advisory; the result stays unsettled
        return PumpResult(settled, settled and self.disk.snapshot["complete"], self.queue.offset,
                          self.disk.snapshot["offset"], self.error)


def load_index(read, job_id: str, nonce: str) -> dict:
    """Read bounded index bytes through a caller-supplied hot or sealed reader."""
    try:
        data = read("index.json", 0, MAX_INDEX_BYTES + 1)
        if len(data) > MAX_INDEX_BYTES:
            raise LogRefused("LOG_INDEX_TOO_LARGE")
        return validate_index(json.loads(data), job_id, nonce)
    except (ValueError, TypeError, KeyError) as exc:
        raise LogRefused(f"INVALID_LOG_INDEX: {exc}") from None


def _text_piece(raw: bytes, boundary: bool) -> tuple[str, int]:
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    text = decoder.decode(raw, final=boundary)
    pending, _ = decoder.getstate()
    return text, len(raw) - len(pending)


def _contiguous(index, read, position, start, end):
    run_end = end
    for segment in sorted(index["segments"], key=lambda s: s["start"]):
        if segment["start"] == run_end:
            run_end = segment["end"]
    stop = min(run_end, position + CHUNK_BYTES)
    parts = []
    for segment in sorted(index["segments"], key=lambda s: s["start"]):
        lower, upper = max(position, segment["start"]), min(stop, segment["end"])
        if lower < upper:
            part = read(_segment(segment["start"]), lower - segment["start"], upper - lower)
            if len(part) != upper - lower:
                raise LogRefused("MAPPED_LOG_BYTES_MISSING")
            parts.append(part)
    return b"".join(parts), run_end


def _fragment(index, read, position):
    if position == 0:
        return False
    for segment in index["segments"]:
        if segment["start"] <= position - 1 < segment["end"]:
            return read(_segment(segment["start"]), position - 1 - segment["start"], 1) != b"\n"
    return True  # a gap cannot prove the beginning of a source line


def page(index: dict, read, *, cursor: str = "0", limit: int = 1000, live: bool = False) -> dict:
    """Page raw-byte cursors with bounded JSON output and UTF-8 line fragments.

    read(name, offset, count) returns a bounded slice from an opened segment.
    Index validation is the caller's boundary. Missing mapped bytes refuse;
    they are never silently reported as a gap or an EOF. M3d adds HTTP/SSE.
    """
    position = decode_cursor(cursor)
    if type(limit) is not int or not 1 <= limit <= 1000 or position > index["offset"]:
        raise LogRefused("INVALID_LOG_PAGE")
    records = []
    # Account using encoded JSON, not Python characters or source byte lengths.
    budget = MAX_PAGE_BYTES - 1024
    spans = sorted([(s["start"], s["end"], "segment", s) for s in index["segments"]] +
                   [(g["from"], g["to"], "gap", g) for g in index["gaps"]])
    for start, end, kind, item in spans:
        if end <= position:
            continue
        if len(records) >= limit:
            break
        if kind == "gap":
            record = dict(type="gap", **{"from": encode_cursor(position), "to": encode_cursor(end)}, reason=item["reason"])
            size = len(json.dumps(record, ensure_ascii=True).encode()) + 2
            if size > budget:
                break
            records.append(record)
            budget -= size
            position = end
            continue
        while position < end and len(records) < limit:
            raw, run_end = _contiguous(index, read, position, start, end)
            newline = raw.find(b"\n")
            if newline >= 0:
                raw = raw[:newline + 1]
            boundary = newline >= 0 or position + len(raw) == run_end and (index["complete"] or run_end < index["offset"])
            if not boundary and len(raw) < CHUNK_BYTES:
                break  # incomplete live line, leave its cursor for the next poll
            text, consumed = _text_piece(raw, boundary)
            if consumed == 0:
                break
            continued = newline < 0
            record = dict(type="line", **{"from": encode_cursor(position), "to": encode_cursor(position + consumed)},
                          text=text, continued=continued, fragment=_fragment(index, read, position))
            size = len(json.dumps(record, ensure_ascii=True).encode()) + 2
            if size > budget:
                # A fully escaped 64KiB fragment can exceed 256KiB. A smaller
                # fragment is still a valid continuation and preserves offsets.
                if records:
                    break
                raw = raw[:max(1, len(raw) // 4)]
                text, consumed = _text_piece(raw, False)
                record.update(to=encode_cursor(position + consumed), text=text, continued=True)
                size = len(json.dumps(record, ensure_ascii=True).encode()) + 2
            if size > budget or consumed == 0:
                break
            records.append(record)
            budget -= size
            position += consumed
        if position < end:
            break
    complete = index["complete"]
    return dict(cursor=cursor, next_cursor=encode_cursor(position), records=records,
                proven_offset=encode_cursor(index["offset"]), eof=complete and position == index["offset"],
                log_status="complete" if complete else "live" if live else "unknown_tail")


def parse_progress(record: dict) -> dict | None:
    """Parse advisory complete bounded progress; never return a lifecycle state."""
    if record.get("type") != "line" or record.get("continued") or record.get("fragment") or len(record.get("text", "")) > CHUNK_BYTES:
        return None
    match = re.fullmatch(r"CBZ_PROGRESS ([0-9]{1,15})/([0-9]{1,15}) ([0-9]{1,3}(?:\.[0-9]{1,6})?)%([^\n]{0,1024})\n?", record["text"])
    if match is None:
        return None
    done, total, percent = int(match[1]), int(match[2]), float(match[3])
    if not 0 <= done <= total <= 10**12 or total == 0 or not 0 <= percent <= 100:
        return None
    return dict(done=done, total=total, percent=percent, label=match[4].strip(), cursor=record["to"])
