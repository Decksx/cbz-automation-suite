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

The output is one JSON report with a top-level `overall_pass: bool` and
a `gates` mapping of `gate_name -> {"pass": bool, "detail": ...}`, so a
human or another script can see exactly which automated gate(s) failed.
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
from comic_automation.jobs.active_job_duplicate_audit import (
    DatabaseChangedError,
    DatabaseIntegrityError,
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
    """A requested output path could clobber a database or another output."""


def _same_file(first: Path, second: Path) -> bool:
    if first.resolve(strict=False) == second.resolve(strict=False):
        return True

    if first.exists() and second.exists():
        try:
            return first.samefile(second)
        except OSError:
            return False

    return False


def _validate_output_paths(
    *,
    databases: Sequence[Path],
    outputs: Sequence[Path | None],
) -> None:
    """Reject any output path that could collide with a database or
    another output, before anything is opened or written."""
    resolved_outputs = [path for path in outputs if path is not None]

    for output in resolved_outputs:
        for database in databases:
            if _same_file(output, database):
                raise OutputPathCollisionError(
                    f"Output path ({output}) must not be the same file "
                    f"as a database being audited ({database})."
                )

    for index, first in enumerate(resolved_outputs):
        for second in resolved_outputs[index + 1 :]:
            if _same_file(first, second):
                raise OutputPathCollisionError(
                    f"Output paths must not collide: {first} == {second}."
                )


def _gate(name: str, passed: bool, detail: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "pass": bool(passed), "detail": detail}


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

    Follows the exact template in active_job_duplicate_audit.py: sample
    `PRAGMA data_version` before opening the transaction that
    encompasses every read (including quick_check), then again after,
    and raise if either that or the file fingerprint changed. Every
    number that a gate compares against an expected value therefore
    comes from a single instant, so gates can never disagree with each
    other because a writer landed between two of this module's own
    queries.
    """
    resolved = Path(database).resolve(strict=True)
    fingerprint_before = fingerprint_database(resolved)

    with readonly_database_connection(resolved) as connection:
        data_version_before = int(
            connection.execute("PRAGMA data_version").fetchone()[0]
        )

        connection.execute("BEGIN")
        try:
            integrity = quick_check(connection)

            if integrity != "ok":
                raise DatabaseIntegrityError(
                    f"PRAGMA quick_check failed for {resolved}: {integrity}"
                )

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
        finally:
            connection.execute("END")

        data_version_after = int(
            connection.execute("PRAGMA data_version").fetchone()[0]
        )

    fingerprint_after = fingerprint_database(resolved)

    if data_version_before != data_version_after:
        raise DatabaseChangedError(
            "Another connection committed to the working database during "
            f"postflight (data_version {data_version_before} -> "
            f"{data_version_after}); the report is not trustworthy."
        )

    if fingerprint_before != fingerprint_after:
        raise DatabaseChangedError(
            "Working database file changed during postflight: "
            f"before={fingerprint_before} after={fingerprint_after}."
        )

    return {
        "database": str(resolved),
        "quick_check": integrity,
        "active_job_count": int(active_count),
        "total_job_population": int(total_job_population),
        "completed_count": int(completed_count),
        "failed_count": int(failed_count),
        "eligible_remaining": int(eligible_remaining),
        "dhash_v1_count": int(dhash_count),
        "phash_v1_count": int(phash_count),
        "page_sha256_count": int(page_sha256_count),
        "near_duplicate_count": int(near_duplicate_count),
        "fingerprint_before": fingerprint_before,
        "fingerprint_after": fingerprint_after,
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
) -> dict[str, Any]:
    database = Path(database).resolve(strict=False)

    if not database.is_file():
        raise FileNotFoundError(f"Database does not exist: {database}")

    resolved_backup = (
        Path(backup_database).resolve(strict=False)
        if backup_database is not None
        else None
    )

    output_paths = [failure_audit_json_output, failure_audit_csv_output]
    databases = [database] + ([resolved_backup] if resolved_backup else [])
    _validate_output_paths(databases=databases, outputs=output_paths)

    report = load_batch_report(batch_report)

    gates: dict[str, dict[str, Any]] = {}

    # --- Gate 1: processed count -------------------------------------
    reported_processed = report.get("processed")
    gates["batch_report_processed"] = _gate(
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

    gates["batch_report_outcome_reconciliation"] = _gate(
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
    gates["enqueue_and_population"] = _gate(
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
    gates["cumulative_outcome_reconciliation"] = _gate(
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
    gates["no_unexpected_active_jobs"] = _gate(
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
    gates["eligibility_recount"] = _gate(
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

    gates["hash_alignment"] = _gate(
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
    gates["page_sha256_count"] = _gate(
        "page_sha256_count",
        snapshot["page_sha256_count"] == expected_page_sha256_count,
        {
            "actual_page_sha256_count": snapshot["page_sha256_count"],
            "expected_page_sha256_count": expected_page_sha256_count,
        },
    )

    # --- Gate 9: near-duplicate candidates ------------------------------
    gates["near_duplicate_candidates"] = _gate(
        "near_duplicate_candidates",
        snapshot["near_duplicate_count"] == expected_near_duplicate_count,
        {
            "actual_near_duplicate_count": snapshot["near_duplicate_count"],
            "expected_near_duplicate_count": expected_near_duplicate_count,
        },
    )

    # --- Gate 10: quick_check on working DB (+ backup) -----------------
    quick_check_detail: dict[str, Any] = {
        "working": {
            "database": str(database),
            "quick_check": snapshot["quick_check"],
        }
    }
    if snapshot_error is not None:
        quick_check_detail["working"]["error"] = snapshot_error
    quick_check_pass = snapshot["quick_check"] == "ok"

    if resolved_backup is not None:
        if not resolved_backup.is_file():
            raise FileNotFoundError(
                f"Backup database does not exist: {resolved_backup}"
            )
        try:
            with readonly_database_connection(resolved_backup) as connection:
                backup_integrity = quick_check(connection)
            quick_check_detail["backup"] = {
                "database": str(resolved_backup),
                "quick_check": backup_integrity,
            }
            quick_check_pass = quick_check_pass and backup_integrity == "ok"
        except READ_FAILURE_EXCEPTIONS as exc:
            quick_check_detail["backup"] = {
                "database": str(resolved_backup),
                "quick_check": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
            quick_check_pass = False
    else:
        quick_check_detail["backup"] = {"skipped": True}

    gates["quick_check"] = _gate("quick_check", quick_check_pass, quick_check_detail)

    # --- Gate 11: active-job duplicate audit on both databases --------
    duplicate_detail: dict[str, Any] = {}
    duplicate_pass = True

    try:
        working_duplicate_report = run_duplicate_active_preflight(
            database=database
        )
        duplicate_detail["working"] = {
            "database": str(database),
            "blocking_group_count": working_duplicate_report[
                "blocking_group_count"
            ],
            "unique_active_index_exists": working_duplicate_report[
                "unique_active_index_exists"
            ],
        }
        duplicate_pass = (
            working_duplicate_report["blocking_group_count"] == 0
            and working_duplicate_report["unique_active_index_exists"]
        )
    except READ_FAILURE_EXCEPTIONS as exc:
        duplicate_detail["working"] = {
            "database": str(database),
            "error": f"{type(exc).__name__}: {exc}",
        }
        duplicate_pass = False

    if resolved_backup is not None:
        try:
            backup_duplicate_report = run_duplicate_active_preflight(
                database=resolved_backup
            )
            duplicate_detail["backup"] = {
                "database": str(resolved_backup),
                "blocking_group_count": backup_duplicate_report[
                    "blocking_group_count"
                ],
                "unique_active_index_exists": backup_duplicate_report[
                    "unique_active_index_exists"
                ],
            }
            duplicate_pass = duplicate_pass and (
                backup_duplicate_report["blocking_group_count"] == 0
                and backup_duplicate_report["unique_active_index_exists"]
            )
        except READ_FAILURE_EXCEPTIONS as exc:
            duplicate_detail["backup"] = {
                "database": str(resolved_backup),
                "error": f"{type(exc).__name__}: {exc}",
            }
            duplicate_pass = False
    else:
        duplicate_detail["backup"] = {"skipped": True}

    gates["active_job_duplicate_audit"] = _gate(
        "active_job_duplicate_audit", duplicate_pass, duplicate_detail
    )

    # --- Gate 12: backup fingerprint -----------------------------------
    if (
        resolved_backup is not None
        and expected_backup_size_bytes is not None
        and expected_backup_modified_time_ns is not None
    ):
        actual_backup_fingerprint = fingerprint_database(resolved_backup)
        backup_fingerprint_pass = (
            actual_backup_fingerprint.size_bytes == expected_backup_size_bytes
            and actual_backup_fingerprint.modified_time_ns
            == expected_backup_modified_time_ns
        )
        backup_fingerprint_detail = {
            "skipped": False,
            "actual_size_bytes": actual_backup_fingerprint.size_bytes,
            "expected_size_bytes": expected_backup_size_bytes,
            "actual_modified_time_ns": (
                actual_backup_fingerprint.modified_time_ns
            ),
            "expected_modified_time_ns": expected_backup_modified_time_ns,
        }
    else:
        backup_fingerprint_pass = True
        backup_fingerprint_detail = {
            "skipped": True,
            "reason": (
                "backup_database and/or expected fingerprint values "
                "were not provided."
            ),
        }

    gates["backup_fingerprint"] = _gate(
        "backup_fingerprint", backup_fingerprint_pass, backup_fingerprint_detail
    )

    # --- Gate 13: repository (git) state --------------------------------
    repository_path = (
        Path(repository).resolve(strict=False)
        if repository is not None
        else REPOSITORY_ROOT
    )
    git_state = _read_repository_state(repository_path)

    if not git_state.determinable:
        repository_gate_pass = True  # "cannot determine" is a warning.
        repository_detail: dict[str, Any] = {
            "determinable": False,
            "warning": git_state.warning,
            "repository": str(repository_path),
        }
    else:
        repository_gate_pass = git_state.clean is True and (
            expected_commit is None or git_state.head == expected_commit
        )
        repository_detail = {
            "determinable": True,
            "repository": str(repository_path),
            "clean": git_state.clean,
            "head": git_state.head,
            "expected_commit": expected_commit,
        }

    gates["repository_state"] = _gate(
        "repository_state", repository_gate_pass, repository_detail
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
        gates["perceptual_failure_audit"] = _gate(
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
        gates["perceptual_failure_audit"] = _gate(
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
            gates[name]["pass"] = False
            gates[name]["detail"]["snapshot_error"] = snapshot_error

    overall_pass = all(gate["pass"] for gate in gates.values())

    return {
        "database": str(database),
        "backup_database": (
            str(resolved_backup) if resolved_backup is not None else None
        ),
        "batch_report_path": str(Path(batch_report).resolve(strict=False)),
        "overall_pass": overall_pass,
        "gates": gates,
        "database_fingerprint_before": asdict(snapshot["fingerprint_before"]),
        "database_fingerprint_after": asdict(snapshot["fingerprint_after"]),
        "database_unchanged": (
            snapshot["fingerprint_before"] == snapshot["fingerprint_after"]
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
            "migrates, or otherwise mutates any database."
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

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

    if not report["overall_pass"]:
        return EXIT_GATE_FAILURE

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
