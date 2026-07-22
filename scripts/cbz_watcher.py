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
import time
import shutil
import fnmatch
import zipfile
import logging
import threading
import xml.etree.ElementTree as ET
from dataclasses import replace as _dataclasses_replace
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from logging.handlers import RotatingFileHandler as _RotatingFileHandler

try:
    from scripts.cbz_core import (
        ParsedComicName,
        clean_directory_name,
        clean_filename,
        extract_chapter_number,
        extract_trailing_bare_number,
        extract_volume_number,
        normalise_archive_key,
        parse_comic_name,
        series_base_name,
        update_comicinfo_xml,
    )
except ModuleNotFoundError:
    from cbz_core import (  # type: ignore[no-redef]
        ParsedComicName,
        clean_directory_name,
        clean_filename,
        extract_chapter_number,
        extract_trailing_bare_number,
        extract_volume_number,
        normalise_archive_key,
        parse_comic_name,
        series_base_name,
        update_comicinfo_xml,
    )

# ─────────────────────────────────────────────
# CONFIGURATION — edit these as needed
# ─────────────────────────────────────────────
WATCH_FOLDER  = r"C:\Temp\Mega\Mega Uploads\book2"
REPO_ROOT     = Path(__file__).resolve().parents[1]
LOG_FILE      = REPO_ROOT / "Logs" / "cbz_watcher.log"
POLL_INTERVAL = 2    # seconds between stability checks
SETTLE_DELAY  = 5    # seconds of inactivity before processing a directory
MIN_AGE       = 300  # seconds a directory must exist before processing

ROUTING_FILE  = REPO_ROOT / "routing.json"

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

_MARKER_WORDS_RE = re.compile(r"\b(?:uncensored|decensored)\b", re.IGNORECASE)


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
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
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


class ProgressReporter:
    """Emit machine-readable progress lines for GUI launchers."""

    def __init__(self, total: int, label: str = "files") -> None:
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
            zip_entries: list[tuple] = []
            with zipfile.ZipFile(cbz_path, "r") as zin:
                for item in zin.infolist():
                    zip_entries.append((item, zin.read(item.filename)))

            gc.collect()
            time.sleep(0.5)

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


def process_comicinfo(cbz_path: Path, parsed: object) -> None:
    """
    Read ComicInfo.xml from *cbz_path*, delegate all field update decisions
    to ``update_comicinfo_xml()``, then write back via ``_write_cbz_with_comicinfo()``.

    If no ComicInfo.xml exists the watcher COMICINFO_TEMPLATE is injected first
    so that Komga-specific namespace tags are preserved, then updated with
    parsed field values.

    Retry/locking logic is watcher-specific and intentionally kept here.
    """
    for attempt in range(5):
        try:
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

            # Use COMICINFO_TEMPLATE as the base when no existing XML is found.
            # This preserves the Komga-specific namespace declarations.
            if not has_xml:
                log.info(f"    No comicinfo.xml found - injecting template.")
                xml_text = COMICINFO_TEMPLATE

            new_xml, changed = update_comicinfo_xml(xml_text, parsed)

            if not changed and has_xml:
                log.info(f"    comicinfo.xml OK - no changes needed.")
                return

            if has_xml:
                _rewrite_comicinfo(cbz_path, real_name, new_xml)
            else:
                _write_cbz_with_comicinfo(cbz_path, new_xml)
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
    """Wait until a file has finished being written, tolerant of SMB stat jitter.

    A file that is still being copied only ever *grows*, so the real signal we
    need is "size has stopped increasing". Network shares (SMB to the Unraid box)
    can return slightly different cached sizes on consecutive ``stat()`` calls
    even for a completely static file — the old "reset the counter on any change"
    logic would then flap forever and wrongly report a finished file as unstable
    (the every-other-file skips seen in the logs).

    New approach: poll the size into a small rolling window. The file is
    considered stable once the window is full and the size has not *grown* across
    it (max == min, or only shrank, ignoring transient equal-ish readings). This
    catches genuine in-progress copies (size climbing) while not tripping on
    harmless cache jitter for files already fully present.
    """
    window: list[int] = []
    needed = max(2, stable_seconds)
    max_polls = 30
    # Byte-level differences between consecutive SMB stat() calls are cache
    # jitter, not a real copy in progress. A genuine in-progress copy grows by
    # far more than this between polls. Only growth beyond this threshold across
    # the window is treated as "still copying".
    growth_threshold = 64 * 1024  # 64 KiB

    for _ in range(max_polls):
        try:
            current = path.stat().st_size
        except FileNotFoundError:
            return False
        except OSError as exc:
            # Transient network error reading metadata — wait and retry rather
            # than declaring the file unstable.
            log.debug("    stat() retry on %s: %s", path.name, exc)
            time.sleep(POLL_INTERVAL)
            continue

        window.append(current)
        if len(window) > needed:
            window.pop(0)

        if len(window) >= needed:
            spread = max(window) - min(window)
            growing = window[-1] - window[0]
            # Stable unless the size is still climbing meaningfully across the
            # whole window (an active copy). Small non-monotonic jitter (spread
            # under the threshold) or any non-increase is treated as settled.
            if spread < growth_threshold or growing < growth_threshold:
                return True

        time.sleep(POLL_INTERVAL)

    log.warning(f"    File did not stabilise in time: {path.name}")
    return False


# ─────────────────────────────────────────────
# SINGLE CBZ PROCESSING
# ─────────────────────────────────────────────
def process_cbz_file(
    cbz_path: Path, override_name: str | None = None
) -> tuple[Path, ParsedComicName | None]:
    """
    Stability check → parse_comic_name → rename → update ComicInfo.
    Returns (final (possibly renamed) path, the ParsedComicName used — or None
    if the file was skipped for being unstable). Does NOT move the file.

    If override_name is given it is used as the filename instead of the
    normalised name from parse_comic_name (empty-stem fallback path).
    """
    log.info(f"  Processing: {cbz_path.name}")

    if not wait_for_file_stable(cbz_path):
        log.warning(f"    Skipping unstable file: {cbz_path.name}")
        return cbz_path, None

    # parse_comic_name runs the full normalisation pipeline:
    #   sanitize → strip leading nums → normalize_stem → normalise_number_tokens
    # This is the single authoritative source for filename and ComicInfo fields.
    parsed   = parse_comic_name(cbz_path)
    new_name = override_name if override_name is not None else parsed.filename

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

    process_comicinfo(cbz_path, parsed)
    return cbz_path, parsed


# ─────────────────────────────────────────────
# DIRECTORY MERGE (keep largest on conflict)
# ─────────────────────────────────────────────
def _find_archive_collision(target_dir: Path, stem: str) -> Path | None:
    """Return an existing archive in *target_dir* whose normalised key matches *stem*.

    Treats files that differ only by spacing or punctuation as the same book
    (e.g. "Series Ch.1" and "Series Ch. 1"), so the merge keeps one copy instead
    of letting both cosmetic variants survive. Returns None when none match.
    """
    if not target_dir.is_dir():
        return None
    key = normalise_archive_key(stem)
    for existing in target_dir.iterdir():
        if existing.is_file() and existing.suffix.lower() in {".cbz", ".cbr"}:
            if normalise_archive_key(existing.stem) == key:
                return existing
    return None


# A stem is a "placeholder" name — carrying no information of its own — when
# it is nothing but an optional short prefix (e.g. a group/circle tag) plus a
# bare chapter/part/issue/volume marker with NO number attached, such as the
# "volvox_Chapter.cbz" convention some scanlation tools reuse verbatim for
# every release. Filenames like this give every chapter of a series the exact
# same on-disk name, which is what let two different issues collide and lose
# data during a directory merge. Anchored on both ends and capped in length so
# genuinely distinct titles (e.g. "The Great Adventure") are never matched.
_PLACEHOLDER_STEM_RE = re.compile(
    r"^(?:[^\d]{1,40}\s+)?(?:chapter|chap|part|issue|vol(?:ume)?)\.?$",
    re.IGNORECASE,
)


def _apply_fallback_naming(
    parsed_by_path: dict[Path, ParsedComicName],
    series_name: str,
    dir_number: str | None,
) -> None:
    """Rename placeholder-named files (see ``_PLACEHOLDER_STEM_RE``) to the
    resolved series name, appending the chapter number implied by the
    enclosing directory (``dir_number``, from ``_resolve_series_dir_name``)
    when one was found.

    Without this, files that arrive with an uninformative, group-generic name
    and no chapter number of their own (e.g. every release from a given
    uploader named identically) end up with identical filenames across
    different issues, silently colliding — and one being discarded — when
    their directories are later merged into the same series folder.

    Mutates *parsed_by_path* in place so later lookups by the caller see the
    renamed path.
    """
    for cbz, parsed in list(parsed_by_path.items()):
        if not cbz.exists() or parsed.chapter or parsed.volume:
            continue
        if not _PLACEHOLDER_STEM_RE.match(parsed.stem):
            continue

        new_stem = f"{series_name} Ch. {dir_number}" if dir_number else series_name
        new_name = new_stem + cbz.suffix
        if new_name == cbz.name:
            continue

        new_path = cbz.parent / new_name
        if new_path.exists():
            log.warning(
                f"    Fallback rename skipped: target already exists '{new_name}' "
                f"(keeping placeholder name '{cbz.name}')"
            )
            continue

        cbz.rename(new_path)
        log.info(f"    Renamed (placeholder fallback): '{cbz.name}' -> '{new_name}'")

        patched = _dataclasses_replace(
            parsed,
            chapter=dir_number or parsed.chapter,
            filename=new_name,
            stem=new_stem,
        )
        process_comicinfo(new_path, patched)
        del parsed_by_path[cbz]
        parsed_by_path[new_path] = patched


def _backfill_chapter_one(dest_dir: Path, series_name: str) -> None:
    """When a chapter-numbered file is about to merge into *dest_dir*, retroactively
    label a lone bare-named sibling (e.g. an earlier chapter that arrived before any
    numbering was known, see ``_apply_fallback_naming``) as "Ch. 1" so both chapters
    end up with distinct, stable names instead of one being liable to collide with —
    and be silently discarded in favour of — the other.
    """
    if not dest_dir.is_dir():
        return
    bare_key = normalise_archive_key(series_name)
    for existing in dest_dir.iterdir():
        if not (existing.is_file() and existing.suffix.lower() in {".cbz", ".cbr"}):
            continue
        if normalise_archive_key(existing.stem) != bare_key:
            continue
        target = existing.with_name(f"{series_name} Ch. 1{existing.suffix}")
        if target.exists():
            return
        existing.rename(target)
        log.info(f"  Backfilled chapter number: '{existing.name}' -> '{target.name}'")
        return


def _merge_directories(src_dir: Path, dest_dir: Path) -> None:
    """Recursively merge src_dir into dest_dir, keeping the larger file on conflict."""
    for src_item in src_dir.rglob("*"):
        relative  = src_item.relative_to(src_dir)
        dest_item = dest_dir / relative

        if src_item.is_dir():
            dest_item.mkdir(parents=True, exist_ok=True)
            continue

        # When the exact name is free, still treat a file that normalises to the
        # same key (differs only by spacing/punctuation) as a conflict so the two
        # cosmetic variants don't both land in the destination.
        if not dest_item.exists() and src_item.suffix.lower() in {".cbz", ".cbr"}:
            match = _find_archive_collision(dest_item.parent, src_item.stem)
            if match is not None:
                dest_item = match

        if dest_item.exists():
            src_size  = src_item.stat().st_size
            dest_size = dest_item.stat().st_size
            if src_size > dest_size:
                log.warning(f"    Conflict '{relative}': incoming ({src_size:,} B) > existing ({dest_size:,} B) - replacing (existing file discarded).")
                dest_item.unlink()
                shutil.move(str(src_item), str(dest_item))
            else:
                log.warning(f"    Conflict '{relative}': existing ({dest_size:,} B) >= incoming ({src_size:,} B) - keeping existing (incoming file discarded).")
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


_SERIES_KEY_PUNCT_RE = re.compile(r"[^\w\s]")
_SERIES_KEY_SPACE_RE = re.compile(r"\s+")


def _series_key(name: str) -> str:
    """Lowercase, strip punctuation and censorship markers for series-folder comparison."""
    name = _MARKER_WORDS_RE.sub("", name)
    name = _SERIES_KEY_PUNCT_RE.sub(" ", name.lower())
    return _SERIES_KEY_SPACE_RE.sub(" ", name).strip()


def _read_comicinfo_series(cbz_path: Path) -> str | None:
    """Best-effort read of the <Series> field from a CBZ's ComicInfo.xml."""
    try:
        with zipfile.ZipFile(cbz_path, "r") as zf:
            names = {n.lower(): n for n in zf.namelist()}
            key = next((k for k in names if os.path.basename(k).lower() == "comicinfo.xml"), None)
            if not key:
                return None
            root = ET.fromstring(zf.read(names[key]).decode("utf-8", errors="replace"))
            el = root.find("Series")
            return (el.text or "").strip() if el is not None else None
    except (zipfile.BadZipFile, OSError, ET.ParseError):
        return None


def _find_existing_series_dir(series_name: str, dest_folder: str) -> Path | None:
    """Return an existing directory in *dest_folder* whose name matches *series_name*
    after normalisation (case/space/punctuation-insensitive), else None."""
    key = _series_key(series_name)
    if not key:
        return None
    dest = Path(dest_folder)
    if not dest.is_dir():
        return None
    for candidate in dest.iterdir():
        if candidate.is_dir() and _series_key(candidate.name) == key:
            return candidate
    return None


def _resolve_series_dir_name(
    comic_dir: Path, cbz_files: list[Path], dest_folder: str
) -> tuple[str, str | None]:
    """Decide which *series* folder a processed comic directory should land in.

    The watcher's premise is "the folder is the series", but incoming archives
    frequently arrive in per-chapter folders ("Berserk Ch. 4", "Berserk 5"),
    which would otherwise each become a separate series directory. This resolves
    them to a single series folder:

      1. Strip keyword-qualified trailing tokens (Ch./Vol./Episode/Issue/v3)
         unconditionally — these are never part of a title.
      2. For a *bare* trailing number ("Berserk 4"), only strip it when there is
         corroborating evidence, so titular numbers ("Area 88") are preserved:
           - an existing series folder in the destination already matches, or
           - the files' ComicInfo <Series> agrees with the stripped base.
      3. Otherwise keep the cleaned folder name as-is.

    Returns ``(series_name, dir_number)``. ``dir_number`` is the chapter/volume
    number recovered from the folder name whenever a trailing token was
    actually stripped (cases 1 and the corroborated branches of case 2), or
    ``None`` when the folder name was left untouched. Callers use this to give
    placeholder-named archives inside the folder a unique, traceable filename
    (see ``_apply_fallback_naming``) instead of leaving them all sharing one
    generic name.
    """
    cleaned = clean_directory_name(comic_dir.name) or comic_dir.name

    kw_base = series_base_name(cleaned, bare_numbers=False)
    if kw_base:
        dir_number = extract_chapter_number(cleaned) or extract_volume_number(cleaned)
        return kw_base, dir_number

    bare_base = series_base_name(cleaned, bare_numbers=True)
    if not bare_base:
        return cleaned, None

    dir_number = extract_trailing_bare_number(cleaned)

    # Evidence 1: a destination series folder already matches the stripped base.
    if _find_existing_series_dir(bare_base, dest_folder) is not None:
        return bare_base, dir_number

    # Evidence 2: the archives' ComicInfo <Series> agrees with the stripped base.
    base_key = _series_key(bare_base)
    for cbz in cbz_files:
        if not cbz.exists():
            continue
        meta_series = _read_comicinfo_series(cbz)
        if meta_series and _series_key(meta_series) == base_key:
            return bare_base, dir_number

    # No corroboration — leave a possibly-titular trailing number alone.
    return cleaned, None


def _normalise_for_censorship_match(name: str) -> str:
    """Strip censorship markers and normalize for sibling detection.

    Only strips marker words and empty parens — NOT general punctuation — so
    'Series!' and 'Series' are not falsely treated as censorship variants.
    """
    s = _MARKER_WORDS_RE.sub("", name)
    s = re.sub(r"\(\s*\)", "", s)
    return re.sub(r"\s+", " ", s).strip(" -_").lower()


def _find_censorship_sibling(dir_name: str, dest_folder: str) -> Path | None:
    """Return the first directory in dest_folder whose name matches dir_name after stripping
    censorship markers, or None if no such sibling exists."""
    incoming_key = _normalise_for_censorship_match(dir_name)
    if not incoming_key:
        return None
    dest = Path(dest_folder)
    if not dest.is_dir():
        return None
    for candidate in dest.iterdir():
        if candidate.is_dir() and candidate.name != dir_name:
            if _normalise_for_censorship_match(candidate.name) == incoming_key:
                return candidate
    return None


def _move_cbz_dir(
    dir_path: Path,
    dest_folder: str,
    target_name: str | None = None,
    chapter_number: str | None = None,
) -> None:
    """Move a processed comic directory to dest_folder, merging if it already exists.

    *target_name* overrides the destination folder name (defaults to the source
    directory's own name). This lets the caller route per-chapter folders such
    as "Berserk Ch. 4" into a single series folder ("Berserk") instead of
    creating a new directory per chapter.

    *chapter_number* is the chapter/volume number recovered from the source
    folder name (see ``_resolve_series_dir_name``), if any. When merging into
    an existing series folder, it signals that this incoming batch is itself a
    numbered chapter — triggering ``_backfill_chapter_one`` so a lone
    bare-named sibling already in the destination gets retroactively labelled
    "Ch. 1" instead of remaining ambiguous alongside the new numbered file.
    """
    if not dir_path.exists():
        log.warning(f"  Skipping move: '{dir_path.name}' no longer exists (already moved by another thread).")
        return

    folder_name = target_name or dir_path.name
    dest_dir = Path(dest_folder) / folder_name

    # If the exact series folder doesn't exist, look for one that matches after
    # normalisation (case/spacing/punctuation) so cosmetic differences don't
    # spawn a duplicate series directory.
    if not dest_dir.exists():
        existing = _find_existing_series_dir(folder_name, dest_folder)
        if existing is not None:
            log.info(f"  Series match: routing '{dir_path.name}' into existing '{existing.name}'")
            dest_dir = existing

    # If still nothing, check whether a censorship variant (e.g. a folder
    # with/without "(Uncensored)") already lives in the destination.  Merge into
    # it so the two flavours land in a single folder rather than side-by-side.
    if not dest_dir.exists():
        sibling = _find_censorship_sibling(folder_name, dest_folder)
        if sibling:
            log.info(
                f"  Censorship variant detected: '{folder_name}' -> merging into '{sibling.name}'"
            )
            dest_dir = sibling

    log.info(f"  Moving '{dir_path.name}' -> {dest_dir}")

    try:
        os.makedirs(dest_folder, exist_ok=True)

        if dest_dir.exists():
            if chapter_number:
                _backfill_chapter_one(dest_dir, folder_name)
            log.info(f"  Destination exists - merging, keeping larger files on conflict.")
            _merge_directories(dir_path, dest_dir)
            if dir_path.exists():
                shutil.rmtree(dir_path, ignore_errors=True)
            log.info(f"  Merge complete.")
        else:
            try:
                shutil.move(str(dir_path), str(dest_dir))
                log.info(f"  Moved successfully.")
            except (OSError, shutil.Error):
                # WinError 183: dest was created by a concurrent thread between
                # our exists() check and the move — fall back to merge.
                if dest_dir.exists():
                    if chapter_number:
                        _backfill_chapter_one(dest_dir, folder_name)
                    log.info(f"  Race on move — destination appeared, falling back to merge.")
                    _merge_directories(dir_path, dest_dir)
                    if dir_path.exists():
                        shutil.rmtree(dir_path, ignore_errors=True)
                    log.info(f"  Merge complete.")
                else:
                    raise

    except Exception as e:
        log.error(f"  Failed to move directory '{dir_path.name}': {e}")


def _move_loose_files(files: list[Path], dest_folder: str, series_name: str) -> None:
    """Move individual .cbz files — not their parent directory — into
    ``dest_folder/series_name``, applying the same keep-the-larger-on-conflict
    policy as ``_merge_directories`` on a per-file basis.

    Used for .cbz files that sit directly in a directory which also contains
    other subdirectories that are themselves comic directories (each with
    their own .cbz files). Moving the shared parent directory wholesale would
    sweep those unrelated subdirectories along with it — see the caller in
    ``_process_and_move_directory_inner``.
    """
    existing = _find_existing_series_dir(series_name, dest_folder)
    dest_dir = existing if existing is not None else Path(dest_folder) / series_name
    dest_dir.mkdir(parents=True, exist_ok=True)

    moved = 0
    for src in files:
        if not src.exists():
            continue

        dest_item = dest_dir / src.name
        if not dest_item.exists():
            match = _find_archive_collision(dest_dir, src.stem)
            if match is not None:
                dest_item = match

        if dest_item.exists():
            src_size  = src.stat().st_size
            dest_size = dest_item.stat().st_size
            if src_size > dest_size:
                log.warning(
                    f"    Conflict '{src.name}': incoming ({src_size:,} B) > "
                    f"existing ({dest_size:,} B) - replacing (existing file discarded)."
                )
                dest_item.unlink()
                shutil.move(str(src), str(dest_item))
            else:
                log.warning(
                    f"    Conflict '{src.name}': existing ({dest_size:,} B) >= "
                    f"incoming ({src_size:,} B) - keeping existing (incoming file discarded)."
                )
                src.unlink()
        else:
            shutil.move(str(src), str(dest_item))
        moved += 1

    log.info(f"  Moved {moved} loose file(s) -> {dest_dir}")


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
    # Clean the top-level watched directory name first.
    clean_top    = clean_directory_name(dir_path.name)
    old_dir_path = dir_path  # keep original for lock update
    if clean_top and clean_top != dir_path.name:
        new_top = dir_path.parent / clean_top
        if new_top.exists():
            log.warning(
                f"  Could not rename top-level dir '{dir_path.name}': "
                f"target '{clean_top}' already exists — continuing with original name."
            )
        else:
            try:
                dir_path.rename(new_top)
                log.info(f"  Directory renamed: '{dir_path.name}' -> '{clean_top}'")
                dir_path = new_top
            except OSError as e:
                log.warning(f"  Could not rename top-level dir '{dir_path.name}': {e}")
    # Update the processing-lock entry to use the new path so watchdog events
    # fired by the rename (which reference the new path) are still suppressed.
    with _processing_dirs_lock:
        _processing_dirs.discard(old_dir_path)
        _processing_dirs.add(dir_path)

    log.info("=" * 60)
    log.info(f"Scanning: {dir_path.name}")

    if not dir_path.exists() or not dir_path.is_dir():
        log.warning(f"  Directory no longer exists: {dir_path}")
        return

    cbz_dirs: dict[Path, list[Path]] = {}
    for cbz in sorted(dir_path.rglob("*.cbz")):
        cbz_dirs.setdefault(cbz.parent, []).append(cbz)

    if not cbz_dirs:
        log.info(f"  No .cbz files found under '{dir_path.name}', skipping.")
        return

    log.info(f"  Found .cbz files in {len(cbz_dirs)} directory(s).")
    progress = ProgressReporter(sum(len(files) for files in cbz_dirs.values()), "files")

    total_processed = total_skipped = total_renamed = 0

    for comic_dir, cbz_files in sorted(cbz_dirs.items()):
        dest_folder = _resolve_dest(comic_dir)

        # A directory that has .cbz files sitting directly inside it *and* also
        # contains subdirectories that themselves hold .cbz files (each already
        # its own entry in cbz_dirs) is a mixed drop point, not a single series
        # folder — e.g. a source folder like "HentaiNexus" with a few loose
        # files alongside dozens of per-title subfolders. Moving such a
        # directory wholesale would sweep every unrelated subdirectory along
        # with it, and the later loop iterations for those subdirectories would
        # then find nothing left to move (they'd all report "already moved").
        has_nested_comic_dirs = any(
            other != comic_dir and _is_subpath(other, comic_dir)
            for other in cbz_dirs
        )

        clean_dir_name = clean_directory_name(comic_dir.name)
        if has_nested_comic_dirs:
            # Do not rename/merge the mixed drop-point directory itself here —
            # it still holds subdirectories that belong to other cbz_dirs
            # entries, and renaming it out from under them would invalidate
            # those entries' paths. Only its loose files are handled below.
            pass
        elif not clean_dir_name:
            log.warning(f"  Skipping rename: cleaning '{comic_dir.name}' produced an empty name.")
        elif clean_dir_name != comic_dir.name:
            new_dir_path = comic_dir.parent / clean_dir_name
            if new_dir_path.exists():
                log.warning(
                    f"  Rename skipped: target already exists '{clean_dir_name}' "
                    f"— merging '{comic_dir.name}' into it."
                )
                for f in list(comic_dir.iterdir()):
                    dest_f = new_dir_path / f.name
                    if not dest_f.exists():
                        shutil.move(str(f), str(dest_f))
                    elif f.stat().st_size > dest_f.stat().st_size:
                        dest_f.unlink()
                        shutil.move(str(f), str(dest_f))
                        log.warning(f"    Replaced (larger, existing file discarded): '{f.name}'")
                    else:
                        f.unlink()
                try:
                    comic_dir.rmdir()
                except OSError:
                    pass
                cbz_files = sorted(new_dir_path.glob("*.cbz"))
                comic_dir = new_dir_path
            else:
                comic_dir.rename(new_dir_path)
                log.info(f"  Directory renamed: '{comic_dir.name}' -> '{clean_dir_name}'")
                cbz_files = [new_dir_path / f.name for f in cbz_files]
                comic_dir = new_dir_path

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
        parsed_by_path: dict[Path, ParsedComicName] = {}
        for cbz in cbz_files:
            try:
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
                result_path, parsed = process_cbz_file(cbz, override_name=override)
                total_processed += 1
                if result_path.name != original_name:
                    total_renamed += 1
                if parsed is not None:
                    parsed_by_path[result_path] = parsed
            finally:
                progress.step()

        # Route per-chapter folders ("Berserk Ch. 4") into a single series folder
        # ("Berserk") instead of creating one destination directory per chapter.
        # Use the post-rename paths (parsed_by_path keys) rather than the stale
        # pre-rename cbz_files list, so the ComicInfo <Series> evidence check
        # below can actually find the (now-renamed) files on disk.
        series_name, dir_number = _resolve_series_dir_name(
            comic_dir, list(parsed_by_path.keys()), dest_folder
        )
        if series_name != comic_dir.name:
            log.info(f"  Series folder: '{comic_dir.name}' -> '{series_name}'")

        # Give placeholder-named archives (see _PLACEHOLDER_STEM_RE) a unique,
        # traceable name before the merge — otherwise every chapter that reuses
        # a scanlator's generic filename collides under the same name and one
        # is silently discarded during _move_cbz_dir's merge.
        _apply_fallback_naming(parsed_by_path, series_name, dir_number)

        if has_nested_comic_dirs:
            log.warning(
                f"  '{comic_dir.name}' has loose .cbz file(s) alongside subdirectories that "
                f"have their own .cbz files — moving only the loose files, not the whole "
                f"directory, so unrelated series aren't swept along with it."
            )
            _move_loose_files(list(parsed_by_path.keys()), dest_folder, series_name)
            continue

        _move_cbz_dir(comic_dir, dest_folder, target_name=series_name, chapter_number=dir_number)

    log.info(
        f"  Batch complete — {total_processed} processed, "
        f"{total_renamed} renamed, {total_skipped} skipped."
    )


# ─────────────────────────────────────────────
# PATH HELPERS
# ─────────────────────────────────────────────
def _is_subpath(child: Path, parent: Path) -> bool:
    """Return True if child is equal to or nested under parent."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


# ─────────────────────────────────────────────
# DIRECTORY SETTLE TRACKER
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
        # Guard against processing a directory already being handled by another thread.
        with _processing_dirs_lock:
            already = any(
                dir_path == p or _is_subpath(dir_path, p) or _is_subpath(p, dir_path)
                for p in _processing_dirs
            )
        if already:
            log.info(f"  Skipping '{dir_path.name}': already being processed.")
            return
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
        with _processing_dirs_lock:
            for proc_dir in _processing_dirs:
                try:
                    parent.relative_to(proc_dir)
                    return
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

    _load_routing()

    log.info("=" * 60)
    log.info("CBZ Watcher started")
    log.info(f"  Watching : {WATCH_FOLDER}")
    log.info(f"  Routing  : {ROUTING_FILE}")
    log.info(f"  Log      : {LOG_FILE}")
    log.info(f"  Settle   : {SETTLE_DELAY}s after last file event")
    log.info("=" * 60)

    tracker = DirectorySettleTracker()

    stale = list(watch_path.rglob("*.tmp.cbz")) + list(watch_path.rglob("*.bak.cbz"))
    if stale:
        log.info(f"  Cleaning up {len(stale)} stale temp file(s) from previous run...")
        for f in stale:
            try:
                f.unlink()
                log.info(f"    Deleted stale file: {f.name}")
            except OSError as e:
                log.warning(f"    Could not delete stale file {f.name}: {e}")

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
