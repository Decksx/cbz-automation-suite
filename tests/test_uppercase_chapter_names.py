"""The canonical chapter token is "Ch." (David, 2026-11-10; PR (C)).

Until PR (C) the canonical formatter, and so the watcher's fallback names and
media naming's chapter style, wrote "ch.". These pin the new spelling, that
every reader still reads both, and what happens to the "ch." names already
in the library: an arrival keeps its own, and the canonical planner renames
a library file once (see tests/test_canonical_plan.py).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from comic_automation.archive import field_resolution as fr
from scripts import cbz_core
from scripts.cbz_canonical_name import (
    CanonicalFields as F,
    NotCanonicalNameError,
    format_canonical_name as fmt,
    parse_canonical_name as parse,
)
from scripts.cbz_core import parse_comic_name

_CJK = re.compile(r"[぀-ヿ一-鿿가-힯]")


@pytest.mark.parametrize("fields, name", [
    (F("Berserk", chapter=4), "Berserk Ch. 4.cbz"),
    (F("S", volume=3, chapter=20), "S v03 Ch. 20.cbz"),
    (F("S", chapter="1-30"), "S Ch. 1-30.cbz"),
    (F("S", series_year=2019, chapter=3), "S (2019) Ch. 3.cbz"),
    (F("S", special="Special", chapter=2, year=2020, version=3),
     "S Special Ch. 2 (2020) [v3].cbz"),
])
def test_the_formatter_writes_Ch_and_reads_it_back(fields, name):
    assert fmt(fields) == name
    assert parse(name) == fields


@pytest.mark.parametrize("name", [
    "Berserk ch. 4.cbz", "S v03 ch. 20.cbz", "S (2019) ch. 3.cbz",
    "S Special ch. 2 (2020) [v3].cbz",
])
def test_the_earlier_ch_image_is_canonical_only_when_asked(name):
    with pytest.raises(NotCanonicalNameError, match=re.escape(name.replace(" ch. ", " Ch. "))):
        parse(name)
    fields = parse(name, lowercase_chapter=True)
    assert fmt(fields) == name.replace(" ch. ", " Ch. ")


@pytest.mark.parametrize("name", [
    "Berserk CH. 4.cbz",      # neither spelling the formatter ever wrote
    "Berserk ch. 04.cbz",     # padded, as before
    "Berserk v1 ch. 4.cbz",   # unpadded volume, as before
    "Berserk ch.4.cbz",       # no space, as before
    "Berserk ch. 4 .cbz",     # trailing space, as before
])
def test_the_lowercase_option_admits_nothing_else(name):
    with pytest.raises(NotCanonicalNameError):
        parse(name, lowercase_chapter=True)


@pytest.mark.parametrize("token", ["ch.", "Ch."])
def test_both_spellings_read_as_the_same_chapter(tmp_path, token):
    name = f"Series {token} 5.cbz"
    tokens = fr.parse_name_tokens(name)
    assert tokens.canonical and tokens.chapters == ("5",)
    path = tmp_path / "Series" / name
    path.parent.mkdir()
    path.write_bytes(b"")
    for display in (False, True):
        assert parse_comic_name(path, display_tokens=display).chapter == "5"


@pytest.mark.parametrize("name, expected", [
    ("Berserk v03 Ch. 7.cbz", "Berserk v03 Ch. 7.cbz"),  # the canonical name now
    ("Berserk v03 ch. 7.cbz", "Berserk v03 Ch. 7.cbz"),  # the canonical name before PR (C)
    ("Berserk ch. 7.cbz", "Berserk Ch. 7.cbz"),
])
def test_an_arrival_with_a_canonical_name_keeps_it(tmp_path, name, expected):
    """A canonical name keeps its padding and tokens; one written before PR
    (C) is spelt "Ch." like every display name since the legacy display path
    does too (PR (D)). Either way the result is a fixpoint."""
    path = tmp_path / "Berserk" / name
    path.parent.mkdir()
    path.write_bytes(b"")
    assert parse_comic_name(path, display_tokens=True).filename == expected
    again = tmp_path / "again" / expected
    again.parent.mkdir()
    again.write_bytes(b"")
    assert parse_comic_name(again, display_tokens=True).filename == expected


def test_a_canonical_name_with_a_dropped_language_edition_is_reformatted_with_Ch(tmp_path):
    path = tmp_path / "Solo Leveling" / "Solo Leveling ch. 4 (English).cbz"
    path.parent.mkdir()
    path.write_bytes(b"")
    first = parse_comic_name(path, display_tokens=True).filename
    assert first == "Solo Leveling Ch. 4.cbz"
    again = path.with_name(first)
    path.rename(again)
    assert parse_comic_name(again, display_tokens=True).filename == first


@pytest.mark.parametrize("name, expected", [
    ("漫画 Ch. 3.cbz", "Story Ch. 3.cbz"),      # translated, as before
    ("漫画 ch. 3.cbz", "Story Ch. 3.cbz"),
    ("漫画 v02 Ch. 3.cbz", "Story v02 Ch. 3.cbz"),
    ("漫画 #003.cbz", "漫画 #003.cbz"),  # an issue: kept, as before
    ("漫画 v01.cbz", "漫画 v01.cbz"),
])
def test_a_translated_title_still_names_a_canonical_chapter(tmp_path, monkeypatch, name, expected):
    """A canonical chapter name is now spelt like most arrivals ("Ch."), so
    keeping canonical names verbatim must not skip the title translation
    those arrivals always had. Other canonical names are kept as before."""
    monkeypatch.setattr(cbz_core, "translate_metadata_text",
                        lambda text: ("Story", text) if _CJK.search(text) else (text, None))
    path = tmp_path / "Series" / name
    path.parent.mkdir()
    path.write_bytes(b"")
    assert parse_comic_name(path, display_tokens=True).filename == expected
