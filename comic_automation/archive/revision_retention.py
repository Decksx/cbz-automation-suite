"""Read-only retention planner for immutable archive revisions.

Roadmap Step 3, first slice. This module answers one question per revision --
*may this revision ever be pruned?* -- and it answers it without deleting
anything, without proposing a migration, and without writing to the database
it reads. There is no apply path here, and adding one is a separately
reviewed change.

Four questions, kept apart on purpose
-------------------------------------

The failure this module is shaped to avoid is collapsing four independent
questions into a single verdict:

1. **Policy** -- may this revision ever be pruned? (`policy_classification`)
2. **Evidence granularity** -- is the reason revision-specific, or an
   archive-level proxy standing in for evidence the schema cannot yet
   express per revision? (`evidence_granularity`)
3. **Feasibility** -- can schema 014 actually execute the policy result?
   (`feasible_under_schema_014`, `infeasibility_reasons`)
4. **Execution** -- deferred. Always `not_performed`, from a constant, so the
   field cannot quietly start meaning something else here.

Policy and feasibility are computed by two functions that never see each
other's results (`_classify_policy` and `_assess_feasibility`). That is what
keeps a revision that policy says is prunable from being silently relabelled
"protected" merely because schema 014 refuses to delete it. Such a revision
is reported as a `candidate` carrying `feasible_under_schema_014 = False` and
an explicit reason, because the two facts are separately actionable: the
first says the policy is willing, the second says the schema is not, and a
later migration changes only the second.

Why every candidate is currently infeasible
-------------------------------------------

Under schema 014, `trg_archive_revisions_not_deletable` refuses to delete any
revision while its archive row exists -- provisional rows included. That guard
is unconditional, so *every* candidate this planner produces is infeasible
today. That is not a bug in the policy and it is deliberately not folded into
the policy: migration 015 will relax the guard under review, and when it does,
the candidate set is already computed and reviewable.

What can reference a revision
-----------------------------

Measured against migration 014 rather than assumed. Exactly three columns
reference `archive_revisions`:

* `archive_files.current_revision_id` -- the sole authoritative current pointer;
* `archive_revision_observations.revision_id` -- sightings of those bytes;
* `archive_revisions.previous_revision_id` -- lineage.

Everything else in the schema -- jobs, quarantine, retirements, supersessions,
disposition events, near-duplicate review -- keys on `archive_id` and cannot
name a revision at all. Those rules are therefore evaluated at archive
granularity and protect *every* revision of the archive, labelled
`archive_proxy` so that no reader mistakes a conservative sweep for a
revision-specific finding. Roadmap Step 4 (revision-aware provenance) is what
would let them become revision-granular; until then, over-protecting is the
only safe reading.

The protection rules
--------------------

Eight rules, mapped to the roadmap's Step 3 keep-list:

===========================  ==============  ==================================
rule                         granularity     roadmap line
===========================  ==============  ==================================
is_current_revision          revision        "the current revision"
retention_window             revision        "at least the immediately
                                             previous revision"
newer_than_current           revision        (see below -- conservative
                                             extension, recorded not assumed)
revision_has_observations    revision        the revision-granular half of
                                             "revisions referenced by ..."
active_or_recoverable_job    archive_proxy   "referenced by active or
                                             recoverable jobs"
open_review_work             archive_proxy   "referenced by open review work"
quarantine_or_resolution     archive_proxy   "referenced by quarantine or
                                             resolution history"
unresolved_failure           archive_proxy   "associated with unresolved
                                             failures"
operator_pin                 revision        "operator-pinned revisions"
===========================  ==============  ==================================

`newer_than_current` is an addition to the roadmap list and is called out
rather than folded in. Rolling the current pointer back to an earlier
generation is a legitimate operator act -- `RevisionRepository.current_for`
resolves current through the pointer for exactly that reason -- which leaves
revisions *newer* than current and noncurrent. No keep rule covers them and no
prune rule authorises them, and the roll-forward path is the thing that would
be destroyed. They are protected under their own named reason so the gap is
visible in the output rather than resolved by whichever branch happened to
catch them.

The retention window is `keep_previous_generations` (default 1) and is walked
back from the current revision along `previous_revision_id`, never by ordinal
arithmetic, because current is not necessarily the highest ordinal. A
*time*-based window is deliberately not implemented: the roadmap says "for a
defined retention window" without defining one, and inventing a duration here
would bury an unmade operator decision inside a planner. See
`RetentionPolicy`.

Failing closed
--------------

`unexplained` is residue, never a positive predicate. A revision lands there
only when an input the decision depends on could not be interpreted: a job,
quarantine, review or disposition row carrying a status outside the vocabulary
migration 014 and its predecessors declare; an archive with no current pointer;
or a lineage link that contradicts the schema's own invariants. Residue is
never a prune candidate, and `unexplained = 0` is the production gate --
`plan_totals` reconciles it and the CLI refuses by default to report a plan
that has any.

A current revision is never residue and never a candidate. Unknown evidence on
its archive is still recorded in its `unknown_evidence` list, and still forces
the archive's noncurrent revisions to residue, but the current pointer is a
fact the planner reads directly and does not need to interpret.

Everything here is read-only. The planner never opens a writable connection;
`plan_from_database` goes through `read_consistent_snapshot`, so the whole
report comes from one deferred read transaction with `PRAGMA data_version`
sampled either side.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from comic_automation.database.read_guards import (
    ConsistentSnapshot,
    quick_check,
    read_consistent_snapshot,
)

__all__ = [
    "PLANNER_VERSION",
    "PROTECTED",
    "CANDIDATE",
    "UNEXPLAINED",
    "GRANULARITY_REVISION",
    "GRANULARITY_ARCHIVE_PROXY",
    "GRANULARITY_NONE",
    "EXECUTION_STATUS",
    "RetentionPlannerError",
    "PinManifestError",
    "PlannerInvariantError",
    "RetentionPolicy",
    "Pin",
    "PinManifest",
    "RevisionPlan",
    "RetentionPlan",
    "load_pin_manifest",
    "build_plan",
    "plan_from_database",
    "write_json",
    "write_csv",
]


# Bumped whenever a policy rule, its inputs, or the canonical digest rendering
# changes. It is part of the hashed payload, so a plan produced by a different
# planner can never collide with this one's digest even over identical rows.
PLANNER_VERSION = "revision-retention-planner/1"

PROTECTED = "protected"
CANDIDATE = "candidate"
UNEXPLAINED = "unexplained"

GRANULARITY_REVISION = "revision"
GRANULARITY_ARCHIVE_PROXY = "archive_proxy"
GRANULARITY_NONE = "none"

# This slice never executes anything. Held as a constant rather than written
# as a literal at each construction site so that a future apply path has to
# change one visible thing rather than drift field by field.
EXECUTION_STATUS = "not_performed"

# --- declared vocabularies ------------------------------------------------
#
# Each of these mirrors a CHECK constraint or a documented status set in the
# migrations. They are duplicated here on purpose: the planner has to be able
# to recognise a value it does *not* know, and a set derived from the database
# at runtime would silently accept whatever it found. A status outside these
# sets is unknown evidence and fails closed.

# jobs.status -- 'blocked' is terminal alongside the obvious three; see
# comic_automation/jobs/queue.py.
ACTIVE_JOB_STATUSES = frozenset({"pending", "claimed", "running"})
TERMINAL_JOB_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "blocked"}
)
KNOWN_JOB_STATUSES = ACTIVE_JOB_STATUSES | TERMINAL_JOB_STATUSES

# A failed job is an unresolved failure until something retires or resolves
# it. The queue resets a retryable attempt back to 'pending', so a row still
# sitting at 'failed' is one nothing has taken responsibility for.
UNRESOLVED_FAILURE_STATUSES = frozenset({"failed"})

# archive_quarantine.status
KNOWN_QUARANTINE_STATUSES = frozenset(
    {"pending_redownload", "resolved", "abandoned"}
)

# near_duplicate_candidates.review_status
KNOWN_REVIEW_STATUSES = frozenset(
    {"pending_review", "confirmed_duplicate", "keep_both", "rejected"}
)
OPEN_REVIEW_STATUSES = frozenset({"pending_review"})

# archive_disposition_events.disposition
KNOWN_DISPOSITIONS = frozenset({"retired", "superseded"})

# archive_revisions.identity_state
KNOWN_IDENTITY_STATES = frozenset({"established", "provisional"})

# Protection reason identifiers, and the granularity each is evaluated at.
# One mapping rather than a granularity chosen at each call site, so a rule
# cannot claim revision granularity in one branch and proxy granularity in
# another.
RULE_GRANULARITY: dict[str, str] = {
    "is_current_revision": GRANULARITY_REVISION,
    "retention_window": GRANULARITY_REVISION,
    "newer_than_current": GRANULARITY_REVISION,
    "revision_has_observations": GRANULARITY_REVISION,
    "operator_pin": GRANULARITY_REVISION,
    "active_or_recoverable_job": GRANULARITY_ARCHIVE_PROXY,
    "open_review_work": GRANULARITY_ARCHIVE_PROXY,
    "quarantine_or_resolution": GRANULARITY_ARCHIVE_PROXY,
    "unresolved_failure": GRANULARITY_ARCHIVE_PROXY,
}

# Infeasibility reason identifiers.
INFEASIBLE_DELETE_GUARD = "delete_guard_refuses_while_archive_exists"
INFEASIBLE_SUCCESSOR_REFERENCE = "successor_references_this_revision"
INFEASIBLE_SUCCESSOR_IMMUTABLE = "successor_cannot_be_repointed"

_HEX_CHARACTERS = frozenset("0123456789abcdef")


class RetentionPlannerError(RuntimeError):
    """Base class for every error this module raises."""


class PinManifestError(RetentionPlannerError):
    """An operator pin manifest was missing, malformed, or contradictory.

    Raised rather than skipped. A pin is an operator saying "never prune
    this", and a pin file that cannot be read in full is indistinguishable
    from one that pins nothing -- so a plan is refused instead of being
    produced against a pin set nobody authorised.
    """


class PlannerInvariantError(RetentionPlannerError):
    """The planner's own reconciliation failed.

    Raised when a revision was classified more than once, or when the totals
    do not add up to the row count. Both are impossible by construction, which
    is exactly why they are checked: the reconciliation is the thing an
    operator is asked to trust, so it verifies itself rather than asserting
    that the code is correct.
    """


@dataclass(frozen=True)
class RetentionPolicy:
    """The policy parameters a plan was computed under.

    `keep_previous_generations` is the "defined retention window" of the
    roadmap's keep-list, expressed in generations rather than in time. Walking
    back N links from the current revision is a statement the schema can
    answer exactly; "90 days" is not, because a revision's `created_at` records
    when the row was written and a migration backfill wrote 59,688 of them on
    one day. A time window is therefore not implemented here rather than
    implemented approximately -- recorded as a deliberate non-decision, since
    silence would be indistinguishable from oversight.

    Zero is permitted and means "keep only the current revision". It is not the
    default: the roadmap says *at least* the immediately previous revision, so
    the default honours the roadmap and an operator has to say otherwise.
    """

    keep_previous_generations: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.keep_previous_generations, int) or isinstance(
            self.keep_previous_generations, bool
        ):
            raise RetentionPlannerError(
                "keep_previous_generations must be an int, not "
                f"{type(self.keep_previous_generations).__name__}"
            )

        if self.keep_previous_generations < 0:
            raise RetentionPlannerError(
                "keep_previous_generations must be >= 0; "
                f"got {self.keep_previous_generations}"
            )

    def as_dict(self) -> dict[str, Any]:
        return {"keep_previous_generations": self.keep_previous_generations}


@dataclass(frozen=True)
class Pin:
    """One operator pin: never prune this revision.

    `archive_id` is optional in the manifest and is validated against the
    revision when supplied. It exists so an operator can write down which
    archive they believed they were pinning; a mismatch means the manifest and
    the database disagree about identity, which is refused rather than
    resolved in either direction.
    """

    revision_id: int
    archive_id: int | None
    reason: str

    def canonical_line(self) -> str:
        archive = "" if self.archive_id is None else str(self.archive_id)
        return (
            f"pin|revision_id={self.revision_id}"
            f"|archive_id={archive}|reason={self.reason}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "archive_id": self.archive_id,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PinManifest:
    """A validated, canonicalised set of operator pins.

    Canonical means sorted by `revision_id` with reasons whitespace-collapsed,
    so the same declarations written in a different order or with different
    incidental spacing produce the same digest contribution. Two pins naming
    the same revision are refused even when otherwise identical: a duplicate is
    a sign the manifest was assembled from two sources, and silently
    deduplicating it would hide that.
    """

    pins: tuple[Pin, ...] = ()
    source: str | None = None

    @property
    def revision_ids(self) -> frozenset[int]:
        return frozenset(pin.revision_id for pin in self.pins)

    def reason_for(self, revision_id: int) -> str | None:
        for pin in self.pins:
            if pin.revision_id == revision_id:
                return pin.reason
        return None

    def canonical_lines(self) -> list[str]:
        """The pin half of the hashed payload.

        The count is emitted even when it is zero, so "no pins were supplied"
        is itself part of the digest. Without it, a plan run with an empty
        manifest and a plan run with no manifest at all would hash
        identically, and the operator could not bind which one they reviewed.
        """
        lines = [f"pins|count={len(self.pins)}"]
        lines.extend(pin.canonical_line() for pin in self.pins)
        return lines

    def as_list(self) -> list[dict[str, Any]]:
        return [pin.as_dict() for pin in self.pins]


@dataclass(frozen=True)
class _ArchiveEvidence:
    """Archive-level evidence, and whatever could not be interpreted.

    Internal. `unknown` carries one string per uninterpretable input, and its
    presence is what pushes an archive's noncurrent revisions into residue.
    """

    archive_id: int
    active_job: bool = False
    unresolved_failure: bool = False
    quarantine: bool = False
    open_review: bool = False
    unknown: tuple[str, ...] = ()

    def proxy_reasons(self) -> list[str]:
        reasons = []

        if self.active_job:
            reasons.append("active_or_recoverable_job")
        if self.open_review:
            reasons.append("open_review_work")
        if self.quarantine:
            reasons.append("quarantine_or_resolution")
        if self.unresolved_failure:
            reasons.append("unresolved_failure")

        return reasons


@dataclass(frozen=True)
class RevisionPlan:
    """One revision's four answers, plus the facts they were derived from."""

    archive_id: int
    revision_id: int
    revision_ordinal: int
    identity_state: str
    archive_sha256: str | None
    is_current: bool
    previous_revision_id: int | None
    observation_count: int
    policy_classification: str
    protection_reasons: tuple[str, ...]
    evidence_granularity: str
    feasible_under_schema_014: bool
    infeasibility_reasons: tuple[str, ...]
    unknown_evidence: tuple[str, ...]
    execution_status: str = EXECUTION_STATUS

    def as_dict(self, *, planner_version: str, snapshot_digest: str) -> dict:
        """One JSON object, with provenance repeated on every row.

        `planner_version` and `snapshot_digest` are stamped onto each row
        rather than left in the header alone because rows get extracted,
        pasted into tickets and filtered through other tools, and a row that
        has lost the identity of the plan it came from cannot be reconciled
        against anything later.
        """
        return {
            "archive_id": self.archive_id,
            "revision_id": self.revision_id,
            "ordinal": self.revision_ordinal,
            "identity_state": self.identity_state,
            "archive_sha256": self.archive_sha256,
            "is_current": self.is_current,
            "previous_revision_id": self.previous_revision_id,
            "observation_count": self.observation_count,
            "policy_classification": self.policy_classification,
            "protection_reasons": list(self.protection_reasons),
            "evidence_granularity": self.evidence_granularity,
            "feasible_under_schema_014": self.feasible_under_schema_014,
            "infeasibility_reasons": list(self.infeasibility_reasons),
            "unknown_evidence": list(self.unknown_evidence),
            "execution_status": self.execution_status,
            "planner_version": planner_version,
            "snapshot_digest": snapshot_digest,
        }


CSV_COLUMNS = (
    "archive_id",
    "revision_id",
    "ordinal",
    "identity_state",
    "archive_sha256",
    "is_current",
    "previous_revision_id",
    "observation_count",
    "policy_classification",
    "protection_reasons",
    "evidence_granularity",
    "feasible_under_schema_014",
    "infeasibility_reasons",
    "unknown_evidence",
    "execution_status",
    "planner_version",
    "snapshot_digest",
)


@dataclass(frozen=True)
class RetentionPlan:
    """A complete, reconciled, read-only retention plan."""

    planner_version: str
    policy: RetentionPolicy
    pins: PinManifest
    revisions: tuple[RevisionPlan, ...]
    snapshot_digest: str
    totals: Mapping[str, int]

    @property
    def candidates(self) -> tuple[RevisionPlan, ...]:
        return tuple(
            plan
            for plan in self.revisions
            if plan.policy_classification == CANDIDATE
        )

    @property
    def unexplained(self) -> tuple[RevisionPlan, ...]:
        return tuple(
            plan
            for plan in self.revisions
            if plan.policy_classification == UNEXPLAINED
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "planner_version": self.planner_version,
            "policy": self.policy.as_dict(),
            "execution_status": EXECUTION_STATUS,
            "snapshot_digest": self.snapshot_digest,
            "rule_granularity": dict(sorted(RULE_GRANULARITY.items())),
            "pins": self.pins.as_list(),
            "pin_source": self.pins.source,
            "totals": dict(self.totals),
            "revisions": [
                plan.as_dict(
                    planner_version=self.planner_version,
                    snapshot_digest=self.snapshot_digest,
                )
                for plan in self.revisions
            ],
        }


# --- pin manifests --------------------------------------------------------


def load_pin_manifest(path: str | Path) -> list[dict[str, Any]]:
    """Read the raw pin entries from a manifest file.

    Structure only; the entries are validated against the database later, by
    `_validate_pins`, because "revision 12 exists" is not a question a file
    can answer. Accepts either a bare list of entries or an object with a
    `pins` key, and refuses anything else rather than guessing.
    """
    resolved = Path(path)

    if not resolved.is_file():
        raise PinManifestError(f"Pin manifest does not exist: {resolved}")

    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PinManifestError(
            f"Pin manifest {resolved} could not be read as JSON: {error}"
        ) from error

    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict) and "pins" in payload:
        entries = payload["pins"]
    else:
        raise PinManifestError(
            f"Pin manifest {resolved} must be a JSON list of pins, or an "
            "object with a 'pins' key. Refused rather than treated as an "
            "empty pin set, which would silently unpin everything."
        )

    if not isinstance(entries, list):
        raise PinManifestError(
            f"Pin manifest {resolved}: 'pins' must be a list, got "
            f"{type(entries).__name__}"
        )

    return entries


def _normalize_reason(value: Any, *, revision_label: str) -> str:
    """Collapse a pin reason to canonical form, refusing a blank one.

    Non-blank evidence is the rule migrations 012, 013 and 014 all apply to
    their own evidence columns, and a pin is the same kind of claim: an
    operator asserting something that has to be reviewable later. The trim set
    matches the schema's, which strips tabs and newlines as well as spaces --
    SQLite's one-argument `trim()` does not, and a lone tab passing as
    evidence was a real defect found on migration 012.
    """
    if not isinstance(value, str):
        raise PinManifestError(
            f"{revision_label}: pin reason must be a string, got "
            f"{type(value).__name__}"
        )

    collapsed = " ".join(value.split())

    if not collapsed:
        raise PinManifestError(
            f"{revision_label}: pin reason is blank. A pin without a stated "
            "reason cannot be reviewed or retired later."
        )

    return collapsed


def _validate_pins(
    entries: Sequence[Mapping[str, Any]],
    revisions_by_id: Mapping[int, "_RevisionRow"],
    *,
    source: str | None,
) -> PinManifest:
    """Validate every pin against the database, then canonicalise.

    Four ways a pin is refused, and all four are refusals rather than
    warnings: a pin is the operator's strongest statement about retention, and
    a plan that quietly dropped one would report a revision as prunable that
    somebody had explicitly protected.
    """
    seen: dict[int, Mapping[str, Any]] = {}
    pins: list[Pin] = []

    for index, entry in enumerate(entries):
        label = f"pin[{index}]"

        if not isinstance(entry, Mapping):
            raise PinManifestError(
                f"{label}: each pin must be an object, got "
                f"{type(entry).__name__}"
            )

        if "revision_id" not in entry:
            raise PinManifestError(f"{label}: missing 'revision_id'")

        raw_id = entry["revision_id"]

        # bool is an int in Python, and `True` would silently pin revision 1.
        if not isinstance(raw_id, int) or isinstance(raw_id, bool):
            raise PinManifestError(
                f"{label}: 'revision_id' must be an integer, got {raw_id!r}"
            )

        revision_id = int(raw_id)
        label = f"pin[{index}] revision_id={revision_id}"

        if revision_id in seen:
            raise PinManifestError(
                f"{label}: duplicated in the manifest. Refused rather than "
                "deduplicated -- a repeated pin usually means the manifest "
                "was assembled from two sources that may disagree."
            )

        row = revisions_by_id.get(revision_id)

        if row is None:
            raise PinManifestError(
                f"{label}: no such revision in this database. A pin naming a "
                "revision that does not exist means the manifest and the "
                "database are not describing the same state."
            )

        declared_archive = entry.get("archive_id")

        if declared_archive is not None:
            if not isinstance(declared_archive, int) or isinstance(
                declared_archive, bool
            ):
                raise PinManifestError(
                    f"{label}: 'archive_id' must be an integer or absent, "
                    f"got {declared_archive!r}"
                )

            if int(declared_archive) != row.archive_id:
                raise PinManifestError(
                    f"{label}: manifest says archive {declared_archive}, but "
                    f"the revision belongs to archive {row.archive_id}. "
                    "Refused rather than resolved in either direction: the "
                    "manifest and the database disagree about identity."
                )

        reason = _normalize_reason(
            entry.get("reason", ""), revision_label=label
        )

        seen[revision_id] = entry
        pins.append(
            Pin(
                revision_id=revision_id,
                archive_id=(
                    None if declared_archive is None else int(declared_archive)
                ),
                reason=reason,
            )
        )

    pins.sort(key=lambda pin: pin.revision_id)
    return PinManifest(pins=tuple(pins), source=source)


# --- reading the database -------------------------------------------------


@dataclass(frozen=True)
class _RevisionRow:
    """One `archive_revisions` row, as read."""

    revision_id: int
    archive_id: int
    revision_ordinal: int
    identity_state: str
    archive_sha256: str | None
    previous_revision_id: int | None


def _read_revisions(connection: sqlite3.Connection) -> list[_RevisionRow]:
    rows = connection.execute(
        """
        SELECT id, archive_id, revision_ordinal, identity_state,
               archive_sha256, previous_revision_id
        FROM archive_revisions
        ORDER BY archive_id, revision_ordinal, id
        """
    ).fetchall()

    return [
        _RevisionRow(
            revision_id=int(row["id"]),
            archive_id=int(row["archive_id"]),
            revision_ordinal=int(row["revision_ordinal"]),
            identity_state=row["identity_state"],
            archive_sha256=row["archive_sha256"],
            previous_revision_id=(
                None
                if row["previous_revision_id"] is None
                else int(row["previous_revision_id"])
            ),
        )
        for row in rows
    ]


def _read_current_pointers(
    connection: sqlite3.Connection,
) -> dict[int, int | None]:
    """Every archive's current pointer, including any that is NULL.

    NULL is read rather than filtered out. Migration 014 makes it impossible
    through two triggers, so a NULL here means the schema's invariant has been
    violated -- which is precisely the state that must reach the report as
    residue instead of being skipped by a WHERE clause.
    """
    return {
        int(row["id"]): (
            None
            if row["current_revision_id"] is None
            else int(row["current_revision_id"])
        )
        for row in connection.execute(
            "SELECT id, current_revision_id FROM archive_files"
        )
    }


def _read_observation_counts(
    connection: sqlite3.Connection,
) -> dict[int, int]:
    return {
        int(row["revision_id"]): int(row["observations"])
        for row in connection.execute(
            """
            SELECT revision_id, COUNT(*) AS observations
            FROM archive_revision_observations
            GROUP BY revision_id
            """
        )
    }


def _read_status_counts(
    connection: sqlite3.Connection, sql: str
) -> dict[int, dict[str, int]]:
    """`{archive_id: {status: count}}` for one archive-keyed evidence table.

    The raw status counts are carried rather than a pre-computed boolean
    because they are digest inputs: a job moving from 'running' to 'completed'
    changes what the planner was looking at even when the resulting protection
    flag is unchanged, and a digest that missed that would bind two different
    database states to one value.
    """
    counts: dict[int, dict[str, int]] = {}

    for row in connection.execute(sql):
        archive_id = int(row["archive_id"])
        status = row["status"]
        key = "" if status is None else str(status)
        counts.setdefault(archive_id, {})[key] = int(row["occurrences"])

    return counts


_JOB_STATUS_SQL = """
    SELECT archive_id, status, COUNT(*) AS occurrences
    FROM jobs
    WHERE archive_id IS NOT NULL
    GROUP BY archive_id, status
"""

_QUARANTINE_STATUS_SQL = """
    SELECT archive_id, status, COUNT(*) AS occurrences
    FROM archive_quarantine
    GROUP BY archive_id, status
"""

# Both sides of a near-duplicate pair are the archives under review, so each
# row contributes to two archives.
_REVIEW_STATUS_SQL = """
    SELECT archive_a_id AS archive_id, review_status AS status,
           COUNT(*) AS occurrences
    FROM near_duplicate_candidates
    GROUP BY archive_a_id, review_status
    UNION ALL
    SELECT archive_b_id AS archive_id, review_status AS status,
           COUNT(*) AS occurrences
    FROM near_duplicate_candidates
    GROUP BY archive_b_id, review_status
"""


def _merge_status_counts(
    counts: dict[int, dict[str, int]], addition: dict[int, dict[str, int]]
) -> None:
    for archive_id, statuses in addition.items():
        target = counts.setdefault(archive_id, {})
        for status, occurrences in statuses.items():
            target[status] = target.get(status, 0) + occurrences


def _read_review_status_counts(
    connection: sqlite3.Connection,
) -> dict[int, dict[str, int]]:
    """Review counts per archive, summed across both sides of each pair.

    The UNION ALL can return the same (archive, status) key twice -- once from
    each side -- so the halves are merged additively rather than by dict
    assignment, which would drop one side's count.
    """
    counts: dict[int, dict[str, int]] = {}
    _merge_status_counts(
        counts, _read_status_counts(connection, _REVIEW_STATUS_SQL)
    )
    return counts


def _read_disposition_archives(
    connection: sqlite3.Connection,
) -> dict[int, dict[str, int]]:
    """Disposition evidence per archive.

    Retirements and supersessions are read through
    `archive_disposition_events`, which migration 013 backfills from both and
    keeps current by trigger, so one read covers all three tables. Reversals
    are counted under their own key rather than cancelling a recording: a
    reversed disposition is still resolution history, which the roadmap's
    keep-list protects.
    """
    counts: dict[int, dict[str, int]] = {}

    for row in connection.execute(
        """
        SELECT archive_id, disposition, action, COUNT(*) AS occurrences
        FROM archive_disposition_events
        GROUP BY archive_id, disposition, action
        """
    ):
        archive_id = int(row["archive_id"])
        disposition = row["disposition"]
        action = row["action"]
        key = f"{disposition}:{action}"
        counts.setdefault(archive_id, {})[key] = int(row["occurrences"])

    return counts


@dataclass(frozen=True)
class _Inputs:
    """Everything a decision can depend on, read from one snapshot."""

    revisions: tuple[_RevisionRow, ...]
    current_pointers: Mapping[int, int | None]
    observation_counts: Mapping[int, int]
    job_statuses: Mapping[int, Mapping[str, int]]
    quarantine_statuses: Mapping[int, Mapping[str, int]]
    review_statuses: Mapping[int, Mapping[str, int]]
    disposition_events: Mapping[int, Mapping[str, int]]


def read_inputs(connection: sqlite3.Connection) -> _Inputs:
    """Read every decision-bearing input from one connection.

    Intended to be called inside `read_consistent_snapshot`, so all seven
    reads see the same snapshot and no two of them can disagree because a
    writer committed in between.
    """
    return _Inputs(
        revisions=tuple(_read_revisions(connection)),
        current_pointers=_read_current_pointers(connection),
        observation_counts=_read_observation_counts(connection),
        job_statuses=_read_status_counts(connection, _JOB_STATUS_SQL),
        quarantine_statuses=_read_status_counts(
            connection, _QUARANTINE_STATUS_SQL
        ),
        review_statuses=_read_review_status_counts(connection),
        disposition_events=_read_disposition_archives(connection),
    )


# --- evidence -------------------------------------------------------------


def _archive_evidence(archive_id: int, inputs: _Inputs) -> _ArchiveEvidence:
    """Interpret one archive's archive-level evidence, failing closed.

    Every status read here is checked against a declared vocabulary before it
    is used. An unrecognised value is not ignored and not treated as benign:
    it is recorded as unknown evidence, which drives the archive's noncurrent
    revisions to residue. A status the planner has never heard of might be a
    new terminal state or a new active one, and guessing wrong in the
    permissive direction proposes pruning evidence somebody is still using.
    """
    unknown: list[str] = []

    jobs = inputs.job_statuses.get(archive_id, {})
    active_job = False
    unresolved_failure = False

    for status, occurrences in sorted(jobs.items()):
        if status not in KNOWN_JOB_STATUSES:
            unknown.append(f"job_status:{status}")
            continue

        if status in ACTIVE_JOB_STATUSES and occurrences > 0:
            active_job = True

        if status in UNRESOLVED_FAILURE_STATUSES and occurrences > 0:
            unresolved_failure = True

    quarantine = False

    for status, occurrences in sorted(
        inputs.quarantine_statuses.get(archive_id, {}).items()
    ):
        if status not in KNOWN_QUARANTINE_STATUSES:
            unknown.append(f"quarantine_status:{status}")
            continue

        # Every quarantine row protects, resolved ones included: the roadmap
        # keeps "quarantine *or resolution history*", and a resolved
        # quarantine is exactly that history.
        if occurrences > 0:
            quarantine = True

    open_review = False

    for status, occurrences in sorted(
        inputs.review_statuses.get(archive_id, {}).items()
    ):
        if status not in KNOWN_REVIEW_STATUSES:
            unknown.append(f"review_status:{status}")
            continue

        if status in OPEN_REVIEW_STATUSES and occurrences > 0:
            open_review = True

    for key, occurrences in sorted(
        inputs.disposition_events.get(archive_id, {}).items()
    ):
        disposition, _, action = key.partition(":")

        if disposition not in KNOWN_DISPOSITIONS:
            unknown.append(f"disposition:{disposition}")
            continue

        if action not in ("recorded", "reversed"):
            unknown.append(f"disposition_action:{action}")
            continue

        # A disposition is resolution history in the same sense as a resolved
        # quarantine, and it is folded into the same proxy reason rather than
        # given its own: both answer "something has already decided something
        # about this archive", and splitting them would imply the planner can
        # tell which decision touched which generation. It cannot.
        if occurrences > 0:
            quarantine = True

    return _ArchiveEvidence(
        archive_id=archive_id,
        active_job=active_job,
        unresolved_failure=unresolved_failure,
        quarantine=quarantine,
        open_review=open_review,
        unknown=tuple(unknown),
    )


def _structural_problems(
    row: _RevisionRow,
    inputs: _Inputs,
    revisions_by_id: Mapping[int, _RevisionRow],
) -> list[str]:
    """Contradictions between a revision row and the schema's own invariants.

    Every check here is for something migration 014 makes impossible. They are
    checked anyway, because the planner may be pointed at a backup, at a
    database restored from an older schema, or at one a future migration has
    changed -- and a planner that assumes its invariants hold cannot report
    that they do not. Each finding fails closed to residue.
    """
    problems: list[str] = []

    if row.identity_state not in KNOWN_IDENTITY_STATES:
        problems.append(f"identity_state:{row.identity_state}")
    elif (row.identity_state == "established") != (
        row.archive_sha256 is not None
    ):
        problems.append("identity_state_disagrees_with_digest")

    if row.archive_id not in inputs.current_pointers:
        problems.append("archive_row_missing")
    elif inputs.current_pointers[row.archive_id] is None:
        problems.append("archive_has_no_current_revision")

    if row.previous_revision_id is not None:
        predecessor = revisions_by_id.get(row.previous_revision_id)

        if predecessor is None:
            problems.append("previous_revision_missing")
        else:
            if predecessor.archive_id != row.archive_id:
                problems.append("previous_revision_in_another_archive")
            if predecessor.revision_ordinal != row.revision_ordinal - 1:
                problems.append("previous_revision_not_prior_ordinal")

    if row.revision_ordinal == 1 and row.previous_revision_id is not None:
        problems.append("first_revision_has_predecessor")

    if row.revision_ordinal > 1 and row.previous_revision_id is None:
        problems.append("later_revision_has_no_predecessor")

    return problems


# --- policy ---------------------------------------------------------------


def _retention_window_ids(
    inputs: _Inputs,
    revisions_by_id: Mapping[int, _RevisionRow],
    policy: RetentionPolicy,
) -> set[int]:
    """Revisions kept by the retention window, per archive.

    Walked backwards from the current revision along `previous_revision_id`,
    never by ordinal arithmetic. Current is resolved through
    `archive_files.current_revision_id` and is not necessarily the highest
    ordinal -- rolling the pointer back to an earlier generation is a
    legitimate act -- so "current minus one" and "highest ordinal minus one"
    are different revisions the moment anyone does it.

    The walk is bounded by a seen-set as well as by the generation count, so a
    lineage cycle cannot spin here. A cycle is impossible under the schema's
    CHECK and lineage trigger; it is bounded anyway because this function may
    be pointed at a database those guards never ran against.
    """
    kept: set[int] = set()

    for current_id in inputs.current_pointers.values():
        if current_id is None:
            continue

        cursor = revisions_by_id.get(current_id)
        seen: set[int] = set()
        remaining = policy.keep_previous_generations

        while cursor is not None and remaining > 0:
            if cursor.revision_id in seen:
                break

            seen.add(cursor.revision_id)
            previous_id = cursor.previous_revision_id

            if previous_id is None:
                break

            kept.add(previous_id)
            cursor = revisions_by_id.get(previous_id)
            remaining -= 1

    return kept


def _classify_policy(
    row: _RevisionRow,
    *,
    is_current: bool,
    in_retention_window: bool,
    newer_than_current: bool,
    observation_count: int,
    pinned: bool,
    evidence: _ArchiveEvidence,
    structural_problems: Sequence[str],
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Answer question 1 -- may this revision ever be pruned?

    Returns `(classification, protection_reasons, unknown_evidence)`.

    Deliberately knows nothing about schema feasibility. It is not passed the
    successor map and cannot consult it, which is the structural guarantee
    that a policy answer here can never be shaded by what schema 014 happens
    to permit. `_assess_feasibility` answers that separately.

    Order matters in exactly one place: the current revision is decided first
    and unconditionally. Unknown evidence on its archive is still returned so
    the row carries it, but it can neither demote the current revision to
    residue nor make it a candidate -- which revision is current is read
    directly from the pointer and needs no interpretation.
    """
    unknown = tuple(list(structural_problems) + list(evidence.unknown))

    reasons: list[str] = []

    if is_current:
        reasons.append("is_current_revision")

    if in_retention_window:
        reasons.append("retention_window")

    if newer_than_current:
        reasons.append("newer_than_current")

    if observation_count > 0:
        reasons.append("revision_has_observations")

    if pinned:
        reasons.append("operator_pin")

    reasons.extend(evidence.proxy_reasons())

    if is_current:
        return PROTECTED, tuple(reasons), unknown

    if unknown:
        # Residue outranks protection for a noncurrent revision. Reporting it
        # as protected would be the safe *action* but the wrong *statement*:
        # it would claim the planner understood this archive's evidence and
        # decided to keep the revision, when in fact it could not read the
        # evidence at all. `unexplained = 0` is a gate precisely because that
        # distinction has to stay visible.
        return UNEXPLAINED, tuple(reasons), unknown

    if reasons:
        return PROTECTED, tuple(reasons), unknown

    return CANDIDATE, (), unknown


def _evidence_granularity(reasons: Sequence[str]) -> str:
    """The weakest granularity any protection reason was evaluated at.

    "Weakest" because the label describes what a reader may conclude from the
    row as a whole. One archive-level proxy reason among four revision-level
    ones still means the row's protection rests partly on evidence that could
    not name this revision, and calling that `revision` would overstate it.
    """
    if not reasons:
        return GRANULARITY_NONE

    granularities = {RULE_GRANULARITY[reason] for reason in reasons}

    if GRANULARITY_ARCHIVE_PROXY in granularities:
        return GRANULARITY_ARCHIVE_PROXY

    return GRANULARITY_REVISION


def _assess_feasibility(
    row: _RevisionRow, successors: Mapping[int, list[int]]
) -> tuple[bool, tuple[str, ...]]:
    """Answer question 3 -- can schema 014 execute a prune of this revision?

    A property of the schema and the row's structural position, computed for
    every revision regardless of what policy decided. Keeping it independent
    is what lets the output show a candidate that is infeasible and a
    protected revision that would have been equally infeasible, rather than
    conflating "we will not" with "we cannot".

    Three refusals, all measured against migration 014 rather than inferred:

    * `trg_archive_revisions_not_deletable` aborts any DELETE while the
      archive row still exists, provisional rows included. Unconditional, so
      it applies to every revision here.
    * the lineage foreign key is NO ACTION (deferred), so deleting a revision
      a successor still points at is refused at COMMIT.
    * that successor cannot be repointed out of the way either --
      `trg_archive_revisions_immutable` refuses the UPDATE, and the
      ordinal/predecessor CHECK refuses a NULL predecessor at ordinal > 1.
    """
    reasons = [INFEASIBLE_DELETE_GUARD]

    if successors.get(row.revision_id):
        reasons.append(INFEASIBLE_SUCCESSOR_REFERENCE)
        reasons.append(INFEASIBLE_SUCCESSOR_IMMUTABLE)

    return False, tuple(reasons)


# --- snapshot digest ------------------------------------------------------

SNAPSHOT_DIGEST_VERSION = "revision-retention-snapshot/1"


def canonical_snapshot_lines(
    inputs: _Inputs, policy: RetentionPolicy, pins: PinManifest
) -> list[str]:
    """Every input that can change a decision, in one canonical rendering.

    Inputs, not outputs. Hashing the plan's own rows would make the digest a
    checksum of a conclusion; hashing the inputs makes it an identity for the
    state the conclusion was drawn from, which is what an operator needs to
    bind a reviewed plan to a database.

    The policy is included because it is an input too: the same database under
    a different retention window is a different decision, and two plans that
    disagree must not share a digest.

    Field names are written into the payload alongside their values, and every
    section carries an explicit count, so neither reordering fields nor
    dropping a whole section can produce a colliding digest.
    """
    lines = [SNAPSHOT_DIGEST_VERSION, f"planner_version={PLANNER_VERSION}"]

    for name, value in sorted(policy.as_dict().items()):
        lines.append(f"policy|{name}={value}")

    lines.append(f"revisions|count={len(inputs.revisions)}")

    for row in sorted(inputs.revisions, key=lambda item: item.revision_id):
        lines.append(
            "revision"
            f"|id={row.revision_id}"
            f"|archive_id={row.archive_id}"
            f"|ordinal={row.revision_ordinal}"
            f"|identity_state={row.identity_state}"
            f"|sha256={'' if row.archive_sha256 is None else row.archive_sha256}"
            f"|previous={'' if row.previous_revision_id is None else row.previous_revision_id}"
            f"|observations={inputs.observation_counts.get(row.revision_id, 0)}"
        )

    lines.append(f"current_pointers|count={len(inputs.current_pointers)}")

    for archive_id, current_id in sorted(inputs.current_pointers.items()):
        lines.append(
            "current"
            f"|archive_id={archive_id}"
            f"|revision_id={'' if current_id is None else current_id}"
        )

    for label, table in (
        ("jobs", inputs.job_statuses),
        ("quarantine", inputs.quarantine_statuses),
        ("review", inputs.review_statuses),
        ("disposition", inputs.disposition_events),
    ):
        total = sum(len(statuses) for statuses in table.values())
        lines.append(f"{label}|groups={total}")

        for archive_id, statuses in sorted(table.items()):
            for status, occurrences in sorted(statuses.items()):
                lines.append(
                    f"{label}|archive_id={archive_id}"
                    f"|status={status}|count={occurrences}"
                )

    lines.extend(pins.canonical_lines())
    return lines


def compute_snapshot_digest(
    inputs: _Inputs, policy: RetentionPolicy, pins: PinManifest
) -> str:
    """SHA-256 over `canonical_snapshot_lines`, lowercase hex.

    Lines are joined with a newline and given a trailing one, so no rendering
    can be a prefix of another, then encoded UTF-8. An empty database still
    yields a digest -- that of the version markers, policy and zero counts --
    so "there is nothing to prune here" is a statement an operator can bind as
    firmly as any other.
    """
    payload = (
        "\n".join(canonical_snapshot_lines(inputs, policy, pins)) + "\n"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- the plan -------------------------------------------------------------


def plan_totals(revisions: Iterable[RevisionPlan]) -> dict[str, int]:
    """Reconcile the plan, and refuse to return unreconciled totals.

    The totals are the thing an operator is asked to trust, so they check
    themselves rather than trusting the classifier. Two independent
    decompositions of the same rows have to agree: current plus noncurrent,
    and protected plus candidate plus unexplained plus current.
    """
    rows = list(revisions)

    seen: set[int] = set()
    for plan in rows:
        if plan.revision_id in seen:
            raise PlannerInvariantError(
                f"revision {plan.revision_id} was classified more than once; "
                "a plan that counts a revision twice cannot be reconciled"
            )
        seen.add(plan.revision_id)

    current = sum(1 for plan in rows if plan.is_current)
    noncurrent = len(rows) - current

    protected = sum(
        1
        for plan in rows
        if plan.policy_classification == PROTECTED and not plan.is_current
    )
    candidates = sum(
        1 for plan in rows if plan.policy_classification == CANDIDATE
    )
    unexplained = sum(
        1 for plan in rows if plan.policy_classification == UNEXPLAINED
    )

    if protected + candidates + unexplained != noncurrent:
        raise PlannerInvariantError(
            "noncurrent revisions do not reconcile: "
            f"{protected} protected + {candidates} candidates + "
            f"{unexplained} unexplained != {noncurrent} noncurrent"
        )

    current_candidates = sum(
        1
        for plan in rows
        if plan.is_current and plan.policy_classification != PROTECTED
    )

    if current_candidates:
        raise PlannerInvariantError(
            f"{current_candidates} current revision(s) were not classified "
            "as protected; the current revision can never be pruned"
        )

    feasible_candidates = sum(
        1
        for plan in rows
        if plan.policy_classification == CANDIDATE
        and plan.feasible_under_schema_014
    )

    return {
        "revisions": len(rows),
        "current": current,
        "noncurrent": noncurrent,
        "protected_noncurrent": protected,
        "candidates": candidates,
        "unexplained": unexplained,
        "candidates_feasible_under_schema_014": feasible_candidates,
        "archives": len({plan.archive_id for plan in rows}),
    }


def build_plan(
    connection: sqlite3.Connection,
    *,
    policy: RetentionPolicy | None = None,
    pin_entries: Sequence[Mapping[str, Any]] = (),
    pin_source: str | None = None,
) -> RetentionPlan:
    """Build a complete plan from an open read-only connection.

    The connection is only read from. Nothing here issues a write, and the
    caller is expected to have opened it read-only -- `plan_from_database`
    does, through `read_consistent_snapshot`.
    """
    resolved_policy = policy or RetentionPolicy()
    inputs = read_inputs(connection)

    revisions_by_id = {row.revision_id: row for row in inputs.revisions}
    pins = _validate_pins(pin_entries, revisions_by_id, source=pin_source)

    current_ids = {
        current_id
        for current_id in inputs.current_pointers.values()
        if current_id is not None
    }
    window_ids = _retention_window_ids(inputs, revisions_by_id, resolved_policy)

    successors: dict[int, list[int]] = {}
    for row in inputs.revisions:
        if row.previous_revision_id is not None:
            successors.setdefault(row.previous_revision_id, []).append(
                row.revision_id
            )

    # The current revision's ordinal per archive, used only to name the
    # `newer_than_current` case. Absent for an archive whose pointer is NULL
    # or dangling, in which case nothing is newer than anything and the row's
    # structural problems carry it to residue instead.
    current_ordinal: dict[int, int] = {}
    for archive_id, current_id in inputs.current_pointers.items():
        if current_id is None:
            continue
        current_row = revisions_by_id.get(current_id)
        if current_row is not None:
            current_ordinal[archive_id] = current_row.revision_ordinal

    evidence_cache: dict[int, _ArchiveEvidence] = {}
    plans: list[RevisionPlan] = []

    for row in inputs.revisions:
        evidence = evidence_cache.get(row.archive_id)
        if evidence is None:
            evidence = _archive_evidence(row.archive_id, inputs)
            evidence_cache[row.archive_id] = evidence

        is_current = row.revision_id in current_ids
        observation_count = inputs.observation_counts.get(row.revision_id, 0)
        ordinal_of_current = current_ordinal.get(row.archive_id)
        newer_than_current = (
            not is_current
            and ordinal_of_current is not None
            and row.revision_ordinal > ordinal_of_current
        )

        classification, reasons, unknown = _classify_policy(
            row,
            is_current=is_current,
            in_retention_window=row.revision_id in window_ids,
            newer_than_current=newer_than_current,
            observation_count=observation_count,
            pinned=row.revision_id in pins.revision_ids,
            evidence=evidence,
            structural_problems=_structural_problems(
                row, inputs, revisions_by_id
            ),
        )

        feasible, infeasibility = _assess_feasibility(row, successors)

        plans.append(
            RevisionPlan(
                archive_id=row.archive_id,
                revision_id=row.revision_id,
                revision_ordinal=row.revision_ordinal,
                identity_state=row.identity_state,
                archive_sha256=row.archive_sha256,
                is_current=is_current,
                previous_revision_id=row.previous_revision_id,
                observation_count=observation_count,
                policy_classification=classification,
                protection_reasons=reasons,
                evidence_granularity=_evidence_granularity(reasons),
                feasible_under_schema_014=feasible,
                infeasibility_reasons=infeasibility,
                unknown_evidence=unknown,
            )
        )

    # Deterministic across insertion order: the sort key is the logical
    # position of the revision, never rowid order or the order rows came back.
    plans.sort(
        key=lambda plan: (
            plan.archive_id,
            plan.revision_ordinal,
            plan.revision_id,
        )
    )

    return RetentionPlan(
        planner_version=PLANNER_VERSION,
        policy=resolved_policy,
        pins=pins,
        revisions=tuple(plans),
        snapshot_digest=compute_snapshot_digest(inputs, resolved_policy, pins),
        totals=plan_totals(plans),
    )


def plan_from_database(
    database_path: str | Path,
    *,
    policy: RetentionPolicy | None = None,
    pin_entries: Sequence[Mapping[str, Any]] = (),
    pin_source: str | None = None,
) -> ConsistentSnapshot[RetentionPlan]:
    """Build a plan under the repository's WAL-aware read guard.

    Returns the `ConsistentSnapshot` rather than the plan alone, so the report
    can state the guarantee it actually got: `PRAGMA data_version` before and
    after, one deferred read transaction between them, and `quick_check`
    inside the window. A concurrent commit raises rather than producing a plan
    that mixes pre- and post-change observations.

    `integrity_check` is passed explicitly from this module's own binding,
    matching every other audit here. It is not redundant: the integrity read
    runs *inside* the guarded window, and passing the binding is what lets a
    WAL regression test wrap it to commit from a second connection at exactly
    that point. Relying on the parameter's default would bind the function at
    definition time and make that window untestable.
    """
    return read_consistent_snapshot(
        database_path,
        lambda connection: build_plan(
            connection,
            policy=policy,
            pin_entries=pin_entries,
            pin_source=pin_source,
        ),
        context="revision retention planning",
        integrity_check=quick_check,
    )


# --- output ---------------------------------------------------------------


def write_json(plan: RetentionPlan, path: str | Path) -> Path:
    """Deterministic JSON: sorted keys, fixed indent, trailing newline."""
    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(
        json.dumps(plan.as_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return resolved


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ";".join(str(item) for item in value)
    return str(value)


def write_csv(plan: RetentionPlan, path: str | Path) -> Path:
    """Deterministic CSV, one row per revision.

    `newline=""` on the file and an explicit `lineterminator` are both
    required for determinism: without the first, Python's text layer would
    translate the writer's line endings on Windows and produce a different
    file from the same plan; without the second, the writer's default is CRLF
    and the same plan would hash differently depending on which the reader
    expected. List columns are joined with ';' because ',' is the delimiter
    and a quoted embedded comma is harder to diff.
    """
    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)

    with resolved.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(CSV_COLUMNS)

        for row in plan.revisions:
            record = row.as_dict(
                planner_version=plan.planner_version,
                snapshot_digest=plan.snapshot_digest,
            )
            writer.writerow(
                [_csv_cell(record[column]) for column in CSV_COLUMNS]
            )

    return resolved
