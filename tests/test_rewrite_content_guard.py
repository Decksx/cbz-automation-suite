"""Regression tests for the central-directory CRC guard on the two
read-rebuild archive-rewrite paths.

The size/mtime guard these sit behind is structurally blind to one case: a
replacement of identical *length* whose mtime lands in the same filesystem
timestamp bucket as the file's previous write. Fault injection on 2026-08-02
measured that case slipping through 11 of 16 times on a 2-second-quantum
volume, with both functions reporting success while the concurrent writer's
content was destroyed (see docs/archive_io_resource_audit.md, "Validated
2026-08-02").

These tests reproduce that case deterministically rather than by racing:
Path.stat is frozen so the size/mtime comparison sees no drift at all --
exactly what a coarse timestamp does in the wild -- while the file's contents
really are replaced mid-rewrite. Only a content-based check can see it, so
each test fails if the CRC comparison is removed.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import cbz_library_maintenance, cbz_sanitizer
from scripts.cbz_library_maintenance import write_comicinfo
from scripts.cbz_sanitizer import _write_cbz_with_comicinfo

NEW_XML = "<ComicInfo><Title>New</Title></ComicInfo>"


def _make_cbz(path: Path, page: bytes = b"original page bytes") -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("ComicInfo.xml", "<ComicInfo><Title>Old</Title></ComicInfo>")
        zf.writestr("001.jpg", page)


def _same_size_replacement(path: Path) -> bytes:
    """A valid archive of identical byte length with different page content."""
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        payload = {i.filename: zf.read(i.filename) for i in infos}

    victim = "001.jpg"
    payload[victim] = b"C" * len(payload[victim])

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zout:
        for info in infos:
            zout.writestr(info, payload[info.filename],
                          compress_type=info.compress_type)
    data = buf.getvalue()
    assert len(data) == path.stat().st_size, "replacement must be the same size"
    return data


def _freeze_stat(target: Path):
    """Report a constant size/mtime for *target*, however it really changes.

    This is what a coarse-quantum filesystem does on its own: the previous
    write and the concurrent write fall in the same bucket, so the guard's
    before/after pair is identical even though the file was replaced.
    """
    real_stat = Path.stat
    frozen = real_stat(target)

    def fake_stat(self, *args, **kwargs):
        if self == target:
            return SimpleNamespace(
                st_size=frozen.st_size, st_mtime_ns=frozen.st_mtime_ns
            )
        return real_stat(self, *args, **kwargs)

    return fake_stat


def _replace_when_tmp_opened(target: Path, replacement: bytes):
    """Swap *target*'s contents the moment the rewrite starts writing its tmp.

    That instant is after the source has been read (so the 'before'
    fingerprint is captured) and before the pre-rename re-check, which is
    precisely the window a concurrent writer occupies.
    """
    real_zipfile = zipfile.ZipFile
    state = {"swapped": False}

    def fake_zipfile(file, mode="r", *args, **kwargs):
        if mode == "w" and not state["swapped"]:
            state["swapped"] = True
            target.write_bytes(replacement)
        return real_zipfile(file, mode, *args, **kwargs)

    return fake_zipfile, state


# ── cbz_library_maintenance.write_comicinfo ──────────────────────


def test_write_comicinfo_abandons_on_same_size_content_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cbz_path = tmp_path / "issue.cbz"
    _make_cbz(cbz_path)
    replacement = _same_size_replacement(cbz_path)

    fake_zip, state = _replace_when_tmp_opened(cbz_path, replacement)
    monkeypatch.setattr(zipfile, "ZipFile", fake_zip)
    monkeypatch.setattr(Path, "stat", _freeze_stat(cbz_path))

    assert not write_comicinfo(cbz_path, "ComicInfo.xml", NEW_XML, dry_run=False)

    assert state["swapped"], "harness never injected the concurrent change"
    # The other writer's bytes survive, and nothing is left behind.
    assert cbz_path.read_bytes() == replacement
    assert not cbz_path.with_suffix(".tmp.cbz").exists()
    assert not cbz_path.with_suffix(".bak.cbz").exists()


def test_write_comicinfo_normal_rewrite_unaffected_by_content_guard(
    tmp_path: Path,
) -> None:
    # The guard must not trip when nothing concurrent happens: 0/8 false
    # positives was a condition of adopting it.
    cbz_path = tmp_path / "quiet.cbz"
    _make_cbz(cbz_path)

    assert write_comicinfo(cbz_path, "ComicInfo.xml", NEW_XML, dry_run=False)

    with zipfile.ZipFile(cbz_path) as zf:
        assert "New" in zf.read("ComicInfo.xml").decode()
    assert not cbz_path.with_suffix(".tmp.cbz").exists()


# ── cbz_sanitizer._write_cbz_with_comicinfo ──────────────────────


def test_sanitizer_abandons_on_same_size_content_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cbz_path = tmp_path / "issue.cbz"
    _make_cbz(cbz_path)
    replacement = _same_size_replacement(cbz_path)

    # Keep the retry loop, but do not actually sleep 5s per attempt.
    monkeypatch.setattr(cbz_sanitizer, "FILE_LOCK_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(cbz_sanitizer, "FILE_LOCK_RETRY_DELAY_SECONDS", 0.0)

    fake_zip, state = _replace_when_tmp_opened(cbz_path, replacement)
    monkeypatch.setattr(zipfile, "ZipFile", fake_zip)
    monkeypatch.setattr(Path, "stat", _freeze_stat(cbz_path))

    _write_cbz_with_comicinfo(cbz_path, NEW_XML, replace_entry="ComicInfo.xml")

    assert state["swapped"], "harness never injected the concurrent change"
    # Detected on the first attempt; the swap only fires once, so the retry
    # re-reads the replaced file and completes from *its* contents. Either
    # way the concurrent writer's page must never be silently discarded.
    with zipfile.ZipFile(cbz_path) as zf:
        assert zf.read("001.jpg") == b"C" * len(b"original page bytes")
    assert not cbz_path.with_suffix(".tmp.cbz").exists()


def test_sanitizer_normal_rewrite_unaffected_by_content_guard(
    tmp_path: Path,
) -> None:
    cbz_path = tmp_path / "quiet.cbz"
    _make_cbz(cbz_path)

    _write_cbz_with_comicinfo(cbz_path, NEW_XML, replace_entry="ComicInfo.xml")

    with zipfile.ZipFile(cbz_path) as zf:
        assert "New" in zf.read("ComicInfo.xml").decode()
        assert zf.read("001.jpg") == b"original page bytes"
    assert not cbz_path.with_suffix(".tmp.cbz").exists()
