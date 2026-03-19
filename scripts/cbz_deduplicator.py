"""
cbz_deduplicator.py — CBZ Deduplicator (parallelised)

Changes in this version
────────────────────────
• --workers N  (default: min(8, cpu_count)).  Pass --workers 1 for serial.
• Hashing/duplicate-detection per directory is parallelised with
  ThreadPoolExecutor (I/O-bound).
• Each directory is an independent unit of work — no shared mutable state
  between threads.
• Deletion/packing still happens inside each worker (safe, isolated per dir).
• --no-recursive still supported.
"""

from __future__ import annotations

import os
import re
import sys
import zipfile
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from logging.handlers import RotatingFileHandler as _RotatingFileHandler

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
SCAN_FOLDERS: list[str] = [
    r"\\tower\media\comics\Comix",
    r"\\tower\media\comics\Manga",
]
LOG_FILE = r"C:\git\ComicAutomation\cbz_deduplicator.log"
DEFAULT_WORKERS = min(8, os.cpu_count() or 4)

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
# NAME NORMALISATION
# ─────────────────────────────────────────────
_NORM_RE = re.compile(r"[\s\-_.,!?'\"]+")

def _normalise(stem: str) -> str:
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
            log.info(f"  CBR/CBZ PAIR [{folder.name}]  keep: '{cbz.name}'  delete: '{cbr.name}'")
            _delete(cbr, dry_run, "cbr superseded by cbz")
            deleted += 1
    return deleted


# ─────────────────────────────────────────────
# TASK 3 — LOOSE IMAGE FOLDERS → CBZ
# ─────────────────────────────────────────────
def _is_image_folder(folder: Path) -> bool:
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
    cbz_path = folder.parent / (folder.name + ".cbz")
    all_files = sorted(folder.iterdir(), key=lambda p: (
        p.name.lower() != "comicinfo.xml",
        [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', p.name)]
    ))
    if cbz_path.exists():
        log.warning(f"  IMAGE FOLDER '{folder.name}': target '{cbz_path.name}' already exists — skipping.")
        return False
    if dry_run:
        log.info(f"  [DRY RUN] Would pack {len(all_files)} file(s) from '{folder.name}' -> '{cbz_path.name}'")
        return True
    try:
        with zipfile.ZipFile(cbz_path, "w", compression=zipfile.ZIP_STORED) as zf:
            for item in all_files:
                zf.write(item, arcname=item.name)
        log.info(f"  IMAGE FOLDER packed: '{folder.name}' ({len(all_files)} file(s)) -> '{cbz_path.name}'")
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
        log.warning(f"  Archive created but could not remove source folder '{folder.name}': {e}")
    return True

def process_image_folders(parent: Path, dry_run: bool) -> int:
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
# PER-DIRECTORY WORKER
# ─────────────────────────────────────────────
def _process_single_dir(d: Path, dry_run: bool) -> tuple[int, int]:
    """
    Run duplicate and CBR/CBZ checks on a single directory.
    Returns (cbz_deleted, cbr_deleted).
    Isolated — safe to call from multiple threads simultaneously.
    """
    try:
        cbz_deleted = find_cbz_duplicates(d, dry_run)
        cbr_deleted = find_cbr_cbz_pairs(d, dry_run)
        return cbz_deleted, cbr_deleted
    except OSError as e:
        log.error(f"  Error scanning '{d}': {e}")
        return 0, 0


# ─────────────────────────────────────────────
# FOLDER PROCESSING
# ─────────────────────────────────────────────
def process_folder(folder: Path, recursive: bool, dry_run: bool, workers: int) -> None:
    if not folder.exists() or not folder.is_dir():
        log.warning(f"Folder not found, skipping: {folder}")
        return

    log.info(f"\n{'=' * 60}")
    log.info(f"Scanning: {folder}")
    log.info(f"{'=' * 60}")

    if recursive:
        dirs_to_check = [folder] + sorted(d for d in folder.rglob("*") if d.is_dir())
    else:
        dirs_to_check = [folder] + sorted(d for d in folder.iterdir() if d.is_dir())

    log.info(f"  Checking {len(dirs_to_check)} director(ies) with {workers} worker(s).")

    total_dup_deleted = 0
    total_cbr_deleted = 0

    if workers == 1:
        for d in dirs_to_check:
            cbz_d, cbr_d = _process_single_dir(d, dry_run)
            total_dup_deleted += cbz_d
            total_cbr_deleted += cbr_d
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_dir = {
                executor.submit(_process_single_dir, d, dry_run): d
                for d in dirs_to_check
            }
            for future in as_completed(future_to_dir):
                d = future_to_dir[future]
                try:
                    cbz_d, cbr_d = future.result()
                    total_dup_deleted += cbz_d
                    total_cbr_deleted += cbr_d
                except Exception as e:
                    log.error(f"  Worker failed for '{d}': {e}")

    # Image folder packing is always serial (avoids filesystem races on subdirs)
    total_img_packed = process_image_folders(folder, dry_run)

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
    recursive = "--no-recursive" not in args

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
    targets = paths if paths else SCAN_FOLDERS

    log.info("=" * 60)
    log.info("CBZ Deduplicator" + (" [DRY RUN]" if dry_run else ""))
    log.info(f"  Mode    : {'recursive' if recursive else 'single-level (--no-recursive)'}")
    log.info(f"  Workers : {workers}")
    log.info("=" * 60)

    for target in targets:
        process_folder(Path(target), recursive=recursive, dry_run=dry_run, workers=workers)

    log.info("\n" + "=" * 60)
    log.info("CBZ Deduplicator complete.")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
