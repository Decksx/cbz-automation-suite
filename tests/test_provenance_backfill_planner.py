"""Tests for the read-only provenance backfill planner (Step 4, slice 3).

The fixture is a miniature of production's shape rather than a copy of its
size: one archive per interesting case, so a classification error is visible
by inspection instead of by arithmetic over 238,956 rows.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from comic_automation.archive import provenance_backfill_planner as planner
from comic_automation.archive.provenance_backfill_planner import (
    FIELD_SEED,
    FIRST_PAGE_PERSISTENCE,
    IDENTITY_SEED,
    PlannedBinding,
    PlannerInvariantError,
    OutputPathError,
    SIGNATURE_CALCULATED_AT,
    SINGLE_REVISION_INHERITED,
    UNRESOLVED_DRIFT,
    build_plan,
    plan_totals,
    write_plan_csv,
    write_plan_json,
)

SCHEMA = """
CREATE TABLE archive_files(id INTEGER PRIMARY KEY, current_revision_id INTEGER);
CREATE TABLE file_locations(
  id INTEGER PRIMARY KEY, archive_id INTEGER NOT NULL, path TEXT NOT NULL,
  is_current INTEGER NOT NULL DEFAULT 1, file_size INTEGER,
  modified_time_ns INTEGER);
CREATE TABLE archive_revisions(
  id INTEGER PRIMARY KEY, archive_id INTEGER NOT NULL,
  revision_ordinal INTEGER NOT NULL DEFAULT 1,
  identity_state TEXT NOT NULL DEFAULT 'established',
  archive_sha256 TEXT);
CREATE TABLE archive_hashes(
  id INTEGER PRIMARY KEY, archive_id INTEGER NOT NULL, digest TEXT NOT NULL);
CREATE TABLE archive_content_signatures(
  id INTEGER PRIMARY KEY, archive_id INTEGER NOT NULL, digest TEXT NOT NULL,
  page_count INTEGER NOT NULL, location_id INTEGER,
  source_file_size INTEGER NOT NULL,
  calculated_at TEXT NOT NULL DEFAULT '2026-07-27 00:00:00');
CREATE TABLE archive_inspections(
  id INTEGER PRIMARY KEY, archive_id INTEGER NOT NULL);
CREATE TABLE archive_pages(
  id INTEGER PRIMARY KEY, archive_id INTEGER NOT NULL, page_index INTEGER NOT NULL,
  location_id INTEGER, created_at TEXT NOT NULL);
CREATE TABLE near_duplicate_candidates(
  id INTEGER PRIMARY KEY, archive_a_id INTEGER NOT NULL,
  archive_b_id INTEGER NOT NULL);
CREATE TABLE archive_quarantine(
  id INTEGER PRIMARY KEY, archive_id INTEGER NOT NULL);
"""


def _archive(c, archive_id, *, pages=2, size=1000, signature_size=None,
             provisional=False, revision=True, zero_page=False):
    """One archive with a full evidence set.

    `signature_size` differing from `size` is what makes an archive drift:
    the signature describes bytes of one length while the file on disk now
    has another.
    """
    c.execute("INSERT INTO archive_files VALUES(?,NULL)", (archive_id,))
    c.execute(
        "INSERT INTO file_locations(id,archive_id,path,is_current,file_size) "
        "VALUES(?,?,?,1,?)",
        (archive_id, archive_id, "/a/%d.cbz" % archive_id, size),
    )

    if revision:
        c.execute(
            "INSERT INTO archive_revisions(id,archive_id,identity_state,archive_sha256)"
            " VALUES(?,?,?,?)",
            (archive_id * 10, archive_id,
             "provisional" if provisional else "established",
             None if provisional else "d%064d" % archive_id),
        )

    if provisional:
        # Slice 1 §7.3: a provisional archive's only evidence is job rows.
        return

    c.execute("INSERT INTO archive_hashes VALUES(?,?,?)",
              (archive_id, archive_id, "h%d" % archive_id))
    c.execute("INSERT INTO archive_inspections VALUES(?,?)", (archive_id, archive_id))
    c.execute(
        "INSERT INTO archive_content_signatures"
        "(id,archive_id,digest,page_count,location_id,source_file_size) "
        "VALUES(?,?,?,?,?,?)",
        (archive_id, archive_id, "sig%d" % archive_id,
         0 if zero_page else pages, archive_id,
         signature_size if signature_size is not None else size),
    )

    if not zero_page:
        for index in range(pages):
            c.execute(
                "INSERT INTO archive_pages(id,archive_id,page_index,location_id,"
                "created_at) VALUES(?,?,?,?,?)",
                (archive_id * 100 + index, archive_id, index, archive_id,
                 "2026-07-27 12:00:0%d" % index),
            )


@pytest.fixture()
def db():
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA)
    _archive(connection, 1, pages=2)                       # ordinary
    _archive(connection, 2, pages=3)                       # ordinary
    _archive(connection, 3, pages=1, signature_size=500)   # drift
    _archive(connection, 4, zero_page=True)                # zero-page result
    _archive(connection, 5, provisional=True)              # provisional gate
    connection.execute("INSERT INTO near_duplicate_candidates VALUES(1,1,2)")
    connection.execute("INSERT INTO archive_quarantine VALUES(1,1)")
    return connection


def test_every_receiving_table_is_planned_and_quarantine_is_not(db):
    plan = build_plan(db)
    tables = {b.table for b in plan.bindings}
    assert tables == set(planner.RECEIVING_TABLES)
    assert "archive_quarantine" not in tables
    # The rows exist and are deliberately excluded, so the count is reported
    # rather than the table simply being absent.
    assert plan.quarantine_rows == 1


def test_hashes_are_the_only_identity_seed(db):
    plan = build_plan(db)
    seeds = {b.table for b in plan.bindings if b.provenance_basis == IDENTITY_SEED}
    assert seeds == {"archive_hashes"}
    assert all(
        b.provenance_basis == IDENTITY_SEED
        for b in plan.bindings
        if b.table == "archive_hashes"
    )


def test_signatures_are_field_seeds_except_where_they_drift(db):
    plan = build_plan(db)
    by_archive = {
        b.archive_id: b for b in plan.bindings
        if b.table == "archive_content_signatures"
    }
    assert by_archive[1].provenance_basis == FIELD_SEED
    assert by_archive[1].source_revision_id == 10
    assert by_archive[3].provenance_basis == UNRESOLVED_DRIFT
    assert by_archive[3].source_revision_id is None


def test_page_inventory_is_planned_by_archive_not_by_page_row(db):
    plan = build_plan(db)
    inventories = [b for b in plan.bindings if b.table == "page_inventory"]
    # four archives with an extraction result: 1, 2, 3, 4 -- not the six page
    # rows they hold between them
    assert len(inventories) == 4
    assert all(b.key_kind == "archive_id" for b in inventories)
    assert {b.key for b in inventories} == {1, 2, 3, 4}


def test_zero_page_inventory_takes_the_signature_timestamp(db):
    plan = build_plan(db)
    zero = next(
        b for b in plan.bindings
        if b.table == "page_inventory" and b.archive_id == 4
    )
    assert zero.values["page_count"] == 0
    assert zero.values["extracted_at_basis"] == SIGNATURE_CALCULATED_AT
    assert zero.values["extracted_at"] == "2026-07-27 00:00:00"
    assert zero.values["location_id"] == 4


def test_populated_inventory_takes_the_first_child_timestamp(db):
    plan = build_plan(db)
    populated = next(
        b for b in plan.bindings
        if b.table == "page_inventory" and b.archive_id == 2
    )
    assert populated.values["page_count"] == 3
    assert populated.values["extracted_at_basis"] == FIRST_PAGE_PERSISTENCE
    # MIN over the children, not MAX -- slice 2 §4.2
    assert populated.values["extracted_at"] == "2026-07-27 12:00:00"


def test_no_producer_basis_is_ever_planned(db):
    plan = build_plan(db)
    assert plan.totals["per_basis"][planner.MEASURED] == 0
    assert plan.totals["per_basis"][planner.STAT_MATCHED] == 0
    assert plan.totals["per_basis"][planner.INHERITED_FROM_PAGE_EVIDENCE] == 0
    assert plan.gate_failures == ()


def test_provisional_archive_is_a_gate_not_a_row(db):
    plan = build_plan(db)
    assert plan.gates.provisional_archives == (5,)
    assert not any(b.archive_id == 5 for b in plan.bindings)


def test_drift_archives_are_reported_as_a_population(db):
    plan = build_plan(db)
    assert plan.gates.drift_archives == (3,)


def test_totals_reconcile_and_match_the_bindings(db):
    plan = build_plan(db)
    assert plan.totals["planned_rows"] == len(plan.bindings)
    assert plan.totals["bound"] + plan.totals["unresolved"] == len(plan.bindings)
    per_table = plan.totals["per_table"]
    assert per_table["archive_hashes"]["rows"] == 4
    assert per_table["archive_content_signatures"]["rows"] == 4
    assert per_table["archive_inspections"]["rows"] == 4
    assert per_table["page_inventory"]["rows"] == 4
    assert per_table["near_duplicate_candidates"]["rows"] == 1


def test_the_digest_is_stable_across_identical_reads(db):
    assert build_plan(db).snapshot_digest == build_plan(db).snapshot_digest


def test_the_digest_moves_when_an_input_moves(db):
    before = build_plan(db).snapshot_digest
    db.execute("INSERT INTO near_duplicate_candidates VALUES(2,1,3)")
    assert build_plan(db).snapshot_digest != before


def test_the_digest_moves_when_only_a_drift_classification_would(db):
    """A file resized on disk changes no evidence row, but changes the plan."""
    before = build_plan(db).snapshot_digest
    db.execute("UPDATE file_locations SET file_size = 999 WHERE archive_id = 1")
    after = build_plan(db)
    assert after.snapshot_digest != before
    signature = next(
        b for b in after.bindings
        if b.table == "archive_content_signatures" and b.archive_id == 1
    )
    assert signature.provenance_basis == UNRESOLVED_DRIFT


def test_bindings_are_emitted_in_a_deterministic_order(db):
    order = [(b.table, b.key) for b in build_plan(db).bindings]
    assert order == sorted(order)


# --- the guards, proven load-bearing by bypassing them ---------------------


def test_a_bound_basis_without_a_revision_is_refused():
    with pytest.raises(PlannerInvariantError, match="with no revision"):
        PlannedBinding(
            table="archive_hashes", key=1, key_kind="row_id", archive_id=1,
            source_revision_id=None, provenance_basis=IDENTITY_SEED,
        )


def test_an_unresolved_basis_carrying_a_revision_is_refused():
    with pytest.raises(PlannerInvariantError, match="carries revision"):
        PlannedBinding(
            table="archive_content_signatures", key=1, key_kind="row_id",
            archive_id=1, source_revision_id=10,
            provenance_basis=UNRESOLVED_DRIFT,
        )


def test_a_basis_outside_the_tables_vocabulary_is_refused():
    """`measured` is in the global union but legal only on archive_hashes."""
    with pytest.raises(PlannerInvariantError, match="not in that table's vocabulary"):
        PlannedBinding(
            table="archive_inspections", key=1, key_kind="row_id", archive_id=1,
            source_revision_id=10, provenance_basis=planner.MEASURED,
        )


def test_unreconciled_totals_are_refused_rather_than_returned(monkeypatch):
    """Bypass the classifier's correctness and confirm plan_totals catches it."""
    good = PlannedBinding(
        table="archive_hashes", key=1, key_kind="row_id", archive_id=1,
        source_revision_id=10, provenance_basis=IDENTITY_SEED,
    )
    # object.__setattr__ defeats the frozen dataclass, which is the only way
    # to construct the inconsistency __post_init__ exists to prevent.
    broken = PlannedBinding(
        table="archive_hashes", key=2, key_kind="row_id", archive_id=2,
        source_revision_id=10, provenance_basis=IDENTITY_SEED,
    )
    object.__setattr__(broken, "provenance_basis", "not_a_basis")

    with pytest.raises(PlannerInvariantError, match="unknown basis"):
        plan_totals([good, broken])


def test_more_than_one_revision_per_archive_stops_the_planner(db):
    """The page-inventory key is archive_id, which is unique only while each
    archive holds one revision. A second revision must stop the plan rather
    than silently binding to whichever came first."""
    db.execute(
        "INSERT INTO archive_revisions(id,archive_id,revision_ordinal,"
        "identity_state,archive_sha256) VALUES(999,1,2,'established','x')"
    )
    with pytest.raises(PlannerInvariantError, match="more than one revision"):
        build_plan(db)


def test_an_archive_without_a_revision_is_a_gate_failure(db):
    db.execute("INSERT INTO archive_files VALUES(77,NULL)")
    plan = build_plan(db)
    assert plan.gates.archives_without_revision == (77,)
    assert any("no revision row" in failure for failure in plan.gate_failures)


# --- artifacts ------------------------------------------------------------


def test_json_and_csv_are_written_deterministically(db, tmp_path):
    plan = build_plan(db)
    first = write_plan_json(plan, tmp_path / "a.json")
    second = write_plan_json(plan, tmp_path / "b.json")
    assert first.read_bytes() == second.read_bytes()

    csv_a = write_plan_csv(plan, tmp_path / "a.csv")
    csv_b = write_plan_csv(plan, tmp_path / "b.csv")
    assert csv_a.read_bytes() == csv_b.read_bytes()

    envelope = json.loads(first.read_text(encoding="utf-8"))
    assert envelope["execution_status"] == "not_performed"
    assert envelope["snapshot_digest"] == plan.snapshot_digest
    assert envelope["target_states"] == ["sealed"]
    # every CSV row carries the digest, so a detached CSV can still be tied
    # back to the envelope that approved it
    assert plan.snapshot_digest in csv_a.read_text(encoding="utf-8")


def test_an_existing_output_is_never_overwritten(db, tmp_path):
    plan = build_plan(db)
    target = tmp_path / "plan.json"
    write_plan_json(plan, target)
    with pytest.raises(OutputPathError, match="overwrite"):
        write_plan_json(plan, target)


def test_writing_beside_the_database_is_refused(db, tmp_path):
    plan = build_plan(db)
    database = tmp_path / "inspection.db"
    database.write_bytes(b"")
    with pytest.raises(OutputPathError, match="beside the database"):
        write_plan_json(plan, tmp_path / "plan.json", database=database)


def test_a_miscounting_accumulator_is_caught_by_the_recount(monkeypatch):
    """The reconciliation checks the accumulation against an independent
    recount, so a bug in the loop is visible rather than self-consistent."""
    bindings = [
        PlannedBinding(
            table="archive_hashes", key=n, key_kind="row_id", archive_id=n,
            source_revision_id=n * 10, provenance_basis=IDENTITY_SEED,
        )
        for n in (1, 2, 3)
    ]
    assert plan_totals(bindings)["planned_rows"] == 3

    # Simulate an accumulator that drops a row: Counter sees three, the
    # bucket sees two.
    real_counter = planner.Counter

    class _ShortCounter(real_counter):
        def get(self, key, default=None):
            value = super().get(key, default)
            return value + 1 if isinstance(value, int) else value

    monkeypatch.setattr(planner, "Counter", _ShortCounter)

    with pytest.raises(PlannerInvariantError, match="recount says"):
        plan_totals(bindings)


def test_a_binding_naming_an_unknown_table_is_refused():
    good = PlannedBinding(
        table="archive_hashes", key=1, key_kind="row_id", archive_id=1,
        source_revision_id=10, provenance_basis=IDENTITY_SEED,
    )
    object.__setattr__(good, "table", "not_a_table")
    with pytest.raises(PlannerInvariantError, match="not a receiving table"):
        plan_totals([good])
