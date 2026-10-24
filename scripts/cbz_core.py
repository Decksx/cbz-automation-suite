"""
Shared core utilities for CBZ Automation Suite.

This module is the intended single source of truth for:
- filename and directory normalization
- mixed English/original-title shortening
- chapter/volume parsing
- root-aware series inference
- ComicInfo.xml field updates

It intentionally does not perform filesystem moves, watcher debounce logic,
archive rewriting, routing, or logging configuration.
"""

from __future__ import annotations

import asyncio
import html
import inspect
import os
import re
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

try:
    import truststore

    # Use the operating-system certificate store. This supports enterprise
    # interception certificates without weakening HTTPS verification.
    truststore.inject_into_ssl()
except ImportError:
    pass

try:
    from googletrans import Translator
except ImportError:
    Translator = None

try:
    from unidecode import unidecode
except ImportError:
    def unidecode(text: str) -> str:
        return text

# The canonical formatter owns the closed edition list and the canonical-name
# recogniser (design §5.5). Imported, never copied: a second copy of the list
# is exactly the drift the convergence exists to prevent.
try:
    from scripts.cbz_canonical_name import (
        EDITION_WORDS,
        CanonicalNameError,
        parse_canonical_name,
    )
except ModuleNotFoundError:  # run from inside scripts/ without the package
    from cbz_canonical_name import (  # type: ignore[no-redef]
        EDITION_WORDS,
        CanonicalNameError,
        parse_canonical_name,
    )

__all__ = [
    "ALL_RULES",
    "GIBBERISH_RE",
    "IGNORED_SERIES_FOLDERS",
    "NUMBER_PREFIX_RE",
    "TRAILING_JUNK_RE",
    "DisplayTokens",
    "ParsedComicName",
    "append_display_tokens",
    "clean_directory_name",
    "clean_filename",
    "clean_xml_field",
    "decensor_tokens_as_words",
    "display_tokens_for",
    "extract_display_tokens",
    "extract_chapter_number",
    "extract_volume_number",
    "extract_season_number",
    "extract_episode_number",
    "extract_part_number",
    "extract_trailing_bare_number",
    "build_sep_notes",
    "infer_series_name",
    "is_generic",
    "is_generic_title",
    "normalise_archive_key",
    "normalise_number_tokens",
    "normalize_stem",
    "parse_comic_name",
    "parse_rules",
    "preserve_decensor_markers",
    "repair_mojibake",
    "sanitize",
    "sanitize_display",
    "sanitize_without_markers",
    "series_base_name",
    "shorten_mixed_original_title",
    "translate_metadata_text",
    "translate_cjk_text",
    "update_comicinfo_xml",
]



ALL_RULES = {
    "url",
    "scan_groups",
    "brackets",
    "windows_safe",
    "shorten_original_titles",
    "leading_nums",
    "trailing_junk",
    "normalize_stem",
    "number_tokens",
    "comicinfo",
    "translate",
}

IGNORED_SERIES_FOLDERS = {
    "issues",
    "issue",
    "chapters",
    "chapter",
    "volumes",
    "volume",
    "extras",
    "specials",
    "omakes",
    "bonus",
    "bonuses",
}

_TITLE_OVERWRITE_RES = [
    re.compile(r"manga[\s_]chapter", re.IGNORECASE),
    re.compile(r"^#\s*english", re.IGNORECASE),
    re.compile(r"^#\s*chapter", re.IGNORECASE),
    re.compile(r"^chapter", re.IGNORECASE),
    re.compile(r"^part\s+\d+", re.IGNORECASE),
    re.compile(r"doujinshi[\s_]chapter", re.IGNORECASE),
    re.compile(r"official[\s_]chapter", re.IGNORECASE),
    re.compile(r"unknown[\s_]chapter", re.IGNORECASE),
]

NUMBER_PREFIX_RE = re.compile(
    r"^(?:\d+\s+v\d+\s+|\d+\s*[-_]\s*)",
    re.IGNORECASE,
)
TRAILING_JUNK_RE = re.compile(r"[\s\-_–—]+$")
_SCANLATION_HASH_RE = re.compile(r"(?:[\s_-][0-9a-f]{6})+$", re.IGNORECASE)
GIBBERISH_RE = re.compile(
    r"^(?:TEMP[\s_-]*[0-9a-f]{8,}|[0-9a-f]{16,}"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    re.IGNORECASE,
)

_BRACKET_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)")
_DECENSOR_MARKER_RE = re.compile(r"\b(?:uncensored|decensored)\b", re.IGNORECASE)
_STRAY_RE = re.compile(r"[\[\]()]")
_SPACES_RE = re.compile(r" {2,}")
_URL_RE = re.compile(
    r"(?:https?://\S+)"
    r"|(?:www\.\S+)"
    r"|(?:\b[\w-]+\.(?:com|net|org|io|co|info|biz|tv|me|cc|us|uk|ca|au)(?:/\S*)?)",
    re.IGNORECASE,
)
_SCAN_GROUP_RE = re.compile(
    r"\b[\w-]*scans?\b|\b[\w-]*scanners?\b|\b[\w-]*scanlations?\b",
    re.IGNORECASE,
)
_GCODE_RE = re.compile(r"[\s\-]*\bG\d{3,5}$")
_TRAILING_SLASH_RE = re.compile(r"[\s/]+$")
_WINDOWS_FORBIDDEN_CHARS_RE = re.compile(r'[<>:"/\\|?*]')
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_MIXED_TITLE_SPLIT_RE = re.compile(r"\s*(?:[/\\|｜]| - | – | — |~)\s*")
_TRANSLATABLE_LANGUAGE_CODES = {"ja", "ko", "zh-cn", "zh-tw", "zh"}
_ROMANIZED_MARKER_RE = re.compile(
    r"""
    (?:
        \b(?:chan|kun|sama|senpai|sensei|desu|kudasai|isekai|shoujo|shonen|doujinshi)\b
      | \b(?:no|wa|ga|wo|ni|kara|made)\b.{0,40}\b(?:no|wa|ga|wo|ni|kara|made)\b
      | \b(?:eui|nim|ssi|seonsaeng|oppa|hyung|sunbae|juseyo)\b
      | \b(?:zhe|zhei|nage|shenme|xiansheng|xiaojie|gongzi|shifu)\b
      | [āáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ]
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
_TRANSLATION_CACHE_LOCK = threading.Lock()

# A marker cannot begin inside a word ("Bitch 1" is not "Bit Ch 1").
# Allow digits before markers so compact sequences like v01ch005 still work.
_NUMBER_MARKER_START = r"(?<![^\W\d_])"
_NUM_TOKEN_RE = re.compile(
    _NUMBER_MARKER_START +
    r"""
    (
        (?:
            ch(?:ap(?:ter)?)?p?
          | issue
          | ep(?:isode)?
          | vol(?:ume)?
          | v(?=\d)
        )
        \.?\s*
    )
    (\d[\d.]*)
    """,
    re.IGNORECASE | re.VERBOSE,
)
_DIR_LEADING_HASH_RE = re.compile(r"^#+\s*")
_DIR_TRAILING_HASH_RE = re.compile(r"\s*#+$")
_DIR_TRAILING_STUB_RE = re.compile(
    r"[\s_\-]+(?:part|v|ch(?:ap(?:ter)?)?)\s*$", re.IGNORECASE
)
_CHAPTER_NUMBER_RE = re.compile(
    _NUMBER_MARKER_START + r"(?:"
    r"ch(?:ap(?:ter)?)?p?\.?\s*(\d[\d.]*)"
    r"|issue\s*(\d[\d.]*)"
    r"|#\s*(\d[\d.]*)"
    r")",
    re.IGNORECASE,
)
_VOLUME_NUMBER_RE = re.compile(
    _NUMBER_MARKER_START + r"(?:"
    r"vol(?:ume)?\.?\s*(\d[\d.]*)"
    r"|v(\d[\d.]*)(?=\s|ch|ep|$)"
    r")",
    re.IGNORECASE,
)
# Season / Episode / Part markers, captured independently of chapter/volume so
# they can be recorded in ComicInfo without polluting the native Number field.
# Each requires its keyword glued to (or spaced from) the digits; the short forms
# (s/e/p) require a word boundary before them so ordinary words are not matched.
_SEASON_NUMBER_RE = re.compile(
    r"(?:\bseasons?\.?\s*(\d[\d.]*)"
    r"|(?<![A-Za-z])s(\d[\d.]*)(?=\s|e\d|ep|x\d|$))",
    re.IGNORECASE,
)
_EPISODE_NUMBER_RE = re.compile(
    r"(?:\bepisodes?\.?\s*(\d[\d.]*)"
    r"|\bep\.?\s*(\d[\d.]*)"
    r"|(?<![A-Za-z])e(\d[\d.]*)(?=\s|$))",
    re.IGNORECASE,
)
_PART_NUMBER_RE = re.compile(
    r"(?:\bparts?\.?\s*(\d[\d.]*)"
    r"|\bpt\.?\s*(\d[\d.]*)"
    r"|(?<![A-Za-z])p(\d[\d.]*)(?=\s|$))",
    re.IGNORECASE,
)
# Matches a Season/Episode/Part segment already present in <Notes>, so the SEP
# line can be replaced on re-run instead of accumulating duplicates.
_SEP_NOTE_SEG_RE = re.compile(r"^(?:Season|Episode|Part):\s*\d", re.IGNORECASE)
GENERIC_CHAPTER_RE = re.compile(
    r"^"
    r"(?:"
    r"(?:(?:doujinshi|official|manga|unknown)[\s_]+)"
    r"(?:#\s*(?:english[\s_]+)?)?"
    r"(?:ch(?:ap(?:ter)?)?p?\.?\s*|part\.?\s*)"
    r"|"
    r"#\s*(?:english[\s_]+)?"
    r"(?:ch(?:ap(?:ter)?)?p?\.?\s*)?"
    r"|"
    r"(?:chapter|part)\.?\s+"
    r")"
    r"(\d[\d.]*)"
    r"(.*?)$",
    re.IGNORECASE,
)
CHAPTER_ONLY_RE = re.compile(
    r"^(?:ch(?:ap(?:ter)?)?\.?\s*|chp\.?\s*)(\d[\d.]*)",
    re.IGNORECASE,
)
NUMBERED_CHAPTER_RE = re.compile(
    r"^(?:"
    r"(?:\d+\.\s*)"
    r"ch(?:ap(?:ter)?)?p?\.?\s*-?\s*(\d[\d.]*)\s*$"
    r"|"
    r"ch(?:ap(?:ter)?)?p?\.?\s*-\s*(\d[\d.]*)\s*$"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedComicName:
    """Fields parsed from one archive path by :func:`parse_comic_name`.

    ``stem`` is the cleaned name text that ComicInfo ``<Title>`` and the
    placeholder checks read. ``filename`` is the name the archive should
    carry on disk. They agree (``filename == stem + ext``) except when
    ``parse_comic_name`` ran with ``display_tokens=True``: the display
    filename then also keeps the canonical tokens of design §5.5 (year,
    edition, version), which belong in the file name but not in the title.
    """

    original_path: Path
    filename: str
    stem: str
    series: str
    chapter: str | None
    volume: str | None
    season: str | None = None
    episode: str | None = None
    part: str | None = None
    original_title: str | None = None
    translated_title: str | None = None


def parse_rules(raw: str) -> set[str]:
    tokens = {t.strip().lower() for t in raw.split(",") if t.strip()}
    return tokens & ALL_RULES


def _collapse_spaces(text: str) -> str:
    return _SPACES_RE.sub(" ", text).strip()


def _make_windows_safe(text: str) -> str:
    return _collapse_spaces(_WINDOWS_FORBIDDEN_CHARS_RE.sub(" ", text))


def _latin_score(text: str) -> tuple[int, int]:
    return (len(_LATIN_RE.findall(text)), -len(text))


def _extract_num_suffix(parts: list[str], chosen: str) -> str:
    """Return any chapter/volume tokens found in segments other than *chosen*.

    Prevents ``shorten_mixed_original_title`` from silently dropping chapter
    numbers that happen to sit in the non-Latin segment, e.g.
    ``"One Piece / ワンピース Ch.005"`` -> chosen ``"One Piece"``, suffix ``" Ch.005"``.
    """
    for part in parts:
        if part == chosen:
            continue
        tokens: list[str] = []
        vol = _VOLUME_NUMBER_RE.search(part)
        ch  = _CHAPTER_NUMBER_RE.search(part)
        if vol:
            tokens.append(part[vol.start():vol.end()])
        if ch:
            tokens.append(part[ch.start():ch.end()])
        if tokens:
            return " " + " ".join(tokens)
    return ""


def shorten_mixed_original_title(text: str, max_length: int = 120) -> str:
    """Prefer a Latin-heavy title segment when a long name includes CJK too.

    This avoids the old destructive behavior of deleting every non-Latin
    character. Non-Latin-only names remain intact.

    Chapter/volume tokens present in a non-chosen segment are appended to the
    result so they are never silently dropped:
      ``"One Piece / ワンピース Ch.005"``  ->  ``"One Piece Ch.005"``
    """
    if not _CJK_RE.search(text):
        return text

    parts = [p.strip() for p in _MIXED_TITLE_SPLIT_RE.split(text) if p.strip()]
    if len(parts) < 2 and len(text) <= max_length:
        return text

    latin_parts = [p for p in parts if _LATIN_RE.search(p)]
    if not latin_parts:
        return text[:max_length].rstrip() if len(text) > max_length else text

    best = max(latin_parts, key=_latin_score)
    return best + _extract_num_suffix(parts, best)


def _is_cjk_only(text: str) -> bool:
    """Check whether *text* is essentially a CJK-only title.

    Chapter/volume/number tokens (``Ch. 5``, ``Vol.2``, ``v3``, bare numbers) and
    common punctuation are removed first, so a name like ``"鬼滅の刃 Ch. 5"`` is
    still recognised as CJK-only (its meaningful title carries no Latin letters).
    """
    if not _CJK_RE.search(text):
        return False
    # Drop chapter/volume keyword tokens, then strip punctuation, digits, spaces.
    work = _NUM_TOKEN_RE.sub("", text)
    work = re.sub(r"\b(?:ch|chap|chapter|chp|vol|volume|v|ep|episode|issue|part)\b\.?", "", work, flags=re.IGNORECASE)
    stripped = re.sub(r"[\s\d\.\,\-_\(\)\[\]'\"・:#]", "", work)
    return bool(stripped) and not _LATIN_RE.search(stripped)


def _romanize(text: str) -> str:
    """Transliterate text to ASCII (romaji/pinyin-ish) via unidecode."""
    try:
        return unidecode(text).strip()
    except Exception:
        return text


@lru_cache(maxsize=4096)
def _run_googletrans(text: str) -> tuple[str | None, str | None]:
    """Call googletrans across its sync (3.0.x) and async (3.1+/4.x) variants.

    Returns ``(translated_text, detected_language)``, or ``(None, None)`` on
    failure. The library's
    API changed over versions:
      - 3.0.0: translator.translate(text, dest='en') is synchronous and returns a
        Translated object with a .text attribute.
      - 3.1.0+/4.x: translate() is a coroutine and must be awaited.
    We detect a coroutine return and run it on a private event loop so the same
    code path works regardless of the installed version.
    """
    if Translator is None or os.environ.get("CBZ_TRANSLATION_ENABLED", "1") == "0":
        return None, None
    try:
        with _TRANSLATION_CACHE_LOCK:
            translator = Translator()
            # Correct kwargs for every published version are dest=/src=.
            result = translator.translate(text, dest="en", src="auto")

            # Async variants return a coroutine; resolve it on a dedicated loop.
            if inspect.iscoroutine(result):
                try:
                    loop = asyncio.new_event_loop()
                    try:
                        result = loop.run_until_complete(result)
                    finally:
                        loop.close()
                except Exception:
                    return None, None

            # Batch input could yield a list; take the first element.
            if isinstance(result, list):
                result = result[0] if result else None
        if result is None:
            return None, None

        translated = getattr(result, "text", None)
        source = getattr(result, "src", None)
        translated_text = (
            translated.strip()
            if isinstance(translated, str) and translated.strip()
            else None
        )
        source_code = source.lower() if isinstance(source, str) else None
        return translated_text, source_code
    except Exception:
        # Network errors, API drift, rate limits, JSON decode errors, etc.
        return None, None


def _has_existing_english_segment(text: str) -> bool:
    if not (_CJK_RE.search(text) and _LATIN_RE.search(text)):
        return False
    parts = [p.strip() for p in _MIXED_TITLE_SPLIT_RE.split(text) if p.strip()]
    return len(parts) > 1 and any(
        _LATIN_RE.search(part) and not _CJK_RE.search(part) for part in parts
    )


def _looks_romanized_cjk(text: str, detected_language: str | None) -> bool:
    if _CJK_RE.search(text) or not _LATIN_RE.search(text):
        return False
    if detected_language in _TRANSLATABLE_LANGUAGE_CODES:
        return True
    return bool(_ROMANIZED_MARKER_RE.search(text))


def translate_metadata_text(text: str) -> tuple[str, str | None]:
    """Translate native or confidently romanized Japanese/Korean/Chinese text.

    Mixed titles that already contain a separate English segment are left alone.
    The original value is returned as the second tuple item only when a usable
    English replacement was produced.
    """
    value = _collapse_spaces(text)
    if not value or _has_existing_english_segment(value):
        return text, None

    has_cjk = bool(_CJK_RE.search(value))
    translated, detected_language = _run_googletrans(value)
    eligible = has_cjk or _looks_romanized_cjk(value, detected_language)
    if (
        eligible
        and translated
        and translated.casefold() != value.casefold()
        and _LATIN_RE.search(translated)
        and not _CJK_RE.search(translated)
    ):
        return translated, text

    # Transliteration is not translation, so it is disabled by default. It can
    # be enabled explicitly for libraries that prefer Latin text over preserving
    # native script during an upstream outage.
    if has_cjk and os.environ.get("CBZ_TRANSLITERATE_FALLBACK", "0") == "1":
        romanized = _romanize(value)
        if romanized and romanized != value and _LATIN_RE.search(romanized):
            return romanized, text

    return text, None


def translate_cjk_text(text: str) -> tuple[str, str | None]:
    """Backward-compatible wrapper for title and metadata translation.

    Strategy (per the requested behavior):
      1. Try machine translation (googletrans) first.
      2. Fall back to romanization/transliteration (unidecode) if translation
         fails or is unavailable.

    Returns ``(english_text, original_text)`` when a usable English rendering was
    produced, or ``(text, None)`` when *text* is not CJK-only or nothing better
    than the original could be generated. The second element is the untouched
    original, suitable for storing as an alternate/original title.
    """
    return translate_metadata_text(text)


def repair_mojibake(text: str) -> str:
    """Repair filenames where non-ASCII characters were written as their literal
    UTF-8 byte hex (mojibake), e.g. ``Playere28099s`` -> ``Player's``.

    Some downloaders encode a title's smart punctuation (’ – — “ ” … and CJK
    characters) as the raw lowercase hex of its UTF-8 bytes, so ``’`` (U+2019,
    bytes ``e2 80 99``) lands in the name as the literal text ``e28099``. This
    walks the string and, at any position that begins a valid 3- or 4-byte UTF-8
    sequence in hex (lead ``e0-ef`` / ``f0-f4`` followed by ``80-bf``
    continuation byte hex), decodes it back to the real character.

    Only 3/4-byte sequences are considered, so two-byte coincidences like the
    hex word ``dead`` or a scanlation hash such as ``a1b2c3`` are left untouched.
    Decoded runs must land in sensible punctuation/CJK/fullwidth ranges, or the
    run is left as-is. Clean names pass through unchanged.
    """
    low = text.lower()
    if "e" not in low and "f" not in low:
        return text  # fast path: no possible lead-byte hex

    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        consumed, decoded = _decode_mojibake_run(text, i)
        if decoded is not None:
            out.append(decoded)
            i += consumed
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _mojibake_codepoint_ok(s: str) -> bool:
    """Whether decoded codepoints look like genuine punctuation/CJK/fullwidth."""
    return all(
        0x00A0 <= ord(c) <= 0x33FF
        or 0x3400 <= ord(c) <= 0x9FFF
        or 0xAC00 <= ord(c) <= 0xD7A3
        or 0xF900 <= ord(c) <= 0xFAFF
        or 0xFF00 <= ord(c) <= 0xFFEF
        for c in s
    )


def _decode_mojibake_run(text: str, i: int) -> tuple[int, str | None]:
    """Try to decode a run of UTF-8 byte-hex starting at index *i*.

    Returns (chars_consumed, decoded_text) or (0, None) when the position does
    not begin a valid multibyte UTF-8 hex run.
    """
    by = bytearray()
    j = i
    while j < len(text):
        lead = text[j:j + 2].lower()
        if len(lead) < 2 or not all(ch in "0123456789abcdef" for ch in lead):
            break
        if not (lead[0] == "e" or (lead[0] == "f" and lead[1] in "01234")):
            break
        nbytes = 3 if lead[0] == "e" else 4
        pos = j + 2
        chunk = [lead]
        ok = True
        for _ in range(nbytes - 1):
            cont = text[pos:pos + 2].lower()
            if len(cont) == 2 and cont[0] in "89ab" and all(ch in "0123456789abcdef" for ch in cont):
                chunk.append(cont)
                pos += 2
            else:
                ok = False
                break
        if not ok:
            break
        by += bytes.fromhex("".join(chunk))
        j = pos
    if not by:
        return 0, None
    try:
        decoded = by.decode("utf-8")
    except UnicodeDecodeError:
        return 0, None
    if not _mojibake_codepoint_ok(decoded):
        return 0, None
    return (j - i), decoded


def preserve_decensor_markers(source: str, cleaned: str) -> str:
    """Keep censorship-status words from bracketed source text after cleanup.

    Scan-group brackets can still be dropped, but an explicit processed-status
    marker must remain visible to the decensor backlog and import guard.
    """
    seen = {_marker.group(0).casefold() for _marker in _DECENSOR_MARKER_RE.finditer(cleaned)}
    missing = []
    for bracket in _BRACKET_RE.finditer(source):
        for marker in _DECENSOR_MARKER_RE.finditer(bracket.group(0)):
            word = marker.group(0)
            if word.casefold() not in seen:
                missing.append(word)
                seen.add(word.casefold())
    return _collapse_spaces(" ".join((cleaned, *missing))) if missing else cleaned


def _to_fixpoint(step, text: str) -> str:
    """Apply *step* until its output stops changing, and return that output.

    Why iterate rather than reorder the rules: the rules interact. Removing a
    stray bracket can expose a URL (``"Batman].cC"`` -> ``"Batman.cC"`` ->
    ``""``, design §5.5), each ``html.unescape`` peels one entity level
    (``"&amp;amp;"``), and ``_GCODE_RE`` strips one trailing code per pass. No
    single ordering removes every such interaction; iterating makes
    ``f(f(x)) == f(x)`` hold by construction rather than by audit, and an
    input that was already a fixpoint costs one extra comparison.

    Termination: every cleaning rule either deletes characters or turns a
    non-space character into a space. The one rule that adds text,
    ``preserve_decensor_markers``, re-adds a marker word only after the
    bracket around it was deleted, and the bracket is longer than the word
    plus its separating space. So every pass that changes the text strictly
    lowers (length, non-space count), and ``2 * len(text) + 2`` passes always
    suffice. Exceeding the bound means some rule broke that argument; it
    raises rather than return a value that is not a fixpoint.

    The argument is about the cleaning rules this module passes in
    (``_sanitize_pass``, ``_sanitize_core``, the filename and directory
    passes). It is not a guarantee for an arbitrary *step*: translation, for
    one, is never run inside this loop.
    """
    limit = 2 * len(text) + 2
    for _ in range(limit):
        cleaned = step(text)
        if cleaned == text:
            return text
        text = cleaned
    raise RuntimeError(f"cleaning did not reach a fixpoint within {limit} passes")


def _sanitize_core(text: str, rules: set[str]) -> str:
    """One pass of the cleaning rules, without re-attaching decensor markers."""
    text = repair_mojibake(text)
    text = html.unescape(text)

    if "url" in rules:
        text = _URL_RE.sub("", text)
    if "scan_groups" in rules:
        text = _SCAN_GROUP_RE.sub("", text)

    text = _TRAILING_SLASH_RE.sub("", text)
    text = _GCODE_RE.sub("", text)

    if "brackets" in rules:
        text = _BRACKET_RE.sub("", text)
        text = _STRAY_RE.sub("", text)

    if "shorten_original_titles" in rules:
        text = shorten_mixed_original_title(text)

    text = text.replace("_", " ")

    if "windows_safe" in rules:
        text = _make_windows_safe(text)

    return _collapse_spaces(text)


def _sanitize_pass(text: str, rules: set[str]) -> str:
    return preserve_decensor_markers(text, _sanitize_core(text, rules))


def sanitize(text: str, rules: set[str] = ALL_RULES) -> str:
    """Clean *text* for use as a name: the import pipeline's base cleaner.

    Strips URLs, scan-group words, G-codes and (with the ``brackets`` rule)
    every bracketed block, then keeps any ``uncensored``/``decensored`` word
    that was inside a removed bracket (:func:`preserve_decensor_markers`).

    Idempotent: ``sanitize(sanitize(x)) == sanitize(x)`` for any rule set.
    A single pass was not (``"Batman].cC"`` -> ``"Batman.cC"`` -> ``""``,
    design §5.5), so the pass is repeated to its fixpoint; see
    :func:`_to_fixpoint`. Inputs that one pass already left stable clean
    exactly as before; inputs that a single pass did not settle now clean to
    their fixpoint, for every caller of this module, not only the watcher.

    This is the **identity** cleaner: series-folder lookup and every caller
    outside the watcher's display path use it, so ``Berserk [Judas]`` still
    cleans to ``Berserk``. Use :func:`sanitize_display` for a display name.
    """
    return _to_fixpoint(lambda t: _sanitize_pass(t, rules), text)


def sanitize_without_markers(text: str, rules: set[str] = ALL_RULES) -> str:
    """:func:`sanitize` without re-attaching decensor markers, to its fixpoint.

    The bare name text with every bracketed block removed: the "core" that
    :func:`sanitize_display` appends canonical tokens to, and what the
    watcher's placeholder check reads so a ``(Uncensored)`` token does not
    hide a placeholder name.
    """
    return _to_fixpoint(lambda t: _sanitize_core(t, rules), text)


# ---------------------------------------------------------------------------
# Display tokens (design §5.5)
#
# David's 2026-10-22 decision splits the "brackets" rule: identity matching
# (series_key, series-folder lookup, everything sanitize() serves) still
# strips every bracketed block, while the display name keeps the canonical
# tokens -- (YYYY), [vN] and the edition list -- and still drops scanlator,
# group and source tags. Recognised shapes, and nothing else:
#
#   (YYYY)              four ASCII digits, 1000-9999, in round brackets
#   [vN]                v or V and a positive whole number, in square brackets;
#                       written [vN] with no padding
#   (Edition)           a whole EDITION_WORDS entry in round brackets, any
#                       case; written in the list's spelling
#   [Colored]           the one square-bracket edition word promoted to its
#                       round-bracket token (David, 2026-10-23): a whole
#                       _SQUARE_EDITION_WORDS entry, any case, written
#                       (Colored); one token even when (Colored) is there too
#   any bracket holding an uncensored/decensored word
#                       written (Uncensored)/(Decensored), the formatter's
#                       edition token, wherever preserve_decensor_markers
#                       would have kept the bare word
#
# Deliberately not kept, pending a decision (see the PR): [2019] and (v2)
# (not the canonical bracket for that token), [English], [Colorized] and
# the other square-bracket edition words outside _SQUARE_EDITION_WORDS (the
# scanlation-tag convention, where nearly every release carries [English]),
# and bracketed SPECIAL_WORDS such as (Omnibus),
# which the grammar writes bare after the series with no bracket to keep.
# A bare v<N> outside brackets is never a token here: name evidence alone
# never makes it a volume or a version (§3.2); it stays in the text as is.
# ---------------------------------------------------------------------------

_EDITION_BY_FOLD = {word.casefold(): word for word in EDITION_WORDS}
# The decensor marker words must be formatter edition words, so a marker is
# displayed as exactly the token format_canonical_name writes. A KeyError here,
# at import, means EDITION_WORDS and _DECENSOR_MARKER_RE have drifted apart.
_DECENSOR_EDITION = {
    marker: _EDITION_BY_FOLD[marker] for marker in ("uncensored", "decensored")
}
# Square-bracket words promoted to their round-bracket edition token, an
# explicit allowlist: David decided [Colored] only (2026-10-23), so
# [Colorized], [English] and every other square-bracket tag stay dropped.
# The decensor words need no entry: _DECENSOR_MARKER_RE already promotes them
# from any bracket. Spellings come from EDITION_WORDS (a KeyError at import
# means the two lists have drifted apart).
_SQUARE_EDITION_WORDS = {
    word: _EDITION_BY_FOLD[word] for word in ("colored",)
}
_DECENSOR_TOKEN_RE = re.compile(
    r"\((" + "|".join(re.escape(w) for w in _DECENSOR_EDITION.values()) + r")\)"
)
_DISPLAY_YEAR_RE = re.compile(r"[1-9][0-9]{3}")
_DISPLAY_VERSION_RE = re.compile(r"[vV]0*([1-9][0-9]*)")
# Bound on the display-name loops below. Each pass is a fixpoint step that
# converges in two passes for every input the property tests generate; the
# bound only stops a pathological input, which then keeps the legacy name.
_DISPLAY_MAX_PASSES = 16


@dataclass(frozen=True)
class DisplayTokens:
    """Canonical display tokens found in a name's brackets, in first-seen order.

    Duplicates are dropped. Tokens are display text taken from the name, not
    resolved fields: a ``[v2]`` here is the uploader's own version marker,
    and nothing in this module treats it as a verified version (§4.6).
    """

    years: tuple[int, ...] = ()
    editions: tuple[str, ...] = ()
    versions: tuple[int, ...] = ()


def extract_display_tokens(source: str) -> DisplayTokens:
    """Return the canonical display tokens in *source*'s bracketed blocks.

    Brackets are found with ``_BRACKET_RE``, the same pattern the identity
    rule strips, so a token is kept exactly when it sat in a block identity
    matching removes. Everything not listed in the section comment above is
    ignored.
    """
    years: list[int] = []
    editions: list[str] = []
    versions: list[int] = []

    def add(found: list, value) -> None:
        if value not in found:
            found.append(value)

    for match in _BRACKET_RE.finditer(source):
        bracket = match.group(0)
        inner = bracket[1:-1].strip()
        if bracket[0] == "(":
            if _DISPLAY_YEAR_RE.fullmatch(inner):
                add(years, int(inner))
            elif inner.casefold() in _EDITION_BY_FOLD:
                add(editions, _EDITION_BY_FOLD[inner.casefold()])
        else:
            version = _DISPLAY_VERSION_RE.fullmatch(inner)
            if version:
                add(versions, int(version.group(1)))
            elif inner.casefold() in _SQUARE_EDITION_WORDS:
                add(editions, _SQUARE_EDITION_WORDS[inner.casefold()])
        for marker in _DECENSOR_MARKER_RE.finditer(bracket):
            add(editions, _DECENSOR_EDITION[marker.group(0).casefold()])
    return DisplayTokens(tuple(years), tuple(editions), tuple(versions))


def append_display_tokens(core: str, source: str) -> str:
    """Append *source*'s display tokens to the bracket-free *core*.

    Tokens go after the core in the canonical grammar's order -- years, then
    editions, then versions -- so a name converges on the grammar's tail
    ``(<year>) (<edition>) [v<N>]`` without anything being parsed out of the
    core. A decensor edition is skipped when the core already carries the
    word bare, matching :func:`preserve_decensor_markers`, which never
    duplicates a marker either. An empty core stays empty: a name made only
    of tokens has no name, and callers already treat an empty clean as
    "fall back to the folder name".
    """
    if not core:
        return core
    tokens = display_tokens_for(core, source)
    parts = [core]
    parts.extend(f"({year})" for year in tokens.years)
    parts.extend(f"({edition})" for edition in tokens.editions)
    parts.extend(f"[v{version}]" for version in tokens.versions)
    return " ".join(parts)


def display_tokens_for(core: str, source: str) -> DisplayTokens:
    """The tokens of *source* that belong after *core*.

    :func:`extract_display_tokens`, less any decensor edition whose word the
    core already carries bare. Shared by :func:`append_display_tokens` and
    the watcher's fallback naming so the two cannot disagree on it.
    """
    tokens = extract_display_tokens(source)
    seen = {m.group(0).casefold() for m in _DECENSOR_MARKER_RE.finditer(core)}
    return DisplayTokens(
        tokens.years,
        tuple(e for e in tokens.editions if e.casefold() not in seen),
        tokens.versions,
    )


def decensor_tokens_as_words(text: str) -> str:
    """Rewrite ``(Uncensored)``/``(Decensored)`` tokens as the bare words.

    Display names write the marker as the formatter's edition token, while
    every name produced before design §5.5 carries the bare word. Comparing
    the two spellings through this function keeps them one book, exactly
    as they were when both were bare.
    """
    return _collapse_spaces(_DECENSOR_TOKEN_RE.sub(r"\1", text))


# Returned by _display_fixpoint when the bound is reached without a fixpoint.
_NO_FIXPOINT = object()


def _display_fixpoint(step, text: str):
    """Iterate a display *step* to its fixpoint.

    Returns the fixpoint; ``None`` when the very first step returned ``None``
    (the input's core is empty: the caller keeps the legacy name); or
    ``_NO_FIXPOINT`` when the bound ran out or a *later* step found an empty
    core. The latter happens when the stem pipeline exposes text the cleaner
    then removes (``"G5678-"`` -> ``"G5678"`` -> ``""``): the legacy name is
    then not a fixpoint either, so each caller resolves ``_NO_FIXPOINT`` to
    a name that is, never to the last value seen.
    """
    current = text
    for passes in range(_DISPLAY_MAX_PASSES):
        following = step(current)
        if following is None:
            return None if passes == 0 else _NO_FIXPOINT
        if following == current:
            return current
        current = following
    return _NO_FIXPOINT


def sanitize_display(text: str, rules: set[str] = ALL_RULES) -> str:
    """Clean *text* as a **display** name (design §5.5), idempotently.

    The identity clean (:func:`sanitize_without_markers`) plus the canonical
    tokens of :func:`extract_display_tokens`: ``"[Judas] Berserk (2019)
    [v2] (Digital)"`` -> ``"Berserk (2019) [v2]"``. Falls back to
    :func:`sanitize` when the core is empty, so ``"[Uncensored]"`` still
    cleans to ``"Uncensored"`` rather than to a name with nothing in front of
    its token. Without the ``brackets`` rule nothing is stripped, so there is
    nothing to keep, and this is :func:`sanitize`.
    """
    if "brackets" not in rules:
        return sanitize(text, rules)

    def step(current: str) -> str | None:
        core = sanitize_without_markers(current, rules)
        return append_display_tokens(core, current) if core else None

    # The core is already a fixpoint and the tokens re-extract identically,
    # so step settles on its second pass; _NO_FIXPOINT is not expected here
    # and is treated like an empty core, whose sanitize() result is a
    # fixpoint of this function too (no brackets are left to keep).
    display = _display_fixpoint(step, text)
    return display if isinstance(display, str) else sanitize(text, rules)


_FILENAME_EXT_RE = re.compile(r"(\.[a-zA-Z0-9]{2,4})$")


def _split_name(name: str) -> tuple[str, str]:
    """Split a bare filename string into (stem, ext) without Path() parsing.

    Path(name).stem/.suffix treats forward-slashes as directory separators on
    every platform, silently discarding everything before the last slash.
    A filename like ``Batman: Year/One?.cbz`` loses ``"Batman: Year"`` before
    sanitize() ever gets a chance to convert the slash to a space.

    This function operates purely on the string, so the full stem is preserved
    and sanitize() can handle the forbidden characters itself.
    """
    m = _FILENAME_EXT_RE.search(name)
    if m:
        return name[: m.start()], m.group(1)
    return name, ""


def _clean_filename_pass(name: str, rules: set[str]) -> str:
    stem, ext = _split_name(name)
    return sanitize(stem, rules) + ext


def clean_filename(name: str, rules: set[str] = ALL_RULES) -> str:
    """:func:`sanitize` the stem of *name* and keep its extension; idempotent.

    Repeated to a fixpoint like :func:`sanitize`, because the stem/extension
    split is re-done on the cleaned name and may land differently.
    """
    return _to_fixpoint(lambda n: _clean_filename_pass(n, rules), name)


def _clean_directory_name_pass(name: str, rules: set[str]) -> str:
    name = sanitize(name, rules)
    name = _DIR_LEADING_HASH_RE.sub("", name)
    name = _DIR_TRAILING_HASH_RE.sub("", name).strip()
    name = _DIR_TRAILING_STUB_RE.sub("", name).strip()
    return _collapse_spaces(name)


def clean_directory_name(name: str, rules: set[str] = ALL_RULES) -> str:
    """Clean a directory name for identity and folder use; idempotent.

    :func:`sanitize` plus stripping of leading/trailing ``#`` and a dangling
    ``part``/``v``/``ch`` stub. Repeated to a fixpoint: stripping one stub
    can expose another (``"Foo ch v"``). Brackets are always stripped here,
    display tokens included: the bracket policy for folder names is
    unchanged (§5.5 is about archive names; the design does not name folder
    display names). A name a single pass did not settle now cleans to its
    fixpoint.
    """
    return _to_fixpoint(lambda n: _clean_directory_name_pass(n, rules), name)


def clean_xml_field(value: str, rules: set[str] = ALL_RULES) -> str:
    """:func:`sanitize` for a ComicInfo field value; idempotent."""
    return sanitize(value, rules)


# Trailing chapter/volume/episode/issue tokens that should be stripped to recover
# a bare series title. Two variants: one that also strips a lone trailing number
# (``Berserk 4`` -> ``Berserk``) and one that does NOT, so callers that risk
# clobbering a titular number (``Area 88``, ``Mobile Suit Gundam 0079``) can opt
# out of the bare-number rule.
_TRAILING_TOKEN_BODY = (
    _NUMBER_MARKER_START + r"(?:"
    r"ch(?:ap(?:ter)?)?p?\.?\s*\d[\d.]*"
    r"|issue\s*\d[\d.]*"
    r"|ep(?:isode)?\.?\s*\d[\d.]*"
    r"|vol(?:ume)?\.?\s*\d[\d.]*"
    r"|v\d[\d.]*(?=\s*$)"
    r")"
)
_TRAILING_TOKEN_RE = re.compile(
    rf"[\s_\-]*(?:{_TRAILING_TOKEN_BODY}|\d+$)[\s_\-.,]*$",
    re.IGNORECASE,
)
_TRAILING_TOKEN_KW_RE = re.compile(
    rf"[\s_\-]*(?:{_TRAILING_TOKEN_BODY})[\s_\-.,]*$",
    re.IGNORECASE,
)


def series_base_name(name: str, bare_numbers: bool = True) -> str | None:
    """Strip a trailing chapter/volume/number token to recover the bare series title.

    Returns ``None`` if no trailing token was found (the name is already a base
    title). When *bare_numbers* is False, a lone trailing number such as the
    ``88`` in ``"Area 88"`` is left intact and only keyword-qualified tokens
    (``Ch.``, ``Vol.``, ``Episode``, ``Issue``, ``v3``) are stripped — useful
    when the caller cannot afford to clobber a number that is part of the title.
    """
    pattern = _TRAILING_TOKEN_RE if bare_numbers else _TRAILING_TOKEN_KW_RE
    m = pattern.search(name)
    if not m:
        return None
    base = name[: m.start()].strip()
    return clean_directory_name(base) if base else None


_TRAILING_BARE_NUMBER_RE = re.compile(r"[\s_\-]+(\d+(?:\.\d+)?)\s*$")


def extract_trailing_bare_number(name: str) -> str | None:
    """Return the bare trailing number in *name* (e.g. ``"2"`` from ``"Series 2"``),
    or ``None`` if *name* has no trailing number.

    Used to recover an implicit chapter/issue number from a directory name that
    carries no explicit Ch./Vol./Issue keyword (e.g. per-chapter upload folders
    named "Series", "Series 2", "Series 3"). Callers should only trust this as a
    genuine chapter marker once other evidence confirms the trailing number is not
    part of the title itself — see the bare-number evidence gating already used by
    ``series_base_name``'s callers (e.g. the watcher's ``_resolve_series_dir_name``),
    which this function is meant to be paired with.
    """
    m = _TRAILING_BARE_NUMBER_RE.search(name)
    return _num_field(m.group(1)) if m else None


def is_generic(text: str) -> bool:
    return any(r.search(text) for r in _TITLE_OVERWRITE_RES)


is_generic_title = is_generic


# One decimal number token: digits, then either one ".digits" fraction or a
# single trailing dot. Validated by fullmatch, not by stripping dots, because
# rstrip('.') turned "1.." into "1" and "1.2." into "1.2" -- multi-dot runs
# accepted as numbers they never were (#126 review).
_NUM_FIELD_RE = re.compile(r"\d+(?:\.\d+|\.)?")


def _num_field(value: str) -> str | None:
    """Canonical text of one decimal number token, or ``None`` if it is not one.

    ``"005"`` -> ``"5"``, ``"12.50"`` -> ``"12.5"``, ``"12."`` -> ``"12"``.
    The number regexes capture a digit followed by any run of digits and
    dots, so a token can carry more than one dot (``"1.2.3"``,
    ``"12.5012.5"``, ``"1.."``, ``"1.2."``) and is then not a number at all;
    a run of digits long enough to overflow ``float`` to ``inf`` is not one
    either.
    Both come back ``None`` instead of raising (issue #120): this runs inside
    ``parse_comic_name`` on every arrival, and one malformed name must not
    raise out of the watcher's per-file loop and abort its whole folder.

    The field extractors use this directly, so a malformed token leaves the
    ComicInfo field unset rather than writing a number the name never
    contained -- guessing ``"12.5"`` out of ``"12.5012.5"`` would be name
    evidence classifying a file (design §3.2).
    """
    if not _NUM_FIELD_RE.fullmatch(value):
        return None
    number = float(value)  # accepts "12." as 12.0
    try:
        return str(int(number)) if number == int(number) else str(number)
    except OverflowError:  # float() gave inf
        return None


def _fmt_num_text(value: str) -> str:
    """Canonical spelling of a number token for a *name*; total, never raises.

    A token :func:`_num_field` rejects is returned unchanged, so it stays in
    the filename as the text it arrived as, and re-parsing that name returns
    it unchanged again (the naming step stays a fixpoint).
    """
    formatted = _num_field(value)
    return value if formatted is None else formatted


def normalise_number_tokens(stem: str) -> str:
    # _fmt_num_text already leaves a malformed token as its own text, so the
    # match is rebuilt from the marker plus that text with no error handling.
    def _sub(match: re.Match[str]) -> str:
        return match.group(1) + _fmt_num_text(match.group(2))

    return _NUM_TOKEN_RE.sub(_sub, stem)


_ARCHIVE_NORM_RE = re.compile(r"[\s\-_.,!?'\"]+")


def normalise_archive_key(stem: str) -> str:
    """Collapse all whitespace/punctuation and lowercase a filename stem for duplicate comparison.

    This makes the comparison insensitive to spacing and punctuation differences,
    so files that represent the same book are treated as duplicates even when their
    names differ only cosmetically, e.g. "Series Ch.1" and "Series Ch. 1".
    """
    return _ARCHIVE_NORM_RE.sub("", stem.lower())


def normalize_stem(stem: str, dir_name: str) -> str:
    if GIBBERISH_RE.match(stem):
        return dir_name

    generic = GENERIC_CHAPTER_RE.match(stem)
    if generic:
        number = _fmt_num_text(generic.group(1))
        suffix = generic.group(2).strip(" -_")
        result = f"{dir_name} Ch. {number}"
        return result + (f" {suffix}" if suffix else "")

    chapter_only = CHAPTER_ONLY_RE.match(stem)
    if chapter_only:
        return f"{dir_name} {stem[0].upper()}{stem[1:]}"

    numbered = NUMBERED_CHAPTER_RE.match(stem)
    if numbered:
        number = numbered.group(1) or numbered.group(2)
        return f"{dir_name} Ch. {number}"

    if is_generic(stem):
        return dir_name

    return stem


def extract_chapter_number(stem: str) -> str | None:
    match = _CHAPTER_NUMBER_RE.search(stem)
    if not match:
        return None
    value = next(group for group in match.groups() if group is not None)
    return _num_field(value)


def extract_volume_number(stem: str) -> str | None:
    match = _VOLUME_NUMBER_RE.search(stem)
    if not match:
        return None
    value = match.group(1) or match.group(2)
    return _num_field(value)


def _extract_first_group(regex: re.Pattern, stem: str) -> str | None:
    """Return the first captured numeric group of *regex* in *stem*, formatted."""
    match = regex.search(stem)
    if not match:
        return None
    value = next((g for g in match.groups() if g is not None), None)
    return _num_field(value) if value is not None else None


def extract_season_number(stem: str) -> str | None:
    """Parse a season number (season N / sN) from a filename stem, or None."""
    return _extract_first_group(_SEASON_NUMBER_RE, stem)


def extract_episode_number(stem: str) -> str | None:
    """Parse an episode number (episode N / ep N / eN) from a filename stem, or None."""
    return _extract_first_group(_EPISODE_NUMBER_RE, stem)


def extract_part_number(stem: str) -> str | None:
    """Parse a part number (part N / pt N / pN) from a filename stem, or None."""
    return _extract_first_group(_PART_NUMBER_RE, stem)


def infer_series_name(path: Path, library_root: Path | None = None) -> str:
    parent = path.parent

    if library_root is not None:
        try:
            parts = list(parent.relative_to(library_root).parts)
        except ValueError:
            parts = list(parent.parts)
    else:
        parts = list(parent.parts)

    candidates = [
        p for p in parts
        if p and p.lower() not in IGNORED_SERIES_FOLDERS
    ]

    return candidates[-1] if candidates else parent.name


def _stem_pipeline(stem: str, series: str, rules: set[str]) -> str:
    """The stem normalisation that follows cleaning in :func:`parse_comic_name`."""
    if "leading_nums" in rules:
        stem = NUMBER_PREFIX_RE.sub("", stem).strip()
    stem = _SCANLATION_HASH_RE.sub("", stem).strip()
    if "normalize_stem" in rules:
        stem = normalize_stem(stem, series)
    if "number_tokens" in rules:
        stem = normalise_number_tokens(stem)
    if "trailing_junk" in rules:
        stem = TRAILING_JUNK_RE.sub("", stem).strip()
    return stem


def _display_stem(
    name: str,
    raw_stem: str,
    series: str,
    rules: set[str],
    translated_stem: str | None,
) -> str | None:
    """The display stem for :func:`parse_comic_name`, or ``None`` for legacy.

    * A name the canonical formatter already produced is kept verbatim:
      re-running the stem pipeline would undo its padding (``v01`` ->
      ``v1``), and keeping it classifies nothing. "Already produced" needs
      more than ``parse_canonical_name``, whose series slot accepts any text:
      ``[Judas] Berserk.cbz`` parses, with series ``[Judas] Berserk``. So
      the series must also be one the cleaning and stem pipeline leave
      alone.
    * A translated name keeps the translation and gains the tokens.
    * Otherwise the stem pipeline runs on the identity core and the tokens
      are appended, repeated to a fixpoint so the watcher's naming step is a
      no-op on its own output.

    ``None`` -- an empty core or no ``brackets`` rule -- means the caller
    keeps today's name. A step with no fixpoint keeps the arrival's own
    stem: ``normalize_stem`` can prefix the folder name forever when the
    folder itself starts with a chapter token (a folder ``ch. 1``), and the
    pipeline can expose text the cleaner then removes (``G5678-`` ->
    ``G5678`` -> empty). Today's name would change again on the next pass in
    both cases; the arrival's name does not, and nothing is named by a guess.
    """
    if "brackets" not in rules:
        return None
    try:
        fields = parse_canonical_name(name)
    except CanonicalNameError:
        pass
    else:
        core = sanitize_without_markers(fields.series, rules)
        if core == fields.series and _stem_pipeline(core, series, rules) == core:
            return raw_stem
    if translated_stem is not None:
        return append_display_tokens(translated_stem, raw_stem) or None

    def step(current: str) -> str | None:
        core = _stem_pipeline(sanitize_without_markers(current, rules), series, rules)
        return append_display_tokens(core, current) if core else None

    display = _display_fixpoint(step, raw_stem)
    return raw_stem if display is _NO_FIXPOINT else display


def parse_comic_name(
    path: Path,
    rules: set[str] = ALL_RULES,
    library_root: Path | None = None,
    *,
    display_tokens: bool = False,
) -> ParsedComicName:
    """Parse *path* into the names and fields the import pipeline writes.

    With ``display_tokens=True`` (the watcher's arrival path) ``filename``
    keeps the canonical display tokens of design §5.5 -- ``(YYYY)``,
    ``[vN]``, ``(Edition)`` -- and is a fixpoint of this function. Every
    other field, ``stem`` and ``series`` included, is computed exactly as
    without it, so ComicInfo and chapter and volume extraction keep their
    bracket policy. The default stays ``False`` because the library
    maintenance and sanitizer CLIs use this function on files already in the
    library, and the display tokens are for arrivals only. Those callers do
    see the fixpoint change in :func:`sanitize` for inputs a single pass did
    not settle.
    """
    raw_series = infer_series_name(path, library_root=library_root)
    series = clean_directory_name(raw_series, rules)

    # Capture the raw title (filename stem before cleaning) so we can detect a
    # CJK-only name and translate it. clean_filename() strips most non-Latin
    # text, so this must happen on the raw stem.
    raw_stem, _raw_ext = _split_name(path.name)

    stem, ext = _split_name(clean_filename(path.name, rules))
    stem = _stem_pipeline(stem, series, rules)
    translated_stem: str | None = None

    chapter = extract_chapter_number(stem)
    volume = extract_volume_number(stem) or extract_volume_number(series)

    # Season / Episode / Part are captured independently of chapter/volume. They
    # are looked for in the cleaned stem and, failing that, the raw stem (the
    # cleaning pipeline may drop a token like "Ep. 1"). Chapter still owns the
    # native ComicInfo <Number>; these are recorded separately in <Notes>.
    season = extract_season_number(stem) or extract_season_number(raw_stem)
    episode = extract_episode_number(stem) or extract_episode_number(raw_stem)
    part = extract_part_number(stem) or extract_part_number(raw_stem)

    # Fallback: pull chapter/volume from CJK markers (第N話/N話 -> chapter,
    # 第N巻/N巻 -> volume) when Latin-token extraction found nothing.
    # The number is the whole digit-and-dot run before the marker, never a
    # fragment of it: "1.2.3話" used to yield "3" and "12.5話" "5" (#120).
    # The run goes through _num_field like every other token, so a
    # malformed one leaves the field unset instead of a guessed number.
    # The greedy run already starts at the leftmost digit; the lookbehind is
    # for a run that starts with a dot (".5話"), whose "5" is a fragment too.
    if chapter is None:
        m = re.search(r"第?\s*(?<![\d.])(\d[\d.]*)\s*[話话章]", raw_stem)
        if m:
            chapter = _num_field(m.group(1))
    if volume is None:
        m = re.search(r"第?\s*(?<![\d.])(\d[\d.]*)\s*[巻卷]", raw_stem)
        if m:
            volume = _num_field(m.group(1))

    # Translation handling: native CJK titles are always eligible; romanized
    # Japanese/Korean/Chinese titles are eligible only after conservative
    # language detection in translate_metadata_text().
    original_title: str | None = None
    translated_title: str | None = None
    if "translate" in rules:
        source = raw_stem
        english, original = translate_metadata_text(source)
        if original is None and raw_series != raw_stem:
            source = raw_series
            english, original = translate_metadata_text(source)
        if original is not None:
            # Translate only the title portion; strip chapter/volume tokens so the
            # translator sees just the name (e.g. "鬼滅の刃 Ch. 5" -> "鬼滅の刃").
            title_part = _NUM_TOKEN_RE.sub("", source)
            title_part = re.sub(
                r"\b(?:ch|chap|chapter|chp|vol|volume|v|ep|episode|issue|part)\b\.?",
                "", title_part, flags=re.IGNORECASE,
            )
            # CJK volume/chapter markers: 第N話 / 第N巻 / 第N章 and bare N話/N巻/N章.
            title_part = re.sub(r"第?\s*\d+\s*[話巻章话卷]", "", title_part).strip(" .-_#")
            title_part = title_part or source
            english, original = translate_metadata_text(title_part)
            if original is not None and english and english != title_part:
                original_title = original
                translated_title = english
                # Anglicise the on-disk filename when the cleaned stem is still
                # non-Latin (empty, punctuation-only, or still CJK), so the file
                # gets a human/Komga-readable name. The untouched original and the
                # translation are preserved in ComicInfo metadata regardless.
                if source == raw_stem and (
                    not stem
                    or not _LATIN_RE.search(stem)
                    or _CJK_RE.search(stem)
                    or _looks_romanized_cjk(title_part, None)
                ):
                    english_stem = sanitize(english, rules) or english
                    rebuilt = english_stem
                    if chapter:
                        rebuilt = f"{english_stem} Ch. {chapter}"
                    elif volume:
                        rebuilt = f"{english_stem} Vol. {volume}"
                    stem = rebuilt.strip() or stem
                    translated_stem = stem

    display = (
        _display_stem(path.name, raw_stem, series, rules, translated_stem)
        if display_tokens
        else None
    )

    stem = preserve_decensor_markers(raw_stem, stem)
    series = preserve_decensor_markers(raw_series, series)

    return ParsedComicName(
        original_path=path,
        filename=(display if display is not None else stem) + ext,
        stem=stem,
        series=series,
        chapter=chapter,
        volume=volume,
        season=season,
        episode=episode,
        part=part,
        original_title=original_title,
        translated_title=translated_title,
    )


def _ensure_child(root: ET.Element, tag: str) -> ET.Element:
    child = root.find(tag)
    if child is None:
        child = ET.SubElement(root, tag)
    return child


_TRANSLATABLE_COMICINFO_FIELDS = (
    "Title",
    "Series",
    "LocalizedSeries",
    "AlternateSeries",
    "Summary",
    "Genre",
    "Tags",
    "Characters",
    "Teams",
    "Locations",
    "StoryArc",
    "SeriesGroup",
)


def _append_original_note(root: ET.Element, field: str, original: str) -> bool:
    note = f"Original {field}: {original}"
    notes_el = _ensure_child(root, "Notes")
    existing = (notes_el.text or "").strip()
    if note in existing:
        return False
    notes_el.text = f"{existing} | {note}".strip(" |") if existing else note
    return True


def _translate_comicinfo_fields(root: ET.Element) -> bool:
    changed = False
    for field in _TRANSLATABLE_COMICINFO_FIELDS:
        element = root.find(field)
        if element is None or not (element.text or "").strip():
            continue
        original = (element.text or "").strip()
        translated, source = translate_metadata_text(original)
        if source is None or translated == original:
            continue
        element.text = preserve_decensor_markers(original, translated)
        changed = True
        if field != "AlternateSeries":
            changed = _append_original_note(root, field, source) or changed
    return changed


def build_sep_notes(
    existing_notes: str,
    season: str | None,
    episode: str | None,
    part: str | None,
) -> tuple[str, bool]:
    """Merge a Season/Episode/Part line into *existing_notes* idempotently.

    Returns (new_notes, changed). Any prior SEP segment is replaced rather than
    duplicated, and non-SEP notes are preserved. When no S/E/P is present the
    notes are returned unchanged.
    """
    sep_bits = []
    if season:
        sep_bits.append(f"Season: {season}")
    if episode:
        sep_bits.append(f"Episode: {episode}")
    if part:
        sep_bits.append(f"Part: {part}")

    existing = (existing_notes or "").strip()
    kept = [
        seg.strip() for seg in existing.split(" | ")
        if seg.strip() and not _SEP_NOTE_SEG_RE.match(seg.strip())
    ]
    if not sep_bits:
        # Nothing to add; only changed if we'd be dropping a stale SEP line, which
        # we don't do when there's nothing new to write.
        return existing, False
    sep_line = " | ".join(sep_bits)
    new_notes = " | ".join([*kept, sep_line]) if kept else sep_line
    return new_notes, new_notes != existing


def update_comicinfo_xml(
    xml_text: str,
    parsed: ParsedComicName,
    overwrite_generic: bool = True,
) -> tuple[str, bool]:
    """Update ComicInfo XML with normalized fields.

    Title replacement is conservative:
    - missing title
    - title equals series
    - title is generic/gibberish and overwrite_generic is True

    Custom titles are preserved.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        root = ET.Element("ComicInfo")

    changed = False

    # Translate existing user/source metadata before applying normalized values.
    # English or mixed English/original values are preserved by the eligibility
    # checks in translate_metadata_text().
    changed = _translate_comicinfo_fields(root) or changed

    title_el = _ensure_child(root, "Title")
    series_el = _ensure_child(root, "Series")

    # Always repair mojibake in an existing title, independent of whether the
    # title is otherwise replaced — a corrupted but "custom" title must not be
    # preserved verbatim.
    raw_title = (title_el.text or "")
    raw_series = (series_el.text or "")
    repaired_title = repair_mojibake(raw_title)
    if repaired_title != raw_title:
        title_el.text = repaired_title
        changed = True

    current_title = clean_xml_field((title_el.text or "").strip())
    should_replace_title = (
        not current_title
        or current_title == parsed.series
        or (
            overwrite_generic
            and (is_generic_title(current_title) or bool(GIBBERISH_RE.match(current_title)))
        )
    )

    if should_replace_title:
        new_title = parsed.stem
        if is_generic_title(new_title) or GIBBERISH_RE.match(new_title):
            new_title = parsed.series
        new_title = preserve_decensor_markers(raw_title, new_title)
        if (title_el.text or "").strip() != new_title:
            title_el.text = new_title
            changed = True

    target_series = parsed.series
    translated_series, original_series = translate_metadata_text(target_series)
    if original_series is not None:
        target_series = translated_series
        changed = _append_original_note(root, "Series", original_series) or changed
    target_series = preserve_decensor_markers(raw_series, target_series)

    if (series_el.text or "").strip() != target_series:
        series_el.text = target_series
        changed = True

    if parsed.chapter:
        number_el = _ensure_child(root, "Number")
        if (number_el.text or "").strip() != parsed.chapter:
            number_el.text = parsed.chapter
            changed = True

    if parsed.volume:
        volume_el = _ensure_child(root, "Volume")
        if (volume_el.text or "").strip() != parsed.volume:
            volume_el.text = parsed.volume
            changed = True

    # Season / Episode / Part have no native ComicInfo fields. Record any that
    # were detected as a single structured, idempotent line in <Notes>. Chapter
    # keeps the native <Number>; this never touches it.
    if parsed.season or parsed.episode or parsed.part:
        notes_el = _ensure_child(root, "Notes")
        new_notes, notes_changed = build_sep_notes(
            notes_el.text or "", parsed.season, parsed.episode, parsed.part
        )
        if notes_changed:
            notes_el.text = new_notes
            changed = True

    # Keep the original-language title in AlternateSeries. Title/Series and the
    # filename carry the English rendering; Notes retains an explicit breadcrumb.
    if parsed.translated_title:
        alt_el = _ensure_child(root, "AlternateSeries")
        alternate = preserve_decensor_markers(
            alt_el.text or "", parsed.original_title or parsed.translated_title
        )
        if (alt_el.text or "").strip() != alternate:
            alt_el.text = alternate
            changed = True

        if parsed.original_title:
            changed = _append_original_note(root, "Title", parsed.original_title) or changed

    if not changed:
        return xml_text, False

    return ET.tostring(root, encoding="unicode"), True
