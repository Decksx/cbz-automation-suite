"""Regression test for a second incident found while validating the
placeholder-chapter fix: a source "drop point" directory (e.g. "HentaiNexus")
had a few loose .cbz files sitting directly in it *alongside* dozens of
per-title subdirectories, each themselves holding .cbz files.

Because the watcher's directory-grouping treats every distinct parent of a
.cbz file as its own "comic directory", the drop-point directory itself
became a comic-directory entry too. Processing that entry called
shutil.move() on the whole drop-point directory — sweeping every one of the
unrelated per-title subdirectories along with it into a single lump
destination folder — and every subsequent loop iteration for those
subdirectories then failed with "already moved by another thread" since their
paths no longer existed.

The fix (see cbz_watcher._move_loose_files and the has_nested_comic_dirs
check in _process_and_move_directory_inner) detects this "mixed" case and
moves only the loose files individually, leaving sibling subdirectories
alone so each is processed — and lands in its own destination folder — on
its own turn through the loop.
"""

import zipfile
from pathlib import Path

from scripts import cbz_watcher as watcher


def _make_cbz(path: Path, payload_size: int = 50) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("page1.jpg", b"\0" * payload_size)


def test_loose_files_dont_sweep_up_sibling_series_directories(tmp_path, monkeypatch):
    watch_root = tmp_path / "incoming"
    dest_root = tmp_path / "library"
    watch_root.mkdir()
    dest_root.mkdir()

    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(watch_root))
    monkeypatch.setattr(watcher, "_routing_rules", [])
    monkeypatch.setattr(watcher, "_routing_default", str(dest_root))
    monkeypatch.setattr(watcher, "POLL_INTERVAL", 0)

    drop_point = watch_root / "HentaiNexus"

    # Loose files sitting directly in the drop-point directory.
    _make_cbz(drop_point / "Watching Over the Poor Bitch.cbz")
    _make_cbz(drop_point / "Yoridori Bitch.cbz")

    # Unrelated per-title subdirectories, each a distinct series.
    _make_cbz(drop_point / "Adult Switch" / "Adult Switch.cbz")
    _make_cbz(drop_point / "Bitch on the Beach" / "Bitch on the Beach.cbz")
    _make_cbz(drop_point / "Rock Bottom Bitch" / "Ch01.cbz")
    _make_cbz(drop_point / "Rock Bottom Bitch" / "Ch02.cbz")

    watcher.process_and_move_directory(drop_point)

    # Each per-title subdirectory must land in its own destination folder —
    # not be swallowed into a single "HentaiNexus" folder.
    assert (dest_root / "Adult Switch" / "Adult Switch.cbz").exists()
    assert (dest_root / "Bitch on the Beach" / "Bitch on the Beach.cbz").exists()
    assert len(list((dest_root / "Rock Bottom Bitch").glob("*.cbz"))) == 2

    # The loose files (no dedicated subdirectory of their own) land under the
    # drop-point name as a best-effort fallback, but nothing else does.
    loose_dest = dest_root / "HentaiNexus"
    assert loose_dest.is_dir()
    assert len(list(loose_dest.glob("*.cbz"))) == 2
    assert not (loose_dest / "Adult Switch").exists()
    assert not (loose_dest / "Bitch on the Beach").exists()
    assert not (loose_dest / "Rock Bottom Bitch").exists()

    # Nothing was silently lost: total cbz count in the library matches what
    # went in.
    total_in_library = sum(1 for _ in dest_root.rglob("*.cbz"))
    assert total_in_library == 6
