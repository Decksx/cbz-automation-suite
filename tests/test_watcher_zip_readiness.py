from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from scripts import cbz_watcher as watcher
from scripts.cbz_watcher import ZipReadiness, probe_cbz_zip_readiness


def _make_cbz(path: Path, payload_size: int = 50) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("page1.jpg", b"\0" * payload_size)


# --- complete CBZ -------------------------------------------------


def test_complete_cbz_is_ready(tmp_path: Path) -> None:
    cbz = tmp_path / "issue-01.cbz"
    _make_cbz(cbz, payload_size=200)

    result = probe_cbz_zip_readiness(cbz, settle_interval=0)

    assert result.status == ZipReadiness.READY
    assert result.reason == "ok"
    assert result.page_count == 1


def test_complete_cbz_with_multiple_pages_reports_page_count(
    tmp_path: Path,
) -> None:
    cbz = tmp_path / "issue-02.cbz"
    cbz.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(cbz, "w") as zf:
        for index in range(5):
            zf.writestr(f"page{index}.jpg", b"\0" * 32)

    result = probe_cbz_zip_readiness(cbz, settle_interval=0)

    assert result.status == ZipReadiness.READY
    assert result.page_count == 5


# --- partially-written / truncated CBZ -------------------------------------------------


def test_truncated_cbz_retries_later(tmp_path: Path) -> None:
    cbz = tmp_path / "issue-03.cbz"
    _make_cbz(cbz, payload_size=500)

    # Truncate to lop off the end-of-central-directory record, simulating a
    # copy that stopped partway through -- the file exists, has a stable
    # size/mtime *at this instant*, but its central directory cannot be
    # parsed yet.
    original = cbz.read_bytes()
    cbz.write_bytes(original[: len(original) // 2])

    result = probe_cbz_zip_readiness(cbz, settle_interval=0)

    assert result.status == ZipReadiness.RETRY_LATER
    assert result.reason.startswith("incomplete_central_directory")


def test_zero_byte_cbz_retries_later(tmp_path: Path) -> None:
    cbz = tmp_path / "issue-04.cbz"
    cbz.parent.mkdir(parents=True, exist_ok=True)
    cbz.write_bytes(b"")

    result = probe_cbz_zip_readiness(cbz, settle_interval=0)

    assert result.status == ZipReadiness.RETRY_LATER
    assert result.reason.startswith("incomplete_central_directory")


def test_missing_file_retries_later(tmp_path: Path) -> None:
    cbz = tmp_path / "does-not-exist.cbz"

    result = probe_cbz_zip_readiness(cbz, settle_interval=0)

    assert result.status == ZipReadiness.RETRY_LATER
    assert result.reason == "file_not_found"


# --- file that changes between probes -------------------------------------------------


def test_size_change_between_probes_retries_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cbz = tmp_path / "issue-05.cbz"
    _make_cbz(cbz, payload_size=200)

    real_stat = Path.stat
    call_count = {"n": 0}

    def flaky_stat(self, *args, **kwargs):
        result = real_stat(self, *args, **kwargs)
        call_count["n"] += 1
        if self == cbz and call_count["n"] == 2:
            # Simulate the writer appending more bytes between the probe's
            # two stat() samples: report a larger size on the second call.
            return os.stat_result(
                (
                    result.st_mode,
                    result.st_ino,
                    result.st_dev,
                    result.st_nlink,
                    result.st_uid,
                    result.st_gid,
                    result.st_size + 4096,
                    result.st_atime,
                    result.st_mtime,
                    result.st_ctime,
                )
            )
        return result

    monkeypatch.setattr(Path, "stat", flaky_stat)

    result = probe_cbz_zip_readiness(cbz, settle_interval=0)

    assert result.status == ZipReadiness.RETRY_LATER
    assert result.reason == "size_or_mtime_changed"
    # The zip must never have been opened -- the probe should short-circuit
    # on the instability before attempting zipfile.ZipFile(...).infolist().
    assert result.page_count is None


def test_mtime_change_between_probes_retries_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cbz = tmp_path / "issue-06.cbz"
    _make_cbz(cbz, payload_size=200)

    real_stat = Path.stat
    call_count = {"n": 0}

    def flaky_stat(self, *args, **kwargs):
        result = real_stat(self, *args, **kwargs)
        call_count["n"] += 1
        if self == cbz and call_count["n"] == 2:
            return os.stat_result(
                (
                    result.st_mode,
                    result.st_ino,
                    result.st_dev,
                    result.st_nlink,
                    result.st_uid,
                    result.st_gid,
                    result.st_size,
                    result.st_atime,
                    result.st_mtime + 5,
                    result.st_ctime,
                )
            )
        return result

    monkeypatch.setattr(Path, "stat", flaky_stat)

    result = probe_cbz_zip_readiness(cbz, settle_interval=0)

    assert result.status == ZipReadiness.RETRY_LATER
    assert result.reason == "size_or_mtime_changed"


# --- sharing violations / transient errors -------------------------------------------------


def test_permission_error_opening_zip_retries_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cbz = tmp_path / "issue-07.cbz"
    _make_cbz(cbz, payload_size=200)

    def raise_permission_error(*args, **kwargs):
        raise PermissionError("The process cannot access the file because "
                               "it is being used by another process")

    monkeypatch.setattr(watcher.zipfile, "ZipFile", raise_permission_error)

    result = probe_cbz_zip_readiness(cbz, settle_interval=0)

    assert result.status == ZipReadiness.RETRY_LATER
    assert result.reason.startswith("sharing_violation")


def test_transient_os_error_opening_zip_retries_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cbz = tmp_path / "issue-08.cbz"
    _make_cbz(cbz, payload_size=200)

    def raise_os_error(*args, **kwargs):
        raise OSError("A network error occurred while reading the file")

    monkeypatch.setattr(watcher.zipfile, "ZipFile", raise_os_error)

    result = probe_cbz_zip_readiness(cbz, settle_interval=0)

    assert result.status == ZipReadiness.RETRY_LATER
    assert result.reason.startswith("transient_io_error")


def test_stat_os_error_retries_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cbz = tmp_path / "issue-09.cbz"
    _make_cbz(cbz, payload_size=200)

    def raise_os_error(self, *args, **kwargs):
        raise OSError("Transient SMB error reading metadata")

    monkeypatch.setattr(Path, "stat", raise_os_error)

    result = probe_cbz_zip_readiness(cbz, settle_interval=0)

    assert result.status == ZipReadiness.RETRY_LATER
    assert result.reason.startswith("stat_error")


# --- purity / no side effects -------------------------------------------------


def test_probe_does_not_modify_the_file(tmp_path: Path) -> None:
    cbz = tmp_path / "issue-10.cbz"
    _make_cbz(cbz, payload_size=200)

    before_bytes = cbz.read_bytes()
    before_stat = cbz.stat()

    probe_cbz_zip_readiness(cbz, settle_interval=0)

    after_bytes = cbz.read_bytes()
    after_stat = cbz.stat()

    assert before_bytes == after_bytes
    assert before_stat.st_size == after_stat.st_size
    assert before_stat.st_mtime_ns == after_stat.st_mtime_ns
