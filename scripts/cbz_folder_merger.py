"""
CBZ Folder Merger
Scans a library root for sibling directories that represent the same series
split across chapter-numbered folders (e.g. "Batman ch. 1", "batman ch2",
"Batman Chapter 7") and merges them into a single clean folder ("Batman").

After merging, every .cbz in the new folder has its ComicInfo.xml updated
with the correct Series, Title, Number, and Volume tags derived from the
filename.

Usage:
    python cbz_folder_merger.py                              # interactive prompt to choose a path, or scan all SCAN_FOLDERS
    python cbz_folder_merger.py "\\\\tower\\media\\Comix"    # UNC network path
    python cbz_folder_merger.py "L:\\Comix"                  # local drive path
    python cbz_folder_merger.py "C:\\Comics\\Batman"         # single series directory
    python cbz_folder_merger.py --dry-run                    # preview without writing
    python cbz_folder_merger.py "L:\\Comix" --dry-run        # local drive, preview only
"""

import os
import gc
import re
import sys
import html
import shutil
import zipfile
import logging
import statistics
from collections import defaultdict
from logging.handlers import RotatingFileHandler as _RotatingFileHandler
from pathlib import Path

# ─────────────────────────────────────────────
# CONFIGURATION — edit these as needed
# ─────────────────────────────────────────────
SCAN_FOLDERS = [
    r"\\tower\media\comics\Comix",
    r"\\tower\media\comics\Manga",
]
LOG_FILE = r"C:\git\ComicAutomation\cbz_folder_merger.log"
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        _RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024,
                             backupCount=3, encoding="utf-8"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# PATTERNS (compiled once)
# ─────────────────────────────────────────────

# Matches a trailing chapter/volume/number token at the END of a folder name.
# Used to strip the number and recover the series base name.
_TRAILING_TOKEN_RE = re.compile(
    r'[\s_\-]*'
    r'(?:'
    r'ch(?:ap(?:ter)?)?p?\.?\s*\d[\d.]*'    # ch/chap/chapter/chp + number
    r'|issue\s*\d[\d.]*'                     # issue + number
    r'|ep(?:isode)?\.?\s*\d[\d.]*'          # ep/episode + number
    r'|vol(?:ume)?\.?\s*\d[\d.]*'           # vol/volume + number
    r'|v\d[\d.]*(?=\s*$)'                   # v3 at end
    r'|\d+$'                                 # bare trailing number
    r')'
    r'[\s_\-.,]*$',
    re.IGNORECASE
)

# Matches chapter number inside a filename stem (keyword required)
_CHAPTER_RE = re.compile(
    r'(?:'
    r'ch(?:ap(?:ter)?)?p?\.?\s*(\d[\d.]*)'
    r'|issue\s*(\d[\d.]*)'
    r'|ep(?:isode)?\.?\s*(\d[\d.]*)'
    r'|#\s*(\d[\d.]*)'
    r')',
    re.IGNORECASE
)

# Matches volume number inside a filename stem
_VOLUME_RE = re.compile(
    r'(?:'
    r'vol(?:ume)?\.?\s*(\d[\d.]*)'
    r'|v(\d[\d.]*)(?=\s|ch|ep|$)'
    r')',
    re.IGNORECASE
)

# Stems that contain no series info — just a chapter/number reference
_GENERIC_STEM_RE = re.compile(
    r'^(?:'
    r'ch(?:ap(?:ter)?)?p?\.?\s*\d[\d.]*'   # ch. 1, chapter 1, chp.1
    r'|ep(?:isode)?\.?\s*\d[\d.]*'         # ep 1, episode 3
    r'|issue\s*\d[\d.]*'                    # issue 5
    r'|vol(?:ume)?\.?\s*\d[\d.]*'          # vol. 3
    r'|#\s*\d[\d.]*'                        # # 12
    r'|\d{1,4}[\d.]*'                        # bare number: 001, 1, 12.5
    r'|chapter$'                               # bare "chapter" with no number
    r'|episode$'                               # bare "episode"
    r')$',
    re.IGNORECASE
)

# Characters illegal in Windows folder names
_ILLEGAL_CHARS_RE  = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_BRACKET_RE        = re.compile(r'\[[^\]]*\]|\([^)]*\)')
_STRAY_RE          = re.compile(r'[\[\]()]')
_SPACES_RE         = re.compile(r' {2,}')
_URL_RE            = re.compile(
    r'(?:https?://\S+)|(?:www\.\S+)'
    r'|(?:\b[\w-]+\.(?:com|net|org|io|co|info|biz|tv|me|cc|us|uk|ca|au)(?:/\S*)?)'
    , re.IGNORECASE)
_SCAN_GROUP_RE     = re.compile(
    r'\b[\w-]*scans?\b|\b[\w-]*scanners?\b|\b[\w-]*scanlations?\b', re.IGNORECASE)
_GCODE_RE          = re.compile(r'[\s\-]*\bG\d{3,5}$')
_TRAILING_SLASH_RE = re.compile(r'[\s/]+$')
_NON_LATIN_RE      = re.compile(
    r'[^\u0000-\u024F'
    r'\u0370-\u03FF'
    r'\u2000-\u206F'
    r'\u2600-\u27BF'
    r'\uFE00-\uFE0F'
    r'\U0001F300-\U0001FAFF'
    r']+'
)
_DIR_LEADING_HASH_RE  = re.compile(r'^#+\s*')
_DIR_TRAILING_HASH_RE = re.compile(r'\s*#+$')
_DIR_TRAILING_STUB_RE = re.compile(
    r'[\s_\-]*(?:part|v|ch(?:ap(?:ter)?)?)\s*$', re.IGNORECASE
)


def sanitize(text: str) -> str:
    """Sanitize pipeline: entities, URLs, scan-groups, trailing-slash, G-code,
    non-Latin, brackets, stray brackets, underscores, collapse whitespace."""
    text = html.unescape(text)
    text = _URL_RE.sub("", text)
    text = _SCAN_GROUP_RE.sub("", text)
    text = _TRAILING_SLASH_RE.sub("", text)
    text = _GCODE_RE.sub("", text)
    text = _NON_LATIN_RE.sub("", text)
    text = _BRACKET_RE.sub("", text)
    text = _STRAY_RE.sub("", text)
    text = text.replace("_", " ")
    return _SPACES_RE.sub(" ", text).strip()


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _fmt_num(n: float) -> str:
    return str(int(n)) if n == int(n) else str(n)


def get_base(dir_name: str) -> str | None:
    """
    Strip the trailing chapter/number token from a directory name.
    Returns the normalised (lowercase, collapsed-spaces) base, or None if the
    name has no trailing number token (i.e. it's not an enumerated folder).
    """
    m = _TRAILING_TOKEN_RE.search(dir_name)
    if not m:
        return None
    base = dir_name[:m.start()].strip()
    if not base:
        return None
    return re.sub(r'\s+', ' ', base).lower()


def canonical_name(dir_names: list[str]) -> str:
    """
    Derive the clean merged-folder name from a list of enumerated folder names.
    Strips the number from each and picks the best-capitalised result.
    Falls back to title-casing the normalised base.
    """
    candidates = []
    for name in dir_names:
        m = _TRAILING_TOKEN_RE.search(name)
        if m:
            stripped = name[:m.start()].strip()
            if stripped:
                candidates.append(stripped)

    if not candidates:
        return dir_names[0]

    # Prefer the candidate with the most uppercase letters (most intentional casing)
    best = max(candidates, key=lambda s: sum(1 for c in s if c.isupper()))
    # Run full sanitize pipeline then apply directory-specific cleaning
    best = sanitize(best)
    best = _ILLEGAL_CHARS_RE.sub("", best).strip()
    best = _DIR_LEADING_HASH_RE.sub('', best)
    best = _DIR_TRAILING_HASH_RE.sub('', best).strip()
    best = _DIR_TRAILING_STUB_RE.sub('', best).strip()
    return best or dir_names[0]


def extract_chapter(stem: str) -> float | None:
    m = _CHAPTER_RE.search(stem)
    if m:
        val = next(g for g in m.groups() if g is not None)
        return float(val)
    return None


def extract_volume(stem: str) -> str | None:
    m = _VOLUME_RE.search(stem)
    if m:
        val = m.group(1) or m.group(2)
        return _fmt_num(float(val))
    return None


def is_generic_stem(stem: str) -> bool:
    """
    Return True if the filename stem contains no series-specific info —
    i.e. it's just a bare chapter/number/episode keyword with no title prefix.
    """
    return bool(_GENERIC_STEM_RE.match(stem.strip()))


def rename_generic_files(src_dir: Path, dry_run: bool = False) -> list[Path]:
    """
    Rename any .cbz files inside src_dir whose stems are generic
    (e.g. 'chapter.cbz', '001.cbz') to use the directory name instead.
    Single file  → '{dir_name}.cbz'
    Multiple files → '{dir_name} 1.cbz', '{dir_name} 2.cbz', ...
    Returns the updated list of cbz paths (renamed paths replace originals).
    """
    cbz_files    = sorted(src_dir.glob('*.cbz'))
    generic_files = [f for f in cbz_files if is_generic_stem(f.stem)]

    if not generic_files:
        return cbz_files

    dir_name = src_dir.name
    log.info(f"    Renaming {len(generic_files)} generic file(s) in '{dir_name}':")

    # Build new names — use index suffix only when multiple generic files exist
    renames: list[tuple[Path, Path]] = []
    for i, cbz in enumerate(generic_files):
        suffix = f" {i + 1}" if len(generic_files) > 1 else ""
        new_name = f"{dir_name}{suffix}.cbz"
        new_path = src_dir / new_name
        renames.append((cbz, new_path))

    result_paths: list[Path] = list(cbz_files)  # start with full list
    for old_path, new_path in renames:
        if old_path == new_path:
            continue
        if dry_run:
            log.info(f"      [DRY RUN] {old_path.name!r} -> {new_path.name!r}")
        else:
            if new_path.exists():
                # Keep larger file on collision
                if old_path.stat().st_size > new_path.stat().st_size:
                    new_path.unlink()
                    old_path.rename(new_path)
                    log.info(f"      Renamed (replaced smaller): {old_path.name!r} -> {new_path.name!r}")
                else:
                    old_path.unlink()
                    log.info(f"      Discarded (collision, kept larger): {old_path.name!r}")
            else:
                old_path.rename(new_path)
                log.info(f"      Renamed: {old_path.name!r} -> {new_path.name!r}")
            # Update path in result list
            if old_path in result_paths:
                idx = result_paths.index(old_path)
                result_paths[idx] = new_path

    return sorted(p for p in result_paths if p.exists())


# ─────────────────────────────────────────────
# COMICINFO.XML UPDATE
# ─────────────────────────────────────────────

def _set_or_insert_tag(xml: str, tag: str, value: str,
                       insert_after: str | None = None) -> tuple[str, bool]:
    """
    Set <tag>value</tag> in xml. If the tag already exists, replace it.
    If it doesn't exist, insert it after the <insert_after> tag (or before
    </ComicInfo> as a fallback). Returns (new_xml, changed).
    """
    pattern = re.compile(rf"<{tag}>.*?</{tag}>", re.IGNORECASE | re.DOTALL)
    new_tag  = f"<{tag}>{value}</{tag}>"

    if pattern.search(xml):
        existing_val = pattern.search(xml).group(0)
        if existing_val == new_tag:
            return xml, False
        return pattern.sub(new_tag, xml, count=1), True

    # Insert after the anchor tag if present
    if insert_after:
        anchor = re.compile(
            rf"(</{insert_after}>)", re.IGNORECASE
        )
        if anchor.search(xml):
            return anchor.sub(rf"\1\n  {new_tag}", xml, count=1), True

    # Fallback: before </ComicInfo>
    return xml.replace("</ComicInfo>", f"  {new_tag}\n</ComicInfo>"), True


def update_comicinfo(cbz_path: Path, series: str, dry_run: bool = False) -> bool:
    """
    Update (or inject) ComicInfo.xml in a CBZ with:
      - Series  = series (the merged folder name)
      - Title   = cbz filename stem (the chapter filename is the best title)
      - Number  = chapter number extracted from filename
      - Volume  = volume number extracted from filename (if present)

    Returns True if the file was (or would be) modified.
    """
    stem       = cbz_path.stem
    chapter    = extract_chapter(stem)
    volume     = extract_volume(stem)

    # ── Read existing ComicInfo.xml ──────────────────────────────────────────
    real_name  = None
    xml        = None
    zip_entries: list[tuple] = []

    try:
        with zipfile.ZipFile(cbz_path, "r") as zin:
            nl_lower = {n.lower(): n for n in zin.namelist()}
            key = next(
                (k for k in nl_lower
                 if os.path.basename(k).lower() == "comicinfo.xml"),
                None
            )
            if key:
                real_name = nl_lower[key]
                xml = zin.read(real_name).decode("utf-8", errors="replace")
            for item in zin.infolist():
                zip_entries.append((item, zin.read(item.filename)))
    except zipfile.BadZipFile:
        log.error(f"    Bad zip: {cbz_path.name} — skipping comicinfo update.")
        return False
    except OSError as e:
        log.error(f"    Cannot read {cbz_path.name}: {e}")
        return False

    # ── Build XML ────────────────────────────────────────────────────────────
    TEMPLATE = (
        '<ComicInfo\n'
        '  xmlns:xsd="http://www.w3.org/2001/XMLSchema"\n'
        '  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
        '  <Title></Title>\n'
        '  <Series></Series>\n'
        '  <Number></Number>\n'
        '</ComicInfo>'
    )

    if xml is None:
        xml = TEMPLATE
        real_name = None   # will be injected fresh

    changed = False

    # Series
    xml, c = _set_or_insert_tag(xml, "Series", series, insert_after="Title")
    changed = changed or c

    # Title = filename stem (most descriptive thing we have)
    xml, c = _set_or_insert_tag(xml, "Title", stem)
    changed = changed or c

    # Volume — insert after Series
    if volume:
        xml, c = _set_or_insert_tag(xml, "Volume", volume, insert_after="Series")
        changed = changed or c

    # Number — insert after Series (or Volume if present)
    if chapter:
        anchor = "Volume" if volume else "Series"
        xml, c = _set_or_insert_tag(xml, "Number", _fmt_num(chapter), insert_after=anchor)
        changed = changed or c

    if not changed:
        log.info(f"    ComicInfo OK (no changes): {cbz_path.name}")
        return False

    tags = []
    if chapter: tags.append(f"Number={_fmt_num(chapter)}")
    if volume:  tags.append(f"Volume={volume}")
    tags.append(f"Series={series!r}")

    if dry_run:
        log.info(f"    [DRY RUN] Would update ComicInfo ({', '.join(tags)}): {cbz_path.name}")
        return True

    # ── Write updated zip ────────────────────────────────────────────────────
    tmp_path = cbz_path.with_suffix(".tmp.cbz")
    bak_path = cbz_path.with_suffix(".bak.cbz")
    try:
        with zipfile.ZipFile(tmp_path, "w") as zout:
            for item, data in zip_entries:
                if real_name and item.filename == real_name:
                    zout.writestr(item, xml.encode("utf-8"))
                else:
                    zout.writestr(item, data, compress_type=item.compress_type)
            if real_name is None:
                # Fresh inject
                zout.writestr("ComicInfo.xml", xml.encode("utf-8"),
                              compress_type=zipfile.ZIP_DEFLATED)

        cbz_path.rename(bak_path)
        tmp_path.rename(cbz_path)
        bak_path.unlink(missing_ok=True)
        log.info(f"    ComicInfo updated ({', '.join(tags)}): {cbz_path.name}")
        return True

    except Exception as e:
        log.error(f"    Failed to write ComicInfo for {cbz_path.name}: {e}")
        if tmp_path.exists():
            tmp_path.unlink()
        return False


# ─────────────────────────────────────────────
# DIRECTORY MERGE
# ─────────────────────────────────────────────

def merge_dirs(src: Path, dest: Path, dry_run: bool = False) -> int:
    """
    Move all .cbz files from src into dest.
    On filename collision, keep the larger file.
    Returns the number of files moved.
    """
    moved = 0
    for src_file in sorted(src.rglob("*.cbz")):
        dest_file = dest / src_file.name

        if dest_file.exists():
            ss = src_file.stat().st_size
            ds = dest_file.stat().st_size
            if ss > ds:
                log.info(
                    f"    Collision '{src_file.name}': "
                    f"incoming ({ss:,}B) > existing ({ds:,}B) — replacing."
                )
                if not dry_run:
                    dest_file.unlink()
                    shutil.move(str(src_file), str(dest_file))
                moved += 1
            else:
                log.info(
                    f"    Collision '{src_file.name}': "
                    f"existing ({ds:,}B) >= incoming ({ss:,}B) — keeping existing."
                )
                if not dry_run:
                    src_file.unlink()
        else:
            if dry_run:
                log.info(f"    [DRY RUN] Would move: {src_file.name}")
            else:
                shutil.move(str(src_file), str(dest_file))
                log.info(f"    Moved: {src_file.name}")
            moved += 1

    if not dry_run and src.exists():
        shutil.rmtree(src, ignore_errors=True)

    return moved



# ─────────────────────────────────────────────
# COMPILATION DETECTION
# Detects chapter numbers that are likely concatenations of two
# earlier chapter numbers (e.g. ch.12 in a series with ch.1 and ch.2)
# and renames the file to use a hyphenated range (ch.1-2).
# ─────────────────────────────────────────────

_CHAPTER_TOKEN_RE = re.compile(
    r'(ch(?:ap(?:ter)?)?p?\.?\s*)(\d[\d.]*)',
    re.IGNORECASE
)


def _detect_compilation_candidates(
    chapter_nums: list[float],
) -> list[tuple[float, float, float]]:
    """
    Given a sorted list of chapter numbers for a series, return a list of
    (suspect, start, end) tuples where 'suspect' is a chapter number that
    appears to be a compilation of chapters start through end.

    Detection criteria (all must be true):
      1. suspect is larger than all other chapter numbers
      2. The gap from the previous chapter to suspect is > 2x the median gap
         of the rest (flags an outlier jump)
      3. str(suspect) starts with str(start) and the remainder equals str(end),
         where end is a chapter number that exists in the series and is
         <= max(others) (the concat digits must represent real chapter numbers)
    """
    if len(chapter_nums) < 2:
        return []

    # Ensure all values are float so arithmetic never fails on mixed int/str types
    nums    = sorted(set(float(n) for n in chapter_nums))
    results = []

    for suspect in nums:
        others = [n for n in nums if n != suspect]
        if not others:
            continue

        # Must be the largest number in the series
        if suspect <= max(others):
            continue

        # Gap must be an unusual outlier
        gaps = [others[j + 1] - others[j] for j in range(len(others) - 1)]
        gap_to_suspect = suspect - max(others)
        if gaps:
            median_gap = statistics.median(gaps)
            if median_gap > 0 and gap_to_suspect <= 2 * median_gap:
                continue
            if median_gap == 0 and gap_to_suspect <= 2:
                continue
        # Single-chapter series: just require a gap > 2 to trigger
        elif gap_to_suspect <= 2:
            continue

        # Concatenation check
        suspect_str = _fmt_num(suspect)
        found_start = found_end = None
        for a in others:
            a_str = _fmt_num(a)
            if len(a_str) >= len(suspect_str):
                continue
            if not suspect_str.startswith(a_str):
                continue
            remainder = suspect_str[len(a_str):]
            if not remainder or not remainder.isdigit():
                continue
            rem_val = float(remainder)
            # The end chapter must be within the existing range
            if rem_val < 1 or rem_val > max(others):
                continue
            # Prefer the largest matching start (most specific)
            if found_start is None or a > found_start:
                found_start = a
                found_end   = rem_val

        if found_start is not None:
            results.append((suspect, found_start, found_end))

    return results


def _rename_stem_for_compilation(stem: str, start: float, end: float) -> str:
    """
    Replace the chapter number in a filename stem with a hyphenated range.
    e.g. "Batman Ch. 12" with start=1, end=2  →  "Batman Ch. 1-2"
         "One Piece ch15" with start=1, end=5 →  "One Piece ch1-5"
    """
    range_str = f"{_fmt_num(start)}-{_fmt_num(end)}"

    def _replacer(m: re.Match) -> str:
        return m.group(1) + range_str

    new_stem, n = _CHAPTER_TOKEN_RE.subn(_replacer, stem, count=1)
    return new_stem if n else f"{stem} {range_str}"


def _update_comicinfo_range(xml: str, start: float, end: float) -> tuple[str, bool]:
    """
    Update the <Number> tag in xml to "start-end" format and add/update
    a <Count> tag with the number of chapters in the range.
    Returns (new_xml, changed).
    """
    range_str = f"{_fmt_num(start)}-{_fmt_num(end)}"
    count_val  = str(int(end - start + 1))
    changed    = False

    # Update <Number>
    num_pat = re.compile(r"<Number>.*?</Number>", re.IGNORECASE | re.DOTALL)
    new_num_tag = f"<Number>{range_str}</Number>"
    if num_pat.search(xml):
        if num_pat.search(xml).group(0) != new_num_tag:
            xml     = num_pat.sub(new_num_tag, xml, count=1)
            changed = True
    else:
        xml     = xml.replace("</ComicInfo>", f"  {new_num_tag}\n</ComicInfo>")
        changed = True

    return xml, changed


def detect_and_fix_compilations(
    series_dir: Path,
    dry_run: bool = False,
) -> int:
    """
    Scan all .cbz files in series_dir, detect any whose chapter number
    appears to be a concatenation of two earlier chapter numbers, rename
    those files to use a hyphenated range, and update their ComicInfo.xml.

    Returns the number of files renamed.
    """
    cbz_files = sorted(series_dir.glob("*.cbz"))
    if len(cbz_files) < 2:
        return 0

    # Build chapter-number → path map
    num_to_path: dict[float, Path] = {}
    for cbz in cbz_files:
        ch = extract_chapter(cbz.stem)
        if ch is not None:
            num_to_path[ch] = cbz

    if len(num_to_path) < 2:
        return 0

    candidates = _detect_compilation_candidates(list(num_to_path.keys()))
    if not candidates:
        return 0

    renamed = 0
    for suspect, start, end in candidates:
        cbz = num_to_path.get(suspect)
        if cbz is None or not cbz.exists():
            continue

        new_stem = _rename_stem_for_compilation(cbz.stem, start, end)
        new_name = new_stem + cbz.suffix
        new_path = cbz.parent / new_name

        log.info(
            f"    Compilation detected: '{cbz.name}' looks like "
            f"ch.{_fmt_num(start)}-{_fmt_num(end)} "
            f"(gap from ch.{_fmt_num(max(n for n in num_to_path if n != suspect))} "
            f"to ch.{_fmt_num(suspect)} is unusually large)"
        )

        if dry_run:
            log.info(f"    [DRY RUN] Would rename: '{cbz.name}' -> '{new_name}'")
            renamed += 1
            continue

        # Rename file
        if new_path != cbz:
            if new_path.exists():
                if cbz.stat().st_size > new_path.stat().st_size:
                    new_path.unlink()
                    cbz.rename(new_path)
                    log.info(f"    Renamed (replaced smaller): '{cbz.name}' -> '{new_name}'")
                else:
                    cbz.unlink()
                    log.info(f"    Discarded (collision, kept larger): '{cbz.name}'")
                    continue
            else:
                cbz.rename(new_path)
                log.info(f"    Renamed: '{cbz.name}' -> '{new_name}'")
        cbz = new_path

        # Update ComicInfo.xml
        _patch_comicinfo_for_range(cbz, start, end)
        renamed += 1

    return renamed


def _patch_comicinfo_for_range(cbz_path: Path, start: float, end: float) -> None:
    """Read the zip, update <Number> to 'start-end', rewrite atomically."""
    zip_entries: list[tuple] = []
    real_name = None
    xml       = None

    try:
        with zipfile.ZipFile(cbz_path, "r") as zin:
            nl = {n.lower(): n for n in zin.namelist()}
            key = next(
                (k for k in nl if os.path.basename(k).lower() == "comicinfo.xml"),
                None
            )
            if key:
                real_name = nl[key]
                xml = zin.read(real_name).decode("utf-8", errors="replace")
            for item in zin.infolist():
                zip_entries.append((item, zin.read(item.filename)))
    except (zipfile.BadZipFile, OSError) as e:
        log.error(f"    Cannot read {cbz_path.name} for ComicInfo patch: {e}")
        return

    if xml is None:
        log.info(f"    No ComicInfo.xml in {cbz_path.name} — skipping range patch.")
        return

    xml, changed = _update_comicinfo_range(xml, start, end)
    if not changed:
        log.info(f"    ComicInfo already correct for range {_fmt_num(start)}-{_fmt_num(end)}.")
        return

    tmp_path = cbz_path.with_suffix(".tmp.cbz")
    bak_path = cbz_path.with_suffix(".bak.cbz")
    try:
        with zipfile.ZipFile(tmp_path, "w") as zout:
            for item, data in zip_entries:
                if item.filename == real_name:
                    zout.writestr(item, xml.encode("utf-8"))
                else:
                    zout.writestr(item, data, compress_type=item.compress_type)
        cbz_path.rename(bak_path)
        tmp_path.rename(cbz_path)
        bak_path.unlink(missing_ok=True)
        log.info(
            f"    ComicInfo updated: Number={_fmt_num(start)}-{_fmt_num(end)}"
            f" in '{cbz_path.name}'"
        )
    except Exception as e:
        log.error(f"    Failed to patch ComicInfo in {cbz_path.name}: {e}")
        if tmp_path.exists():
            tmp_path.unlink()



# ─────────────────────────────────────────────
# SCAN AND GROUP
# ─────────────────────────────────────────────

def find_groups(library_root: Path) -> dict[str, list[Path]]:
    """
    Scan immediate subdirectories of library_root.
    Return a dict of {normalised_base: [Path, ...]} for groups of 2+ dirs
    that share the same base name after stripping the trailing number.
    """
    groups: dict[str, list[Path]] = defaultdict(list)

    for d in sorted(library_root.iterdir()):
        if not d.is_dir():
            continue
        base = get_base(d.name)
        if base:
            groups[base].append(d)

    # Only return groups with 2+ members
    return {b: dirs for b, dirs in groups.items() if len(dirs) >= 2}


def process_library(library_root: Path, dry_run: bool = False) -> None:
    """Find and merge all enumerated folder groups under library_root."""
    if not library_root.exists():
        log.warning(f"Library root not found, skipping: {library_root}")
        return

    groups = find_groups(library_root)

    if not groups:
        log.info(f"  No enumerated folder groups found in: {library_root.name}")
        return

    log.info(f"  Found {len(groups)} group(s) to merge in: {library_root.name}")

    total_merged = 0
    total_files  = 0

    for base, dirs in sorted(groups.items()):
        dir_names  = [d.name for d in dirs]
        target_name = canonical_name(dir_names)
        target_path = library_root / target_name

        log.info(f"\n  Group: {base!r}")
        for d in dirs:
            log.info(f"    Source: {d.name!r}")
        log.info(f"    Target: {target_name!r}")

        if dry_run:
            cbz_count = sum(1 for d in dirs for _ in d.rglob("*.cbz"))
            generic_count = sum(
                1 for d in dirs
                for f in d.glob("*.cbz") if is_generic_stem(f.stem)
            )
            log.info(
                f"    [DRY RUN] Would merge {len(dirs)} folders "
                f"({cbz_count} .cbz files, {generic_count} to rename) -> {target_name!r}"
            )
            for d in dirs:
                rename_generic_files(d, dry_run=True)
            # Preview compilation detection
            detect_and_fix_compilations(library_root / canonical_name(dir_names), dry_run=True)
            total_merged += 1
            total_files  += cbz_count
            continue

        # Create target if it doesn't already exist
        target_path.mkdir(exist_ok=True)

        files_moved = 0
        for src_dir in dirs:
            # Skip if this dir IS the target (can happen if target already exists)
            if src_dir.resolve() == target_path.resolve():
                log.info(f"    Skipping '{src_dir.name}' — already the target folder.")
                continue
            # Rename any generic-named files before merging so they don't
            # collide or lose their identity in the merged folder
            rename_generic_files(src_dir, dry_run=False)
            n = merge_dirs(src_dir, target_path, dry_run=False)
            files_moved += n

        # Update ComicInfo.xml for every CBZ now in the target folder
        cbz_files = sorted(target_path.rglob("*.cbz"))
        log.info(f"    Updating ComicInfo.xml for {len(cbz_files)} file(s)...")
        for cbz in cbz_files:
            update_comicinfo(cbz, series=target_name, dry_run=False)

        # Detect and fix any compilation chapters (e.g. ch.12 → ch.1-2)
        comp_fixed = detect_and_fix_compilations(target_path, dry_run=False)
        if comp_fixed:
            log.info(f"    Fixed {comp_fixed} compilation chapter(s).")

        total_merged += 1
        total_files  += files_moved
        log.info(
            f"    Done: {len(dirs)} folder(s) merged into '{target_name}' "
            f"({files_moved} file(s) moved)."
        )

    log.info(
        f"\n  Summary: {total_merged} group(s) merged, "
        f"{total_files} file(s) moved."
    )


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

def main() -> None:
    args    = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry_run = "--dry-run" in sys.argv

    if args:
        # Path(s) supplied on the command line — use them directly.
        # Works for UNC shares (\\tower\...), local drives (L:\...), or any path.
        targets = args
    elif SCAN_FOLDERS:
        # No CLI args: prompt the user so they can override without editing the script.
        print()
        print("  No path given. Choose an option:")
        for i, folder in enumerate(SCAN_FOLDERS, start=1):
            print(f"  [{i}] {folder}")
        print(f"  [A] All of the above ({len(SCAN_FOLDERS)} folder(s))")
        print("  [C] Enter a custom path (local drive or UNC share)")
        choice = input("  Choice: ").strip().upper()

        if choice == "A":
            targets = SCAN_FOLDERS
        elif choice == "C":
            custom = input("  Path: ").strip().strip('"').strip("'")
            if not custom:
                print("  No path entered, exiting.")
                return
            targets = [custom]
        elif choice.isdigit() and 1 <= int(choice) <= len(SCAN_FOLDERS):
            targets = [SCAN_FOLDERS[int(choice) - 1]]
        else:
            print(f"  Unrecognised choice '{choice}', exiting.")
            return
    else:
        print("  No SCAN_FOLDERS configured and no path given, exiting.")
        return

    log.info("=" * 60)
    log.info("CBZ Folder Merger" + (" [DRY RUN]" if dry_run else ""))
    log.info("=" * 60)

    for target in targets:
        path = Path(target)
        log.info(f"\nScanning: {path}")
        process_library(path, dry_run=dry_run)

    log.info("\n" + "=" * 60)
    log.info("CBZ Folder Merger complete.")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
