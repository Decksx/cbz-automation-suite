"""Downloader chapter names carry the scanlation group, not the series.

Arrivals named ``<Group>_<Chapter|Ch.|Episode|Ep.> N[ - title]_<hash6>.cbz``
(live, 2026-11-10: ``Alpha_Episode 42_2a8cb9.cbz`` in "Life of a Demon
Hunter") became ``Alpha Episode 42.cbz``: cleaning turned the underscores
into spaces, so ``normalize_stem`` saw a stem that did not start with a
chapter token and never put the folder's series in front. The group-free
``Chapter 17_028c98.cbz`` already became ``Survival Supremacy Ch. 17.cbz``.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest

from scripts import cbz_core
from scripts import cbz_watcher as watcher
from scripts.cbz_core import parse_comic_name


@pytest.fixture(params=[False, True], ids=["plain", "display"])
def display(request):
    """Both the library CLIs' parse and the watcher's display-token parse."""
    return request.param


def _name(folder: str, arrival: str, display: bool) -> str:
    return parse_comic_name(Path("C:/intake") / folder / arrival, display_tokens=display).filename


@pytest.mark.parametrize("folder, arrival, expected", [
    ("Life of a Demon Hunter", "Alpha_Episode 42_2a8cb9.cbz", "Life of a Demon Hunter Ch. 42.cbz"),
    ("Life of a Demon Hunter", "Asura_Chapter 42_d1a897.cbz", "Life of a Demon Hunter Ch. 42.cbz"),
    ("The Toy of 100 Baseball Players", "Myung_Chapter 49_51801c.cbz",
     "The Toy of 100 Baseball Players Ch. 49.cbz"),
    ("Life of a Demon Hunter", "Webtoon_Ep. 45 - Friend (7)_d3f353.cbz",
     "Life of a Demon Hunter Ch. 45 Friend.cbz"),
    ("Lookism", "Luminare Translations_Chapter 36.5_d8b483.cbz", "Lookism Ch. 36.5.cbz"),
    ("Lookism", "Literary Curator_Chapter 7_0a1b2c.cbz", "Lookism Ch. 7.cbz"),
    ("My Girlfriend", "The Hours Between_Episode 10_d655f0.cbz", "My Girlfriend Ch. 10.cbz"),
    ("Palace Slave", "gladys_Episode 9_96debe.cbz", "Palace Slave Ch. 9.cbz"),
    ("Berserk", "Setsu Scans_Ch. 5_ab12cd.cbz", "Berserk Ch. 5.cbz"),
])
def test_a_group_prefix_gives_way_to_the_folder_series(folder, arrival, expected, display):
    assert _name(folder, arrival, display) == expected


def test_a_prefix_that_is_the_folder_series_names_the_chapter_the_same_way(display):
    assert _name("Life of a Demon Hunter", "Life of a Demon Hunter_Chapter 42_2a8cb9.cbz",
                 display) == "Life of a Demon Hunter Ch. 42.cbz"
    assert _name("Life of a Demon Hunter", "Life of a Demon Hunter_Chapter 42_2a8cb9.cbz", display) \
        == _name("Life of a Demon Hunter", "Asura_Chapter 42_d1a897.cbz", display)


@pytest.mark.parametrize("folder, arrival", [
    ("Survival Supremacy", "Chapter 17_028c98.cbz"),        # group-free: unchanged
    ("Berserk", "Berserk_Chapter 5.cbz"),                   # no downloader hash
    ("Berserk", "Vol. 3_Chapter 12_aaaaaa.cbz"),            # a volume, not a group
    ("Berserk", "Season 2_Episode 4_bbbbbb.cbz"),           # a season, not a group
    ("Palace Slave", "gladys_Prologue_c3b658.cbz"),         # no chapter number
    ("Berserk", "Berserk Ch. 5.cbz"),
])
def test_names_without_a_droppable_group_are_parsed_as_before(folder, arrival, display):
    """Passes before and after the change: these names keep today's result."""
    assert _name(folder, arrival, display) == {
        "Chapter 17_028c98.cbz": "Survival Supremacy Ch. 17.cbz",
        "Berserk_Chapter 5.cbz": "Berserk Chapter 5.cbz",
        "Vol. 3_Chapter 12_aaaaaa.cbz": "Vol. 3 Chapter 12.cbz",
        "Season 2_Episode 4_bbbbbb.cbz": "Season 2 Episode 4.cbz",
        "gladys_Prologue_c3b658.cbz": "gladys Prologue.cbz",
        "Berserk Ch. 5.cbz": "Berserk Ch. 5.cbz",
    }[arrival]


def test_an_episode_arrival_keeps_its_episode_number():
    parsed = parse_comic_name(Path("C:/intake/Life of a Demon Hunter/Alpha_Episode 42_2a8cb9.cbz"))
    assert (parsed.chapter, parsed.episode) == ("42", "42")
    parsed = parse_comic_name(Path("C:/intake/Lookism/Asura_Chapter 42_d1a897.cbz"))
    assert (parsed.chapter, parsed.episode) == ("42", None)


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
    """A watch folder and library under *tmp_path*, wired into the watcher."""
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


def test_two_translations_of_one_chapter_meet_the_collision_path(tmp_path, monkeypatch):
    """Alpha's Episode 42 and Asura's Chapter 42 now share one canonical name.
    The second never overwrites the first: one lands, the other is set aside
    (quarantine or review), and both books survive."""
    incoming, library = _watch(tmp_path, monkeypatch)
    arrival = incoming / "Life of a Demon Hunter"
    _make_cbz(arrival / "Alpha_Episode 42_2a8cb9.cbz", 400)
    _make_cbz(arrival / "Asura_Chapter 42_d1a897.cbz", 300)

    watcher.process_and_move_directory(arrival)

    series = library / "Life of a Demon Hunter"
    assert sorted(p.name for p in series.glob("*.cbz")) == ["Life of a Demon Hunter Ch. 42.cbz"]
    set_aside = _payloads(tmp_path / "_quarantine") + _payloads(tmp_path / "_review")
    assert sorted(_payloads(series) + set_aside) == [300, 400]
    assert len(set_aside) == 1
    assert not list(incoming.rglob("*.cbz"))


# PR #178 review: a structural prefix is identity, never a group. Each name
# keeps what it was named before this change (numeric parts keep master's
# older spelling, which is not redesigned here).
@pytest.mark.parametrize("prefix, expected", [
    ("Part A", "Part A Chapter 5.cbz"),
    ("Part B", "Part B Chapter 5.cbz"),
    ("Part 1", "Berserk Ch. 1 Chapter 5.cbz"),
    ("Part 2", "Berserk Ch. 2 Chapter 5.cbz"),
    ("Pt. 3", "Pt. 3 Chapter 5.cbz"),
    ("S2", "S2 Chapter 5.cbz"),
    ("Arc 3", "Arc 3 Chapter 5.cbz"),
    ("Book 1", "Book 1 Chapter 5.cbz"),
    ("Side Story", "Side Story Chapter 5.cbz"),
    ("Extra", "Extra Chapter 5.cbz"),
    ("Special", "Special Chapter 5.cbz"),
    ("Bonus", "Bonus Chapter 5.cbz"),
    ("Act II", "Act II Chapter 5.cbz"),
])
def test_a_structural_prefix_is_not_taken_for_a_group(prefix, expected, display):
    assert _name("Berserk", f"{prefix}_Chapter 5_ab12cd.cbz", display) == expected


@pytest.mark.parametrize("parts", [("Part 1", "Part 2"), ("Part A", "Part B")],
                         ids=["numeric", "letters"])
def test_distinct_parts_land_as_distinct_books(tmp_path, monkeypatch, parts):
    """Codex's #178 probe, adapted: two parts of one chapter are two books.
    They must not share a name, so neither is reviewed as a second copy."""
    incoming, library = _watch(tmp_path, monkeypatch)
    arrival = incoming / "Berserk"
    for i, part in enumerate(parts):
        _make_cbz(arrival / f"{part}_Chapter 5_ab12cd.cbz", 101 + i)

    watcher.process_and_move_directory(arrival)

    assert _payloads(library) == [101, 102]
    assert len(list(library.rglob("*.cbz"))) == 2
    assert _payloads(tmp_path / "_review") == [] and _payloads(tmp_path / "_quarantine") == []


# PR #178 review round 2: the identity grammar already in the code decides,
# in every spelling it accepts (run together, hyphenated, with a number).
@pytest.mark.parametrize("prefix", [
    "PartA", "PartB", "One-shot", "Oneshot", "One shot", "SideStory", "Side-Story",
    "Extra2", "Omake", "Complete",
])
def test_known_identity_spellings_are_not_taken_for_a_group(prefix, display):
    assert _name("Berserk", f"{prefix}_Chapter 5_ab12cd.cbz", display) == f"{prefix} Chapter 5.cbz"


def test_the_watcher_and_the_group_check_share_one_part_grammar():
    """The watcher's same-chapter copy check reads parts with the grammar
    that keeps them here, so the two cannot drift apart."""
    assert watcher._PART_RE is cbz_core.PART_MARKER_RE
    assert watcher._PART_RE.search("parta").group(1) == "a"


def test_compact_parts_land_as_distinct_books(tmp_path, monkeypatch):
    """Codex's round-2 probe, adapted: 'PartA' and 'PartB' are two books."""
    incoming, library = _watch(tmp_path, monkeypatch)
    arrival = incoming / "Berserk"
    for i, part in enumerate(("PartA", "PartB")):
        _make_cbz(arrival / f"{part}_Chapter 5_ab12cd.cbz", 101 + i)

    watcher.process_and_move_directory(arrival)

    assert _payloads(library) == [101, 102]
    assert len(list(library.rglob("*.cbz"))) == 2
    assert _payloads(tmp_path / "_review") == [] and _payloads(tmp_path / "_quarantine") == []


# PR #178 review round 3: the grammars are used in addition to the earlier
# word guards, never instead of them.

def _identity(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.casefold())


@pytest.mark.parametrize("prefix", ["Part-A", "Part-B", "Side A", "Side B", "Extras", "Specials"])
def test_previously_protected_identity_stays_protected(prefix, display):
    """Codex's round-3 probe, adapted."""
    name = _name("Berserk", f"{prefix}_Chapter 5_ab12cd.cbz", display)
    assert _identity(prefix) in _identity(name), f"{prefix} lost in {name}"


@pytest.mark.parametrize("pair", [("Part-A", "Part-B"), ("Side A", "Side B")],
                         ids=["hyphenated-parts", "lettered-sides"])
def test_paired_structural_books_both_land(tmp_path, monkeypatch, pair):
    incoming, library = _watch(tmp_path, monkeypatch)
    arrival = incoming / "Berserk"
    for i, part in enumerate(pair):
        _make_cbz(arrival / f"{part}_Chapter 5_ab12cd.cbz", 101 + i)

    watcher.process_and_move_directory(arrival)

    assert _payloads(library) == [101, 102]
    assert len(list(library.rglob("*.cbz"))) == 2
    assert _payloads(tmp_path / "_review") == [] and _payloads(tmp_path / "_quarantine") == []


# The word set the group check had at d1bc97b, written out here rather than
# read from the code, so no word can leave the guard unnoticed.
_D1BC97B_WORDS = (
    "part", "parts", "pt", "arc", "book", "story", "side", "extra", "extras",
    "special", "specials", "bonus", "omake", "oneshot", "prologue", "epilogue",
    "afterword", "interlude", "act", "season", "volume", "vol", "chapter", "ch",
    "episode", "ep",
)


@pytest.mark.parametrize("word", _D1BC97B_WORDS)
def test_every_earlier_structural_word_is_still_identity(word):
    for spelling in (word, word.title(), word.upper(), f"{word.title()} A", f"{word.title()}-B"):
        assert not cbz_core._is_group_prefix(spelling), spelling
        arrival = f"{spelling}_Chapter 5_ab12cd.cbz"
        assert cbz_core._without_group_prefix(arrival) == arrival  # left to today's parse
