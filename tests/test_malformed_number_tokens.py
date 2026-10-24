"""Malformed number tokens never raise out of name parsing (issue #120).

The number regexes in ``scripts/cbz_core.py`` capture ``\\d[\\d.]*``, so a
chapter, volume or episode token can carry more than one dot
(``Ch. 1.2.3``, ``Ch. 12.5012.5``) or be long enough to overflow ``float``
to ``inf``. On master ``_fmt_num_text`` raised ``ValueError`` /
``OverflowError`` for these, out of ``parse_comic_name`` and out of the
watcher's per-file loop, which catches only ``AiDecensorError`` -- so one
such arrival aborted its whole folder.

What is pinned here:

* through ``parse_comic_name``: the name keeps the malformed token as the
  text it arrived as, the matching field is ``None`` (no number is guessed
  out of it), and the result is a fixpoint;
* well-formed tokens are formatted exactly as before (controls);
* through the watcher's directory pass with real tmp CBZ files: a
  malformed sibling neither aborts the folder nor gets a ComicInfo
  ``<Number>`` invented for it.

Every fixture lives under ``tmp_path``; translation is stubbed.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest

from scripts import cbz_core
from scripts import cbz_watcher as watcher

NO_TRANSLATE = cbz_core.ALL_RULES - {"translate"}
HUGE = "9" * 400  # float() gives inf, int(inf) raised OverflowError


def _parse(name: str, folder: str = "Series", **kwargs):
    path = Path("library") / folder / name
    return cbz_core.parse_comic_name(path, rules=NO_TRANSLATE, **kwargs)


@pytest.mark.parametrize(
    "name, field",
    [
        ("Series Ch. 1.2.3.cbz", "chapter"),
        ("Series Ch. 12.5012.5.cbz", "chapter"),
        ("Series Ch. 1..2.cbz", "chapter"),
        (f"Series Ch. {HUGE}.cbz", "chapter"),
        ("Series Vol. 1.2.3.cbz", "volume"),
        ("Series v1.2.3 Ch. 4.cbz", "volume"),
        ("Series Ep. 1.2.3.cbz", "episode"),
        ("Series Season 1.2.3.cbz", "season"),
        ("Series Part 1.2.3.cbz", "part"),
        (f"Series 第{HUGE}話.cbz", "chapter"),
        (f"Series 第{HUGE}巻.cbz", "volume"),
        # More than one dot counting the trailing one (#126 review): rstrip
        # used to accept "1.." as 1 and "1.2." as 1.2.
        ("Series Ch. 1...cbz", "chapter"),
        ("Series Ch. 1.2..cbz", "chapter"),
        ("Series Ch. 1.2...cbz", "chapter"),
        ("Series Vol. 3...cbz", "volume"),
        ("Series #1.2..cbz", "chapter"),
        ("Series Ep. 1...cbz", "episode"),
        ("Series Part 1.2..cbz", "part"),
        ("Series S1..E4.cbz", "season"),
        ("Series 第1..話.cbz", "chapter"),
        ("Series 第1.2.話.cbz", "chapter"),
        ("Series 1.2.話.cbz", "chapter"),
        ("Series 第1.2.巻.cbz", "volume"),
    ],
)
@pytest.mark.parametrize("display_tokens", [False, True])
def test_malformed_token_is_kept_as_text_and_leaves_the_field_unset(
    name, field, display_tokens
):
    parsed = _parse(name, display_tokens=display_tokens)
    assert getattr(parsed, field) is None, (name, field)
    again = _parse(parsed.filename, display_tokens=display_tokens)
    assert again.filename == parsed.filename


@pytest.mark.parametrize(
    "name",
    [
        "Series Ch. 1.2.3.cbz",
        "Series Ch. 12.5012.5.cbz",
        "Series Vol. 1.2.3.cbz",
        "Series Ch. 1...cbz",
        "Series Ch. 1.2..cbz",
        "Series 第1.2.話.cbz",
    ],
)
def test_malformed_token_survives_verbatim_in_the_filename(name):
    """The issue's examples come back unchanged: the token is neither dropped
    nor rewritten into a number it never was."""
    assert _parse(name, display_tokens=True).filename == name


def test_generic_chapter_stem_with_a_malformed_token_is_named_from_the_folder():
    """``normalize_stem``'s generic-chapter branch used the same formatter and
    raised too; it now builds the name with the token as text."""
    parsed = _parse("Chapter 1.2.3.cbz", display_tokens=True)
    assert parsed.filename == "Series Ch. 1.2.3.cbz"
    assert parsed.chapter is None


@pytest.mark.parametrize(
    "name, chapter, volume",
    [
        ("Series Ch. 12.5.cbz", "12.5", None),
        ("Series Ch. 005.cbz", "5", None),
        ("Series Ch. 12.50.cbz", "12.5", None),
        # One trailing dot is still a number (the documented "12." case).
        ("Series Ch. 12..cbz", "12", None),
        ("Series Vol. 3. Ch. 4.cbz", "4", "3"),
        ("Series 第12.話.cbz", "12", None),
        ("Series Vol. 03 Ch. 7.cbz", "7", "3"),
        ("Series Vol. 1.2.3 Ch. 4.cbz", "4", None),
    ],
)
def test_well_formed_tokens_are_formatted_as_before(name, chapter, volume):
    parsed = _parse(name)
    assert (parsed.chapter, parsed.volume) == (chapter, volume)


_NUMBER_FIELDS = ("chapter", "volume", "season", "episode", "part")


@pytest.mark.parametrize(
    "name, expected",
    [
        # CJK fallbacks: on 740045f (and on master for the forms without 第)
        # these took the trailing fragment "3" of "1.2.3" as the number.
        ("Series 第1.2.3話.cbz", {}),
        ("Series 1.2.3話.cbz", {}),
        ("Series 1.2.3话.cbz", {}),
        ("Series 1.2.3章.cbz", {}),
        ("Series 第1.2.3巻.cbz", {}),
        ("Series 1.2.3巻.cbz", {}),
        ("Series 1.2.3卷.cbz", {}),
        # A run that starts with a dot: "5" is a fragment of ".5".
        ("Series .5話.cbz", {}),
        ("Series 第.5巻.cbz", {}),
        # Latin markers and bare runs: no fragment of the run is picked up.
        ("Series 1.2.3.cbz", {}),
        ("Series #1.2.3.cbz", {}),
        ("Series Vol 1.2.3.cbz", {}),
        ("Series v1.2.3.cbz", {}),
        ("Series Part 1.2.3.cbz", {}),
        ("Series Pt 1.2.3.cbz", {}),
        ("Series p1.2.3.cbz", {}),
        ("Series E1.2.3.cbz", {}),
        ("Series s1.2.3.cbz", {}),
        ("Series Ch.1.2.3.4.cbz", {}),
        # The season run is malformed and stays unset; E4 is its own marker
        # token (as in S01E04), not a fragment of the run, so it is kept.
        ("Series S1.2.3E4.cbz", {"episode": "4"}),
    ],
)
def test_no_number_is_taken_from_a_fragment_of_a_malformed_run(name, expected):
    parsed = _parse(name)
    found = {f: getattr(parsed, f) for f in _NUMBER_FIELDS if getattr(parsed, f)}
    assert found == expected, name


@pytest.mark.parametrize(
    "name, chapter, volume",
    [
        # Controls: the CJK fallback still reads well-formed markers, and now
        # reads a fractional one whole ("12.5", where it used to give "5").
        ("Series 第3話.cbz", "3", None),
        ("Series 第 03 话.cbz", "3", None),
        ("Series 12.5話.cbz", "12.5", None),
        ("Series 2.5巻.cbz", None, "2.5"),
        ("Series 2巻 第5話.cbz", "5", "2"),
    ],
)
def test_cjk_fallback_reads_the_whole_number_run(name, chapter, volume):
    parsed = _parse(name)
    assert (parsed.chapter, parsed.volume) == (chapter, volume)


@pytest.mark.parametrize(
    "name", ["Series 1.2.3", "Series - 12.5012.5", "Series_1..2", "Series 1..", "Series 1.2."]
)
def test_trailing_bare_number_takes_no_fragment_of_a_malformed_run(name):
    assert cbz_core.extract_trailing_bare_number(name) is None


def test_trailing_bare_number_overflow_is_none():
    assert cbz_core.extract_trailing_bare_number(f"Series {HUGE}") is None
    assert cbz_core.extract_trailing_bare_number("Series 02") == "2"


def test_normalise_number_tokens_keeps_a_malformed_token_and_formats_the_rest():
    assert (
        cbz_core.normalise_number_tokens("Ch. 1.2.3 Vol. 004")
        == "Ch. 1.2.3 Vol. 4"
    )


# ---------------------------------------------------------------------------
# Through the watcher's directory pass
# ---------------------------------------------------------------------------


def _make_cbz(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("page1.jpg", b"\0" * 64)


def _comicinfo_number(cbz_path: Path) -> str | None:
    with zipfile.ZipFile(cbz_path) as zf:
        xml = zf.read("ComicInfo.xml").decode("utf-8")
    element = ElementTree.fromstring(xml).find("Number")
    if element is None:
        return None
    return (element.text or "").strip() or None


@pytest.fixture
def watch(tmp_path, monkeypatch):
    """A tmp watch folder and library wired into the watcher, offline."""
    watch_root = tmp_path / "incoming"
    library = tmp_path / "library"
    watch_root.mkdir()
    library.mkdir()
    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(watch_root))
    monkeypatch.setattr(watcher, "_routing_rules", [])
    monkeypatch.setattr(watcher, "_routing_default", str(library))
    monkeypatch.setattr(watcher, "POLL_INTERVAL", 0)
    monkeypatch.setattr(watcher, "wait_for_file_stable", lambda *a, **k: True)
    monkeypatch.setattr(cbz_core, "translate_metadata_text", lambda text: (text, None))
    return watch_root, library


@pytest.mark.parametrize("bad", ["Series Ch. 1.2.3.cbz", "Series Ch. 12.5012.5.cbz"])
def test_a_malformed_sibling_does_not_abort_its_folder(watch, bad):
    """The arrival loop processes files in sorted order; the malformed name
    sits between two good ones, so an exception would leave at least the
    later sibling unprocessed in the watch folder (on master the pass raised
    out of ``process_and_move_directory``)."""
    watch_root, library = watch
    arrival = watch_root / "Series"
    names = ["Series Ch. 1.cbz", bad, "Series Ch. 2.cbz"]
    for name in names:
        _make_cbz(arrival / name)

    watcher.process_and_move_directory(arrival)

    dest = library / "Series"
    assert sorted(p.name for p in dest.iterdir()) == sorted(names)
    assert not list(watch_root.rglob("*.cbz"))
    assert _comicinfo_number(dest / "Series Ch. 1.cbz") == "1"
    assert _comicinfo_number(dest / "Series Ch. 2.cbz") == "2"
    # No number is invented from the malformed token.
    assert _comicinfo_number(dest / bad) is None


# ---------------------------------------------------------------------------
# What reaches ComicInfo (#126 review): multi-dot runs counting the trailing
# dot ("1..", "1.2.") write no <Number>/<Volume>; controls write exact values.
# ---------------------------------------------------------------------------

_WRITTEN_CASES = [
    # name, expected <Number>, expected <Volume>
    ("Series 第1..話.cbz", None, None),
    ("Series 第1.2.話.cbz", None, None),
    ("Series 第1..巻.cbz", None, None),
    ("Series 第1.2.巻.cbz", None, None),
    ("Series Ch. 1...cbz", None, None),
    ("Series Ch. 1.2..cbz", None, None),
    ("Series Vol. 3...cbz", None, None),
    ("Series Vol. 1.2..cbz", None, None),
    ("Series #1.2..cbz", None, None),
    # Controls: well-formed, fractional, full-width and single-trailing-dot.
    ("Series 第12話.cbz", "12", None),
    ("Series 第3巻.cbz", None, "3"),
    ("Series 12.5話.cbz", "12.5", None),
    ("Series 第１２話.cbz", "12", None),
    ("Series Ch. 12..cbz", "12", None),
    ("Series Vol. 3. Ch. 4.cbz", "4", "3"),
]


def _field_text(xml_text: str, tag: str) -> str | None:
    """Text of *tag*, or None when it is absent or empty. The watcher's
    template carries an empty ``<Number></Number>``, so "not written" is
    an empty element there and a missing one from a bare ``<ComicInfo/>``."""
    element = ElementTree.fromstring(xml_text).find(tag)
    if element is None:
        return None
    return (element.text or "").strip() or None


@pytest.mark.parametrize("name, number, volume", _WRITTEN_CASES)
def test_update_comicinfo_writes_only_well_formed_numbers(name, number, volume):
    parsed = _parse(name)
    xml_text, _changed = cbz_core.update_comicinfo_xml("<ComicInfo/>", parsed)
    root = ElementTree.fromstring(xml_text)
    if number is None:
        assert root.find("Number") is None, xml_text
    else:
        assert root.findtext("Number") == number
    if volume is None:
        assert root.find("Volume") is None, xml_text
    else:
        assert root.findtext("Volume") == volume


@pytest.mark.parametrize("name, number, volume", _WRITTEN_CASES)
def test_watcher_writes_only_well_formed_numbers_into_the_cbz(watch, name, number, volume):
    """End to end: one arrival per case (so no two land on the same name),
    processed by the watcher; the ComicInfo.xml inside the archive that
    reaches the library is read back."""
    watch_root, library = watch
    arrival = watch_root / "Series"
    _make_cbz(arrival / name)

    watcher.process_and_move_directory(arrival)

    landed = list(library.rglob("*.cbz"))
    assert len(landed) == 1, landed
    with zipfile.ZipFile(landed[0]) as zf:
        xml_text = zf.read("ComicInfo.xml").decode("utf-8")
    assert _field_text(xml_text, "Number") == number
    assert _field_text(xml_text, "Volume") == volume
