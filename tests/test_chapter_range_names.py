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
