"""A known group's prefix gives way to the series folder, hash or no hash.

PR #178 drops a downloader's group prefix only with its trailing hex hash
(``Alpha_Episode 42_2a8cb9.cbz``): without it the prefix could be a real
series. Live on 2026-11-10 11:58, Comick (Unoriginal) delivered
``Mangakakalot Ch. 53.cbz`` into "City of Sins", and it kept that name. A
prefix that is exactly a known group or site (``config/known-groups.txt``)
is now dropped without the hash too, when the arrival's folder confirms it
(one series, every archive carrying that prefix); any other prefix keeps
today's name.
"""

from __future__ import annotations

import itertools
import zipfile
from pathlib import Path

import pytest

from scripts import cbz_core
from scripts import cbz_watcher as watcher
from scripts.cbz_core import parse_comic_name

_ROOT: Path
_FRESH = itertools.count()


@pytest.fixture(autouse=True)
def _arrival_root(tmp_path):
    global _ROOT
    _ROOT = tmp_path / "intake"
    reset = getattr(cbz_core, "known_groups_mixed_drop", lambda mixed: None)
    reset(False)
    yield
    reset(False)


@pytest.fixture(params=[False, True], ids=["plain", "display"])
def display(request):
    return request.param


def _arrive(folder: str, *arrivals: str) -> list[Path]:
    """*arrivals* in a fresh arrival folder named *folder*, and nothing else."""
    base = _ROOT / str(next(_FRESH)) / folder
    base.mkdir(parents=True)
    paths = []
    for arrival in arrivals:
        path = base / arrival
        path.write_bytes(b"")
        paths.append(path)
    return paths


def _name(folder: str, arrival: str, display: bool, *siblings: str) -> str:
    path, *_ = _arrive(folder, arrival, *siblings)
    return parse_comic_name(path, display_tokens=display).filename


@pytest.mark.parametrize("folder, arrival, expected", [
    ("City of Sins", "Mangakakalot Ch. 53.cbz", "City of Sins Ch. 53.cbz"),  # the live case
    ("City of Sins", "Mangakakalot Chapter 53.cbz", "City of Sins Ch. 53.cbz"),
    ("City of Sins", "Mangakakalot_Chapter 53.cbz", "City of Sins Ch. 53.cbz"),
    ("City of Sins", "mangakakalot ch. 53.5.cbz", "City of Sins Ch. 53.5.cbz"),
    ("Lookism", "Literary Curator Episode 7.cbz", "Lookism Ch. 7.cbz"),
    ("Lookism", "Literary-Curator Ch. 8.cbz", "Lookism Ch. 8.cbz"),
    ("Berserk", "LOST_VEYL Ch. 3 - Eclipse.cbz", "Berserk Ch. 3 Eclipse.cbz"),
    ("Berserk", "Lost Veyl Ch. 4.cbz", "Berserk Ch. 4.cbz"),
    ("Berserk", "WebToon Ep. 45 - Friend.cbz", "Berserk Ch. 45 Friend.cbz"),
    ("Berserk", "Setsu Scans Chapter 9.cbz", "Berserk Ch. 9.cbz"),
])
def test_a_known_group_without_a_hash_gives_way_to_the_folder_series(folder, arrival,
                                                                      expected, display):
    assert _name(folder, arrival, display) == expected


def test_a_prefix_equal_to_the_folder_series_gives_the_same_name(display):
    assert _name("City of Sins", "City of Sins Ch. 53.cbz", display) \
        == _name("City of Sins", "Mangakakalot Ch. 53.cbz", display) == "City of Sins Ch. 53.cbz"


@pytest.mark.parametrize("folder, arrival, expected", [
    ("City of Sins", "Unknownseries Ch. 3.cbz", "Unknownseries Ch. 3.cbz"),  # unknown, no hash
    ("City of Sins", "Mangakakalots Ch. 3.cbz", "Mangakakalots Ch. 3.cbz"),  # not exact
    ("City of Sins", "Alphabet Chapter 4.cbz", "Alphabet Chapter 4.cbz"),
    ("City of Sins", "Other9 Ch. 2.cbz", "Other9 Ch. 2.cbz"),  # unlisted, with a digit
    ("City of Sins", "Manhwa19 Chapter 1.cbz", "Manhwa19 Chapter 1.cbz"),  # not exact
    ("Berserk", "Part A Ch. 5.cbz", "Part A Ch. 5.cbz"),  # structural
    ("Berserk", "Mangakakalot.cbz", "Mangakakalot.cbz"),  # no chapter token
])
def test_other_prefixes_without_a_hash_keep_todays_name(folder, arrival, expected, display):
    """Pass before and after the change."""
    assert _name(folder, arrival, display) == expected


def test_the_known_groups_file_is_what_decides(tmp_path, monkeypatch):
    """The list lives in a version-controlled data file, re-read when it
    changes; without it nothing without a hash is dropped."""
    arrival = "Zorblax Crew Ch. 2.cbz"
    assert _name("Berserk", arrival, True) == arrival
    listed = tmp_path / "known-groups.txt"
    listed.write_text("# a comment\n\nZorblax Crew\n", encoding="utf-8")
    monkeypatch.setattr(cbz_core, "KNOWN_GROUPS_FILE", listed)
    assert _name("Berserk", arrival, True) == "Berserk Ch. 2.cbz"
    monkeypatch.setattr(cbz_core, "KNOWN_GROUPS_FILE", tmp_path / "missing.txt")
    assert _name("City of Sins", "Mangakakalot Ch. 53.cbz", True) == "Mangakakalot Ch. 53.cbz"


@pytest.mark.parametrize("prefix", [
    "Part A", "Part 2", "S2", "V3", "Ep4",
    # PR #179 review: the forms the list matching normalises to the same keys
    "S 2", "V 3", "P 2", "E.p.4", "\uff33\uff12", "\uff36\uff13",
])
def test_a_listed_structural_entry_never_strips(tmp_path, monkeypatch, prefix):
    """The structural checks win over the list in every form the matcher
    sees, in a folder that would otherwise confirm the prefix."""
    listed = tmp_path / "known-groups.txt"
    listed.write_text("Part A\nPart 2\nS2\nV3\nP2\nEp4\n", encoding="utf-8")
    monkeypatch.setattr(cbz_core, "KNOWN_GROUPS_FILE", listed)
    arrival = f"{prefix} Chapter 5.cbz"
    path, = _arrive("Berserk", arrival)
    assert cbz_core._without_group_prefix(arrival, path.parent, "Berserk") == arrival
    assert parse_comic_name(path, display_tokens=True).filename != "Berserk Ch. 5.cbz"


def test_the_shipped_list_only_holds_names_the_guards_let_through():
    """A line the structural checks reject (a part, season or volume marker,
    a structural or special word) would never take effect; none is shipped.
    A digit alone is fine: an exact listed match overrides the digit rule."""
    path = cbz_core.KNOWN_GROUPS_FILE
    assert path.parent.name == "config" and path.name == "known-groups.txt"
    lines = [line for line in path.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    assert {"Mangakakalot", "The Hours Between", "Manhwa18", "Manhwa18 2"} <= set(lines)
    assert "Official \u00b7 sugerrrrr 2.5" not in lines
    assert [line for line in lines if cbz_core._is_structural_prefix(line)] == []
    keys = [cbz_core._group_key(line) for line in lines]
    assert all(keys) and len(keys) == len(set(keys))


def test_an_undecodable_list_means_no_known_groups_and_warns_once(tmp_path, monkeypatch, caplog):
    """PR #179 review: a list that is not valid UTF-8 is unreadable, so no
    prefix without a hash is dropped; parsing goes on, and it is logged."""
    listed = tmp_path / "known-groups.txt"
    listed.write_bytes(b"Mangakakalot\n\xff\n")
    monkeypatch.setattr(cbz_core, "KNOWN_GROUPS_FILE", listed)
    monkeypatch.setattr(cbz_core, "_known_groups_cache", None)
    monkeypatch.setattr(cbz_core, "_known_groups_warned", set())
    with caplog.at_level("WARNING"):
        assert _name("City of Sins", "Mangakakalot Ch. 53.cbz", True) == "Mangakakalot Ch. 53.cbz"
        assert _name("City of Sins", "Mangakakalot Ch. 54.cbz", False) == "Mangakakalot Ch. 54.cbz"
    warnings = [r for r in caplog.records if "unreadable" in r.getMessage()]
    assert len(warnings) == 1


# PR #179 review: the list says a prefix *can* name a group; the arrival's
# folder must confirm it.
@pytest.mark.parametrize("title", ["Desire", "Violet"])
def test_a_listed_name_that_is_a_title_in_a_mixed_folder_keeps_its_name(title, display):
    assert _name("Mixed Drop", f"{title} Ch. 1.cbz", display, "Some Other Book Ch. 1.cbz") \
        == f"{title} Ch. 1.cbz"


@pytest.mark.parametrize("title", ["Desire", "Violet"])
def test_without_a_folder_to_confirm_it_the_name_is_kept(title, display):
    """Codex's probe: a path whose folder cannot be read confirms nothing."""
    parsed = parse_comic_name(Path("C:/intake/Mixed Drop") / f"{title} Ch. 1.cbz",
                              display_tokens=display)
    assert parsed.filename == f"{title} Ch. 1.cbz"


def test_a_folder_with_subfolders_confirms_nothing(display):
    path, = _arrive("City of Sins", "Mangakakalot Ch. 53.cbz")
    (path.parent / "Other Series").mkdir()
    assert parse_comic_name(path, display_tokens=display).filename == "Mangakakalot Ch. 53.cbz"


def test_a_mixed_drops_loose_archives_confirm_nothing(display):
    """A mixed drop point's loose archives are claimed into a folder of
    their own (no subfolders left in it); the watcher says it is a mixed drop."""
    path, = _arrive("Mixed Drop", "Desire Ch. 1.cbz")
    cbz_core.known_groups_mixed_drop(True)
    assert parse_comic_name(path, display_tokens=display).filename == "Desire Ch. 1.cbz"
    cbz_core.known_groups_mixed_drop(False)
    assert parse_comic_name(path, display_tokens=display).filename == "Mixed Drop Ch. 1.cbz"


def test_siblings_already_named_after_the_series_still_confirm(display):
    """A batch is renamed one archive at a time; the archives already done
    carry the folder's series and keep confirming the rest."""
    assert _name("City of Sins", "Mangakakalot Ch. 54.cbz", display,
                 "City of Sins Ch. 53.cbz", "Mangakakalot Ch. 55.cbz") == "City of Sins Ch. 54.cbz"


def _make_cbz(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("page1.jpg", b"\0" * size)
    return path


def _payloads(root: Path) -> list[int]:
    found = []
    for path in root.rglob("*.cbz") if root.exists() else []:
        with zipfile.ZipFile(path) as zf:
            found.append(len(zf.read("page1.jpg")))
    return sorted(found)


def _watch(tmp_path, monkeypatch):
    incoming, library = tmp_path / "incoming", tmp_path / "Manga"
    incoming.mkdir()
    library.mkdir()
    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(incoming))
    monkeypatch.setattr(watcher, "_routing_rules", [])
    monkeypatch.setattr(watcher, "_routing_default", str(library))
    monkeypatch.setattr(watcher, "_series_resolvers", {})
    monkeypatch.setattr(watcher, "POLL_INTERVAL", 0)
    monkeypatch.setattr(watcher, "AI_DECENSOR_ENABLED", False)
    monkeypatch.setattr(watcher, "wait_for_file_stable", lambda *a, **k: True)
    monkeypatch.setattr(cbz_core, "translate_metadata_text", lambda text: (text, None))
    return incoming, library


def test_the_live_arrival_lands_under_its_series(tmp_path, monkeypatch):
    incoming, library = _watch(tmp_path, monkeypatch)
    _make_cbz(incoming / "City of Sins" / "Mangakakalot Ch. 53.cbz", 53)
    _make_cbz(incoming / "City of Sins" / "Mangakakalot Ch. 54.cbz", 54)

    watcher.process_and_move_directory(incoming / "City of Sins")

    assert sorted(p.name for p in (library / "City of Sins").glob("*.cbz")) \
        == ["City of Sins Ch. 53.cbz", "City of Sins Ch. 54.cbz"]


def test_a_mixed_drops_titles_land_under_their_own_names(tmp_path, monkeypatch):
    """Codex's #179 probe, adapted: loose 'Desire' and 'Violet' books beside a
    nested series keep their titles; nothing is reviewed."""
    incoming, library = _watch(tmp_path, monkeypatch)
    mixed = incoming / "Mixed Drop"
    _make_cbz(mixed / "Desire Ch. 1.cbz", 101)
    _make_cbz(mixed / "Violet Ch. 1.cbz", 202)
    _make_cbz(mixed / "Other Series" / "Other Series Ch. 2.cbz", 303)

    watcher.process_and_move_directory(mixed)

    names = {p.name for p in library.rglob("*.cbz")}
    assert {"Desire Ch. 1.cbz", "Violet Ch. 1.cbz"} <= names
    assert _payloads(library) == [101, 202, 303]
    assert _payloads(tmp_path / "_review") == []


def test_a_lone_listed_title_in_a_mixed_drop_keeps_its_name(tmp_path, monkeypatch):
    """One loose 'Desire' beside a nested series: its claim folder holds it
    alone, so only the watcher's mixed-drop context keeps it a title."""
    incoming, library = _watch(tmp_path, monkeypatch)
    mixed = incoming / "Mixed Drop"
    _make_cbz(mixed / "Desire Ch. 1.cbz", 101)
    _make_cbz(mixed / "Other Series" / "Other Series Ch. 2.cbz", 303)

    watcher.process_and_move_directory(mixed)

    assert "Desire Ch. 1.cbz" in {p.name for p in library.rglob("*.cbz")}


# David, 2026-11-10: an exact listed group overrides the digit rule only.
@pytest.mark.parametrize("folder, arrival, expected", [
    ("3XLOVE Uncensored", "Manhwa18 Chapter 1.cbz", "3XLOVE Uncensored Ch. 1.cbz"),
    ("3XLOVE Uncensored", "Manhwa18 2 Chapter 5.cbz", "3XLOVE Uncensored Ch. 5.cbz"),
    ("Berserk", "Gamma 2 Ch. 3.cbz", "Berserk Ch. 3.cbz"),
    ("Berserk", "Omega 2 Chapter 4.cbz", "Berserk Ch. 4.cbz"),
    ("Berserk", "Amin3 Ch. 2.cbz", "Berserk Ch. 2.cbz"),
    ("Berserk", "KM 42 Chapter 6.cbz", "Berserk Ch. 6.cbz"),
])
def test_a_listed_group_with_a_digit_gives_way_to_the_folder_series(folder, arrival,
                                                                   expected, display):
    assert _name(folder, arrival, display) == expected


def test_the_hashed_path_keeps_its_digit_rule(display):
    """Unchanged: with the downloader's hash any group is dropped, but never
    one holding a digit, listed or not."""
    assert _name("3XLOVE Uncensored", "Manhwa18_Chapter 1_ab12cd.cbz", display) \
        == "Manhwa18 Chapter 1.cbz"
    assert _name("Berserk", "Alpha_Chapter 3_ab12cd.cbz", display) == "Berserk Ch. 3.cbz"


def test_a_second_copy_from_a_numbered_group_meets_the_review_path(tmp_path, monkeypatch):
    """'Manhwa18 2' is a second copy of 'Manhwa18' with the same numbering.
    Stripped, it collides with the primary copy and goes to review; both
    books are kept. The two prefixes differ, so each needs its own folder's
    confirmation: they arrive in one folder only alongside their own kind."""
    incoming, library = _watch(tmp_path, monkeypatch)
    _make_cbz(incoming / "3XLOVE Uncensored" / "Manhwa18 Chapter 5.cbz", 500)
    watcher.process_and_move_directory(incoming / "3XLOVE Uncensored")
    _make_cbz(incoming / "3XLOVE Uncensored" / "Manhwa18 2 Chapter 5.cbz", 400)
    watcher.process_and_move_directory(incoming / "3XLOVE Uncensored")

    series = library / "3XLOVE Uncensored"
    assert [p.name for p in series.glob("*.cbz")] == ["3XLOVE Uncensored Ch. 5.cbz"]
    set_aside = _payloads(tmp_path / "_review") + _payloads(tmp_path / "_quarantine")
    assert sorted(_payloads(series) + set_aside) == [400, 500]


# PR #179 review round 2: a sibling confirms the folder only when it is the
# folder's series itself followed by pure numbering, not a different title
# that merely starts with it.
@pytest.mark.parametrize("sibling", [
    "City of Sins Side Story Ch. 2.cbz",
    "City of Sins Extra Ch. 2.cbz",
    "City of Sins2 Ch. 2.cbz",
])
def test_a_different_title_cannot_confirm_the_folder_series(sibling, display):
    """Codex's round-2 probe, adapted: 'Desire' keeps its title."""
    assert _name("City of Sins", "Desire Ch. 1.cbz", display, sibling) == "Desire Ch. 1.cbz"


@pytest.mark.parametrize("sibling", [
    "City of Sins Ch. 5.cbz", "City of Sins v2 Ch. 3.cbz", "City of Sins Ch. 1-30.cbz",
    "City of Sins ch. 4.cbz", "City of Sins #6.cbz", "City of Sins 第7話.cbz",
])
def test_a_sibling_named_after_the_series_still_confirms(sibling, display):
    assert _name("City of Sins", "Mangakakalot Ch. 53.cbz", display, sibling) \
        == "City of Sins Ch. 53.cbz"


@pytest.mark.parametrize("stem, named", [
    ("City of Sins Ch. 5", True), ("City of Sins v2 Ch. 3", True),
    ("City of Sins Ch. 1-30", True), ("city  of sins ch. 7", True),
    ("City of Sins Side Story Ch. 2", False), ("City of Sins Extra Ch. 2", False),
    ("City of Sins2 Ch. 2", False), ("City of Sins Ch. 5 Eclipse", False),
    ("City of Sins Ch. 4 (2019)", False), ("City of Sins", False),
])
def test_named_after_series_is_the_series_and_pure_numbering(stem, named):
    assert cbz_core._named_after_series(stem, "City of Sins") is named


def test_a_different_title_does_not_erase_a_listed_title_on_landing(tmp_path, monkeypatch):
    """Codex's round-2 probe, adapted: both books land, 'Desire' under its own
    name, nothing reviewed."""
    incoming, library = _watch(tmp_path, monkeypatch)
    folder = incoming / "City of Sins"
    _make_cbz(folder / "Desire Ch. 1.cbz", 101)
    _make_cbz(folder / "City of Sins Side Story Ch. 2.cbz", 202)

    watcher.process_and_move_directory(folder)

    names = {p.name for p in library.rglob("*.cbz")}
    assert "Desire Ch. 1.cbz" in names
    assert _payloads(library) == [101, 202]
    assert _payloads(tmp_path / "_review") == []
