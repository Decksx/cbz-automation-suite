"""Regression tests for _write_cbz_with_comicinfo()'s pre-rename staleness
guard (see docs/archive_io_resource_audit.md, "Small, low-risk
improvements": a size/mtime re-check immediately before the destructive
rename, mirroring the before/after stat() pattern already proven in
comic_automation/archive/{page_hashing,perceptual_hashing}.py).
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.cbz_sanitizer import (
    FILE_LOCK_RETRY_ATTEMPTS,
    _write_cbz_with_comicinfo,
)
import scripts.cbz_sanitizer as sanitizer


def _make_cbz(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "ComicInfo.xml",
            "<ComicInfo><Title>Old</Title></ComicInfo>",
        )
        zf.writestr("001.jpg", b"fake page bytes")


def test_normal_rewrite_still_succeeds_on_first_attempt(
    tmp_path: Path,
) -> None:
    # No drift, no lock contention: the new staleness check must not
    # change the ordinary happy path.
    cbz_path = tmp_path / "issue.cbz"
    _make_cbz(cbz_path)

    _write_cbz_with_comicinfo(
        cbz_path,
        "<ComicInfo><Title>New</Title></ComicInfo>",
        replace_entry="ComicInfo.xml",
    )

    with zipfile.ZipFile(cbz_path) as zf:
        assert "New" in zf.read("ComicInfo.xml").decode()
    assert not cbz_path.with_suffix(".tmp.cbz").exists()
    assert not cbz_path.with_suffix(".bak.cbz").exists()


def test_concurrent_modification_is_detected_and_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Simulate another writer touching cbz_path between the read at the
    # top of the function and the rename at the bottom: the first
    # attempt's "after" stat() call reports a different mtime than its
    # "before" call, which must raise (routing into the existing
    # file-locked retry branch) rather than silently proceeding to
    # rename over the other writer's output. The second attempt sees a
    # stable file and must succeed.
    cbz_path = tmp_path / "issue.cbz"
    _make_cbz(cbz_path)

    real_stat = Path.stat
    calls = {"n": 0}

    def flaky_stat(self, *args, **kwargs):
        result = real_stat(self, *args, **kwargs)
        if self == cbz_path:
            calls["n"] += 1
            if calls["n"] == 2:
                # The "after" call on the first attempt: report a
                # different mtime than whatever "before" just saw.
                return SimpleNamespace(
                    st_size=result.st_size,
                    st_mtime_ns=result.st_mtime_ns + 1_000_000_000,
                )
        return result

    monkeypatch.setattr(Path, "stat", flaky_stat)
    # Avoid the real 5s sleep between retry attempts in this test.
    monkeypatch.setattr(sanitizer.time, "sleep", lambda _seconds: None)

    _write_cbz_with_comicinfo(
        cbz_path,
        "<ComicInfo><Title>New</Title></ComicInfo>",
        replace_entry="ComicInfo.xml",
    )

    with zipfile.ZipFile(cbz_path) as zf:
        assert "New" in zf.read("ComicInfo.xml").decode()
    # Detected drift on attempt 1, succeeded on attempt 2 -- strictly
    # fewer than the full retry budget was consumed.
    assert calls["n"] < FILE_LOCK_RETRY_ATTEMPTS * 2


def test_persistent_drift_exhausts_retries_without_corrupting_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If the file *never* stabilizes, the function must give up after
    # FILE_LOCK_RETRY_ATTEMPTS rather than eventually renaming over
    # stale data, and the original archive must be left untouched (no
    # partial .bak.cbz/.tmp.cbz artifacts, original content intact).
    cbz_path = tmp_path / "issue.cbz"
    _make_cbz(cbz_path)

    real_stat = Path.stat
    counter = {"n": 0}

    def always_drifting_stat(self, *args, **kwargs):
        result = real_stat(self, *args, **kwargs)
        if self == cbz_path:
            # A monotonic counter guarantees every call sees a distinct
            # mtime, unlike id(object()), which can coincidentally repeat
            # across calls once a short-lived object's memory is reused
            # by the next allocation -- that flakily let two consecutive
            # stat() calls agree "by chance" and made this test pass for
            # the wrong reason.
            counter["n"] += 1
            return SimpleNamespace(
                st_size=result.st_size,
                st_mtime_ns=result.st_mtime_ns + counter["n"],
            )
        return result

    monkeypatch.setattr(Path, "stat", always_drifting_stat)
    monkeypatch.setattr(sanitizer.time, "sleep", lambda _seconds: None)

    _write_cbz_with_comicinfo(
        cbz_path,
        "<ComicInfo><Title>New</Title></ComicInfo>",
        replace_entry="ComicInfo.xml",
    )

    # Gave up: original file (still "Old") is untouched, no leftover
    # temp/backup files.
    with zipfile.ZipFile(cbz_path) as zf:
        assert "Old" in zf.read("ComicInfo.xml").decode()
    assert not cbz_path.with_suffix(".tmp.cbz").exists()
    assert not cbz_path.with_suffix(".bak.cbz").exists()
