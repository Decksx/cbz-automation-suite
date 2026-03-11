"""
CBZ Compilation Resolver
Scans a single user-specified directory for series where a compilation archive
(e.g. "Batman Ch. 1-5.cbz") overlaps with individual chapter archives
(e.g. "Batman Ch. 1.cbz" through "Batman Ch. 5.cbz").

When ALL chapters covered by a compilation are present as individual archives:
  1. Compares total page counts — must match before any action is taken.
  2. For each page position, picks the higher-quality page:
       - PNG beats JPEG regardless of size
       - Otherwise, larger file size wins
  3. Rewrites the compilation with the best pages from either source.
  4. Moves the now-redundant individual archives to a configurable processed
     folder under a subfolder named after the series.

Usage:
    python cbz_compilation_resolver.py                  # prompts for directory
    python cbz_compilation_resolver.py "C:/path/Batman" # process one directory
    python cbz_compilation_resolver.py --dry-run        # preview, no changes
    python cbz_compilation_resolver.py "C:/path" --dry-run

All cases where individual archives are incomplete (some chapters missing) are
reported but never acted on automatically.
"""

import os
import re
import sys
import shutil
import zipfile
import logging
from pathlib import Path
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler as _RotatingFileHandler

# ─────────────────────────────────────────────
# CONFIGURATION — edit these as needed
# ─────────────────────────────────────────────
LOG_FILE          = r"C:\ComicAutomation\cbz_compilation_resolver.log"
PROCESSED_FOLDER  = r"C:\ComicAutomation\Processed"   # individual archives moved here

# Image file extensions treated as comic pages
IMAGE_EXTENSIONS  = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".tif"}

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
log = logging.getLogger("cbz_resolver")
log.setLevel(logging.DEBUG)

_fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                          datefmt="%Y-%m-%d %H:%M:%S")

_fh = _RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_fh.setFormatter(_fmt)
log.addHandler(_fh)

_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
log.addHandler(_sh)

# ─────────────────────────────────────────────
# REGEX — chapter number extraction
# ─────────────────────────────────────────────
_CHAPTER_NUMBER_RE = re.compile(
    r'(?:'
    r'ch(?:ap(?:ter)?)?p?\.?\s*(\d[\d.]*)'   # ch/chap/chapter/chp + number
    r'|issue\s*(\d[\d.]*)'                    # issue + number
    r'|ep(?:isode)?\.?\s*(\d[\d.]*)'         # episode/ep + number
    r'|#\s*(\d[\d.]*)'                        # # + number
    r')',
    re.IGNORECASE
)

# Compilation range: "ch 1-5", "ch. 3-7", "chapter 1-12", etc.
_COMPILATION_RE = re.compile(
    r'ch(?:ap(?:ter)?)?p?\.?\s*(\d[\d.]*)\s*[-–—]\s*(\d[\d.]*)',
    re.IGNORECASE
)

# ─────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────
@dataclass
class PageInfo:
    """Metadata for a single page within a CBZ archive."""
    archive:   Path
    entry:     str        # zip entry name
    size:      int        # uncompressed size in bytes
    ext:       str        # lowercase extension e.g. ".png"
    compress_type: int    # original zipfile compression constant


@dataclass
class ChapterArchive:
    """A single-chapter CBZ with its parsed chapter number."""
    path:    Path
    number:  float
    pages:   list[PageInfo] = field(default_factory=list)


@dataclass
class CompilationArchive:
    """A compilation CBZ covering a range of chapters."""
    path:       Path
    start:      float
    end:        float
    pages:      list[PageInfo] = field(default_factory=list)

    @property
    def chapter_range(self) -> list[float]:
        """Integer chapter numbers in the range [start, end]."""
        nums = []
        n = self.start
        while n <= self.end + 0.001:
            nums.append(round(n, 1))
            n = round(n + 1, 1)
        return nums


@dataclass
class OverlapGroup:
    """A compilation paired with the individual chapters that cover it."""
    compilation:  CompilationArchive
    individuals:  list[ChapterArchive]   # sorted by chapter number
    missing:      list[float]            # chapter numbers not found as individuals


# ─────────────────────────────────────────────
# CHAPTER / PAGE EXTRACTION
# ─────────────────────────────────────────────
def _fmt_num(n: float) -> str:
    """Format a float chapter number as a clean string (1.0 -> '1', 1.5 -> '1.5')."""
    return str(int(n)) if n == int(n) else str(n)
def extract_chapter_number(stem: str) -> str | None:
    """
    Extract the chapter/issue number from a filename stem.
    Requires an explicit keyword (ch, chapter, issue, ep, episode, #) to avoid
    misidentifying title digits as chapter numbers.
    Returns a string like "12" or "12.5", or None if not found.
    """
    m = _CHAPTER_NUMBER_RE.search(stem)
    if m:
        val = next(g for g in m.groups() if g is not None)
        n = float(val)
        return str(int(n)) if n == int(n) else str(n)
    return None
def extract_compilation_range(stem: str) -> tuple[float, float] | None:
    """Return (start, end) chapter floats if stem contains a range, else None."""
    m = _COMPILATION_RE.search(stem)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None


def _is_image(entry_name: str) -> bool:
    return Path(entry_name).suffix.lower() in IMAGE_EXTENSIONS


def read_pages(cbz_path: Path) -> list[PageInfo] | None:
    """
    Open a CBZ and return a list of PageInfo for every image entry,
    sorted by entry name (natural page order).
    Returns None on read error.
    """
    try:
        pages = []
        with zipfile.ZipFile(cbz_path, "r") as zf:
            for info in sorted(zf.infolist(), key=lambda i: i.filename):
                if not _is_image(info.filename):
                    continue
                data = zf.read(info.filename)
                pages.append(PageInfo(
                    archive      = cbz_path,
                    entry        = info.filename,
                    size         = len(data),
                    ext          = Path(info.filename).suffix.lower(),
                    compress_type= info.compress_type,
                ))
        return pages
    except (zipfile.BadZipFile, OSError) as e:
        log.error(f"  Cannot read {cbz_path.name}: {e}")
        return None


# ─────────────────────────────────────────────
# PAGE QUALITY COMPARISON
# ─────────────────────────────────────────────
def _page_wins(candidate: PageInfo, current: PageInfo) -> bool:
    """
    Return True if candidate is higher quality than current.
    Rules:
      1. PNG beats JPEG regardless of size
      2. Otherwise larger file size wins
    """
    c_is_png   = candidate.ext == ".png"
    cur_is_png = current.ext   == ".png"

    if c_is_png and not cur_is_png:
        return True   # PNG always beats non-PNG
    if cur_is_png and not c_is_png:
        return False  # current PNG beats candidate non-PNG
    return candidate.size > current.size  # same format → larger wins


# ─────────────────────────────────────────────
# DIRECTORY SCAN
# ─────────────────────────────────────────────
def scan_directory(series_dir: Path) -> list[OverlapGroup]:
    """
    Scan series_dir for CBZ files. Identify compilations and individual
    chapters, then pair each compilation with its individual chapter coverage.
    Returns a list of OverlapGroup — one per compilation found.
    """
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
                individuals.append(ChapterArchive(path=cbz, number=n))

    if not compilations:
        return []

    # Index individuals by chapter number
    ind_by_num: dict[float, ChapterArchive] = {c.number: c for c in individuals}

    groups: list[OverlapGroup] = []
    for comp in compilations:
        needed   = comp.chapter_range
        found    = [ind_by_num[n] for n in needed if n in ind_by_num]
        missing  = [n for n in needed if n not in ind_by_num]
        if found:   # only report if at least one individual overlaps
            groups.append(OverlapGroup(
                compilation = comp,
                individuals = sorted(found, key=lambda c: c.number),
                missing     = missing,
            ))

    return groups


# ─────────────────────────────────────────────
# PAGE-LEVEL RESOLUTION
# ─────────────────────────────────────────────
def resolve_pages(
    group: OverlapGroup,
) -> tuple[list[tuple[PageInfo, PageInfo | None]], bool]:
    """
    For a fully-covered overlap group, build a page-by-page resolution plan.

    Returns:
        (plan, any_upgrades)
        plan: list of (winner_page, loser_page_or_None)
              winner is the page to use; loser is the one being replaced (or None
              if the compilation page is already the winner)
        any_upgrades: True if at least one individual page beats the compilation

    Reads pages from disk here so we only do it when actually needed.
    """
    # Read compilation pages
    comp_pages = read_pages(group.compilation.path)
    if comp_pages is None:
        return [], False

    # Read each individual archive's pages in chapter order
    ind_pages: list[PageInfo] = []
    for ind in group.individuals:
        pages = read_pages(ind.path)
        if pages is None:
            return [], False
        ind_pages.extend(pages)

    if len(comp_pages) != len(ind_pages):
        return [], False   # page count mismatch — caller logs this

    plan: list[tuple[PageInfo, PageInfo | None]] = []
    any_upgrades = False

    for comp_page, ind_page in zip(comp_pages, ind_pages):
        if _page_wins(ind_page, comp_page):
            plan.append((ind_page, comp_page))
            any_upgrades = True
        else:
            plan.append((comp_page, None))

    return plan, any_upgrades


# ─────────────────────────────────────────────
# ZIP REWRITE
# ─────────────────────────────────────────────
def _rewrite_compilation(
    compilation: CompilationArchive,
    plan: list[tuple[PageInfo, PageInfo | None]],
) -> bool:
    """
    Rewrite the compilation CBZ using the pages specified in plan.
    Non-image entries (ComicInfo.xml, etc.) are preserved unchanged.
    Uses atomic tmp→bak→rename swap.
    Returns True on success.
    """
    tmp_path = compilation.path.with_suffix(".tmp.cbz")
    bak_path = compilation.path.with_suffix(".bak.cbz")

    # Build a set of compilation image entry names that are being replaced
    replaced_entries = {
        loser.entry
        for _, loser in plan
        if loser is not None and loser.archive == compilation.path
    }

    # Read all non-image entries from the compilation (ComicInfo.xml, etc.)
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
            # Write non-image entries first (preserves ComicInfo.xml, etc.)
            for info, data in non_image_entries:
                zout.writestr(info, data, compress_type=info.compress_type)

            # Write pages from the resolution plan
            for i, (winner, _) in enumerate(plan):
                # Derive a clean entry name: use the compilation's original
                # page naming scheme to keep sort order consistent
                # We'll use a zero-padded index so readers sort correctly
                ext      = winner.ext
                new_name = f"page_{i+1:04d}{ext}"

                # Read the winning page data from its source archive
                with zipfile.ZipFile(winner.archive, "r") as zsrc:
                    data = zsrc.read(winner.entry)

                zout.writestr(
                    new_name,
                    data,
                    compress_type=winner.compress_type,
                )

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


# ─────────────────────────────────────────────
# PROCESSED FOLDER MOVE
# ─────────────────────────────────────────────
def _move_to_processed(cbz_path: Path, series_name: str, dry_run: bool) -> None:
    """Move a CBZ to PROCESSED_FOLDER / series_name / filename."""
    dest_dir  = Path(PROCESSED_FOLDER) / series_name
    dest_path = dest_dir / cbz_path.name

    if dry_run:
        log.info(f"    [DRY RUN] Would move: '{cbz_path.name}' -> {dest_path}")
        return

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        if dest_path.exists():
            # Keep larger file on collision
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
# MAIN PROCESSING
# ─────────────────────────────────────────────
def process_directory(series_dir: Path, dry_run: bool = False) -> None:
    """
    Scan a single series directory and resolve any compilation/individual overlaps.
    """
    series_name = series_dir.name
    log.info("=" * 60)
    log.info(f"Processing: {series_name}")
    log.info(f"  Path: {series_dir}")

    if not series_dir.exists() or not series_dir.is_dir():
        log.error(f"  Directory not found: {series_dir}")
        return

    groups = scan_directory(series_dir)

    if not groups:
        log.info("  No compilation/individual overlaps found.")
        return

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

        # All chapters present — read pages and compare
        log.info(f"    Coverage : complete — comparing page quality...")

        plan, any_upgrades = resolve_pages(group)

        if not plan:
            # Page count mismatch or read error — check which
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

        # Count upgrades
        upgrades = sum(1 for _, loser in plan if loser is not None)
        total    = len(plan)
        log.info(
            f"    Pages    : {total} total, "
            f"{upgrades} would be upgraded from individual archives, "
            f"{total - upgrades} already best in compilation"
        )

        if not any_upgrades:
            log.info(
                f"    Result   : compilation pages are already equal or better — "
                f"moving individuals to processed, no rewrite needed."
            )
            if not dry_run:
                for ind in group.individuals:
                    _move_to_processed(ind.path, series_name, dry_run=False)
            else:
                for ind in group.individuals:
                    _move_to_processed(ind.path, series_name, dry_run=True)
            continue

        # Rewrite compilation with upgraded pages
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


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
def main() -> None:
    args    = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry_run = "--dry-run" in sys.argv

    log.info("=" * 60)
    log.info("CBZ Compilation Resolver")
    if dry_run:
        log.info("  Mode: DRY RUN — no files will be modified or moved")
    log.info("=" * 60)

    # Determine target directory
    if args:
        target = Path(args[0])
    else:
        print("\nCBZ Compilation Resolver")
        print("-" * 40)
        print("Enter the full path to the series directory to process.")
        print("This should be the folder containing the .cbz files")
        print("(e.g. C:\\Comics\\Batman  or  \\\\tower\\media\\comics\\Comix\\Batman)\n")
        raw = input("Directory: ").strip().strip('"').strip("'")
        if not raw:
            print("No directory entered. Exiting.")
            return
        target = Path(raw)

    if not target.exists() or not target.is_dir():
        print(f"\nERROR: Directory not found: {target}")
        log.error(f"Directory not found: {target}")
        return

    log.info(f"  Target   : {target}")
    log.info(f"  Processed: {PROCESSED_FOLDER}")
    log.info(f"  Log      : {LOG_FILE}")

    if dry_run:
        log.info("  (Dry-run mode — no files will be written or moved)")

    process_directory(target, dry_run=dry_run)

    log.info("\n" + "=" * 60)
    log.info("CBZ Compilation Resolver complete.")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
