"""Guarded, verified SQLite backup for the production database.

This packages the manual "protected schema-10 batch backup" procedure
described in `docs/production_handoff_2026-07-30.md` into a reusable,
tested command:

- the backup destination must not already exist (no silent overwrite);
- the copy is made with SQLite's online backup API
  (`sqlite3.Connection.backup()`), not a raw file copy, so it is
  transactionally consistent even if a writer were active;
- the source is opened strictly read-only (`mode=ro` +
  `PRAGMA query_only`, the same pattern as
  `comic_automation/jobs/active_job_duplicate_audit.py` and
  `comic_automation/jobs/abandoned_job_audit.py`) for every read this
  tool performs against it, and its `PRAGMA data_version` plus file
  fingerprint (size + mtime_ns) are sampled before and after the whole
  backup operation -- any change means something wrote to the source
  mid-backup, and the backup cannot be trusted as a clean snapshot;
- `PRAGMA quick_check` is run against the source both before and after
  the backup, and against the freshly written backup file;
- applied `schema_migrations` versions, the full set of index names
  (including the exact predicate of the unique active-job index), and
  row counts for the core tables are compared between source and
  backup;
- every check is recorded, with an overall `verified` flag, in a
  durable JSON report -- and the report path (like the backup path) is
  validated against the source database before anything is written,
  so it can never clobber either database.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence


# The core tables this tool compares row counts for by default. Names
# come directly from `comic_automation/database/migrations/*.sql`:
#   001_operational_foundation.sql: application_settings,
#       processing_runs, processing_stages, processing_items,
#       source_batches, archive_files, file_locations, file_events, jobs
#   002_discovery_checkpoints.sql: discovery_checkpoints
#   003_archive_inspections.sql: archive_inspections
#   006_archive_hashes.sql: archive_hashes
#   007_archive_page_hashes.sql: archive_pages, page_hashes,
#       archive_content_signatures
#   008_near_duplicate_candidates.sql: near_duplicate_candidates
#   009_archive_quarantine.sql: archive_quarantine
DEFAULT_TABLES: tuple[str, ...] = (
    "application_settings",
    "processing_runs",
    "processing_stages",
    "processing_items",
    "source_batches",
    "archive_files",
    "file_locations",
    "file_events",
    "jobs",
    "discovery_checkpoints",
    "archive_inspections",
    "archive_hashes",
    "archive_pages",
    "page_hashes",
    "archive_content_signatures",
    "near_duplicate_candidates",
    "archive_quarantine",
)

# Migration 010's partial unique index -- see
# comic_automation/database/migrations/010_unique_active_jobs.sql:
#
#   CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_unique_active
#       ON jobs(job_type, archive_id)
#       WHERE status IN ('pending', 'claimed', 'running');
#
# The handoff doc requires confirming this index "exists on both
# copies with the exact production predicate", so its `sqlite_master`
# SQL text (not just its name) is compared between source and backup.
UNIQUE_ACTIVE_INDEX_NAME = "idx_jobs_unique_active"

_VALID_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class BackupError(RuntimeError):
    """Base class for conditions that invalidate a backup run."""


class DatabaseMutatedError(BackupError):
    """The source database changed while the backup was being made.

    Detected via `PRAGMA data_version` and the file's size/mtime
    fingerprint, sampled before and after the entire backup operation.
    A change means another connection wrote to the source mid-backup,
    so the backup cannot be trusted as a clean snapshot.
    """


class DatabaseIntegrityError(BackupError):
    """`PRAGMA quick_check` did not return 'ok' for a database."""


class SchemaMismatchError(BackupError):
    """The backup failed one or more schema/index/count comparisons."""


class OutputPathCollisionError(ValueError):
    """Raised when a requested output path could clobber input data."""


class BackupDestinationExistsError(FileExistsError):
    """Raised when the requested backup path already exists.

    This tool never overwrites an existing file -- a caller that wants
    to replace a previous backup must remove or rename it first.
    """


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

    Same two-layer guard used throughout this codebase's audits: the
    `mode=ro` URI flag opens the connection read-only at the VFS level
    (and refuses to create the file if it doesn't exist), and
    `PRAGMA query_only = ON` rejects any statement that would modify
    the database at the statement level.
    """
    path = Path(database_path)

    if not path.is_file():
        raise FileNotFoundError(f"Database does not exist: {path}")

    resolved = path.resolve(strict=True)
    uri = f"{resolved.as_uri()}?mode=ro"
    connection = sqlite3.connect(
        uri,
        uri=True,
        timeout=30.0,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row

    try:
        connection.execute("PRAGMA query_only = ON")
        yield connection
    finally:
        connection.close()


def _data_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA data_version").fetchone()[0])


def quick_check(connection: sqlite3.Connection) -> str:
    # A sufficiently corrupted database can make PRAGMA quick_check
    # itself raise (rather than return a non-"ok" row); either outcome
    # must be treated as an integrity failure, not an unhandled crash.
    try:
        rows = connection.execute("PRAGMA quick_check").fetchall()
    except sqlite3.DatabaseError as exc:
        return f"error: {exc}"
    return "\n".join(str(row[0]) for row in rows)


def _perform_backup(
    source_connection: sqlite3.Connection,
    destination_connection: sqlite3.Connection,
) -> None:
    """Thin wrapper around SQLite's online backup API.

    Kept as a module-level seam (rather than calling
    `source_connection.backup(...)` inline) because `sqlite3.Connection`
    is a C type and cannot be monkeypatched directly -- tests that need
    to simulate a corrupted backup patch this function instead.
    """
    source_connection.backup(destination_connection)


def applied_schema_versions(connection: sqlite3.Connection) -> list[int]:
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


def index_definitions(connection: sqlite3.Connection) -> dict[str, str | None]:
    """Every index name and its `sqlite_master` SQL text.

    SQLite's implicit indexes backing UNIQUE/PRIMARY KEY constraints
    (named `sqlite_autoindex_*`) have a NULL `sql` column; they are
    included here by name so index *presence* still compares correctly
    between source and backup, even though their definition text is
    not directly inspectable.
    """
    rows = connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'index' ORDER BY name"
    ).fetchall()
    return {str(row["name"]): row["sql"] for row in rows}


def index_sql(connection: sqlite3.Connection, name: str) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
        (name,),
    ).fetchone()
    return row["sql"] if row is not None else None


def table_counts(
    connection: sqlite3.Connection,
    tables: Sequence[str],
) -> dict[str, int]:
    counts: dict[str, int] = {}

    for table in tables:
        if not _VALID_IDENTIFIER.match(table):
            raise ValueError(f"Not a valid table name: {table!r}")

        row = connection.execute(
            f"SELECT COUNT(*) AS row_count FROM {table}"
        ).fetchone()
        counts[table] = int(row["row_count"])

    return counts


def _same_file(first: Path, second: Path) -> bool:
    if first.resolve(strict=False) == second.resolve(strict=False):
        return True

    if first.exists() and second.exists():
        try:
            return first.samefile(second)
        except OSError:
            return False

    return False


def validate_backup_paths(
    database: Path,
    backup: Path,
    *,
    json_output: Path | None,
) -> None:
    """Reject any destination that could clobber input data or itself.

    Must be called before the source database is opened, before the
    backup file is created, and before any output directory is made --
    a rejected run must leave the filesystem exactly as it found it.
    """
    if _same_file(backup, database):
        raise OutputPathCollisionError(
            f"--backup ({backup}) must not be the same file as "
            f"--database ({database})."
        )

    if backup.exists():
        raise BackupDestinationExistsError(
            f"--backup ({backup}) already exists; refusing to overwrite "
            "an existing backup."
        )

    if json_output is not None:
        if _same_file(json_output, database):
            raise OutputPathCollisionError(
                f"--json-output ({json_output}) must not be the same "
                f"file as --database ({database})."
            )

        if _same_file(json_output, backup):
            raise OutputPathCollisionError(
                f"--json-output ({json_output}) must not be the same "
                f"file as --backup ({backup})."
            )


def _write_json(path: Path, payload: object) -> Path:
    resolved = path.resolve(strict=False)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return resolved


def run_backup(
    *,
    database: Path,
    backup: Path,
    json_output: Path | None = None,
    tables: Sequence[str] = DEFAULT_TABLES,
    unique_active_index_name: str = UNIQUE_ACTIVE_INDEX_NAME,
) -> dict:
    """Create and verify a guarded online backup of `database`.

    Returns the verification report on full success. Raises (and, if
    `json_output` was given, still writes a best-effort report
    recording every check attempted so far with `verified: False`)
    on any failure:

    - `FileNotFoundError` if the source database does not exist;
    - `OutputPathCollisionError` / `BackupDestinationExistsError` if
      the destination paths are unsafe (raised before anything is
      written -- no partial report in this case);
    - `DatabaseIntegrityError` if `PRAGMA quick_check` fails for the
      source (before or after the backup) or for the backup itself;
    - `DatabaseMutatedError` if the source's `PRAGMA data_version` or
      file fingerprint changed during the backup;
    - `SchemaMismatchError` if the backup's schema, indexes, or table
      counts do not match the source.
    """
    database = Path(database).resolve(strict=False)

    if not database.is_file():
        raise FileNotFoundError(f"Database does not exist: {database}")

    backup = Path(backup).resolve(strict=False)
    tables = list(tables)

    # Checked before anything is opened or created: a rejected run
    # must leave the filesystem untouched.
    validate_backup_paths(database, backup, json_output=json_output)

    checks: dict[str, bool] = {}
    report: dict = {
        "source_database": str(database),
        "backup_database": str(backup),
        "tables_compared": tables,
        "unique_active_index_name": unique_active_index_name,
    }

    try:
        fingerprint_before = fingerprint_database(database)
        report["source_size_bytes_before"] = fingerprint_before.size_bytes
        report["source_modified_time_ns_before"] = (
            fingerprint_before.modified_time_ns
        )

        with readonly_database_connection(database) as connection:
            data_version_before = _data_version(connection)
            report["data_version_before"] = data_version_before

            connection.execute("BEGIN")
            try:
                source_quick_check_before = quick_check(connection)

                if source_quick_check_before == "ok":
                    source_schema_versions = applied_schema_versions(
                        connection
                    )
                    source_indexes = index_definitions(connection)
                    source_counts = table_counts(connection, tables)
                    source_unique_index_sql = index_sql(
                        connection, unique_active_index_name
                    )
                else:
                    source_schema_versions = []
                    source_indexes = {}
                    source_counts = {}
                    source_unique_index_sql = None
            finally:
                try:
                    connection.execute("END")
                except sqlite3.DatabaseError:
                    # The read connection may already be unusable if
                    # the database is corrupt; the quick_check result
                    # captured above is what matters, not this cleanup.
                    pass

            report["source_quick_check_before"] = source_quick_check_before
            checks["source_quick_check_before_ok"] = (
                source_quick_check_before == "ok"
            )
            if not checks["source_quick_check_before_ok"]:
                raise DatabaseIntegrityError(
                    "Source failed PRAGMA quick_check before backup: "
                    f"{source_quick_check_before}"
                )

            # SQLite's online backup API: transactionally consistent
            # even against a database with concurrent writers, unlike
            # a raw file copy.
            backup.parent.mkdir(parents=True, exist_ok=True)
            destination_connection = sqlite3.connect(str(backup))
            try:
                _perform_backup(connection, destination_connection)
            finally:
                destination_connection.close()

            connection.execute("BEGIN")
            try:
                source_quick_check_after = quick_check(connection)
            finally:
                try:
                    connection.execute("END")
                except sqlite3.DatabaseError:
                    pass

            report["source_quick_check_after"] = source_quick_check_after
            checks["source_quick_check_after_ok"] = (
                source_quick_check_after == "ok"
            )
            if not checks["source_quick_check_after_ok"]:
                raise DatabaseIntegrityError(
                    "Source failed PRAGMA quick_check after backup: "
                    f"{source_quick_check_after}"
                )

            data_version_after = _data_version(connection)
            report["data_version_after"] = data_version_after

        fingerprint_after = fingerprint_database(database)
        report["source_size_bytes_after"] = fingerprint_after.size_bytes
        report["source_modified_time_ns_after"] = (
            fingerprint_after.modified_time_ns
        )

        checks["data_version_unchanged"] = (
            data_version_before == data_version_after
        )
        checks["source_fingerprint_unchanged"] = (
            fingerprint_before == fingerprint_after
        )

        if not checks["data_version_unchanged"] or not checks[
            "source_fingerprint_unchanged"
        ]:
            raise DatabaseMutatedError(
                "Source database changed during the backup: "
                f"data_version {data_version_before} -> "
                f"{data_version_after}, fingerprint {fingerprint_before} "
                f"-> {fingerprint_after}. The backup is not trustworthy "
                "as a clean snapshot."
            )

        if not backup.is_file():
            raise DatabaseIntegrityError(
                f"Backup file was not created: {backup}"
            )

        backup_fingerprint = fingerprint_database(backup)
        report["backup_size_bytes"] = backup_fingerprint.size_bytes
        report["backup_modified_time_ns"] = (
            backup_fingerprint.modified_time_ns
        )

        with readonly_database_connection(backup) as backup_connection:
            backup_quick_check = quick_check(backup_connection)
            report["backup_quick_check"] = backup_quick_check
            checks["backup_quick_check_ok"] = backup_quick_check == "ok"
            if not checks["backup_quick_check_ok"]:
                raise DatabaseIntegrityError(
                    f"Backup failed PRAGMA quick_check: {backup_quick_check}"
                )

            backup_schema_versions = applied_schema_versions(
                backup_connection
            )
            backup_indexes = index_definitions(backup_connection)
            backup_counts = table_counts(backup_connection, tables)
            backup_unique_index_sql = index_sql(
                backup_connection, unique_active_index_name
            )

        report["source_schema_versions"] = source_schema_versions
        report["backup_schema_versions"] = backup_schema_versions
        checks["schema_versions_match"] = (
            source_schema_versions == backup_schema_versions
        )

        report["source_index_names"] = sorted(source_indexes)
        report["backup_index_names"] = sorted(backup_indexes)
        checks["index_names_match"] = (
            set(source_indexes) == set(backup_indexes)
        )

        report["source_table_counts"] = source_counts
        report["backup_table_counts"] = backup_counts
        checks["table_counts_match"] = source_counts == backup_counts

        report["source_unique_active_index_sql"] = source_unique_index_sql
        report["backup_unique_active_index_sql"] = backup_unique_index_sql
        checks["unique_active_index_present_in_source"] = (
            source_unique_index_sql is not None
        )
        checks["unique_active_index_present_in_backup"] = (
            backup_unique_index_sql is not None
        )
        checks["unique_active_index_predicate_matches"] = (
            source_unique_index_sql is not None
            and source_unique_index_sql == backup_unique_index_sql
        )

        report["checks"] = checks
        verified = all(checks.values())
        report["verified"] = verified

        if json_output is not None:
            report["json_output"] = str(_write_json(json_output, report))

        if not verified:
            failed = sorted(
                name for name, ok in checks.items() if not ok
            )
            raise SchemaMismatchError(
                "Backup verification failed one or more checks: "
                f"{failed}"
            )

        return report
    except Exception:
        if json_output is not None and "json_output" not in report:
            report["checks"] = checks
            report["verified"] = False
            try:
                report["json_output"] = str(
                    _write_json(json_output, report)
                )
            except Exception:
                pass
        raise


def print_summary(report: dict) -> None:
    print("Database backup completed and verified.")
    print(f"Source:              {report['source_database']}")
    print(f"Backup:              {report['backup_database']}")
    print(f"Source quick_check:  {report['source_quick_check_before']} "
          f"(before) / {report['source_quick_check_after']} (after)")
    print(f"Backup quick_check:  {report['backup_quick_check']}")
    print(f"Schema versions:     {report['source_schema_versions']}")
    print(f"Verified:            {report['verified']}")

    if report.get("json_output"):
        print(f"JSON report:         {report['json_output']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Guarded, verified SQLite backup. Refuses to overwrite an "
            "existing destination, copies with SQLite's online backup "
            "API, and verifies quick_check, PRAGMA data_version and "
            "file fingerprint stability on the source, and schema/"
            "index/table-count agreement between source and backup."
        )
    )
    parser.add_argument(
        "--database",
        type=Path,
        required=True,
        help="Source SQLite database to back up.",
    )
    parser.add_argument(
        "--backup",
        type=Path,
        required=True,
        help=(
            "Destination path for the backup. Must not already exist."
        ),
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="Optional path for the JSON verification report.",
    )
    parser.add_argument(
        "--table",
        dest="tables",
        action="append",
        help=(
            "Table to include in the row-count comparison (repeatable). "
            "Defaults to this schema's core tables."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tables = args.tables if args.tables else list(DEFAULT_TABLES)

    try:
        report = run_backup(
            database=args.database,
            backup=args.backup,
            json_output=args.json_output,
            tables=tables,
        )
    except Exception as exc:
        print(
            json.dumps(
                {"error": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
