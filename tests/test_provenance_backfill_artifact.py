"""Reading a written backfill plan back, and every way that must refuse.

The point of these tests is not that a writer-produced pair round-trips.
That is one test, and it would pass against a reader that validated
nothing. Each refusal below is exercised **independently**, from an
artifact that is valid in every other respect, so a guard that stopped
working would fail its own test by name rather than hiding behind another.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping

import pytest

from comic_automation.archive import provenance_backfill_planner as planner
from comic_automation.archive.provenance_backfill_artifact import (
    EXPECTED_ENVELOPE_FIELDS,
    PlanArtifactError,
    UNVERIFIED_ENVELOPE_FIELDS,
    read_plan_artifacts,
    reconstruct_gate_failures,
)


# --- fixtures -------------------------------------------------------------


def _side(label, archive_id, revision_id, basis):
    return planner.SideAttribution(
        label=label,
        archive_id=archive_id,
        source_revision_id=revision_id,
        provenance_basis=basis,
    )


def _bindings() -> list[planner.PlannedBinding]:
    """One binding per receiving table, covering every shape that exists.

    Deliberately includes a `page_inventory` row (natural-keyed, five
    artifact columns), a pairwise candidate with one bound and one
    unresolved side, and an inspection whose `inspector_version` is `None`
    -- the value that renders as an empty cell and is the reason the
    reader disambiguates by table.
    """
    rows = [
        planner.PlannedBinding(
            table="archive_hashes",
            key=1,
            key_kind="row_id",
            archive_id=10,
            sides=(_side("", 10, 7, planner.IDENTITY_SEED),),
            values={},
        ),
        planner.PlannedBinding(
            table="archive_content_signatures",
            key=2,
            key_kind="row_id",
            archive_id=10,
            sides=(_side("", 10, None, planner.UNRESOLVED_DRIFT),),
            values={},
        ),
        planner.PlannedBinding(
            table="archive_inspections",
            key=3,
            key_kind="row_id",
            archive_id=10,
            sides=(_side("", 10, None, planner.UNRESOLVED_NO_IDENTITY),),
            values={
                "inspector_version": None,
                "inspector_version_basis": "unknown_legacy",
            },
        ),
        planner.PlannedBinding(
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
        ),
        planner.PlannedBinding(
            table="near_duplicate_candidates",
            key=5,
            key_kind="row_id",
            archive_id=10,
            sides=(
                _side("a", 10, 7, planner.SINGLE_REVISION_INHERITED),
                _side("b", 11, None, planner.UNRESOLVED_NO_IDENTITY),
            ),
            values={"archive_b_id": 11, "parameters_basis": "unknown_legacy"},
        ),
    ]

    rows.sort(key=lambda binding: (binding.table, binding.key))
    return rows


SNAPSHOT_DIGEST = "s" * 64


def _plan(bindings=None) -> planner.BackfillPlan:
    bindings = list(_bindings() if bindings is None else bindings)
    bindings.sort(key=lambda binding: (binding.table, binding.key))

    return planner.BackfillPlan(
        planner_version=planner.PLANNER_VERSION,
        snapshot_digest=SNAPSHOT_DIGEST,
        plan_digest=planner.compute_plan_digest(bindings, SNAPSHOT_DIGEST),
        bindings=tuple(bindings),
        totals=planner.plan_totals(bindings),
        gates=planner.ArchiveGate((), (), ()),
        quarantine_rows=0,
    )


def _write(tmp_path: Path, plan=None, *, csv_text=None, envelope=None):
    """Write a plan pair, with either half overridable.

    The CSV digest is recomputed from whatever text is actually written
    unless the envelope is overridden wholesale, so a test that edits the
    CSV gets an artifact that is *consistent* and differs only in the one
    thing it set out to change. Tests that want the digest itself to
    disagree say so explicitly.
    """
    plan = _plan() if plan is None else plan

    if csv_text is None:
        csv_text = planner.render_plan_csv(plan)

    csv_bytes = csv_text.encode("utf-8")
    digest = hashlib.sha256(csv_bytes).hexdigest()

    if envelope is None:
        envelope_text = planner.render_plan_json(plan, csv_sha256=digest)
    else:
        envelope_text = json.dumps(envelope, indent=2, sort_keys=True) + "\n"

    json_path = tmp_path / "plan.json"
    csv_path = tmp_path / "plan.csv"
    csv_path.write_bytes(csv_bytes)
    json_path.write_bytes(envelope_text.encode("utf-8"))

    return json_path, csv_path


def _envelope_of(plan, csv_text) -> dict:
    """The envelope a plan would be written with, as a mutable dict."""
    digest = hashlib.sha256(csv_text.encode("utf-8")).hexdigest()
    return json.loads(planner.render_plan_json(plan, csv_sha256=digest))


def _csv_lines(csv_text: str) -> list[str]:
    """Split a rendered CSV into lines, keeping the CRLF terminators out.

    `splitlines()` rather than `split("\\n")`, and the caller rejoins with
    the same terminator the writer used -- see
    `test_the_csv_digest_covers_crlf_bytes` for why that matters.
    """
    return csv_text.split("\r\n")


# --- the round trip -------------------------------------------------------


def test_a_written_plan_reads_back_identically(tmp_path: Path) -> None:
    plan = _plan()
    json_path, csv_path = _write(tmp_path, plan)

    loaded = read_plan_artifacts(json_path, csv_path)

    assert loaded.bindings == plan.bindings
    assert loaded.plan_digest == plan.plan_digest
    assert loaded.snapshot_digest == plan.snapshot_digest
    assert loaded.planner_version == planner.PLANNER_VERSION


def test_the_inspection_none_value_survives_the_empty_cell(
    tmp_path: Path,
) -> None:
    """`inspector_version=None` renders as "" and must read back as None.

    The cell is indistinguishable from an unused column at the cell; it is
    distinguished by table. If the reader resolved it the other way the
    value would come back as the empty string, and the plan digest would
    not match -- so this test also pins which of the two the reader picks.
    """
    json_path, csv_path = _write(tmp_path)

    loaded = read_plan_artifacts(json_path, csv_path)
    inspection = next(
        b for b in loaded.bindings if b.table == "archive_inspections"
    )

    assert inspection.values["inspector_version"] is None
    assert inspection.values["inspector_version_basis"] == "unknown_legacy"


def test_page_inventory_rows_are_read_rather_than_skipped(
    tmp_path: Path,
) -> None:
    """They are 4p's to apply, but 015's digest is computed over them.

    A reader that dropped them would recompute a different digest on every
    real plan. Asserted as a property of the read AND by removing the row
    to show the digest then fails -- otherwise "it reads them" would be a
    claim about a list length rather than about the digest.

    The envelope's totals are recounted over the *reduced* set on purpose.
    Left alone they disagree first, and the test would then prove only that
    the totals check works -- which is a different test, below. Restating
    them leaves the plan digest as the single check that can still fire.
    """
    json_path, csv_path = _write(tmp_path)
    loaded = read_plan_artifacts(json_path, csv_path)

    assert any(b.table == "page_inventory" for b in loaded.bindings)

    plan = _plan()
    lines = _csv_lines(planner.render_plan_csv(plan))
    without = [line for line in lines if not line.startswith("page_inventory,")]
    csv_text = "\r\n".join(without)

    envelope = _envelope_of(plan, csv_text)
    envelope["totals"] = planner.plan_totals(
        [b for b in plan.bindings if b.table != "page_inventory"]
    )

    json_path, csv_path = _write(
        tmp_path, plan, csv_text=csv_text, envelope=envelope
    )

    with pytest.raises(PlanArtifactError, match="not the same plan"):
        read_plan_artifacts(json_path, csv_path)


def test_row_order_does_not_change_the_verdict(tmp_path: Path) -> None:
    """A shuffled CSV verifies: the digest is over the planner's order.

    `build_plan()` sorts by (table, key) before digesting, so the reader
    sorts too. Without that a file whose rows were reordered -- which no
    check above the digest would notice -- would be refused as a different
    plan.
    """
    plan = _plan()
    lines = _csv_lines(planner.render_plan_csv(plan))
    header, body = lines[0], [line for line in lines[1:] if line]
    shuffled = "\r\n".join([header, *reversed(body)]) + "\r\n"

    json_path, csv_path = _write(tmp_path, plan, csv_text=shuffled)
    loaded = read_plan_artifacts(json_path, csv_path)

    assert loaded.plan_digest == plan.plan_digest


def test_the_csv_digest_covers_crlf_bytes(tmp_path: Path) -> None:
    """The written CSV really does contain CRLF, and the digest covers it.

    This is the reason the reader opens the file in binary. Reading it as
    text would translate the terminators on Windows, change the digest and
    reject every valid plan -- a failure that would look like corruption
    rather than like a mode bug. The terminator is asserted here so the
    property is pinned rather than assumed from the csv module's defaults.
    """
    json_path, csv_path = _write(tmp_path)
    raw = csv_path.read_bytes()

    assert b"\r\n" in raw

    envelope = json.loads(json_path.read_text(encoding="utf-8"))
    assert envelope["artifacts"]["csv_sha256"] == (
        hashlib.sha256(raw).hexdigest()
    )

    # And with the terminators normalised to LF -- exactly what a text-mode
    # writer would produce -- the pair no longer verifies.
    csv_path.write_bytes(raw.replace(b"\r\n", b"\n"))

    with pytest.raises(PlanArtifactError, match="do not match the envelope"):
        read_plan_artifacts(json_path, csv_path)


# --- envelope refusals ----------------------------------------------------


def test_an_unparseable_envelope_is_refused(tmp_path: Path) -> None:
    json_path, csv_path = _write(tmp_path)
    json_path.write_bytes(b"{not json")

    with pytest.raises(PlanArtifactError, match="not valid JSON"):
        read_plan_artifacts(json_path, csv_path)


def test_an_envelope_that_is_not_an_object_is_refused(
    tmp_path: Path,
) -> None:
    json_path, csv_path = _write(tmp_path)
    json_path.write_bytes(b"[]")

    with pytest.raises(PlanArtifactError, match="not an object"):
        read_plan_artifacts(json_path, csv_path)


def test_a_duplicate_envelope_key_is_refused(tmp_path: Path) -> None:
    """`json.loads` keeps the last occurrence and reports nothing.

    So an approved envelope could have a second `plan_digest` appended and
    would parse cleanly as the appended value. Written as raw text because
    no Python dict can express the state under test.
    """
    plan = _plan()
    json_path, csv_path = _write(tmp_path, plan)
    envelope = json_path.read_text(encoding="utf-8")

    doubled = envelope.replace(
        '"plan_digest":',
        '"plan_digest": "' + "0" * 64 + '", "plan_digest":',
        1,
    )
    json_path.write_bytes(doubled.encode("utf-8"))

    with pytest.raises(PlanArtifactError, match="repeats the key"):
        read_plan_artifacts(json_path, csv_path)


@pytest.mark.parametrize("field", sorted(EXPECTED_ENVELOPE_FIELDS))
def test_a_missing_envelope_field_is_refused(
    tmp_path: Path, field: str
) -> None:
    """Every one of the thirteen, not the five the first revision checked.

    The earlier subset let `execution_status`, `target_states`,
    `gate_failures` and `archive_gates` be removed or forged while the
    bindings, totals and both digests stayed valid.
    """
    plan = _plan()
    csv_text = planner.render_plan_csv(plan)
    envelope = _envelope_of(plan, csv_text)
    del envelope[field]

    json_path, csv_path = _write(
        tmp_path, plan, csv_text=csv_text, envelope=envelope
    )

    with pytest.raises(
        PlanArtifactError, match="does not carry exactly the expected fields"
    ):
        read_plan_artifacts(json_path, csv_path)


def test_an_unexpected_envelope_field_is_refused(tmp_path: Path) -> None:
    """An unexamined key in an approval record is a place to hide something.

    Nothing in this reader would look at it, and a later consumer might.
    """
    plan = _plan()
    csv_text = planner.render_plan_csv(plan)
    envelope = _envelope_of(plan, csv_text)
    envelope["unexpected_top_level"] = "ignored"

    json_path, csv_path = _write(
        tmp_path, plan, csv_text=csv_text, envelope=envelope
    )

    with pytest.raises(
        PlanArtifactError, match=r"unexpected \['unexpected_top_level'\]"
    ):
        read_plan_artifacts(json_path, csv_path)


@pytest.mark.parametrize(
    "field, forged",
    [
        ("execution_status", "EXECUTE_NOW"),
        ("target_states", ["forged"]),
        ("receiving_tables", ["archive_hashes"]),
        ("natural_key_tables", []),
        ("table_vocabulary", {"archive_hashes": ["forged"]}),
    ],
)
def test_a_forged_envelope_constant_is_refused(
    tmp_path: Path, field: str, forged: object
) -> None:
    """These are constants of this tree, not opinions the plan may hold.

    Each is rendered straight out of a planner module constant, so an
    envelope disagreeing with one was not written by this planner against
    this tree whatever its planner_version claims. `execution_status` is
    the one that matters most: it is the field that would be moved to make
    an unexecuted plan look like something else.
    """
    plan = _plan()
    csv_text = planner.render_plan_csv(plan)
    envelope = _envelope_of(plan, csv_text)
    envelope[field] = forged

    json_path, csv_path = _write(
        tmp_path, plan, csv_text=csv_text, envelope=envelope
    )

    with pytest.raises(PlanArtifactError, match="this tree defines"):
        read_plan_artifacts(json_path, csv_path)


@pytest.mark.parametrize(
    "gates",
    [
        {"forged": "accepted"},
        {"provisional_archives": 0, "archives_without_revision": 0,
         "drift_archives": 0},
        "not an object",
        # Non-iterables, and they are the reason the `isinstance(gates,
        # dict)` check exists at all. Every iterable value above is already
        # caught by the field-set comparison below it -- `set("not an
        # object")` is a set of characters, which is not the expected field
        # set -- so with only those cases the type check could be deleted
        # and nothing would fail. These two reach `set()` on an int and on
        # None, which raises TypeError: a crash escaping as the wrong
        # exception type rather than a refusal an operator can act on.
        5,
        None,
    ],
)
def test_a_malformed_archive_gates_object_is_refused(
    tmp_path: Path, gates: object
) -> None:
    """Its values are unverifiable, so its shape is the only thing checked.

    That makes the shape check load-bearing rather than cosmetic: a
    consumer reading `LoadedPlan.unverified` is entitled to the declared
    fields with the declared types, since nothing downstream will catch a
    surprise there.
    """
    plan = _plan()
    csv_text = planner.render_plan_csv(plan)
    envelope = _envelope_of(plan, csv_text)
    envelope["archive_gates"] = gates

    json_path, csv_path = _write(
        tmp_path, plan, csv_text=csv_text, envelope=envelope
    )

    with pytest.raises(PlanArtifactError, match="archive_gates"):
        read_plan_artifacts(json_path, csv_path)


@pytest.mark.parametrize(
    "field, value",
    [
        ("provisional_archives", "many"),
        ("provisional_archives", True),
        ("drift_archive_ids", "1,2"),
        ("drift_archive_ids", [1, "2"]),
        ("drift_archive_ids", [True]),
    ],
)
def test_a_mistyped_archive_gate_value_is_refused(
    tmp_path: Path, field: str, value: object
) -> None:
    """`True` is refused where a count belongs: bool subclasses int."""
    plan = _plan()
    csv_text = planner.render_plan_csv(plan)
    envelope = _envelope_of(plan, csv_text)
    envelope["archive_gates"][field] = value

    json_path, csv_path = _write(
        tmp_path, plan, csv_text=csv_text, envelope=envelope
    )

    with pytest.raises(PlanArtifactError, match=f"archive_gates.{field}"):
        read_plan_artifacts(json_path, csv_path)


@pytest.mark.parametrize("value", ["many", None, 1.5, True])
def test_a_mistyped_quarantine_count_is_refused(
    tmp_path: Path, value: object
) -> None:
    plan = _plan()
    csv_text = planner.render_plan_csv(plan)
    envelope = _envelope_of(plan, csv_text)
    envelope["quarantine_rows_excluded"] = value

    json_path, csv_path = _write(
        tmp_path, plan, csv_text=csv_text, envelope=envelope
    )

    with pytest.raises(
        PlanArtifactError, match="quarantine_rows_excluded"
    ):
        read_plan_artifacts(json_path, csv_path)


@pytest.mark.parametrize("value", ["a failure", [1], {"a": 1}, None])
def test_a_mistyped_gate_failures_field_is_refused(
    tmp_path: Path, value: object
) -> None:
    plan = _plan()
    csv_text = planner.render_plan_csv(plan)
    envelope = _envelope_of(plan, csv_text)
    envelope["gate_failures"] = value

    json_path, csv_path = _write(
        tmp_path, plan, csv_text=csv_text, envelope=envelope
    )

    with pytest.raises(
        PlanArtifactError, match="expected a list of strings"
    ):
        read_plan_artifacts(json_path, csv_path)


@pytest.mark.parametrize(
    "artifacts",
    [
        {"csv_sha256": "0" * 64, "extra": 1},
        {},
        {"sha256": "0" * 64},
    ],
)
def test_a_malformed_artifacts_object_is_refused(
    tmp_path: Path, artifacts: dict
) -> None:
    plan = _plan()
    csv_text = planner.render_plan_csv(plan)
    envelope = _envelope_of(plan, csv_text)
    envelope["artifacts"] = artifacts

    json_path, csv_path = _write(
        tmp_path, plan, csv_text=csv_text, envelope=envelope
    )

    with pytest.raises(PlanArtifactError, match="artifacts carries"):
        read_plan_artifacts(json_path, csv_path)


def test_an_unsupported_planner_version_is_refused(tmp_path: Path) -> None:
    """A future format is refused, not read on a best effort.

    The reader's decoding rules -- which column belongs to which table,
    what an empty cell means -- belong to a format version. Applying them
    to a file written under another one produces bindings nobody approved.
    """
    plan = _plan()
    csv_text = planner.render_plan_csv(plan)
    envelope = _envelope_of(plan, csv_text)
    envelope["planner_version"] = "provenance-backfill-planner/99"

    json_path, csv_path = _write(
        tmp_path, plan, csv_text=csv_text, envelope=envelope
    )

    with pytest.raises(PlanArtifactError, match="this reader supports"):
        read_plan_artifacts(json_path, csv_path)


@pytest.mark.parametrize(
    "field, value",
    [
        ("snapshot_digest", 17),
        ("snapshot_digest", ""),
        ("plan_digest", None),
        ("planner_version", []),
    ],
)
def test_a_mistyped_envelope_field_is_refused(
    tmp_path: Path, field: str, value: object
) -> None:
    plan = _plan()
    csv_text = planner.render_plan_csv(plan)
    envelope = _envelope_of(plan, csv_text)
    envelope[field] = value

    json_path, csv_path = _write(
        tmp_path, plan, csv_text=csv_text, envelope=envelope
    )

    with pytest.raises(PlanArtifactError, match="expected a non-empty string"):
        read_plan_artifacts(json_path, csv_path)


def test_a_non_object_totals_field_is_refused(tmp_path: Path) -> None:
    plan = _plan()
    csv_text = planner.render_plan_csv(plan)
    envelope = _envelope_of(plan, csv_text)
    envelope["totals"] = []

    json_path, csv_path = _write(
        tmp_path, plan, csv_text=csv_text, envelope=envelope
    )

    with pytest.raises(PlanArtifactError, match="totals is a list"):
        read_plan_artifacts(json_path, csv_path)


def test_an_envelope_written_without_a_csv_is_refused(
    tmp_path: Path,
) -> None:
    """`csv_sha256: null` is what an envelope-only write records.

    It is a legitimate artifact and an illegitimate input: nothing attests
    to any bindings file, so no CSV can be shown to be the one this
    envelope approved. Refused rather than read against whatever CSV
    happens to sit beside it.
    """
    plan = _plan()
    csv_text = planner.render_plan_csv(plan)
    envelope = _envelope_of(plan, csv_text)
    envelope["artifacts"] = {"csv_sha256": None}

    json_path, csv_path = _write(
        tmp_path, plan, csv_text=csv_text, envelope=envelope
    )

    with pytest.raises(PlanArtifactError, match="attests to no CSV"):
        read_plan_artifacts(json_path, csv_path)


def test_a_non_object_artifacts_field_is_refused(tmp_path: Path) -> None:
    plan = _plan()
    csv_text = planner.render_plan_csv(plan)
    envelope = _envelope_of(plan, csv_text)
    envelope["artifacts"] = "deadbeef"

    json_path, csv_path = _write(
        tmp_path, plan, csv_text=csv_text, envelope=envelope
    )

    with pytest.raises(PlanArtifactError, match="artifacts is a str"):
        read_plan_artifacts(json_path, csv_path)


# --- CSV structure refusals ----------------------------------------------


def test_a_csv_that_does_not_match_its_recorded_digest_is_refused(
    tmp_path: Path,
) -> None:
    """One altered cell, everything else intact.

    The row still parses, still names a real table, and still reconciles
    against nothing else -- the digest is the only thing that catches it.
    """
    plan = _plan()
    csv_text = planner.render_plan_csv(plan)
    envelope = _envelope_of(plan, csv_text)

    tampered = csv_text.replace(
        "unresolved_no_identity", "single_revision_inherited", 1
    )
    assert tampered != csv_text

    json_path, csv_path = _write(
        tmp_path, plan, csv_text=tampered, envelope=envelope
    )

    with pytest.raises(PlanArtifactError, match="do not match the envelope"):
        read_plan_artifacts(json_path, csv_path)


def test_an_empty_csv_is_refused(tmp_path: Path) -> None:
    json_path, csv_path = _write(tmp_path, csv_text="")

    with pytest.raises(PlanArtifactError, match="are empty"):
        read_plan_artifacts(json_path, csv_path)


def test_a_duplicated_header_column_is_refused(tmp_path: Path) -> None:
    """`csv.DictReader` would collapse the pair and drop a real column.

    The duplicate is named in the message, because a header of 25 columns
    does not show its own repetition to a reader scanning it.
    """
    plan = _plan()
    lines = _csv_lines(planner.render_plan_csv(plan))
    lines[0] = lines[0].replace("bound,", "bound,bound,", 1)

    json_path, csv_path = _write(
        tmp_path, plan, csv_text="\r\n".join(lines)
    )

    with pytest.raises(PlanArtifactError, match="duplicated columns"):
        read_plan_artifacts(json_path, csv_path)


def test_a_reordered_header_is_refused(tmp_path: Path) -> None:
    """Every value would pair with the wrong name, and parse without error."""
    plan = _plan()
    lines = _csv_lines(planner.render_plan_csv(plan))
    columns = lines[0].split(",")
    columns[1], columns[2] = columns[2], columns[1]
    lines[0] = ",".join(columns)

    json_path, csv_path = _write(
        tmp_path, plan, csv_text="\r\n".join(lines)
    )

    with pytest.raises(PlanArtifactError, match="unexpected header"):
        read_plan_artifacts(json_path, csv_path)


def test_a_missing_header_column_is_refused(tmp_path: Path) -> None:
    plan = _plan()
    lines = _csv_lines(planner.render_plan_csv(plan))
    lines[0] = lines[0].replace(",parameters_basis", "", 1)

    json_path, csv_path = _write(
        tmp_path, plan, csv_text="\r\n".join(lines)
    )

    with pytest.raises(PlanArtifactError, match="unexpected header"):
        read_plan_artifacts(json_path, csv_path)


def test_a_short_row_is_refused(tmp_path: Path) -> None:
    """A row with fewer fields than the header, named by line number."""
    plan = _plan()
    lines = _csv_lines(planner.render_plan_csv(plan))
    lines[1] = lines[1].rsplit(",", 1)[0]

    json_path, csv_path = _write(
        tmp_path, plan, csv_text="\r\n".join(lines)
    )

    with pytest.raises(PlanArtifactError, match=r"line 2: \d+ field"):
        read_plan_artifacts(json_path, csv_path)


# --- row refusals ---------------------------------------------------------


def _row_index(lines: list[str], table: str) -> int:
    """The index of the first body line belonging to `table`."""
    return next(
        index for index, line in enumerate(lines)
        if line.startswith(table + ",")
    )


def _rewrite_cell(lines: list[str], table: str, column: str, value: str):
    """Replace one cell of one table's row, by column name.

    By name rather than by offset: the header is 24 columns wide and an
    off-by-one would silently edit a neighbour, producing a test that
    passes for the wrong reason.
    """
    index = _row_index(lines, table)
    cells = lines[index].split(",")
    cells[list(planner.CSV_COLUMNS).index(column)] = value
    lines[index] = ",".join(cells)
    return lines


def test_an_unknown_table_is_refused(tmp_path: Path) -> None:
    plan = _plan()
    lines = _csv_lines(planner.render_plan_csv(plan))
    lines = _rewrite_cell(lines, "archive_hashes", "table", "archive_notes")

    json_path, csv_path = _write(
        tmp_path, plan, csv_text="\r\n".join(lines)
    )

    with pytest.raises(PlanArtifactError, match="not a receiving table"):
        read_plan_artifacts(json_path, csv_path)


def test_a_wrong_key_kind_is_refused(tmp_path: Path) -> None:
    """Only `page_inventory` is natural-keyed; the other four are row-keyed."""
    plan = _plan()
    lines = _csv_lines(planner.render_plan_csv(plan))
    lines = _rewrite_cell(lines, "archive_hashes", "key_kind", "archive_id")

    json_path, csv_path = _write(
        tmp_path, plan, csv_text="\r\n".join(lines)
    )

    with pytest.raises(PlanArtifactError, match="is keyed by 'row_id'"):
        read_plan_artifacts(json_path, csv_path)


def test_a_column_the_table_does_not_use_is_refused(tmp_path: Path) -> None:
    """This is what makes the empty cell unambiguous.

    `archive_hashes` has no artifact columns, so `inspector_version` must
    be empty on its rows. A populated one means the row does not describe
    the table it names -- and without this check the reader would have to
    decide per cell what an empty string meant.
    """
    plan = _plan()
    lines = _csv_lines(planner.render_plan_csv(plan))
    lines = _rewrite_cell(
        lines, "archive_hashes", "inspector_version", "1.2"
    )

    json_path, csv_path = _write(
        tmp_path, plan, csv_text="\r\n".join(lines)
    )

    with pytest.raises(PlanArtifactError, match="does not use"):
        read_plan_artifacts(json_path, csv_path)


def test_a_pairwise_column_on_a_single_sided_row_is_refused(
    tmp_path: Path,
) -> None:
    """The same guard, in the direction that matters for attribution."""
    plan = _plan()
    lines = _csv_lines(planner.render_plan_csv(plan))
    lines = _rewrite_cell(lines, "archive_hashes", "revision_b_id", "9")

    json_path, csv_path = _write(
        tmp_path, plan, csv_text="\r\n".join(lines)
    )

    with pytest.raises(PlanArtifactError, match="does not use"):
        read_plan_artifacts(json_path, csv_path)


def test_an_empty_basis_is_refused(tmp_path: Path) -> None:
    """An unresolved side carries an unresolved basis, never none at all."""
    plan = _plan()
    lines = _csv_lines(planner.render_plan_csv(plan))
    lines = _rewrite_cell(
        lines, "archive_content_signatures", "provenance_basis", ""
    )

    json_path, csv_path = _write(
        tmp_path, plan, csv_text="\r\n".join(lines)
    )

    with pytest.raises(PlanArtifactError, match="provenance_basis is empty"):
        read_plan_artifacts(json_path, csv_path)


def test_an_empty_side_archive_id_is_refused(tmp_path: Path) -> None:
    plan = _plan()
    lines = _csv_lines(planner.render_plan_csv(plan))
    lines = _rewrite_cell(
        lines, "near_duplicate_candidates", "archive_a_id", ""
    )

    json_path, csv_path = _write(
        tmp_path, plan, csv_text="\r\n".join(lines)
    )

    with pytest.raises(PlanArtifactError, match="archive_a_id is empty"):
        read_plan_artifacts(json_path, csv_path)


@pytest.mark.parametrize("raw", [" 7", "7 ", "+7", "1_0", "7.0", "abc", "-",
                                 "²"])
def test_a_non_plain_integer_is_refused(tmp_path: Path, raw: str) -> None:
    """`int()` would accept four of these; none is anything the writer emits.

    Refused at the cell rather than left for the plan digest, so the
    operator is handed the offending value instead of a whole-plan
    mismatch. `"\\u00b2"` is superscript two: `str.isdigit()` is true for
    it and `int()` raises, which is why the check tests ASCII as well.
    """
    plan = _plan()
    lines = _csv_lines(planner.render_plan_csv(plan))
    lines = _rewrite_cell(lines, "archive_hashes", "source_revision_id", raw)

    json_path, csv_path = _write(
        tmp_path, plan, csv_text="\r\n".join(lines)
    )

    with pytest.raises(PlanArtifactError, match="not a plain integer"):
        read_plan_artifacts(json_path, csv_path)


@pytest.mark.parametrize("raw", ["true", "1", "", "yes", "TRUE"])
def test_a_non_python_bool_is_refused(tmp_path: Path, raw: str) -> None:
    """The writer renders a Python bool; anything else is not this format."""
    plan = _plan()
    lines = _csv_lines(planner.render_plan_csv(plan))
    lines = _rewrite_cell(lines, "archive_hashes", "bound", raw)

    json_path, csv_path = _write(
        tmp_path, plan, csv_text="\r\n".join(lines)
    )

    with pytest.raises(PlanArtifactError, match="expected 'True' or 'False'"):
        read_plan_artifacts(json_path, csv_path)


def test_a_bound_column_contradicting_its_sides_is_refused(
    tmp_path: Path,
) -> None:
    """The column is parsed, the fact is derived, and the two must agree.

    `archive_content_signatures` row 2 is unresolved, so its sides derive
    `bound=False`. Claiming `True` is a file contradicting the digest its
    own envelope carries, and accepting the column would let it pass.
    """
    plan = _plan()
    lines = _csv_lines(planner.render_plan_csv(plan))
    lines = _rewrite_cell(
        lines, "archive_content_signatures", "bound", "True"
    )

    json_path, csv_path = _write(
        tmp_path, plan, csv_text="\r\n".join(lines)
    )

    with pytest.raises(PlanArtifactError, match="claims bound=True"):
        read_plan_artifacts(json_path, csv_path)


def test_a_duplicate_table_and_key_is_refused(tmp_path: Path) -> None:
    """015 stages the plan under PRIMARY KEY (table_name, row_id).

    A duplicate could not be staged, so one of the two would be lost --
    and which one would be decided by insertion order rather than by
    anything an operator approved.
    """
    plan = _plan()
    lines = _csv_lines(planner.render_plan_csv(plan))
    index = _row_index(lines, "archive_hashes")
    lines.insert(index + 1, lines[index])

    json_path, csv_path = _write(
        tmp_path, plan, csv_text="\r\n".join(lines)
    )

    with pytest.raises(PlanArtifactError, match="was already read at line"):
        read_plan_artifacts(json_path, csv_path)


@pytest.mark.parametrize(
    "column, value",
    [
        ("planner_version", "provenance-backfill-planner/1"),
        ("snapshot_digest", "f" * 64),
        ("plan_digest", "e" * 64),
    ],
)
def test_a_row_disagreeing_with_the_envelope_is_refused(
    tmp_path: Path, column: str, value: str
) -> None:
    """Rows spliced in from another plan parse perfectly.

    Each row repeats the envelope's identity, so the repetition is checked
    rather than ignored -- otherwise a row lifted from a different plan
    would be reconciled against this envelope.
    """
    plan = _plan()
    lines = _csv_lines(planner.render_plan_csv(plan))
    lines = _rewrite_cell(lines, "archive_hashes", column, value)

    json_path, csv_path = _write(
        tmp_path, plan, csv_text="\r\n".join(lines)
    )

    with pytest.raises(PlanArtifactError, match="but the envelope records"):
        read_plan_artifacts(json_path, csv_path)


def test_a_planner_invariant_violation_is_reported_as_an_artifact_error(
    tmp_path: Path,
) -> None:
    """A bound basis with no revision is the planner's paired invariant.

    Re-raised as a `PlanArtifactError` naming the line: a
    `PlannerInvariantError` escaping the reader would read as a classifier
    defect, when what actually happened is that a file on disk is wrong.
    """
    plan = _plan()
    lines = _csv_lines(planner.render_plan_csv(plan))
    lines = _rewrite_cell(lines, "archive_hashes", "source_revision_id", "")
    lines = _rewrite_cell(lines, "archive_hashes", "bound", "False")

    json_path, csv_path = _write(
        tmp_path, plan, csv_text="\r\n".join(lines)
    )

    with pytest.raises(PlanArtifactError, match="bound basis"):
        read_plan_artifacts(json_path, csv_path)


def test_a_basis_outside_the_tables_vocabulary_is_refused(
    tmp_path: Path,
) -> None:
    """`stat_matched_revision` is legal for signatures, never for hashes."""
    plan = _plan()
    lines = _csv_lines(planner.render_plan_csv(plan))
    lines = _rewrite_cell(
        lines, "archive_hashes", "provenance_basis",
        planner.STAT_MATCHED,
    )

    json_path, csv_path = _write(
        tmp_path, plan, csv_text="\r\n".join(lines)
    )

    with pytest.raises(PlanArtifactError, match="not in that table's"):
        read_plan_artifacts(json_path, csv_path)


# --- totals and the digest backstop --------------------------------------


def test_totals_disagreeing_with_the_bindings_are_refused(
    tmp_path: Path,
) -> None:
    """Recounted with the planner's own function, not one written here.

    Two independent recounts would be two definitions of a total, and the
    reader would then be able to accept a plan the planner would reject.
    """
    plan = _plan()
    csv_text = planner.render_plan_csv(plan)
    envelope = _envelope_of(plan, csv_text)
    envelope["totals"]["planned_rows"] += 1

    json_path, csv_path = _write(
        tmp_path, plan, csv_text=csv_text, envelope=envelope
    )

    with pytest.raises(PlanArtifactError, match="totals that do not match"):
        read_plan_artifacts(json_path, csv_path)


def test_an_envelope_digest_that_does_not_match_is_refused(
    tmp_path: Path,
) -> None:
    """The last check, and the one that makes the rest safe to rely on."""
    plan = _plan()
    csv_text = planner.render_plan_csv(plan)
    envelope = _envelope_of(plan, csv_text)
    envelope["plan_digest"] = "0" * 64

    # Every row repeats the digest, so they move together -- otherwise the
    # row-consistency check fires first and this test proves nothing about
    # the digest.
    csv_text = csv_text.replace(plan.plan_digest, "0" * 64)
    envelope["artifacts"]["csv_sha256"] = hashlib.sha256(
        csv_text.encode("utf-8")
    ).hexdigest()

    json_path, csv_path = _write(
        tmp_path, plan, csv_text=csv_text, envelope=envelope
    )

    with pytest.raises(PlanArtifactError, match="not the same plan"):
        read_plan_artifacts(json_path, csv_path)


def test_a_genuinely_empty_text_value_fails_the_digest(
    tmp_path: Path,
) -> None:
    """The residual ambiguity fails closed rather than being misread.

    The CSV cannot distinguish `None` from `""` for a text artifact column.
    No planner path emits `""` today. This builds the plan that would --
    an inspection whose `inspector_version_basis` is the empty string --
    and shows it is refused at the digest rather than silently read back
    as `None`, which is what makes the limitation safe to live with.
    """
    rows = [
        binding for binding in _bindings()
        if binding.table != "archive_inspections"
    ]
    rows.append(
        planner.PlannedBinding(
            table="archive_inspections",
            key=3,
            key_kind="row_id",
            archive_id=10,
            sides=(_side("", 10, None, planner.UNRESOLVED_NO_IDENTITY),),
            values={
                "inspector_version": "",
                "inspector_version_basis": "unknown_legacy",
            },
        )
    )

    plan = _plan(rows)
    json_path, csv_path = _write(tmp_path, plan)

    with pytest.raises(PlanArtifactError, match="not the same plan"):
        read_plan_artifacts(json_path, csv_path)


# --- gate failures, reconstructed ----------------------------------------


def _measured_plan() -> planner.BackfillPlan:
    """A plan whose classifier reached a producer-only basis.

    `measured` is in `archive_hashes`' vocabulary but the backfill re-reads
    nothing, so a row planned that way means the classifier took a path it
    should not have -- exactly the condition `gate_failures` reports.
    """
    rows = [
        planner.PlannedBinding(
            table="archive_hashes",
            key=1,
            key_kind="row_id",
            archive_id=10,
            sides=(_side("", 10, 7, planner.MEASURED),),
            values={},
        )
    ]

    return _plan(rows)


def test_a_plan_with_a_producer_basis_reports_a_gate_failure(
    tmp_path: Path,
) -> None:
    """The precondition for the forgery test below: the failure is real."""
    plan = _measured_plan()

    assert plan.gate_failures == (
        "1 row(s) planned as measured, which only a producer can establish",
    )

    json_path, csv_path = _write(tmp_path, plan)
    loaded = read_plan_artifacts(json_path, csv_path)

    # A tuple, not a list: the verified envelope is deeply frozen, so every
    # sequence in it comes back immutable.
    assert loaded.envelope["gate_failures"] == tuple(plan.gate_failures)


def test_gate_failures_forged_empty_are_refused(tmp_path: Path) -> None:
    """The field that says whether a plan should be applied at all.

    Emptying it presents a plan carrying producer-only bases as clean, and
    every other check still passes: the bindings, the totals, the CSV
    digest and the plan digest are all untouched.
    """
    plan = _measured_plan()
    csv_text = planner.render_plan_csv(plan)
    envelope = _envelope_of(plan, csv_text)
    envelope["gate_failures"] = []

    json_path, csv_path = _write(
        tmp_path, plan, csv_text=csv_text, envelope=envelope
    )

    with pytest.raises(PlanArtifactError, match="do not imply"):
        read_plan_artifacts(json_path, csv_path)


def test_gate_failures_forged_nonempty_are_refused(tmp_path: Path) -> None:
    """Both directions: an invented failure is a claim too."""
    plan = _plan()
    csv_text = planner.render_plan_csv(plan)
    envelope = _envelope_of(plan, csv_text)
    envelope["gate_failures"] = ["forged failure"]

    json_path, csv_path = _write(
        tmp_path, plan, csv_text=csv_text, envelope=envelope
    )

    with pytest.raises(PlanArtifactError, match="do not imply"):
        read_plan_artifacts(json_path, csv_path)


def test_a_hidden_unclassifiable_archive_is_refused(tmp_path: Path) -> None:
    """The archive-count arm of the reconstruction, driven alone.

    `archives_without_revision` is itself unverifiable, but it still
    constrains `gate_failures` -- so raising the count while leaving the
    failure list empty is caught even though the count could not have been
    checked on its own.
    """
    plan = _plan()
    csv_text = planner.render_plan_csv(plan)
    envelope = _envelope_of(plan, csv_text)
    envelope["archive_gates"]["archives_without_revision"] = 3

    json_path, csv_path = _write(
        tmp_path, plan, csv_text=csv_text, envelope=envelope
    )

    with pytest.raises(PlanArtifactError, match="do not imply"):
        read_plan_artifacts(json_path, csv_path)


@pytest.mark.parametrize(
    "bases, without_revision",
    [
        ((), 0),
        ((planner.MEASURED,), 0),
        ((planner.MEASURED, planner.MEASURED), 0),
        ((), 4),
        ((planner.MEASURED,), 2),
    ],
)
def test_the_gate_failure_reconstruction_matches_the_planner(
    bases: tuple, without_revision: int
) -> None:
    """Pins the duplicated logic to the definition it duplicates.

    `reconstruct_gate_failures()` reimplements `BackfillPlan.gate_failures`
    rather than calling it, because reaching that property needs a
    `BackfillPlan` and the envelope carries only a COUNT of archives
    without a revision, not their ids -- so calling it would mean
    fabricating ids to make a check pass. This test is what keeps the two
    from drifting: both run over the same plans and must agree exactly.
    """
    rows = [
        planner.PlannedBinding(
            table="archive_hashes",
            key=index + 1,
            key_kind="row_id",
            archive_id=10 + index,
            sides=(_side("", 10 + index, 7, basis),),
            values={},
        )
        for index, basis in enumerate(bases)
    ]

    plan = planner.BackfillPlan(
        planner_version=planner.PLANNER_VERSION,
        snapshot_digest=SNAPSHOT_DIGEST,
        plan_digest=planner.compute_plan_digest(rows, SNAPSHOT_DIGEST),
        bindings=tuple(rows),
        totals=planner.plan_totals(rows),
        gates=planner.ArchiveGate(
            (), tuple(range(900, 900 + without_revision)), ()
        ),
        quarantine_rows=0,
    )

    assert list(plan.gate_failures) == reconstruct_gate_failures(
        plan.totals, without_revision
    )


# --- the verified result is deeply immutable -----------------------------


def test_the_verified_envelope_cannot_be_mutated(tmp_path: Path) -> None:
    """A frozen dataclass freezes field bindings, not the objects behind them.

    The reviewed revision returned a plain dict, so an envelope value could
    be rewritten after verification while `plan_digest` went on reporting
    the digest of what had been checked.
    """
    json_path, csv_path = _write(tmp_path)
    loaded = read_plan_artifacts(json_path, csv_path)

    with pytest.raises(TypeError):
        loaded.envelope["plan_digest"] = "0" * 64

    with pytest.raises(TypeError):
        loaded.envelope["totals"]["planned_rows"] = 999

    with pytest.raises(TypeError):
        loaded.unverified["archive_gates"]["drift_archives"] = 99


def test_a_verified_bindings_values_cannot_be_mutated(
    tmp_path: Path,
) -> None:
    """The reproduction from review, asserted as a regression.

    Previously: mutate `values["inspector_version"]`, and the LoadedPlan
    kept reporting the old digest while a recomputation produced a
    different one.
    """
    json_path, csv_path = _write(tmp_path)
    loaded = read_plan_artifacts(json_path, csv_path)
    inspection = next(
        b for b in loaded.bindings if b.table == "archive_inspections"
    )

    with pytest.raises(TypeError):
        inspection.values["inspector_version"] = "FORGED"

    # And the digest the result carries still describes the bindings it
    # carries -- which is the property the mutation broke.
    assert planner.compute_plan_digest(
        list(loaded.bindings), loaded.snapshot_digest
    ) == loaded.plan_digest


def test_nested_envelope_sequences_are_frozen(tmp_path: Path) -> None:
    """Lists become tuples all the way down, not just at the top level."""
    json_path, csv_path = _write(tmp_path)
    loaded = read_plan_artifacts(json_path, csv_path)

    assert isinstance(loaded.envelope["target_states"], tuple)
    assert isinstance(loaded.unverified["archive_gates"], Mapping)
    assert isinstance(
        loaded.unverified["archive_gates"]["drift_archive_ids"], tuple
    )

    with pytest.raises(TypeError):
        loaded.envelope["table_vocabulary"]["archive_hashes"] = ()


def test_the_unverified_fields_are_segregated(tmp_path: Path) -> None:
    """4B-2 must not be able to reach for "the envelope" and get both.

    The two census figures cannot be reconstructed from the bindings -- an
    archive that produced no binding is exactly what they report -- so they
    are handed over separately and labelled rather than mixed in.
    """
    json_path, csv_path = _write(tmp_path)
    loaded = read_plan_artifacts(json_path, csv_path)

    assert set(loaded.unverified) == UNVERIFIED_ENVELOPE_FIELDS
    assert not (set(loaded.envelope) & UNVERIFIED_ENVELOPE_FIELDS)
    assert (
        set(loaded.envelope) | set(loaded.unverified)
        == EXPECTED_ENVELOPE_FIELDS
    )


# --- the CSV must be the writer's canonical form -------------------------


def test_malformed_quoting_fails_at_parse(tmp_path: Path) -> None:
    """`"x"junk` decoded as `xjunk` with every later check still passing.

    The raw CSV hash, the reconstructed binding, the totals and the plan
    digest were all valid, because the malformed field decoded to the exact
    value the plan expected. `strict=True` makes it a parse error instead.
    """
    rows = [
        binding for binding in _bindings()
        if binding.table != "archive_inspections"
    ]
    rows.append(
        planner.PlannedBinding(
            table="archive_inspections",
            key=3,
            key_kind="row_id",
            archive_id=10,
            sides=(_side("", 10, None, planner.UNRESOLVED_NO_IDENTITY),),
            values={
                "inspector_version": "xjunk",
                "inspector_version_basis": "unknown_legacy",
            },
        )
    )

    plan = _plan(rows)
    csv_text = planner.render_plan_csv(plan)
    malformed = csv_text.replace(",xjunk,", ',"x"junk,')
    assert malformed != csv_text

    json_path, csv_path = _write(tmp_path, plan, csv_text=malformed)

    with pytest.raises(PlanArtifactError, match="not valid CSV"):
        read_plan_artifacts(json_path, csv_path)


@pytest.mark.parametrize(
    "label, old, new",
    [
        ("needless quoting", ",xjunk,", ',"xjunk",'),
        ("lf terminators", "\r\n", "\n"),
    ],
)
def test_a_non_canonical_csv_is_refused(
    tmp_path: Path, label: str, old: str, new: str
) -> None:
    """Parses cleanly, decodes to the right values, is still not the file.

    `strict=True` catches malformed syntax; this catches everything that is
    merely not what `render_plan_csv()` would have produced. The digest is
    recomputed over the altered text on purpose, so the raw-hash check
    cannot fire and only the canonical-form check can.
    """
    rows = [
        binding for binding in _bindings()
        if binding.table != "archive_inspections"
    ]
    rows.append(
        planner.PlannedBinding(
            table="archive_inspections",
            key=3,
            key_kind="row_id",
            archive_id=10,
            sides=(_side("", 10, None, planner.UNRESOLVED_NO_IDENTITY),),
            values={
                "inspector_version": "xjunk",
                "inspector_version_basis": "unknown_legacy",
            },
        )
    )

    plan = _plan(rows)
    altered = planner.render_plan_csv(plan).replace(old, new)

    json_path, csv_path = _write(tmp_path, plan, csv_text=altered)

    with pytest.raises(PlanArtifactError, match="canonical CSV form"):
        read_plan_artifacts(json_path, csv_path)


@pytest.mark.parametrize(
    "awkward",
    ["has,comma", 'has"quote', "has\nnewline", "has\r\ncrlf",
     "  padded  ", "semi;colon\ttab"],
)
def test_the_canonical_check_does_not_reject_awkward_values(
    tmp_path: Path, awkward: str
) -> None:
    """The canonical check must refuse non-canonical FILES, not odd VALUES.

    A value carrying a delimiter, a quote or an embedded newline is quoted
    by the writer and re-quoted identically on the round trip, so each of
    these must still verify. Without this, the check could pass its own
    negative tests while rejecting a legitimate plan.
    """
    rows = [
        binding for binding in _bindings()
        if binding.table != "archive_inspections"
    ]
    rows.append(
        planner.PlannedBinding(
            table="archive_inspections",
            key=3,
            key_kind="row_id",
            archive_id=10,
            sides=(_side("", 10, None, planner.UNRESOLVED_NO_IDENTITY),),
            values={
                "inspector_version": awkward,
                "inspector_version_basis": "unknown_legacy",
            },
        )
    )

    plan = _plan(rows)
    json_path, csv_path = _write(tmp_path, plan)

    loaded = read_plan_artifacts(json_path, csv_path)
    inspection = next(
        b for b in loaded.bindings if b.table == "archive_inspections"
    )

    assert inspection.values["inspector_version"] == awkward
    assert loaded.plan_digest == plan.plan_digest


# --- IO ------------------------------------------------------------------


def test_a_missing_envelope_is_refused(tmp_path: Path) -> None:
    json_path, csv_path = _write(tmp_path)
    json_path.unlink()

    with pytest.raises(PlanArtifactError, match="could not read"):
        read_plan_artifacts(json_path, csv_path)


def test_a_missing_bindings_file_is_refused(tmp_path: Path) -> None:
    json_path, csv_path = _write(tmp_path)
    csv_path.unlink()

    with pytest.raises(PlanArtifactError, match="could not read"):
        read_plan_artifacts(json_path, csv_path)


def test_a_non_utf8_bindings_file_is_refused(tmp_path: Path) -> None:
    """Caught after the digest, which is over raw bytes and cannot decode."""
    plan = _plan()
    csv_text = planner.render_plan_csv(plan)
    envelope = _envelope_of(plan, csv_text)
    raw = csv_text.encode("utf-8").replace(b"archive_hashes", b"archive_h\xff")
    envelope["artifacts"]["csv_sha256"] = hashlib.sha256(raw).hexdigest()

    json_path = tmp_path / "plan.json"
    csv_path = tmp_path / "plan.csv"
    csv_path.write_bytes(raw)
    json_path.write_bytes(
        (json.dumps(envelope, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )

    with pytest.raises(PlanArtifactError, match="not valid UTF-8"):
        read_plan_artifacts(json_path, csv_path)
