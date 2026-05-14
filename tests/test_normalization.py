from pathlib import Path

from scripts.cbz_core import (
    clean_directory_name,
    clean_filename,
    extract_chapter_number,
    normalise_number_tokens,
    parse_comic_name,
    sanitize,
)


def test_scan_group_removed():
    assert sanitize("One Piece [CoolScans]") == "One Piece"


def test_url_removed():
    assert sanitize("Batman readcomics.net") == "Batman"


def test_windows_forbidden_characters_removed():
    assert clean_filename('Batman: Year/One?.cbz') == "Batman Year One.cbz"


def test_mixed_original_title_prefers_english_segment():
    assert clean_filename("One Piece / ワンピース Ch.005.cbz") == "One Piece.cbz"


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
