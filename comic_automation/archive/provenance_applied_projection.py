"""The slice-4 applied projection: one rendering, applied to both sides.

Design section 12.2. Migration 015's reconciliation compares the plan an
operator approved against the rows 015 actually wrote, and the comparison
is only meaningful if both sides are reduced to the same shape by the same
code. This module is that shape and that code.

Why `PlannedBinding` cannot be the comparison format
----------------------------------------------------

It was the obvious answer and it does not work, measured:

```text
PlannedBinding(table="near_duplicate_candidates", ..., values={})
    PlannerInvariantError: planned values do not match the table's artifact
    columns (missing ['archive_b_id', 'parameters_basis'])

ARTIFACT_COLUMNS["near_duplicate_candidates"] == ('archive_b_id',
                                                  'parameters_basis')
```

`parameters_basis` is required by the planner's own invariant and is
deliberately **not written by slice 4** -- it lands in slice 6 with the
fields that give it meaning. So a candidate binding reconstructed from
post-015 state cannot be a `PlannedBinding` at all: the column does not
exist in the database to read. Hence a projection defined once and applied
to both sides, rather than two formats compared.

Its own version marker, deliberately
------------------------------------

`APPLIED_PROJECTION_VERSION` is `"provenance-backfill-applied/1"` and is
**not** `PLAN_DIGEST_VERSION`. A projection that borrowed the planner's
marker would keep comparing equal across a change to either definition,
which is the exact failure the planner's own marker exists to prevent.

One projector applied twice is not a cross-check
------------------------------------------------

Stated because it is easy to believe otherwise. Projecting both sides
through this module gives a comparison of like with like; it gives **no**
protection against a shared omission, because one projector applied twice
drops the same field from both sides and the two compare equal. Two things
protect against that, and neither is this function:

* `PROJECTION_VALUE_FIELDS` and `PROJECTION_BINDING_FIELDS` below are
  written out as data, transcribed from section 12.2, and asserted by tests
  against that list -- **not** derived from the projector, which would make
  the assertion tautological;
* the fault-injection tests, which alter one field at a time and require
  the mismatch to be reported.

The projector renders; it does not judge evidence
-------------------------------------------------

It validates the *shape* it is handed -- table, key kind, side labels,
field names, scalar types -- because a shape error means the caller
constructed the wrong object and no comparison it produced would mean
anything.

It deliberately does **not** validate values: not the basis vocabulary, not
the bound-basis-implies-a-revision pairing that `PlannedBinding` enforces.
Those are the disagreements reconciliation exists to *report*, and a
projector that raised on them would convert a reportable finding into an
aborted transaction carrying no diagnosis -- an operator would learn that
015 failed but not which row, field or value moved. A projector that raises
where it should have rendered is a projector that hides the answer.

`page_inventory` and `parameters_basis` are absent by construction
-----------------------------------------------------------------

`page_inventory` is slice 4p's, and its bindings are excluded at selection
(`select_slice4_bindings`) and **counted** as deliberately unapplied rather
than silently dropped. `parameters_basis` is slice 6's and appears in no
value registry here. Both absences are asserted by tests rather than
assumed: a projection that grew either would mean slice 4 had started
writing another slice's column.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from comic_automation.archive.provenance_backfill_planner import (
    PAIRWISE_TABLES,
    PlannedBinding,
    SideAttribution,
    # Private to the planner and imported anyway, on purpose. The
    # alternative is a second copy of the canonicalisation, and two copies
    # drift -- at which point this module would compare an approved plan
    # against rebuilt rows using a rule the plan was never written under.
    # The coupling is the correctness property, so it is stated rather than
    # worked around.
    _canonical_json,
)


# The projection's own marker. Never PLAN_DIGEST_VERSION -- see the module
# docstring.
APPLIED_PROJECTION_VERSION = "provenance-backfill-applied/1"


# The four tables slice 4 rebuilds. `page_inventory` is absent by
# construction: it is slice 4p's table, its bindings carry key_kind
# "archive_id", and no row of it exists for 015 to write.
PROJECTION_TABLES: tuple[str, ...] = (
    "archive_hashes",
    "archive_content_signatures",
    "archive_inspections",
    "near_duplicate_candidates",
)


# Slice 4 writes row-keyed bindings only. The natural-key form belongs to
# 4p, so a projection carrying one is a slice boundary being crossed rather
# than an unusual input.
PROJECTION_KEY_KIND = "row_id"


# --- the independent field registry --------------------------------------
#
# Transcribed from design section 12.2, by hand, and NOT derived from
# `project_planned_binding()` or from `ARTIFACT_COLUMNS`. Deriving it would
# make every test asserting "the projection carries exactly these fields"
# compare the projector against itself, which passes unconditionally and
# catches nothing. The cost is that this list must be updated by hand when
# section 12.2 changes; that cost is the point.

PROJECTION_BINDING_FIELDS: tuple[str, ...] = (
    "table",
    "key_kind",
    "key",
    "archive_id",
    "sides",
    "values",
)

PROJECTION_SIDE_FIELDS: tuple[str, ...] = (
    "label",
    "archive_id",
    "source_revision_id",
    "provenance_basis",
)

# Exactly the columns migration 015 writes, per table. Note what is NOT
# here: `parameters_basis` (slice 6), every measurement column (015
# preserves them, and section 5.8 verifies that separately -- they are not
# attribution), and `location_id` (source_context, R9).
PROJECTION_VALUE_FIELDS: Mapping[str, tuple[str, ...]] = {
    "archive_hashes": (),
    "archive_content_signatures": (),
    "archive_inspections": ("inspector_version", "inspector_version_basis"),
    "near_duplicate_candidates": ("archive_b_id",),
}


# Side labels per table, ordered. A pairwise table binds each side
# independently, so its two sides are distinguished by label and never by
# position -- see `sides_by_label()`.
PROJECTION_SIDE_LABELS: Mapping[str, tuple[str, ...]] = {
    table: ("a", "b") if table in PAIRWISE_TABLES else ("",)
    for table in PROJECTION_TABLES
}


class ProjectionError(RuntimeError):
    """A binding cannot be projected, or was projected from the wrong slice.

    A shape refusal, never a value judgement: the projector raises when it
    has been handed the wrong kind of object, and renders faithfully when it
    has been handed the right kind carrying surprising data.
    """


@dataclass(frozen=True)
class AppliedBinding:
    """One row in the slice-4 applied projection's shape.

    Deliberately not `PlannedBinding`: that type requires
    `parameters_basis`, which slice 4 does not write and post-015 state
    therefore cannot supply (see the module docstring). Both sides of the
    reconciliation are reduced to this.
    """

    table: str
    key: int
    archive_id: int
    sides: tuple[SideAttribution, ...]
    values: Mapping[str, Any] = field(default_factory=dict)
    key_kind: str = PROJECTION_KEY_KIND

    def __post_init__(self) -> None:
        if self.table not in PROJECTION_TABLES:
            raise ProjectionError(
                f"{self.table!r} is not a slice-4 receiving table; the "
                f"projection covers exactly {list(PROJECTION_TABLES)}. "
                "page_inventory belongs to slice 4p and is excluded by "
                "construction."
            )

        if self.key_kind != PROJECTION_KEY_KIND:
            raise ProjectionError(
                f"{self.table} key_kind {self.key_kind!r}: slice 4 projects "
                f"{PROJECTION_KEY_KIND!r} only. The natural-key form is "
                "slice 4p's."
            )

        for name, value in (("key", self.key),
                            ("archive_id", self.archive_id)):
            _require_int(f"{self.table} {name}", value)

        expected_labels = PROJECTION_SIDE_LABELS[self.table]
        labels = tuple(side.label for side in self.sides)

        # One equality catches all three of the lead's cases at once: a
        # missing label shortens the tuple, a duplicate repeats an entry,
        # and an unexpected one substitutes a value. Order is part of it
        # because the projection's `sides` are specified as ordered by
        # label, so a reversed pair is a different rendering and must not
        # be silently accepted as equivalent.
        if labels != expected_labels:
            raise ProjectionError(
                f"{self.table} row {self.key}: side labels {list(labels)} "
                f"do not match the expected {list(expected_labels)}, in "
                "order"
            )

        for side in self.sides:
            _require_int(
                f"{self.table} row {self.key} side {side.label!r} archive_id",
                side.archive_id,
            )
            _require_optional_int(
                f"{self.table} row {self.key} side {side.label!r} "
                "source_revision_id",
                side.source_revision_id,
            )

            # A type check, not a vocabulary check. An unrecognised basis
            # string renders and is reported as a field mismatch; raising
            # here would abort the transaction without naming the row.
            if not isinstance(side.provenance_basis, str):
                raise ProjectionError(
                    f"{self.table} row {self.key} side {side.label!r}: "
                    f"provenance_basis is "
                    f"{type(side.provenance_basis).__name__}, not str"
                )

        expected_values = set(PROJECTION_VALUE_FIELDS[self.table])
        supplied = set(self.values)

        if supplied != expected_values:
            missing = sorted(expected_values - supplied)
            unexpected = sorted(supplied - expected_values)
            raise ProjectionError(
                f"{self.table} row {self.key}: projected values do not match "
                f"the registry (missing {missing}, unexpected {unexpected}). "
                "parameters_basis is a slice-6 column and is absent by "
                "design; its appearance here would mean slice 4 had started "
                "writing it."
            )

    def sides_by_label(self) -> Mapping[str, SideAttribution]:
        """The sides keyed by label, for callers binding table columns.

        Design section 12.2: `sides[label='a']` maps to `(revision_a_id,
        provenance_basis_a)` and `sides[label='b']` to `(revision_b_id,
        provenance_basis_b)`, **never by tuple position**. Offered as a
        mapping so a caller cannot reach for `sides[0]` and be right by
        accident on data that happens to be ordered.
        """
        return {side.label: side for side in self.sides}

    def projection_payload(self) -> dict[str, Any]:
        """This binding as the mapping the canonical line renders.

        Split out from `projection_line()` so a test can assert the field
        set directly against `PROJECTION_BINDING_FIELDS` without parsing
        JSON back out of a rendered line.
        """
        return {
            "table": self.table,
            "key_kind": self.key_kind,
            "key": self.key,
            "archive_id": self.archive_id,
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

    def projection_line(self) -> str:
        """One projected binding, canonically rendered.

        The line **is** the JSON object, with no surrounding delimiters to
        escape. `_canonical_json` is the planner's -- sorted keys, no
        inserted whitespace, delimiters escaped inside strings, nesting
        preserved so `sides` and `values` occupy their own scopes rather
        than flattening into one stream, and a missing value rendered
        `null` rather than `""`. Injective, which is the property that
        stops two distinct bindings rendering identically.

        Shared with the planner rather than reimplemented: two copies of a
        canonicalisation drift, and a drift here compares a plan against
        rows using a rule the plan was not written under.
        """
        return _canonical_json(self.projection_payload())


def _require_int(label: str, value: Any) -> None:
    """Reject a non-integer where the projection requires one.

    `bool` is excluded explicitly. It is a subclass of `int`, so a stray
    `True` would satisfy an `isinstance` check and then render as `true`
    rather than `1` -- a different document, a different digest, and a
    reconciliation that reports a mismatch whose cause is a type rather
    than a moved value.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProjectionError(
            f"{label} is {value!r} ({type(value).__name__}), not an integer"
        )


def _require_optional_int(label: str, value: Any) -> None:
    """As `_require_int`, but `None` is a legal value.

    A side with no revision is an ordinary unresolved side, and `None`
    renders as `null` -- distinct from any integer and from the empty
    string.
    """
    if value is None:
        return

    _require_int(label, value)


def project_planned_binding(binding: PlannedBinding) -> AppliedBinding:
    """Project one approved-plan binding into the comparison shape.

    Drops `parameters_basis` -- slice 6's column, which 015 does not write
    -- and keeps `archive_b_id`, which is existing candidate payload
    preserved through the rebuild. Including it is how the reconciliation
    verifies that preservation; it is not backfill attribution and is not
    staged as such.

    Refuses a `page_inventory` binding rather than silently dropping it.
    The by-construction exclusion belongs to `select_slice4_bindings()`,
    which counts what it excluded; one arriving here individually means a
    caller bypassed that selection, and returning `None` for it would let
    the count of deliberately-unapplied bindings quietly disagree with
    reality.
    """
    if binding.table not in PROJECTION_TABLES:
        raise ProjectionError(
            f"{binding.table} row {binding.key} is not a slice-4 binding. "
            "Use select_slice4_bindings(), which excludes it by "
            "construction and counts it as deliberately unapplied rather "
            "than dropping it."
        )

    values = {
        name: binding.values[name]
        for name in PROJECTION_VALUE_FIELDS[binding.table]
    }

    return AppliedBinding(
        table=binding.table,
        key=binding.key,
        key_kind=binding.key_kind,
        archive_id=binding.archive_id,
        sides=tuple(binding.sides),
        values=values,
    )


def select_slice4_bindings(
    bindings: Iterable[PlannedBinding],
) -> tuple[tuple[AppliedBinding, ...], tuple[PlannedBinding, ...]]:
    """Split an approved plan into what 015 applies and what it does not.

    Returns `(projected, excluded)`. Design section 12.2 step 1: a
    `page_inventory` binding is excluded because its table is not among the
    four and its `key_kind` is `archive_id` -- and it is **returned**, not
    discarded, so the postflight artifact can carry the deliberately
    unapplied count as a measured figure rather than an assumption.
    """
    projected: list[AppliedBinding] = []
    excluded: list[PlannedBinding] = []

    for binding in bindings:
        if binding.table in PROJECTION_TABLES:
            projected.append(project_planned_binding(binding))
        else:
            excluded.append(binding)

    return tuple(projected), tuple(excluded)


def sort_projection(
    bindings: Iterable[AppliedBinding],
) -> tuple[AppliedBinding, ...]:
    """Projected bindings in document order: sorted by `(table, key)`.

    Total, because the staging table's ``PRIMARY KEY (table_name, row_id)``
    makes the pair unique -- so the sort never has to break a tie and the
    document order is a property of the data rather than of the sort's
    stability.
    """
    return tuple(sorted(bindings, key=lambda b: (b.table, b.key)))


def projection_document(bindings: Iterable[AppliedBinding]) -> bytes:
    """The projected document, as bytes.

    Framing, exactly as design section 12.2 specifies it:

    ```text
    <version marker>
    bindings|count=<n>
    <one JSON object per binding, sorted by (table, key)>
    ```

    Joined with one LF byte (0x0A) between lines and one trailing LF byte,
    so no rendering can be a prefix of another.

    **Returns bytes, and builds them as bytes.** Not a `str` that a caller
    then encodes, and never written through a text-mode writer: on Windows
    that translates LF to CRLF and silently changes every digest this
    module produces. The separator is a byte value here for the same reason
    the design specifies it as one -- an escape sequence did not survive
    that document's own authoring and was emitted as a real line break,
    splitting the specification of the separator across the lines it was
    specifying.
    """
    ordered = sort_projection(bindings)

    lines = [
        APPLIED_PROJECTION_VERSION,
        "bindings|count=%d" % len(ordered),
    ]
    lines.extend(binding.projection_line() for binding in ordered)

    return b"\n".join(line.encode("utf-8") for line in lines) + b"\n"


def projection_digest(bindings: Iterable[AppliedBinding]) -> str:
    """SHA-256 over `projection_document`, lowercase hex."""
    return hashlib.sha256(projection_document(bindings)).hexdigest()


def table_projection_document(
    bindings: Iterable[AppliedBinding],
    table: str,
) -> bytes:
    """The same framing, restricted to one table's lines.

    Same version marker and same count line, so a per-table document is a
    document in its own right rather than a fragment of one. The count is
    that table's, which is what makes a per-table digest able to move while
    another table's stays still.
    """
    if table not in PROJECTION_TABLES:
        raise ProjectionError(
            f"{table!r} is not a slice-4 receiving table; the projection "
            f"covers exactly {list(PROJECTION_TABLES)}"
        )

    return projection_document(
        binding for binding in bindings if binding.table == table
    )


def table_projection_digest(
    bindings: Iterable[AppliedBinding],
    table: str,
) -> str:
    """SHA-256 over `table_projection_document`, lowercase hex."""
    return hashlib.sha256(
        table_projection_document(bindings, table)
    ).hexdigest()


def projection_digests(
    bindings: Sequence[AppliedBinding],
) -> dict[str, Any]:
    """Every digest the postflight artifact carries, in one reading.

    All four tables appear in `per_table`, including any that projected no
    rows: a table absent from the report is indistinguishable from a table
    whose digest was never computed, and the empty document still has a
    digest -- of the version marker and a zero count -- so "this table
    received nothing" is a statement an operator can bind as firmly as any
    other.
    """
    ordered = sort_projection(bindings)

    return {
        "projection_version": APPLIED_PROJECTION_VERSION,
        "count": len(ordered),
        "digest": projection_digest(ordered),
        "per_table": {
            table: {
                "count": sum(1 for b in ordered if b.table == table),
                "digest": table_projection_digest(ordered, table),
            }
            for table in PROJECTION_TABLES
        },
    }
