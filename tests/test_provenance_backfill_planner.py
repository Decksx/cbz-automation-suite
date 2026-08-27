"""Tests for the read-only provenance backfill planner (Step 4, slice 3).

The fixture is a miniature of production's shape rather than a copy of its
size: one archive per interesting case, so a classification error is visible
by inspection instead of by arithmetic over 238,956 rows.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sqlite3

import pytest

from pathlib import Path

from comic_automation.archive import output_guards
from comic_automation.archive import provenance_backfill_planner as planner
from comic_automation.archive.provenance_backfill_planner import (
    SideAttribution,
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
  archive_sha256 TEXT, content_signature TEXT);
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
        # Migration 014 seeded archive_sha256 from archive_hashes.digest and
        # content_signature from archive_content_signatures.digest, so the
        # revision here carries the SAME values the evidence rows below do.
        # An earlier fixture gave the revision unrelated digests, which made
        # every seed binding a disagreement the planner now refuses -- and
        # which is why the defect was invisible from the tests.
        c.execute(
            "INSERT INTO archive_revisions(id,archive_id,identity_state,"
            "archive_sha256,content_signature) VALUES(?,?,?,?,?)",
            (archive_id * 10, archive_id,
             "provisional" if provisional else "established",
             None if provisional else "h%d" % archive_id,
             None if provisional else "sig%d" % archive_id),
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
    seeds = {
        b.table for b in plan.bindings
        if any(s.provenance_basis == IDENTITY_SEED for s in b.sides)
    }
    assert seeds == {"archive_hashes"}
    assert all(
        b.sides[0].provenance_basis == IDENTITY_SEED
        for b in plan.bindings
        if b.table == "archive_hashes"
    )


def test_signatures_are_field_seeds_except_where_they_drift(db):
    plan = build_plan(db)
    by_archive = {
        b.archive_id: b for b in plan.bindings
        if b.table == "archive_content_signatures"
    }
    assert by_archive[1].sides[0].provenance_basis == FIELD_SEED
    assert by_archive[1].sides[0].source_revision_id == 10
    assert by_archive[3].sides[0].provenance_basis == UNRESOLVED_DRIFT
    assert by_archive[3].sides[0].source_revision_id is None


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
    assert signature.sides[0].provenance_basis == UNRESOLVED_DRIFT


def test_bindings_are_emitted_in_a_deterministic_order(db):
    order = [(b.table, b.key) for b in build_plan(db).bindings]
    assert order == sorted(order)


# --- the guards, proven load-bearing by bypassing them ---------------------


def test_a_bound_basis_without_a_revision_is_refused():
    with pytest.raises(PlannerInvariantError, match="with no revision"):
        PlannedBinding(
            table="archive_hashes", key=1, key_kind="row_id", archive_id=1,
            sides=(SideAttribution("", 1, None, IDENTITY_SEED),),
        )


def test_an_unresolved_basis_carrying_a_revision_is_refused():
    with pytest.raises(PlannerInvariantError, match="carries revision"):
        PlannedBinding(
            table="archive_content_signatures", key=1, key_kind="row_id",
            archive_id=1, sides=(SideAttribution("", 1, 10, UNRESOLVED_DRIFT),),
        )


def test_a_basis_outside_the_tables_vocabulary_is_refused():
    """`measured` is in the global union but legal only on archive_hashes."""
    with pytest.raises(PlannerInvariantError, match="not in that table's vocabulary"):
        PlannedBinding(
            table="archive_inspections", key=1, key_kind="row_id", archive_id=1,
            sides=(SideAttribution("", 1, 10, planner.MEASURED),),
            values={"inspector_version": None,
                    "inspector_version_basis": "unknown_legacy"},
        )


def test_unreconciled_totals_are_refused_rather_than_returned(monkeypatch):
    """Bypass the classifier's correctness and confirm plan_totals catches it."""
    good = PlannedBinding(
        table="archive_hashes", key=1, key_kind="row_id", archive_id=1,
        sides=(SideAttribution("", 1, 10, IDENTITY_SEED),),
    )
    # object.__setattr__ defeats the frozen dataclass, which is the only way
    # to construct the inconsistency __post_init__ exists to prevent.
    broken = PlannedBinding(
        table="archive_hashes", key=2, key_kind="row_id", archive_id=2,
        sides=(SideAttribution("", 2, 10, IDENTITY_SEED),),
    )
    object.__setattr__(broken.sides[0], "provenance_basis", "not_a_basis")

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
    assert envelope["target_states"] == [
        "sealed", "created_at_from_migration_clock"]
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
            sides=(SideAttribution("", n, n * 10, IDENTITY_SEED),),
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
        sides=(SideAttribution("", 1, 10, IDENTITY_SEED),),
    )
    object.__setattr__(good, "table", "not_a_table")
    with pytest.raises(PlannerInvariantError, match="not a receiving table"):
        plan_totals([good])


# --- blocker 1: candidate sides bind independently ------------------------


def _candidate(db, archive_a, archive_b, row_id=99):
    db.execute("INSERT INTO near_duplicate_candidates VALUES(?,?,?)",
               (row_id, archive_a, archive_b))
    binding = next(
        b for b in build_plan(db).bindings
        if b.table == "near_duplicate_candidates" and b.key == row_id
    )
    return {side.label: side for side in binding.sides}, binding


def test_candidate_both_sides_bound(db):
    sides, binding = _candidate(db, 1, 2)
    assert sides["a"].source_revision_id == 10
    assert sides["b"].source_revision_id == 20
    assert sides["a"].provenance_basis == SINGLE_REVISION_INHERITED
    assert sides["b"].provenance_basis == SINGLE_REVISION_INHERITED
    assert binding.bound is True


def test_candidate_a_bound_b_unresolved(db):
    """Archive 5 is provisional, so its side cannot bind -- and A's valid
    attribution must survive that rather than being discarded with it."""
    sides, binding = _candidate(db, 1, 5)
    assert sides["a"].source_revision_id == 10
    assert sides["a"].provenance_basis == SINGLE_REVISION_INHERITED
    assert sides["b"].source_revision_id is None
    assert sides["b"].provenance_basis == planner.UNRESOLVED_NO_IDENTITY
    # The ROW is unresolved -- a comparison is only as well attributed as its
    # weaker side (slice 1 9.6) -- while A's binding is still recorded.
    assert binding.bound is False


def test_candidate_a_unresolved_b_bound(db):
    """The mirror of the case above, with the unbound side first."""
    db.execute("INSERT INTO archive_files VALUES(6,NULL)")
    db.execute(
        "INSERT INTO archive_revisions(id,archive_id,identity_state,archive_sha256)"
        " VALUES(60,6,'provisional',NULL)")
    sides, binding = _candidate(db, 5, 6, row_id=98)
    assert sides["a"].source_revision_id is None
    # rebuild with a bound B side
    sides, binding = _candidate(db, 5, 2, row_id=97)
    assert sides["a"].source_revision_id is None
    assert sides["a"].provenance_basis == planner.UNRESOLVED_NO_IDENTITY
    assert sides["b"].source_revision_id == 20
    assert sides["b"].provenance_basis == SINGLE_REVISION_INHERITED
    assert binding.bound is False


def test_candidate_both_sides_unresolved(db):
    db.execute("INSERT INTO archive_files VALUES(6,NULL)")
    db.execute(
        "INSERT INTO archive_revisions(id,archive_id,identity_state,archive_sha256)"
        " VALUES(60,6,'provisional',NULL)")
    sides, binding = _candidate(db, 5, 6)
    assert sides["a"].source_revision_id is None
    assert sides["b"].source_revision_id is None
    assert binding.bound is False


def test_sides_are_counted_separately_from_rows(db):
    plan = build_plan(db)
    # one candidate row contributes two sides
    assert plan.totals["planned_sides"] == plan.totals["planned_rows"] + 1


# --- blocker 2: the CSV carries every planned field -----------------------


def test_csv_carries_both_candidate_sides(db, tmp_path):
    plan = build_plan(db)
    path = write_plan_csv(plan, tmp_path / "p.csv")
    text = path.read_text(encoding="utf-8")
    header = text.splitlines()[0].split(",")

    for column in ("archive_b_id", "revision_a_id", "revision_b_id",
                   "provenance_basis_a", "provenance_basis_b",
                   "inspector_version", "plan_digest"):
        assert column in header, column

    candidate = next(
        line for line in text.splitlines()
        if line.startswith("near_duplicate_candidates,")
    ).split(",")
    assert candidate[header.index("archive_b_id")] != ""
    assert candidate[header.index("revision_b_id")] != ""


def test_a_binding_whose_values_do_not_match_its_table_is_refused():
    """The artifact writer can no longer drop a field, because a field it
    would drop cannot be constructed."""
    with pytest.raises(PlannerInvariantError, match="artifact columns"):
        PlannedBinding(
            table="archive_hashes", key=1, key_kind="row_id", archive_id=1,
            sides=(SideAttribution("", 1, 10, IDENTITY_SEED),),
            values={"unexpected": 1},
        )

    with pytest.raises(PlannerInvariantError, match="artifact columns"):
        PlannedBinding(
            table="archive_inspections", key=1, key_kind="row_id", archive_id=1,
            sides=(SideAttribution("", 1, 10, SINGLE_REVISION_INHERITED),),
            values={"inspector_version_basis": "unknown_legacy"},
        )


def test_inspections_plan_a_null_version_explicitly(db):
    plan = build_plan(db)
    inspection = next(b for b in plan.bindings if b.table == "archive_inspections")
    assert inspection.values["inspector_version"] is None
    assert inspection.values["inspector_version_basis"] == "unknown_legacy"


# --- blocker 3: the plan digest binds the bindings ------------------------


def test_the_plan_digest_covers_the_bindings_not_just_the_inputs(db):
    plan = build_plan(db)
    assert plan.plan_digest != plan.snapshot_digest

    altered = list(plan.bindings)
    index = next(i for i, b in enumerate(altered) if b.table == "page_inventory")
    victim = altered[index]
    tampered = dict(victim.values)
    tampered["page_count"] = 999
    altered[index] = PlannedBinding(
        table=victim.table, key=victim.key, key_kind=victim.key_kind,
        archive_id=victim.archive_id, sides=victim.sides, values=tampered,
    )
    assert (
        planner.compute_plan_digest(altered, plan.snapshot_digest)
        != plan.plan_digest
    )


def test_both_artifacts_carry_the_plan_digest(db, tmp_path):
    plan = build_plan(db)
    written = planner.write_plan_artifacts(
        plan, json_path=tmp_path / "p.json", csv_path=tmp_path / "p.csv")
    envelope = json.loads(written["json"].read_text(encoding="utf-8"))
    assert envelope["plan_digest"] == plan.plan_digest
    assert plan.plan_digest in written["csv"].read_text(encoding="utf-8")


# --- blocker 4: invalid page populations are refused ----------------------


def test_a_signature_claiming_pages_with_no_page_rows_is_refused(db):
    db.execute("UPDATE archive_content_signatures SET page_count = 2 "
               "WHERE archive_id = 4")
    with pytest.raises(PlannerInvariantError, match="no page rows exist"):
        build_plan(db)


def test_a_child_count_disagreeing_with_the_signature_is_refused(db):
    db.execute("UPDATE archive_content_signatures SET page_count = 9 "
               "WHERE archive_id = 1")
    with pytest.raises(PlannerInvariantError, match="page row"):
        build_plan(db)


def test_sparse_page_indexes_are_refused(db):
    db.execute("UPDATE archive_pages SET page_index = 7 WHERE id = 101")
    with pytest.raises(PlannerInvariantError, match="dense"):
        build_plan(db)


def test_a_mixed_location_page_set_is_refused(db):
    db.execute("UPDATE archive_pages SET location_id = 999 WHERE id = 101")
    with pytest.raises(PlannerInvariantError, match="span 2 location"):
        build_plan(db)


def test_page_location_disagreeing_with_the_signature_is_refused(db):
    db.execute("UPDATE archive_content_signatures SET location_id = 42 "
               "WHERE archive_id = 1")
    with pytest.raises(PlannerInvariantError, match="the signature at"):
        build_plan(db)


def test_the_inventory_location_comes_from_the_signature(db):
    plan = build_plan(db)
    inventory = next(
        b for b in plan.bindings
        if b.table == "page_inventory" and b.archive_id == 2
    )
    signature_location = db.execute(
        "SELECT location_id FROM archive_content_signatures WHERE archive_id = 2"
    ).fetchone()[0]
    assert inventory.values["location_id"] == signature_location


# --- blocker 5: provisional revisions never bind --------------------------


def test_evidence_under_a_provisional_revision_stays_unresolved(db):
    """Archive 5 is provisional. Give it an inspection: the plan must refuse
    to bind that to a digestless revision."""
    db.execute("INSERT INTO archive_inspections VALUES(500,5)")
    plan = build_plan(db)
    inspection = next(
        b for b in plan.bindings
        if b.table == "archive_inspections" and b.archive_id == 5
    )
    assert inspection.sides[0].source_revision_id is None
    assert inspection.sides[0].provenance_basis == planner.UNRESOLVED_NO_IDENTITY


def test_a_hash_under_a_provisional_revision_is_an_impossible_state(db):
    """archive_hashes has no unresolved basis: the hasher computes a digest
    and binds in the same transaction, so this state should not exist and
    normalising it would hide that."""
    db.execute("INSERT INTO archive_hashes VALUES(500,5,'h5')")
    with pytest.raises(PlannerInvariantError, match="no established revision"):
        build_plan(db)


# --- the artifact preflight correction ------------------------------------


def test_neither_artifact_is_written_when_one_path_is_bad(db, tmp_path):
    plan = build_plan(db)
    good = tmp_path / "p.json"
    blocked = tmp_path / "p.csv"
    blocked.write_text("already here", encoding="utf-8")

    with pytest.raises(OutputPathError, match="overwrite"):
        planner.write_plan_artifacts(plan, json_path=good, csv_path=blocked)

    # the envelope must NOT have been written first
    assert not good.exists()


def test_the_two_artifacts_must_be_different_files(db, tmp_path):
    """The simplest case of the pairwise name check: both finals identical."""
    plan = build_plan(db)
    same = tmp_path / "both"
    with pytest.raises(OutputPathError, match="are the same file"):
        planner.write_plan_artifacts(plan, json_path=same, csv_path=same)

    assert sorted(q.name for q in tmp_path.iterdir()) == []


def test_a_missing_output_directory_is_a_planner_refusal(db, tmp_path):
    plan = build_plan(db)
    with pytest.raises(OutputPathError, match="directory does not exist"):
        write_plan_json(plan, tmp_path / "nope" / "p.json")


# --- review round 2: the six blockers -------------------------------------
#
# Each of these reproduces a defect the lead demonstrated executably against
# 0e6907e. They are grouped by blocker so a later reader can tell which
# finding a test is guarding rather than inferring it from the name.


def _symlink_or_skip(link, target):
    """Create a symlink, or skip -- Windows needs a privilege for this.

    The escape being reproduced requires a real link: `..` segments resolve
    identically under a lexical and a resolved parent, so only a link
    separates the two readings. Where the privilege is absent the test cannot
    run at all, and skipping says so rather than passing vacuously.
    """
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as error:  # pragma: no cover
        pytest.skip(f"symlink creation unavailable: {error}")


# --- blocker 1: the digests ignored archive-hash values -------------------


def test_a_hash_digest_change_moves_both_digests(db):
    """Editing the hashed VALUE must invalidate a plan drawn from it.

    Before this, both digests covered only hash row ids and archive ids, so
    `archive_hashes.digest` could be rewritten and the snapshot digest and
    plan digest stayed byte-identical -- a plan approved against one state
    would apply against another without anything detecting it. The revision is
    updated alongside the hash, because the two are a seed pair and changing
    one alone is a different defect (refused below).
    """
    before = build_plan(db)
    db.execute("UPDATE archive_hashes SET digest = 'rewritten' WHERE archive_id = 1")
    db.execute("UPDATE archive_revisions SET archive_sha256 = 'rewritten' "
               "WHERE archive_id = 1")
    after = build_plan(db)

    assert after.snapshot_digest != before.snapshot_digest
    assert after.plan_digest != before.plan_digest


def test_a_hash_digest_disagreeing_with_its_revision_is_refused(db):
    """`migration_014_identity_seed` is a claim, and it is now checked.

    Migration 014 built `archive_revisions.archive_sha256` from
    `archive_hashes.digest`. Labelling the row a seed while the two disagree
    asserts a provenance the data contradicts.
    """
    db.execute("UPDATE archive_hashes SET digest = 'drifted' WHERE archive_id = 1")
    with pytest.raises(PlannerInvariantError, match="seed basis here would assert"):
        build_plan(db)


def test_a_signature_digest_disagreeing_with_its_revision_is_refused(db):
    """The same defect class on the field seed, per the review's instruction.

    `content_signature` was seeded from `archive_content_signatures.digest`
    exactly as `archive_sha256` was seeded from the hash, so the same
    disagreement makes `migration_014_field_seed` false in the same way.
    """
    db.execute("UPDATE archive_content_signatures SET digest = 'drifted' "
               "WHERE archive_id = 1")
    with pytest.raises(PlannerInvariantError, match="seed basis here would assert"):
        build_plan(db)


def test_a_seed_whose_revision_records_no_digest_is_refused(db):
    """A missing seed column is a missing relationship, not a passing check.

    Comparing against NULL would be an equality test that silently never
    fires, which is how a validation becomes decorative.
    """
    db.execute("UPDATE archive_revisions SET content_signature = NULL "
               "WHERE archive_id = 1")
    with pytest.raises(PlannerInvariantError, match="carries no content_signature"):
        build_plan(db)


# --- blocker 2: page rows with no signature vanished ----------------------


def test_page_rows_without_a_content_signature_are_refused(db):
    """Evidence must not leave the plan unreported.

    Classification iterates `signature_by_archive`, so an archive holding page
    rows but no signature produced no inventory, no gate failure and no
    mention anywhere -- the rows simply were not in the plan, and the plan
    said it had no failures.
    """
    db.execute("DELETE FROM archive_content_signatures WHERE archive_id = 1")

    with pytest.raises(PlannerInvariantError, match="page rows but no") as raised:
        build_plan(db)

    # The refusal names the archive, so an operator can go look at it.
    assert "1" in str(raised.value)


def test_the_orphan_refusal_reports_the_full_count_but_a_bounded_sample(db):
    """A refusal that pastes 58,000 ids is a refusal nobody reads."""
    for archive_id in range(100, 115):
        db.execute("INSERT INTO archive_files VALUES(?,NULL)", (archive_id,))
        db.execute(
            "INSERT INTO archive_pages(id,archive_id,page_index,location_id,"
            "created_at) VALUES(?,?,0,?,'2026-07-27 12:00:00')",
            (archive_id * 100, archive_id, archive_id),
        )

    with pytest.raises(PlannerInvariantError) as raised:
        build_plan(db)

    message = str(raised.value)
    assert "15 archive(s) hold page rows" in message
    assert message.rstrip().endswith(", ...")


# --- blocker 3: the output guard compared the lexical parent --------------


def test_a_broken_symlink_cannot_escape_into_the_database_directory(db, tmp_path):
    """The reproduction from the review, end to end.

    A link inside an allowed directory targeting a nonexistent file beside the
    database passed preflight, because `Path.parent` answered `allowed/`
    regardless of where the link pointed, and the check resolved that. The
    write then created the target next to production.

    The defect was the order, not the resolver: resolving the PATH and then
    taking its directory is correct whichever resolver does it, and resolving
    the lexical parent is wrong however carefully it is done.
    """
    plan = build_plan(db)
    database_directory = tmp_path / "prod"
    database_directory.mkdir()
    database = database_directory / "inspection.db"
    database.write_bytes(b"")
    allowed = tmp_path / "allowed"
    allowed.mkdir()

    escape = database_directory / "escaped.json"
    link = allowed / "plan.json"
    _symlink_or_skip(link, escape)

    with pytest.raises(OutputPathError, match="beside the database"):
        write_plan_json(plan, link, database=database)

    # The refusal is only meaningful if nothing was created through the link.
    assert not escape.exists()


def test_the_resolved_parent_of_a_dangling_link_is_its_target_directory(tmp_path):
    """The property the guard rests on, pinned directly.

    Both resolvers agree here, and that is worth recording because an earlier
    version of this docstring claimed they did not -- it said
    `Path.resolve(strict=False)` returned the link itself for a dangling
    link. It returns the target, on this runtime and in this test. What the
    guard actually depends on is resolving the path BEFORE taking its
    directory, and the final assertion below is the one that pins it.
    """
    target_directory = tmp_path / "elsewhere"
    target_directory.mkdir()
    link = tmp_path / "link.json"
    _symlink_or_skip(link, target_directory / "missing.json")

    assert not link.exists()          # dangling
    assert os.path.lexists(link)      # but the name is taken
    assert output_guards.resolved_parent(link) == output_guards.path_identity(
        target_directory
    )
    # Both resolvers reach the target; the claim that they differ was wrong.
    assert Path(link).resolve(strict=False) == Path(
        os.path.realpath(target_directory / "missing.json")
    )
    # The reading the old guard used, and the one that actually differs:
    # taking the parent first answers a question about the name.
    assert Path(link).parent.resolve() != Path(os.path.realpath(target_directory))


def test_an_output_path_that_is_the_database_is_refused(db, tmp_path):
    """No privilege required, and the most direct form of the same hole."""
    plan = build_plan(db)
    database = tmp_path / "prod" / "inspection.db"
    database.parent.mkdir()
    database.write_bytes(b"")

    with pytest.raises(OutputPathError, match="the database being read"):
        write_plan_json(plan, database, database=database)

    assert database.read_bytes() == b""


def test_an_output_path_that_is_a_database_sidecar_is_refused(db, tmp_path):
    """Truncating a WAL destroys uncommitted state as surely as truncating
    the database does, so the sidecars are protected by name."""
    plan = build_plan(db)
    database = tmp_path / "prod" / "inspection.db"
    database.parent.mkdir()
    database.write_bytes(b"")

    with pytest.raises(OutputPathError, match="a database sidecar"):
        write_plan_json(plan, Path(str(database) + "-wal"), database=database)


# --- blocker 4: "both artifacts or neither" was not implemented -----------


def test_a_failure_committing_the_envelope_leaves_neither_artifact(
    db, tmp_path, monkeypatch
):
    """The case preflight cannot see.

    Preflight refuses paths that are already unusable. It cannot refuse a
    write that fails halfway -- a full disk, a revoked permission -- and the
    sequential writes left the first artifact on disk when the second failed.
    Here the CSV commits and the envelope's rename fails, which is exactly
    that shape.
    """
    plan = build_plan(db)
    json_path = tmp_path / "plan.json"
    csv_path = tmp_path / "plan.csv"
    real_replace = os.replace

    def failing_replace(source, destination, *args, **kwargs):
        if str(destination).endswith(".json"):
            raise OSError("no space left on device")
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(planner.os, "replace", failing_replace)

    with pytest.raises(OutputPathError, match="could not commit"):
        planner.write_plan_artifacts(plan, json_path=json_path, csv_path=csv_path)

    # Neither artifact, and no staging residue under either name.
    assert not json_path.exists()
    assert not csv_path.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_a_successful_write_leaves_no_staging_files(db, tmp_path):
    plan = build_plan(db)
    written = planner.write_plan_artifacts(
        plan, json_path=tmp_path / "plan.json", csv_path=tmp_path / "plan.csv"
    )

    assert set(written) == {"json", "csv"}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["plan.csv", "plan.json"]


def test_the_envelope_carries_the_digest_of_the_csv_beside_it(db, tmp_path):
    """The commit marker, and what makes the pair verifiable.

    The envelope is renamed last, so its presence attests the CSV finished
    writing; `artifacts.csv_sha256` is what lets migration 015 prove the
    bindings beside it are the ones this envelope approved, rather than two
    files that happen to share a directory.
    """
    plan = build_plan(db)
    json_path = tmp_path / "plan.json"
    csv_path = tmp_path / "plan.csv"
    planner.write_plan_artifacts(plan, json_path=json_path, csv_path=csv_path)

    envelope = json.loads(json_path.read_text(encoding="utf-8"))
    assert envelope["artifacts"]["csv_sha256"] == hashlib.sha256(
        csv_path.read_bytes()
    ).hexdigest()


def test_an_envelope_written_alone_attests_to_no_bindings(db, tmp_path):
    """Null rather than a digest of nothing: this call wrote no CSV, so there
    is nothing for the envelope to vouch for and it says so."""
    plan = build_plan(db)
    json_path = tmp_path / "plan.json"
    write_plan_json(plan, json_path)

    envelope = json.loads(json_path.read_text(encoding="utf-8"))
    assert envelope["artifacts"]["csv_sha256"] is None


def test_a_stale_staging_file_is_refused_rather_than_overwritten(db, tmp_path):
    """The planner cannot know what put it there, so it does not destroy it."""
    plan = build_plan(db)
    json_path = tmp_path / "plan.json"
    (tmp_path / "plan.json.partial").write_text("residue", encoding="utf-8")

    with pytest.raises(OutputPathError, match="overwrite"):
        write_plan_json(plan, json_path)

    assert (tmp_path / "plan.json.partial").read_text(encoding="utf-8") == "residue"


# --- blocker 5: the NULL/-1 location collision ---------------------------


def test_a_null_location_and_a_minus_one_location_are_not_one_location(db):
    """`count(DISTINCT ifnull(location_id, -1))` cannot tell them apart.

    One child with no location and one child at a legitimate location id of
    -1 counted as a single location, so a genuinely mixed page set passed the
    validation that exists to refuse exactly that.
    """
    db.execute("UPDATE archive_pages SET location_id = NULL WHERE id = 100")
    db.execute("UPDATE archive_pages SET location_id = -1 WHERE id = 101")

    with pytest.raises(PlannerInvariantError, match="span 2 location"):
        build_plan(db)


def test_pages_that_all_lack_a_location_are_a_single_location(db):
    """The positive control the counting change must not break.

    Every child NULL is one location -- the unknown one -- and the signature
    agreeing that it has none is a consistent state, not a mixed one.
    """
    db.execute("UPDATE archive_pages SET location_id = NULL WHERE archive_id = 1")
    db.execute("UPDATE archive_content_signatures SET location_id = NULL "
               "WHERE archive_id = 1")

    inventory = next(
        b for b in build_plan(db).bindings
        if b.table == "page_inventory" and b.archive_id == 1
    )
    assert inventory.values["location_id"] is None


# --- blocker 6: the canonical encoding was not injective ------------------


def _inspection_binding(values):
    return PlannedBinding(
        table="archive_inspections",
        key=1,
        key_kind="row_id",
        archive_id=1,
        sides=(SideAttribution("", 1, 10, SINGLE_REVISION_INHERITED),),
        values=values,
    )


def test_delimiter_injection_cannot_forge_a_colliding_canonical_line():
    """Two different bindings that rendered to the same line, byte for byte.

    The old rendering joined unescaped `name=value` fields with `|`, so a
    value containing those characters could reproduce a different binding's
    line exactly. These two are the demonstration: under the old encoding both
    became `...|inspector_version=a|inspector_version_basis=b|
    inspector_version_basis=c`, and therefore shared a plan digest.
    """
    left = _inspection_binding(
        {"inspector_version": "a|inspector_version_basis=b",
         "inspector_version_basis": "c"}
    )
    right = _inspection_binding(
        {"inspector_version": "a",
         "inspector_version_basis": "b|inspector_version_basis=c"}
    )

    assert left.canonical_line() != right.canonical_line()
    assert planner.compute_plan_digest([left], "s") != planner.compute_plan_digest(
        [right], "s"
    )


def test_a_null_value_and_an_empty_string_are_different_bindings():
    """The old rendering substituted `''` for `None`, conflating "unknown"
    with "known to be empty" -- two states this schema deliberately keeps
    apart everywhere else."""
    absent = _inspection_binding(
        {"inspector_version": None, "inspector_version_basis": "unknown_legacy"}
    )
    empty = _inspection_binding(
        {"inspector_version": "", "inspector_version_basis": "unknown_legacy"}
    )

    assert absent.canonical_line() != empty.canonical_line()


def test_a_value_that_cannot_be_rendered_reproducibly_is_refused():
    """A float is the type that would quietly reintroduce ambiguity.

    Two different floats can share a repr, so a digest over one would stop
    distinguishing them. Nothing in the plan is a float today; this is what
    makes that a checked property rather than a habit.
    """
    binding = _inspection_binding(
        {"inspector_version": 1.5, "inspector_version_basis": "unknown_legacy"}
    )

    with pytest.raises(PlannerInvariantError, match="canonically rendered"):
        binding.canonical_line()


def test_the_snapshot_rendering_is_canonical_json_throughout():
    """Applied to the snapshot as well as the bindings, per the review.

    Every line parses as one JSON object, which is the property that makes the
    encoding injective; a line that fell back to the old `name=value` form
    would fail to parse and fail here.
    """
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA)
    _archive(connection, 1, pages=2)

    inputs = planner._read_inputs(connection)

    for line in planner.canonical_snapshot_lines(inputs):
        assert isinstance(json.loads(line), dict)


# --- properties the bypass run found nothing failing for ------------------


def test_the_snapshot_digest_covers_the_hash_digest(db):
    """The snapshot digest is an identity for the state, not for what the
    classifier looked at.

    Removing this field from the rendering failed no test, because every
    reachable hash-digest change is already refused by `_validate_seed`. The
    coverage still matters -- it is what the digest CLAIMS -- so it is pinned
    here by rendering a mutated `_Inputs` directly, which is the only way to
    reach a state classification would refuse.
    """
    inputs = planner._read_inputs(db)
    rewritten = dataclasses.replace(
        inputs,
        hashes=tuple(
            (row_id, archive_id, "rewritten" if archive_id == 1 else digest)
            for row_id, archive_id, digest in inputs.hashes
        ),
    )

    assert planner.compute_snapshot_digest(rewritten) != planner.compute_snapshot_digest(
        inputs
    )


def test_the_bindings_are_committed_before_the_envelope(db, tmp_path, monkeypatch):
    """The envelope is renamed last, so its presence attests to a finished CSV.

    Reversing the order failed no test, because the rollback removes both
    artifacts either way. The order is not for the errors cleanup can reach:
    it is for a kill between the two renames, where the surviving state
    should be a CSV with no envelope -- recognisably incomplete -- rather
    than an envelope with no bindings, which reads as a complete plan. That
    crash cannot be reproduced here, so the sequence is pinned instead.
    """
    plan = build_plan(db)
    real_replace = os.replace
    committed = []

    def recording_replace(source, destination, *args, **kwargs):
        committed.append(Path(destination).suffix)
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(planner.os, "replace", recording_replace)
    planner.write_plan_artifacts(
        plan, json_path=tmp_path / "plan.json", csv_path=tmp_path / "plan.csv"
    )

    assert committed == [".csv", ".json"]


# --- review round 3: the staging blockers ---------------------------------


def test_an_artifact_path_that_is_another_artifacts_staging_path_is_refused(
    db, tmp_path
):
    """The four-name collision, reproduced from the review.

    With JSON=`plan` and CSV=`plan.partial`, the envelope's staging path IS
    the CSV's final path. Comparing only the two finals let it through:
    staging wrote the envelope to `plan.partial`, committing the CSV renamed
    its own staging file over it, and committing the envelope then renamed the
    CSV's bytes to `plan`. The call reported success naming both artifacts,
    while on disk the CSV was gone and the envelope held CSV content -- worse
    than the half-pair the staging protocol was added to prevent.
    """
    plan = build_plan(db)

    with pytest.raises(OutputPathError, match="staging file"):
        planner.write_plan_artifacts(
            plan, json_path=tmp_path / "plan", csv_path=tmp_path / "plan.partial"
        )

    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_the_collision_check_covers_the_reverse_pairing_too(db, tmp_path):
    """The same collision with the roles swapped.

    Testing the whole class rather than the reported instance: here the CSV's
    staging path is the envelope's final path, which a check written against
    only the reported ordering would miss.
    """
    plan = build_plan(db)

    with pytest.raises(OutputPathError, match="staging file"):
        planner.write_plan_artifacts(
            plan, json_path=tmp_path / "plan.partial", csv_path=tmp_path / "plan"
        )

    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_a_staging_write_that_fails_after_creating_the_file_leaves_nothing(
    db, tmp_path, monkeypatch
):
    """The residue case: created, partially written, then failed.

    The cleanup set was populated only after the write RETURNED, so a write
    that created the file and then failed -- a full disk is the ordinary way
    -- left a `.partial` nothing knew about. The failure is injected at the
    stream rather than at the path, because the file must genuinely exist by
    the time the error is raised for the test to mean anything.
    """
    plan = build_plan(db)
    real_fdopen = os.fdopen
    calls = []

    def failing_fdopen(descriptor, *args, **kwargs):
        stream = real_fdopen(descriptor, *args, **kwargs)
        calls.append(descriptor)

        if len(calls) > 1:
            return stream

        class PartialThenFails:
            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                stream.close()
                return False

            def write(self, payload):
                stream.write(payload[:50])
                raise OSError("no space left on device")

        return PartialThenFails()

    monkeypatch.setattr(planner.os, "fdopen", failing_fdopen)

    with pytest.raises(OutputPathError, match="could not write"):
        planner.write_plan_artifacts(
            plan, json_path=tmp_path / "plan.json", csv_path=tmp_path / "plan.csv"
        )

    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_a_staging_name_taken_after_preflight_is_neither_written_nor_removed(
    tmp_path
):
    """What O_EXCL buys, tested at the writer rather than through preflight.

    Preflight refuses a staging path that already exists, but it cannot cover
    the window between that check and the write. Exclusive creation closes it,
    and the property that matters is twofold: the existing file is not
    overwritten, and it does not enter the cleanup set -- otherwise the
    rollback would delete a file this call never created.
    """
    victim = tmp_path / "someone_elses.partial"
    victim.write_text("not ours", encoding="utf-8")
    created = []

    with pytest.raises(OutputPathError, match="existing staging file"):
        planner._create_and_write(victim, "our payload", created)

    assert victim.read_text(encoding="utf-8") == "not ours"
    assert created == []
