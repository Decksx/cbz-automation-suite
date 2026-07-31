"""Read-only, full-library Version 1 perceptual-hash coverage audit.

`perceptual_failure_audit.py` answers a narrower question: which
`hash_archive_pages_perceptual` jobs terminally failed, and why. This
module answers a broader one, appropriate for the end of a backfill
(see docs/production_handoff_2026-07-30.md, "Remaining project
sequence" step 3): for *every* archive, whether it ever had a job or
not, what is its Version 1 dHash/pHash coverage state right now?

Every archive is classified into exactly one of five mutually
exclusive populations:

- ``complete``: every page has a Version 1 dHash, a Version 1 pHash,
  and recorded width/height, and the archive is structurally eligible
  (see below) -- fully covered, nothing left to do.
- ``failed``: structurally eligible, and has a terminal
  (``status = 'failed'``) `hash_archive_pages_perceptual` job on
  record. The failure-category breakdown within this population
  reuses ``perceptual_failure_audit.py``'s stable categories directly
  rather than reimplementing that classification.
- ``stale``: structurally eligible, no terminal failure, and has an
  active (``claimed``/``running``) job whose age exceeds the given
  threshold, using the exact same staleness predicate as
  ``comic_automation/jobs/abandoned_job_audit.py``'s
  ``collect_stale_jobs`` (itself mirroring
  ``JobQueue.recover_abandoned()``). A merely ``pending`` job is not
  "stale" under that predicate -- it is legitimately queued work, not
  evidence of an abandoned worker.
- ``incomplete``: structurally eligible, no terminal failure, no stale
  job, and at least one page still missing a Version 1 dHash, pHash,
  or decoded width/height. This is the catch-all "still eligible,
  pending or in-progress work" bucket -- it includes archives with a
  live (non-stale) active job, archives with partial per-page
  coverage, and archives with zero coverage and no job history at
  all. That last case is additionally flagged as
  ``never_enqueued_backlog`` (see below): it is reported as a count and
  an archive-id list distinct from the plain ``incomplete`` count, but
  it remains counted inside ``incomplete`` for the partition invariant
  (``complete + incomplete + failed + stale + ineligible ==
  total_archive_count``) rather than being a sixth bucket.
- ``ineligible``: falls outside
  ``ArchivePerceptualHashRepository.enqueue_missing()``'s eligibility
  predicate entirely -- no current (``is_current = 1``) file location,
  no ``archive_content_signatures`` row, a page_count of zero, or a
  content signature that no longer matches the current file's
  size/mtime (stale relative to a file that changed on disk since
  exact page hashing ran). These archives were never expected to gain
  Version 1 coverage and are not gaps.

Backlog vs. unexplained gap: the same population, two readings
-------------------------------------------------------------

``never_enqueued_backlog`` is the sub-population of ``incomplete``
that is structurally eligible, has zero Version 1 coverage, and has
*no* ``hash_archive_pages_perceptual`` job of any status on record
(not pending, not claimed, not running, not failed, not completed).

That set is computed identically no matter how the audit is invoked.
What changes with ``--expect-backfill-complete`` is only the
*interpretation*, because the same observation means opposite things
at two points in the project:

- **Mid-backfill (the default).** The Version 1 backfill runs in
  guarded batches (docs/production_handoff_2026-07-30.md, "Remaining
  project sequence" steps 1-2), and ``enqueue_missing()`` only ever
  enqueues the next batch, not the whole library. Archives that have
  not had their batch yet therefore have no job history *by design*.
  Calling them "unexplained gaps" would label the entire remaining
  workload -- tens of thousands of archives at the time this audit
  was written -- a defect, which trains operators to ignore the field
  that is supposed to catch a real missed enqueue. So they are
  reported neutrally, as expected work remaining, and never affect
  the exit code.
- **Post-backfill (``--expect-backfill-complete``).** Step 3 of that
  same sequence runs this audit only *after* eligibility has reached
  zero. At that moment there is no un-enqueued batch left to explain
  the absence of a job, so an eligible archive with no job history is
  evidence of a missed enqueue or an orchestration bug. Only then are
  these reported as blocking unexplained gaps and only then do they
  drive a distinct non-zero exit code
  (``EXIT_BLOCKING_UNEXPLAINED_GAPS``).

The production handoff calls out the underlying distinction: ordinary
terminal failures are "legitimate archive or image defects... not
evidence of queue, database, or orchestration failure". An archive
that was eligible work and never got a job at all is a different kind
of finding -- but only once every eligible archive was supposed to
have been enqueued already.

Both modes always emit the complete, untruncated archive-id list to
the JSON and CSV outputs. Only the human-facing console summary is
capped (``MAX_PRINTED_ARCHIVE_IDS``), and it never truncates silently:
the omitted count is always printed alongside the sample.

Like the audits it builds on, this module never writes: it opens the
database with SQLite's ``mode=ro`` URI flag plus
``PRAGMA query_only = ON``, applies no migrations, and fingerprints
the database file before and after the run to detect any mutation
(by this process or another) during the audit.

The headline guarantee here is stronger than "nothing was written":
the five populations are claimed to *provably partition* the library,
so every count in the report has to describe one and the same library.
Classification issues several separate queries (structural facts, page
coverage, jobs, terminal failures, stale jobs), so this module reads
them all inside a single deferred transaction bracketed by
``PRAGMA data_version`` readings taken outside it -- exactly the
pattern ``comic_automation/jobs/active_job_duplicate_audit.py`` uses,
and for exactly the reason documented there: under WAL a commit by
another connection can touch only the ``-wal`` file, leaving the main
database's size and mtime *identical*, so the file fingerprint alone
cannot detect a writer landing between two of the classification
queries. Such a writer would otherwise produce a report that mixes
pre- and post-change observations while still looking clean.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
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
from comic_automation.archive.perceptual_failure_audit import (
    JOB_TYPE,
    STABLE_CATEGORY_ORDER,
    category_counts as failure_category_counts,
    collect_failures,
)
from comic_automation.archive.perceptual_hashing import (
    DHASH_ALGORITHM,
    DHASH_ALGORITHM_VERSION,
    PHASH_ALGORITHM,
    PHASH_ALGORITHM_VERSION,
)
from comic_automation.jobs.abandoned_job_audit import collect_stale_jobs


# The active statuses that represent "work is queued or in flight" for
# a `hash_archive_pages_perceptual` job. Matches the statuses
# `ArchivePerceptualHashRepository.enqueue_missing()` treats as
# blocking re-enqueue, minus 'failed' (failed jobs are their own
# population here, not "active").
ACTIVE_JOB_STATUSES = ("pending", "claimed", "running")
TERMINAL_FAILURE_STATUS = "failed"

POPULATION_ORDER = (
    "complete",
    "incomplete",
    "failed",
    "stale",
    "ineligible",
)

# The membership rule for the never-enqueued population. Deliberately
# free of any judgement about whether membership is good or bad: that
# depends entirely on whether the backfill is still running, which this
# audit cannot infer from the database and must be told
# (--expect-backfill-complete).
NEVER_ENQUEUED_BACKLOG_EXPLANATION = (
    "An archive lands here only when it is structurally eligible for "
    "Version 1 perceptual hashing (current file location, a matching "
    "content signature, at least one page), has zero Version 1 "
    "dHash/pHash coverage, and has never had a "
    "hash_archive_pages_perceptual job of any status (pending, "
    "claimed, running, failed, or completed). While the backfill is "
    "still running this is simply the remaining work: enqueue_missing() "
    "enqueues one guarded batch at a time, so archives whose batch has "
    "not come up yet legitimately have no job history. It becomes a "
    "blocking unexplained gap only under --expect-backfill-complete."
)

# The same population, read after the backfill was declared finished.
# Kept as a separate constant (rather than one string with an "if")
# because these are two distinct operational claims, and only this one
# asks for investigation.
BLOCKING_UNEXPLAINED_GAP_EXPLANATION = (
    "Reported only under --expect-backfill-complete, which asserts that "
    "Version 1 eligibility has already reached zero. Under that "
    "assertion there is no un-enqueued batch left to explain an "
    "eligible archive with zero coverage and no job history of any "
    "status, so each of these is evidence of a missed enqueue or an "
    "orchestration bug and must be investigated. Ordinary terminal "
    "failures are legitimate archive/image defects and are never "
    "counted here. Outside final-audit mode this list is empty by "
    "construction and the identical population is reported neutrally "
    "as never_enqueued_backlog."
)

# Console-only cap on how many archive ids are printed for any one
# list. A production run of this audit mid-backfill legitimately found
# 17,554 never-enqueued archives; printing them all buried the rest of
# the summary (populations, partition check, integrity, snapshot
# boundary) under a wall of ids. The full list always remains in the
# JSON and CSV outputs, and the omitted count is always printed, so
# nothing is ever hidden -- only relocated to the machine-readable
# artefacts that are built to hold it.
MAX_PRINTED_ARCHIVE_IDS = 20

EXIT_OK = 0
EXIT_FAILURE = 1
# Distinct from EXIT_FAILURE so an operator (or a wrapper script) can
# tell "the audit could not run / crashed" apart from "the audit ran
# cleanly and found blocking gaps". Mirrors
# jobs/active_job_duplicate_audit.py's EXIT_BLOCKING_DUPLICATES. Only
# reachable in final-audit mode: mid-backfill the same finding is
# expected work and must not fail a pipeline.
EXIT_BLOCKING_UNEXPLAINED_GAPS = 2


# `DatabaseChangedError`, `DatabaseMutatedError`,
# `DatabaseIntegrityError`, `DatabaseFingerprint`,
# `fingerprint_database` and `readonly_database_connection` are
# re-exported from `comic_automation.database.read_guards` above; they
# used to be defined here, one of five near-identical copies across the
# read-only audits. `DatabaseMutatedError` is still a subclass of
# `DatabaseChangedError`, so catching the base class still gets both
# the authoritative data_version guard and the weaker file-fingerprint
# diagnostic, exactly as before. The names stay importable from this
# module because the tests import them from here.


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


def _same_file(first: Path, second: Path) -> bool:
    """True if `first` and `second` name the same file on disk."""
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


def _collect_structural(connection: sqlite3.Connection) -> dict[int, dict]:
    """Per-archive structural facts needed for the eligibility check.

    Mirrors the joins `ArchivePerceptualHashRepository.enqueue_missing()`
    uses: the current (`is_current = 1`) file_locations row and the
    archive_content_signatures row, if either exists.
    """
    rows = connection.execute(
        """
        SELECT
            af.id AS archive_id,
            fl.id AS location_id,
            fl.path AS current_path,
            fl.file_size AS location_file_size,
            fl.modified_time_ns AS location_modified_time_ns,
            acs.id AS signature_id,
            acs.page_count AS signature_page_count,
            acs.source_file_size AS signature_file_size,
            acs.source_modified_time_ns AS signature_modified_time_ns
        FROM archive_files AS af
        LEFT JOIN file_locations AS fl
          ON fl.archive_id = af.id
         AND fl.is_current = 1
        LEFT JOIN archive_content_signatures AS acs
          ON acs.archive_id = af.id
        ORDER BY af.id
        """
    ).fetchall()

    # If more than one is_current=1 row somehow existed for an
    # archive (an application-level invariant violation, not one the
    # schema enforces), the last row wins; that is a pre-existing data
    # problem outside this audit's scope, not something to paper over
    # with an artificial ORDER BY tiebreak.
    return {int(row["archive_id"]): dict(row) for row in rows}


def _collect_page_coverage(
    connection: sqlite3.Connection,
) -> dict[int, dict]:
    """Per-archive page counts: total pages vs. pages still missing a
    Version 1 dHash, Version 1 pHash, or decoded width/height.
    """
    rows = connection.execute(
        """
        SELECT
            ap.archive_id AS archive_id,
            COUNT(*) AS total_pages,
            SUM(
                CASE
                    WHEN dh.id IS NULL
                      OR ph.id IS NULL
                      OR ap.width IS NULL
                      OR ap.height IS NULL
                    THEN 1
                    ELSE 0
                END
            ) AS pages_missing
        FROM archive_pages AS ap
        LEFT JOIN page_hashes AS dh
          ON dh.page_id = ap.id
         AND dh.algorithm = ?
         AND dh.algorithm_version = ?
        LEFT JOIN page_hashes AS ph
          ON ph.page_id = ap.id
         AND ph.algorithm = ?
         AND ph.algorithm_version = ?
        GROUP BY ap.archive_id
        """,
        (
            DHASH_ALGORITHM,
            DHASH_ALGORITHM_VERSION,
            PHASH_ALGORITHM,
            PHASH_ALGORITHM_VERSION,
        ),
    ).fetchall()

    coverage: dict[int, dict] = {}

    for row in rows:
        total_pages = int(row["total_pages"])
        pages_missing = int(row["pages_missing"])
        coverage[int(row["archive_id"])] = {
            "total_pages": total_pages,
            "pages_missing": pages_missing,
            "pages_covered": total_pages - pages_missing,
        }

    return coverage


def _collect_jobs(connection: sqlite3.Connection) -> dict[int, list[dict]]:
    """Every hash_archive_pages_perceptual job, grouped by archive_id.

    Jobs with a NULL archive_id are excluded: they cannot be
    attributed to any archive in this per-archive audit.
    """
    rows = connection.execute(
        """
        SELECT
            id,
            archive_id,
            status,
            failure_category,
            attempts,
            max_attempts,
            claimed_at,
            started_at,
            completed_at
        FROM jobs
        WHERE job_type = ?
        ORDER BY archive_id, id
        """,
        (JOB_TYPE,),
    ).fetchall()

    jobs_by_archive: dict[int, list[dict]] = {}

    for row in rows:
        if row["archive_id"] is None:
            continue

        jobs_by_archive.setdefault(int(row["archive_id"]), []).append(
            dict(row)
        )

    return jobs_by_archive


def classify_archives(
    connection: sqlite3.Connection,
    *,
    stale_older_than_seconds: int,
    now: datetime | None = None,
) -> list[dict]:
    """Classify every archive_files row into exactly one population.

    See the module docstring for the full definition of each
    population. Returns one dict per archive, ordered by archive_id.
    """
    structural = _collect_structural(connection)
    coverage = _collect_page_coverage(connection)
    jobs_by_archive = _collect_jobs(connection)
    failures_by_archive: dict[int, dict] = {}

    for failure in collect_failures(connection):
        archive_id = failure["archive_id"]
        if archive_id is not None and archive_id not in failures_by_archive:
            # An archive should have at most one terminal failed job
            # at a time (enqueue_missing() blocks re-enqueue while a
            # 'failed' job exists for it), so the first row is the
            # authoritative one; ORDER BY in collect_failures is
            # deterministic (failure_category, path, job id).
            failures_by_archive[archive_id] = failure

    stale_job_ids = {
        job["job_id"]
        for job in collect_stale_jobs(
            connection,
            older_than_seconds=stale_older_than_seconds,
            now=now,
        )
        if job["job_type"] == JOB_TYPE
    }

    results: list[dict] = []

    for archive_id in sorted(structural.keys()):
        info = structural[archive_id]
        page_stats = coverage.get(
            archive_id,
            {"total_pages": 0, "pages_missing": 0, "pages_covered": 0},
        )
        archive_jobs = jobs_by_archive.get(archive_id, [])

        structural_eligible = (
            info["location_id"] is not None
            and info["signature_id"] is not None
            and (info["signature_page_count"] or 0) > 0
            and info["signature_file_size"] == info["location_file_size"]
            and info["signature_modified_time_ns"]
            == info["location_modified_time_ns"]
        )

        # A page still needs work if it (or its dimensions) is
        # missing, or if no pages have been inventoried at all yet
        # (page_count > 0 was promised by the content signature but
        # archive_pages hasn't caught up -- an edge case, treated as
        # "still has work to do" rather than vacuously complete).
        has_missing_hash_work = (
            page_stats["pages_missing"] > 0 or page_stats["total_pages"] == 0
        )

        active_jobs = [
            job for job in archive_jobs if job["status"] in ACTIVE_JOB_STATUSES
        ]
        failed_jobs = [
            job
            for job in archive_jobs
            if job["status"] == TERMINAL_FAILURE_STATUS
        ]
        has_any_job = len(archive_jobs) > 0
        is_stale = any(job["id"] in stale_job_ids for job in active_jobs)

        if not structural_eligible:
            population = "ineligible"
        elif failed_jobs:
            population = "failed"
        elif is_stale:
            population = "stale"
        elif not has_missing_hash_work:
            population = "complete"
        else:
            population = "incomplete"

        # Membership only; deliberately mode-independent. Whether this
        # flag means "expected remaining work" or "blocking unexplained
        # gap" is decided once, in `run_audit`, from
        # `expect_backfill_complete` -- classification must not shift
        # under the operator's claim about backfill state, or the two
        # modes would no longer be describing the same population.
        is_never_enqueued_backlog = (
            population == "incomplete"
            and not has_any_job
            and page_stats["pages_covered"] == 0
        )

        failure = failures_by_archive.get(archive_id)

        results.append(
            {
                "archive_id": archive_id,
                "population": population,
                "never_enqueued_backlog": is_never_enqueued_backlog,
                "structural_eligible": structural_eligible,
                "current_path": info["current_path"],
                "total_pages": page_stats["total_pages"],
                "pages_missing": page_stats["pages_missing"],
                "pages_covered": page_stats["pages_covered"],
                "has_any_job": has_any_job,
                "active_job_count": len(active_jobs),
                "active_job_ids": [job["id"] for job in active_jobs],
                "is_stale": is_stale,
                "failed_job_id": (
                    failure["job_id"] if failure is not None else None
                ),
                "failed_stable_category": (
                    failure["stable_category"] if failure is not None else None
                ),
            }
        )

    return results


def population_counts(archives: list[dict]) -> dict[str, int]:
    counts = Counter(archive["population"] for archive in archives)
    return {name: counts.get(name, 0) for name in POPULATION_ORDER}


def failed_category_counts(archives: list[dict]) -> dict[str, int]:
    """Stable failure-category breakdown, scoped to archives this audit
    actually classified as 'failed' (i.e. still structurally eligible),
    reusing perceptual_failure_audit.py's category machinery.
    """
    pseudo_failures = [
        {"stable_category": archive["failed_stable_category"]}
        for archive in archives
        if archive["population"] == "failed"
    ]
    return failure_category_counts(pseudo_failures)


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
    "archive_id",
    "population",
    "never_enqueued_backlog",
    "structural_eligible",
    "current_path",
    "total_pages",
    "pages_missing",
    "pages_covered",
    "has_any_job",
    "active_job_count",
    "is_stale",
    "failed_job_id",
    "failed_stable_category",
]


def _write_csv(path: Path, archives: list[dict]) -> Path:
    resolved = path.resolve(strict=False)
    resolved.parent.mkdir(parents=True, exist_ok=True)

    with resolved.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=_CSV_FIELDNAMES, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(archives)

    return resolved


def run_audit(
    *,
    database: Path,
    stale_older_than_seconds: int,
    now: datetime | None = None,
    json_output: Path | None = None,
    csv_output: Path | None = None,
    expect_backfill_complete: bool = False,
) -> dict:
    """Produce the read-only, full-library coverage-audit report.

    `expect_backfill_complete` is the operator's assertion that Version
    1 eligibility has already reached zero (the handoff document's
    "Remaining project sequence" step 3). It changes no classification
    and no query: the never-enqueued population is identical either
    way. It only decides how that population is *reported* -- as
    expected remaining backlog (default) or as blocking unexplained
    gaps that a missed enqueue or orchestration bug would explain
    (final-audit mode). See the module docstring.

    Never mutates `database`. `json_output`/`csv_output` are validated
    against `database` (and against each other) *before* the database
    is opened or any directory is created.

    Raises `FileNotFoundError` if the database does not exist,
    `OutputPathCollisionError` if an output path could clobber it,
    `DatabaseIntegrityError` if `PRAGMA quick_check` fails, and
    `DatabaseChangedError` (or its `DatabaseMutatedError` subclass) if
    another connection committed during the run or the file's
    size/mtime changed.
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
        # tests can wrap an internal query to commit from another
        # connection mid-classification.
        return classify_archives(
            connection,
            stale_older_than_seconds=stale_older_than_seconds,
            now=effective_now,
        )

    # One deferred read transaction, bracketed by PRAGMA data_version
    # readings taken outside it (see
    # `read_guards.read_consistent_snapshot`, which is where this
    # sequence now lives): the structural, page-coverage, job, failure
    # and staleness queries inside classify_archives all read from the
    # same snapshot, so the population counts cannot disagree with each
    # other because a writer landed between two of them. Without this,
    # the partition invariant this audit reports would be an assertion
    # about no single state of the library.
    snapshot = read_consistent_snapshot(
        database,
        read,
        context="audit",
        integrity_check=quick_check,
    )
    archives = snapshot.result

    # Re-stat *after* the connection is closed: if opening read-only or
    # running any SELECT touched the file (it shouldn't -- mode=ro
    # plus query_only forbid it, but this is the actual guarantee the
    # audit promises), this run is not trustworthy and must not be
    # reported as if it were. Checked *after* the data_version gate,
    # which is the stronger of the two detectors, so a concurrent
    # commit is reported as exactly that and not as an ambiguous "the
    # file moved".
    fingerprint_after = fingerprint_database(database)
    files_after = fingerprint_database_files(database)

    if fingerprint_after != fingerprint_before:
        raise DatabaseMutatedError(
            "Database changed during a read-only audit run: "
            f"before={fingerprint_before} after={fingerprint_after}. "
            "This audit must never modify the database it inspects."
        )

    elapsed = time.perf_counter() - started

    counts = population_counts(archives)
    total_archive_count = len(archives)
    partition_sum = sum(counts.values())
    never_enqueued = [
        archive for archive in archives if archive["never_enqueued_backlog"]
    ]
    # Full fidelity, always: the console may sample this list, but the
    # JSON and CSV artefacts are the record of what the audit actually
    # found and must never be abridged.
    never_enqueued_archive_ids = [
        archive["archive_id"] for archive in never_enqueued
    ]

    output = {
        "database": str(database),
        "job_type": JOB_TYPE,
        "stale_older_than_seconds": stale_older_than_seconds,
        "total_archive_count": total_archive_count,
        "population_counts": counts,
        "population_partition_sum": partition_sum,
        "population_partition_matches_total": (
            partition_sum == total_archive_count
        ),
        "failed_stable_category_counts": failed_category_counts(archives),
        "expect_backfill_complete": expect_backfill_complete,
        "never_enqueued_backlog_count": len(never_enqueued),
        "never_enqueued_backlog_archive_ids": never_enqueued_archive_ids,
        "never_enqueued_backlog_explanation": (
            NEVER_ENQUEUED_BACKLOG_EXPLANATION
        ),
        # Same archives, reported under the blocking keys only when the
        # operator asserted the backfill is finished. Both keys are
        # always present (empty by default) so downstream parsers can
        # read one stable schema and simply check the count.
        "blocking_unexplained_gap_count": (
            len(never_enqueued) if expect_backfill_complete else 0
        ),
        "blocking_unexplained_gap_archive_ids": (
            list(never_enqueued_archive_ids)
            if expect_backfill_complete
            else []
        ),
        "blocking_unexplained_gap_explanation": (
            BLOCKING_UNEXPLAINED_GAP_EXPLANATION
        ),
        "archives": archives,
        **snapshot.report_fields(),
        **fingerprint_report_fields(
            fingerprint_before=fingerprint_before,
            fingerprint_after=fingerprint_after,
            files_before=files_before,
            files_after=files_after,
        ),
        "elapsed_seconds": round(elapsed, 6),
    }

    if json_output is not None:
        output["json_output"] = str(_write_json(json_output, output))

    if csv_output is not None:
        output["csv_output"] = str(_write_csv(csv_output, archives))

    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only, full-library Version 1 perceptual-hash coverage "
            "audit. Classifies every archive into exactly one of "
            "complete / incomplete / failed / stale / ineligible, and "
            "separately reports eligible archives with zero coverage "
            "and no job history at all as the never-enqueued backlog "
            "(expected remaining work while the backfill is running; "
            "pass --expect-backfill-complete to treat them as blocking "
            "unexplained gaps instead). Never enqueues, retries, "
            "quarantines, or moves anything; safe to point at a "
            "protected backup."
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
        "--stale-older-than-seconds",
        type=int,
        required=True,
        help=(
            "Staleness cutoff in seconds for active (claimed/running) "
            "jobs, using the same predicate as "
            "JobQueue.recover_abandoned() / abandoned_job_audit.py's "
            "collect_stale_jobs."
        ),
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="Optional path for the JSON coverage report.",
    )
    parser.add_argument(
        "--csv-output",
        type=Path,
        help="Optional path for the per-archive CSV classification.",
    )
    parser.add_argument(
        "--expect-backfill-complete",
        action="store_true",
        help=(
            "Final-audit mode. Assert that Version 1 eligibility has "
            "already reached zero, so an eligible archive that never "
            "had a hash_archive_pages_perceptual job is a blocking "
            "unexplained gap (missed enqueue / orchestration bug) "
            "rather than remaining backlog. Changes no classification "
            "-- only the interpretation, the console framing, and the "
            "exit code (2 when any are found). Use this for step 3 of "
            "the handoff document's remaining project sequence; leave "
            "it off while the backfill is still running."
        ),
    )
    return parser


def print_archive_id_sample(
    archive_ids: Sequence[int],
    *,
    indent: str = "  ",
    label: str = "ARCHIVE IDS",
    limit: int = MAX_PRINTED_ARCHIVE_IDS,
) -> None:
    """Print at most `limit` ids, plus an explicit omitted count.

    Truncation is never silent: when the list is longer than `limit`
    the header states how many of how many are shown and a following
    line names the exact number omitted and where the full list lives.
    An operator who sees only part of a list must be able to tell that
    from the console alone -- otherwise a capped summary is worse than
    no summary, because it looks complete.
    """
    total = len(archive_ids)

    if total == 0:
        return

    shown = list(archive_ids[:limit])
    omitted = total - len(shown)

    if omitted:
        print(f"{indent}{label} (first {len(shown):,} of {total:,}): {shown}")
        print(
            f"{indent}... and {omitted:,} more "
            "(see JSON/CSV for the full list)"
        )
    else:
        print(f"{indent}{label} ({total:,}): {shown}")


def _print_never_enqueued_section(output: dict) -> None:
    """The one part of the summary whose wording depends on the mode.

    Identical population, two framings -- see the module docstring.
    """
    archive_ids = output["never_enqueued_backlog_archive_ids"]

    if output.get("expect_backfill_complete"):
        count = output["blocking_unexplained_gap_count"]
        print(
            "BLOCKING UNEXPLAINED GAPS (eligible, zero coverage, no job "
            f"ever): {count:,}"
        )

        if count:
            print(
                "  Final-audit mode asserted the backfill is complete, so "
                "these indicate a missed enqueue or an orchestration bug "
                "and must be investigated before the backfill is signed "
                "off."
            )
            print_archive_id_sample(
                output["blocking_unexplained_gap_archive_ids"]
            )

        return

    count = output["never_enqueued_backlog_count"]
    print(
        "Never-enqueued backlog (eligible, zero coverage, not yet "
        f"enqueued): {count:,}"
    )

    if count:
        # Deliberately free of the word "gap": mid-backfill this is the
        # work queue, and an operator scanning the summary should not
        # read the remaining workload as a defect. The pointer to
        # --expect-backfill-complete is how they get the strict reading
        # once it is actually the right one.
        print(
            "  Expected remaining work while the Version 1 backfill is in "
            "progress -- not an anomaly. Once eligibility reaches zero, "
            "re-run with --expect-backfill-complete for the strict "
            "post-backfill check."
        )
        print_archive_id_sample(archive_ids, label="Sample archive IDs")


def print_summary(output: dict) -> None:
    print("Perceptual-hashing Version 1 coverage audit completed.")
    print(f"Database:              {output['database']}")
    print(f"Total archives:        {output['total_archive_count']}")
    print("Populations:")

    for population, count in output["population_counts"].items():
        print(f"  {population}: {count}")

    print(
        "Partition check:       "
        f"{output['population_partition_sum']} == "
        f"{output['total_archive_count']} -> "
        f"{output['population_partition_matches_total']}"
    )
    print("Failed-job categories (within 'failed'):")

    for category, count in output["failed_stable_category_counts"].items():
        print(f"  {category}: {count}")

    _print_never_enqueued_section(output)

    print(f"Integrity check:       {output['quick_check']}")
    print(
        "Snapshot data_version: "
        f"{output['data_version_before']} -> "
        f"{output['data_version_after']} (authoritative guard)"
    )
    print(
        "DB file unchanged:     "
        f"{output['database_file_unchanged']} (diagnostic only; a WAL "
        "commit can leave the main file identical)"
    )

    if output.get("json_output"):
        print(f"JSON output:           {output['json_output']}")
    if output.get("csv_output"):
        print(f"CSV output:            {output['csv_output']}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        output = run_audit(
            database=args.database,
            stale_older_than_seconds=args.stale_older_than_seconds,
            json_output=args.json_output,
            csv_output=args.csv_output,
            expect_backfill_complete=args.expect_backfill_complete,
        )
    except Exception as exc:
        print(f"Perceptual coverage audit failed: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    print_summary(output)

    # Only final-audit mode can fail the run. Mid-backfill the same
    # population is the work queue itself, and exiting non-zero on it
    # would fail every scheduled run until the backfill finished --
    # which is exactly how a real gap ends up ignored.
    if output["blocking_unexplained_gap_count"] > 0:
        return EXIT_BLOCKING_UNEXPLAINED_GAPS

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
