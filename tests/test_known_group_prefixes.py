"""A known group's prefix gives way to the series folder, hash or no hash.

PR #178 drops a downloader's group prefix only with its trailing hex hash
(``Alpha_Episode 42_2a8cb9.cbz``): without it the prefix could be a real
series. Live on 2026-11-10 11:58, Comick (Unoriginal) delivered
``Mangakakalot Ch. 53.cbz`` into "City of Sins", and it kept that name. A
prefix that is exactly a known group or site (``config/known-groups.txt``)
is now dropped without the hash too; any other prefix keeps today's name.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from scripts import cbz_core
from scripts import cbz_watcher as watcher
from scripts.cbz_core import parse_comic_name


@pytest.fixture(params=[False, True], ids=["plain", "display"])
def display(request):
    return request.param


def _name(folder: str, arrival: str, display: bool) -> str:
    return parse_comic_name(Path("C:/intake") / folder / arrival, display_tokens=display).filename


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
    listed.write_text("# a comment\n\nZorblax Crew\nPart A\nPart 2\nS2\nV3\nEp4\n",
                      encoding="utf-8")
    monkeypatch.setattr(cbz_core, "KNOWN_GROUPS_FILE", listed)
    assert _name("Berserk", arrival, True) == "Berserk Ch. 2.cbz"
    # The structural checks still win over the list: a listed part, season,
    # volume or episode marker is never a group, digit or not.
    for prefix in ("Part A", "Part 2", "S2", "V3", "Ep4"):
        held = f"{prefix} Ch. 5.cbz"
        assert cbz_core._without_group_prefix(held) == held, prefix
    monkeypatch.setattr(cbz_core, "KNOWN_GROUPS_FILE", tmp_path / "missing.txt")
    assert _name("City of Sins", "Mangakakalot Ch. 53.cbz", True) == "Mangakakalot Ch. 53.cbz"


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

    watcher.process_and_move_directory(incoming / "City of Sins")

    assert [p.name for p in (library / "City of Sins").glob("*.cbz")] == ["City of Sins Ch. 53.cbz"]


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
    books are kept."""
    incoming, library = _watch(tmp_path, monkeypatch)
    arrival = incoming / "3XLOVE Uncensored"
    _make_cbz(arrival / "Manhwa18 Chapter 5.cbz", 500)
    _make_cbz(arrival / "Manhwa18 2 Chapter 5.cbz", 400)

    watcher.process_and_move_directory(arrival)

    series = library / "3XLOVE Uncensored"
    assert [p.name for p in series.glob("*.cbz")] == ["3XLOVE Uncensored Ch. 5.cbz"]
    assert sorted(_payloads(series) + _payloads(tmp_path / "_review")) == [400, 500]
