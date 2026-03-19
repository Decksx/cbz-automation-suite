"""
cbz_series_matcher.py — CBZ Series Matcher (parallelised)

Changes in this version
────────────────────────
• --workers N  (default: min(8, cpu_count)).  Pass --workers 1 for serial.
• Sibling-group matching runs in parallel: each sibling group collected by
  _collect_dir_groups() is dispatched to a ThreadPoolExecutor worker.
• find_matches() and process_matches() are each group's independent unit of
  work — no shared mutable state between group workers.
• Counters are summed from returned values after futures complete.
"""

from __future__ import annotations

import os
import re
import sys
import shutil
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib   import SequenceMatcher
from pathlib   import Path
from itertools import combinations
from logging.handlers import RotatingFileHandler as _RotatingFileHandler

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
SCAN_FOLDERS: list[str] = [
    r"\\tower\media\comics\Comix",
    r"\\tower\media\comics\Manga",
]
LOG_FILE               = r"C:\git\ComicAutomation\cbz_series_matcher.log"
AUTO_RENAME_THRESHOLD  = 0.90
REPORT_THRESHOLD       = 0.80
DEFAULT_WORKERS        = min(8, os.cpu_count() or 4)

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
# NAME NORMALISATION
# ─────────────────────────────────────────────
_PUNCT_RE  = re.compile(r"[^\w\s]")
_SPACES_RE = re.compile(r"\s+")

def _normalise_for_compare(name: str) -> str:
    name = name.lower()
    name = _PUNCT_RE.sub(" ", name)
    name = _SPACES_RE.sub(" ", name).strip()
    return name

def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


# ─────────────────────────────────────────────
# DIRECTORY HELPERS
# ─────────────────────────────────────────────
def _cbz_count(path: Path) -> int:
    try:
        return sum(1 for f in path.iterdir() if f.suffix.lower() == ".cbz")
    except OSError:
        return 0

def _choose_primary(a: Path, b: Path) -> tuple[Path, Path]:
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
def find_matches(dirs: list[Path]) -> list[tuple[float, Path, Path]]:
    matches: list[tuple[float, Path, Path]] = []
    normalised = {d: _normalise_for_compare(d.name) for d in dirs}
    for a, b in combinations(dirs, 2):
        ratio = _similarity(normalised[a], normalised[b])
        if ratio >= REPORT_THRESHOLD:
            matches.append((ratio, a, b))
    return sorted(matches, key=lambda t: t[0], reverse=True)

def process_matches(
    matches: list[tuple[float, Path, Path]],
    dry_run: bool,
) -> tuple[int, int]:
    auto_merged  = 0
    needs_review = 0
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
                f"'{b.name}' ({_cbz_count(b)} files)  — below auto threshold"
            )
            needs_review += 1

    return auto_merged, needs_review


# ─────────────────────────────────────────────
# RECURSIVE GROUP COLLECTION
# ─────────────────────────────────────────────
def _collect_dir_groups(folder: Path) -> list[list[Path]]:
    groups: list[list[Path]] = []
    try:
        siblings = sorted(p for p in folder.iterdir() if p.is_dir())
    except OSError as e:
        log.error(f"Cannot scan '{folder}': {e}")
        return groups
    if len(siblings) >= 2:
        groups.append(siblings)
    for sibling in siblings:
        groups.extend(_collect_dir_groups(sibling))
    return groups


# ─────────────────────────────────────────────
# PER-GROUP WORKER
# ─────────────────────────────────────────────
def _process_group(group: list[Path], dry_run: bool) -> tuple[int, int]:
    """
    Match and process a single sibling group.
    Returns (auto_merged, needs_review).
    Safe to call from multiple threads — operates on independent directory sets.
    """
    if len(group) < 2:
        return 0, 0
    matches = find_matches(group)
    if not matches:
        return 0, 0

    auto_pairs   = [(r, a, b) for r, a, b in matches if r >= AUTO_RENAME_THRESHOLD]
    review_pairs = [(r, a, b) for r, a, b in matches if REPORT_THRESHOLD <= r < AUTO_RENAME_THRESHOLD]

    parent_label = group[0].parent.name
    log.info(
        f"  [{parent_label}]  {len(auto_pairs)} pair(s) above auto threshold, "
        f"{len(review_pairs)} pair(s) flagged for review."
    )
    return process_matches(matches, dry_run=dry_run)


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
def main() -> None:
    raw_args = sys.argv[1:]
    dry_run  = "--dry-run" in raw_args

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

    log.info("=" * 60)
    log.info("CBZ Series Matcher")
    if dry_run:
        log.info("  Mode: DRY RUN — no files will be moved or renamed")
    log.info(f"  Auto-merge threshold : {AUTO_RENAME_THRESHOLD}")
    log.info(f"  Review threshold     : {REPORT_THRESHOLD}")
    log.info(f"  Workers              : {workers}")
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

        dir_groups = _collect_dir_groups(folder)
        all_dirs   = {d for group in dir_groups for d in group}
        log.info(
            f"  Found {len(all_dirs)} director{'y' if len(all_dirs) == 1 else 'ies'} "
            f"across {len(dir_groups)} sibling group(s).  Workers: {workers}."
        )
        total_dirs += len(all_dirs)

        if not dir_groups:
            log.info("  Not enough directories to compare.")
            continue

        groups_to_process = [g for g in dir_groups if len(g) >= 2]

        if workers == 1:
            for group in groups_to_process:
                a, r = _process_group(group, dry_run)
                total_auto += a; total_review += r
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(_process_group, group, dry_run): group
                    for group in groups_to_process
                }
                for future in as_completed(futures):
                    try:
                        a, r = future.result()
                        total_auto += a; total_review += r
                    except Exception as e:
                        group = futures[future]
                        log.error(f"  Worker failed for group under '{group[0].parent.name}': {e}")

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
