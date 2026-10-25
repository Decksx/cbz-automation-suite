"""A stripped tag must not let one book replace a different one (issue #131).

The watcher's display name drops round-bracket tags such as ``(s2)``,
``(1)`` and ``(Inuyasha)``. The merge then took a name collision as "same
book, keep the larger", so ``Solo Leveling ch. 4 (s2)`` arriving beside
``Solo Leveling ch. 4`` replaced it: legacy mode deleted the season-1
archive outright, resolver mode quarantined it, and the library kept the
season-2 bytes under the season-1 name.

These run the whole ``process_and_move_directory`` path on tmp archives, in
legacy mode and with the canonical resolver. The guarded case is held in the
import folder under its arrival name with the existing archive untouched.
The controls pin what must not change: an ordinary same-name arrival still
replaces a smaller copy, an exact duplicate is still removed, and a stripped
arrival with nothing to collide with still lands.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest

from comic_automation.series_resolver import SeriesResolver
from scripts import cbz_core
from scripts import cbz_watcher as watcher

# More than REPLACEMENT_MIN_GAIN_BYTES over the 100-byte existing page, so
# without the guard keep-the-larger would replace the existing archive.
BIG = 12_240
SMALL = 100


def _make_cbz(path: Path, payload_size: int, fill: bytes = b"\0") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("page1.jpg", fill * payload_size)
    return path


def _payload(cbz_path: Path) -> bytes:
    with zipfile.ZipFile(cbz_path) as zf:
        return zf.read("page1.jpg")


@pytest.fixture(params=["legacy", "resolver"])
def env(request, tmp_path, monkeypatch):
    """A tmp import folder, library and quarantine wired into the watcher."""
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
    return incoming, library, quarantine, request.param


def _files(root: Path) -> list[str]:
    """Every archive under *root*. The resolver keeps its own registry JSON
    in the library root, which is not an archive and is ignored here."""
    return sorted(
        str(p.relative_to(root)) for p in root.rglob("*.cbz") if p.is_file()
    )


def _assert_held(env, caplog, existing: Path, existing_bytes: bytes,
                 held: Path, held_payload: bytes) -> None:
    """The guarded outcome: nothing in the library or quarantine changed,
    and the arrival is back under the name it came with, in the import folder."""
    _incoming, library, quarantine, _mode = env
    assert existing.read_bytes() == existing_bytes
    assert _files(library) == [str(existing.relative_to(library))]
    assert _files(quarantine) == []
    assert held.is_file()
    assert _payload(held) == held_payload
    assert "HELD FOR REVIEW" in caplog.text
    assert held.name in caplog.text


# --- the guarded case ------------------------------------------------------


@pytest.mark.parametrize("incoming_size", [BIG, 50], ids=["larger", "smaller"])
def test_season_tag_arrival_is_held_and_the_existing_book_is_untouched(
    env, caplog, incoming_size
):
    """The issue's reproducer. Larger, legacy mode used to unlink the existing
    archive and resolver mode to quarantine it; smaller, the incoming one was
    the one lost. Either way, nothing in the library may change now."""
    incoming, library, _quarantine, _mode = env
    existing = _make_cbz(library / "Solo Leveling" / "Solo Leveling ch. 4.cbz", SMALL)
    existing_bytes = existing.read_bytes()
    arrival = incoming / "Solo Leveling"
    _make_cbz(arrival / "Solo Leveling ch. 4 (s2).cbz", incoming_size, b"\1")

    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(arrival)

    held = arrival / "Solo Leveling ch. 4 (s2).cbz"
    _assert_held(env, caplog, existing, existing_bytes, held, b"\1" * incoming_size)
    assert not (arrival / "Solo Leveling ch. 4.cbz").exists()
    # The hold is a clean skip, not a merge that failed part-way.
    assert "Failed to move directory" not in caplog.text


def test_a_hold_does_not_stop_the_rest_of_the_arrival_from_landing(env, caplog):
    """Only the colliding archive is held; its siblings merge as usual, and
    the merge finishes without error."""
    incoming, library, _quarantine, _mode = env
    existing = _make_cbz(library / "Solo Leveling" / "Solo Leveling ch. 4.cbz", SMALL)
    existing_bytes = existing.read_bytes()
    arrival = incoming / "Solo Leveling"
    _make_cbz(arrival / "Solo Leveling ch. 4 (s2).cbz", BIG, b"\1")
    _make_cbz(arrival / "Solo Leveling ch. 5 (s2).cbz", 80, b"\3")

    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(arrival)

    assert existing.read_bytes() == existing_bytes
    assert _payload(library / "Solo Leveling" / "Solo Leveling ch. 5.cbz") == b"\3" * 80
    assert sorted(p.name for p in arrival.iterdir()) == ["Solo Leveling ch. 4 (s2).cbz"]
    assert "Merge complete" in caplog.text
    assert "Failed to move directory" not in caplog.text


def test_a_held_arrival_is_held_again_on_the_next_pass(env, caplog):
    """The retry (next settle pass or restart) sees the arrival name again,
    so it must hold again rather than meet a now-unchanged stripped name."""
    incoming, library, _quarantine, _mode = env
    existing = _make_cbz(library / "Solo Leveling" / "Solo Leveling ch. 4.cbz", SMALL)
    existing_bytes = existing.read_bytes()
    arrival = incoming / "Solo Leveling"
    _make_cbz(arrival / "Solo Leveling ch. 4 (s2).cbz", BIG, b"\1")

    watcher.process_and_move_directory(arrival)
    caplog.clear()
    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(arrival)

    held = arrival / "Solo Leveling ch. 4 (s2).cbz"
    _assert_held(env, caplog, existing, existing_bytes, held, b"\1" * BIG)


def test_copy_suffix_arrival_with_different_bytes_is_held(env, monkeypatch, caplog):
    """``(1)`` is usually a true duplicate, but not always: with different
    bytes it is held like any other stripped tag. The archive is the same
    size as the existing one, so only the digest tells the two apart: equal
    size alone must not count as a duplicate. The ComicInfo step is disabled
    so the import does not grow the incoming archive and break the tie."""
    incoming, library, _quarantine, _mode = env
    monkeypatch.setattr(watcher, "process_comicinfo", lambda *a, **k: None)
    existing = _make_cbz(library / "Solo Leveling" / "Solo Leveling ch. 4.cbz", SMALL)
    existing_bytes = existing.read_bytes()
    arrival = incoming / "Solo Leveling"
    _make_cbz(arrival / "Solo Leveling ch. 4 (1).cbz", SMALL, b"\1")

    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(arrival)

    held = arrival / "Solo Leveling ch. 4 (1).cbz"
    _assert_held(env, caplog, existing, existing_bytes, held, b"\1" * SMALL)


def test_season_tag_arrival_is_held_when_the_series_appears_mid_move(
    env, monkeypatch, caplog
):
    """``_move_cbz_dir`` falls back to a second merge call when the series
    folder appears between its existence check and the move (a concurrent
    import). That call must hold the stripped arrival too."""
    incoming, library, _quarantine, _mode = env
    arrival = incoming / "Solo Leveling"
    _make_cbz(arrival / "Solo Leveling ch. 4 (s2).cbz", BIG, b"\1")
    existing = library / "Solo Leveling" / "Solo Leveling ch. 4.cbz"
    original_move = watcher.shutil.move

    def series_appears_then_move_fails(src, dest, *args, **kwargs):
        if Path(src) == arrival and Path(dest) == library / "Solo Leveling":
            _make_cbz(existing, SMALL)
            raise OSError("simulated: destination appeared concurrently")
        return original_move(src, dest, *args, **kwargs)

    monkeypatch.setattr(watcher.shutil, "move", series_appears_then_move_fails)
    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(arrival)

    held = arrival / "Solo Leveling ch. 4 (s2).cbz"
    assert "Race on move" in caplog.text
    assert _payload(existing) == b"\0" * SMALL
    _assert_held(env, caplog, existing, existing.read_bytes(), held, b"\1" * BIG)


def test_parody_tag_arrival_is_held(env, caplog):
    """Two doujins with one title and different source works collide once
    the parody tag is stripped."""
    incoming, library, _quarantine, _mode = env
    existing = _make_cbz(library / "Kagome Diary" / "Kagome Diary.cbz", SMALL)
    existing_bytes = existing.read_bytes()
    arrival = incoming / "Kagome Diary"
    _make_cbz(arrival / "Kagome Diary (Inuyasha).cbz", BIG, b"\1")

    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(arrival)

    held = arrival / "Kagome Diary (Inuyasha).cbz"
    _assert_held(env, caplog, existing, existing_bytes, held, b"\1" * BIG)


def test_loose_file_in_a_mixed_drop_point_is_held(env, caplog):
    """``_move_loose_files`` carries its own copy of keep-the-larger; the
    hold must apply there too. The loose archive goes to a series folder
    named after the drop point."""
    incoming, library, _quarantine, _mode = env
    existing = _make_cbz(library / "Drop" / "Drop ch. 4.cbz", SMALL)
    existing_bytes = existing.read_bytes()
    drop = incoming / "Drop"
    _make_cbz(drop / "Drop ch. 4 (s2).cbz", BIG, b"\1")
    _make_cbz(drop / "Other Series" / "Other Series ch. 1.cbz", 60)

    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(drop)

    assert existing.read_bytes() == existing_bytes
    held = drop / "Drop ch. 4 (s2).cbz"
    assert _payload(held) == b"\1" * BIG
    assert "HELD FOR REVIEW" in caplog.text
    # The sibling series is unaffected by the hold.
    assert (library / "Other Series" / "Other Series ch. 1.cbz").is_file()
    assert sorted(p.name for p in (library / "Drop").iterdir()) == ["Drop ch. 4.cbz"]


# --- controls: behaviour that must not change ------------------------------


def test_ordinary_same_name_higher_res_arrival_still_replaces(env, caplog):
    """The case keep-the-larger is for: the same name, a bigger scan."""
    incoming, library, quarantine, mode = env
    existing = _make_cbz(library / "Solo Leveling" / "Solo Leveling ch. 4.cbz", SMALL)
    arrival = incoming / "Solo Leveling"
    _make_cbz(arrival / "Solo Leveling ch. 4.cbz", BIG, b"\1")

    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(arrival)

    assert _payload(existing) == b"\1" * BIG
    assert "HELD FOR REVIEW" not in caplog.text
    assert not arrival.exists()
    if mode == "resolver":
        loser = quarantine / "Solo Leveling" / "Solo Leveling ch. 4.cbz"
        assert _payload(loser) == b"\0" * SMALL
    else:
        assert _files(quarantine) == []


def test_normalised_name_without_a_stripped_tag_still_replaces(env, caplog):
    """A rename that only normalises (``ch. 4`` in folder ``Solo Leveling``
    becomes ``Solo Leveling Ch. 4``) changes the collision key without any
    tag being stripped. That is a same-book upgrade, not a hold."""
    incoming, library, _quarantine, _mode = env
    existing = _make_cbz(library / "Solo Leveling" / "Solo Leveling ch. 4.cbz", SMALL)
    arrival = incoming / "Solo Leveling"
    _make_cbz(arrival / "ch. 4.cbz", BIG, b"\1")

    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(arrival)

    assert _payload(existing) == b"\1" * BIG
    assert "HELD FOR REVIEW" not in caplog.text
    assert not arrival.exists()


def test_tagged_name_kept_as_is_still_replaces_its_same_named_copy(env, caplog):
    """A tag that was not stripped is no reason to hold. Here the rename of
    ``ch. 4 (s2)`` is skipped (``ch. 4`` already sits in the arrival
    folder), so the archive keeps its tagged name and meets the library's
    own ``ch. 4 (s2)``: an ordinary same-name arrival, settled by size."""
    incoming, library, _quarantine, _mode = env
    existing = _make_cbz(
        library / "Solo Leveling" / "Solo Leveling ch. 4 (s2).cbz", SMALL
    )
    arrival = incoming / "Solo Leveling"
    _make_cbz(arrival / "Solo Leveling ch. 4.cbz", 70, b"\2")
    _make_cbz(arrival / "Solo Leveling ch. 4 (s2).cbz", BIG, b"\1")

    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(arrival)

    assert _payload(existing) == b"\1" * BIG
    assert "HELD FOR REVIEW" not in caplog.text


def test_square_bracket_group_tag_arrival_still_replaces(env, caplog):
    """Scanlator tags in square brackets were stripped long before design
    §5.5, and the keep-the-larger upgrade has been relied on for them; they
    are deliberately outside the hold (``dropped_round_tags``)."""
    incoming, library, _quarantine, _mode = env
    existing = _make_cbz(library / "Solo Leveling" / "Solo Leveling ch. 4.cbz", SMALL)
    arrival = incoming / "Solo Leveling"
    _make_cbz(arrival / "[Judas] Solo Leveling ch. 4.cbz", BIG, b"\1")

    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(arrival)

    assert _payload(existing) == b"\1" * BIG
    assert "HELD FOR REVIEW" not in caplog.text


def test_stripped_tag_exact_duplicate_is_still_removed(env, monkeypatch, caplog):
    """Same bytes are the same book whatever the name: the duplicate is
    removed from the import folder as before, and nothing is held.

    The import rewrites ComicInfo, stamping the zip entry with the current
    time, so a re-arrival is byte-identical only when that rewrite lands in
    the same second as the first one did. To make the duplicate exact on
    every run the ComicInfo step is disabled here; the merge, which is what
    this exercises, still sees the real file sizes and digests."""
    incoming, library, quarantine, _mode = env
    monkeypatch.setattr(watcher, "process_comicinfo", lambda *a, **k: None)
    existing = _make_cbz(library / "Solo Leveling" / "Solo Leveling ch. 4.cbz", SMALL)
    existing_bytes = existing.read_bytes()
    arrival = incoming / "Solo Leveling"
    arrival.mkdir()
    shutil.copy2(existing, arrival / "Solo Leveling ch. 4 (s2).cbz")

    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(arrival)

    assert existing.read_bytes() == existing_bytes
    assert _files(library) == [str(Path("Solo Leveling") / "Solo Leveling ch. 4.cbz")]
    assert _files(quarantine) == []
    assert not arrival.exists()
    assert "HELD FOR REVIEW" not in caplog.text


@pytest.mark.parametrize("series_exists", [True, False], ids=["merge", "new-series"])
def test_stripped_tag_arrival_with_no_collision_still_lands(env, caplog, series_exists):
    """Stripping alone is not a reason to hold: with no archive to collide
    with, the arrival lands under its display name."""
    incoming, library, _quarantine, _mode = env
    if series_exists:
        _make_cbz(library / "Solo Leveling" / "Solo Leveling ch. 3.cbz", SMALL)
    arrival = incoming / "Solo Leveling"
    _make_cbz(arrival / "Solo Leveling ch. 4 (s2).cbz", BIG, b"\1")

    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(arrival)

    landed = library / "Solo Leveling" / "Solo Leveling ch. 4.cbz"
    assert _payload(landed) == b"\1" * BIG
    assert not arrival.exists()
    assert "HELD FOR REVIEW" not in caplog.text


# --- the trigger --------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "tags"),
    [
        ("Solo Leveling ch. 4 (s2)", ("(s2)",)),
        ("Solo Leveling ch. 4 (1)", ("(1)",)),
        ("Kagome Diary (Inuyasha)", ("(Inuyasha)",)),
        ("Berserk v01 (Digital) (2019)", ("(Digital)",)),
        ("Solo Leveling ch. 4 (English)", ()),
        ("Solo Leveling ch. 4 (2019) (Colored) (Uncensored) [v2]", ()),
        ("[Circle (Artist)] Title", ()),
        ("[Judas] Berserk v01", ()),
        ("Title ()", ()),
    ],
)
def test_dropped_round_tags(name, tags):
    """Pinned: which stripped blocks count. Display tokens, language words
    (hive #2646) and square-bracket blocks are not distinguishing tags."""
    assert cbz_core.dropped_round_tags(name) == tags
