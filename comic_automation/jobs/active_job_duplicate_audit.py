"""Strictly read-only preflight for the active-job uniqueness migration.

Migration 010 adds a partial unique index equivalent to:

    CREATE UNIQUE INDEX idx_jobs_unique_active
        ON jobs(job_type, archive_id)
        WHERE status IN ('pending', 'claimed', 'running');

SQLite refuses to build a UNIQUE index over data that already violates
it, so that migration cannot be applied to a database that already
contains two or more *active* jobs sharing the same non-null
(job_type, archive_id). This module finds those blocking groups before
the migration is attempted, so the decision about what to do with them
stays a human one.

This audit never writes. It opens the database with SQLite's `mode=ro`
URI flag plus `PRAGMA query_only = ON`, applies no migrations, and
reads inside a single deferred transaction so every count in the report
comes from one consistent snapshot. It never deletes, cancels, merges,
or rewrites any row, and deliberately reports no payloads, filesystem
paths, error messages, or failure categories.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence


# The statuses migration 010's partial index predicate covers. Rows in
# any other status ('completed', 'failed', 'cancelled', 'blocked') fall
# outside the index and can never block it, however many of them share
# an identity.
ACTIVE_STATUSES = ("pending", "claimed", "running")

UNIQUE_ACTIVE_INDEX_NAME = "idx_jobs_unique_active"

NULL_ARCHIVE_LIMITATION = (
    "SQL treats every NULL as distinct for uniqueness purposes, so the "
    "partial unique index cannot constrain active jobs whose "
    "archive_id IS NULL. Any such rows are reported here for "
    "visibility only -- they never block the migration, and the "
    "migration will not deduplicate them."
)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_BLOCKING_DUPLICATES = 2


class PreflightError(RuntimeError):
    """Base class for conditions that invalidate a preflight run."""


class DatabaseChangedError(PreflightError):
    """Another connection committed while the audit was reading.

    The report would then mix pre- and post-change observations, so the
    run is rejected rather than reported as trustworthy.
    """


class DatabaseIntegrityError(PreflightError):
    """`PRAGMA quick_check` did not return 'ok'."""


@dataclass(frozen=True)
class DatabaseFingerprint:
    size_bytes: int
    modified_time_ns: int


def fingerprint_database(database_path: str | Path) -> DatabaseFingerprint:
    stat = Path(database_path).stat()
    return DatabaseFingerprint(
        size_bytes=stat.st_size,
        modified_time_ns=stat.st_mtime_ns,
    )


@contextmanager
def readonly_database_connection(
    database_path: str | Path,
) -> Iterator[sqlite3.Connection]:
    """Open `database_path` strictly read-only.

    Two independent safeguards, deliberately layered:

    - The `mode=ro` URI flag opens the connection read-only at the
      VFS level and refuses to create the file if it does not already
      exist (unlike a plain `sqlite3.connect`, which would silently
      create an empty database).
    - `PRAGMA query_only = ON` rejects any statement that would modify
      the database at the statement level, in case a future edit to
      this module accidentally introduces a write.

    No migrations are applied and no schema is created: the database is
    read exactly as found.
    """
    path = Path(database_path)

    # Checked before touching SQLite at all, so a missing path can
    # never result in a created file or directory.
    if not path.is_file():
        raise FileNotFoundError(f"Database does not exist: {path}")

    resolved = path.resolve(strict=True)
    uri = f"{resolved.as_uri()}?mode=ro"
    connection = sqlite3.connect(
        uri,
        uri=True,
        timeout=30.0,
        # Transaction boundaries are managed explicitly below so the
        # report reads from one deferred snapshot.
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row

    try:
        connection.execute("PRAGMA query_only = ON")
        yield connection
    finally:
        connection.close()


def _data_version(connection: sqlite3.Connection) -> int:
    """SQLite's counter of commits made by *other* connections.

    A change between the reading before and after the snapshot means
    someone else wrote to the database mid-audit.
    """
    return int(connection.execute("PRAGMA data_version").fetchone()[0])


def quick_check(connection: sqlite3.Connection) -> str:
    rows = connection.execute("PRAGMA quick_check").fetchall()
    return "\n".join(str(row[0]) for row in rows)


def applied_schema_versions(
    connection: sqlite3.Connection,
) -> list[int]:
    table = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name = 'schema_migrations'
        """
    ).fetchone()

    if table is None:
        return []

    return [
        int(row["version"])
        for row in connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    ]


def unique_active_index_exists(
    connection: sqlite3.Connection,
) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'index' AND name = ?
        """,
        (UNIQUE_ACTIVE_INDEX_NAME,),
    ).fetchone()

    return row is not None


def collect_blocking_groups(
    connection: sqlite3.Connection,
) -> list[dict]:
    """Every (job_type, archive_id) with more than one active row.

    Only non-null archive_ids can block: see NULL_ARCHIVE_LIMITATION.
    Ordering is fully deterministic (job_type, then archive_id), as is
    the job-id list within each group, so two runs over unchanged data
    produce byte-identical JSON.
    """
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)

    rows = connection.execute(
        f"""
        SELECT job_type, archive_id, id, status
        FROM jobs
        WHERE status IN ({placeholders})
          AND archive_id IS NOT NULL
          AND (job_type, archive_id) IN (
              SELECT job_type, archive_id
              FROM jobs
              WHERE status IN ({placeholders})
                AND archive_id IS NOT NULL
              GROUP BY job_type, archive_id
              HAVING COUNT(*) > 1
          )
        ORDER BY job_type, archive_id, id
        """,
        (*ACTIVE_STATUSES, *ACTIVE_STATUSES),
    ).fetchall()

    grouped: dict[tuple[str, int], list[sqlite3.Row]] = {}

    for row in rows:
        key = (str(row["job_type"]), int(row["archive_id"]))
        grouped.setdefault(key, []).append(row)

    groups: list[dict] = []

    for (job_type, archive_id), members in grouped.items():
        status_counts = Counter(
            str(member["status"]) for member in members
        )
        groups.append(
            {
                "job_type": job_type,
                "archive_id": archive_id,
                "active_count": len(members),
                "status_counts": dict(sorted(status_counts.items())),
                "job_ids": [int(member["id"]) for member in members],
            }
        )

    return groups


def collect_null_archive_active_jobs(
    connection: sqlite3.Connection,
) -> dict:
    """Active jobs with archive_id IS NULL, grouped by job type.

    Reported for visibility only. These can never block migration 010
    (see NULL_ARCHIVE_LIMITATION) but they are also not protected by it
    afterwards, which is worth seeing before the migration lands.
    """
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)

    rows = connection.execute(
        f"""
        SELECT job_type, status, COUNT(*) AS row_count
        FROM jobs
        WHERE status IN ({placeholders})
          AND archive_id IS NULL
        GROUP BY job_type, status
        ORDER BY job_type, status
        """,
        ACTIVE_STATUSES,
    ).fetchall()

    by_job_type: dict[str, int] = {}
    by_status: dict[str, int] = {}
    total = 0

    for row in rows:
        count = int(row["row_count"])
        total += count
        by_job_type[str(row["job_type"])] = (
            by_job_type.get(str(row["job_type"]), 0) + count
        )
        by_status[str(row["status"])] = (
            by_status.get(str(row["status"]), 0) + count
        )

    return {
        "total": total,
        "by_job_type": dict(sorted(by_job_type.items())),
        "by_status": dict(sorted(by_status.items())),
        "blocking": False,
        "limitation": NULL_ARCHIVE_LIMITATION,
    }


def _counts_by(
    connection: sqlite3.Connection,
    column: str,
) -> dict[str, int]:
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)

    rows = connection.execute(
        f"""
        SELECT {column} AS key, COUNT(*) AS row_count
        FROM jobs
        WHERE status IN ({placeholders})
        GROUP BY {column}
        ORDER BY {column}
        """,
        ACTIVE_STATUSES,
    ).fetchall()

    return {str(row["key"]): int(row["row_count"]) for row in rows}


def run_preflight(*, database: Path) -> dict:
    """Produce the read-only duplicate-active preflight report.

    Raises `FileNotFoundError` if the database does not exist,
    `DatabaseIntegrityError` if `PRAGMA quick_check` fails, and
    `DatabaseChangedError` if another connection committed during the
    run or the file's size/mtime changed.
    """
    path = Path(database)

    if not path.is_file():
        raise FileNotFoundError(f"Database does not exist: {path}")

    resolved = path.resolve(strict=True)
    fingerprint_before = fingerprint_database(resolved)

    with readonly_database_connection(resolved) as connection:
        # data_version is sampled *outside* and around the whole
        # transaction, so the change-detection window covers every read
        # the report depends on -- including quick_check. Sampling it
        # after quick_check would leave that read outside the window,
        # and a WAL commit landing there would go undetected: a WAL
        # write can touch only the -wal file, leaving the main
        # database's size and mtime identical, so the fingerprint
        # comparison below cannot be relied on to catch it either.
        data_version_before = _data_version(connection)

        # One deferred read transaction: every observation below comes
        # from the same snapshot, so totals cannot disagree with each
        # other because a writer landed between two queries.
        connection.execute("BEGIN")

        try:
            integrity = quick_check(connection)

            if integrity != "ok":
                raise DatabaseIntegrityError(
                    "PRAGMA quick_check failed for "
                    f"{resolved}: {integrity}"
                )

            schema_versions = applied_schema_versions(connection)
            index_exists = unique_active_index_exists(connection)
            blocking_groups = collect_blocking_groups(connection)
            null_archive = collect_null_archive_active_jobs(connection)
            active_by_status = _counts_by(connection, "status")
            active_by_job_type = _counts_by(connection, "job_type")
        finally:
            # A read transaction still has to be ended; END is not a
            # write and is permitted under query_only.
            connection.execute("END")

        data_version_after = _data_version(connection)

    fingerprint_after = fingerprint_database(resolved)

    if data_version_before != data_version_after:
        raise DatabaseChangedError(
            "Another connection committed to the database during the "
            f"preflight (data_version {data_version_before} -> "
            f"{data_version_after}); the report is not trustworthy."
        )

    if fingerprint_before != fingerprint_after:
        raise DatabaseChangedError(
            "Database file changed during the preflight: "
            f"before={fingerprint_before} after={fingerprint_after}."
        )

    blocking_row_total = sum(
        group["active_count"] for group in blocking_groups
    )
    total_active = sum(active_by_status.values())

    return {
        "database": str(resolved),
        "audited_statuses": list(ACTIVE_STATUSES),
        "quick_check": integrity,
        "applied_schema_versions": schema_versions,
        "unique_active_index_exists": index_exists,
        "unique_active_index_name": UNIQUE_ACTIVE_INDEX_NAME,
        "total_active_jobs": total_active,
        "active_by_status": active_by_status,
        "active_by_job_type": active_by_job_type,
        "blocking_group_count": len(blocking_groups),
        "blocking_row_count": blocking_row_total,
        "blocking_groups": blocking_groups,
        "null_archive_active_jobs": null_archive,
        "migration_blocked": bool(blocking_groups),
        "database_size_bytes_before": fingerprint_before.size_bytes,
        "database_size_bytes_after": fingerprint_after.size_bytes,
        "database_modified_time_ns_before": (
            fingerprint_before.modified_time_ns
        ),
        "database_modified_time_ns_after": (
            fingerprint_after.modified_time_ns
        ),
        "database_unchanged": fingerprint_after == fingerprint_before,
        "data_version_before": data_version_before,
        "data_version_after": data_version_after,
    }


def render_json(report: dict) -> str:
    return json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Strictly read-only preflight for migration 010's active-job "
            "unique index. Reports (job_type, archive_id) identities "
            "that already have more than one active job and would "
            "therefore block the migration. Never modifies, deletes, "
            "cancels, or merges any row."
        )
    )
    parser.add_argument(
        "--database",
        type=Path,
        required=True,
        help=(
            "SQLite database to audit, opened read-only "
            "(mode=ro + PRAGMA query_only)."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        report = run_preflight(database=args.database)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "error": type(exc).__name__,
                    "message": str(exc),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return EXIT_FAILURE

    print(render_json(report))

    if report["migration_blocked"]:
        return EXIT_BLOCKING_DUPLICATES

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
