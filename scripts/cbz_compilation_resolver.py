"""
cbz_compilation_resolver.py — CBZ Compilation Resolver (parallelised)

Changes in this version
────────────────────────
• --workers N  (default: min(8, cpu_count)).  Pass --workers 1 for serial.
• Each series directory is processed in parallel with ThreadPoolExecutor.
  process_directory() is the independent unit of work — it operates on a
  single directory and touches no shared state.
• Summary counters are aggregated from returned values.
"""

from __future__ import annotations

import os
import re
import sys
import shutil
import zipfile
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler as _RotatingFileHandler

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
LOG_FILE          = r"C:\git\ComicAutomation\cbz_compilation_resolver.log"
PROCESSED_FOLDER  = r"C:\git\ComicAutomation\Processed"
SCAN_FOLDERS: list[str] = [
    r"\\tower\media\comics\Comix",
    r"\\tower\media\comics\Manga",
]
IMAGE_EXTENSIONS  = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".tif"}
DEFAULT_WORKERS   = min(8, os.cpu_count() or 4)

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
log = logging.getLogger("cbz_resolver")
log.setLevel(logging.DEBUG)
_fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_fh  = _RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
_fh.setFormatter(_fmt)
log.addHandler(_fh)
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
log.addHandler(_sh)

# ─────────────────────────────────────────────
# REGEX
# ─────────────────────────────────────────────
_CHAPTER_NUMBER_RE = re.compile(
    r'(?:'
    r'ch(?:ap(?:ter)?)?p?\.?\s*(\d[\d.]*)'
    r'|issue\s*(\d[\d.]*)'
    r'|ep(?:isode)?\.?\s*(\d[\d.]*)'
    r'|#\s*(\d[\d.]*)'
    r')',
    re.IGNORECASE
)
_COMPILATION_RE = re.compile(
    r'ch(?:ap(?:ter)?)?p?\.?\s*(\d[\d.]*)\s*[-–—]\s*(\d[\d.]*)',
    re.IGNORECASE
)


# ─────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────
@dataclass
class PageInfo:
    archive:   Path
    entry:     str
    size:      int
    ext:       str
    compress_type: int

@dataclass
class ChapterArchive:
    path:    Path
    number:  float
    pages:   list[PageInfo] = field(default_factory=list)

@dataclass
class CompilationArchive:
    path:       Path
    start:      float
    end:        float
    pages:      list[PageInfo] = field(default_factory=list)

    @property
    def chapter_range(self) -> list[float]:
        nums = []
        n = self.start
        while n <= self.end + 0.001:
            nums.append(round(n, 1))
            n = round(n + 1, 1)
        return nums

@dataclass
class OverlapGroup:
    compilation:  CompilationArchive
    individuals:  list[ChapterArchive]
    missing:      list[float]


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def _fmt_num(n: float) -> str:
    return str(int(n)) if n == int(n) else str(n)

def extract_chapter_number(stem: str) -> str | None:
    m = _CHAPTER_NUMBER_RE.search(stem)
    if m:
        val = next(g for g in m.groups() if g is not None)
        n = float(val)
        return str(int(n)) if n == int(n) else str(n)
    return None

def extract_compilation_range(stem: str) -> tuple[float, float] | None:
    m = _COMPILATION_RE.search(stem)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None

def _is_image(entry_name: str) -> bool:
    return Path(entry_name).suffix.lower() in IMAGE_EXTENSIONS

def read_pages(cbz_path: Path) -> list[PageInfo] | None:
    try:
        pages = []
        with zipfile.ZipFile(cbz_path, "r") as zf:
            for info in sorted(zf.infolist(), key=lambda i: i.filename):
                if not _is_image(info.filename):
                    continue
                data = zf.read(info.filename)
                pages.append(PageInfo(
                    archive=cbz_path, entry=info.filename,
                    size=len(data), ext=Path(info.filename).suffix.lower(),
                    compress_type=info.compress_type,
                ))
        return pages
    except (zipfile.BadZipFile, OSError) as e:
        log.error(f"  Cannot read {cbz_path.name}: {e}")
        return None

def _page_wins(candidate: PageInfo, current: PageInfo) -> bool:
    c_is_png   = candidate.ext == ".png"
    cur_is_png = current.ext   == ".png"
    if c_is_png and not cur_is_png:
        return True
    if cur_is_png and not c_is_png:
        return False
    return candidate.size > current.size


# ─────────────────────────────────────────────
# SCAN / RESOLVE / REWRITE  (unchanged logic)
# ─────────────────────────────────────────────
def scan_directory(series_dir: Path) -> list[OverlapGroup]:
    cbz_files = sorted(series_dir.glob("*.cbz"))
    if not cbz_files:
        return []

    compilations: list[CompilationArchive] = []
    individuals:  list[ChapterArchive]     = []
    for cbz in cbz_files:
        r = extract_compilation_range(cbz.stem)
        if r:
            compilations.append(CompilationArchive(path=cbz, start=r[0], end=r[1]))
        else:
            n = extract_chapter_number(cbz.stem)
            if n is not None:
                individuals.append(ChapterArchive(path=cbz, number=float(n)))

    if not compilations:
        return []

    ind_by_num: dict[float, ChapterArchive] = {c.number: c for c in individuals}
    groups: list[OverlapGroup] = []
    for comp in compilations:
        needed  = comp.chapter_range
        found   = [ind_by_num[n] for n in needed if n in ind_by_num]
        missing = [n for n in needed if n not in ind_by_num]
        if found:
            groups.append(OverlapGroup(compilation=comp, individuals=sorted(found, key=lambda c: c.number), missing=missing))
    return groups

def resolve_pages(group: OverlapGroup) -> tuple[list[tuple[PageInfo, PageInfo | None]], bool]:
    comp_pages = read_pages(group.compilation.path)
    if comp_pages is None:
        return [], False
    ind_pages: list[PageInfo] = []
    for ind in group.individuals:
        pages = read_pages(ind.path)
        if pages is None:
            return [], False
        ind_pages.extend(pages)
    if len(comp_pages) != len(ind_pages):
        return [], False
    plan: list[tuple[PageInfo, PageInfo | None]] = []
    any_upgrades = False
    for comp_page, ind_page in zip(comp_pages, ind_pages):
        if _page_wins(ind_page, comp_page):
            plan.append((ind_page, comp_page))
            any_upgrades = True
        else:
            plan.append((comp_page, None))
    return plan, any_upgrades

def _rewrite_compilation(compilation: CompilationArchive, plan: list[tuple[PageInfo, PageInfo | None]]) -> bool:
    tmp_path = compilation.path.with_suffix(".tmp.cbz")
    bak_path = compilation.path.with_suffix(".bak.cbz")
    non_image_entries: list[tuple[zipfile.ZipInfo, bytes]] = []
    try:
        with zipfile.ZipFile(compilation.path, "r") as zin:
            for info in zin.infolist():
                if not _is_image(info.filename):
                    non_image_entries.append((info, zin.read(info.filename)))
    except (zipfile.BadZipFile, OSError) as e:
        log.error(f"  Cannot read compilation for rewrite: {e}")
        return False
    try:
        with zipfile.ZipFile(tmp_path, "w") as zout:
            for info, data in non_image_entries:
                zout.writestr(info, data, compress_type=info.compress_type)
            for i, (winner, _) in enumerate(plan):
                ext      = winner.ext
                new_name = f"page_{i+1:04d}{ext}"
                with zipfile.ZipFile(winner.archive, "r") as zsrc:
                    data = zsrc.read(winner.entry)
                zout.writestr(new_name, data, compress_type=winner.compress_type)
        compilation.path.rename(bak_path)
        tmp_path.rename(compilation.path)
        bak_path.unlink(missing_ok=True)
        return True
    except Exception as e:
        log.error(f"  Failed to rewrite compilation: {e}")
        if tmp_path.exists():
            tmp_path.unlink()
        if bak_path.exists():
            bak_path.rename(compilation.path)
        return False

def _move_to_processed(cbz_path: Path, series_name: str, dry_run: bool) -> None:
    dest_dir  = Path(PROCESSED_FOLDER) / series_name
    dest_path = dest_dir / cbz_path.name
    if dry_run:
        log.info(f"    [DRY RUN] Would move: '{cbz_path.name}' -> {dest_path}")
        return
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        if dest_path.exists():
            if cbz_path.stat().st_size > dest_path.stat().st_size:
                dest_path.unlink()
                shutil.move(str(cbz_path), str(dest_path))
                log.info(f"    Moved (replaced smaller): '{cbz_path.name}' -> {dest_path}")
            else:
                cbz_path.unlink()
                log.info(f"    Discarded (collision, kept larger in processed): '{cbz_path.name}'")
        else:
            shutil.move(str(cbz_path), str(dest_path))
            log.info(f"    Moved to processed: '{cbz_path.name}' -> {dest_path}")
    except OSError as e:
        log.error(f"    Failed to move '{cbz_path.name}': {e}")


# ─────────────────────────────────────────────
# PER-SERIES WORKER  (returns overlap count)
# ─────────────────────────────────────────────
def process_directory(series_dir: Path, dry_run: bool = False) -> int:
    """
    Scan and resolve one series directory.
    Returns number of overlap groups found (for summary).
    Safe to call from threads — operates entirely within series_dir.
    """
    series_name = series_dir.name
    log.info("=" * 60)
    log.info(f"Processing: {series_name}")
    log.info(f"  Path: {series_dir}")

    if not series_dir.exists() or not series_dir.is_dir():
        log.error(f"  Directory not found: {series_dir}")
        return 0

    groups = scan_directory(series_dir)
    if not groups:
        log.info("  No compilation/individual overlaps found.")
        return 0

    log.info(f"  Found {len(groups)} compilation(s) with overlapping individual chapters.")

    for group in groups:
        comp = group.compilation
        log.info(f"\n  Compilation: '{comp.path.name}'")
        log.info(f"    Range    : Ch.{_fmt_num(comp.start)} – Ch.{_fmt_num(comp.end)}"
                 f" ({len(comp.chapter_range)} chapters)")
        log.info(f"    Found    : {len(group.individuals)} individual archive(s)")

        if group.missing:
            missing_str = ", ".join(_fmt_num(n) for n in sorted(group.missing))
            log.info(f"    Missing  : Ch. {missing_str} — skipping (incomplete coverage)")
            continue

        log.info(f"    Coverage : complete — comparing page quality...")
        plan, any_upgrades = resolve_pages(group)

        if not plan:
            comp_pages = read_pages(comp.path)
            ind_pages: list = []
            for ind in group.individuals:
                p = read_pages(ind.path)
                if p:
                    ind_pages.extend(p)
            if comp_pages is not None:
                log.warning(
                    f"    Page count mismatch: compilation has {len(comp_pages)} pages, "
                    f"individuals have {len(ind_pages)} combined — skipping."
                )
            continue

        upgrades = sum(1 for _, loser in plan if loser is not None)
        total    = len(plan)
        log.info(
            f"    Pages    : {total} total, "
            f"{upgrades} would be upgraded from individual archives, "
            f"{total - upgrades} already best in compilation"
        )

        if not any_upgrades:
            log.info(f"    Result   : compilation pages already equal or better — moving individuals.")
            for ind in group.individuals:
                _move_to_processed(ind.path, series_name, dry_run=dry_run)
            continue

        log.info(f"    Action   : rewriting compilation with {upgrades} upgraded page(s)...")
        if dry_run:
            log.info(f"    [DRY RUN] Would rewrite: '{comp.path.name}'")
            for ind in group.individuals:
                _move_to_processed(ind.path, series_name, dry_run=True)
            continue

        success = _rewrite_compilation(comp, plan)
        if success:
            log.info(f"    Rewrite  : OK — '{comp.path.name}' updated.")
            for ind in group.individuals:
                _move_to_processed(ind.path, series_name, dry_run=False)
        else:
            log.error(f"    Rewrite  : FAILED — individual archives not moved.")

    return len(groups)


# ─────────────────────────────────────────────
# RECURSIVE SERIES DIR COLLECTION
# ─────────────────────────────────────────────
def _iter_series_dirs(root: Path) -> list[Path]:
    series: list[Path] = []
    try:
        entries = list(root.iterdir())
    except OSError as e:
        log.error(f"Cannot iterate '{root}': {e}")
        return series
    has_cbz = any(e.is_file() and e.suffix.lower() == ".cbz" for e in entries)
    if has_cbz:
        series.append(root)
    else:
        for entry in sorted(entries):
            if entry.is_dir():
                series.extend(_iter_series_dirs(entry))
    return series


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
def main() -> None:
    raw_args = sys.argv[1:]
    dry_run  = "--dry-run" in raw_args
    args     = [a for a in raw_args if a != "--dry-run"]

    workers = DEFAULT_WORKERS
    clean_args = []
    i = 0
    while i < len(args):
        arg = args[i]
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
            i += 1
        else:
            clean_args.append(arg)
        i += 1

    log.info("=" * 60)
    log.info("CBZ Compilation Resolver")
    if dry_run:
        log.info("  Mode: DRY RUN — no files will be modified or moved")
    log.info(f"  Workers  : {workers}")
    log.info("=" * 60)

    targets = [Path(a) for a in clean_args] if clean_args else [Path(f) for f in SCAN_FOLDERS]

    log.info(f"  Processed: {PROCESSED_FOLDER}")
    log.info(f"  Log      : {LOG_FILE}")
    log.info("=" * 60)

    total_dirs     = 0
    total_overlaps = 0

    for target in targets:
        if not target.exists() or not target.is_dir():
            log.error(f"Directory not found, skipping: {target}")
            continue

        log.info(f"\nScanning: {target}")
        series_dirs = _iter_series_dirs(target)
        log.info(
            f"  Found {len(series_dirs)} series director"
            f"{'y' if len(series_dirs) == 1 else 'ies'}.  Workers: {workers}."
        )
        total_dirs += len(series_dirs)

        if workers == 1:
            for sd in series_dirs:
                total_overlaps += process_directory(sd, dry_run=dry_run)
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(process_directory, sd, dry_run): sd
                    for sd in series_dirs
                }
                for future in as_completed(futures):
                    sd = futures[future]
                    try:
                        total_overlaps += future.result()
                    except Exception as e:
                        log.error(f"  Worker failed for '{sd.name}': {e}")

    log.info("\n" + "=" * 60)
    log.info("CBZ Compilation Resolver complete.")
    log.info(f"  Series directories checked : {total_dirs}")
    log.info(f"  Overlap groups found       : {total_overlaps}")
    if dry_run:
        log.info("  (Dry-run — no changes written)")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
