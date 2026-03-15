"""
strip_duplicates.py

Cleans filenames / strings by removing:
  1. Duplicated number/label patterns (e.g. "ver. 9 ver.9", "ch. 12 ch.12",
     "chapter 5 Chapter 5", "9 9")
  2. Oddly spaced punctuation (e.g. "! !", ".. .", "?  ?  ?")
  3. Asymmetrically spaced hyphens (e.g. "word -word", "word- word")

Standalone usage — scan a folder and rename .cbz files in-place:
    python strip_duplicates.py "C:/path/to/folder"
    python strip_duplicates.py "C:/path/to/folder" --dry-run
    python strip_duplicates.py "C:/path/to/folder" --no-recursive

Library usage — import and call clean():
    from strip_duplicates import clean
    print(clean("Batman ver. 9 ver.9 Wow! !"))
    # -> "Batman ver. 9 Wow!!"

File conflict resolution (when rename target already exists):
    The larger file is kept; the smaller file is discarded.
    Ties (equal size) keep the destination (existing) file.
"""

import os
import re
import sys
import logging
from logging.handlers import RotatingFileHandler as _RotatingFileHandler
from pathlib import Path

# ─────────────────────────────────────────────
# CONFIGURATION — edit for standalone use
# ─────────────────────────────────────────────
LOG_FILE = r"C:\git\ComicAutomation\strip_duplicates.log"
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# COMPILED PATTERNS
# ─────────────────────────────────────────────
_LABEL_FRAG       = r'(?:ver(?:sion)?|v|ch(?:ap(?:ter)?)?|episode|ep|vol(?:ume)?|part|pt)'
_DUP_LABEL_NUM_RE = re.compile(
    rf'({_LABEL_FRAG}\.?\s*\d+(?:\.\d+)?)\s+{_LABEL_FRAG}\.?\s*(\d+(?:\.\d+)?)',
    re.IGNORECASE
)
_DUP_BARE_NUM_RE  = re.compile(r'\b(\d+(?:\.\d+)?)\s+\1\b')
_SPACED_PUNCT_RE  = re.compile(r'([!?.])(?: +\1)+')
_ASYM_HYPH_L_RE   = re.compile(r'(\S) -(\S)')   # space only on left of hyphen
_ASYM_HYPH_R_RE   = re.compile(r'(\S)- (\S)')   # space only on right of hyphen


# ─────────────────────────────────────────────
# CORE CLEANING
# ─────────────────────────────────────────────
def clean(s: str) -> str:
    """
    Apply all three cleaning passes and return the cleaned string.

    1. Labelled duplicate numbers   "ver. 9 ver.9"    -> "ver. 9"
    2. Bare duplicate numbers       "9 9"              -> "9"
    3. Spaced punctuation           "! !"              -> "!!"
    4. Asymmetric hyphens           "word -next"       -> "word-next"
       (symmetric "word - word" is left untouched)
    """
    # Pass 1: labelled duplicates
    def _replace_labeled(m: re.Match) -> str:
        first  = m.group(1)
        num2   = m.group(2)
        num1_m = re.search(r'\d+(?:\.\d+)?', first)
        if num1_m and num1_m.group() == num2:
            return first
        return m.group(0)
    s = _DUP_LABEL_NUM_RE.sub(_replace_labeled, s)

    # Pass 2: bare duplicate numbers
    s = _DUP_BARE_NUM_RE.sub(r'\1', s)

    # Pass 3: spaced punctuation
    def _collapse_punct(m: re.Match) -> str:
        ch    = m.group(1)
        count = len(re.findall(re.escape(ch), m.group(0)))
        return ch * count
    s = _SPACED_PUNCT_RE.sub(_collapse_punct, s)

    # Pass 4: asymmetric hyphens
    s = _ASYM_HYPH_L_RE.sub(r'\1-\2', s)
    s = _ASYM_HYPH_R_RE.sub(r'\1-\2', s)

    # Collapse any double-spaces left behind
    s = re.sub(r'  +', ' ', s).strip()
    return s


# ─────────────────────────────────────────────
# STANDALONE FILE RENAMER
# ─────────────────────────────────────────────
def _setup_logging() -> logging.Logger:
    """Configure logging to file + console."""
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
        pass  # log dir not writable — console only

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(sh)
    return log


def _clean_name(name: str) -> str:
    """Clean a filename stem, preserve extension."""
    p    = Path(name)
    stem = clean(p.stem)
    return stem + p.suffix


def _resolve_conflict(src: Path, dest: Path, log: logging.Logger) -> bool:
    """
    Handle a rename collision: dest already exists.
    - Keep the larger file.
    - Ties (equal size) keep dest (existing).
    Returns True if src should be renamed to dest (caller must do the rename),
    False if src should be discarded instead.
    """
    src_size  = src.stat().st_size
    dest_size = dest.stat().st_size
    if src_size > dest_size:
        log.info(
            f"    Conflict: '{dest.name}' already exists — "
            f"incoming ({src_size:,} B) > existing ({dest_size:,} B) — replacing."
        )
        dest.unlink()
        return True   # proceed with rename
    else:
        log.info(
            f"    Conflict: '{dest.name}' already exists — "
            f"existing ({dest_size:,} B) >= incoming ({src_size:,} B) — discarding incoming."
        )
        src.unlink()
        return False  # discard src


def process_folder(folder: Path, recursive: bool, dry_run: bool, log: logging.Logger) -> None:
    """Scan folder for .cbz files and rename those whose names change after cleaning."""
    pattern = "**/*.cbz" if recursive else "*.cbz"
    cbz_files = sorted(folder.glob(pattern))

    if not cbz_files:
        log.info(f"No .cbz files found in: {folder}")
        return

    log.info(f"Found {len(cbz_files)} .cbz file(s) in: {folder}")
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

    log.info(
        f"Done — {renamed} renamed, {unchanged} unchanged, "
        f"{discarded} discarded (conflict, kept larger), {skipped} error(s)."
    )


# ─────────────────────────────────────────────
# SELF-TEST
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
        ("word - next",                   "word - next"),   # symmetric, unchanged
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
    recursive = "--no-recursive" not in args   # recursive by default
    test_mode = "--test"         in args
    paths     = [a for a in args if not a.startswith("--")]

    if test_mode:
        _run_tests()
        return

    log = _setup_logging()

    log.info("=" * 60)
    log.info("Strip Duplicates" + (" [DRY RUN]" if dry_run else ""))
    log.info("=" * 60)

    if not paths:
        print("Usage:")
        print("  python strip_duplicates.py <folder> [--dry-run] [--no-recursive]")
        print("  python strip_duplicates.py --test")
        return

    for raw in paths:
        folder = Path(raw)
        if not folder.exists() or not folder.is_dir():
            log.warning(f"Not a valid directory, skipping: {folder}")
            continue
        log.info(f"Scanning: {folder}")
        process_folder(folder, recursive=recursive, dry_run=dry_run, log=log)

    log.info("=" * 60)
    log.info("Strip Duplicates complete.")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
