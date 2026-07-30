from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from scripts import cbz_watcher as watcher
from scripts.cbz_watcher import (
    ZipReadiness,
    probe_cbz_directory_readiness,
    probe_cbz_zip_readiness,
)


def _make_cbz(path: Path, page_count: int = 1, payload_size: int = 50) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for index in range(page_count):
            zf.writestr(f"page{index}.jpg", b"\0" * payload_size)


def _truncate(path: Path) -> None:
    original = path.read_bytes()
    path.write_bytes(original[: len(original) // 2])


# --- all-ready -------------------------------------------------


def test_all_ready_directory(tmp_path: Path) -> None:
    paths = [tmp_path / f"issue-{i}.cbz" for i in range(3)]
    for path in paths:
        _make_cbz(path, page_count=2)

    result = probe_cbz_directory_readiness(paths, settle_interval=0)

    assert result.status == ZipReadiness.READY
    assert result.reason == "ok"
    assert result.archive_count == 3
    assert result.ready_count == 3
    assert [entry.status for entry in result.entries] == [
        ZipReadiness.READY
    ] * 3


# --- mixed readiness -------------------------------------------------


def test_mixed_readiness_is_not_ready(tmp_path: Path) -> None:
    ready_path = tmp_path / "issue-ready.cbz"
    truncated_path = tmp_path / "issue-truncated.cbz"
    _make_cbz(ready_path, page_count=1)
    _make_cbz(truncated_path, page_count=1, payload_size=500)
    _truncate(truncated_path)

    result = probe_cbz_directory_readiness(
        [ready_path, truncated_path], settle_interval=0
    )

    assert result.status == ZipReadiness.RETRY_LATER
    assert result.reason == "not_all_ready"
    assert result.archive_count == 2
    assert result.ready_count == 1
    assert [entry.path for entry in result.entries] == [
        ready_path,
        truncated_path,
    ]
    assert result.entries[0].status == ZipReadiness.READY
    assert result.entries[1].status == ZipReadiness.RETRY_LATER
    assert result.entries[1].reason.startswith("incomplete_central_directory")


# --- multiple failures -------------------------------------------------


def test_multiple_failures_reports_each_diagnostic(tmp_path: Path) -> None:
    truncated_path = tmp_path / "issue-truncated.cbz"
    missing_path = tmp_path / "issue-missing.cbz"
    _make_cbz(truncated_path, page_count=1, payload_size=500)
    _truncate(truncated_path)

    result = probe_cbz_directory_readiness(
        [truncated_path, missing_path], settle_interval=0
    )

    assert result.status == ZipReadiness.RETRY_LATER
    assert result.reason == "not_all_ready"
    assert result.archive_count == 2
    assert result.ready_count == 0
    assert result.entries[0].reason.startswith("incomplete_central_directory")
    assert result.entries[1].reason == "file_not_found"
    # Diagnostics for both failures are preserved independently, not just
    # the first one encountered.
    assert result.entries[0].path == truncated_path
    assert result.entries[1].path == missing_path


# --- empty input -------------------------------------------------


def test_empty_input_is_retry_later_no_cbz_files() -> None:
    result = probe_cbz_directory_readiness([], settle_interval=0)

    assert result.status == ZipReadiness.RETRY_LATER
    assert result.reason == "no_cbz_files"
    assert result.entries == ()
    assert result.archive_count == 0
    assert result.ready_count == 0
    assert result.page_count == 0


# --- page-count aggregation -------------------------------------------------


def test_page_count_aggregation_sums_ready_archives_only(
    tmp_path: Path,
) -> None:
    ready_a = tmp_path / "issue-a.cbz"
    ready_b = tmp_path / "issue-b.cbz"
    truncated = tmp_path / "issue-c.cbz"
    _make_cbz(ready_a, page_count=3)
    _make_cbz(ready_b, page_count=4)
    _make_cbz(truncated, page_count=1, payload_size=500)
    _truncate(truncated)

    result = probe_cbz_directory_readiness(
        [ready_a, ready_b, truncated], settle_interval=0
    )

    # Truncated archive contributes 0 (its page_count is None), so the
    # aggregate is exactly the sum of the two ready archives' page counts.
    assert result.page_count == 7
    assert result.entries[2].page_count is None


def test_page_count_aggregation_all_ready(tmp_path: Path) -> None:
    paths = [tmp_path / f"issue-{i}.cbz" for i in range(3)]
    for index, path in enumerate(paths, start=1):
        _make_cbz(path, page_count=index)

    result = probe_cbz_directory_readiness(paths, settle_interval=0)

    assert result.status == ZipReadiness.READY
    assert result.page_count == 1 + 2 + 3


# --- deterministic ordering -------------------------------------------------


def test_entries_preserve_input_order_regardless_of_readiness(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "b-ready.cbz"
    missing = tmp_path / "a-missing.cbz"
    truncated = tmp_path / "c-truncated.cbz"
    _make_cbz(ready, page_count=1)
    _make_cbz(truncated, page_count=1, payload_size=500)
    _truncate(truncated)

    # Deliberately not in alphabetical/filesystem order -- the aggregator
    # must preserve exactly the order it was given, not re-sort by path or
    # by readiness.
    result = probe_cbz_directory_readiness(
        [ready, missing, truncated], settle_interval=0
    )

    assert [entry.path for entry in result.entries] == [
        ready,
        missing,
        truncated,
    ]


# --- purity -------------------------------------------------


def test_probe_calls_zip_readiness_exactly_once_per_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = [tmp_path / f"issue-{i}.cbz" for i in range(4)]
    for path in paths:
        _make_cbz(path, page_count=1)

    call_counts: dict[Path, int] = {path: 0 for path in paths}
    real_probe = probe_cbz_zip_readiness

    def counting_probe(path, **kwargs):
        call_counts[path] += 1
        return real_probe(path, **kwargs)

    monkeypatch.setattr(watcher, "probe_cbz_zip_readiness", counting_probe)

    watcher.probe_cbz_directory_readiness(paths, settle_interval=0)

    assert call_counts == {path: 1 for path in paths}


def test_probe_does_not_modify_any_file(tmp_path: Path) -> None:
    paths = [tmp_path / f"issue-{i}.cbz" for i in range(3)]
    for path in paths:
        _make_cbz(path, page_count=2)

    before = {path: (path.read_bytes(), path.stat()) for path in paths}

    probe_cbz_directory_readiness(paths, settle_interval=0)

    for path in paths:
        after_bytes = path.read_bytes()
        after_stat = path.stat()
        before_bytes, before_stat = before[path]
        assert after_bytes == before_bytes
        assert after_stat.st_size == before_stat.st_size
        assert after_stat.st_mtime_ns == before_stat.st_mtime_ns


def test_probe_does_not_create_extra_files(tmp_path: Path) -> None:
    paths = [tmp_path / f"issue-{i}.cbz" for i in range(2)]
    for path in paths:
        _make_cbz(path, page_count=1)

    before_listing = sorted(p.name for p in tmp_path.iterdir())

    probe_cbz_directory_readiness(paths, settle_interval=0)

    after_listing = sorted(p.name for p in tmp_path.iterdir())
    assert before_listing == after_listing
