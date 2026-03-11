"""
CBZ File Watcher & Processor
Monitors a folder for incoming .cbz files inside subdirectories.
Processes ALL .cbz files in a directory first, then moves the
immediate comic directory to the configured destination.
"""

import os
import re
import gc
import html
import time
import shutil
import zipfile
import logging
import threading
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from logging.handlers import RotatingFileHandler as _RotatingFileHandler

# ─────────────────────────────────────────────
# CONFIGURATION — edit these as needed
# ─────────────────────────────────────────────
WATCH_FOLDER  = r"C:\Temp\Mega\Mega Uploads\book2"
LOG_FILE      = r"C:\ComicAutomation\cbz_watcher.log"
POLL_INTERVAL = 2    # seconds between stability checks
SETTLE_DELAY  = 5    # seconds of inactivity before processing a directory
MIN_AGE       = 300  # seconds a directory must exist before processing

DEFAULT_DEST  = r"\\tower\media\comics\Comix"

# Only list sources that need a NON-default destination.
# Keys are case-insensitive. Anything not listed goes to DEFAULT_DEST.
SOURCE_ROUTING = {
"1Manga.co (EN)":               r"\\tower\media\comics\Manga",
"Akuma (EN)":                   r"\\tower\media\comics\Manga",
"AllManga (EN)":                r"\\tower\media\comics\Manga",
"Anisa Scans (EN)":             r"\\tower\media\comics\Manga",
"Aqua Manga (EN)":              r"\\tower\media\comics\Manga",
"Armageddon (EN)":              r"\\tower\media\comics\Manga",
"Arven Scans (EN)":             r"\\tower\media\comics\Manga",
"Asura Scans (EN)":             r"\\tower\media\comics\Manga",
"Atsumaru (EN)":                r"\\tower\media\comics\Manga",
"BoxManhwa (EN)":               r"\\tower\media\comics\Manga",
"Cocomic (EN)":                 r"\\tower\media\comics\Manga",
"ComicHubFree (EN)":            r"\\tower\media\comics\Manga",
"Comix (EN)":                   r"\\tower\media\comics\Manga",
"Danboru (EN)":                 r"\\tower\media\comics\Manga",
"Erofus (EN)":                  r"\\tower\media\comics\Manga",
"Eros Scans (EN)":              r"\\tower\media\comics\Manga",
"GlobalComix (EN)":             r"\\tower\media\comics\Manga",
"Goda (EN)":                    r"\\tower\media\comics\Manga",
"Harimanga (EN)":               r"\\tower\media\comics\Manga",
"HeyToon (EN)":                 r"\\tower\media\comics\Manga",
"Hiperdex (EN)":                r"\\tower\media\comics\Manga",
"Hyakuro (EN)":                 r"\\tower\media\comics\Manga",
"Hyakuro Translations (EN)":    r"\\tower\media\comics\Manga",
"Kagane (EN)":                  r"\\tower\media\comics\Manga",
"KaliScan.com (EN)":            r"\\tower\media\comics\Manga",
"Kaliscan.io (EN)":             r"\\tower\media\comics\Manga",
"Kewn Scans (EN)":              r"\\tower\media\comics\Manga",
"King of Shojo (EN)":           r"\\tower\media\comics\Manga",
"Kissmanga.in (EN)":            r"\\tower\media\comics\Manga",
"LikeManga (EN)":               r"\\tower\media\comics\Manga",
"MadaraDex (EN)":               r"\\tower\media\comics\Manga",
"Manga Ball (EN)":              r"\\tower\media\comics\Manga",
"Manga Demon (EN)":             r"\\tower\media\comics\Manga",
"Manga District (EN)":          r"\\tower\media\comics\Manga",
"MangaBTT (EN)":                r"\\tower\media\comics\Manga",
"MangaClash (EN)":              r"\\tower\media\comics\Manga",
"MangaCrazy (ALL)":             r"\\tower\media\comics\Manga",
"MangaDex (EN)":                r"\\tower\media\comics\Manga",
"MangaFire (EN)":               r"\\tower\media\comics\Manga",
"MangaFox (EN)":                r"\\tower\media\comics\Manga",
"MangaFreak (EN)":              r"\\tower\media\comics\Manga",
"MangaGG (EN)":                 r"\\tower\media\comics\Manga",
"MangaHub (EN)":                r"\\tower\media\comics\Manga",
"Mangakakalot (EN)":            r"\\tower\media\comics\Manga",
"MangaKatana (EN)":             r"\\tower\media\comics\Manga",
"MangaTaro (EN)":               r"\\tower\media\comics\Manga",
"ManyToon (EN)":                r"\\tower\media\comics\Manga",
"ReadAllComics (EN)":           r"\\tower\media\comics\Manga",
"Toonily.me (EN)":              r"\\tower\media\comics\Manga",
"Top Manhua (EN)":              r"\\tower\media\comics\Manga",
"Webtoons.com (EN)":            r"\\tower\media\comics\Manga",
"Weeb Central (EN)":            r"\\tower\media\comics\Manga",
"XOXO Comics (EN)":             r"\\tower\media\comics\Manga",
"YakshaScans (EN)":             r"\\tower\media\comics\Manga",
"Zazamanga (EN)":               r"\\tower\media\comics\Manga",
}
# ─────────────────────────────────────────────

COMICINFO_TEMPLATE = """<ComicInfo xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Title></Title>
  <Series></Series>
  <Number></Number>
  <Summary></Summary>
  <Writer></Writer>
  <Penciller></Penciller>
  <Genre></Genre>
  <Web></Web>
  <ty:PublishingStatusTachiyomi xmlns:ty="http://www.w3.org/2001/XMLSchema"></ty:PublishingStatusTachiyomi>
  <ty:Categories xmlns:ty="http://www.w3.org/2001/XMLSchema"></ty:Categories>
  <mh:SourceMihon xmlns:mh="http://www.w3.org/2001/XMLSchema">Komga</mh:SourceMihon>
</ComicInfo>"""

# ─────────────────────────────────────────────
# MODULE-LEVEL CONSTANTS (compiled once)
# ─────────────────────────────────────────────
# Titles/filenames matching these patterns are treated as generic
# and may be overwritten by the title logic.
_TITLE_OVERWRITE_RES = [
    re.compile(r"manga_chapter",          re.IGNORECASE),
    re.compile(r"^#\s*english",           re.IGNORECASE),
    re.compile(r"^#\s*chapter",           re.IGNORECASE),
    re.compile(r"^chapter",               re.IGNORECASE),
    re.compile(r"^part\s+\d+",           re.IGNORECASE),
    re.compile(r"^doujinshi[\s_]chapter", re.IGNORECASE),
    re.compile(r"^unknown[\s_]chapter",   re.IGNORECASE),
]
NUMBER_PREFIX_RE = re.compile(r"^\d+\s*-\s*", re.IGNORECASE)

# Pre-normalised routing lookup — populated at startup for O(1) resolution
_ROUTING_LOWER: dict = {}

# Directories currently being processed — events for these are suppressed to
# prevent the file-rename step from re-triggering the settle timer in a loop.
_processing_dirs: set = set()
_processing_dirs_lock = threading.Lock()

# ─────────────────────────────────────────────
# LOGGING SETUP
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        _RotatingFileHandler(
            LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        ),  # 5 MB per file, keep 3 backups
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# CLEANING HELPERS
# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
# COMPILED REGEX PATTERNS
# ─────────────────────────────────────────────
_BRACKET_RE       = re.compile(r'\[[^\]]*\]|\([^)]*\)')
_STRAY_RE         = re.compile(r'[\[\]()]')
_SPACES_RE        = re.compile(r' {2,}')
_URL_RE           = re.compile(                                 # website URLs to strip from filenames/titles
    r'(?:https?://\S+)'
    r'|(?:www\.\S+)'
    r'|(?:\b[\w-]+\.(?:com|net|org|io|co|info|biz|tv|me|cc|us|uk|ca|au)(?:/\S*)?)',
    re.IGNORECASE
)
_SCAN_GROUP_RE    = re.compile(                                 # scanlator/scan-group names to strip
    r'\b[\w-]*scans?\b|\b[\w-]*scanners?\b|\b[\w-]*scanlations?\b',
    re.IGNORECASE
)
_GCODE_RE         = re.compile(r'[\s\-]*\bG\d{3,5}$')
_TRAILING_SLASH_RE = re.compile(r'[\s/]+$')
_NON_LATIN_RE     = re.compile(
    r'[^\u0000-\u024F'        # Basic + Extended Latin
    r'\u0370-\u03FF'          # Greek
    r'\u2000-\u206F'          # General Punctuation (en/em dash, ellipsis, curly quotes)
    r'\u2600-\u27BF'          # Misc Symbols + Dingbats
    r'\uFE00-\uFE0F'          # Variation Selectors (emoji presentation)
    r'\U0001F300-\U0001FAFF'  # Emoji / Supplemental Symbols and Pictographs
    r']+'
)                              # non-Latin / non-Greek / non-emoji characters to strip
_NUM_TOKEN_RE     = re.compile(                              # leading-zero / .0 number normalisation
    r'''
    (                                           # group 1: keyword + separator
        (?:
            ch(?:ap(?:ter)?)?p?                 # ch / chap / chapter / chp
          | issue
          | ep(?:isode)?                        # ep / episode
          | vol(?:ume)?                         # vol / volume
          | v(?=\d)                             # bare 'v' immediately before digits
        )
        \.?\s*
    )
    (\d[\d.]*)                                  # group 2: the raw number
    ''',
    re.IGNORECASE | re.VERBOSE
)
_DIR_LEADING_HASH_RE  = re.compile(r'^#+\s*')
_DIR_TRAILING_HASH_RE = re.compile(r'\s*#+$')
_DIR_TRAILING_STUB_RE = re.compile(                         # dangling trailing tokens with no number
    r'[\s_\-]*(?:part|v|ch(?:ap(?:ter)?)?)\s*$', re.IGNORECASE)


def sanitize(text: str) -> str:
    """
    Shared sanitization pipeline applied to filenames, directory names,
    and XML fields (Title, Series). Steps in order:

      1. Decode XML/HTML entities (e.g. &amp; -> &, &apos; -> ', &lt; -> <)
      2. Remove website URLs (http://, www., bare domain.tld)
      3. Remove scanlator group names (words containing scan/scans/scanners/scanlation)
      4. Strip trailing G-code suffix (e.g. "Batman G1234" -> "Batman")
      5. Strip non-Latin/non-Greek/non-emoji characters
      6. Remove [bracketed] and (parenthesised) groups in one pass
      7. Strip any lone stray bracket/parenthesis characters left behind
      8. Replace underscores with spaces
      9. Collapse multiple spaces and strip leading/trailing whitespace
    """
    text = html.unescape(text)                    # 1. decode entities
    text = _URL_RE.sub("", text)                  # 2. strip URLs/websites
    text = _SCAN_GROUP_RE.sub("", text)           # 3. strip scan-group names
    text = _TRAILING_SLASH_RE.sub("", text)        # 4. strip trailing slash(es)
    text = _GCODE_RE.sub("", text)                 # 5. strip trailing G-code suffix
    text = _NON_LATIN_RE.sub("", text)             # 6. strip non-Latin/non-Greek/non-emoji
    text = _BRACKET_RE.sub("", text)              # 7. strip bracketed groups
    text = _STRAY_RE.sub("", text)                # 8. strip stray brackets
    text = text.replace("_", " ")                 # 9. underscores -> spaces
    return _SPACES_RE.sub(" ", text).strip()      # 10. collapse whitespace
def clean_filename(name: str) -> str:
    """Sanitize a .cbz filename stem, preserving the extension."""
    stem = Path(name).stem
    ext  = Path(name).suffix
    return sanitize(stem) + ext


def clean_directory_name(name: str) -> str:
    """
    Sanitize a directory name, then apply extra directory-specific cleaning:
      - Strip leading hashtags  (# Batman -> Batman)
      - Strip trailing hashtags (Batman # -> Batman)
      - Strip trailing dangling tokens with no following number:
          "- part", " v", " ch", " chap", " chapter"
        (numbered forms like "Batman v2" or "Batman ch 3" are left intact)
    """
    name = sanitize(name)
    name = _DIR_LEADING_HASH_RE.sub('', name)
    name = _DIR_TRAILING_HASH_RE.sub('', name).strip()
    name = _DIR_TRAILING_STUB_RE.sub('', name).strip()
    return name
def clean_xml_field(value: str) -> str:
    """Sanitize an XML field value (Title, Series)."""
    return sanitize(value)


def is_generic(text: str) -> bool:
    """Return True if text matches any generic title/filename pattern."""
    return any(r.search(text) for r in _TITLE_OVERWRITE_RES)
def _write_cbz_with_comicinfo(
    cbz_path: Path,
    new_xml: str,
    replace_entry: str | None = None
) -> None:
    """
    Rewrite a .cbz with an updated or injected ComicInfo.xml.
      - replace_entry: existing zip entry name to overwrite (None = inject new).
    Each file's original compression method is preserved to avoid
    re-compressing already-compressed image data.
    """
    tmp_path = cbz_path.with_suffix(".tmp.cbz")
    action   = "updated" if replace_entry else "injected"

    for attempt in range(5):
        try:
            # Step 1: Read entire zip into memory then close the file handle
            zip_entries: list[tuple] = []
            with zipfile.ZipFile(cbz_path, "r") as zin:
                for item in zin.infolist():
                    zip_entries.append((item, zin.read(item.filename)))

            # Step 2: Write to tmp — preserve original compression per entry,
            #         use DEFLATED only for XML (plain text compresses well)
            with zipfile.ZipFile(tmp_path, "w") as zout:
                for item, data in zip_entries:
                    if item.filename == replace_entry:
                        zout.writestr(item, new_xml.encode("utf-8"))
                    else:
                        zout.writestr(item, data, compress_type=item.compress_type)
                if not replace_entry:
                    zout.writestr(
                        "ComicInfo.xml",
                        new_xml.encode("utf-8"),
                        compress_type=zipfile.ZIP_DEFLATED
                    )

            # Step 3: Atomic swap — rename avoids unlink lock issues on Windows
            bak_path = cbz_path.with_suffix(".bak.cbz")
            cbz_path.rename(bak_path)
            tmp_path.rename(cbz_path)
            bak_path.unlink(missing_ok=True)
            log.info(f"    comicinfo.xml {action} successfully.")
            return

        except OSError as e:
            log.warning(f"    File locked (attempt {attempt + 1}/5), retrying in 0.5s... ({e})")
            if tmp_path.exists():
                tmp_path.unlink()
            time.sleep(0.5)
        except Exception as e:
            log.error(f"    Failed to write comicinfo.xml: {e}")
            if tmp_path.exists():
                tmp_path.unlink()
            return

    log.error(f"    Gave up writing comicinfo.xml after 5 attempts: {cbz_path.name}")
def _rewrite_comicinfo(cbz_path: Path, xml_entry_name: str, new_xml: str) -> None:
    _write_cbz_with_comicinfo(cbz_path, new_xml, replace_entry=xml_entry_name)


def _inject_comicinfo(cbz_path: Path) -> None:
    _write_cbz_with_comicinfo(cbz_path, COMICINFO_TEMPLATE)


def process_comicinfo(
    cbz_path: Path,
    prefetched_xml: tuple[str, str] | None = None
) -> None:
    """
    Inspect the .cbz for comicinfo.xml.
    - Found:   check/fix Title and Series tags.
    - Missing: inject a fresh comicinfo.xml from template.

    prefetched_xml: optional (entry_name, xml_text) already read by the
    caller so the zip does not need to be opened a second time.
    """
    parent_dir    = cbz_path.parent.name
    filename_stem = NUMBER_PREFIX_RE.sub("", cbz_path.stem).strip()

    for attempt in range(5):
        try:
            if prefetched_xml is not None:
                # Caller already read the zip — use the data directly
                real_name, xml_text = prefetched_xml
                has_xml = xml_text is not None
                prefetched_xml = None  # only use on first attempt
            else:
                # Read zip then close before any writes
                found_key = real_name = xml_text = None
                has_xml = False

                with zipfile.ZipFile(cbz_path, "r") as zf:
                    namelist_lower = {n.lower(): n for n in zf.namelist()}
                    found_key = next(
                        (k for k in namelist_lower if os.path.basename(k).lower() == "comicinfo.xml"),
                        None
                    )
                    if found_key:
                        real_name = namelist_lower[found_key]
                        xml_text  = zf.read(real_name).decode("utf-8", errors="replace")
                        has_xml   = True

            if has_xml:
                title_match  = re.search(r"<Title>(.*?)</Title>",   xml_text, re.IGNORECASE | re.DOTALL)
                series_match = re.search(r"<Series>(.*?)</Series>", xml_text, re.IGNORECASE | re.DOTALL)
                title_value  = clean_xml_field(title_match.group(1).strip())  if title_match  else ""
                series_value = clean_xml_field(series_match.group(1).strip()) if series_match else ""

                # Clean brackets/CJK from Series tag if needed
                if series_match and series_value != series_match.group(1).strip():
                    xml_text = re.sub(
                        r"<Series>.*?</Series>",
                        f"<Series>{series_value}</Series>",
                        xml_text, count=1, flags=re.IGNORECASE | re.DOTALL
                    )
                    log.info(f"    Series cleaned: '{series_match.group(1).strip()}' -> '{series_value}'")

                # Strip leading "# - " prefix (e.g. "3 - Batman" -> "Batman")
                title_value   = NUMBER_PREFIX_RE.sub("", title_value).strip()

                title_generic    = is_generic(title_value) or bool(GIBBERISH_RE.match(title_value))
                filename_generic = is_generic(filename_stem)

                # ── Title resolution logic ──────────────────────────────
                # Title == dir  + filename custom   → use filename
                # Title == dir  + filename generic  → already correct
                # Title gibberish/generic + filename custom   → use filename
                # Title gibberish/generic + filename generic  → use parent dir
                # Title custom  (any filename)      → leave unchanged
                # NOTE: we never early-return here — Number/Volume tags
                # must always be checked even when the title is fine.
                new_title    = None
                title_changed = False
                if title_value == parent_dir and not filename_generic:
                    new_title = filename_stem
                    log.info(f"    Title matches dir but filename='{filename_stem}' is custom - using filename.")
                elif title_value == parent_dir:
                    log.info(f"    Title matches parent dir '{parent_dir}' - OK.")
                elif title_generic and not filename_generic:
                    new_title = filename_stem
                    log.info(f"    Title='{title_value}' is gibberish/generic, filename is custom - using filename.")
                elif title_generic and filename_generic:
                    new_title = parent_dir
                    log.info(f"    Title='{title_value}' is gibberish/generic and filename is generic - using parent dir '{new_title}'.")
                else:
                    log.info(f"    Title='{title_value}' is custom - leaving unchanged.")

                if new_title is not None:
                    xml_text = re.sub(
                        r"<Title>.*?</Title>",
                        f"<Title>{new_title}</Title>",
                        xml_text, count=1, flags=re.IGNORECASE | re.DOTALL
                    )
                    title_changed = True

                # Update <Number> and <Volume> tags from filename — always runs
                chapter_num = extract_chapter_number(cbz_path.stem)
                volume_num  = extract_volume_number(cbz_path.stem)

                if chapter_num:
                    if re.search(r"<Number>.*?</Number>", xml_text, re.IGNORECASE | re.DOTALL):
                        xml_text = re.sub(
                            r"<Number>.*?</Number>",
                            f"<Number>{chapter_num}</Number>",
                            xml_text, count=1, flags=re.IGNORECASE | re.DOTALL
                        )
                    else:
                        xml_text = xml_text.replace(
                            "</ComicInfo>",
                            f"  <Number>{chapter_num}</Number>\n</ComicInfo>"
                        )
                    log.info(f"    Number set to '{chapter_num}'.")

                if volume_num:
                    if re.search(r"<Volume>.*?</Volume>", xml_text, re.IGNORECASE | re.DOTALL):
                        xml_text = re.sub(
                            r"<Volume>.*?</Volume>",
                            f"<Volume>{volume_num}</Volume>",
                            xml_text, count=1, flags=re.IGNORECASE | re.DOTALL
                        )
                    else:
                        # Insert <Volume> directly after <Series> tag
                        xml_text = re.sub(
                            r"(<Series>.*?</Series>)",
                            rf"\1\n  <Volume>{volume_num}</Volume>",
                            xml_text, count=1, flags=re.IGNORECASE | re.DOTALL
                        )
                    log.info(f"    Volume set to '{volume_num}'.")

                if title_changed or chapter_num or volume_num:
                    _rewrite_comicinfo(cbz_path, real_name, xml_text)
                else:
                    log.info(f"    comicinfo.xml OK - no changes needed.")
                return

            else:
                # No comicinfo.xml — build one with resolved title, series, number, and volume
                resolved_title = filename_stem if not is_generic(filename_stem) and not GIBBERISH_RE.match(filename_stem) else parent_dir
                chapter_num    = extract_chapter_number(cbz_path.stem)
                volume_num     = extract_volume_number(cbz_path.stem)
                number_tag     = f"  <Number>{chapter_num}</Number>" if chapter_num else "  <Number></Number>"
                injected_xml   = COMICINFO_TEMPLATE.replace(
                    "<Title></Title>",  f"<Title>{resolved_title}</Title>"
                ).replace(
                    "<Series></Series>", f"<Series>{parent_dir}</Series>"
                ).replace(
                    "<Number></Number>", number_tag
                )
                if volume_num:
                    injected_xml = re.sub(
                        r"(<Series>.*?</Series>)",
                        rf"\1\n  <Volume>{volume_num}</Volume>",
                        injected_xml, count=1, flags=re.IGNORECASE | re.DOTALL
                    )
                log.info(f"    Injecting comicinfo.xml: Title='{resolved_title}', Series='{parent_dir}'" +
                         (f", Number='{chapter_num}'" if chapter_num else "") +
                         (f", Volume='{volume_num}'" if volume_num else "") + ".")
                _write_cbz_with_comicinfo(cbz_path, injected_xml)
                return

        except OSError:
            log.warning(f"    File locked reading zip (attempt {attempt + 1}/5), retrying in 5s...")
            time.sleep(5)
        except zipfile.BadZipFile:
            log.error(f"    Cannot open {cbz_path.name} - bad zip file, skipping.")
            return

    log.error(f"    Gave up reading {cbz_path.name} after 5 attempts.")


# ─────────────────────────────────────────────
# DIRECTORY MERGE (conflict = keep largest)
# ─────────────────────────────────────────────
def wait_for_file_stable(path: Path, stable_seconds: int = 3) -> bool:
    """Wait until file size stops changing (i.e. file has finished copying)."""
    previous_size = -1
    stable_count  = 0

    for _ in range(30):  # max ~60 seconds
        try:
            current_size = path.stat().st_size
        except FileNotFoundError:
            return False

        if current_size == previous_size:
            stable_count += 1
            if stable_count >= stable_seconds:
                return True
        else:
            stable_count = 0

        previous_size = current_size
        time.sleep(POLL_INTERVAL)

    log.warning(f"    File did not stabilise in time: {path.name}")
    return False


# ─────────────────────────────────────────────
# SINGLE CBZ PROCESSING
# ─────────────────────────────────────────────



GIBBERISH_RE = re.compile(
    r'^(?:TEMP[\s_-]*[0-9a-f]{8,}|[0-9a-f]{16,}'
    r'|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$',
    re.IGNORECASE
)

# Matches "Unknown Chapter N" — numbered form gets dir prefix, bare form gets replaced
CHAPTER_ONLY_RE = re.compile(
    r'^(?:ch(?:ap(?:ter)?)?\.?\s*|chp\.?\s*)(\d[\d.]*)',
    re.IGNORECASE
)

# Matches "indexed chapter-dash" stems: "1. CHAPTER - 1" or "Chapter - 12"
# Requires either a leading index ("1. ") OR a dash after the keyword to avoid
# overlapping with CHAPTER_ONLY_RE (which handles "ch. 12", "chapter 14" etc.)
HASH_CHAPTER_RE = re.compile(
    r'^#\s*chapter\s*(\d[\d.]*)(.*?)$',
    re.IGNORECASE
)

# Matches chapter-only stems that should be prepended with the directory name:
#   chapter 14 / ch. 12 / ch.12 / ch12 / chp. 5 / chap. 100 etc.
NUMBERED_CHAPTER_RE = re.compile(
    r'^(?:'
    r'(?:\d+\.\s*)'                           # branch 1: leading "1. " required
    r'ch(?:ap(?:ter)?)?p?\.?\s*-?\s*(\d[\d.]*)\s*$'
    r'|'
    r'ch(?:ap(?:ter)?)?p?\.?\s*-\s*(\d[\d.]*)\s*$'  # branch 2: dash required
    r')',
    re.IGNORECASE
)

# ─────────────────────────────────────────────
# LOGGING SETUP
# ─────────────────────────────────────────────
UNKNOWN_CHAPTER_RE = re.compile(
    r'^unknown[\s_]chapter[\s_]*(\d[\d.]*)(.*?)$',
    re.IGNORECASE
)

# Matches "# Chapter N" — numbered form gets dir prefix, bare "# Chapter" gets replaced
def normalise_number_tokens(stem: str) -> str:
    """
    Normalise chapter/volume number tokens in a filename stem:
      - Strip leading zeros:  Ch. 001  -> Ch. 1
      - Drop trailing .0:     Ch. 1.0  -> Ch. 1
      - Preserve decimals:    Ch. 1.5  -> Ch. 1.5  (unchanged)
      - Covers: ch/chap/chapter/chp, vol/volume/v, issue, ep/episode
    """
    def _sub(m: re.Match) -> str:
        try:
            n = float(m.group(2))
            fmt = str(int(n)) if n == int(n) else str(n)
            return m.group(1) + fmt
        except ValueError:
            return m.group(0)
    return _NUM_TOKEN_RE.sub(_sub, stem)


def normalize_stem(stem: str, dir_name: str) -> str:
    """
    Apply directory-aware fixes to a cleaned filename stem:
      - Gibberish/temp names (hex strings, TEMP + hex, UUIDs) → replaced with dir_name
      - Chapter-only names (ch. 12, chapter 14, chp. 5 etc.) → prepended with dir_name
      - Indexed/dashed chapter names (1. CHAPTER - 1, Chapter - 12) → "Dir Ch. N"
    Returns the (possibly modified) stem. Extension is NOT included.
    """
    if GIBBERISH_RE.match(stem):
        return dir_name

    # Chapter patterns run BEFORE the generic check so "chapter 14" becomes
    # "Dir Chapter 14" rather than just "Dir"
    m = CHAPTER_ONLY_RE.match(stem)
    if m:
        chapter_part = stem[0].upper() + stem[1:]
        return f"{dir_name} {chapter_part}"

    m = NUMBERED_CHAPTER_RE.match(stem)
    if m:
        num = m.group(1) or m.group(2)
        return f"{dir_name} Ch. {num}"

    m = UNKNOWN_CHAPTER_RE.match(stem)
    if m:
        num    = m.group(1)
        suffix = m.group(2).strip(" -_")
        return f"{dir_name} Chapter {num}" + (f" {suffix}" if suffix else "")

    m = HASH_CHAPTER_RE.match(stem)
    if m:
        num    = m.group(1)
        suffix = m.group(2).strip(" -_")
        return f"{dir_name} Chapter {num}" + (f" {suffix}" if suffix else "")

    # Fully generic stems (# English, # Chapter, manga_chapter etc.) → dir name only
    if is_generic(stem):
        return dir_name

    return stem



# ─────────────────────────────────────────────
# CHAPTER NUMBER EXTRACTION
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

# Volume number: vol/volume/v followed by a number
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


def extract_volume_number(stem: str) -> str | None:
    """
    Extract the volume number from a filename stem.
    Handles: Vol. 3, Volume 3, v3 (at word boundary before ch/ep/end).
    Returns a string like "3" or "3.5", or None if not found.
    """
    m = _VOLUME_NUMBER_RE.search(stem)
    if m:
        val = m.group(1) or m.group(2)
        n = float(val)
        return str(int(n)) if n == int(n) else str(n)
    return None


# ─────────────────────────────────────────────
# COMICINFO.XML HANDLING
# ─────────────────────────────────────────────
def process_cbz_file(cbz_path: Path, override_name: str | None = None) -> Path:
    """
    Clean filename and process comicinfo for a single .cbz.
    Returns the final (possibly renamed) path. Does NOT move the file.
    If override_name is given it is used as the filename instead of the cleaned name.
    """
    log.info(f"  Processing: {cbz_path.name}")

    if not cbz_path.exists():
        log.warning(f"    File no longer exists, skipping: {cbz_path.name}")
        return cbz_path

    if cbz_path.stat().st_size == 0:
        log.warning(f"    Skipping zero-byte file: {cbz_path.name}")
        return cbz_path

    # Use override_name directly if supplied (avoids redundant clean_filename call)
    if override_name is not None:
        new_name = override_name
    else:
        stem     = Path(clean_filename(cbz_path.name)).stem
        stem     = normalize_stem(stem, cbz_path.parent.name)
        stem     = normalise_number_tokens(stem)
        new_name = stem + cbz_path.suffix

    if new_name != cbz_path.name:
        new_path = cbz_path.parent / new_name
        if new_path.exists():
            # Destination already exists — keep the larger file, discard the smaller
            src_size  = cbz_path.stat().st_size
            dest_size = new_path.stat().st_size
            if src_size > dest_size:
                log.info(
                    f"    Rename collision: '{new_name}' already exists but incoming "
                    f"({src_size:,} B) > existing ({dest_size:,} B) - replacing."
                )
                cbz_path.replace(new_path)  # replace() overwrites on Windows
                cbz_path = new_path
            else:
                log.info(
                    f"    Rename collision: '{new_name}' already exists and existing "
                    f"({dest_size:,} B) >= incoming ({src_size:,} B) - discarding incoming."
                )
                cbz_path.unlink()
                return new_path  # point at the winner so comicinfo still runs on it
        else:
            cbz_path.rename(new_path)
            log.info(f"    Renamed: '{cbz_path.name}' -> '{new_name}'")
            cbz_path = new_path
    else:
        log.info(f"    Filename unchanged: '{cbz_path.name}'")

    # Read ComicInfo.xml here so process_comicinfo does not need to reopen the zip
    prefetched: tuple[str, str] | None = None
    try:
        with zipfile.ZipFile(cbz_path, "r") as zf:
            namelist_lower = {n.lower(): n for n in zf.namelist()}
            key = next(
                (k for k in namelist_lower if os.path.basename(k).lower() == "comicinfo.xml"),
                None
            )
            if key:
                real = namelist_lower[key]
                prefetched = (real, zf.read(real).decode("utf-8", errors="replace"))
            else:
                prefetched = (None, None)
    except (zipfile.BadZipFile, OSError):
        pass  # let process_comicinfo handle the error with its retry logic

    process_comicinfo(cbz_path, prefetched_xml=prefetched)
    return cbz_path



# ─────────────────────────────────────────────
# PROGRESS TRACKING
# ─────────────────────────────────────────────
def _merge_directories(src_dir: Path, dest_dir: Path) -> None:
    """
    Merge src_dir into dest_dir recursively.
    On any file conflict, keep whichever copy is larger.
    After merging, remove src_dir.
    """
    for src_item in sorted(src_dir.rglob("*")):
        relative  = src_item.relative_to(src_dir)
        dest_item = dest_dir / relative

        if src_item.is_dir():
            dest_item.mkdir(parents=True, exist_ok=True)
            continue

        if dest_item.exists():
            src_size  = src_item.stat().st_size
            dest_size = dest_item.stat().st_size

            if src_size > dest_size:
                log.info(
                    f"    Conflict '{relative}': incoming ({src_size:,} B) > "
                    f"existing ({dest_size:,} B) - replacing."
                )
                dest_item.unlink()
                shutil.move(str(src_item), str(dest_item))
            else:
                log.info(
                    f"    Conflict '{relative}': existing ({dest_size:,} B) >= "
                    f"incoming ({src_size:,} B) - keeping existing."
                )
                src_item.unlink()
        else:
            dest_item.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_item), str(dest_item))

    shutil.rmtree(src_dir, ignore_errors=True)


# ─────────────────────────────────────────────
# SINGLE CBZ PROCESSING
# ─────────────────────────────────────────────
def _resolve_dest(comic_dir: Path) -> str:
    """
    Walk up from comic_dir to find the immediate child of WATCH_FOLDER,
    then do an O(1) lookup against _ROUTING_LOWER.
    Falls back to DEFAULT_DEST if not found.
    """
    watch = Path(WATCH_FOLDER)
    source_dir = comic_dir
    for candidate in [comic_dir] + list(comic_dir.parents):
        if candidate.parent == watch:
            source_dir = candidate
            break

    dest = _ROUTING_LOWER.get(source_dir.name.lower())
    if dest:
        log.info(f"  Routing '{source_dir.name}' -> {dest}")
        return dest
    return DEFAULT_DEST


def _move_cbz_dir(dir_path: Path, dest_folder: str) -> None:
    """Move a processed comic directory to dest_folder, merging if it already exists."""
    dest_dir = Path(dest_folder) / dir_path.name
    log.info(f"  Moving '{dir_path.name}' -> {dest_dir}")

    try:
        os.makedirs(dest_folder, exist_ok=True)

        if dest_dir.exists():
            log.info(f"  Destination exists - merging, keeping larger files on conflict.")
            _merge_directories(dir_path, dest_dir)
            if dir_path.exists():
                shutil.rmtree(dir_path, ignore_errors=True)
            log.info(f"  Merge complete.")
        else:
            shutil.move(str(dir_path), str(dest_dir))
            log.info(f"  Moved successfully.")

    except Exception as e:
        log.error(f"  Failed to move directory '{dir_path.name}': {e}")


def process_and_move_directory(dir_path: Path) -> None:
    """
    Recursively find all immediate directories containing .cbz files under
    dir_path. For each one: clean dir name → process files → move to dest.
    """
    with _processing_dirs_lock:
        _processing_dirs.add(dir_path)
    try:
        _process_and_move_directory_inner(dir_path)
    finally:
        with _processing_dirs_lock:
            _processing_dirs.discard(dir_path)


def _process_and_move_directory_inner(dir_path: Path) -> None:
    # Clean the top-level watched directory name first (handles the case where
    # cbz files live in a subdirectory and dir_path itself never enters the
    # per-comic_dir cleaning loop below).
    clean_top = clean_directory_name(dir_path.name)
    if clean_top and clean_top != dir_path.name:
        new_top = dir_path.parent / clean_top
        try:
            dir_path.rename(new_top)
            log.info(f"  Directory renamed: '{dir_path.name}' -> '{clean_top}'")
            dir_path = new_top
        except OSError as e:
            log.warning(f"  Could not rename top-level dir '{dir_path.name}': {e}")
    # Also update the processing-lock entry so event suppression stays correct
    with _processing_dirs_lock:
        _processing_dirs.discard(dir_path.parent / dir_path.name)
        _processing_dirs.add(dir_path)

    log.info("=" * 60)
    log.info(f"Scanning: {dir_path.name}")

    if not dir_path.exists() or not dir_path.is_dir():
        log.warning(f"  Directory no longer exists: {dir_path}")
        return

    # Group .cbz files by their immediate parent directory
    cbz_dirs: dict[Path, list[Path]] = {}
    for cbz in sorted(dir_path.rglob("*.cbz")):
        cbz_dirs.setdefault(cbz.parent, []).append(cbz)

    if not cbz_dirs:
        log.info(f"  No .cbz files found under '{dir_path.name}', skipping.")
        return

    log.info(f"  Found .cbz files in {len(cbz_dirs)} directory(s).")

    total_processed = total_skipped = total_renamed = 0

    for comic_dir, cbz_files in sorted(cbz_dirs.items()):
        dest_folder = _resolve_dest(comic_dir)

        # Clean directory name BEFORE processing files so comicinfo
        # title logic compares against the final clean name
        clean_dir_name = clean_directory_name(comic_dir.name)
        if not clean_dir_name:
            log.warning(f"  Skipping rename: cleaning '{comic_dir.name}' produced an empty name.")
        elif clean_dir_name != comic_dir.name:
            new_dir_path = comic_dir.parent / clean_dir_name
            comic_dir.rename(new_dir_path)
            log.info(f"  Directory renamed: '{comic_dir.name}' -> '{clean_dir_name}'")
            cbz_files = [new_dir_path / f.name for f in cbz_files]
            comic_dir = new_dir_path

        # Pre-compute fallback names for any files whose cleaned stem is empty.
        # If only one such file exists, use the directory name directly.
        # If multiple, enumerate: "DirName 1.cbz", "DirName 2.cbz", ...
        empty_stem_files = [
            cbz for cbz in cbz_files
            if cbz.exists() and not Path(clean_filename(cbz.name)).stem
        ]
        fallback_names: dict[Path, str] = {}
        if len(empty_stem_files) == 1:
            fallback_names[empty_stem_files[0]] = comic_dir.name + ".cbz"
        elif len(empty_stem_files) > 1:
            for i, cbz in enumerate(empty_stem_files, start=1):
                fallback_names[cbz] = f"{comic_dir.name} {i}.cbz"

        log.info(f"  Processing directory: {comic_dir.name} ({len(cbz_files)} file(s)) -> {dest_folder}")
        for cbz in cbz_files:
            if not cbz.exists():
                total_skipped += 1
                continue
            if cbz.stat().st_size == 0:
                log.warning(f"    Skipping zero-byte file: {cbz.name}")
                total_skipped += 1
                continue
            original_name = cbz.name
            override = fallback_names.get(cbz)
            if override:
                log.info(f"    Empty stem fallback: '{cbz.name}' -> '{override}'")
            result_path = process_cbz_file(cbz, override_name=override)
            total_processed += 1
            if result_path.name != original_name:
                total_renamed += 1
        _move_cbz_dir(comic_dir, dest_folder)

    log.info(
        f"  Batch complete — {total_processed} processed, "
        f"{total_renamed} renamed, {total_skipped} skipped."
    )


# ─────────────────────────────────────────────
# DIRECTORY SETTLE TRACKER
# Debounces rapid file events — waits until no
# new files have arrived for SETTLE_DELAY seconds
# before triggering processing.
# ─────────────────────────────────────────────
class DirectorySettleTracker:
    def __init__(self, settle_delay: float = SETTLE_DELAY):
        self.settle_delay = settle_delay
        self._timers: dict = {}
        self._lock = threading.Lock()

    def notify(self, dir_path: Path) -> None:
        """Reset the settle timer each time a file event fires."""
        with self._lock:
            existing = self._timers.get(dir_path)
            if existing:
                existing.cancel()
            timer = threading.Timer(self.settle_delay, self._on_settled, args=[dir_path])
            self._timers[dir_path] = timer
            timer.start()

    def _on_settled(self, dir_path: Path) -> None:
        with self._lock:
            self._timers.pop(dir_path, None)
        # Enforce minimum age — re-schedule if the directory isn't old enough yet
        if dir_path.exists():
            age = time.time() - dir_path.stat().st_ctime
            if age < MIN_AGE:
                wait = MIN_AGE - age
                log.info(
                    f"Directory '{dir_path.name}' settled but minimum age not reached "
                    f"({int(age)}s / {MIN_AGE}s) — waiting {int(wait)}s more."
                )
                timer = threading.Timer(wait, self._on_settled, args=[dir_path])
                with self._lock:
                    self._timers[dir_path] = timer
                timer.start()
                return
        log.info(f"Directory ready: '{dir_path.name}' (settled + minimum age {MIN_AGE}s met)")
        process_and_move_directory(dir_path)


# ─────────────────────────────────────────────
# WATCHDOG EVENT HANDLER
# ─────────────────────────────────────────────
class CBZHandler(FileSystemEventHandler):
    def __init__(self, tracker: DirectorySettleTracker):
        self.tracker = tracker

    def _handle(self, path: Path) -> None:
        if path.suffix.lower() != ".cbz":
            return
        parent = path.parent
        # Suppress events that are caused by our own rename/rewrite operations
        # to avoid an infinite re-processing loop.
        with _processing_dirs_lock:
            for proc_dir in _processing_dirs:
                try:
                    parent.relative_to(proc_dir)
                    return  # event is inside a directory we're currently processing
                except ValueError:
                    pass
        if parent == Path(WATCH_FOLDER):
            log.warning(
                f"'{path.name}' dropped directly into watch root. "
                f"Place files inside a subdirectory for best results. Processing anyway."
            )
        self.tracker.notify(parent)

    def on_created(self, event):
        if not event.is_directory:
            self._handle(Path(event.src_path))

    def on_moved(self, event):
        if not event.is_directory:
            self._handle(Path(event.dest_path))

    def on_modified(self, event):
        if not event.is_directory:
            self._handle(Path(event.src_path))


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
def main():
    watch_path = Path(WATCH_FOLDER)
    os.makedirs(watch_path, exist_ok=True)
    for dest in set(SOURCE_ROUTING.values()) | {DEFAULT_DEST}:
        os.makedirs(dest, exist_ok=True)

    # Build normalised routing lookup once at startup
    global _ROUTING_LOWER
    _ROUTING_LOWER = {k.lower(): v for k, v in SOURCE_ROUTING.items()}

    log.info("=" * 60)
    log.info("CBZ Watcher started")
    log.info(f"  Watching : {WATCH_FOLDER}")
    log.info(f"  Routes   : {len(SOURCE_ROUTING)} source(s) -> Manga, all others -> {DEFAULT_DEST}")
    log.info(f"  Log      : {LOG_FILE}")
    log.info(f"  Settle   : {SETTLE_DELAY}s after last file event")
    log.info("=" * 60)

    tracker = DirectorySettleTracker()

    # Clean up any orphaned temp/bak files left by a previous interrupted run
    stale = list(watch_path.rglob("*.tmp.cbz")) + list(watch_path.rglob("*.bak.cbz"))
    if stale:
        log.info(f"  Cleaning up {len(stale)} stale temp file(s) from previous run...")
        for f in stale:
            try:
                f.unlink()
                log.info(f"    Deleted stale file: {f.name}")
            except OSError as e:
                log.warning(f"    Could not delete stale file {f.name}: {e}")

    # Process any directories already present at startup
    for subdir in sorted(watch_path.iterdir()):
        if subdir.is_dir() and any(subdir.rglob("*.cbz")):
            log.info(f"Found existing directory at startup: {subdir.name}")
            process_and_move_directory(subdir)

    handler  = CBZHandler(tracker)
    observer = Observer()
    observer.schedule(handler, str(watch_path), recursive=True)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down watcher...")
        observer.stop()

    observer.join()
    log.info("CBZ Watcher stopped.")


if __name__ == "__main__":
    main()