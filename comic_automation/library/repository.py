from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path

from comic_automation.jobs import EnqueueOutcome, JobQueue
from comic_automation.library.discovery import (
    DEFAULT_ARCHIVE_EXTENSIONS,
    DiscoveredArchive,
    DiscoverySummary,
    discover_archives,
    normalize_library_path,
)
from comic_automation.library.exclusions import (
    normalize_excluded_directories,
    path_contains_excluded_directory,
)


ProgressCallback = Callable[[int, Path], None]


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _path_key(path: str | Path) -> str:
    return str(
        normalize_library_path(path)
    ).replace("/", "\\").casefold()


class LibraryRepository:
    """
    Persists the results of a read-only library discovery scan: batch
    and checkpoint bookkeeping (source_batches, discovery_checkpoints),
    archive identity and location tracking (archive_files,
    file_locations), and the resulting inspect_archive job enqueues.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.queue = JobQueue(connection)

    def start_batch(self, source_path: str | Path) -> int:
        normalized = str(normalize_library_path(source_path))

        # A source_batches row represents this scan run as a whole.
        cursor = self.connection.execute(
            """
            INSERT INTO source_batches (
                source_path,
                status,
                details_json
            )
            VALUES (?, 'running', ?)
            """,
            (
                normalized,
                json.dumps(
                    {"mode": "read-only-discovery"},
                    sort_keys=True,
                ),
            ),
        )

        batch_id = int(cursor.lastrowid)

        # Its paired discovery_checkpoints row starts at zero progress;
        # update_checkpoint() advances it as the scan proceeds so a
        # crash mid-scan can be resumed from the last checkpoint.
        self.connection.execute(
            """
            INSERT INTO discovery_checkpoints (
                batch_id,
                source_path
            )
            VALUES (?, ?)
            """,
            (batch_id, normalized),
        )

        return batch_id

    def find_resumable_batch(
        self,
        source_path: str | Path,
    ) -> sqlite3.Row | None:
        normalized = str(normalize_library_path(source_path))

        # A batch is resumable if it's still 'running' (crashed without
        # finishing) or 'failed' (raised an exception); 'completed'
        # batches are excluded so a finished scan is never resumed by
        # mistake. Most recent batch for the path wins.
        return self.connection.execute(
            """
            SELECT
                sb.id AS batch_id,
                sb.status,
                dc.last_path,
                dc.scanned,
                dc.new_count,
                dc.changed_count,
                dc.unchanged_count,
                dc.jobs_queued,
                dc.error_count
            FROM source_batches AS sb
            JOIN discovery_checkpoints AS dc
              ON dc.batch_id = sb.id
            WHERE sb.source_path = ?
              AND sb.status IN ('running', 'failed')
            ORDER BY sb.id DESC
            LIMIT 1
            """,
            (normalized,),
        ).fetchone()

    def update_checkpoint(
        self,
        *,
        batch_id: int,
        last_path: str | None,
        scanned: int,
        new: int,
        changed: int,
        unchanged: int,
        jobs_queued: int,
        errors: int,
    ) -> None:
        # Overwrites the checkpoint's running totals and last_path (the
        # most recently processed path in scan order), so a resumed
        # scan knows both where to continue and what to add to.
        self.connection.execute(
            """
            UPDATE discovery_checkpoints
            SET
                last_path = ?,
                scanned = ?,
                new_count = ?,
                changed_count = ?,
                unchanged_count = ?,
                jobs_queued = ?,
                error_count = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE batch_id = ?
            """,
            (
                last_path,
                scanned,
                new,
                changed,
                unchanged,
                jobs_queued,
                errors,
                batch_id,
            ),
        )

    def complete_batch(
        self,
        batch_id: int,
        summary: dict,
    ) -> None:
        self.connection.execute(
            """
            UPDATE source_batches
            SET
                status = 'completed',
                details_json = ?
            WHERE id = ?
            """,
            (json.dumps(summary, sort_keys=True), batch_id),
        )

    def fail_batch(
        self,
        batch_id: int,
        error_message: str,
    ) -> None:
        self.connection.execute(
            """
            UPDATE source_batches
            SET
                status = 'failed',
                details_json = ?
            WHERE id = ?
            """,
            (
                json.dumps(
                    {
                        "mode": "read-only-discovery",
                        "error": error_message,
                    },
                    sort_keys=True,
                ),
                batch_id,
            ),
        )

    def record_archive(
        self,
        archive: DiscoveredArchive,
    ) -> tuple[str, bool]:
        path_text = str(archive.path)

        # Look up whether this exact path is already tracked, and if
        # so, whether it's currently considered the live location for
        # its archive.
        row = self.connection.execute(
            """
            SELECT
                fl.id AS location_id,
                fl.archive_id,
                fl.is_current,
                fl.file_size,
                fl.modified_time_ns
            FROM file_locations AS fl
            WHERE fl.path = ?
            """,
            (path_text,),
        ).fetchone()

        now = _utc_timestamp()

        if row is None:
            # Never seen this path before: create a brand-new archive
            # identity and its first location row together.
            archive_cursor = self.connection.execute(
                """
                INSERT INTO archive_files (
                    file_size,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?)
                """,
                (archive.file_size, now, now),
            )
            archive_id = int(archive_cursor.lastrowid)

            self.connection.execute(
                """
                INSERT INTO file_locations (
                    archive_id,
                    path,
                    is_current,
                    file_size,
                    modified_time_ns,
                    first_seen_at,
                    last_seen_at
                )
                VALUES (?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    archive_id,
                    path_text,
                    archive.file_size,
                    archive.modified_time_ns,
                    now,
                    now,
                ),
            )

            self._record_event(
                archive_id,
                "discovered",
                source_path=path_text,
            )

            queued = self._enqueue_inspection_if_absent(
                archive_id,
                path_text,
            )
            return "new", queued

        archive_id = int(row["archive_id"])
        was_current = bool(row["is_current"])
        previous_size = row["file_size"]
        previous_modified = row["modified_time_ns"]

        # "changed" covers both a genuine content change (size or
        # mtime differ from what's recorded) and a path that had been
        # marked missing and has now reappeared (was_current is false).
        changed = (
            not was_current
            or previous_size != archive.file_size
            or previous_modified != archive.modified_time_ns
        )

        self.connection.execute(
            """
            UPDATE file_locations
            SET
                is_current = 1,
                file_size = ?,
                modified_time_ns = ?,
                last_seen_at = ?
            WHERE id = ?
            """,
            (
                archive.file_size,
                archive.modified_time_ns,
                now,
                int(row["location_id"]),
            ),
        )

        if not changed:
            return "unchanged", False

        # File content may have changed: invalidate the previously
        # computed hash/content-signature/page-count so downstream
        # hashing stages recompute them rather than trusting stale
        # values.
        self.connection.execute(
            """
            UPDATE archive_files
            SET
                sha256 = NULL,
                content_signature = NULL,
                file_size = ?,
                page_count = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (archive.file_size, now, archive_id),
        )

        self._record_event(
            archive_id,
            "restored" if not was_current else "changed",
            source_path=path_text,
            details={
                "previous_file_size": previous_size,
                "current_file_size": archive.file_size,
                "previous_modified_time_ns": previous_modified,
                "current_modified_time_ns": (
                    archive.modified_time_ns
                ),
            },
        )

        queued = self._enqueue_inspection_if_absent(
            archive_id,
            path_text,
        )
        return "changed", queued

    def mark_missing(
        self,
        root: str | Path,
        seen_path_keys: set[str],
        *,
        excluded_directories: Iterable[str] | None = None,
    ) -> int:
        library_root = normalize_library_path(root)
        exclusions = normalize_excluded_directories(
            excluded_directories
        )
        missing_count = 0
        now = _utc_timestamp()

        # Every location currently believed to be live, across the
        # whole database (not scoped to this scan's root by the query
        # itself -- the relative_to() check below filters to this
        # root in Python).
        rows = self.connection.execute(
            """
            SELECT id, archive_id, path
            FROM file_locations
            WHERE is_current = 1
            ORDER BY id
            """
        ).fetchall()

        for row in rows:
            location_path = normalize_library_path(row["path"])

            try:
                location_path.relative_to(library_root)
            except ValueError:
                continue

            if path_contains_excluded_directory(
                location_path,
                library_root,
                excluded_directories=exclusions,
            ):
                continue

            if _path_key(location_path) in seen_path_keys:
                continue

            # Not seen during this complete scan of the root: flip
            # is_current off rather than deleting the row, preserving
            # location history for later moves/restores.
            self.connection.execute(
                """
                UPDATE file_locations
                SET is_current = 0, last_seen_at = ?
                WHERE id = ?
                """,
                (now, int(row["id"])),
            )

            self._record_event(
                int(row["archive_id"]),
                "missing",
                source_path=str(location_path),
            )
            missing_count += 1

        return missing_count

    def _enqueue_inspection_if_absent(
        self,
        archive_id: int,
        path: str,
    ) -> bool:
        # Avoid piling up duplicate inspect_archive jobs for the same
        # archive if one is already pending/claimed/running.
        #
        # The separate "does an active job exist?" SELECT this method
        # used to run has been removed in favor of
        # JobQueue.enqueue_if_absent(), which performs the check and the
        # insert as one atomic statement. The old check-then-insert
        # could be raced by any enqueue path that does not hold this
        # method's caller-owned transaction -- notably
        # ArchiveHashRepository._enqueue_reinspection_if_absent(), which
        # targets the same job_type from a worker handler. The return
        # contract is unchanged: True when this call created the job,
        # False when one was already active.
        outcome = self.queue.enqueue_if_absent(
            "inspect_archive",
            archive_id=archive_id,
            payload={"path": path},
            priority=100,
        )

        return outcome is EnqueueOutcome.CREATED

    def _record_event(
        self,
        archive_id: int,
        event_type: str,
        *,
        source_path: str | None = None,
        destination_path: str | None = None,
        details: dict | None = None,
    ) -> None:
        # Appends one row to the file_events audit log; never updates
        # or deletes existing events.
        self.connection.execute(
            """
            INSERT INTO file_events (
                archive_id,
                event_type,
                source_path,
                destination_path,
                details_json
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                archive_id,
                event_type,
                source_path,
                destination_path,
                (
                    json.dumps(details, sort_keys=True)
                    if details is not None
                    else None
                ),
            ),
        )


def scan_library(
    connection: sqlite3.Connection,
    root: str | Path,
    *,
    batch_size: int = 500,
    extensions: Iterable[str] = DEFAULT_ARCHIVE_EXTENSIONS,
    excluded_directories: Iterable[str] | None = None,
    limit: int | None = None,
    resume: bool = False,
    progress_callback: ProgressCallback | None = None,
) -> DiscoverySummary:
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1.")

    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1.")

    started = time.monotonic()
    repository = LibraryRepository(connection)
    library_root = normalize_library_path(root)

    resumable = (
        repository.find_resumable_batch(library_root)
        if resume
        else None
    )

    if resumable is not None:
        batch_id = int(resumable["batch_id"])
        last_checkpoint = resumable["last_path"]
        scanned = int(resumable["scanned"])
        new = int(resumable["new_count"])
        changed = int(resumable["changed_count"])
        unchanged = int(resumable["unchanged_count"])
        jobs_queued = int(resumable["jobs_queued"])
        error_count = int(resumable["error_count"])
        resumed = True

        # Flip a previously 'failed' batch back to 'running' now that
        # it's being resumed.
        connection.execute(
            """
            UPDATE source_batches
            SET status = 'running'
            WHERE id = ?
            """,
            (batch_id,),
        )
    else:
        batch_id = repository.start_batch(library_root)
        last_checkpoint = None
        scanned = 0
        new = 0
        changed = 0
        unchanged = 0
        jobs_queued = 0
        error_count = 0
        resumed = False

    excluded_directory_count = 0
    seen_path_keys: set[str] = set()
    pending: list[DiscoveredArchive] = []
    last_path: str | None = last_checkpoint
    limited = False

    def on_error(path: Path, error: OSError) -> None:
        nonlocal error_count
        error_count += 1

    def on_excluded_directory(path: Path) -> None:
        nonlocal excluded_directory_count
        excluded_directory_count += 1

    def flush() -> None:
        nonlocal new, changed, unchanged, jobs_queued

        if not pending:
            return

        try:
            # Record a whole batch of discovered archives and advance
            # the checkpoint in one transaction, so a crash mid-batch
            # either records all of it or none of it (never a partial,
            # inconsistent checkpoint).
            connection.execute("BEGIN IMMEDIATE")

            for archive in pending:
                classification, queued = (
                    repository.record_archive(archive)
                )

                if classification == "new":
                    new += 1
                elif classification == "changed":
                    changed += 1
                else:
                    unchanged += 1

                if queued:
                    jobs_queued += 1

            repository.update_checkpoint(
                batch_id=batch_id,
                last_path=last_path,
                scanned=scanned,
                new=new,
                changed=changed,
                unchanged=unchanged,
                jobs_queued=jobs_queued,
                errors=error_count,
            )

            connection.execute("COMMIT")
            pending.clear()

        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    try:
        for archive in discover_archives(
            library_root,
            extensions=extensions,
            excluded_directories=excluded_directories,
            on_error=on_error,
            on_excluded_directory=on_excluded_directory,
        ):
            path_text = str(archive.path)

            if (
                last_checkpoint is not None
                and _path_key(path_text)
                <= _path_key(last_checkpoint)
            ):
                seen_path_keys.add(_path_key(path_text))
                continue

            if limit is not None and scanned >= limit:
                limited = True
                break

            scanned += 1
            last_path = path_text
            seen_path_keys.add(_path_key(path_text))
            pending.append(archive)

            if progress_callback is not None:
                progress_callback(scanned, archive.path)

            if len(pending) >= batch_size:
                flush()

        flush()

        # Missing-file detection is unsafe after a partial or resumed scan
        # because the complete set of current paths was not observed.
        missing = 0

        if not limited and not resumed:
            try:
                connection.execute("BEGIN IMMEDIATE")
                missing = repository.mark_missing(
                    library_root,
                    seen_path_keys,
                    excluded_directories=excluded_directories,
                )
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

        elapsed = time.monotonic() - started

        summary_data = {
            "scanned": scanned,
            "new": new,
            "changed": changed,
            "unchanged": unchanged,
            "missing": missing,
            "jobs_queued": jobs_queued,
            "errors": error_count,
            "excluded_directories": excluded_directory_count,
            "resumed": resumed,
            "limited": limited,
            "elapsed_seconds": elapsed,
        }

        if limited:
            repository.update_checkpoint(
                batch_id=batch_id,
                last_path=last_path,
                scanned=scanned,
                new=new,
                changed=changed,
                unchanged=unchanged,
                jobs_queued=jobs_queued,
                errors=error_count,
            )
        else:
            repository.complete_batch(batch_id, summary_data)

        return DiscoverySummary(
            batch_id=batch_id,
            scanned=scanned,
            new=new,
            changed=changed,
            unchanged=unchanged,
            missing=missing,
            jobs_queued=jobs_queued,
            errors=error_count,
            excluded_directories=excluded_directory_count,
            resumed=resumed,
            limited=limited,
            elapsed_seconds=elapsed,
        )

    except Exception as exc:
        if connection.in_transaction:
            connection.execute("ROLLBACK")

        repository.update_checkpoint(
            batch_id=batch_id,
            last_path=last_path,
            scanned=scanned,
            new=new,
            changed=changed,
            unchanged=unchanged,
            jobs_queued=jobs_queued,
            errors=error_count,
        )
        repository.fail_batch(batch_id, str(exc))
        raise
