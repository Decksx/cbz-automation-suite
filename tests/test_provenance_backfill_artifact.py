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

import pytest

from comic_automation.archive import provenance_backfill_planner as planner
from comic_automation.archive.provenance_backfill_artifact import (
    PlanArtifactError,
    read_plan_artifacts,
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


@pytest.mark.parametrize(
    "field",
    ["planner_version", "snapshot_digest", "plan_digest", "totals",
     "artifacts"],
)
def test_a_missing_envelope_field_is_refused(
    tmp_path: Path, field: str
) -> None:
    plan = _plan()
    csv_text = planner.render_plan_csv(plan)
    envelope = _envelope_of(plan, csv_text)
    del envelope[field]

    json_path, csv_path = _write(
        tmp_path, plan, csv_text=csv_text, envelope=envelope
    )

    with pytest.raises(PlanArtifactError, match="is missing"):
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
