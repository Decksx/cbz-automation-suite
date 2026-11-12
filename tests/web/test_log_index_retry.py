"""The stress-fixture retry must not broaden the production storage contract."""
import copy
import errno
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from comic_automation.web.jobs import logs as lg
import log_test_support as support
from log_test_support import retry_log_index

JOB = "00000000000000000000000001"
NONCE = "0" * 32


def index_error(disk, *, winerror=5, source=None, target=None):
    error = PermissionError(errno.EACCES, "injected index replacement refusal")
    error.winerror = winerror
    error.filename = str(source if source is not None else disk.directory / "index.tmp")
    error.filename2 = str(target if target is not None else disk.index_path)
    return error


def dummy(tmp_path):
    return SimpleNamespace(directory=tmp_path, index_path=tmp_path / "index.json")


def test_retry_recovers_the_same_candidate_and_preserves_the_return(tmp_path, monkeypatch):
    disk, candidate = dummy(tmp_path), object()
    calls, waits = [], []
    error = index_error(disk)
    marker = object()

    def publish(got_disk, got_candidate):
        calls.append((got_disk, got_candidate))
        if len(calls) < 3:
            raise error
        return marker

    monkeypatch.setattr(support.time, "sleep", waits.append)
    with pytest.warns(pytest.PytestWarning, match="Transient log index WinError5") as reported:
        assert support._publish_with_retry(disk, candidate, publish) is marker
    assert calls == [(disk, candidate)] * 3 and waits == [0.02, 0.02]
    assert len(reported) == 2


def test_permanent_index_refusal_stops_after_five_calls_and_is_not_swallowed(tmp_path, monkeypatch):
    disk = dummy(tmp_path)
    error, calls, waits = index_error(disk), [], []

    def publish(*args):
        calls.append(args)
        raise error

    monkeypatch.setattr(support.time, "sleep", waits.append)
    with pytest.warns(pytest.PytestWarning) as reported:
        with pytest.raises(PermissionError) as caught:
            support._publish_with_retry(disk, object(), publish)
    assert caught.value is error and len(calls) == 5
    assert waits == [0.02] * 4 and len(reported) == 4


@pytest.mark.parametrize("fault", ["other_winerror", "non_windows", "source", "target", "missing_source", "missing_target", "disk_full"])
def test_other_errors_propagate_without_retry(tmp_path, monkeypatch, fault):
    disk = dummy(tmp_path)
    error = index_error(disk)
    if fault == "other_winerror":
        error.winerror = 32
    elif fault == "non_windows":
        del error.winerror
    elif fault == "source":
        error.filename = str(tmp_path / "segment.log")
    elif fault == "target":
        error.filename2 = str(tmp_path / "other-index.json")
    elif fault == "missing_source":
        error.filename = None
    elif fault == "missing_target":
        error.filename2 = None
    else:
        error = OSError(errno.ENOSPC, "disk full")
    calls = []

    def publish(*args):
        calls.append(args)
        raise error

    monkeypatch.setattr(support.time, "sleep", lambda _: pytest.fail("unrelated failure retried"))
    with pytest.raises(OSError) as caught:
        support._publish_with_retry(disk, object(), publish)
    assert caught.value is error and len(calls) == 1


def test_default_writer_still_exposes_the_first_index_failure(tmp_path, monkeypatch):
    disk = lg.LogDisk(tmp_path / "log", JOB, NONCE)
    before = disk.index_path.read_bytes()
    error = index_error(disk)
    real_replace = lg.os.replace

    def refuse(source, target):
        if Path(target) == disk.index_path:
            raise error
        return real_replace(source, target)

    monkeypatch.setattr(lg.os, "replace", refuse)
    with pytest.raises(PermissionError) as caught:
        disk.append(0, b"uncommitted\n")
    assert caught.value is error
    assert disk.index_path.read_bytes() == before and disk.snapshot["offset"] == 0


def test_opt_in_retries_a_real_fsynced_publication_without_early_watermark(tmp_path, monkeypatch, retry_log_index):
    disk = lg.LogDisk(tmp_path / "log", JOB, NONCE)
    before = disk.index_path.read_bytes()
    candidate = copy.deepcopy(disk.snapshot)
    real_replace = lg.os.replace
    calls, fsyncs = [], []
    real_fsync = lg.os.fsync

    def fsync(fd):
        fsyncs.append(fd)
        return real_fsync(fd)

    def replace(source, target):
        if Path(target) == disk.index_path:
            calls.append(json.loads(Path(source).read_bytes()))
            assert disk.snapshot == candidate and disk.index_path.read_bytes() == before
            assert len(fsyncs) >= len(calls) + 1  # segment and each index fsync
            if len(calls) == 1:
                raise index_error(disk)
        return real_replace(source, target)

    monkeypatch.setattr(lg.os, "replace", replace)
    monkeypatch.setattr(lg.os, "fsync", fsync)
    with pytest.warns(pytest.PytestWarning):
        disk.append(0, b"kept\n")
    assert len(calls) == 2 and calls[0] == calls[1]
    assert disk.snapshot["offset"] == 5
    assert (disk.directory / "seg-0000000000000000.log").read_bytes() == b"kept\n"

