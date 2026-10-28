"""Bounded temporary log fixtures, real sharing and deterministic storage faults."""
import builtins
import copy
import io
import json
import os
import hashlib
from pathlib import Path
import threading

import pytest

from comic_automation.web.jobs import logs as lg
from comic_automation.web.jobs.log_io import shared_read, cancel_thread_io

JOB = "00000000000000000000000001"
NONCE = "0" * 32


@pytest.fixture
def disk(tmp_path):
    return lg.LogDisk(tmp_path / "log", JOB, NONCE)


def reader(disk):
    def read(name, offset, count):
        with shared_read(disk.directory / name) as stream:
            stream.seek(offset)
            return stream.read(count)
    return read


def collect(disk, cursor="0", limit=1000):
    return lg.page(lg.load_index(reader(disk), JOB, NONCE), reader(disk), cursor=cursor, limit=limit)


@pytest.mark.parametrize("value", ["0", "42", str(2**63 - 1)])
def test_cursor_roundtrip_is_exact(value):
    assert lg.encode_cursor(lg.decode_cursor(value)) == value


@pytest.mark.parametrize("value", ["", "00", "01", "-1", "+1", "1.0", " 1", "1\n", "1e9", str(2**63), True, 1, None])
def test_cursor_refuses_noncanonical_or_unbounded_values(value):
    with pytest.raises(lg.LogRefused):
        lg.decode_cursor(value)


def test_queue_advances_raw_offsets_without_pending_gap_objects():
    queue = lg.LogQueue(1)
    assert queue.offer(b"a")
    for _ in range(10000):
        assert not queue.offer(b"b")
    assert queue.offset == 10001 and len(queue.items) == 1
    assert queue.take() == (0, b"a")
    assert queue.offer(b"c")
    assert queue.take() == (10001, b"c")
    queue.finish(complete=True)
    assert queue.take() is None
    assert queue.complete
    with pytest.raises(lg.LogRefused):
        queue.offer(b"late")


def test_contiguous_segments_and_utf8_do_not_change_source_cursors(tmp_path):
    disk = lg.LogDisk(tmp_path / "log", JOB, NONCE, segment_bytes=3)
    body = "hé😊\nsecond\n".encode()
    disk.append(0, body)
    disk.finish(len(body), complete=True)
    output = collect(disk, limit=1)
    assert output["records"][0]["text"] == "hé😊\n"
    assert output["next_cursor"] == str(len("hé😊\n".encode()))
    tail = collect(disk, output["next_cursor"])
    assert tail["records"][0]["text"] == "second\n" and tail["eof"]


def test_gap_cursors_before_inside_and_after_do_not_alias(disk):
    disk.append(0, b"a\n")
    disk.append(7, b"z\n")
    disk.finish(9, complete=True)
    assert [r["type"] for r in collect(disk)["records"]] == ["line", "gap", "line"]
    for cursor in ("2", "3", "6"):
        result = collect(disk, cursor)
        assert result["records"][0] == {"type": "gap", "from": cursor, "to": "7", "reason": "dropped"}
        assert result["records"][1]["from"] == "7" and result["eof"]
    assert collect(disk, "7")["records"][0]["text"] == "z\n"
    assert [p.name for p in disk.directory.glob("*.log")] == ["seg-0000000000000000.log", "seg-0000000000000007.log"]


def test_retention_gap_is_durable_before_closed_file_deletion(tmp_path, monkeypatch):
    disk = lg.LogDisk(tmp_path / "log", JOB, NONCE, segment_bytes=4, keep=2)
    disk.append(0, b"aaaa")
    disk.append(4, b"bbbb")
    unlink = Path.unlink
    seen = []
    def witness(path, *args, **kwargs):
        if path.suffix == ".log":
            index = json.loads(disk.index_path.read_bytes())
            seen.append(index["gaps"][0])
            assert index["gaps"][0] == {"from": 0, "to": 4, "reason": "retention"}
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", witness)
    disk.append(8, b"cccc")
    disk.finish(12, complete=True)
    assert seen and len(list(disk.directory.glob("*.log"))) == 2
    result = collect(disk)
    assert result["records"][0]["reason"] == "retention"
    assert "".join(r.get("text", "") for r in result["records"]) == "bbbbcccc"


def test_alternating_overflow_compacts_map_and_physical_files(tmp_path):
    disk = lg.LogDisk(tmp_path / "log", JOB, NONCE, keep=4, segment_bytes=8)
    for n in range(300):
        disk.append(n * 4, b"a\n")
    disk.finish(1200, complete=True)
    index = lg.load_index(reader(disk), JOB, NONCE)
    assert len(index["segments"]) == 4 and len(index["gaps"]) <= 5
    assert len(list(disk.directory.iterdir())) == 5  # four segments + compact index
    assert index["gaps"][0]["reason"] == "retention" and index["gaps"][-1]["to"] == 1200


def test_partial_write_preserves_prefix_and_accounts_remaining_bytes(disk, monkeypatch):
    class Short:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def write(self, data):
            return self.stream.write(data[:3])
    monkeypatch.setattr(lg, "open", lambda path, *a, **k: Short(builtins.open(path, *a, **k))
                        if Path(path).suffix == ".log" else builtins.open(path, *a, **k), raising=False)
    disk.append(0, b"abcdef")
    disk.append(6, b"zz\n")
    disk.finish(9, complete=True)
    result = collect(disk)
    assert [(s["start"], s["end"]) for s in disk.snapshot["segments"]] == [(0, 3), (6, 9)]
    assert result["records"][1] == {"type": "gap", "from": "3", "to": "6", "reason": "dropped"}
    assert (disk.directory / "seg-0000000000000000.log").read_bytes() == b"abc"


def test_zero_writes_do_not_leak_empty_segments(tmp_path, monkeypatch):
    disk = lg.LogDisk(tmp_path / "log", JOB, NONCE)
    class Zero:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def __exit__(self, *args): self.stream.close()
        def __getattr__(self, name): return getattr(self.stream, name)
        def write(self, data): return 0
    monkeypatch.setattr(lg, "open", lambda path, *a, **k: Zero(builtins.open(path, *a, **k))
                        if Path(path).suffix == ".log" else builtins.open(path, *a, **k), raising=False)
    for n in range(100):
        disk.append(n, b"x")
    disk.finish(100, complete=True)
    assert list(disk.directory.iterdir()) == [disk.index_path]
    assert disk.snapshot["gaps"] == [{"from": 0, "to": 100, "reason": "dropped"}]


def test_index_failure_retry_truncates_uncommitted_suffix(disk, monkeypatch):
    publish = disk._publish
    def fail(candidate):
        raise OSError("injected index storage error")
    monkeypatch.setattr(disk, "_publish", fail)
    with pytest.raises(OSError):
        disk.append(0, b"uncommitted\n")
    assert disk.snapshot["offset"] == 0
    assert collect(disk)["log_status"] == "unknown_tail" and not collect(disk)["eof"]
    monkeypatch.setattr(disk, "_publish", publish)
    disk.append(0, b"good\n")
    disk.finish(5, complete=True)
    assert collect(disk)["records"][0]["text"] == "good\n"
    assert (disk.directory / "seg-0000000000000000.log").read_bytes() == b"good\n"


def test_stalled_writer_does_not_stop_reader_and_publishes_exact_loss(disk, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    append = disk.append
    def stall(*args):
        entered.set()
        assert release.wait(5)
        return append(*args)
    monkeypatch.setattr(disk, "append", stall)
    data = b"x" * lg.CHUNK_BYTES * 100
    pump = lg.LogPump(io.BytesIO(data), disk, capacity=2).start()
    try:
        assert entered.wait(5)
        pump.reader.join(2)
        assert not pump.reader.is_alive() and pump.queue.offset == len(data)
        assert len(pump.queue.items) <= 2
    finally:
        release.set()
    result = pump.join(5)
    assert result.settled and result.complete and result.durable_offset == len(data)
    assert disk.snapshot["gaps"][-1]["to"] == len(data)
    assert sum(s["end"] - s["start"] for s in disk.snapshot["segments"]) <= lg.CHUNK_BYTES * 3


def test_join_timeout_is_not_a_successful_eof(disk, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    append = disk.append
    def stall(*args):
        entered.set()
        assert release.wait(5)
        return append(*args)
    monkeypatch.setattr(disk, "append", stall)
    pump = lg.LogPump(io.BytesIO(b"a\n"), disk).start()
    try:
        assert entered.wait(5)
        result = pump.join(0)
        assert not result.settled and not result.complete
        assert not collect(disk)["eof"]
    finally:
        release.set()
        pump.reader.join(2)
        pump.writer.join(2)
    assert not pump.writer.is_alive()


def test_writer_retries_storage_failure_without_losing_the_queued_chunk(disk, monkeypatch):
    attempted, recover = threading.Event(), threading.Event()
    append = disk.append
    def fault(*args):
        if not recover.is_set():
            attempted.set()
            raise OSError("disk unavailable")
        return append(*args)
    monkeypatch.setattr(disk, "append", fault)
    pump = lg.LogPump(io.BytesIO(b"recover me\n"), disk, retry_seconds=0.001).start()
    assert attempted.wait(5)
    recover.set()
    result = pump.join(5)
    assert result.settled and result.complete
    assert collect(disk)["records"][0]["text"] == "recover me\n"


def test_unreadable_pipe_retains_unknown_tail(disk):
    class Failed:
        def read(self, count): raise OSError("read lost")
    result = lg.LogPump(Failed(), disk).start().join(5)
    assert result.settled and not result.complete
    assert collect(disk)["log_status"] == "unknown_tail" and not collect(disk)["eof"]


def test_megabyte_line_has_bounded_parts_and_exact_reconnects(disk):
    body = b"a" * 1024 * 1024 + b"\n"
    for offset in range(0, len(body), lg.CHUNK_BYTES):
        disk.append(offset, body[offset:offset + lg.CHUNK_BYTES])
    disk.finish(len(body), complete=True)
    cursor, parts = "0", []
    while True:
        output = collect(disk, cursor, limit=2)
        assert len(json.dumps(output).encode()) <= lg.MAX_PAGE_BYTES
        assert output["next_cursor"] != cursor
        parts.extend(output["records"])
        cursor = output["next_cursor"]
        if output["eof"]:
            break
    assert "".join(part["text"] for part in parts).encode() == body
    assert all(len(part["text"].encode()) <= lg.CHUNK_BYTES for part in parts)
    assert all(part["continued"] for part in parts[:-1])
    assert all(part["fragment"] for part in parts[1:])


def test_json_budget_counts_replacement_characters_and_escapes(disk):
    body = (b"\x00\xff" * (lg.CHUNK_BYTES // 2)) + b"\n"
    disk.append(0, body[:lg.CHUNK_BYTES])
    disk.append(lg.CHUNK_BYTES, body[lg.CHUNK_BYTES:])
    disk.finish(len(body), complete=True)
    first = collect(disk)
    assert len(json.dumps(first).encode()) <= lg.MAX_PAGE_BYTES
    assert "\ufffd" in first["records"][0]["text"]
    assert first["records"][0]["continued"]


def test_incomplete_live_line_waits_without_fabricating_eof(disk):
    disk.append(0, b"partial")
    result = lg.page(disk.snapshot, reader(disk), live=True)
    assert result["records"] == [] and result["next_cursor"] == "0" and result["log_status"] == "live"
    disk.append(7, b"\n")
    assert collect(disk)["records"][0]["text"] == "partial\n"


@pytest.mark.parametrize("fault", ["overlap", "uncovered", "wrong_nonce", "too_many", "bool_offset"])
def test_index_corruption_is_refused(disk, fault):
    disk.append(0, b"a\n")
    value = copy.deepcopy(disk.snapshot)
    if fault == "overlap": value["gaps"] = [{"from": 0, "to": 1, "reason": "dropped"}]
    elif fault == "uncovered": value["offset"] = 3
    elif fault == "wrong_nonce": value["nonce"] = "f" * 32
    elif fault == "too_many": value["segments"] *= 33
    else: value["offset"] = True
    with pytest.raises(lg.LogRefused):
        lg.validate_index(value, JOB, NONCE)


def test_deleted_mapped_segment_is_not_reported_as_eof(disk):
    disk.append(0, b"a\n")
    (disk.directory / "seg-0000000000000000.log").write_bytes(b"a")
    with pytest.raises(lg.LogRefused, match="MISSING"):
        collect(disk)


def test_existing_log_evidence_is_never_reopened_for_writing(disk):
    disk.append(0, b"evidence\n")
    before = disk.index_path.read_bytes()
    with pytest.raises(lg.LogRefused, match="EXISTING"):
        lg.LogDisk(disk.directory, JOB, NONCE)
    assert disk.index_path.read_bytes() == before


@pytest.mark.parametrize("limit", [0, 1001, True])
def test_page_limits_are_checked_at_the_direct_service_boundary(disk, limit):
    with pytest.raises(lg.LogRefused, match="INVALID_LOG_PAGE"):
        collect(disk, limit=limit)


def test_future_cursor_is_refused_instead_of_becoming_eof(disk):
    with pytest.raises(lg.LogRefused, match="INVALID_LOG_PAGE"):
        collect(disk, cursor="1")


def test_partial_index_write_cannot_publish_a_watermark(disk, monkeypatch):
    before = disk.index_path.read_bytes()
    class ShortIndex:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def __exit__(self, *args): self.stream.close()
        def __getattr__(self, name): return getattr(self.stream, name)
        def write(self, data): return self.stream.write(data[:10])
    monkeypatch.setattr(lg, "open", lambda path, *a, **k: ShortIndex(builtins.open(path, *a, **k))
                        if Path(path).name == "index.tmp" else builtins.open(path, *a, **k), raising=False)
    with pytest.raises(OSError, match="Short log index"):
        disk.append(0, b"not proven\n")
    assert disk.index_path.read_bytes() == before and disk.snapshot["offset"] == 0


def test_segment_fsync_failure_cannot_advance_durable_coverage(disk, monkeypatch):
    before = disk.index_path.read_bytes()
    def fail(fd):
        raise OSError("injected segment fsync failure")
    monkeypatch.setattr(lg.os, "fsync", fail)
    with pytest.raises(OSError, match="fsync"):
        disk.append(0, b"not yet durable\n")
    assert disk.index_path.read_bytes() == before and disk.snapshot["offset"] == 0


def test_retention_settings_cannot_raise_service_or_task_limits(tmp_path):
    for service, keep in ((True, 5), (False, 33)):
        with pytest.raises(ValueError):
            lg.LogDisk(tmp_path / "unused", JOB, NONCE, service=service, keep=keep)


def test_progress_has_strict_finite_counts_and_never_changes_lifecycle(disk):
    body = b"CBZ_PROGRESS 1/2 50% half done\n"
    disk.append(0, body)
    result = collect(disk)["records"][0]
    assert lg.parse_progress(result) == dict(done=1, total=2, percent=50.0, label="half done", cursor=str(len(body)))
    for text in ("CBZ_PROGRESS 3/2 150% bad\n", "CBZ_PROGRESS 0/0 0% bad\n", "CBZ_PROGRESS 1/2 nan% bad\n"):
        assert lg.parse_progress({**result, "text": text}) is None
    assert lg.parse_progress({**result, "fragment": True}) is None
    assert lg.parse_progress({**result, "continued": True}) is None


def test_fifty_mib_are_durable_and_pageable_without_a_server(disk):
    # No ConsoleApp, HTTP server or sessions exist. A deliberately paced fake
    # producer keeps eight chunks outstanding; overflow is tested separately.
    credits = threading.Semaphore(8)
    block = (b"server-independent output\n" * 3000)[:lg.CHUNK_BYTES]
    total = 50 * 1024 * 1024
    class Producer:
        offset = 0
        def read(self, count):
            if self.offset == total:
                return b""
            assert credits.acquire(timeout=5)
            part = block[:min(count, total - self.offset)]
            self.offset += len(part)
            return part
    append = disk.append
    def acknowledged(*args):
        append(*args)
        credits.release()
    disk.append = acknowledged
    result = lg.LogPump(Producer(), disk).start().join(30)
    assert result.settled and result.complete and result.durable_offset == total
    assert disk.snapshot["gaps"] == []
    actual = hashlib.sha256()
    expected = hashlib.sha256()
    for segment in disk.snapshot["segments"]:
        actual.update((disk.directory / lg._segment(segment["start"])).read_bytes())
    for offset in range(0, total, len(block)):
        expected.update(block[:min(len(block), total - offset)])
    assert actual.digest() == expected.digest()
    # Recreated reader uses only persisted artifacts; runner/server memory is
    # not required for Last-Event-ID resumption.
    index = lg.load_index(reader(disk), JOB, NONCE)
    tail = lg.page(index, reader(disk), cursor=str(total - 10))
    assert tail["eof"] and tail["next_cursor"] == str(total)


@pytest.mark.skipif(os.name != "nt", reason="native Windows file sharing")
def test_native_shared_reader_allows_write_delete_and_noninheritance(tmp_path):
    import msvcrt
    path = tmp_path / "segment"
    path.write_bytes(b"old")
    with shared_read(path) as stream:
        assert not os.get_handle_inheritable(msvcrt.get_osfhandle(stream.fileno()))
        with open(path, "ab") as writer:
            writer.write(b"new")
        path.unlink()
        assert stream.read() == b"oldnew"
    assert not path.exists()


def test_cancel_without_pending_io_is_not_completion_proof():
    assert cancel_thread_io(threading.get_native_id()) is False
