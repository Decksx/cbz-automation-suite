"""
CBZ Series Matcher
Scans one or more configured library folders for series directories whose
names are suspiciously similar (near-duplicates caused by punctuation
differences, spacing, romanisation variants, etc.).

For each matched pair above the similarity threshold:
  - The directory with MORE files is treated as the primary (canonical) name.
  - If both have equal file counts, the LONGER name wins (usually more complete).
  - If similarity >= AUTO_RENAME_THRESHOLD, the secondary directory is
    automatically renamed/merged into the primary.
  - Pairs below AUTO_RENAME_THRESHOLD but above REPORT_THRESHOLD are logged
    as warnings for manual review.

Usage:
    python cbz_series_matcher.py             # run with configured folders
    python cbz_series_matcher.py --dry-run   # preview, no changes
"""

import os
import re
import sys
import shutil
import logging
from difflib   import SequenceMatcher
from pathlib   import Path
from itertools import combinations
from logging.handlers import RotatingFileHandler as _RotatingFileHandler

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

# Folders to scan — add or remove as needed
SCAN_FOLDERS: list[str] = [
    r"\\tower\media\comics\Comix",
    r"\\tower\media\comics\Manga",
]

LOG_FILE = r"C:\git\ComicAutomation\cbz_series_matcher.log"

# Pairs at or above this ratio are auto-renamed/merged
AUTO_RENAME_THRESHOLD  = 0.90

# Pairs at or above this ratio (but below auto threshold) are flagged for review
REPORT_THRESHOLD       = 0.80

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
log = logging.getLogger("series_matcher")
log.setLevel(logging.DEBUG)

_fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                          datefmt="%Y-%m-%d %H:%M:%S")
_fh  = _RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_fh.setFormatter(_fmt)
log.addHandler(_fh)
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
log.addHandler(_sh)

# ─────────────────────────────────────────────
# NAME NORMALISATION FOR COMPARISON ONLY
# (never written to disk — used only to compute similarity)
# ─────────────────────────────────────────────
_PUNCT_RE    = re.compile(r"[^\w\s]")      # strip all punctuation
_SPACES_RE   = re.compile(r"\s+")

def _normalise_for_compare(name: str) -> str:
    """
    Reduce a series name to a comparable form:
      - Lowercase
      - Strip all punctuation (hyphens, apostrophes, commas, etc.)
      - Collapse whitespace
    This is used ONLY for similarity scoring — never written to disk.
    """
    name = name.lower()
    name = _PUNCT_RE.sub(" ", name)
    name = _SPACES_RE.sub(" ", name).strip()
    return name


def _similarity(a: str, b: str) -> float:
    """SequenceMatcher ratio of two normalised names."""
    return SequenceMatcher(None, a, b).ratio()


# ─────────────────────────────────────────────
# DIRECTORY HELPERS
# ─────────────────────────────────────────────
def _cbz_count(path: Path) -> int:
    """Return number of .cbz files directly inside path."""
    try:
        return sum(1 for f in path.iterdir() if f.suffix.lower() == ".cbz")
    except OSError:
        return 0


def _choose_primary(a: Path, b: Path) -> tuple[Path, Path]:
    """
    Return (primary, secondary) where primary is the canonical name.
    Rules (in order):
      1. More CBZ files → primary
      2. Tie: longer directory name → primary  (usually more complete)
      3. Tie: alphabetically first → primary
    """
    ca, cb = _cbz_count(a), _cbz_count(b)
    if ca != cb:
        return (a, b) if ca > cb else (b, a)
    if len(a.name) != len(b.name):
        return (a, b) if len(a.name) > len(b.name) else (b, a)
    return (a, b) if a.name <= b.name else (b, a)


# ─────────────────────────────────────────────
# MERGE / RENAME
# ─────────────────────────────────────────────
def _merge_into(secondary: Path, primary: Path, dry_run: bool) -> int:
    """
    Move all files from secondary into primary.
    On filename collision, keep the larger file.
    Returns number of files moved.
    """
    moved = 0
    try:
        for item in list(secondary.iterdir()):
            if not item.is_file():
                continue
            dest = primary / item.name
            if dry_run:
                log.info(f"      [DRY RUN] Would move: '{item.name}' -> {primary.name}/")
                moved += 1
                continue
            try:
                if dest.exists():
                    if item.stat().st_size > dest.stat().st_size:
                        dest.unlink()
                        shutil.move(str(item), str(dest))
                        log.info(f"      Moved (replaced smaller): '{item.name}'")
                    else:
                        item.unlink()
                        log.info(f"      Discarded (collision, kept larger): '{item.name}'")
                else:
                    shutil.move(str(item), str(dest))
                    log.info(f"      Moved: '{item.name}'")
                moved += 1
            except OSError as e:
                log.error(f"      Failed to move '{item.name}': {e}")
    except OSError as e:
        log.error(f"    Cannot iterate '{secondary}': {e}")
        return moved

    # Remove secondary directory if now empty
    if not dry_run:
        try:
            remaining = list(secondary.iterdir())
            if not remaining:
                secondary.rmdir()
                log.info(f"    Removed empty directory: '{secondary.name}'")
            else:
                log.warning(
                    f"    '{secondary.name}' not empty after merge "
                    f"({len(remaining)} item(s) remain) — leaving in place."
                )
        except OSError as e:
            log.error(f"    Could not remove '{secondary.name}': {e}")

    return moved


# ─────────────────────────────────────────────
# SCAN + MATCH
# ─────────────────────────────────────────────
def scan_folder(folder: Path) -> list[Path]:
    """Return all immediate subdirectories of folder."""
    try:
        return sorted(p for p in folder.iterdir() if p.is_dir())
    except OSError as e:
        log.error(f"Cannot scan '{folder}': {e}")
        return []


def find_matches(
    dirs: list[Path],
) -> list[tuple[float, Path, Path]]:
    """
    Compare every pair of directories by normalised name similarity.
    Returns list of (ratio, dir_a, dir_b) sorted by ratio descending,
    filtered to ratio >= REPORT_THRESHOLD.
    """
    matches: list[tuple[float, Path, Path]] = []
    normalised = {d: _normalise_for_compare(d.name) for d in dirs}

    for a, b in combinations(dirs, 2):
        ratio = _similarity(normalised[a], normalised[b])
        if ratio >= REPORT_THRESHOLD:
            matches.append((ratio, a, b))

    return sorted(matches, key=lambda t: t[0], reverse=True)


# ─────────────────────────────────────────────
# DEDUPLICATION — avoid processing a dir twice
# ─────────────────────────────────────────────
def process_matches(
    matches: list[tuple[float, Path, Path]],
    dry_run: bool,
) -> tuple[int, int]:
    """
    Process each matched pair.
    Auto-merges pairs at or above AUTO_RENAME_THRESHOLD.
    Logs warnings for pairs between REPORT_THRESHOLD and AUTO_RENAME_THRESHOLD.
    Returns (auto_merged_count, review_count).
    """
    auto_merged = 0
    needs_review = 0
    # Track dirs already consumed by a merge so we don't double-process
    consumed: set[Path] = set()

    for ratio, a, b in matches:
        if a in consumed or b in consumed:
            continue

        primary, secondary = _choose_primary(a, b)
        ca = _cbz_count(primary)
        cb = _cbz_count(secondary)

        if ratio >= AUTO_RENAME_THRESHOLD:
            log.info(
                f"  AUTO-MERGE  [{ratio:.3f}]  "
                f"'{secondary.name}' ({cb} files) -> '{primary.name}' ({ca} files)"
                + ("  [DRY RUN]" if dry_run else "")
            )
            moved = _merge_into(secondary, primary, dry_run=dry_run)
            log.info(f"    {moved} file(s) moved.")
            consumed.add(secondary)
            auto_merged += 1
        else:
            log.warning(
                f"  REVIEW      [{ratio:.3f}]  "
                f"'{a.name}' ({_cbz_count(a)} files)  <->  "
                f"'{b.name}' ({_cbz_count(b)} files)  — below auto threshold, manual action needed"
            )
            needs_review += 1

    return auto_merged, needs_review


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
def main() -> None:
    dry_run = "--dry-run" in sys.argv

    log.info("=" * 60)
    log.info("CBZ Series Matcher")
    if dry_run:
        log.info("  Mode: DRY RUN — no files will be moved or renamed")
    log.info(f"  Auto-merge threshold : {AUTO_RENAME_THRESHOLD}")
    log.info(f"  Review threshold     : {REPORT_THRESHOLD}")
    log.info("=" * 60)

    total_auto   = 0
    total_review = 0
    total_dirs   = 0

    for folder_str in SCAN_FOLDERS:
        folder = Path(folder_str)
        log.info(f"\nScanning: {folder}")

        if not folder.exists() or not folder.is_dir():
            log.error(f"  Folder not found: {folder}")
            continue

        dirs = scan_folder(folder)
        log.info(f"  Found {len(dirs)} series director{'y' if len(dirs)==1 else 'ies'}.")
        total_dirs += len(dirs)

        if len(dirs) < 2:
            log.info("  Not enough directories to compare.")
            continue

        matches = find_matches(dirs)

        auto_pairs    = [(r,a,b) for r,a,b in matches if r >= AUTO_RENAME_THRESHOLD]
        review_pairs  = [(r,a,b) for r,a,b in matches if REPORT_THRESHOLD <= r < AUTO_RENAME_THRESHOLD]

        log.info(
            f"  {len(auto_pairs)} pair(s) above auto threshold ({AUTO_RENAME_THRESHOLD}), "
            f"{len(review_pairs)} pair(s) flagged for review."
        )

        if not matches:
            log.info("  No near-duplicate series names found.")
            continue

        auto, review = process_matches(matches, dry_run=dry_run)
        total_auto   += auto
        total_review += review

    log.info("\n" + "=" * 60)
    log.info("Series Matcher complete.")
    log.info(f"  Directories scanned : {total_dirs}")
    log.info(f"  Auto-merged         : {total_auto}")
    log.info(f"  Flagged for review  : {total_review}")
    if dry_run:
        log.info("  (Dry-run — no changes written)")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
