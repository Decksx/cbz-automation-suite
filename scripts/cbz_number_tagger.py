"""
CBZ Number Tagger
Scans a folder for .cbz files and sets the <Number> tag in each
ComicInfo.xml based on the chapter/issue number found in the filename.

Only updates files where a chapter keyword is present in the filename
(ch, chapter, chp, issue, #) to avoid misidentifying title digits.
Files with no detectable number are skipped.

Usage:
    python cbz_number_tagger.py                        # scans all SCAN_FOLDERS
    python cbz_number_tagger.py "C:/path/to/series"    # scan one folder
    python cbz_number_tagger.py --dry-run              # preview without writing
"""

import os
import re
import sys
import time
import zipfile
import logging
from logging.handlers import RotatingFileHandler as _RotatingFileHandler
from pathlib import Path

# ─────────────────────────────────────────────
# CONFIGURATION — edit these as needed
# ─────────────────────────────────────────────
SCAN_FOLDERS = [
    r"\\tower\media\comics\Comix",
    r"\\tower\media\comics\Manga",
]
LOG_FILE = r"C:\git\ComicAutomation\cbz_number_tagger.log"
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        _RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# NUMBER EXTRACTION
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

_VOLUME_NUMBER_RE = re.compile(
    r'(?:'
    r'vol(?:ume)?\.?\s*(\d[\d.]*)'           # vol/volume + number
    r'|v(\d[\d.]*)(?=\s|ch|ep|$)'             # v# at word boundary
    r')',
    re.IGNORECASE
)


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


def read_comicinfo(cbz_path: Path) -> tuple[str | None, str | None]:
    """
    Open a CBZ and return (entry_name, xml_text) for ComicInfo.xml,
    or (None, None) if not found or unreadable.
    """
    try:
        with zipfile.ZipFile(cbz_path, "r") as zf:
            namelist_lower = {n.lower(): n for n in zf.namelist()}
            key = next(
                (k for k in namelist_lower if os.path.basename(k).lower() == "comicinfo.xml"),
                None
            )
            if key:
                real_name = namelist_lower[key]
                return real_name, zf.read(real_name).decode("utf-8", errors="replace")
    except zipfile.BadZipFile:
        log.error(f"  Bad zip file, skipping: {cbz_path.name}")
    except OSError as e:
        log.error(f"  Cannot read {cbz_path.name}: {e}")
    return None, None


def write_comicinfo(cbz_path: Path, entry_name: str, new_xml: str) -> bool:
    """
    Rewrite a CBZ with an updated ComicInfo.xml.
    Preserves original compression for all other entries.
    Returns True on success.
    """
    tmp_path = cbz_path.with_suffix(".tmp.cbz")
    bak_path = cbz_path.with_suffix(".bak.cbz")

    for attempt in range(5):
        try:
            zip_entries: list[tuple] = []
            with zipfile.ZipFile(cbz_path, "r") as zin:
                for item in zin.infolist():
                    zip_entries.append((item, zin.read(item.filename)))

            with zipfile.ZipFile(tmp_path, "w") as zout:
                for item, data in zip_entries:
                    if item.filename == entry_name:
                        zout.writestr(item, new_xml.encode("utf-8"))
                    else:
                        zout.writestr(item, data, compress_type=item.compress_type)

            cbz_path.rename(bak_path)
            tmp_path.rename(cbz_path)
            bak_path.unlink(missing_ok=True)
            return True

        except OSError as e:
            log.warning(f"  File locked (attempt {attempt + 1}/5), retrying... ({e})")
            if tmp_path.exists():
                tmp_path.unlink()
            time.sleep(5)
        except Exception as e:
            log.error(f"  Failed to write {cbz_path.name}: {e}")
            if tmp_path.exists():
                tmp_path.unlink()
            return False

    log.error(f"  Gave up after 5 attempts: {cbz_path.name}")
    return False


# ─────────────────────────────────────────────
# CORE LOGIC
# ─────────────────────────────────────────────
def set_number_tag(xml_text: str, number: str) -> tuple[str, bool]:
    """
    Set <Number> in xml_text to the given number.
    Returns (updated_xml, changed) where changed is False if value was already correct.
    """
    existing = re.search(r"<Number>(.*?)</Number>", xml_text, re.IGNORECASE | re.DOTALL)

    if existing:
        current = existing.group(1).strip()
        if current == number:
            return xml_text, False
        xml_text = re.sub(
            r"<Number>.*?</Number>",
            f"<Number>{number}</Number>",
            xml_text, count=1, flags=re.IGNORECASE | re.DOTALL
        )
    else:
        xml_text = xml_text.replace(
            "</ComicInfo>",
            f"  <Number>{number}</Number>\n</ComicInfo>"
        )

    return xml_text, True


def process_cbz(cbz_path: Path, dry_run: bool = False) -> str:
    """
    Process a single CBZ file. Returns one of:
      "updated"  — Number tag written
      "skipped"  — already correct or no number found in filename
      "no_xml"   — no ComicInfo.xml in archive
      "error"    — read/write failure
    """
    number = extract_chapter_number(cbz_path.stem)
    if not number:
        return "skipped"

    entry_name, xml_text = read_comicinfo(cbz_path)
    if xml_text is None:
        return "no_xml"

    updated_xml, changed = set_number_tag(xml_text, number)
    if not changed:
        return "skipped"

    if dry_run:
        log.info(f"  [DRY RUN] Would set <Number>{number}</Number>: {cbz_path.name}")
        return "updated"

    success = write_comicinfo(cbz_path, entry_name, updated_xml)
    if success:
        log.info(f"  Set <Number>{number}</Number>: {cbz_path.name}")
        return "updated"
    return "error"


def process_folder(folder: Path, dry_run: bool = False) -> None:
    """Recursively process all CBZ files under folder."""
    cbz_files = sorted(folder.rglob("*.cbz"))
    if not cbz_files:
        log.info(f"No .cbz files found under: {folder}")
        return

    log.info(f"Found {len(cbz_files)} .cbz file(s) under: {folder}")

    counts = {"updated": 0, "skipped": 0, "no_xml": 0, "error": 0}
    for cbz in cbz_files:
        result = process_cbz(cbz, dry_run=dry_run)
        counts[result] += 1

    log.info(
        f"Done — {counts['updated']} updated, {counts['skipped']} skipped "
        f"(already correct or no number), {counts['no_xml']} had no ComicInfo.xml, "
        f"{counts['error']} error(s)."
    )


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
def main() -> None:
    args     = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry_run  = "--dry-run" in sys.argv

    targets  = args if args else SCAN_FOLDERS

    log.info("=" * 60)
    log.info("CBZ Number Tagger" + (" [DRY RUN]" if dry_run else ""))
    log.info("=" * 60)

    for target in targets:
        path = Path(target)
        if not path.exists():
            log.warning(f"Path not found, skipping: {path}")
            continue

        if path.is_file() and path.suffix.lower() == ".cbz":
            result = process_cbz(path, dry_run=dry_run)
            log.info(f"{path.name}: {result}")
        else:
            process_folder(path, dry_run=dry_run)

    log.info("=" * 60)
    log.info("CBZ Number Tagger complete.")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
