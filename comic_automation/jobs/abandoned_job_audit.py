"""Read-only audit of stale `claimed` / `running` jobs.

This module never recovers, retries, enqueues, cancels, fails, or
otherwise mutates a job -- it only reads `jobs` rows that look
abandoned by the same age predicate `JobQueue.recover_abandoned()`
uses, and reports them as deterministic evidence.

It exists to answer "what does the queue currently look like" *before*
any abandoned-job recovery feature is designed. Age alone does not
prove that a worker died: a job can look stale because its worker
crashed, or because it is a legitimate long-running job with no lease
or heartbeat to distinguish the two cases. This audit reports the
`recover_abandoned()`-projected outcome as informational only; it
never acts on it.

Like `comic_automation/archive/perceptual_failure_audit.py`, this
module opens the database with SQLite's `mode=ro` URI flag plus
`PRAGMA query_only = ON` and never applies migrations, so it is safe
to point at a live or backup database without risk of mutating it.

Reads go through `comic_automation/database/read_guards.py`'s
`read_consistent_snapshot`, so the whole report comes from one deferred
read transaction bracketed by `PRAGMA data_version` readings taken
outside it. That is the guard that holds under WAL: this database runs
in WAL mode, where another connection's commit is appended to the
`-wal` sidecar and can leave the main file's size and mtime
byte-identical -- so the file fingerprints this report still carries
are diagnostic evidence only, never the concurrency gate. See
`read_guards.FINGERPRINT_DIAGNOSTIC_NOTE`.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from comic_automation.database.read_guards import (
    DatabaseChangedError,
    DatabaseFingerprint,
    DatabaseIntegrityError,
    DatabaseMutatedError,
    fingerprint_database,
    fingerprint_database_files,
    fingerprint_report_fields,
    quick_check,
    read_consistent_snapshot,
    readonly_database_connection,
)


# The two statuses `JobQueue.recover_abandoned()` treats as
# potentially abandoned. Any other status (pending, completed, failed,
# cancelled, blocked) is never a candidate, regardless of age.
AUDITED_STATUSES = ("claimed", "running")

# recover_abandoned() marks a stale job PENDING (retry) if attempts
# remain, otherwise FAILED (terminal). This audit mirrors that exact
# projection so the report reflects what recovery *would* currently
# do -- without doing it.
PROJECTED_OUTCOME_RETRYABLE = "pending"
PROJECTED_OUTCOME_TERMINAL = "failed"

WORKER_LIVENESS_WARNING = (
    "A stale claimed/running timestamp only shows that a job's status "
    "has not advanced recently. Without leases or heartbeats, this "
    "audit cannot distinguish a dead worker from a legitimate "
    "long-running one -- age alone is not proof of abandonment."
)

PROJECTED_OUTCOME_DISCLAIMER = (
    "projected_outcome is informational only: it is what "
    "JobQueue.recover_abandoned()'s existing attempts-remaining rule "
    "would currently produce for this job. This audit never applies "
    "it -- no job is recovered, retried, enqueued, cancelled, or "
    "failed by running this report."
)

_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


class OutputPathCollisionError(ValueError):
    """Raised when a requested output path could clobber input data.

    The audit's own read-only guarantees (mode=ro + query_only) only
    protect the connection it opens to *read* the database. The
    separate step that later *writes* the JSON/CSV report opens the
    output path in write mode with no such protection, so if a caller
    pointed --json-output or --csv-output at the database file itself
    (directly, or via a symlink/hard link alias), that write would
    silently overwrite or corrupt the database this audit just
    verified was untouched. This is checked and rejected before the
    database is even opened, so no directory is created and nothing
    is written once a collision is detected.
    """


# `DatabaseFingerprint`, `fingerprint_database`,
# `readonly_database_connection`, `DatabaseChangedError`,
# `DatabaseIntegrityError` and `DatabaseMutatedError` are re-exported
# from `comic_automation.database.read_guards` above. They used to be
# defined here (one of five near-identical copies across the read-only
# audits, which had already drifted on `isolation_level`); the names
# stay importable from this module because
# `comic_automation/jobs/abandoned_job_recovery.py` and the tests
# import them from here.


def _same_file(first: Path, second: Path) -> bool:
    """True if `first` and `second` name the same file on disk.

    Path equality after `resolve()` catches the common cases (identical
    path, or a path reached through a different but symlink-resolvable
    route). `Path.samefile()` additionally catches hard links, which
    have distinct paths and both resolve to themselves, but share the
    same underlying inode -- `samefile()` is only meaningful when both
    paths already exist, so it's attempted in addition to, not instead
    of, the resolved-path comparison.
    """
    if first.resolve(strict=False) == second.resolve(strict=False):
        return True

    if first.exists() and second.exists():
        try:
            return first.samefile(second)
        except OSError:
            return False

    return False


def validate_output_paths(
    database: Path,
    *,
    json_output: Path | None,
    csv_output: Path | None,
) -> None:
    """Reject any output path that could clobber the database or the
    other output, before anything is opened or written.

    Must be called before the database is opened for reading and
    before any output directory is created -- a rejected collision
    must leave the filesystem exactly as it found it.
    """
    if json_output is not None and _same_file(json_output, database):
        raise OutputPathCollisionError(
            f"--json-output ({json_output}) must not be the same file "
            f"as --database ({database}); this would let the report "
            "writer overwrite the database."
        )

    if csv_output is not None and _same_file(csv_output, database):
        raise OutputPathCollisionError(
            f"--csv-output ({csv_output}) must not be the same file "
            f"as --database ({database}); this would let the report "
            "writer overwrite the database."
        )

    if (
        json_output is not None
        and csv_output is not None
        and _same_file(json_output, csv_output)
    ):
        raise OutputPathCollisionError(
            f"--json-output ({json_output}) and --csv-output "
            f"({csv_output}) must not be the same file."
        )


def _utc_sql_timestamp(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    return moment.astimezone(timezone.utc).strftime(_TIMESTAMP_FORMAT)


def _parse_utc_sql_timestamp(text: str) -> datetime:
    return datetime.strptime(text, _TIMESTAMP_FORMAT).replace(
        tzinfo=timezone.utc
    )


def _projected_outcome(*, attempts: int, max_attempts: int) -> str:
    if attempts < max_attempts:
        return PROJECTED_OUTCOME_RETRYABLE

    return PROJECTED_OUTCOME_TERMINAL


def collect_stale_jobs(
    connection: sqlite3.Connection,
    *,
    older_than_seconds: int,
    now: datetime | None = None,
) -> list[dict]:
    """Every `claimed`/`running` job stale as of `now`.

    Mirrors `JobQueue.recover_abandoned()`'s own predicate exactly, so
    this report reflects what that method would currently select:

    - only `status IN ('claimed', 'running')`;
    - staleness cutoff is `COALESCE(started_at, claimed_at) <= cutoff`,
      i.e. a job's *most recent* activity timestamp, falling back to
      when it was claimed if it never started running.
    - the cutoff comparison is inclusive (`<=`): a job whose effective
      activity timestamp lands exactly on the cutoff is reported as
      stale, matching `recover_abandoned()`'s own boundary behavior.

    `now` is injectable so tests do not depend on wall-clock timing;
    it defaults to the current UTC time.
    """
    if older_than_seconds < 0:
        raise ValueError("older_than_seconds cannot be negative.")

    effective_now = now or datetime.now(timezone.utc)

    if effective_now.tzinfo is None:
        effective_now = effective_now.replace(tzinfo=timezone.utc)

    cutoff = effective_now - timedelta(seconds=older_than_seconds)
    cutoff_text = _utc_sql_timestamp(cutoff)

    status_placeholders = ",".join("?" for _ in AUDITED_STATUSES)

    rows = connection.execute(
        f"""
        SELECT
            id,
            job_type,
            archive_id,
            status,
            worker_id,
            attempts,
            max_attempts,
            claimed_at,
            started_at,
            COALESCE(started_at, claimed_at) AS effective_activity_at
        FROM jobs
        WHERE status IN ({status_placeholders})
          AND COALESCE(started_at, claimed_at) <= ?
        ORDER BY
            COALESCE(started_at, claimed_at) ASC,
            id ASC
        """,
        (*AUDITED_STATUSES, cutoff_text),
    ).fetchall()

    stale_jobs: list[dict] = []

    for row in rows:
        effective_activity_at = str(row["effective_activity_at"])
        activity_moment = _parse_utc_sql_timestamp(effective_activity_at)
        age_seconds = (
            effective_now - activity_moment
        ).total_seconds()
        attempts = int(row["attempts"])
        max_attempts = int(row["max_attempts"])

        stale_jobs.append(
            {
                "job_id": int(row["id"]),
                "job_type": str(row["job_type"]),
                "archive_id": (
                    int(row["archive_id"])
                    if row["archive_id"] is not None
                    else None
                ),
                "status": str(row["status"]),
                "worker_id": row["worker_id"],
                "attempts": attempts,
                "max_attempts": max_attempts,
                "claimed_at": row["claimed_at"],
                "started_at": row["started_at"],
                "effective_activity_at": effective_activity_at,
                "age_seconds": round(age_seconds, 3),
                "projected_outcome": _projected_outcome(
                    attempts=attempts,
                    max_attempts=max_attempts,
                ),
            }
        )

    return stale_jobs


def _counts_by(stale_jobs: list[dict], field: str) -> dict[str, int]:
    counts = Counter(job[field] for job in stale_jobs)
    return dict(sorted(counts.items()))


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
    "job_type",
    "archive_id",
    "status",
    "worker_id",
    "attempts",
    "max_attempts",
    "claimed_at",
    "started_at",
    "effective_activity_at",
    "age_seconds",
    "projected_outcome",
]


def _write_csv(path: Path, stale_jobs: list[dict]) -> Path:
    resolved = path.resolve(strict=False)
    resolved.parent.mkdir(parents=True, exist_ok=True)

    with resolved.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=_CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(stale_jobs)

    return resolved


def run_audit(
    *,
    database: Path,
    older_than_seconds: int,
    now: datetime | None = None,
    json_output: Path | None = None,
    csv_output: Path | None = None,
) -> dict:
    """Produce a read-only report of stale claimed/running jobs.

    Never mutates `database`: opens it via `readonly_database_connection`
    (mode=ro + PRAGMA query_only) and never applies migrations.

    The authoritative concurrency gate is `PRAGMA data_version`,
    sampled outside and around the single deferred read transaction
    that every query runs in (see `read_consistent_snapshot`); a
    concurrent commit raises `DatabaseChangedError`. `PRAGMA
    quick_check` runs inside that same window and raises
    `DatabaseIntegrityError` if it does not return 'ok'.

    The main file's size and mtime are still fingerprinted before and
    after, and a change raises `DatabaseMutatedError` -- but that is
    defense in depth against *this* process touching the file, not a
    concurrency guarantee: under WAL another connection's commit can
    leave those bytes identical. The report labels them accordingly.

    `json_output`/`csv_output` are validated against `database` (and
    against each other) *before* the database is opened or any
    directory is created: see `validate_output_paths`. A rejected
    collision raises `OutputPathCollisionError` and leaves the
    filesystem, including the database, completely untouched.
    """
    database = Path(database).resolve(strict=False)

    if not database.is_file():
        raise FileNotFoundError(f"Database does not exist: {database}")

    validate_output_paths(
        database,
        json_output=json_output,
        csv_output=csv_output,
    )

    effective_now = now or datetime.now(timezone.utc)

    if effective_now.tzinfo is None:
        effective_now = effective_now.replace(tzinfo=timezone.utc)

    started = time.perf_counter()
    fingerprint_before = fingerprint_database(database)
    files_before = fingerprint_database_files(database)

    def read(connection: sqlite3.Connection) -> list[dict]:
        # Looked up on the module at call time, so the WAL regression
        # test can wrap it to commit from another connection while this
        # snapshot is open.
        return collect_stale_jobs(
            connection,
            older_than_seconds=older_than_seconds,
            now=effective_now,
        )

    snapshot = read_consistent_snapshot(
        database,
        read,
        context="audit",
        integrity_check=quick_check,
    )
    stale_jobs = snapshot.result

    # Re-stat *after* the connection is closed: if opening read-only or
    # running the SELECT touched the file (it shouldn't -- mode=ro
    # plus query_only forbid it, but this is the actual guarantee the
    # audit promises), this run is not trustworthy and must not be
    # reported as if it were. Checked *after* the data_version gate
    # above, which is the stronger detector, so a concurrent commit is
    # always reported as exactly that.
    fingerprint_after = fingerprint_database(database)
    files_after = fingerprint_database_files(database)

    if fingerprint_after != fingerprint_before:
        raise DatabaseMutatedError(
            "Database changed during a read-only audit run: "
            f"before={fingerprint_before} after={fingerprint_after}. "
            "This audit must never modify the database it inspects."
        )

    elapsed = time.perf_counter() - started

    output = {
        "database": str(database),
        "audited_statuses": list(AUDITED_STATUSES),
        "older_than_seconds": older_than_seconds,
        "now_utc": _utc_sql_timestamp(effective_now),
        "cutoff_utc": _utc_sql_timestamp(
            effective_now - timedelta(seconds=older_than_seconds)
        ),
        "cutoff_is_inclusive": True,
        "stale_job_count": len(stale_jobs),
        "status_counts": _counts_by(stale_jobs, "status"),
        "job_type_counts": _counts_by(stale_jobs, "job_type"),
        "projected_outcome_counts": _counts_by(
            stale_jobs, "projected_outcome"
        ),
        "jobs": stale_jobs,
        **snapshot.report_fields(),
        **fingerprint_report_fields(
            fingerprint_before=fingerprint_before,
            fingerprint_after=fingerprint_after,
            files_before=files_before,
            files_after=files_after,
        ),
        "elapsed_seconds": round(elapsed, 6),
        "worker_liveness_warning": WORKER_LIVENESS_WARNING,
        "projected_outcome_disclaimer": PROJECTED_OUTCOME_DISCLAIMER,
    }

    if json_output is not None:
        output["json_output"] = str(_write_json(json_output, output))

    if csv_output is not None:
        output["csv_output"] = str(_write_csv(csv_output, stale_jobs))

    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only audit of stale claimed/running jobs, using the "
            "same staleness predicate as JobQueue.recover_abandoned(). "
            "Never recovers, retries, enqueues, cancels, or fails "
            "anything -- reports evidence only."
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
        "--older-than-seconds",
        type=int,
        required=True,
        help=(
            "Staleness cutoff in seconds, applied to "
            "COALESCE(started_at, claimed_at), inclusive."
        ),
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="Optional path for the JSON report.",
    )
    parser.add_argument(
        "--csv-output",
        type=Path,
        help="Optional path for the CSV report.",
    )
    return parser


def print_summary(output: dict) -> None:
    print("Abandoned-job audit completed (read-only; no jobs were changed).")
    print(f"Database:            {output['database']}")
    print(f"Cutoff (UTC, <=):    {output['cutoff_utc']}")
    print(f"Stale jobs found:    {output['stale_job_count']}")
    print("By status:")

    for status, count in output["status_counts"].items():
        print(f"  {status}: {count}")

    print("Projected outcome (informational only, not applied):")

    for outcome, count in output["projected_outcome_counts"].items():
        print(f"  {outcome}: {count}")

    print(f"Integrity check:     {output['quick_check']}")
    print(
        "Snapshot data_version: "
        f"{output['data_version_before']} -> "
        f"{output['data_version_after']} (authoritative guard)"
    )
    print(
        "DB file unchanged:   "
        f"{output['database_file_unchanged']} (diagnostic only; a WAL "
        "commit can leave the main file identical)"
    )
    print(f"Warning:             {output['worker_liveness_warning']}")

    if output.get("json_output"):
        print(f"JSON output:         {output['json_output']}")
    if output.get("csv_output"):
        print(f"CSV output:          {output['csv_output']}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        output = run_audit(
            database=args.database,
            older_than_seconds=args.older_than_seconds,
            json_output=args.json_output,
            csv_output=args.csv_output,
        )
    except Exception as exc:
        print(f"Abandoned-job audit failed: {exc}", file=sys.stderr)
        return 1

    print_summary(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
