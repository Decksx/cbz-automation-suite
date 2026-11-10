"""A chapter range is never merged into one number.

A compilation ("The Toy of 100 Baseball Players 1-30", holding chapters 1
to 30) reached the library as "The Toy of 100 Baseball Players 130.cbz".
That collapse could not be reproduced on master (eb32d3e) or on the March
2026 code (5ca4a3b); the logs of the day have rotated away. These pin what
does still lose a range: "Ch. 1-30" read as chapter 1 (media naming then
wrote "ch. 1" or "#001", and ComicInfo Number 1), and a bracketed range
"(1-30)" dropped with the brackets.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from comic_automation.archive import field_resolution as fr
from comic_automation.library.media_naming import KeepArrivalName, watcher_naming
from comic_automation.library.number_style import NumberStyleRegistry
from scripts import cbz_core
from scripts import cbz_watcher as watcher
from scripts.cbz_canonical_name import format_canonical_name
from scripts.cbz_core import parse_comic_name

SERIES = "The Toy of 100 Baseball Players"


@pytest.fixture(params=[False, True], ids=["plain", "display"])
def display(request):
    return request.param


def _parsed(arrival: str, display: bool = True):
    return parse_comic_name(Path("C:/intake") / SERIES / arrival, display_tokens=display)


@pytest.mark.parametrize("arrival, expected", [
    (f"{SERIES} Ch. 1-30.cbz", f"{SERIES} Ch. 1-30.cbz"),
    (f"{SERIES} Ch. 1 - 30.cbz", f"{SERIES} Ch. 1 - 30.cbz"),
    (f"{SERIES} #1-30.cbz", f"{SERIES} #1-30.cbz"),
    (f"{SERIES} Vol. 1-3.cbz", f"{SERIES} Vol. 1-3.cbz"),
    (f"{SERIES} (1-30).cbz", f"{SERIES} 1-30.cbz"),
    (f"{SERIES} [1~30].cbz", f"{SERIES} 1~30.cbz"),
    (f"{SERIES} (01\u201312).cbz", f"{SERIES} 01\u201312.cbz"),
])
def test_a_range_is_kept_and_never_read_as_its_first_number(arrival, expected, display):
    parsed = _parsed(arrival, display)
    assert parsed.filename == expected
    assert (parsed.chapter, parsed.volume) == (None, None)


@pytest.mark.parametrize("arrival", [
    f"{SERIES} 1-30.cbz", f"{SERIES} 1\u201330.cbz", f"{SERIES} 1~30.cbz",
    f"{SERIES} Chapters 1-30.cbz",
])
def test_bare_ranges_keep_their_name(arrival, display):
    """Already kept on master; pinned so they stay kept."""
    parsed = _parsed(arrival, display)
    assert parsed.filename == arrival and parsed.chapter is None


@pytest.mark.parametrize("arrival, chapter", [
    (f"{SERIES} Ch. 5.cbz", "5"),
    (f"{SERIES} Ch. 1.5.cbz", "1.5"),
    (f"{SERIES} Ch. 5 - 3 Kittens.cbz", "5"),  # descending: a title, not a range
    (f"{SERIES} Ch. 7 - 7.cbz", "7"),          # equal: not a range
])
def test_a_single_number_is_still_read(arrival, chapter):
    assert _parsed(arrival).chapter == chapter


def _fact(tmp_path: Path, arrival: str):
    path = tmp_path / SERIES / arrival
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("00 - Chapter 1.cbz - 001__001.jpg", b"x")
    return path, fr.archive_facts_from_file(path)


@pytest.mark.parametrize("style", ["chapter", "issue"])
@pytest.mark.parametrize("arrival", [
    f"{SERIES} Ch. 1-30.cbz", f"{SERIES} #1-30.cbz", f"{SERIES} Vol. 1-3.cbz",
    f"{SERIES} (1-30).cbz",
])
def test_media_naming_keeps_the_legacy_name_for_a_range(tmp_path, arrival, style):
    path, fact = _fact(tmp_path, arrival)
    with pytest.raises(KeepArrivalName, match="number range"):
        watcher_naming(parse_comic_name(path, display_tokens=True), fact,
                       series=SERIES, style=style)


def test_media_naming_still_names_a_single_chapter(tmp_path):
    path, fact = _fact(tmp_path, f"{SERIES} Ch. 5.cbz")
    fields, _ = watcher_naming(parse_comic_name(path, display_tokens=True), fact,
                               series=SERIES, style="chapter")
    assert format_canonical_name(fields) == f"{SERIES} ch. 5.cbz"


def test_a_compilation_lands_with_its_range_and_no_comicinfo_number(tmp_path, monkeypatch):
    """End to end with media naming on: the name keeps 'Ch. 1-30' and the
    ComicInfo Number is left unset, not set to 1."""
    incoming, library = tmp_path / "incoming", tmp_path / "Manga"
    incoming.mkdir()
    library.mkdir()
    registry = tmp_path / "number-styles.json"
    registry.write_text(json.dumps({"version": 1, "roots": [{"path": str(library), "style": "chapter"}]}))
    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(incoming))
    monkeypatch.setattr(watcher, "_routing_rules", [])
    monkeypatch.setattr(watcher, "_routing_default", str(library))
    monkeypatch.setattr(watcher, "_series_resolvers", {})
    monkeypatch.setattr(watcher, "_router", None)
    monkeypatch.setattr(watcher, "_number_style_registry", NumberStyleRegistry.load(registry))
    monkeypatch.setattr(watcher, "_number_style_requested", "auto")
    monkeypatch.setattr(watcher, "POLL_INTERVAL", 0)
    monkeypatch.setattr(watcher, "AI_DECENSOR_ENABLED", False)
    monkeypatch.setattr(watcher, "wait_for_file_stable", lambda *a, **k: True)
    monkeypatch.setattr(cbz_core, "translate_metadata_text", lambda text: (text, None))
    arrival = incoming / SERIES / f"{SERIES} Ch. 1-30.cbz"
    arrival.parent.mkdir(parents=True)
    with zipfile.ZipFile(arrival, "w") as zf:
        zf.writestr("00 - Chapter 1.cbz - 001__001.jpg", b"x")
        zf.writestr("29 - Chapter 30.cbz - 010.jpg", b"y")

    watcher.process_and_move_directory(arrival.parent)

    landed, = (library / SERIES).glob("*.cbz")
    assert landed.name == f"{SERIES} Ch. 1-30.cbz"
    with zipfile.ZipFile(landed) as zf:
        info = zf.read("ComicInfo.xml").decode("utf-8")
    assert "<Number>" not in info


# PR #180 review: a mixed-language title is shortened to its Latin segment
# (shorten_mixed_original_title), and the number carried over from the
# other segment must keep both ends of a range.
@pytest.mark.parametrize("token", ["Ch. 1-30", "Vol. 1-3"])
def test_a_range_survives_mixed_title_shortening(token, display):
    """Codex's #180 probe, adapted."""
    parsed = parse_comic_name(Path("C:/intake/Series") / f"Series - \u6f2b\u753b {token}.cbz",
                              rules=cbz_core.ALL_RULES - {"translate"}, display_tokens=display)
    assert parsed.filename == f"Series {token}.cbz"
    assert (parsed.chapter, parsed.volume) == (None, None)


@pytest.mark.parametrize("arrival, expected, chapter", [
    ("Series - \u6f2b\u753b Ch. 5.cbz", "Series Ch. 5.cbz", "5"),
    ("Series - \u6f2b\u753b Ch. 5 - 3 Kittens.cbz", "3 Kittens Ch. 5.cbz", "5"),
])
def test_a_single_number_through_shortening_is_unchanged(arrival, expected, chapter):
    """Pass before and after: only a range keeps its second end."""
    parsed = parse_comic_name(Path("C:/intake/Series") / arrival,
                              rules=cbz_core.ALL_RULES - {"translate"}, display_tokens=True)
    assert (parsed.filename, parsed.chapter) == (expected, chapter)


@pytest.mark.parametrize("token", ["Ch. 1-30", "Vol. 1-3"])
def test_a_shortened_compilation_lands_with_its_range(tmp_path, monkeypatch, token):
    incoming, library = tmp_path / "incoming", tmp_path / "Manga"
    incoming.mkdir()
    library.mkdir()
    registry = tmp_path / "number-styles.json"
    registry.write_text(json.dumps({"version": 1, "roots": [{"path": str(library), "style": "chapter"}]}))
    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(incoming))
    monkeypatch.setattr(watcher, "_routing_rules", [])
    monkeypatch.setattr(watcher, "_routing_default", str(library))
    monkeypatch.setattr(watcher, "_series_resolvers", {})
    monkeypatch.setattr(watcher, "_router", None)
    monkeypatch.setattr(watcher, "_number_style_registry", NumberStyleRegistry.load(registry))
    monkeypatch.setattr(watcher, "_number_style_requested", "auto")
    monkeypatch.setattr(watcher, "POLL_INTERVAL", 0)
    monkeypatch.setattr(watcher, "AI_DECENSOR_ENABLED", False)
    monkeypatch.setattr(watcher, "wait_for_file_stable", lambda *a, **k: True)
    monkeypatch.setattr(cbz_core, "translate_metadata_text", lambda text: (text, None))
    arrival = incoming / "Series" / f"Series - \u6f2b\u753b {token}.cbz"
    arrival.parent.mkdir(parents=True)
    with zipfile.ZipFile(arrival, "w") as zf:
        zf.writestr("page.jpg", b"compilation-pages")

    watcher.process_and_move_directory(arrival.parent)

    landed, = library.rglob("*.cbz")
    assert landed.name == f"Series {token}.cbz"
    with zipfile.ZipFile(landed) as zf:
        info = zf.read("ComicInfo.xml").decode("utf-8")
    assert "<Number>1</Number>" not in info and "<Volume>1</Volume>" not in info
    assert not list((tmp_path / "_review").rglob("*.cbz"))


@pytest.mark.parametrize("text, expected", [
    ("Series / 漫画 Ch. 1-30", "Series Ch. 1-30"),
    ("Series / 漫画 Vol. 1-3", "Series Vol. 1-3"),
    ("Series / 漫画 Ch. 7-3", "Series Ch. 7"),   # descending: as before
    ("Series / 漫画 Vol. 4-2", "Series Vol. 4"),  # descending: as before
    ("One Piece / ワンピース Ch.005", "One Piece Ch.005"),
])
def test_shortening_carries_a_range_but_never_a_descending_tail(text, expected):
    assert cbz_core.shorten_mixed_original_title(text) == expected


# PR #180 review round 2: every place that reads a chapter, volume or issue
# number out of a name, against an ascending range with every separator.
import re as _re
import xml.etree.ElementTree as _ET

_SEPARATORS = ["-", "\u2013", "~", "\u301c", "\uff5e", " - "]
_RANGE_FORMS = [
    "Ch. 1{s}30", "Chapter 1{s}30", "Chapters 1{s}30", "#1{s}30", "Issue 1{s}30",
    "Vol. 1{s}3", "Volume 1{s}3", "v01{s}03", "c001{s}030", "1{s}30",
    "Episode 1{s}30", "Ep. 1{s}30",
    "\u7b2c1{s}30\u8a71", "1{s}30\u8a71", "\u7b2c1{s}30\u7ae0", "\u7b2c1{s}30\u56de",
    "\u7b2c1{s}3\u5dfb", "1{s}3\u5dfb", "1{s}30\ud654", "1{s}3\uad8c",
    "\u7b2c\uff11{s}\uff13\uff10\u8a71", "Ch. \uff11{s}\uff13\uff10",
]
_KEPT = _re.compile(r"[1\uff11]\s*[-\u2013~\u301c\uff5e]\s*0*[3\uff13]")


@pytest.mark.parametrize("prefixed", [True, False], ids=["series-prefixed", "whole-name"])
@pytest.mark.parametrize("sep", _SEPARATORS, ids=["hyphen", "en-dash", "tilde", "wave-dash",
                                                   "fullwidth-tilde", "spaced-hyphen"])
@pytest.mark.parametrize("form", _RANGE_FORMS)
def test_no_reader_takes_an_end_of_a_range(tmp_path, form, sep, prefixed):
    """The audit, pinned: no field is set from either end, the name keeps the
    range, and media naming keeps the legacy name."""
    token = form.format(s=sep)
    stem = f"Series {token}" if prefixed else token
    path = tmp_path / "Series" / f"{stem}.cbz"
    path.parent.mkdir()
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("a.jpg", b"x")
    parsed = parse_comic_name(path, rules=cbz_core.ALL_RULES - {"translate"}, display_tokens=True)
    assert (parsed.chapter, parsed.volume) == (None, None)
    assert cbz_core.extract_chapter_number(stem) is None
    assert cbz_core.extract_volume_number(stem) is None
    assert cbz_core.extract_trailing_bare_number(stem) is None
    assert _KEPT.search(parsed.filename), parsed.filename
    with pytest.raises(KeepArrivalName):
        watcher_naming(parsed, fr.archive_facts_from_file(path), series="Series", style="chapter")


@pytest.mark.parametrize("arrival, expected", [
    ("Chapter 1-30.cbz", "Series Ch. 1-30.cbz"),    # was "Series Ch. 1 30.cbz", chapter 1
    ("Chapter 1 - 30.cbz", "Series Ch. 1 - 30.cbz"),
    ("#1-30.cbz", "Series Ch. 1-30.cbz"),          # was "Series Ch. 1 30.cbz"
    ("Chapters 1-30.cbz", "Series Ch. 1-30.cbz"),  # was "Series.cbz"
    ("1-30.cbz", "1-30.cbz"),                      # was "30.cbz"
    ("Series \u7b2c1~30\u8a71.cbz", "Series \u7b2c1~30\u8a71.cbz"),  # was "Series 第1.cbz"
])
def test_a_whole_name_range_is_kept(tmp_path, arrival, expected):
    path = tmp_path / "Series" / arrival
    path.parent.mkdir()
    path.write_bytes(b"")
    parsed = parse_comic_name(path, rules=cbz_core.ALL_RULES - {"translate"}, display_tokens=True)
    assert parsed.filename == expected and parsed.chapter is None


@pytest.mark.parametrize("arrival, chapter, volume", [
    ("Series Ch. 30.cbz", "30", None), ("Series #30.cbz", "30", None),
    ("Series Vol. 3.cbz", None, "3"), ("Series v03.cbz", None, "3"),
    ("Series \u7b2c30\u8a71.cbz", "30", None), ("Series 30\u8a71.cbz", "30", None),
    ("Series \u7b2c3\u5dfb.cbz", None, "3"), ("Series 3\u5dfb.cbz", None, "3"),
    ("Series \u7b2c\uff11\uff12\u8a71.cbz", "12", None),
    ("Series Ch. 5 - 3 Kittens.cbz", "5", None), ("Series Ch. 7 - 7.cbz", "7", None),
])
def test_single_numbers_are_still_read(arrival, chapter, volume):
    """Pass before and after (Codex's CJK singles among them)."""
    parsed = parse_comic_name(Path("C:/intake/Series") / arrival,
                              rules=cbz_core.ALL_RULES - {"translate"})
    assert (parsed.chapter, parsed.volume) == (chapter, volume)


@pytest.mark.parametrize("arrival, expected", [
    ("01 - The Beginning.cbz", "The Beginning.cbz"),
    ("05 - 3 Kittens.cbz", "3 Kittens.cbz"),     # descending: an index, as before
    ("12 - 12 Angry Men.cbz", "12 Angry Men.cbz"),
])
def test_a_leading_index_is_still_dropped(arrival, expected):
    assert parse_comic_name(Path("C:/intake/Series") / arrival,
                            rules=cbz_core.ALL_RULES - {"translate"}).filename == expected


def test_a_trailing_bare_number_still_names_a_chapter_folder():
    assert cbz_core.extract_trailing_bare_number("Series 2") == "2"
    assert cbz_core.extract_trailing_bare_number("Series 5 - 3") == "3"  # descending: as before


@pytest.mark.parametrize("token", ["\u7b2c1-30\u8a71", "\u7b2c1-3\u5dfb"])
def test_a_cjk_compilation_lands_without_a_number(tmp_path, monkeypatch, token):
    """Codex's round-2 probe, adapted: lands under its name, Number and
    Volume absent or empty."""
    incoming, library = tmp_path / "incoming", tmp_path / "Manga"
    incoming.mkdir()
    library.mkdir()
    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(incoming))
    monkeypatch.setattr(watcher, "_routing_rules", [])
    monkeypatch.setattr(watcher, "_routing_default", str(library))
    monkeypatch.setattr(watcher, "_series_resolvers", {})
    monkeypatch.setattr(watcher, "_router", None)
    monkeypatch.setattr(watcher, "_number_style_registry", None)
    monkeypatch.setattr(watcher, "POLL_INTERVAL", 0)
    monkeypatch.setattr(watcher, "AI_DECENSOR_ENABLED", False)
    monkeypatch.setattr(watcher, "wait_for_file_stable", lambda *a, **k: True)
    monkeypatch.setattr(cbz_core, "translate_metadata_text", lambda text: (text, None))
    arrival = incoming / "Series" / f"Series {token}.cbz"
    arrival.parent.mkdir(parents=True)
    with zipfile.ZipFile(arrival, "w") as zf:
        zf.writestr("page.jpg", b"compilation")

    watcher.process_and_move_directory(arrival.parent)

    landed, = library.rglob("*.cbz")
    assert landed.name == f"Series {token}.cbz"
    with zipfile.ZipFile(landed) as zf:
        info = _ET.fromstring(zf.read("ComicInfo.xml"))
    assert not info.findtext("Number") and not info.findtext("Volume")


# PR #180 review round 3: the name REBUILDERS too, not only the readers.
# A title translation rebuilds the name from the parsed fields, and the
# watcher's fallback names a placeholder from its folder; neither may drop
# a range or reduce it to one end.
def _translate_cjk_titles(text):
    """A deterministic stand-in translator: any CJK title becomes 'Story'."""
    return ("Story", text) if _re.search(r"[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]", text) \
        else (text, None)


@pytest.mark.parametrize("prefixed", [True, False], ids=["series-prefixed", "whole-name"])
@pytest.mark.parametrize("sep", _SEPARATORS, ids=["hyphen", "en-dash", "tilde", "wave-dash",
                                                   "fullwidth-tilde", "spaced-hyphen"])
@pytest.mark.parametrize("form", _RANGE_FORMS)
@pytest.mark.parametrize("display", [False, True], ids=["plain", "display"])
def test_a_translated_title_rebuild_keeps_the_range(tmp_path, monkeypatch, form, sep, prefixed,
                                                     display):
    monkeypatch.setattr(cbz_core, "translate_metadata_text", _translate_cjk_titles)
    token = form.format(s=sep)
    stem = f"\u6f2b\u753b {token}" if prefixed else token
    path = tmp_path / "Series" / f"{stem}.cbz"
    path.parent.mkdir()
    path.write_bytes(b"")
    parsed = parse_comic_name(path, display_tokens=display)
    assert (parsed.chapter, parsed.volume) == (None, None)
    assert _KEPT.search(parsed.filename), parsed.filename


@pytest.mark.parametrize("display", [False, True], ids=["plain", "display"])
def test_a_translated_single_chapter_keeps_its_number(monkeypatch, display):
    """Codex's round-3 probe, adapted: passes before and after."""
    monkeypatch.setattr(cbz_core, "translate_metadata_text", _translate_cjk_titles)
    parsed = parse_comic_name(Path("C:/intake/Series") / "\u6f2b\u753b Ch. 30.cbz",
                              display_tokens=display)
    assert parsed.chapter == "30" and "30" in parsed.filename


def test_two_translated_compilations_land_as_two_books(tmp_path, monkeypatch):
    """Codex's round-3 probe, adapted: '漫画 Ch. 1-30' and '漫画 Ch. 31-60'
    become 'Story Ch. 1-30' and 'Story Ch. 31-60'; neither is reviewed."""
    incoming, library = tmp_path / "incoming", tmp_path / "Manga"
    incoming.mkdir()
    library.mkdir()
    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(incoming))
    monkeypatch.setattr(watcher, "_routing_rules", [])
    monkeypatch.setattr(watcher, "_routing_default", str(library))
    monkeypatch.setattr(watcher, "_series_resolvers", {})
    monkeypatch.setattr(watcher, "_router", None)
    monkeypatch.setattr(watcher, "_number_style_registry", None)
    monkeypatch.setattr(watcher, "POLL_INTERVAL", 0)
    monkeypatch.setattr(watcher, "AI_DECENSOR_ENABLED", False)
    monkeypatch.setattr(watcher, "wait_for_file_stable", lambda *a, **k: True)
    monkeypatch.setattr(cbz_core, "translate_metadata_text", _translate_cjk_titles)
    arrival = incoming / "Series"
    arrival.mkdir()
    for span in ("1-30", "31-60"):
        with zipfile.ZipFile(arrival / f"\u6f2b\u753b Ch. {span}.cbz", "w") as zf:
            zf.writestr("page.jpg", span.encode())

    watcher.process_and_move_directory(arrival)

    names = sorted(p.name for p in library.rglob("*.cbz"))
    assert names == ["Story Ch. 1-30.cbz", "Story Ch. 31-60.cbz"]
    assert not list((tmp_path / "_review").rglob("*.cbz"))


@pytest.mark.parametrize("folder, expected", [
    ("Berserk Ch. 1-30", "Berserk ch. 1-30.cbz"),
    ("Berserk Ch. 1~30", "Berserk ch. 1-30.cbz"),       # the formatter's spelling
    ("Berserk Ch. 1\u301c30", "Berserk ch. 1-30.cbz"),
    ("Berserk 1-30", "Berserk ch. 1-30.cbz"),
    ("Berserk Ch. 4", "Berserk ch. 4.cbz"),  # a single number, as before
])
def test_a_placeholder_in_a_range_folder_is_named_with_the_range(tmp_path, folder, expected):
    """The fallback writer: on master the placeholder took the range's first
    end ('ch. 1'), so two range folders' placeholders differed only by luck;
    now it carries the range, and the chapter field stays unset.

    Lowercase 'ch.' on purpose (David, 2026-11-10): every fallback name
    comes from the canonical formatter, as 'Berserk ch. 4' does on master. A
    separate follow-up PR switches that formatter's display to 'Ch.'."""
    path = tmp_path / folder / "volvox_Chapter.cbz"
    path.parent.mkdir()
    path.write_bytes(b"")
    parsed = parse_comic_name(path, display_tokens=True)
    _name, number = watcher._resolve_series_dir_name(path.parent, [path], str(tmp_path))
    mapping = {path: parsed}
    assert watcher._apply_fallback_naming(mapping, "Berserk", number) == []
    (renamed, patched), = mapping.items()
    assert renamed.name == expected
    if expected != "Berserk ch. 4.cbz":
        assert patched.chapter is None


def test_a_placeholder_in_a_volume_range_folder_is_reviewed(tmp_path):
    """A volume range has no chapter slot: never 'Berserk.cbz' (which two such
    folders would share), never a chapter label; the placeholder is held."""
    path = tmp_path / "Berserk Vol. 1-3" / "volvox_Chapter.cbz"
    path.parent.mkdir()
    path.write_bytes(b"")
    parsed = parse_comic_name(path, display_tokens=True)
    _name, number = watcher._resolve_series_dir_name(path.parent, [path], str(tmp_path))
    mapping = {path: parsed}
    assert watcher._apply_fallback_naming(mapping, "Berserk", number) == [path]
    assert list(mapping) == [path]


@pytest.mark.parametrize("arrival, expected", [
    ("\u6f2b\u753b Vol. 1-3.cbz", "Story Vol. 1-3.cbz"),
    ("\u6f2b\u753b \u7b2c1-3\u5dfb.cbz", "Story Vol. 1-3.cbz"),
    ("\u6f2b\u753b \u7b2c1-30\u8a71.cbz", "Story Ch. 1-30.cbz"),
    ("\u6f2b\u753b 1-30.cbz", "Story 1-30.cbz"),
])
def test_a_translated_rebuild_keeps_the_ranges_kind(monkeypatch, arrival, expected):
    monkeypatch.setattr(cbz_core, "translate_metadata_text", _translate_cjk_titles)
    assert parse_comic_name(Path("C:/intake/Series") / arrival).filename == expected
