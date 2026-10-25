"""Malformed number tokens never crash the sanitizer, resolver or gap checker.

Follow-up to #126 (issue #120). #126 made ``scripts/cbz_core.py`` total over
number tokens like ``ch. 1.2.3`` or ``v1..2``: ``_num_field`` fullmatches one
decimal number and gives ``None`` for anything else, and ``_fmt_num_text``
keeps such a token as the text it arrived as. Three other scripts parse the
same ``\\d[\\d.]*`` captures out of filenames with a bare ``float()``:

* ``cbz_sanitizer`` -- ``extract_chapter`` runs on every renamed file in
  ``_process_series_dir`` (core available or not), so one ``ch. 1.2.3``
  aborted its whole series directory with ``ValueError``; a chapter long
  enough to overflow came back ``inf`` and then raised ``OverflowError`` in
  compilation detection. The fallback (no ``cbz_core``) path also ran
  ``extract_chapter_number`` / ``extract_volume_number`` /
  ``normalise_number_tokens`` / the CJK fallback over raw tokens.
* ``cbz_compilation_resolver`` -- ``extract_chapter_number`` and
  ``extract_compilation_range`` in ``scan_directory``; an ``inf`` range end
  made ``chapter_range`` loop forever.
* ``cbz_gap_checker`` -- ``extract_number`` in ``scan_series``; an ``inf``
  number made ``find_gaps`` loop forever.

Pinned here, through each script's public entry points on tmp CBZ files:
no crash, the malformed token stays in the name as text, no number is
invented from it (field / ComicInfo ``<Number>`` unset), and well-formed
tokens behave exactly as before.

The overflow token (``HUGE``, 400 digits) cannot occur in a real filename:
NTFS and ext4 both cap a path component at 255 characters, and ``float``
only overflows past ~309 digits. It is still pinned at the string-level
extractors -- they are public and total by contract, as in core -- but not
through the file-based entry points. The two infinite-loop cases it caused
(resolver ``chapter_range``, gap checker ``find_gaps``) are pinned at the
extractor too, so a regression fails instead of hanging the suite.
"""

from __future__ import annotations

import importlib.util
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest

from scripts import cbz_compilation_resolver as resolver
from scripts import cbz_gap_checker as gap_checker
from scripts import cbz_sanitizer as sanitizer

HUGE = "9" * 400  # float() gives inf; int(inf) raises OverflowError


def _short_id(value):
    """Keep the 400-digit parameter out of test IDs."""
    if isinstance(value, str) and HUGE in value:
        return value.replace(HUGE, "HUGE")
    return None


# The three Tower-shaped names plus the trailing-dot variants #126 pinned in
# core. No HUGE here: a 400-character name is not a valid file (see above).
MALFORMED_NAMES = [
    ("Series ch. 1.2.3.cbz", "1.2.3"),
    ("Series v1..2 #003.cbz", "1..2"),
    ("Series 12.5.1 (2019).cbz", "12.5.1"),
    ("Series ch. 1.2..cbz", "1.2."),
    ("Series ch. 1...cbz", "1.."),
]


def _make_cbz(path: Path, pages: int = 1, comicinfo: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for i in range(pages):
            zf.writestr(f"{i + 1:03d}.jpg", b"\xff\xd8page" + bytes([i]))
        if comicinfo is not None:
            zf.writestr("ComicInfo.xml", comicinfo)
    return path


def _comicinfo(cbz_path: Path) -> dict[str, str]:
    with zipfile.ZipFile(cbz_path) as zf:
        name = next(
            (n for n in zf.namelist() if n.lower().endswith("comicinfo.xml")), None
        )
        if name is None:
            return {}
        root = ElementTree.fromstring(zf.read(name))
    return {child.tag: (child.text or "") for child in root}


def _file_holding(folder: Path, token: str) -> Path:
    matches = [p for p in folder.iterdir() if token in p.name]
    assert len(matches) == 1, (token, sorted(p.name for p in folder.iterdir()))
    return matches[0]


# ── cbz_sanitizer: extractors ────────────────────────────────────


@pytest.mark.parametrize(
    "stem",
    ids=_short_id,
    argvalues=["Series ch. 1.2.3", "Series ch. 1.2.", "Series ch. 1..", f"Series ch. {HUGE}",
     "Series #1.2.3", "Series Ep. 1..2"],
)
def test_sanitizer_chapter_extractors_leave_a_malformed_token_unset(stem):
    assert sanitizer.extract_chapter_number(stem) is None
    assert sanitizer.extract_chapter(stem) is None


@pytest.mark.parametrize(
    "stem",
    ids=_short_id,
    argvalues=["Series v1..2 #003", "Series Vol. 1.2.3", "Series Vol. 3..", f"Series Vol. {HUGE}"],
)
def test_sanitizer_volume_extractor_leaves_a_malformed_token_unset(stem):
    assert sanitizer.extract_volume_number(stem) is None


@pytest.mark.parametrize(
    "stem, chapter, volume",
    [
        ("Series v1..2 #003", "3", None),
        ("Series ch. 12.5", "12.5", None),
        ("Series ch. 005", "5", None),
        ("Series ch. 12.", "12", None),
        ("Series Vol. 03 ch. 7", "7", "3"),
        ("Series v2 ch. 1", "1", "2"),
    ],
)
def test_sanitizer_extractors_keep_well_formed_tokens(stem, chapter, volume):
    assert sanitizer.extract_chapter_number(stem) == chapter
    assert sanitizer.extract_volume_number(stem) == volume
    assert sanitizer.extract_chapter(stem) == (
        None if chapter is None else float(chapter)
    )


@pytest.mark.parametrize(
    "stem",
    ids=_short_id,
    argvalues=["Series ch. 1.2.3", "Series v1..2 #003", "Series ch. 1.2.",
     f"Series ch. {HUGE}", f"Series Vol. {HUGE} ch. 2"],
)
def test_sanitizer_normalise_number_tokens_keeps_a_malformed_token_as_text(stem):
    out = sanitizer.normalise_number_tokens(stem)
    for token in ("1.2.3", "1..2", "1.2.", HUGE):
        if token in stem:
            assert token in out, (stem, out)


def test_sanitizer_normalise_number_tokens_still_formats_good_tokens():
    assert sanitizer.normalise_number_tokens("Series ch. 005 v02") == "Series ch. 5 v2"
    assert sanitizer.normalise_number_tokens("Series ch. 12.50") == "Series ch. 12.5"


@pytest.mark.parametrize(
    "value, expected",
    ids=_short_id,
    argvalues=[("005", "5"), ("12.50", "12.5"), ("1.2.3", "1.2.3"), (HUGE, HUGE)],
)
def test_sanitizer_format_chapter_number_is_total(value, expected):
    assert sanitizer.format_chapter_number(value) == expected


@pytest.mark.parametrize(
    "stem, chapter, volume",
    ids=_short_id,
    argvalues=[
        ("進撃の巨人 第1.2.3話", None, None),
        ("進撃の巨人 12.5話", "12.5", None),
        ("進撃の巨人 第1..巻", None, None),
        (f"進撃の巨人 第{HUGE}話", None, None),
        ("進撃の巨人 第12話", "12", None),
        ("進撃の巨人 第3巻", None, "3"),
        ("進撃の巨人 12.5巻", None, "12.5"),
        ("進撃の巨人 第1.2.3巻", None, None),
        # A run that starts with a dot: "5" is a fragment of ".5", not a number.
        ("進撃の巨人 .5話", None, None),
        ("進撃の巨人 .5巻", None, None),
    ],
)
def test_sanitizer_cjk_fallback_takes_the_whole_run_or_nothing(
    monkeypatch, stem, chapter, volume
):
    """The CJK fallback used ``\\d+``, so ``第1.2.3話`` yielded the fragment
    ``3`` and ``12.5話`` yielded ``5`` -- the invented-number case #126 fixed
    in core. It now takes the whole digit-and-dot run through _num_field."""
    monkeypatch.setattr(sanitizer, "_is_cjk_only", lambda text: True)
    monkeypatch.setattr(sanitizer, "_translate_cjk_text", lambda text: ("Titan", text))
    _english, _original, got_chapter, got_volume = sanitizer._resolve_cjk_title(stem)
    assert (got_chapter, got_volume) == (chapter, volume)


# ── cbz_sanitizer: directory entry points ────────────────────────


@pytest.fixture
def no_progress_file(monkeypatch):
    monkeypatch.setattr(sanitizer, "save_progress", lambda path: None)


SANITIZE_RULES = sanitizer.ALL_RULES - {"translate"}


@pytest.mark.parametrize("name, token", MALFORMED_NAMES)
@pytest.mark.parametrize("workers", [1, 2])
def test_sanitize_directory_survives_a_malformed_token(
    tmp_path, no_progress_file, name, token, workers
):
    """Serially the ValueError escaped sanitize_directory; in parallel the
    worker swallowed it and the rest of the series directory went
    unprocessed. Either way the files sorted after the bad one stayed dirty.
    """
    series = tmp_path / "Lib" / "Series"
    _make_cbz(series / name)
    _make_cbz(series / "Series ch. 1.cbz")
    _make_cbz(series / "Series_ch_2.cbz")  # sorts last; must still be cleaned

    sanitizer.sanitize_directory(
        tmp_path / "Lib", set(), "start", workers, rules=SANITIZE_RULES
    )

    names = sorted(p.name for p in series.iterdir())
    assert "Series_ch_2.cbz" not in names, names
    landed = _file_holding(series, token)
    _assert_no_invented_number(_comicinfo(landed), token)


def _assert_no_invented_number(info: dict[str, str], token: str) -> None:
    if token == "1..2":
        # "v1..2 #003": the chapter is the well-formed #003; only the volume
        # token is malformed, so Volume must stay unset.
        assert info.get("Number") == "3", info
        assert not info.get("Volume"), info
    else:
        assert not info.get("Number"), info
        assert not info.get("Volume"), info


def test_detect_and_fix_compilations_skips_a_malformed_sibling(tmp_path):
    """The ValueError used to escape here before any compilation was looked
    at; the real compilation (ch. 12 beside 1-3 reads as 1-2) must still be
    split."""
    series = tmp_path / "Series"
    for n in ("1", "2", "3", "12"):
        _make_cbz(series / f"Series ch. {n}.cbz")
    _make_cbz(series / "Series ch. 1.2.3.cbz")
    assert sanitizer.detect_and_fix_compilations(series) == 1
    assert (series / "Series ch. 1-2.cbz").exists()
    assert (series / "Series ch. 1.2.3.cbz").exists()


# ── cbz_sanitizer without cbz_core (its documented fallback) ────


@pytest.fixture
def sanitizer_without_core(monkeypatch):
    """A fresh copy of cbz_sanitizer whose cbz_core import fails, so it runs
    on its own fallbacks -- the path where the legacy rename and ComicInfo
    code (normalise_number_tokens, extract_*_number) is live."""
    monkeypatch.setitem(sys.modules, "scripts.cbz_core", None)
    monkeypatch.setitem(sys.modules, "cbz_core", None)
    spec = importlib.util.spec_from_file_location(
        "cbz_sanitizer_without_core", sanitizer.__file__
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._parse_comic_name is None, "cbz_core was not blocked"
    monkeypatch.setattr(module, "save_progress", lambda path: None)
    return module


@pytest.mark.parametrize("name, token", MALFORMED_NAMES)
def test_sanitizer_fallback_path_survives_a_malformed_token(
    tmp_path, sanitizer_without_core, name, token
):
    module = sanitizer_without_core
    series = tmp_path / "Lib" / "Series"
    _make_cbz(series / name)
    _make_cbz(series / "Series ch. 1.cbz")

    module.sanitize_directory(
        tmp_path / "Lib", set(), "start", 1, rules=module.ALL_RULES - {"translate"}
    )

    landed = _file_holding(series, token)
    _assert_no_invented_number(_comicinfo(landed), token)
    assert _comicinfo(series / "Series ch. 1.cbz").get("Number") == "1"


@pytest.mark.parametrize(
    "value, expected",
    ids=_short_id,
    argvalues=[("005", "5"), ("12.50", "12.5"), ("12.", "12"), ("1.2.3", None),
               ("1..", None), ("1.2.", None), (HUGE, None)],
)
def test_sanitizer_fallback_extractors_match_core_contract(
    sanitizer_without_core, value, expected
):
    """The fallback _num_field twin, through the fallback module's public
    extractors -- including the overflowing run, which no filename carries."""
    module = sanitizer_without_core
    assert module.extract_chapter_number(f"Series ch. {value}") == expected
    assert module.extract_volume_number(f"Series Vol. {value}") == expected
    assert module.normalise_number_tokens(f"Series ch. {value}") == (
        f"Series ch. {value if expected is None else expected}"
    )


def test_sanitizer_fallback_number_field_matches_core():
    from scripts import cbz_core

    for value in ("005", "12.50", "12.", "1.2.3", "1..", "1.2.", HUGE, "7"):
        # The fallback module copy is exercised above; here the core helper
        # the sanitizer imports must be the one it calls.
        assert sanitizer._num_field(value) == cbz_core._num_field(value)
    assert sanitizer._num_field is cbz_core._num_field


# ── cbz_compilation_resolver ─────────────────────────────────────


@pytest.mark.parametrize(
    "stem",
    ids=_short_id,
    argvalues=["Series ch. 1.2.3", "Series ch. 1.2.", "Series ch. 1..", f"Series ch. {HUGE}",
     "Series #1.2.3"],
)
def test_resolver_chapter_extractor_leaves_a_malformed_token_unset(stem):
    assert resolver.extract_chapter_number(stem) is None


@pytest.mark.parametrize(
    "stem",
    ids=_short_id,
    argvalues=["Series ch. 1.2.3-5", "Series ch. 1-2.3.4", "Series ch. 1..-3",
     f"Series ch. 1-{HUGE}", f"Series ch. {HUGE}-{HUGE}"],
)
def test_resolver_range_extractor_rejects_a_malformed_endpoint(stem):
    """An inf end also made CompilationArchive.chapter_range loop forever,
    which is why this is pinned at the extractor rather than only through
    scan_directory: a regression must fail, not hang."""
    assert resolver.extract_compilation_range(stem) is None


@pytest.mark.parametrize(
    "stem, expected",
    [("Series ch. 1-3", (1.0, 3.0)), ("Series ch. 1.5-2.5", (1.5, 2.5)),
     ("Series ch. 01 - 03.", (1.0, 3.0))],
)
def test_resolver_range_extractor_keeps_well_formed_ranges(stem, expected):
    assert resolver.extract_compilation_range(stem) == expected


@pytest.mark.parametrize("name, token", MALFORMED_NAMES)
def test_resolver_process_directory_survives_a_malformed_sibling(
    tmp_path, name, token
):
    series = tmp_path / "Series"
    _make_cbz(series / "Series ch. 1-2.cbz", pages=2)
    _make_cbz(series / "Series ch. 1.cbz")
    _make_cbz(series / "Series ch. 2.cbz")
    _make_cbz(series / name)

    groups = resolver.scan_directory(series)
    assert len(groups) == 1
    assert [c.path.name for c in groups[0].individuals] == [
        "Series ch. 1.cbz", "Series ch. 2.cbz"
    ]
    assert groups[0].missing == []
    assert resolver.process_directory(series, dry_run=True) == 1
    assert _file_holding(series, token).name == name


def test_resolver_v1_dot_dot_still_reads_the_hash_chapter(tmp_path):
    series = tmp_path / "Series"
    _make_cbz(series / "Series ch. 3-4.cbz", pages=2)
    _make_cbz(series / "Series v1..2 #003.cbz")
    _make_cbz(series / "Series ch. 4.cbz")
    groups = resolver.scan_directory(series)
    assert [c.path.name for c in groups[0].individuals] == [
        "Series v1..2 #003.cbz", "Series ch. 4.cbz"
    ]


def test_resolver_malformed_compilation_is_not_a_compilation(tmp_path):
    series = tmp_path / "Series"
    _make_cbz(series / "Series ch. 1.2.3-5.cbz", pages=3)
    for n in ("3", "4", "5"):
        _make_cbz(series / f"Series ch. {n}.cbz")
    assert resolver.scan_directory(series) == []
    assert resolver.process_directory(series, dry_run=True) == 0


# ── cbz_gap_checker ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "stem",
    ids=_short_id,
    argvalues=["Series ch. 1.2.3", "Series ch. 1.2.", "Series #1..2", "Series issue 1.2.3",
     f"Series ch. {HUGE}"],
)
def test_gap_checker_extract_number_leaves_a_malformed_token_unset(stem):
    """An inf here also made find_gaps loop forever (``while n < hi``)."""
    assert gap_checker.extract_number(stem) is None


@pytest.mark.parametrize(
    "stem, expected",
    [("Series ch. 12.5", 12.5), ("Series ch. 12.", 12.0), ("Series #003", 3.0),
     ("Series 0042", 42.0), ("Series v1..2 #003", 3.0)],
)
def test_gap_checker_extract_number_keeps_well_formed_tokens(stem, expected):
    assert gap_checker.extract_number(stem) == expected


def test_gap_checker_scan_series_counts_malformed_names_as_unnumbered(tmp_path):
    series = tmp_path / "Series"
    for name, _token in MALFORMED_NAMES:
        if HUGE not in name:
            _make_cbz(series / name)
    _make_cbz(series / "Series ch. 1.cbz")
    _make_cbz(series / "Series ch. 5.cbz")

    report = gap_checker.scan_series(series)

    assert report is not None
    # Numbered: ch. 1, ch. 5 and the "#003" of "v1..2 #003". The other four
    # malformed names carry no number the checker may use.
    assert report["total_found"] == 3
    assert report["unnumbered"] == 4
    assert report["missing"] == "2, 4"
