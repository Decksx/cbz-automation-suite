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
import io
import json
import os
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from comic_automation.archive import output_guards
from comic_automation.database.read_guards import read_consistent_snapshot

# Bumped whenever a classification rule, its inputs, or the canonical digest
# rendering changes, so a plan produced by an older planner can never collide
# with this one's digest even over an identical database.
# Version 2 across all three markers. The canonical rendering changed from
# delimiter-joined `name=value` to canonical JSON, and both digests now cover
# inputs they previously ignored, so a version 1 digest and a version 2 digest
# over the identical database are different strings and must not be compared.
# Any plan digest recorded against version 1 -- including any quoted in a
# review or handoff -- is superseded rather than contradicted.
PLANNER_VERSION = "provenance-backfill-planner/2"
SNAPSHOT_DIGEST_VERSION = "provenance-backfill-snapshot/2"

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

# Slice 2 §10.1 and §11: the two facts the plan asserts and only the
# migration can value. `sealed` because every minted inventory describes an
# extraction that completed long ago; `created_at` because a plan computed
# before the migration runs cannot name the migration's own clock. An earlier
# revision of this module listed only the first, which read as though
# `created_at` had been forgotten rather than deliberately deferred.
TARGET_STATES = ("sealed", "created_at_from_migration_clock")

# What each receiving table's plan row carries beyond the ownership pair.
# Artifact columns are generated from this rather than from a hand-kept list,
# and a binding whose values do not match its table's tuple EXACTLY is
# refused -- neither a missing field nor an unexpected one may pass.
ARTIFACT_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "archive_hashes": (),
    "archive_content_signatures": (),
    "archive_inspections": ("inspector_version", "inspector_version_basis"),
    "page_inventory": (
        "page_count",
        "content_digest",
        "location_id",
        "extracted_at",
        "extracted_at_basis",
    ),
    "near_duplicate_candidates": ("archive_b_id", "parameters_basis"),
}

# Tables whose attribution is pairwise. Their sides bind independently, so a
# single ownership pair cannot describe them (slice 1 §7.4).
PAIRWISE_TABLES = frozenset({"near_duplicate_candidates"})


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


def _canonical(value: Any) -> Any:
    """Reduce a value to something JSON renders injectively.

    Only `None`, `bool`, `int` and `str` are accepted as scalars, plus
    mappings and sequences of them. The rejection is load-bearing rather than
    defensive: `float` is the type that would silently reintroduce ambiguity,
    because two different floats can share a repr and `json` would render
    them identically, so a digest could stop distinguishing them. Nothing in
    the plan is a float today, and this is what makes that a checked property
    instead of a habit.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value

    if isinstance(value, Mapping):
        return {str(name): _canonical(item) for name, item in value.items()}

    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]

    raise PlannerInvariantError(
        f"{type(value).__name__} cannot be canonically rendered; a digest over "
        "it would not be reproducible"
    )


def _canonical_json(payload: Mapping[str, Any]) -> str:
    """One digest line, rendered so no two distinct payloads can collide.

    JSON, not `name=value` joined by `|`. The previous rendering escaped
    nothing, so a value containing a delimiter could reproduce another
    record's line exactly -- two different valid binding shapes were shown to
    produce identical canonical lines and therefore identical plan digests.
    JSON escapes the delimiters inside strings, so no value can forge
    structure; `sort_keys` and explicit separators keep it deterministic;
    `ensure_ascii` keeps it byte-stable regardless of locale; and `None`
    renders as `null`, distinct from the empty string that the old rendering
    substituted for it.
    """
    return json.dumps(
        _canonical(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


@dataclass(frozen=True)
class SideAttribution:
    """One side's ownership, because some tables have more than one.

    `near_duplicate_candidates` compares two page sets and each side binds
    independently (slice 1 §7.4), so a single revision/basis pair cannot
    describe it: a candidate whose A side is bound and whose B side is not is
    an ordinary case, and collapsing that to one pair discards A's valid
    attribution. Single-sided tables carry exactly one side, labelled "".
    """

    label: str
    archive_id: int
    source_revision_id: int | None
    provenance_basis: str

    @property
    def bound(self) -> bool:
        return self.provenance_basis in BOUND_BASES

    def suffix(self) -> str:
        return f"_{self.label}" if self.label else ""


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
    sides: tuple[SideAttribution, ...]
    # Table-specific frozen values. Checked against ARTIFACT_COLUMNS exactly,
    # so a field the artifact writer would drop cannot be created here.
    values: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        vocabulary = TABLE_VOCABULARY.get(self.table)

        if vocabulary is None:
            raise PlannerInvariantError(f"unknown receiving table {self.table!r}")

        expected_labels = ("a", "b") if self.table in PAIRWISE_TABLES else ("",)
        labels = tuple(side.label for side in self.sides)

        if labels != expected_labels:
            raise PlannerInvariantError(
                f"{self.table} row {self.key}: sides {labels} do not match "
                f"the expected {expected_labels}"
            )

        for side in self.sides:
            if side.provenance_basis not in ALL_BASES:
                raise PlannerInvariantError(
                    f"{self.table} row {self.key} side {side.label!r}: unknown "
                    f"basis {side.provenance_basis!r}"
                )

            if side.provenance_basis not in vocabulary:
                raise PlannerInvariantError(
                    f"{self.table} row {self.key} side {side.label!r}: basis "
                    f"{side.provenance_basis!r} is not in that table's vocabulary"
                )

            # The paired invariant of slice 1 §9.2, per side, enforced at plan
            # time rather than left for the migration's CHECK to discover.
            if side.bound and side.source_revision_id is None:
                raise PlannerInvariantError(
                    f"{self.table} row {self.key} side {side.label!r}: bound "
                    f"basis {side.provenance_basis!r} with no revision"
                )

            if not side.bound and side.source_revision_id is not None:
                raise PlannerInvariantError(
                    f"{self.table} row {self.key} side {side.label!r}: "
                    f"unresolved basis {side.provenance_basis!r} carries "
                    f"revision {side.source_revision_id}"
                )

        expected_values = set(ARTIFACT_COLUMNS[self.table])
        supplied = set(self.values)

        if supplied != expected_values:
            missing = sorted(expected_values - supplied)
            unexpected = sorted(supplied - expected_values)
            raise PlannerInvariantError(
                f"{self.table} row {self.key}: planned values do not match the "
                f"table's artifact columns (missing {missing}, unexpected "
                f"{unexpected})"
            )

    @property
    def bound(self) -> bool:
        """A row is bound only when EVERY side is.

        Slice 1 §9.6 defines a mixed-side candidate as unresolved, because a
        comparison is only as well-attributed as its weaker side. The per-side
        detail is not lost -- it travels in `sides` and is counted per side in
        the totals.
        """
        return all(side.bound for side in self.sides)

    def canonical_line(self) -> str:
        """One digest line, canonically encoded.

        Field names travelling with values is not sufficient on its own, which
        is what the previous `name=value` rendering joined by `|` assumed.
        Nothing escaped the delimiters, so a value containing one could
        reproduce a different binding's line exactly -- two distinct valid
        binding shapes were demonstrated to render identically and therefore
        to produce the same plan digest. `_canonical_json` is injective: the
        delimiters are escaped inside strings, the nesting keeps sides and
        values in their own scopes rather than flattened into one stream, and
        a missing value is `null` rather than the empty string.
        """
        return _canonical_json(
            {
                "k": "binding",
                "table": self.table,
                "key_kind": self.key_kind,
                "key": self.key,
                "archive_id": self.archive_id,
                "bound": self.bound,
                "sides": [
                    {
                        "label": side.label,
                        "archive_id": side.archive_id,
                        "source_revision_id": side.source_revision_id,
                        "provenance_basis": side.provenance_basis,
                    }
                    for side in self.sides
                ],
                "values": dict(self.values),
            }
        )

    def as_dict(self, *, planner_version: str, snapshot_digest: str,
                plan_digest: str) -> dict[str, Any]:
        row: dict[str, Any] = {
            "table": self.table,
            "key_kind": self.key_kind,
            "key": self.key,
            "archive_id": self.archive_id,
            "bound": self.bound,
            "planner_version": planner_version,
            "snapshot_digest": snapshot_digest,
            "plan_digest": plan_digest,
        }

        for side in self.sides:
            if side.label:
                row[f"archive_{side.label}_id"] = side.archive_id
                row[f"revision_{side.label}_id"] = side.source_revision_id
                row[f"provenance_basis_{side.label}"] = side.provenance_basis
            else:
                row["source_revision_id"] = side.source_revision_id
                row["provenance_basis"] = side.provenance_basis

        row.update(self.values)
        return row


def _csv_columns() -> tuple[str, ...]:
    """Every column any binding can emit, derived rather than hand-kept.

    An earlier revision listed these by hand and omitted `archive_b_id` and
    `revision_b_id`, which `DictWriter(extrasaction="ignore")` then dropped
    from the artifact without error. Both halves of that are fixed: the list
    is generated from `ARTIFACT_COLUMNS` and the side labels, and the writer
    raises on anything it did not expect.
    """
    columns = ["table", "key_kind", "key", "archive_id", "bound"]
    columns += ["source_revision_id", "provenance_basis"]

    for label in ("a", "b"):
        columns += [
            f"archive_{label}_id",
            f"revision_{label}_id",
            f"provenance_basis_{label}",
        ]

    for table in RECEIVING_TABLES:
        for name in ARTIFACT_COLUMNS[table]:
            if name not in columns:
                columns.append(name)

    columns += ["planner_version", "snapshot_digest", "plan_digest"]
    return tuple(columns)


CSV_COLUMNS = _csv_columns()


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

    revisions: Mapping[int, tuple[int, str, str | None, str | None]]
    """revision_id -> (archive_id, identity_state, archive_sha256, content_signature)

    Both digests travel, and not only so the snapshot digest covers them.
    They are what a seed basis is VALIDATED against: migration 014 built
    `archive_sha256` from `archive_hashes.digest` and `content_signature`
    from `archive_content_signatures.digest`, so a seed binding asserts a
    relationship that can be checked rather than assumed.
    """
    revision_by_archive: Mapping[int, int]
    """archive_id -> revision_id, ESTABLISHED revisions only.

    A provisional revision has no digest, so binding evidence to it would
    assert an identity the database explicitly says is unknown. Slice 1 §7.3
    requires such evidence to stay unresolved, so provisional archives are
    absent from this map and every lookup against it misses, which is the
    behaviour the classifier wants.
    """
    provisional_by_archive: Mapping[int, int]
    hashes: tuple[tuple[int, int, str], ...]
    """(row_id, archive_id, digest)

    The digest is read because the identity seed is checked against it and
    because both digests must cover it. Hashing only ids meant a changed
    `archive_hashes.digest` left the snapshot digest and the plan digest
    byte-identical, so the state a plan was approved against could be edited
    without invalidating the plan.
    """
    signatures: tuple[tuple[int, int, int, str, int | None, str], ...]
    inspections: tuple[tuple[int, int], ...]
    page_archives: Mapping[int, tuple[int, str, int | None, int, int, int, int]]
    """archive_id -> (page_count, min_child_created_at, location_id,
    distinct_location_ids, null_location_pages, min_index, max_index)

    The two location counters are separate on purpose. An earlier revision
    asked for `count(DISTINCT ifnull(location_id, -1))`, which cannot tell an
    unknown location from a legitimate location id of -1: one NULL child plus
    one child at -1 counted as a single location, and a genuinely mixed page
    set passed validation.
    """
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

    revisions: dict[int, tuple[int, str, str | None, str | None]] = {}
    revision_by_archive: dict[int, int] = {}
    provisional_by_archive: dict[int, int] = {}

    for row in connection.execute(
        "SELECT id, archive_id, identity_state, archive_sha256, "
        "       content_signature "
        "FROM archive_revisions ORDER BY id"
    ):
        revisions[int(row["id"])] = (
            int(row["archive_id"]),
            str(row["identity_state"]),
            row["archive_sha256"],
            row["content_signature"],
        )
        # Slice 1 §3.1 measured exactly one revision per archive. If that ever
        # stops being true the planner must not silently pick one, so the
        # reconciliation below counts archives with more than one and the gate
        # reports them rather than this dict quietly keeping the last.
        if str(row["identity_state"]) == "provisional":
            provisional_by_archive.setdefault(int(row["archive_id"]), int(row["id"]))
        else:
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
        (int(row["id"]), int(row["archive_id"]), str(row["digest"]))
        for row in connection.execute(
            "SELECT id, archive_id, digest FROM archive_hashes ORDER BY id"
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
    # point of slice 2: 58,437 planned rows rather than 2,955,391. Everything
    # the validation of `_classify` needs is aggregated in the same pass --
    # index bounds and the DISTINCT location count, not min(location_id),
    # because a minimum silently hides a mixed-location page set.
    page_archives = {
        int(row["archive_id"]): (
            int(row["page_count"]),
            str(row["extracted_at"]),
            None if row["location_id"] is None else int(row["location_id"]),
            int(row["distinct_location_ids"]),
            int(row["null_location_pages"]),
            int(row["min_index"]),
            int(row["max_index"]),
        )
        for row in connection.execute(
            "SELECT archive_id, count(*) AS page_count, "
            "       min(created_at) AS extracted_at, "
            "       min(location_id) AS location_id, "
            # count(DISTINCT location_id) ignores NULLs entirely, so the NULL
            # rows are counted in their own term. Folding them in with
            # ifnull(location_id, -1) conflates "no location" with location
            # id -1 and accepts a mixed set as a single location.
            "       count(DISTINCT location_id) AS distinct_location_ids, "
            "       sum(CASE WHEN location_id IS NULL THEN 1 ELSE 0 END) "
            "           AS null_location_pages, "
            "       min(page_index) AS min_index, "
            "       max(page_index) AS max_index "
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
        for revision_id, (archive_id, state, _sha, _sig) in sorted(revisions.items())
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

    # Slice 2 §10.1 makes the SIGNATURE the authority for which archives hold
    # an extraction result, and `_classify` iterates signatures for exactly
    # that reason. That leaves page rows whose archive has no signature with
    # nowhere to go: they produced no inventory, no gate failure and no
    # mention anywhere in the plan, so page evidence could disappear from a
    # backfill that reported success. There is no basis to classify them
    # under -- the signature is what a `page_inventory` row is seeded from --
    # so this refuses rather than inventing one.
    orphaned_page_archives = sorted(set(page_archives) - set(signature_by_archive))

    if orphaned_page_archives:
        shown = ", ".join(str(a) for a in orphaned_page_archives[:10])
        raise PlannerInvariantError(
            f"{len(orphaned_page_archives)} archive(s) hold page rows but no "
            "content signature, so no inventory would be planned for them and "
            f"their page evidence would leave the plan unreported: {shown}"
            + ("" if len(orphaned_page_archives) <= 10 else ", ...")
        )

    return _Inputs(
        revisions=revisions,
        revision_by_archive=revision_by_archive,
        provisional_by_archive=provisional_by_archive,
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


# Where each seed basis's expected value sits in an `_Inputs.revisions`
# tuple. Migration 014 populated both columns from the tables being classified
# here, which is the relationship `_validate_seed` re-checks.
_SEED_FIELDS: Mapping[str, int] = {"archive_sha256": 2, "content_signature": 3}


def _validate_seed(inputs: "_Inputs", *, table: str, archive_id: int,
                   revision_id: int, observed: str, field: str) -> None:
    """Refuse a seed binding whose digest disagrees with the revision it names.

    A seed basis is a CLAIM about where a revision's identity came from:
    migration 014 built `archive_sha256` from `archive_hashes.digest` and
    `content_signature` from `archive_content_signatures.digest`. Labelling a
    row `migration_014_identity_seed` without checking that relationship
    asserts a provenance the data may no longer support.

    Nothing else would catch it. Until both digests covered these values, a
    changed `archive_hashes.digest` left the snapshot digest and the plan
    digest byte-identical, so the row was still labelled a seed and no
    artifact recorded that its basis had stopped being true.
    """
    expected = inputs.revisions[revision_id][_SEED_FIELDS[field]]

    if expected is None:
        raise PlannerInvariantError(
            f"{table}: archive {archive_id} binds to revision {revision_id} as "
            f"a seed, but that revision carries no {field}; the relationship "
            "migration 014 established is not there to verify"
        )

    if observed != expected:
        raise PlannerInvariantError(
            f"{table}: archive {archive_id} holds digest {observed!r} while "
            f"revision {revision_id} records {field} {expected!r}; a seed "
            "basis here would assert a provenance the data contradicts"
        )


def _single(archive_id: int, revision_id: int | None, basis: str) -> tuple:
    return (SideAttribution("", archive_id, revision_id, basis),)


def _resolve(inputs: "_Inputs", archive_id: int, table: str,
             bound_basis: str) -> tuple[int | None, str]:
    """Which revision an archive's evidence binds to, and on what basis.

    Provisional archives are the case this exists for. Their revision carries
    no digest, so binding to it would assert an identity the database says is
    unknown; slice 1 §7.3 requires the evidence to stay unresolved instead.
    `revision_by_archive` holds established revisions only, so the miss is
    what produces `unresolved_no_identity` rather than a special case here.
    """
    revision_id = inputs.revision_by_archive.get(archive_id)

    if revision_id is not None:
        return revision_id, bound_basis

    if UNRESOLVED_NO_IDENTITY not in TABLE_VOCABULARY[table]:
        # `archive_hashes` has no unresolved state: the hasher computes a
        # digest and binds in the same transaction, so a hash row under a
        # digestless revision is not a row to classify conservatively -- it is
        # a state that should not exist, and normalising it would hide that.
        raise PlannerInvariantError(
            f"{table}: archive {archive_id} has evidence but no established "
            "revision, which this table has no unresolved basis to express"
        )

    return None, UNRESOLVED_NO_IDENTITY


def _validate_page_population(inputs: "_Inputs", archive_id: int,
                              signature_pages: int, signature_location: int | None):
    """Refuse a page population the plan cannot honestly describe.

    Every check here is a disagreement between two things production is
    measured to agree on (slice 2 §4.4), so any of them firing means the
    database is not in the shape the design was drawn against -- which is a
    reason to stop planning, not to pick one side and continue.
    """
    child = inputs.page_archives.get(archive_id)

    if child is None:
        if signature_pages != 0:
            raise PlannerInvariantError(
                f"archive {archive_id}: signature claims {signature_pages} "
                "page(s) but no page rows exist; a zero-page inventory here "
                "would record a result the signature contradicts"
            )
        return 0, None

    (count, extracted_at, location_id, distinct_location_ids,
     null_location_pages, min_index, max_index) = child

    if count != signature_pages:
        raise PlannerInvariantError(
            f"archive {archive_id}: signature claims {signature_pages} page(s), "
            f"{count} page row(s) exist"
        )

    if min_index != 0 or max_index != count - 1:
        raise PlannerInvariantError(
            f"archive {archive_id}: page indexes are not a dense 0..{count - 1} "
            f"run (min {min_index}, max {max_index})"
        )

    # NULL is counted as its own location rather than folded in with a
    # sentinel. `count(DISTINCT ifnull(location_id, -1))` cannot tell an
    # unknown location from a real location id of -1, so a set holding one of
    # each counted as a single location and was accepted.
    distinct_locations = distinct_location_ids + (1 if null_location_pages else 0)

    if distinct_locations != 1:
        raise PlannerInvariantError(
            f"archive {archive_id}: page rows span {distinct_locations} "
            f"location(s) -- {distinct_location_ids} distinct id(s) and "
            f"{null_location_pages} row(s) with none; the inventory carries one"
        )

    if location_id != signature_location:
        raise PlannerInvariantError(
            f"archive {archive_id}: page rows are at location {location_id}, "
            f"the signature at {signature_location}"
        )

    return count, extracted_at


def _classify(inputs: "_Inputs") -> list[PlannedBinding]:
    """Turn the snapshot into one planned binding per receiving row.

    Nothing here measures or re-reads anything, which is why `measured` and
    `stat_matched_revision` cannot appear: both describe a producer that read
    bytes, and the backfill reads only the database (slice 1 §7.1).
    """
    bindings: list[PlannedBinding] = []

    # --- archive_hashes: the identity seed, and the only table that has one.
    for row_id, archive_id, digest in inputs.hashes:
        revision_id, basis = _resolve(
            inputs, archive_id, "archive_hashes", IDENTITY_SEED
        )

        if basis == IDENTITY_SEED:
            _validate_seed(
                inputs, table="archive_hashes", archive_id=archive_id,
                revision_id=revision_id, observed=digest, field="archive_sha256",
            )

        bindings.append(
            PlannedBinding(
                table="archive_hashes",
                key=row_id,
                key_kind="row_id",
                archive_id=archive_id,
                sides=_single(archive_id, revision_id, basis),
            )
        )

    # --- archive_content_signatures: field seeds, except where the signature
    # describes a byte generation no revision holds.
    for row_id, archive_id, _pages, digest, _loc, _calc in inputs.signatures:
        if archive_id in inputs.drift_archives:
            revision_id, basis = None, UNRESOLVED_DRIFT
        else:
            revision_id, basis = _resolve(
                inputs, archive_id, "archive_content_signatures", FIELD_SEED
            )

            # The same defect class as the identity seed, checked the same
            # way: a field seed claims the revision's `content_signature` came
            # from this row's digest, so a disagreement makes the basis false.
            if basis == FIELD_SEED:
                _validate_seed(
                    inputs, table="archive_content_signatures",
                    archive_id=archive_id, revision_id=revision_id,
                    observed=digest, field="content_signature",
                )

        bindings.append(
            PlannedBinding(
                table="archive_content_signatures",
                key=row_id,
                key_kind="row_id",
                archive_id=archive_id,
                sides=_single(archive_id, revision_id, basis),
            )
        )

    # --- archive_inspections: inherited, and explicitly version-unknown.
    # `inspector_version` is planned as NULL rather than left unstated: the
    # migration writes both columns, so both belong in the plan and in the
    # binding digest that reconciles against it (slice 1 §6.5).
    for row_id, archive_id in inputs.inspections:
        revision_id, basis = _resolve(
            inputs, archive_id, "archive_inspections", SINGLE_REVISION_INHERITED
        )
        bindings.append(
            PlannedBinding(
                table="archive_inspections",
                key=row_id,
                key_kind="row_id",
                archive_id=archive_id,
                sides=_single(archive_id, revision_id, basis),
                values={
                    "inspector_version": None,
                    "inspector_version_basis": "unknown_legacy",
                },
            )
        )

    # --- page_inventory: one row per extraction result, planned by
    # archive_id because the row does not exist yet (slice 2 §10.1). The
    # SIGNATURE is the authority throughout -- for which archives have a
    # result, for the page count, and for the location.
    for archive_id in sorted(inputs.signature_by_archive):
        sig_pages, sig_location, calculated_at = inputs.signature_by_archive[archive_id]
        page_count, child_extracted_at = _validate_page_population(
            inputs, archive_id, sig_pages, sig_location
        )

        if child_extracted_at is None:
            extracted_at = calculated_at
            extracted_at_basis = SIGNATURE_CALCULATED_AT
        else:
            extracted_at = child_extracted_at
            extracted_at_basis = FIRST_PAGE_PERSISTENCE

        if archive_id in inputs.drift_archives:
            revision_id, basis = None, UNRESOLVED_DRIFT
        else:
            revision_id, basis = _resolve(
                inputs, archive_id, "page_inventory", SINGLE_REVISION_INHERITED
            )

        bindings.append(
            PlannedBinding(
                table="page_inventory",
                key=archive_id,
                key_kind="archive_id",
                archive_id=archive_id,
                sides=_single(archive_id, revision_id, basis),
                values={
                    "page_count": page_count,
                    "content_digest": _signature_digest(inputs, archive_id),
                    # The signature's location, not the children's: slice 2
                    # §10.1 makes the signature the planned authority, and a
                    # zero-page result has no child to take one from at all.
                    "location_id": sig_location,
                    "extracted_at": extracted_at,
                    "extracted_at_basis": extracted_at_basis,
                },
            )
        )

    # --- near_duplicate_candidates: two sides, bound independently. The 3,000
    # backfilled rows were bound by the one-revision-per-archive census rather
    # than from page evidence, so a bound side takes
    # `single_revision_inherited` and not the producer's
    # `inherited_from_page_evidence` (slice 1 §7.4).
    for row_id, archive_a, archive_b in inputs.candidates:
        sides = []

        for label, side_archive in (("a", archive_a), ("b", archive_b)):
            revision_id, basis = _resolve(
                inputs, side_archive, "near_duplicate_candidates",
                SINGLE_REVISION_INHERITED,
            )
            sides.append(
                SideAttribution(label, side_archive, revision_id, basis)
            )

        bindings.append(
            PlannedBinding(
                table="near_duplicate_candidates",
                key=row_id,
                key_kind="row_id",
                archive_id=archive_a,
                sides=tuple(sides),
                values={
                    "archive_b_id": archive_b,
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
    lines = [
        _canonical_json(
            {
                "k": "version",
                "snapshot": SNAPSHOT_DIGEST_VERSION,
                "planner": PLANNER_VERSION,
            }
        ),
        _canonical_json(
            {"k": "count", "section": "revisions", "n": len(inputs.revisions)}
        ),
    ]

    for revision_id in sorted(inputs.revisions):
        archive_id, state, sha256_digest, content_signature = inputs.revisions[
            revision_id
        ]
        lines.append(
            _canonical_json(
                {
                    "k": "revision",
                    "id": revision_id,
                    "archive_id": archive_id,
                    "identity_state": state,
                    # Both seed columns, because both are now what a seed
                    # basis is validated against. Omitting them let the state
                    # a plan was drawn from change without the digest moving.
                    "archive_sha256": sha256_digest,
                    "content_signature": content_signature,
                }
            )
        )

    lines.append(
        _canonical_json({"k": "count", "section": "hashes", "n": len(inputs.hashes)})
    )
    for row_id, archive_id, digest in inputs.hashes:
        lines.append(
            _canonical_json(
                {
                    "k": "hash",
                    "id": row_id,
                    "archive_id": archive_id,
                    # The value, not only the row's existence. Hashing ids
                    # alone meant editing a digest changed neither this digest
                    # nor the plan's.
                    #
                    # This overlaps with `_validate_seed`, and the overlap is
                    # deliberate. A bypass run on 2026-08-27 removed this
                    # field and no test failed: every hash row either
                    # validates against its revision or belongs to an archive
                    # with no established revision, which `_resolve` refuses,
                    # so no reachable state has a hash digest the seed check
                    # would not already have caught. It stays because the
                    # snapshot digest's contract is to be an identity for the
                    # state read, not a summary of whatever the classifier
                    # happened to look at -- and because a later slice that
                    # relaxes the seed check would otherwise silently reopen
                    # the original defect. It is pinned directly by
                    # test_the_snapshot_digest_covers_the_hash_digest, which
                    # renders the snapshot without going through
                    # classification.
                    "digest": digest,
                }
            )
        )

    lines.append(
        _canonical_json(
            {"k": "count", "section": "inspections", "n": len(inputs.inspections)}
        )
    )
    for row_id, archive_id in inputs.inspections:
        lines.append(
            _canonical_json(
                {"k": "inspection", "id": row_id, "archive_id": archive_id}
            )
        )

    lines.append(
        _canonical_json(
            {"k": "count", "section": "signatures", "n": len(inputs.signatures)}
        )
    )
    for row_id, archive_id, pages, digest, location, calculated in inputs.signatures:
        lines.append(
            _canonical_json(
                {
                    "k": "signature",
                    "id": row_id,
                    "archive_id": archive_id,
                    "pages": pages,
                    "digest": digest,
                    "location": location,
                    "calculated_at": calculated,
                }
            )
        )

    lines.append(
        _canonical_json(
            {"k": "count", "section": "page_archives", "n": len(inputs.page_archives)}
        )
    )
    for archive_id in sorted(inputs.page_archives):
        (pages, extracted_at, location, distinct_location_ids,
         null_location_pages, min_index, max_index) = inputs.page_archives[archive_id]
        lines.append(
            _canonical_json(
                {
                    "k": "page_archive",
                    "archive_id": archive_id,
                    "pages": pages,
                    "extracted_at": extracted_at,
                    "location": location,
                    "distinct_location_ids": distinct_location_ids,
                    "null_location_pages": null_location_pages,
                    "min_index": min_index,
                    "max_index": max_index,
                }
            )
        )

    lines.append(
        _canonical_json(
            {"k": "count", "section": "candidates", "n": len(inputs.candidates)}
        )
    )
    for row_id, archive_a, archive_b in inputs.candidates:
        lines.append(
            _canonical_json({"k": "candidate", "id": row_id, "a": archive_a,
                             "b": archive_b})
        )

    for section, values in (
        ("drift", sorted(inputs.drift_archives)),
        ("provisional", list(inputs.provisional_archives)),
        ("archive_without_revision", list(inputs.archives_without_revision)),
    ):
        lines.append(
            _canonical_json({"k": "count", "section": section, "n": len(values)})
        )
        for archive_id in values:
            lines.append(
                _canonical_json({"k": section, "archive_id": archive_id})
            )

    lines.append(
        _canonical_json({"k": "quarantine", "rows": inputs.quarantine_rows})
    )
    return lines


PLAN_DIGEST_VERSION = "provenance-backfill-plan/2"


def canonical_plan_lines(bindings, snapshot_digest: str) -> list[str]:
    """Every planned binding, canonically rendered.

    The snapshot digest identifies the STATE a plan was drawn from; it says
    nothing about the plan. An earlier revision shipped only that, and
    `canonical_line` was written and never called -- so a CSV row's revision,
    basis, page count, digest or timestamp could be altered while the artifact
    still carried the digest that appeared to approve it. Verified: the
    envelope held no bindings and the CSV merely repeated the input digest.

    This hashes the decisions. Migration 015 recomputes it from the artifact
    it is about to apply and refuses if it differs, which is what makes the
    approved plan and the applied plan the same object rather than two things
    that share a filename.
    """
    lines = [
        PLAN_DIGEST_VERSION,
        "planner_version=" + PLANNER_VERSION,
        "snapshot_digest=" + snapshot_digest,
        "target_states|count=%d" % len(TARGET_STATES),
    ]
    lines.extend("target_state|%s" % state for state in TARGET_STATES)
    lines.append("bindings|count=%d" % len(bindings))
    lines.extend(binding.canonical_line() for binding in bindings)
    return lines


def compute_plan_digest(bindings, snapshot_digest: str) -> str:
    """SHA-256 over `canonical_plan_lines`, lowercase hex."""
    payload = "\n".join(canonical_plan_lines(bindings, snapshot_digest)) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
    planned_sides = 0

    for binding in bindings:
        bucket = per_table.get(binding.table)

        if bucket is None:
            raise PlannerInvariantError(
                f"binding names {binding.table!r}, which is not a receiving table"
            )

        unknown = [
            side.provenance_basis for side in binding.sides
            if side.provenance_basis not in per_basis
        ]

        if unknown:
            # Reachable only if a binding was built around __post_init__ --
            # which the reconciliation tests do deliberately. A KeyError here
            # would be a crash rather than a refusal, and the whole point of
            # this function is that it refuses rather than returning totals it
            # cannot stand behind.
            raise PlannerInvariantError(
                f"{binding.table} row {binding.key}: unknown basis "
                f"{unknown[0]!r} reached the totals"
            )

        bucket["rows"] += 1
        bucket["bound" if binding.bound else "unresolved"] += 1

        # Bases are counted per SIDE, because a pairwise row can carry two
        # different ones. Rows and sides are therefore different totals and
        # are reconciled separately rather than being made to agree.
        for side in binding.sides:
            planned_sides += 1
            bucket.setdefault(side.provenance_basis, 0)
            bucket[side.provenance_basis] += 1
            per_basis[side.provenance_basis] += 1

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
    recount_basis = Counter(
        side.provenance_basis for binding in bindings for side in binding.sides
    )
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
        "planned_sides": planned_sides,
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
    plan_digest: str
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

    def as_dict(self, *, csv_sha256: str | None = None) -> dict[str, Any]:
        """The plan envelope. Per-row bindings travel in the CSV.

        The split is deliberate: 238,956 rows of JSON is an artifact nobody
        reads, while the envelope is the part a reviewer actually checks. Both
        carry the `plan_digest`, which is computed over the bindings
        themselves -- so a CSV whose rows were altered no longer matches the
        envelope that approved it, which the snapshot digest alone could not
        detect.
        """
        return {
            "planner_version": self.planner_version,
            "execution_status": EXECUTION_STATUS,
            # The envelope is written last and names the bindings file it was
            # written with, so its presence attests that the CSV beside it
            # completed and this digest proves the two are the same pair
            # rather than two files that share a directory. `None` when this
            # call wrote no CSV -- there is then nothing to attest.
            "artifacts": {"csv_sha256": csv_sha256},
            "snapshot_digest": self.snapshot_digest,
            "plan_digest": self.plan_digest,
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

    snapshot_digest = compute_snapshot_digest(inputs)

    return BackfillPlan(
        planner_version=PLANNER_VERSION,
        snapshot_digest=snapshot_digest,
        plan_digest=compute_plan_digest(bindings, snapshot_digest),
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

# Artifacts are written under this suffix and renamed into place, so no reader
# ever sees a partially written plan under a name it would trust.
STAGING_SUFFIX = ".partial"


def _staging_path(target: Path) -> Path:
    """Where an artifact is written before it is committed to its real name."""
    return target.with_name(target.name + STAGING_SUFFIX)


def _check_output_path(path: Path, database: Path | None) -> None:
    """Refuse an output path before anything is written to it.

    Every check is made against the RESOLVED target rather than the typed
    name, and that distinction is the whole of it. `Path.parent` on
    `allowed/link.json` answers `allowed/` however the link points, so a
    lexical parent check accepted a symlink in an allowed directory whose
    target was a nonexistent file beside the database -- and the write then
    created that file exactly where the rule forbids one.

    The defect was the ORDER, not the resolver. Measured on 2026-08-27,
    Python 3.11.3 / win32: `Path(link).resolve(strict=False)` on a dangling
    link returns the link's TARGET, so resolving the path itself would have
    been sound -- what failed was taking `.parent` first and resolving that,
    which answers a question about the name rather than about the write. An
    earlier revision of this comment claimed `resolve(strict=False)` returned
    the link itself; that was false and is corrected here rather than left to
    be read as current. `output_guards.resolved_parent` resolves first and
    takes the directory second, which is the order that matters.

    Order matters. The collision checks run before the existence check so an
    operator who names the database is told that it is the database, rather
    than that a file already exists there.
    """
    if database is not None:
        for protected, description in output_guards.protected_database_paths(database):
            if output_guards.same_file(path, protected):
                raise OutputPathError(
                    f"refusing to write the plan over {description} "
                    f"({os.path.realpath(protected)}): the database is read "
                    "through a read-only connection, but that guarantee stops "
                    "at the connection and this write would truncate the file "
                    "the plan describes"
                )

        if output_guards.resolved_parent(path) == output_guards.resolved_parent(database):
            # The guarded-operation rules keep reports away from the database
            # directory, so a read-only planner can never be the thing that
            # put a file next to production.
            raise OutputPathError(
                "refusing to write beside the database: "
                f"{os.path.dirname(os.path.realpath(path))}"
            )

    # lexists, not exists: a dangling symlink is not a file, so `exists()`
    # says False and the write would then create its target. Refusing the
    # NAME refuses that.
    if os.path.lexists(path):
        raise OutputPathError(f"refusing to overwrite an existing file: {path}")

    parent = Path(os.path.realpath(path)).parent

    if not parent.is_dir():
        raise OutputPathError(f"output directory does not exist: {parent}")


def preflight_output_paths(json_path=None, csv_path=None, *, database=None):
    """Check every path BEFORE writing any of them.

    An earlier revision checked each path as it wrote it, so a valid JSON path
    and an invalid CSV path produced a written envelope, a failure, and no
    bindings. Every path is now checked first -- each artifact's staging path
    included, so a stale `.partial` from an earlier failed run is refused
    rather than silently overwritten, because this planner cannot know what
    put it there -- and all four names are compared against each other.

    Preflight is necessary and not sufficient: it sees only the failures that
    are already visible. `write_plan_artifacts` handles the ones that are not.
    """
    targets = {}

    for label, value in (("json", json_path), ("csv", csv_path)):
        if value is None:
            continue
        targets[label] = Path(value)

    # Every name this call may create, compared pairwise -- both finals AND
    # both staging paths. Comparing only the two finals was not enough, and
    # the gap was not theoretical: with --json plan and --csv plan.partial,
    # the JSON's staging path IS the CSV's final path. Staging wrote the
    # envelope to `plan.partial`, committing the CSV renamed its own staging
    # file over it, and committing the envelope then renamed the CSV's bytes
    # to `plan`. The call reported success with both artifacts named, while
    # on disk the CSV was gone and the envelope held CSV content.
    #
    # same_file rather than comparing resolved strings: two of these can be
    # hard links to one file, which resolve differently and share an inode.
    claimed: list[tuple[str, Path]] = []

    for label, path in targets.items():
        claimed.append((f"--{label}", path))
        claimed.append((f"--{label}'s staging file", _staging_path(path)))

    for index, (left_role, left) in enumerate(claimed):
        for right_role, right in claimed[index + 1:]:
            if output_guards.same_file(left, right):
                raise OutputPathError(
                    f"{left_role} ({left}) and {right_role} ({right}) are the "
                    "same file; every artifact and staging name this call "
                    "creates must be distinct, or one write silently "
                    "destroys another"
                )

    database_path = Path(database) if database is not None else None

    for path in targets.values():
        _check_output_path(path, database_path)
        _check_output_path(_staging_path(path), database_path)

    return targets


def _create_and_write(path: Path, payload: str, created: list[Path]) -> Path:
    """Create a staging file exclusively, record it, then fill it.

    The order is the whole correction. An earlier version wrote through
    `Path.write_text` and added the path to the cleanup set only after the
    write RETURNED, so a write that created the file and then failed -- a
    full disk is the ordinary way for that to happen -- left a file no
    cleanup knew about. The path is registered here the moment creation
    succeeds and before a single byte is written.

    `O_EXCL` rather than a plain open, because it is what makes the cleanup
    set honest: creation is the act that claims the name, so every file
    `_discard` later removes is one this call is known to have created. A
    plain open would let cleanup delete a file some other process created in
    the window between the preflight check and this write.

    `O_BINARY` where the platform has it, with the newline translation left
    to the text wrapper, so the bytes on disk are exactly the payload.
    """
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)

    try:
        descriptor = os.open(path, flags)
    except FileExistsError as error:
        raise OutputPathError(
            f"refusing to overwrite an existing staging file: {path}"
        ) from error
    except OSError as error:
        raise OutputPathError(f"could not create {path}: {error}") from error

    # Ours from here: whatever happens next, cleanup may remove it.
    created.append(path)

    try:
        stream = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
    except BaseException:
        # fdopen did not take ownership, so the descriptor is still this
        # function's to close.
        os.close(descriptor)
        raise

    try:
        with stream:
            stream.write(payload)
    except OSError as error:
        raise OutputPathError(f"could not write {path}: {error}") from error

    return path


def _discard(paths: Iterable[Path]) -> list[Path]:
    """Remove files a failed write created, and report what survived.

    Best effort by necessity -- a filesystem refusing writes may refuse
    unlinks too -- so each removal is attempted independently. What it must
    not do is fail silently: a surviving file would contradict the
    both-or-neither guarantee, so the paths that could not be removed are
    returned and the caller names them in the error rather than leaving
    residue nobody was told about.
    """
    survived: list[Path] = []

    for path in paths:
        try:
            os.unlink(path)
        except FileNotFoundError:
            continue
        except OSError:
            survived.append(path)

    return survived


def render_plan_csv(plan: "BackfillPlan") -> str:
    """One row per planned binding, in the plan's canonical order.

    `extrasaction="raise"`: a binding carrying a field the column list does
    not know about is a defect to surface, not a field to drop. The previous
    "ignore" silently discarded `archive_b_id` and `revision_b_id` from every
    candidate row.
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="raise",
                            restval="")
    writer.writeheader()

    for binding in plan.bindings:
        writer.writerow(
            binding.as_dict(
                planner_version=plan.planner_version,
                snapshot_digest=plan.snapshot_digest,
                plan_digest=plan.plan_digest,
            )
        )

    return buffer.getvalue()


def render_plan_json(plan: "BackfillPlan", *, csv_sha256: str | None = None) -> str:
    """The plan envelope. Deterministic: sorted keys, fixed indentation."""
    return json.dumps(
        plan.as_dict(csv_sha256=csv_sha256), indent=2, sort_keys=True
    ) + "\n"


def write_plan_artifacts(plan: "BackfillPlan", *, json_path=None, csv_path=None,
                         database=None) -> dict:
    """Write both artifacts, or roll back to neither.

    The contract stated exactly, because "both artifacts, or neither"
    promised more than this code can deliver and the analysis further down
    already contradicted it:

    * a **recoverable** failure -- a refused path, a failed write, a failed
      rename -- is rolled back, leaving neither artifact. Anything that
      cannot be removed is named in the error rather than left silently.
    * an **interruption the process does not survive** -- a power loss, a
      kill -- can leave the CSV committed with no envelope beside it. That
      is the state the commit order is chosen to produce: bindings with no
      envelope read as incomplete, where an envelope with no bindings would
      read as a finished plan.

    Preflight alone delivered neither of those, and saying it did was the
    original defect. Preflight refuses paths that are *already* unusable; it
    cannot see the case where the first write succeeds and the second fails,
    which left an envelope on disk with no bindings beside it.

    So the writes are staged and then committed:

    1. both payloads are rendered in memory, so a rendering failure happens
       before the filesystem is touched at all;
    2. each is written to a sibling `.partial` file;
    3. the CSV is renamed into place first, the envelope second.

    The envelope is the commit marker. It is renamed last and it carries the
    CSV's SHA-256, so its presence attests that the bindings finished writing
    and `artifacts.csv_sha256` proves they are the bindings this envelope
    approved -- something migration 015 can verify rather than infer from two
    files sharing a directory.

    If any step fails, every file this call created is removed -- one already
    renamed into place included -- so a recoverable failure leaves neither
    artifact. Staging files are created with `O_EXCL`, so every name the
    rollback removes is one this call is known to have created rather than
    one that merely has the expected shape.
    """
    targets = preflight_output_paths(
        json_path=json_path, csv_path=csv_path, database=database
    )
    payloads: dict[str, str] = {}

    if "csv" in targets:
        payloads["csv"] = render_plan_csv(plan)

    if "json" in targets:
        payloads["json"] = render_plan_json(
            plan,
            csv_sha256=(
                hashlib.sha256(payloads["csv"].encode("utf-8")).hexdigest()
                if "csv" in payloads
                else None
            ),
        )

    staged: dict[str, Path] = {}
    committed: dict[str, Path] = {}
    # Every name this call has created and not yet renamed away. Maintained
    # as the writes happen rather than reconstructed afterwards, so a failure
    # at any point can remove exactly what exists.
    created: list[Path] = []

    # BaseException, not Exception: a KeyboardInterrupt between the two
    # renames would otherwise leave exactly the half-pair this exists to
    # prevent.
    try:
        for label in ("csv", "json"):
            if label in payloads:
                staged[label] = _create_and_write(
                    _staging_path(targets[label]), payloads[label], created
                )

        # Commit order: bindings first, envelope last, so the envelope's
        # existence is never a promise about a CSV that is not there yet.
        #
        # What this can and cannot promise is worth stating exactly. Against
        # an error the process survives, the rollback below is what delivers
        # "neither", and a bypass run on 2026-08-27 reversing this order
        # failed no test for precisely that reason -- the cleanup masks it.
        # The order earns its place against what cleanup cannot reach: a
        # power loss or a kill between the two renames, where the surviving
        # state is a CSV with no envelope (recognisably incomplete) rather
        # than an envelope with no bindings (indistinguishable from a plan).
        # That is not reproducible in a test, so the sequence itself is
        # pinned by test_the_bindings_are_committed_before_the_envelope and
        # the reasoning is recorded here rather than only in a review.
        for label in ("csv", "json"):
            if label in staged:
                try:
                    os.replace(staged[label], targets[label])
                except OSError as error:
                    raise OutputPathError(
                        f"could not commit {targets[label]}: {error}"
                    ) from error

                # The staging name no longer exists and the final one now
                # does, so the cleanup set follows the rename rather than
                # holding a name that is gone.
                created.remove(staged[label])
                created.append(targets[label])
                committed[label] = targets[label]
    except BaseException as error:
        residue = _discard(created)

        if residue:
            raise OutputPathError(
                f"{error}; and these files could not be removed afterwards, so "
                "the plan is left incomplete on disk rather than absent: "
                + ", ".join(str(path) for path in residue)
            ) from error

        raise

    return committed


def write_plan_json(plan: "BackfillPlan", path, *, database=None) -> Path:
    """Write the plan envelope alone.

    Goes through the same staged commit as the pair, so an interrupted write
    leaves no envelope rather than a truncated one. `artifacts.csv_sha256` is
    null: this call wrote no bindings, so the envelope attests to none.
    """
    return write_plan_artifacts(plan, json_path=path, database=database)["json"]


def write_plan_csv(plan: "BackfillPlan", path, *, database=None) -> Path:
    """Write the per-row bindings alone, through the same staged commit."""
    return write_plan_artifacts(plan, csv_path=path, database=database)["csv"]
