"""
cbz_library_maintenance.py

Aggressively consolidated maintenance tool for archive cleanup, series
organization, and retroactive metadata repair.

This is intended to replace day-to-day use of:

- cbz_deduplicator.py
- strip_duplicates.py
- cbz_folder_merger.py
- cbz_series_matcher.py
- find_uncensored_dupes.py
- cbz_number_tagger.py

Specialized tools that remain separate for now:

- cbz_watcher.py
- cbz_sanitizer.py
- cbz_compilation_resolver.py
- cbz_gap_checker.py
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import statistics
import sys
import threading
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from difflib import SequenceMatcher
from logging.handlers import RotatingFileHandler
from pathlib import Path


try:
    from scripts.cbz_core import (
        clean_directory_name,
        clean_filename,
        extract_chapter_number,
        extract_volume_number,
        is_generic_title,
        parse_comic_name,
        update_comicinfo_xml,
    )
except ModuleNotFoundError:
    from cbz_core import (  # type: ignore[no-redef]
        clean_directory_name,
        clean_filename,
        extract_chapter_number,
        extract_volume_number,
        is_generic_title,
        parse_comic_name,
        update_comicinfo_xml,
    )


LOG_FILE = r"C:\git\ComicAutomation\Logs\cbz_library_maintenance.log"
DEFAULT_WORKERS = min(8, os.cpu_count() or 4)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".tiff", ".tif"}
CHECK_FOLDER_NAME = "_Check"

_DUP_LABEL_FRAG = r"(?:ver(?:sion)?|v|ch(?:ap(?:ter)?)?|episode|ep|vol(?:ume)?|part|pt)"
_DUP_LABEL_NUM_RE = re.compile(
    rf"({_DUP_LABEL_FRAG}\.?\s*\d+(?:\.\d+)?)\s+{_DUP_LABEL_FRAG}\.?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_DUP_BARE_NUM_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s+\1\b")
_SPACED_PUNCT_RE = re.compile(r"([!?.])(?: +\1)+")
_ASYM_HYPH_L_RE = re.compile(r"(\S) -(\S)")
_ASYM_HYPH_R_RE = re.compile(r"(\S)- (\S)")
_ARCHIVE_NORM_RE = re.compile(r"[\s\-_.,!?'\"]+")
_PUNCT_RE = re.compile(r"[^\w\s]")
_SPACES_RE = re.compile(r"\s+")
_MARKER_WORDS_RE = re.compile(r"\b(uncensored|decensored)\b", re.IGNORECASE)
_TRAILING_TOKEN_RE = re.compile(
    r"[\s_\-]*(?:"
    r"ch(?:ap(?:ter)?)?p?\.?\s*\d[\d.]*"
    r"|issue\s*\d[\d.]*"
    r"|ep(?:isode)?\.?\s*\d[\d.]*"
    r"|vol(?:ume)?\.?\s*\d[\d.]*"
    r"|v\d[\d.]*(?=\s*$)"
    r"|\d+$"
    r")[\s_\-.,]*$",
    re.IGNORECASE,
)
_CHAPTER_TOKEN_RE = re.compile(r"(ch(?:ap(?:ter)?)?p?\.?\s*)(\d[\d.]*)", re.IGNORECASE)


def setup_logging(verbose: bool = False) -> logging.Logger:
    log = logging.getLogger("cbz_library_maintenance")
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.handlers.clear()

    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", "%Y-%m-%d %H:%M:%S")
    try:
        Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError:
        pass

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(sh)
    return log


log = setup_logging()


class ProgressReporter:
    """Emit machine-readable progress lines for GUI launchers."""

    def __init__(self, total: int, label: str = "items") -> None:
        self.total = max(0, total)
        self.label = label
        self.current = 0
        self.enabled = os.environ.get("CBZ_PROGRESS") == "1"
        self._lock = threading.Lock()
        self.emit(0)

    def step(self, amount: int = 1) -> None:
        with self._lock:
            self.current = min(self.total, self.current + amount)
            self.emit(self.current)

    def emit(self, current: int) -> None:
        if not self.enabled:
            return
        percent = 100 if self.total == 0 else int((current / self.total) * 100)
        print(f"CBZ_PROGRESS {current}/{self.total} {percent}% {self.label}", flush=True)


@dataclass
class MaintenanceStats:
    renamed: int = 0
    deleted: int = 0
    packed: int = 0
    moved: int = 0
    merged: int = 0
    updated_xml: int = 0
    skipped: int = 0
    errors: int = 0

    def add(self, other: "MaintenanceStats") -> "MaintenanceStats":
        self.renamed += other.renamed
        self.deleted += other.deleted
        self.packed += other.packed
        self.moved += other.moved
        self.merged += other.merged
        self.updated_xml += other.updated_xml
        self.skipped += other.skipped
        self.errors += other.errors
        return self


def iter_dirs_with_files(root: Path, recursive: bool = True) -> list[Path]:
    if not root.exists():
        return []
    pattern = "**/*" if recursive else "*"
    dirs: set[Path] = set()
    for p in root.glob(pattern):
        if p.is_file():
            dirs.add(p.parent)
    return sorted(dirs)


def iter_series_dirs(root: Path) -> list[Path]:
    """Directories containing CBZ files directly are treated as series dirs."""
    result: list[Path] = []
    for d, _, files in os.walk(root):
        if any(f.lower().endswith(".cbz") for f in files):
            result.append(Path(d))
    return sorted(result)


def natural_key(path: Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", path.name)]


def clean_duplicate_tokens(name: str) -> str:
    def replace_labeled(m: re.Match) -> str:
        first = m.group(1)
        num2 = m.group(2)
        num1_m = re.search(r"\d+(?:\.\d+)?", first)
        if num1_m and num1_m.group() == num2:
            return first
        return m.group(0)

    s = _DUP_LABEL_NUM_RE.sub(replace_labeled, name)
    s = _DUP_BARE_NUM_RE.sub(r"\1", s)

    def collapse_punct(m: re.Match) -> str:
        ch = m.group(1)
        count = len(re.findall(re.escape(ch), m.group(0)))
        return ch * count

    s = _SPACED_PUNCT_RE.sub(collapse_punct, s)
    s = _ASYM_HYPH_L_RE.sub(r"\1-\2", s)
    s = _ASYM_HYPH_R_RE.sub(r"\1-\2", s)
    return re.sub(r"  +", " ", s).strip()


def normalise_archive_key(stem: str) -> str:
    return _ARCHIVE_NORM_RE.sub("", stem.lower())


def normalise_series_key(name: str) -> str:
    name = _MARKER_WORDS_RE.sub("", name)
    name = name.lower()
    name = _PUNCT_RE.sub(" ", name)
    return _SPACES_RE.sub(" ", name).strip()


def fmt_number(value: float) -> str:
    return str(int(value)) if value == int(value) else str(value)


def extract_chapter_float(stem: str) -> float | None:
    chapter = extract_chapter_number(stem)
    if chapter is None:
        return None
    try:
        return float(chapter)
    except ValueError:
        return None


def larger_file_wins(src: Path, dest: Path, dry_run: bool) -> str:
    if not dest.exists():
        if dry_run:
            log.info("    [DRY RUN] Would move: %s -> %s", src.name, dest)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
        return "moved"

    src_size = src.stat().st_size
    dest_size = dest.stat().st_size
    if src_size > dest_size:
        log.info("    Collision: incoming larger, replacing: %s", dest.name)
        if not dry_run:
            dest.unlink()
            shutil.move(str(src), str(dest))
        return "replaced"

    log.info("    Collision: existing larger/equal, discarding incoming: %s", src.name)
    if not dry_run:
        src.unlink()
    return "discarded"


def rename_duplicate_tokens_in_dir(folder: Path, dry_run: bool) -> MaintenanceStats:
    stats = MaintenanceStats()
    for cbz in sorted(folder.glob("*.cbz")):
        new_name = clean_duplicate_tokens(cbz.name)
        if new_name == cbz.name:
            continue
        dest = cbz.parent / new_name
        if dry_run:
            log.info("  [DRY RUN] Would rename: %s -> %s", cbz.name, new_name)
            stats.renamed += 1
        elif dest.exists():
            outcome = larger_file_wins(cbz, dest, dry_run=False)
            if outcome in {"moved", "replaced"}:
                stats.renamed += 1
            else:
                stats.deleted += 1
        else:
            cbz.rename(dest)
            log.info("  Renamed: %s -> %s", cbz.name, new_name)
            stats.renamed += 1
    return stats


def dedupe_archives_in_dir(folder: Path, dry_run: bool) -> MaintenanceStats:
    stats = MaintenanceStats()
    files = [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in {".cbz", ".cbr"}]
    if not files:
        return stats

    by_key: dict[str, list[Path]] = {}
    for f in files:
        by_key.setdefault(normalise_archive_key(f.stem), []).append(f)

    for key, group in by_key.items():
        if len(group) < 2:
            continue

        cbz_files = [p for p in group if p.suffix.lower() == ".cbz"]
        if cbz_files:
            keep = sorted(cbz_files, key=lambda p: (-p.stat().st_size, p.name.lower()))[0]
        else:
            keep = sorted(group, key=lambda p: (-p.stat().st_size, p.name.lower()))[0]

        log.info("  Duplicate group [%s], keep: %s", folder.name, keep.name)
        for dup in group:
            if dup == keep:
                continue
            reason = "cbr superseded by cbz" if keep.suffix.lower() == ".cbz" and dup.suffix.lower() == ".cbr" else "duplicate"
            if dry_run:
                log.info("    [DRY RUN] Would delete (%s): %s", reason, dup.name)
            else:
                dup.unlink()
                log.info("    Deleted (%s): %s", reason, dup.name)
            stats.deleted += 1
    return stats


def is_packable_image_folder(folder: Path) -> bool:
    if not folder.is_dir():
        return False
    has_image = False
    for item in folder.iterdir():
        if item.is_dir():
            return False
        if item.suffix.lower() in IMAGE_EXTENSIONS:
            has_image = True
        elif item.name.lower() == "comicinfo.xml":
            continue
        else:
            return False
    return has_image


def pack_image_folder(folder: Path, dry_run: bool) -> MaintenanceStats:
    stats = MaintenanceStats()
    if not is_packable_image_folder(folder):
        return stats

    cbz_path = folder.parent / f"{folder.name}.cbz"
    packable = sorted(
        [p for p in folder.iterdir() if p.is_file()],
        key=lambda p: (p.name.lower() != "comicinfo.xml", natural_key(p)),
    )

    if dry_run:
        log.info("  [DRY RUN] Would pack image folder: %s -> %s", folder.name, cbz_path.name)
        stats.packed += 1
        return stats

    tmp_path = cbz_path.with_suffix(".tmp.cbz")
    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_STORED) as zf:
            for item in packable:
                zf.write(item, arcname=item.name)

        if cbz_path.exists():
            if tmp_path.stat().st_size > cbz_path.stat().st_size:
                cbz_path.unlink()
                tmp_path.rename(cbz_path)
                log.info("  Packed image folder and replaced smaller archive: %s", cbz_path.name)
            else:
                tmp_path.unlink()
                log.info("  Packed archive was not larger; kept existing: %s", cbz_path.name)
                stats.skipped += 1
                return stats
        else:
            tmp_path.rename(cbz_path)
            log.info("  Packed image folder: %s -> %s", folder.name, cbz_path.name)

        for item in packable:
            item.unlink(missing_ok=True)
        try:
            folder.rmdir()
        except OSError:
            pass
        stats.packed += 1
        return stats
    except Exception as exc:
        log.error("  Failed to pack %s: %s", folder, exc)
        tmp_path.unlink(missing_ok=True)
        stats.errors += 1
        return stats


def archive_clean_worker(folder: Path, args: argparse.Namespace) -> MaintenanceStats:
    stats = MaintenanceStats()
    log.info("Archive clean: %s", folder)
    if args.strip_names:
        stats.add(rename_duplicate_tokens_in_dir(folder, args.dry_run))
    if args.dedupe_archives:
        stats.add(dedupe_archives_in_dir(folder, args.dry_run))
    return stats


def run_archive_clean(args: argparse.Namespace) -> int:
    paths = [Path(p) for p in args.paths]
    dirs: set[Path] = set()
    for root in paths:
        dirs.update(iter_dirs_with_files(root, recursive=not args.no_recursive))
        if not args.no_recursive:
            dirs.add(root)

    all_dirs = sorted(dirs)
    pack_folders: list[Path] = []
    if args.pack_loose_images:
        for root in paths:
            pattern = "**/*" if not args.no_recursive else "*"
            pack_folders.extend([p for p in root.glob(pattern) if p.is_dir()])
        pack_folders = sorted(pack_folders, key=lambda p: len(p.parts), reverse=True)

    progress = ProgressReporter(len(all_dirs) + len(pack_folders), "items")
    stats = MaintenanceStats()

    if args.workers == 1:
        for d in all_dirs:
            stats.add(archive_clean_worker(d, args))
            progress.step()
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = [ex.submit(archive_clean_worker, d, args) for d in all_dirs]
            for fut in as_completed(futures):
                stats.add(fut.result())
                progress.step()

    if args.pack_loose_images:
        # Pack folders after duplicate archive cleanup to avoid parent/child races.
        for folder in pack_folders:
            stats.add(pack_image_folder(folder, args.dry_run))
            progress.step()

    log.info("Archive clean complete: %s", stats)
    return 0


def series_base_name(name: str) -> str | None:
    m = _TRAILING_TOKEN_RE.search(name)
    if not m:
        return None
    base = name[: m.start()].strip()
    return clean_directory_name(base) if base else None


def canonical_series_name(names: list[str]) -> str:
    bases = [series_base_name(n) for n in names]
    bases = [b for b in bases if b]
    candidates = bases or names
    return max(candidates, key=lambda s: (sum(1 for c in s if c.isupper()), len(s)))


def rename_generic_files(folder: Path, dry_run: bool) -> MaintenanceStats:
    """Rename generic archive names to use the containing series folder name."""
    stats = MaintenanceStats()
    cbz_files = sorted(folder.glob("*.cbz"))
    generic_files = [f for f in cbz_files if is_generic_title(f.stem)]
    if not generic_files:
        return stats

    log.info("    Renaming %d generic file(s) in %s", len(generic_files), folder.name)
    fallback_names: dict[Path, str] = {}
    for index, cbz in enumerate(generic_files, start=1):
        suffix = f" {index}" if len(generic_files) > 1 else ""
        fallback_names[cbz] = f"{folder.name}{suffix}.cbz"

    for cbz, new_name in fallback_names.items():
        dest = cbz.parent / new_name
        if cbz == dest:
            continue
        if dry_run:
            log.info("      [DRY RUN] Would rename: %s -> %s", cbz.name, new_name)
            stats.renamed += 1
            continue
        outcome = larger_file_wins(cbz, dest, dry_run=False) if dest.exists() else "moved"
        if outcome == "moved":
            if not dest.exists():
                cbz.rename(dest)
            log.info("      Renamed: %s -> %s", cbz.name, new_name)
            stats.renamed += 1
        elif outcome == "replaced":
            stats.renamed += 1
        else:
            stats.deleted += 1
    return stats


def update_series_metadata(folder: Path, dry_run: bool, workers: int = 1) -> MaintenanceStats:
    """Update ComicInfo for all CBZ files under a merged series folder."""
    stats = MaintenanceStats()
    cbz_files = sorted(folder.rglob("*.cbz"))
    if not cbz_files:
        return stats

    log.info("    Updating ComicInfo.xml for %d file(s) in %s", len(cbz_files), folder.name)
    if workers == 1 or len(cbz_files) <= 1:
        for cbz in cbz_files:
            stats.add(metadata_worker(cbz, dry_run))
        return stats

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(metadata_worker, cbz, dry_run): cbz for cbz in cbz_files}
        for fut in as_completed(futures):
            cbz = futures[fut]
            try:
                stats.add(fut.result())
            except Exception as exc:
                log.error("    ComicInfo worker failed for %s: %s", cbz.name, exc)
                stats.errors += 1
    return stats


def merge_dir_contents(src: Path, dest: Path, dry_run: bool) -> MaintenanceStats:
    stats = MaintenanceStats()
    for item in sorted(src.rglob("*")):
        if not item.is_file():
            continue
        rel = item.relative_to(src)
        target = dest / rel
        outcome = larger_file_wins(item, target, dry_run)
        if outcome in {"moved", "replaced"}:
            stats.moved += 1
        elif outcome == "discarded":
            stats.deleted += 1

    if not dry_run:
        shutil.rmtree(src, ignore_errors=True)
    stats.merged += 1
    return stats


def merge_series_dir(src: Path, dest: Path, dry_run: bool) -> MaintenanceStats:
    stats = MaintenanceStats()
    stats.add(rename_generic_files(src, dry_run))
    stats.add(merge_dir_contents(src, dest, dry_run))
    return stats


def detect_compilation_candidates(chapter_nums: list[float]) -> list[tuple[float, float, float]]:
    """Detect likely concatenated compilation numbers, e.g. 112 -> 1-12."""
    if len(chapter_nums) < 2:
        return []

    nums = sorted(set(float(n) for n in chapter_nums))
    results: list[tuple[float, float, float]] = []
    for suspect in nums:
        others = [n for n in nums if n != suspect]
        if not others or suspect <= max(others):
            continue

        gaps = [others[i + 1] - others[i] for i in range(len(others) - 1)]
        gap_to_suspect = suspect - max(others)
        if gaps:
            median_gap = statistics.median(gaps)
            if median_gap > 0 and gap_to_suspect <= 2 * median_gap:
                continue
            if median_gap == 0 and gap_to_suspect <= 2:
                continue
        elif gap_to_suspect <= 2:
            continue

        suspect_str = fmt_number(suspect)
        found_start = found_end = None
        for candidate_start in others:
            start_str = fmt_number(candidate_start)
            if len(start_str) >= len(suspect_str) or not suspect_str.startswith(start_str):
                continue
            remainder = suspect_str[len(start_str):]
            if not remainder or not remainder.isdigit():
                continue
            candidate_end = float(remainder)
            if candidate_end < 1 or candidate_end > max(others):
                continue
            if found_start is None or candidate_start > found_start:
                found_start = candidate_start
                found_end = candidate_end

        if found_start is not None and found_end is not None:
            results.append((suspect, found_start, found_end))
    return results


def rename_stem_for_compilation(stem: str, start: float, end: float) -> str:
    range_str = f"{fmt_number(start)}-{fmt_number(end)}"

    def replace_chapter(match: re.Match) -> str:
        return match.group(1) + range_str

    new_stem, count = _CHAPTER_TOKEN_RE.subn(replace_chapter, stem, count=1)
    return new_stem if count else f"{stem} {range_str}"


def update_comicinfo_range(xml: str, start: float, end: float) -> tuple[str, bool]:
    range_str = f"{fmt_number(start)}-{fmt_number(end)}"
    pattern = re.compile(r"<Number>.*?</Number>", re.IGNORECASE | re.DOTALL)
    tag = f"<Number>{range_str}</Number>"
    if pattern.search(xml):
        if pattern.search(xml).group(0) == tag:
            return xml, False
        return pattern.sub(tag, xml, count=1), True
    return xml.replace("</ComicInfo>", f"  {tag}\n</ComicInfo>"), True


def patch_comicinfo_range(cbz_path: Path, start: float, end: float, dry_run: bool) -> MaintenanceStats:
    stats = MaintenanceStats()
    entry_name, xml = read_comicinfo(cbz_path)
    if xml is None:
        stats.skipped += 1
        return stats

    new_xml, changed = update_comicinfo_range(xml, start, end)
    if not changed:
        stats.skipped += 1
        return stats

    if dry_run:
        log.info(
            "    [DRY RUN] Would update ComicInfo range Number=%s-%s in %s",
            fmt_number(start),
            fmt_number(end),
            cbz_path.name,
        )
        stats.updated_xml += 1
        return stats

    if write_comicinfo(cbz_path, entry_name, new_xml, dry_run=False):
        log.info(
            "    ComicInfo updated: Number=%s-%s in %s",
            fmt_number(start),
            fmt_number(end),
            cbz_path.name,
        )
        stats.updated_xml += 1
    else:
        stats.errors += 1
    return stats


def detect_and_fix_compilations(series_dir: Path, dry_run: bool) -> MaintenanceStats:
    stats = MaintenanceStats()
    cbz_files = sorted(series_dir.glob("*.cbz"))
    if len(cbz_files) < 2:
        return stats

    num_to_path: dict[float, Path] = {}
    for cbz in cbz_files:
        chapter = extract_chapter_float(cbz.stem)
        if chapter is not None:
            num_to_path[chapter] = cbz
    if len(num_to_path) < 2:
        return stats

    for suspect, start, end in detect_compilation_candidates(list(num_to_path.keys())):
        cbz = num_to_path.get(suspect)
        if cbz is None or not cbz.exists():
            continue

        new_stem = rename_stem_for_compilation(cbz.stem, start, end)
        new_path = cbz.parent / f"{new_stem}{cbz.suffix}"
        log.info(
            "    Compilation detected: %s looks like ch.%s-%s",
            cbz.name,
            fmt_number(start),
            fmt_number(end),
        )

        if dry_run:
            log.info("    [DRY RUN] Would rename: %s -> %s", cbz.name, new_path.name)
            stats.renamed += 1
            stats.add(patch_comicinfo_range(cbz, start, end, dry_run=True))
            continue

        if new_path != cbz:
            if new_path.exists():
                outcome = larger_file_wins(cbz, new_path, dry_run=False)
                if outcome == "discarded":
                    stats.deleted += 1
                    continue
            else:
                cbz.rename(new_path)
                log.info("    Renamed: %s -> %s", cbz.name, new_path.name)
            stats.renamed += 1
            cbz = new_path

        stats.add(patch_comicinfo_range(cbz, start, end, dry_run=False))
    return stats


def merge_chapter_folders(parent: Path, dry_run: bool, metadata_workers: int = 1) -> MaintenanceStats:
    stats = MaintenanceStats()
    children = [d for d in parent.iterdir() if d.is_dir() and d.name != CHECK_FOLDER_NAME]
    groups: dict[str, list[Path]] = {}
    for d in children:
        base = series_base_name(d.name)
        if base:
            groups.setdefault(normalise_series_key(base), []).append(d)

    for _, group in groups.items():
        if len(group) < 2:
            continue
        canonical = canonical_series_name([g.name for g in group])
        dest = parent / canonical
        log.info("  Merge chapter-folder group -> %s", dest.name)
        if dry_run:
            log.info("    [DRY RUN] Would create/use canonical folder: %s", dest)
        else:
            dest.mkdir(exist_ok=True)

        for src in group:
            if src == dest:
                continue
            stats.add(merge_series_dir(src, dest, dry_run))
        stats.add(update_series_metadata(dest, dry_run, workers=metadata_workers))
        stats.add(detect_and_fix_compilations(dest, dry_run))
    return stats


def find_series_matches(
    parent: Path,
    report_threshold: float,
    auto_threshold: float,
    dry_run: bool,
    metadata_workers: int = 1,
) -> MaintenanceStats:
    stats = MaintenanceStats()
    dirs = [d for d in parent.iterdir() if d.is_dir() and d.name != CHECK_FOLDER_NAME]
    entries = [(d, normalise_series_key(d.name)) for d in dirs]
    consumed: set[Path] = set()

    for i, (a, na) in enumerate(entries):
        if a in consumed or not na:
            continue
        for b, nb in entries[i + 1:]:
            if b in consumed or not nb:
                continue
            if not na or not nb:
                continue
            if 2 * min(len(na), len(nb)) / (len(na) + len(nb)) < report_threshold:
                continue
            sm = SequenceMatcher(None, na, nb)
            if sm.quick_ratio() < report_threshold:
                continue
            ratio = sm.ratio()
            if ratio < report_threshold:
                continue

            a_count = len(list(a.glob("*.cbz")))
            b_count = len(list(b.glob("*.cbz")))
            primary, secondary = (a, b) if (a_count, len(a.name), b.name) >= (b_count, len(b.name), a.name) else (b, a)
            log.info("  Series match %.3f: %s <-> %s", ratio, a.name, b.name)

            if ratio >= auto_threshold:
                log.info("    Auto-merge: %s -> %s", secondary.name, primary.name)
                stats.add(merge_series_dir(secondary, primary, dry_run))
                stats.add(update_series_metadata(primary, dry_run, workers=metadata_workers))
                stats.add(detect_and_fix_compilations(primary, dry_run))
                consumed.add(secondary)
            else:
                stats.skipped += 1
    return stats




# ─────────────────────────────────────────────
# POSSIBLE SAME-SERIES GROUPING FOR MANUAL REVIEW
# ─────────────────────────────────────────────
_SERIES_CONNECTOR_RE = re.compile(r"\s*(?:[:\-–—|/\\])\s*")
_SERIES_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_SERIES_JOIN_PHRASES = {
    ("bat", "man"): "batman",
    ("super", "man"): "superman",
    ("spider", "man"): "spiderman",
    ("iron", "man"): "ironman",
    ("wonder", "woman"): "wonderwoman",
}
_SERIES_CANONICAL_TOKENS = {
    "&": "and",
    "+": "and",
}


def _series_title_part(name: str) -> str:
    """Return the likely series/title prefix before a subtitle separator."""
    # Keep colons/dashes as strong subtitle breaks but normalize connectors first.
    normalized = name.replace("&", " and ").replace("+", " and ")
    parts = [p.strip() for p in _SERIES_CONNECTOR_RE.split(normalized) if p.strip()]
    return parts[0] if parts else normalized


def _series_tokens(name: str) -> list[str]:
    """Return normalized tokens for same-series detection."""
    title = _series_title_part(name)
    raw = [t.lower() for t in _SERIES_TOKEN_RE.findall(title)]
    tokens: list[str] = []
    i = 0
    while i < len(raw):
        if i + 1 < len(raw) and (raw[i], raw[i + 1]) in _SERIES_JOIN_PHRASES:
            tokens.append(_SERIES_JOIN_PHRASES[(raw[i], raw[i + 1])])
            i += 2
            continue
        tokens.append(_SERIES_CANONICAL_TOKENS.get(raw[i], raw[i]))
        i += 1
    return tokens


def _tokens_match(a: str, b: str, threshold: float = 0.82) -> bool:
    if a == b:
        return True
    return SequenceMatcher(None, a, b).ratio() >= threshold


def _common_prefix_tokens(names: list[str]) -> list[str]:
    """Fuzzy common prefix across a group of directory names."""
    token_lists = [_series_tokens(name) for name in names]
    if not token_lists:
        return []

    common: list[str] = []
    for pos in range(min(len(t) for t in token_lists)):
        column = [tokens[pos] for tokens in token_lists]
        first = column[0]
        if all(_tokens_match(first, other) for other in column[1:]):
            # Prefer the most common spelling. Tie-breaker: longest token, then alpha.
            chosen = sorted(column, key=lambda t: (-column.count(t), -len(t), t))[0]
            common.append(chosen)
        else:
            break
    return common


def _display_series_name(tokens: list[str]) -> str:
    if not tokens:
        return "Possible Series"
    words = []
    for token in tokens:
        if token == "and":
            words.append("And")
        else:
            words.append(token[:1].upper() + token[1:])
    return clean_directory_name(" ".join(words)) or "Possible Series"


def _unique_dir(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(1, 1000):
        candidate = path.parent / f"{path.name} ({i})"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Unable to find unique destination for {path}")


def _group_possible_same_series_dirs(
    parent: Path,
    min_common_words: int,
    min_group_size: int,
) -> list[tuple[str, list[Path]]]:
    """Find sibling directories that appear to be different entries in one series.

    Unlike exact duplicate matching, this catches directories that share a likely
    series title but have different subtitles, e.g.:

        Batman And Superman - Fighting the Joker
        Batman & Superman - Battle Against Catwoman
        Bat man + Super man - Team Up Against Evil

    The return value is a list of (suggested_series_name, directories).
    """
    dirs = [
        d for d in parent.iterdir()
        if d.is_dir() and d.name != CHECK_FOLDER_NAME and not d.name.startswith(".")
    ]
    if len(dirs) < min_group_size:
        return []

    # Build graph where edges connect likely same-series dirs.
    neighbors: dict[Path, set[Path]] = {d: set() for d in dirs}
    for i, a in enumerate(dirs):
        for b in dirs[i + 1:]:
            common = _common_prefix_tokens([a.name, b.name])
            if len(common) >= min_common_words:
                neighbors[a].add(b)
                neighbors[b].add(a)

    groups: list[list[Path]] = []
    seen: set[Path] = set()
    for start in dirs:
        if start in seen:
            continue
        stack = [start]
        component: list[Path] = []
        seen.add(start)
        while stack:
            cur = stack.pop()
            component.append(cur)
            for nxt in neighbors[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        if len(component) >= min_group_size:
            groups.append(sorted(component, key=lambda p: p.name.lower()))

    results: list[tuple[str, list[Path]]] = []
    for group in groups:
        common = _common_prefix_tokens([g.name for g in group])
        if len(common) < min_common_words:
            continue
        results.append((_display_series_name(common), group))
    return results


def move_possible_same_series_to_check(
    parent: Path,
    dry_run: bool,
    min_common_words: int = 2,
    min_group_size: int = 2,
) -> MaintenanceStats:
    """Move likely same-series sibling folders into _Check/<Series Name>/.

    The original folders are preserved as subdirectories for manual review.
    """
    stats = MaintenanceStats()
    groups = _group_possible_same_series_dirs(
        parent=parent,
        min_common_words=min_common_words,
        min_group_size=min_group_size,
    )
    if not groups:
        return stats

    check_root = parent / CHECK_FOLDER_NAME
    for suggested_name, group in groups:
        group_dest = _unique_dir(check_root / suggested_name)
        log.info(
            "  POSSIBLE SERIES GROUP -> _Check/%s  (%d folder(s))",
            group_dest.name,
            len(group),
        )
        for folder in group:
            log.info("    candidate: %s", folder.name)

        if dry_run:
            for folder in group:
                log.info("    [DRY RUN] Would move: %s -> %s", folder.name, group_dest)
                stats.moved += 1
            stats.merged += 1
            continue

        group_dest.mkdir(parents=True, exist_ok=True)
        for folder in group:
            dest = _unique_dir(group_dest / folder.name)
            shutil.move(str(folder), str(dest))
            log.info("    Moved for review: %s -> %s", folder.name, dest)
            stats.moved += 1
        stats.merged += 1

    return stats



def find_uncensored_pairs(root: Path, dry_run: bool, move_which: str) -> MaintenanceStats:
    stats = MaintenanceStats()
    dirs = [d for d in root.iterdir() if d.is_dir() and d.name != CHECK_FOLDER_NAME]
    marked = [d for d in dirs if _MARKER_WORDS_RE.search(d.name)]
    normal = {normalise_series_key(d.name): d for d in dirs if not _MARKER_WORDS_RE.search(d.name)}
    check_dir = root / CHECK_FOLDER_NAME

    for uncensored in marked:
        match = normal.get(normalise_series_key(uncensored.name))
        if not match:
            log.info("  No counterpart for: %s", uncensored.name)
            continue

        to_move: list[Path]
        if move_which == "uncensored":
            to_move = [uncensored]
        elif move_which == "censored":
            to_move = [match]
        else:
            to_move = [uncensored, match]

        for src in to_move:
            dest = check_dir / src.name
            if dry_run:
                log.info("  [DRY RUN] Would move to _Check: %s", src.name)
                stats.moved += 1
            else:
                check_dir.mkdir(exist_ok=True)
                suffix = 1
                while dest.exists():
                    dest = check_dir / f"{src.name} ({suffix})"
                    suffix += 1
                shutil.move(str(src), str(dest))
                log.info("  Moved to _Check: %s", src.name)
                stats.moved += 1
    return stats


def run_organize_series(args: argparse.Namespace) -> int:
    stats = MaintenanceStats()
    planned_units = 0
    planned_parents_by_root: dict[Path, list[Path]] = {}
    for root_arg in args.paths:
        root = Path(root_arg)
        if not root.exists():
            planned_parents_by_root[root] = []
            planned_units += 1
            continue
        parents = [root]
        if args.recursive_parents:
            parents.extend(sorted({p.parent for p in root.rglob("*") if p.is_dir()}))
        unique_parents = sorted(set(parents))
        planned_parents_by_root[root] = unique_parents
        planned_units += len(unique_parents)
        if args.uncensored_check:
            planned_units += 1

    progress = ProgressReporter(planned_units, "groups")
    for root_arg in args.paths:
        root = Path(root_arg)
        log.info("Organize series root: %s", root)
        if not root.exists():
            log.error("Missing root: %s", root)
            stats.errors += 1
            progress.step()
            continue

        for parent in planned_parents_by_root.get(root, [root]):
            if args.merge_chapter_folders:
                stats.add(merge_chapter_folders(parent, args.dry_run, metadata_workers=args.workers))
            if args.match_series:
                stats.add(
                    find_series_matches(
                        parent,
                        args.report_threshold,
                        args.auto_threshold,
                        args.dry_run,
                        metadata_workers=args.workers,
                    )
                )
            if args.possible_series_check:
                stats.add(
                    move_possible_same_series_to_check(
                        parent=parent,
                        dry_run=args.dry_run,
                        min_common_words=args.series_common_words,
                        min_group_size=args.series_min_group_size,
                    )
                )
            progress.step()

        if args.uncensored_check:
            stats.add(find_uncensored_pairs(root, args.dry_run, args.move_which))
            progress.step()

    log.info("Organize complete: %s", stats)
    return 0


def read_comicinfo(cbz_path: Path) -> tuple[str | None, str | None]:
    try:
        with zipfile.ZipFile(cbz_path, "r") as zf:
            names = {n.lower(): n for n in zf.namelist()}
            key = next((k for k in names if os.path.basename(k).lower() == "comicinfo.xml"), None)
            if key:
                real = names[key]
                return real, zf.read(real).decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, OSError) as exc:
        log.error("  Cannot read ComicInfo from %s: %s", cbz_path.name, exc)
    return None, None


def write_comicinfo(cbz_path: Path, entry_name: str | None, xml: str, dry_run: bool) -> bool:
    if dry_run:
        log.info("  [DRY RUN] Would update ComicInfo: %s", cbz_path.name)
        return True

    tmp_path = cbz_path.with_suffix(".tmp.cbz")
    bak_path = cbz_path.with_suffix(".bak.cbz")
    try:
        entries: list[tuple[zipfile.ZipInfo, bytes]] = []
        with zipfile.ZipFile(cbz_path, "r") as zin:
            for info in zin.infolist():
                entries.append((info, zin.read(info.filename)))

        with zipfile.ZipFile(tmp_path, "w") as zout:
            wrote = False
            for info, data in entries:
                if entry_name and info.filename == entry_name:
                    zout.writestr(info, xml.encode("utf-8"))
                    wrote = True
                else:
                    zout.writestr(info, data, compress_type=info.compress_type)
            if not wrote:
                zout.writestr("ComicInfo.xml", xml.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)

        cbz_path.rename(bak_path)
        tmp_path.rename(cbz_path)
        bak_path.unlink(missing_ok=True)
        return True
    except Exception as exc:
        log.error("  Failed to write ComicInfo for %s: %s", cbz_path.name, exc)
        tmp_path.unlink(missing_ok=True)
        return False


def metadata_worker(cbz_path: Path, dry_run: bool) -> MaintenanceStats:
    stats = MaintenanceStats()
    parsed = parse_comic_name(cbz_path)
    entry, xml = read_comicinfo(cbz_path)
    if xml is None:
        xml = "<ComicInfo><Title></Title><Series></Series><Number></Number></ComicInfo>"
    new_xml, changed = update_comicinfo_xml(xml, parsed)
    if not changed:
        stats.skipped += 1
        return stats
    if write_comicinfo(cbz_path, entry, new_xml, dry_run):
        stats.updated_xml += 1
    else:
        stats.errors += 1
    return stats


def run_metadata(args: argparse.Namespace) -> int:
    cbz_files: list[Path] = []
    for root_arg in args.paths:
        root = Path(root_arg)
        cbz_files.extend(sorted(root.rglob("*.cbz")) if root.is_dir() else [root])

    progress = ProgressReporter(len(cbz_files), "files")
    stats = MaintenanceStats()
    if args.workers == 1:
        for cbz in cbz_files:
            stats.add(metadata_worker(cbz, args.dry_run))
            progress.step()
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = [ex.submit(metadata_worker, cbz, args.dry_run) for cbz in cbz_files]
            for fut in as_completed(futures):
                stats.add(fut.result())
                progress.step()

    log.info("Metadata complete: %s", stats)
    return 0


def run_all(args: argparse.Namespace) -> int:
    archive_args = argparse.Namespace(**vars(args))
    archive_args.strip_names = True
    archive_args.dedupe_archives = True
    archive_args.pack_loose_images = True
    archive_args.no_recursive = False

    organize_args = argparse.Namespace(**vars(args))
    organize_args.merge_chapter_folders = True
    organize_args.match_series = True
    organize_args.uncensored_check = False
    organize_args.possible_series_check = False
    organize_args.recursive_parents = False
    organize_args.report_threshold = args.report_threshold
    organize_args.auto_threshold = args.auto_threshold
    organize_args.series_common_words = 2
    organize_args.series_min_group_size = 2
    organize_args.move_which = "both"

    rc = run_archive_clean(archive_args)
    if rc != 0:
        return rc
    rc = run_organize_series(organize_args)
    if rc != 0:
        return rc
    return run_metadata(args)


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("paths", nargs="+", help="Files or folders to process")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="Worker threads")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Consolidated CBZ library maintenance tool.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("archive-clean", help="Clean duplicate archives, duplicate filename tokens, and loose image folders")
    add_common(p)
    p.add_argument("--no-recursive", action="store_true")
    p.add_argument("--strip-names", action="store_true", default=True)
    p.add_argument("--dedupe-archives", action="store_true", default=True)
    p.add_argument("--pack-loose-images", action="store_true", default=True)
    p.set_defaults(func=run_archive_clean)

    p = sub.add_parser("organize-series", help="Merge split folders, fuzzy-match series folders, and find uncensored pairs")
    add_common(p)
    p.add_argument("--merge-chapter-folders", action="store_true", default=True)
    p.add_argument("--match-series", action="store_true", default=True)
    p.add_argument("--uncensored-check", action="store_true")
    p.add_argument(
        "--possible-series-check",
        action="store_true",
        help="Move likely same-series sibling folders into _Check/<suggested series>/ for manual review",
    )
    p.add_argument("--recursive-parents", action="store_true")
    p.add_argument("--report-threshold", type=float, default=0.75)
    p.add_argument("--auto-threshold", type=float, default=0.87)
    p.add_argument("--series-common-words", type=int, default=2, help="Minimum fuzzy common prefix words for possible-series groups")
    p.add_argument("--series-min-group-size", type=int, default=2, help="Minimum directories required to create a possible-series review group")
    p.add_argument("--move-which", choices=["both", "uncensored", "censored"], default="both")
    p.set_defaults(func=run_organize_series)

    p = sub.add_parser("metadata", help="Repair ComicInfo metadata using cbz_core")
    add_common(p)
    p.set_defaults(func=run_metadata)

    p = sub.add_parser("all", help="Run archive-clean, organize-series, then metadata")
    add_common(p)
    p.add_argument("--report-threshold", type=float, default=0.75)
    p.add_argument("--auto-threshold", type=float, default=0.87)
    p.set_defaults(func=run_all)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    global log
    log = setup_logging(args.verbose)

    if args.workers < 1:
        parser.error("--workers must be >= 1")
    if hasattr(args, "series_common_words") and args.series_common_words < 1:
        parser.error("--series-common-words must be >= 1")
    if hasattr(args, "series_min_group_size") and args.series_min_group_size < 2:
        parser.error("--series-min-group-size must be >= 2")

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
