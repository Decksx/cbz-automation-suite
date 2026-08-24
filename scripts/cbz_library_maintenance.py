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
import hashlib
import json
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
from datetime import datetime
from difflib import SequenceMatcher
from logging.handlers import RotatingFileHandler
from pathlib import Path

from comic_automation.archive.page_hashing import calculate_page_hashes
from scripts.cbz_routing import series_key


try:
    from scripts.cbz_core import (
        clean_directory_name,
        clean_filename,
        extract_chapter_number,
        extract_volume_number,
        is_generic_title,
        normalise_archive_key,
        parse_comic_name,
        repair_mojibake,
        series_base_name,
        update_comicinfo_xml,
    )
except ModuleNotFoundError:
    from cbz_core import (  # type: ignore[no-redef]
        clean_directory_name,
        clean_filename,
        extract_chapter_number,
        extract_volume_number,
        is_generic_title,
        normalise_archive_key,
        parse_comic_name,
        repair_mojibake,
        series_base_name,
        update_comicinfo_xml,
    )


REPO_ROOT              = Path(__file__).resolve().parents[1]
LOG_FILE               = REPO_ROOT / "Logs" / "cbz_library_maintenance.log"
SERIES_EXCLUSIONS_FILE = REPO_ROOT / "Logs" / "series_exclusions.json"
DEFAULT_WORKERS = min(8, os.cpu_count() or 4)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".tiff", ".tif"}
CHECK_FOLDER_NAME = "_Check"

# ─────────────────────────────────────────────
# ACTION PLAN RECORDER  (dry-run -> save -> replay without re-scanning)
# ─────────────────────────────────────────────
# When a plan is "open", the mutation primitives record the concrete file
# operations they *would* perform during a dry run, instead of only logging
# them. The plan can then be saved to JSON and replayed by the 'apply-plan'
# subcommand, so a confirmed dry run is executed without re-scanning the library.
_PLAN: list[dict] | None = None
_PLAN_LOCK = threading.Lock()


def plan_open() -> None:
    """Begin recording actions into a fresh in-memory plan."""
    global _PLAN
    _PLAN = []


def plan_is_open() -> bool:
    return _PLAN is not None


def plan_record(op: str, **fields) -> None:
    """Append one action to the active plan (no-op when no plan is open)."""
    if _PLAN is None:
        return
    entry = {"op": op}
    for k, v in fields.items():
        entry[k] = str(v) if isinstance(v, Path) else v
    with _PLAN_LOCK:
        _PLAN.append(entry)


def plan_save(path: Path, meta: dict | None = None) -> int:
    """Write the active plan to *path* as JSON. Returns the number of actions."""
    actions = _PLAN or []
    data = {
        "version": 1,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        **(meta or {}),
        "actions": actions,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        log.info("  Plan written: %s  (%d action(s))", path, len(actions))
    except OSError as e:
        log.error("  Could not write plan file %s: %s", path, e)
    return len(actions)

_DUP_LABEL_FRAG = r"(?:ver(?:sion)?|v|ch(?:ap(?:ter)?)?|episode|ep|vol(?:ume)?|part|pt)"
_DUP_LABEL_NUM_RE = re.compile(
    rf"({_DUP_LABEL_FRAG}\.?\s*\d+(?:\.\d+)?)\s+{_DUP_LABEL_FRAG}\.?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_DUP_BARE_NUM_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s+\1\b")
_SPACED_PUNCT_RE = re.compile(r"([!?.])(?: +\1)+")
_ASYM_HYPH_L_RE = re.compile(r"(\S) -(\S)")
_ASYM_HYPH_R_RE = re.compile(r"(\S)- (\S)")
_SPACES_RE = re.compile(r"\s+")
_MARKER_WORDS_RE = re.compile(r"\b(uncensored|decensored)\b", re.IGNORECASE)
_CHAPTER_TOKEN_RE = re.compile(r"(ch(?:ap(?:ter)?)?p?\.?\s*)(\d[\d.]*)", re.IGNORECASE)


def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configure a rotating file handler and a stderr stream handler; return the logger."""
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
    """Return every unique parent directory that contains at least one file under *root*."""
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
    """Sort key that orders numeric runs as integers so '10' sorts after '9'."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", path.name)]


def clean_duplicate_tokens(name: str) -> str:
    """Remove repeated label/number fragments from a filename stem.

    Handles three classes of duplication: labeled tokens (e.g. "Ch.5 Ch 5"),
    bare repeated numbers (e.g. "12 12"), and spaced repeated punctuation
    (e.g. "!! !"). Also normalises asymmetric hyphens to "word-word" form.
    """
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


# The canonical series identity, not a local copy. This module had its own
# implementation until #44, agreeing with the other two only by coincidence:
# nothing compared them, and each carried its own regex constants. Grouping
# and merge decisions here must use the same rule the router and the index
# use, or maintenance merges two directories the router considers distinct.
#
# _MARKER_WORDS_RE and _SPACES_RE are deliberately kept: both are used
# independently elsewhere in this module for questions other than identity.
normalise_series_key = series_key


def fmt_number(value: float) -> str:
    """Format a float as an integer string when it is whole (e.g. 5.0 -> '5'), else as a float string."""
    return str(int(value)) if value == int(value) else str(value)


def extract_chapter_float(stem: str) -> float | None:
    """Parse a chapter number from a filename stem and return it as a float, or None if absent."""
    chapter = extract_chapter_number(stem)
    if chapter is None:
        return None
    try:
        return float(chapter)
    except ValueError:
        return None


def larger_file_wins(src: Path, dest: Path, dry_run: bool) -> str:
    """Move *src* to *dest*, keeping whichever file is larger on collision.

    Returns 'moved' (no collision), 'replaced' (src was larger, dest overwritten),
    or 'discarded' (dest was larger/equal, src removed).
    """
    if not dest.exists():
        if dry_run:
            log.info("    [DRY RUN] Would move: %s -> %s", src.name, dest)
            plan_record("file", src=src, dest=dest)
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
        else:
            plan_record("file", src=src, dest=dest)
        return "replaced"

    log.info("    Collision: existing larger/equal, discarding incoming: %s", src.name)
    if not dry_run:
        src.unlink()
    else:
        plan_record("file", src=src, dest=dest)
    return "discarded"


def rename_duplicate_tokens_in_dir(folder: Path, dry_run: bool) -> MaintenanceStats:
    """Apply clean_duplicate_tokens to every CBZ filename in *folder*, using larger_file_wins on collisions."""
    stats = MaintenanceStats()
    for cbz in sorted(folder.glob("*.cbz")):
        new_name = clean_duplicate_tokens(cbz.name)
        if new_name == cbz.name:
            continue
        dest = cbz.parent / new_name
        if dry_run:
            log.info("  [DRY RUN] Would rename: %s -> %s", cbz.name, new_name)
            plan_record("file", src=cbz, dest=dest)
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


def _normalise_meta_number(value: str) -> str:
    """Normalise a ComicInfo Number/Volume value so '5', '05' and '5.0' compare equal."""
    v = value.strip()
    if not v:
        return ""
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else str(f)
    except ValueError:
        return _SPACES_RE.sub(" ", v.lower())


def _comicinfo_dup_key(cbz_path: Path) -> str | None:
    """Return a duplicate key from a CBZ's ComicInfo.xml (Series + Volume + Number), or None.

    Identifies the same issue/chapter even when filenames differ completely. Returns
    None when the archive has no readable ComicInfo or lacks a Number — Series alone
    would wrongly group an entire run, so a per-issue discriminator is required.

    Title is intentionally not used: update_comicinfo_xml derives Title from the
    filename stem, so duplicate files with different names also have different Titles.
    """
    _, xml = read_comicinfo(cbz_path)
    if not xml:
        return None
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None

    def field(tag: str) -> str:
        el = root.find(tag)
        return (el.text or "").strip() if el is not None else ""

    number = _normalise_meta_number(field("Number"))
    if not number:
        return None
    series = normalise_series_key(field("Series"))
    volume = _normalise_meta_number(field("Volume"))

    # Corroborate with the chapter number parsed from the *filename*. Some sources
    # populate ComicInfo <Number> inconsistently within one folder — e.g. a file
    # named "Chapter 88 Ep. 1" gets Number=1 (the episode), colliding with a
    # genuinely different "Chapter 1" file that also has Number=1. Two files are
    # only the same chapter if their filenames agree on the chapter number too, so
    # fold it in. When a filename has no parseable chapter we fall back to the
    # metadata number alone (the original behaviour) rather than guessing.
    fname_chapter = extract_chapter_number(cbz_path.stem)
    if fname_chapter is not None:
        fname_norm = _normalise_meta_number(str(fname_chapter))
        if fname_norm and fname_norm != number:
            # Filename disagrees with metadata Number — use the filename chapter as
            # the authoritative discriminator so different chapters never collide.
            return f"{series}|v{volume}|c{fname_norm}"
    return f"{series}|v{volume}|n{number}"


def _archive_content_fingerprint(cbz_path: Path) -> str | None:
    """Return the archive's canonical ordered-page digest, or None if unreadable.

    Delegates to `comic_automation.archive.page_hashing.calculate_page_hashes`,
    the same implementation that produces `archive_content_signatures.digest`,
    rather than computing a second opinion here.

    That reuse is the point. An earlier revision hand-rolled its own digest and
    diverged from the canonical one in two ways review caught: it sorted entry
    names lexicographically instead of by natural key, and it hashed every
    non-ComicInfo entry instead of only image extensions. Both change page
    order or page membership, so two archives the rest of the system considers
    different could produce the same maintenance fingerprint -- for instance by
    re-zero-padding page names and redistributing content, since lexicographic
    order puts "10.jpg" before "2.jpg" and natural order does not.

    A deletion guard that disagrees with the system's own definition of page
    order is not a guard, so there is now one definition and this is a caller
    of it.

    Returns None when the archive cannot be read, is not a CBZ, or contains no
    image pages at all, so the caller isolates it rather than treating it as
    matching other archives in the same state.

    The zero-page case is not a corner: `calculate_page_hashes` hashes an empty
    page sequence to one fixed digest, so every image-less archive shares it.
    Accepting that digest would group two archives that have no page evidence
    whatsoever and delete one of them -- deletion justified by the *absence* of
    evidence, which is the same defect the guard exists to prevent.
    `content_duplicate_audit` excludes `page_count = 0` for the same reason.
    """
    try:
        result = calculate_page_hashes(cbz_path)
    except (ValueError, zipfile.BadZipFile, OSError, RuntimeError) as error:
        log.debug("Content fingerprint failed for %s: %s", cbz_path, error)
        return None
    if not result.pages:
        log.debug("No image pages to fingerprint in %s", cbz_path)
        return None
    return result.content_digest


def _split_group_by_content(group: list[Path]) -> list[list[Path]]:
    """Partition *group* into subgroups whose page content is byte-identical.

    ComicInfo metadata is written by upstream sources and is frequently wrong:
    distinct chapters routinely share a Series/Volume/Number triple, and on
    2026-08-17 a production run deleted 1,922 archives on that basis of which
    only 68 were genuine duplicates. Metadata may therefore *propose* a group,
    but content decides it.

    Archives whose fingerprint cannot be read are returned as singletons, so an
    unreadable file is never deleted as somebody else's duplicate.
    """
    by_content: dict[str, list[Path]] = {}
    unreadable: list[list[Path]] = []
    for path in group:
        fingerprint = _archive_content_fingerprint(path)
        if fingerprint is None:
            unreadable.append([path])
        else:
            by_content.setdefault(fingerprint, []).append(path)
    return list(by_content.values()) + unreadable


def _resolve_dup_group(
    folder: Path,
    group: list[Path],
    dry_run: bool,
    stats: MaintenanceStats,
    by: str,
) -> Path:
    """Keep the largest CBZ (CBZ preferred over CBR) in *group*, delete the rest; return the keeper."""
    cbz_files = [p for p in group if p.suffix.lower() == ".cbz"]
    pool = cbz_files or group
    keep = sorted(pool, key=lambda p: (-p.stat().st_size, p.name.lower()))[0]

    log.info("  Duplicate group [%s] by %s, keep: %s", folder.name, by, keep.name)
    for dup in group:
        if dup == keep:
            continue
        if keep.suffix.lower() == ".cbz" and dup.suffix.lower() == ".cbr":
            reason = "cbr superseded by cbz"
        else:
            reason = f"{by} duplicate"
        if dry_run:
            log.info("    [DRY RUN] Would delete (%s): %s", reason, dup.name)
            plan_record("delete", path=dup, reason=reason)
        else:
            dup.unlink()
            log.info("    Deleted (%s): %s", reason, dup.name)
        stats.deleted += 1
    return keep


def dedupe_archives_in_dir(folder: Path, dry_run: bool, use_metadata: bool = True) -> MaintenanceStats:
    """Delete duplicate CBZ/CBR files in *folder*, keeping the largest CBZ in each duplicate set.

    Two passes:
      1. Filename: files whose normalise_archive_key matches (insensitive to spacing
         and punctuation, e.g. "Ch.1" vs "Ch. 1"). Like pass 2, a matching name
         only proposes a group; identical page content decides it.
      2. Metadata (when *use_metadata*): among the pass-1 survivors, CBZ files whose
         ComicInfo.xml resolves to the same Series + Volume + Number — catches the
         same chapter saved under completely different filenames. Metadata only
         *proposes* a group; members are deleted only when their page content is
         identical, because upstream ComicInfo routinely collides across genuinely
         different chapters.
    """
    stats = MaintenanceStats()
    files = [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in {".cbz", ".cbr"}]
    if len(files) < 2:
        return stats

    # Pass 1 — filename-key duplicates.
    by_key: dict[str, list[Path]] = {}
    for f in files:
        by_key.setdefault(normalise_archive_key(f.stem), []).append(f)

    survivors: list[Path] = []
    for group in by_key.values():
        if len(group) < 2:
            survivors.extend(group)
            continue
        # The filename key is tighter than the metadata key, but "tighter" is
        # not a safety property: two files whose names normalise alike can hold
        # different chapters. Deletion is irreversible, so this pass requires
        # the same content proof the metadata pass does.
        for subgroup in _split_group_by_content(group):
            if len(subgroup) >= 2:
                survivors.append(
                    _resolve_dup_group(
                        folder, subgroup, dry_run, stats, by="name+content"
                    )
                )
                continue
            survivors.extend(subgroup)
            stats.skipped += 1
            log.info(
                "  Kept (filename matched but content differs) [%s]: %s",
                folder.name,
                subgroup[0].name,
            )

    # Pass 2 — ComicInfo metadata duplicates among survivors.
    if use_metadata and len(survivors) > 1:
        by_meta: dict[str, list[Path]] = {}
        for f in survivors:
            if f.suffix.lower() != ".cbz":
                continue  # CBR (RAR) cannot be opened as a zip to read ComicInfo
            key = _comicinfo_dup_key(f)
            if key is not None:
                by_meta.setdefault(key, []).append(f)
        for group in by_meta.values():
            if len(group) < 2:
                continue
            # Metadata proposed these as the same chapter; require identical
            # page content before any of them is deleted.
            for subgroup in _split_group_by_content(group):
                if len(subgroup) >= 2:
                    _resolve_dup_group(
                        folder, subgroup, dry_run, stats, by="metadata+content"
                    )
                    continue
                stats.skipped += 1
                log.info(
                    "  Kept (metadata matched but content differs) [%s]: %s",
                    folder.name,
                    subgroup[0].name,
                )

    return stats


def is_packable_image_folder(folder: Path) -> bool:
    """Return True if *folder* contains only image files (and optionally ComicInfo.xml) with no subdirectories."""
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
    """Zip a flat image folder into a CBZ archive beside it, then delete the source folder.

    Writes to a .tmp.cbz first, then atomically renames. If a same-named CBZ already
    exists, only replaces it when the newly packed archive is strictly larger.
    """
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
        plan_record("pack", folder=folder, dest=cbz_path)
        stats.packed += 1
        return stats

    tmp_path = cbz_path.with_suffix(".tmp.cbz")
    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_STORED) as zf:
            for item in packable:
                zf.write(item, arcname=item.name)

        if cbz_path.exists():
            # Capture the exact stat the replace decision is made from, then
            # re-verify it immediately before unlinking. Without this, a
            # writer that replaces cbz_path between the size comparison and
            # the unlink would have its file deleted on the strength of a
            # measurement that no longer describes it. The window is narrow
            # (the two calls are adjacent), so this is a smaller gap than the
            # read-rebuild window guarded in write_comicinfo() above -- but it
            # is the same class of unchecked-staleness bug and is closed the
            # same way. See docs/archive_io_resource_audit.md, "Small,
            # low-risk improvements".
            #
            # NOTE: this does not make the replacement atomic. The
            # unlink-then-rename below still leaves a window in which no file
            # exists at cbz_path. Collapsing that into a single atomic
            # os.replace() is deliberately NOT done here: the audit classes it
            # as a correctness-sensitive change to the mutation path that must
            # be validated against SMB rename semantics (not just local NTFS)
            # before rollout, unlike this check.
            existing_stat = cbz_path.stat()
            if tmp_path.stat().st_size > existing_stat.st_size:
                current_stat = cbz_path.stat()
                if (
                    current_stat.st_size != existing_stat.st_size
                    or current_stat.st_mtime_ns != existing_stat.st_mtime_ns
                ):
                    raise OSError(
                        f"{cbz_path.name} changed on disk while packing a "
                        "replacement; abandoning the replace."
                    )
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
    """Run the enabled archive-clean passes (strip names, dedupe) for a single folder; safe to call from a thread.

    A transient network/filesystem error on one folder (e.g. an SMB share blip,
    WinError 59) is logged and counted as an error, never propagated — so one bad
    folder cannot abort the entire run.
    """
    stats = MaintenanceStats()
    log.info("Archive clean: %s", folder)
    try:
        if args.strip_names:
            stats.add(rename_duplicate_tokens_in_dir(folder, args.dry_run))
        if args.dedupe_archives:
            stats.add(
                dedupe_archives_in_dir(
                    folder, args.dry_run, use_metadata=getattr(args, "metadata_dedupe", True)
                )
            )
    except OSError as exc:
        log.error("  Skipping folder (filesystem/network error): %s — %s", folder, exc)
        stats.errors += 1
    return stats


def run_archive_clean(args: argparse.Namespace) -> int:
    """Entry point for the 'archive-clean' subcommand.

    Discovers all file-containing directories under the given paths, dispatches
    archive_clean_worker in a thread pool, then packs loose image folders
    (deepest-first to avoid parent/child races).
    """
    if args.dry_run and getattr(args, "plan_out", None):
        plan_open()
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
                try:
                    stats.add(fut.result())
                except OSError as exc:
                    log.error("  Worker filesystem/network error (skipped): %s", exc)
                    stats.errors += 1
                progress.step()

    if args.pack_loose_images:
        # Pack folders after duplicate archive cleanup to avoid parent/child races.
        for folder in pack_folders:
            try:
                stats.add(pack_image_folder(folder, args.dry_run))
            except OSError as exc:
                log.error("  Skipping pack (filesystem/network error): %s — %s", folder, exc)
                stats.errors += 1
            progress.step()

    log.info("Archive clean complete: %s", stats)
    if args.dry_run and getattr(args, "plan_out", None):
        plan_save(Path(args.plan_out), {"source": "archive-clean", "paths": [str(p) for p in args.paths]})
    return 0


def canonical_series_name(names: list[str]) -> str:
    """Pick the best display name from a group of related directory names.

    Prefers base names (trailing tokens stripped), then breaks ties by uppercase
    letter count (proxy for proper capitalisation) and length.
    """
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
            plan_record("file", src=cbz, dest=dest)
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


def _find_archive_collision(target_dir: Path, stem: str) -> Path | None:
    """Return an existing archive in *target_dir* whose normalised key matches *stem*.

    Lets the merge treat files that differ only by spacing or punctuation as the
    same book (e.g. "Series Ch.1" and "Series Ch. 1"), so one copy is kept rather
    than both landing side-by-side. Returns None when there is no such file.
    """
    if not target_dir.is_dir():
        return None
    key = normalise_archive_key(stem)
    for existing in target_dir.iterdir():
        if existing.is_file() and existing.suffix.lower() in {".cbz", ".cbr"}:
            if normalise_archive_key(existing.stem) == key:
                return existing
    return None


def merge_dir_contents(src: Path, dest: Path, dry_run: bool) -> MaintenanceStats:
    """Recursively move every file from *src* into *dest*, applying larger_file_wins on collisions, then delete *src*."""
    stats = MaintenanceStats()
    for item in sorted(src.rglob("*")):
        if not item.is_file():
            continue
        rel = item.relative_to(src)
        target = dest / rel
        # When the exact name is free, still treat a file that normalises to the
        # same key (differs only by spacing/punctuation) as a collision so the
        # two cosmetic variants don't both survive the merge.
        if not target.exists() and item.suffix.lower() in {".cbz", ".cbr"}:
            match = _find_archive_collision(target.parent, item.stem)
            if match is not None:
                target = match
        outcome = larger_file_wins(item, target, dry_run)
        if outcome in {"moved", "replaced"}:
            stats.moved += 1
        elif outcome == "discarded":
            stats.deleted += 1

    if not dry_run:
        shutil.rmtree(src, ignore_errors=True)
    else:
        plan_record("rmtree", path=src)
    stats.merged += 1
    return stats


def merge_series_dir(src: Path, dest: Path, dry_run: bool) -> MaintenanceStats:
    """Rename any generic-titled files in *src*, then merge the entire directory into *dest*."""
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
    """Replace the chapter number in *stem* with a range token (e.g. 'Ch.12' -> 'Ch.1-2')."""
    range_str = f"{fmt_number(start)}-{fmt_number(end)}"

    def replace_chapter(match: re.Match) -> str:
        return match.group(1) + range_str

    new_stem, count = _CHAPTER_TOKEN_RE.subn(replace_chapter, stem, count=1)
    return new_stem if count else f"{stem} {range_str}"


def update_comicinfo_range(xml: str, start: float, end: float) -> tuple[str, bool]:
    """Set the <Number> tag in a ComicInfo XML string to a chapter range; returns (new_xml, changed)."""
    range_str = f"{fmt_number(start)}-{fmt_number(end)}"
    pattern = re.compile(r"<Number>.*?</Number>", re.IGNORECASE | re.DOTALL)
    tag = f"<Number>{range_str}</Number>"
    if pattern.search(xml):
        if pattern.search(xml).group(0) == tag:
            return xml, False
        return pattern.sub(tag, xml, count=1), True
    return xml.replace("</ComicInfo>", f"  {tag}\n</ComicInfo>"), True


def patch_comicinfo_range(cbz_path: Path, start: float, end: float, dry_run: bool) -> MaintenanceStats:
    """Read the ComicInfo.xml from *cbz_path*, update the Number to a range, and write it back."""
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
    """Find chapter numbers that look like concatenated ranges and rename/patch them.

    Calls detect_compilation_candidates on the chapter numbers present in *series_dir*,
    then for each suspect renames the file stem to the range form and updates ComicInfo.
    """
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
    """Group sibling directories that share a series base name and merge each group into a single canonical folder.

    Groups are keyed by normalise_series_key(series_base_name(dir)). After merging,
    ComicInfo is updated and compilation numbers are fixed for the combined folder.
    """
    stats = MaintenanceStats()
    children = [d for d in parent.iterdir() if d.is_dir() and d.name != CHECK_FOLDER_NAME]

    # Folders that carry a trailing chapter/volume/number token are grouped by
    # their stripped base name. Folders that are ALREADY a bare series title
    # (series_base_name -> None) are remembered separately so a numbered folder
    # can be folded into the existing canonical series even when it is the only
    # numbered sibling — previously a lone "Berserk 4" formed a size-1 group,
    # was skipped, and survived as its own directory next to "Berserk".
    groups: dict[str, list[Path]] = {}
    bare_by_key: dict[str, Path] = {}
    for d in children:
        base = series_base_name(d.name)
        if base:
            groups.setdefault(normalise_series_key(base), []).append(d)
        else:
            bare_by_key.setdefault(normalise_series_key(clean_directory_name(d.name)), d)

    for key, group in groups.items():
        bare = bare_by_key.get(key)
        members = list(group)
        if bare is not None and bare not in members:
            members.append(bare)
        if len(members) < 2:
            continue

        # Prefer the existing bare-named folder as the canonical destination so
        # files land in "Berserk", not "Berserk 4". Fall back to the best name
        # derived from the numbered folders when no bare folder exists.
        if bare is not None:
            canonical = bare.name
        else:
            canonical = canonical_series_name([g.name for g in group])
        dest = parent / canonical
        log.info("  Merge chapter-folder group -> %s", dest.name)
        if dry_run:
            log.info("    [DRY RUN] Would create/use canonical folder: %s", dest)
            plan_record("mkdir", path=dest)
        else:
            dest.mkdir(exist_ok=True)

        for src in members:
            if src == dest:
                continue
            stats.add(merge_series_dir(src, dest, dry_run))
        # In a dry run the canonical folder may not exist yet; the post-merge
        # sweeps need a real directory to read, so skip them when it is absent.
        if dest.exists():
            stats.add(dedupe_archives_in_dir(dest, dry_run))
            stats.add(update_series_metadata(dest, dry_run, workers=metadata_workers))
            stats.add(detect_and_fix_compilations(dest, dry_run))
    return stats


def find_series_matches(
    parent: Path,
    report_threshold: float,
    auto_threshold: float,
    dry_run: bool,
    metadata_workers: int = 1,
    interactive: bool = False,
    exclusions: set[frozenset] | None = None,
) -> MaintenanceStats:
    """Fuzzy-match sibling directories and merge likely duplicates.

    Every pair of directories in *parent* is compared with SequenceMatcher on their
    normalised names. Pairs above *report_threshold* are logged; pairs above
    *auto_threshold* are auto-merged (or prompted when interactive=True). The
    secondary folder (fewer files, shorter name) is merged into the primary.
    """
    stats = MaintenanceStats()

    if interactive and not sys.stdin.isatty():
        log.warning(
            "  --interactive requires a terminal (stdin is not a tty); "
            "proceeding without prompts."
        )
        interactive = False

    _excl = exclusions or set()

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
            if is_excluded_pair(a.name, b.name, _excl):
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

            if interactive:
                try:
                    sys.stdout.write(
                        f"  [{ratio:.3f}] Merge '{secondary.name}' into '{primary.name}'? "
                        "[y]es / [n]o / e[x]clude permanently: "
                    )
                    sys.stdout.flush()
                    answer = sys.stdin.readline().strip().lower()
                except EOFError:
                    answer = "n"

                if answer in ("x", "exclude"):
                    added = record_series_exclusion([a.name, b.name], _excl, dry_run)
                    if dry_run:
                        log.info("    [DRY RUN] Would record %d pair(s) as excluded.", added)
                    else:
                        log.info("    Recorded %d pair(s) as excluded.", added)
                    continue
                elif answer not in ("y", "yes"):
                    log.info("    Skipped.")
                    stats.skipped += 1
                    continue
                log.info("    Merging: %s -> %s", secondary.name, primary.name)
                stats.add(merge_series_dir(secondary, primary, dry_run))
                stats.add(dedupe_archives_in_dir(primary, dry_run))
                stats.add(update_series_metadata(primary, dry_run, workers=metadata_workers))
                stats.add(detect_and_fix_compilations(primary, dry_run))
                consumed.add(secondary)
            elif ratio >= auto_threshold:
                log.info("    Auto-merge: %s -> %s", secondary.name, primary.name)
                stats.add(merge_series_dir(secondary, primary, dry_run))
                stats.add(dedupe_archives_in_dir(primary, dry_run))
                stats.add(update_series_metadata(primary, dry_run, workers=metadata_workers))
                stats.add(detect_and_fix_compilations(primary, dry_run))
                consumed.add(secondary)
            else:
                stats.skipped += 1
    return stats




# ─────────────────────────────────────────────
# POSSIBLE SAME-SERIES GROUPING FOR MANUAL REVIEW
# ─────────────────────────────────────────────
_SERIES_CONNECTOR_RE = re.compile(r"\s+[-]\s+|\s*[/\\|:–—]\s*")
_SERIES_TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)*", re.IGNORECASE)
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
_SERIES_STOPWORDS = frozenset({"the", "a", "an"})


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
        token = _SERIES_CANONICAL_TOKENS.get(raw[i], raw[i])
        if token not in _SERIES_STOPWORDS:
            tokens.append(token)
        i += 1
    return tokens


def _tokens_match(a: str, b: str, threshold: float = 0.80) -> bool:
    """Return True if two tokens are identical or fuzzy-similar above *threshold*."""
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
    """Convert a list of normalised tokens into a title-cased display name suitable for a directory."""
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
    """Return *path* if it does not exist, otherwise append ' (N)' until a free name is found."""
    if not path.exists():
        return path
    for i in range(1, 1000):
        candidate = path.parent / f"{path.name} ({i})"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Unable to find unique destination for {path}")


# ─────────────────────────────────────────────
# SERIES EXCLUSION LOG  (persists "not related" decisions across runs)
# ─────────────────────────────────────────────
def _exclusion_key(name: str) -> str:
    """Normalise a directory name to the canonical form used as a key in the exclusions set."""
    return normalise_series_key(name)


def load_series_exclusions() -> set[frozenset]:
    """Load all excluded pairs from the JSON log. Returns a set of frozensets."""
    try:
        with open(SERIES_EXCLUSIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {frozenset(pair) for pair in data if len(pair) == 2}
    except FileNotFoundError:
        return set()
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("series_exclusions.json is malformed, ignoring: %s", e)
        return set()


def _save_series_exclusions(exclusions: set[frozenset]) -> None:
    """Overwrite the exclusion log with the current in-memory set."""
    try:
        Path(SERIES_EXCLUSIONS_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(SERIES_EXCLUSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump([sorted(p) for p in exclusions], f, indent=2)
    except OSError as e:
        log.warning("Could not save series exclusions: %s", e)


def record_series_exclusion(
    names: list[str],
    exclusions: set[frozenset],
    dry_run: bool,
) -> int:
    """Add all pairwise combinations of *names* to *exclusions* and persist.

    Returns the number of new pairs added.
    """
    added = 0
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            pair = frozenset([_exclusion_key(a), _exclusion_key(b)])
            if pair not in exclusions:
                exclusions.add(pair)
                added += 1
    if not dry_run and added:
        _save_series_exclusions(exclusions)
    return added


def is_excluded_pair(name_a: str, name_b: str, exclusions: set[frozenset]) -> bool:
    """Return True if the normalised pair (name_a, name_b) has been marked as permanently unrelated."""
    return frozenset([_exclusion_key(name_a), _exclusion_key(name_b)]) in exclusions


def _group_possible_same_series_dirs(
    parent: Path,
    min_common_words: int,
    min_group_size: int,
    exclusions: set[frozenset] | None = None,
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

    _excl = exclusions or set()

    # Build graph where edges connect likely same-series dirs.
    neighbors: dict[Path, set[Path]] = {d: set() for d in dirs}
    for i, a in enumerate(dirs):
        for b in dirs[i + 1:]:
            if is_excluded_pair(a.name, b.name, _excl):
                continue
            # If the only difference is a censorship marker (uncensored/decensored),
            # treat them as confirmed siblings regardless of the token threshold.
            if (
                normalise_series_key(a.name) == normalise_series_key(b.name)
                and (_MARKER_WORDS_RE.search(a.name) or _MARKER_WORDS_RE.search(b.name))
            ):
                neighbors[a].add(b)
                neighbors[b].add(a)
                continue
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
        if not common:
            continue
        results.append((_display_series_name(common), group))
    return results


def move_possible_same_series_to_check(
    parent: Path,
    dry_run: bool,
    min_common_words: int = 1,
    min_group_size: int = 2,
    interactive: bool = False,
    exclusions: set[frozenset] | None = None,
    check_root: Path | None = None,
) -> MaintenanceStats:
    """Move likely same-series sibling folders into _Check/<Series Name>/.

    The original folders are preserved as subdirectories for manual review.

    interactive=True prompts for each group before acting:
      [y] move to _Check   [n] skip this run   [x] exclude permanently
    Requires a real terminal (stdin tty); falls back to auto-move if not.
    """
    stats = MaintenanceStats()

    if exclusions is None:
        exclusions = set()

    if interactive and not sys.stdin.isatty():
        log.warning(
            "  --interactive requires a terminal (stdin is not a tty); "
            "proceeding without prompts."
        )
        interactive = False

    groups = _group_possible_same_series_dirs(
        parent=parent,
        min_common_words=min_common_words,
        min_group_size=min_group_size,
        exclusions=exclusions,
    )
    if not groups:
        return stats

    check_root = (check_root or parent) / CHECK_FOLDER_NAME
    for suggested_name, group in groups:
        group_dest = _unique_dir(check_root / suggested_name)
        log.info(
            "  POSSIBLE SERIES GROUP -> _Check/%s  (%d folder(s))",
            group_dest.name,
            len(group),
        )
        for folder in group:
            log.info("    candidate: %s", folder.name)

        if interactive:
            try:
                sys.stdout.write(
                    f"  Move to _Check/{group_dest.name}? "
                    "[y]es / [n]o / e[x]clude permanently: "
                )
                sys.stdout.flush()
                answer = sys.stdin.readline().strip().lower()
            except EOFError:
                answer = "n"

            if answer in ("x", "exclude"):
                names = [f.name for f in group]
                added = record_series_exclusion(names, exclusions, dry_run)
                if dry_run:
                    log.info(
                        "    [DRY RUN] Would record %d pair(s) as permanently excluded.",
                        added,
                    )
                else:
                    log.info("    Recorded %d pair(s) as permanently excluded.", added)
                continue
            elif answer not in ("y", "yes"):
                log.info("    Skipped.")
                continue

        if dry_run:
            plan_record("mkdir", path=group_dest)
            for folder in group:
                log.info("    [DRY RUN] Would move: %s -> %s", folder.name, group_dest)
                plan_record("movedir", src=folder, dest=group_dest / folder.name)
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
    """Find directories marked 'uncensored'/'decensored' that have a matching normal counterpart.

    *move_which* controls what gets moved to _Check: 'uncensored', 'censored', or 'both'.
    Matching is done by normalise_series_key after stripping the marker words.
    """
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


def run_clear_exclusions(args: argparse.Namespace) -> int:
    """List and optionally purge the series exclusions log."""
    exclusions = load_series_exclusions()

    if not exclusions:
        log.info("Series exclusions log is empty (%s)", SERIES_EXCLUSIONS_FILE)
        return 0

    # Apply optional substring filter.
    filter_str = getattr(args, "filter", None)
    if filter_str:
        keep = set()
        remove = set()
        for pair in exclusions:
            names = sorted(pair)
            if any(filter_str.lower() in n.lower() for n in names):
                remove.add(pair)
            else:
                keep.add(pair)
    else:
        keep = set()
        remove = exclusions

    log.info("Series exclusions log: %s", SERIES_EXCLUSIONS_FILE)
    log.info("  Total pairs : %d", len(exclusions))
    log.info("  To remove   : %d", len(remove))
    log.info("  To keep     : %d", len(keep))

    if not remove:
        log.info("  Nothing to remove.")
        return 0

    if args.dry_run:
        log.info("  [DRY RUN] Would remove %d pair(s):", len(remove))
        for pair in sorted(remove, key=lambda p: sorted(p)):
            a, b = sorted(pair)
            log.info("    - %s  <->  %s", a, b)
        return 0

    log.info("  Removing %d pair(s):", len(remove))
    for pair in sorted(remove, key=lambda p: sorted(p)):
        a, b = sorted(pair)
        log.info("    - %s  <->  %s", a, b)

    if keep:
        _save_series_exclusions(keep)
        log.info("  Saved %d remaining pair(s).", len(keep))
    else:
        try:
            Path(SERIES_EXCLUSIONS_FILE).unlink(missing_ok=True)
            log.info("  Exclusions file deleted (no pairs remain).")
        except OSError as e:
            log.error("  Could not delete exclusions file: %s", e)
            return 1

    return 0


def run_organize_series(args: argparse.Namespace) -> int:
    """Entry point for the 'organize-series' subcommand.

    Iterates each root path, optionally walking recursive parent directories, and
    runs the enabled organisation passes: merge_chapter_folders, find_series_matches,
    move_possible_same_series_to_check, find_uncensored_pairs, then a per-folder
    duplicate-archive sweep (dedupe_archives_in_dir) over every series folder.
    """
    dedupe_archives = getattr(args, "dedupe_archives", True)
    metadata_dedupe = getattr(args, "metadata_dedupe", True)
    if args.dry_run and getattr(args, "plan_out", None):
        plan_open()
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
        if dedupe_archives:
            planned_units += 1

    exclusions: set[frozenset] = load_series_exclusions()

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
                        interactive=getattr(args, "interactive", False),
                        exclusions=exclusions,
                    )
                )
            if args.possible_series_check:
                stats.add(
                    move_possible_same_series_to_check(
                        parent=parent,
                        dry_run=args.dry_run,
                        min_common_words=args.series_common_words,
                        min_group_size=args.series_min_group_size,
                        interactive=getattr(args, "interactive", False),
                        exclusions=exclusions,
                        check_root=root / CHECK_FOLDER_NAME,
                    )
                )
            progress.step()

        if args.uncensored_check:
            stats.add(find_uncensored_pairs(root, args.dry_run, args.move_which))
            progress.step()

        # Sweep every series folder under the root so cosmetic duplicates
        # (e.g. "Ch.1" vs "Ch. 1") are collapsed even in folders no merge touched.
        if dedupe_archives:
            for series_dir in iter_series_dirs(root):
                stats.add(
                    dedupe_archives_in_dir(series_dir, args.dry_run, use_metadata=metadata_dedupe)
                )
            progress.step()

    log.info("Organize complete: %s", stats)
    if args.dry_run and getattr(args, "plan_out", None):
        plan_save(Path(args.plan_out), {"source": "organize-series", "paths": [str(p) for p in args.paths]})
    return 0


def read_comicinfo(cbz_path: Path) -> tuple[str | None, str | None]:
    """Read ComicInfo.xml from a CBZ archive; returns (entry_name, xml_text) or (None, None) on failure."""
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


def _central_directory_fingerprint(zf: zipfile.ZipFile) -> tuple:
    """Per-entry (name, CRC32, uncompressed size) from an open archive.

    Taken from the central directory, which zipfile has already parsed on
    open -- capturing it from an archive we are reading anyway is free.
    """
    return tuple((i.filename, i.CRC, i.file_size) for i in zf.infolist())


def _read_central_directory_fingerprint(path: Path) -> tuple:
    """Re-read just the archive's central directory.

    A tail-of-file read, not a second full pass: measured at 0.39 ms against
    a 173 ms full read of the same 200 MB archive.
    """
    with zipfile.ZipFile(path, "r") as zf:
        return _central_directory_fingerprint(zf)


def write_comicinfo(cbz_path: Path, entry_name: str | None, xml: str, dry_run: bool) -> bool:
    """Rewrite a CBZ archive with an updated ComicInfo.xml, using a tmp+backup swap for atomicity.

    Reads all entries into memory, replaces (or appends) ComicInfo.xml with *xml*,
    writes a .tmp.cbz, then renames original -> .bak and tmp -> original, finally
    deleting the backup. Returns True on success.
    """
    if dry_run:
        log.info("  [DRY RUN] Would update ComicInfo: %s", cbz_path.name)
        return True

    tmp_path = cbz_path.with_suffix(".tmp.cbz")
    bak_path = cbz_path.with_suffix(".bak.cbz")
    try:
        # Snapshot size/mtime before reading, so a concurrent writer that
        # touches cbz_path during the read-rebuild window below can be
        # detected immediately before the destructive rename rather than
        # silently overwritten. Mirrors the before/after stat() pattern
        # already used by comic_automation/archive/{page_hashing,
        # perceptual_hashing}.py for the read-only hashing path, and now
        # by cbz_sanitizer._write_cbz_with_comicinfo(). See
        # docs/archive_io_resource_audit.md, "Small, low-risk improvements".
        before_stat = cbz_path.stat()
        entries: list[tuple[zipfile.ZipInfo, bytes]] = []
        before_fingerprint: tuple = ()
        with zipfile.ZipFile(cbz_path, "r") as zin:
            # Free: the central directory is already parsed for the
            # infolist() walk below.
            before_fingerprint = _central_directory_fingerprint(zin)
            for info in zin.infolist():
                # info.filename is round-tripped unchanged into the rewritten
                # archive below. Nothing here extracts to a real filesystem
                # path, so an unsafe ("..", absolute, or otherwise traversing)
                # member name is not exploitable in this function -- but it is
                # silently preserved into the output rather than rejected, and
                # would be inherited by any downstream tool that later performs
                # a naive extraction. See docs/archive_io_resource_audit.md,
                # "Confirmed risks in current code".
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

        # Re-check immediately before the destructive rename: if cbz_path
        # changed since before_stat was captured, another writer touched it
        # during the read-rebuild window above and replacing it now would
        # discard that writer's output undetected. Unlike cbz_sanitizer,
        # this module has no retry loop, so this raise is caught by the
        # broad "except Exception" below: the rewrite is abandoned, the
        # temp file is cleaned up, the original is left untouched, and the
        # caller counts it as an error.
        after_stat = cbz_path.stat()
        if (
            before_stat.st_size != after_stat.st_size
            or before_stat.st_mtime_ns != after_stat.st_mtime_ns
        ):
            raise OSError(
                f"{cbz_path.name} changed on disk while its ComicInfo.xml "
                "was being rewritten; abandoning the rewrite."
            )

        # Defence in depth for the case size/mtime structurally cannot see: a
        # replacement of identical length whose mtime lands in the same
        # filesystem timestamp bucket as the previous write. Content is what
        # changed, and per-entry CRC32 is the only recorded thing that
        # reflects it. This narrows the race window; it does not close it, and
        # it is not atomicity -- the rename still follows. Local volumes only:
        # over SMB the client caches file data independently of attributes and
        # this check is blind too. See docs/archive_io_resource_audit.md,
        # "Validated 2026-08-02".
        #
        # Deliberately NOT applied to pack_image_folder(): its window is two
        # adjacent stat() calls with no read-rebuild between them, and the
        # target there is an arbitrary existing file that need not be a
        # readable archive at all.
        if _read_central_directory_fingerprint(cbz_path) != before_fingerprint:
            raise OSError(
                f"{cbz_path.name} contents changed on disk while its "
                "ComicInfo.xml was being rewritten; abandoning the rewrite."
            )

        bak_path.unlink(missing_ok=True)  # remove any stale backup before renaming
        cbz_path.rename(bak_path)

        # Between these two renames there is no file at cbz_path at all. If
        # the second one fails the original is sitting at bak_path under a
        # name nothing else knows about: the database, the library scan and
        # every other tool look for the archive at cbz_path, and the
        # watcher's startup cleanup deletes *.bak.cbz outright -- so bytes
        # left parked there are one restart away from being gone for good.
        # Put the original back before letting the failure propagate.
        try:
            tmp_path.rename(cbz_path)
        except Exception:
            try:
                bak_path.rename(cbz_path)
            except Exception:
                # Both renames failed, so the archive is not at its recorded
                # path and this function cannot put it there. Say exactly
                # where the two copies are and stop: the handler below must
                # not delete the rebuilt one, because with the original
                # stranded it may be the only intact archive left.
                log.critical(
                    "  %s: rewrite failed AND the original could not be "
                    "restored. Original bytes are at %s, rebuilt copy at "
                    "%s. Neither will be deleted. Recover by hand before "
                    "running any tool that cleans up .bak.cbz files.",
                    cbz_path.name, bak_path, tmp_path,
                )
            raise

        bak_path.unlink(missing_ok=True)
        return True
    except Exception as exc:
        log.error("  Failed to write ComicInfo for %s: %s", cbz_path.name, exc)
        # Only discard the rebuilt copy once the original is genuinely back
        # at its recorded path. If cbz_path is missing, tmp_path may be the
        # last intact archive and deleting it turns a failed rewrite into
        # data loss -- which is the whole failure this guard exists for.
        if cbz_path.exists():
            tmp_path.unlink(missing_ok=True)
        else:
            log.critical(
                "  %s: not present at its recorded path after a failed "
                "rewrite; keeping %s rather than deleting it.",
                cbz_path.name, tmp_path,
            )
        return False


def metadata_worker(cbz_path: Path, dry_run: bool) -> MaintenanceStats:
    """Parse a CBZ filename, merge the result into its ComicInfo.xml, and write the update back; safe to call from a thread."""
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


def rename_worker(cbz_path: Path, dry_run: bool) -> MaintenanceStats:
    """Rename a single CBZ file to the normalised name produced by cbz_core; safe to call from a thread."""
    stats = MaintenanceStats()
    try:
        parsed = parse_comic_name(cbz_path)
        new_name = parsed.filename
        if new_name == cbz_path.name:
            stats.skipped += 1
            return stats
        dest = cbz_path.parent / new_name
        if dry_run:
            log.info("  [DRY RUN] Would rename: %s -> %s", cbz_path.name, new_name)
            stats.renamed += 1
            return stats
        if dest.exists():
            outcome = larger_file_wins(cbz_path, dest, dry_run=False)
            if outcome in {"moved", "replaced"}:
                stats.renamed += 1
            else:
                stats.deleted += 1
        else:
            cbz_path.rename(dest)
            log.info("  Renamed: %s -> %s", cbz_path.name, new_name)
            stats.renamed += 1
    except Exception as exc:
        log.error("  Error processing %s: %s", cbz_path.name, exc)
        stats.errors += 1
    return stats


def run_rename(args: argparse.Namespace) -> int:
    """Entry point for the 'rename' subcommand; discovers CBZ files and dispatches rename_worker in a thread pool."""
    cbz_files: list[Path] = []
    for root_arg in args.paths:
        root = Path(root_arg)
        cbz_files.extend(sorted(root.rglob("*.cbz")) if root.is_dir() else [root])

    progress = ProgressReporter(len(cbz_files), "files")
    stats = MaintenanceStats()
    if args.workers == 1:
        for cbz in cbz_files:
            stats.add(rename_worker(cbz, args.dry_run))
            progress.step()
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = [ex.submit(rename_worker, cbz, args.dry_run) for cbz in cbz_files]
            for fut in as_completed(futures):
                stats.add(fut.result())
                progress.step()

    log.info("Rename complete: %s", stats)
    return 0


def run_metadata(args: argparse.Namespace) -> int:
    """Entry point for the 'metadata' subcommand; discovers CBZ files and dispatches metadata_worker in a thread pool."""
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
    """Entry point for the 'all' subcommand; runs archive-clean, organize-series, then metadata in sequence."""
    # When recording a dry-run plan, open it once here and let every sub-pass
    # record into the same plan, so 'all' produces a single combined plan file
    # rather than each pass overwriting the last.
    combined_plan = args.dry_run and getattr(args, "plan_out", None)
    if combined_plan:
        plan_open()

    archive_args = argparse.Namespace(**vars(args))
    archive_args.strip_names = True
    archive_args.dedupe_archives = True
    archive_args.metadata_dedupe = True
    archive_args.pack_loose_images = True
    archive_args.no_recursive = False
    archive_args.plan_out = None  # plan is managed at the run_all level

    organize_args = argparse.Namespace(**vars(args))
    organize_args.merge_chapter_folders = True
    organize_args.match_series = True
    organize_args.dedupe_archives = True
    organize_args.metadata_dedupe = True
    organize_args.uncensored_check = False
    organize_args.possible_series_check = False
    organize_args.recursive_parents = False
    organize_args.report_threshold = args.report_threshold
    organize_args.auto_threshold = args.auto_threshold
    organize_args.series_common_words = 1
    organize_args.series_min_group_size = 2
    organize_args.move_which = "both"
    organize_args.plan_out = None  # plan is managed at the run_all level

    rc = run_archive_clean(archive_args)
    if rc != 0:
        return rc
    rc = run_organize_series(organize_args)
    if rc != 0:
        return rc
    rc = run_metadata(args)
    if combined_plan:
        plan_save(Path(args.plan_out), {"source": "all", "paths": [str(p) for p in args.paths]})
    return rc


# ─────────────────────────────────────────────
# PLAN REPLAY  (execute the actions captured during a dry run)
# ─────────────────────────────────────────────
def execute_plan(actions: list[dict], dry_run: bool = False) -> MaintenanceStats:
    """Replay a recorded action plan, re-checking the filesystem at each step.

    Actions are applied in recorded order. Every op re-validates current state so
    a plan stays safe even if the library changed slightly since the dry run:
      - file   : resolve src vs dest with larger_file_wins (move/replace/discard)
      - delete : unlink path if it still exists
      - rmtree : recursively remove a (now-merged) source directory
      - mkdir  : create a destination/series directory
      - movedir: move a whole directory, finding a unique name on collision
    """
    stats = MaintenanceStats()
    for action in actions:
        op = action.get("op")
        try:
            if op == "mkdir":
                dest = Path(action["path"])
                if dry_run:
                    log.info("  [DRY RUN] Would create: %s", dest)
                else:
                    dest.mkdir(parents=True, exist_ok=True)
            elif op == "file":
                src = Path(action["src"])
                dest = Path(action["dest"])
                if not src.exists():
                    log.info("  Skip (source gone): %s", src.name)
                    stats.skipped += 1
                    continue
                outcome = larger_file_wins(src, dest, dry_run=dry_run)
                if outcome in {"moved", "replaced"}:
                    stats.moved += 1
                else:
                    stats.deleted += 1
            elif op == "delete":
                path = Path(action["path"])
                if not path.exists():
                    stats.skipped += 1
                    continue
                if dry_run:
                    log.info("  [DRY RUN] Would delete: %s", path.name)
                else:
                    path.unlink()
                    log.info("  Deleted: %s", path.name)
                stats.deleted += 1
            elif op == "rmtree":
                path = Path(action["path"])
                if not path.exists():
                    stats.skipped += 1
                    continue
                if dry_run:
                    log.info("  [DRY RUN] Would remove tree: %s", path)
                else:
                    shutil.rmtree(path, ignore_errors=True)
                    log.info("  Removed: %s", path)
            elif op == "movedir":
                src = Path(action["src"])
                dest = Path(action["dest"])
                if not src.exists():
                    stats.skipped += 1
                    continue
                if dry_run:
                    log.info("  [DRY RUN] Would move dir: %s -> %s", src.name, dest)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    final = _unique_dir(dest)
                    shutil.move(str(src), str(final))
                    log.info("  Moved dir: %s -> %s", src.name, final)
                stats.moved += 1
            elif op == "pack":
                folder = Path(action["folder"])
                if not folder.exists():
                    stats.skipped += 1
                    continue
                # Re-run the real pack on the live folder; it re-checks packability
                # and the larger-wins rule against any existing archive.
                stats.add(pack_image_folder(folder, dry_run=dry_run))
            else:
                log.warning("  Unknown plan op '%s' — skipping.", op)
                stats.skipped += 1
        except (OSError, shutil.Error) as exc:
            log.error("  Plan action failed (%s): %s", op, exc)
            stats.errors += 1
    return stats


def _repair_xml_titles(xml_text: str) -> tuple[str, bool]:
    """Repair mojibake in <Title> and <Series> text of a ComicInfo XML string.

    Operates on the parsed tree so only element text is touched. Returns
    (new_xml, changed).
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return xml_text, False
    changed = False
    for tag in ("Title", "Series", "LocalizedSeries", "AlternateSeries"):
        for el in root.iter(tag):
            if el.text:
                fixed = repair_mojibake(el.text)
                if fixed != el.text:
                    el.text = fixed
                    changed = True
    if not changed:
        return xml_text, False
    return ET.tostring(root, encoding="unicode"), True


def run_repair_names(args: argparse.Namespace) -> int:
    """Entry point for 'repair-names'.

    Walks each path and repairs mojibake (non-ASCII written as literal UTF-8 byte
    hex, e.g. ``Playere28099s`` -> ``Player's``) in:
      - .cbz file names
      - directory names (deepest-first, so children are renamed before parents)
      - ComicInfo <Title>/<Series> metadata inside each archive (unless --names-only)

    Honours --dry-run. Renames skip when a repaired target already exists.
    """
    dry = args.dry_run
    names_only = getattr(args, "names_only", False)
    stats = MaintenanceStats()

    # Collect every file and directory under the targets up front so we can sort
    # directories deepest-first (rename children before their parents).
    files: list[Path] = []
    dirs: list[Path] = []
    for raw in args.paths:
        root = Path(raw)
        if not root.exists():
            log.warning("  Path not found, skipping: %s", root)
            continue
        for p in root.rglob("*"):
            if p.is_dir():
                dirs.append(p)
            elif p.suffix.lower() == ".cbz":
                files.append(p)
    dirs.sort(key=lambda p: len(p.parts), reverse=True)

    progress = ProgressReporter(len(files) + len(dirs), "items")

    # ── 1. Repair file names + (optionally) ComicInfo inside each archive ──
    for cbz in files:
        try:
            fixed_name = repair_mojibake(cbz.name)
            current = cbz
            if fixed_name != cbz.name:
                target = cbz.parent / fixed_name
                if target.exists():
                    log.warning("  Rename skipped (target exists): %s", fixed_name)
                else:
                    if dry:
                        log.info("  [DRY RUN] Would rename file: %s -> %s", cbz.name, fixed_name)
                    else:
                        cbz.rename(target)
                        log.info("  Renamed file: %s -> %s", cbz.name, fixed_name)
                        current = target
                    stats.renamed += 1

            if not names_only:
                entry, xml = read_comicinfo(current if not dry else cbz)
                if xml is not None:
                    new_xml, changed = _repair_xml_titles(xml)
                    if changed:
                        if dry:
                            log.info("  [DRY RUN] Would repair ComicInfo titles in: %s", current.name)
                        elif write_comicinfo(current, entry, new_xml, dry_run=False):
                            log.info("  Repaired ComicInfo titles in: %s", current.name)
                        stats.updated_xml += 1
        except Exception as exc:
            log.error("  Error repairing %s: %s", cbz.name, exc)
            stats.errors += 1
        finally:
            progress.step()

    # ── 2. Repair directory names (deepest-first) ──
    for d in dirs:
        try:
            if not d.exists():
                progress.step()
                continue
            fixed = repair_mojibake(d.name)
            if fixed != d.name:
                target = d.parent / fixed
                if target.exists():
                    log.warning("  Dir rename skipped (target exists): %s", fixed)
                else:
                    if dry:
                        log.info("  [DRY RUN] Would rename dir: %s -> %s", d.name, fixed)
                    else:
                        d.rename(target)
                        log.info("  Renamed dir: %s -> %s", d.name, fixed)
                    stats.moved += 1
        except Exception as exc:
            log.error("  Error repairing dir %s: %s", d.name, exc)
            stats.errors += 1
        finally:
            progress.step()

    log.info("Repair-names complete: %s", stats)
    return 0


def run_apply_plan(args: argparse.Namespace) -> int:
    """Entry point for the 'apply-plan' subcommand. Replays a saved dry-run plan."""
    plan_path = Path(args.plan)
    try:
        with open(plan_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        log.error("Plan file not found: %s", plan_path)
        return 1
    except (json.JSONDecodeError, OSError) as e:
        log.error("Could not read plan file %s: %s", plan_path, e)
        return 1

    actions = data.get("actions", [])
    log.info("Applying plan: %s", plan_path)
    log.info("  Generated : %s", data.get("generated", "unknown"))
    log.info("  Actions   : %d", len(actions))
    if args.dry_run:
        log.info("  Mode      : DRY RUN (no changes will be made)")

    progress = ProgressReporter(len(actions), "actions")
    stats = MaintenanceStats()
    # Apply serially: order matters (mkdir before the moves that depend on it).
    for action in actions:
        stats.add(execute_plan([action], dry_run=args.dry_run))
        progress.step()

    log.info("Apply-plan complete: %s", stats)
    return 0


# ─────────────────────────────────────────────
# SERIES PROPOSAL  (scan once, review in GUI, apply later)
# ─────────────────────────────────────────────
def find_fuzzy_series_pairs(
    parent: Path,
    report_threshold: float,
    exclusions: set[frozenset] | None = None,
) -> list[tuple[float, Path, Path]]:
    """Return sibling directory pairs whose normalised names are similar.

    This is the detection half of find_series_matches, factored out so it can feed
    the review-proposal workflow without performing any merges.
    """
    _excl = exclusions or set()
    dirs = [d for d in parent.iterdir() if d.is_dir() and d.name != CHECK_FOLDER_NAME]
    entries = [(d, normalise_series_key(d.name)) for d in dirs]
    pairs: list[tuple[float, Path, Path]] = []
    for i, (a, na) in enumerate(entries):
        if not na:
            continue
        for b, nb in entries[i + 1:]:
            if not nb or is_excluded_pair(a.name, b.name, _excl):
                continue
            if 2 * min(len(na), len(nb)) / (len(na) + len(nb)) < report_threshold:
                continue
            sm = SequenceMatcher(None, na, nb)
            if sm.quick_ratio() < report_threshold:
                continue
            ratio = sm.ratio()
            if ratio < report_threshold:
                continue
            pairs.append((ratio, a, b))
    return pairs


def _file_count(folder: Path) -> int:
    try:
        return len(list(folder.glob("*.cbz")))
    except OSError:
        return 0


def collect_series_proposals(
    parent: Path,
    report_threshold: float,
    min_common_words: int,
    min_group_size: int,
    exclusions: set[frozenset] | None = None,
    start_index: int = 1,
) -> list[dict]:
    """Build review-ready candidate groups for one parent directory.

    Combines two detectors:
      - subtitle/variant groups (_group_possible_same_series_dirs)
      - fuzzy name pairs (find_fuzzy_series_pairs)
    Fuzzy pairs already covered by a subtitle group are dropped, so each set of
    folders appears once. Returns a list of group dicts ready for JSON.
    """
    _excl = exclusions or set()
    groups: list[dict] = []
    covered: set[Path] = set()
    idx = start_index

    subtitle_groups = _group_possible_same_series_dirs(
        parent=parent,
        min_common_words=min_common_words,
        min_group_size=min_group_size,
        exclusions=_excl,
    )
    for suggested_name, members in subtitle_groups:
        groups.append({
            "id": f"g{idx:04d}",
            "kind": "subtitle-group",
            "score": None,
            "suggested_name": suggested_name,
            "parent": str(parent),
            "members": [
                {"name": m.name, "path": str(m), "file_count": _file_count(m)}
                for m in members
            ],
        })
        covered.update(members)
        idx += 1

    for ratio, a, b in find_fuzzy_series_pairs(parent, report_threshold, _excl):
        if a in covered or b in covered:
            continue
        members = sorted([a, b], key=lambda p: p.name.lower())
        suggested = canonical_series_name([m.name for m in members])
        groups.append({
            "id": f"g{idx:04d}",
            "kind": "fuzzy-pair",
            "score": round(ratio, 3),
            "suggested_name": suggested,
            "parent": str(parent),
            "members": [
                {"name": m.name, "path": str(m), "file_count": _file_count(m)}
                for m in members
            ],
        })
        covered.update(members)
        idx += 1

    return groups


def write_series_proposal(path: Path, root: Path, groups: list[dict]) -> None:
    """Write the candidate groups to *path* as a JSON proposal file."""
    data = {
        "version": 1,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "root": str(root),
        "group_count": len(groups),
        "groups": groups,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        log.info("Series proposal written: %s  (%d group(s))", path, len(groups))
    except OSError as e:
        log.error("Could not write proposal %s: %s", path, e)


def run_propose_series(args: argparse.Namespace) -> int:
    """Entry point for 'propose-series': scan and write a review file. Makes no changes."""
    exclusions = load_series_exclusions()
    all_groups: list[dict] = []
    for root_arg in args.paths:
        root = Path(root_arg)
        if not root.exists():
            log.error("Missing root: %s", root)
            continue
        parents = [root]
        if getattr(args, "recursive_parents", False):
            parents.extend(sorted({p.parent for p in root.rglob("*") if p.is_dir()}))
        for parent in sorted(set(parents)):
            all_groups.extend(
                collect_series_proposals(
                    parent=parent,
                    report_threshold=args.report_threshold,
                    min_common_words=args.series_common_words,
                    min_group_size=args.series_min_group_size,
                    exclusions=exclusions,
                    start_index=len(all_groups) + 1,
                )
            )

    out = Path(args.out)
    write_series_proposal(out, Path(args.paths[0]), all_groups)
    log.info("Found %d candidate group(s) for review.", len(all_groups))
    return 0


def apply_series_decisions(
    decisions: list[dict],
    dry_run: bool,
    metadata_workers: int = 1,
    exclusions: set[frozenset] | None = None,
) -> MaintenanceStats:
    """Apply reviewed series decisions.

    Each decision: {"verdict": "yes"|"no", "members": [paths], "target_name": str,
    "parent": str}. "yes" merges all members into <parent>/<target_name>; "no"
    records the members as a permanent exclusion so they are not re-proposed.
    """
    stats = MaintenanceStats()
    _excl = exclusions if exclusions is not None else load_series_exclusions()

    for decision in decisions:
        verdict = (decision.get("verdict") or "").strip().lower()
        members = [Path(p) for p in decision.get("members", [])]
        members = [m for m in members if m.exists()]
        if not members:
            continue

        if verdict == "no":
            added = record_series_exclusion([m.name for m in members], _excl, dry_run)
            log.info(
                "  %sExcluded %d pair(s) for: %s",
                "[DRY RUN] " if dry_run else "",
                added,
                ", ".join(m.name for m in members),
            )
            continue

        if verdict != "yes":
            continue  # undecided — leave untouched

        parent = Path(decision.get("parent") or members[0].parent)
        raw_target = (decision.get("target_name") or "").strip()
        target_name = clean_directory_name(raw_target) if raw_target else canonical_series_name(
            [m.name for m in members]
        )
        if not target_name:
            log.warning("  Skipping group with empty target name: %s", [m.name for m in members])
            stats.skipped += 1
            continue

        dest = parent / target_name
        log.info(
            "  %sMerge %d folder(s) -> %s",
            "[DRY RUN] " if dry_run else "",
            len(members),
            dest,
        )
        if dry_run:
            plan_record("mkdir", path=dest)
        else:
            dest.mkdir(parents=True, exist_ok=True)

        for src in members:
            if src == dest:
                continue
            stats.add(merge_series_dir(src, dest, dry_run))
        if dest.exists():
            stats.add(dedupe_archives_in_dir(dest, dry_run))
            stats.add(update_series_metadata(dest, dry_run, workers=metadata_workers))
            stats.add(detect_and_fix_compilations(dest, dry_run))

    return stats


def run_apply_series(args: argparse.Namespace) -> int:
    """Entry point for 'apply-series': read a decisions file and apply it."""
    dec_path = Path(args.decisions)
    try:
        with open(dec_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        log.error("Decisions file not found: %s", dec_path)
        return 1
    except (json.JSONDecodeError, OSError) as e:
        log.error("Could not read decisions file %s: %s", dec_path, e)
        return 1

    decisions = data.get("decisions", [])
    log.info("Applying series decisions: %s  (%d group(s))", dec_path, len(decisions))
    if args.dry_run:
        log.info("  Mode: DRY RUN (no changes will be made)")

    plan_out = getattr(args, "plan_out", None)
    if args.dry_run and plan_out:
        plan_open()

    stats = apply_series_decisions(
        decisions,
        dry_run=args.dry_run,
        metadata_workers=getattr(args, "workers", 1),
    )

    if args.dry_run and plan_out:
        plan_save(Path(plan_out), {"source": "apply-series", "decisions_file": str(dec_path)})

    log.info("Apply-series complete: %s", stats)
    return 0


def add_common(parser: argparse.ArgumentParser) -> None:
    """Add the shared CLI arguments (paths, --dry-run, --workers, --verbose) to a subcommand parser."""
    parser.add_argument("paths", nargs="+", help="Files or folders to process")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="Worker threads")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")
    parser.add_argument(
        "--plan-out",
        dest="plan_out",
        metavar="FILE",
        default=None,
        help="During a --dry-run, record the planned actions to FILE so they can later "
             "be executed with 'apply-plan' without re-scanning.",
    )


def build_parser() -> argparse.ArgumentParser:
    """Construct and return the top-level argument parser with all subcommands registered."""
    parser = argparse.ArgumentParser(description="Consolidated CBZ library maintenance tool.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("archive-clean", help="Clean duplicate archives, duplicate filename tokens, and loose image folders")
    add_common(p)
    p.add_argument("--no-recursive", action="store_true")
    p.add_argument("--strip-names", action="store_true", default=True)
    p.add_argument("--dedupe-archives", action="store_true", default=True)
    p.add_argument(
        "--metadata-dedupe",
        action="store_true",
        default=True,
        help="During dedupe, also match duplicates by ComicInfo.xml Series+Volume+Number (catches same chapter under different filenames)",
    )
    p.add_argument(
        "--no-metadata-dedupe",
        dest="metadata_dedupe",
        action="store_false",
        help="Dedupe by filename only; do not read ComicInfo.xml metadata",
    )
    p.add_argument("--pack-loose-images", action="store_true", default=True)
    p.set_defaults(func=run_archive_clean)

    p = sub.add_parser("organize-series", help="Merge split folders, fuzzy-match series folders, and find uncensored pairs")
    add_common(p)
    p.add_argument("--merge-chapter-folders", action="store_true", default=True)
    p.add_argument("--match-series", action="store_true", default=True)
    p.add_argument(
        "--dedupe-archives",
        action="store_true",
        default=True,
        help="After merging, delete duplicate archives in every series folder (treats files that differ only by spacing/punctuation as the same book)",
    )
    p.add_argument(
        "--no-dedupe-archives",
        dest="dedupe_archives",
        action="store_false",
        help="Skip the per-folder duplicate-archive sweep",
    )
    p.add_argument(
        "--metadata-dedupe",
        action="store_true",
        default=True,
        help="During dedupe, also match duplicates by ComicInfo.xml Series+Volume+Number (catches same chapter under different filenames)",
    )
    p.add_argument(
        "--no-metadata-dedupe",
        dest="metadata_dedupe",
        action="store_false",
        help="Dedupe by filename only; do not read ComicInfo.xml metadata",
    )
    p.add_argument("--uncensored-check", action="store_true")
    p.add_argument(
        "--possible-series-check",
        action="store_true",
        help="Move likely same-series sibling folders into _Check/<suggested series>/ for manual review",
    )
    p.add_argument("--recursive-parents", action="store_true")
    p.add_argument("--report-threshold", type=float, default=0.75)
    p.add_argument("--auto-threshold", type=float, default=0.87)
    p.add_argument("--series-common-words", type=int, default=1, help="Minimum fuzzy common prefix words for possible-series groups")
    p.add_argument("--series-min-group-size", type=int, default=2, help="Minimum directories required to create a possible-series review group")
    p.add_argument(
        "--interactive",
        action="store_true",
        help=(
            "Prompt before moving each possible-series group to _Check. "
            "Answers: [y]es / [n]o / e[x]clude permanently. "
            "Requires a real terminal; auto-skips prompts when stdin is not a tty."
        ),
    )
    p.add_argument("--move-which", choices=["both", "uncensored", "censored"], default="both")
    p.set_defaults(func=run_organize_series)

    p = sub.add_parser(
        "clear-exclusions",
        help="List or purge the series exclusions log",
    )
    p.add_argument("--dry-run", action="store_true", help="Preview what would be removed without changing the file")
    p.add_argument("--verbose", action="store_true", help="Verbose logging")
    p.add_argument(
        "--filter",
        metavar="TEXT",
        help="Only remove pairs where at least one name contains TEXT (case-insensitive). Omit to remove all.",
    )
    p.set_defaults(func=run_clear_exclusions)

    p = sub.add_parser("rename", help="Rename CBZ files using the cbz_core normalisation pipeline (strips hex suffixes, normalises chapter tokens, etc.)")
    add_common(p)
    p.set_defaults(func=run_rename)

    p = sub.add_parser(
        "repair-names",
        help="Repair mojibake (non-ASCII written as literal UTF-8 hex, e.g. Playere28099s -> Player's) "
             "in file names, folder names, and ComicInfo Title/Series metadata",
    )
    p.add_argument("paths", nargs="+", help="Files or folders to repair")
    p.add_argument("--dry-run", action="store_true", help="Preview changes without renaming or rewriting")
    p.add_argument("--names-only", dest="names_only", action="store_true",
                   help="Only repair file/folder names; do not rewrite ComicInfo metadata inside archives")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=run_repair_names)

    p = sub.add_parser("metadata", help="Repair ComicInfo metadata using cbz_core")
    add_common(p)
    p.set_defaults(func=run_metadata)

    p = sub.add_parser("all", help="Run archive-clean, organize-series, then metadata")
    add_common(p)
    p.add_argument("--report-threshold", type=float, default=0.75)
    p.add_argument("--auto-threshold", type=float, default=0.87)
    p.set_defaults(func=run_all)

    p = sub.add_parser(
        "propose-series",
        help="Scan for likely same-series folders and write a review file (makes no changes)",
    )
    p.add_argument("paths", nargs="+", help="Library folder(s) to scan")
    p.add_argument("--out", required=True, metavar="FILE", help="Where to write the proposal JSON")
    p.add_argument("--report-threshold", type=float, default=0.75)
    p.add_argument("--series-common-words", type=int, default=1)
    p.add_argument("--series-min-group-size", type=int, default=2)
    p.add_argument("--recursive-parents", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=run_propose_series)

    p = sub.add_parser(
        "apply-series",
        help="Apply a reviewed series decisions file (merge 'yes' groups, exclude 'no' groups)",
    )
    p.add_argument("decisions", help="Path to the decisions JSON written by the GUI review window")
    p.add_argument("--dry-run", action="store_true", help="Preview only")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    p.add_argument("--plan-out", dest="plan_out", metavar="FILE", default=None,
                   help="During --dry-run, record planned merges to FILE for later 'apply-plan'")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=run_apply_series)

    p = sub.add_parser(
        "apply-plan",
        help="Execute a plan file recorded during an earlier --dry-run (no re-scanning)",
    )
    p.add_argument("plan", help="Path to the plan JSON written via --plan-out")
    p.add_argument("--dry-run", action="store_true", help="Preview the replay without changing files")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=run_apply_plan)

    return parser


def main() -> int:
    """Parse CLI arguments, validate them, re-configure logging, and dispatch to the chosen subcommand."""
    parser = build_parser()
    args = parser.parse_args()

    global log
    log = setup_logging(args.verbose)

    if hasattr(args, "workers") and args.workers < 1:
        parser.error("--workers must be >= 1")
    if hasattr(args, "series_common_words") and args.series_common_words < 1:
        parser.error("--series-common-words must be >= 1")
    if hasattr(args, "series_min_group_size") and args.series_min_group_size < 2:
        parser.error("--series-min-group-size must be >= 2")

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
