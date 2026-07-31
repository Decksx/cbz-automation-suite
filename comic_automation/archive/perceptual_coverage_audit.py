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
  all. That last case is additionally flagged as an
  ``unexplained_gap`` (see below): it is reported as a count and an
  archive-id list distinct from the plain ``incomplete`` count, but it
  remains counted inside ``incomplete`` for the partition invariant
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

An "unexplained gap" -- eligible, zero coverage, and *no* job of this
type ever recorded (not pending, not claimed, not running, not
failed, not completed) -- is the signal the production handoff
document calls out specifically: ordinary terminal failures are
"legitimate archive or image defects... not evidence of queue,
database, or orchestration failure", but an archive that was eligible
work and simply never got a job at all suggests a missed enqueue or a
bug, and is surfaced loudly via ``unexplained_gap_count`` /
``unexplained_gap_archive_ids`` rather than being silently folded into
the ordinary "still pending" story.

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
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

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

UNEXPLAINED_GAP_EXPLANATION = (
    "An archive lands here only when it is structurally eligible for "
    "Version 1 perceptual hashing (current file location, a matching "
    "content signature, at least one page), has zero Version 1 "
    "dHash/pHash coverage, and has never had a "
    "hash_archive_pages_perceptual job of any status (pending, "
    "claimed, running, failed, or completed). Ordinary terminal "
    "failures are legitimate archive/image defects and are not "
    "unexplained gaps; an eligible archive with no job history at all "
    "is evidence of a missed enqueue or an orchestration bug, not a "
    "normal backlog item."
)


class DatabaseChangedError(RuntimeError):
    """Raised when another connection committed while the audit read.

    Detected via ``PRAGMA data_version``, which counts commits made by
    *other* connections. This is the guard that actually holds under
    WAL: a WAL commit can be entirely contained in the ``-wal`` file,
    leaving the main database file's size and mtime untouched, so the
    fingerprint check below can miss it completely. If the counter
    moved, the report may mix pre- and post-change observations -- and
    a mixed snapshot silently breaks this audit's headline guarantee
    that the five populations partition the library -- so the run is
    rejected instead of reported as trustworthy.
    """


class DatabaseMutatedError(DatabaseChangedError):
    """Raised when a database changed size or mtime during an audit run.

    This audit is read-only by construction (mode=ro + query_only),
    but this check is defense in depth: if the underlying file was
    touched by *anything* (this process or another) while the audit
    ran, the run is treated as untrustworthy rather than silently
    reporting a possibly-inconsistent snapshot.

    It is a subclass of `DatabaseChangedError` because it reports the
    same class of problem through a weaker detector: callers who want
    "the database did not change under me" can catch the base class and
    get both guards. `run_audit` checks `data_version` *first*, so a
    concurrent commit is always reported as the more precise
    `DatabaseChangedError` even when the file also happened to change.
    """


class DatabaseIntegrityError(RuntimeError):
    """Raised when ``PRAGMA quick_check`` did not return 'ok'.

    Classifying archives out of a structurally damaged database would
    produce populations that look authoritative but are not, so the run
    is abandoned. Matches `active_job_duplicate_audit.py` and the other
    read-only audits.
    """


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

    This helper is module-local by design (each read-only audit owns its
    own copy) and is only imported by this module and its tests.
    """
    path = Path(database_path).resolve(strict=False)

    if not path.is_file():
        raise FileNotFoundError(f"Database does not exist: {path}")

    uri = f"{path.as_uri()}?mode=ro"
    connection = sqlite3.connect(
        uri,
        uri=True,
        timeout=30.0,
        # Disable pysqlite's implicit transaction handling so the
        # explicit BEGIN/END in `run_audit` are the only transaction
        # boundaries in play; with the default isolation_level the
        # driver's own bookkeeping would fight them.
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

    Frozen for the duration of a read transaction, which is precisely
    why `run_audit` samples it outside and around the transaction: a
    difference between the two readings means someone else committed
    while the audit was reading.
    """
    return int(connection.execute("PRAGMA data_version").fetchone()[0])


def quick_check(connection: sqlite3.Connection) -> str:
    """`PRAGMA quick_check` output, joined into a single string.

    'ok' means the database passed. Anything else is the error text
    SQLite produced, reported verbatim.
    """
    rows = connection.execute("PRAGMA quick_check").fetchall()
    return "\n".join(str(row[0]) for row in rows)


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

        is_unexplained_gap = (
            population == "incomplete"
            and not has_any_job
            and page_stats["pages_covered"] == 0
        )

        failure = failures_by_archive.get(archive_id)

        results.append(
            {
                "archive_id": archive_id,
                "population": population,
                "unexplained_gap": is_unexplained_gap,
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
    "unexplained_gap",
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
) -> dict:
    """Produce the read-only, full-library coverage-audit report.

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

    with readonly_database_connection(database) as connection:
        # data_version is sampled *outside* and around the whole
        # transaction, so the change-detection window covers every read
        # the report depends on -- including quick_check. Sampling it
        # after quick_check would leave that read outside the window,
        # and a WAL commit landing there would go undetected: a WAL
        # write can touch only the -wal file, leaving the main
        # database's size and mtime identical, so the fingerprint
        # comparison below cannot be relied on to catch it either.
        data_version_before = _data_version(connection)

        # One deferred read transaction: the structural, page-coverage,
        # job, failure and staleness queries inside classify_archives
        # all read from the same snapshot, so the population counts
        # cannot disagree with each other because a writer landed
        # between two of them. Without this, the partition invariant
        # this audit reports would be an assertion about no single
        # state of the library.
        connection.execute("BEGIN")

        try:
            integrity = quick_check(connection)

            if integrity != "ok":
                raise DatabaseIntegrityError(
                    "PRAGMA quick_check failed for "
                    f"{database}: {integrity}"
                )

            archives = classify_archives(
                connection,
                stale_older_than_seconds=stale_older_than_seconds,
                now=effective_now,
            )
        finally:
            # A read transaction still has to be ended; END is not a
            # write and is permitted under query_only.
            connection.execute("END")

        data_version_after = _data_version(connection)

    # Re-stat *after* closing the connection: if opening read-only or
    # running any SELECT touched the file (it shouldn't -- mode=ro
    # plus query_only forbid it, but this is the actual guarantee the
    # audit promises), this run is not trustworthy and must not be
    # reported as if it were.
    fingerprint_after = fingerprint_database(database)

    # Checked before the fingerprint, because it is the stronger of the
    # two detectors: a concurrent commit is reported as exactly that,
    # not as an ambiguous "the file moved".
    if data_version_before != data_version_after:
        raise DatabaseChangedError(
            "Another connection committed to the database during the "
            f"audit (data_version {data_version_before} -> "
            f"{data_version_after}); the classification would mix pre- "
            "and post-change observations and is not trustworthy."
        )

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
    unexplained_gaps = [
        archive for archive in archives if archive["unexplained_gap"]
    ]

    output = {
        "database": str(database),
        "job_type": JOB_TYPE,
        "quick_check": integrity,
        "stale_older_than_seconds": stale_older_than_seconds,
        "total_archive_count": total_archive_count,
        "population_counts": counts,
        "population_partition_sum": partition_sum,
        "population_partition_matches_total": (
            partition_sum == total_archive_count
        ),
        "failed_stable_category_counts": failed_category_counts(archives),
        "unexplained_gap_count": len(unexplained_gaps),
        "unexplained_gap_archive_ids": [
            archive["archive_id"] for archive in unexplained_gaps
        ],
        "unexplained_gap_explanation": UNEXPLAINED_GAP_EXPLANATION,
        "archives": archives,
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
            "separately flags eligible archives with zero coverage and "
            "no job history at all as unexplained gaps. Never enqueues, "
            "retries, quarantines, or moves anything; safe to point at "
            "a protected backup."
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
    return parser


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

    print(
        "Unexplained gaps (eligible, zero coverage, no job ever): "
        f"{output['unexplained_gap_count']}"
    )

    if output["unexplained_gap_count"] > 0:
        print(
            "  ARCHIVE IDS: "
            f"{output['unexplained_gap_archive_ids']}"
        )

    print(f"Integrity check:       {output['quick_check']}")
    print(f"Database unchanged:    {output['database_unchanged']}")
    print(
        "Snapshot data_version: "
        f"{output['data_version_before']} -> "
        f"{output['data_version_after']}"
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
        )
    except Exception as exc:
        print(f"Perceptual coverage audit failed: {exc}", file=sys.stderr)
        return 1

    print_summary(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
