"""Regression tests for the pre-rename staleness guards added to
cbz_library_maintenance.py's two archive-replacement paths.

Both closures come from docs/archive_io_resource_audit.md, "Small,
low-risk improvements": re-verify the target's size/mtime immediately
before the destructive step, mirroring the before/after stat() pattern
already proven in comic_automation/archive/{page_hashing,
perceptual_hashing}.py.

Unlike cbz_sanitizer.py, this module has no retry loop anywhere (the
audit confirmed zero `sleep(` call sites), so a detected drift is not
retried -- it is caught by the function's own broad exception handler,
the rewrite/replace is abandoned, and the original file is left intact.
That "abandon, don't retry" outcome is what these tests pin down.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.cbz_library_maintenance import (
    pack_image_folder,
    write_comicinfo,
)


def _make_cbz(path: Path, title: str = "Old") -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "ComicInfo.xml",
            f"<ComicInfo><Title>{title}</Title></ComicInfo>",
        )
        zf.writestr("001.jpg", b"fake page bytes")


def _drifting_stat(target: Path, *, on_call: int):
    """Patch Path.stat so the Nth stat() of *target* reports a changed mtime."""
    real_stat = Path.stat
    calls = {"n": 0}

    def fake_stat(self, *args, **kwargs):
        result = real_stat(self, *args, **kwargs)
        if self == target:
            calls["n"] += 1
            if calls["n"] == on_call:
                return SimpleNamespace(
                    st_size=result.st_size,
                    st_mtime_ns=result.st_mtime_ns + 1_000_000_000,
                )
        return result

    return fake_stat


# ── write_comicinfo ──────────────────────────────────────────────


def test_write_comicinfo_normal_rewrite_still_succeeds(
    tmp_path: Path,
) -> None:
    # No drift: the new guard must not disturb the ordinary happy path.
    cbz_path = tmp_path / "issue.cbz"
    _make_cbz(cbz_path)

    assert write_comicinfo(
        cbz_path,
        "ComicInfo.xml",
        "<ComicInfo><Title>New</Title></ComicInfo>",
        dry_run=False,
    )

    with zipfile.ZipFile(cbz_path) as zf:
        assert "New" in zf.read("ComicInfo.xml").decode()
    assert not cbz_path.with_suffix(".tmp.cbz").exists()
    assert not cbz_path.with_suffix(".bak.cbz").exists()


def test_write_comicinfo_dry_run_makes_no_changes(
    tmp_path: Path,
) -> None:
    cbz_path = tmp_path / "issue.cbz"
    _make_cbz(cbz_path)

    assert write_comicinfo(
        cbz_path,
        "ComicInfo.xml",
        "<ComicInfo><Title>New</Title></ComicInfo>",
        dry_run=True,
    )

    with zipfile.ZipFile(cbz_path) as zf:
        assert "Old" in zf.read("ComicInfo.xml").decode()


def test_write_comicinfo_abandons_rewrite_on_concurrent_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Second stat() of cbz_path is the "after" re-check; make it disagree
    # with the "before" snapshot, as a concurrent writer would.
    cbz_path = tmp_path / "issue.cbz"
    _make_cbz(cbz_path)

    monkeypatch.setattr(
        Path, "stat", _drifting_stat(cbz_path, on_call=2)
    )

    assert not write_comicinfo(
        cbz_path,
        "ComicInfo.xml",
        "<ComicInfo><Title>New</Title></ComicInfo>",
        dry_run=False,
    )

    # Original untouched, no leftover artifacts, no retry attempted.
    with zipfile.ZipFile(cbz_path) as zf:
        assert "Old" in zf.read("ComicInfo.xml").decode()
    assert not cbz_path.with_suffix(".tmp.cbz").exists()


# ── pack_image_folder ────────────────────────────────────────────


def _make_image_folder(folder: Path, *, page_bytes: bytes) -> None:
    folder.mkdir()
    (folder / "001.jpg").write_bytes(page_bytes)
    (folder / "002.jpg").write_bytes(page_bytes)


def test_pack_image_folder_creates_archive_when_none_exists(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "Series Ch 1"
    _make_image_folder(folder, page_bytes=b"x" * 500)

    stats = pack_image_folder(folder, dry_run=False)

    cbz_path = tmp_path / "Series Ch 1.cbz"
    assert stats.packed == 1
    assert cbz_path.exists()
    assert not folder.exists()


def test_pack_image_folder_keeps_existing_when_not_larger(
    tmp_path: Path,
) -> None:
    # Existing archive is bigger, so the freshly packed one is discarded
    # and the existing file is never unlinked -- the guard is not even
    # reached on this branch.
    folder = tmp_path / "Series Ch 2"
    _make_image_folder(folder, page_bytes=b"x" * 10)

    cbz_path = tmp_path / "Series Ch 2.cbz"
    cbz_path.write_bytes(b"y" * 100_000)

    stats = pack_image_folder(folder, dry_run=False)

    assert stats.skipped == 1
    assert cbz_path.read_bytes() == b"y" * 100_000
    assert not cbz_path.with_suffix(".tmp.cbz").exists()


def test_pack_image_folder_abandons_replace_on_concurrent_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The existing archive is small enough to be replaced, but it changes
    # between the size decision and the unlink. The guard must abandon
    # rather than delete a file it measured before the change.
    folder = tmp_path / "Series Ch 3"
    _make_image_folder(folder, page_bytes=b"x" * 5000)

    cbz_path = tmp_path / "Series Ch 3.cbz"
    original = b"y" * 10
    cbz_path.write_bytes(original)

    # stat #1 = existing_stat (the decision), stat #2 = the re-check.
    monkeypatch.setattr(
        Path, "stat", _drifting_stat(cbz_path, on_call=2)
    )

    stats = pack_image_folder(folder, dry_run=False)

    # Existing archive survives untouched; the pack is counted as an
    # error rather than silently destroying the other writer's file.
    assert stats.errors == 1
    assert stats.packed == 0
    assert cbz_path.read_bytes() == original
    assert not cbz_path.with_suffix(".tmp.cbz").exists()
