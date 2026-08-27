"""Read-only backfill planner for revision-aware provenance (Step 4, slice 3).

This module plans and never writes. It reads one provably-consistent snapshot
of the database, classifies every row that will receive an ownership column
into the bases of `docs/revision_aware_provenance_assessment.md` §7.1, and
emits a deterministic plan plus a digest of the inputs the plan was drawn
from. Migration 015 consumes that plan; nothing here applies it.

Two design documents govern what is planned, both merged:

- slice 1, `docs/revision_aware_provenance_assessment.md` -- the bases, the
  five receiving tables, and the per-table vocabularies;
- slice 2, `docs/page_inventory_design.md` -- page evidence is planned as
  `page_inventory` rows keyed by `archive_id`, not as `archive_pages` rows.

The distinction that shapes the whole module is between a **frozen value**,
which the plan computes from existing data and digests, and a **target
state**, which the plan asserts and only the migration can value. `sealed_at`
and `created_at` are target states: a plan computed before the migration runs
cannot name them without predicting a clock (slice 2 §10.1).
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from comic_automation.database.read_guards import read_consistent_snapshot

# Bumped whenever a classification rule, its inputs, or the canonical digest
# rendering changes, so a plan produced by an older planner can never collide
# with this one's digest even over an identical database.
PLANNER_VERSION = "provenance-backfill-planner/1"
SNAPSHOT_DIGEST_VERSION = "provenance-backfill-snapshot/1"

# This planner reads. The constant exists so a reader of the emitted artifact
# never has to infer it from the absence of an "applied" field.
EXECUTION_STATUS = "not_performed"

# --- bases, from slice 1 §7.1 ---------------------------------------------

MEASURED = "measured"
STAT_MATCHED = "stat_matched_revision"
IDENTITY_SEED = "migration_014_identity_seed"
FIELD_SEED = "migration_014_field_seed"
SINGLE_REVISION_INHERITED = "single_revision_inherited"
INHERITED_FROM_PAGE_EVIDENCE = "inherited_from_page_evidence"
UNRESOLVED_DRIFT = "unresolved_drift"
UNRESOLVED_NO_IDENTITY = "unresolved_no_identity"

BOUND_BASES = frozenset(
    {
        MEASURED,
        STAT_MATCHED,
        IDENTITY_SEED,
        FIELD_SEED,
        SINGLE_REVISION_INHERITED,
        INHERITED_FROM_PAGE_EVIDENCE,
    }
)
UNRESOLVED_BASES = frozenset({UNRESOLVED_DRIFT, UNRESOLVED_NO_IDENTITY})
ALL_BASES = BOUND_BASES | UNRESOLVED_BASES

# Each receiving table's own narrower vocabulary (slice 1 §9.4.2). The global
# union above is not a licence: `measured` is legal only where a producer
# computes a digest, which the backfill never does.
TABLE_VOCABULARY: Mapping[str, frozenset[str]] = {
    "archive_hashes": frozenset({MEASURED, IDENTITY_SEED}),
    "archive_content_signatures": frozenset(
        {STAT_MATCHED, FIELD_SEED, UNRESOLVED_DRIFT, UNRESOLVED_NO_IDENTITY}
    ),
    "archive_inspections": frozenset(
        {STAT_MATCHED, SINGLE_REVISION_INHERITED, UNRESOLVED_NO_IDENTITY}
    ),
    "page_inventory": frozenset(
        {
            STAT_MATCHED,
            SINGLE_REVISION_INHERITED,
            UNRESOLVED_DRIFT,
            UNRESOLVED_NO_IDENTITY,
        }
    ),
    "near_duplicate_candidates": frozenset(
        {INHERITED_FROM_PAGE_EVIDENCE, SINGLE_REVISION_INHERITED, UNRESOLVED_NO_IDENTITY}
    ),
}

RECEIVING_TABLES = tuple(TABLE_VOCABULARY)

# `page_inventory` rows do not exist when this planner runs, so they are
# planned by their natural key rather than a row id (slice 2 §10.1). Every
# other table's rows already exist and are planned by id.
NATURAL_KEY_TABLES = frozenset({"page_inventory"})

# Slice 2 §4.2: the parent's timestamp comes from its children unless it has
# none, in which case it comes from the signature that recorded the result.
FIRST_PAGE_PERSISTENCE = "first_page_persistence"
SIGNATURE_CALCULATED_AT = "signature_calculated_at"

# Slice 2 §11: every inventory the migration mints is minted sealed, because
# the extraction it describes completed long ago. The plan asserts the state;
# the migration supplies the timestamp.
TARGET_STATES = ("sealed",)


class BackfillPlannerError(RuntimeError):
    """Base class for every refusal this module raises."""


class PlannerInvariantError(BackfillPlannerError):
    """A plan failed to reconcile against itself.

    Raised rather than returned, because an unreconciled plan is not a
    weaker plan -- it is a plan whose totals cannot be trusted, and handing
    one to an operator is worse than handing them nothing.
    """


class OutputPathError(BackfillPlannerError):
    """An output path would overwrite something, or sits beside the database."""


@dataclass(frozen=True)
class PlannedBinding:
    """One row that migration 015 will write, and how it was classified.

    `key` is a `row_id` for the four tables whose rows already exist, and an
    `archive_id` for `page_inventory`, whose rows the migration creates. The
    `key_kind` field records which, so a consumer never has to infer it from
    the table name.
    """

    table: str
    key: int
    key_kind: str
    archive_id: int
    source_revision_id: int | None
    provenance_basis: str
    # Table-specific frozen values, empty for tables that write only the
    # ownership pair. Kept as a mapping rather than a widening set of
    # optional columns so a table that gains a planned field does not change
    # this class's shape.
    values: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.provenance_basis not in ALL_BASES:
            raise PlannerInvariantError(
                f"{self.table} row {self.key}: unknown basis "
                f"{self.provenance_basis!r}"
            )

        vocabulary = TABLE_VOCABULARY.get(self.table)

        if vocabulary is None:
            raise PlannerInvariantError(f"unknown receiving table {self.table!r}")

        if self.provenance_basis not in vocabulary:
            raise PlannerInvariantError(
                f"{self.table} row {self.key}: basis "
                f"{self.provenance_basis!r} is not in that table's vocabulary"
            )

        bound = self.provenance_basis in BOUND_BASES

        # The paired invariant of slice 1 §9.2, enforced at plan time rather
        # than left for the migration's CHECK to discover: a row may not
        # carry a revision without saying how it got one, or omit one
        # without saying why.
        if bound and self.source_revision_id is None:
            raise PlannerInvariantError(
                f"{self.table} row {self.key}: bound basis "
                f"{self.provenance_basis!r} with no revision"
            )

        if not bound and self.source_revision_id is not None:
            raise PlannerInvariantError(
                f"{self.table} row {self.key}: unresolved basis "
                f"{self.provenance_basis!r} carries revision "
                f"{self.source_revision_id}"
            )

    @property
    def bound(self) -> bool:
        return self.provenance_basis in BOUND_BASES

    def canonical_line(self) -> str:
        """One digest line. Field names travel with values so reordering
        cannot collide, and `values` is rendered sorted for the same reason.
        """
        rendered = "|".join(
            f"{name}={'' if value is None else value}"
            for name, value in sorted(self.values.items())
        )
        return (
            f"binding|table={self.table}"
            f"|key_kind={self.key_kind}|key={self.key}"
            f"|archive_id={self.archive_id}"
            f"|revision={'' if self.source_revision_id is None else self.source_revision_id}"
            f"|basis={self.provenance_basis}"
            + (f"|{rendered}" if rendered else "")
        )

    def as_dict(self, *, planner_version: str, snapshot_digest: str) -> dict[str, Any]:
        row: dict[str, Any] = {
            "table": self.table,
            "key_kind": self.key_kind,
            "key": self.key,
            "archive_id": self.archive_id,
            "source_revision_id": self.source_revision_id,
            "provenance_basis": self.provenance_basis,
            "bound": self.bound,
            "planner_version": planner_version,
            "snapshot_digest": snapshot_digest,
        }
        row.update(self.values)
        return row


CSV_COLUMNS = (
    "table",
    "key_kind",
    "key",
    "archive_id",
    "source_revision_id",
    "provenance_basis",
    "bound",
    "page_count",
    "content_digest",
    "location_id",
    "extracted_at",
    "extracted_at_basis",
    "inspector_version_basis",
    "parameters_basis",
    "planner_version",
    "snapshot_digest",
)


@dataclass(frozen=True)
class ArchiveGate:
    """Archive-level facts that are gates rather than row classifications.

    Slice 1 §7.3: the 147 provisional archives have only job rows, and jobs
    are excluded from provenance, so no receiving table holds a row for them.
    They are reported here so the population stays visible instead of
    vanishing because it produced nothing to classify.
    """

    provisional_archives: tuple[int, ...]
    archives_without_revision: tuple[int, ...]
    drift_archives: tuple[int, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "provisional_archives": len(self.provisional_archives),
            "archives_without_revision": len(self.archives_without_revision),
            "drift_archives": len(self.drift_archives),
            "drift_archive_ids": list(self.drift_archives),
        }


@dataclass(frozen=True)
class _Inputs:
    """Everything read from the snapshot, before any classification.

    Held as one object so the digest hashes *inputs* rather than the plan's
    own conclusions: hashing the output would make the digest a checksum of a
    decision, while hashing the input makes it an identity for the state that
    decision was drawn from.
    """

    revisions: Mapping[int, tuple[int, str, str | None]]
    """revision_id -> (archive_id, identity_state, archive_sha256)"""
    revision_by_archive: Mapping[int, int]
    hashes: tuple[tuple[int, int], ...]
    signatures: tuple[tuple[int, int, int, str, int | None, str], ...]
    inspections: tuple[tuple[int, int], ...]
    page_archives: Mapping[int, tuple[int, str, int | None]]
    """archive_id -> (page_count, min_child_created_at, location_id)"""
    candidates: tuple[tuple[int, int, int], ...]
    signature_by_archive: Mapping[int, tuple[int, int | None, str]]
    """archive_id -> (page_count, location_id, calculated_at)"""
    signature_digests: Mapping[int, str]
    drift_archives: frozenset[int]
    provisional_archives: tuple[int, ...]
    archives_without_revision: tuple[int, ...]
    quarantine_rows: int


def _read_inputs(connection: sqlite3.Connection) -> _Inputs:
    """One pass over the snapshot. Every query here is a SELECT."""
    connection.row_factory = sqlite3.Row

    revisions: dict[int, tuple[int, str, str | None]] = {}
    revision_by_archive: dict[int, int] = {}

    for row in connection.execute(
        "SELECT id, archive_id, identity_state, archive_sha256 "
        "FROM archive_revisions ORDER BY id"
    ):
        revisions[int(row["id"])] = (
            int(row["archive_id"]),
            str(row["identity_state"]),
            row["archive_sha256"],
        )
        # Slice 1 §3.1 measured exactly one revision per archive. If that ever
        # stops being true the planner must not silently pick one, so the
        # reconciliation below counts archives with more than one and the gate
        # reports them rather than this dict quietly keeping the last.
        revision_by_archive.setdefault(int(row["archive_id"]), int(row["id"]))

    multi = [
        int(row["archive_id"])
        for row in connection.execute(
            "SELECT archive_id FROM archive_revisions "
            "GROUP BY archive_id HAVING count(*) > 1 ORDER BY archive_id"
        )
    ]

    if multi:
        raise PlannerInvariantError(
            f"{len(multi)} archive(s) hold more than one revision; this "
            "planner's page-inventory key (archive_id) is only unique while "
            "each archive has exactly one. Re-plan after slice 2's unit is "
            "revisited."
        )

    hashes = tuple(
        (int(row["id"]), int(row["archive_id"]))
        for row in connection.execute(
            "SELECT id, archive_id FROM archive_hashes ORDER BY id"
        )
    )

    signatures = tuple(
        (
            int(row["id"]),
            int(row["archive_id"]),
            int(row["page_count"]),
            str(row["digest"]),
            None if row["location_id"] is None else int(row["location_id"]),
            str(row["calculated_at"]),
        )
        for row in connection.execute(
            "SELECT id, archive_id, page_count, digest, location_id, calculated_at "
            "FROM archive_content_signatures ORDER BY id"
        )
    )

    inspections = tuple(
        (int(row["id"]), int(row["archive_id"]))
        for row in connection.execute(
            "SELECT id, archive_id FROM archive_inspections ORDER BY id"
        )
    )

    # Page evidence is aggregated to its inventory here, which is the whole
    # point of slice 2: 58,437 planned rows rather than 2,955,391.
    page_archives = {
        int(row["archive_id"]): (
            int(row["page_count"]),
            str(row["extracted_at"]),
            None if row["location_id"] is None else int(row["location_id"]),
        )
        for row in connection.execute(
            "SELECT archive_id, count(*) AS page_count, "
            "       min(created_at) AS extracted_at, "
            "       min(location_id) AS location_id "
            "FROM archive_pages GROUP BY archive_id ORDER BY archive_id"
        )
    }

    candidates = tuple(
        (int(row["id"]), int(row["archive_a_id"]), int(row["archive_b_id"]))
        for row in connection.execute(
            "SELECT id, archive_a_id, archive_b_id "
            "FROM near_duplicate_candidates ORDER BY id"
        )
    )

    # Drift: the signature describes bytes whose size differs from the file on
    # disk now, while the hash agrees with it (slice 1 §4.2). Size, not mtime
    # -- §3.4 measured mtime-only disagreement on 439 archives and declined to
    # treat it as evidence that bytes changed.
    drift_archives = frozenset(
        int(row["archive_id"])
        for row in connection.execute(
            """
            SELECT s.archive_id
              FROM archive_content_signatures AS s
              JOIN file_locations AS fl
                ON fl.archive_id = s.archive_id AND fl.is_current = 1
             WHERE fl.file_size IS NOT NULL
               AND s.source_file_size <> fl.file_size
             ORDER BY s.archive_id
            """
        )
    )

    provisional_archives = tuple(
        archive_id
        for revision_id, (archive_id, state, _digest) in sorted(revisions.items())
        if state == "provisional"
    )

    archives_without_revision = tuple(
        int(row["id"])
        for row in connection.execute(
            "SELECT id FROM archive_files WHERE NOT EXISTS "
            "(SELECT 1 FROM archive_revisions WHERE archive_id = archive_files.id) "
            "ORDER BY id"
        )
    )

    quarantine_rows = int(
        connection.execute("SELECT count(*) FROM archive_quarantine").fetchone()[0]
    )

    signature_by_archive = {
        archive_id: (pages, location, calculated)
        for _id, archive_id, pages, _digest, location, calculated in signatures
    }
    signature_digests = {
        archive_id: digest
        for _id, archive_id, _pages, digest, _loc, _calc in signatures
    }

    return _Inputs(
        revisions=revisions,
        revision_by_archive=revision_by_archive,
        hashes=hashes,
        signatures=signatures,
        inspections=inspections,
        page_archives=page_archives,
        candidates=candidates,
        signature_by_archive=signature_by_archive,
        signature_digests=signature_digests,
        drift_archives=drift_archives,
        provisional_archives=provisional_archives,
        archives_without_revision=archives_without_revision,
        quarantine_rows=quarantine_rows,
    )


def _signature_digest(inputs: "_Inputs", archive_id: int) -> str:
    """The ordered-page digest already computed for this archive.

    The inventory's `content_digest` is not recomputed here. Slice 2 §8.4.1
    puts digest verification at the migration, which recomputes from the
    children it attaches; the planner freezes the value that exists so the
    two can be compared rather than both being derived the same way.
    """
    digest = inputs.signature_digests.get(archive_id)

    if digest is None:
        raise PlannerInvariantError(
            f"archive {archive_id} has no content signature to seed its inventory"
        )

    return digest


def _classify(inputs: "_Inputs") -> list[PlannedBinding]:
    """Turn the snapshot into one planned binding per receiving row.

    Nothing here measures or re-reads anything, which is why `measured` and
    `stat_matched_revision` cannot appear: both describe a producer that read
    bytes, and the backfill reads only the database (slice 1 §7.1).
    """
    bindings: list[PlannedBinding] = []

    # --- archive_hashes: the identity seed, and the only table that has one.
    # Each of these rows IS what created its revision's archive_sha256, so
    # binding it records the actual causal history of the identity.
    for row_id, archive_id in inputs.hashes:
        bindings.append(
            PlannedBinding(
                table="archive_hashes",
                key=row_id,
                key_kind="row_id",
                archive_id=archive_id,
                source_revision_id=inputs.revision_by_archive.get(archive_id),
                provenance_basis=IDENTITY_SEED,
            )
        )

    # --- archive_content_signatures: field seeds, except where the signature
    # describes a byte generation no revision holds.
    for row_id, archive_id, _pages, _digest, _loc, _calc in inputs.signatures:
        drift = archive_id in inputs.drift_archives
        bindings.append(
            PlannedBinding(
                table="archive_content_signatures",
                key=row_id,
                key_kind="row_id",
                archive_id=archive_id,
                source_revision_id=(
                    None if drift else inputs.revision_by_archive.get(archive_id)
                ),
                provenance_basis=UNRESOLVED_DRIFT if drift else FIELD_SEED,
            )
        )

    # --- archive_inspections: inherited. Migration 014 never joined this
    # table; exactly one candidate revision existed and nothing contradicted
    # it. Unique, unchallenged, and unverified.
    for row_id, archive_id in inputs.inspections:
        bindings.append(
            PlannedBinding(
                table="archive_inspections",
                key=row_id,
                key_kind="row_id",
                archive_id=archive_id,
                source_revision_id=inputs.revision_by_archive.get(archive_id),
                provenance_basis=SINGLE_REVISION_INHERITED,
                values={"inspector_version_basis": "unknown_legacy"},
            )
        )

    # --- page_inventory: one row per extraction result, planned by
    # archive_id because the row does not exist yet (slice 2 §10.1). The
    # SIGNATURE is the authority for which archives have an extraction
    # result, not archive_pages: a zero-page result has a signature and no
    # page rows at all (slice 2 §4.5).
    for archive_id in sorted(inputs.signature_by_archive):
        _pages, sig_location, calculated_at = inputs.signature_by_archive[archive_id]
        drift = archive_id in inputs.drift_archives
        child = inputs.page_archives.get(archive_id)

        if child is None:
            page_count, extracted_at, location_id = 0, calculated_at, sig_location
            extracted_at_basis = SIGNATURE_CALCULATED_AT
        else:
            page_count, extracted_at, location_id = child
            extracted_at_basis = FIRST_PAGE_PERSISTENCE

        bindings.append(
            PlannedBinding(
                table="page_inventory",
                key=archive_id,
                key_kind="archive_id",
                archive_id=archive_id,
                source_revision_id=(
                    None if drift else inputs.revision_by_archive.get(archive_id)
                ),
                provenance_basis=(
                    UNRESOLVED_DRIFT if drift else SINGLE_REVISION_INHERITED
                ),
                values={
                    "page_count": page_count,
                    "content_digest": _signature_digest(inputs, archive_id),
                    "location_id": location_id,
                    "extracted_at": extracted_at,
                    "extracted_at_basis": extracted_at_basis,
                },
            )
        )

    # --- near_duplicate_candidates: the 3,000 backfilled rows were bound by
    # the one-revision-per-archive census, not from page evidence, so they
    # take `single_revision_inherited` rather than the producer's
    # `inherited_from_page_evidence` (slice 1 §7.4).
    for row_id, archive_a, archive_b in inputs.candidates:
        revision_a = inputs.revision_by_archive.get(archive_a)
        revision_b = inputs.revision_by_archive.get(archive_b)
        both_bound = revision_a is not None and revision_b is not None
        bindings.append(
            PlannedBinding(
                table="near_duplicate_candidates",
                key=row_id,
                key_kind="row_id",
                archive_id=archive_a,
                source_revision_id=revision_a if both_bound else None,
                provenance_basis=(
                    SINGLE_REVISION_INHERITED if both_bound else UNRESOLVED_NO_IDENTITY
                ),
                values={
                    "archive_b_id": archive_b,
                    "revision_b_id": revision_b if both_bound else None,
                    "parameters_basis": "unknown_legacy",
                },
            )
        )

    return bindings


def canonical_snapshot_lines(inputs: "_Inputs") -> list[str]:
    """Every input that can change a classification, canonically rendered.

    Inputs, not outputs, for the reason the retention planner gives: hashing
    the plan's rows would make the digest a checksum of a conclusion, while
    hashing the inputs makes it an identity for the state the conclusion was
    drawn from -- which is what binds a reviewed plan to a database.

    Every section carries an explicit count, so dropping a whole section
    cannot produce a colliding digest, and field names travel with their
    values so reordering cannot either.
    """
    lines = [SNAPSHOT_DIGEST_VERSION, "planner_version=" + PLANNER_VERSION]

    lines.append("revisions|count=%d" % len(inputs.revisions))
    for revision_id in sorted(inputs.revisions):
        archive_id, state, digest = inputs.revisions[revision_id]
        lines.append(
            "revision|id=%d|archive_id=%d|identity_state=%s|sha256=%s"
            % (revision_id, archive_id, state, "" if digest is None else digest)
        )

    for label, rows in (("hashes", inputs.hashes), ("inspections", inputs.inspections)):
        lines.append("%s|count=%d" % (label, len(rows)))
        for row_id, archive_id in rows:
            lines.append("%s|id=%d|archive_id=%d" % (label, row_id, archive_id))

    lines.append("signatures|count=%d" % len(inputs.signatures))
    for row_id, archive_id, pages, digest, location, calculated in inputs.signatures:
        lines.append(
            "signature|id=%d|archive_id=%d|pages=%d|digest=%s|location=%s"
            "|calculated_at=%s"
            % (row_id, archive_id, pages, digest,
               "" if location is None else location, calculated)
        )

    lines.append("page_archives|count=%d" % len(inputs.page_archives))
    for archive_id in sorted(inputs.page_archives):
        pages, extracted_at, location = inputs.page_archives[archive_id]
        lines.append(
            "page_archive|archive_id=%d|pages=%d|extracted_at=%s|location=%s"
            % (archive_id, pages, extracted_at,
               "" if location is None else location)
        )

    lines.append("candidates|count=%d" % len(inputs.candidates))
    for row_id, archive_a, archive_b in inputs.candidates:
        lines.append("candidate|id=%d|a=%d|b=%d" % (row_id, archive_a, archive_b))

    for label, values in (
        ("drift", sorted(inputs.drift_archives)),
        ("provisional", list(inputs.provisional_archives)),
        ("archive_without_revision", list(inputs.archives_without_revision)),
    ):
        lines.append("%s|count=%d" % (label, len(values)))
        for archive_id in values:
            lines.append("%s|archive_id=%d" % (label, archive_id))

    lines.append("quarantine|rows=%d" % inputs.quarantine_rows)
    return lines


def compute_snapshot_digest(inputs: "_Inputs") -> str:
    """SHA-256 over `canonical_snapshot_lines`, lowercase hex.

    Lines are joined with a newline and given a trailing one, so no rendering
    can be a prefix of another. An empty database still yields a digest -- of
    the version markers and zero counts -- so "there is nothing to backfill"
    is a statement an operator can bind as firmly as any other.
    """
    payload = "\n".join(canonical_snapshot_lines(inputs)) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def plan_totals(bindings: Sequence[PlannedBinding]) -> dict[str, Any]:
    """Reconcile the plan, and refuse to return unreconciled totals.

    The totals are what an operator is asked to trust, so they check
    themselves rather than trusting the classifier. Two independent
    reconciliations must hold: bound plus unresolved equals the row count for
    every table, and the per-basis breakdown sums to the same figure. A
    classifier bug that mislabels a row shows up in the second even when the
    first still balances.
    """
    per_table: dict[str, dict[str, int]] = {}

    for table in RECEIVING_TABLES:
        per_table[table] = {"rows": 0, "bound": 0, "unresolved": 0}

    per_basis: dict[str, int] = {basis: 0 for basis in sorted(ALL_BASES)}

    for binding in bindings:
        bucket = per_table.get(binding.table)

        if bucket is None:
            raise PlannerInvariantError(
                f"binding names {binding.table!r}, which is not a receiving table"
            )

        if binding.provenance_basis not in per_basis:
            # Reachable only if a binding was built around __post_init__ --
            # which the reconciliation tests do deliberately. A KeyError here
            # would be a crash rather than a refusal, and the whole point of
            # this function is that it refuses rather than returning totals it
            # cannot stand behind.
            raise PlannerInvariantError(
                f"{binding.table} row {binding.key}: unknown basis "
                f"{binding.provenance_basis!r} reached the totals"
            )

        bucket["rows"] += 1
        bucket["bound" if binding.bound else "unresolved"] += 1
        bucket.setdefault(binding.provenance_basis, 0)
        bucket[binding.provenance_basis] += 1
        per_basis[binding.provenance_basis] += 1

    for table, bucket in per_table.items():
        if bucket["bound"] + bucket["unresolved"] != bucket["rows"]:
            raise PlannerInvariantError(
                f"{table}: bound {bucket['bound']} + unresolved "
                f"{bucket['unresolved']} does not equal {bucket['rows']} rows"
            )

    # The accumulation above is checked against an INDEPENDENT recount of the
    # same bindings, rather than against itself. Summing the buckets the loop
    # just filled would be tautological -- every branch increments `rows` and
    # exactly one basis, so the two can never disagree however wrong the
    # classification is. Recounting from the sequence catches a bug in the
    # loop, which is the only thing this check can usefully catch.
    recount_rows = Counter(binding.table for binding in bindings)
    recount_basis = Counter(binding.provenance_basis for binding in bindings)
    recount_bound = Counter(
        binding.table for binding in bindings if binding.bound
    )

    for table, bucket in per_table.items():
        if bucket["rows"] != recount_rows.get(table, 0):
            raise PlannerInvariantError(
                f"{table}: accumulated {bucket['rows']} rows, recount says "
                f"{recount_rows.get(table, 0)}"
            )

        if bucket["bound"] != recount_bound.get(table, 0):
            raise PlannerInvariantError(
                f"{table}: accumulated {bucket['bound']} bound rows, recount "
                f"says {recount_bound.get(table, 0)}"
            )

    for basis, count in per_basis.items():
        if count != recount_basis.get(basis, 0):
            raise PlannerInvariantError(
                f"basis {basis}: accumulated {count}, recount says "
                f"{recount_basis.get(basis, 0)}"
            )

    total_rows = sum(bucket["rows"] for bucket in per_table.values())

    if total_rows != len(bindings):
        raise PlannerInvariantError(
            f"per-table totals sum to {total_rows}, not {len(bindings)} bindings"
        )

    return {
        "planned_rows": total_rows,
        "bound": sum(b["bound"] for b in per_table.values()),
        "unresolved": sum(b["unresolved"] for b in per_table.values()),
        "per_table": {t: dict(sorted(b.items())) for t, b in per_table.items()},
        "per_basis": per_basis,
    }


@dataclass(frozen=True)
class BackfillPlan:
    """A complete, reconciled, read-only backfill plan."""

    planner_version: str
    snapshot_digest: str
    bindings: tuple[PlannedBinding, ...]
    totals: Mapping[str, Any]
    gates: ArchiveGate
    quarantine_rows: int

    @property
    def gate_failures(self) -> tuple[str, ...]:
        """Every reason this plan does not pass its own gate.

        These are not classifier errors -- a classifier error raises. These
        are conditions under which the plan is internally sound but should
        not be applied, and each is reported separately so the operator is
        told which one fired rather than handed one boolean.
        """
        failures: list[str] = []

        # The backfill measures nothing and re-reads nothing, so a producer
        # basis appearing here means the classifier reached a path it should
        # not have (slice 1 §7.1).
        for basis in (MEASURED, STAT_MATCHED, INHERITED_FROM_PAGE_EVIDENCE):
            count = self.totals["per_basis"].get(basis, 0)
            if count:
                failures.append(
                    f"{count} row(s) planned as {basis}, which only a producer "
                    "can establish"
                )

        if self.gates.archives_without_revision:
            failures.append(
                f"{len(self.gates.archives_without_revision)} archive(s) hold "
                "no revision row and could not be classified at all"
            )

        return tuple(failures)

    def as_dict(self) -> dict[str, Any]:
        """The plan envelope. Per-row bindings travel in the CSV.

        The split is deliberate: 238,956 rows of JSON is an artifact nobody
        reads, while the envelope is the part a reviewer actually checks. Both
        carry the same digest, so a CSV can always be tied back to the
        envelope that approved it.
        """
        return {
            "planner_version": self.planner_version,
            "execution_status": EXECUTION_STATUS,
            "snapshot_digest": self.snapshot_digest,
            "target_states": list(TARGET_STATES),
            "receiving_tables": list(RECEIVING_TABLES),
            "natural_key_tables": sorted(NATURAL_KEY_TABLES),
            "table_vocabulary": {
                table: sorted(bases) for table, bases in TABLE_VOCABULARY.items()
            },
            "totals": dict(self.totals),
            "archive_gates": self.gates.as_dict(),
            "quarantine_rows_excluded": self.quarantine_rows,
            "gate_failures": list(self.gate_failures),
        }


def build_plan(connection: sqlite3.Connection) -> BackfillPlan:
    """Classify one already-open read-only snapshot.

    Separated from `plan_backfill` so tests can drive a fixture connection
    without going through the guarded reader, and so the guarded reader stays
    the only thing that decides what "one snapshot" means.
    """
    inputs = _read_inputs(connection)
    bindings = _classify(inputs)
    bindings.sort(key=lambda b: (b.table, b.key))

    return BackfillPlan(
        planner_version=PLANNER_VERSION,
        snapshot_digest=compute_snapshot_digest(inputs),
        bindings=tuple(bindings),
        totals=plan_totals(bindings),
        gates=ArchiveGate(
            provisional_archives=inputs.provisional_archives,
            archives_without_revision=inputs.archives_without_revision,
            drift_archives=tuple(sorted(inputs.drift_archives)),
        ),
        quarantine_rows=inputs.quarantine_rows,
    )


def plan_backfill(database: str | Path):
    """Plan against one provably-consistent read-only snapshot.

    Returns the guarded reader's `ConsistentSnapshot`, so the caller gets the
    plan together with the `quick_check` result and the `data_version` pair
    that prove nothing moved underneath it. Callers report those alongside the
    plan rather than claiming a guarantee the read did not make.
    """
    return read_consistent_snapshot(
        database, build_plan, context="provenance backfill plan"
    )


# --- artifacts ------------------------------------------------------------


def _refuse_unsafe_output(path: Path, database: Path | None) -> None:
    if path.exists():
        raise OutputPathError(f"refusing to overwrite an existing file: {path}")

    if database is not None and path.parent.resolve() == database.parent.resolve():
        # The guarded-operation rules keep reports away from the database
        # directory, so a read-only planner can never be the thing that put a
        # file next to production.
        raise OutputPathError(
            f"refusing to write beside the database: {path.parent}"
        )


def write_plan_json(plan: BackfillPlan, path: str | Path, *,
                    database: str | Path | None = None) -> Path:
    """Write the plan envelope. Deterministic: sorted keys, fixed separators."""
    target = Path(path)
    _refuse_unsafe_output(target, Path(database) if database else None)
    target.write_text(
        json.dumps(plan.as_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return target


def write_plan_csv(plan: BackfillPlan, path: str | Path, *,
                   database: str | Path | None = None) -> Path:
    """Write one row per planned binding, in the plan's canonical order."""
    target = Path(path)
    _refuse_unsafe_output(target, Path(database) if database else None)

    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=CSV_COLUMNS, extrasaction="ignore"
        )
        writer.writeheader()
        for binding in plan.bindings:
            writer.writerow(
                binding.as_dict(
                    planner_version=plan.planner_version,
                    snapshot_digest=plan.snapshot_digest,
                )
            )

    return target
