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
    assert sanitize("One Piece [CoolScans]") == "One Piece"


def test_url_removed():
    assert sanitize("Batman readcomics.net") == "Batman"


def test_windows_forbidden_characters_removed():
    assert clean_filename('Batman: Year/One?.cbz') == "Batman Year One.cbz"


def test_mixed_original_title_prefers_english_segment():
    assert clean_filename("One Piece / ワンピース Ch.005.cbz") == "One Piece Ch.005.cbz"


def test_does_not_empty_non_latin_only_title():
    assert clean_filename("ワンピース Ch.005.cbz") != ".cbz"


def test_number_token_normalization():
    assert normalise_number_tokens("Vol.01 Ch.005") == "Vol.1 Ch.5"


def test_chapter_extraction_decimal():
    assert extract_chapter_number("One Piece Ch. 10.5") == "10.5"


def test_clean_directory_name_removes_hash_wrappers():
    assert clean_directory_name("## One Piece ##") == "One Piece"


def test_parse_comic_name_uses_clean_directory_name_for_series():
    parsed = parse_comic_name(Path("## One Piece ##/001 - One Piece Ch.005.cbz"))

    assert parsed.series == "One Piece"
    assert parsed.chapter == "5"

def test_mixed_title_preserves_volume_from_other_segment():
    assert clean_filename("One Piece Vol.02 / ワンピース Ch.005.cbz") == "One Piece Vol.02 Ch.005.cbz"


def test_non_latin_only_title_preserved_with_chapter():
    result = clean_filename("ワンピース Ch.005.cbz")
    assert result != ".cbz"
    assert "Ch.5" in result or "Ch.005" in result or "ワンピース" in result


def test_parse_comic_name_ext_from_split_not_path_suffix():
    # Verifies _ext from _split_name is used, not path.suffix
    from scripts.cbz_core import parse_comic_name
    parsed = parse_comic_name(Path("Series/Batman Ch.5.cbz"))
    assert parsed.filename.endswith(".cbz")
    assert not parsed.filename.endswith(".cbz.cbz")


def test_extract_season_episode_part():
    from scripts.cbz_core import (
        extract_season_number, extract_episode_number, extract_part_number,
    )
    assert extract_season_number("Show S1E05") == "1"
    assert extract_episode_number("Show S1E05") == "5"
    assert extract_episode_number("Show Ep. 12") == "12"
    assert extract_part_number("Show pt.4") == "4"
    assert extract_part_number("Show Part 2") == "2"


def test_sep_does_not_match_ordinary_words():
    from scripts.cbz_core import (
        extract_season_number, extract_episode_number, extract_part_number,
    )
    assert extract_season_number("Superman Ch.3") is None
    assert extract_part_number("Part-Time Hero Ch.5") is None
    assert extract_episode_number("Episode Zero Ch.1") is None


def test_episode_not_treated_as_chapter():
    from scripts.cbz_core import extract_chapter_number, extract_episode_number
    # "Ep. 1" must no longer be read as chapter 1
    assert extract_chapter_number("Show Chapter 88 Ep. 1") == "88"
    assert extract_episode_number("Show Chapter 88 Ep. 1") == "1"


def test_native_cjk_title_is_translated():
    with patch(
        "scripts.cbz_core._run_googletrans",
        return_value=("Demon Slayer", "ja"),
    ):
        assert translate_metadata_text("鬼滅の刃") == ("Demon Slayer", "鬼滅の刃")


def test_romanized_japanese_title_is_translated_when_language_is_detected():
    with patch(
        "scripts.cbz_core._run_googletrans",
        return_value=("Attack on Titan", "ja"),
    ):
        assert translate_metadata_text("Shingeki no Kyojin") == (
            "Attack on Titan",
            "Shingeki no Kyojin",
        )


def test_mixed_title_with_existing_english_segment_is_not_translated():
    with patch("scripts.cbz_core._run_googletrans") as translator:
        assert translate_metadata_text("Demon Slayer / 鬼滅の刃") == (
            "Demon Slayer / 鬼滅の刃",
            None,
        )
        translator.assert_not_called()


def test_extract_trailing_bare_number():
    from scripts.cbz_core import extract_trailing_bare_number

    assert extract_trailing_bare_number("Arrest Thy Neighbor 2") == "2"
    assert extract_trailing_bare_number("Arrest Thy Neighbor") is None
    # No evidence gating here by design — callers (e.g. the watcher's
    # _resolve_series_dir_name) are responsible for confirming the trailing
    # number is really a chapter marker and not part of the title.
    assert extract_trailing_bare_number("Area 88") == "88"


def test_parse_comic_name_uses_english_title_and_preserves_original():
    with patch(
        "scripts.cbz_core._run_googletrans",
        return_value=("Demon Slayer", "ja"),
    ):
        parsed = parse_comic_name(Path("鬼滅の刃/鬼滅の刃 第5話.cbz"))

    assert parsed.filename == "Demon Slayer Ch. 5.cbz"
    assert parsed.translated_title == "Demon Slayer"
    assert parsed.original_title == "鬼滅の刃"
