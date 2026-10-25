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

import os
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


@pytest.mark.parametrize(
    "tag",
    [" (s2)", " (1)", " (Parody)", " (Zorblax)", " (Digital) (s2)"],
    ids=["season", "copy-suffix", "parody", "unknown", "noise-plus-season"],
)
def test_distinguishing_or_unclassified_tag_arrival_is_held(env, caplog, tag):
    """Option (b) holds every tag that may name a different book: seasons,
    copy suffixes, fan-work credits and, failing safe, any tag nobody has
    classified. A noise tag beside one of those does not release it."""
    incoming, library, _quarantine, _mode = env
    existing = _make_cbz(library / "Solo Leveling" / "Solo Leveling ch. 4.cbz", SMALL)
    existing_bytes = existing.read_bytes()
    arrival = incoming / "Solo Leveling"
    _make_cbz(arrival / f"Solo Leveling ch. 4{tag}.cbz", BIG, b"\1")

    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(arrival)

    held = arrival / f"Solo Leveling ch. 4{tag}.cbz"
    _assert_held(env, caplog, existing, existing_bytes, held, b"\1" * BIG)


# --- controls: behaviour that must not change ------------------------------


@pytest.mark.parametrize("tag", ["(Digital)", "(ManhwaClub)"])
def test_noise_tag_higher_res_arrival_replaces(env, caplog, tag):
    """Option (b): an approved group or source tag names the same book, so a
    larger copy with different bytes replaces the existing one exactly as an
    untagged arrival does, instead of waiting for review."""
    incoming, library, quarantine, mode = env
    existing = _make_cbz(library / "Solo Leveling" / "Solo Leveling ch. 4.cbz", SMALL)
    arrival = incoming / "Solo Leveling"
    _make_cbz(arrival / f"Solo Leveling ch. 4 {tag}.cbz", BIG, b"\1")

    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(arrival)

    assert _payload(existing) == b"\1" * BIG
    assert _files(library) == [str(Path("Solo Leveling") / "Solo Leveling ch. 4.cbz")]
    assert "HELD FOR REVIEW" not in caplog.text
    assert not arrival.exists()
    if mode == "resolver":
        loser = quarantine / "Solo Leveling" / "Solo Leveling ch. 4.cbz"
        assert _payload(loser) == b"\0" * SMALL
    else:
        assert _files(quarantine) == []


@pytest.mark.parametrize("tag", ["", " (Digital)"], ids=["untagged", "digital"])
def test_smaller_noise_tag_arrival_loses_like_an_untagged_one(env, caplog, tag):
    """The other half of keep-the-larger: a smaller ``(Digital)`` arrival is
    the loser, settled exactly as the untagged control beside it. The
    existing archive is kept; the incoming one is removed in legacy mode and
    quarantined in resolver mode. Nothing is held."""
    incoming, library, quarantine, mode = env
    existing = _make_cbz(library / "Solo Leveling" / "Solo Leveling ch. 4.cbz", BIG)
    existing_bytes = existing.read_bytes()
    arrival = incoming / "Solo Leveling"
    _make_cbz(arrival / f"Solo Leveling ch. 4{tag}.cbz", 50, b"\1")

    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(arrival)

    assert existing.read_bytes() == existing_bytes
    assert _files(library) == [str(Path("Solo Leveling") / "Solo Leveling ch. 4.cbz")]
    assert "HELD FOR REVIEW" not in caplog.text
    assert not arrival.exists()
    quarantined = [p for p in quarantine.rglob("*.cbz") if p.is_file()]
    if mode == "resolver":
        assert [_payload(p) for p in quarantined] == [b"\1" * 50]
    else:
        assert quarantined == []


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
        ("Berserk v01 (Digital) (2019)", ()),
        ("Solo Leveling ch. 4 (ManhwaClub)", ()),
        ("Solo Leveling ch. 4 (Incognitymous) (Ongoing)", ()),
        ("Solo Leveling ch. 4 (digital)", ()),
        ("Solo Leveling ch. 4 (Asura Scans)", ()),
        ("Solo Leveling ch. 4 (Digital) (s2)", ("(s2)",)),
        ("Solo Leveling ch. 4 (Season 2)", ("(Season 2)",)),
        ("Title (Parody)", ("(Parody)",)),
        ("Title (Variant) (Alt Cover)", ("(Variant)", "(Alt Cover)")),
        ("Title (Zorblax)", ("(Zorblax)",)),
        ("Title (Digital-Empire)", ("(Digital-Empire)",)),
        ("Title (Scans of Old)", ("(Scans of Old)",)),
        ("Solo Leveling ch. 4 (English)", ()),
        ("Solo Leveling ch. 4 (2019) (Colored) (Uncensored) [v2]", ()),
        ("[Circle (Artist)] Title", ()),
        ("[Judas] Berserk v01", ()),
        ("Title ()", ()),
    ],
)
def test_dropped_round_tags(name, tags):
    """Pinned: which stripped blocks count. Display tokens, language words
    (hive #2646), square-bracket blocks and approved group, source, status
    and scanlator noise (option (b), 2026-10-25) are not distinguishing
    tags. A noise tag beside a listed one leaves the listed one, and a tag
    that is only close to noise (``(Digital-Empire)``, a scan word that is
    not the last word) is unclassified, so it counts."""
    assert cbz_core.dropped_round_tags(name) == tags


# --- a hold survives failures, retries and restarts (PR #132 review) ---------
#
# Codex's review found that the hold lived in one rename: the arrival was
# renamed to its stripped name on import and renamed back when held, so a
# failed rename back left the next pass nothing to hold on. A rename that
# strips a distinguishing tag is now deferred until the archive lands, so
# the tag stays in the file name for as long as the archive is in the import
# folder. These drive the real entry point through failures, a retry, a
# restart and a crash, and check that both books survive each.

S2_ARRIVAL = "Solo Leveling ch. 4 (s2).cbz"


class _Crash(BaseException):
    """A crash: not an ``Exception``, so no handler in the watcher stops it."""


def _seed_collision(env):
    incoming, library, _quarantine, _mode = env
    existing = _make_cbz(library / "Solo Leveling" / "Solo Leveling ch. 4.cbz", SMALL)
    arrival = incoming / "Solo Leveling"
    _make_cbz(arrival / S2_ARRIVAL, BIG, b"\1")
    return existing, existing.read_bytes(), arrival


def _break_tag_restoring_renames(monkeypatch, folder: Path) -> list[tuple[str, str]]:
    """Codex's injection: every rename in *folder* that would put ``(s2)``
    back on an archive's name raises ``OSError``; the rest go through. Every
    rename in *folder* is recorded, failed or not, as (from, to) names."""
    renames: list[tuple[str, str]] = []
    real_rename = Path.rename

    def rename(path, target):
        target = Path(target)
        if path.parent == folder:
            renames.append((path.name, target.name))
            if "(s2)" in target.name and "(s2)" not in path.name:
                raise OSError("injected: restoring the arrival name failed")
        return real_rename(path, target)

    monkeypatch.setattr(Path, "rename", rename)
    return renames


def _restart(env, monkeypatch) -> None:
    """What a watcher restart resets: the series locks, the in-progress set
    and, in resolver mode, the resolver, rebuilt from the library on disk."""
    _incoming, library, quarantine, mode = env
    monkeypatch.setattr(watcher, "_SERIES_LOCKS", type(watcher._SERIES_LOCKS)())
    monkeypatch.setattr(watcher, "_processing_dirs", set())
    if mode == "resolver":
        monkeypatch.setattr(watcher, "_series_resolvers", {
            "manga": SeriesResolver(library, quarantine_root=quarantine)})


@pytest.mark.parametrize("restart", [False, True], ids=["retry", "restart"])
def test_a_failed_rename_back_cannot_release_the_hold(env, monkeypatch, caplog, restart):
    """Codex's P1 probe. The first pass runs with every tag-restoring rename
    in the import folder failing; the second is a normal pass, in the same
    process (a settle-pass retry) or after a restart. The existing book's
    bytes never change, and the incoming book is still in the import folder,
    byte for byte, under the name it arrived with. No rename in the import
    folder ever took the tag off it, so there was nothing to restore."""
    existing, existing_bytes, arrival = _seed_collision(env)
    real_rename = Path.rename
    renames = _break_tag_restoring_renames(monkeypatch, arrival)

    watcher.process_and_move_directory(arrival)
    held = arrival / S2_ARRIVAL
    assert existing.read_bytes() == existing_bytes
    assert _payload(held) == b"\1" * BIG

    monkeypatch.setattr(Path, "rename", real_rename)
    if restart:
        _restart(env, monkeypatch)
    caplog.clear()
    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(arrival)

    _assert_held(env, caplog, existing, existing_bytes, held, b"\1" * BIG)
    assert [r for r in renames if "(s2)" in r[0] and "(s2)" not in r[1]] == []


@pytest.mark.parametrize("crash_in", ["_resolve_series_dir_name", "_move_cbz_dir"])
def test_a_crash_after_processing_leaves_the_tag_for_the_restart(
    env, monkeypatch, caplog, crash_in
):
    """A crash after the archive was processed (renamed, ComicInfo written)
    but before or at the move. Before the fix the archive already carried
    its stripped name here, so the restart met a plain same-name conflict
    and replaced the existing book. Now the tag is still on disk."""
    existing, existing_bytes, arrival = _seed_collision(env)
    real = getattr(watcher, crash_in)

    def crash(*args, **kwargs):
        raise _Crash(crash_in)

    monkeypatch.setattr(watcher, crash_in, crash)
    with pytest.raises(_Crash):
        watcher.process_and_move_directory(arrival)
    held = arrival / S2_ARRIVAL
    assert held.is_file()
    assert existing.read_bytes() == existing_bytes

    monkeypatch.setattr(watcher, crash_in, real)
    _restart(env, monkeypatch)
    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(arrival)

    _assert_held(env, caplog, existing, existing_bytes, held, b"\1" * BIG)


@pytest.mark.parametrize("error", [OSError, PermissionError])
def test_a_failed_comparison_holds_and_the_next_pass_holds_again(
    env, monkeypatch, caplog, error
):
    """Same-size archives are told apart by digest. If reading either one
    fails, nothing shows the two are the same book, so the arrival is held,
    not merged and not left for a pass that would no longer see its tag."""
    incoming, library, _quarantine, _mode = env
    monkeypatch.setattr(watcher, "process_comicinfo", lambda *a, **k: None)
    existing = _make_cbz(library / "Solo Leveling" / "Solo Leveling ch. 4.cbz", SMALL)
    existing_bytes = existing.read_bytes()
    arrival = incoming / "Solo Leveling"
    _make_cbz(arrival / "Solo Leveling ch. 4 (1).cbz", SMALL, b"\1")
    real_digest = watcher.digest

    def failing_digest(path):
        raise error("injected: digest read failed")

    monkeypatch.setattr(watcher, "digest", failing_digest)
    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(arrival)
    held = arrival / "Solo Leveling ch. 4 (1).cbz"
    _assert_held(env, caplog, existing, existing_bytes, held, b"\1" * SMALL)
    assert "Could not compare" in caplog.text
    assert "Failed to move directory" not in caplog.text

    monkeypatch.setattr(watcher, "digest", real_digest)
    caplog.clear()
    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(arrival)
    _assert_held(env, caplog, existing, existing_bytes, held, b"\1" * SMALL)


def test_a_failed_sibling_move_keeps_the_held_tag_for_the_retry(env, monkeypatch, caplog):
    """An ``OSError`` during the merge itself (a sibling that cannot be
    moved) stops the directory part-way. The held archive was never renamed,
    so the retry holds it again and lands the sibling."""
    existing, existing_bytes, arrival = _seed_collision(env)
    _make_cbz(arrival / "Solo Leveling ch. 5 (s2).cbz", 80, b"\3")
    real_move = watcher.shutil.move
    failures = []

    def failing_move(src, dest, *args, **kwargs):
        if Path(src).name == "Solo Leveling ch. 5 (s2).cbz" and not failures:
            failures.append(src)
            raise PermissionError("injected: sibling locked")
        return real_move(src, dest, *args, **kwargs)

    monkeypatch.setattr(watcher.shutil, "move", failing_move)
    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(arrival)
    assert failures
    assert "Failed to move directory" in caplog.text
    assert existing.read_bytes() == existing_bytes
    assert (arrival / S2_ARRIVAL).is_file()

    caplog.clear()
    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(arrival)
    landed = existing.parent / "Solo Leveling ch. 5.cbz"
    assert _payload(landed) == b"\3" * 80
    assert existing.read_bytes() == existing_bytes
    assert _payload(arrival / S2_ARRIVAL) == b"\1" * BIG
    assert "HELD FOR REVIEW" in caplog.text


def test_a_placeholder_fallback_name_that_strips_a_tag_is_deferred_too(env, caplog):
    """``_apply_fallback_naming`` renames a placeholder after processing. A
    fallback name that drops a distinguishing tag is deferred like any
    other, so a held placeholder also keeps its arrival name on disk."""
    incoming, library, _quarantine, _mode = env
    existing = _make_cbz(library / "Solo Leveling" / "Solo Leveling ch. 4.cbz", SMALL)
    existing_bytes = existing.read_bytes()
    arrival = incoming / "Solo Leveling Ch. 4"
    _make_cbz(arrival / "volvox_Chapter (s2).cbz", BIG, b"\1")

    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(arrival)

    held = arrival / "volvox_Chapter (s2).cbz"
    _assert_held(env, caplog, existing, existing_bytes, held, b"\1" * BIG)
    assert "Rename deferred to landing (placeholder fallback)" in caplog.text


def test_display_names_on_a_whole_directory_move_never_overwrite(env, monkeypatch, caplog):
    """A new series folder is moved whole, archives under their arrival
    names, and renamed in the library. Two arrivals that strip to one name
    both land: the first gets the display name, the second keeps its tag.
    A rename that fails there keeps the arrival name; no book is lost.
    Renames in the library follow POSIX here, replacing an existing target,
    so it is the watcher's own check that keeps the first book, not the
    Windows refusal to rename onto an existing file."""
    incoming, library, _quarantine, _mode = env
    arrival = incoming / "Solo Leveling"
    _make_cbz(arrival / "Solo Leveling ch. 4 (s2).cbz", BIG, b"\1")
    _make_cbz(arrival / "Solo Leveling ch. 4 (s3).cbz", 90, b"\2")
    _make_cbz(arrival / "Solo Leveling ch. 6 (s2).cbz", 70, b"\4")
    series = library / "Solo Leveling"
    real_rename = Path.rename

    def rename(path, target):
        target = Path(target)
        if path.parent != series:
            return real_rename(path, target)
        if target.name == "Solo Leveling ch. 6.cbz":
            raise PermissionError("injected: landing rename failed")
        os.replace(path, target)
        return target

    monkeypatch.setattr(Path, "rename", rename)
    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(arrival)

    payloads = {p.name: _payload(p) for p in series.glob("*.cbz")}
    assert payloads == {
        "Solo Leveling ch. 4.cbz": b"\1" * BIG,
        "Solo Leveling ch. 4 (s3).cbz": b"\2" * 90,
        "Solo Leveling ch. 6 (s2).cbz": b"\4" * 70,
    }
    assert not arrival.exists()
    assert "Kept arrival name" in caplog.text


def test_a_held_archive_keeps_its_ai_original_while_a_routed_sibling_offloads(
    env, monkeypatch, tmp_path
):
    """Codex's P2 probe, with a sibling. AI decensor runs (a fixture stub:
    the original is copied to a tmp folder; no Camelia process) and the
    offload is a recorder (no backup drive). The held archive's original
    must stay local for the retry; the routed sibling's is offloaded. The
    originals are told apart by their bytes, not by any file name."""
    existing, existing_bytes, arrival = _seed_collision(env)
    _make_cbz(arrival / "Solo Leveling ch. 5 (s2).cbz", 80, b"\3")
    originals = tmp_path / "originals"
    originals.mkdir()
    backups: list[Path] = []

    def fake_decensor(path, destination):
        backup = originals / f"{len(backups)}.cbz"
        backup.write_bytes(path.read_bytes())
        backups.append(backup)
        return backup

    offloaded: list[Path] = []
    monkeypatch.setattr(watcher, "AI_DECENSOR_ENABLED", True)
    monkeypatch.setattr(watcher, "destination_is_comix", lambda _dest: True)
    monkeypatch.setattr(watcher, "run_ai_decensor", fake_decensor)
    monkeypatch.setattr(
        watcher, "archive_backup",
        lambda backup, root, series: offloaded.append(backup) or backup,
    )

    watcher.process_and_move_directory(arrival)

    held_original, = [b for b in backups if _payload(b) == b"\1" * BIG]
    routed_original, = [b for b in backups if _payload(b) == b"\3" * 80]
    assert existing.read_bytes() == existing_bytes
    assert _payload(arrival / S2_ARRIVAL) == b"\1" * BIG
    assert _payload(existing.parent / "Solo Leveling ch. 5.cbz") == b"\3" * 80
    assert offloaded == [routed_original]
    assert held_original.is_file()
