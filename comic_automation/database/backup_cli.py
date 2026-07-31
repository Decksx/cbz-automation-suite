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
- the schema being verified is *discovered from the source database
  itself*, never from a list maintained by hand: every user table in
  `sqlite_master` has its row count compared, and every row of
  `sqlite_master` (tables, indexes, views, triggers) has its `type`,
  `name`, `tbl_name` and full `sql` text compared verbatim. A table
  added by a future migration is therefore compared the day it
  appears, with no code change here -- the failure mode this replaced
  was a hard-coded table list silently going stale and letting a
  backup differ from its source in an uncompared table while still
  being reported as verified;
- applied `schema_migrations` versions are compared as well (the
  `schema_migrations` table is treated as an ordinary user table for
  row-count purposes, and additionally has its contents compared);
- every check is recorded, with an overall `verified` flag, in a
  durable JSON report -- and the report path (like the backup path)
  must not already exist and is validated against both databases
  before anything is written, so it can neither clobber a database
  nor destroy the evidence of an earlier verification run.
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


# Migration 010's partial unique index -- see
# comic_automation/database/migrations/010_unique_active_jobs.sql:
#
#   CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_unique_active
#       ON jobs(job_type, archive_id)
#       WHERE status IN ('pending', 'claimed', 'running');
#
# The handoff doc requires confirming this index "exists on both
# copies with the exact production predicate", so it gets its own
# named checks in the report for operator clarity.
#
# It is *not* how schema drift is caught: the verbatim whole-schema
# comparison (`schema_objects()`) already proves this index's SQL text
# -- predicate included -- is byte-identical on both copies, along
# with every other schema object. Naming this one index is a
# readability affordance for whoever reads the report, nothing more.
UNIQUE_ACTIVE_INDEX_NAME = "idx_jobs_unique_active"

# Caller-supplied `--table` overrides are held to plain-identifier
# syntax. Names discovered from `sqlite_master` are quoted instead
# (see `_quote_identifier`), because SQLite happily permits table
# names this pattern would reject and discovery must never refuse to
# compare a table that actually exists.
_VALID_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# `sqlite_master` rows SQLite maintains for itself: `sqlite_sequence`
# (AUTOINCREMENT bookkeeping), `sqlite_stat1` (ANALYZE output),
# `sqlite_autoindex_*` (implicit UNIQUE/PK indexes). They are excluded
# from *table discovery* -- they are not user data and some are not
# even selectable -- but deliberately left in the whole-schema
# comparison, where their presence and definitions still have to agree
# between source and backup.
_SQLITE_INTERNAL_PREFIX = "sqlite_"


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


class ReportDestinationExistsError(FileExistsError):
    """Raised when the requested `--json-output` path already exists.

    A verification report is the durable evidence that some earlier
    backup was a faithful copy; the handoff doc's read-only audit
    rules say to "always use new output paths and keep reports outside
    the repository" for exactly that reason. Silently overwriting one
    destroys that evidence, so a report path that already exists is
    refused up front -- alongside the backup-destination check, before
    any backup work is done -- rather than at write time, after the
    expensive part already ran.
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


def discover_user_tables(connection: sqlite3.Connection) -> list[str]:
    """Every user table in this database, read from `sqlite_master`.

    This is the tool's source of truth for *what to compare*. It is
    deliberately not a list kept in this file: a hand-maintained list
    goes stale the moment a migration adds a table, and a stale list
    means an uncompared table can differ between source and backup
    while the run still reports `verified: true`. For a tool whose
    only job is proving a backup is a faithful copy, that is the one
    hole that must not exist.

    `sqlite_`-prefixed names are SQLite's own bookkeeping (see
    `_SQLITE_INTERNAL_PREFIX`) and are excluded. `schema_migrations`
    is *not* special-cased: it is a user table like any other and its
    row count is compared like any other (its contents are compared
    too, by `applied_schema_versions`).
    """
    rows = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
        ORDER BY name
        """
    ).fetchall()

    return [
        str(row["name"])
        for row in rows
        if not str(row["name"]).startswith(_SQLITE_INTERNAL_PREFIX)
    ]


def schema_objects(
    connection: sqlite3.Connection,
) -> list[dict[str, str | None]]:
    """The complete `sqlite_master` contents, normalised for comparison.

    Every schema object -- tables, indexes, views, triggers, including
    SQLite's implicit `sqlite_autoindex_*` entries -- with its `type`,
    `name`, `tbl_name` and full `sql` text exactly as stored. Comparing
    this list verbatim between source and backup is what actually
    proves the two schemas are identical; it subsumes any targeted
    per-object check, because a differing partial-index predicate, a
    renamed trigger, a missing view or a dropped table all show up
    here as a difference in the list.

    Rows are ordered by `(type, name, tbl_name)` so the comparison does
    not depend on `sqlite_master`'s physical row order, which the
    online backup API is not obliged to preserve.
    """
    rows = connection.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_master
        ORDER BY type, name, tbl_name
        """
    ).fetchall()

    return [
        {
            "type": str(row["type"]),
            "name": str(row["name"]),
            "tbl_name": str(row["tbl_name"]),
            "sql": row["sql"],
        }
        for row in rows
    ]


def schema_object_differences(
    source: Sequence[dict[str, str | None]],
    backup: Sequence[dict[str, str | None]],
) -> dict[str, list]:
    """Explain *how* two `schema_objects()` listings differ.

    The boolean check is the gate; this is what an operator reads when
    the gate fails, so the report says which object drifted rather
    than only that something did.
    """
    source_by_key = {(item["type"], item["name"]): item for item in source}
    backup_by_key = {(item["type"], item["name"]): item for item in backup}

    only_in_source = sorted(set(source_by_key) - set(backup_by_key))
    only_in_backup = sorted(set(backup_by_key) - set(source_by_key))
    differing = [
        {
            "type": key[0],
            "name": key[1],
            "source": source_by_key[key],
            "backup": backup_by_key[key],
        }
        for key in sorted(set(source_by_key) & set(backup_by_key))
        if source_by_key[key] != backup_by_key[key]
    ]

    return {
        "only_in_source": [
            {"type": key[0], "name": key[1]} for key in only_in_source
        ],
        "only_in_backup": [
            {"type": key[0], "name": key[1]} for key in only_in_backup
        ],
        "differing": differing,
    }


def index_definitions(connection: sqlite3.Connection) -> dict[str, str | None]:
    """Every index name and its `sqlite_master` SQL text.

    SQLite's implicit indexes backing UNIQUE/PRIMARY KEY constraints
    (named `sqlite_autoindex_*`) have a NULL `sql` column; they are
    included here by name so index *presence* still compares correctly
    between source and backup, even though their definition text is
    not directly inspectable.

    Kept as a legible index inventory for the report and as a
    narrower, independently-failing check; the authoritative schema
    comparison is `schema_objects()`, which covers these rows too.
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


def _quote_identifier(name: str) -> str:
    """Quote a table name for interpolation into a COUNT(*) query.

    Table names come from `sqlite_master`, not from user input, but
    they are still interpolated as text (SQLite has no parameter slot
    for an identifier), so they are double-quoted with embedded quotes
    doubled -- the standard SQL escape. A NUL byte cannot be quoted at
    all and is refused outright.
    """
    if "\x00" in name:
        raise ValueError(f"Not a usable table name: {name!r}")

    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def table_counts(
    connection: sqlite3.Connection,
    tables: Sequence[str],
) -> dict[str, int]:
    counts: dict[str, int] = {}

    for table in tables:
        row = connection.execute(
            f"SELECT COUNT(*) AS row_count FROM {_quote_identifier(table)}"
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

        # Checked here, with the other destination checks, rather than
        # at write time: the report is written last, so a late check
        # would let a run do the whole backup and only then fail --
        # and a *failure*-path report would have overwritten the older
        # evidence before anyone noticed. Refusing up front means a
        # run that cannot record its result never starts.
        if json_output.exists():
            raise ReportDestinationExistsError(
                f"--json-output ({json_output}) already exists; refusing "
                "to overwrite a previous verification report."
            )


def _write_json(path: Path, payload: object) -> Path:
    resolved = path.resolve(strict=False)

    # Belt-and-braces against the up-front check in
    # `validate_backup_paths`: if something created this path between
    # validation and now, the report is still not clobbered.
    if resolved.exists():
        raise ReportDestinationExistsError(
            f"Refusing to overwrite an existing report: {resolved}"
        )

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
    tables: Sequence[str] | None = None,
    unique_active_index_name: str = UNIQUE_ACTIVE_INDEX_NAME,
) -> dict:
    """Create and verify a guarded online backup of `database`.

    `tables` is an optional override restricting the row-count
    comparison to the named tables. It exists for callers who
    knowingly want a narrower check; leaving it `None` (the default,
    and what the CLI does unless `--table` is passed) compares every
    user table discovered in the source. Whole-schema comparison and
    table-set comparison are unconditional -- the override narrows
    row counting only, never what counts as schema drift.

    Returns the verification report on full success. Raises (and, if
    `json_output` was given, still writes a best-effort report
    recording every check attempted so far with `verified: False`)
    on any failure:

    - `FileNotFoundError` if the source database does not exist;
    - `OutputPathCollisionError` / `BackupDestinationExistsError` /
      `ReportDestinationExistsError` if the destination paths are
      unsafe or already occupied (raised before anything is opened or
      written -- no partial report and no backup file in this case);
    - `ValueError` if a `tables` override names something that is not
      a discovered user table in the source;
    - `DatabaseIntegrityError` if `PRAGMA quick_check` fails for the
      source (before or after the backup) or for the backup itself;
    - `DatabaseMutatedError` if the source's `PRAGMA data_version` or
      file fingerprint changed during the backup;
    - `SchemaMismatchError` if the backup's discovered table set,
      schema objects, indexes, applied migrations or row counts do
      not match the source.
    """
    database = Path(database).resolve(strict=False)

    if not database.is_file():
        raise FileNotFoundError(f"Database does not exist: {database}")

    backup = Path(backup).resolve(strict=False)

    table_override: list[str] | None = None
    if tables is not None:
        table_override = list(tables)

        if not table_override:
            raise ValueError(
                "tables override is empty; pass None to compare every "
                "discovered user table."
            )

        for table in table_override:
            if not _VALID_IDENTIFIER.match(table):
                raise ValueError(f"Not a valid table name: {table!r}")

    # Checked before anything is opened or created: a rejected run
    # must leave the filesystem untouched. Note this call sits outside
    # the try/except below on purpose -- a run rejected here must not
    # write the very report file it just refused.
    validate_backup_paths(database, backup, json_output=json_output)

    checks: dict[str, bool] = {}
    report: dict = {
        "source_database": str(database),
        "backup_database": str(backup),
        "table_selection": (
            "explicit_override" if table_override is not None
            else "discovered_from_source"
        ),
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
                    # Discovery happens here, inside the same read
                    # transaction as the counts it selects, so the
                    # table list and the row counts describe one
                    # consistent view of the source.
                    source_tables = discover_user_tables(connection)
                    source_schema = schema_objects(connection)
                    source_schema_versions = applied_schema_versions(
                        connection
                    )
                    source_indexes = index_definitions(connection)

                    if table_override is not None:
                        unknown = sorted(
                            set(table_override) - set(source_tables)
                        )
                        if unknown:
                            raise ValueError(
                                "tables override names tables that do not "
                                f"exist in the source: {unknown}"
                            )
                        compared_tables = list(table_override)
                    else:
                        compared_tables = list(source_tables)

                    source_counts = table_counts(connection, compared_tables)
                    source_unique_index_sql = index_sql(
                        connection, unique_active_index_name
                    )
                else:
                    source_tables = []
                    source_schema = []
                    source_schema_versions = []
                    source_indexes = {}
                    compared_tables = []
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

            backup_tables = discover_user_tables(backup_connection)
            backup_schema = schema_objects(backup_connection)
            backup_schema_versions = applied_schema_versions(
                backup_connection
            )
            backup_indexes = index_definitions(backup_connection)
            # Counted only for tables the backup actually has: a table
            # missing from the backup must surface as a *mismatch* in
            # the report (both in `tables_match` and as a missing key
            # here), not as an OperationalError that aborts the run
            # before any of the comparisons are recorded.
            backup_present = set(backup_tables)
            backup_counts = table_counts(
                backup_connection,
                [table for table in compared_tables if table in backup_present],
            )
            backup_unique_index_sql = index_sql(
                backup_connection, unique_active_index_name
            )

        report["source_tables"] = source_tables
        report["backup_tables"] = backup_tables
        report["tables_compared"] = compared_tables

        # A source with no user tables at all means discovery found
        # nothing to compare -- "verified" would then be vacuous.
        checks["source_tables_discovered"] = bool(source_tables)
        checks["tables_match"] = source_tables == backup_tables

        report["source_schema_objects"] = source_schema
        report["backup_schema_objects"] = backup_schema
        checks["schema_objects_match"] = source_schema == backup_schema
        if not checks["schema_objects_match"]:
            report["schema_object_differences"] = schema_object_differences(
                source_schema, backup_schema
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

        # Redundant with `schema_objects_match` above by construction
        # -- kept because the handoff doc calls this index out by name,
        # so an operator reading the report should see it named rather
        # than have to grep the schema dump for it.
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
    print(f"Tables compared:     {len(report['tables_compared'])} "
          f"({report['table_selection']})")
    print(f"Schema objects:      {len(report['source_schema_objects'])} "
          "compared verbatim")
    print(f"Verified:            {report['verified']}")

    if report.get("json_output"):
        print(f"JSON report:         {report['json_output']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Guarded, verified SQLite backup. Refuses to overwrite an "
            "existing backup or verification report, copies with "
            "SQLite's online backup API, and verifies quick_check, "
            "PRAGMA data_version and file fingerprint stability on the "
            "source, plus verbatim whole-schema and row-count "
            "agreement between source and backup for every table "
            "discovered in the source."
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
        help=(
            "Optional path for the JSON verification report. Must not "
            "already exist -- an earlier report is evidence, not "
            "scratch space."
        ),
    )
    parser.add_argument(
        "--table",
        dest="tables",
        action="append",
        help=(
            "Restrict the row-count comparison to this table "
            "(repeatable). By default every user table found in the "
            "source is compared; this only ever narrows that. Schema "
            "comparison is unaffected."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # No --table means full discovery from the source, not a fallback
    # list: there is no fallback list any more.
    tables = args.tables if args.tables else None

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
