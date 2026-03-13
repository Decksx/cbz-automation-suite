"""
CBZ File Watcher & Processor
Monitors a folder for incoming .cbz files inside subdirectories.
Processes ALL .cbz files in a directory first, then moves the
immediate comic directory to the configured destination.
"""

import os
import re
import gc
import json
import html
import time
import shutil
import fnmatch
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
LOG_FILE      = r"C:\git\ComicAutomation\cbz_watcher.log"
POLL_INTERVAL = 2    # seconds between stability checks
SETTLE_DELAY  = 5    # seconds of inactivity before processing a directory
MIN_AGE       = 300  # seconds a directory must exist before processing

ROUTING_FILE  = r"C:\git\ComicAutomation\routing.json"

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
TITLE_OVERWRITE_PATTERNS = [
    r"manga_chapter",
    r"#\s*english",
    r"^chapter",
    r"^part\s+\d+",
    r"doujinshi_chapter",
]
NUMBER_PREFIX_RE = re.compile(r"^\d+\s*-\s*", re.IGNORECASE)

# Routing state — loaded from ROUTING_FILE at startup
_routing_destinations: dict[str, str] = {}   # short-name -> full path
_routing_rules: list[dict] = []               # ordered rules list
_routing_default: str = ''                    # default destination path

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
_URL_RE           = re.compile(
    r'(?:https?://\S+)|(?:www\.\S+)' +
    r'|(?:\b[\w-]+\.(?:com|net|org|io|co|info|biz|tv|me|cc|us|uk|ca|au)(?:/\S*)?)'
    , re.IGNORECASE)
_SCAN_GROUP_RE    = re.compile(
    r'\b[\w-]*scans?\b|\b[\w-]*scanners?\b|\b[\w-]*scanlations?\b', re.IGNORECASE)
_GCODE_RE         = re.compile(r'[\s\-]*\bG\d{3,5}$')
_TRAILING_SLASH_RE = re.compile(r'[\s/]+$')
_NON_LATIN_RE     = re.compile(
    r'[^\u0000-\u024F'
    r'\u0370-\u03FF'
    r'\u2000-\u206F'
    r'\u2600-\u27BF'
    r'\uFE00-\uFE0F'
    r'\U0001F300-\U0001FAFF'
    r']+'
)
_NUM_TOKEN_RE     = re.compile(
    r'((?:ch(?:ap(?:ter)?)?p?|issue|ep(?:isode)?|vol(?:ume)?|v(?=\d))\.?\s*)(\d[\d.]*)',
    re.IGNORECASE
)
_DIR_LEADING_HASH_RE  = re.compile(r'^#+\s*')
_DIR_TRAILING_HASH_RE = re.compile(r'\s*#+$')
_DIR_TRAILING_STUB_RE = re.compile(
    r'[\s_\-]*(?:part|v|ch(?:ap(?:ter)?)?)\s*$', re.IGNORECASE)


def sanitize(text: str) -> str:
    """
    Shared sanitization pipeline for filenames, directory names, and XML fields.
    Order: entities → URLs → scan-groups → trailing-slash → G-code → non-Latin
           → brackets → stray brackets → underscores → collapse whitespace
    """
    text = html.unescape(text)
    text = _URL_RE.sub("", text)
    text = _SCAN_GROUP_RE.sub("", text)
    text = _TRAILING_SLASH_RE.sub("", text)
    text = _GCODE_RE.sub("", text)
    text = _NON_LATIN_RE.sub("", text)
    text = _BRACKET_RE.sub("", text)
    text = _STRAY_RE.sub("", text)
    text = text.replace("_", " ")
    return _SPACES_RE.sub(" ", text).strip()


def clean_filename(name: str) -> str:
    """Sanitize a .cbz filename stem, preserving the extension."""
    stem = Path(name).stem
    ext  = Path(name).suffix
    return sanitize(stem) + ext


def clean_directory_name(name: str) -> str:
    """Sanitize a directory name with extra directory-specific cleaning."""
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
    return any(re.search(p, text, re.IGNORECASE) for p in TITLE_OVERWRITE_PATTERNS)


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

            gc.collect()
            time.sleep(0.5)

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

            gc.collect()
            time.sleep(0.5)

            # Step 3: Atomic swap — rename avoids unlink lock issues on Windows
            bak_path = cbz_path.with_suffix(".bak.cbz")
            cbz_path.rename(bak_path)
            tmp_path.rename(cbz_path)
            bak_path.unlink(missing_ok=True)
            log.info(f"    comicinfo.xml {action} successfully.")
            return

        except OSError as e:
            log.warning(f"    File locked (attempt {attempt + 1}/5), retrying in 5s... ({e})")
            if tmp_path.exists():
                tmp_path.unlink()
            time.sleep(5)
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


def process_comicinfo(cbz_path: Path) -> None:
    """
    Inspect the .cbz for comicinfo.xml.
    - Found:   check/fix Title and Series tags.
    - Missing: inject a fresh comicinfo.xml from template.
    """
    parent_dir = cbz_path.parent.name

    for attempt in range(5):
        try:
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

            gc.collect()
            time.sleep(0.2)

            if has_xml:
                title_match  = re.search(r"<Title>(.*?)</Title>",   xml_text, re.IGNORECASE | re.DOTALL)
                series_match = re.search(r"<Series>(.*?)</Series>", xml_text, re.IGNORECASE | re.DOTALL)
                title_value  = clean_xml_field(title_match.group(1).strip())  if title_match  else ""
                series_value = clean_xml_field(series_match.group(1).strip()) if series_match else ""

                # Clean brackets from Series tag if needed
                if series_match and series_value != series_match.group(1).strip():
                    xml_text = re.sub(
                        r"<Series>.*?</Series>",
                        f"<Series>{series_value}</Series>",
                        xml_text, count=1, flags=re.IGNORECASE | re.DOTALL
                    )
                    log.info(f"    Series cleaned: '{series_match.group(1).strip()}' -> '{series_value}'")

                # Strip leading "# - " prefix (e.g. "3 - Batman" -> "Batman")
                title_value   = NUMBER_PREFIX_RE.sub("", title_value).strip()
                filename_stem = NUMBER_PREFIX_RE.sub("", cbz_path.stem).strip()

                title_generic    = is_generic(title_value)
                filename_generic = is_generic(filename_stem)

                # ── Title resolution logic ──────────────────────────────
                # Title == dir  + filename custom   → use filename
                # Title == dir  + filename generic  → already correct
                # Title generic + filename custom   → use filename
                # Title generic + filename generic  → use parent dir
                # Title custom  (any filename)      → leave unchanged
                if title_value == parent_dir and not filename_generic:
                    new_title = filename_stem
                    log.info(f"    Title matches dir but filename='{filename_stem}' is custom - using filename.")
                elif title_value == parent_dir:
                    log.info(f"    comicinfo.xml OK - Title matches parent dir '{parent_dir}'")
                    return
                elif title_generic and not filename_generic:
                    new_title = filename_stem
                    log.info(f"    Title='{title_value}' is generic, filename is custom - using filename.")
                elif title_generic and filename_generic:
                    new_title = parent_dir
                    log.info(f"    Both title and filename are generic - using parent dir '{new_title}'.")
                else:
                    log.info(f"    Title='{title_value}' is custom - leaving unchanged.")
                    return

                xml_text = re.sub(
                    r"<Title>.*?</Title>",
                    f"<Title>{new_title}</Title>",
                    xml_text, count=1, flags=re.IGNORECASE | re.DOTALL
                )
                _rewrite_comicinfo(cbz_path, real_name, xml_text)
                return

            else:
                log.info(f"    No comicinfo.xml found - injecting template.")
                _inject_comicinfo(cbz_path)
                return

        except OSError:
            log.warning(f"    File locked reading zip (attempt {attempt + 1}/5), retrying in 5s...")
            time.sleep(5)
        except zipfile.BadZipFile:
            log.error(f"    Cannot open {cbz_path.name} - bad zip file, skipping.")
            return

    log.error(f"    Gave up reading {cbz_path.name} after 5 attempts.")


# ─────────────────────────────────────────────
# FILE STABILITY CHECK
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
def process_cbz_file(cbz_path: Path, override_name: str | None = None) -> Path:
    """
    Stability check → clean filename → process comicinfo.
    Returns the final (possibly renamed) path. Does NOT move the file.
    If override_name is given it is used as the filename instead of the cleaned name.
    """
    log.info(f"  Processing: {cbz_path.name}")

    if not wait_for_file_stable(cbz_path):
        log.warning(f"    Skipping unstable file: {cbz_path.name}")
        return cbz_path

    # Use override_name directly if supplied (avoids redundant clean_filename call)
    if override_name is not None:
        new_name = override_name
    else:
        new_name = clean_filename(cbz_path.name)
    if new_name != cbz_path.name:
        new_path = cbz_path.parent / new_name
        if new_path.exists():
            log.warning(
                f"    Rename skipped: target already exists '{new_name}' "
                f"(keeping original '{cbz_path.name}')"
            )
        else:
            cbz_path.rename(new_path)
            log.info(f"    Renamed: '{cbz_path.name}' -> '{new_name}'")
            cbz_path = new_path
    else:
        log.info(f"    Filename unchanged: '{cbz_path.name}'")

    process_comicinfo(cbz_path)
    return cbz_path


# ─────────────────────────────────────────────
# DIRECTORY MERGE (keep largest on conflict)
# ─────────────────────────────────────────────
def _merge_directories(src_dir: Path, dest_dir: Path) -> None:
    """Recursively merge src_dir into dest_dir, keeping the larger file on conflict."""
    for src_item in src_dir.rglob("*"):
        relative  = src_item.relative_to(src_dir)
        dest_item = dest_dir / relative

        if src_item.is_dir():
            dest_item.mkdir(parents=True, exist_ok=True)
            continue

        if dest_item.exists():
            src_size  = src_item.stat().st_size
            dest_size = dest_item.stat().st_size
            if src_size > dest_size:
                log.info(f"    Conflict '{relative}': incoming ({src_size:,} B) > existing ({dest_size:,} B) - replacing.")
                dest_item.unlink()
                shutil.move(str(src_item), str(dest_item))
            else:
                log.info(f"    Conflict '{relative}': existing ({dest_size:,} B) >= incoming ({src_size:,} B) - keeping existing.")
                src_item.unlink()
        else:
            dest_item.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_item), str(dest_item))


# ─────────────────────────────────────────────
# ROUTING & DIRECTORY MOVE
# ─────────────────────────────────────────────
def _load_routing() -> None:
    """Load routing.json and populate module-level routing state."""
    global _routing_destinations, _routing_rules, _routing_default
    try:
        with open(ROUTING_FILE, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        _routing_destinations = cfg.get('destinations', {})
        _routing_rules        = cfg.get('rules', [])
        default_key           = cfg.get('default', '')
        _routing_default      = _routing_destinations.get(default_key, '')
        if not _routing_default:
            log.warning(f"  routing.json: default key '{default_key}' not found in destinations.")
        # Ensure all destination directories exist
        for dest_path in _routing_destinations.values():
            os.makedirs(dest_path, exist_ok=True)
        log.info(f"  Routing   : {len(_routing_rules)} rule(s) loaded from {ROUTING_FILE}")
    except FileNotFoundError:
        log.warning(f"  routing.json not found at {ROUTING_FILE} — all files will go to default dest.")
        _routing_default = ''
    except Exception as e:
        log.error(f"  Failed to load routing.json: {e}")
        _routing_default = ''


def _resolve_dest(comic_dir: Path) -> str:
    """
    Walk up from comic_dir to find the immediate child of WATCH_FOLDER.
    Evaluate routing rules top-to-bottom, first match wins.
    Falls back to _routing_default if no rule matches.
    """
    watch = Path(WATCH_FOLDER)
    source_dir = comic_dir
    for candidate in [comic_dir] + list(comic_dir.parents):
        if candidate.parent == watch:
            source_dir = candidate
            break

    source_name = source_dir.name
    cbz_name    = comic_dir.name

    for rule in _routing_rules:
        match_on = rule.get('match', 'source')
        pattern  = rule.get('pattern', '')
        dest_key = rule.get('dest', '')

        if match_on == 'source':
            subject = source_name
        elif match_on == 'title':
            subject = cbz_name
        else:
            continue

        if fnmatch.fnmatch(subject.lower(), pattern.lower()):
            dest_path = _routing_destinations.get(dest_key, _routing_default)
            log.info(f"  Routing '{source_name}' matched rule '{pattern}' -> {dest_path}")
            return dest_path

    return _routing_default or ''



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

    # Load routing config from JSON
    _load_routing()

    log.info("=" * 60)
    log.info("CBZ Watcher started")
    log.info(f"  Watching : {WATCH_FOLDER}")
    log.info(f"  Routing  : {ROUTING_FILE}")
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
