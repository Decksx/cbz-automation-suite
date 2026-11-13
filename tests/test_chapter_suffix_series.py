"""A chapter token followed by a dash-separated title/artist suffix ends the series title.

Per-chapter drops named "<Series> Chapter N - <title> - <artist>" used to become
one series per chapter, because only a *trailing* chapter token was stripped
(2026-11-09: 50 Comix folders merged by hand). The keyword-plus-number rule now
also applies when the token is followed by " - <suffix>", while ranges, names
with no series before the token, and titles without a chapter keyword are left
alone.
"""
import pytest

from scripts import cbz_watcher as watcher
from scripts.cbz_core import series_base_name


@pytest.mark.parametrize("name, series", [
    ("Hypnotist Security Guard Chapter 12 – The Night Shift – Kuroneko", "Hypnotist Security Guard"),
    ("Hypnotist Security Guard Chapter 13 - Overtime - Kuroneko", "Hypnotist Security Guard"),
    ("Knock Knock Ch. 4 - Title - Artist", "Knock Knock"),
    ("Knock Knock Chapter 3.5 — Homecoming", "Knock Knock"),
    ("Knock Knock chapter 7 - lower case keyword", "Knock Knock"),
])
def test_chapter_token_before_a_dash_suffix_ends_the_series(name, series):
    assert series_base_name(name, bare_numbers=False) == series
    assert series_base_name(name, bare_numbers=True) == series


@pytest.mark.parametrize("name", [
    "Batman Chapter 1-30 - Compilation",   # a range is never one chapter
    "Chapter 1 - Untitled",                # nothing before the token to be a series
    "Area 88 - Desert Story",              # no chapter keyword
    "Knock Knock Chapter Two - Title",     # no number after the keyword
    "Knock Knock Chapter 4-Title",         # no spaced separator: not this pattern
    # Spaced ranges, in the range grammar or as an em dash (PR #186 review).
    "Book Chapter 1 - 30 - Compilation",
    "Book Chapter 1 – 30 – Compilation",
    "Book Chapter 1 — 30 — Compilation",
    # Identity-bearing suffixes are structural, not title/artist text.
    "Book Chapter 12 - Part A",
    "Book Chapter 12 - Part B",
    "Book Chapter 12 - Side A",
    "Book Chapter 12 – Side B",
    "Book Chapter 3 - Extras",
    "Book Chapter 3.5 — Extra",
    "Book Chapter 3 - Specials",
    "Book Chapter 3 - Part-A",
])
def test_names_outside_the_pattern_are_left_alone(name):
    assert series_base_name(name, bare_numbers=False) is None
    assert series_base_name(name, bare_numbers=True) is None


@pytest.mark.parametrize("name", ["Book Chapter 1 - 30", "Book Chapter 1 — 30"])
def test_a_bare_spaced_range_is_not_a_chapter_suffix(name):
    """Only the digit guard sees these: "30" alone is not structural. (With
    bare numbers allowed, the pre-existing trailing-number rule still strips
    the final "30"; that path is unchanged by this rule.)"""
    assert series_base_name(name, bare_numbers=False) is None


def test_trailing_tokens_still_win(tmp_path):
    assert series_base_name("Usogui Chapter 100", bare_numbers=False) == "Usogui"
    assert series_base_name("Berserk 4", bare_numbers=True) == "Berserk"
    assert series_base_name("Area 88", bare_numbers=False) is None


def test_watcher_lands_every_chapter_drop_in_one_series_folder(tmp_path):
    dest = tmp_path / "Comix"
    dest.mkdir()
    resolved = set()
    for number, title in ((12, "The Night Shift"), (13, "Overtime"), (14, "Day Off")):
        folder = tmp_path / "claims" / f"Hypnotist Security Guard Chapter {number} – {title} – Kuroneko"
        folder.mkdir(parents=True)
        series, dir_number = watcher._resolve_series_dir_name(folder, [], str(dest))
        assert dir_number == str(number)
        resolved.add(series)
    assert resolved == {"Hypnotist Security Guard"}


@pytest.mark.parametrize("suffix", ["Night Shift", "Artist"])
def test_media_mode_keeps_the_series_year_of_a_chapter_suffix_drop(tmp_path, monkeypatch, suffix):
    """Registered media mode reattaches the folder's series year before the
    keyword rule runs; the suffix rule must not drop it, or 2019 and 2020
    reboots land in one series as the same chapter (PR #186 review round 2)."""
    monkeypatch.setattr(watcher, "_media_naming_enabled", lambda: True)
    dest = tmp_path / "Comix"
    dest.mkdir()
    resolved = []
    for year in (2019, 2020):
        folder = tmp_path / "claims" / f"Book Chapter 12 - {suffix} ({year})"
        folder.mkdir(parents=True)
        resolved.append(watcher._resolve_series_dir_name(folder, [], str(dest)))
    assert resolved == [("Book (2019)", "12"), ("Book (2020)", "12")]


@pytest.mark.parametrize("pair", [("Part A", "Part B"), ("Side A", "Side B")])
def test_watcher_keeps_part_and_side_folders_distinct(tmp_path, pair):
    """Two halves of one chapter must not collapse into one series and one
    chapter filename; one of them would go to duplicate quarantine."""
    dest = tmp_path / "Comix"
    dest.mkdir()
    resolved = []
    for marker in pair:
        folder = tmp_path / "claims" / f"Book Chapter 12 - {marker}"
        folder.mkdir(parents=True)
        series, _ = watcher._resolve_series_dir_name(folder, [], str(dest))
        resolved.append(series)
    assert resolved[0] != resolved[1]
    assert all(marker in series for marker, series in zip(pair, resolved))
