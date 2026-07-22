"""Regression tests for the "two chapters, one generic filename" data-loss bug.

Incident: two different issues of the same series ("Arrest Thy Neighbor" and
"Arrest Thy Neighbor 2") were dropped into separate folders. Both contained an
archive named identically by the release tool ("volvox_Chapter.cbz" — a
generic placeholder some scanlation tools reuse verbatim for every release).
Because neither archive carried its own chapter number, both got renamed to
the exact same on-disk name ("volvox Chapter.cbz"), and when the second
directory was merged into the series folder created by the first, the watcher
treated them as the same file and silently discarded the smaller one.

The fix (see cbz_watcher._apply_fallback_naming / _backfill_chapter_one) gives
placeholder-named archives a unique, chapter-numbered filename derived from
the enclosing directory before any merge happens, so sibling chapters can
never collide under one generic name again.
"""

import zipfile
from pathlib import Path

from scripts import cbz_watcher as watcher


def _make_cbz(path: Path, payload_size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("page1.jpg", b"\0" * payload_size)


def _payload_size(cbz_path: Path) -> int:
    with zipfile.ZipFile(cbz_path) as zf:
        return len(zf.read("page1.jpg"))


def test_generic_same_name_releases_get_unique_chapter_names(tmp_path, monkeypatch):
    watch_root = tmp_path / "incoming"
    dest_root = tmp_path / "library"
    watch_root.mkdir()
    dest_root.mkdir()

    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(watch_root))
    monkeypatch.setattr(watcher, "_routing_rules", [])
    monkeypatch.setattr(watcher, "_routing_default", str(dest_root))
    monkeypatch.setattr(watcher, "POLL_INTERVAL", 0)

    dir1 = watch_root / "Arrest Thy Neighbor"
    dir2 = watch_root / "Arrest Thy Neighbor 2"
    _make_cbz(dir1 / "volvox_Chapter.cbz", 200)
    _make_cbz(dir2 / "volvox_Chapter.cbz", 400)

    # Two separate watcher-triggered runs, exactly as in the original incident
    # (the two directories were dropped/settled a minute apart).
    watcher.process_and_move_directory(dir1)
    watcher.process_and_move_directory(dir2)

    series_dir = dest_root / "Arrest Thy Neighbor"
    assert series_dir.is_dir()

    names = sorted(p.name for p in series_dir.glob("*.cbz"))
    assert names == [
        "Arrest Thy Neighbor Ch. 1.cbz",
        "Arrest Thy Neighbor Ch. 2.cbz",
    ]

    # Both issues' actual page content survived intact — no silent data loss.
    assert _payload_size(series_dir / "Arrest Thy Neighbor Ch. 1.cbz") == 200
    assert _payload_size(series_dir / "Arrest Thy Neighbor Ch. 2.cbz") == 400


def test_single_generic_release_with_no_sibling_gets_bare_series_name(tmp_path, monkeypatch):
    """A lone placeholder-named release (no second chapter ever arrives) should
    just be renamed to the bare series name, not stamped "Ch. 1" — nothing has
    corroborated it as part of a multi-chapter series."""
    watch_root = tmp_path / "incoming"
    dest_root = tmp_path / "library"
    watch_root.mkdir()
    dest_root.mkdir()

    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(watch_root))
    monkeypatch.setattr(watcher, "_routing_rules", [])
    monkeypatch.setattr(watcher, "_routing_default", str(dest_root))
    monkeypatch.setattr(watcher, "POLL_INTERVAL", 0)

    only_dir = watch_root / "Some Oneshot"
    _make_cbz(only_dir / "group_Chapter.cbz", 123)

    watcher.process_and_move_directory(only_dir)

    series_dir = dest_root / "Some Oneshot"
    assert series_dir.is_dir()
    names = sorted(p.name for p in series_dir.glob("*.cbz"))
    assert names == ["Some Oneshot.cbz"]
