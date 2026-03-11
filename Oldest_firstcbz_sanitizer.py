"""
CBZ Sanitizer
Recursively scans a folder for .cbz files, processes each one
(filename cleaning, comicinfo.xml creation/repair, directory renaming)
but does NOT move or transfer anything when done.

Use this to sanitize a batch of files already in their target location.
"""

import os
import sys
import html
import re
import gc
import json
import shutil
import time
import statistics
import zipfile
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler as _RotatingFileHandler
from pathlib import Path

# ─────────────────────────────────────────────
# CONFIGURATION — edit these as needed
# ─────────────────────────────────────────────
SCAN_FOLDER    = r"\\tower\media\comics\Comix"   # Folder to sanitize
LOG_FILE       = r"C:\ComicAutomation\Oldestfirstcbz_sanitizer.log"
PROGRESS_FILE  = r"C:\ComicAutomation\Oldestfirstcbz_sanitizer_progress.json"
# ─────────────────────────────────────────────

COMICINFO_TEMPLATE = """<ComicInfo
  xmlns:xsd="http://www.w3.org/2001/XMLSchema"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
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
# Compiled once at module level — avoids recompiling on every is_generic() call
_TITLE_OVERWRITE_RES = [
    re.compile(r"manga_chapter",          re.IGNORECASE),
    re.compile(r"^#\s*english",           re.IGNORECASE),  # "# English"
    re.compile(r"^#\s*chapter",           re.IGNORECASE),  # "# Chapter" or "# Chapter 12"
    re.compile(r"^chapter",               re.IGNORECASE),
    re.compile(r"^part\s+\d+",           re.IGNORECASE),
    re.compile(r"^doujinshi[\s_]chapter", re.IGNORECASE),  # "doujinshi Chapter"
    re.compile(r"^unknown[\s_]chapter",   re.IGNORECASE),  # "unknown chapter"
]
NUMBER_PREFIX_RE = re.compile(r"^\d+\s*-\s*", re.IGNORECASE)

# Matches gibberish/temp filenames that should be replaced with the directory name:
#   TEMP + hex string, bare long hex string, or UUID
GIBBERISH_RE = re.compile(
    r'^(?:TEMP[\s_-]*[0-9a-f]{8,}|[0-9a-f]{16,}'
    r'|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$',
    re.IGNORECASE
)

# Matches "Unknown Chapter N" — numbered form gets dir prefix, bare form gets replaced
UNKNOWN_CHAPTER_RE = re.compile(
    r'^unknown[\s_]chapter[\s_]*(\d[\d.]*)(.*?)$',
    re.IGNORECASE
)

# Matches "# Chapter N" — numbered form gets dir prefix, bare "# Chapter" gets replaced
HASH_CHAPTER_RE = re.compile(
    r'^#\s*chapter\s*(\d[\d.]*)(.*?)$',
    re.IGNORECASE
)

# Matches chapter-only stems that should be prepended with the directory name:
#   chapter 14 / ch. 12 / ch.12 / ch12 / chp. 5 / chap. 100 etc.
CHAPTER_ONLY_RE = re.compile(
    r'^(?:ch(?:ap(?:ter)?)?\.?\s*|chp\.?\s*)(\d[\d.]*)',
    re.IGNORECASE
)

# Matches "indexed chapter-dash" stems: "1. CHAPTER - 1" or "Chapter - 12"
# Requires either a leading index ("1. ") OR a dash after the keyword to avoid
# overlapping with CHAPTER_ONLY_RE (which handles "ch. 12", "chapter 14" etc.)
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
# Compiled regex patterns for the sanitize() hot path
_CJK_RE     = re.compile(
    r'[\u4E00-\u9FFF'        # CJK Unified Ideographs
    r'\u3400-\u4DBF'         # CJK Extension A
    r'\u3000-\u303F'         # CJK Symbols & Punctuation
    r'\u3040-\u309F'         # Hiragana
    r'\u30A0-\u30FF'         # Katakana
    r'\uFF00-\uFFEF'         # Halfwidth/Fullwidth Forms
    r'\U00020000-\U0002A6DF' # CJK Extension B
    r'\U0002A700-\U0002CEAF' # CJK Extensions C-F
    r']'
)
_BRACKET_RE    = re.compile(r'\[[^\]]*\]|\([^)]*\)')  # [..] and (..) groups in one pass
_STRAY_RE      = re.compile(r'[\[\]()]')                  # lone stray bracket/paren chars
_SPACES_RE     = re.compile(r' {2,}')                       # multiple consecutive spaces
_URL_RE        = re.compile(                                 # website URLs to strip from filenames/titles
    r'(?:https?://\S+)'
    r'|(?:www\.\S+)'
    r'|(?:\b[\w-]+\.(?:com|net|org|io|co|info|biz|tv|me|cc|us|uk|ca|au)(?:/\S*)?)',
    re.IGNORECASE
)
_SCAN_GROUP_RE = re.compile(                                 # scanlator/scan-group names to strip
    r'\b[\w-]*scans?\b|\b[\w-]*scanners?\b|\b[\w-]*scanlations?\b',
    re.IGNORECASE
)
_GCODE_RE = re.compile(r'[\s\-]*\bG\d{3,5}$')             # trailing G-code suffix (e.g. G1234, G12345)
_TRAILING_SLASH_RE = re.compile(r'[\s/]+$')                  # trailing slash(es)
_NON_LATIN_RE = re.compile(
    r'[^\u0000-\u024F'        # Basic + Extended Latin
    r'\u0370-\u03FF'          # Greek
    r'\u2000-\u206F'          # General Punctuation (en/em dash, ellipsis, curly quotes)
    r'\u2600-\u27BF'          # Misc Symbols + Dingbats
    r'\uFE00-\uFE0F'          # Variation Selectors (emoji presentation)
    r'\U0001F300-\U0001FAFF'  # Emoji / Supplemental Symbols and Pictographs
    r']+'
)                              # non-Latin / non-Greek / non-emoji characters to strip
_NUM_TOKEN_RE = re.compile(                              # leading-zero / .0 number normalisation
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
# Directory-name-only cleaning (applied AFTER sanitize())
_DIR_LEADING_HASH_RE  = re.compile(r'^#+\s*')               # leading # or ##
_DIR_TRAILING_HASH_RE = re.compile(r'\s*#+$')               # trailing # or ##
_DIR_TRAILING_STUB_RE = re.compile(                         # dangling trailing tokens with no number
    r'[\s_\-]*(?:part|v|ch(?:ap(?:ter)?)?)\s*$',
    re.IGNORECASE
)


def sanitize(text: str) -> str:
    """
    Shared sanitization pipeline applied to filenames, directory names,
    and XML fields (Title, Series). Steps in order:

      1. Decode XML/HTML entities (e.g. &amp; -> &, &apos; -> ', &lt; -> <)
      2. Remove website URLs (http://, www., bare domain.tld)
      3. Remove scanlator group names (words containing scan/scans/scanners/scanlation)
      4. Strip trailing G-code suffix (e.g. "Batman G1234" -> "Batman")
      5. Remove CJK (Asian language) characters  [regex, ~11x faster than char loop]
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
def load_progress() -> set:
    """
    Replay the append-only progress log and return the set of processed paths.
    Each line is a JSON object: first line is a header, rest are {"p": path} entries.
    Silently ignores malformed lines (partial writes from crashes).
    """
    paths: set[str] = set()
    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if i == 0:
                        continue  # header line — skip
                    if "p" in obj:
                        paths.add(obj["p"])
                except json.JSONDecodeError:
                    pass  # ignore truncated last line from a crash
        log.info(f"  Resumed: {len(paths)} file(s) already processed.")
    except FileNotFoundError:
        pass
    return paths


def save_progress(path: str) -> None:
    """
    Append a single processed path to the progress file as one JSON line.
    O(1) per file — no re-sorting or full rewrite of the growing set.
    """
    try:
        with open(PROGRESS_FILE, "a", encoding="utf-8") as f:
            json.dump({"p": path}, f, separators=(",", ":"))
            f.write("\n")
    except OSError as e:
        log.warning(f"  Could not save progress file: {e}")


def init_progress_file(started: str) -> None:
    """Write the header line to a fresh progress file."""
    try:
        os.makedirs(os.path.dirname(PROGRESS_FILE), exist_ok=True)
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump({"started": started, "v": 2}, f, separators=(",", ":"))
            f.write("\n")
    except OSError as e:
        log.warning(f"  Could not init progress file: {e}")


def clear_progress() -> None:
    """Delete the progress file on clean completion."""
    try:
        Path(PROGRESS_FILE).unlink(missing_ok=True)
        log.info("  Progress file cleared (run complete).")
    except OSError as e:
        log.warning(f"  Could not delete progress file: {e}")



def _fmt_num(n: float) -> str:
    """Format a float chapter number as a clean string (1.0 -> '1', 1.5 -> '1.5')."""
    return str(int(n)) if n == int(n) else str(n)

def extract_chapter(name: str) -> float | None:
    """Extract chapter number from a filename stem (alias for detection logic)."""
    m = _CHAPTER_NUMBER_RE.search(name)
    if m:
        val = next(g for g in m.groups() if g is not None)
        return float(val)
    return None

# ─────────────────────────────────────────────
# COMPILATION DETECTION
# Detects chapter numbers that are likely concatenations of two
# earlier chapter numbers (e.g. ch.12 in a series with ch.1 and ch.2)
# and renames the file to use a hyphenated range (ch.1-2).
# ─────────────────────────────────────────────

_CHAPTER_TOKEN_RE = re.compile(
    r'(ch(?:ap(?:ter)?)?p?\.?\s*)(\d[\d.]*)',
    re.IGNORECASE
)


def _detect_compilation_candidates(
    chapter_nums: list[float],
) -> list[tuple[float, float, float]]:
    """
    Given a sorted list of chapter numbers for a series, return a list of
    (suspect, start, end) tuples where 'suspect' is a chapter number that
    appears to be a compilation of chapters start through end.

    Detection criteria (all must be true):
      1. suspect is larger than all other chapter numbers
      2. The gap from the previous chapter to suspect is > 2x the median gap
         of the rest (flags an outlier jump)
      3. str(suspect) starts with str(start) and the remainder equals str(end),
         where end is a chapter number that exists in the series and is
         <= max(others) (the concat digits must represent real chapter numbers)
    """
    if len(chapter_nums) < 2:
        return []

    # Ensure all values are float so arithmetic never fails on mixed int/str types
    nums    = sorted(set(float(n) for n in chapter_nums))
    results = []

    for suspect in nums:
        others = [n for n in nums if n != suspect]
        if not others:
            continue

        # Must be the largest number in the series
        if suspect <= max(others):
            continue

        # Gap must be an unusual outlier
        gaps = [others[j + 1] - others[j] for j in range(len(others) - 1)]
        gap_to_suspect = suspect - max(others)
        if gaps:
            median_gap = statistics.median(gaps)
            if median_gap > 0 and gap_to_suspect <= 2 * median_gap:
                continue
            if median_gap == 0 and gap_to_suspect <= 2:
                continue
        # Single-chapter series: just require a gap > 2 to trigger
        elif gap_to_suspect <= 2:
            continue

        # Concatenation check
        suspect_str = _fmt_num(suspect)
        found_start = found_end = None
        for a in others:
            a_str = _fmt_num(a)
            if len(a_str) >= len(suspect_str):
                continue
            if not suspect_str.startswith(a_str):
                continue
            remainder = suspect_str[len(a_str):]
            if not remainder or not remainder.isdigit():
                continue
            rem_val = float(remainder)
            # The end chapter must be within the existing range
            if rem_val < 1 or rem_val > max(others):
                continue
            # Prefer the largest matching start (most specific)
            if found_start is None or a > found_start:
                found_start = a
                found_end   = rem_val

        if found_start is not None:
            results.append((suspect, found_start, found_end))

    return results


def _rename_stem_for_compilation(stem: str, start: float, end: float) -> str:
    """
    Replace the chapter number in a filename stem with a hyphenated range.
    e.g. "Batman Ch. 12" with start=1, end=2  →  "Batman Ch. 1-2"
         "One Piece ch15" with start=1, end=5 →  "One Piece ch1-5"
    """
    range_str = f"{_fmt_num(start)}-{_fmt_num(end)}"

    def _replacer(m: re.Match) -> str:
        return m.group(1) + range_str

    new_stem, n = _CHAPTER_TOKEN_RE.subn(_replacer, stem, count=1)
    return new_stem if n else f"{stem} {range_str}"


def _update_comicinfo_range(xml: str, start: float, end: float) -> tuple[str, bool]:
    """
    Update the <Number> tag in xml to "start-end" format and add/update
    a <Count> tag with the number of chapters in the range.
    Returns (new_xml, changed).
    """
    range_str = f"{_fmt_num(start)}-{_fmt_num(end)}"
    count_val  = str(int(end - start + 1))
    changed    = False

    # Update <Number>
    num_pat = re.compile(r"<Number>.*?</Number>", re.IGNORECASE | re.DOTALL)
    new_num_tag = f"<Number>{range_str}</Number>"
    if num_pat.search(xml):
        if num_pat.search(xml).group(0) != new_num_tag:
            xml     = num_pat.sub(new_num_tag, xml, count=1)
            changed = True
    else:
        xml     = xml.replace("</ComicInfo>", f"  {new_num_tag}\n</ComicInfo>")
        changed = True

    return xml, changed


def detect_and_fix_compilations(
    series_dir: Path,
    dry_run: bool = False,
    num_to_path: dict[float, Path] | None = None,
) -> int:
    """
    Detect chapter numbers that appear to be compilations (e.g. ch.12 in a
    series that has ch.1 and ch.2) and rename them to a hyphenated range
    (ch.1-2), updating ComicInfo.xml to match.

    num_to_path: optional pre-built {chapter_float: Path} mapping collected
    during the main processing loop — avoids a second glob + regex scan of
    the directory.  When None the map is built here from a fresh glob.

    Returns the number of files renamed.
    """
    if num_to_path is None:
        # Build from scratch — used when called standalone (e.g. dry-run)
        cbz_files = sorted(series_dir.glob("*.cbz"))
        if len(cbz_files) < 2:
            return 0
        num_to_path = {}
        for cbz in cbz_files:
            ch = extract_chapter(cbz.stem)
            if ch is not None:
                num_to_path[ch] = cbz

    if len(num_to_path) < 2:
        return 0

    candidates = _detect_compilation_candidates(list(num_to_path.keys()))
    if not candidates:
        return 0

    renamed = 0
    for suspect, start, end in candidates:
        cbz = num_to_path.get(suspect)
        if cbz is None or not cbz.exists():
            continue

        new_stem = _rename_stem_for_compilation(cbz.stem, start, end)
        new_name = new_stem + cbz.suffix
        new_path = cbz.parent / new_name

        log.info(
            f"    Compilation detected: '{cbz.name}' looks like "
            f"ch.{_fmt_num(start)}-{_fmt_num(end)} "
            f"(gap from ch.{_fmt_num(max(n for n in num_to_path if n != suspect))} "
            f"to ch.{_fmt_num(suspect)} is unusually large)"
        )

        if dry_run:
            log.info(f"    [DRY RUN] Would rename: '{cbz.name}' -> '{new_name}'")
            renamed += 1
            continue

        # Rename file
        if new_path != cbz:
            if new_path.exists():
                if cbz.stat().st_size > new_path.stat().st_size:
                    new_path.unlink()
                    cbz.rename(new_path)
                    log.info(f"    Renamed (replaced smaller): '{cbz.name}' -> '{new_name}'")
                else:
                    cbz.unlink()
                    log.info(f"    Discarded (collision, kept larger): '{cbz.name}'")
                    continue
            else:
                cbz.rename(new_path)
                log.info(f"    Renamed: '{cbz.name}' -> '{new_name}'")
        cbz = new_path

        # Update ComicInfo.xml
        _patch_comicinfo_for_range(cbz, start, end)
        renamed += 1

    return renamed


def _patch_comicinfo_for_range(cbz_path: Path, start: float, end: float) -> None:
    """
    Update <Number> to "start-end" in a CBZ's ComicInfo.xml.
    Reads the existing XML, applies _update_comicinfo_range, then delegates
    the atomic zip rewrite to _write_cbz_with_comicinfo — no duplicated write logic.
    """
    try:
        with zipfile.ZipFile(cbz_path, "r") as zin:
            nl      = {n.lower(): n for n in zin.namelist()}
            key     = next(
                (k for k in nl if os.path.basename(k).lower() == "comicinfo.xml"),
                None
            )
            if not key:
                log.info(f"    No ComicInfo.xml in {cbz_path.name} — skipping range patch.")
                return
            real_name = nl[key]
            xml       = zin.read(real_name).decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, OSError) as e:
        log.error(f"    Cannot read {cbz_path.name} for ComicInfo patch: {e}")
        return

    xml, changed = _update_comicinfo_range(xml, start, end)
    if not changed:
        log.info(f"    ComicInfo already correct for range {_fmt_num(start)}-{_fmt_num(end)}.")
        return

    log.info(
        f"    ComicInfo updated: Number={_fmt_num(start)}-{_fmt_num(end)}"
        f" in '{cbz_path.name}'"
    )
    _rewrite_comicinfo(cbz_path, real_name, xml)



# ─────────────────────────────────────────────
# DIRECTORY SANITIZING (no move)
# ─────────────────────────────────────────────
def sanitize_directory(dir_path: Path, processed: set, started: str) -> None:
    """
    Recursively find all immediate directories containing .cbz files,
    clean directory names, process all .cbz files inside each one.
    Nothing is moved or transferred.
    """
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

    total_comp_fixed = 0

    for comic_dir, cbz_files in sorted(cbz_dirs.items()):
        log.info(f"  Directory: {comic_dir.name} ({len(cbz_files)} file(s))")

        # Clean directory name BEFORE processing files so comicinfo
        # title logic compares against the final clean name
        clean_dir_name = clean_directory_name(comic_dir.name)
        if not clean_dir_name:
            log.warning(f"  Skipping rename: cleaning '{comic_dir.name}' produced an empty name.")
        elif clean_dir_name != comic_dir.name:
            new_dir_path = comic_dir.parent / clean_dir_name
            if new_dir_path.exists():
                log.info(
                    f"    Directory conflict: '{clean_dir_name}' already exists "
                    f"- merging '{comic_dir.name}' into it."
                )
                _merge_directories(comic_dir, new_dir_path)
                cbz_files = [new_dir_path / f.name for f in cbz_files if (new_dir_path / f.name).exists()]
            else:
                comic_dir.rename(new_dir_path)
                log.info(f"    Directory renamed: '{comic_dir.name}' -> '{clean_dir_name}'")
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

        # Accumulate chapter-number → path during the file loop so
        # detect_and_fix_compilations does not need a second glob pass.
        dir_num_to_path: dict[float, Path] = {}

        for cbz in cbz_files:
            if not cbz.exists():
                total_skipped += 1
                continue
            if str(cbz) in processed:
                log.info(f"    Already processed (skipping): {cbz.name}")
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
            # Mark as done immediately — survives crashes mid-batch
            processed.add(str(result_path))
            save_progress(str(result_path))  # O(1) append, not full rewrite
            total_processed += 1
            if result_path.name != original_name:
                total_renamed += 1
            # Collect chapter number using the final (post-rename) path
            ch = extract_chapter(result_path.stem)
            if ch is not None:
                dir_num_to_path[ch] = result_path

        # Detect and fix compilation chapters — pass the map we just built
        # so no second glob or regex scan is needed, and we use the correct
        # post-rename paths (fixes the stale-key bug when dirs were renamed)
        n = detect_and_fix_compilations(
            comic_dir, dry_run=False, num_to_path=dir_num_to_path or None
        )
        if n:
            log.info(f"    Fixed {n} compilation chapter(s).")
            total_comp_fixed += n

    if total_comp_fixed:
        log.info(f"  Fixed {total_comp_fixed} compilation chapter(s) across all directories.")

    log.info(
        f"  Batch complete — {total_processed} processed, "
        f"{total_renamed} renamed, {total_skipped} skipped."
    )


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
def main():
    scan_path = Path(SCAN_FOLDER)

    if not scan_path.exists():
        print(f"ERROR: Scan folder not found: {SCAN_FOLDER}")
        return

    # ── Determine resume / restart mode ──────────────────────────
    args    = sys.argv[1:]
    restart = "--restart" in args
    resume  = "--resume"  in args
    progress_exists = Path(PROGRESS_FILE).exists()

    if restart:
        processed = set()
        log.info("  --restart flag: ignoring any saved progress.")
    elif resume:
        processed = load_progress()
    elif progress_exists:
        # Progress file found but no flag — ask the user
        print()
        print("  A progress file was found from a previous run.")
        print("  [R] Resume from where it left off")
        print("  [S] Start over from the beginning")
        choice = input("  Choice (R/S): ").strip().upper()
        if choice == "R":
            processed = load_progress()
        else:
            processed = set()
            log.info("  Starting over — progress file reset.")
    else:
        processed = set()

    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Always write a fresh header so the file exists from the start
    init_progress_file(started)

    log.info("=" * 60)
    log.info("CBZ Sanitizer started")
    log.info(f"  Scanning : {SCAN_FOLDER}")
    log.info(f"  Log      : {LOG_FILE}")
    log.info(f"  Progress : {PROGRESS_FILE}")
    log.info("=" * 60)

    subdirs = sorted((d for d in scan_path.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime)

    if not subdirs:
        sanitize_directory(scan_path, processed, started)
    else:
        for subdir in subdirs:
            sanitize_directory(subdir, processed, started)

    log.info("=" * 60)
    log.info("CBZ Sanitizer complete.")
    log.info("=" * 60)
    clear_progress()


if __name__ == "__main__":
    main()
