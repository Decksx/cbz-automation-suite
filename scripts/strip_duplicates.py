"""
strip_duplicates.py — Strip Duplicates (parallelised)

Changes in this version
────────────────────────
• --workers N  (default: min(8, cpu_count)).  Pass --workers 1 for serial.
• When recursive, file renaming is parallelised: each directory's batch of
  .cbz files is an independent unit of work dispatched to ThreadPoolExecutor.
• Conflict resolution (larger-file-wins) is handled per-directory so no two
  threads ever touch the same file simultaneously.
• Library usage / clean() function is completely unchanged.
"""

from __future__ import annotations

import os
import re
import sys
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from logging.handlers import RotatingFileHandler as _RotatingFileHandler
from pathlib import Path

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
LOG_FILE = r"C:\git\ComicAutomation\strip_duplicates.log"
DEFAULT_WORKERS = min(8, os.cpu_count() or 4)

# ─────────────────────────────────────────────
# COMPILED PATTERNS  (unchanged)
# ─────────────────────────────────────────────
_LABEL_FRAG       = r'(?:ver(?:sion)?|v|ch(?:ap(?:ter)?)?|episode|ep|vol(?:ume)?|part|pt)'
_DUP_LABEL_NUM_RE = re.compile(
    rf'({_LABEL_FRAG}\.?\s*\d+(?:\.\d+)?)\s+{_LABEL_FRAG}\.?\s*(\d+(?:\.\d+)?)',
    re.IGNORECASE
)
_DUP_BARE_NUM_RE  = re.compile(r'\b(\d+(?:\.\d+)?)\s+\1\b')
_SPACED_PUNCT_RE  = re.compile(r'([!?.])(?: +\1)+')
_ASYM_HYPH_L_RE   = re.compile(r'(\S) -(\S)')
_ASYM_HYPH_R_RE   = re.compile(r'(\S)- (\S)')


# ─────────────────────────────────────────────
# CORE CLEANING  (unchanged — library-safe)
# ─────────────────────────────────────────────
def clean(s: str) -> str:
    """
    Apply all three cleaning passes and return the cleaned string.
    This function is pure (no I/O) and safe to call from any context.
    """
    def _replace_labeled(m: re.Match) -> str:
        first  = m.group(1)
        num2   = m.group(2)
        num1_m = re.search(r'\d+(?:\.\d+)?', first)
        if num1_m and num1_m.group() == num2:
            return first
        return m.group(0)
    s = _DUP_LABEL_NUM_RE.sub(_replace_labeled, s)
    s = _DUP_BARE_NUM_RE.sub(r'\1', s)

    def _collapse_punct(m: re.Match) -> str:
        ch    = m.group(1)
        count = len(re.findall(re.escape(ch), m.group(0)))
        return ch * count
    s = _SPACED_PUNCT_RE.sub(_collapse_punct, s)
    s = _ASYM_HYPH_L_RE.sub(r'\1-\2', s)
    s = _ASYM_HYPH_R_RE.sub(r'\1-\2', s)
    s = re.sub(r'  +', ' ', s).strip()
    return s


# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
def _setup_logging() -> logging.Logger:
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    log = logging.getLogger("strip_duplicates")
    log.setLevel(logging.INFO)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        fh = _RotatingFileHandler(
            LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError:
        pass
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(sh)
    return log


# ─────────────────────────────────────────────
# FILE HELPERS  (unchanged logic)
# ─────────────────────────────────────────────
def _clean_name(name: str) -> str:
    p    = Path(name)
    stem = clean(p.stem)
    return stem + p.suffix

def _resolve_conflict(src: Path, dest: Path, log: logging.Logger) -> bool:
    src_size  = src.stat().st_size
    dest_size = dest.stat().st_size
    if src_size > dest_size:
        log.info(
            f"    Conflict: '{dest.name}' already exists — "
            f"incoming ({src_size:,} B) > existing ({dest_size:,} B) — replacing."
        )
        dest.unlink()
        return True
    else:
        log.info(
            f"    Conflict: '{dest.name}' already exists — "
            f"existing ({dest_size:,} B) >= incoming ({src_size:,} B) — discarding incoming."
        )
        src.unlink()
        return False


# ─────────────────────────────────────────────
# PER-DIRECTORY WORKER
# ─────────────────────────────────────────────
def _process_dir(
    directory: Path,
    cbz_files: list[Path],
    dry_run: bool,
    log: logging.Logger,
) -> tuple[int, int, int, int]:
    """
    Rename .cbz files in a single directory.
    Returns (renamed, unchanged, discarded, errors).
    Safe to call from threads — all files belong to `directory` only.
    """
    renamed = skipped = discarded = unchanged = 0

    for cbz in cbz_files:
        new_name = _clean_name(cbz.name)
        if new_name == cbz.name:
            log.info(f"  Unchanged: {cbz.name}")
            unchanged += 1
            continue

        new_path = cbz.parent / new_name

        if dry_run:
            log.info(f"  [DRY RUN] Would rename: '{cbz.name}' -> '{new_name}'")
            renamed += 1
            continue

        if new_path.exists():
            keep = _resolve_conflict(cbz, new_path, log)
            if keep:
                try:
                    cbz.rename(new_path)
                    log.info(f"  Renamed (replaced): '{cbz.name}' -> '{new_name}'")
                    renamed += 1
                except OSError as e:
                    log.error(f"  Rename failed for '{cbz.name}': {e}")
                    skipped += 1
            else:
                discarded += 1
        else:
            try:
                cbz.rename(new_path)
                log.info(f"  Renamed: '{cbz.name}' -> '{new_name}'")
                renamed += 1
            except OSError as e:
                log.error(f"  Rename failed for '{cbz.name}': {e}")
                skipped += 1

    return renamed, unchanged, discarded, skipped


# ─────────────────────────────────────────────
# FOLDER PROCESSING
# ─────────────────────────────────────────────
def process_folder(folder: Path, recursive: bool, dry_run: bool, log: logging.Logger, workers: int) -> None:
    """Scan folder for .cbz files, group by directory, rename in parallel."""
    pattern   = "**/*.cbz" if recursive else "*.cbz"
    cbz_files = sorted(folder.glob(pattern))

    if not cbz_files:
        log.info(f"No .cbz files found in: {folder}")
        return

    log.info(f"Found {len(cbz_files)} .cbz file(s) in: {folder}  ({workers} worker(s))")

    # Group files by parent directory so each worker gets an isolated batch
    dir_to_files: dict[Path, list[Path]] = {}
    for cbz in cbz_files:
        dir_to_files.setdefault(cbz.parent, []).append(cbz)

    total_renamed = total_unchanged = total_discarded = total_skipped = 0

    if workers == 1 or len(dir_to_files) == 1:
        for directory, files in dir_to_files.items():
            r, u, d, s = _process_dir(directory, files, dry_run, log)
            total_renamed    += r; total_unchanged += u
            total_discarded  += d; total_skipped   += s
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_process_dir, directory, files, dry_run, log): directory
                for directory, files in dir_to_files.items()
            }
            for future in as_completed(futures):
                directory = futures[future]
                try:
                    r, u, d, s = future.result()
                    total_renamed    += r; total_unchanged += u
                    total_discarded  += d; total_skipped   += s
                except Exception as e:
                    log.error(f"  Worker failed for '{directory}': {e}")

    log.info(
        f"Done — {total_renamed} renamed, {total_unchanged} unchanged, "
        f"{total_discarded} discarded (conflict, kept larger), {total_skipped} error(s)."
    )


# ─────────────────────────────────────────────
# SELF-TEST  (unchanged)
# ─────────────────────────────────────────────
def _run_tests() -> None:
    tests = [
        ("My Comic ver. 9 ver.9",         "My Comic ver. 9"),
        ("Title ch. 12 ch.12",            "Title ch. 12"),
        ("Story chapter 5 Chapter 5",     "Story chapter 5"),
        ("Vol. 3 vol.3 Extra",            "Vol. 3 Extra"),
        ("Episode 7 ep.7",                "Episode 7"),
        ("Part 2 pt.2",                   "Part 2"),
        ("Something 9 9",                 "Something 9"),
        ("A 12 12 B",                     "A 12 B"),
        ("Wow! !",                        "Wow!!"),
        ("Wait.. .",                      "Wait..."),
        ("Really?  ?  ?",                 "Really???"),
        ("word -next",                    "word-next"),
        ("word- next",                    "word-next"),
        ("word - next",                   "word - next"),
        ("ch. 5 ch.5 Wow! ! word -end",   "ch. 5 Wow!! word-end"),
    ]
    passed = 0
    for inp, expected in tests:
        result = clean(inp)
        status = "✓" if result == expected else "✗"
        if result != expected:
            print(f"{status} INPUT:    {inp!r}")
            print(f"  EXPECTED: {expected!r}")
            print(f"  GOT:      {result!r}")
        else:
            print(f"{status} {inp!r}  →  {result!r}")
            passed += 1
    print(f"\n{passed}/{len(tests)} tests passed.")


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
def main() -> None:
    args      = sys.argv[1:]
    dry_run   = "--dry-run"      in args
    recursive = "--no-recursive" not in args
    test_mode = "--test"         in args

    workers = DEFAULT_WORKERS
    for i, arg in enumerate(args):
        if arg.startswith("--workers="):
            try:
                workers = max(1, int(arg.split("=", 1)[1]))
            except ValueError:
                pass
        elif arg == "--workers" and i + 1 < len(args):
            try:
                workers = max(1, int(args[i + 1]))
            except ValueError:
                pass

    paths = [a for a in args if not a.startswith("--")]

    if test_mode:
        _run_tests()
        return

    log = _setup_logging()

    log.info("=" * 60)
    log.info("Strip Duplicates" + (" [DRY RUN]" if dry_run else ""))
    log.info(f"  Workers : {workers}")
    log.info("=" * 60)

    if not paths:
        print("Usage:")
        print("  python strip_duplicates.py <folder> [--dry-run] [--no-recursive] [--workers N]")
        print("  python strip_duplicates.py --test")
        return

    for raw in paths:
        folder = Path(raw)
        if not folder.exists() or not folder.is_dir():
            log.warning(f"Not a valid directory, skipping: {folder}")
            continue
        log.info(f"Scanning: {folder}")
        process_folder(folder, recursive=recursive, dry_run=dry_run, log=log, workers=workers)

    log.info("=" * 60)
    log.info("Strip Duplicates complete.")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
