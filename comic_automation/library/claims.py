"""Claim-first processing: an arrival leaves the watch folder before any change.

The watch folder is synced by MEGA, so every in-place rename or ComicInfo
write there is uploaded, and an archive the watcher held in place was
re-processed on every pass. Issue #174 (data loss) came from that second
property: what had been decided about a held archive had to be remembered
somewhere, and every way of losing that memory lost a book.

A claim is a unique folder in the processing root (outside every watch and
sync root, on the same volume) holding:

* ``claim.json`` -- the record, written **before** anything moves: the claim
  id, the watch root, the arrival's source folder and relative path, its
  routing, and later its planned dispositions and AI-backup custody;
* the payload -- the arrival's series folder (or, for loose archives in a
  mixed drop point, a folder of those archives), moved in by a same-volume
  rename, the first change ever made to the arrival.

The folder an archive is in is its state: processing (unfinished work,
resumed on start), review (decision needed, kept indefinitely), quarantine
(not kept, purged 30 days after placement), or the library. Nothing about a
held archive needs remembering, because it is no longer where arrivals are.

Contracts (PR replacing #175; Codex review, 2026-11-09):

1. A failed record write or claim rename leaves the arrival untouched and
   retryable. Roots are validated on physical paths (junctions resolved)
   and must share the watch root's volume; an invalid configuration is
   refused, never worked around by copy-and-delete inside the sync tree.
2. The record is written before the rename, so a payload is always
   recoverable; a payload with a missing or corrupt record is preserved
   whole under ``review/_unrecovered_claims`` rather than guessed at. A
   recreated source path is a new arrival; it never affects an old claim.
3. Each claim has one owner at a time (``OwnerLock``), also across watcher
   roots sharing a processing root.
4. Retention purges quarantine only, by placement date; processing and
   review are never purged.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from comic_automation.series_resolver import unique_path

RECORD_NAME = "claim.json"
LOCK_NAME = "owner.lock"
RECORD_VERSION = 1
UNRECOVERED_DIR = "_unrecovered_claims"
QUARANTINE_RETENTION_DAYS = 30
_DATED_FOLDER = re.compile(r"\d{4}-\d{2}-\d{2}")


class ClaimConfigError(ValueError):
    """The processing, review or quarantine root cannot be used safely."""


def _physical(path: Path) -> Path:
    """The path with every symlink and junction resolved, as far as it exists."""
    return Path(os.path.realpath(path))


def _key(path: Path) -> str:
    return os.path.normcase(str(_physical(path))).rstrip("\\/")


def _overlaps(a: Path, b: Path) -> bool:
    """Whether one path is the other or contains it (physically)."""
    ka, kb = _key(a), _key(b)
    return ka == kb or ka.startswith(kb + os.sep) or kb.startswith(ka + os.sep)


def _volume(path: Path) -> int:
    """The device of *path*, or of its nearest existing ancestor."""
    probe = _physical(path)
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return os.stat(probe).st_dev


def validate_roots(watch_root: Path, processing: Path, review: Path, quarantine: Path,
                   library_roots=()) -> None:
    """Refuse a configuration under which claiming would not be safe.

    The three roots must lie outside the watch root (else the watcher would
    discover its own work) and outside every library root, must not overlap
    each other, and must be on the watch root's volume, so that a claim and
    every later disposition is one atomic rename.
    """
    roots = {"processing": processing, "review": review, "quarantine": quarantine}
    for name, root in roots.items():
        if _overlaps(root, watch_root):
            raise ClaimConfigError(f"{name} root {root} overlaps the watch root {watch_root}")
        for library in library_roots:
            if library and _overlaps(root, Path(library)):
                raise ClaimConfigError(f"{name} root {root} overlaps the library root {library}")
        if _volume(root) != _volume(watch_root):
            raise ClaimConfigError(
                f"{name} root {root} is not on the watch root's volume: a claim must be a "
                f"same-volume rename, never a copy and delete inside the synced folder")
    names = list(roots)
    for i, first in enumerate(names):
        for second in names[i + 1:]:
            if _overlaps(roots[first], roots[second]):
                raise ClaimConfigError(f"the {first} and {second} roots overlap")


def read_record(claim_dir: Path) -> dict | None:
    """The claim's record, or None when it is missing or unreadable."""
    try:
        record = json.loads((claim_dir / RECORD_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("id") == claim_dir.name else None


def write_record(claim_dir: Path, record: dict) -> None:
    """Replace the record atomically (temporary file, flush, rename). Raises OSError."""
    temporary = claim_dir / (RECORD_NAME + ".tmp")
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(record, stream, indent=1, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, claim_dir / RECORD_NAME)


@dataclass
class Claim:
    """One claim folder and its record."""

    dir: Path
    record: dict

    @property
    def id(self) -> str:
        return self.dir.name

    @property
    def payload(self) -> Path | None:
        """The payload folder: the claim's one subdirectory, whatever its name.

        Found by listing, not by the recorded name, so a crash between
        renaming the payload (name cleaning) and updating the record cannot
        hide it.
        """
        dirs = [p for p in self.dir.iterdir() if p.is_dir()]
        return dirs[0] if len(dirs) == 1 else None

    def save(self) -> None:
        write_record(self.dir, self.record)


def _new_claim(processing_root: Path, context: dict) -> Claim:
    claim_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:10]
    claim_dir = processing_root / claim_id
    claim_dir.mkdir(parents=True)
    record = {"version": RECORD_VERSION, "id": claim_id, "state": "claiming",
              "created": time.strftime("%Y-%m-%d %H:%M:%S"), **context}
    try:
        write_record(claim_dir, record)
    except OSError:
        shutil.rmtree(claim_dir, ignore_errors=True)
        raise
    return Claim(claim_dir, record)


def has_arrival_files(claim_dir: Path) -> bool:
    """Whether the claim folder holds any file besides its record and lock."""
    return any(p.is_file() and p.name not in (RECORD_NAME, RECORD_NAME + ".tmp", LOCK_NAME)
               for p in claim_dir.rglob("*"))


def abandon(claim: Claim) -> None:
    """Remove a claim that holds no arrival file (its rename never happened)."""
    if not has_arrival_files(claim.dir):
        shutil.rmtree(claim.dir, ignore_errors=True)


def claim_directory(series_dir: Path, processing_root: Path, context: dict) -> Claim | None:
    """Claim a whole series folder with one same-volume rename.

    Returns None, with the arrival untouched, if the record cannot be
    written or the rename fails (an open file, say); the caller retries.
    """
    try:
        claim = _new_claim(processing_root, {**context, "kind": "directory",
                                             "payload_name": series_dir.name})
    except OSError:
        return None
    try:
        os.rename(series_dir, claim.dir / series_dir.name)
    except OSError:
        abandon(claim)
        return None
    claim.record["state"] = "claimed"
    try:
        claim.save()
    except OSError:
        pass  # the payload in the claim folder is the evidence that matters
    return claim


def claim_files(parent: Path, files: list[Path], processing_root: Path,
                context: dict, kind: str = "loose") -> Claim | None:
    """Claim files of a folder that must stay, one same-volume rename each.

    ``kind`` "loose": the archives of a mixed drop point, whose folder holds
    other series. ``kind`` "files": the files of a series folder that also
    holds subfolders (a sync tool's history, say) which are not the
    arrival's to take. A rename that fails stops the claim there; files
    already moved are claimed and the rest are left untouched for a later
    claim. None when nothing was claimed.
    """
    try:
        claim = _new_claim(processing_root, {**context, "kind": kind,
                                             "payload_name": parent.name,
                                             "files": [f.name for f in files]})
        payload = claim.dir / parent.name
        payload.mkdir()
    except OSError:
        return None
    for path in files:
        try:
            os.rename(path, payload / path.name)
        except OSError:
            break
    if not has_arrival_files(claim.dir):
        abandon(claim)
        return None
    claim.record["state"] = "claimed"
    try:
        claim.save()
    except OSError:
        pass
    return claim


class OwnerLock:
    """An exclusive, crash-released lock on one claim (``owner.lock``).

    The OS releases it when its process dies, so a crashed owner never
    leaves a claim locked, and two watchers sharing a processing root can
    never work on one claim at once.
    """

    def __init__(self, claim_dir: Path):
        self.path = claim_dir / LOCK_NAME
        self._stream = None

    def acquire(self) -> bool:
        try:
            stream = open(self.path, "a+b")
        except OSError:
            return False
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - the watcher runs on Windows
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self._stream = stream
        return True

    def release(self) -> None:
        if self._stream is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._stream.seek(0)
                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self._stream.close()
        self._stream = None


def claim_dirs(processing_root: Path) -> list[Path]:
    """Every claim folder in the processing root, oldest first."""
    try:
        return sorted(p for p in processing_root.iterdir() if p.is_dir())
    except FileNotFoundError:
        return []


def preserve_unrecoverable(claim_dir: Path, review_root: Path) -> Path:
    """Move a claim that cannot be trusted, whole, to review. Raises OSError."""
    target = unique_path(review_root / UNRECOVERED_DIR / claim_dir.name)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.rename(claim_dir, target)
    return target


def place(path: Path, root: Path, series: str) -> Path:
    """Move *path* to ``root/<YYYY-MM-DD>/<series>/`` under a unique name.

    A same-volume rename (the roots are validated), so the archive is either
    still where it was or entirely at the target. Raises OSError.
    """
    target = unique_path(root / time.strftime("%Y-%m-%d") / series / path.name)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.rename(path, target)
    return target


def purge_quarantine(quarantine_root: Path, days: int = QUARANTINE_RETENTION_DAYS,
                     today: _dt.date | None = None) -> list[Path]:
    """Delete quarantine placements older than *days*, by placement date.

    Only the dated folders (``YYYY-MM-DD``) the watcher creates are purged,
    and only in the quarantine root: anything else there (hand-placed
    items) and every processing or review folder are never touched.
    """
    today = today or _dt.date.today()
    removed = []
    try:
        entries = list(quarantine_root.iterdir())
    except FileNotFoundError:
        return removed
    for entry in entries:
        if not entry.is_dir() or not _DATED_FOLDER.fullmatch(entry.name):
            continue
        try:
            placed = _dt.date.fromisoformat(entry.name)
        except ValueError:
            continue
        if (today - placed).days > days:
            try:
                shutil.rmtree(entry)
            except OSError:
                continue  # a locked file: purged at a later start
            removed.append(entry)
    return removed
