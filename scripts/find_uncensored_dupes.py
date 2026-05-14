"""
find_uncensored_dupes.py
------------------------
Scans comic library folders for directories that appear to be censored/uncensored
duplicates of each other, then moves the matched pair(s) into a _Check folder for
manual review.

Usage:
    python find_uncensored_dupes.py [--live] [--move censored|uncensored|both]
                                    [--library <path> [<path> ...]]

Options:
    --live              Actually move folders (default is dry run)
    --move              Which folder(s) to move into _Check:
                          censored    = move only the censored counterpart
                          uncensored  = move only the uncensored/decensored folder
                          both        = move both (DEFAULT)
    --library <path>    One or more library root paths to scan
                        (default: \\\\tower\\media\\comics\\Comix)

The script uses fuzzy matching: both names are normalised (lowercased, punctuation
stripped, whitespace collapsed) before comparing, so minor title differences are
tolerated.

_Check folder is always created inside the library root being scanned.
"""

import argparse
import logging
import os
import re
import shutil
import unicodedata
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_LIBRARIES = [r"\\tower\media\comics\Comix"]
CHECK_FOLDER_NAME = "_Check"

MARKER_WORDS = re.compile(r"\b(uncensored|decensored)\b", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def to_raw_unc(p: Path) -> Path:
    """Prefix UNC paths with \\?\\UNC\\ to bypass Windows 260-char and
    trailing-dot normalisation quirks."""
    s = str(p)
    if s.startswith("\\\\?\\"):
        return p  # already raw
    if s.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + s[2:])
    return Path("\\\\?\\" + s)


def safe_exists(p: Path) -> bool:
    try:
        return to_raw_unc(p).exists()
    except OSError:
        return False


def safe_mkdir(p: Path) -> None:
    to_raw_unc(p).mkdir(parents=True, exist_ok=True)


def normalise(name: str) -> str:
    """Return a lowercase, punctuation-free, whitespace-collapsed version of
    *name* with marker words removed — used for fuzzy duplicate detection."""
    # Unicode normalise
    name = unicodedata.normalize("NFKD", name)
    # Remove marker words
    name = MARKER_WORDS.sub("", name)
    # Lowercase
    name = name.lower()
    # Strip punctuation / special chars (keep alphanumeric + space)
    name = re.sub(r"[^a-z0-9\s]", " ", name)
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name).strip()
    return name


def move_folder(src: Path, dest_parent: Path, dry_run: bool, label: str) -> None:
    """Move *src* directory into *dest_parent*, handling collisions by appending
    a numeric suffix.  Uses raw UNC paths to avoid Windows trailing-dot issues."""
    dest = dest_parent / src.name
    # Resolve collision
    if safe_exists(dest):
        stem = src.name
        for i in range(1, 100):
            candidate = dest_parent / f"{stem} ({i})"
            if not safe_exists(candidate):
                dest = candidate
                break

    if dry_run:
        log.info("    [DRY RUN] Would move %s  ->  %s", src.name, dest_parent)
        return

    raw_src = to_raw_unc(src)
    raw_dst = to_raw_unc(dest)

    try:
        os.rename(str(raw_src), str(raw_dst))
        log.info("    Moved (%s): %s  ->  _Check\\%s", label, src.name, dest.name)
    except OSError:
        # Rename failed (e.g. cross-volume); fall back to copy+delete
        shutil.copytree(str(raw_src), str(raw_dst))
        shutil.rmtree(str(raw_src))
        log.info(
            "    Copied+deleted (%s): %s  ->  _Check\\%s", label, src.name, dest.name
        )


# ---------------------------------------------------------------------------
# Core scan
# ---------------------------------------------------------------------------


def scan_library(lib_root: Path, dry_run: bool, move_which: str) -> None:
    log.info("=" * 60)
    log.info("Scanning: %s%s", lib_root, "  [DRY RUN]" if dry_run else "")
    log.info("=" * 60)

    if not safe_exists(lib_root):
        log.error("Library root not found: %s", lib_root)
        return

    # Collect immediate subdirectories only (one level deep, like the merger script)
    try:
        all_dirs = sorted(
            [d for d in lib_root.iterdir() if d.is_dir() and d.name != CHECK_FOLDER_NAME],
            key=lambda d: d.name.lower(),
        )
    except PermissionError as exc:
        log.error("Cannot list %s: %s", lib_root, exc)
        return

    log.info("  Found %d subdirectories", len(all_dirs))

    # Split into two buckets: those that contain a marker word, and those that don't
    uncensored_dirs: list[Path] = []
    normal_dirs: list[Path] = []

    for d in all_dirs:
        if MARKER_WORDS.search(d.name):
            uncensored_dirs.append(d)
        else:
            normal_dirs.append(d)

    log.info(
        "  %d folder(s) contain 'uncensored'/'decensored'; %d are candidates for matching",
        len(uncensored_dirs),
        len(normal_dirs),
    )
    log.info("")

    if not uncensored_dirs:
        log.info("  Nothing to do.")
        return

    # Build normalised lookup for the normal dirs
    normal_map: dict[str, Path] = {normalise(d.name): d for d in normal_dirs}

    check_dir = lib_root / CHECK_FOLDER_NAME
    pairs: list[tuple[Path, Path | None]] = []  # (uncensored_dir, matched_normal_dir|None)

    for u_dir in uncensored_dirs:
        key = normalise(u_dir.name)
        match = normal_map.get(key)
        pairs.append((u_dir, match))

    # Report findings
    matched = [(u, n) for u, n in pairs if n is not None]
    unmatched = [(u, n) for u, n in pairs if n is None]

    log.info("  Matched pairs (both sides exist): %d", len(matched))
    for u, n in matched:
        log.info("    '%s'", u.name)
        log.info("    '%s'", n.name)
        log.info("")

    if unmatched:
        log.info(
            "  Uncensored/decensored folders with NO matching counterpart: %d",
            len(unmatched),
        )
        for u, _ in unmatched:
            log.info("    '%s'  (no match found -- skipping)", u.name)
        log.info("")

    if not matched:
        log.info("  No pairs to move.")
        return

    # Create _Check dir
    if not dry_run:
        safe_mkdir(check_dir)
    else:
        log.info("  [DRY RUN] Would create: %s", check_dir)

    # Move according to --move preference
    moved_count = 0
    for u_dir, n_dir in matched:
        log.info("  Processing pair: '%s' / '%s'", u_dir.name, n_dir.name)

        if move_which in ("uncensored", "both"):
            move_folder(u_dir, check_dir, dry_run, label="uncensored")
            moved_count += 1

        if move_which in ("censored", "both") and n_dir is not None:
            move_folder(n_dir, check_dir, dry_run, label="censored")
            moved_count += 1

    log.info("")
    log.info(
        "  Summary: %d pair(s) found, %d folder(s) %s.",
        len(matched),
        moved_count,
        "would be moved" if dry_run else "moved",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find censored/uncensored duplicate comic folders and quarantine them.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Actually move folders (omit for dry run)",
    )
    parser.add_argument(
        "--move",
        choices=["censored", "uncensored", "both"],
        default="both",
        metavar="censored|uncensored|both",
        help="Which folder(s) of each pair to move into _Check (default: both)",
    )
    parser.add_argument(
        "--library",
        nargs="+",
        metavar="PATH",
        default=DEFAULT_LIBRARIES,
        help="Library root path(s) to scan",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dry_run = not args.live

    log.info("CBZ Uncensored Duplicate Finder")
    log.info("Mode    : %s", "DRY RUN" if dry_run else "LIVE")
    log.info("Moving  : %s", args.move)
    log.info("Libraries: %s", args.library)
    log.info("")

    for lib_path in args.library:
        scan_library(Path(lib_path), dry_run=dry_run, move_which=args.move)

    log.info("")
    log.info("Done.")


if __name__ == "__main__":
    main()
