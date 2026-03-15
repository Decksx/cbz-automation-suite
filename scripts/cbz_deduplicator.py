"""
CBZ Deduplicator
Scans one or more series folders for three classes of problem:

  1. DUPLICATE FILES — pairs of .cbz files whose names differ only by
     whitespace, hyphens, underscores, or punctuation spacing (e.g.
     "Batman - Ch. 12.cbz" vs "Batman Ch.12.cbz").  The larger file is
     kept; the smaller is deleted.  Ties keep the alphabetically-first name.

  2. CBR vs CBZ DUPLICATES — pairs where the same base name exists as both
     a .cbr and a .cbz.  The .cbz is always kept; the .cbr is deleted.

  3. LOOSE IMAGE FOLDERS — subdirectories that contain only image files
     (jpg, jpeg, png, gif, webp, avif, bmp, tiff) plus optionally a single
     ComicInfo.xml, and no other file types.  These are zipped into a .cbz
     archive alongside the source folder, then the source folder is removed.

Usage:
    python cbz_deduplicator.py                          # scan all SCAN_FOLDERS (recursive)
    python cbz_deduplicator.py "C:/path/to/series"     # scan one folder (recursive)
    python cbz_deduplicator.py --dry-run               # preview, no changes
    python cbz_deduplicator.py --no-recursive          # disable recursive descent
"""

import os
import re
import sys
import zipfile
import logging
from pathlib import Path
from logging.handlers import RotatingFileHandler as _RotatingFileHandler

# ─────────────────────────────────────────────
# CONFIGURATION — edit these as needed
# ─────────────────────────────────────────────
SCAN_FOLDERS: list[str] = [
    r"\\tower\media\comics\Comix",
    r"\\tower\media\comics\Manga",
]
LOG_FILE = r"C:\git\ComicAutomation\cbz_deduplicator.log"
# ─────────────────────────────────────────────

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".tiff", ".tif"}

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
log = logging.getLogger("cbz_deduplicator")
log.setLevel(logging.DEBUG)

_fmt = logging.Formatter(
    "%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
_fh = _RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_fh.setFormatter(_fmt)
log.addHandler(_fh)
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
log.addHandler(_sh)

# ─────────────────────────────────────────────
# NAME NORMALISATION (for comparison only)
# ─────────────────────────────────────────────
_NORM_RE = re.compile(r"[\s\-_.,!?'\"]+")


def _normalise(stem: str) -> str:
    """
    Reduce a filename stem to a canonical form for duplicate comparison.
    Lowercases, strips/collapses all whitespace, hyphens, underscores,
    and common punctuation.  Never written to disk.
    """
    return _NORM_RE.sub("", stem.lower())


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def _delete(path: Path, dry_run: bool, reason: str) -> None:
    if dry_run:
        log.info(f"    [DRY RUN] Would delete ({reason}): {path.name}")
    else:
        try:
            path.unlink()
            log.info(f"    Deleted ({reason}): {path.name}")
        except OSError as e:
            log.error(f"    Failed to delete {path.name}: {e}")


# ─────────────────────────────────────────────
# TASK 1 — DUPLICATE CBZ FILES
# ─────────────────────────────────────────────
def find_cbz_duplicates(folder: Path, dry_run: bool) -> int:
    """
    Within a single directory, find .cbz files whose normalised stems
    are identical.  Keep the larger file; delete the rest.
    Returns number of files deleted (or would-delete in dry-run).
    """
    cbz_files = [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() == ".cbz"]
    if len(cbz_files) < 2:
        return 0

    groups: dict[str, list[Path]] = {}
    for f in cbz_files:
        key = _normalise(f.stem)
        groups.setdefault(key, []).append(f)

    deleted = 0
    for key, group in groups.items():
        if len(group) < 2:
            continue

        group.sort(key=lambda p: (-p.stat().st_size, p.name))
        keep = group[0]
        duplicates = group[1:]

        log.info(f"  DUPLICATE GROUP [{folder.name}]  keep: '{keep.name}'")
        for dup in duplicates:
            log.info(f"    vs '{dup.name}'  (keep={keep.stat().st_size:,}B  discard={dup.stat().st_size:,}B)")
            _delete(dup, dry_run, "duplicate")
            deleted += 1

    return deleted


# ─────────────────────────────────────────────
# TASK 2 — CBR vs CBZ
# ─────────────────────────────────────────────
def find_cbr_cbz_pairs(folder: Path, dry_run: bool) -> int:
    """
    Find files where the same normalised stem exists as both .cbr and .cbz.
    Always keeps the .cbz; deletes the .cbr.
    Returns number of .cbr files deleted (or would-delete).
    """
    files = [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in {".cbz", ".cbr"}]
    if not files:
        return 0

    cbz_stems: dict[str, Path] = {}
    cbr_stems: dict[str, Path] = {}

    for f in files:
        key = _normalise(f.stem)
        if f.suffix.lower() == ".cbz":
            cbz_stems[key] = f
        else:
            cbr_stems[key] = f

    deleted = 0
    for key, cbr in cbr_stems.items():
        if key in cbz_stems:
            cbz = cbz_stems[key]
            log.info(
                f"  CBR/CBZ PAIR [{folder.name}]  "
                f"keep: '{cbz.name}'  delete: '{cbr.name}'"
            )
            _delete(cbr, dry_run, "cbr superseded by cbz")
            deleted += 1

    return deleted


# ─────────────────────────────────────────────
# TASK 3 — LOOSE IMAGE FOLDERS → CBZ
# ─────────────────────────────────────────────
def _is_image_folder(folder: Path) -> bool:
    """
    Return True if folder contains only image files + optionally one
    ComicInfo.xml, and nothing else (no subdirectories, no other file types).
    Must contain at least one image.
    """
    if not folder.is_dir():
        return False

    has_image = False
    for item in folder.iterdir():
        if item.is_dir():
            return False
        ext = item.suffix.lower()
        if ext in IMAGE_EXTENSIONS:
            has_image = True
        elif item.name.lower() == "comicinfo.xml":
            pass
        else:
            return False

    return has_image


def convert_image_folder(folder: Path, dry_run: bool) -> bool:
    """
    Pack a loose image folder into a .cbz archive placed next to the folder.
    Images are sorted naturally (numerically by embedded digits).
    ComicInfo.xml is placed first in the archive if present.
    Returns True on success (or simulated success in dry-run).
    """
    cbz_path = folder.parent / (folder.name + ".cbz")

    all_files = sorted(folder.iterdir(), key=lambda p: (
        p.name.lower() != "comicinfo.xml",
        [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', p.name)]
    ))

    if cbz_path.exists():
        log.warning(
            f"  IMAGE FOLDER '{folder.name}': target '{cbz_path.name}' already exists — skipping."
        )
        return False

    if dry_run:
        log.info(
            f"  [DRY RUN] Would pack {len(all_files)} file(s) from "
            f"'{folder.name}' -> '{cbz_path.name}'"
        )
        return True

    try:
        with zipfile.ZipFile(cbz_path, "w", compression=zipfile.ZIP_STORED) as zf:
            for item in all_files:
                zf.write(item, arcname=item.name)
        log.info(
            f"  IMAGE FOLDER packed: '{folder.name}' ({len(all_files)} file(s)) "
            f"-> '{cbz_path.name}'"
        )
    except Exception as e:
        log.error(f"  Failed to create '{cbz_path.name}': {e}")
        if cbz_path.exists():
            cbz_path.unlink(missing_ok=True)
        return False

    try:
        import shutil
        shutil.rmtree(folder)
        log.info(f"  Removed source folder: '{folder.name}'")
    except OSError as e:
        log.warning(
            f"  Archive created but could not remove source folder '{folder.name}': {e}"
        )

    return True


def process_image_folders(parent: Path, dry_run: bool) -> int:
    """
    Scan immediate subdirectories of parent for loose image folders.
    Returns number of folders converted (or would-convert).
    """
    converted = 0
    try:
        subdirs = sorted(d for d in parent.iterdir() if d.is_dir())
    except OSError as e:
        log.error(f"  Cannot iterate '{parent}': {e}")
        return 0

    for subdir in subdirs:
        if _is_image_folder(subdir):
            log.info(f"  IMAGE FOLDER detected: '{subdir.name}'")
            if convert_image_folder(subdir, dry_run):
                converted += 1

    return converted


# ─────────────────────────────────────────────
# FOLDER PROCESSING
# ─────────────────────────────────────────────
def process_folder(folder: Path, recursive: bool, dry_run: bool) -> None:
    """
    Run all three tasks against folder.
    If recursive=True, also descend into subdirectories for duplicate checks.
    Image-folder conversion always targets immediate subdirectories only.
    """
    if not folder.exists() or not folder.is_dir():
        log.warning(f"Folder not found, skipping: {folder}")
        return

    log.info(f"\n{'=' * 60}")
    log.info(f"Scanning: {folder}")
    log.info(f"{'=' * 60}")

    total_dup_deleted  = 0
    total_cbr_deleted  = 0
    total_img_packed   = 0

    if recursive:
        dirs_to_check = [folder] + sorted(d for d in folder.rglob("*") if d.is_dir())
    else:
        dirs_to_check = [folder] + sorted(d for d in folder.iterdir() if d.is_dir())

    for d in dirs_to_check:
        cbz_deleted = find_cbz_duplicates(d, dry_run)
        cbr_deleted = find_cbr_cbz_pairs(d, dry_run)
        total_dup_deleted += cbz_deleted
        total_cbr_deleted += cbr_deleted

    total_img_packed += process_image_folders(folder, dry_run)

    log.info(f"\n  Summary for: {folder}")
    log.info(f"    Duplicate .cbz removed : {total_dup_deleted}")
    log.info(f"    .cbr superseded by .cbz: {total_cbr_deleted}")
    log.info(f"    Image folders packed   : {total_img_packed}")
    if dry_run:
        log.info("    (Dry-run — no changes written)")


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
def main() -> None:
    args      = sys.argv[1:]
    dry_run   = "--dry-run"      in args
    recursive = "--no-recursive" not in args   # recursive by default
    paths     = [a for a in args if not a.startswith("--")]

    targets = paths if paths else SCAN_FOLDERS

    log.info("=" * 60)
    log.info("CBZ Deduplicator" + (" [DRY RUN]" if dry_run else ""))
    log.info(f"  Mode      : {'recursive' if recursive else 'single-level (--no-recursive)'}")
    log.info("=" * 60)

    for target in targets:
        process_folder(Path(target), recursive=recursive, dry_run=dry_run)

    log.info("\n" + "=" * 60)
    log.info("CBZ Deduplicator complete.")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
