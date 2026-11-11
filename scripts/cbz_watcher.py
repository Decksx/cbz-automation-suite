"""
CBZ File Watcher & Processor
Monitors a folder for incoming .cbz files inside subdirectories.
Processes ALL .cbz files in a directory first, then moves the
immediate comic directory to the configured destination.
"""

import contextlib
import os
import hashlib
from contextlib import contextmanager
import argparse
import contextlib
import re
import gc
import json
import subprocess
import time
import zlib
import shutil
import fnmatch
import zipfile
import logging
import threading
import xml.etree.ElementTree as ET
from enum import Enum
from dataclasses import dataclass
from dataclasses import replace as _dataclasses_replace
from pathlib import Path
from collections.abc import Iterable
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from logging.handlers import RotatingFileHandler as _RotatingFileHandler
from comic_automation.series_resolver import (
    SeriesResolver,
    digest,
    normalize_apostrophes,
    safe_name,
    settle_file_conflict,
    unique_path,
)
from comic_automation.library.readiness_hold import ReadinessHolds
from comic_automation.library import claims as _claims
from comic_automation.library.watcher_instance import watcher_instance_lock, WatcherInstanceRefused, WatcherInstanceCleanupError
from comic_automation.archive import field_resolution as _media_fr
from comic_automation.library.media_naming import media_fields, watcher_naming, with_comicinfo_year, KeepArrivalName, split_series_year
from comic_automation.library.media_naming import display_key, distinct_numbers
from comic_automation.library.arrival_rename import rename_arrival, CaseRenameRecoveryError
from comic_automation.library.number_style import NumberStyleRegistry, NumberStyleRefused, DEFAULT_REGISTRY
from comic_automation.library.exclusions import (
    DEFAULT_EXCLUDED_DIRECTORIES,
    iter_files_excluding_directories,
    iter_paths_excluding_directories,
    path_contains_excluded_directory,
)
from scripts.cbz_routing import series_key
from scripts.ai_decensor_backups import ARCHIVE_ROOT, archive_backup
from scripts.cbz_lock_order import (
    SeriesLockRegistry,
    UnstableSeriesIdentityError,
    stable_series_lock,
)

try:
    from scripts.cbz_core import (
        PART_MARKER_RE,
        ParsedComicName,
        clean_directory_name,
        clean_filename,
        decensor_tokens_as_words,
        display_tokens_for,
        dropped_round_tags,
        extract_chapter_number,
        extract_trailing_bare_number,
        extract_volume_number,
        known_groups_mixed_drop,
        normalise_archive_key,
        parse_comic_name,
        range_in,
        sanitize_without_markers,
        series_base_name,
        update_comicinfo_xml,
    )
except ModuleNotFoundError:
    from cbz_core import (  # type: ignore[no-redef]
        PART_MARKER_RE,
        ParsedComicName,
        clean_directory_name,
        clean_filename,
        decensor_tokens_as_words,
        display_tokens_for,
        dropped_round_tags,
        extract_chapter_number,
        extract_trailing_bare_number,
        extract_volume_number,
        known_groups_mixed_drop,
        normalise_archive_key,
        parse_comic_name,
        range_in,
        sanitize_without_markers,
        series_base_name,
        update_comicinfo_xml,
    )

# The canonical formatter (design §5.5): fallback names are formatted by it,
# not assembled here, so arrivals and the canonical-naming plan cannot drift.
try:
    from scripts.cbz_canonical_name import (
        CANONICAL_EXTENSION,
        CanonicalFields,
        CanonicalNameError,
        format_canonical_name,
    )
except ModuleNotFoundError:
    from cbz_canonical_name import (  # type: ignore[no-redef]
        CANONICAL_EXTENSION,
        CanonicalFields,
        CanonicalNameError,
        format_canonical_name,
    )

# ─────────────────────────────────────────────
# CONFIGURATION — edit these as needed
# ─────────────────────────────────────────────
WATCH_FOLDER  = r"C:\Temp\Mega\Mega Uploads\book2"
REPO_ROOT     = Path(__file__).resolve().parents[1]
LOG_FILE      = REPO_ROOT / "Logs" / "cbz_watcher.log"
# Main initializes the shared policy; direct legacy helper callers retain their API.
_number_style_registry = None
_number_style_requested = "auto"
POLL_INTERVAL = 2    # seconds between stability checks
SETTLE_DELAY  = 5    # seconds of inactivity before processing a directory
MIN_AGE       = 300  # seconds a directory must exist before processing

# Optional Camelia stage. It is disabled unless the watcher is launched with
# --ai-decensor, so existing watcher installations retain their old behavior.
AI_DECENSOR_ENABLED = False
AI_DECENSOR_MODELS = ["black_bars"]
AI_DECENSOR_REPROCESS = False
# Where keep-the-larger puts the copy it does not keep: nothing that differs
# from the kept copy is ever deleted. Placements go in a dated subfolder
# (YYYY-MM-DD) so a retention purge can work by age.
QUARANTINE_ROOT = Path(os.environ.get("CBZ_QUARANTINE_ROOT", r"C:\Temp\cbz_held\quarantine"))
# Claim-first processing (comic_automation.library.claims). Every arrival is
# moved here, out of the MEGA-synced watch folder, by one same-volume rename
# before anything changes it; work in progress lives here and is resumed on
# start. None processes in place (direct callers only; the CLI always claims).
PROCESSING_ROOT: Path | None = Path(os.environ.get("CBZ_PROCESSING_ROOT", r"C:\Temp\cbz_processing"))
# Archives held for a decision (a refused name, a collision with a different
# library book, an interrupted AI rewrite). Kept until someone acts on them.
REVIEW_ROOT = Path(os.environ.get("CBZ_REVIEW_ROOT", r"C:\Temp\cbz_held\review"))
CAMELIA_ROOT = Path(os.environ.get("CBZ_CAMELIA_ROOT", r"C:\git\camelia"))
CAMELIA_PYTHON = Path(os.environ.get(
    "CBZ_CAMELIA_PYTHON",
    r"C:\ProgramData\miniconda3\envs\camelia_env\python.exe",
))
AI_DECENSOR_BACKUP_DIR = REPO_ROOT / "data" / "ai-decensor-originals"
AI_DECENSOR_ARCHIVE_DIR = Path(os.environ.get("CBZ_AI_DECENSOR_ARCHIVE_DIR", str(ARCHIVE_ROOT)))

# Optional canonical-series routing. The GUI enables it explicitly; plain CLI
# launches remain on the legacy staging flow unless --series-resolver is used.
SERIES_RESOLVER_ENABLED = False
SERIES_RESOLVER_ROOTS = {
    "comix": Path(r"Z:\comics\Comix"),
    "manga": Path(r"Z:\comics\Manga"),
}
SERIES_RESOLVER_QUARANTINE_ROOT = Path(r"Z:\comics\_resolver_quarantine")
SERIES_RESOLVER_THRESHOLD = 0.95
SERIES_RESOLVER_REFRESH = False
SERIES_RESOLVER_NORMALIZE_APOSTROPHES = False

ROUTING_FILE  = REPO_ROOT / "routing.json"

# v2 routing. The legacy v1 loader above still decides every move; this is a
# parallel classifier for observation only.
#
#   off    -- v2 is never consulted (checked-in default)
#   shadow -- v2 classifies each directory and logs how it would differ
#
# Shadow mode opens up to ROUTING_V2_SAMPLE archives per directory to read
# ComicInfo, and writes nothing. Set CBZ_ROUTING_V2_MODE=shadow to run a
# comparison pass without editing this file.
#
# There is no "active" value: letting v2 choose a destination needs the review
# staging path that does not exist yet, and repointing ROUTING_FILE at the v2
# config would NOT activate v2 -- the legacy resolver reads only match/pattern
# rules and would match nothing, sending the whole library to the default.
ROUTING_V2_FILE   = REPO_ROOT / "config" / "routing.v2.json"
ROUTING_V2_MODE   = os.environ.get("CBZ_ROUTING_V2_MODE", "off")
ROUTING_V2_SAMPLE = 5

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
_routing_default_key: str = ''                # key for category-aware resolving

# One live registry per configured category. Populated at startup only when the
# resolver is enabled; an empty mapping means every legacy destination retains
# its previous behavior.
_series_resolvers: dict[str, SeriesResolver] = {}

# v2 router, built once at startup. None whenever v2 is off or its config
# failed to load — the watcher must keep running on legacy routing either way,
# since an observation feature has no business stopping ingestion.
_router = None

# Directories currently being processed — events for these are suppressed to
# prevent the file-rename step from re-triggering the settle timer in a loop.
#
# Keyed by arrival PATH, and that is the whole reason the registry below
# exists: this guard only prevents two threads working the same or a nested
# path. Two different arrival directories that resolve to the same series are
# not serialized by it at all (issue #32).
_processing_dirs: set = set()
_processing_dirs_lock = threading.Lock()

# One lock per resolved series identity, for the classify -> move -> index
# transaction. Process-wide because the Timer threads that run it are, and
# because the identity is a property of the library rather than of a run.
# See docs/lock_topology.md.
_SERIES_LOCKS = SeriesLockRegistry()

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
def _discard_rebuild_if_original_intact(cbz_path: Path, tmp_path: Path) -> None:
    """Delete the rebuilt copy only while the original is still in place.

    The rebuild at *tmp_path* is disposable exactly as long as *cbz_path*
    holds a real archive. If the swap failed partway and cbz_path is
    missing, the rebuild may be the last intact copy in existence, and
    deleting it is what turns a recoverable failure into data loss.
    """
    if not tmp_path.exists():
        return

    if cbz_path.exists():
        tmp_path.unlink()
        return

    log.critical(
        f"    {cbz_path.name}: missing from its recorded path after a "
        f"failed rewrite; keeping {tmp_path} rather than deleting it."
    )


def _shadowed_archive_path(leftover: Path) -> Path | None:
    """The archive a ``.tmp.cbz`` / ``.bak.cbz`` leftover belongs to.

    Both names are produced by `Path.with_suffix` on the original, so the
    original's name is recovered by putting ``.cbz`` back. Returns None for
    anything that is not one of those two shapes, so an unrelated file is
    never treated as a leftover.
    """
    for marker in (".tmp.cbz", ".bak.cbz"):
        if leftover.name.endswith(marker):
            return leftover.with_name(leftover.name[: -len(marker)] + ".cbz")

    return None


def _clean_up_stale_rewrite_files(watch_path: Path) -> None:
    """Delete rewrite leftovers, but never the last copy of an archive.

    A ``.tmp.cbz`` or ``.bak.cbz`` is genuinely disposable only when the
    archive it shadows is present at its own name. When that archive is
    missing, the leftover is not stale at all -- it is the surviving copy
    of an interrupted rewrite, and this cleanup running on the next start
    was the step that turned a recoverable interruption into permanent
    loss.

    Kept files are reported at CRITICAL and left exactly where they are,
    because quarantining them here would move bytes out from under the
    operator recovering them by hand.
    """
    leftovers = sorted(
        path
        for path in iter_files_excluding_directories(watch_path)
        if path.name.endswith((".tmp.cbz", ".bak.cbz"))
    )

    if not leftovers:
        return

    deleted = 0
    kept: list[Path] = []

    for leftover in leftovers:
        original = _shadowed_archive_path(leftover)

        # Checked before deleting, so a leftover that is the only copy is
        # reported even when deletion would have succeeded.
        if original is not None and not original.exists():
            kept.append(leftover)
            continue

        try:
            leftover.unlink()
            deleted += 1
            log.info(f"    Deleted stale file: {leftover.name}")
        except OSError as e:
            log.warning(f"    Could not delete stale file {leftover.name}: {e}")

    log.info(
        f"  Cleaned up {deleted} stale temp file(s) from previous run; "
        f"kept {len(kept)}."
    )

    for leftover in kept:
        log.critical(
            f"    KEPT {leftover}: the archive it belongs to is missing "
            f"from its recorded path, so this is the surviving copy of an "
            f"interrupted rewrite. Recover it by hand."
        )


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
            bak_path.unlink(missing_ok=True)
            cbz_path.rename(bak_path)

            # Nothing exists at cbz_path between these two renames, and the
            # failure compounds badly here: the retry below re-opens
            # cbz_path, which is now missing, so every remaining attempt
            # fails on a file that is not there, the handler deletes the
            # rebuilt copy, and the only surviving original is a .bak.cbz
            # that this watcher's own startup cleanup deletes. Restore
            # first, so the retry runs against a file that exists.
            try:
                tmp_path.rename(cbz_path)
            except OSError:
                try:
                    bak_path.rename(cbz_path)
                except OSError:
                    log.critical(
                        f"    {cbz_path.name}: rewrite failed AND the "
                        f"original could not be restored. Original bytes "
                        f"are at {bak_path}, rebuilt copy at {tmp_path}. "
                        f"Neither will be deleted. Recover by hand."
                    )
                raise

            bak_path.unlink(missing_ok=True)
            log.info(f"    comicinfo.xml {action} successfully.")
            return

        except OSError as e:
            log.warning(f"    File locked (attempt {attempt + 1}/5), retrying in 5s... ({e})")
            _discard_rebuild_if_original_intact(cbz_path, tmp_path)
            time.sleep(5)
        except Exception as e:
            log.error(f"    Failed to write comicinfo.xml: {e}")
            _discard_rebuild_if_original_intact(cbz_path, tmp_path)
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
# ZIP READINESS PROBE
# ─────────────────────────────────────────────
# Seconds between the two stat() samples this probe takes to confirm size and
# mtime haven't moved. Kept short: this runs once per caller invocation (unlike
# wait_for_file_stable's own polling loop above), so callers are expected to
# invoke it repeatedly from their own retry/settle loop rather than block here.
ZIP_READINESS_SETTLE_INTERVAL_SECONDS = 0.05
ZIP_READINESS_IMAGE_EXTENSIONS = frozenset({
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".jfif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
})


class ZipReadiness(Enum):
    """Outcome of a single readiness-probe call. Never a permanent failure —
    RETRY_LATER always means "ask again next pass", including for a bad/
    incomplete central directory, since a file mid-copy can look byte-for-byte
    like a corrupt zip until the writer finishes."""

    READY = "ready"
    RETRY_LATER = "retry_later"


@dataclass(frozen=True)
class ZipReadinessResult:
    """Result of probe_cbz_zip_readiness(). `reason` is a short machine-stable
    tag (not a full log line) so callers can decide their own logging/metrics
    without this pure helper making that decision for them."""

    status: ZipReadiness
    reason: str
    page_count: int | None = None
    detail: str | None = None


def probe_cbz_zip_readiness(
    path: Path,
    *,
    settle_interval: float = ZIP_READINESS_SETTLE_INTERVAL_SECONDS,
) -> ZipReadinessResult:
    """Single-shot, side-effect-free check: is this CBZ's central directory
    readable right now?

    Two things this deliberately does NOT do, unlike wait_for_file_stable():
      - It never loops/retries internally — one call does one check and
        returns. Callers (the watcher's settle timer, a future polling loop,
        tests) decide when to call again.
      - It takes no exclusive lock and makes no database writes. It only
        reads the file's stat() twice and, if that looks stable, opens it
        read-only to enumerate zipfile.ZipFile(...).infolist(). Nothing here
        mutates the file, the queue, or any database.

    Stability check: two stat() samples separated by `settle_interval`
    seconds must agree on both size and mtime. A final sample after ZIP
    enumeration must still match, closing the window where a writer could
    resume during the central-directory probe. This catches the common CBZ
    watcher case of a file that is still being written (growing size) or
    still being finalised (mtime still ticking) even when a single stat()
    snapshot would look plausible.

    Zip probe: zipfile.ZipFile(path).infolist() is used because reading the
    central directory is exactly what "this archive is enumerable" means for
    a CBZ — it doesn't decompress any page content, just confirms the End Of
    Central Directory record and directory entries are intact and parseable.

    Every failure mode here is treated as RETRY_LATER, not a permanent
    error, matching the exception vocabulary already established in
    scripts/cbz_watcher.py and scripts/cbz_sanitizer.py for corrupt/locked
    CBZ files on this Windows/SMB setup:
      - FileNotFoundError / a stat() OSError -- the file vanished or a
        transient network stat failure occurred.
      - size/mtime disagreeing between the two samples -- still being
        written or touched.
      - zipfile.BadZipFile (also zlib.error/EOFError, which zipfile can
        raise for a truncated End Of Central Directory record) -- an
        incomplete/corrupt central directory, indistinguishable from "still
        being written" from here.
      - PermissionError -- a Windows/SMB sharing violation (another process
        holds the file open).
      - Any other OSError -- a transient SMB/network I/O error.
    """
    if settle_interval < 0:
        raise ValueError("settle_interval cannot be negative")

    try:
        first_stat = path.stat()
    except FileNotFoundError:
        return ZipReadinessResult(ZipReadiness.RETRY_LATER, "file_not_found")
    except OSError as exc:
        return ZipReadinessResult(
            ZipReadiness.RETRY_LATER,
            "stat_error",
            detail=str(exc),
        )

    if settle_interval > 0:
        time.sleep(settle_interval)

    try:
        second_stat = path.stat()
    except FileNotFoundError:
        return ZipReadinessResult(ZipReadiness.RETRY_LATER, "file_not_found")
    except OSError as exc:
        return ZipReadinessResult(
            ZipReadiness.RETRY_LATER,
            "stat_error",
            detail=str(exc),
        )

    if (
        first_stat.st_size != second_stat.st_size
        or first_stat.st_mtime_ns != second_stat.st_mtime_ns
    ):
        return ZipReadinessResult(
            ZipReadiness.RETRY_LATER, "size_or_mtime_changed"
        )

    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
    except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
        return ZipReadinessResult(
            ZipReadiness.RETRY_LATER,
            "incomplete_central_directory",
            detail=str(exc),
        )
    except PermissionError as exc:
        return ZipReadinessResult(
            ZipReadiness.RETRY_LATER,
            "sharing_violation",
            detail=str(exc),
        )
    except OSError as exc:
        return ZipReadinessResult(
            ZipReadiness.RETRY_LATER,
            "transient_io_error",
            detail=str(exc),
        )

    try:
        final_stat = path.stat()
    except FileNotFoundError:
        return ZipReadinessResult(ZipReadiness.RETRY_LATER, "file_not_found")
    except OSError as exc:
        return ZipReadinessResult(
            ZipReadiness.RETRY_LATER,
            "stat_error",
            detail=str(exc),
        )

    if (
        second_stat.st_size != final_stat.st_size
        or second_stat.st_mtime_ns != final_stat.st_mtime_ns
    ):
        return ZipReadinessResult(
            ZipReadiness.RETRY_LATER,
            "size_or_mtime_changed",
        )

    page_count = sum(
        1
        for entry in entries
        if not entry.is_dir()
        and Path(entry.filename).suffix.casefold()
        in ZIP_READINESS_IMAGE_EXTENSIONS
    )

    return ZipReadinessResult(
        ZipReadiness.READY,
        "ok",
        page_count=page_count,
    )


# ─────────────────────────────────────────────
# ZIP READINESS PROBE — DIRECTORY AGGREGATION
# ─────────────────────────────────────────────
@dataclass(frozen=True)
class CbzReadinessEntry:
    """Per-archive diagnostic record within a directory-level probe.

    A direct copy of one probe_cbz_zip_readiness() call's fields, plus the
    path it was called on, so callers can see exactly which archive(s) in a
    directory are holding up the whole directory without re-probing.
    """

    path: Path
    status: ZipReadiness
    reason: str
    detail: str | None
    page_count: int | None


@dataclass(frozen=True)
class DirectoryReadinessResult:
    """Result of probe_cbz_directory_readiness().

    `status` is READY only when every archive in `entries` is READY;
    otherwise RETRY_LATER, whether that's because one archive isn't ready
    yet or because no archives were given at all (reason="no_cbz_files").
    `entries` preserves the exact order paths were given in — one entry per
    input path, in that order, regardless of how many are ready.
    """

    status: ZipReadiness
    reason: str
    entries: tuple[CbzReadinessEntry, ...]
    archive_count: int
    ready_count: int
    page_count: int


def probe_cbz_directory_readiness(
    paths: Iterable[Path],
    *,
    settle_interval: float = ZIP_READINESS_SETTLE_INTERVAL_SECONDS,
) -> DirectoryReadinessResult:
    """Pure directory-level aggregation over probe_cbz_zip_readiness().

    Calls probe_cbz_zip_readiness() exactly once per path, in the exact
    order `paths` iterates in, and is READY only when every single archive
    comes back READY -- one unreadable archive is enough to hold back the
    whole directory. Like the single-file probe, this is entirely pure: no
    logging, no locking, no database access, and no file mutation. It also
    performs no filesystem discovery of its own -- the caller decides which
    paths belong to "this directory" and passes them in.

    An empty `paths` is treated as RETRY_LATER with reason="no_cbz_files"
    rather than vacuously READY, since a directory with zero CBZs currently
    enumerated is not a directory this probe can vouch for -- there is
    nothing here to confirm readiness of yet.

    `page_count` on the result is the sum of each READY entry's page count
    (entries that aren't READY have page_count=None, contributing 0) -- it
    is not meaningful as a total until every archive is READY.

    This function does not decide what happens next -- it does not move
    files, does not enqueue anything, and is not wired into
    process_cbz_file() or directory-move logic. That integration is left
    for a follow-up change, since an unreadable archive needing to block an
    entire directory move is a decision that belongs at the call site, not
    inside this pure helper.
    """
    ordered_paths = tuple(paths)

    if not ordered_paths:
        return DirectoryReadinessResult(
            status=ZipReadiness.RETRY_LATER,
            reason="no_cbz_files",
            entries=(),
            archive_count=0,
            ready_count=0,
            page_count=0,
        )

    entries: list[CbzReadinessEntry] = []

    for path in ordered_paths:
        result = probe_cbz_zip_readiness(path, settle_interval=settle_interval)
        entries.append(
            CbzReadinessEntry(
                path=path,
                status=result.status,
                reason=result.reason,
                detail=result.detail,
                page_count=result.page_count,
            )
        )

    archive_count = len(entries)
    ready_count = sum(
        1 for entry in entries if entry.status == ZipReadiness.READY
    )
    page_count = sum(entry.page_count or 0 for entry in entries)

    all_ready = ready_count == archive_count
    overall_status = ZipReadiness.READY if all_ready else ZipReadiness.RETRY_LATER
    overall_reason = "ok" if all_ready else "not_all_ready"

    return DirectoryReadinessResult(
        status=overall_status,
        reason=overall_reason,
        entries=tuple(entries),
        archive_count=archive_count,
        ready_count=ready_count,
        page_count=page_count,
    )


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
    # display_tokens: the new name keeps (YYYY), [vN] and edition tokens while
    # group/source tags are still dropped (design §5.5); ComicInfo fields and
    # the series identity are computed exactly as before.
    parsed   = parse_comic_name(cbz_path, display_tokens=True)
    new_name = normalize_apostrophes(
        override_name if override_name is not None else parsed.filename
    )

    if new_name != cbz_path.name:
        new_path = cbz_path.parent / new_name
        if new_path.exists():
            log.warning(
                f"    Rename skipped: target already exists '{new_name}' "
                f"(keeping original '{cbz_path.name}')"
            )
        elif _defers_rename(cbz_path.stem, new_path.stem):
            # The new name drops a tag that may name a different book, such
            # as (s2), so the archive keeps its arrival name until it lands
            # (_stripped_arrivals, issue #131). Renamed here, a failure or a
            # restart before the merge would leave nothing on disk to tell
            # the next pass that the tag was ever there.
            log.info(
                f"    Rename deferred to landing: '{cbz_path.name}' -> '{new_name}'"
            )
            if parsed.filename != new_name:
                parsed = _dataclasses_replace(parsed, filename=new_name)
        else:
            cbz_path.rename(new_path)
            log.info(f"    Renamed: '{cbz_path.name}' -> '{new_name}'")
            cbz_path = new_path
    else:
        log.info(f"    Filename unchanged: '{cbz_path.name}'")

    process_comicinfo(cbz_path, parsed)
    return cbz_path, parsed


class AiDecensorError(RuntimeError):
    """Raised when Camelia cannot safely replace an incoming archive."""


def destination_is_comix(destination: str | Path) -> bool:
    """Return whether a resolved watcher destination contains `Comix`."""
    return any(
        part.casefold() == "comix"
        for part in re.split(r"[\\/]+", str(destination))
        if part
    )


def run_ai_decensor(cbz_path: Path, destination_dir: str | Path) -> Path | None:
    """AI-decensor one archive in place and return its quarantined original.

    Camelia verifies the rebuilt archive before replacing ``cbz_path`` and
    stores the untouched input below ``AI_DECENSOR_BACKUP_DIR``. The watcher
    accepts the subprocess result only when both paths resolve exactly where
    configured; a malformed or partial response fails the directory closed.
    """
    cli_path = CAMELIA_ROOT / "scripts" / "process_cbz.py"
    if not CAMELIA_PYTHON.is_file():
        raise AiDecensorError(f"Camelia Python was not found: {CAMELIA_PYTHON}")
    if not cli_path.is_file():
        raise AiDecensorError(f"Camelia CLI was not found: {cli_path}")

    stage_sequence = " -> ".join(AI_DECENSOR_MODELS)
    log.info(f"    AI decensor stages for {cbz_path.name}: {stage_sequence}")
    command = [
        str(CAMELIA_PYTHON),
        str(cli_path),
        str(cbz_path),
    ]
    for model_type in AI_DECENSOR_MODELS:
        command.extend(["--model-type", model_type])
    if AI_DECENSOR_REPROCESS:
        command.append("--reprocess")
    command.extend([
        "--replace",
        "--backup-dir",
        str(AI_DECENSOR_BACKUP_DIR),
        "--destination-dir",
        str(destination_dir),
    ])
    completed = subprocess.run(
        command,
        cwd=str(CAMELIA_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    for line in completed.stdout.splitlines():
        if not line.startswith("CAMELIA_RESULT="):
            log.info(f"    Camelia: {line}")
    stderr_lines = [line for line in completed.stderr.splitlines() if line.strip()]
    for line in stderr_lines:
        log.error(f"    Camelia: {line}")
    if completed.returncode != 0:
        detail = stderr_lines[-1] if stderr_lines else completed.stdout.strip()
        raise AiDecensorError(detail or f"Camelia exited with code {completed.returncode}")

    result_line = next(
        (line for line in reversed(completed.stdout.splitlines())
         if line.startswith("CAMELIA_RESULT=")),
        None,
    )
    if result_line is None:
        raise AiDecensorError("Camelia returned no completion record")
    try:
        result = json.loads(result_line.partition("=")[2])
        if result.get("status") == "skipped":
            if Path(result.get("path", "")).resolve() != cbz_path.resolve():
                raise AiDecensorError("Camelia skipped an unexpected archive")
            log.info(f"    AI decensor skipped for {cbz_path.name}: selected methods already applied")
            return None
        result_path = Path(result["path"]).resolve()
        backup_path = Path(result["backup_path"]).resolve()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AiDecensorError("Camelia returned an invalid completion record") from exc

    backup_root = AI_DECENSOR_BACKUP_DIR.resolve()
    if result_path != cbz_path.resolve():
        raise AiDecensorError(f"Camelia replaced an unexpected path: {result_path}")
    if not backup_path.is_file() or not backup_path.is_relative_to(backup_root):
        raise AiDecensorError(f"Camelia backup is missing or outside quarantine: {backup_path}")
    log.info(f"    AI decensored: {cbz_path.name}")
    log.info(f"    Original quarantined: {backup_path}")
    return backup_path


def restore_ai_decensor_backups(processed: list[tuple[Path, Path]]) -> None:
    """Restore earlier archives when a later archive in the batch fails."""
    for archive_path, backup_path in reversed(processed):
        rollback_path = archive_path.with_name(f".{archive_path.name}.ai-rollback.tmp")
        try:
            shutil.copy2(backup_path, rollback_path)
            with zipfile.ZipFile(rollback_path) as archive:
                if archive.testzip() is not None:
                    raise AiDecensorError(f"Rollback backup is corrupt: {backup_path}")
            os.replace(rollback_path, archive_path)
            log.warning(f"    Restored after batch failure: {archive_path.name}")
        finally:
            try:
                rollback_path.unlink(missing_ok=True)
            except OSError:
                pass


def _rekey_ai_backups(
    ai_backups: list[tuple[Path, Path]],
    origins: dict[Path, Path],
    parsed_by_path: dict,
) -> list[tuple[Path, Path]]:
    """Re-key ``(archive, original)`` pairs to each archive's current path.

    The pairs are recorded under the path ``process_cbz_file`` returned;
    ``_apply_fallback_naming`` may rename the archive after that. *origins*
    maps each pre-fallback path to its ``ParsedComicName.original_path``,
    which a fallback rename carries over unchanged, so the pair follows its
    archive instead of pointing at a name that no longer exists.
    """
    current = {
        getattr(parsed, "original_path", path): path
        for path, parsed in parsed_by_path.items()
    }
    return [
        (current.get(origins.get(archive, archive), archive), backup)
        for archive, backup in ai_backups
    ]


def _routed_ai_backups(
    ai_backups: list[tuple[Path, Path]], landed: Path | None
) -> list[tuple[Path, Path]]:
    """The originals whose **own** archive routed, for offloading.

    A non-None *landed* says some archive of the directory landed; it is no
    receipt for any particular one. In the mixed-directory branch a refused
    placeholder stays in the import folder while its accepted siblings move,
    so each original is completed only if its own archive has left the
    import folder. The rest stay on C: with their archive, for the deferred
    retry. With *landed* None nothing routed and every pair is passed on,
    so ``archive_completed_ai_backups`` reports them all as kept.
    """
    if landed is None:
        return list(ai_backups)
    routed = []
    for archive, backup in ai_backups:
        if archive.exists():
            log.warning(
                f"    AI original kept on C: for deferred '{archive.name}': {backup}"
            )
        else:
            routed.append((archive, backup))
    return routed


def archive_completed_ai_backups(processed: list[tuple[Path, Path]], landed: Path | None) -> None:
    """Offload originals only after the whole directory was routed successfully."""
    if not processed:
        return
    if landed is None:
        log.warning("    AI originals remain on C: because the directory was not routed")
        return
    for _processed_path, backup_path in processed:
        try:
            archived = archive_backup(backup_path, AI_DECENSOR_ARCHIVE_DIR, landed.name)
            log.info(f"    AI original archived: {backup_path} -> {archived}")
        except (OSError, ValueError) as exc:
            log.error(
                f"    AI original remains on C: after archive failure: {backup_path} "
                f"({type(exc).__name__}: {exc})"
            )


# ─────────────────────────────────────────────
# DIRECTORY MERGE (keep largest on conflict)
# ─────────────────────────────────────────────
def _find_archive_collision(target_dir: Path, stem: str) -> Path | None:
    """Return an existing archive in *target_dir* whose normalised key matches *stem*.

    Treats files that differ only by spacing or punctuation as the same book
    (e.g. "Series Ch.1" and "Series Ch. 1"), so the merge keeps one copy instead
    of letting both cosmetic variants survive. Returns None when none match.

    Display tokens (design §5.5) compare as the pre-§5.5 names did:

    * ``(YYYY)`` is dropped from the key. Names before §5.5 never carried
      it, so ``Berserk Ch. 5 (2019)`` arriving beside ``Berserk Ch. 5`` is
      the same book and still meets it.
    * Zero-padding after a volume, issue or chapter marker is ignored, so
      the canonical ``Berserk v01 #003`` meets the legacy ``Berserk v1
      #003``. ``v01`` and ``v02`` still differ.
    * ``(Uncensored)``/``(Decensored)`` compares as the bare word every
      earlier name carried, so a decensored re-arrival meets its copy.
    * ``[vN]`` and the other edition tokens stay in the key on purpose: a
      [v3] stored as "... [v2]" would be a wrong version label (§4.6), and
      two editions are not one book. Those land side by side for the
      supersession plan to judge.

    On a match the merge works as before: the existing **name** always
    survives. The incoming bytes replace the existing bytes under that name
    only when they are larger by ``REPLACEMENT_MIN_GAIN_BYTES``; otherwise
    the incoming copy is quarantined (``_settle_by_size``). So a ``(2019)``
    arrival that wins on size
    loses its year token from the name -- accepted: the existing name wins,
    exactly as it did when both names were token-free.

    A match met only because a round-bracket tag was stripped (``(s2)``,
    ``(1)``, a parody tag) is not evidence of the same book: the merge holds
    such an arrival for review instead (``_stripped_arrivals``, issue #131).
    """
    if not target_dir.is_dir():
        return None
    key = _archive_collision_key(stem)
    for existing in target_dir.iterdir():
        if existing.is_file() and existing.suffix.lower() in {".cbz", ".cbr"}:
            if _archive_collision_key(existing.stem) == key:
                return existing
    return None


# A canonical year token, as cbz_core's display path writes it.
_YEAR_TOKEN_RE = re.compile(r"\(([1-9][0-9]{3})\)")
# Leading zeros of a number written after a volume, issue or chapter marker:
# the canonical "v01", "#003", "ch. 01" and the legacy "v1", "Vol. 1",
# "#3", "Ch. 1". The legacy pipeline unpads v/Vol/Ch numbers and the
# formatter pads them, so without this one book keys two ways
# ("berserkv1#003" vs "berserkv01#003"). Only padding is removed, and only
# for comparison: nothing is classified as a volume or version, and a
# bracketed "[v02]" is left alone (the lookbehind excludes "[") so version
# labels keep distinguishing books.
_NUMBER_PADDING_RE = re.compile(
    r"(?<![\w\[])(v|vol(?:ume)?\.?\s*|#\s*|ch(?:ap(?:ter)?)?\.?\s*)0+(?=\d)",
    re.IGNORECASE,
)


def _archive_collision_key(stem: str) -> str:
    """The book-identity key ``_find_archive_collision`` compares."""
    stem = _NUMBER_PADDING_RE.sub(r"\1", _YEAR_TOKEN_RE.sub(" ", stem))
    return normalise_archive_key(decensor_tokens_as_words(stem))


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


@dataclass(frozen=True)
class FallbackRefused:
    """``_fallback_display_stem`` could not name the archive; why, for review.

    A refusal is never turned into some other name. The caller keeps the
    archive's current name and routes it to review (#118: a
    ``CanonicalNameError`` is a review item, not a cue to assemble an
    unchecked alternative).
    """

    reason: str


def _fallback_display_stem(
    series_name: str, chapter: str | None, source_stem: str, *, number_style="chapter"
) -> str | FallbackRefused:
    """The stem a fallback-named archive gets, via the canonical formatter.

    The fields are the resolved series folder and the chapter number the
    enclosing folder implied -- folder context, the third source in design
    §3.1, not name evidence -- plus the display tokens of the archive's own
    name (``source_stem``): its ``(YYYY)``, ``(Edition)`` and ``[vN]``.

    A ``[vN]`` is carried into the version slot as **display preservation
    only**: the uploader's own bracketed text stays in the name it already
    had. Nothing here verifies it as a version label in the §4.2/§4.6 sense
    (family membership, keeper label); that is the supersession plan's job.
    The bracketed spelling is used because it is the one that can never read
    as a volume. A bare ``v<N>`` is never a token, so it cannot arrive here
    as either.

    Returns :class:`FallbackRefused` -- never a substitute name -- when the
    fields do not fit the grammar (more than one year, edition or version)
    or the formatter refuses them (a series that ends in a name token such
    as ``Series Special``, a reserved or over-long name).
    """
    # A decensor word the folder name already carries is not repeated as an
    # edition: the rule append_display_tokens and preserve_decensor_markers
    # apply, taken from the same function.
    tokens = display_tokens_for(series_name, source_stem)
    editions = tokens.editions
    if len(tokens.years) > 1 or len(editions) > 1 or len(tokens.versions) > 1:
        return FallbackRefused(
            f"conflicting tokens in '{source_stem}': years {list(tokens.years)}, "
            f"editions {list(editions)}, versions {list(tokens.versions)}"
        )
    try:
        name = format_canonical_name(media_fields(
            series=series_name, number=chapter, volume=None, style=number_style,
            tokens=_media_fr.parse_name_tokens(source_stem + ".cbz"),
            year=tokens.years[0] if tokens.years else None,
            edition=editions[0] if editions else None,
            version=tokens.versions[0] if tokens.versions else None,
        ))
    except CanonicalNameError as exc:
        return FallbackRefused(f"{type(exc).__name__}: {exc}")
    return name[: -len(CANONICAL_EXTENSION)]


def _media_naming_enabled():
    """An empty registry keeps the complete legacy watcher path enabled.

    A loaded nonempty registry opts arrivals into media naming, including
    explicit syntax overrides. The planner has its independent strict default.
    """
    return _number_style_registry is not None and bool(_number_style_registry.roots)


def _apply_media_naming(parsed_by_path, originals, series_name, destination, transient=None):
    """Name arrivals under the final resolved destination's media policy.

    Pre-rewrite metadata stays bound to each parsed original_path. A policy
    refusal keeps the archive in intake for review. Existing tag-sensitive
    landing and no-overwrite rules still own the actual rename.

    *transient*, when given, collects the refusals that can clear without
    any file changing: an OSError (a locked archive, a failed rename) or a
    style the registry could not select (a root can be briefly
    inaccessible). The caller leaves those where they are to be retried,
    instead of sending them to review as a decision (``_dispose``).
    """
    if not _media_naming_enabled():
        return set(), None
    try:
        selection = _number_style_registry.select(destination, _number_style_requested)
    except (OSError, NumberStyleRefused) as exc:
        log.warning("  REVIEW: media naming refused: %s", exc)
        if transient is not None:
            transient.update(parsed_by_path)
        return set(parsed_by_path), None
    log.info("  Media naming: %s (%s), registry SHA256 %s",
             selection.style, selection.source, selection.registry_sha256)
    refused = set()
    for path, parsed in list(parsed_by_path.items()):
        try:
            fact = originals[parsed.original_path]
            # Generic placeholders need the resolved directory number below.
            if not parsed.chapter and not parsed.volume and _PLACEHOLDER_STEM_RE.match(parsed.stem):
                continue
            fields, xml_year = watcher_naming(parsed, fact, series=series_name, style=selection.style)
            name = format_canonical_name(fields)
            target = path.with_name(name)
            patched = _dataclasses_replace(parsed, filename=name, series=fields.series + (f" ({fields.series_year})" if fields.series_year else ""),
                chapter=fields.issue or fields.chapter, volume=fields.volume)
            if xml_year is not None:
                # Persist and read back the issue year while the archive name
                # still carries it; a failure refuses before any rename.
                _record_comicinfo_year(path, xml_year)
            if name != path.name:
                if not _defers_rename(path.stem, target.stem):
                    rename_arrival(path, target)
                    del parsed_by_path[path]
                    path = target
                elif target.exists():
                    raise CanonicalNameError("media destination already exists")
            parsed_by_path[path] = patched
            process_comicinfo(path, patched)
        except KeepArrivalName as exc:
            # Keep the existing parsed/deferred display and its distinguishing
            # number; this is no new field or family authority.
            log.info("    Media naming kept legacy arrival %s: %s", path.name, exc)
        except CaseRenameRecoveryError as exc:
            retained = exc.retained_path
            del parsed_by_path[path]
            parsed_by_path[retained] = _dataclasses_replace(parsed, filename=retained.name)
            refused.add(retained)
            if transient is not None:
                transient.add(retained)
            log.warning("    REVIEW: %s", exc)
        except (OSError, ValueError, KeyError) as exc:
            log.warning("    REVIEW: media naming refused for %s: %s", path.name, exc)
            refused.add(path)
            if transient is not None and isinstance(exc, OSError):
                transient.add(path)
    return refused, selection


def _read_comicinfo_entry(cbz_path):
    with zipfile.ZipFile(cbz_path, "r") as zf:
        entry = next((n for n in zf.namelist() if os.path.basename(n).lower() == "comicinfo.xml"), None)
        return entry, (zf.read(entry).decode("utf-8", errors="replace") if entry else None)


def _record_comicinfo_year(cbz_path, year):
    """Persist the issue year a reboot series-year filename omits in <Year>.

    Called before the media rename, while the arrival name still carries the
    year. The writer can give up after its retries without raising, so the
    Year is read back: if it is not there, OSError keeps the archive in intake
    under its year-bearing name. An existing different Year refuses
    (ValueError) instead of replacing it.
    """
    entry, xml_text = _read_comicinfo_entry(cbz_path)
    new_xml = with_comicinfo_year(xml_text if entry else COMICINFO_TEMPLATE, year)
    if new_xml is not None:
        _write_cbz_with_comicinfo(cbz_path, new_xml, replace_entry=entry)
    entry, xml_text = _read_comicinfo_entry(cbz_path)
    if entry is None or with_comicinfo_year(xml_text, year) is not None:
        raise OSError(f"ComicInfo Year {year} was not persisted; keeping the year-bearing name")


def _apply_fallback_naming(
    parsed_by_path: dict[Path, ParsedComicName],
    series_name: str,
    dir_number: str | None,
    *, number_style="chapter",
) -> list[Path]:
    """Rename placeholder-named files (see ``_PLACEHOLDER_STEM_RE``) to the
    resolved series name, with the chapter number implied by the enclosing
    directory (``dir_number``, from ``_resolve_series_dir_name``) when one was
    found. The name comes from the canonical formatter
    (``_fallback_display_stem``): ``Series ch. 4 (2019) (Uncensored) [v2]``.

    Without this, files that arrive with an uninformative, group-generic name
    and no chapter number of their own (e.g. every release from a given
    uploader named identically) end up with identical filenames across
    different issues, silently colliding — and one being discarded — when
    their directories are later merged into the same series folder.

    A placeholder is recognised on the cleaned stem as before, or on the
    archive name with every bracketed block removed, so a display token such
    as ``(Uncensored)`` cannot hide one. ``dir_number`` is always a chapter
    here: a folder named with a volume (``v3``, ``Vol. 3``) makes
    ``parsed.volume`` non-empty, and those archives are skipped, as before.

    When the formatter refuses (``FallbackRefused``) the archive keeps its
    current name, a review reason is logged, and its path is returned. The
    caller must then **not** merge it into the library: a placeholder that
    keeps its generic name is exactly what collides with the next chapter
    that shares it. The returned list is empty when nothing was refused.

    Mutates *parsed_by_path* in place so later lookups by the caller see the
    renamed path.
    """
    refused: list[Path] = []
    for cbz, parsed in list(parsed_by_path.items()):
        if not cbz.exists() or parsed.chapter or parsed.volume:
            continue
        if not (
            _PLACEHOLDER_STEM_RE.match(parsed.stem)
            or _PLACEHOLDER_STEM_RE.match(sanitize_without_markers(cbz.stem))
        ):
            continue

        # The ComicInfo title keeps the pre-§5.5 form; only the file name
        # carries the display tokens (see ParsedComicName).
        # A range folder ("Berserk Ch. 1-30") labels the name with its range,
        # which is never a chapter field (the ComicInfo chapter stays as is).
        # A volume range has no chapter slot to carry it: such a placeholder
        # is reviewed, never named after the series alone.
        folder_range = None if dir_number else range_in(cbz.parent.name)
        if folder_range is not None and folder_range[0] == "vol":
            log.warning(
                f"    REVIEW: fallback name refused for '{cbz.name}' (series "
                f"'{series_name}', volumes {folder_range[1]}): a volume range "
                f"has no chapter number. Keeping its current name."
            )
            refused.append(cbz)
            continue
        label = dir_number or (folder_range[1] if folder_range else None)
        new_stem = f"{series_name} Ch. {label}" if label else series_name
        display_stem = _fallback_display_stem(series_name, label, cbz.stem,
                                               number_style=number_style)
        if isinstance(display_stem, FallbackRefused):
            log.warning(
                f"    REVIEW: fallback name refused for '{cbz.name}' "
                f"(series '{series_name}', chapter {label}): "
                f"{display_stem.reason}. Keeping its current name."
            )
            refused.append(cbz)
            continue
        new_name = display_stem + cbz.suffix
        if new_name == cbz.name:
            continue

        new_path = cbz.parent / new_name
        if new_path.exists():
            log.warning(
                f"    Fallback rename skipped: target already exists '{new_name}' "
                f"(keeping placeholder name '{cbz.name}')"
            )
            continue

        patched = _dataclasses_replace(
            parsed,
            chapter=dir_number or parsed.chapter,
            filename=new_name,
            stem=new_stem,
        )
        if _defers_rename(cbz.stem, new_path.stem):
            # As in process_cbz_file: a name that drops a distinguishing tag
            # is applied only when the archive lands (_stripped_arrivals).
            log.info(
                f"    Rename deferred to landing (placeholder fallback): "
                f"'{cbz.name}' -> '{new_name}'"
            )
            process_comicinfo(cbz, patched)
            parsed_by_path[cbz] = patched
            continue

        cbz.rename(new_path)
        log.info(f"    Renamed (placeholder fallback): '{cbz.name}' -> '{new_name}'")

        process_comicinfo(new_path, patched)
        del parsed_by_path[cbz]
        parsed_by_path[new_path] = patched
    return refused


def _backfill_chapter_one(dest_dir: Path, series_name: str) -> None:
    """When a chapter-numbered file is about to merge into *dest_dir*, retroactively
    label a lone bare-named sibling (e.g. an earlier chapter that arrived before any
    numbering was known, see ``_apply_fallback_naming``) as "Ch. 1" so both chapters
    end up with distinct, stable names instead of one being liable to collide with —
    and be silently discarded in favour of — the other.

    The "Ch. 1" name is formatted like every fallback name
    (``_fallback_display_stem``), so a backfilled first chapter and the
    fallback-named second one share one spelling. Note this renames a file
    that is already in the library, as it did before design §5.5; only the
    target spelling changed (``ch. 1``). It is not a bulk canonical rename.
    When the formatter refuses, nothing is renamed and a review reason is
    logged: the sibling keeps its bare name, which still differs from the
    incoming numbered chapter's, so the merge cannot collide on it.
    """
    if not dest_dir.is_dir():
        return
    bare_key = normalise_archive_key(series_name)
    for existing in dest_dir.iterdir():
        if not (existing.is_file() and existing.suffix.lower() in {".cbz", ".cbr"}):
            continue
        if normalise_archive_key(existing.stem) != bare_key:
            continue
        display_stem = _fallback_display_stem(series_name, "1", existing.stem)
        if isinstance(display_stem, FallbackRefused):
            log.warning(
                f"  REVIEW: chapter-1 backfill refused for '{existing.name}': "
                f"{display_stem.reason}. Not renamed."
            )
            return
        target = existing.with_name(display_stem + existing.suffix)
        if target.exists():
            return
        existing.rename(target)
        log.info(f"  Backfilled chapter number: '{existing.name}' -> '{target.name}'")
        return


# Minimum size gain before an incoming archive may replace an existing one
# of the same name.
#
# Measured across 1,261 conflict replacements in the watcher logs (2026-03 to
# 2026-08): 15.1% gained under 1 KB, and the smallest were +1, +2 and +6 bytes.
# A gain that small cannot be a better scan -- it is a repack, a rewritten
# ComicInfo.xml, or different zip metadata around identical pages. Replacing on
# it discards a working archive and, because the database has already hashed
# the old file, silently invalidates its recorded page inventory, archive hash
# and content signature.
#
# 10 KB is where the distribution turns: it blocks 19.1% of past replacements,
# while 20 KB blocks only 0.9% more (11 of 1,261) and 100 KB starts refusing
# gains large enough to be real. Size remains a proxy for quality, not
# evidence of it -- this threshold removes the obvious noise, it does not make
# "larger" mean "better".
REPLACEMENT_MIN_GAIN_BYTES = 10 * 1024


def _replacement_gain_is_meaningful(
    src_size: int,
    dest_size: int,
    min_gain: int = REPLACEMENT_MIN_GAIN_BYTES,
) -> bool:
    """Whether *src_size* beats *dest_size* by enough to justify replacing.

    One definition for a question asked at two call sites, which previously
    each carried their own copy of `src_size > dest_size`.
    """
    return (src_size - dest_size) >= min_gain


def _reserve(path: Path, *, numbered: bool = True) -> Path | None:
    """Create *path* exclusively and return it: this placement now owns the name.

    Choosing a free name and then moving into it is a race: two placements
    (two admitted directories, or two watchers) can choose the same name,
    and the second move overwrites the first. An exclusive create
    (``O_CREAT | O_EXCL``) cannot be won twice. With *numbered*, a taken
    name is followed by ``<stem> (2)``, ``(3)`` ... until one is created;
    without it, None says the name is taken. The empty file is then
    replaced by the archive, and on failure removed: it is the only path a
    failed placement may clean up, because it is the only one it created.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = unique_path(path) if numbered else path
    index = 2
    while True:
        try:
            os.close(os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return candidate
        except FileExistsError:
            if not numbered:
                return None
            candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
            index += 1


# One lock per library destination being settled, keyed by its normalized
# physical path. Admitted directories run on independent timer threads, so
# two of them can reach one library name at once; whichever owns it settles
# it, and the other defers to its next pass.
_destination_locks: dict[str, list] = {}  # key -> [lock, users]
_destination_locks_guard = threading.Lock()


@contextlib.contextmanager
def _destination_lock(dest: Path):
    """Own *dest* for one whole settlement; yields whether ownership was won.

    Acquired **before** anything about *dest* is decided and held until the
    settlement is complete: the collision decision, quarantining the
    library copy, landing the arrival, a rollback and any cleanup. A
    separate check ("is anyone landing here?") goes stale the moment it is
    made; ownership cannot. Another thread that wants *dest* meanwhile does
    not wait: it defers its arrival, untouched, to its next pass. Paths are
    normalized (resolved, and case-folded on Windows, whose filesystem is
    case-insensitive), so two spellings of one file share one lock.

    Scope: threads of this watcher process, the single watcher-owned
    pipeline (one watcher per watch root, ``watcher_instance_lock``).
    Another process writing the same library names is not serialized by
    this; it would need its own coordination.
    """
    key = os.path.normcase(os.path.realpath(dest))
    with _destinations_guard_lookup(key) as lock:
        if not lock.acquire(blocking=False):
            yield False
            return
        try:
            yield True
        finally:
            lock.release()


@contextlib.contextmanager
def _destinations_guard_lookup(key: str):
    """The lock for *key*, kept in the registry only while someone uses it."""
    with _destination_locks_guard:
        entry = _destination_locks.setdefault(key, [threading.Lock(), 0])
        entry[1] += 1
    try:
        yield entry[0]
    finally:
        with _destination_locks_guard:
            entry[1] -= 1
            if entry[1] == 0 and _destination_locks.get(key) is entry:
                del _destination_locks[key]


# Suffix of a file being copied into the library from another volume. It
# never ends in .cbz, so nothing reads it as a book.
LANDING_SUFFIX = ".cbz-landing"


def _same_volume(src: Path, dest_dir: Path) -> bool:
    try:
        return os.stat(src).st_dev == os.stat(dest_dir).st_dev
    except OSError:
        return False


def _land(src: Path, dest: Path) -> bool:
    """Move *src* to the free name *dest*. The caller owns *dest* (``_destination_lock``).

    The name is also created exclusively first (``_reserve``), so a writer
    that does not take the lock still cannot be overwritten. False, with
    *src* left in place, when *dest* is taken. A failed move removes only
    this placement's reservation (or its own partial copy) and re-raises,
    as the plain move did; with the lock held, nothing else can have taken
    the name meanwhile.
    """
    if _reserve(dest, numbered=False) is None:
        log.warning(f"    '{dest.name}' was taken by another arrival meanwhile; "
                    f"'{src}' stays for the next pass.")
        return False
    partial = dest.with_name(f".{dest.name}{LANDING_SUFFIX}")
    try:
        if _same_volume(src, dest.parent):
            shutil.move(str(src), str(dest))
        else:
            # Across volumes (the library on Tower) the copy goes to a hidden
            # partial name, is flushed, and replaces the reservation in one
            # rename; only then is *src* removed. The name therefore only
            # ever holds the empty reservation or the complete archive, and
            # *src* survives until it is complete.
            partial.unlink(missing_ok=True)
            shutil.copy2(src, partial)
            with open(partial, "rb+") as stream:
                os.fsync(stream.fileno())
            os.replace(partial, dest)
            src.unlink()
    except (OSError, shutil.Error):
        if src.exists():
            dest.unlink(missing_ok=True)
            partial.unlink(missing_ok=True)
        raise
    return True


def _clear_stale_landings(dest_folder: Path) -> None:
    """Remove what a crashed landing left in *dest_folder*'s series folders.

    A crash mid-landing leaves at most an empty reservation under the final
    name, or (across volumes) a hidden ``.<name>.cbz-landing`` partial copy;
    the arrival itself is still in its claim. Neither is ever a book, so both
    are removed, each under that name's destination lock: a landing still in
    progress on another thread owns its name and is skipped.
    """
    try:
        series_dirs = [d for d in dest_folder.iterdir() if d.is_dir()]
    except OSError:
        return
    for series_dir in series_dirs:
        stale = [p for p in series_dir.glob("*.cbz") if p.is_file() and p.stat().st_size == 0]
        stale += [p.with_name(p.name[1:-len(LANDING_SUFFIX)])
                  for p in series_dir.glob(f".*{LANDING_SUFFIX}")]
        for dest in stale:
            with _destination_lock(dest) as owned:
                if not owned:
                    continue
                partial = dest.with_name(f".{dest.name}{LANDING_SUFFIX}")
                try:
                    if dest.is_file() and dest.stat().st_size == 0:
                        dest.unlink()
                        log.warning(f"  Removed an empty reservation left by a crash: {dest}")
                    if partial.exists():
                        partial.unlink()
                        log.warning(f"  Removed a partial copy left by a crash: {partial}")
                except OSError as e:
                    log.error(f"  Could not clear a crashed landing at {dest}: {e}")


def _settle_or_land(src: Path, dest: Path, *, label: str, series_dir: str,
                    display_name: str | None, conflict_quarantine_root: Path | None,
                    quarantine_name, protected: bool = False,
                    holds: set | None = None) -> bool:
    """Land *src* on *dest*, or settle their collision, owning *dest* throughout.

    The one path every mover takes for every file (both movers, legacy and
    resolver settlement, new names and collisions). Returns True when the
    file completed: it landed, its collision was settled, or it was removed
    as an exact duplicate. False leaves *src* where it is: *dest* is owned
    by another settlement (deferred to the next pass), a stripped-tag
    collision is held (issue #131), or the settlement failed.
    """
    with _destination_lock(dest) as owned:
        if not owned:
            log.warning(f"    '{dest.name}' is being settled by another arrival; "
                        f"'{label}' waits for the next pass.")
            return False
        if not dest.exists():
            return _land(src, dest)
        if ((display_name is not None and _hold_stripped_collision(src, dest, display_name))
                or (protected and _hold_protected_collision(src, dest))):
            # A decision, not a failure: reported so the caller can send it
            # to review rather than retry it.
            if holds is not None:
                holds.add(src)
            return False
        if conflict_quarantine_root is not None:
            outcome = settle_file_conflict(
                src, dest, conflict_quarantine_root / series_dir / quarantine_name)
            action = outcome["action"]
            if action == "deleted_exact_duplicate":
                log.info(f"    Exact duplicate removed: '{label}'")
            else:
                log.warning(f"    Conflict '{label}': {action}; smaller file quarantined at "
                            f"'{outcome['quarantined']}'.")
            return True
        return _settle_by_size(src, dest, label, series_dir)


def _settle_by_size(src: Path, dest: Path, label: str, series_dir: str) -> bool:
    """Settle a name collision by size without deleting a different book.

    Byte-identical copies are an exact duplicate: the incoming one is removed,
    which loses nothing. Otherwise the larger copy is kept under *dest*'s name
    (an incoming copy must win by ``REPLACEMENT_MIN_GAIN_BYTES``) and the
    other is moved to ``QUARANTINE_ROOT/<date>/<series_dir>/`` under a name
    this placement owns (``_reserve``). Size is only a proxy for quality, so
    the loser is kept for review.

    The loser is quarantined before anything else moves. If that fails, both
    copies stay where they are and the incoming one is retried on a later
    pass. If the incoming copy then cannot take *dest*'s place, the quarantined
    library copy is moved back; either way no copy is lost.

    Returns True only when the collision was settled (duplicate removed,
    loser quarantined, or replacement done); False leaves the arrival in
    place, and the caller must not count it as landed.
    """
    src_size, dest_size = src.stat().st_size, dest.stat().st_size
    if src_size == dest_size and digest(src) == digest(dest):
        src.unlink()
        log.info(f"    Exact duplicate removed: '{label}'")
        return True
    incoming_wins = _replacement_gain_is_meaningful(src_size, dest_size)
    loser = dest if incoming_wins else src
    try:
        target = _reserve(QUARANTINE_ROOT / time.strftime("%Y-%m-%d") / series_dir / loser.name)
    except OSError as e:
        log.error(f"    Conflict '{label}': could not reserve a quarantine name ({e}); "
                  f"both copies kept, incoming left in place for the next pass.")
        return False
    try:
        shutil.move(str(loser), str(target))
    except (OSError, shutil.Error) as e:
        if loser.exists():
            target.unlink(missing_ok=True)  # this placement's own reservation or partial copy
        log.error(f"    Conflict '{label}': could not quarantine '{loser}' ({e}); "
                  f"both copies kept, incoming left in place for the next pass.")
        return False
    if not incoming_wins:
        log.warning(f"    Conflict '{label}': existing ({dest_size:,} B) kept over incoming "
                    f"({src_size:,} B); incoming quarantined at '{target}'.")
        return True
    try:
        landed = _land(src, dest)
    except (OSError, shutil.Error) as e:
        log.error(f"    Conflict '{label}': could not move the incoming copy into place ({e}); "
                  f"restoring the existing copy from '{target}'.")
        landed = False
    if not landed:
        if _land(target, dest):
            log.info(f"    Restored the existing copy of '{label}'.")
        else:
            log.error(f"    Conflict '{label}': another arrival took the name; the existing "
                      f"copy stays quarantined at '{target}'.")
        return False
    log.warning(f"    Conflict '{label}': incoming ({src_size:,} B) replaced existing "
                f"({dest_size:,} B); existing quarantined at '{target}'.")
    return True
def _defers_rename(current_stem: str, new_stem: str) -> bool:
    """Whether renaming *current_stem* to *new_stem* drops a distinguishing tag.

    True when *current_stem* carries a tag ``dropped_round_tags`` reports
    (``(s2)``, ``(1)``, ``(Inuyasha)``) **and** its collision key differs
    from *new_stem*'s. The second test is the one issue #131 proposes: an
    existing archive that the new name would meet, but the current name
    would not, is met only because of the stripping, so it is not evidence
    of the same book.

    Both tests are needed. Without the first, a rename that only normalises
    (``Solo_Leveling_Chapter_4`` -> ``Solo Leveling ch. 4``) would be
    deferred and held, and those arrivals are exactly the same-book upgrades
    the keep-the-larger merge is for. Without the second, a tag the name
    kept in another form would count as stripped.

    Such a rename is deferred until the archive lands (``process_cbz_file``,
    ``_apply_fallback_naming``), so the tag stays on disk, in the import
    folder, for as long as the archive does. That is what lets a hold
    survive a failure, a retry and a restart: every pass reads the tag from
    the file name again, and there is never a rename to undo.
    """
    if not dropped_round_tags(current_stem):
        return False
    return _archive_collision_key(current_stem) != _archive_collision_key(new_stem)


def _stripped_arrivals(
    parsed_by_path: dict[Path, ParsedComicName],
) -> dict[Path, str]:
    """Archives whose rename was deferred because it strips a tag.

    Maps each archive's path, still under its arrival name, to the display
    name it lands under (``ParsedComicName.filename``). The moves apply that
    name as the archive lands, or hold the archive instead
    (``_hold_stripped_collision``) when the name meets a different existing
    archive.

    An archive whose display name is already taken in the import folder is
    not listed: its rename was skipped, not deferred, so it lands under its
    own name, as it always has.
    """
    stripped: dict[Path, str] = {}
    for path, parsed in parsed_by_path.items():
        display = getattr(parsed, "filename", None)
        if display is None or display == path.name:
            continue
        if not _defers_rename(path.stem, Path(display).stem):
            continue
        if any(
            (path.parent / name).exists()
            for name in {display, normalize_apostrophes(display)}
        ):
            continue
        stripped[path] = display
    return stripped


def _hold_stripped_collision(src: Path, dest: Path, display_name: str) -> bool:
    """Hold *src* in the import folder instead of landing it on *dest*.

    Called only for an archive whose rename was deferred
    (``_stripped_arrivals``) and whose display name *display_name* meets
    the existing archive *dest*. Returns ``False``, holding nothing, only
    when the two provably hold the same bytes: an exact duplicate is removed
    as before by the caller's own conflict handling. Otherwise the existing
    archive is left untouched, *src* stays where it is under its arrival
    name, and ``True`` is returned.

    Nothing is renamed or written, so a hold cannot fail part-way, and the
    next settle pass or restart reads the same tag from the same name and
    holds again. A comparison that fails (an ``OSError`` reading either
    file) cannot show that the two are the same book, so it holds as well.
    """
    try:
        same = (
            src.stat().st_size == dest.stat().st_size
            and digest(src) == digest(dest)
        )
    except OSError as e:
        log.error(
            f"    Could not compare '{src.name}' with '{dest}' "
            f"({type(e).__name__}: {e}); holding it."
        )
        same = False
    if same:
        return False
    log.warning(
        f"    HELD FOR REVIEW: '{src.name}' would land as '{display_name}', "
        f"which collides with the different existing '{dest}'. Existing left "
        f"untouched; incoming kept in the import folder as '{src}' (issue #131)."
    )
    return True


def _hold_protected_collision(src: Path, dest: Path) -> bool:
    """Leave a protected arrival where it is instead of settling it by size.

    A protected arrival is another copy of a chapter, or any archive of a
    resumed claim (whose earlier decisions may not have been recorded). Which
    of two different copies belongs in the library is then the operator's
    decision; the caller sends the arrival to review, the library copy left
    untouched. Returns ``False`` only for the same bytes, an exact duplicate
    the caller's own handling removes without losing anything; a failed
    comparison holds, as in ``_hold_stripped_collision``.
    """
    try:
        same = src.stat().st_size == dest.stat().st_size and digest(src) == digest(dest)
    except OSError as e:
        log.error(f"    Could not compare '{src.name}' with '{dest}' "
                  f"({type(e).__name__}: {e}); holding it.")
        same = False
    if same:
        return False
    log.warning(f"    HELD FOR REVIEW: '{src.name}' meets the different '{dest}'; "
                f"library copy left untouched (issue #174).")
    return True


def _land_deferred_names(
    src_dir: Path, dest_dir: Path, stripped_arrivals: dict[Path, str] | None
) -> None:
    """Give the archives of a directory moved whole their deferred names.

    ``_move_cbz_dir`` moves a directory whole only when its series folder
    did not exist, so nothing already in the library can collide with it:
    the archives carry their arrival names in and are renamed here. A
    display name already taken in the new folder (two arrivals that strip
    to one name), or a rename that fails, leaves that archive under its
    arrival name. Both books are kept; only the name is not the display one.
    """
    for src, display_name in (stripped_arrivals or {}).items():
        try:
            landed = dest_dir / src.relative_to(src_dir)
            target = landed.with_name(display_name)
            if not landed.is_file():
                continue
            if target.exists() or _find_archive_collision(
                target.parent, target.stem
            ) is not None:
                log.warning(
                    f"    Kept arrival name '{landed.name}': its display name "
                    f"'{display_name}' is taken in '{dest_dir}'."
                )
                continue
            landed.rename(target)
            log.info(f"    Renamed on landing: '{landed.name}' -> '{display_name}'")
        except (OSError, ValueError) as e:
            log.warning(
                f"    Kept arrival name for '{src.name}' in '{dest_dir}': "
                f"{type(e).__name__}: {e}"
            )


def _merge_directories(
    src_dir: Path,
    dest_dir: Path,
    conflict_quarantine_root: Path | None = None,
    stripped_arrivals: dict[Path, str] | None = None,
    held: frozenset[Path] = frozenset(),
    protected: frozenset[Path] = frozenset(),
    holds: set | None = None,
) -> int:
    """Recursively merge *src_dir* into *dest_dir*; return how many files completed.

    Legacy imports retain the measured replacement threshold. Resolver imports
    compare exact duplicates and quarantine the smaller non-identical file
    outside the library instead of discarding it. Legacy imports quarantine
    the copy they do not keep too (``_settle_by_size``): no mode deletes a
    copy that differs from the one it keeps.

    *stripped_arrivals* (``_stripped_arrivals``) names the archives whose
    match with an existing file may exist only because a tag was stripped.
    Such a conflict with different bytes is held for review in either mode
    (``_hold_stripped_collision``), so neither keep-the-larger branch can
    replace, quarantine or delete a different book. Every other conflict is
    settled exactly as before.

    *held* names archives the caller is keeping in the import folder for
    review (a refused name, or a second copy of a chapter). They are never
    moved, replaced or deleted here, so ``_remove_merged_source`` finds them
    still in *src_dir* and leaves it in place.

    A file completes when it lands, when its collision is settled, or when
    it is removed as an exact duplicate. A held, deferred or failed file
    does not; ``_move_cbz_dir`` gives no landing receipt for a merge in which
    nothing completed.

    A *protected* archive (another copy of a chapter, or any archive of a
    resumed claim) that meets a different existing file is left in *src_dir*
    too (``_hold_protected_collision``): which copy belongs in the library is
    the operator's decision, so the caller sends it to review.
    """
    stripped_arrivals = stripped_arrivals or {}
    completed = 0
    for src_item in iter_paths_excluding_directories(src_dir):
        relative  = src_item.relative_to(src_dir)
        dest_item = dest_dir / relative

        if src_item.is_dir():
            dest_item.mkdir(parents=True, exist_ok=True)
            continue
        if src_item in held:
            continue

        # A deferred rename (_stripped_arrivals) is applied here, as the
        # archive lands, so the conflict checks below read its display name.
        display_name = stripped_arrivals.get(src_item)
        if display_name is not None:
            dest_item = dest_item.with_name(display_name)

        # When the exact name is free, still treat a file that normalises to the
        # same key (differs only by spacing/punctuation) as a conflict so the two
        # cosmetic variants don't both land in the destination.
        if not dest_item.exists() and src_item.suffix.lower() in {".cbz", ".cbr"}:
            match = _find_archive_collision(dest_item.parent, dest_item.stem)
            if match is not None:
                dest_item = match

        completed += _settle_or_land(
            src_item, dest_item, label=str(relative), series_dir=dest_dir.name,
            display_name=display_name, conflict_quarantine_root=conflict_quarantine_root,
            quarantine_name=relative, protected=src_item in protected, holds=holds)
    return completed


# ─────────────────────────────────────────────
# ROUTING & DIRECTORY MOVE
# ─────────────────────────────────────────────
def _load_routing() -> None:
    """Load routing.json and populate module-level routing state."""
    global _routing_destinations, _routing_rules, _routing_default
    global _routing_default_key
    try:
        with open(ROUTING_FILE, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        _routing_destinations = cfg.get('destinations', {})
        _routing_rules        = cfg.get('rules', [])
        default_key           = cfg.get('default', '')
        _routing_default_key  = default_key
        _routing_default      = _routing_destinations.get(default_key, '')
        if not _routing_default:
            log.warning(f"  routing.json: default key '{default_key}' not found in destinations.")
        for dest_path in _routing_destinations.values():
            os.makedirs(dest_path, exist_ok=True)
        log.info(f"  Routing   : {len(_routing_rules)} rule(s) loaded from {ROUTING_FILE}")
    except FileNotFoundError:
        log.warning(f"  routing.json not found at {ROUTING_FILE} — all files will go to default dest.")
        _routing_default = ''
        _routing_default_key = ''
    except Exception as e:
        log.error(f"  Failed to load routing.json: {e}")
        _routing_default = ''
        _routing_default_key = ''


def _load_series_resolvers() -> None:
    """Load or build the enabled Z-library registries.

    Initialization is intentionally fatal when enabled. Silently falling back
    to X staging because Z is disconnected would make an apparently successful
    watcher run accumulate exactly the manual cleanup this mode replaces.
    """
    global _series_resolvers
    _series_resolvers = {}
    if not SERIES_RESOLVER_ENABLED:
        return
    for category, root in SERIES_RESOLVER_ROOTS.items():
        resolver = SeriesResolver(
            root,
            threshold=SERIES_RESOLVER_THRESHOLD,
            quarantine_root=SERIES_RESOLVER_QUARANTINE_ROOT / root.name,
            refresh=SERIES_RESOLVER_REFRESH,
            repair_apostrophes=SERIES_RESOLVER_NORMALIZE_APOSTROPHES,
        )
        _series_resolvers[category] = resolver
        log.info(
            f"  Series resolver: {category} -> {resolver.root} "
            f"({resolver.registry.get('series_count', 0)} series, "
            f"{resolver.registry.get('alias_count', 0)} aliases)"
        )
        normalization = resolver.apostrophe_normalization
        if normalization is not None:
            log.info(
                f"  Apostrophe normalization: {category} repaired "
                f"{len(normalization.get('actions', []))} item(s); "
                f"{len(normalization.get('errors', []))} error(s)"
            )


def _load_routing_v2() -> None:
    """Build the v2 router once, if a mode was asked for.

    Deliberately non-fatal. v2 controls nothing on this branch, so a missing
    or invalid v2 config must not stop the watcher from ingesting on legacy
    routing. The one thing it will not do is run degraded and quiet: a failure
    is logged as an error and v2 stays off.
    """
    global _router
    _router = None
    if ROUTING_V2_MODE == "off":
        return
    try:
        from scripts.cbz_watcher_router import WatcherRouter
        _router = WatcherRouter.load(ROUTING_V2_FILE, mode=ROUTING_V2_MODE,
                                     sample_limit=ROUTING_V2_SAMPLE)
    except Exception as e:                       # noqa: BLE001 — never fatal
        log.error(f"  Routing v2 disabled: {type(e).__name__}: {e}")
        _router = None


def _source_folder_name(comic_dir: Path) -> str:
    """The immediate child of WATCH_FOLDER that *comic_dir* arrived under.

    A claimed payload is no longer under WATCH_FOLDER: its source was
    recorded when it was claimed (``_claim_sources``) and is used instead.
    """
    key = os.path.normcase(str(comic_dir))
    with _claim_sources_lock:
        for claim_key, (source, _arrival) in _claim_sources.items():
            if key == claim_key or key.startswith(claim_key + os.sep):
                return source
    watch = Path(WATCH_FOLDER)
    for candidate in [comic_dir] + list(comic_dir.parents):
        if candidate.parent == watch:
            return candidate.name
    return comic_dir.name


def _arrival_path(comic_dir: Path) -> Path:
    """Where *comic_dir* arrived in the watch folder (itself if never claimed)."""
    key = os.path.normcase(str(comic_dir))
    with _claim_sources_lock:
        for claim_key, (_source, arrival) in _claim_sources.items():
            if key == claim_key or key.startswith(claim_key + os.sep):
                return arrival
    return comic_dir


def _shadow_route(comic_dir: Path, legacy_dest: str, series_name: str,
                  archives: list[Path], results: list,
                  handoffs: list | None = None,
                  mixed_parent: bool = False) -> None:
    """Log how v2 would have classified this batch. Changes nothing.

    Called after the series identity is resolved and the archives have been
    renamed, because both change the answer: an arrival named "Berserk Ch. 4"
    is filed under "Berserk" and would otherwise miss the existing-series
    index hit, and a mixed drop point's loose files must not be classified
    from a nested series' metadata.
    """
    if _router is None or not _router.enabled:
        return
    comparison, error = None, ""
    try:
        comparison = _router.shadow(comic_dir, _source_folder_name(comic_dir),
                                    legacy_dest, series_name, archives)
        if comparison is not None:
            results.append(comparison)
    except Exception as e:                       # noqa: BLE001 — never fatal
        error = f"{type(e).__name__}: {e}"
        log.error(f"  Routing v2 shadow failed for '{comic_dir.name}': "
                  f"{type(e).__name__}: {e}")
    if handoffs is not None:
        _shadow_handoff(comic_dir, series_name, archives, comparison, error,
                        mixed_parent, handoffs)


def _shadow_handoff(comic_dir: Path, series_name: str, archives: list[Path],
                    comparison, error: str, mixed_parent: bool,
                    handoffs: list) -> None:
    """Record what a staging handoff would do with this arrival. Moves nothing.

    An observation only (design A3.1): the caller's move runs next exactly as
    it would without this call, whatever the outcome. A classification error
    is passed through rather than swallowed, because it is itself an outcome
    (`defer`, classification_failed) and must be counted, not lost.
    """
    try:
        from scripts.cbz_shadow_handoff import observe
        observation = observe(
            comic_dir, series_name, _source_folder_name(comic_dir),
            comparison.decision if comparison is not None else None,
            mixed_parent=mixed_parent, archives=archives, error=error,
            arrival=_arrival_path(comic_dir))
        handoffs.append(observation)
        log.info(f"  {observation.describe()}")
    except Exception as e:                       # noqa: BLE001 — never fatal
        log.error(f"  Staging shadow-plan failed for '{comic_dir.name}': "
                  f"{type(e).__name__}: {e}")


def _note_move(series_name: str, dest_folder: str,
               landed: Path | None) -> None:
    """Record a completed move so the series is sticky for this session.

    *landed* is None when the move failed, in which case nothing is recorded:
    an index entry for a series that is not on disk would hand out authority
    for a directory that does not exist.
    """
    if _router is None or not _router.enabled or landed is None:
        return
    try:
        _router.note_move(series_name, dest_folder, landed)
    except Exception as e:                       # noqa: BLE001 — never fatal
        log.error(f"  Routing v2 index update failed for '{series_name}': "
                  f"{type(e).__name__}: {e}")


def _resolve_route(comic_dir: Path) -> tuple[str, str]:
    """
    Walk up from comic_dir to find the immediate child of WATCH_FOLDER.
    Evaluate routing rules top-to-bottom, first match wins.
    Return ``(destination_key, destination_path)`` and fall back to the
    configured default when no rule matches.
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
            return dest_key, dest_path

    return _routing_default_key, _routing_default or ''


def _resolve_dest(comic_dir: Path) -> str:
    """Return the legacy destination path for compatibility with callers."""
    return _resolve_route(comic_dir)[1]


def _resolve_import_target(
    category: str, title: str, legacy_dest: str
) -> tuple[str, str, dict[str, object], SeriesResolver | None]:
    """Resolve a title across every configured library.

    Existing series identity is stronger evidence than the download source.
    The source category is used only when no live library folder matches. A
    stale registry path is ignored, except when the same folder can be found
    under another configured root after a manual move.
    """
    source_resolver = _series_resolvers.get(category)
    if not _series_resolvers:
        return title, legacy_dest, {"match_type": "resolver_disabled"}, None

    resolved = {
        candidate_category: resolver.resolve(title)
        for candidate_category, resolver in _series_resolvers.items()
    }
    candidates: dict[tuple[str, str], dict[str, object]] = {}

    def _add_candidate(
        evidence_category: str,
        relative_path: object,
        evidence_match_type: str,
        confidence: object,
    ) -> None:
        relative = str(relative_path)
        if Path(relative).name != relative:
            raise ValueError(
                f"Resolver returned a non-series path: {relative!r}"
            )
        evidence_resolver = _series_resolvers[evidence_category]
        locations: list[tuple[str, SeriesResolver]] = []
        if (evidence_resolver.root / relative).is_dir():
            locations.append((evidence_category, evidence_resolver))
        else:
            # A folder manually moved between libraries is useful evidence even
            # before the next registry refresh, provided its canonical name did
            # not change during the move.
            locations.extend(
                (other_category, other_resolver)
                for other_category, other_resolver in _series_resolvers.items()
                if other_category != evidence_category
                and (other_resolver.root / relative).is_dir()
            )
        for selected_category, selected_resolver in locations:
            relocated = selected_category != evidence_category
            rank = 2 if evidence_match_type in {
                "exact_alias", "ambiguous_exact"
            } else 1
            try:
                numeric_confidence = float(confidence)
            except (TypeError, ValueError):
                numeric_confidence = 1.0 if rank == 2 else 0.0
            candidate = {
                "category": selected_category,
                "resolver": selected_resolver,
                "relative_path": relative,
                "confidence": numeric_confidence,
                "rank": rank,
                "match_type": "relocated_series" if relocated else (
                    "exact_alias" if rank == 2 else "fuzzy_alias"
                ),
                "evidence_category": evidence_category,
                "evidence_match_type": evidence_match_type,
            }
            key = (selected_category, relative.casefold())
            previous = candidates.get(key)
            if previous is None or (
                rank,
                numeric_confidence,
            ) > (
                int(previous["rank"]),
                float(previous["confidence"]),
            ):
                candidates[key] = candidate

    for evidence_category, result in resolved.items():
        match_type = str(result.get("match_type", ""))
        if result.get("matched"):
            _add_candidate(
                evidence_category,
                result.get("relative_path"),
                match_type,
                result.get("confidence"),
            )
            continue
        if match_type not in {"ambiguous_exact", "ambiguous_fuzzy"}:
            continue
        for item in result.get("candidates", []):
            if isinstance(item, dict):
                relative_path = item.get("relative_path")
                confidence = item.get("confidence")
            else:
                relative_path = item
                confidence = 1.0
            if relative_path:
                _add_candidate(
                    evidence_category,
                    relative_path,
                    match_type,
                    confidence,
                )

    ranked = sorted(
        candidates.values(),
        key=lambda item: (
            int(item["rank"]),
            float(item["confidence"]),
            str(item["category"]).casefold(),
            str(item["relative_path"]).casefold(),
        ),
        reverse=True,
    )
    exact = [candidate for candidate in ranked if candidate["rank"] == 2]
    selected: dict[str, object] | None = None
    ambiguous: list[dict[str, object]] = []
    if len(exact) == 1:
        selected = exact[0]
    elif len(exact) > 1:
        ambiguous = exact
    elif ranked:
        if len(ranked) == 1 or (
            float(ranked[0]["confidence"])
            - float(ranked[1]["confidence"])
            >= 0.02
        ):
            selected = ranked[0]
        else:
            best = float(ranked[0]["confidence"])
            ambiguous = [
                candidate
                for candidate in ranked
                if best - float(candidate["confidence"]) < 0.02
            ]

    if ambiguous:
        candidate_details = [
            {
                "category": str(candidate["category"]),
                "relative_path": str(candidate["relative_path"]),
                "confidence": round(float(candidate["confidence"]), 4),
                "match_type": str(candidate["match_type"]),
            }
            for candidate in ambiguous[:10]
        ]
        categories = {item["category"] for item in candidate_details}
        ambiguity_type = (
            "ambiguous_cross_library"
            if len(categories) > 1
            else "ambiguous_exact" if exact else "ambiguous_fuzzy"
        )
        return title, legacy_dest, {
            "matched": False,
            "match_type": ambiguity_type,
            "title": title,
            "source_category": category,
            "candidates": candidate_details,
        }, None

    if selected is not None:
        selected_resolver = selected["resolver"]
        assert isinstance(selected_resolver, SeriesResolver)
        target_name = str(selected["relative_path"])
        result = {
            "matched": True,
            "match_type": str(selected["match_type"]),
            "confidence": round(float(selected["confidence"]), 4),
            "title": title,
            "relative_path": target_name,
            "path": str(selected_resolver.root / target_name),
            "source_category": category,
            "selected_category": str(selected["category"]),
            "evidence_category": str(selected["evidence_category"]),
            "evidence_match_type": str(selected["evidence_match_type"]),
        }
        return target_name, str(selected_resolver.root), result, selected_resolver

    if source_resolver is None:
        return title, legacy_dest, {"match_type": "resolver_disabled"}, None
    target_name = safe_name(title)
    return target_name, str(source_resolver.root), {
        "matched": False,
        "match_type": "none",
        "title": title,
        "suggested_name": target_name,
        "suggested_path": str(source_resolver.root / target_name),
        "source_category": category,
        "selected_category": category,
    }, source_resolver


# The canonical series identity, not a local copy. Three implementations of
# this rule existed until #44 and agreed only by coincidence; the watcher,
# the lock registry, and SeriesIndex must agree on what "same series" means,
# or the watcher files under one rule while the index records another.
# tests/test_series_key_canonical.py fails if a private implementation
# reappears here.
#
# _MARKER_WORDS_RE is deliberately kept: it is used independently below to
# detect censorship markers in a directory name, which is a different
# question from series identity.
_series_key = series_key


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


def _consistent_comicinfo_series(cbz_files: list[Path]) -> str | None:
    """Return one shared ComicInfo series name, or None when metadata differs."""
    values: dict[str, str] = {}
    for cbz in cbz_files:
        if not cbz.exists():
            continue
        value = _read_comicinfo_series(cbz)
        if value:
            values.setdefault(_series_key(value), value)
    if len(values) != 1:
        return None
    value = next(iter(values.values()))
    return series_base_name(value, bare_numbers=False) or value


def _observed_series_aliases(
    incoming_name: str,
    resolved_name: str,
    final_name: str,
    parsed_values: Iterable[ParsedComicName],
) -> set[str]:
    """Collect folder, translated, romanized, and original-script aliases."""
    aliases = {incoming_name, resolved_name, final_name}
    for parsed in parsed_values:
        aliases.update(
            value
            for value in (
                parsed.series,
                parsed.original_title,
                parsed.translated_title,
            )
            if value
        )
    return aliases


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
    if _media_naming_enabled():
        base, reboot_year = split_series_year(comic_dir.name)
        if reboot_year is not None:
            cleaned = f"{clean_directory_name(base) or base} ({reboot_year})"

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


def _remove_merged_source(src_dir: Path) -> None:
    """Clean up *src_dir* after ``_merge_directories`` emptied it.

    The merge skips excluded directories (Syncthing's ``.stversions`` and
    ``.stfolder``, and the rest of ``DEFAULT_EXCLUDED_DIRECTORIES``), so those
    are still in the source when the merge returns. Deleting the whole tree
    would destroy them. Everything the merge handled has already left the
    source, so only directories that are now empty are removed. Excluded
    subtrees, the directories leading to them, and any file the merge did not
    move stay in place, and are logged.
    """
    if not src_dir.exists():
        return
    excluded: list[Path] = []
    leftover: list[Path] = []
    walked: list[Path] = []
    for directory, directory_names, filenames in os.walk(src_dir, topdown=True):
        current = Path(directory)
        walked.append(current)
        kept_names = []
        for name in directory_names:
            if name.casefold() in DEFAULT_EXCLUDED_DIRECTORIES:
                excluded.append(current / name)
            else:
                kept_names.append(name)
        directory_names[:] = kept_names
        leftover.extend(current / name for name in filenames)
    for directory in reversed(walked):  # deepest first
        try:
            directory.rmdir()
        except OSError:
            pass  # not empty: holds an excluded subtree or a leftover file
    for path in excluded:
        log.info(f"  Left excluded directory in place: {path}")
    for path in leftover:
        log.warning(f"  Left unmerged file in place: {path}")


def _move_cbz_dir(
    dir_path: Path,
    dest_folder: str,
    target_name: str | None = None,
    chapter_number: str | None = None,
    conflict_quarantine_root: Path | None = None,
    stripped_arrivals: dict[Path, str] | None = None,
    held: frozenset[Path] = frozenset(),
    protected: frozenset[Path] = frozenset(),
    holds: set | None = None,
) -> Path | None:
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

    *stripped_arrivals* is passed to ``_merge_directories``; an archive it
    holds for review stays in *dir_path*, which ``_remove_merged_source``
    then leaves in place. A move into a folder that does not exist yet has
    no conflict to hold; its archives whose rename was deferred get their
    display names once the folder has landed (``_land_deferred_names``).

    *held* archives stay in *dir_path* for review (``_merge_directories``).
    With any held, the directory is never moved wholesale -- that would carry
    them into the library -- so the destination is created if needed and the
    rest are merged into it, leaving *dir_path* behind with the held files.
    *protected* is passed to ``_merge_directories``.
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
        if held and not dest_dir.exists():
            dest_dir.mkdir()
            log.info(f"  Created '{dest_dir.name}'; merging all but the {len(held)} held archive(s).")

        if dest_dir.exists():
            if chapter_number:
                _backfill_chapter_one(dest_dir, folder_name)
            log.info(f"  Destination exists - merging, keeping larger files on conflict.")
            completed = _merge_directories(
                dir_path, dest_dir,
                conflict_quarantine_root=conflict_quarantine_root,
                stripped_arrivals=stripped_arrivals,
                held=held,
                protected=protected,
                holds=holds,
            )
            _remove_merged_source(dir_path)
            if not completed:
                log.warning(f"  Nothing from '{dir_path.name}' completed; it stays for the next pass.")
                return None
            log.info(f"  Merge complete.")
            return dest_dir
        else:
            # A whole folder moves only as one rename (same volume, and the
            # destination folder not there yet): atomic, so nothing in it is
            # visible before it is complete. Never shutil.move: across volumes
            # it copies the tree straight into the library, file by file,
            # outside every destination's ownership, where another arrival can
            # settle against a half-copied book.
            try:
                os.rename(dir_path, dest_dir)
                log.info(f"  Moved successfully.")
                _land_deferred_names(dir_path, dest_dir, stripped_arrivals)
                return dest_dir
            except OSError:
                # Another volume (the library on Tower), or the destination
                # appeared meanwhile (WinError 183): merge file by file, each
                # under its own destination lock (_settle_or_land).
                dest_dir.mkdir(parents=True, exist_ok=True)
                if chapter_number:
                    _backfill_chapter_one(dest_dir, folder_name)
                log.info(f"  Not a same-volume rename; merging file by file.")
                completed = _merge_directories(
                    dir_path, dest_dir,
                    conflict_quarantine_root=conflict_quarantine_root,
                    stripped_arrivals=stripped_arrivals,
                    held=held,
                    protected=protected,
                    holds=holds,
                )
                _remove_merged_source(dir_path)
                if not completed:
                    log.warning(f"  Nothing from '{dir_path.name}' completed; it stays "
                                f"for the next pass.")
                    return None
                log.info(f"  Merge complete.")
                return dest_dir

    except Exception as e:
        log.error(f"  Failed to move directory '{dir_path.name}': {e}")

    # The destination is returned only on success, so a caller can record the
    # move without re-deriving where the files went: the merge branches above
    # may land in a pre-existing folder whose name differs from target_name.
    return None


def _move_loose_files(
    files: list[Path],
    dest_folder: str,
    series_name: str,
    conflict_quarantine_root: Path | None = None,
    stripped_arrivals: dict[Path, str] | None = None,
    protected: frozenset[Path] = frozenset(),
    holds: set | None = None,
) -> Path | None:
    """Move individual .cbz files — not their parent directory — into
    ``dest_folder/series_name``, applying the same keep-the-larger-on-conflict
    policy as ``_merge_directories`` on a per-file basis, including its hold
    for a stripped arrival that meets a different book (*stripped_arrivals*,
    issue #131) and for a *protected* arrival (``_hold_protected_collision``).
    A held file stays where it is and is not counted as moved.

    Used for .cbz files that sit directly in a directory which also contains
    other subdirectories that are themselves comic directories (each with
    their own .cbz files). Moving the shared parent directory wholesale would
    sweep those unrelated subdirectories along with it — see the caller in
    ``_process_and_move_directory_inner``.
    """
    existing = _find_existing_series_dir(series_name, dest_folder)
    dest_dir = existing if existing is not None else Path(dest_folder) / series_name
    dest_dir.mkdir(parents=True, exist_ok=True)

    stripped_arrivals = stripped_arrivals or {}
    moved = 0
    for src in files:
        if not src.exists():
            continue

        # A deferred rename (_stripped_arrivals) is applied as the file lands.
        display_name = stripped_arrivals.get(src)
        dest_item = dest_dir / (display_name or src.name)
        if not dest_item.exists():
            match = _find_archive_collision(dest_dir, dest_item.stem)
            if match is not None:
                dest_item = match

        if not _settle_or_land(
                src, dest_item, label=src.name, series_dir=dest_dir.name,
                display_name=display_name, conflict_quarantine_root=conflict_quarantine_root,
                quarantine_name=src.name, protected=src in protected, holds=holds):
            continue  # deferred, held or failed: the arrival stays and is not a landing
        moved += 1

    log.info(f"  Moved {moved} loose file(s) -> {dest_dir}")
    return dest_dir if moved else None


# ─────────────────────────────────────────────
# HELD ARCHIVES: same-chapter copies and unchanged held directories
# ─────────────────────────────────────────────
_BRACKET_TAG_RE = re.compile(r"[\[(]([^\[\]()]*)[\])]")
# One grammar for parts, shared with cbz_core's group-prefix check.
_PART_RE = PART_MARKER_RE


def _duplicate_chapter_copies(
    parsed_by_path: dict[Path, ParsedComicName], series_name: str
) -> list[list[tuple[Path, str]]]:
    """Clusters of same-chapter copies from different groups, best copy first.

    A series folder sometimes arrives holding two scanlations of the same
    chapter (``Easy Going & Endless Abyss Vol.3 Ch.26 - I bet!.cbz`` beside
    ``Team Duwang Vol.3 Ch.26 - Bet.cbz``). Which translation to keep is the
    operator's call, so one copy lands and the others are held in the import
    folder for review (`_second_copies`). Nothing is deleted.

    Two archives are copies only when their names give the same single
    chapter/issue number, the same volume (or none), the same bracketed tags
    (a year ``(2019)`` or season ``(s2)`` apart is another book, not another
    translation), the same ``Part`` markers, and the same
    specials, editions, collection markers and version markers, and their
    pre-number prefixes (the "group") differ. Both groups must also have at
    least two archives
    in the directory. That excludes a folder of unrelated one-shots that each
    happen to be ``#1``, where the prefix is a title, not a group. A prefix
    that is *series_name* plus more words (``Batman Annual`` in the
    ``Batman`` folder) is a different title, so it is never a group either.
    Names whose numbering is ambiguous are left alone for media naming to
    judge.

    Each cluster is ranked deterministically: the copy whose group has the
    most archives in the directory (keeping one translation consistent across
    the run), then the larger file, then the name. Each entry is
    ``(path, group)``; every cluster spans at least two groups.
    """
    series = display_key(split_series_year(series_name)[0])
    facts: dict[Path, tuple[tuple, str]] = {}
    for path in parsed_by_path:
        if not path.exists():
            continue
        tokens = _media_fr.parse_name_tokens(path.name)
        group = display_key(tokens.series or "")
        if series and group != series and group.startswith(series):
            continue  # "Batman Annual" beside "Batman": another title
        numbers = distinct_numbers(value for _kind, value in tokens.primary_numbers)
        volumes = distinct_numbers(tokens.volume_tokens)
        if len(numbers) != 1 or len(volumes) > 1:
            continue
        brackets = frozenset(display_key(tag) for tag in _BRACKET_TAG_RE.findall(path.stem))
        parts = frozenset(_PART_RE.findall(path.stem.casefold()))
        key = (numbers[0], volumes[0] if volumes else None,
               tokens.specials, tokens.editions, tokens.collections,
               frozenset(tokens.version_markers), brackets, parts)
        facts[path] = (key, group)

    group_sizes: dict[str, int] = {}
    for _key, group in facts.values():
        group_sizes[group] = group_sizes.get(group, 0) + 1
    by_key: dict[tuple, list[Path]] = {}
    for path, (key, group) in facts.items():
        if group_sizes[group] >= 2:
            by_key.setdefault(key, []).append(path)

    clusters = []
    for copies in by_key.values():
        if len({facts[path][1] for path in copies}) < 2:
            continue
        ranked = sorted(copies, key=lambda p: (
            -group_sizes[facts[p][1]], -p.stat().st_size, p.name.casefold()))
        clusters.append([(path, facts[path][1]) for path in ranked])
    return clusters


def _second_copies(
    clusters: list[list[tuple[Path, str]]], refused: set[Path]
) -> dict[Path, Path]:
    """Held copy -> the copy kept in its place, given what naming refused.

    The kept copy is the best-ranked one naming has not refused: a copy that
    cannot land must not hold a valid translation back. Copies from the kept
    copy's own group are not second translations (two same-numbered archives
    from one group are parts or re-uploads), and refused copies are already
    held for their own reason.
    """
    held: dict[Path, Path] = {}
    for cluster in clusters:
        live = [(path, group) for path, group in cluster if path not in refused]
        if not live:
            continue
        kept, kept_group = live[0]
        for path, group in live[1:]:
            if group != kept_group:
                held[path] = kept
    return held


def _name_batch(
    batch: dict[Path, ParsedComicName], media_originals: dict, series_name: str,
    dest_folder: str, dir_number: str | None, transient: set[Path],
) -> set[Path]:
    """Media naming, then fallback naming, for *batch*; returns the refused paths.

    *batch* is updated in place to the archives' new paths. Fallback naming
    runs only for the archives media naming accepted: a refused archive stays
    in the import folder under its arrival name.
    """
    refused, selection = set(), None
    if _media_naming_enabled():  # an empty registry keeps the legacy path untouched
        refused, selection = _apply_media_naming(
            batch, media_originals, series_name, dest_folder, transient=transient)
    options = {"number_style": selection.style} if selection is not None else {}
    named = {p: v for p, v in batch.items() if p not in refused}
    kept_back = {p: v for p, v in batch.items() if p in refused}
    refused = set(refused)
    refused.update(_apply_fallback_naming(named, series_name, dir_number, **options) or ())
    batch.clear()
    batch.update(kept_back)
    batch.update(named)
    return refused


def process_and_move_directory(dir_path: Path) -> bool:
    """Claim the directory and run its recursive import pass.

    Return False on processing contention so readiness can retry; True means
    this invocation owned the pass, not that every archive landed successfully.
    The pass cleans directory names, processes files and routes them as before.
    """
    with _processing_dirs_lock:
        # Claim atomically: a readiness hold can begin after dispatch's check.
        if any(dir_path == p or _is_subpath(dir_path, p) or _is_subpath(p, dir_path)
               for p in _processing_dirs):
            log.info("  Directory processing already owned: %s", dir_path)
            return False
        _processing_dirs.add(dir_path)
    try:
        _process_and_move_directory_inner(dir_path)
    finally:
        with _processing_dirs_lock:
            _processing_dirs.discard(dir_path)
    return True


# ─────────────────────────────────────────────
# CLAIM-FIRST PROCESSING (comic_automation.library.claims)
# ─────────────────────────────────────────────
# Claim folders being processed, mapped to the source folder each arrival
# came from (the immediate child of WATCH_FOLDER) and its arrival path. A
# claimed payload is no longer under WATCH_FOLDER, so _source_folder_name
# and _arrival_path read what was recorded instead of re-deriving it from
# the processing path.
_claim_sources: dict[str, tuple[str, Path]] = {}
_claim_sources_lock = threading.Lock()


def _claim_context(comic_dir: Path) -> dict:
    """What a claim records about where its arrival came from, before it moves."""
    route_category, legacy_dest = _resolve_route(comic_dir)
    watch = Path(WATCH_FOLDER)
    try:
        relative = str(comic_dir.relative_to(watch))
    except ValueError:
        relative = comic_dir.name
    return {"watch_root": str(watch), "source": _source_folder_name(comic_dir),
            "relative": relative, "route": [route_category, legacy_dest]}


def _claimed_items(cbz_dirs: dict[Path, list[Path]], progress):
    """Work items for ``_process_work``: each comic directory, claimed first.

    A directory is claimed whole; a mixed drop point's loose archives are
    claimed one by one (its subfolders are other series, claimed as their
    own entries). A claim that cannot be made leaves the arrival untouched
    and is retried on the next pass. With ``PROCESSING_ROOT`` None the
    directory is processed in place.
    """
    for comic_dir, cbz_files in sorted(cbz_dirs.items()):
        mixed = any(other != comic_dir and _is_subpath(other, comic_dir) for other in cbz_dirs)
        if PROCESSING_ROOT is None:
            yield comic_dir, cbz_files, None, False, mixed, _resolve_route(comic_dir)
            continue
        context = _claim_context(comic_dir)
        if mixed:
            claim = _claims.claim_files(comic_dir, cbz_files, PROCESSING_ROOT, context)
        elif any(p.is_dir() for p in comic_dir.iterdir()):
            # Its subfolders (a sync tool's history, say) are not the
            # arrival's to take: claim its files and leave the folder.
            files = sorted(p for p in comic_dir.iterdir() if p.is_file())
            claim = _claims.claim_files(comic_dir, files, PROCESSING_ROOT, context, kind="files")
            if claim is not None:
                _remove_merged_source(comic_dir)  # empty dirs go, excluded history stays
        else:
            claim = _claims.claim_directory(comic_dir, PROCESSING_ROOT, context)
        if claim is None:
            log.warning(f"  Could not claim '{comic_dir.name}' (record or rename failed); "
                        f"left untouched, retried on the next pass.")
            progress.step(len(cbz_files))
            continue
        log.info(f"  Claimed '{comic_dir.name}' -> {claim.dir}")
        try:
            yield from _claim_item(claim, resumed=False)
        finally:
            claim.release()  # only if closed before processing took ownership over


def _claim_item(claim: "_claims.Claim", *, resumed: bool):
    """Yield one claim's payload for processing while owning the claim.

    Before anything is processed the claim is marked started (a later run
    then treats it as resumed: every archive protected), the payload is
    re-inventoried and readiness-probed in processing (a late writer shows
    here), and dispositions an earlier run recorded are carried out first.
    Any of those failing leaves the claim as it is for a later attempt.
    The claim is retired once nothing of the arrival is left in it.

    A new claim arrives owned by its creator (``claims.claim_directory``),
    which hands that ownership on here, so it is never unowned between its
    publication and its retirement. A resumed claim arrives owned by
    ``_resumed_items``, which read its record under that ownership. Every
    claim comes owned: one that is not was read without its lock, and its
    record cannot be trusted to be current.
    """
    lock, claim.owner = claim.owner, None
    if lock is None:
        raise ValueError(f"claim {claim.id} reached processing without its ownership")
    try:
        payload = claim.payload
        if payload is None:
            return
        resumed = resumed or bool(claim.record.get("started"))
        claim.record["started"] = True
        try:
            claim.save()
        except OSError as e:
            log.error(f"  Claim {claim.id}: record not writable ({e}); nothing processed.")
            return
        cbz_files = sorted(p for p in payload.iterdir()
                           if p.is_file() and p.suffix.casefold() == ".cbz")
        if not cbz_files:
            return
        readiness = probe_cbz_directory_readiness(cbz_files)
        if readiness.status != ZipReadiness.READY:
            log.info(f"  Claim {claim.id}: {readiness.ready_count}/{readiness.archive_count} "
                     f"archive(s) ready; waiting in processing.")
            return
        if not _dispose_recorded(claim):
            return
        if resumed:
            for folder in claim.record.get("dest_folders", []):
                _clear_stale_landings(Path(folder))
        cbz_files = [p for p in cbz_files if p.exists()]
        key = os.path.normcase(str(claim.dir))
        with _claim_sources_lock:
            _claim_sources[key] = (
                claim.record.get("source") or payload.name,
                Path(claim.record.get("watch_root") or WATCH_FOLDER)
                / (claim.record.get("relative") or payload.name))
        try:
            yield (claim.payload, cbz_files, claim, resumed,
                   claim.record.get("kind") == "loose", tuple(claim.record.get("route") or ("", "")))
        finally:
            with _claim_sources_lock:
                _claim_sources.pop(key, None)
    finally:
        _retire_claim(claim, lock)


def _retire_claim(claim: "_claims.Claim", owner: "_claims.OwnerLock | None" = None) -> None:
    """Remove a claim once none of its arrival is left; otherwise report it.

    Retiring needs the claim's ownership: *owner* (released here) or the
    lock taken by ``claims.retire``, which re-checks emptiness under it. A
    claim that only looks empty may be one its creator is still filling.
    """
    if _claims.has_arrival_files(claim.dir):
        if owner is not None:
            owner.release()
        log.warning(f"  Claim {claim.id} unfinished: archive(s) remain in {claim.dir}; "
                    f"resumed on the next pass.")
        return
    if _claims.retire(claim.dir, owner):
        log.info(f"  Claim {claim.id} complete.")


def _resume_claims() -> None:
    """Finish this watcher's unfinished claims, before any new arrival.

    A claim folder whose record is missing or unreadable but which holds
    files is preserved whole under review (``_unrecovered_claims``): what it
    is cannot be trusted, and guessing could lose it. A claim folder with no
    arrival left (its rename never happened, or its work is done) is
    removed. A claim recorded for another watch root is left to its watcher.
    """
    if PROCESSING_ROOT is None:
        return
    items = _resumed_items()
    with contextlib.closing(items):
        _process_work(items, progress=ProgressReporter(0, "files"),
                      shadow_results=[], shadow_handoffs=[])


def _resumed_items():
    """Each resumable claim, owned before anything about it is read.

    Its record is read and judged (missing, corrupt, another watch root,
    nothing left) only under the claim's ownership: read before it, a
    resumer waiting for the lock could act on, and later save, a record that
    the owner meanwhile replaced, such as completed AI custody (PR #177
    review). The ownership is handed on to ``_claim_item``, which keeps it
    through processing and retirement.
    """
    watch_key = os.path.normcase(str(Path(WATCH_FOLDER)))
    for claim_dir in _claims.claim_dirs(PROCESSING_ROOT):
        lock = _claims.OwnerLock(claim_dir)
        if not lock.acquire():
            continue  # its creator, another resumer or another watcher owns it
        record = _claims.read_record(claim_dir)
        if record is None:
            if not _claims.has_arrival_files(claim_dir):
                _claims.retire(claim_dir, lock)  # re-checked while owned
                continue
            lock.release()  # the folder cannot be renamed while its lock file is open
            try:
                target = _claims.preserve_unrecoverable(claim_dir, REVIEW_ROOT)
                log.critical(f"Claim {claim_dir.name} has no readable record; preserved "
                             f"whole for review at {target}.")
            except OSError as e:
                log.critical(f"Claim {claim_dir.name} has no readable record and could not be "
                             f"moved to review ({e}); left in processing.")
            continue
        if os.path.normcase(str(record.get("watch_root", ""))) != watch_key:
            lock.release()
            continue
        if not _claims.has_arrival_files(claim_dir):
            _retire_claim(_claims.Claim(claim_dir, record), lock)
            continue
        claim = _claims.Claim(claim_dir, record, lock)
        log.info(f"Resuming claim {claim.id} ({record.get('relative')}).")
        try:
            yield from _claim_item(claim, resumed=True)
        finally:
            claim.release()  # only if closed before processing took ownership over


def _ai_custody(claim: "_claims.Claim | None", name: str) -> dict | None:
    """What the claim recorded about *name*'s AI rewrite, if anything."""
    if claim is None:
        return None
    return (claim.record.get("ai") or {}).get(name)


def _note_ai(claim: "_claims.Claim | None", name: str | None, entry: dict | None) -> bool:
    """Record AI custody for *name* (None clears all). False if it could not be saved.

    Written before the rewrite starts and again with the backup once it is
    done, so a resumed claim never repeats the rewrite and always knows
    where the original went. In place there is nothing to record.
    """
    if claim is None:
        return True
    custody = claim.record.setdefault("ai", {})
    if name is None:
        custody.clear()
        claim.record.pop("ai_naming", None)  # nothing left to follow
    else:
        custody[name] = entry
    try:
        claim.save()
    except OSError as e:
        log.error(f"    Claim {claim.id}: AI custody not recorded ({e}).")
        return False
    return True


def _mark_ai_naming(claim: "_claims.Claim | None") -> bool:
    """Mark a claim holding AI custody before its archive names change.

    Custody is keyed by archive name, and a pass renames archives
    (``process_cbz_file``, then naming) before ``_rekey_ai_custody`` moves
    the keys and clears the mark. A mark found by a later run means a name
    may have changed unrecorded: archives under no custody entry then have
    an unknown AI state (review), never a silent second rewrite. False when
    the mark cannot be saved; nothing may be renamed then.
    """
    if claim is None or not claim.record.get("ai") or claim.record.get("ai_naming"):
        return True
    claim.record["ai_naming"] = True
    try:
        claim.save()
    except OSError as e:
        claim.record.pop("ai_naming", None)
        log.error(f"    Claim {claim.id}: AI custody could not be marked ({e}); "
                  f"nothing is renamed this pass.")
        return False
    return True


def _rekey_ai_custody(claim: "_claims.Claim | None", origins: dict[Path, Path],
                      parsed_by_path: dict) -> None:
    """Move AI custody to each archive's name after naming, and clear the mark.

    *origins* maps each pre-naming path to its ``original_path``, which
    naming carries over (as for ``_rekey_ai_backups``). If the record cannot
    be saved the mark stays, so a later run reviews what it cannot match.
    """
    if claim is None or not claim.record.get("ai_naming"):
        return
    current = {getattr(parsed, "original_path", path): path
               for path, parsed in parsed_by_path.items()}
    renamed = {before.name: current[origin].name for before, origin in origins.items()
               if origin in current and current[origin].name != before.name}
    claim.record["ai"] = {renamed.get(name, name): entry
                          for name, entry in (claim.record.get("ai") or {}).items()}
    claim.record.pop("ai_naming", None)
    try:
        claim.save()
    except OSError as e:
        claim.record["ai_naming"] = True
        log.error(f"    Claim {claim.id}: AI custody not re-keyed after naming ({e}); "
                  f"a resume reviews what it cannot match.")


def _dispose(claim: "_claims.Claim | None", planned: dict, series: str,
             disposed: set) -> bool:
    """Move each planned archive to review or quarantine; False if any could not.

    With a claim, the decisions are written to its record first, so a crash
    between deciding and moving is finished by the next run
    (``_dispose_recorded``) before anything else of the claim is touched.
    A failed move leaves that archive where it is, still held.
    """
    if claim is not None:
        record = claim.record.setdefault("dispositions", {})
        for path, (to, reason) in planned.items():
            record[path.name] = {"to": to, "reason": reason, "series": series}
        try:
            claim.save()
        except OSError as e:
            log.error(f"    Claim {claim.id}: decisions not recorded ({e}); nothing moved.")
            return False
    ok = True
    for path, (to, reason) in planned.items():
        root = QUARANTINE_ROOT if to == "quarantine" else REVIEW_ROOT
        try:
            target = _claims.place(path, root, series)
        except OSError as e:
            log.error(f"    Could not move '{path.name}' to {to} ({e}); it stays held.")
            ok = False
            continue
        disposed.add(path)
        log.warning(f"    {to.upper()}: '{path.name}' ({reason}) -> {target}")
        if claim is not None:
            claim.record["dispositions"].pop(path.name, None)
    if claim is not None:
        try:
            claim.save()
        except OSError:
            pass  # the next run finds the moved archives gone and drops them
    return ok


def _dispose_recorded(claim: "_claims.Claim") -> bool:
    """Carry out dispositions an earlier run recorded but did not finish."""
    planned = claim.record.get("dispositions") or {}
    payload = claim.payload
    pending = {}
    for name, plan in list(planned.items()):
        path = payload / name if payload is not None else None
        if path is None or not path.exists():
            planned.pop(name, None)
            continue
        pending[path] = (plan.get("to", "review"), plan.get("reason", "recorded decision"))
        series = plan.get("series") or payload.name
    if not pending:
        return True
    return _dispose(claim, pending, series, set())


def _process_and_move_directory_inner(dir_path: Path) -> None:
    """Claim each comic directory under *dir_path* and process it outside the watch folder.

    Unfinished claims are resumed first: their arrivals were accepted before
    this one. Each comic directory is then claimed (``_claimed_items``):
    moved out of the MEGA-synced watch folder by one same-volume rename
    before anything changes it. A mixed drop point's loose archives are
    claimed one by one; its subfolders are other series, claimed on their
    own. Nothing in the watch folder is renamed or rewritten.

    With ``PROCESSING_ROOT`` None (direct callers only) each directory is
    processed in place, with the same dispositions.
    """
    _resume_claims()

    log.info("=" * 60)
    log.info(f"Scanning: {dir_path.name}")

    if not dir_path.exists() or not dir_path.is_dir():
        log.warning(f"  Directory no longer exists: {dir_path}")
        return

    cbz_dirs: dict[Path, list[Path]] = {}
    for cbz in sorted(
        path
        for path in iter_files_excluding_directories(dir_path)
        if path.suffix.casefold() == ".cbz"
    ):
        cbz_dirs.setdefault(cbz.parent, []).append(cbz)

    if not cbz_dirs:
        log.info(f"  No .cbz files found under '{dir_path.name}', skipping.")
        return

    log.info(f"  Found .cbz files in {len(cbz_dirs)} directory(s).")
    progress = ProgressReporter(sum(len(files) for files in cbz_dirs.values()), "files")

    total_processed = total_skipped = total_renamed = 0
    # Bounded to this pass. The router keeps no state of its own, so a
    # long-running watcher never accumulates a record per directory.
    shadow_results: list = []
    shadow_handoffs: list = []
    items = _claimed_items(cbz_dirs, progress)
    with contextlib.closing(items):
        total_processed, total_renamed, total_skipped = _process_work(
            items, progress=progress, shadow_results=shadow_results,
            shadow_handoffs=shadow_handoffs)


    log.info(
        f"  Batch complete — {total_processed} processed, "
        f"{total_renamed} renamed, {total_skipped} skipped."
    )
    # Reported whenever shadow is running, not only when something was
    # classified. If every classification fails, the errors are logged but the
    # pass summary would otherwise vanish -- and a missing summary reads as
    # "shadow was off", which is exactly the wrong conclusion.
    if _router is not None and _router.enabled:
        from scripts.cbz_watcher_router import summarise
        log.info(summarise(shadow_results))
        from scripts.cbz_shadow_handoff import summarise as summarise_handoffs
        log.info(summarise_handoffs(shadow_handoffs))


def _process_work(items, *, progress, shadow_results, shadow_handoffs) -> tuple[int, int, int]:
    """Process comic directories: name, decide, dispose of holds, land the rest.

    Each item is ``(comic_dir, cbz_files, claim, resumed, mixed_parent,
    route)``: a claim's payload (``claim`` set) or a directory in place.
    Archives that need a decision go to review and unneeded second
    translations to quarantine (``_dispose``) **before** anything lands, so a
    held copy can never enter a merge. With a claim, each decision is written
    to its record first, so a crash between deciding and moving cannot lose
    it. *resumed* (a claim an earlier run started) protects every archive: a
    decision that run made but never recorded must not be settled by
    keep-the-larger. Returns (processed, renamed, skipped).
    """
    total_processed = total_skipped = total_renamed = 0
    for comic_dir, cbz_files, claim, resumed, mixed_parent, route in items:
        route_category, legacy_dest = route
        # A mixed drop point's loose archives are not one series, so their
        # folder cannot confirm a known group's prefix (cbz_core).
        known_groups_mixed_drop(mixed_parent)
        incoming_series_alias = comic_dir.name
        clean_dir_name = clean_directory_name(comic_dir.name)
        if _media_naming_enabled():
            base, reboot_year = split_series_year(comic_dir.name)
            if reboot_year is not None:
                clean_dir_name = f"{clean_directory_name(base) or base} ({reboot_year})"
        preliminary_title = clean_dir_name or comic_dir.name
        _, dest_folder, _, active_resolver = (
            _resolve_import_target(route_category, preliminary_title, legacy_dest)
        )
        conflict_quarantine_root = (
            active_resolver.quarantine_root if active_resolver is not None else None
        )

        # A directory that has .cbz files sitting directly inside it *and* also
        has_nested_comic_dirs = mixed_parent
        if (claim is not None and not mixed_parent and clean_dir_name
                and clean_dir_name != comic_dir.name):
            # Inside the claim nothing else shares the folder, so its name is
            # cleaned by a plain rename. The watch folder is never renamed.
            cleaned = comic_dir.parent / clean_dir_name
            try:
                comic_dir.rename(cleaned)
                log.info(f"  Directory renamed: '{comic_dir.name}' -> '{clean_dir_name}'")
                cbz_files = [cleaned / f.name for f in cbz_files]
                comic_dir = cleaned
            except OSError as e:
                log.warning(f"  Could not clean the name '{comic_dir.name}': {e}")

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
        media_originals = {}
        ai_decensor_backups: list[tuple[Path, Path]] = []
        ai_decensor_this_directory = (
            AI_DECENSOR_ENABLED and destination_is_comix(dest_folder)
        )
        if AI_DECENSOR_ENABLED and not ai_decensor_this_directory:
            log.info(
                f"    AI decensor skipped: destination is not under a Comix "
                f"directory ({dest_folder})"
            )
        # Archives whose AI rewrite an earlier run started but never recorded
        # as finished: their state is unknown, so they go to review.
        ai_unknown: set[Path] = set()
        abandon = False
        # A mark left by an earlier run (_mark_ai_naming): names may have
        # changed since custody was recorded, so what matches no entry is
        # reviewed. Marked again now if this pass may rename.
        custody_unresolved = claim is not None and bool(claim.record.get("ai_naming"))
        if not _mark_ai_naming(claim):
            continue
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
                if _media_naming_enabled():
                    try:
                        media_originals[cbz] = _media_fr.archive_facts_from_file(cbz)
                    except (OSError, ValueError, zipfile.BadZipFile) as exc:
                        log.warning("    REVIEW: cannot bind original media facts for %s: %s", cbz, exc)
                        abandon = True
                        break
                result_path, parsed = process_cbz_file(cbz, override_name=override)
                total_processed += 1
                if result_path.name != original_name:
                    total_renamed += 1
                if parsed is not None:
                    if ai_decensor_this_directory:
                        recorded = _ai_custody(claim, result_path.name)
                        if recorded is None and result_path.name != original_name:
                            recorded = _ai_custody(claim, original_name)
                            if recorded is not None:  # renamed just now: custody follows
                                claim.record["ai"][result_path.name] = (
                                    claim.record["ai"].pop(original_name))
                        if recorded is not None and recorded.get("done"):
                            # An earlier run of this claim decensored it: never
                            # repeat destructive processing on a resume.
                            if recorded.get("backup"):
                                ai_decensor_backups.append(
                                    (result_path, Path(recorded["backup"])))
                        elif recorded is not None:
                            ai_unknown.add(result_path)
                        elif custody_unresolved:
                            # Possibly a decensored archive whose new name was
                            # never recorded: review it, never rewrite twice.
                            ai_unknown.add(result_path)
                        elif not _note_ai(claim, result_path.name, {"started": True}):
                            # Custody before the rewrite: if it cannot be
                            # recorded, nothing is rewritten.
                            abandon = True
                            break
                        else:
                            backup_path = run_ai_decensor(result_path, dest_folder)
                            _note_ai(claim, result_path.name, {
                                "started": True, "done": True,
                                "backup": str(backup_path) if backup_path else None})
                            if backup_path is not None:
                                ai_decensor_backups.append((result_path, backup_path))
                    parsed_by_path[result_path] = parsed
            except AiDecensorError as exc:
                log.error(f"    AI decensor failed for {cbz.name}: {exc}")
                log.error("    Directory import stopped; completed AI replacements are being restored.")
                restore_ai_decensor_backups(ai_decensor_backups)
                _note_ai(claim, None, None)  # the originals are back in place
                abandon = True
                break
            finally:
                progress.step()

        if abandon:
            continue

        # Route per-chapter folders ("Berserk Ch. 4") into a single series folder
        # ("Berserk") instead of creating one destination directory per chapter.
        # Use the post-rename paths (parsed_by_path keys) rather than the stale
        # pre-rename cbz_files list, so the ComicInfo <Series> evidence check
        # below can actually find the (now-renamed) files on disk.
        # ── classify -> move -> index, serialized by resolved series identity
        #
        # The identity is read from the destination filesystem, which is what
        # another thread's move changes, so it is resolved provisionally,
        # locked, and re-resolved under that lock before anything is moved.
        # Everything above this point is per-arrival work on the source
        # directory and stays outside the lock: holding it across file
        # processing would serialize unrelated series on unrelated work.
        resolution: dict = {}

        def _resolve_identity() -> str:
            """Read-only: inspects the destination and archives, moves nothing.

            Called once before the lock and once under each acquisition, so it
            must stay free of irreversible work -- releasing a provisional lock
            is only safe because nothing was mutated under it.
            """
            name, number = _resolve_series_dir_name(
                comic_dir, list(parsed_by_path.keys()), dest_folder
            )
            if _series_resolvers:
                metadata_name = _consistent_comicinfo_series(
                    list(parsed_by_path.keys())
                )
                if (metadata_name and _series_key(metadata_name) != _series_key(name)
                        and not (_media_naming_enabled() and split_series_year(name)[1] is not None)):
                    # process_cbz_file translates native and confidently romanized
                    # titles before this read. Prefer that English ComicInfo value
                    # only in resolver mode, leaving legacy watcher naming intact.
                    name = metadata_name
            target_name, target_dest, resolver_result, resolver = (
                _resolve_import_target(route_category, name, legacy_dest)
            )
            resolution.update(
                dir_number=number,
                incoming_name=name,
                target_dest=target_dest,
                resolver_result=resolver_result,
                resolver=resolver,
            )
            return target_name

        try:
            with stable_series_lock(_SERIES_LOCKS, _resolve_identity) as settled:
                # The identity the transaction confirmed under the lock it
                # holds, not a fresh read: resolving again here would be a
                # third answer nothing had agreed to.
                series_name = settled.identity
                dir_number = resolution["dir_number"]
                dest_folder = resolution["target_dest"]
                resolver_result = resolution["resolver_result"]
                active_resolver = resolution["resolver"]
                conflict_quarantine_root = (
                    active_resolver.quarantine_root
                    if active_resolver is not None
                    else None
                )
                match_type = resolver_result.get("match_type")
                if str(match_type).startswith("ambiguous_"):
                    log.warning(
                        f"  Series resolver left '{resolution['incoming_name']}' in "
                        f"review staging: {match_type}; candidates="
                        f"{resolver_result.get('candidates', [])}"
                    )
                elif active_resolver is not None:
                    selected_category = resolver_result.get("selected_category")
                    if selected_category != route_category:
                        log.info(
                            f"  Series resolver overrode source category "
                            f"'{route_category}' -> '{selected_category}'"
                        )
                    log.info(
                        f"  Series resolver: '{resolution['incoming_name']}' -> "
                        f"'{series_name}' ({match_type}) in {dest_folder}"
                    )
                if series_name != comic_dir.name:
                    log.info(
                        f"  Series folder: '{comic_dir.name}' -> '{series_name}'")

                # Give placeholder-named archives (see _PLACEHOLDER_STEM_RE) a
                # unique, traceable name before the merge — otherwise every
                # chapter that reuses a scanlator's generic filename collides
                # under the same name and one is silently discarded during
                # _move_cbz_dir's merge.
                origins = {
                    path: getattr(parsed, "original_path", path)
                    for path, parsed in parsed_by_path.items()
                }
                # Second copies of a chapter are set aside before naming, so
                # media naming cannot give one copy the other's name. A kept
                # copy that naming then refuses cannot stand in for the rest:
                # _second_copies picks the next copy, which is released and
                # named in turn, until every cluster has a kept copy or none.
                clusters = _duplicate_chapter_copies(parsed_by_path, series_name)
                transient = set()
                duplicates = _second_copies(clusters, set())
                set_aside = {path: parsed_by_path.pop(path) for path in duplicates}
                if not _mark_ai_naming(claim):
                    continue  # names must not change while custody is unmarked
                refused = _name_batch(parsed_by_path, media_originals, series_name,
                                      dest_folder, dir_number, transient)
                while True:
                    duplicates = _second_copies(clusters, refused)
                    released = {path: set_aside.pop(path)
                                for path in list(set_aside) if path not in duplicates}
                    if not released:
                        break
                    refused |= _name_batch(released, media_originals, series_name,
                                           dest_folder, dir_number, transient)
                    parsed_by_path.update(released)
                parsed_by_path.update(set_aside)
                _rekey_ai_custody(claim, origins, parsed_by_path)
                second_copies = {path: kept.name for path, kept in duplicates.items()}
                # Decisions, made before anything lands: a refusal that can
                # clear by itself (a locked file) stays to be retried; every
                # other refusal, and an archive of unknown AI state, needs the
                # operator (review); an unneeded second translation goes to
                # quarantine.
                unknown_origins = {origins.get(p, p) for p in ai_unknown}
                retry = refused & transient
                unknown_ai = {
                    p for p, parsed in parsed_by_path.items()
                    if getattr(parsed, "original_path", p) in unknown_origins}
                # Two archives of this arrival named alike, such as two
                # groups' translations of one chapter once their group
                # prefixes are dropped: the second kept its arrival name only
                # because the first took the name. Landed, it would be a raw
                # duplicate beside the first; which to keep is the operator's
                # call, as for any collision with a different book. A name
                # kept because it carries a distinguishing tag (``(s2)``,
                # dropped_round_tags) is not one: that tag tells them apart.
                taken = {path.name.casefold() for path in parsed_by_path}
                same_name = set()
                for path, parsed in parsed_by_path.items():
                    display = getattr(parsed, "filename", None)
                    if (display is None or path in second_copies
                            or dropped_round_tags(path.stem)):
                        continue
                    display = normalize_apostrophes(display).casefold()
                    if display != path.name.casefold() and display in taken:
                        same_name.add(path)
                review = (refused - transient) | unknown_ai | same_name
                held = retry | review | set(second_copies)
                # Every other copy of a duplicated chapter is protected where it
                # lands, and on a resumed claim every archive is: a collision
                # with a different library book is then held for review,
                # never settled by keep-the-larger.
                cluster_origins = {
                    origins.get(path, path) for cluster in clusters for path, _ in cluster}
                protected = frozenset(
                    path for path, parsed in parsed_by_path.items()
                    if path not in held and (
                        resumed or getattr(parsed, "original_path", path) in cluster_origins))
                # The movers report each archive they hold on a collision with
                # a different book; those are decisions, sent to review below.
                collision_holds: set[Path] = set()
                protected_option = {"protected": protected, "holds": collision_holds}
                ai_backups = _rekey_ai_backups(
                    ai_decensor_backups, origins, parsed_by_path)
                # Read after the fallback renames, which change paths. Passed
                # to the moves below only when non-empty, like the quarantine
                # root, so their signatures are unchanged for every other
                # arrival (issue #131).
                stripped = _stripped_arrivals(parsed_by_path)
                stripped_option = (
                    {"stripped_arrivals": stripped} if stripped else {}
                )
                planned = {path: ("review", "name refused, see REVIEW above")
                           for path in review - unknown_ai - same_name}
                planned.update({path: ("review", "another archive of this arrival has its name")
                                for path in same_name - unknown_ai})
                planned.update({path: ("review", "AI rewrite of unknown state")
                                for path in unknown_ai})
                planned.update({path: ("quarantine", f"second copy of '{kept}'")
                                for path, kept in second_copies.items()})
                if claim is not None and str(dest_folder) not in claim.record.get("dest_folders", []):
                    # Lets a resumed claim find and clear its own crashed
                    # landings there (_clear_stale_landings).
                    claim.record.setdefault("dest_folders", []).append(str(dest_folder))
                    try:
                        claim.save()
                    except OSError:
                        pass  # only that automatic clean-up depends on it
                disposed: set[Path] = set()
                blocked = bool(planned) and not _dispose(claim, planned, series_name, disposed)
                if retry:
                    log.warning(f"  {len(retry)} archive(s) of '{comic_dir.name}' refused for "
                                f"a reason that may clear by itself; retried later.")

                if blocked:
                    # A decision could not be recorded or carried out. Nothing
                    # lands this pass: every archive stays where it is.
                    log.error(f"  Nothing from '{comic_dir.name}' lands this pass: a hold "
                              f"could not be moved aside; retried later.")
                    landed = None
                elif has_nested_comic_dirs:
                    log.warning(
                        f"  '{comic_dir.name}' has loose .cbz file(s) alongside subdirectories that "
                        f"have their own .cbz files — moving only the loose files, not the whole "
                        f"directory, so unrelated series aren't swept along with it."
                    )
                    # Held archives stay behind in the import folder; the
                    # other loose files move as before. With none left there
                    # is nothing to move, and _move_loose_files would still create
                    # an empty series folder, so it is not called.
                    archives = [a for a in parsed_by_path if a not in held]
                    _shadow_route(comic_dir, legacy_dest, series_name, archives,
                                  shadow_results, shadow_handoffs,
                                  mixed_parent=True)
                    loose_options = (
                        {"conflict_quarantine_root": conflict_quarantine_root}
                        if conflict_quarantine_root is not None
                        else {}
                    )
                    landed = _move_loose_files(
                        archives, dest_folder, series_name,
                        **loose_options, **stripped_option, **protected_option,
                    ) if archives else None
                    _note_move(series_name, dest_folder, landed)
                elif held and all(path in held for path in parsed_by_path):
                    # Nothing to land: creating the series folder for no
                    # archive would only leave an empty directory behind.
                    log.warning(
                        f"  Nothing from '{comic_dir.name}' lands: all "
                        f"{len(held)} archive(s) held.")
                    landed = None
                else:
                    # A refused placeholder kept its generic name. Merging it
                    # into the series folder is where the next chapter sharing
                    # that name collides -- the loss the fallback exists to
                    # prevent -- so held archives never leave the import
                    # folder (_move_cbz_dir's *held*). Their siblings are
                    # unaffected by that risk and move now, instead of the
                    # whole series waiting on one archive.
                    _shadow_route(comic_dir, legacy_dest, series_name,
                                  [p for p in parsed_by_path if p not in held],
                                  shadow_results, shadow_handoffs)
                    move_options = (
                        {"conflict_quarantine_root": conflict_quarantine_root}
                        if conflict_quarantine_root is not None
                        else {}
                    )
                    held_option = {"held": frozenset(held)} if held else {}
                    landed = _move_cbz_dir(
                        comic_dir,
                        dest_folder,
                        target_name=series_name,
                        chapter_number=dir_number,
                        **move_options,
                        **stripped_option,
                        **held_option,
                        **protected_option,
                    )
                    _note_move(series_name, dest_folder, landed)
                if (
                    landed is not None
                    and active_resolver is not None
                    and Path(dest_folder) == active_resolver.root
                ):
                    try:
                        observed_aliases = _observed_series_aliases(
                            incoming_series_alias,
                            str(resolution["incoming_name"]),
                            series_name,
                            parsed_by_path.values(),
                        )
                        active_resolver.register(
                            landed.name,
                            observed_aliases,
                        )
                    except Exception as e:  # noqa: BLE001 — import already landed
                        log.error(
                            f"  Series registry update failed for '{landed.name}': "
                            f"{type(e).__name__}: {e}"
                        )
                if collision_holds and not blocked:
                    # Held on a collision with a different book (protected, or a
                    # stripped tag, issue #131), whether or not anything else
                    # landed. The library copy is untouched; which to keep is
                    # the operator's call. Anything else still here (a deferral
                    # or a failed move) stays in processing and is retried.
                    collided = {
                        path: ("review", "collides with a different library book")
                        for path in sorted(collision_holds) if path.exists()}
                    if collided:
                        _dispose(claim, collided, series_name, disposed)
            # A disposed archive's AI original stays on C: (it did not land).
            for archive, backup in ai_backups:
                if archive in disposed:
                    log.warning(f"    AI original kept on C: for '{archive.name}': {backup}")
            ai_backups = [(a, b) for a, b in ai_backups if a not in disposed]
            archive_completed_ai_backups(_routed_ai_backups(ai_backups, landed), landed)
        except UnstableSeriesIdentityError as e:
            # The destination kept moving under us. Deferring is correct:
            # nothing was mutated, the arrival is still in the watch folder,
            # and the next settle pass retries against a quieter library.
            log.error(f"  Deferred '{comic_dir.name}': {e}")
            continue
    return total_processed, total_renamed, total_skipped

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
# READINESS-GATED DIRECTORY PROCESSING
# ─────────────────────────────────────────────
# Seconds before re-probing a directory that wasn't ready last time. Kept
# on the same order of magnitude as SETTLE_DELAY so a not-yet-ready
# directory gets rechecked at a similar cadence to the settle timer itself,
# rather than hammering the filesystem or waiting unreasonably long.
READINESS_RETRY_DELAY_SECONDS = 5
WATCHER_SOURCE_VERSION = "sha256:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _log_directory_not_ready(
    dir_path: Path,
    result: DirectoryReadinessResult,
) -> None:
    log.info(
        f"  Directory not ready: '{dir_path.name}' "
        f"({result.ready_count}/{result.archive_count} archive(s) ready) "
        f"— retrying in {READINESS_RETRY_DELAY_SECONDS}s."
    )
    for entry in result.entries:
        if entry.status == ZipReadiness.READY:
            continue
        detail_suffix = f" ({entry.detail})" if entry.detail else ""
        log.info(
            f"    Not ready: '{entry.path.name}' - "
            f"{entry.reason}{detail_suffix}"
        )


def _process_directory_when_ready(
    dir_path: Path,
    tracker: "DirectorySettleTracker",
    *,
    readiness_generation: int | None = None,
) -> None:
    """Admit one whole directory before probing or changing its archives."""
    with tracker._directory_admission() as admitted:
        if admitted:
            _process_admitted_directory(
                dir_path, tracker, readiness_generation=readiness_generation,
            )


def _process_admitted_directory(
    dir_path: Path,
    tracker: "DirectorySettleTracker",
    *,
    readiness_generation: int | None = None,
) -> None:
    """
    Readiness gate in front of process_and_move_directory().

    Enumerates dir_path's .cbz files and probes them with
    probe_cbz_directory_readiness() -- a pure, read-only check (no
    database access, no locks, no file mutation; see the ZIP READINESS
    PROBE sections above). Only calls process_and_move_directory() (which
    performs the actual renames/metadata edits/moves) once every archive
    reports READY.

    If any archive is not ready, optional readiness holding may remove an
    expired, structurally incomplete archive under processing exclusion. A
    successful hold restarts this gate with a fresh inventory. Otherwise it
    schedules exactly one retry via `tracker` after
    READINESS_RETRY_DELAY_SECONDS. If that retry also finds something not
    ready, it schedules its own single retry in turn -- this is a normal
    timer-based recheck cadence, not a busy loop.

    A directory that no longer exists, or that contains no .cbz files at
    all, stops immediately with no retry scheduled -- there is nothing
    that could become "ready" later for either case, so retrying would
    loop forever.
    """
    if not dir_path.exists() or not dir_path.is_dir():
        log.info(
            f"  Directory no longer exists, not probing: {dir_path}"
        )
        tracker.readiness_holds.forget(dir_path)
        return

    cbz_files = sorted(
        path
        for path in iter_files_excluding_directories(dir_path)
        if path.suffix.casefold() == ".cbz"
    )

    if not cbz_files:
        log.info(
            f"  No .cbz files found under '{dir_path.name}', "
            "not probing."
        )
        tracker.readiness_holds.forget(dir_path)
        return

    if readiness_generation is None:
        readiness_generation = tracker._current_readiness_generation(
            dir_path
        )

    result = probe_cbz_directory_readiness(cbz_files)

    # Timer.cancel() cannot stop a callback that has already begun. A fresh
    # filesystem event may therefore arrive while this probe is in progress.
    # Discard that stale result instead of processing or scheduling another
    # retry from information that predates the new event.
    if not tracker._readiness_generation_is_current(
        dir_path,
        readiness_generation,
    ):
        return

    if result.status == ZipReadiness.READY:
        tracker.readiness_holds.forget(dir_path)
        if process_and_move_directory(dir_path) is False:
            # A hold may claim exclusion after this gate's readiness probe.
            # Preserve this event, but never replace a newer notify() timer.
            tracker._schedule_readiness_retry(
                dir_path, expected_generation=readiness_generation,
            )
        return

    if tracker._hold_stalled_archives(dir_path, result, readiness_generation):
        # Never process ready siblings using a probe that preceded a hold.
        _process_admitted_directory(
            dir_path, tracker, readiness_generation=readiness_generation,
        )
        return

    _log_directory_not_ready(dir_path, result)
    tracker._schedule_readiness_retry(
        dir_path,
        expected_generation=readiness_generation,
    )


# ─────────────────────────────────────────────
# DIRECTORY SETTLE TRACKER
# ─────────────────────────────────────────────
def _validated_stop_file(value: Path | None) -> Path | None:
    """Require a caller-owned absolute stop path in an existing directory."""
    if value is None:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("--stop-file must be an absolute path")
    if not path.parent.is_dir():
        raise ValueError(f"--stop-file parent directory does not exist: {path.parent}")
    return path


class DirectorySettleTracker:
    """Coordinate directory admission, graceful stopping, readiness and holds."""

    def __init__(
        self, settle_delay: float = SETTLE_DELAY, *,
        watch_root: Path | None = None, readiness_hold_root: Path | None = None,
        readiness_stall_minutes: float = 30, clock=None, stop_file: Path | None = None,
    ):
        self._stop_file = _validated_stop_file(stop_file)
        self.readiness_holds = ReadinessHolds(
            watch_root if watch_root is not None else Path(WATCH_FOLDER),
            hold_root=readiness_hold_root, stall_minutes=readiness_stall_minutes,
            watcher_version=WATCHER_SOURCE_VERSION, clock=clock, logger=log,
        )
        self.settle_delay = settle_delay
        self._timers: dict = {}
        # Pending single-shot readiness recheck timers, keyed by directory
        # -- kept separate from _timers (the settle-notify timers) so a
        # fresh filesystem event (notify()) can always cancel a stale
        # readiness retry without disturbing the bookkeeping for the
        # normal settle cycle, and vice versa.
        self._readiness_retry_timers: dict = {}
        # A monotonically increasing generation invalidates readiness callbacks
        # that were already running when a newer filesystem event arrived.
        self._readiness_generations: dict[Path, int] = {}
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._closed = False
        self._active_callbacks = 0
        self._active_directories = 0
        self._stop_latched = False
        self._stop_poll_warned = False

    def _cancel_pending_locked(self) -> None:
        for timer in (*self._timers.values(), *self._readiness_retry_timers.values()):
            timer.cancel()
        self._timers.clear()
        self._readiness_retry_timers.clear()

    def _poll_stop_locked(self) -> bool:
        # Polling and admission share the tracker lock, so after the request
        # is latched no other directory can start. Already-admitted units finish.
        if self._stop_latched:
            return True
        if self._stop_file is None:
            return False
        try:
            try:
                self._stop_file.stat()
            except FileNotFoundError:
                if self._stop_file.parent.is_dir():
                    return False
                raise
        except OSError as exc:
            if not self._stop_poll_warned:
                log.warning("Cannot poll watcher stop file %s; continuing: %s", self._stop_file, exc)
                self._stop_poll_warned = True
            return False
        self._stop_latched = True
        self._closed = True
        self._cancel_pending_locked()
        log.info("Watcher stop request seen: %s; %d admitted directories still finishing.",
                 self._stop_file, self._active_directories)
        return True

    def stop_requested(self) -> bool:
        """Latch a present request; transient poll errors warn once and keep running."""
        with self._idle:
            return self._poll_stop_locked()

    @contextmanager
    def _directory_admission(self):
        with self._idle:
            admitted = not self._closed and not self._poll_stop_locked()
            if admitted:
                self._active_directories += 1
        try:
            yield admitted
        finally:
            if admitted:
                with self._idle:
                    self._active_directories -= 1
                    self._idle.notify_all()

    def close(self) -> None:
        """Cancel pending work and drain callbacks/directory units before root unlock."""
        with self._idle:
            self._closed = True
            self._cancel_pending_locked()
            while self._active_callbacks or self._active_directories:
                try:
                    self._idle.wait()
                except KeyboardInterrupt:
                    log.warning("Waiting for active import callbacks before releasing watcher ownership.")

    def _tracked_callback(self, callback):
        # A callback is counted before it can leave the tracker lock. close()
        # either prevents it entering or waits until all its file work ends.
        def invoke(*args):
            with self._idle:
                if self._closed or self._poll_stop_locked():
                    return
                self._active_callbacks += 1
            try:
                return callback(*args)
            finally:
                with self._idle:
                    self._active_callbacks -= 1
                    self._idle.notify_all()
        return invoke

    def notify(self, dir_path: Path) -> None:
        """Reset the settle timer each time a file event fires.

        A fresh filesystem event always supersedes any readiness retry
        already pending for this directory: cancelling it here means a new
        event's settle timer can never be overwritten or raced by an older
        readiness retry firing later on stale information.
        """
        with self._lock:
            if self._closed or self._poll_stop_locked():
                return
            existing = self._timers.get(dir_path)
            if existing:
                existing.cancel()
            stale_retry = self._readiness_retry_timers.pop(dir_path, None)
            if stale_retry:
                stale_retry.cancel()
            self._readiness_generations[dir_path] = (
                self._readiness_generations.get(dir_path, 0) + 1
            )
            timer = threading.Timer(self.settle_delay, self._tracked_callback(self._on_settled), args=[dir_path])
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
                timer = threading.Timer(wait, self._tracked_callback(self._on_settled), args=[dir_path])
                with self._lock:
                    if self._closed or self._poll_stop_locked():
                        return
                    self._timers[dir_path] = timer
                    timer.start()
                return
        log.info(f"Directory ready: '{dir_path.name}' (settled + minimum age {MIN_AGE}s met)")
        self._dispatch(dir_path)

    def _dispatch(
        self,
        dir_path: Path,
        *,
        expected_generation: int | None = None,
    ) -> None:
        with self._lock:
            readiness_generation = self._readiness_generations.get(
                dir_path,
                0,
            )
            if (
                expected_generation is not None
                and expected_generation != readiness_generation
            ):
                return

        # Guard against processing a directory already being handled by another thread.
        with _processing_dirs_lock:
            already = any(
                dir_path == p or _is_subpath(dir_path, p) or _is_subpath(p, dir_path)
                for p in _processing_dirs
            )
        if already:
            log.info(f"  Skipping '{dir_path.name}': already being processed.")
            return
        _process_directory_when_ready(
            dir_path,
            self,
            readiness_generation=readiness_generation,
        )

    @contextmanager
    def _hold_commit_guard(self, dir_path: Path, generation: int):
        # Serialize the final generation check and rename with notify().
        with self._lock:
            yield self._readiness_generations.get(dir_path, 0) == generation

    def _hold_stalled_archives(
        self, dir_path: Path, result: DirectoryReadinessResult, generation: int,
    ) -> bool:
        """Claim processing exclusion before considering any holding move."""
        if not self.readiness_holds.enabled:
            return False
        with _processing_dirs_lock:
            if any(dir_path == p or _is_subpath(dir_path, p) or _is_subpath(p, dir_path)
                   for p in _processing_dirs):
                return False
            _processing_dirs.add(dir_path)
        try:
            def probe(path):
                fresh = probe_cbz_zip_readiness(path)
                return None if fresh.status == ZipReadiness.READY else fresh.reason

            return self.readiness_holds.consider(
                dir_path,
                ((entry.path, entry.reason) for entry in result.entries
                 if entry.status != ZipReadiness.READY),
                probe=probe,
                commit_guard=lambda: self._hold_commit_guard(dir_path, generation),
            )
        finally:
            with _processing_dirs_lock:
                _processing_dirs.discard(dir_path)

    def _current_readiness_generation(self, dir_path: Path) -> int:
        with self._lock:
            return self._readiness_generations.get(dir_path, 0)

    def _readiness_generation_is_current(
        self,
        dir_path: Path,
        generation: int,
    ) -> bool:
        with self._lock:
            return self._readiness_generations.get(dir_path, 0) == generation

    def _schedule_readiness_retry(
        self,
        dir_path: Path,
        *,
        expected_generation: int | None = None,
    ) -> bool:
        with self._lock:
            if self._closed or self._poll_stop_locked():
                return False
            current_generation = self._readiness_generations.get(
                dir_path,
                0,
            )
            if (
                expected_generation is not None
                and expected_generation != current_generation
            ):
                return False

            existing = self._readiness_retry_timers.get(dir_path)
            if existing:
                existing.cancel()
            retry_generation = current_generation + 1
            self._readiness_generations[dir_path] = retry_generation
            timer = threading.Timer(
                READINESS_RETRY_DELAY_SECONDS,
                self._tracked_callback(self._on_readiness_retry),
                args=[dir_path, retry_generation],
            )
            self._readiness_retry_timers[dir_path] = timer
            timer.start()
            return True

    def _on_readiness_retry(
        self,
        dir_path: Path,
        readiness_generation: int,
    ) -> None:
        with self._lock:
            if (
                self._readiness_generations.get(dir_path, 0)
                != readiness_generation
            ):
                return
            self._readiness_retry_timers.pop(dir_path, None)
        self._dispatch(
            dir_path,
            expected_generation=readiness_generation,
        )


# ─────────────────────────────────────────────
# WATCHDOG EVENT HANDLER
# ─────────────────────────────────────────────
class CBZHandler(FileSystemEventHandler):
    def __init__(self, tracker: DirectorySettleTracker):
        self.tracker = tracker

    def _handle(self, path: Path) -> None:
        # Some move events can name an external destination. Opt-in holding
        # must never feed those archives back into the import timers.
        if (self.tracker.readiness_holds.enabled
                and not path.absolute().is_relative_to(self.tracker.readiness_holds.watch_root)):
            return
        if path.suffix.lower() != ".cbz":
            return
        if path_contains_excluded_directory(
            path,
            WATCH_FOLDER,
            excluded_directories=DEFAULT_EXCLUDED_DIRECTORIES,
        ):
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
def _discover_startup_directories(
    watch_path: Path,
    tracker: DirectorySettleTracker,
) -> None:
    """
    Route directories that already contain .cbz files at watcher startup
    through the same readiness gate as live filesystem events, rather than
    calling process_and_move_directory() directly -- a directory dropped
    in while the watcher was offline deserves the same
    probe_cbz_directory_readiness() check as one that arrives while it's
    running.
    """
    if tracker.stop_requested():
        return
    for subdir in sorted(watch_path.iterdir()):
        if tracker.stop_requested():
            return
        if subdir.is_dir() and any(
            path.suffix.casefold() == ".cbz"
            for path in iter_files_excluding_directories(subdir)
        ):
            log.info(f"Found existing directory at startup: {subdir.name}")
            _process_directory_when_ready(subdir, tracker)


def build_argument_parser() -> argparse.ArgumentParser:
    """Build watcher integration, graceful stop and opt-in holding options."""
    parser = argparse.ArgumentParser(description="Watch and import incoming CBZ archives")
    parser.add_argument(
        "--series-resolver",
        action="store_true",
        help=(
            "Resolve canonical series names and import directly into the Z libraries; "
            "ambiguous matches remain in legacy staging."
        ),
    )
    parser.add_argument(
        "--resolver-comix-root",
        type=Path,
        default=SERIES_RESOLVER_ROOTS["comix"],
        help=r"Canonical Comix library (default: Z:\comics\Comix).",
    )
    parser.add_argument(
        "--resolver-manga-root",
        type=Path,
        default=SERIES_RESOLVER_ROOTS["manga"],
        help=r"Canonical Manga library (default: Z:\comics\Manga).",
    )
    parser.add_argument(
        "--resolver-quarantine-root",
        type=Path,
        default=SERIES_RESOLVER_QUARANTINE_ROOT,
        help=r"Conflict quarantine outside Komga roots (default: Z:\comics\_resolver_quarantine).",
    )
    parser.add_argument(
        "--resolver-threshold",
        type=float,
        default=SERIES_RESOLVER_THRESHOLD,
        help="Minimum fuzzy title similarity from 0 to 1 (default: 0.95).",
    )
    parser.add_argument(
        "--resolver-refresh",
        action="store_true",
        help="Rebuild both library registries before the watcher starts.",
    )
    parser.add_argument(
        "--ai-decensor",
        action="store_true",
        help="Run Camelia on each archive before routing the directory.",
    )
    parser.add_argument(
        "--ai-decensor-model",
        choices=("black_bars", "white_bars", "transparent_black", "mosaic"),
        action="append",
        dest="ai_decensor_models",
        help=(
            "Camelia stage to run. Repeat this option to run stages in order; "
            "defaults to black_bars."
        ),
    )
    parser.add_argument(
        "--quarantine-root", type=Path, default=QUARANTINE_ROOT,
        help="Where a file-name collision's losing copy is moved (never deleted).",
    )
    parser.add_argument(
        "--processing-root", type=Path, default=PROCESSING_ROOT,
        help="Where arrivals are claimed and processed, outside the watch folder "
             "and on its volume.",
    )
    parser.add_argument(
        "--review-root", type=Path, default=REVIEW_ROOT,
        help="Where archives needing a decision are moved; never purged.",
    )
    parser.add_argument("--camelia-root", type=Path, default=CAMELIA_ROOT)
    parser.add_argument("--camelia-python", type=Path, default=CAMELIA_PYTHON)
    parser.add_argument(
        "--ai-decensor-backup-dir",
        type=Path,
        default=AI_DECENSOR_BACKUP_DIR,
    )
    parser.add_argument(
        "--resolver-normalize-apostrophes",
        action="store_true",
        help=(
            "Normalize apostrophes in library folder/file names before registry "
            "loading; merge collisions and quarantine smaller non-duplicates."
        ),
    )
    parser.add_argument(
        "--ai-decensor-reprocess", action="store_true",
        help="Run selected Camelia methods even if already recorded in ComicInfo tags.",
    )
    parser.add_argument(
        "--ai-decensor-archive-dir",
        type=Path,
        default=AI_DECENSOR_ARCHIVE_DIR,
        help="Verified long-term original backups, grouped by series (default: F:\\ai-decensor-originals).",
    )
    parser.add_argument(
        "--readiness-stall-minutes", type=float, default=30,
        help="Minutes without observed archive progress before holding (default: 30; 0 disables).",
    )
    parser.add_argument(
        "--readiness-hold-root", type=Path,
        help="Existing writable same-volume directory outside the watch tree; holds are disabled unless set.",
    )
    parser.add_argument("--number-style", choices=("auto", "issue", "chapter"), default="auto",
                        help="Number syntax used when a nonempty registry enables media naming.")
    parser.add_argument("--number-style-registry", type=Path, default=DEFAULT_REGISTRY,
                        help="Shared media roots; an empty registry keeps legacy watcher naming.")
    parser.add_argument("--stop-file", type=Path, default=None,
                        help="Absolute per-session stop request path; its parent must exist.")
    return parser


def _run_watcher(argv: list[str] | None = None):
    """Start the watcher with command-line-selected optional integrations."""
    global _number_style_registry, _number_style_requested
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    try:
        args.stop_file = _validated_stop_file(args.stop_file)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    watch_path = Path(WATCH_FOLDER)
    try:
        os.makedirs(watch_path, exist_ok=True)
    except OSError as exc:
        raise WatcherInstanceRefused(f"Cannot prepare watch root {watch_path}: {exc}") from exc
    with watcher_instance_lock(watch_path) as instance:
        log.info("Watcher mutex acquired: %s; supplied=%s; resolved=%s; abandoned=%s",
                 instance.mutex_name, instance.supplied_root, instance.resolved_root, instance.abandoned)
        previous = _number_style_registry, _number_style_requested
        try:
            return _configure_and_watch(args, watch_path)
        finally:
            _number_style_registry, _number_style_requested = previous


def _configure_and_watch(args: argparse.Namespace, watch_path: Path):
    """Apply session globals only after exclusive root ownership is held."""
    global AI_DECENSOR_ENABLED, AI_DECENSOR_MODELS, AI_DECENSOR_REPROCESS
    global CAMELIA_ROOT, CAMELIA_PYTHON, AI_DECENSOR_BACKUP_DIR, AI_DECENSOR_ARCHIVE_DIR
    global QUARANTINE_ROOT, PROCESSING_ROOT, REVIEW_ROOT
    global SERIES_RESOLVER_ENABLED, SERIES_RESOLVER_ROOTS
    global SERIES_RESOLVER_QUARANTINE_ROOT, SERIES_RESOLVER_THRESHOLD
    global SERIES_RESOLVER_REFRESH, SERIES_RESOLVER_NORMALIZE_APOSTROPHES
    global _number_style_registry, _number_style_requested
    try:
        _number_style_registry = NumberStyleRegistry.load(args.number_style_registry)
        _number_style_requested = args.number_style
    except NumberStyleRefused as exc:
        build_argument_parser().error(str(exc))
    AI_DECENSOR_ENABLED = args.ai_decensor
    AI_DECENSOR_MODELS = list(dict.fromkeys(
        args.ai_decensor_models or ["black_bars"]
    ))
    AI_DECENSOR_REPROCESS = args.ai_decensor_reprocess
    CAMELIA_ROOT = args.camelia_root
    CAMELIA_PYTHON = args.camelia_python
    QUARANTINE_ROOT = args.quarantine_root
    PROCESSING_ROOT = args.processing_root
    REVIEW_ROOT = args.review_root
    AI_DECENSOR_BACKUP_DIR = args.ai_decensor_backup_dir
    AI_DECENSOR_ARCHIVE_DIR = args.ai_decensor_archive_dir
    SERIES_RESOLVER_ENABLED = args.series_resolver
    SERIES_RESOLVER_ROOTS = {
        "comix": args.resolver_comix_root,
        "manga": args.resolver_manga_root,
    }
    SERIES_RESOLVER_QUARANTINE_ROOT = args.resolver_quarantine_root
    SERIES_RESOLVER_THRESHOLD = args.resolver_threshold
    SERIES_RESOLVER_REFRESH = args.resolver_refresh
    SERIES_RESOLVER_NORMALIZE_APOSTROPHES = args.resolver_normalize_apostrophes

    return _watch_with_config(args, watch_path)


def _watch_with_config(args: argparse.Namespace, watch_path: Path):
    """Run startup and the observer while the caller owns the root mutex."""
    try:
        tracker = DirectorySettleTracker(
            watch_root=watch_path, readiness_hold_root=args.readiness_hold_root,
            readiness_stall_minutes=args.readiness_stall_minutes, stop_file=args.stop_file,
        )
    except (OSError, ValueError) as exc:
        build_argument_parser().error(str(exc))

    try:
        return _watch_session(args, watch_path, tracker)
    finally:
        tracker.close()
        if tracker._stop_latched:
            log.info("CBZ Watcher stopped by stop-file request: %s", tracker._stop_file)
        else:
            log.info("CBZ Watcher stopped.")


def _watch_session(args: argparse.Namespace, watch_path: Path, tracker: DirectorySettleTracker):
    """Keep discovery and observer work inside the tracker's draining lifetime."""
    if tracker.stop_requested():
        return
    _load_routing()
    _load_routing_v2()
    _load_series_resolvers()

    log.info("=" * 60)
    log.info("CBZ Watcher started")
    log.info("  Media naming: %s", "configured registry" if _media_naming_enabled() else "off (legacy naming)")
    log.info(f"  Watching : {WATCH_FOLDER}")
    log.info(f"  Routing  : {ROUTING_FILE}")
    if _router is not None:
        log.info(f"  Routing v2: {_router.mode} ({ROUTING_V2_FILE})")
    elif ROUTING_V2_MODE == "off":
        log.info("  Routing v2: off")
    else:
        # Never report the requested mode as if it were in effect.
        log.info(f"  Routing v2: off (requested {ROUTING_V2_MODE}; "
                 f"initialization failed)")
    log.info(f"  Log      : {LOG_FILE}")
    log.info(f"  Settle   : {SETTLE_DELAY}s after last file event")
    if SERIES_RESOLVER_ENABLED:
        log.info(
            f"  Resolver : enabled at {SERIES_RESOLVER_THRESHOLD:.1%}; "
            f"quarantine {SERIES_RESOLVER_QUARANTINE_ROOT}"
        )
    if AI_DECENSOR_ENABLED:
        log.info(
            f"  Camelia  : {' -> '.join(AI_DECENSOR_MODELS)} ({CAMELIA_ROOT})"
        )
        log.info(f"  AI backup: {AI_DECENSOR_BACKUP_DIR}")
        log.info(f"  AI archive: {AI_DECENSOR_ARCHIVE_DIR}")
    log.info("=" * 60)

    log.info(
        "  Readiness holds: %s; inactivity %.1f minutes; root %s",
        "enabled" if tracker.readiness_holds.enabled else "disabled",
        args.readiness_stall_minutes, args.readiness_hold_root,
    )

    if tracker.stop_requested():
        return
    if PROCESSING_ROOT is not None:
        library_roots = [*_routing_destinations.values(), _routing_default,
                         *SERIES_RESOLVER_ROOTS.values()]
        try:
            _claims.validate_roots(watch_path, PROCESSING_ROOT, REVIEW_ROOT,
                                   QUARANTINE_ROOT, library_roots)
        except _claims.ClaimConfigError as exc:
            # Claiming would not be a same-volume rename outside the synced
            # folder; working around it would change watched content first.
            log.critical(f"Refusing to start: {exc}")
            raise WatcherInstanceRefused(f"unsafe claim roots: {exc}") from exc
        log.info(f"  Processing: {PROCESSING_ROOT}")
        log.info(f"  Review    : {REVIEW_ROOT}")
        log.info(f"  Quarantine: {QUARANTINE_ROOT} (purged after "
                 f"{_claims.QUARANTINE_RETENTION_DAYS} days)")
        for purged in _claims.purge_quarantine(QUARANTINE_ROOT):
            log.info(f"  Quarantine purged: {purged}")
        # Rewrites happen in claims now; the synced folder is never touched.
        PROCESSING_ROOT.mkdir(parents=True, exist_ok=True)
        _clean_up_stale_rewrite_files(PROCESSING_ROOT)
        _resume_claims()
    else:
        _clean_up_stale_rewrite_files(watch_path)

    _discover_startup_directories(watch_path, tracker)

    if tracker.stop_requested():
        return
    handler  = CBZHandler(tracker)
    observer = Observer()
    try:
        observer.schedule(handler, str(watch_path), recursive=True)
        observer.start()
        try:
            while not tracker.stop_requested():
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("Shutting down watcher...")
    finally:
        observer.stop()
        if observer.ident is not None:
            observer.join()


def main(argv: list[str] | None = None):
    """Run one watcher session and release its media policy on every exit.

    A stopped/failed CLI session must not configure later embedded callers
    or another session in the same process with stale policy state.
    """
    try:
        return _run_watcher(argv)
    except WatcherInstanceRefused as exc:
        log.error("Watcher start refused: %s", exc)
        return 3
    except WatcherInstanceCleanupError as exc:
        log.error("Watcher shutdown failed: %s", exc)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
