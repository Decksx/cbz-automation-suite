"""Real publications: bounded recovery must not weaken durability or EOF proof."""
import copy
import errno
import io
import json
from pathlib import Path

import pytest

from comic_automation.web.jobs import logs as lg

JOB = "00000000000000000000000001"
NONCE = "0" * 32


def index_error(directory):
    error = PermissionError(errno.EACCES, "injected index replacement refusal")
    error.winerror = 5
    error.filename = str(directory / "index.tmp")
    error.filename2 = str(directory / "index.json")
    return error


@pytest.fixture
def disk(tmp_path):
    return lg.LogDisk(tmp_path / "log", JOB, NONCE)


@pytest.mark.parametrize("failures", [1, 2, 3, 4])
def test_initial_publication_recovers_within_the_bound(tmp_path, monkeypatch, failures):
    directory = tmp_path / "log"
    native, calls, waits = lg.os.replace, [], []
    def replace(source, target):
        calls.append((source, target))
        if len(calls) <= failures:
            raise index_error(directory)
        return native(source, target)
    monkeypatch.setattr(lg.os, "replace", replace)
    monkeypatch.setattr(lg.time, "sleep", waits.append)
    created = lg.LogDisk(directory, JOB, NONCE)
    assert len(calls) == failures + 1 and waits == [0.02] * failures
    assert json.loads(created.index_path.read_bytes()) == created.snapshot


def test_each_retry_revalidates_rewrites_and_fsyncs_without_early_watermark(disk, monkeypatch):
    before = disk.index_path.read_bytes()
    native_replace, native_fsync = lg.os.replace, lg.os.fsync
    calls, synced, validated, waits = [], [], [], []
    monkeypatch.setattr(disk, "validate", validated.append)
    def fsync(fd):
        synced.append(fd)
        return native_fsync(fd)
    def replace(source, target):
        calls.append(json.loads(Path(source).read_bytes()))
        assert disk.snapshot["offset"] == 0 and disk.index_path.read_bytes() == before
        assert len(synced) >= len(calls) + 1  # data plus every fresh index write
        assert validated.count(disk.directory / "index.tmp") == len(calls)
        assert validated.count(disk.index_path) == len(calls)
        if len(calls) < 3:
            # Retrying just replace would publish this damaged temporary file.
            Path(source).write_bytes(b"damaged temporary index")
            raise index_error(disk.directory)
        return native_replace(source, target)
    monkeypatch.setattr(lg.os, "replace", replace)
    monkeypatch.setattr(lg.os, "fsync", fsync)
    monkeypatch.setattr(lg.time, "sleep", waits.append)
    disk.append(0, b"kept\n")
    assert len(calls) == 3 and calls[0] == calls[1] == calls[2]
    assert waits == [0.02, 0.02] and disk.snapshot["offset"] == 5
    assert json.loads(disk.index_path.read_bytes()) == disk.snapshot


def test_exhausted_refusal_preserves_the_old_durable_index(disk, monkeypatch):
    before, calls, waits = disk.index_path.read_bytes(), [], []
    error = index_error(disk.directory)
    def replace(*args):
        calls.append(args)
        raise error
    monkeypatch.setattr(lg.os, "replace", replace)
    monkeypatch.setattr(lg.time, "sleep", waits.append)
    with pytest.raises(PermissionError) as caught:
        disk.append(0, b"uncommitted\n")
    assert caught.value is error and len(calls) == 5 and waits == [0.02] * 4
    assert disk.index_path.read_bytes() == before and disk.snapshot["offset"] == 0


@pytest.mark.parametrize("fault", ["other_winerror", "non_windows", "source", "target", "missing_source", "missing_target", "disk_full", "wrong_exception_type"])
def test_other_replace_errors_propagate_without_retry(disk, monkeypatch, fault):
    error = index_error(disk.directory)
    if fault == "other_winerror":
        error.winerror = 32
    elif fault == "non_windows":
        del error.winerror
    elif fault == "source":
        error.filename = str(disk.directory / "segment.log")
    elif fault == "target":
        error.filename2 = str(disk.directory / "other-index.json")
    elif fault == "missing_source":
        error.filename = None
    elif fault == "missing_target":
        error.filename2 = None
    elif fault == "disk_full":
        error = OSError(errno.ENOSPC, "disk full")
    else:
        error = OSError(errno.EIO, "not a permission refusal")
        error.winerror = 5
        error.filename = str(disk.directory / "index.tmp")
        error.filename2 = str(disk.index_path)
    calls, before = [], disk.index_path.read_bytes()
    def replace(*args):
        calls.append(args)
        raise error
    monkeypatch.setattr(lg.os, "replace", replace)
    monkeypatch.setattr(lg.time, "sleep", lambda _: pytest.fail("unrelated error retried"))
    with pytest.raises(OSError) as caught:
        disk.append(0, b"uncommitted\n")
    assert caught.value is error and len(calls) == 1
    assert disk.index_path.read_bytes() == before and disk.snapshot["offset"] == 0


@pytest.mark.parametrize("change", ["claim", "coverage", "size"])
def test_retry_rechecks_claim_candidate_and_size_before_writing(disk, monkeypatch, change):
    candidate = copy.deepcopy(disk.snapshot)
    calls, revoked = [], []
    before = disk.index_path.read_bytes()
    def replace(*args):
        calls.append(args)
        raise index_error(disk.directory)
    def sleep(_):
        revoked.append(True)
        if change == "coverage":
            candidate["offset"] = 1
        elif change == "size":
            candidate["padding"] = "x" * lg.MAX_INDEX_BYTES
    def validate(path):
        if revoked and change == "claim":
            raise lg.LogRefused("CLAIM_CLOSED")
    monkeypatch.setattr(lg.os, "replace", replace)
    monkeypatch.setattr(lg.time, "sleep", sleep)
    monkeypatch.setattr(disk, "validate", validate)
    with pytest.raises(lg.LogRefused):
        disk._publish(candidate)
    assert len(calls) == 1 and disk.index_path.read_bytes() == before
    assert disk.snapshot["offset"] == 0


def test_index_fsync_error_is_not_a_replace_retry(disk, monkeypatch):
    calls = []
    error = index_error(disk.directory)
    def fsync(_):
        calls.append(True)
        raise error
    monkeypatch.setattr(lg.os, "fsync", fsync)
    monkeypatch.setattr(lg.time, "sleep", lambda _: pytest.fail("fsync error retried"))
    with pytest.raises(PermissionError) as caught:
        disk._publish(copy.deepcopy(disk.snapshot))
    assert caught.value is error and len(calls) == 1


@pytest.mark.parametrize("phase", ["append", "finish"])
@pytest.mark.parametrize("failures", [1, 4, 5])
def test_claimed_pump_only_keeps_eof_complete_when_recovery_stays_inside_publish(disk, monkeypatch, phase, failures):
    native, refused = lg.os.replace, []
    def replace(source, target):
        candidate = json.loads(Path(source).read_bytes())
        matching = candidate["complete"] if phase == "finish" else candidate["offset"] > 0 and not candidate["complete"]
        if matching and len(refused) < failures:
            refused.append(True)
            raise index_error(disk.directory)
        return native(source, target)
    monkeypatch.setattr(lg.os, "replace", replace)
    monkeypatch.setattr(lg.time, "sleep", lambda _: None)
    body = b"retained bytes\n"
    pump = lg.LogPump(io.BytesIO(body), disk, sticky_failure=True, retry_seconds=0.001).start()
    result = pump.join(5)
    assert result.settled and result.read_offset == result.durable_offset == len(body)
    assert len(refused) == failures
    assert result.complete is (failures < 5)
    assert result.error == (None if failures < 5 else "PermissionError")
    assert disk.snapshot["complete"] is result.complete
    assert (disk.directory / "seg-0000000000000000.log").read_bytes() == body


def test_unrelated_storage_failure_stays_sticky_after_recovery(disk, monkeypatch):
    native, failures = disk._publish, []
    def publish(candidate):
        if candidate["offset"] > 0 and not failures:
            failures.append(True)
            raise OSError(errno.EIO, "unrelated storage failure")
        return native(candidate)
    monkeypatch.setattr(disk, "_publish", publish)
    body = b"retained after unrelated failure\n"
    result = lg.LogPump(io.BytesIO(body), disk, sticky_failure=True, retry_seconds=0.001).start().join(5)
    assert result.settled and result.read_offset == result.durable_offset == len(body)
    assert not result.complete and result.error == "OSError"
    assert not disk.snapshot["complete"]


def test_transient_publication_recovery_preserves_explicit_gap_coverage(disk, monkeypatch):
    native, failures = lg.os.replace, []
    def replace(source, target):
        if not failures:
            failures.append(True)
            raise index_error(disk.directory)
        return native(source, target)
    monkeypatch.setattr(lg.os, "replace", replace)
    monkeypatch.setattr(lg.time, "sleep", lambda _: None)
    disk.append(3, b"tail\n")
    disk.finish(8, complete=True)
    def read(name, start, count):
        return (disk.directory / name).read_bytes()[start:start + count]
    result = lg.page(lg.load_index(read, JOB, NONCE), read)
    assert result["eof"]
    assert result["records"][0] == {"type": "gap", "from": "0", "to": "3", "reason": "dropped"}
    assert result["records"][1]["text"] == "tail\n"

