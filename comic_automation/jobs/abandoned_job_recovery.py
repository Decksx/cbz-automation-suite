"""Guarded, operator-facing recovery of stale claimed/running jobs.

`JobQueue.recover_abandoned()` (`comic_automation/jobs/queue.py`) used to
run unattended and unconditionally from
`ComicAutomationService.initialize()` every time the service started,
with a 300-second staleness window. That call had no operator
confirmation, no minimum-age guard beyond its own
`older_than_seconds >= 0` check, and no report of *which* jobs it
touched -- it just returned a count. It has since been removed: startup
now only *detects* stale jobs read-only and logs a warning pointing
here, because age cannot distinguish a dead worker from a slow one (see
"Guard 3" below). `recover_abandoned()` itself remains in `queue.py`,
but this module is now the only supported way to recover jobs.

This module is the deliberate, human-run counterpart. By default it is
strictly report-only: it
reuses `comic_automation.jobs.abandoned_job_audit.collect_stale_jobs()`
(the same helper `abandoned_job_audit.run_audit()` uses) and returns
exactly what recovery *would* do, without writing anything. Only when
the caller passes `--confirm` together with the full set of attestations
described below does this module actually mutate the database -- and
even then, the mutation reuses the identical predicate, ordering, and
per-row update that `JobQueue.recover_abandoned()` uses, executed inside
the same `BEGIN IMMEDIATE` locking discipline as the rest of `queue.py`.
SQLite blocks any concurrent writer (`claim_next()`, `mark_completed()`,
`mark_failed()`, ...) on another connection until this transaction
commits or rolls back, and the whole batch rolls back atomically --all
rows or none-- if anything raises partway through.

Guarded age check: `--older-than-seconds` below `MINIMUM_OLDER_THAN_SECONDS`
is refused unless `--allow-short-window` is also passed. Recovering a
job that is merely slow, not abandoned, resets its claim and (once
attempts are exhausted) fails it -- a destructive, hard-to-undo action
that should never happen because of an operator typo.

Guard 1 -- output paths are validated before anything is opened:
The read-only guarantees this module inherits from
`abandoned_job_audit` (`mode=ro` + `PRAGMA query_only`) protect only
the connection used to *read* the database. The later step that writes
the JSON report opens `--json-output` in write mode with no such
protection, so pointing `--json-output` at the database (directly, or
through a symlink/hard-link alias) would silently destroy the very
database this tool just inspected. `validate_output_path()` therefore
runs before the database is opened, before it is fingerprinted, and
before any parent directory is created, so a rejected run leaves the
filesystem byte-for-byte as it found it. It additionally refuses an
`--json-output` path that *already exists*: a recovery report is the
only durable evidence that a destructive run happened, and silently
overwriting the report of a previous run would destroy the audit trail
of the thing this tool exists to make auditable.

Guard 2 -- why `--expected-count` alone is not sufficient:
An operator reviews a report-only preview, sees N stale jobs, and
re-runs with `--confirm --expected-count N`. A bare count check inside
the write transaction does *not* prove the reviewed set is the set
about to be recovered. Concretely: suppose the operator reviewed jobs
{7, 9}. Between the preview and the confirm, a worker completes job 7
(so it leaves the stale set) while job 12 -- which the operator never
looked at -- crosses the age threshold and enters the stale set. The
live set is now {9, 12}: the count is still 2, the count guard passes,
and the tool would recover job 12, a job no human ever reviewed. The
count changed by zero while the *identity* of the set changed by half.

So the count is kept only as a cheap, human-readable first line of
defense, and the real guard is a stable snapshot digest
(`--expected-snapshot`) that binds the identity and the outcome-
determining state of the reviewed set. Report-only mode prints and
emits the digest; `--confirm` recomputes it over the freshly-read set
*inside the same `BEGIN IMMEDIATE` transaction* that performs the
writes and raises `SnapshotMismatchError` (rolling back, writing
nothing) if it differs. In the swap scenario above the digest differs,
because job 7's row is gone from the canonical serialization and job
12's has appeared.

Digest scheme (`SNAPSHOT_DIGEST_VERSION`): the stale jobs are sorted by
`job_id` ascending, and each is rendered as a single line of
`key=value` fields joined by `|`, in this fixed order: `job_id`,
`status`, `attempts`, `max_attempts`, `effective_activity_at`,
`projected_outcome`. Those fields are exactly what determines whether
a given row is a candidate and what recovery would do to it --
`attempts` versus `max_attempts` decides retry-versus-permanent-fail,
and `effective_activity_at` is the value the staleness predicate tests.
A version marker line is prepended so a future change to the scheme can
never be mistaken for an unchanged set. The lines are joined with "\\n",
terminated with a trailing "\\n", encoded UTF-8, and hashed with
SHA-256; the digest is the lowercase hex string. An empty stale set has
a well-defined digest (the version line alone), so `--expected-snapshot`
remains meaningful and mandatory even when there is nothing to recover.

Guard 3 -- age is not proof of abandonment; workers must be stopped:
This module has no leases and no heartbeats, because the queue schema
has none. `abandoned_job_audit.WORKER_LIVENESS_WARNING` states the
consequence plainly and this module reuses that exact text: a stale
`claimed`/`running` timestamp only shows that a job's status has not
advanced recently, which is equally consistent with a dead worker and
with a legitimately slow one. Some archives in this library are large
and slow to decode, and the production handoff notes explicitly warn
against inferring failure from an interval simply taking longer than a
previous interval. No age floor -- not 300 seconds, not any value --
can distinguish the two cases, so the 300-second floor must not be
treated as evidence. `--confirm` therefore additionally requires
`--workers-stopped`, an explicit operator attestation that no worker is
running against this database; without it, confirm mode refuses. The
same warning is surfaced in the report-only console output and in the
JSON report, so an operator sees it while deciding rather than after.

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
import hashlib
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from comic_automation.database.connection import connect_database
from comic_automation.jobs.abandoned_job_audit import (
    AUDITED_STATUSES,
    WORKER_LIVENESS_WARNING,
    DatabaseFingerprint,
    DatabaseMutatedError,
    OutputPathCollisionError,
    _same_file,
    collect_stale_jobs,
    fingerprint_database,
    readonly_database_connection,
)


# 300 seconds was the staleness window recover_abandoned()'s former
# unattended caller (ComicAutomationService.initialize()) used, and is
# still the threshold that service's read-only startup detection
# reports on. That production value is the floor here: anything
# shorter is very likely to catch
# jobs that are merely slow, not abandoned by a dead worker. Note that
# the floor is a typo guard, not evidence of abandonment -- see the
# module docstring's "Guard 3" section and WORKER_LIVENESS_WARNING.
MINIMUM_OLDER_THAN_SECONDS = 300

# Must stay byte-identical to the literal JobQueue.recover_abandoned()
# writes to error_message -- see the module docstring's "Design
# decision" section for why reusing this exact literal (rather than
# preserving or appending to a prior error_message) is safe.
RECOVERY_ERROR_MESSAGE = "Recovered after worker abandonment."

# Version marker prepended to the canonical snapshot serialization. It
# is part of the hashed bytes on purpose: if the field list or the
# rendering below ever changes, digests produced by the old and new
# schemes cannot collide, so an operator can never carry a digest
# across a scheme change and have it silently "match".
SNAPSHOT_DIGEST_VERSION = "abandoned-job-recovery-snapshot-v1"

# The fields hashed into the snapshot digest, in the exact order they
# are rendered. Every one of them is load-bearing: job_id and status
# fix *which* rows were reviewed and in what state, attempts and
# max_attempts decide retry-versus-permanent-fail, and
# effective_activity_at is the value the staleness predicate tests.
# projected_outcome is derived from attempts/max_attempts but is hashed
# explicitly so the recovery decision the operator actually reviewed is
# bound directly, not only through its inputs.
SNAPSHOT_DIGEST_FIELDS = (
    "job_id",
    "status",
    "attempts",
    "max_attempts",
    "effective_activity_at",
    "projected_outcome",
)

# A SHA-256 hex digest: 64 lowercase hex characters.
_SNAPSHOT_DIGEST_LENGTH = 64
_HEX_CHARACTERS = frozenset("0123456789abcdef")

WORKERS_STOPPED_ATTESTATION = (
    "--confirm requires --workers-stopped: an explicit attestation "
    "that no worker process is running against this database. "
    + WORKER_LIVENESS_WARNING
    + " A long-running archive decode can look identical to an "
    "abandoned one at any age threshold, so recovering while workers "
    "are live can reset the claim of a job that is still being worked "
    "on -- and, once its attempts are exhausted, permanently fail it. "
    "Stop the workers first, then re-run with --workers-stopped."
)

_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


class UnsafeOlderThanSecondsError(ValueError):
    """Raised when --older-than-seconds is below the safety floor.

    See MINIMUM_OLDER_THAN_SECONDS. Overridable via allow_short_window.
    """


class OutputPathExistsError(ValueError):
    """Raised when --json-output names a path that already exists.

    A recovery report is the only durable evidence that a destructive
    run happened -- which jobs were touched, what they looked like
    before, and what they became. Overwriting the report of a previous
    run would silently destroy that evidence, so an existing path is
    refused outright rather than clobbered. The operator must pick a
    fresh path (or move the old report aside deliberately), which also
    makes an accidental second `--confirm` run against a stale command
    line fail loudly instead of quietly replacing history.

    Dangling symlinks count as existing: writing through one would
    create a file at the link's target, outside the path the operator
    named and outside anything this check could have validated.
    """


class WorkersNotStoppedError(RuntimeError):
    """Raised when --confirm is passed without --workers-stopped.

    Without leases or heartbeats this tool cannot tell a dead worker
    from a slow one (see the module docstring's "Guard 3" section and
    `abandoned_job_audit.WORKER_LIVENESS_WARNING`). The age floor is a
    typo guard, not proof of abandonment, so the only thing that makes
    recovery safe is the operator knowing the workers are stopped --
    which only the operator can know, and must therefore state.
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
    the recovery. This is the cheap, human-readable first line of
    defense: it catches the common case where a worker completed a job
    between the report-only preview and the --confirm call. It is *not*
    sufficient on its own -- see `SnapshotMismatchError` and the module
    docstring's "Guard 2" section for the equal-count set swap it
    cannot detect.
    """


class ExpectedSnapshotRequiredError(RuntimeError):
    """Raised when --confirm lacks a usable --expected-snapshot.

    Covers both an absent digest and a malformed one (anything that is
    not a 64-character hex SHA-256 string, case-insensitively). A
    truncated or mistyped digest is rejected here, before the database
    is opened, rather than being allowed to fall through to
    `SnapshotMismatchError` -- otherwise an operator typo would be
    reported as "the job set changed underneath you", sending them to
    investigate a race that never happened.
    """


class SnapshotMismatchError(RuntimeError):
    """Raised when the live stale-job set differs from the reviewed one.

    Computed inside the same BEGIN IMMEDIATE transaction that performs
    the writes, over the freshly-read job set, and compared against the
    digest the operator captured from their report-only preview. This
    is the guard that actually binds the reviewed *set*: unlike the
    count check it detects a same-size swap, where one reviewed job
    leaves the stale set while a never-reviewed job enters it. On
    mismatch the transaction rolls back and nothing is written.
    """


def _utc_sql_timestamp(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    return moment.astimezone(timezone.utc).strftime(_TIMESTAMP_FORMAT)


def validate_output_path(
    database: Path,
    *,
    json_output: Path | None,
) -> None:
    """Reject a --json-output path that could clobber data.

    Must be called before the database is opened, before it is
    fingerprinted, and before any output directory is created: a
    rejected run must leave the filesystem exactly as it found it,
    creating no directories and touching no files.

    `_same_file()` is imported from `abandoned_job_audit` rather than
    reimplemented so the two tools cannot drift apart on what counts as
    "the same file": it compares resolved paths (catching identical and
    symlink-reachable paths) and additionally uses `Path.samefile()`
    (catching hard links, which have distinct paths that each resolve
    to themselves but share one inode).
    """
    if json_output is None:
        return

    if _same_file(json_output, database):
        raise OutputPathCollisionError(
            f"--json-output ({json_output}) must not be the same file "
            f"as --database ({database}); this would let the report "
            "writer overwrite the database. Note that the read-only "
            "protections on the audit connection do not extend to the "
            "report write, so this collision would destroy the "
            "database."
        )

    # os.path.lexists rather than Path.exists: a dangling symlink must
    # count as existing, because writing through it would create a file
    # at the link target -- somewhere other than the path the operator
    # named, and somewhere this validation never inspected.
    if os.path.lexists(json_output):
        raise OutputPathExistsError(
            f"--json-output ({json_output}) already exists; refusing "
            "to overwrite it. A recovery report is the only durable "
            "record of a destructive run, so replacing a previous "
            "run's report would erase that evidence. Choose a new "
            "path, or move the existing report aside deliberately."
        )


def canonical_snapshot_lines(jobs: list[dict]) -> list[str]:
    """The canonical, deterministic serialization hashed by the digest.

    Returns the version marker followed by one line per stale job,
    sorted by `job_id` ascending so the ordering can never depend on
    SQLite's row order (which `collect_stale_jobs()` sorts by activity
    timestamp, a value that ties and is therefore not a stable sort key
    on its own). Each line renders `SNAPSHOT_DIGEST_FIELDS` as
    `key=value` pairs joined by `|`; the field names are included in
    the hashed bytes so a future reordering or renaming of fields
    cannot produce a colliding digest.
    """
    lines = [SNAPSHOT_DIGEST_VERSION]

    for job in sorted(jobs, key=lambda entry: int(entry["job_id"])):
        lines.append(
            "|".join(
                f"{field}={job[field]}" for field in SNAPSHOT_DIGEST_FIELDS
            )
        )

    return lines


def compute_snapshot_digest(jobs: list[dict]) -> str:
    """SHA-256 over `canonical_snapshot_lines()`, lowercase hex.

    Deterministic for a given set of stale jobs and stable across
    processes and platforms: the lines are joined with "\\n" and given a
    trailing "\\n" (so no line is a prefix of another rendering), then
    encoded UTF-8. An empty job list still yields a digest -- that of
    the version marker alone -- so "there is nothing to recover" is a
    statement the operator can bind just as firmly as any other.
    """
    payload = "\n".join(canonical_snapshot_lines(jobs)) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_expected_snapshot(value: str | None) -> str:
    """Validate and normalize an operator-supplied digest.

    Surrounding whitespace is stripped and hex is lowercased, because
    the value is copy-pasted out of console output by a human. Anything
    that is not then a 64-character hex string is rejected as
    unusable -- see `ExpectedSnapshotRequiredError` for why malformed
    input must not be allowed to masquerade as a mismatch.
    """
    if value is None:
        raise ExpectedSnapshotRequiredError(
            "--confirm requires --expected-snapshot: the snapshot "
            "digest printed by the report-only run the operator "
            "actually reviewed. --expected-count alone cannot detect "
            "an equal-sized change to the job set (one reviewed job "
            "leaving the stale set while an unreviewed job enters "
            "it), so the digest is the guard that binds the identity "
            "of the reviewed set."
        )

    normalized = value.strip().lower()

    if (
        len(normalized) != _SNAPSHOT_DIGEST_LENGTH
        or not set(normalized) <= _HEX_CHARACTERS
    ):
        raise ExpectedSnapshotRequiredError(
            f"--expected-snapshot={value!r} is not a well-formed "
            f"SHA-256 digest ({_SNAPSHOT_DIGEST_LENGTH} hex "
            "characters). Copy the snapshot digest exactly as printed "
            "by the report-only run; a truncated or mistyped value is "
            "refused here rather than reported as a changed job set."
        )

    return normalized


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
    expected_snapshot: str | None,
    snapshot_digest: str,
    workers_stopped: bool,
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
        "expected_snapshot": expected_snapshot,
        "snapshot_digest": snapshot_digest,
        "snapshot_digest_version": SNAPSHOT_DIGEST_VERSION,
        "snapshot_digest_fields": list(SNAPSHOT_DIGEST_FIELDS),
        "workers_stopped_attested": workers_stopped,
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
        # Carried in every report, preview and applied alike: an
        # operator reading the JSON must see the liveness limitation
        # before deciding, not discover it afterwards.
        "worker_liveness_warning": WORKER_LIVENESS_WARNING,
    }

    return report


def run_recovery(
    *,
    database: Path,
    older_than_seconds: int,
    confirm: bool = False,
    expected_count: int | None = None,
    expected_snapshot: str | None = None,
    workers_stopped: bool = False,
    allow_short_window: bool = False,
    now: datetime | None = None,
    json_output: Path | None = None,
) -> dict:
    """Report on, or (guarded) apply, abandoned-job recovery.

    Report-only (confirm=False, the default): opens the database via
    `readonly_database_connection` (mode=ro + PRAGMA query_only, see
    `abandoned_job_audit`) and returns exactly what recovery would do,
    including the `snapshot_digest` the operator must pass back as
    `expected_snapshot` to confirm. Never writes; the database's
    fingerprint is checked before/after and `DatabaseMutatedError` is
    raised if it changed regardless. `expected_count`,
    `expected_snapshot` and `workers_stopped` are recorded in the
    report but not enforced in this mode -- there is nothing to guard.

    Mutating (confirm=True) requires all three attestations:

    - `workers_stopped`, because no age threshold can distinguish a
      dead worker from a slow one (`WorkersNotStoppedError`);
    - `expected_count`, the cheap readable check
      (`ExpectedCountRequiredError` / `ExpectedCountMismatchError`);
    - `expected_snapshot`, the digest that actually binds the reviewed
      set (`ExpectedSnapshotRequiredError` / `SnapshotMismatchError`).

    Confirm mode opens a writable connection, takes the write lock with
    `BEGIN IMMEDIATE` (matching `JobQueue.recover_abandoned()`'s own
    locking), re-reads the live stale-job set *inside* that
    transaction, and checks the count and then the recomputed digest
    against what the operator supplied -- rolling back and writing
    nothing on either mismatch. Otherwise it applies the identical
    per-row update `recover_abandoned()` applies, commits, and returns a
    before/after report. Any exception raised partway through the batch
    rolls back the whole transaction: no partial recovery is ever
    committed.

    `json_output` is validated against `database` before anything is
    opened, fingerprinted or created (see `validate_output_path`); a
    rejected path leaves the filesystem completely untouched.
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

    if confirm:
        # Checked first, and before the database is touched at all:
        # this is the attestation that makes every later guard
        # meaningful. If workers are live, a matching count and a
        # matching digest still only describe a moment in time.
        if not workers_stopped:
            raise WorkersNotStoppedError(WORKERS_STOPPED_ATTESTATION)

        if expected_count is None:
            raise ExpectedCountRequiredError(
                "--confirm requires --expected-count: state the exact "
                "number of stale jobs reviewed (from a prior "
                "report-only run) so this tool can refuse to mutate a "
                "set that has since changed."
            )

        expected_snapshot = _normalize_expected_snapshot(expected_snapshot)

    database = Path(database).resolve(strict=False)

    # Output-path validation runs before the existence check on the
    # database is allowed to matter and, critically, before the file is
    # opened, fingerprinted, or any report directory is created.
    validate_output_path(database, json_output=json_output)

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
            expected_snapshot=expected_snapshot,
            snapshot_digest=compute_snapshot_digest(stale_jobs),
            workers_stopped=workers_stopped,
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
        # expected-count check, the digest check, and the update below
        # all see (and act on) one consistent snapshot. Recomputing the
        # digest anywhere outside this transaction would reintroduce
        # exactly the race the digest exists to close.
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
                "pass the fresh count and snapshot digest."
            )

        actual_snapshot = compute_snapshot_digest(stale_jobs)

        if actual_snapshot != expected_snapshot:
            raise SnapshotMismatchError(
                f"--expected-snapshot={expected_snapshot} does not "
                f"match the live snapshot digest {actual_snapshot}. "
                "The stale-job set is no longer the set that was "
                "reviewed, even though its size still matches: a "
                "reviewed job may have left the set while a different, "
                "never-reviewed job entered it, or a reviewed job's "
                "attempts/status/activity timestamp changed such that "
                "recovery would now do something different to it. "
                "Nothing has been written. Re-run the report-only "
                "preview, review the fresh set, and pass its digest."
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
        expected_snapshot=expected_snapshot,
        # The digest of the set that was actually recovered, which the
        # checks above proved identical to the reviewed one.
        snapshot_digest=actual_snapshot,
        workers_stopped=workers_stopped,
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
            "Applying recovery requires --confirm together with "
            "--workers-stopped, --expected-count and "
            "--expected-snapshot, and matches "
            "JobQueue.recover_abandoned()'s own predicate and per-row "
            "update exactly."
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
            "is also passed. Age is never proof of abandonment -- see "
            "--workers-stopped."
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
        "--workers-stopped",
        action="store_true",
        help=(
            "Required together with --confirm: attest that no worker "
            "process is running against --database. This tool has no "
            "leases or heartbeats and cannot distinguish a dead worker "
            "from a legitimately slow one, so no age threshold proves "
            "abandonment -- only the operator can."
        ),
    )
    parser.add_argument(
        "--expected-count",
        type=int,
        default=None,
        help=(
            "Required together with --confirm: the exact stale-job "
            "count the operator reviewed via a prior report-only run. "
            "Recovery is refused if the live count no longer matches. "
            "A cheap first check only; --expected-snapshot is the "
            "guard that binds the reviewed set."
        ),
    )
    parser.add_argument(
        "--expected-snapshot",
        default=None,
        help=(
            "Required together with --confirm: the snapshot_digest "
            "printed by the report-only run that was reviewed. "
            "Recomputed inside the write transaction and compared; "
            "recovery is refused on any difference. Unlike "
            "--expected-count this detects an equal-sized swap, where "
            "a reviewed job leaves the stale set and an unreviewed one "
            "enters it."
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
        help=(
            "Optional path for the JSON report. Must not be the "
            "database (directly or via a symlink/hard-link alias) and "
            "must not already exist."
        ),
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
    print(f"Snapshot digest:     {output['snapshot_digest']}")
    print(f"Digest scheme:       {output['snapshot_digest_version']}")

    # Printed for previews *and* applied runs, and printed before the
    # copy-paste confirm line below so it cannot be skipped past: the
    # operator's decision to confirm depends on knowing that a stale
    # timestamp is not evidence a worker died.
    print(f"Worker liveness:     {output['worker_liveness_warning']}")

    if not output["applied"]:
        print(
            "To apply this exact reviewed set, stop all workers, then "
            "re-run with:"
        )
        print(
            "  --confirm --workers-stopped "
            f"--expected-count {output['would_recover_count']} "
            f"--expected-snapshot {output['snapshot_digest']}"
        )

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
            expected_snapshot=args.expected_snapshot,
            workers_stopped=args.workers_stopped,
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
