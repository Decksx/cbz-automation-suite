"""Strictly read-only postflight reconciliation for a completed batch.

This module automates steps 1-14 of the "Required postflight" checklist
in `docs/production_handoff_2026-07-30.md`, which an operator otherwise
runs by hand after every `hash_archive_pages_perceptual` batch. Step 15
(updating the roadmap / dev log) is a documentation step for a human
and is deliberately not attempted here.

Like every audit in this codebase's `jobs`/`archive` read-only-tooling
family (`active_job_duplicate_audit.py`, `abandoned_job_audit.py`,
`perceptual_failure_audit.py`), this command never enqueues, retries,
migrates, recovers, or otherwise mutates anything. It opens every
database it touches with SQLite's `mode=ro` URI flag plus
`PRAGMA query_only = ON`, samples `PRAGMA data_version` and a file
fingerprint (size + mtime_ns) before and after every read, and raises
rather than reports if either changed mid-run. Where an existing
module already implements a gate's read-only logic exactly --
`active_job_duplicate_audit.run_preflight` for duplicate-active
detection, `perceptual_failure_audit.run_audit` for failure
classification, `ArchivePerceptualHashRepository.count_eligible()` for
enqueue eligibility -- this module imports and calls it directly rather
than re-deriving an approximation that could drift from production
behavior.

Gate status model
-----------------

Every gate reports an explicit tri-state `status` of `"pass"`,
`"fail"`, or `"skipped"`, plus a `required` boolean saying whether the
current mode demands that gate be evaluated at all. The legacy
`pass: bool` key is retained for readability but is derived strictly
from the status (`status == "pass"`), so a gate that could not be
evaluated is *never* reported as passing. This is the whole point of
the tri-state: an earlier revision of this module encoded "no backup
was supplied, so the backup half of this gate was skipped" as a passing
gate, which let a run with no backup and no git access report
`overall_pass: true` -- exactly the outcome this tool exists to
prevent.

`overall_pass` is therefore *not* `all(gate["pass"])`. It is true only
when no gate failed **and** no gate that the current mode requires was
skipped. A top-level `summary` block carries pass/fail/skip counts plus
`failed_gates`, `skipped_gates`, and `required_gates_skipped` lists, so
an operator can see at a glance why a run did not pass.

Production mode
---------------

The command is strict by default, because it exists specifically to
gate production batches and a postflight that quietly grades itself on
a subset of the checklist is worse than no postflight at all. In the
default (production) mode:

* `--backup-database` plus `--expected-backup-size-bytes` and
  `--expected-backup-modified-time-ns` are required; omitting them
  leaves the backup gates skipped-but-required, which forces
  `overall_pass: false`.
* An undeterminable repository state (git missing, not a checkout,
  subprocess failure) is a gate *failure*, not a warning.

Non-production callers -- a developer reconciling a scratch batch on a
machine with no protected backup -- opt out explicitly and per concern
with `--allow-missing-backup` and `--allow-undeterminable-repository`.
Those flags downgrade only the affected gates to not-required; every
other gate stays strict. `--production` is available as an explicit
affirmation of intent and is mutually exclusive with both opt-outs, so
a production runbook can encode "refuse any relaxation" in the command
line itself.

Exit codes distinguish the two ways a run can fail to pass:
`EXIT_GATE_FAILURE` when a gate actively failed, and
`EXIT_REQUIRED_GATE_SKIPPED` when the run was merely incomplete.

Path safety
-----------

Every input and output path -- working database, backup database,
batch report, failure-audit JSON/CSV, and this command's own JSON
report -- is cross-validated against every other one *before* any
database is opened and before any directory is created. No output may
be the same file as an input (a failure-audit output equal to the batch
report would overwrite the very input being reconciled against), no two
outputs may collide, and no output may already exist: consistent with
the handoff document's read-only audit rule ("Always use new output
paths"), this command never silently replaces historical evidence.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from comic_automation.archive.page_hashing import (
    PAGE_HASH_ALGORITHM,
    PAGE_HASH_ALGORITHM_VERSION,
)
from comic_automation.archive.perceptual_failure_audit import (
    STABLE_CATEGORY_ORDER,
    run_audit as run_perceptual_failure_audit,
)
from comic_automation.archive.perceptual_hashing import (
    DHASH_ALGORITHM,
    DHASH_ALGORITHM_VERSION,
    PHASH_ALGORITHM,
    PHASH_ALGORITHM_VERSION,
    ArchivePerceptualHashRepository,
)
from comic_automation.database.read_guards import (
    fingerprint_database_files,
    fingerprint_report_fields,
    read_consistent_snapshot,
)
from comic_automation.jobs.active_job_duplicate_audit import (
    DatabaseChangedError,
    DatabaseIntegrityError,
    DatabaseMutatedError,
    fingerprint_database,
    quick_check,
    readonly_database_connection,
    run_preflight as run_duplicate_active_preflight,
)


JOB_TYPE = "hash_archive_pages_perceptual"

# Statuses that must not remain for this job type once a batch is
# reconciled, mirroring active_job_duplicate_audit.ACTIVE_STATUSES.
UNEXPECTED_ACTIVE_STATUSES = ("pending", "claimed", "running")

# Failure categories perceptual_failure_audit.py buckets a raw
# jobs.failure_category value into. corrupt_images/corrupt_archives are
# the two categories the handoff document treats as legitimate archive
# or image defects; the rest require investigation before a new batch
# should be trusted.
CATEGORIES_REQUIRING_INVESTIGATION = (
    "missing_files",
    "permissions",
    "unsupported_formats",
    "unclassified",
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_GATE_FAILURE = 2
# A run can fail to pass for two structurally different reasons, and an
# operator (or a wrapping script) has to be able to tell them apart
# without parsing the JSON: EXIT_GATE_FAILURE means a gate was
# evaluated and disagreed with reality, EXIT_REQUIRED_GATE_SKIPPED
# means the run was simply not complete enough to grade -- e.g. it was
# invoked in production mode without the protected backup. The former
# demands investigation of the batch; the latter demands re-running the
# postflight with the missing inputs.
EXIT_REQUIRED_GATE_SKIPPED = 3

# Gate statuses. "skipped" exists so that "this gate was not evaluated"
# can never be confused with "this gate was evaluated and passed".
GATE_PASS = "pass"
GATE_FAIL = "fail"
GATE_SKIPPED = "skipped"

BACKUP_NOT_SUPPLIED_REASON = (
    "No --backup-database was supplied, so this gate could not be "
    "evaluated. Step 10-12 of the required postflight checklist cover "
    "the protected backup; in production mode this leaves the run "
    "incomplete and overall_pass false."
)

BACKUP_FINGERPRINT_MISSING_REASON = (
    "--backup-database, --expected-backup-size-bytes and "
    "--expected-backup-modified-time-ns must all be supplied to verify "
    "the protected backup fingerprint (checklist step 12). In "
    "production mode this leaves the run incomplete and overall_pass "
    "false."
)

# Exceptions that mean "this database (or connection) could not be read
# trustworthily right now" -- a corrupt file, a mid-run write, a missing
# file. Every gate that depends on actually reading a database catches
# exactly this set and turns it into a failed gate with an error detail,
# rather than letting it crash the whole postflight run: a single
# corrupt or racing database should show up as one specific gate
# failure in the report, not an unhandled exception that hides every
# other gate's result.
READ_FAILURE_EXCEPTIONS = (
    DatabaseIntegrityError,
    DatabaseChangedError,
    sqlite3.DatabaseError,
    sqlite3.OperationalError,
    FileNotFoundError,
)


class PostflightError(RuntimeError):
    """Base class for conditions that invalidate a postflight run."""


class OutputPathCollisionError(PostflightError):
    """A requested output path could clobber an input or another output."""


class OutputPathExistsError(PostflightError):
    """A requested output path already exists.

    Refused rather than overwritten. Postflight artifacts are the
    evidence that a batch reconciled; the handoff document's read-only
    audit section instructs operators to "Always use new output paths",
    and silently replacing a previous run's report would destroy the
    record of what the previous run saw.
    """


def _same_file(first: Path, second: Path) -> bool:
    if first.resolve(strict=False) == second.resolve(strict=False):
        return True

    if first.exists() and second.exists():
        try:
            return first.samefile(second)
        except OSError:
            return False

    return False


def _validate_paths(
    *,
    inputs: Sequence[tuple[str, Path | None]],
    outputs: Sequence[tuple[str, Path | None]],
) -> None:
    """Cross-validate every input and output path this run will touch.

    Called before any database is opened, any report is read, and any
    directory is created, so a rejected run leaves the filesystem
    exactly as it found it.

    Three rules, each labelled with the CLI flag that supplied the path
    so the error names the actual mistake:

    1. No output may be the same file as any input. The working
       database and protected backup are the obvious cases, but the
       batch report matters just as much: a failure-audit JSON path
       equal to `--batch-report` would overwrite the very input this
       command reconciles against, and the resulting report would be
       "reconciled" against its own output.
    2. No two outputs may be the same file, or the second write would
       silently discard the first.
    3. No output may already exist. See `OutputPathExistsError`.
    """
    resolved_inputs = [
        (label, path) for label, path in inputs if path is not None
    ]
    resolved_outputs = [
        (label, path) for label, path in outputs if path is not None
    ]

    for output_label, output in resolved_outputs:
        for input_label, source in resolved_inputs:
            if _same_file(output, source):
                raise OutputPathCollisionError(
                    f"Output path {output_label} ({output}) must not be "
                    f"the same file as the input {input_label} "
                    f"({source})."
                )

    for index, (first_label, first) in enumerate(resolved_outputs):
        for second_label, second in resolved_outputs[index + 1 :]:
            if _same_file(first, second):
                raise OutputPathCollisionError(
                    f"Output paths must not collide: {first_label} "
                    f"({first}) == {second_label} ({second})."
                )

    for output_label, output in resolved_outputs:
        if output.exists():
            raise OutputPathExistsError(
                f"Output path {output_label} ({output}) already exists. "
                "This command never overwrites an existing report: "
                "always use a new output path so previous evidence is "
                "preserved."
            )


def _gate(
    name: str,
    status: str,
    detail: dict[str, Any],
    *,
    required: bool = True,
) -> dict[str, Any]:
    """Build one gate entry.

    `pass` is derived from `status` and kept only for readability of
    the rendered JSON; it is deliberately impossible to construct a
    gate that was skipped yet reports `pass: True`.

    `required` records whether the *current mode* demands this gate be
    evaluated, so `overall_pass` can distinguish "skipped and that is
    fine" from "skipped and that invalidates the run" without
    re-deriving the mode rules at the bottom of the function.
    """
    if status not in (GATE_PASS, GATE_FAIL, GATE_SKIPPED):
        raise ValueError(f"Unknown gate status: {status!r}")

    return {
        "name": name,
        "status": status,
        "pass": status == GATE_PASS,
        "required": bool(required),
        "detail": detail,
    }


def _boolean_gate(
    name: str,
    passed: bool,
    detail: dict[str, Any],
    *,
    required: bool = True,
) -> dict[str, Any]:
    """A gate that was actually evaluated and either passed or failed."""
    return _gate(
        name,
        GATE_PASS if passed else GATE_FAIL,
        detail,
        required=required,
    )


def load_batch_report(path: Path) -> dict:
    resolved = Path(path).resolve(strict=True)

    with resolved.open("r", encoding="utf-8") as stream:
        return json.load(stream)


@dataclass(frozen=True)
class GitRepositoryState:
    determinable: bool
    clean: bool | None
    head: str | None
    warning: str | None


def _read_repository_state(repository: Path) -> GitRepositoryState:
    """Best-effort `git status`/`git rev-parse HEAD` read.

    Per spec, "cannot determine" (git missing, repository not a git
    checkout, subprocess failure, timeout) is reported as a warning
    rather than raised: this tool may run against a checkout it has no
    git access to, and that must not crash an otherwise-valid
    postflight run.
    """
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repository,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return GitRepositoryState(
            determinable=False,
            clean=None,
            head=None,
            warning=f"Could not run git against {repository}: {exc}",
        )

    if status.returncode != 0 or head.returncode != 0:
        return GitRepositoryState(
            determinable=False,
            clean=None,
            head=None,
            warning=(
                f"git exited non-zero against {repository}: "
                f"status.returncode={status.returncode}, "
                f"head.returncode={head.returncode}, "
                f"stderr={(status.stderr or head.stderr).strip()!r}"
            ),
        )

    return GitRepositoryState(
        determinable=True,
        clean=(status.stdout.strip() == ""),
        head=head.stdout.strip(),
        warning=None,
    )


def _snapshot_working_database(database: Path) -> dict[str, Any]:
    """One consistent read-only snapshot of every DB-derived gate input.

    Delegates to `read_guards.read_consistent_snapshot`, which is the
    shared implementation of the sequence this module used to inline:
    sample `PRAGMA data_version` before opening the transaction that
    encompasses every read (including quick_check), then again after,
    and raise `DatabaseChangedError` if it moved. Every number that a
    gate compares against an expected value therefore comes from a
    single instant, so gates can never disagree with each other because
    a writer landed between two of this module's own queries.

    The file fingerprint is still taken and still raises on a change,
    but it is the weaker detector and is checked second: under WAL a
    concurrent commit lands in the `-wal` sidecar and can leave the
    main file byte-identical, so it can never be the gate.
    """
    resolved = Path(database).resolve(strict=True)
    fingerprint_before = fingerprint_database(resolved)

    files_before = fingerprint_database_files(resolved)

    def read(connection: sqlite3.Connection) -> dict[str, Any]:
        active_count = connection.execute(
            f"""
            SELECT COUNT(*)
            FROM jobs
            WHERE job_type = ?
              AND status IN (
                  {",".join("?" for _ in UNEXPECTED_ACTIVE_STATUSES)}
              )
            """,
            (JOB_TYPE, *UNEXPECTED_ACTIVE_STATUSES),
        ).fetchone()[0]

        total_job_population = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE job_type = ?",
            (JOB_TYPE,),
        ).fetchone()[0]

        completed_count = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE job_type = ? AND status = 'completed'",
            (JOB_TYPE,),
        ).fetchone()[0]

        failed_count = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE job_type = ? AND status = 'failed'",
            (JOB_TYPE,),
        ).fetchone()[0]

        eligible_remaining = ArchivePerceptualHashRepository(
            connection
        ).count_eligible()

        dhash_count = connection.execute(
            "SELECT COUNT(*) FROM page_hashes WHERE algorithm = ? AND algorithm_version = ?",
            (DHASH_ALGORITHM, DHASH_ALGORITHM_VERSION),
        ).fetchone()[0]

        phash_count = connection.execute(
            "SELECT COUNT(*) FROM page_hashes WHERE algorithm = ? AND algorithm_version = ?",
            (PHASH_ALGORITHM, PHASH_ALGORITHM_VERSION),
        ).fetchone()[0]

        page_sha256_count = connection.execute(
            "SELECT COUNT(*) FROM page_hashes WHERE algorithm = ? AND algorithm_version = ?",
            (PAGE_HASH_ALGORITHM, PAGE_HASH_ALGORITHM_VERSION),
        ).fetchone()[0]

        near_duplicate_count = connection.execute(
            "SELECT COUNT(*) FROM near_duplicate_candidates"
        ).fetchone()[0]

        return {
            "active_job_count": int(active_count),
            "total_job_population": int(total_job_population),
            "completed_count": int(completed_count),
            "failed_count": int(failed_count),
            "eligible_remaining": int(eligible_remaining),
            "dhash_v1_count": int(dhash_count),
            "phash_v1_count": int(phash_count),
            "page_sha256_count": int(page_sha256_count),
            "near_duplicate_count": int(near_duplicate_count),
        }

    snapshot = read_consistent_snapshot(
        resolved,
        read,
        context="postflight",
        integrity_check=quick_check,
    )

    fingerprint_after = fingerprint_database(resolved)
    files_after = fingerprint_database_files(resolved)

    # Checked after the data_version gate inside the helper, which is
    # the stronger detector. This one only catches a checkpoint or this
    # process touching the file; it cannot see a WAL commit.
    if fingerprint_before != fingerprint_after:
        raise DatabaseMutatedError(
            "Working database file changed during postflight: "
            f"before={fingerprint_before} after={fingerprint_after}."
        )

    return {
        "database": str(resolved),
        "quick_check": snapshot.quick_check,
        **snapshot.result,
        "fingerprint_before": fingerprint_before,
        "fingerprint_after": fingerprint_after,
        "files_before": files_before,
        "files_after": files_after,
        "data_version_before": snapshot.data_version_before,
        "data_version_after": snapshot.data_version_after,
    }


def run_postflight(
    *,
    database: Path,
    batch_report: Path,
    expected_processed: int,
    expected_enqueued: int,
    expected_job_population_before: int,
    expected_completed_before: int,
    expected_failed_before: int,
    expected_eligible_remaining: int,
    expected_page_sha256_count: int,
    backup_database: Path | None = None,
    expected_job_population_after: int | None = None,
    acknowledge_retry_scheduled: bool = False,
    expected_hash_rows_before: int | None = None,
    expected_near_duplicate_count: int = 0,
    expected_backup_size_bytes: int | None = None,
    expected_backup_modified_time_ns: int | None = None,
    expected_commit: str | None = None,
    repository: Path | None = None,
    failure_audit_json_output: Path | None = None,
    failure_audit_csv_output: Path | None = None,
    report_json_output: Path | None = None,
    allow_missing_backup: bool = False,
    allow_undeterminable_repository: bool = False,
) -> dict[str, Any]:
    """Run every automated postflight gate and return one JSON report.

    Strict (production) by default: see the module docstring for what
    `allow_missing_backup` and `allow_undeterminable_repository`
    relax and why the defaults are the way round they are.

    `report_json_output` is not written here -- `main` writes it after
    this function returns -- but it is accepted so that it can be
    cross-validated against every other path *before* any database is
    opened. Validating it in `main` after the run would be too late:
    the failure audit would already have written its own outputs.
    """
    database = Path(database).resolve(strict=False)

    resolved_backup = (
        Path(backup_database).resolve(strict=False)
        if backup_database is not None
        else None
    )
    resolved_batch_report = Path(batch_report).resolve(strict=False)
    resolved_failure_audit_json = (
        Path(failure_audit_json_output).resolve(strict=False)
        if failure_audit_json_output is not None
        else None
    )
    resolved_failure_audit_csv = (
        Path(failure_audit_csv_output).resolve(strict=False)
        if failure_audit_csv_output is not None
        else None
    )
    resolved_report_json = (
        Path(report_json_output).resolve(strict=False)
        if report_json_output is not None
        else None
    )

    # First thing this function does, before any stat-for-existence,
    # any database open, and any directory creation: prove that no path
    # this run touches can clobber another.
    _validate_paths(
        inputs=[
            ("--database", database),
            ("--backup-database", resolved_backup),
            ("--batch-report", resolved_batch_report),
        ],
        outputs=[
            ("--failure-audit-json-output", resolved_failure_audit_json),
            ("--failure-audit-csv-output", resolved_failure_audit_csv),
            ("--json-output", resolved_report_json),
        ],
    )

    if not database.is_file():
        raise FileNotFoundError(f"Database does not exist: {database}")

    # A backup path that was supplied but does not exist is an operator
    # error, not a gate result: the run cannot check what it was told
    # to check. (Omitting the backup entirely is different -- that is
    # reported as skipped-and-required, see the backup gates below.)
    if resolved_backup is not None and not resolved_backup.is_file():
        raise FileNotFoundError(
            f"Backup database does not exist: {resolved_backup}"
        )

    # Mode. Requiredness is decided once, here, and then handed to each
    # gate, so the rules live in one place instead of being re-derived
    # where overall_pass is computed.
    backup_gates_required = not allow_missing_backup
    repository_determinable_required = not allow_undeterminable_repository
    production_mode = (
        backup_gates_required and repository_determinable_required
    )

    report = load_batch_report(resolved_batch_report)

    gates: dict[str, dict[str, Any]] = {}

    # --- Gate 1: processed count -------------------------------------
    reported_processed = report.get("processed")
    gates["batch_report_processed"] = _boolean_gate(
        "batch_report_processed",
        reported_processed == expected_processed,
        {
            "reported_processed": reported_processed,
            "expected_processed": expected_processed,
            "batch_report_path": str(Path(batch_report).resolve(strict=False)),
        },
    )

    # --- Gate 2: succeeded + terminally_failed + retry == processed --
    succeeded = report.get("succeeded")
    terminally_failed = report.get("terminally_failed")
    retry_scheduled = report.get("retry_scheduled")
    outcome_sum = None
    if None not in (succeeded, terminally_failed, retry_scheduled):
        outcome_sum = succeeded + terminally_failed + retry_scheduled

    gates["batch_report_outcome_reconciliation"] = _boolean_gate(
        "batch_report_outcome_reconciliation",
        outcome_sum is not None and outcome_sum == reported_processed,
        {
            "succeeded": succeeded,
            "terminally_failed": terminally_failed,
            "retry_scheduled": retry_scheduled,
            "outcome_sum": outcome_sum,
            "reported_processed": reported_processed,
        },
    )

    # --- Snapshot the working database for every remaining DB gate ---
    # A read failure here (corruption, a mid-run write) must not crash
    # the whole postflight run: it is reported as the quick_check gate
    # failing, and every other DB-derived gate below is forced to fail
    # too, since none of their numbers can be trusted from a database
    # that could not be read.
    snapshot_error: str | None = None
    try:
        snapshot = _snapshot_working_database(database)
    except READ_FAILURE_EXCEPTIONS as exc:
        snapshot_error = f"{type(exc).__name__}: {exc}"
        stat_fingerprint = fingerprint_database(database)
        snapshot = {
            "database": str(database),
            "quick_check": None,
            "data_version_before": None,
            "data_version_after": None,
            "files_before": fingerprint_database_files(database),
            "files_after": fingerprint_database_files(database),
            "active_job_count": None,
            "total_job_population": None,
            "completed_count": None,
            "failed_count": None,
            "eligible_remaining": None,
            "dhash_v1_count": None,
            "phash_v1_count": None,
            "page_sha256_count": None,
            "near_duplicate_count": None,
            "fingerprint_before": stat_fingerprint,
            "fingerprint_after": fingerprint_database(database),
        }

    # --- Gate 3: enqueued count + total job population ---------------
    reported_enqueued = report.get("enqueued")
    expected_population_after = (
        expected_job_population_after
        if expected_job_population_after is not None
        else expected_job_population_before + expected_enqueued
    )
    enqueue_gate_pass = reported_enqueued == expected_enqueued and (
        snapshot["total_job_population"] == expected_population_after
    )
    gates["enqueue_and_population"] = _boolean_gate(
        "enqueue_and_population",
        enqueue_gate_pass,
        {
            "reported_enqueued": reported_enqueued,
            "expected_enqueued": expected_enqueued,
            "expected_job_population_before": expected_job_population_before,
            "expected_job_population_after": expected_population_after,
            "actual_total_job_population": snapshot["total_job_population"],
        },
    )

    # --- Gate 4: cumulative completed/failed reconciliation ----------
    expected_completed_after = None
    expected_failed_after = None
    cumulative_pass = False
    if succeeded is not None and terminally_failed is not None:
        expected_completed_after = expected_completed_before + succeeded
        expected_failed_after = expected_failed_before + terminally_failed
        cumulative_pass = (
            snapshot["completed_count"] == expected_completed_after
            and snapshot["failed_count"] == expected_failed_after
        )
    gates["cumulative_outcome_reconciliation"] = _boolean_gate(
        "cumulative_outcome_reconciliation",
        cumulative_pass,
        {
            "expected_completed_before": expected_completed_before,
            "expected_failed_before": expected_failed_before,
            "expected_completed_after": expected_completed_after,
            "expected_failed_after": expected_failed_after,
            "actual_completed_count": snapshot["completed_count"],
            "actual_failed_count": snapshot["failed_count"],
        },
    )

    # --- Gate 5: no unexpected active jobs ----------------------------
    allowed_active = (
        (retry_scheduled or 0) if acknowledge_retry_scheduled else 0
    )
    gates["no_unexpected_active_jobs"] = _boolean_gate(
        "no_unexpected_active_jobs",
        snapshot["active_job_count"] == allowed_active,
        {
            "job_type": JOB_TYPE,
            "audited_statuses": list(UNEXPECTED_ACTIVE_STATUSES),
            "actual_active_job_count": snapshot["active_job_count"],
            "allowed_active_job_count": allowed_active,
            "acknowledge_retry_scheduled": acknowledge_retry_scheduled,
            "retry_scheduled_reported": retry_scheduled,
        },
    )

    # --- Gate 6: eligibility recount ----------------------------------
    gates["eligibility_recount"] = _boolean_gate(
        "eligibility_recount",
        snapshot["eligible_remaining"] == expected_eligible_remaining,
        {
            "actual_eligible_remaining": snapshot["eligible_remaining"],
            "expected_eligible_remaining": expected_eligible_remaining,
            "source": (
                "ArchivePerceptualHashRepository.count_eligible() "
                "(same predicate as enqueue_missing())"
            ),
        },
    )

    # --- Gate 7: dHash/pHash alignment + delta reconciliation --------
    aligned = snapshot["dhash_v1_count"] == snapshot["phash_v1_count"]
    profiled_pages = None
    phase_timing = report.get("phase_timing")
    if isinstance(phase_timing, dict):
        profiled_pages = phase_timing.get("profiled_pages")

    delta_detail: dict[str, Any] = {
        "expected_hash_rows_before": expected_hash_rows_before,
        "profiled_pages": profiled_pages,
    }
    delta_pass = True
    if expected_hash_rows_before is not None:
        dhash_delta = snapshot["dhash_v1_count"] - expected_hash_rows_before
        phash_delta = snapshot["phash_v1_count"] - expected_hash_rows_before
        delta_detail["dhash_delta"] = dhash_delta
        delta_detail["phash_delta"] = phash_delta
        if profiled_pages is None:
            delta_pass = False
            delta_detail["reason"] = (
                "expected_hash_rows_before was given but the batch "
                "report has no phase_timing.profiled_pages to reconcile "
                "the delta against (batch must have been run with "
                "--profile)."
            )
        else:
            delta_pass = (
                dhash_delta == profiled_pages and phash_delta == profiled_pages
            )

    gates["hash_alignment"] = _boolean_gate(
        "hash_alignment",
        aligned and delta_pass,
        {
            "dhash_v1_count": snapshot["dhash_v1_count"],
            "phash_v1_count": snapshot["phash_v1_count"],
            "aligned": aligned,
            **delta_detail,
        },
    )

    # --- Gate 8: page SHA-256 row count --------------------------------
    gates["page_sha256_count"] = _boolean_gate(
        "page_sha256_count",
        snapshot["page_sha256_count"] == expected_page_sha256_count,
        {
            "actual_page_sha256_count": snapshot["page_sha256_count"],
            "expected_page_sha256_count": expected_page_sha256_count,
        },
    )

    # --- Gate 9: near-duplicate candidates ------------------------------
    gates["near_duplicate_candidates"] = _boolean_gate(
        "near_duplicate_candidates",
        snapshot["near_duplicate_count"] == expected_near_duplicate_count,
        {
            "actual_near_duplicate_count": snapshot["near_duplicate_count"],
            "expected_near_duplicate_count": expected_near_duplicate_count,
        },
    )

    # --- Gate 10a: quick_check on the working database -----------------
    # Checklist step 10 covers two databases, and they are reported as
    # two gates rather than one. A single gate could only carry one
    # status, which is precisely how "the backup half was skipped"
    # previously hid inside a passing gate.
    quick_check_detail: dict[str, Any] = {
        "database": str(database),
        "quick_check": snapshot["quick_check"],
    }
    if snapshot_error is not None:
        quick_check_detail["error"] = snapshot_error

    gates["quick_check"] = _boolean_gate(
        "quick_check", snapshot["quick_check"] == "ok", quick_check_detail
    )

    # --- Gate 10b: quick_check on the protected backup -----------------
    if resolved_backup is None:
        gates["backup_quick_check"] = _gate(
            "backup_quick_check",
            GATE_SKIPPED,
            {"reason": BACKUP_NOT_SUPPLIED_REASON},
            required=backup_gates_required,
        )
    else:
        try:
            with readonly_database_connection(resolved_backup) as connection:
                backup_integrity = quick_check(connection)
            gates["backup_quick_check"] = _boolean_gate(
                "backup_quick_check",
                backup_integrity == "ok",
                {
                    "database": str(resolved_backup),
                    "quick_check": backup_integrity,
                },
            )
        except READ_FAILURE_EXCEPTIONS as exc:
            gates["backup_quick_check"] = _boolean_gate(
                "backup_quick_check",
                False,
                {
                    "database": str(resolved_backup),
                    "quick_check": None,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )

    # --- Gate 11a: active-job duplicate audit on the working DB -------
    try:
        working_duplicate_report = run_duplicate_active_preflight(
            database=database
        )
        gates["active_job_duplicate_audit"] = _boolean_gate(
            "active_job_duplicate_audit",
            working_duplicate_report["blocking_group_count"] == 0
            and working_duplicate_report["unique_active_index_exists"],
            {
                "database": str(database),
                "blocking_group_count": working_duplicate_report[
                    "blocking_group_count"
                ],
                "unique_active_index_exists": working_duplicate_report[
                    "unique_active_index_exists"
                ],
            },
        )
    except READ_FAILURE_EXCEPTIONS as exc:
        gates["active_job_duplicate_audit"] = _boolean_gate(
            "active_job_duplicate_audit",
            False,
            {
                "database": str(database),
                "error": f"{type(exc).__name__}: {exc}",
            },
        )

    # --- Gate 11b: active-job duplicate audit on the backup -----------
    if resolved_backup is None:
        gates["backup_active_job_duplicate_audit"] = _gate(
            "backup_active_job_duplicate_audit",
            GATE_SKIPPED,
            {"reason": BACKUP_NOT_SUPPLIED_REASON},
            required=backup_gates_required,
        )
    else:
        try:
            backup_duplicate_report = run_duplicate_active_preflight(
                database=resolved_backup
            )
            gates["backup_active_job_duplicate_audit"] = _boolean_gate(
                "backup_active_job_duplicate_audit",
                backup_duplicate_report["blocking_group_count"] == 0
                and backup_duplicate_report["unique_active_index_exists"],
                {
                    "database": str(resolved_backup),
                    "blocking_group_count": backup_duplicate_report[
                        "blocking_group_count"
                    ],
                    "unique_active_index_exists": backup_duplicate_report[
                        "unique_active_index_exists"
                    ],
                },
            )
        except READ_FAILURE_EXCEPTIONS as exc:
            gates["backup_active_job_duplicate_audit"] = _boolean_gate(
                "backup_active_job_duplicate_audit",
                False,
                {
                    "database": str(resolved_backup),
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )

    # --- Gate 12: backup fingerprint -----------------------------------
    # Needs all three inputs to mean anything: the backup itself plus
    # both expected values from the handoff document. With any of them
    # missing the gate is skipped, never assumed good.
    if (
        resolved_backup is not None
        and expected_backup_size_bytes is not None
        and expected_backup_modified_time_ns is not None
    ):
        actual_backup_fingerprint = fingerprint_database(resolved_backup)
        gates["backup_fingerprint"] = _boolean_gate(
            "backup_fingerprint",
            actual_backup_fingerprint.size_bytes == expected_backup_size_bytes
            and actual_backup_fingerprint.modified_time_ns
            == expected_backup_modified_time_ns,
            {
                "database": str(resolved_backup),
                "actual_size_bytes": actual_backup_fingerprint.size_bytes,
                "expected_size_bytes": expected_backup_size_bytes,
                "actual_modified_time_ns": (
                    actual_backup_fingerprint.modified_time_ns
                ),
                "expected_modified_time_ns": (
                    expected_backup_modified_time_ns
                ),
            },
        )
    else:
        gates["backup_fingerprint"] = _gate(
            "backup_fingerprint",
            GATE_SKIPPED,
            {
                "reason": BACKUP_FINGERPRINT_MISSING_REASON,
                "backup_database": (
                    str(resolved_backup)
                    if resolved_backup is not None
                    else None
                ),
                "expected_size_bytes": expected_backup_size_bytes,
                "expected_modified_time_ns": (
                    expected_backup_modified_time_ns
                ),
            },
            required=backup_gates_required,
        )

    # --- Gate 13: repository (git) state --------------------------------
    repository_path = (
        Path(repository).resolve(strict=False)
        if repository is not None
        else REPOSITORY_ROOT
    )
    git_state = _read_repository_state(repository_path)

    if not git_state.determinable:
        # "Cannot determine" means checklist step 13 was not performed.
        # In production mode that is a failure: a production postflight
        # that cannot see whether the deployed tree is clean and at the
        # expected revision has not verified the thing step 13 asks
        # about, and treating that as a pass is how an unreviewed
        # working-tree change rides along with a "reconciled" batch.
        # Outside production mode the same condition is a skip with a
        # warning, since a developer may legitimately be running this
        # against a checkout git cannot read.
        gates["repository_state"] = _gate(
            "repository_state",
            GATE_FAIL if repository_determinable_required else GATE_SKIPPED,
            {
                "determinable": False,
                "warning": git_state.warning,
                "repository": str(repository_path),
            },
            required=repository_determinable_required,
        )
    else:
        gates["repository_state"] = _boolean_gate(
            "repository_state",
            git_state.clean is True
            and (
                expected_commit is None or git_state.head == expected_commit
            ),
            {
                "determinable": True,
                "repository": str(repository_path),
                "clean": git_state.clean,
                "head": git_state.head,
                "expected_commit": expected_commit,
            },
        )

    # --- Gate 14: fresh perceptual-failure audit ------------------------
    try:
        failure_report = run_perceptual_failure_audit(
            database=database,
            json_output=failure_audit_json_output,
            csv_output=failure_audit_csv_output,
        )
        stable_counts = failure_report["stable_category_counts"]
        needs_investigation = {
            category: stable_counts.get(category, 0)
            for category in CATEGORIES_REQUIRING_INVESTIGATION
            if stable_counts.get(category, 0) > 0
        }
        gates["perceptual_failure_audit"] = _boolean_gate(
            "perceptual_failure_audit",
            len(needs_investigation) == 0,
            {
                "terminal_failure_count": (
                    failure_report["terminal_failure_count"]
                ),
                "stable_category_counts": stable_counts,
                "categories_requiring_investigation": (
                    list(CATEGORIES_REQUIRING_INVESTIGATION)
                ),
                "categories_needing_investigation_now": needs_investigation,
                "json_output": failure_report.get("json_output"),
                "csv_output": failure_report.get("csv_output"),
            },
        )
    except READ_FAILURE_EXCEPTIONS as exc:
        gates["perceptual_failure_audit"] = _boolean_gate(
            "perceptual_failure_audit",
            False,
            {"error": f"{type(exc).__name__}: {exc}"},
        )

    # A read failure while snapshotting the working database means none
    # of the counts gates 3-9 depend on can be trusted, however they
    # individually evaluated (a None-vs-None comparison could otherwise
    # accidentally read as "pass"). Force them to fail explicitly and
    # attach the same error for context.
    if snapshot_error is not None:
        for name in (
            "enqueue_and_population",
            "cumulative_outcome_reconciliation",
            "no_unexpected_active_jobs",
            "eligibility_recount",
            "hash_alignment",
            "page_sha256_count",
            "near_duplicate_candidates",
        ):
            gates[name]["status"] = GATE_FAIL
            gates[name]["pass"] = False
            gates[name]["detail"]["snapshot_error"] = snapshot_error

    # overall_pass is deliberately NOT all(gate["pass"]). A gate that
    # was never evaluated must not be able to contribute to a passing
    # run when the current mode required it, and it must not silently
    # sink a run when the operator explicitly opted out of it.
    failed_gates = sorted(
        name for name, gate in gates.items() if gate["status"] == GATE_FAIL
    )
    skipped_gates = sorted(
        name for name, gate in gates.items() if gate["status"] == GATE_SKIPPED
    )
    required_gates_skipped = sorted(
        name for name in skipped_gates if gates[name]["required"]
    )
    passed_gates = sorted(
        name for name, gate in gates.items() if gate["status"] == GATE_PASS
    )
    overall_pass = not failed_gates and not required_gates_skipped

    summary = {
        "gate_count": len(gates),
        "passed_count": len(passed_gates),
        "failed_count": len(failed_gates),
        "skipped_count": len(skipped_gates),
        "failed_gates": failed_gates,
        "skipped_gates": skipped_gates,
        # The single most important line for an operator staring at a
        # false overall_pass with no failed gate: these are the gates
        # the run was supposed to evaluate and could not.
        "required_gates_skipped": required_gates_skipped,
    }

    return {
        "database": str(database),
        "backup_database": (
            str(resolved_backup) if resolved_backup is not None else None
        ),
        "batch_report_path": str(resolved_batch_report),
        "mode": {
            "production": production_mode,
            "allow_missing_backup": bool(allow_missing_backup),
            "allow_undeterminable_repository": bool(
                allow_undeterminable_repository
            ),
        },
        "overall_pass": overall_pass,
        "summary": summary,
        "gates": gates,
        "database_fingerprint_before": asdict(snapshot["fingerprint_before"]),
        "database_fingerprint_after": asdict(snapshot["fingerprint_after"]),
        "data_version_before": snapshot["data_version_before"],
        "data_version_after": snapshot["data_version_after"],
        # `database_unchanged` keeps its historical name and value, but
        # is emitted through the shared helper so it always travels
        # with the note explaining that it is diagnostic evidence only:
        # the concurrency gate for every DB-derived number above is the
        # data_version pair, enforced inside
        # `_snapshot_working_database`.
        **fingerprint_report_fields(
            fingerprint_before=snapshot["fingerprint_before"],
            fingerprint_after=snapshot["fingerprint_after"],
            files_before=snapshot["files_before"],
            files_after=snapshot["files_after"],
        ),
    }


def render_json(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Strictly read-only postflight reconciliation for a "
            "completed hash_archive_pages_perceptual batch. Automates "
            "steps 1-14 of docs/production_handoff_2026-07-30.md's "
            "'Required postflight' checklist. Never enqueues, retries, "
            "migrates, or otherwise mutates any database. Strict "
            "(production) by default: the protected backup and its "
            "expected fingerprint are required, and an undeterminable "
            "repository state is a failure. Relax individual gates "
            "only with the explicit --allow-* flags below."
        )
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--backup-database", type=Path, default=None)
    parser.add_argument("--batch-report", type=Path, required=True)
    parser.add_argument("--expected-processed", type=int, required=True)
    parser.add_argument("--expected-enqueued", type=int, required=True)
    parser.add_argument(
        "--expected-job-population-before", type=int, required=True
    )
    parser.add_argument(
        "--expected-job-population-after", type=int, default=None
    )
    parser.add_argument("--expected-completed-before", type=int, required=True)
    parser.add_argument("--expected-failed-before", type=int, required=True)
    parser.add_argument(
        "--acknowledge-retry-scheduled",
        action="store_true",
        help=(
            "Permit up to retry_scheduled pending jobs of this job type "
            "instead of requiring zero active jobs."
        ),
    )
    parser.add_argument(
        "--expected-eligible-remaining", type=int, required=True
    )
    parser.add_argument("--expected-hash-rows-before", type=int, default=None)
    parser.add_argument(
        "--expected-page-sha256-count", type=int, required=True
    )
    parser.add_argument(
        "--expected-near-duplicate-count", type=int, default=0
    )
    parser.add_argument(
        "--expected-backup-size-bytes", type=int, default=None
    )
    parser.add_argument(
        "--expected-backup-modified-time-ns", type=int, default=None
    )
    parser.add_argument("--expected-commit", type=str, default=None)
    parser.add_argument("--repository", type=Path, default=None)
    parser.add_argument("--failure-audit-json-output", type=Path, default=None)
    parser.add_argument("--failure-audit-csv-output", type=Path, default=None)
    parser.add_argument("--json-output", type=Path, default=None)
    parser.add_argument(
        "--production",
        action="store_true",
        help=(
            "Explicitly affirm that this is a production postflight. "
            "Strict behavior is already the default; passing this flag "
            "additionally makes the command refuse any --allow-* "
            "relaxation, so a runbook can encode that intent in the "
            "command line itself."
        ),
    )
    parser.add_argument(
        "--allow-missing-backup",
        action="store_true",
        help=(
            "Non-production only. Treat the protected-backup gates "
            "(quick_check, duplicate audit, fingerprint) as optional "
            "rather than required, so omitting --backup-database "
            "reports them as skipped without forcing overall_pass "
            "false. Never use this to gate a production batch."
        ),
    )
    parser.add_argument(
        "--allow-undeterminable-repository",
        action="store_true",
        help=(
            "Non-production only. Treat an undeterminable git "
            "repository state (git missing, not a checkout) as a "
            "skipped gate with a warning instead of a failure."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # --production is an assertion, not a mode switch (strict is the
    # default), so combining it with a relaxation is a contradiction
    # the operator has to resolve rather than something to silently
    # rank one way or the other.
    if args.production and (
        args.allow_missing_backup or args.allow_undeterminable_repository
    ):
        parser.error(
            "--production cannot be combined with --allow-missing-backup "
            "or --allow-undeterminable-repository."
        )

    try:
        report = run_postflight(
            database=args.database,
            backup_database=args.backup_database,
            batch_report=args.batch_report,
            expected_processed=args.expected_processed,
            expected_enqueued=args.expected_enqueued,
            expected_job_population_before=(
                args.expected_job_population_before
            ),
            expected_job_population_after=(
                args.expected_job_population_after
            ),
            expected_completed_before=args.expected_completed_before,
            expected_failed_before=args.expected_failed_before,
            acknowledge_retry_scheduled=args.acknowledge_retry_scheduled,
            expected_eligible_remaining=args.expected_eligible_remaining,
            expected_hash_rows_before=args.expected_hash_rows_before,
            expected_page_sha256_count=args.expected_page_sha256_count,
            expected_near_duplicate_count=args.expected_near_duplicate_count,
            expected_backup_size_bytes=args.expected_backup_size_bytes,
            expected_backup_modified_time_ns=(
                args.expected_backup_modified_time_ns
            ),
            expected_commit=args.expected_commit,
            repository=args.repository,
            failure_audit_json_output=args.failure_audit_json_output,
            failure_audit_csv_output=args.failure_audit_csv_output,
            # Passed for cross-validation only; written below, after
            # every gate has run and the path has been proven not to
            # collide with any input or other output.
            report_json_output=args.json_output,
            allow_missing_backup=args.allow_missing_backup,
            allow_undeterminable_repository=(
                args.allow_undeterminable_repository
            ),
        )
    except Exception as exc:
        payload = {"error": type(exc).__name__, "message": str(exc)}
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return EXIT_FAILURE

    rendered = render_json(report)

    if args.json_output is not None:
        resolved = args.json_output.resolve(strict=False)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(rendered + "\n", encoding="utf-8")

    print(rendered)

    # An actively failing gate outranks an incomplete run: if something
    # disagreed with reality the operator needs to know that first,
    # even if the run was also missing an input.
    if report["summary"]["failed_gates"]:
        return EXIT_GATE_FAILURE

    if report["summary"]["required_gates_skipped"]:
        return EXIT_REQUIRED_GATE_SKIPPED

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
