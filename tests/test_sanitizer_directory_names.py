"""Regression tests for a bug in cbz_sanitizer.py's own (duplicated) copy of the
trailing chapter/part/volume stub regex.

_DIR_TRAILING_STUB_RE used `[\\s_\\-]*` (zero-or-more separator) instead of the
canonical `[\\s_\\-]+` (one-or-more, see cbz_core.py) used everywhere else. With
`*` the leading separator is optional, so the regex happily matched a bare "ch"
glued onto the end of any word with no separator at all — silently truncating
directory names like "Switch" -> "Swit", "Beach" -> "Bea", "Bitch" -> "Bit",
"Watch"/"Stopwatch" -> "Stopwat", "Match"/"Rematch" -> "Remat", "Patch" -> "Pat",
"Crotch" -> "Crot", "Reach" -> "Rea", across an entire library during a
sanitize/maintenance run.

A second, unrelated bug in the same file — CHAPTER_ONLY_RE using doubled
backslashes (`\\\\.?\\\\s*` inside a raw string, i.e. "require a literal
backslash character") instead of single ones (`\\.?\\s*`) — is also covered
here, even though that regex is normally unreachable (cbz_sanitizer only falls
back to it if importing cbz_core fails).
"""

from scripts.cbz_sanitizer import CHAPTER_ONLY_RE, clean_directory_name


def test_words_ending_in_ch_are_not_truncated():
    assert clean_directory_name("Switch") == "Switch"
    assert clean_directory_name("Iroha Switch") == "Iroha Switch"
    assert clean_directory_name("Beach") == "Beach"
    assert clean_directory_name("Bitch") == "Bitch"
    assert clean_directory_name("Stopwatch") == "Stopwatch"
    assert clean_directory_name("Rematch") == "Rematch"
    assert clean_directory_name("Sweetness Always Follows a Sour Patch") == (
        "Sweetness Always Follows a Sour Patch"
    )
    assert clean_directory_name("Do You Love Me More Than My Crotch") == (
        "Do You Love Me More Than My Crotch"
    )
    assert clean_directory_name("Out of Reach") == "Out of Reach"


def test_real_trailing_chapter_tokens_still_stripped():
    # A genuine trailing Ch./Part/v token, separated by whitespace, must still
    # be stripped — only the "no separator required" bug is being fixed.
    assert clean_directory_name("Berserk Ch") == "Berserk"
    assert clean_directory_name("Berserk Chapter") == "Berserk"
    assert clean_directory_name("One Piece Part") == "One Piece"
    assert clean_directory_name("Naruto v") == "Naruto"


def test_chapter_only_re_matches_bare_chapter_number():
    m = CHAPTER_ONLY_RE.match("Ch.5")
    assert m is not None
    assert m.group(1) == "5"

    m = CHAPTER_ONLY_RE.match("Chapter 12")
    assert m is not None
    assert m.group(1) == "12"
