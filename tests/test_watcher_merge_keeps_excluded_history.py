"""A watcher merge must not delete the excluded directories it skipped.

``_merge_directories`` skips Syncthing's ``.stversions`` / ``.stfolder`` (and
the rest of ``DEFAULT_EXCLUDED_DIRECTORIES``), so after a merge they are still
in the arrival folder. ``_move_cbz_dir`` used to ``shutil.rmtree`` that folder
in both merge branches -- the destination already existing, and the race where
it appears between the existence check and the move -- and the skipped history
was destroyed. ``test_merge_leaves_syncthing_history_untouched`` only called
the merge helper, so it never saw the cleanup that followed.

These run the whole ``process_and_move_directory`` path, with legacy routing
and with the canonical resolver. Everything lives under ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from comic_automation.series_resolver import SeriesResolver
from scripts import cbz_core
from scripts import cbz_watcher as watcher
from test_watcher_canonical_convergence import _make_cbz, _payload_size


@pytest.fixture(params=["legacy", "resolver"])
def env(request, tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    library = tmp_path / "Manga"
    quarantine = tmp_path / "quarantine"
    for path in (incoming, library, quarantine):
        path.mkdir()
    resolvers = {}
    if request.param == "resolver":
        resolvers = {"manga": SeriesResolver(library, quarantine_root=quarantine)}
    monkeypatch.setattr(watcher, "_series_resolvers", resolvers)
    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(incoming))
    monkeypatch.setattr(watcher, "_routing_rules", [])
    monkeypatch.setattr(watcher, "_routing_default", str(library))
    monkeypatch.setattr(watcher, "_routing_default_key", "manga")
    monkeypatch.setattr(watcher, "POLL_INTERVAL", 0)
    monkeypatch.setattr(watcher, "AI_DECENSOR_ENABLED", False)
    monkeypatch.setattr(watcher, "wait_for_file_stable", lambda *a, **k: True)
    monkeypatch.setattr(cbz_core, "translate_metadata_text", lambda text: (text, None))
    return incoming, library


def _arrange_merge(monkeypatch, source: Path, library: Path, race: bool) -> None:
    """Make the destination series folder exist, now or mid-move."""
    existing = library / "Berserk" / "Berserk Ch. 1.cbz"
    if not race:
        _make_cbz(existing, 100)
        return
    original_move = watcher.shutil.move

    def destination_appears_then_move_fails(src, dest, *args, **kwargs):
        if Path(src) == source and Path(dest) == library / "Berserk":
            _make_cbz(existing, 100)
            raise OSError("simulated: destination appeared concurrently")
        return original_move(src, dest, *args, **kwargs)

    monkeypatch.setattr(watcher.shutil, "move", destination_appears_then_move_fails)


@pytest.mark.parametrize("race", [False, True], ids=["merge", "race-to-merge"])
def test_merge_keeps_excluded_history_in_the_arrival_folder(
    env, monkeypatch, caplog, race
):
    incoming, library = env
    source = incoming / "Berserk"
    _make_cbz(source / "Berserk Ch. 2.cbz", 200)
    history = source / ".stversions" / "old.cbz"
    _make_cbz(history, 500)
    marker = source / ".stfolder"
    marker.mkdir()
    history_bytes = history.read_bytes()
    _arrange_merge(monkeypatch, source, library, race)

    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(source)

    # The comic content merged into the existing series folder ...
    assert _payload_size(library / "Berserk" / "Berserk Ch. 2.cbz") == 200
    assert (library / "Berserk" / "Berserk Ch. 1.cbz").is_file()
    assert not (source / "Berserk Ch. 2.cbz").exists()
    # ... and the excluded entries, with their parent, stayed where they were.
    assert history.is_file() and history.read_bytes() == history_bytes
    assert marker.is_dir()
    assert sorted(p.name for p in source.iterdir()) == [".stfolder", ".stversions"]
    # They were not carried into the library either.
    assert not (library / "Berserk" / ".stversions").exists()
    assert not (library / "Berserk" / ".stfolder").exists()
    assert "Left excluded directory in place" in caplog.text

    # A retry over the leftover folder (the next settle pass, or a restart)
    # must still treat the history as excluded: nothing imported or moved.
    library_before = sorted(str(p.relative_to(library)) for p in library.rglob("*"))
    watcher.process_and_move_directory(source)
    assert history.is_file() and history.read_bytes() == history_bytes
    assert marker.is_dir()
    assert sorted(p.name for p in source.iterdir()) == [".stfolder", ".stversions"]
    assert sorted(str(p.relative_to(library)) for p in library.rglob("*")) == library_before


@pytest.mark.parametrize("race", [False, True], ids=["merge", "race-to-merge"])
def test_merge_keeps_the_path_to_nested_history_and_drops_empty_dirs(
    env, monkeypatch, race
):
    incoming, library = env
    source = incoming / "Berserk"
    _make_cbz(source / "Berserk Ch. 2.cbz", 200)
    nested_history = source / "extras" / ".stversions" / "old.cbz"
    _make_cbz(nested_history, 500)
    (source / "empty" / "deeper").mkdir(parents=True)
    _arrange_merge(monkeypatch, source, library, race)

    watcher.process_and_move_directory(source)

    assert nested_history.is_file()
    assert not (source / "empty").exists()
    assert sorted(p.name for p in source.iterdir()) == ["extras"]
    assert sorted(p.name for p in (source / "extras").iterdir()) == [".stversions"]


@pytest.mark.parametrize("race", [False, True], ids=["merge", "race-to-merge"])
def test_merge_without_excluded_entries_still_removes_the_arrival_folder(
    env, monkeypatch, race
):
    incoming, library = env
    source = incoming / "Berserk"
    _make_cbz(source / "Berserk Ch. 2.cbz", 200)
    (source / "empty").mkdir()
    _arrange_merge(monkeypatch, source, library, race)

    watcher.process_and_move_directory(source)

    assert _payload_size(library / "Berserk" / "Berserk Ch. 2.cbz") == 200
    assert not source.exists()
