"""Guarded, operator-facing recovery of stale claimed/running jobs.

`JobQueue.recover_abandoned()` (`comic_automation/jobs/queue.py`) already
runs unattended and unconditionally from
`ComicAutomationService.initialize()` every time the service starts, with
a 300-second staleness window. That call has no operator confirmation,
no minimum-age guard beyond its own `older_than_seconds >= 0` check, and
no report of *which* jobs it touched -- it just returns a count. That is
appropriate for an unattended startup hook, but it is not something a
human should be able to trigger against a live database by accident.

This module is the deliberate, human-run counterpart, distinct from
that implicit startup call. By default it is strictly report-only: it
reuses `comic_automation.jobs.abandoned_job_audit.collect_stale_jobs()`
(the same helper `abandoned_job_audit.run_audit()` uses) and returns
exactly what recovery *would* do, without writing anything. Only when
the caller passes `--confirm` *and* an `--expected-count` that matches
the live stale-job count at the moment of the write does this module
actually mutate the database -- and even then, the mutation reuses the
identical predicate, ordering, and per-row update that
`JobQueue.recover_abandoned()` uses, executed inside the same
`BEGIN IMMEDIATE` locking discipline as the rest of `queue.py`. SQLite
blocks any concurrent writer (`claim_next()`, `mark_completed()`,
`mark_failed()`, ...) on another connection until this transaction
commits or rolls back, and the whole batch rolls back atomically --all
rows or none-- if anything raises partway through.

Guarded age check: `--older-than-seconds` below `MINIMUM_OLDER_THAN_SECONDS`
is refused unless `--allow-short-window` is also passed. Recovering a
job that is merely slow, not abandoned, resets its claim and (once
attempts are exhausted) fails it -- a destructive, hard-to-undo action
that should never happen because of an operator typo.

Design decision -- error_message on recovery:
`recover_abandoned()` unconditionally overwrites `error_message` with
the fixed string `RECOVERY_ERROR_MESSAGE`. This module keeps that exact
behavior rather than trying to append to or preserve whatever was there
before, for a concrete reason: by the time a job reaches 'claimed' or
'running' (the only statuses this predicate ever touches), its
`error_message` has already been reset to NULL by `claim_next()` -- see
queue.py's `UPDATE ... SET error_message = NULL` as part of the
pending -> claimed transition. So there is no prior diagnostic context
sitting in `error_message` for a claimed/running job to lose: whatever
the previous attempt's failure reason was, it was already cleared the
moment this attempt was claimed. `attempts` is never touched by
recovery (it is only ever incremented by `claim_next()`), so the number
of prior tries -- the actually load-bearing diagnostic signal -- always
survives recovery unchanged. `tests/test_abandoned_job_recovery.py`
asserts both of these facts directly.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from comic_automation.database.connection import connect_database
from comic_automation.jobs.abandoned_job_audit import (
    AUDITED_STATUSES,
    DatabaseFingerprint,
    DatabaseMutatedError,
    collect_stale_jobs,
    fingerprint_database,
    readonly_database_connection,
)


# recover_abandoned()'s unattended caller (ComicAutomationService
# .initialize()) uses a 300-second staleness window. That production
# value is the floor here: anything shorter is very likely to catch
# jobs that are merely slow, not abandoned by a dead worker.
MINIMUM_OLDER_THAN_SECONDS = 300

# Must stay byte-identical to the literal JobQueue.recover_abandoned()
# writes to error_message -- see the module docstring's "Design
# decision" section for why reusing this exact literal (rather than
# preserving or appending to a prior error_message) is safe.
RECOVERY_ERROR_MESSAGE = "Recovered after worker abandonment."

_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


class UnsafeOlderThanSecondsError(ValueError):
    """Raised when --older-than-seconds is below the safety floor.

    See MINIMUM_OLDER_THAN_SECONDS. Overridable via allow_short_window.
    """


class ExpectedCountRequiredError(RuntimeError):
    """Raised when --confirm is passed without --expected-count.

    Mutation is refused rather than silently defaulting expected_count
    to "whatever is live right now" -- the whole point of the guard is
    that the operator states the count they reviewed *before* the tool
    checks it against the live database.
    """


class ExpectedCountMismatchError(RuntimeError):
    """Raised when --expected-count does not match the live count.

    Checked inside the same BEGIN IMMEDIATE transaction that performs
    the recovery, so this is the guard that actually protects against a
    job changing status (e.g. a worker completing it) between when the
    operator reviewed the report-only preview and when --confirm was
    run: if the live set differs from what was reviewed, nothing is
    written.
    """


def _utc_sql_timestamp(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    return moment.astimezone(timezone.utc).strftime(_TIMESTAMP_FORMAT)


def _write_json(path: Path, payload: object) -> Path:
    resolved = path.resolve(strict=False)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return resolved


def _apply_recovery_row(
    connection,
    *,
    job: dict,
    now_text: str,
) -> dict:
    """Apply the exact same update JobQueue.recover_abandoned() makes.

    `job` is one entry from `collect_stale_jobs()`'s output, read inside
    the caller's BEGIN IMMEDIATE transaction. Returns a before/after
    record for the JSON report.
    """
    if job["attempts"] < job["max_attempts"]:
        status_after = "pending"
        completed_at_after = None
    else:
        status_after = "failed"
        completed_at_after = now_text

    connection.execute(
        """
        UPDATE jobs
        SET
            status = ?,
            available_at = ?,
            claimed_at = NULL,
            started_at = NULL,
            completed_at = ?,
            worker_id = NULL,
            error_message = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            status_after,
            now_text,
            completed_at_after,
            RECOVERY_ERROR_MESSAGE,
            now_text,
            job["job_id"],
        ),
    )

    return {
        "job_id": job["job_id"],
        "job_type": job["job_type"],
        "archive_id": job["archive_id"],
        "status_before": job["status"],
        "status_after": status_after,
        "attempts": job["attempts"],
        "max_attempts": job["max_attempts"],
        "worker_id_before": job["worker_id"],
        "worker_id_after": None,
        "error_message_after": RECOVERY_ERROR_MESSAGE,
        "completed_at_after": completed_at_after,
        "effective_activity_at": job["effective_activity_at"],
        "age_seconds": job["age_seconds"],
        "projected_outcome": job["projected_outcome"],
    }


def _counts_by(jobs: list[dict], field: str) -> dict[str, int]:
    counts = Counter(job[field] for job in jobs)
    return dict(sorted(counts.items()))


def _build_report(
    *,
    database: Path,
    older_than_seconds: int,
    effective_now: datetime,
    mode: str,
    applied: bool,
    jobs: list[dict],
    outcome_field: str,
    fingerprint_before: DatabaseFingerprint,
    fingerprint_after: DatabaseFingerprint,
    started: float,
    expected_count: int | None,
) -> dict:
    elapsed = time.perf_counter() - started

    report = {
        "database": str(database),
        "audited_statuses": list(AUDITED_STATUSES),
        "older_than_seconds": older_than_seconds,
        "minimum_older_than_seconds": MINIMUM_OLDER_THAN_SECONDS,
        "now_utc": _utc_sql_timestamp(effective_now),
        "mode": mode,
        "applied": applied,
        "expected_count": expected_count,
        "job_count": len(jobs),
        "outcome_counts": _counts_by(jobs, outcome_field),
        "jobs": jobs,
        "would_recover_count": len(jobs) if not applied else None,
        "recovered_count": len(jobs) if applied else 0,
        "database_size_bytes_before": fingerprint_before.size_bytes,
        "database_size_bytes_after": fingerprint_after.size_bytes,
        "database_modified_time_ns_before": (
            fingerprint_before.modified_time_ns
        ),
        "database_modified_time_ns_after": (
            fingerprint_after.modified_time_ns
        ),
        "database_unchanged": fingerprint_after == fingerprint_before,
        "elapsed_seconds": round(elapsed, 6),
        "recovery_error_message": RECOVERY_ERROR_MESSAGE,
    }

    return report


def run_recovery(
    *,
    database: Path,
    older_than_seconds: int,
    confirm: bool = False,
    expected_count: int | None = None,
    allow_short_window: bool = False,
    now: datetime | None = None,
    json_output: Path | None = None,
) -> dict:
    """Report on, or (guarded) apply, abandoned-job recovery.

    Report-only (confirm=False, the default): opens the database via
    `readonly_database_connection` (mode=ro + PRAGMA query_only, see
    `abandoned_job_audit`) and returns exactly what recovery would do.
    Never writes; the database's fingerprint is checked before/after
    and `DatabaseMutatedError` is raised if it changed regardless.

    Mutating (confirm=True): requires `expected_count`. Opens a
    writable connection, takes the write lock with `BEGIN IMMEDIATE`
    (matching `JobQueue.recover_abandoned()`'s own locking), re-reads
    the live stale-job set *inside* that transaction, and refuses to
    write -- raising `ExpectedCountMismatchError` and rolling back --
    if the live count no longer matches `expected_count`. Otherwise
    applies the identical per-row update `recover_abandoned()` applies,
    commits, and returns a before/after report. Any exception raised
    partway through the batch rolls back the whole transaction: no
    partial recovery is ever committed.
    """
    if older_than_seconds < 0:
        raise ValueError("older_than_seconds cannot be negative.")

    if (
        older_than_seconds < MINIMUM_OLDER_THAN_SECONDS
        and not allow_short_window
    ):
        raise UnsafeOlderThanSecondsError(
            f"--older-than-seconds={older_than_seconds} is below the "
            f"safety floor of {MINIMUM_OLDER_THAN_SECONDS} seconds. A "
            "short window risks recovering jobs that are merely slow, "
            "not abandoned -- which resets their claim and, once "
            "attempts are exhausted, permanently fails them. Pass "
            "--allow-short-window to override deliberately."
        )

    if confirm and expected_count is None:
        raise ExpectedCountRequiredError(
            "--confirm requires --expected-count: state the exact "
            "number of stale jobs reviewed (from a prior report-only "
            "run) so this tool can refuse to mutate a set that has "
            "since changed."
        )

    database = Path(database).resolve(strict=False)

    if not database.is_file():
        raise FileNotFoundError(f"Database does not exist: {database}")

    effective_now = now or datetime.now(timezone.utc)

    if effective_now.tzinfo is None:
        effective_now = effective_now.replace(tzinfo=timezone.utc)

    started = time.perf_counter()
    fingerprint_before = fingerprint_database(database)

    if not confirm:
        with readonly_database_connection(database) as connection:
            stale_jobs = collect_stale_jobs(
                connection,
                older_than_seconds=older_than_seconds,
                now=effective_now,
            )

        fingerprint_after = fingerprint_database(database)

        if fingerprint_after != fingerprint_before:
            raise DatabaseMutatedError(
                "Database changed during a report-only recovery "
                f"preview: before={fingerprint_before} "
                f"after={fingerprint_after}. This mode must never "
                "modify the database it previews."
            )

        report = _build_report(
            database=database,
            older_than_seconds=older_than_seconds,
            effective_now=effective_now,
            mode="report_only",
            applied=False,
            jobs=stale_jobs,
            outcome_field="projected_outcome",
            fingerprint_before=fingerprint_before,
            fingerprint_after=fingerprint_after,
            started=started,
            expected_count=expected_count,
        )

        if json_output is not None:
            report["json_output"] = str(_write_json(json_output, report))

        return report

    # confirm=True: a guarded, atomic mutation.
    connection = connect_database(database)

    try:
        # BEGIN IMMEDIATE takes the write lock up front, exactly like
        # JobQueue.recover_abandoned() and claim_next(): any concurrent
        # claim_next()/mark_completed()/mark_failed() on another
        # connection blocks until this transaction ends, so the
        # expected-count check and the update below always see (and
        # act on) one consistent snapshot.
        connection.execute("BEGIN IMMEDIATE")

        stale_jobs = collect_stale_jobs(
            connection,
            older_than_seconds=older_than_seconds,
            now=effective_now,
        )
        actual_count = len(stale_jobs)

        if actual_count != expected_count:
            raise ExpectedCountMismatchError(
                f"--expected-count={expected_count} does not match the "
                f"live stale-job count of {actual_count}. The set of "
                "abandoned jobs changed since it was last reviewed (a "
                "worker may have completed or failed one, or a new job "
                "became stale); refusing to recover a set that was not "
                "actually reviewed. Re-run the report-only preview and "
                "pass the fresh count."
            )

        now_text = _utc_sql_timestamp(effective_now)
        recovered_jobs = [
            _apply_recovery_row(connection, job=job, now_text=now_text)
            for job in stale_jobs
        ]

        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()

    fingerprint_after = fingerprint_database(database)

    report = _build_report(
        database=database,
        older_than_seconds=older_than_seconds,
        effective_now=effective_now,
        mode="applied",
        applied=True,
        jobs=recovered_jobs,
        outcome_field="status_after",
        fingerprint_before=fingerprint_before,
        fingerprint_after=fingerprint_after,
        started=started,
        expected_count=expected_count,
    )

    if json_output is not None:
        report["json_output"] = str(_write_json(json_output, report))

    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Guarded, operator-run recovery of stale claimed/running "
            "jobs. Report-only by default (reuses "
            "abandoned_job_audit.collect_stale_jobs(); never writes). "
            "Pass --confirm and --expected-count to actually apply "
            "recovery, matching JobQueue.recover_abandoned()'s own "
            "predicate and per-row update exactly."
        )
    )
    parser.add_argument(
        "--database",
        type=Path,
        required=True,
        help="SQLite database to inspect or recover.",
    )
    parser.add_argument(
        "--older-than-seconds",
        type=int,
        required=True,
        help=(
            "Staleness cutoff in seconds, applied to "
            "COALESCE(started_at, claimed_at), inclusive. Refused below "
            f"{MINIMUM_OLDER_THAN_SECONDS} unless --allow-short-window "
            "is also passed."
        ),
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help=(
            "Actually apply recovery. Without this flag the command is "
            "strictly report-only and never writes to --database."
        ),
    )
    parser.add_argument(
        "--expected-count",
        type=int,
        default=None,
        help=(
            "Required together with --confirm: the exact stale-job "
            "count the operator reviewed via a prior report-only run. "
            "Recovery is refused if the live count no longer matches."
        ),
    )
    parser.add_argument(
        "--allow-short-window",
        action="store_true",
        help=(
            "Permit --older-than-seconds below the "
            f"{MINIMUM_OLDER_THAN_SECONDS}-second safety floor."
        ),
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="Optional path for the JSON report.",
    )
    return parser


def print_summary(output: dict) -> None:
    if output["applied"]:
        print("Abandoned-job recovery applied.")
    else:
        print(
            "Abandoned-job recovery preview only "
            "(no jobs were changed; pass --confirm to apply)."
        )

    print(f"Database:            {output['database']}")
    print(f"Cutoff (UTC, <=):    {output['now_utc']}")
    print(f"Older-than-seconds:  {output['older_than_seconds']}")

    if output["applied"]:
        print(f"Recovered jobs:      {output['recovered_count']}")
    else:
        print(f"Would recover:       {output['would_recover_count']}")

    print("By outcome:")

    for outcome, count in output["outcome_counts"].items():
        print(f"  {outcome}: {count}")

    print(f"Database unchanged:  {output['database_unchanged']}")

    if output.get("json_output"):
        print(f"JSON output:         {output['json_output']}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        output = run_recovery(
            database=args.database,
            older_than_seconds=args.older_than_seconds,
            confirm=args.confirm,
            expected_count=args.expected_count,
            allow_short_window=args.allow_short_window,
            json_output=args.json_output,
        )
    except Exception as exc:
        print(f"Abandoned-job recovery failed: {exc}", file=sys.stderr)
        return 1

    print_summary(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
