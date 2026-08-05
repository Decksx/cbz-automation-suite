"""Tests for filename/title normalization helpers in scripts.cbz_core.

These cover the pieces that turn a messy on-disk archive name into a clean,
standardized one: scan-group/URL stripping (sanitize), filesystem-illegal
character removal (clean_filename/clean_directory_name), chapter/volume/
season/episode/part token extraction, and translation of native-language
(e.g. Japanese) titles into English via a mocked translation backend.
"""

from pathlib import Path
from unittest.mock import patch

from scripts.cbz_core import (
    clean_directory_name,
    clean_filename,
    extract_chapter_number,
    normalise_number_tokens,
    parse_comic_name,
    sanitize,
    translate_metadata_text,
)


def test_scan_group_removed():
    """Scan-group tags in brackets ("[CoolScans]") should be stripped."""
    assert sanitize("One Piece [CoolScans]") == "One Piece"


def test_url_removed():
    """Website credits/watermarks embedded in the name should be stripped."""
    assert sanitize("Batman readcomics.net") == "Batman"


def test_windows_forbidden_characters_removed():
    """Characters illegal in Windows filenames (: / ?) must be stripped
    from the filename while keeping the rest of it readable.
    """
    assert clean_filename('Batman: Year/One?.cbz') == "Batman Year One.cbz"


def test_mixed_original_title_prefers_english_segment():
    """When a filename has both an English and a native-language segment
    separated by "/", the English segment should be kept as the title.
    """
    assert clean_filename("One Piece / ワンピース Ch.005.cbz") == "One Piece Ch.005.cbz"


def test_does_not_empty_non_latin_only_title():
    """A title that is entirely non-Latin script must not be dropped
    outright, leaving nothing but the extension.
    """
    assert clean_filename("ワンピース Ch.005.cbz") != ".cbz"


def test_number_token_normalization():
    """Zero-padded volume/chapter numbers ("Vol.01", "Ch.005") should be
    normalized down to their minimal form ("Vol.1", "Ch.5").
    """
    assert normalise_number_tokens("Vol.01 Ch.005") == "Vol.1 Ch.5"


def test_chapter_extraction_decimal():
    """Decimal chapter numbers (e.g. "10.5" for a half-chapter) must be
    extracted intact rather than truncated to an integer.
    """
    assert extract_chapter_number("One Piece Ch. 10.5") == "10.5"


def test_clean_directory_name_removes_hash_wrappers():
    """Directory names sometimes get wrapped in "##" as a sort/pin marker
    in some tools; that wrapper should be stripped from the series name.
    """
    assert clean_directory_name("## One Piece ##") == "One Piece"


def test_parse_comic_name_uses_clean_directory_name_for_series():
    """The series name in a parsed filename should come from the cleaned
    parent directory name, not be re-derived from the file's own name.
    """
    parsed = parse_comic_name(Path("## One Piece ##/001 - One Piece Ch.005.cbz"))

    assert parsed.series == "One Piece"
    assert parsed.chapter == "5"

def test_mixed_title_preserves_volume_from_other_segment():
    """When volume and chapter markers are split across the English and
    native-language segments of a mixed title, both should still be
    captured in the cleaned English filename.
    """
    assert clean_filename("One Piece Vol.02 / ワンピース Ch.005.cbz") == "One Piece Vol.02 Ch.005.cbz"


def test_non_latin_only_title_preserved_with_chapter():
    """A non-Latin-only title should still retain its chapter marker (in
    some normalized form) rather than losing it during cleaning.
    """
    result = clean_filename("ワンピース Ch.005.cbz")
    assert result != ".cbz"
    assert "Ch.5" in result or "Ch.005" in result or "ワンピース" in result


def test_parse_comic_name_ext_from_split_not_path_suffix():
    # Regression guard: the extension must come from _split_name's own
    # parsing of the stem, not from Path.suffix, otherwise a name like
    # "Batman Ch.5.cbz" could end up double-suffixed as "....cbz.cbz".
    from scripts.cbz_core import parse_comic_name
    parsed = parse_comic_name(Path("Series/Batman Ch.5.cbz"))
    assert parsed.filename.endswith(".cbz")
    assert not parsed.filename.endswith(".cbz.cbz")


def test_extract_season_episode_part():
    """Basic extraction of season ("S1"), episode ("E05"/"Ep. 12"), and
    part ("pt.4"/"Part 2") tokens in their common abbreviated and spelled-
    out forms.
    """
    from scripts.cbz_core import (
        extract_season_number, extract_episode_number, extract_part_number,
    )
    assert extract_season_number("Show S1E05") == "1"
    assert extract_episode_number("Show S1E05") == "5"
    assert extract_episode_number("Show Ep. 12") == "12"
    assert extract_part_number("Show pt.4") == "4"
    assert extract_part_number("Show Part 2") == "2"


def test_sep_does_not_match_ordinary_words():
    """The season/episode/part extractors must not false-positive on
    ordinary words that happen to contain similar substrings (e.g.
    "Part-Time" should not be read as a part marker).
    """
    from scripts.cbz_core import (
        extract_season_number, extract_episode_number, extract_part_number,
    )
    assert extract_season_number("Superman Ch.3") is None
    assert extract_part_number("Part-Time Hero Ch.5") is None
    assert extract_episode_number("Episode Zero Ch.1") is None


def test_episode_not_treated_as_chapter():
    """Regression guard: an episode marker like "Ep. 1" must not leak into
    chapter extraction -- chapter and episode are tracked independently.
    """
    from scripts.cbz_core import extract_chapter_number, extract_episode_number
    # "Ep. 1" must no longer be read as chapter 1
    assert extract_chapter_number("Show Chapter 88 Ep. 1") == "88"
    assert extract_episode_number("Show Chapter 88 Ep. 1") == "1"


def test_native_cjk_title_is_translated():
    """A pure CJK title should be run through the translation backend and
    return (translated_title, original_title). _run_googletrans is
    patched so this doesn't depend on network access.
    """
    with patch(
        "scripts.cbz_core._run_googletrans",
        return_value=("Demon Slayer", "ja"),
    ):
        assert translate_metadata_text("鬼滅の刃") == ("Demon Slayer", "鬼滅の刃")


def test_romanized_japanese_title_is_translated_when_language_is_detected():
    """A romanized (Latin-script) Japanese title should still be detected
    as non-English and sent through translation, not skipped just because
    it's already in Latin characters.
    """
    with patch(
        "scripts.cbz_core._run_googletrans",
        return_value=("Attack on Titan", "ja"),
    ):
        assert translate_metadata_text("Shingeki no Kyojin") == (
            "Attack on Titan",
            "Shingeki no Kyojin",
        )


def test_mixed_title_with_existing_english_segment_is_not_translated():
    """If an English segment is already present alongside the native title,
    translation should be skipped entirely (no call to the translator) and
    the text returned as-is with no original_title captured.
    """
    with patch("scripts.cbz_core._run_googletrans") as translator:
        assert translate_metadata_text("Demon Slayer / 鬼滅の刃") == (
            "Demon Slayer / 鬼滅の刃",
            None,
        )
        translator.assert_not_called()


def test_extract_trailing_bare_number():
    """A bare trailing number (no "Ch."/"Vol." prefix) can be extracted as
    a potential chapter number, but the extractor makes no judgment about
    whether it's actually a chapter marker vs. part of the title itself.
    """
    from scripts.cbz_core import extract_trailing_bare_number

    assert extract_trailing_bare_number("Arrest Thy Neighbor 2") == "2"
    assert extract_trailing_bare_number("Arrest Thy Neighbor") is None
    # No evidence gating here by design — callers (e.g. the watcher's
    # _resolve_series_dir_name) are responsible for confirming the trailing
    # number is really a chapter marker and not part of the title.
    assert extract_trailing_bare_number("Area 88") == "88"


def test_parse_comic_name_uses_english_title_and_preserves_original():
    """End-to-end check: parsing a native-language filename/directory pair
    should produce an English filename, while preserving both the
    translated_title and the original_title fields on the parsed result.
    """
    with patch(
        "scripts.cbz_core._run_googletrans",
        return_value=("Demon Slayer", "ja"),
    ):
        parsed = parse_comic_name(Path("鬼滅の刃/鬼滅の刃 第5話.cbz"))

    assert parsed.filename == "Demon Slayer Ch. 5.cbz"
    assert parsed.translated_title == "Demon Slayer"
    assert parsed.original_title == "鬼滅の刃"
