"""The shared, read-only classification contract for archive identities.

Every archive gets one row here, and that row answers six independent
questions rather than one. The independence is the whole design: an archive
can be simultaneously **active** (nobody decided anything about it),
**unavailable** (its root is not mounted), **fully hashed** (its pages were
covered while the root was there) and **not eligible** (nothing is left to
enqueue). Collapsing those into a single "population" is what let 178 archives
go unreported for three weeks, and what let 95 archives under a root that no
longer exists be counted as complete.

The six axes
------------

``disposition``
    A recorded decision: ``active`` (no row in any disposition table),
    ``retired``, or ``superseded`` by a named successor. This is the only axis
    that is *stored*. See `comic_automation/archive/disposition.py`.

``availability``
    What the filesystem says right now. Never stored, never inferred from a
    decision, and -- critically -- never allowed to *become* one. An
    unreachable scope is reported as ``unavailable_declared_scope``, which is
    a statement about the observer, not about the content: this module must
    never call it missing or gone.

``inventory``
    Whether pages were inventoried and whether they are covered. When they
    were not, a sub-reason says why, because "no pages" spans an archive that
    was never inspected, one whose inspection failed, and one that genuinely
    contains no images -- three different situations that a single count of
    1,256 cannot distinguish.

``perceptual_work``
    What the perceptual job history shows: ``never_enqueued``, ``active``,
    ``completed``, ``failed``, ``cancelled``. Kept distinguishable on purpose;
    archive 45217 is cancelled *and* retired, and both facts matter.

``selection``
    What the real selection path would do: ``eligible``, ``refused`` with
    `candidate_selection`'s reasons, ``excluded`` with the eligibility
    predicate's reasons, or ``unexplained``.

``quarantine``
    Reported alongside, never as a disposition. ``pending_redownload`` means
    "we intend to get this back", which is in-scope-and-broken. Treating it as
    a disposition would quietly remove archives from operational scope on the
    strength of an intention.

What ``unexplained`` means
--------------------------

Residue, and only residue: an archive that is neither in the eligible set nor
explained by an exclusion reason. There is no predicate that produces it. This
is the direct replacement for the ``never_enqueued_backlog`` flag, whose
positive predicate ("eligible, zero coverage, no job") reported 225 fully
explained archives as blocking gaps while staying silent on the 162 that had
no explanation at all.

Under a correct database ``unexplained`` is always zero, because
`_eligible_archive_rows()` and `_archive_exclusion_reasons()` partition the
library. A non-zero count means those two disagree, which is a defect in this
code rather than in the data, and is reported as such.

Failing closed
--------------

Three conditions abort classification rather than being reported as findings,
because each means a stored invariant has been bypassed and any report built
on top of it would be describing a state the schema says cannot exist:
conflicting dispositions, supersession cycles, and a broken eligible/excluded
partition.

This module never writes. It issues only SELECTs and `os.stat`, so it is safe
on a connection opened with SQLite's ``mode=ro`` URI flag plus
``PRAGMA query_only = ON``, and safe to point at a protected backup.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat as stat_module
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from comic_automation.archive import disposition as disposition_module
from comic_automation.archive.candidate_selection import (
    REJECTION_REASONS,
    select_candidates,
)
from comic_automation.archive.perceptual_hashing import (
    EXCLUSION_REASONS,
    ArchivePerceptualHashRepository,
)


# --- axis vocabularies ---------------------------------------------------
#
# Every value a report may emit, listed once. A report that prints a value not
# in these tuples is printing something no reader has a definition for.

DISPOSITION_ACTIVE = "active"
DISPOSITION_RETIRED = "retired"
DISPOSITION_SUPERSEDED = "superseded"

DISPOSITIONS = (
    DISPOSITION_ACTIVE,
    DISPOSITION_RETIRED,
    DISPOSITION_SUPERSEDED,
)

AVAILABILITY_NOT_OBSERVED = "not_observed"
AVAILABILITY_PRESENT_MATCHING = "present_matching"
AVAILABILITY_PRESENT_DRIFTED = "present_drifted"
AVAILABILITY_MISSING = "missing"
AVAILABILITY_UNREADABLE = "unreadable"
AVAILABILITY_NON_REGULAR = "non_regular"
AVAILABILITY_UNAVAILABLE_SCOPE = "unavailable_declared_scope"
AVAILABILITY_UNDECLARED_SCOPE = "undeclared_scope"
AVAILABILITY_NO_CURRENT_LOCATION = "no_current_location"
AVAILABILITY_MULTIPLE_CURRENT_LOCATIONS = "multiple_current_locations"

AVAILABILITIES = (
    AVAILABILITY_NOT_OBSERVED,
    AVAILABILITY_PRESENT_MATCHING,
    AVAILABILITY_PRESENT_DRIFTED,
    AVAILABILITY_MISSING,
    AVAILABILITY_UNREADABLE,
    AVAILABILITY_NON_REGULAR,
    AVAILABILITY_UNAVAILABLE_SCOPE,
    AVAILABILITY_UNDECLARED_SCOPE,
    AVAILABILITY_NO_CURRENT_LOCATION,
    AVAILABILITY_MULTIPLE_CURRENT_LOCATIONS,
)

# Availability values that say something about the *observer* rather than the
# content. A report must never describe these as missing or gone: the file may
# be perfectly intact on a volume nobody asked about.
AVAILABILITY_UNOBSERVABLE = (
    AVAILABILITY_NOT_OBSERVED,
    AVAILABILITY_UNAVAILABLE_SCOPE,
    AVAILABILITY_UNDECLARED_SCOPE,
)

INVENTORY_COVERED = "inventoried_covered"
INVENTORY_INCOMPLETE = "inventoried_incomplete"
INVENTORY_NOT_INVENTORIED = "not_inventoried"

INVENTORIES = (
    INVENTORY_COVERED,
    INVENTORY_INCOMPLETE,
    INVENTORY_NOT_INVENTORIED,
)

NOT_INVENTORIED_INSPECTION_NEVER_ENQUEUED = "inspection_never_enqueued"
NOT_INVENTORIED_INSPECTION_ACTIVE = "inspection_active"
NOT_INVENTORIED_INSPECTION_FAILED = "inspection_failed"
NOT_INVENTORIED_INSPECTION_CANCELLED = "inspection_cancelled"
NOT_INVENTORIED_COMPLETED_NO_IMAGES = "completed_no_images"
NOT_INVENTORIED_COMPLETED_INVENTORY_ABSENT = "completed_inventory_absent"
NOT_INVENTORIED_QUARANTINE_PENDING_REDOWNLOAD = "quarantine_pending_redownload"
NOT_INVENTORIED_UNKNOWN = "unknown_residue"

NOT_INVENTORIED_SUBREASONS = (
    NOT_INVENTORIED_INSPECTION_NEVER_ENQUEUED,
    NOT_INVENTORIED_INSPECTION_ACTIVE,
    NOT_INVENTORIED_INSPECTION_FAILED,
    NOT_INVENTORIED_INSPECTION_CANCELLED,
    NOT_INVENTORIED_COMPLETED_NO_IMAGES,
    NOT_INVENTORIED_COMPLETED_INVENTORY_ABSENT,
    NOT_INVENTORIED_QUARANTINE_PENDING_REDOWNLOAD,
    NOT_INVENTORIED_UNKNOWN,
)

WORK_NEVER_ENQUEUED = "never_enqueued"
WORK_ACTIVE = "active"
WORK_COMPLETED = "completed"
WORK_FAILED = "failed"
WORK_CANCELLED = "cancelled"

WORK_STATES = (
    WORK_NEVER_ENQUEUED,
    WORK_ACTIVE,
    WORK_COMPLETED,
    WORK_FAILED,
    WORK_CANCELLED,
)

SELECTION_ELIGIBLE = "eligible"
SELECTION_REFUSED = "refused"
SELECTION_EXCLUDED = "excluded"
SELECTION_UNEXPLAINED = "unexplained"

SELECTIONS = (
    SELECTION_ELIGIBLE,
    SELECTION_REFUSED,
    SELECTION_EXCLUDED,
    SELECTION_UNEXPLAINED,
)

PERCEPTUAL_JOB_TYPE = "hash_archive_pages_perceptual"
INSPECT_JOB_TYPE = "inspect_archive"
ACTIVE_JOB_STATUSES = ("pending", "claimed", "running")

AXES = (
    "disposition",
    "availability",
    "inventory",
    "perceptual_work",
    "selection",
)


class ClassificationInvariantError(RuntimeError):
    """A stored invariant was violated, so no report can be trusted."""


class PartitionError(ClassificationInvariantError):
    """Eligible and excluded archives do not partition the library."""


# --- declared scope ------------------------------------------------------


@dataclass(frozen=True)
class DeclaredScope:
    """The filesystem roots this run was told to consider, and their state.

    There is no configured root list anywhere in this project -- every
    discovery scan this database has seen used a single `source_path` -- so
    scope is an input to the run rather than a fact about the system. It is
    carried explicitly, with each root's reachability, so two reports taken
    under different scopes are visibly incomparable instead of quietly
    disagreeing.

    `roots=None` means the filesystem was not consulted at all, and every
    archive with a location is reported ``not_observed``. That is an honest
    answer, and a better one than a guess.
    """

    roots: tuple[str, ...] = ()
    reachable: tuple[bool, ...] = ()

    @classmethod
    def declare(cls, roots: Iterable[str] | None) -> "DeclaredScope":
        if roots is None:
            return cls(roots=(), reachable=())

        ordered = tuple(str(root).rstrip("\\/") for root in roots)
        return cls(
            roots=ordered,
            reachable=tuple(os.path.isdir(root) for root in ordered),
        )

    @property
    def consulted(self) -> bool:
        """True when this run was given roots to look at."""
        return bool(self.roots)

    @property
    def digest(self) -> str:
        """Fingerprint of the declared set, for comparing two reports."""
        joined = "\n".join(
            f"{root}\t{reachable}"
            for root, reachable in zip(self.roots, self.reachable)
        )
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    def containing_root(self, path: str) -> tuple[str, bool] | None:
        """The declared root holding `path`, and whether it is reachable."""
        candidate = str(path).replace("/", "\\").casefold()

        for root, reachable in zip(self.roots, self.reachable):
            prefix = root.replace("/", "\\").casefold() + "\\"

            if candidate.startswith(prefix):
                return root, reachable

        return None

    def as_dict(self) -> dict:
        return {
            "consulted": self.consulted,
            "digest": self.digest,
            "roots": [
                {"root": root, "reachable": reachable}
                for root, reachable in zip(self.roots, self.reachable)
            ],
        }


# --- the classification --------------------------------------------------


@dataclass(frozen=True)
class ArchiveClassification:
    """One archive identity, on every axis at once."""

    archive_id: int
    disposition: str
    availability: str
    inventory: str
    perceptual_work: str
    selection: str
    successor_archive_id: int | None = None
    availability_detail: str | None = None
    not_inventoried_subreason: str | None = None
    selection_reasons: tuple[str, ...] = ()
    quarantine_status: str | None = None
    current_path: str | None = None
    total_pages: int = 0
    outstanding_pages: int = 0

    @property
    def dispositioned(self) -> bool:
        """True when a decision was recorded about this archive."""
        return self.disposition != DISPOSITION_ACTIVE

    def as_dict(self) -> dict:
        return {
            "archive_id": self.archive_id,
            "disposition": self.disposition,
            "successor_archive_id": self.successor_archive_id,
            "availability": self.availability,
            "availability_detail": self.availability_detail,
            "inventory": self.inventory,
            "not_inventoried_subreason": self.not_inventoried_subreason,
            "perceptual_work": self.perceptual_work,
            "selection": self.selection,
            "selection_reasons": list(self.selection_reasons),
            "quarantine_status": self.quarantine_status,
            "current_path": self.current_path,
            "total_pages": self.total_pages,
            "outstanding_pages": self.outstanding_pages,
        }


@dataclass(frozen=True)
class ClassificationResult:
    archives: tuple[ArchiveClassification, ...]
    scope: DeclaredScope
    filesystem_consulted: bool

    def __len__(self) -> int:
        return len(self.archives)


# --- collection helpers --------------------------------------------------


def _structural_rows(connection: sqlite3.Connection) -> dict[int, dict]:
    rows = connection.execute(
        """
        SELECT
            af.id AS archive_id,
            (
                SELECT COUNT(*) FROM file_locations AS fl
                WHERE fl.archive_id = af.id AND fl.is_current = 1
            ) AS current_location_count,
            (
                SELECT fl.path FROM file_locations AS fl
                WHERE fl.archive_id = af.id AND fl.is_current = 1
                ORDER BY fl.id LIMIT 1
            ) AS current_path,
            (
                SELECT fl.file_size FROM file_locations AS fl
                WHERE fl.archive_id = af.id AND fl.is_current = 1
                ORDER BY fl.id LIMIT 1
            ) AS location_file_size,
            (
                SELECT fl.modified_time_ns FROM file_locations AS fl
                WHERE fl.archive_id = af.id AND fl.is_current = 1
                ORDER BY fl.id LIMIT 1
            ) AS location_modified_time_ns
        FROM archive_files AS af
        ORDER BY af.id
        """
    ).fetchall()

    return {int(row["archive_id"]): dict(row) for row in rows}


def _page_totals(connection: sqlite3.Connection) -> dict[int, int]:
    return {
        int(row["archive_id"]): int(row["total_pages"])
        for row in connection.execute(
            "SELECT archive_id, COUNT(*) AS total_pages "
            "FROM archive_pages GROUP BY archive_id"
        )
    }


def _job_states(
    connection: sqlite3.Connection, job_type: str
) -> dict[int, Counter]:
    states: dict[int, Counter] = defaultdict(Counter)

    for row in connection.execute(
        "SELECT archive_id, status, COUNT(*) AS n FROM jobs "
        "WHERE job_type = ? AND archive_id IS NOT NULL "
        "GROUP BY archive_id, status",
        (job_type,),
    ):
        states[int(row["archive_id"])][str(row["status"])] = int(row["n"])

    return states


def _quarantine_statuses(connection: sqlite3.Connection) -> dict[int, str]:
    return {
        int(row["archive_id"]): str(row["status"])
        for row in connection.execute(
            "SELECT archive_id, status FROM archive_quarantine"
        )
    }


def _work_state(statuses: Counter) -> str:
    """Collapse one archive's perceptual jobs into a single work state.

    Ordered by what a reader needs to act on, not by recency. A failure
    outranks a completion because it is the thing still requiring a decision;
    a cancellation outranks a completion for the same reason. Archive 45217 is
    exactly this case -- cancelled *and* retired -- and both facts survive,
    because they live on different axes.
    """
    if not statuses:
        return WORK_NEVER_ENQUEUED

    if any(statuses.get(status) for status in ACTIVE_JOB_STATUSES):
        return WORK_ACTIVE

    if statuses.get("failed"):
        return WORK_FAILED

    if statuses.get("cancelled"):
        return WORK_CANCELLED

    if statuses.get("completed"):
        return WORK_COMPLETED

    return WORK_NEVER_ENQUEUED


def _not_inventoried_subreason(
    inspect_statuses: Counter,
    quarantine_status: str | None,
) -> str:
    """Why an archive has no pages.

    Quarantine is checked first only because it is the most specific
    statement available: `pending_redownload` says an operator already looked
    at this archive and is waiting on a replacement, which is more useful than
    repeating that its inspection failed.
    """
    if quarantine_status == "pending_redownload":
        return NOT_INVENTORIED_QUARANTINE_PENDING_REDOWNLOAD

    if not inspect_statuses:
        return NOT_INVENTORIED_INSPECTION_NEVER_ENQUEUED

    if any(inspect_statuses.get(status) for status in ACTIVE_JOB_STATUSES):
        return NOT_INVENTORIED_INSPECTION_ACTIVE

    if inspect_statuses.get("failed"):
        return NOT_INVENTORIED_INSPECTION_FAILED

    if inspect_statuses.get("cancelled"):
        return NOT_INVENTORIED_INSPECTION_CANCELLED

    if inspect_statuses.get("completed"):
        # Inspection ran and wrote no pages. Whether that means "this archive
        # holds no images" or "the inventory was lost afterwards" is not
        # something the job row can settle, so both are named and neither is
        # asserted: the signature is the only other witness.
        return NOT_INVENTORIED_COMPLETED_NO_IMAGES

    return NOT_INVENTORIED_UNKNOWN


def _availability(
    info: dict,
    scope: DeclaredScope,
) -> tuple[str, str | None]:
    """Where the file is, as observed -- never as decided."""
    locations = int(info["current_location_count"] or 0)

    if locations == 0:
        return AVAILABILITY_NO_CURRENT_LOCATION, None

    if locations > 1:
        return (
            AVAILABILITY_MULTIPLE_CURRENT_LOCATIONS,
            f"{locations} current locations",
        )

    path = str(info["current_path"])

    if not scope.consulted:
        return AVAILABILITY_NOT_OBSERVED, "no scope declared for this run"

    containing = scope.containing_root(path)

    if containing is None:
        return (
            AVAILABILITY_UNDECLARED_SCOPE,
            "path lies outside every declared root",
        )

    root, reachable = containing

    if not reachable:
        # Deliberately not "missing". The file may be perfectly intact on a
        # volume that is simply not attached; saying otherwise would turn an
        # observation about this machine into a claim about the content.
        return (
            AVAILABILITY_UNAVAILABLE_SCOPE,
            f"declared root {root} is not reachable",
        )

    try:
        result = os.stat(path)
    except FileNotFoundError:
        return AVAILABILITY_MISSING, path
    except OSError as error:
        # An unreadable path is not evidence of absence. Treating a
        # permission or I/O error as "missing" is what previously sent repair
        # hunting for a replacement file that was never gone.
        return AVAILABILITY_UNREADABLE, f"{path}: {error}"

    if not stat_module.S_ISREG(result.st_mode):
        kind = (
            "directory"
            if stat_module.S_ISDIR(result.st_mode)
            else "special file"
        )
        return AVAILABILITY_NON_REGULAR, f"{path} ({kind})"

    if (
        result.st_size == info["location_file_size"]
        and result.st_mtime_ns == info["location_modified_time_ns"]
    ):
        return AVAILABILITY_PRESENT_MATCHING, None

    return (
        AVAILABILITY_PRESENT_DRIFTED,
        f"on disk {result.st_size} @ {result.st_mtime_ns}, recorded "
        f"{info['location_file_size']} @ "
        f"{info['location_modified_time_ns']}",
    )


# --- the contract --------------------------------------------------------


def classify(
    connection: sqlite3.Connection,
    *,
    scope: Iterable[str] | None = None,
) -> ClassificationResult:
    """Classify every archive identity on all six axes.

    `scope` names the filesystem roots this run may look at. Omit it to skip
    the filesystem entirely, in which case availability is ``not_observed``
    and selection reports the database-level answer -- honest, and clearly
    labelled as such by `ClassificationResult.filesystem_consulted`.

    Raises `ClassificationInvariantError` when a stored invariant has been
    bypassed: conflicting dispositions, a supersession cycle, or a broken
    eligible/excluded partition. Those are not findings to report, they are
    reasons the report cannot be built.
    """
    declared = DeclaredScope.declare(scope)

    # --- fail closed on bypassed stored invariants --------------------
    conflicts = disposition_module.conflicting_dispositions(connection)

    if conflicts:
        raise ClassificationInvariantError(
            "Archives hold both a retirement and a supersession, which "
            f"migration 013 forbids: {conflicts}. The database's own "
            "constraints have been bypassed; no classification built on "
            "these rows would be trustworthy."
        )

    dispositions = disposition_module.dispositions_for(connection)
    successors = disposition_module.successor_map(connection)

    for archive_id in successors:
        try:
            disposition_module.resolve_successor(
                connection, archive_id, successors=successors
            )
        except disposition_module.SupersessionChainError as error:
            # Re-raised as an invariant failure so a caller can catch one
            # exception type for "this database cannot be classified",
            # rather than having to know that a chain walk lives underneath.
            raise ClassificationInvariantError(
                f"Supersession chain from archive {archive_id} does not "
                f"terminate: {error}. Migration 013's cycle trigger forbids "
                "this, so the constraint has been bypassed."
            ) from error

    # --- facts --------------------------------------------------------
    repository = ArchivePerceptualHashRepository(connection)
    structural = _structural_rows(connection)
    totals = _page_totals(connection)
    outstanding = repository.outstanding_page_counts()
    perceptual_jobs = _job_states(connection, PERCEPTUAL_JOB_TYPE)
    inspect_jobs = _job_states(connection, INSPECT_JOB_TYPE)
    quarantine = _quarantine_statuses(connection)

    eligible_ids = {
        int(row["archive_id"])
        for row in repository._eligible_archive_rows(limit=None)
    }
    exclusions = repository._archive_exclusion_reasons()

    # --- the partition ------------------------------------------------
    #
    # Overlap and residue are treated differently on purpose.
    #
    # An archive that is both eligible and excluded is a contradiction: the
    # predicate and its explanation assert opposite things about the same row,
    # and there is no honest way to report that as a finding, so it fails
    # closed.
    #
    # An archive that is in neither is residue, and residue is exactly what
    # the `unexplained` selection value exists to carry. Raising on it would
    # make the value unreachable and hide the one condition a reader most
    # needs to see -- which is how `never_enqueued_backlog` came to report 225
    # explained archives as gaps while saying nothing about 162 unexplained
    # ones. It is reported, counted, and asserted to be zero by tests.
    overlap = eligible_ids & set(exclusions)

    if overlap:
        raise PartitionError(
            "Archives are both eligible and excluded, so the eligibility "
            f"predicate and its explanation disagree: {sorted(overlap)[:20]}"
        )

    # --- refusals, from the real selection path -----------------------
    #
    # Not a reimplementation: this is the same function enqueue_missing()
    # calls, so a refusal reported here is a refusal that would actually
    # happen. check_filesystem follows the declared scope, because a refusal
    # this run could not test must not be asserted.
    selection = select_candidates(
        connection,
        sorted(eligible_ids),
        check_filesystem=declared.consulted,
    )
    accepted_ids = set(selection.accepted_ids)
    refusals: dict[int, list[str]] = defaultdict(list)

    for rejection in selection.rejected:
        refusals[int(rejection.archive_id)].append(rejection.reason)

    # --- assemble -----------------------------------------------------
    classifications: list[ArchiveClassification] = []

    for archive_id in sorted(structural):
        info = structural[archive_id]
        recorded = dispositions.get(archive_id)
        total_pages = totals.get(archive_id, 0)
        outstanding_pages = outstanding.get(archive_id, 0)
        quarantine_status = quarantine.get(archive_id)

        if total_pages == 0:
            inventory = INVENTORY_NOT_INVENTORIED
            subreason = _not_inventoried_subreason(
                inspect_jobs.get(archive_id, Counter()), quarantine_status
            )
        elif outstanding_pages == 0:
            inventory, subreason = INVENTORY_COVERED, None
        else:
            inventory, subreason = INVENTORY_INCOMPLETE, None

        if archive_id in accepted_ids:
            selection_state: str = SELECTION_ELIGIBLE
            reasons: tuple[str, ...] = ()
        elif archive_id in refusals:
            selection_state = SELECTION_REFUSED
            reasons = tuple(refusals[archive_id])
        elif archive_id in exclusions:
            selection_state = SELECTION_EXCLUDED
            reasons = exclusions[archive_id]
        else:
            # Residue. Never produced by a predicate -- an archive lands here
            # only by being in none of the sets above.
            selection_state = SELECTION_UNEXPLAINED
            reasons = ()

        availability, detail = _availability(info, declared)

        classifications.append(
            ArchiveClassification(
                archive_id=archive_id,
                disposition=(
                    recorded.disposition if recorded else DISPOSITION_ACTIVE
                ),
                successor_archive_id=(
                    recorded.successor_archive_id if recorded else None
                ),
                availability=availability,
                availability_detail=detail,
                inventory=inventory,
                not_inventoried_subreason=subreason,
                perceptual_work=_work_state(
                    perceptual_jobs.get(archive_id, Counter())
                ),
                selection=selection_state,
                selection_reasons=reasons,
                quarantine_status=quarantine_status,
                current_path=info["current_path"],
                total_pages=total_pages,
                outstanding_pages=outstanding_pages,
            )
        )

    return ClassificationResult(
        archives=tuple(classifications),
        scope=declared,
        filesystem_consulted=declared.consulted,
    )


# --- reporting -----------------------------------------------------------


def axis_totals(
    result: ClassificationResult | Sequence[ArchiveClassification],
) -> dict[str, dict[str, int]]:
    """Counts per axis, each computed from that axis alone.

    Every axis independently sums to the archive count, and no total here is
    derived from `presentation_label` -- the axes are orthogonal, so counting
    a single collapsed label would silently under-report every axis but the
    one that happened to win precedence.
    """
    archives = _as_sequence(result)
    totals: dict[str, dict[str, int]] = {}

    for axis, vocabulary in (
        ("disposition", DISPOSITIONS),
        ("availability", AVAILABILITIES),
        ("inventory", INVENTORIES),
        ("perceptual_work", WORK_STATES),
        ("selection", SELECTIONS),
    ):
        counts = Counter(getattr(archive, axis) for archive in archives)
        # Every value present, including zeros: a category that vanishes when
        # empty cannot be told from one nobody thought to report.
        totals[axis] = {value: counts.get(value, 0) for value in vocabulary}

    totals["not_inventoried_subreason"] = {
        value: sum(
            1
            for archive in archives
            if archive.not_inventoried_subreason == value
        )
        for value in NOT_INVENTORIED_SUBREASONS
    }
    totals["quarantine_status"] = dict(
        Counter(
            archive.quarantine_status
            for archive in archives
            if archive.quarantine_status
        )
    )

    return totals


def selection_reason_totals(
    result: ClassificationResult | Sequence[ArchiveClassification],
) -> dict[str, dict[str, int]]:
    """Refusal and exclusion reasons counted separately.

    An archive with several exclusion reasons is counted under each, so these
    deliberately do not sum to the archive count -- unlike the axis totals,
    which do.
    """
    archives = _as_sequence(result)
    refused: Counter = Counter()
    excluded: Counter = Counter()

    for archive in archives:
        target = (
            refused if archive.selection == SELECTION_REFUSED else excluded
        )

        if archive.selection in (SELECTION_REFUSED, SELECTION_EXCLUDED):
            for reason in archive.selection_reasons:
                target[reason.split(":", 1)[0]] += 1

    return {
        "refused": {
            reason: refused.get(reason, 0) for reason in REJECTION_REASONS
        },
        "excluded": {
            reason: excluded.get(reason, 0) for reason in EXCLUSION_REASONS
        },
    }


def outstanding_pages_by_axis(
    result: ClassificationResult | Sequence[ArchiveClassification],
    axis: str,
) -> dict[str, int]:
    """Unhashed pages attributed to each value of one axis.

    The page column is what lets a report reconcile itself against the
    measured gap; an archive count alone cannot.
    """
    archives = _as_sequence(result)
    pages: Counter = Counter()

    for archive in archives:
        pages[getattr(archive, axis)] += archive.outstanding_pages

    return dict(pages)


# Presentation only. Nothing in this module counts by the value it returns,
# and a test asserts that reordering it leaves every axis total unchanged.
PRESENTATION_PRECEDENCE: tuple[tuple[str, str], ...] = (
    ("disposition", DISPOSITION_RETIRED),
    ("disposition", DISPOSITION_SUPERSEDED),
    ("selection", SELECTION_UNEXPLAINED),
    ("perceptual_work", WORK_FAILED),
    ("availability", AVAILABILITY_UNAVAILABLE_SCOPE),
    ("availability", AVAILABILITY_UNDECLARED_SCOPE),
    ("availability", AVAILABILITY_NOT_OBSERVED),
    ("availability", AVAILABILITY_NO_CURRENT_LOCATION),
    ("availability", AVAILABILITY_MULTIPLE_CURRENT_LOCATIONS),
    ("availability", AVAILABILITY_MISSING),
    ("availability", AVAILABILITY_UNREADABLE),
    ("availability", AVAILABILITY_NON_REGULAR),
    ("availability", AVAILABILITY_PRESENT_DRIFTED),
    ("inventory", INVENTORY_NOT_INVENTORIED),
    ("inventory", INVENTORY_INCOMPLETE),
)


def presentation_label(
    archive: ArchiveClassification,
    precedence: Sequence[tuple[str, str]] = PRESENTATION_PRECEDENCE,
) -> str:
    """One headline label for an archive, for a summary line.

    A convenience for humans reading a list, and nothing more. The full
    six-axis tuple is always what gets emitted to JSON and CSV; this is an
    extra column beside it, never a replacement for it.
    """
    for axis, value in precedence:
        if getattr(archive, axis) == value:
            return f"{axis}:{value}"

    return "covered" if archive.outstanding_pages == 0 else "outstanding"


def _as_sequence(
    result: ClassificationResult | Sequence[ArchiveClassification],
) -> Sequence[ArchiveClassification]:
    return (
        result.archives
        if isinstance(result, ClassificationResult)
        else result
    )
