"""Read-only audit of terminal hash_archive_pages_perceptual failures.

This module never enqueues, retries, quarantines, or moves anything --
it only reads jobs.* / file_locations.* rows for terminally-failed
`hash_archive_pages_perceptual` jobs and reports them, grouped into a
small set of stable categories, as JSON and CSV.

It is deliberately independent from comic_automation/archive/cli.py's
`_failure_review` (the equivalent report for `inspect_archive` jobs):
that function reuses a live read/write connection already open for job
processing, whereas this module's whole purpose is to be safe to point
at a protected backup file, so it opens the database with SQLite's
`mode=ro` URI flag plus `PRAGMA query_only = ON` and never applies
migrations against it.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence


JOB_TYPE = "hash_archive_pages_perceptual"
TERMINAL_STATUS = "failed"

# The stable, human-facing buckets this audit reports failures under.
# These are intentionally coarser and more stable than the raw
# jobs.failure_category strings a handler happens to set today (see
# comic_automation/archive/perceptual_hashing.py), so the report keeps
# working even as handler code evolves new category names.
STABLE_CATEGORY_ORDER = (
    "corrupt_images",
    "corrupt_archives",
    "missing_files",
    "permissions",
    "unsupported_formats",
    "unclassified",
)

# Raw jobs.failure_category value -> stable report bucket. Anything
# not listed here (including categories introduced by future handler
# changes) falls back to "unclassified" rather than being dropped.
_RAW_CATEGORY_TO_STABLE = {
    "page_image_corrupt": "corrupt_images",
    "archive_corrupt": "corrupt_archives",
    "archive_unreadable": "corrupt_archives",
    "corrupt_archive": "corrupt_archives",  # legacy/inspect_archive naming
    "filesystem_not_found": "missing_files",
    "filesystem_permission": "permissions",
    "unsupported_archive_format": "unsupported_formats",
    "unsupported_image_format": "unsupported_formats",
    "filesystem_io": "unclassified",
    "legacy_unclassified": "unclassified",
}


def stable_category(raw_category: str | None) -> str:
    """Map a raw jobs.failure_category value to a stable report bucket.

    A NULL failure_category (jobs that failed before category-aware
    handling existed) is treated the same way migration 005 treats it
    for inspect_archive jobs: as "legacy_unclassified", which in turn
    maps to the "unclassified" bucket.
    """
    raw = raw_category if raw_category is not None else "legacy_unclassified"
    return _RAW_CATEGORY_TO_STABLE.get(raw, "unclassified")


class DatabaseMutatedError(RuntimeError):
    """Raised when a database changed size or mtime during an audit run.

    This audit is read-only by construction (mode=ro + query_only),
    but this check is defense in depth: if the underlying file was
    touched by *anything* (this process or another) while the audit
    ran, the run is treated as untrustworthy rather than silently
    reporting a possibly-inconsistent snapshot.
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

    Two independent safeguards, deliberately layered:

    - The `mode=ro` SQLite URI flag opens the connection itself
      read-only at the OS/VFS level and refuses to create the file if
      it doesn't already exist (unlike a plain sqlite3.connect, which
      would silently create an empty database).
    - `PRAGMA query_only = ON` rejects any statement that would modify
      the database *at the statement level*, in case a future edit to
      this module accidentally introduces a write.

    Neither PRAGMA foreign_keys nor PRAGMA journal_mode is set here:
    both would either be no-ops or require write access, and this
    connection never runs a statement that depends on either.
    """
    path = Path(database_path).resolve(strict=False)

    if not path.is_file():
        raise FileNotFoundError(f"Database does not exist: {path}")

    uri = f"{path.as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30.0)
    connection.row_factory = sqlite3.Row

    try:
        connection.execute("PRAGMA query_only = ON")
        yield connection
    finally:
        connection.close()


def collect_failures(connection: sqlite3.Connection) -> list[dict]:
    """Every terminal failure for hash_archive_pages_perceptual jobs.

    Mirrors comic_automation/archive/cli.py's `_failure_review` query
    for inspect_archive jobs, scoped to
    job_type = 'hash_archive_pages_perceptual'. LEFT JOIN
    file_locations because an archive's current (is_current = 1)
    location may no longer exist -- moved, renamed, or deleted since
    the job failed -- and the audit should still report the failure
    (with current_path = None) rather than silently dropping it.
    """
    rows = connection.execute(
        """
        SELECT
            j.id AS job_id,
            j.archive_id,
            fl.path AS current_path,
            j.failure_category,
            j.error_message,
            j.attempts,
            j.max_attempts,
            j.completed_at
        FROM jobs AS j
        LEFT JOIN file_locations AS fl
          ON fl.archive_id = j.archive_id
         AND fl.is_current = 1
        WHERE j.job_type = ?
          AND j.status = ?
        ORDER BY j.failure_category, fl.path, j.id
        """,
        (JOB_TYPE, TERMINAL_STATUS),
    ).fetchall()

    failures: list[dict] = []

    for row in rows:
        raw_category = row["failure_category"]
        failures.append(
            {
                "job_id": int(row["job_id"]),
                "archive_id": (
                    int(row["archive_id"])
                    if row["archive_id"] is not None
                    else None
                ),
                "current_path": row["current_path"],
                "failure_category": (
                    raw_category
                    if raw_category is not None
                    else "legacy_unclassified"
                ),
                "stable_category": stable_category(raw_category),
                "failure_message": row["error_message"],
                "attempts": (
                    int(row["attempts"])
                    if row["attempts"] is not None
                    else None
                ),
                "completed_at": row["completed_at"],
            }
        )

    return failures


def group_by_category(failures: list[dict]) -> dict[str, list[dict]]:
    """Group failures by stable_category, preserving STABLE_CATEGORY_ORDER."""
    grouped: dict[str, list[dict]] = {
        name: [] for name in STABLE_CATEGORY_ORDER
    }

    for failure in failures:
        grouped[failure["stable_category"]].append(failure)

    return grouped


def category_counts(failures: list[dict]) -> dict[str, int]:
    counts = Counter(failure["stable_category"] for failure in failures)
    return {name: counts.get(name, 0) for name in STABLE_CATEGORY_ORDER}


def _write_json(path: Path, payload: object) -> Path:
    resolved = path.resolve(strict=False)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return resolved


_CSV_FIELDNAMES = [
    "job_id",
    "archive_id",
    "current_path",
    "failure_category",
    "stable_category",
    "failure_message",
    "attempts",
    "completed_at",
]


def _write_csv(path: Path, failures: list[dict]) -> Path:
    resolved = path.resolve(strict=False)
    resolved.parent.mkdir(parents=True, exist_ok=True)

    with resolved.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=_CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(failures)

    return resolved


def run_audit(
    *,
    database: Path,
    json_output: Path | None = None,
    csv_output: Path | None = None,
) -> dict:
    database = Path(database).resolve(strict=False)

    if not database.is_file():
        raise FileNotFoundError(f"Database does not exist: {database}")

    started = time.perf_counter()
    fingerprint_before = fingerprint_database(database)

    with readonly_database_connection(database) as connection:
        failures = collect_failures(connection)

    # Re-stat *after* closing the connection: if opening read-only or
    # running the SELECT touched the file (it shouldn't -- mode=ro
    # plus query_only forbid it, but this is the actual guarantee the
    # audit promises), this run is not trustworthy and must not be
    # reported as if it were.
    fingerprint_after = fingerprint_database(database)

    if fingerprint_after != fingerprint_before:
        raise DatabaseMutatedError(
            "Database changed during a read-only audit run: "
            f"before={fingerprint_before} after={fingerprint_after}. "
            "This audit must never modify the database it inspects."
        )

    elapsed = time.perf_counter() - started

    output = {
        "database": str(database),
        "job_type": JOB_TYPE,
        "status": TERMINAL_STATUS,
        "terminal_failure_count": len(failures),
        "stable_category_counts": category_counts(failures),
        "raw_category_counts": dict(
            sorted(
                Counter(
                    failure["failure_category"] for failure in failures
                ).items()
            )
        ),
        "failures": failures,
        "database_size_bytes_before": fingerprint_before.size_bytes,
        "database_size_bytes_after": fingerprint_after.size_bytes,
        "database_modified_time_ns_before": (
            fingerprint_before.modified_time_ns
        ),
        "database_modified_time_ns_after": (
            fingerprint_after.modified_time_ns
        ),
        "elapsed_seconds": round(elapsed, 6),
    }

    if json_output is not None:
        output["json_output"] = str(_write_json(json_output, output))

    if csv_output is not None:
        output["csv_output"] = str(_write_csv(csv_output, failures))

    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only audit of terminally-failed "
            "hash_archive_pages_perceptual jobs, grouped by stable "
            "failure category. Never retries, enqueues, quarantines, "
            "or moves anything; safe to point at a protected backup."
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
    parser.add_argument(
        "--json-output",
        type=Path,
        help="Optional path for the JSON failure report.",
    )
    parser.add_argument(
        "--csv-output",
        type=Path,
        help="Optional path for the CSV failure report.",
    )
    return parser


def print_summary(output: dict) -> None:
    print("Perceptual-hashing terminal-failure audit completed.")
    print(f"Database:            {output['database']}")
    print(f"Terminal failures:   {output['terminal_failure_count']}")
    print("Stable categories:")

    for category, count in output["stable_category_counts"].items():
        print(f"  {category}: {count}")

    print(
        "Database unchanged:  "
        f"size={output['database_size_bytes_before']} bytes, "
        f"mtime_ns={output['database_modified_time_ns_before']}"
    )

    if output.get("json_output"):
        print(f"JSON output:         {output['json_output']}")
    if output.get("csv_output"):
        print(f"CSV output:          {output['csv_output']}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        output = run_audit(
            database=args.database,
            json_output=args.json_output,
            csv_output=args.csv_output,
        )
    except Exception as exc:
        print(f"Perceptual failure audit failed: {exc}", file=sys.stderr)
        return 1

    print_summary(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
