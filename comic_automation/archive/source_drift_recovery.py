"""Guarded recovery for perceptual jobs whose archive changed in place.

The default command path is a strictly read-only analysis. Applying a
recovery requires the operator to repeat the exact live file size and
mtime reported by that analysis. The apply path then recomputes the
archive hash, structural inspection, exact page inventory, and page
SHA-256 evidence with the production implementations before updating
those records atomically.

The existing pending perceptual job is retained. Its prior error is
cleared and it is made immediately available only after all refreshed
exact evidence commits successfully. Perceptual hashes are not
calculated by this module; the normal bounded worker performs that next
step.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

from comic_automation.archive.hashing import (
    ArchiveHashRepository,
    calculate_archive_hash,
)
from comic_automation.archive.inspection import (
    IMAGE_EXTENSIONS,
    inspect_archive,
)
from comic_automation.archive.page_hashing import (
    ArchivePageHashRepository,
    _natural_key,
    calculate_page_hashes,
)
from comic_automation.archive.perceptual_failure_audit import (
    DatabaseMutatedError,
    fingerprint_database,
    readonly_database_connection,
)
from comic_automation.archive.repository import (
    ArchiveInspectionRepository,
)
from comic_automation.database.connection import database_connection


JOB_TYPE = "hash_archive_pages_perceptual"
SOURCE_DRIFT_ERROR_PREFIX = (
    "Stored page inventory does not match the current archive"
)


class SourceDriftRecoveryError(RuntimeError):
    """Base error for a recovery that cannot safely proceed."""


class RecoveryPreconditionError(SourceDriftRecoveryError):
    """Raised when the job or live file no longer matches the report."""


@dataclass(frozen=True)
class FileSnapshot:
    size_bytes: int
    modified_time_ns: int


def _file_snapshot(path: Path) -> FileSnapshot:
    stat = path.stat()
    return FileSnapshot(
        size_bytes=int(stat.st_size),
        modified_time_ns=int(stat.st_mtime_ns),
    )


def _job_and_location(
    connection: sqlite3.Connection,
    job_id: int,
) -> dict:
    row = connection.execute(
        """
        SELECT
            j.id AS job_id,
            j.job_type,
            j.status,
            j.archive_id,
            j.attempts,
            j.max_attempts,
            j.available_at,
            j.error_message,
            j.failure_category,
            j.updated_at AS job_updated_at,
            fl.id AS location_id,
            fl.path AS current_path,
            fl.file_size AS recorded_file_size,
            fl.modified_time_ns AS recorded_modified_time_ns,
            acs.source_file_size,
            acs.source_modified_time_ns,
            acs.page_count AS signature_page_count
        FROM jobs AS j
        LEFT JOIN file_locations AS fl
          ON fl.archive_id = j.archive_id
         AND fl.is_current = 1
        LEFT JOIN archive_content_signatures AS acs
          ON acs.archive_id = j.archive_id
        WHERE j.id = ?
        """,
        (job_id,),
    ).fetchone()

    if row is None:
        raise RecoveryPreconditionError(f"Job does not exist: {job_id}")

    return dict(row)


def _active_conflicts(
    connection: sqlite3.Connection,
    *,
    archive_id: int,
    target_job_id: int,
) -> list[dict]:
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT id AS job_id, job_type, status
            FROM jobs
            WHERE archive_id = ?
              AND id != ?
              AND status IN ('pending', 'claimed', 'running')
            ORDER BY id
            """,
            (archive_id, target_job_id),
        )
    ]


def _stored_inventory(
    connection: sqlite3.Connection,
    archive_id: int,
) -> list[dict]:
    return [
        {
            "page_index": int(row["page_index"]),
            "entry_name": str(row["entry_name"]),
        }
        for row in connection.execute(
            """
            SELECT page_index, entry_name
            FROM archive_pages
            WHERE archive_id = ?
            ORDER BY page_index
            """,
            (archive_id,),
        )
    ]


def _live_inventory(path: Path) -> tuple[FileSnapshot, list[dict]]:
    before = _file_snapshot(path)

    with zipfile.ZipFile(path, mode="r") as archive:
        entries = sorted(
            (
                entry
                for entry in archive.infolist()
                if not entry.is_dir()
                and Path(entry.filename).suffix.casefold()
                in IMAGE_EXTENSIONS
            ),
            key=lambda entry: _natural_key(entry.filename),
        )

        inventory = [
            {
                "page_index": page_index,
                "entry_name": entry.filename,
            }
            for page_index, entry in enumerate(entries)
        ]

    after = _file_snapshot(path)

    if before != after:
        raise RecoveryPreconditionError(
            f"Archive changed while inventorying: {path}"
        )

    return after, inventory


def _inventory_differences(
    stored: list[dict],
    live: list[dict],
    *,
    limit: int = 20,
) -> list[dict]:
    differences: list[dict] = []

    for index in range(max(len(stored), len(live))):
        stored_page = stored[index] if index < len(stored) else None
        live_page = live[index] if index < len(live) else None

        if stored_page == live_page:
            continue

        differences.append(
            {
                "index": index,
                "stored": stored_page,
                "live": live_page,
            }
        )

        if len(differences) >= limit:
            break

    return differences


def analyze_source_drift(
    *,
    database: Path,
    job_id: int,
    json_output: Path | None = None,
) -> dict:
    """Analyze one pending perceptual job without changing SQLite."""
    database = Path(database).resolve(strict=False)

    if not database.is_file():
        raise FileNotFoundError(f"Database does not exist: {database}")

    started = time.perf_counter()
    fingerprint_before = fingerprint_database(database)

    with readonly_database_connection(database) as connection:
        candidate = _job_and_location(connection, job_id)
        archive_id = candidate["archive_id"]

        if archive_id is None:
            raise RecoveryPreconditionError(
                f"Job {job_id} has no archive_id."
            )

        stored_inventory = _stored_inventory(
            connection,
            int(archive_id),
        )
        conflicts = _active_conflicts(
            connection,
            archive_id=int(archive_id),
            target_job_id=job_id,
        )

    current_path = candidate["current_path"]

    if current_path is None:
        live_snapshot = None
        live_inventory: list[dict] = []
        live_error = "Archive has no current file location."
    else:
        path = Path(str(current_path))

        try:
            live_snapshot, live_inventory = _live_inventory(path)
            live_error = None
        except (OSError, zipfile.BadZipFile) as exc:
            live_snapshot = None
            live_inventory = []
            live_error = f"{type(exc).__name__}: {exc}"

    fingerprint_after = fingerprint_database(database)

    if fingerprint_after != fingerprint_before:
        raise DatabaseMutatedError(
            "Database changed during source-drift analysis: "
            f"before={fingerprint_before} after={fingerprint_after}."
        )

    error_message = str(candidate["error_message"] or "")
    correct_job = candidate["job_type"] == JOB_TYPE
    pending = candidate["status"] == "pending"
    expected_error = error_message.startswith(
        SOURCE_DRIFT_ERROR_PREFIX
    )
    attempts_remain = (
        int(candidate["attempts"]) < int(candidate["max_attempts"])
    )

    if live_snapshot is None:
        metadata_drift = False
        inventory_matches = False
    else:
        metadata_drift = (
            candidate["recorded_file_size"]
            != live_snapshot.size_bytes
            or candidate["recorded_modified_time_ns"]
            != live_snapshot.modified_time_ns
        )
        inventory_matches = stored_inventory == live_inventory

    recoverable = all(
        (
            correct_job,
            pending,
            expected_error,
            attempts_remain,
            live_snapshot is not None,
            not conflicts,
            metadata_drift or not inventory_matches,
        )
    )

    output = {
        "mode": "read_only_analysis",
        "database": str(database),
        "job": candidate,
        "stored_page_count": len(stored_inventory),
        "live_page_count": len(live_inventory),
        "live_file": (
            asdict(live_snapshot)
            if live_snapshot is not None
            else None
        ),
        "live_error": live_error,
        "metadata_drift": metadata_drift,
        "inventory_matches": inventory_matches,
        "inventory_differences": _inventory_differences(
            stored_inventory,
            live_inventory,
        ),
        "conflicting_active_jobs": conflicts,
        "checks": {
            "correct_job_type": correct_job,
            "job_is_pending": pending,
            "source_drift_error_present": expected_error,
            "attempts_remain": attempts_remain,
            "current_file_readable": live_snapshot is not None,
            "no_conflicting_active_jobs": not conflicts,
            "drift_detected": metadata_drift or not inventory_matches,
        },
        "recoverable": recoverable,
        "database_size_bytes": fingerprint_before.size_bytes,
        "database_modified_time_ns": (
            fingerprint_before.modified_time_ns
        ),
        "elapsed_seconds": round(time.perf_counter() - started, 6),
    }

    if json_output is not None:
        output["json_output"] = str(_write_json(json_output, output))

    return output


def _inspect_stable_archive(path: Path):
    before = _file_snapshot(path)
    result = inspect_archive(path)
    after = _file_snapshot(path)

    if before != after:
        raise RecoveryPreconditionError(
            f"Archive changed while inspecting: {path}"
        )

    return result, after


def _assert_expected_live_file(
    *,
    actual: FileSnapshot,
    expected_file_size: int,
    expected_modified_time_ns: int,
) -> None:
    expected = FileSnapshot(
        size_bytes=expected_file_size,
        modified_time_ns=expected_modified_time_ns,
    )

    if actual != expected:
        raise RecoveryPreconditionError(
            "Live archive no longer matches the reviewed analysis: "
            f"expected={expected} actual={actual}."
        )


def apply_source_drift_recovery(
    *,
    database: Path,
    job_id: int,
    expected_file_size: int,
    expected_modified_time_ns: int,
    json_output: Path | None = None,
) -> dict:
    """Atomically refresh exact evidence and release one pending job."""
    if expected_file_size < 0:
        raise ValueError("expected_file_size cannot be negative.")
    if expected_modified_time_ns < 0:
        raise ValueError(
            "expected_modified_time_ns cannot be negative."
        )

    started = time.perf_counter()
    analysis = analyze_source_drift(database=database, job_id=job_id)

    if not analysis["recoverable"]:
        raise RecoveryPreconditionError(
            f"Job {job_id} did not pass the read-only recovery gates."
        )

    path = Path(str(analysis["job"]["current_path"]))
    reviewed_snapshot = FileSnapshot(**analysis["live_file"])
    _assert_expected_live_file(
        actual=reviewed_snapshot,
        expected_file_size=expected_file_size,
        expected_modified_time_ns=expected_modified_time_ns,
    )

    archive_hash = calculate_archive_hash(path)
    _assert_expected_live_file(
        actual=FileSnapshot(
            size_bytes=archive_hash.file_size,
            modified_time_ns=archive_hash.modified_time_ns,
        ),
        expected_file_size=expected_file_size,
        expected_modified_time_ns=expected_modified_time_ns,
    )

    inspection, inspection_snapshot = _inspect_stable_archive(path)
    _assert_expected_live_file(
        actual=inspection_snapshot,
        expected_file_size=expected_file_size,
        expected_modified_time_ns=expected_modified_time_ns,
    )

    page_hashes = calculate_page_hashes(path)
    _assert_expected_live_file(
        actual=FileSnapshot(
            size_bytes=page_hashes.source_file_size,
            modified_time_ns=page_hashes.source_modified_time_ns,
        ),
        expected_file_size=expected_file_size,
        expected_modified_time_ns=expected_modified_time_ns,
    )

    if inspection.page_count != page_hashes.page_count:
        raise RecoveryPreconditionError(
            "Inspection and exact page hashing disagree on page count: "
            f"inspection={inspection.page_count} "
            f"page_hashing={page_hashes.page_count}."
        )

    database = Path(database).resolve(strict=False)
    baseline = analysis["job"]
    archive_id = int(baseline["archive_id"])
    location_id = int(baseline["location_id"])

    with database_connection(database) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = _job_and_location(connection, job_id)
            conflicts = _active_conflicts(
                connection,
                archive_id=archive_id,
                target_job_id=job_id,
            )

            guarded_fields = (
                "job_type",
                "status",
                "archive_id",
                "attempts",
                "max_attempts",
                "error_message",
                "failure_category",
                "job_updated_at",
                "location_id",
                "current_path",
                "recorded_file_size",
                "recorded_modified_time_ns",
            )

            if any(
                current[field] != baseline[field]
                for field in guarded_fields
            ):
                raise RecoveryPreconditionError(
                    "Job or location state changed after the reviewed "
                    "analysis; recovery was not applied."
                )

            if conflicts:
                raise RecoveryPreconditionError(
                    "Another active job now targets this archive; "
                    "recovery was not applied."
                )

            latest_snapshot = _file_snapshot(path)
            _assert_expected_live_file(
                actual=latest_snapshot,
                expected_file_size=expected_file_size,
                expected_modified_time_ns=expected_modified_time_ns,
            )

            ArchiveHashRepository(connection).save(
                archive_id=archive_id,
                location_id=location_id,
                result=archive_hash,
                enqueue_reinspection=False,
            )
            ArchiveInspectionRepository(connection).save(
                archive_id=archive_id,
                location_id=location_id,
                result=inspection,
                file_size=inspection_snapshot.size_bytes,
                modified_time_ns=(
                    inspection_snapshot.modified_time_ns
                ),
            )
            ArchivePageHashRepository(connection).save(
                archive_id=archive_id,
                location_id=location_id,
                result=page_hashes,
            )

            connection.execute(
                """
                INSERT INTO file_events (
                    archive_id,
                    event_type,
                    source_path,
                    details_json
                )
                VALUES (?, 'source_drift_recovered', ?, ?)
                """,
                (
                    archive_id,
                    str(path),
                    json.dumps(
                        {
                            "job_id": job_id,
                            "previous_file_size": (
                                baseline["recorded_file_size"]
                            ),
                            "previous_modified_time_ns": (
                                baseline[
                                    "recorded_modified_time_ns"
                                ]
                            ),
                            "current_file_size": (
                                expected_file_size
                            ),
                            "current_modified_time_ns": (
                                expected_modified_time_ns
                            ),
                            "page_count": page_hashes.page_count,
                            "archive_sha256": archive_hash.digest,
                            "content_signature": (
                                page_hashes.content_digest
                            ),
                        },
                        sort_keys=True,
                    ),
                ),
            )

            cursor = connection.execute(
                """
                UPDATE jobs
                SET
                    available_at = CURRENT_TIMESTAMP,
                    error_message = NULL,
                    failure_category = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                  AND status = 'pending'
                  AND job_type = ?
                  AND archive_id = ?
                """,
                (job_id, JOB_TYPE, archive_id),
            )

            if cursor.rowcount != 1:
                raise RecoveryPreconditionError(
                    "Pending perceptual job changed during recovery."
                )

            # The archive is outside SQLite's transaction boundary.
            # Re-stat it after every database write but before COMMIT;
            # if it changed during the critical section, roll back all
            # refreshed evidence rather than committing a mixed state.
            _assert_expected_live_file(
                actual=_file_snapshot(path),
                expected_file_size=expected_file_size,
                expected_modified_time_ns=expected_modified_time_ns,
            )

            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

        post_job = _job_and_location(connection, job_id)
        post_pages = _stored_inventory(connection, archive_id)
        exact_hash_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM archive_pages AS ap
                JOIN page_hashes AS ph ON ph.page_id = ap.id
                WHERE ap.archive_id = ?
                  AND ph.algorithm = 'sha256'
                  AND ph.algorithm_version = '1'
                """,
                (archive_id,),
            ).fetchone()[0]
        )
        perceptual_hash_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM archive_pages AS ap
                JOIN page_hashes AS ph ON ph.page_id = ap.id
                WHERE ap.archive_id = ?
                  AND ph.algorithm IN ('dhash', 'phash')
                  AND ph.algorithm_version = '1'
                """,
                (archive_id,),
            ).fetchone()[0]
        )

    output = {
        "mode": "applied",
        "database": str(database),
        "job_id": job_id,
        "archive_id": archive_id,
        "path": str(path),
        "previous_file": {
            "size_bytes": baseline["recorded_file_size"],
            "modified_time_ns": (
                baseline["recorded_modified_time_ns"]
            ),
        },
        "current_file": asdict(reviewed_snapshot),
        "archive_sha256": archive_hash.digest,
        "content_signature": page_hashes.content_digest,
        "page_count": page_hashes.page_count,
        "exact_page_hash_count": exact_hash_count,
        "perceptual_hash_count": perceptual_hash_count,
        "job_status": post_job["status"],
        "job_attempts": post_job["attempts"],
        "job_error_message": post_job["error_message"],
        "job_failure_category": post_job["failure_category"],
        "ready_for_perceptual_retry": (
            post_job["status"] == "pending"
            and post_job["error_message"] is None
            and exact_hash_count == len(post_pages)
            and perceptual_hash_count == 0
        ),
        "elapsed_seconds": round(time.perf_counter() - started, 6),
    }

    if json_output is not None:
        output["json_output"] = str(_write_json(json_output, output))

    return output


def _write_json(path: Path, payload: object) -> Path:
    resolved = Path(path).resolve(strict=False)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return resolved


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze or apply guarded recovery for one pending "
            "perceptual-hashing job whose archive changed in place. "
            "Default mode is strictly read-only."
        )
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Refresh exact evidence atomically and release the pending "
            "job. Requires both expected live metadata arguments."
        ),
    )
    parser.add_argument("--expected-file-size", type=int)
    parser.add_argument("--expected-modified-time-ns", type=int)
    parser.add_argument("--json-output", type=Path)
    return parser


def print_summary(output: dict) -> None:
    if output["mode"] == "read_only_analysis":
        print("Perceptual source-drift analysis completed.")
        print(f"Job:                  {output['job']['job_id']}")
        print(f"Archive:              {output['job']['archive_id']}")
        print(f"Path:                 {output['job']['current_path']}")
        print(f"Metadata drift:       {output['metadata_drift']}")
        print(f"Inventory matches:    {output['inventory_matches']}")
        print(f"Recoverable:          {output['recoverable']}")

        if output["live_file"] is not None:
            print(
                "Expected apply guard:  "
                f"--expected-file-size "
                f"{output['live_file']['size_bytes']} "
                f"--expected-modified-time-ns "
                f"{output['live_file']['modified_time_ns']}"
            )
    else:
        print("Perceptual source-drift recovery applied.")
        print(f"Job:                  {output['job_id']}")
        print(f"Archive:              {output['archive_id']}")
        print(f"Pages refreshed:      {output['page_count']}")
        print(
            "Ready for retry:      "
            f"{output['ready_for_perceptual_retry']}"
        )

    if output.get("json_output"):
        print(f"JSON output:           {output['json_output']}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.apply:
            if (
                args.expected_file_size is None
                or args.expected_modified_time_ns is None
            ):
                raise ValueError(
                    "--apply requires --expected-file-size and "
                    "--expected-modified-time-ns."
                )

            output = apply_source_drift_recovery(
                database=args.database,
                job_id=args.job_id,
                expected_file_size=args.expected_file_size,
                expected_modified_time_ns=(
                    args.expected_modified_time_ns
                ),
                json_output=args.json_output,
            )
        else:
            output = analyze_source_drift(
                database=args.database,
                job_id=args.job_id,
                json_output=args.json_output,
            )
    except Exception as exc:
        print(f"Source-drift recovery failed: {exc}", file=sys.stderr)
        return 1

    print_summary(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
