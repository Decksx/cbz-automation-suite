"""The slice-4 applied projection: its shape, its framing, its refusals.

Design section 12.2 defines a projection applied to both sides of the
migration-015 reconciliation. These tests pin three separable things:

* the **field set**, asserted against a list transcribed here by hand from
  section 12.2 rather than read out of the module, so the assertion is not
  the projector agreeing with itself;
* the **byte framing**, asserted on bytes -- an LF-joined document whose
  digest a text-mode write would silently change on Windows;
* the **refusals**, each exercised alone.

The fault-injection group at the end alters exactly one field per test and
requires the digest to move. That group is what stands between this
projection and the failure the module docstring names: one projector
applied to both sides drops the same field from both and compares equal.
"""

from __future__ import annotations

import pytest

from comic_automation.archive import provenance_backfill_planner as planner
from comic_automation.archive.provenance_applied_projection import (
    APPLIED_PROJECTION_VERSION,
    AppliedBinding,
    PROJECTION_BINDING_FIELDS,
    PROJECTION_SIDE_FIELDS,
    PROJECTION_TABLES,
    PROJECTION_VALUE_FIELDS,
    ProjectionError,
    project_planned_binding,
    projection_digest,
    projection_digests,
    projection_document,
    select_slice4_bindings,
    sort_projection,
    table_projection_digest,
)


# --- section 12.2, transcribed by hand -----------------------------------
#
# Read off the design document, NOT imported from the module under test.
# Importing it would make every assertion below `x == x`. If section 12.2
# changes, both this block and the module must be edited, and that is the
# intended cost.

SPEC_VERSION = "provenance-backfill-applied/1"

SPEC_BINDING_FIELDS = {
    "table", "key_kind", "key", "archive_id", "sides", "values",
}

SPEC_SIDE_FIELDS = {
    "label", "archive_id", "source_revision_id", "provenance_basis",
}

SPEC_VALUE_FIELDS = {
    "archive_hashes": set(),
    "archive_content_signatures": set(),
    "archive_inspections": {"inspector_version", "inspector_version_basis"},
    "near_duplicate_candidates": {"archive_b_id"},
}

SPEC_TABLES = {
    "archive_hashes",
    "archive_content_signatures",
    "archive_inspections",
    "near_duplicate_candidates",
}


# --- fixtures -------------------------------------------------------------


def _side(label, archive_id, revision_id, basis):
    return planner.SideAttribution(
        label=label,
        archive_id=archive_id,
        source_revision_id=revision_id,
        provenance_basis=basis,
    )


def _hashes(key=1, archive_id=10, revision_id=7):
    return AppliedBinding(
        table="archive_hashes",
        key=key,
        archive_id=archive_id,
        sides=(_side("", archive_id, revision_id, planner.IDENTITY_SEED),),
        values={},
    )


def _inspection(key=3, archive_id=10):
    return AppliedBinding(
        table="archive_inspections",
        key=key,
        archive_id=archive_id,
        sides=(
            _side("", archive_id, None, planner.UNRESOLVED_NO_IDENTITY),
        ),
        values={
            "inspector_version": None,
            "inspector_version_basis": "unknown_legacy",
        },
    )


def _candidate(key=5, archive_a=10, archive_b=11, revision_a=7,
               revision_b=None):
    """A half-bound candidate, which section 7.6 calls an ordinary outcome."""
    basis_b = (
        planner.SINGLE_REVISION_INHERITED if revision_b is not None
        else planner.UNRESOLVED_NO_IDENTITY
    )

    return AppliedBinding(
        table="near_duplicate_candidates",
        key=key,
        archive_id=archive_a,
        sides=(
            _side("a", archive_a, revision_a,
                  planner.SINGLE_REVISION_INHERITED),
            _side("b", archive_b, revision_b, basis_b),
        ),
        values={"archive_b_id": archive_b},
    )


def _signature(key=2, archive_id=10):
    return AppliedBinding(
        table="archive_content_signatures",
        key=key,
        archive_id=archive_id,
        sides=(_side("", archive_id, None, planner.UNRESOLVED_DRIFT),),
        values={},
    )


def _all() -> list[AppliedBinding]:
    return [_hashes(), _signature(), _inspection(), _candidate()]


# --- the contract, against the transcribed spec ---------------------------


def test_the_marker_is_the_projections_own() -> None:
    """And is specifically NOT the planner's.

    A projection that borrowed `PLAN_DIGEST_VERSION` would keep comparing
    equal across a change to either definition -- the failure the planner's
    own marker exists to prevent.
    """
    assert APPLIED_PROJECTION_VERSION == SPEC_VERSION
    assert APPLIED_PROJECTION_VERSION != planner.PLAN_DIGEST_VERSION
    assert APPLIED_PROJECTION_VERSION != planner.PLANNER_VERSION
    assert APPLIED_PROJECTION_VERSION != planner.SNAPSHOT_DIGEST_VERSION


def test_the_registry_matches_the_design_exactly() -> None:
    assert set(PROJECTION_BINDING_FIELDS) == SPEC_BINDING_FIELDS
    assert set(PROJECTION_SIDE_FIELDS) == SPEC_SIDE_FIELDS
    assert set(PROJECTION_TABLES) == SPEC_TABLES
    assert {
        table: set(fields)
        for table, fields in PROJECTION_VALUE_FIELDS.items()
    } == SPEC_VALUE_FIELDS


def test_a_rendered_binding_carries_exactly_the_registered_fields() -> None:
    """The rendering, not just the registry, is checked against the spec."""
    for binding in _all():
        payload = binding.projection_payload()

        assert set(payload) == SPEC_BINDING_FIELDS
        assert set(payload["values"]) == SPEC_VALUE_FIELDS[binding.table]

        for side in payload["sides"]:
            assert set(side) == SPEC_SIDE_FIELDS


def test_parameters_basis_appears_nowhere() -> None:
    """Slice 6's column. Its absence is asserted, never assumed.

    A projection that grew it would mean slice 4 had started writing a
    column whose meaning does not exist yet -- and the planner's own
    invariant requires it, so the mistake is an easy one to make by reusing
    `PlannedBinding`.
    """
    document = projection_document(_all()).decode("utf-8")

    assert "parameters_basis" not in document

    for fields in PROJECTION_VALUE_FIELDS.values():
        assert "parameters_basis" not in fields


def test_page_inventory_is_not_a_projection_table() -> None:
    assert "page_inventory" not in PROJECTION_TABLES
    assert "page_inventory" not in PROJECTION_VALUE_FIELDS
    assert "page_inventory" in planner.RECEIVING_TABLES


# --- selection ------------------------------------------------------------


def _planned_page_inventory() -> planner.PlannedBinding:
    return planner.PlannedBinding(
        table="page_inventory",
        key=10,
        key_kind="archive_id",
        archive_id=10,
        sides=(_side("", 10, 7, planner.SINGLE_REVISION_INHERITED),),
        values={
            "page_count": 20,
            "content_digest": "ab" * 32,
            "location_id": 4,
            "extracted_at": "2026-01-01T00:00:00",
            "extracted_at_basis": planner.SIGNATURE_CALCULATED_AT,
        },
    )


def _planned_candidate() -> planner.PlannedBinding:
    return planner.PlannedBinding(
        table="near_duplicate_candidates",
        key=5,
        key_kind="row_id",
        archive_id=10,
        sides=(
            _side("a", 10, 7, planner.SINGLE_REVISION_INHERITED),
            _side("b", 11, None, planner.UNRESOLVED_NO_IDENTITY),
        ),
        values={"archive_b_id": 11, "parameters_basis": "unknown_legacy"},
    )


def test_page_inventory_is_excluded_and_counted() -> None:
    """Returned, not discarded.

    The postflight artifact carries the deliberately-unapplied count as a
    measured figure. A selection that dropped these silently would leave
    that number to be asserted from the design document instead.
    """
    projected, excluded = select_slice4_bindings(
        [_planned_candidate(), _planned_page_inventory()]
    )

    assert [b.table for b in projected] == ["near_duplicate_candidates"]
    assert [b.table for b in excluded] == ["page_inventory"]
    assert excluded[0].key_kind == "archive_id"


def test_projecting_a_page_inventory_binding_directly_is_refused() -> None:
    """Returning None instead would let the excluded count drift.

    `select_slice4_bindings()` owns the by-construction exclusion because
    it counts what it excluded; one arriving at the projector individually
    means that selection was bypassed.
    """
    with pytest.raises(ProjectionError, match="not a slice-4 binding"):
        project_planned_binding(_planned_page_inventory())


def test_projecting_a_candidate_drops_parameters_basis_and_keeps_archive_b(
) -> None:
    """`archive_b_id` is preserved payload, not backfill attribution.

    It is in the projection so the reconciliation verifies the rebuild
    preserved it. It is read from the planned binding on the planned side
    and from the rebuilt row on the applied side, and is deliberately not
    staged as attribution.
    """
    projected = project_planned_binding(_planned_candidate())

    assert projected.values == {"archive_b_id": 11}
    assert "parameters_basis" not in projected.values


def test_both_sides_reduce_to_the_same_line() -> None:
    """The planned side via the planner's type, the applied side directly.

    This is the whole purpose of the module: like compared with like. It is
    NOT protection against a shared omission -- see the fault-injection
    group below, which is.
    """
    planned = project_planned_binding(_planned_candidate())
    applied = _candidate()

    assert planned.projection_line() == applied.projection_line()


# --- the byte framing -----------------------------------------------------


def test_the_document_framing_is_exact() -> None:
    document = projection_document([_hashes()])
    lines = document.split(b"\n")

    assert lines[0] == APPLIED_PROJECTION_VERSION.encode("utf-8")
    assert lines[1] == b"bindings|count=1"
    assert lines[2].startswith(b"{") and lines[2].endswith(b"}")
    # A trailing LF means the final split yields one empty element, and
    # nothing after it.
    assert lines[3] == b""
    assert len(lines) == 4


def test_the_document_carries_no_carriage_returns() -> None:
    """0x0D anywhere would mean a text-mode write reached this document.

    On Windows that is the default, and it would change every digest below
    without changing a single value -- the failure mode the design pinned
    the separator as a byte value to avoid.
    """
    assert b"\r" not in projection_document(_all())


def test_the_document_is_bytes_not_text() -> None:
    assert isinstance(projection_document(_all()), bytes)


def test_the_document_ends_with_exactly_one_newline() -> None:
    """So no rendering can be a prefix of another."""
    document = projection_document(_all())

    assert document.endswith(b"\n")
    assert not document.endswith(b"\n\n")


def test_an_empty_projection_still_has_a_document_and_a_digest() -> None:
    """"This table received nothing" is a bindable statement.

    A table absent from the postflight is indistinguishable from one whose
    digest was never computed, so the empty document is a real document.
    """
    document = projection_document([])

    assert document == (
        APPLIED_PROJECTION_VERSION.encode("utf-8") + b"\nbindings|count=0\n"
    )
    assert len(projection_digest([])) == 64


def test_the_lines_are_sorted_by_table_and_key() -> None:
    """Document order is a property of the data, not of sort stability.

    `(table, key)` is unique because 015 stages the plan under
    ``PRIMARY KEY (table_name, row_id)``, so the sort never breaks a tie.
    """
    bindings = [_hashes(key=9), _hashes(key=2), _signature(key=4)]
    ordered = sort_projection(bindings)

    assert [(b.table, b.key) for b in ordered] == [
        ("archive_content_signatures", 4),
        ("archive_hashes", 2),
        ("archive_hashes", 9),
    ]


def test_input_order_does_not_change_the_digest() -> None:
    forward = _all()
    backward = list(reversed(_all()))

    assert projection_digest(forward) == projection_digest(backward)


def test_the_digest_is_stable_across_calls() -> None:
    assert projection_digest(_all()) == projection_digest(_all())


# --- per-table digests ----------------------------------------------------


def test_every_table_appears_in_the_digest_report() -> None:
    report = projection_digests([_hashes()])

    assert set(report["per_table"]) == SPEC_TABLES
    assert report["per_table"]["archive_hashes"]["count"] == 1
    assert report["per_table"]["archive_inspections"]["count"] == 0
    assert report["count"] == 1
    assert report["projection_version"] == SPEC_VERSION


def test_a_change_moves_one_table_digest_and_the_whole_run_digest() -> None:
    """The per-table digests are what localise a mismatch.

    A whole-run digest that differs tells an operator nothing about where.
    This asserts the untouched tables stay still, which is the property
    that makes the per-table figures worth carrying.
    """
    before = _all()
    after = [b for b in before if b.table != "archive_hashes"]
    after.append(_hashes(revision_id=99))

    assert projection_digest(before) != projection_digest(after)
    assert (
        table_projection_digest(before, "archive_hashes")
        != table_projection_digest(after, "archive_hashes")
    )

    for table in ("archive_content_signatures", "archive_inspections",
                  "near_duplicate_candidates"):
        assert (
            table_projection_digest(before, table)
            == table_projection_digest(after, table)
        )


def test_an_unknown_table_digest_is_refused() -> None:
    with pytest.raises(ProjectionError, match="not a slice-4 receiving"):
        table_projection_digest(_all(), "page_inventory")


# --- side mapping ---------------------------------------------------------


def test_sides_map_by_label_not_position() -> None:
    """Section 12.2 maps `sides[label='a']` to the `_a` columns, never [0].

    Asserted through `sides_by_label()` so a caller cannot be right by
    accident on data that happens to be in order.
    """
    candidate = _candidate(archive_a=10, archive_b=11, revision_a=7,
                           revision_b=None)
    by_label = candidate.sides_by_label()

    assert set(by_label) == {"a", "b"}
    assert by_label["a"].archive_id == 10
    assert by_label["a"].source_revision_id == 7
    assert by_label["b"].archive_id == 11
    assert by_label["b"].source_revision_id is None


def test_a_swapped_side_pair_renders_differently() -> None:
    """Two sides carrying each other's data is a real, detectable change.

    If the projection rendered by position and the labels travelled with
    the values, this would be invisible. The reconciliation must see it:
    it is a candidate attributed to the wrong archives.
    """
    straight = _candidate(archive_a=10, archive_b=11, revision_a=7,
                          revision_b=8)
    swapped = AppliedBinding(
        table="near_duplicate_candidates",
        key=5,
        archive_id=10,
        sides=(
            _side("a", 11, 8, planner.SINGLE_REVISION_INHERITED),
            _side("b", 10, 7, planner.SINGLE_REVISION_INHERITED),
        ),
        values={"archive_b_id": 11},
    )

    assert straight.projection_line() != swapped.projection_line()


# --- shape refusals -------------------------------------------------------


def test_an_unknown_table_is_refused() -> None:
    with pytest.raises(ProjectionError, match="not a slice-4 receiving"):
        AppliedBinding(
            table="archive_notes", key=1, archive_id=10,
            sides=(_side("", 10, 7, planner.IDENTITY_SEED),), values={},
        )


def test_a_page_inventory_binding_is_refused() -> None:
    with pytest.raises(ProjectionError, match="page_inventory belongs"):
        AppliedBinding(
            table="page_inventory", key=1, archive_id=10,
            sides=(_side("", 10, 7, planner.SINGLE_REVISION_INHERITED),),
            values={},
        )


def test_a_natural_key_kind_is_refused() -> None:
    """Slice 4 projects row-keyed bindings; the natural-key form is 4p's."""
    with pytest.raises(ProjectionError, match="slice 4p's"):
        AppliedBinding(
            table="archive_hashes", key=1, archive_id=10,
            sides=(_side("", 10, 7, planner.IDENTITY_SEED),), values={},
            key_kind="archive_id",
        )


def test_a_missing_side_is_refused() -> None:
    with pytest.raises(ProjectionError, match="side labels"):
        AppliedBinding(
            table="near_duplicate_candidates", key=5, archive_id=10,
            sides=(_side("a", 10, 7, planner.SINGLE_REVISION_INHERITED),),
            values={"archive_b_id": 11},
        )


def test_a_duplicated_side_label_is_refused() -> None:
    with pytest.raises(ProjectionError, match="side labels"):
        AppliedBinding(
            table="near_duplicate_candidates", key=5, archive_id=10,
            sides=(
                _side("a", 10, 7, planner.SINGLE_REVISION_INHERITED),
                _side("a", 11, 8, planner.SINGLE_REVISION_INHERITED),
            ),
            values={"archive_b_id": 11},
        )


def test_an_unexpected_side_label_is_refused() -> None:
    with pytest.raises(ProjectionError, match="side labels"):
        AppliedBinding(
            table="near_duplicate_candidates", key=5, archive_id=10,
            sides=(
                _side("a", 10, 7, planner.SINGLE_REVISION_INHERITED),
                _side("c", 11, 8, planner.SINGLE_REVISION_INHERITED),
            ),
            values={"archive_b_id": 11},
        )


def test_side_labels_out_of_order_are_refused() -> None:
    """`sides` are specified as ordered by label.

    A reversed pair renders differently, so accepting it would make two
    documents for one state -- and the digest could then differ from the
    planned side for no reason an operator could act on.
    """
    with pytest.raises(ProjectionError, match="in order"):
        AppliedBinding(
            table="near_duplicate_candidates", key=5, archive_id=10,
            sides=(
                _side("b", 11, 8, planner.SINGLE_REVISION_INHERITED),
                _side("a", 10, 7, planner.SINGLE_REVISION_INHERITED),
            ),
            values={"archive_b_id": 11},
        )


def test_a_labelled_side_on_a_single_sided_table_is_refused() -> None:
    with pytest.raises(ProjectionError, match="side labels"):
        AppliedBinding(
            table="archive_hashes", key=1, archive_id=10,
            sides=(_side("a", 10, 7, planner.IDENTITY_SEED),), values={},
        )


def test_a_missing_value_field_is_refused() -> None:
    with pytest.raises(ProjectionError, match="missing"):
        AppliedBinding(
            table="archive_inspections", key=3, archive_id=10,
            sides=(
                _side("", 10, None, planner.UNRESOLVED_NO_IDENTITY),
            ),
            values={"inspector_version": None},
        )


def test_an_unexpected_value_field_is_refused() -> None:
    """Specifically including `parameters_basis`, named in the message."""
    with pytest.raises(ProjectionError, match="parameters_basis is a slice-6"):
        AppliedBinding(
            table="near_duplicate_candidates", key=5, archive_id=10,
            sides=(
                _side("a", 10, 7, planner.SINGLE_REVISION_INHERITED),
                _side("b", 11, None, planner.UNRESOLVED_NO_IDENTITY),
            ),
            values={"archive_b_id": 11, "parameters_basis": "unknown_legacy"},
        )


@pytest.mark.parametrize("bad", ["7", 7.0, None])
def test_a_non_integer_key_is_refused(bad: object) -> None:
    with pytest.raises(ProjectionError, match="not an integer"):
        AppliedBinding(
            table="archive_hashes", key=bad, archive_id=10,
            sides=(_side("", 10, 7, planner.IDENTITY_SEED),), values={},
        )


def test_a_bool_is_not_accepted_as_an_integer() -> None:
    """`bool` subclasses `int`, so a bare isinstance check would pass it.

    `True` would then render as `true` rather than `1` -- a different
    document and a different digest, from a type error rather than from a
    value that moved.
    """
    with pytest.raises(ProjectionError, match="not an integer"):
        AppliedBinding(
            table="archive_hashes", key=True, archive_id=10,
            sides=(_side("", 10, 7, planner.IDENTITY_SEED),), values={},
        )


def test_a_non_integer_side_revision_is_refused() -> None:
    with pytest.raises(ProjectionError, match="not an integer"):
        AppliedBinding(
            table="archive_hashes", key=1, archive_id=10,
            sides=(_side("", 10, "7", planner.IDENTITY_SEED),), values={},
        )


def test_a_null_side_revision_is_accepted() -> None:
    """An unresolved side has no revision, and `null` is not an error."""
    binding = AppliedBinding(
        table="archive_hashes", key=1, archive_id=10,
        sides=(_side("", 10, None, planner.UNRESOLVED_NO_IDENTITY),),
        values={},
    )

    assert '"source_revision_id":null' in binding.projection_line()


def test_a_non_string_basis_is_refused() -> None:
    with pytest.raises(ProjectionError, match="provenance_basis is int"):
        AppliedBinding(
            table="archive_hashes", key=1, archive_id=10,
            sides=(_side("", 10, 7, 15),), values={},
        )


# --- what the projector deliberately does NOT judge -----------------------


def test_an_unrecognised_basis_renders_rather_than_raising() -> None:
    """A value disagreement is reconciliation's to report, not the
    projector's to crash on.

    Raising here would abort migration 015's transaction with no diagnosis
    -- the operator would learn that reconciliation failed but not which
    row, field or value moved. Rendering it means the line differs from the
    planned side and the mismatch is reported by name.
    """
    binding = AppliedBinding(
        table="archive_hashes", key=1, archive_id=10,
        sides=(_side("", 10, 7, "basis_from_a_future_slice"),), values={},
    )

    assert "basis_from_a_future_slice" in binding.projection_line()


def test_a_bound_basis_without_a_revision_renders_rather_than_raising(
) -> None:
    """`PlannedBinding` refuses this pairing; the projector does not.

    R12 makes it structurally impossible after 015, so an applied row
    carrying it is exactly the corruption reconciliation exists to surface.
    A projector that raised would turn a reportable finding into a crash.
    """
    binding = AppliedBinding(
        table="archive_hashes", key=1, archive_id=10,
        sides=(_side("", 10, None, planner.IDENTITY_SEED),), values={},
    )

    assert '"source_revision_id":null' in binding.projection_line()

    with pytest.raises(planner.PlannerInvariantError, match="bound basis"):
        planner.PlannedBinding(
            table="archive_hashes", key=1, key_kind="row_id", archive_id=10,
            sides=(_side("", 10, None, planner.IDENTITY_SEED),), values={},
        )


# --- fault injection: one field at a time --------------------------------
#
# The group that actually protects the reconciliation. Each test alters a
# single field of a single binding and requires the whole-run digest to
# move. A projection that dropped that field from its rendering would leave
# the two digests equal and the test would fail by name.


def test_altering_a_basis_moves_the_digest() -> None:
    before = _all()
    after = [b for b in before if b.table != "archive_hashes"]
    after.append(
        AppliedBinding(
            table="archive_hashes", key=1, archive_id=10,
            sides=(_side("", 10, 7, planner.MEASURED),), values={},
        )
    )

    assert projection_digest(before) != projection_digest(after)


def test_altering_a_revision_moves_the_digest() -> None:
    before = _all()
    after = [b for b in before if b.table != "archive_hashes"]
    after.append(_hashes(revision_id=8))

    assert projection_digest(before) != projection_digest(after)


def test_altering_a_side_label_assignment_moves_the_digest() -> None:
    """The same two revisions, swapped between the labels."""
    before = [_candidate(revision_a=7, revision_b=8)]
    after = [_candidate(revision_a=8, revision_b=7)]

    assert projection_digest(before) != projection_digest(after)


def test_altering_an_archive_id_moves_the_digest() -> None:
    before = _all()
    after = [b for b in before if b.table != "archive_hashes"]
    after.append(_hashes(archive_id=99))

    assert projection_digest(before) != projection_digest(after)


def test_altering_a_key_moves_the_digest() -> None:
    before = _all()
    after = [b for b in before if b.table != "archive_hashes"]
    after.append(_hashes(key=2))

    assert projection_digest(before) != projection_digest(after)


def test_altering_a_table_specific_value_moves_the_digest() -> None:
    before = _all()
    after = [b for b in before if b.table != "archive_inspections"]
    after.append(
        AppliedBinding(
            table="archive_inspections", key=3, archive_id=10,
            sides=(
                _side("", 10, None, planner.UNRESOLVED_NO_IDENTITY),
            ),
            values={
                "inspector_version": "1.2",
                "inspector_version_basis": "known",
            },
        )
    )

    assert projection_digest(before) != projection_digest(after)


def test_a_none_value_and_an_empty_string_do_not_collide() -> None:
    """`_canonical_json` renders `null` distinctly from `""`.

    The property the reader's residual CSV ambiguity relies on: a misread
    of one for the other changes the digest instead of passing silently.
    """
    with_none = [_inspection()]
    with_empty = [
        AppliedBinding(
            table="archive_inspections", key=3, archive_id=10,
            sides=(
                _side("", 10, None, planner.UNRESOLVED_NO_IDENTITY),
            ),
            values={
                "inspector_version": "",
                "inspector_version_basis": "unknown_legacy",
            },
        )
    ]

    assert projection_digest(with_none) != projection_digest(with_empty)


def test_a_basis_containing_a_delimiter_cannot_forge_structure() -> None:
    """JSON escapes it; the `name=value|` rendering it replaced did not.

    Two distinct bindings were once shown to render identically that way.
    The equivalent attempt here must produce a different digest.
    """
    honest = [
        AppliedBinding(
            table="archive_hashes", key=1, archive_id=10,
            sides=(_side("", 10, 7, 'a","label":"b'),), values={},
        )
    ]
    plain = [_hashes()]

    assert projection_digest(honest) != projection_digest(plain)
    assert len(projection_document(honest).splitlines()) == 3
