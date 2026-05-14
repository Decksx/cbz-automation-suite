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

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ALL_RULES",
    "GIBBERISH_RE",
    "IGNORED_SERIES_FOLDERS",
    "NUMBER_PREFIX_RE",
    "TRAILING_JUNK_RE",
    "ParsedComicName",
    "clean_directory_name",
    "clean_filename",
    "clean_xml_field",
    "extract_chapter_number",
    "extract_volume_number",
    "infer_series_name",
    "is_generic",
    "is_generic_title",
    "normalise_number_tokens",
    "normalize_stem",
    "parse_comic_name",
    "parse_rules",
    "sanitize",
    "shorten_mixed_original_title",
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
GIBBERISH_RE = re.compile(
    r"^(?:TEMP[\s_-]*[0-9a-f]{8,}|[0-9a-f]{16,}"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    re.IGNORECASE,
)

_BRACKET_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)")
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

_NUM_TOKEN_RE = re.compile(
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
    r"[\s_\-]*(?:part|v|ch(?:ap(?:ter)?)?)\s*$", re.IGNORECASE
)
_CHAPTER_NUMBER_RE = re.compile(
    r"(?:"
    r"ch(?:ap(?:ter)?)?p?\.?\s*(\d[\d.]*)"
    r"|issue\s*(\d[\d.]*)"
    r"|ep(?:isode)?\.?\s*(\d[\d.]*)"
    r"|#\s*(\d[\d.]*)"
    r")",
    re.IGNORECASE,
)
_VOLUME_NUMBER_RE = re.compile(
    r"(?:"
    r"vol(?:ume)?\.?\s*(\d[\d.]*)"
    r"|v(\d[\d.]*)(?=\s|ch|ep|$)"
    r")",
    re.IGNORECASE,
)
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
    original_path: Path
    filename: str
    stem: str
    series: str
    chapter: str | None
    volume: str | None


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


def sanitize(text: str, rules: set[str] = ALL_RULES) -> str:
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


def clean_filename(name: str, rules: set[str] = ALL_RULES) -> str:
    stem, ext = _split_name(name)
    return sanitize(stem, rules) + ext


def clean_directory_name(name: str, rules: set[str] = ALL_RULES) -> str:
    name = sanitize(name, rules)
    name = _DIR_LEADING_HASH_RE.sub("", name)
    name = _DIR_TRAILING_HASH_RE.sub("", name).strip()
    name = _DIR_TRAILING_STUB_RE.sub("", name).strip()
    return _collapse_spaces(name)


def clean_xml_field(value: str, rules: set[str] = ALL_RULES) -> str:
    return sanitize(value, rules)


def is_generic(text: str) -> bool:
    return any(r.search(text) for r in _TITLE_OVERWRITE_RES)


is_generic_title = is_generic


def _fmt_num_text(value: str) -> str:
    number = float(value)
    return str(int(number)) if number == int(number) else str(number)


def normalise_number_tokens(stem: str) -> str:
    def _sub(match: re.Match[str]) -> str:
        try:
            return match.group(1) + _fmt_num_text(match.group(2))
        except ValueError:
            return match.group(0)

    return _NUM_TOKEN_RE.sub(_sub, stem)


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
    return _fmt_num_text(value)


def extract_volume_number(stem: str) -> str | None:
    match = _VOLUME_NUMBER_RE.search(stem)
    if not match:
        return None
    value = match.group(1) or match.group(2)
    return _fmt_num_text(value)


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


def parse_comic_name(
    path: Path,
    rules: set[str] = ALL_RULES,
    library_root: Path | None = None,
) -> ParsedComicName:
    raw_series = infer_series_name(path, library_root=library_root)
    series = clean_directory_name(raw_series, rules)

    stem, ext = _split_name(clean_filename(path.name, rules))
    if "leading_nums" in rules:
        stem = NUMBER_PREFIX_RE.sub("", stem).strip()
    if "normalize_stem" in rules:
        stem = normalize_stem(stem, series)
    if "number_tokens" in rules:
        stem = normalise_number_tokens(stem)
    if "trailing_junk" in rules:
        stem = TRAILING_JUNK_RE.sub("", stem).strip()

    chapter = extract_chapter_number(stem)
    volume = extract_volume_number(stem) or extract_volume_number(series)

    return ParsedComicName(
        original_path=path,
        filename=stem + ext,
        stem=stem,
        series=series,
        chapter=chapter,
        volume=volume,
    )


def _ensure_child(root: ET.Element, tag: str) -> ET.Element:
    child = root.find(tag)
    if child is None:
        child = ET.SubElement(root, tag)
    return child


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

    title_el = _ensure_child(root, "Title")
    series_el = _ensure_child(root, "Series")

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
        if (title_el.text or "").strip() != new_title:
            title_el.text = new_title
            changed = True

    if (series_el.text or "").strip() != parsed.series:
        series_el.text = parsed.series
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

    if not changed:
        return xml_text, False

    return ET.tostring(root, encoding="unicode"), True
