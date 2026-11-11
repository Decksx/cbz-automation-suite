"""The watcher's display name spells a standalone chapter token "Ch."

David, 2026-11-10: uppercase "Ch." everywhere the watcher names a file. PR
(C) did it for the canonical formatter; this does it for the legacy display
path, which kept an arrival's own "ch."/"ch"/"CH." ("Book ch. 3
(ManhwaClub).cbz" landed as "Book ch. 3.cbz"). Only the token changes, only
when it stands alone before a number, and plain mode (the library CLIs) is
unchanged.

The 2026-03 sanitizer bug cut a "ch" off the end of words ("Beach" ->
"Bea", see tests/test_sanitizer_directory_names.py); the word-ending cases
below prove this change cannot do that.
"""

from __future__ import annotations

import re

import pytest

from scripts import cbz_core
from scripts.cbz_core import _display_chapter_token, parse_comic_name

_CJK = re.compile(r"[぀-ヿ一-鿿가-힯]")


def _name(tmp_path, folder, arrival, display=True):
    path = tmp_path / folder / arrival
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return parse_comic_name(path, display_tokens=display).filename


@pytest.mark.parametrize("arrival, expected", [
    ("Book ch. 3 (ManhwaClub).cbz", "Book Ch. 3.cbz"),   # David's example
    ("Book ch 3 (ManhwaClub).cbz", "Book Ch. 3.cbz"),
    ("Book CH. 3.cbz", "Book Ch. 3.cbz"),
    ("Book CH.3.cbz", "Book Ch.3.cbz"),
    ("Book ch3.cbz", "Book Ch.3.cbz"),
    ("Book Ch 3.cbz", "Book Ch. 3.cbz"),
    ("Book ch. 1-30.cbz", "Book Ch. 1-30.cbz"),
    ("Book ch. 3 (2019) [v2].cbz", "Book Ch. 3 (2019) [v2].cbz"),
    ("Book ch. 3.cbz", "Book Ch. 3.cbz"),                # the canonical image before PR (C)
    ("Book v03 ch. 7.cbz", "Book v03 Ch. 7.cbz"),
    ("Book Ch. 3.cbz", "Book Ch. 3.cbz"),                # already right
    ("Book Chapter 3.cbz", "Book Chapter 3.cbz"),        # not the token
    ("Book {ch 3}.cbz", "Book {ch 3}.cbz"),              # a kept {...} group is text
])
def test_the_display_name_spells_the_chapter_token_Ch(tmp_path, arrival, expected):
    assert _name(tmp_path, "Book", arrival) == expected
    assert _name(tmp_path / "again", "Book", expected) == expected  # its own fixpoint


@pytest.mark.parametrize("arrival", ["Book ch. 3 (ManhwaClub).cbz", "Book CH.3.cbz", "Book ch 3.cbz"])
def test_plain_mode_is_unchanged(tmp_path, arrival):
    """Plain mode is what the library maintenance and sanitizer CLIs use on
    files already in the library; it keeps the arrival's spelling as before."""
    assert "Ch" not in _name(tmp_path, "Book", arrival, display=False)


_WORD_ENDINGS = ["Beach", "Witch", "Punch", "Lunch", "Sandwich"]


@pytest.mark.parametrize("display", [False, True], ids=["plain", "display"])
@pytest.mark.parametrize("series", _WORD_ENDINGS)
def test_a_ch_ending_a_word_is_never_touched(tmp_path, series, display):
    token = "Ch." if display else "ch"
    for arrival, expected in [
        (f"{series}.cbz", f"{series}.cbz"),
        (f"{series} 3.cbz", f"{series} 3.cbz"),
        (f"{series} 3 (ManhwaClub).cbz", f"{series} 3.cbz"),  # through the stem pipeline
        (f"{series}.5 (Digital).cbz", f"{series}.5.cbz"),
        (f"{series}3.cbz", f"{series}3.cbz"),
        (f"{series} ch 3.cbz", f"{series} {token} 3.cbz"),
        (f"{series} Ch. 3.cbz", f"{series} Ch. 3.cbz"),
        (f"Out of {series} ch 12.cbz", f"Out of {series} {token} 12.cbz"),
    ]:
        assert _name(tmp_path / arrival, series, arrival, display) == expected, arrival


@pytest.mark.parametrize("text", [
    "Beach 3", "Witch3", "Punch.3", "Lunch 1-30", "Sandwich #3", "Beach-3",
    "漫画第3話",          # CJK marker
    "漫画ch.3",                    # glued to CJK text: not standalone
    "나 3화",                      # Korean marker
    "Book Chapter 3", "Book ch. Three", "Book {ch 3}", "Book {x ch 3}",
])
def test_the_respelling_leaves_everything_else_alone(text):
    assert _display_chapter_token(text) == text


@pytest.mark.parametrize("text, expected", [
    ("Book ch. 3", "Book Ch. 3"), ("Book ch 3", "Book Ch. 3"), ("Book CH.3", "Book Ch.3"),
    ("Book cH 3", "Book Ch. 3"), ("ch 3", "Ch. 3"), ("Book-ch.3", "Book-Ch.3"),
    ("Book {x} ch 3", "Book {x} Ch. 3"), ("Book ch 3 ch 4", "Book Ch. 3 Ch. 4"),
])
def test_the_respelling_changes_only_the_token(text, expected):
    assert _display_chapter_token(text) == expected
    assert _display_chapter_token(expected) == expected


def test_a_translated_range_is_spelt_Ch(tmp_path, monkeypatch):
    monkeypatch.setattr(cbz_core, "translate_metadata_text",
                        lambda text: ("Story", text) if _CJK.search(text) else (text, None))
    assert _name(tmp_path, "Series", "漫画 ch. 1-30.cbz") == "Story Ch. 1-30.cbz"
