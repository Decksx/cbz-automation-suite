"""Reconcile completed Camelia backlog replacements without rescanning X:.

The Camelia backlog's job_complete events are a durable source of changed
paths. Each event and this consumer's cursor commit in one ComicAutomation
transaction, so a crash replays safely without creating another archive ID.
No migrations or model workers are run here. The archive hash and current
revision update atomically with the cursor; normal inspection and page-hash
jobs refresh the remaining derived evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

from comic_automation.archive.hashing import ArchiveHashRepository, calculate_archive_hash
from comic_automation.database.connection import database_connection
from comic_automation.database.dal import connection_scope, transaction
from comic_automation.library.discovery import DiscoveredArchive, normalize_library_path
from comic_automation.library.exclusions import normalize_excluded_directories, path_contains_excluded_directory
from comic_automation.library.repository import LibraryRepository
from comic_automation.jobs import EnqueueOutcome, JobQueue


class HandoffRefused(RuntimeError):
    """The event cannot safely update the currently tracked library book."""


@dataclass(frozen=True)
class HandoffResult:
    event_id: int
    archive_id: int
    path: str
    action: str
    inspection_queued: bool
    page_hash_queued: bool
    sha256: str


@dataclass(frozen=True)
class PreparationResult:
    archive_id: int | None
    path: str
    action: str
    sha256: str


def _cursor_key(camelia_database: Path, camelia: sqlite3.Connection, root: Path) -> str:
    row = camelia.execute(
        "SELECT value FROM controls WHERE name='instance_uuid'"
    ).fetchone()
    if not row or not row["value"]:
        raise HandoffRefused("Camelia backlog is missing its stable instance identifier")
    database_key = (
        str(camelia_database.resolve()).casefold() + "|" + row["value"]
        + "|" + str(root).casefold()
    ).encode("utf-8")
    return "camelia_handoff_last_event_" + hashlib.sha256(database_key).hexdigest()[:16]


def _last_event_id(connection: sqlite3.Connection, key: str) -> int:
    row = connection.execute(
        "SELECT value FROM application_settings WHERE key=?", (key,)
    ).fetchone()
    return int(row["value"]) if row else 0


def _events(connection: sqlite3.Connection, last_id: int, root: Path, limit: int) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT e.id AS event_id, b.id AS book_id, b.source_path,
                  b.file_size, b.mtime_ns
           FROM events AS e JOIN books AS b ON b.id=e.book_id
           WHERE e.event_type='job_complete' AND e.id>?
             AND b.state='completed' AND b.root_path=? COLLATE NOCASE
             AND e.id=(SELECT MAX(newer.id) FROM events AS newer
                       WHERE newer.book_id=b.id AND newer.event_type='job_complete')
           ORDER BY e.id LIMIT ?""",
        (last_id, str(root), limit),
    ).fetchall()


def _tracked_location(connection: sqlite3.Connection, path: Path) -> sqlite3.Row:
    row = connection.execute(
        """SELECT fl.id AS location_id, fl.archive_id, fl.path, fl.is_current,
                  fl.file_size, fl.modified_time_ns
           FROM file_locations AS fl WHERE fl.path=? COLLATE NOCASE""",
        (str(path),),
    ).fetchone()
    if row is None:
        raise HandoffRefused(f"Refusing untracked X: book; discovery must establish identity first: {path}")
    if not row["is_current"]:
        raise HandoffRefused(f"Refusing non-current location: {path}")
    return row


def _stored_hash_matches(connection: sqlite3.Connection, archive_id: int, digest: str) -> bool:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_hashes'"
    ).fetchone()
    if not exists:
        return False
    row = connection.execute(
        "SELECT digest FROM archive_hashes WHERE archive_id=?", (archive_id,)
    ).fetchone()
    return bool(row and row["digest"] == digest)


def _candidate(event: sqlite3.Row, root: Path):
    source = Path(str(event["source_path"]))
    if source.is_symlink():
        raise HandoffRefused(f"Completed archive is a symbolic link: {source}")
    path = normalize_library_path(source)
    if (path.suffix.casefold() != ".cbz" or not path.is_relative_to(root)
            or path_contains_excluded_directory(
                path, root, excluded_directories=normalize_excluded_directories()
            )):
        raise HandoffRefused(f"Completed path is not a CBZ under the selected library root: {path}")
    if not path.is_file():
        raise HandoffRefused(f"Completed archive is missing: {path}")
    measured = calculate_archive_hash(path)
    if measured.file_size != event["file_size"] or measured.modified_time_ns != event["mtime_ns"]:
        raise HandoffRefused(f"Camelia completion fingerprint no longer matches the file: {path}")
    archive = DiscoveredArchive(
        path=path, extension=".cbz", file_size=measured.file_size,
        modified_time_ns=measured.modified_time_ns,
    )
    return path, archive, measured


def reconcile_completed(
    *, camelia_database: Path, comic_database: Path, root: Path,
    limit: int = 25, apply: bool = False,
) -> list[HandoffResult]:
    """Plan or atomically record completed X: replacements, in event order."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    root = normalize_library_path(root)
    if not root.is_dir():
        raise FileNotFoundError(f"Library root is unavailable: {root}")
    if not comic_database.is_file():
        raise FileNotFoundError(f"ComicAutomation database is unavailable: {comic_database}")
    with connection_scope(camelia_database, readonly=True) as camelia:
        key = _cursor_key(camelia_database, camelia, root)
        if apply:
            with database_connection(comic_database) as comic:
                return _reconcile(camelia, comic, key, root, limit, apply=True)
        with connection_scope(comic_database, readonly=True) as comic:
            return _reconcile(camelia, comic, key, root, limit, apply=False)


def _reconcile(camelia, comic, key, root, limit, *, apply: bool) -> list[HandoffResult]:
    if apply:
        _require_evidence_schema(comic)
    last_id = _last_event_id(comic, key)
    results = []
    for event in _events(camelia, last_id, root, limit):
        path, archive, measured_hash = _candidate(event, root)
        if apply:
            with transaction(comic):
                result = _reconcile_one(comic, event, path, archive, measured_hash, key, apply=True)
        else:
            result = _reconcile_one(comic, event, path, archive, measured_hash, key, apply=False)
        results.append(result)
    return results


def _require_evidence_schema(comic: sqlite3.Connection) -> None:
    required = {"archive_inspections", "archive_hashes", "archive_pages",
                "page_hashes", "archive_revisions"}
    present = {row["name"] for row in comic.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    missing = sorted(required - present)
    if missing:
        raise HandoffRefused(
            "ComicAutomation database is not ready for decensor evidence jobs; "
            f"missing tables: {', '.join(missing)}. No migrations were applied."
        )


def prepare_source(*, comic_database: Path, root: Path, source: Path,
                   apply: bool = False) -> PreparationResult:
    """Establish the current CBZ identity and byte revision before model work."""
    root = normalize_library_path(root)
    if not root.is_dir():
        raise FileNotFoundError(f"Library root is unavailable: {root}")
    if not comic_database.is_file():
        raise FileNotFoundError(f"ComicAutomation database is unavailable: {comic_database}")
    if source.is_symlink():
        raise HandoffRefused(f"Source is a symbolic link: {source}")
    path = normalize_library_path(source)
    if (path.suffix.casefold() != ".cbz" or not path.is_relative_to(root)
            or path_contains_excluded_directory(
                path, root, excluded_directories=normalize_excluded_directories()
            )):
        raise HandoffRefused(f"Source is not an active CBZ under the library root: {path}")
    if not path.is_file():
        raise FileNotFoundError(f"Source is unavailable: {path}")
    measured = calculate_archive_hash(path)
    archive = DiscoveredArchive(path=path, extension=".cbz", file_size=measured.file_size,
                                modified_time_ns=measured.modified_time_ns)
    if apply:
        with database_connection(comic_database) as comic:
            _require_evidence_schema(comic)
            with transaction(comic):
                return _prepare_one(comic, path, archive, measured, apply=True)
    with connection_scope(comic_database, readonly=True) as comic:
        _require_evidence_schema(comic)
        return _prepare_one(comic, path, archive, measured, apply=False)


def _prepare_one(comic, path, archive, measured, *, apply: bool) -> PreparationResult:
    location = comic.execute(
        """SELECT id AS location_id, archive_id, path, is_current, file_size, modified_time_ns
           FROM file_locations WHERE path=? COLLATE NOCASE""", (str(path),)
    ).fetchone()
    new_location = location is None
    if location is not None and not location["is_current"]:
        raise HandoffRefused(f"Source location is recorded as non-current: {path}")
    changed = (location is None or location["file_size"] != archive.file_size
               or location["modified_time_ns"] != archive.modified_time_ns)
    digest_row = None
    if location is not None and not changed:
        digest_row = comic.execute(
            "SELECT digest FROM archive_hashes WHERE archive_id=?", (location["archive_id"],)
        ).fetchone()
        if digest_row is not None and digest_row["digest"] != measured.digest:
            raise HandoffRefused(f"Source bytes disagree with same-size/same-mtime database hash: {path}")
    archive_id = int(location["archive_id"]) if location is not None else None
    if apply and (changed or location is None or digest_row is None):
        if changed:
            LibraryRepository(comic).record_archive(
                DiscoveredArchive(path=Path(str(location["path"])) if location else path,
                                  extension=".cbz", file_size=archive.file_size,
                                  modified_time_ns=archive.modified_time_ns)
            )
            location = comic.execute(
                "SELECT id AS location_id, archive_id FROM file_locations WHERE path=? COLLATE NOCASE",
                (str(path),),
            ).fetchone()
            archive_id = int(location["archive_id"])
        ArchiveHashRepository(comic).save(
            archive_id=archive_id, location_id=int(location["location_id"]),
            result=measured, enqueue_reinspection=False,
        )
    current = path.stat()
    if current.st_size != measured.file_size or current.st_mtime_ns != measured.modified_time_ns:
        raise HandoffRefused(f"Source changed during registration: {path}")
    action = ("registered" if new_location else "refreshed") if changed else "unchanged"
    if not apply and changed:
        action = "would_register" if new_location else "would_refresh"
    return PreparationResult(archive_id=archive_id, path=str(path),
                             action=action, sha256=measured.digest)


def _reconcile_one(comic, event, path, archive, measured_hash, key, *, apply: bool) -> HandoffResult:
    location = _tracked_location(comic, path)
    archive_id = int(location["archive_id"])
    changed_fingerprint = (
        location["file_size"] != archive.file_size
        or location["modified_time_ns"] != archive.modified_time_ns
    )
    if not changed_fingerprint and not _stored_hash_matches(comic, archive_id, measured_hash.digest):
        raise HandoffRefused(
            f"Bytes may have changed without a size/mtime change; refusing ambiguous update: {path}"
        )
    if apply:
        if changed_fingerprint:
            classification, queued = LibraryRepository(comic).record_archive(
                DiscoveredArchive(
                    path=Path(str(location["path"])), extension=".cbz",
                    file_size=archive.file_size, modified_time_ns=archive.modified_time_ns,
                )
            )
            ArchiveHashRepository(comic).save(
                archive_id=archive_id,
                location_id=int(location["location_id"]),
                result=measured_hash,
                enqueue_reinspection=False,
            )
            page_queued = JobQueue(comic).enqueue_if_absent(
                "hash_archive_pages", archive_id=archive_id, priority=300,
            ) is EnqueueOutcome.CREATED
        else:
            classification, queued = "unchanged", False
            page_queued = False
        current = path.stat()
        if current.st_size != archive.file_size or current.st_mtime_ns != archive.modified_time_ns:
            raise HandoffRefused(f"File changed during handoff: {path}")
        comic.execute(
            """INSERT INTO application_settings(key,value) VALUES(?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                   updated_at=CURRENT_TIMESTAMP""",
            (key, str(event["event_id"])),
        )
    else:
        classification, queued = ("would_change", True) if changed_fingerprint else ("unchanged", False)
        page_queued = changed_fingerprint
    return HandoffResult(
        event_id=int(event["event_id"]), archive_id=archive_id,
        path=str(path), action=classification, inspection_queued=queued,
        page_hash_queued=page_queued,
        sha256=measured_hash.digest,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camelia-database", type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--root", type=Path, default=Path(r"X:\comix"))
    parser.add_argument("--prepare-source", type=Path,
                        help="Register/hash this source before Camelia processes it")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--apply", action="store_true", help="Commit the reviewed handoff; default is read-only")
    args = parser.parse_args(argv)
    try:
        if args.prepare_source is not None:
            result = prepare_source(comic_database=args.database, root=args.root,
                                    source=args.prepare_source, apply=args.apply)
            print(json.dumps({"applied": args.apply, "preparation": asdict(result)}, indent=2))
            return 0
        if args.camelia_database is None:
            parser.error("--camelia-database is required unless --prepare-source is used")
        results = reconcile_completed(
            camelia_database=args.camelia_database, comic_database=args.database,
            root=args.root, limit=args.limit, apply=args.apply,
        )
    except (HandoffRefused, OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(1, f"Camelia handoff refused: {exc}\n")
    print(json.dumps({"applied": args.apply, "count": len(results),
                      "results": [asdict(item) for item in results]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
