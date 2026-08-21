"""Read-only perceptual-coverage accounting, built on the shared contract.

This module used to classify archives itself, into one of five mutually
exclusive populations, and that single-axis model is what it no longer
does. `comic_automation/archive/classification.py` now owns eligibility,
availability, job history, disposition and exclusion, and this module
consumes those classifications rather than re-deriving them. The audit's
job is arithmetic and accountability on top of the contract, not a second
opinion about what an archive is.

Three measurements, deliberately separate
-----------------------------------------

The old report published one coverage number, which meant every reader
had to guess which question it answered. It answered all of them badly:
an archive that went missing from disk made "coverage" rise, because the
denominator quietly shrank. Pages that were hashed do not become unhashed
when a drive is unplugged, so a number that moves on that observation is
not measuring coverage of the library.

``historical``
    Did the Version 1 backfill hash the pages it inventoried? The
    denominator is every row in ``archive_pages``, frozen. Nothing
    observed at run time may move it -- not a retirement, not a
    supersession, not a missing file, not an unavailable root, not
    signature drift. This is a statement about work that was or was not
    done, and the past does not change when a volume is unmounted.

``operational``
    Of the library we still consider ours, how much is covered? Pages are
    excluded only for identities carrying a *recorded decision* --
    ``retired`` or ``superseded``. Disposition is the only stored axis,
    and a decision is the only thing entitled to remove an archive from
    the operational denominator. Availability is an observation and must
    never move this number; that is asserted here, not hoped for.

``accountability``
    Is every identity accounted for at all? Every archive appears here,
    including those holding zero pages, which therefore cannot
    participate in either coverage ratio. An identity that cannot be
    measured must still be *named*, or a report that reconciles perfectly
    is reconciling over a library it silently shrank.

``unexplained`` is residue
--------------------------

The contract produces ``selection = unexplained`` only for an archive
that is in neither the eligible set nor the excluded set. No predicate
creates it. This module reports it, and fails on it, and never converts
it into a positive population -- which is exactly what the removed
``never_enqueued_backlog`` flag did. That flag had a predicate of its own
("eligible, zero coverage, no job history"), so it confidently reported
archives that were fully explained by a path refusal while saying nothing
about the ones that had no explanation at all. It is gone from the model,
the calculations, the CSV, the console and the tests, and is not replaced
by another positive predicate.

Scope is part of the answer
---------------------------

Two runs over the same database under different declared roots answer
different questions, and their numbers are not comparable. Under the
volume root the removed ``Horrorsplat`` folder is a large block of
missing files; under a declaration that names the library folders
individually, Horrorsplat is itself an unavailable declared root and
those archives are unobservable rather than gone. The declared roots and
`DeclaredScope.digest` are therefore printed prominently and carried in
every artefact, so two results can never be compared as though they had
asked the same thing.

Read-only
---------

The database is opened with SQLite's ``mode=ro`` URI flag plus
``PRAGMA query_only = ON``; every read happens inside one deferred
transaction bracketed by ``PRAGMA data_version`` readings taken outside
it, and the file is fingerprinted before and after. The data_version
bracket is the load-bearing guard: under WAL another connection's commit
can touch only the ``-wal`` file, leaving the main database's size and
mtime identical, so the fingerprint alone cannot see a writer landing
between two of the audit's queries. Such a writer would otherwise produce
a report that mixes pre- and post-change observations while still looking
clean.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import time
from dataclasses import dataclass
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
from comic_automation.archive import classification as classification_module
from comic_automation.archive.classification import (
    AXES,
    ArchiveClassification,
    ClassificationInvariantError,
    ClassificationResult,
    DeclaredScope,
    DISPOSITION_RETIRED,
    DISPOSITION_SUPERSEDED,
    PERCEPTUAL_JOB_TYPE,
    SELECTION_UNEXPLAINED,
    WORK_COMPLETED,
    axis_totals,
    outstanding_pages_by_axis,
    presentation_label,
    selection_reason_totals,
)
from comic_automation.archive.perceptual_hashing import (
    DHASH_ALGORITHM,
    DHASH_ALGORITHM_VERSION,
    PHASH_ALGORITHM,
    PHASH_ALGORITHM_VERSION,
)


# Kept as an alias so callers and tests written against the audit's own
# job-type constant keep working; the contract owns the value now.
JOB_TYPE = PERCEPTUAL_JOB_TYPE

# The dispositions that remove an identity from the operational
# denominator. Nothing else may. This tuple is the complete list of
# things entitled to shrink operational scope, and it contains only
# recorded decisions -- no observation appears in it, and a test proves
# that adding an observation cannot move the measurement.
RETIRING_DISPOSITIONS = (DISPOSITION_RETIRED, DISPOSITION_SUPERSEDED)

MEASUREMENT_HISTORICAL = "historical"
MEASUREMENT_OPERATIONAL = "operational"

MEASUREMENT_BASIS = {
    MEASUREMENT_HISTORICAL: (
        "Denominator is every archive_pages row, frozen. No run-time "
        "observation and no recorded disposition may change it."
    ),
    MEASUREMENT_OPERATIONAL: (
        "Denominator excludes pages belonging to identities with a "
        "recorded retirement or supersession, and nothing else. "
        "Availability observations never move it."
    ),
}

# Console-only cap on how many archive ids are printed for any one list.
# The full list always remains in the JSON and CSV artefacts, and the
# omitted count is always printed, so nothing is hidden -- only relocated
# to the machine-readable outputs built to hold it.
MAX_PRINTED_ARCHIVE_IDS = 20

EXIT_OK = 0
EXIT_FAILURE = 1
# Distinct from EXIT_FAILURE so an operator can tell "the audit crashed"
# apart from "the audit ran cleanly and its own arithmetic does not hold".
# A failed invariant means the report cannot be trusted; unexplained
# residue is one such failure.
EXIT_INVARIANT_VIOLATION = 2


# `DatabaseChangedError`, `DatabaseMutatedError`, `DatabaseIntegrityError`,
# `DatabaseFingerprint`, `fingerprint_database` and
# `readonly_database_connection` are re-exported from
# `comic_automation.database.read_guards` above. The names stay importable
# from this module because the tests import them from here.


class OutputPathCollisionError(ValueError):
    """Raised when a requested output path could clobber input data.

    The audit's read-only guarantees (mode=ro + query_only) protect only
    the connection it opens to *read*. The separate step that later
    writes the JSON/CSV report opens the output path in write mode with
    no such protection, so a caller pointing --json-output at the
    database file would silently overwrite the database this audit just
    verified was untouched. Checked before the database is opened, so
    nothing is created once a collision is detected.
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


# --- the independent page census -----------------------------------------


def identity_census(connection: sqlite3.Connection) -> list[int]:
    """Every `archive_files` id, read independently of the classifier.

    The page census cannot stand in for this one. An archive holding zero
    pages contributes nothing to `archive_pages`, so a classifier that
    dropped such an identity entirely would leave every page number in
    this report reconciling perfectly -- the library would simply have
    become smaller without anything saying so. Reading the identity set
    straight from `archive_files` is the only way the audit can notice.
    """
    return [
        int(row["id"])
        for row in connection.execute(
            "SELECT id FROM archive_files ORDER BY id"
        )
    ]


def reconcile_identities(
    result: ClassificationResult, expected_ids: Sequence[int]
) -> dict:
    """Compare the classified identity set against the database's own.

    Four distinct ways this can go wrong, reported separately because
    they have different causes: an identity the classifier dropped, one
    it invented, one it emitted twice, and one whose axis tuple is
    incomplete. Collapsing them into a single boolean would say the
    report is untrustworthy without saying what to go and look at.
    """
    expected = set(expected_ids)
    classified_ids = [archive.archive_id for archive in result.archives]
    classified = set(classified_ids)

    seen: set[int] = set()
    duplicates: set[int] = set()

    for archive_id in classified_ids:
        if archive_id in seen:
            duplicates.add(archive_id)

        seen.add(archive_id)

    incomplete = [
        archive.archive_id
        for archive in result.archives
        if not all(
            (
                archive.disposition,
                archive.availability,
                archive.inventory,
                archive.perceptual_work,
                archive.selection,
            )
        )
    ]

    return {
        "expected_identities": len(expected),
        "classified_identities": len(classified_ids),
        "classified_distinct_identities": len(classified),
        "missing_count": len(expected - classified),
        "missing_archive_ids": sorted(expected - classified),
        "extra_count": len(classified - expected),
        "extra_archive_ids": sorted(classified - expected),
        "duplicate_count": len(duplicates),
        "duplicate_archive_ids": sorted(duplicates),
        "incomplete_tuple_count": len(incomplete),
        "incomplete_tuple_archive_ids": sorted(incomplete),
    }


def page_census(connection: sqlite3.Connection) -> dict[str, int]:
    """Whole-table page facts, counted independently of the contract.

    The contract reports one number per archive: how many of its pages
    are outstanding, where outstanding means missing a Version 1 dHash,
    a Version 1 pHash, or decoded dimensions. That is the right answer
    for coverage, but it collapses three different defects into one
    count, so it cannot answer "are dHash and pHash actually paired?".

    This is deliberately a *second* measurement of the same library,
    taken by a single grouped scan rather than by summing the contract's
    per-archive numbers. The reconciliation invariants below compare the
    two. If this simply re-used the contract's aggregation it could not
    disagree with it, and a check that cannot fail proves nothing.
    """
    row = connection.execute(
        """
        SELECT
            COUNT(*) AS pages,
            SUM(CASE WHEN dh.id IS NOT NULL THEN 1 ELSE 0 END)
                AS dhash_pages,
            SUM(CASE WHEN ph.id IS NOT NULL THEN 1 ELSE 0 END)
                AS phash_pages,
            SUM(
                CASE WHEN (dh.id IS NULL) <> (ph.id IS NULL)
                THEN 1 ELSE 0 END
            ) AS half_paired_pages,
            SUM(
                CASE WHEN ap.width IS NULL OR ap.height IS NULL
                THEN 1 ELSE 0 END
            ) AS pages_missing_dimensions,
            SUM(
                CASE
                    WHEN dh.id IS NOT NULL
                     AND ph.id IS NOT NULL
                     AND ap.width IS NOT NULL
                     AND ap.height IS NOT NULL
                    THEN 1 ELSE 0
                END
            ) AS covered_pages
        FROM archive_pages AS ap
        LEFT JOIN page_hashes AS dh
          ON dh.page_id = ap.id
         AND dh.algorithm = ?
         AND dh.algorithm_version = ?
        LEFT JOIN page_hashes AS ph
          ON ph.page_id = ap.id
         AND ph.algorithm = ?
         AND ph.algorithm_version = ?
        """,
        (
            DHASH_ALGORITHM,
            DHASH_ALGORITHM_VERSION,
            PHASH_ALGORITHM,
            PHASH_ALGORITHM_VERSION,
        ),
    ).fetchone()

    return {
        "pages": int(row["pages"] or 0),
        "dhash_pages": int(row["dhash_pages"] or 0),
        "phash_pages": int(row["phash_pages"] or 0),
        "half_paired_pages": int(row["half_paired_pages"] or 0),
        "pages_missing_dimensions": int(row["pages_missing_dimensions"] or 0),
        "covered_pages": int(row["covered_pages"] or 0),
    }


# --- the three measurements ----------------------------------------------


@dataclass(frozen=True)
class Coverage:
    """One coverage ratio, carrying the basis it was computed on.

    A bare percentage is what made the old report unreadable: two numbers
    that answered different questions looked identical on the page. The
    basis string travels with the number so a reader always knows which
    denominator produced it.
    """

    name: str
    covered_pages: int
    total_pages: int
    excluded_pages: int
    excluded_archives: int
    basis: str
    # Split out so the derivation is visible rather than inferred. A
    # denominator that shrank by the right amount says nothing about
    # whether the numerator moved with it, and the retirement this
    # library actually holds (archive 45217) has zero covered pages --
    # so an implementation that shrank the denominator and kept the full
    # historical numerator would produce the correct total on production
    # data while being wrong.
    excluded_covered_pages: int = 0
    excluded_outstanding_pages: int = 0

    @property
    def percentage(self) -> float:
        if self.total_pages == 0:
            return 0.0

        return 100.0 * self.covered_pages / self.total_pages

    @property
    def outstanding_pages(self) -> int:
        return self.total_pages - self.covered_pages

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "covered_pages": self.covered_pages,
            "total_pages": self.total_pages,
            "outstanding_pages": self.outstanding_pages,
            "excluded_pages": self.excluded_pages,
            "excluded_archives": self.excluded_archives,
            "excluded_covered_pages": self.excluded_covered_pages,
            "excluded_outstanding_pages": self.excluded_outstanding_pages,
            "percentage": round(self.percentage, 6),
            "percentage_display": f"{self.percentage:.4f}%",
            "basis": self.basis,
        }


def _covered(archive: ArchiveClassification) -> int:
    """Pages of one archive with a complete Version 1 record."""
    return archive.total_pages - archive.outstanding_pages


def measure_historical(
    archives: Sequence[ArchiveClassification],
) -> Coverage:
    """Coverage over every inventoried page, with nothing excluded.

    Every archive contributes, whatever its disposition and whatever the
    filesystem said this run. That is the entire point: this denominator
    is frozen, so the number describes work done rather than the state of
    a drive at the moment somebody ran the audit.
    """
    return Coverage(
        name=MEASUREMENT_HISTORICAL,
        covered_pages=sum(_covered(archive) for archive in archives),
        total_pages=sum(archive.total_pages for archive in archives),
        excluded_pages=0,
        excluded_archives=0,
        basis=MEASUREMENT_BASIS[MEASUREMENT_HISTORICAL],
    )


def dispositioned_archives(
    archives: Sequence[ArchiveClassification],
) -> list[ArchiveClassification]:
    """Identities carrying a recorded retirement or supersession.

    Read from `disposition`, the stored axis, and never from
    `presentation_label`. The label collapses six axes by precedence, so
    counting by it would attribute an archive to whichever axis happened
    to win -- and a retired archive that is also missing would land under
    whichever the precedence table listed first.
    """
    return [
        archive
        for archive in archives
        if archive.disposition in RETIRING_DISPOSITIONS
    ]


def measure_operational(
    archives: Sequence[ArchiveClassification],
) -> Coverage:
    """Coverage over the library still considered ours.

    Only recorded dispositions shrink the denominator. An archive that is
    missing, drifted, unreadable, beneath an unavailable root or outside
    every declared root is still fully counted here, because none of
    those is a decision -- they are things this run happened to observe,
    and an observation must not retire anything.
    """
    excluded = dispositioned_archives(archives)
    excluded_ids = {archive.archive_id for archive in excluded}
    retained = [
        archive
        for archive in archives
        if archive.archive_id not in excluded_ids
    ]

    return Coverage(
        name=MEASUREMENT_OPERATIONAL,
        covered_pages=sum(_covered(archive) for archive in retained),
        total_pages=sum(archive.total_pages for archive in retained),
        excluded_pages=sum(archive.total_pages for archive in excluded),
        excluded_archives=len(excluded),
        excluded_covered_pages=sum(_covered(archive) for archive in excluded),
        excluded_outstanding_pages=sum(
            archive.outstanding_pages for archive in excluded
        ),
        basis=MEASUREMENT_BASIS[MEASUREMENT_OPERATIONAL],
    )


def zero_page_archives(
    archives: Sequence[ArchiveClassification],
) -> list[ArchiveClassification]:
    """Identities that hold no pages and so cannot be measured.

    They are not a coverage finding and they are not an error. They are
    the population that both ratios are structurally unable to describe,
    and accountability exists to keep them visible anyway.
    """
    return [archive for archive in archives if archive.total_pages == 0]


def build_accountability(result: ClassificationResult) -> dict:
    """Every identity, on every axis, with nothing collapsed.

    This is the measurement that must cover the whole library: the two
    coverage ratios describe pages, and an archive with no pages is
    invisible to both.
    """
    archives = result.archives
    zero_page = zero_page_archives(archives)
    unexplained = [
        archive
        for archive in archives
        if archive.selection == SELECTION_UNEXPLAINED
    ]

    return {
        "archive_identities": len(archives),
        "axis_totals": axis_totals(result),
        "selection_reason_totals": selection_reason_totals(result),
        "outstanding_pages_by_axis": {
            axis: outstanding_pages_by_axis(result, axis) for axis in AXES
        },
        "zero_page_identity_count": len(zero_page),
        "zero_page_archive_ids": [
            archive.archive_id for archive in zero_page
        ],
        "zero_page_subreasons": _counted(
            archive.not_inventoried_subreason for archive in zero_page
        ),
        "unexplained_count": len(unexplained),
        "unexplained_archive_ids": [
            archive.archive_id for archive in unexplained
        ],
        "unexplained_explanation": (
            "Residue only: an archive that the contract's eligibility "
            "predicate and its exclusion explanation both failed to "
            "claim. No predicate produces this value. A non-zero count "
            "is a defect in the classification code, not a finding "
            "about the library, and fails the audit."
        ),
    }


def _counted(values) -> dict[str, int]:
    """Count non-null values, sorted, without inventing empty buckets."""
    counts: dict[str, int] = {}

    for value in values:
        if value is None:
            continue

        counts[value] = counts.get(value, 0) + 1

    return dict(sorted(counts.items()))


# --- invariants ----------------------------------------------------------


@dataclass(frozen=True)
class Invariant:
    """One checked claim, and what was actually observed.

    Carrying `detail` even when the check passes matters: a reader who
    does not trust the report should be able to see the numbers the
    check compared, not just the word PASS.
    """

    name: str
    passed: bool
    detail: str

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
        }


def completed_jobs_with_partial_coverage(
    archives: Sequence[ArchiveClassification],
) -> list[ArchiveClassification]:
    """Archives whose latest perceptual job completed, yet lack pages.

    A completed job that left pages outstanding means the worker reported
    success over work it did not finish, which would make every coverage
    number here an overstatement of what actually ran.
    """
    return [
        archive
        for archive in archives
        if archive.perceptual_work == WORK_COMPLETED
        and archive.outstanding_pages > 0
    ]


def check_invariants(
    result: ClassificationResult,
    census: dict[str, int],
    historical: Coverage,
    operational: Coverage,
    identities: dict | None = None,
) -> list[Invariant]:
    """Every claim this report makes about its own arithmetic.

    These are checks, not assertions, so the report is still written when
    one fails -- an operator needs to see the numbers that did not
    reconcile. The exit code is what turns a failure into a refusal.

    `identities` carries the independent `archive_files` census. Omitting
    it falls back to reconciling the classified rows against themselves,
    which still catches duplicates, extras and incomplete tuples but
    *cannot* catch an omitted identity -- so `run_audit` always supplies
    it, and only invariant-level unit tests leave it out.
    """
    archives = result.archives
    identity_total = len(archives)
    invariants: list[Invariant] = []

    def record(name: str, passed: bool, detail: str) -> None:
        invariants.append(Invariant(name=name, passed=passed, detail=detail))

    # 1. One complete tuple per identity, checked against the database's
    #    own identity set rather than against the returned rows.
    #    Comparing the rows with themselves detects a duplicate but is
    #    blind to an omission: a zero-page identity the classifier
    #    dropped contributes nothing to the page census either, so every
    #    other number in this report would still reconcile while the
    #    library had quietly shrunk.
    if identities is None:
        identities = reconcile_identities(
            result, [archive.archive_id for archive in archives]
        )

    record(
        "every_archive_has_exactly_one_complete_tuple",
        identities["missing_count"] == 0
        and identities["extra_count"] == 0
        and identities["duplicate_count"] == 0
        and identities["incomplete_tuple_count"] == 0,
        f"expected {identities['expected_identities']} identities, "
        f"classified {identities['classified_identities']} "
        f"({identities['classified_distinct_identities']} distinct); "
        f"missing={identities['missing_count']} "
        f"extra={identities['extra_count']} "
        f"duplicate={identities['duplicate_count']} "
        f"incomplete={identities['incomplete_tuple_count']}; "
        f"missing_ids={identities['missing_archive_ids'][:10]} "
        f"extra_ids={identities['extra_archive_ids'][:10]}",
    )

    # 2. Each axis independently sums to the identity total. Computed
    #    from the axis itself, never from the presentation label.
    totals = axis_totals(result)
    axis_sums = {axis: sum(totals[axis].values()) for axis in AXES}
    record(
        "every_axis_sums_to_the_identity_total",
        all(value == identity_total for value in axis_sums.values()),
        f"expected {identity_total} per axis; got {axis_sums}",
    )

    # 3./4. Pairing, from the independent census.
    record(
        "dhash_and_phash_counts_are_equal",
        census["dhash_pages"] == census["phash_pages"],
        f"dhash={census['dhash_pages']} phash={census['phash_pages']}",
    )
    record(
        "no_half_paired_pages",
        census["half_paired_pages"] == 0,
        f"half_paired={census['half_paired_pages']}",
    )

    # 5. The contract's per-archive page sums agree with the independent
    #    whole-table scan, in both directions.
    record(
        "historical_denominator_matches_the_page_census",
        historical.total_pages == census["pages"],
        f"classified={historical.total_pages} census={census['pages']}",
    )
    record(
        "covered_pages_match_the_page_census",
        historical.covered_pages == census["covered_pages"],
        f"classified={historical.covered_pages} "
        f"census={census['covered_pages']}",
    )

    # 6. Outstanding pages reconcile by archive and by every axis --
    #    against the *census*, not against another sum over the same
    #    per-archive numbers. Reconciling the per-archive total with the
    #    per-axis total would compare a number with itself: grouping a
    #    set of values by any key and re-summing the groups is an
    #    identity, so that check could never fail and would prove
    #    nothing. The census is an independent scan of archive_pages, so
    #    these comparisons can genuinely disagree.
    outstanding_by_census = census["pages"] - census["covered_pages"]
    outstanding_by_archive = sum(
        archive.outstanding_pages for archive in archives
    )
    record(
        "outstanding_pages_reconcile_by_archive",
        outstanding_by_archive == outstanding_by_census,
        f"by_archive={outstanding_by_archive} "
        f"by_census={outstanding_by_census}",
    )

    axis_page_sums = {
        axis: sum(outstanding_pages_by_axis(result, axis).values())
        for axis in AXES
    }
    record(
        "outstanding_pages_reconcile_by_every_axis",
        all(
            value == outstanding_by_census
            for value in axis_page_sums.values()
        ),
        f"expected {outstanding_by_census} per axis; got {axis_page_sums}",
    )

    # 7. No completed job left partial coverage.
    partial = completed_jobs_with_partial_coverage(archives)
    record(
        "no_completed_job_left_partial_coverage",
        not partial,
        f"{len(partial)} archives, ids "
        f"{[archive.archive_id for archive in partial][:10]}",
    )

    # 8. Residue is empty.
    unexplained = [
        archive
        for archive in archives
        if archive.selection == SELECTION_UNEXPLAINED
    ]
    record(
        "no_unexplained_residue",
        not unexplained,
        f"{len(unexplained)} archives, ids "
        f"{[archive.archive_id for archive in unexplained][:10]}",
    )

    # 9. Operational excluded exactly the dispositioned identities, and
    #    the two denominators differ by exactly their pages.
    dispositioned = dispositioned_archives(archives)
    record(
        "operational_excludes_only_recorded_dispositions",
        operational.excluded_archives == len(dispositioned)
        and operational.excluded_pages
        == sum(archive.total_pages for archive in dispositioned),
        f"excluded {operational.excluded_archives} archives / "
        f"{operational.excluded_pages} pages; dispositioned "
        f"{len(dispositioned)} archives / "
        f"{sum(archive.total_pages for archive in dispositioned)} pages",
    )
    record(
        "operational_denominator_is_historical_minus_dispositioned",
        operational.total_pages
        == historical.total_pages - operational.excluded_pages,
        f"operational={operational.total_pages} historical="
        f"{historical.total_pages} excluded={operational.excluded_pages}",
    )

    # 10. The numerator has to move with the denominator. Checking only
    #     the denominator would pass an implementation that removed a
    #     retired archive's pages from the total while keeping its
    #     covered pages in the numerator -- and on this library that bug
    #     is invisible, because the one retirement on record (archive
    #     45217) has zero covered pages to keep.
    excluded_covered = sum(
        _covered(archive) for archive in dispositioned
    )
    excluded_outstanding = sum(
        archive.outstanding_pages for archive in dispositioned
    )
    record(
        "operational_numerator_is_historical_minus_dispositioned",
        operational.covered_pages
        == historical.covered_pages - excluded_covered
        and operational.excluded_covered_pages == excluded_covered,
        f"operational={operational.covered_pages} historical="
        f"{historical.covered_pages} excluded_covered={excluded_covered} "
        f"reported_excluded_covered="
        f"{operational.excluded_covered_pages}",
    )
    record(
        "operational_outstanding_is_historical_minus_dispositioned",
        operational.outstanding_pages
        == historical.outstanding_pages - excluded_outstanding
        and operational.excluded_outstanding_pages == excluded_outstanding,
        f"operational={operational.outstanding_pages} historical="
        f"{historical.outstanding_pages} excluded_outstanding="
        f"{excluded_outstanding} reported_excluded_outstanding="
        f"{operational.excluded_outstanding_pages}",
    )

    return invariants


# --- report --------------------------------------------------------------


def collect(
    connection: sqlite3.Connection, *, scope: Sequence[str] | None
) -> tuple[ClassificationResult, dict[str, int], list[int]]:
    """Every read this audit performs, inside one snapshot.

    Three reads, deliberately: the classification, an independent page
    census and an independent identity census. All three must come from
    the same snapshot or the reconciliations between them would compare
    different states of the library.

    Kept as a single module-level function, and looked up on the module
    at call time by `run_audit`, so the WAL regression tests can wrap it
    and commit from another connection between the reads -- which is
    precisely the interleaving the data_version bracket exists to catch.
    """
    result = classification_module.classify(connection, scope=scope)
    return result, page_census(connection), identity_census(connection)


_CSV_FIELDNAMES = [
    "archive_id",
    "disposition",
    "successor_archive_id",
    "availability",
    "availability_detail",
    "inventory",
    "not_inventoried_subreason",
    "perceptual_work",
    "ever_failed",
    "ever_cancelled",
    "selection",
    "selection_reasons",
    "quarantine_status",
    "current_path",
    "total_pages",
    "covered_pages",
    "outstanding_pages",
    "presentation_label",
]


def archive_rows(result: ClassificationResult) -> list[dict]:
    """The full classification tuple per archive, for JSON and CSV.

    `presentation_label` is emitted as one extra column beside the tuple,
    never in place of it. Nothing in this module counts by it.
    """
    rows: list[dict] = []

    for archive in result.archives:
        row = archive.as_dict()
        row["covered_pages"] = _covered(archive)
        row["presentation_label"] = presentation_label(archive)
        rows.append(row)

    return rows


def _write_json(path: Path, payload: object) -> Path:
    resolved = path.resolve(strict=False)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return resolved


def _write_csv(path: Path, rows: list[dict]) -> Path:
    resolved = path.resolve(strict=False)
    resolved.parent.mkdir(parents=True, exist_ok=True)

    with resolved.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=_CSV_FIELDNAMES, extrasaction="ignore"
        )
        writer.writeheader()

        for row in rows:
            flattened = dict(row)
            # Reasons are a list on the JSON side, where structure is
            # free; CSV has one cell, so they are joined rather than
            # dropped. A reader must never have to guess that an archive
            # had a second reason the column could not hold.
            flattened["selection_reasons"] = "|".join(
                row.get("selection_reasons") or ()
            )
            writer.writerow(flattened)

    return resolved


def run_audit(
    *,
    database: Path,
    scope: Sequence[str] | None = None,
    json_output: Path | None = None,
    csv_output: Path | None = None,
) -> dict:
    """Produce the read-only accounting report.

    Never mutates `database`. `json_output`/`csv_output` are validated
    against `database` and each other *before* the database is opened or
    any directory is created.

    Raises `FileNotFoundError` if the database does not exist,
    `OutputPathCollisionError` if an output path could clobber it,
    `DatabaseIntegrityError` if `PRAGMA quick_check` fails,
    `ClassificationInvariantError` if the contract refuses to classify,
    and `DatabaseChangedError` (or its `DatabaseMutatedError` subclass)
    if another connection committed during the run or the file's
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

    started = time.perf_counter()
    fingerprint_before = fingerprint_database(database)
    files_before = fingerprint_database_files(database)

    def read(
        connection: sqlite3.Connection,
    ) -> tuple[ClassificationResult, dict[str, int], list[int]]:
        # Looked up on the module at call time, so tests can wrap it.
        return globals()["collect"](connection, scope=scope)

    snapshot = read_consistent_snapshot(
        database,
        read,
        context="audit",
        integrity_check=quick_check,
    )
    result, census, expected_ids = snapshot.result

    # Re-stat *after* the connection is closed: if opening read-only or
    # running any SELECT touched the file (it should not -- mode=ro plus
    # query_only forbid it, but this is the actual guarantee the audit
    # promises), this run is not trustworthy. Checked after the
    # data_version gate, which is the stronger detector, so a concurrent
    # commit is reported as exactly that rather than as "the file moved".
    fingerprint_after = fingerprint_database(database)
    files_after = fingerprint_database_files(database)

    if fingerprint_after != fingerprint_before:
        raise DatabaseMutatedError(
            "Database changed during a read-only audit run: "
            f"before={fingerprint_before} after={fingerprint_after}. "
            "This audit must never modify the database it inspects."
        )

    elapsed = time.perf_counter() - started

    historical = measure_historical(result.archives)
    operational = measure_operational(result.archives)
    accountability = build_accountability(result)
    identities = reconcile_identities(result, expected_ids)
    invariants = check_invariants(
        result, census, historical, operational, identities
    )
    failed = [invariant for invariant in invariants if not invariant.passed]

    output = {
        "database": str(database),
        "job_type": JOB_TYPE,
        "declared_scope": result.scope.as_dict(),
        "scope_digest": result.scope.digest,
        "filesystem_consulted": result.filesystem_consulted,
        "page_census": census,
        "identity_census": identities,
        "measurements": {
            MEASUREMENT_HISTORICAL: historical.as_dict(),
            MEASUREMENT_OPERATIONAL: operational.as_dict(),
            "accountability": accountability,
        },
        "invariants": [invariant.as_dict() for invariant in invariants],
        "invariants_passed": not failed,
        "failed_invariants": [invariant.name for invariant in failed],
        "archives": archive_rows(result),
        # Stated affirmatively as well as via `concurrent_commit_detected`:
        # the read-only claim is the one a reviewer checks first, and it
        # should not have to be read as a negation.
        "data_version_unchanged": snapshot.data_version_unchanged,
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
        output["csv_output"] = str(_write_csv(csv_output, output["archives"]))

    return output


# --- console -------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only perceptual-coverage accounting over every archive "
            "identity, built on the shared classification contract. "
            "Reports historical coverage (frozen page denominator), "
            "operational coverage (only recorded retirements and "
            "supersessions excluded) and accountability (every identity, "
            "including zero-page ones), and fails on any unexplained "
            "residue. Never enqueues, retries, quarantines or moves "
            "anything; safe to point at a protected backup."
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
        "--scope",
        action="append",
        metavar="ROOT",
        help=(
            "A declared filesystem root this run may observe. Repeat for "
            "several. Omit entirely to skip the filesystem: availability "
            "is then reported as not_observed and nothing is stat()ed, "
            "which is an honest answer rather than a guess. Results taken "
            "under different scopes are not comparable; the canonical "
            "digest is printed so they cannot be confused."
        ),
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="Optional path for the JSON accounting report.",
    )
    parser.add_argument(
        "--csv-output",
        type=Path,
        help="Optional path for the per-archive classification CSV.",
    )
    return parser


def print_archive_id_sample(
    archive_ids: Sequence[int],
    *,
    indent: str = "  ",
    label: str = "ARCHIVE IDS",
    limit: int = MAX_PRINTED_ARCHIVE_IDS,
) -> None:
    """Print a capped sample, always stating what was omitted."""
    if not archive_ids:
        return

    shown = list(archive_ids[:limit])
    omitted = len(archive_ids) - len(shown)
    print(f"{indent}{label}: {', '.join(str(value) for value in shown)}")

    if omitted:
        print(
            f"{indent}  ... and {omitted} more "
            "(full list in the JSON and CSV outputs)"
        )


def print_scope(output: dict) -> None:
    """The declared roots and digest, printed before any number.

    First, deliberately. Every figure below is conditional on this scope,
    and a reader who skips it can compare two incomparable reports.
    """
    scope = output["declared_scope"]
    print("DECLARED SCOPE")

    if not scope["roots"]:
        print(
            "  (none declared -- the filesystem was not consulted; "
            "availability is not_observed for every archive)"
        )
    else:
        for entry in scope["roots"]:
            state = "reachable" if entry["reachable"] else "UNREACHABLE"
            print(f"  {entry['root']}  [{state}]")

    print(f"  canonical scope digest: {scope['digest']}")
    print(f"  filesystem consulted:   {output['filesystem_consulted']}")
    print()


def _print_coverage(coverage: dict) -> None:
    print(f"  {coverage['name'].upper()}")
    print(
        f"    covered / total: {coverage['covered_pages']:,} / "
        f"{coverage['total_pages']:,} = {coverage['percentage_display']}"
    )
    print(f"    outstanding:     {coverage['outstanding_pages']:,}")
    print(
        f"    excluded:        {coverage['excluded_pages']:,} pages "
        f"across {coverage['excluded_archives']:,} identities"
    )
    # Printed so the numerator's derivation is visible: a denominator
    # that shrank by the right amount says nothing about whether the
    # covered pages moved with it.
    print(
        f"      of which covered:     "
        f"{coverage['excluded_covered_pages']:,}"
    )
    print(
        f"      of which outstanding: "
        f"{coverage['excluded_outstanding_pages']:,}"
    )
    print(f"    basis:           {coverage['basis']}")


def print_summary(output: dict) -> None:
    """The whole report, scope first and invariants last."""
    print()
    print("=" * 72)
    print("PERCEPTUAL COVERAGE ACCOUNTING")
    print("=" * 72)
    print(f"database: {output['database']}")
    print()

    print_scope(output)

    measurements = output["measurements"]
    accountability = measurements["accountability"]

    print("COVERAGE")
    _print_coverage(measurements[MEASUREMENT_HISTORICAL])
    print()
    _print_coverage(measurements[MEASUREMENT_OPERATIONAL])
    print()

    census = output["page_census"]
    print("PAGE CENSUS (independent of the classification)")
    print(f"  pages:                    {census['pages']:,}")
    print(f"  dHash v1:                 {census['dhash_pages']:,}")
    print(f"  pHash v1:                 {census['phash_pages']:,}")
    print(f"  half-paired pages:        {census['half_paired_pages']:,}")
    print(
        f"  missing dimensions:       "
        f"{census['pages_missing_dimensions']:,}"
    )
    print()

    identities = output["identity_census"]
    print("IDENTITY CENSUS (independent of the classification)")
    print(
        f"  archive_files rows:       "
        f"{identities['expected_identities']:,}"
    )
    print(
        f"  classified rows:          "
        f"{identities['classified_identities']:,}"
    )
    print(f"  missing:                  {identities['missing_count']:,}")
    print_archive_id_sample(
        identities["missing_archive_ids"], indent="    ", label="MISSING"
    )
    print(f"  extra:                    {identities['extra_count']:,}")
    print_archive_id_sample(
        identities["extra_archive_ids"], indent="    ", label="EXTRA"
    )
    print(f"  duplicate:                {identities['duplicate_count']:,}")
    print_archive_id_sample(
        identities["duplicate_archive_ids"],
        indent="    ",
        label="DUPLICATE",
    )
    print(
        f"  incomplete tuples:        "
        f"{identities['incomplete_tuple_count']:,}"
    )
    print()

    print("ACCOUNTABILITY")
    print(
        f"  archive identities:       "
        f"{accountability['archive_identities']:,}"
    )
    print(
        f"  zero-page identities:     "
        f"{accountability['zero_page_identity_count']:,}"
    )

    for subreason, count in accountability["zero_page_subreasons"].items():
        print(f"    {subreason}: {count:,}")

    print(f"  unexplained residue:      {accountability['unexplained_count']:,}")
    print_archive_id_sample(
        accountability["unexplained_archive_ids"],
        indent="    ",
        label="UNEXPLAINED",
    )
    print()

    for axis in AXES:
        print(f"  {axis}")
        pages = accountability["outstanding_pages_by_axis"][axis]

        for value, count in accountability["axis_totals"][axis].items():
            print(
                f"    {value:<28} {count:>8,} identities  "
                f"{pages.get(value, 0):>9,} outstanding pages"
            )

        print()

    print("INVARIANTS")

    for invariant in output["invariants"]:
        state = "PASS" if invariant["passed"] else "FAIL"
        print(f"  [{state}] {invariant['name']}")

        if not invariant["passed"]:
            print(f"         {invariant['detail']}")

    print()
    print("READ-ONLY EVIDENCE")
    print(
        f"  data_version before/after: "
        f"{output['data_version_before']} / {output['data_version_after']}"
    )
    print(f"  data_version unchanged:    {output['data_version_unchanged']}")
    print(
        f"  concurrent commit:         "
        f"{output['concurrent_commit_detected']}"
    )
    print(f"  quick_check:               {output['quick_check']}")
    # Labelled as the weaker signal it is: under WAL a commit can leave
    # the main file's size and mtime identical, so this confirms nothing
    # about concurrency on its own.
    print(
        f"  file unchanged (diagnostic only): "
        f"{output['database_file_unchanged']}"
    )
    print(f"  elapsed: {output['elapsed_seconds']}s")
    print("=" * 72)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        output = run_audit(
            database=args.database,
            scope=args.scope,
            json_output=args.json_output,
            csv_output=args.csv_output,
        )
    except (
        FileNotFoundError,
        OutputPathCollisionError,
        DatabaseIntegrityError,
        DatabaseChangedError,
        ClassificationInvariantError,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return EXIT_FAILURE

    print_summary(output)

    if not output["invariants_passed"]:
        print(
            "FAILED INVARIANTS: "
            + ", ".join(output["failed_invariants"]),
            file=sys.stderr,
        )
        return EXIT_INVARIANT_VIOLATION

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
