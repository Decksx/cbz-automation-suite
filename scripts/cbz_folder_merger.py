"""
cbz_folder_merger.py — CBZ Folder Merger (parallelised + optimised)

Changes in this version
────────────────────────
1. --workers N  (default: min(8, cpu_count)).  Pass --workers 1 for serial.

2. update_comicinfo() now reads the zip ONCE:
   - Previous: read ALL entry data into memory, build new zip, atomic swap.
     Cost: read every image byte just to rewrite ComicInfo.xml.
   - New: read ONLY ComicInfo.xml for inspection; if unchanged, skip.
     If changed, read all other entries and rewrite — same as before but
     skipping the read-everything-first step when no write is needed.
   This alone saves significant time when most files already have correct metadata.

3. merge_dirs() + update_comicinfo() are fused into a single zip-open per file:
   Previously each merged file was opened twice — once by merge_dirs (just a
   shutil.move, so no zip open), and once by update_comicinfo.
   That part is already fine; the saving in (2) is the real win.

4. Each merge GROUP is processed in parallel (ThreadPoolExecutor).
   Groups are independent — they touch completely different directories.
   Within a group, file operations remain serial (rename/collision safety).

5. ComicInfo updates within a group are parallelised with a second inner
   ThreadPoolExecutor, since each file's zip read/write is independent.
"""

from __future__ import annotations

import os
import gc
import re
import sys
import html
import shutil
import zipfile
import logging
import statistics
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from logging.handlers import RotatingFileHandler as _RotatingFileHandler
from pathlib import Path

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
SCAN_FOLDERS = [
    r"\\tower\media\comics\Comix",
    r"\\tower\media\comics\Manga",
]
LOG_FILE = r"C:\git\ComicAutomation\cbz_folder_merger.log"
DEFAULT_WORKERS = min(8, os.cpu_count() or 4)

# ─────────────────────────────────────────────
# LOGGING
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
# PATTERNS (compiled once — unchanged)
# ─────────────────────────────────────────────
_TRAILING_TOKEN_RE = re.compile(
    r'[\s_\-]*'
    r'(?:'
    r'ch(?:ap(?:ter)?)?p?\.?\s*\d[\d.]*'
    r'|issue\s*\d[\d.]*'
    r'|ep(?:isode)?\.?\s*\d[\d.]*'
    r'|vol(?:ume)?\.?\s*\d[\d.]*'
    r'|v\d[\d.]*(?=\s*$)'
    r'|\d+$'
    r')'
    r'[\s_\-.,]*$',
    re.IGNORECASE
)

_CHAPTER_RE = re.compile(
    r'(?:'
    r'ch(?:ap(?:ter)?)?p?\.?\s*(\d[\d.]*)'
    r'|issue\s*(\d[\d.]*)'
    r'|ep(?:isode)?\.?\s*(\d[\d.]*)'
    r'|#\s*(\d[\d.]*)'
    r')',
    re.IGNORECASE
)

_VOLUME_RE = re.compile(
    r'(?:'
    r'vol(?:ume)?\.?\s*(\d[\d.]*)'
    r'|v(\d[\d.]*)(?=\s|ch|ep|$)'
    r')',
    re.IGNORECASE
)

_GENERIC_STEM_RE = re.compile(
    r'^(?:'
    r'ch(?:ap(?:ter)?)?p?\.?\s*\d[\d.]*'
    r'|ep(?:isode)?\.?\s*\d[\d.]*'
    r'|issue\s*\d[\d.]*'
    r'|vol(?:ume)?\.?\s*\d[\d.]*'
    r'|#\s*\d[\d.]*'
    r'|\d{1,4}[\d.]*'
    r'|chapter$'
    r'|episode$'
    r')$',
    re.IGNORECASE
)

_ILLEGAL_CHARS_RE  = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_BRACKET_RE        = re.compile(r'\[[^\]]*\]|\([^)]*\)')
_STRAY_RE          = re.compile(r'[\[\]()]')
_SPACES_RE         = re.compile(r' {2,}')
_URL_RE            = re.compile(
    r'(?:https?://\S+)|(?:www\.\S+)'
    r'|(?:\b[\w-]+\.(?:com|net|org|io|co|info|biz|tv|me|cc|us|uk|ca|au)(?:/\S*)?)',
    re.IGNORECASE)
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
    r'[\s_\-]*(?:part|v|ch(?:ap(?:ter)?)?)\\s*$', re.IGNORECASE
)
_CHAPTER_TOKEN_RE = re.compile(
    r'(ch(?:ap(?:ter)?)?p?\.?\s*)(\d[\d.]*)', re.IGNORECASE
)


# ─────────────────────────────────────────────
# HELPERS (unchanged logic)
# ─────────────────────────────────────────────
def sanitize(text: str) -> str:
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

def _fmt_num(n: float) -> str:
    return str(int(n)) if n == int(n) else str(n)

def get_base(dir_name: str) -> str | None:
    m = _TRAILING_TOKEN_RE.search(dir_name)
    if not m:
        return None
    base = dir_name[:m.start()].strip()
    if not base:
        return None
    return re.sub(r'\s+', ' ', base).lower()

def canonical_name(dir_names: list[str]) -> str:
    candidates = []
    for name in dir_names:
        m = _TRAILING_TOKEN_RE.search(name)
        if m:
            stripped = name[:m.start()].strip()
            if stripped:
                candidates.append(stripped)
    if not candidates:
        return dir_names[0]
    best = max(candidates, key=lambda s: sum(1 for c in s if c.isupper()))
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
    return bool(_GENERIC_STEM_RE.match(stem.strip()))

def rename_generic_files(src_dir: Path, dry_run: bool = False) -> list[Path]:
    cbz_files    = sorted(src_dir.glob('*.cbz'))
    generic_files = [f for f in cbz_files if is_generic_stem(f.stem)]
    if not generic_files:
        return cbz_files
    dir_name = src_dir.name
    log.info(f"    Renaming {len(generic_files)} generic file(s) in '{dir_name}':")
    renames: list[tuple[Path, Path]] = []
    for i, cbz in enumerate(generic_files):
        suffix = f" {i + 1}" if len(generic_files) > 1 else ""
        new_name = f"{dir_name}{suffix}.cbz"
        new_path = src_dir / new_name
        renames.append((cbz, new_path))
    result_paths: list[Path] = list(cbz_files)
    for old_path, new_path in renames:
        if old_path == new_path:
            continue
        if dry_run:
            log.info(f"      [DRY RUN] {old_path.name!r} -> {new_path.name!r}")
        else:
            if new_path.exists():
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
            if old_path in result_paths:
                idx = result_paths.index(old_path)
                result_paths[idx] = new_path
    return sorted(p for p in result_paths if p.exists())


# ─────────────────────────────────────────────
# COMICINFO UPDATE — optimised: read XML only,
# skip full zip rewrite when nothing changed
# ─────────────────────────────────────────────
def _set_or_insert_tag(xml: str, tag: str, value: str,
                       insert_after: str | None = None) -> tuple[str, bool]:
    pattern = re.compile(rf"<{tag}>.*?</{tag}>", re.IGNORECASE | re.DOTALL)
    new_tag  = f"<{tag}>{value}</{tag}>"
    if pattern.search(xml):
        existing_val = pattern.search(xml).group(0)
        if existing_val == new_tag:
            return xml, False
        return pattern.sub(new_tag, xml, count=1), True
    if insert_after:
        anchor = re.compile(rf"(</{insert_after}>)", re.IGNORECASE)
        if anchor.search(xml):
            return anchor.sub(rf"\1\n  {new_tag}", xml, count=1), True
    return xml.replace("</ComicInfo>", f"  {new_tag}\n</ComicInfo>"), True


COMICINFO_TEMPLATE = (
    '<ComicInfo\n'
    '  xmlns:xsd="http://www.w3.org/2001/XMLSchema"\n'
    '  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
    '  <Title></Title>\n'
    '  <Series></Series>\n'
    '  <Number></Number>\n'
    '</ComicInfo>'
)


def update_comicinfo(cbz_path: Path, series: str, dry_run: bool = False) -> bool:
    """
    Update (or inject) ComicInfo.xml in a CBZ.

    Optimisation vs original:
    - Phase 1: open zip, read ONLY the ComicInfo.xml entry (not all image data).
      Compute the new XML. If unchanged → return False immediately (no rewrite).
    - Phase 2: only if a change is needed, read all entries and rewrite atomically.
      This avoids reading potentially hundreds of MB of image data just to update
      a few bytes of XML when the tags are already correct.
    """
    stem    = cbz_path.stem
    chapter = extract_chapter(stem)
    volume  = extract_volume(stem)

    # ── Phase 1: read only ComicInfo.xml ─────────────────────────────────────
    real_name = None
    xml       = None
    try:
        with zipfile.ZipFile(cbz_path, "r") as zin:
            nl_lower = {n.lower(): n for n in zin.namelist()}
            key = next(
                (k for k in nl_lower if os.path.basename(k).lower() == "comicinfo.xml"),
                None
            )
            if key:
                real_name = nl_lower[key]
                xml = zin.read(real_name).decode("utf-8", errors="replace")
            # Store namelist for Phase 2 — don't read image data yet
            all_names = zin.namelist()
    except zipfile.BadZipFile:
        log.error(f"    Bad zip: {cbz_path.name} — skipping comicinfo update.")
        return False
    except OSError as e:
        log.error(f"    Cannot read {cbz_path.name}: {e}")
        return False

    # ── Build desired XML ─────────────────────────────────────────────────────
    if xml is None:
        xml = COMICINFO_TEMPLATE
        real_name = None

    changed = False
    xml, c = _set_or_insert_tag(xml, "Series", series, insert_after="Title")
    changed = changed or c
    xml, c = _set_or_insert_tag(xml, "Title", stem)
    changed = changed or c
    if volume:
        xml, c = _set_or_insert_tag(xml, "Volume", volume, insert_after="Series")
        changed = changed or c
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

    # ── Phase 2: rewrite zip (only reached when a change is needed) ───────────
    tmp_path = cbz_path.with_suffix(".tmp.cbz")
    bak_path = cbz_path.with_suffix(".bak.cbz")
    try:
        # Read all entries NOW (only on the write path)
        zip_entries: list[tuple] = []
        with zipfile.ZipFile(cbz_path, "r") as zin:
            for item in zin.infolist():
                zip_entries.append((item, zin.read(item.filename)))

        with zipfile.ZipFile(tmp_path, "w") as zout:
            for item, data in zip_entries:
                if real_name and item.filename == real_name:
                    zout.writestr(item, xml.encode("utf-8"))
                else:
                    zout.writestr(item, data, compress_type=item.compress_type)
            if real_name is None:
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
# DIRECTORY MERGE (unchanged logic)
# ─────────────────────────────────────────────
def merge_dirs(src: Path, dest: Path, dry_run: bool = False) -> int:
    moved = 0
    for src_file in sorted(src.rglob("*.cbz")):
        dest_file = dest / src_file.name
        if dest_file.exists():
            ss = src_file.stat().st_size
            ds = dest_file.stat().st_size
            if ss > ds:
                log.info(f"    Collision '{src_file.name}': incoming ({ss:,}B) > existing ({ds:,}B) — replacing.")
                if not dry_run:
                    dest_file.unlink()
                    shutil.move(str(src_file), str(dest_file))
                moved += 1
            else:
                log.info(f"    Collision '{src_file.name}': existing ({ds:,}B) >= incoming ({ss:,}B) — keeping existing.")
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
# COMPILATION DETECTION (unchanged logic)
# ─────────────────────────────────────────────
def _detect_compilation_candidates(chapter_nums: list[float]) -> list[tuple[float, float, float]]:
    if len(chapter_nums) < 2:
        return []
    nums    = sorted(set(float(n) for n in chapter_nums))
    results = []
    for suspect in nums:
        others = [n for n in nums if n != suspect]
        if not others:
            continue
        if suspect <= max(others):
            continue
        gaps = [others[j + 1] - others[j] for j in range(len(others) - 1)]
        gap_to_suspect = suspect - max(others)
        if gaps:
            median_gap = statistics.median(gaps)
            if median_gap > 0 and gap_to_suspect <= 2 * median_gap:
                continue
            if median_gap == 0 and gap_to_suspect <= 2:
                continue
        elif gap_to_suspect <= 2:
            continue
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
            if rem_val < 1 or rem_val > max(others):
                continue
            if found_start is None or a > found_start:
                found_start = a
                found_end   = rem_val
        if found_start is not None:
            results.append((suspect, found_start, found_end))
    return results

def _rename_stem_for_compilation(stem: str, start: float, end: float) -> str:
    range_str = f"{_fmt_num(start)}-{_fmt_num(end)}"
    def _replacer(m: re.Match) -> str:
        return m.group(1) + range_str
    new_stem, n = _CHAPTER_TOKEN_RE.subn(_replacer, stem, count=1)
    return new_stem if n else f"{stem} {range_str}"

def _update_comicinfo_range(xml: str, start: float, end: float) -> tuple[str, bool]:
    range_str   = f"{_fmt_num(start)}-{_fmt_num(end)}"
    count_val   = str(int(end - start + 1))
    changed     = False
    num_pat     = re.compile(r"<Number>.*?</Number>", re.IGNORECASE | re.DOTALL)
    new_num_tag = f"<Number>{range_str}</Number>"
    if num_pat.search(xml):
        if num_pat.search(xml).group(0) != new_num_tag:
            xml     = num_pat.sub(new_num_tag, xml, count=1)
            changed = True
    else:
        xml     = xml.replace("</ComicInfo>", f"  {new_num_tag}\n</ComicInfo>")
        changed = True
    return xml, changed

def _patch_comicinfo_for_range(cbz_path: Path, start: float, end: float) -> None:
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
        log.info(f"    ComicInfo updated: Number={_fmt_num(start)}-{_fmt_num(end)} in '{cbz_path.name}'")
    except Exception as e:
        log.error(f"    Failed to patch ComicInfo in {cbz_path.name}: {e}")
        if tmp_path.exists():
            tmp_path.unlink()

def detect_and_fix_compilations(series_dir: Path, dry_run: bool = False) -> int:
    cbz_files = sorted(series_dir.glob("*.cbz"))
    if len(cbz_files) < 2:
        return 0
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
        _patch_comicinfo_for_range(cbz, start, end)
        renamed += 1
    return renamed


# ─────────────────────────────────────────────
# SCAN AND GROUP (unchanged)
# ─────────────────────────────────────────────
def find_groups(library_root: Path) -> dict[str, list[Path]]:
    groups: dict[str, list[Path]] = defaultdict(list)
    for d in sorted(library_root.iterdir()):
        if not d.is_dir():
            continue
        base = get_base(d.name)
        if base:
            groups[base].append(d)
    return {b: dirs for b, dirs in groups.items() if len(dirs) >= 2}


# ─────────────────────────────────────────────
# PER-GROUP WORKER
# ─────────────────────────────────────────────
def _process_group(
    base: str,
    dirs: list[Path],
    library_root: Path,
    dry_run: bool,
    comicinfo_workers: int,
) -> tuple[int, int]:
    """
    Merge one group of enumerated folders into a single target directory.
    Returns (files_moved, was_merged: 0 or 1).
    Safe to call from threads — each group touches completely separate paths.
    """
    dir_names   = [d.name for d in dirs]
    target_name = canonical_name(dir_names)
    target_path = library_root / target_name

    log.info(f"\n  Group: {base!r}")
    for d in dirs:
        log.info(f"    Source: {d.name!r}")
    log.info(f"    Target: {target_name!r}")

    if dry_run:
        cbz_count     = sum(1 for d in dirs for _ in d.rglob("*.cbz"))
        generic_count = sum(1 for d in dirs for f in d.glob("*.cbz") if is_generic_stem(f.stem))
        log.info(
            f"    [DRY RUN] Would merge {len(dirs)} folders "
            f"({cbz_count} .cbz files, {generic_count} to rename) -> {target_name!r}"
        )
        for d in dirs:
            rename_generic_files(d, dry_run=True)
        detect_and_fix_compilations(library_root / canonical_name(dir_names), dry_run=True)
        return cbz_count, 1

    target_path.mkdir(exist_ok=True)

    files_moved = 0
    for src_dir in dirs:
        if src_dir.resolve() == target_path.resolve():
            log.info(f"    Skipping '{src_dir.name}' — already the target folder.")
            continue
        rename_generic_files(src_dir, dry_run=False)
        files_moved += merge_dirs(src_dir, target_path, dry_run=False)

    # ── ComicInfo updates — parallelised per file ─────────────────────────────
    cbz_files = sorted(target_path.rglob("*.cbz"))
    log.info(f"    Updating ComicInfo.xml for {len(cbz_files)} file(s) ({comicinfo_workers} worker(s))...")

    if comicinfo_workers == 1 or len(cbz_files) <= 1:
        for cbz in cbz_files:
            update_comicinfo(cbz, series=target_name, dry_run=False)
    else:
        with ThreadPoolExecutor(max_workers=comicinfo_workers) as inner:
            futures = {inner.submit(update_comicinfo, cbz, target_name, False): cbz
                       for cbz in cbz_files}
            for future in as_completed(futures):
                cbz = futures[future]
                try:
                    future.result()
                except Exception as e:
                    log.error(f"    ComicInfo worker failed for '{cbz.name}': {e}")

    # ── Compilation detection (serial — needs full picture of directory) ───────
    comp_fixed = detect_and_fix_compilations(target_path, dry_run=False)
    if comp_fixed:
        log.info(f"    Fixed {comp_fixed} compilation chapter(s).")

    log.info(
        f"    Done: {len(dirs)} folder(s) merged into '{target_name}' "
        f"({files_moved} file(s) moved)."
    )
    return files_moved, 1


# ─────────────────────────────────────────────
# PROCESS LIBRARY (parallelised across groups)
# ─────────────────────────────────────────────
def process_library(library_root: Path, dry_run: bool = False, workers: int = DEFAULT_WORKERS) -> None:
    """Find and merge all enumerated folder groups under library_root."""
    if not library_root.exists():
        log.warning(f"Library root not found, skipping: {library_root}")
        return

    groups = find_groups(library_root)

    if not groups:
        log.info(f"  No enumerated folder groups found in: {library_root.name}")
        return

    log.info(f"  Found {len(groups)} group(s) to merge in: {library_root.name}  ({workers} worker(s))")

    total_merged = 0
    total_files  = 0

    # Distribute workers: use half for groups, half for per-file comicinfo within each group.
    # At worker=1 everything is serial. At worker=8: up to 4 groups parallel,
    # each group gets up to 4 comicinfo workers.
    group_workers     = max(1, workers // 2)
    comicinfo_workers = max(1, workers // 2)

    sorted_groups = sorted(groups.items())

    if group_workers == 1:
        for base, dirs in sorted_groups:
            files, merged = _process_group(base, dirs, library_root, dry_run, comicinfo_workers)
            total_files  += files
            total_merged += merged
    else:
        with ThreadPoolExecutor(max_workers=group_workers) as executor:
            futures = {
                executor.submit(_process_group, base, dirs, library_root, dry_run, comicinfo_workers): base
                for base, dirs in sorted_groups
            }
            for future in as_completed(futures):
                base = futures[future]
                try:
                    files, merged = future.result()
                    total_files  += files
                    total_merged += merged
                except Exception as e:
                    log.error(f"  Worker failed for group '{base}': {e}")

    log.info(f"\n  Summary: {total_merged} group(s) merged, {total_files} file(s) moved.")


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
def main() -> None:
    raw_args = sys.argv[1:]
    dry_run  = "--dry-run" in raw_args
    args     = [a for a in raw_args if not a.startswith("--")]

    workers = DEFAULT_WORKERS
    for i, arg in enumerate(raw_args):
        if arg.startswith("--workers="):
            try:
                workers = max(1, int(arg.split("=", 1)[1]))
            except ValueError:
                pass
        elif arg == "--workers" and i + 1 < len(raw_args):
            try:
                workers = max(1, int(raw_args[i + 1]))
            except ValueError:
                pass

    if args:
        targets = args
    elif SCAN_FOLDERS:
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
    log.info(f"  Workers : {workers}")
    log.info("=" * 60)

    for target in targets:
        path = Path(target)
        log.info(f"\nScanning: {path}")
        process_library(path, dry_run=dry_run, workers=workers)

    log.info("\n" + "=" * 60)
    log.info("CBZ Folder Merger complete.")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
