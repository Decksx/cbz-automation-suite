"""The read-only revision-retention planner (roadmap Step 3, planner only).

Every test here runs against a temporary database built by the real
migrations. Nothing opens production, and nothing in the module under test has
a write path to open it with.

The tests are organised around the four questions the planner keeps apart,
because conflating them is the specific failure the module exists to prevent:

* **policy** -- may this revision ever be pruned;
* **evidence granularity** -- was the reason revision-specific, or an
  archive-level proxy;
* **feasibility** -- can schema 014 execute the policy result;
* **execution** -- never, in this slice.

The pairing that matters most is `test_a_candidate_is_reported_as_a_candidate_
even_though_schema_014_refuses_it` together with
`test_feasibility_is_identical_for_a_protected_revision_in_the_same_position`:
between them they pin that policy is not shaded by feasibility, which no
single-verdict assertion could show.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import pytest

from comic_automation.archive import revision_retention as planner
from comic_automation.archive import revision_retention_cli as cli
from comic_automation.database import dal
from comic_automation.database.connection import connect_database
from comic_automation.database.migrations import apply_migrations

MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHARED = "5" * 64


# --- fixtures and builders ------------------------------------------------


@pytest.fixture()
def database_path(tmp_path: Path) -> Path:
    """An empty database at the current schema."""
    path = tmp_path / "retention.db"
    connection = connect_database(path)

    try:
        apply_migrations(connection, MIGRATIONS)
        connection.commit()
    finally:
        connection.close()

    return path


@pytest.fixture()
def connection(database_path: Path):
    conn = dal.open_connection(database_path)

    try:
        yield conn
    finally:
        conn.close()


def _new_archive(conn: sqlite3.Connection, file_size: int = 4096) -> int:
    """A new archive, which schema 014 immediately gives a provisional origin.

    Ordinal 1 is therefore always the provisional placeholder, and byte
    generations start at ordinal 2. Tests that count generations filter on
    `identity_state` rather than counting rows.
    """
    with dal.transaction(conn):
        return dal.ArchiveRepository(conn).create(file_size=file_size)


def _generation(
    conn: sqlite3.Connection,
    archive_id: int,
    digest: str,
    *,
    evidence: str = "test generation",
    promote: bool = True,
) -> int:
    """Append one byte generation and, by default, make it current.

    `record_or_reuse` promotes only when the current revision is still
    provisional -- an archive deliberately rolled back stays where the operator
    put it -- so the promotion is explicit here. `promote=False` is how the
    rolled-back-pointer cases are built.
    """
    with dal.transaction(conn):
        revisions = dal.RevisionRepository(conn)
        revision_id, _ = revisions.record_or_reuse(
            archive_id=archive_id,
            archive_sha256=digest,
            evidence=evidence,
        )

        if promote:
            revisions.set_current(archive_id, revision_id)

    return revision_id


def _plan(database_path: Path, **kwargs) -> planner.RetentionPlan:
    return planner.plan_from_database(database_path, **kwargs).result


def _by_id(plan: planner.RetentionPlan) -> dict[int, planner.RevisionPlan]:
    return {row.revision_id: row for row in plan.revisions}


def _add_job(
    conn: sqlite3.Connection, archive_id: int, status: str
) -> None:
    """Insert one job row directly.

    Raw SQL rather than the queue: several of these statuses are ones the
    queue would refuse to create, and the point of the test is what the planner
    does when it *finds* such a row.
    """
    with dal.transaction(conn):
        conn.execute(
            "INSERT INTO jobs (job_type, status, archive_id) "
            "VALUES ('inspect', ?, ?)",
            (status, archive_id),
        )


# --- lineage shapes -------------------------------------------------------


def test_a_single_revision_archive_yields_no_candidate_and_no_residue(
    connection, database_path: Path
) -> None:
    """The production shape: one revision per archive, all current.

    Migration 014 left every one of the 59,688 archives in exactly this state,
    so this is the census the production smoke has to reproduce -- zero
    candidates and zero residue, from policy rather than from an empty table.
    """
    archive_id = _new_archive(connection)
    plan = _plan(database_path)

    assert plan.totals["revisions"] == 1
    assert plan.totals["current"] == 1
    assert plan.totals["noncurrent"] == 0
    assert plan.totals["candidates"] == 0
    assert plan.totals["unexplained"] == 0

    only = plan.revisions[0]
    assert only.archive_id == archive_id
    assert only.is_current is True
    assert only.identity_state == "provisional"
    assert only.policy_classification == planner.PROTECTED
    assert only.protection_reasons == ("is_current_revision",)


def test_a_two_generation_lineage_keeps_the_predecessor_and_nothing_else(
    connection, database_path: Path
) -> None:
    """Provisional origin, one established generation, default window.

    The origin is the immediately previous revision of the current one, so the
    default window of one generation keeps it. There is nothing older, so
    nothing is a candidate yet -- which is what makes the three-generation case
    below the first that produces one.
    """
    archive_id = _new_archive(connection)
    established = _generation(connection, archive_id, SHA_A)

    plan = _plan(database_path)
    rows = _by_id(plan)
    origin = next(
        row for row in plan.revisions if row.revision_ordinal == 1
    )

    assert rows[established].is_current is True
    assert rows[established].protection_reasons == ("is_current_revision",)
    assert origin.is_current is False
    assert origin.policy_classification == planner.PROTECTED
    assert origin.protection_reasons == ("retention_window",)
    assert plan.totals["candidates"] == 0


def test_a_three_generation_lineage_produces_exactly_one_candidate(
    connection, database_path: Path
) -> None:
    """Provisional origin plus two byte generations, window of one.

    Current is generation 2, the window keeps generation 1, and the
    provisional origin falls out of the window with no other evidence
    attached -- the first revision in these tests that policy is willing to
    prune.
    """
    archive_id = _new_archive(connection)
    first = _generation(connection, archive_id, SHA_A)
    second = _generation(connection, archive_id, SHA_B)

    plan = _plan(database_path)
    rows = _by_id(plan)
    origin = next(
        row for row in plan.revisions if row.revision_ordinal == 1
    )

    assert rows[second].policy_classification == planner.PROTECTED
    assert rows[second].protection_reasons == ("is_current_revision",)
    assert rows[first].policy_classification == planner.PROTECTED
    assert rows[first].protection_reasons == ("retention_window",)

    assert origin.policy_classification == planner.CANDIDATE
    assert origin.protection_reasons == ()
    assert origin.evidence_granularity == planner.GRANULARITY_NONE
    assert plan.totals["candidates"] == 1
    assert plan.totals["unexplained"] == 0


def test_the_retention_window_can_be_widened_to_protect_more(
    connection, database_path: Path
) -> None:
    """A wider window is a different policy, and a different plan."""
    archive_id = _new_archive(connection)
    _generation(connection, archive_id, SHA_A)
    _generation(connection, archive_id, SHA_B)

    wide = _plan(
        database_path,
        policy=planner.RetentionPolicy(keep_previous_generations=2),
    )

    assert wide.totals["candidates"] == 0
    origin = next(row for row in wide.revisions if row.revision_ordinal == 1)
    assert origin.protection_reasons == ("retention_window",)


def test_a_zero_window_keeps_only_the_current_revision(
    connection, database_path: Path
) -> None:
    archive_id = _new_archive(connection)
    _generation(connection, archive_id, SHA_A)

    plan = _plan(
        database_path,
        policy=planner.RetentionPolicy(keep_previous_generations=0),
    )

    assert plan.totals["candidates"] == 1
    assert plan.totals["current"] == 1


def test_the_window_is_walked_through_lineage_not_ordinal_arithmetic(
    connection, database_path: Path
) -> None:
    """A rolled-back pointer moves the window with it.

    Current is generation 1 while generation 2 exists, so "current minus one"
    is the provisional origin and *not* "highest ordinal minus one". Ordinal
    arithmetic would protect generation 1 as the predecessor of the tip and
    leave the origin a candidate -- the exact inversion of the truth.
    """
    archive_id = _new_archive(connection)
    first = _generation(connection, archive_id, SHA_A)
    second = _generation(connection, archive_id, SHA_B, promote=False)

    with dal.transaction(connection):
        dal.RevisionRepository(connection).set_current(archive_id, first)

    plan = _plan(database_path)
    rows = _by_id(plan)
    origin = next(row for row in plan.revisions if row.revision_ordinal == 1)

    assert rows[first].is_current is True
    assert origin.protection_reasons == ("retention_window",)
    assert rows[second].protection_reasons == ("newer_than_current",)
    assert plan.totals["candidates"] == 0


def test_a_revision_newer_than_current_is_protected_not_pruned(
    connection, database_path: Path
) -> None:
    """The roll-forward path is kept, under its own named reason.

    No roadmap keep-rule covers a revision newer than current, and no prune
    rule authorises it either. Protecting it under a distinct reason keeps the
    gap visible in the output instead of letting whichever branch caught it
    decide silently.
    """
    archive_id = _new_archive(connection)
    first = _generation(connection, archive_id, SHA_A)
    second = _generation(connection, archive_id, SHA_B)
    with dal.transaction(connection):
        dal.RevisionRepository(connection).set_current(archive_id, first)

    row = _by_id(_plan(database_path))[second]

    assert row.is_current is False
    assert row.policy_classification == planner.PROTECTED
    assert row.protection_reasons == ("newer_than_current",)
    assert row.evidence_granularity == planner.GRANULARITY_REVISION


# --- the current revision -------------------------------------------------


def test_the_current_revision_is_never_a_candidate(
    connection, database_path: Path
) -> None:
    """Across every shape in one database, not one current row is prunable."""
    plain = _new_archive(connection)

    rolled_back = _new_archive(connection)
    first = _generation(connection, rolled_back, SHA_A)
    _generation(connection, rolled_back, SHA_B)
    with dal.transaction(connection):
        dal.RevisionRepository(connection).set_current(rolled_back, first)

    long_lineage = _new_archive(connection)
    for digest in (SHA_A, SHA_B, SHA_C):
        _generation(connection, long_lineage, digest)

    quarantined = _new_archive(connection)
    _generation(connection, quarantined, SHA_C)
    _add_job(connection, quarantined, "quiesced")

    plan = _plan(database_path)
    current_rows = [row for row in plan.revisions if row.is_current]

    assert len(current_rows) == 4
    for row in current_rows:
        assert row.policy_classification == planner.PROTECTED
        assert "is_current_revision" in row.protection_reasons

    assert plan.totals["current"] == 4
    assert not any(
        row.is_current and row.policy_classification == planner.CANDIDATE
        for row in plan.revisions
    )


def test_unknown_evidence_cannot_demote_the_current_revision_to_residue(
    connection, database_path: Path
) -> None:
    """Which revision is current is read, not interpreted.

    The archive carries a job status the planner does not recognise, which
    pushes its noncurrent revisions to residue. The current revision still
    reports `protected`, and still carries the unknown evidence in its own
    row so the finding is not lost.
    """
    archive_id = _new_archive(connection)
    _generation(connection, archive_id, SHA_A)
    _generation(connection, archive_id, SHA_B)
    _add_job(connection, archive_id, "hibernating")

    plan = _plan(database_path)
    current = next(row for row in plan.revisions if row.is_current)

    assert current.policy_classification == planner.PROTECTED
    assert current.unknown_evidence == ("job_status:hibernating",)
    assert plan.totals["unexplained"] == 2


# --- observations ---------------------------------------------------------


def test_an_observation_protects_only_the_revision_it_references(
    connection, database_path: Path
) -> None:
    """Observations are the one revision-granular reference besides lineage.

    The archive has two noncurrent revisions that would otherwise both be
    candidates. One is observed, and only that one is protected -- if
    observations leaked to the archive, both would be.
    """
    archive_id = _new_archive(connection)
    first = _generation(connection, archive_id, SHA_A)
    second = _generation(connection, archive_id, SHA_B)
    _generation(connection, archive_id, SHA_C)

    origin = next(
        row
        for row in _plan(database_path).revisions
        if row.revision_ordinal == 1
    )

    with dal.transaction(connection):
        dal.RevisionRepository(connection).observe(
            revision_id=origin.revision_id
        )

    plan = _plan(
        database_path,
        policy=planner.RetentionPolicy(keep_previous_generations=0),
    )
    rows = _by_id(plan)

    assert rows[origin.revision_id].policy_classification == planner.PROTECTED
    assert rows[origin.revision_id].protection_reasons == (
        "revision_has_observations",
    )
    assert rows[origin.revision_id].observation_count == 1
    assert (
        rows[origin.revision_id].evidence_granularity
        == planner.GRANULARITY_REVISION
    )

    # The unobserved siblings are untouched by it.
    assert rows[first].policy_classification == planner.CANDIDATE
    assert rows[first].observation_count == 0
    assert rows[second].policy_classification == planner.CANDIDATE


# --- archive-level proxy evidence -----------------------------------------


@pytest.mark.parametrize(
    "setup,expected_reason",
    [
        (
            lambda conn, archive_id: conn.execute(
                "INSERT INTO jobs (job_type, status, archive_id) "
                "VALUES ('hash', 'running', ?)",
                (archive_id,),
            ),
            "active_or_recoverable_job",
        ),
        (
            lambda conn, archive_id: conn.execute(
                "INSERT INTO jobs (job_type, status, archive_id) "
                "VALUES ('hash', 'failed', ?)",
                (archive_id,),
            ),
            "unresolved_failure",
        ),
        (
            lambda conn, archive_id: conn.execute(
                "INSERT INTO archive_quarantine "
                "(archive_id, source_path, quarantine_path, "
                " failure_category, status) "
                "VALUES (?, 'a', 'b', 'corrupt', 'resolved')",
                (archive_id,),
            ),
            "quarantine_or_resolution",
        ),
    ],
)
def test_archive_level_evidence_protects_every_revision_as_proxy(
    connection, database_path: Path, setup, expected_reason: str
) -> None:
    """One archive-keyed row protects the whole lineage, labelled as proxy.

    None of these tables can name a revision -- they key on `archive_id` --
    so the planner cannot tell which generation the evidence concerns and
    conservatively keeps all of them. The label is the load-bearing part: a
    reader must be able to tell this from a revision-specific finding.

    The resolved quarantine is deliberate. The roadmap keeps "quarantine *or
    resolution history*", so a closed quarantine still protects.
    """
    archive_id = _new_archive(connection)
    _generation(connection, archive_id, SHA_A)
    _generation(connection, archive_id, SHA_B)

    with dal.transaction(connection):
        setup(connection, archive_id)

    plan = _plan(
        database_path,
        policy=planner.RetentionPolicy(keep_previous_generations=0),
    )

    assert plan.totals["candidates"] == 0
    assert plan.totals["unexplained"] == 0

    for row in plan.revisions:
        assert row.policy_classification == planner.PROTECTED
        assert expected_reason in row.protection_reasons
        assert row.evidence_granularity == planner.GRANULARITY_ARCHIVE_PROXY


def test_open_review_work_protects_both_sides_of_the_pair(
    connection, database_path: Path
) -> None:
    """A near-duplicate under review keeps both archives' revisions.

    Both sides matter: the review is about the relationship, so pruning
    either side's history would remove evidence the reviewer may need.
    """
    left = _new_archive(connection)
    right = _new_archive(connection)
    for archive_id in (left, right):
        _generation(connection, archive_id, SHA_A if archive_id == left else SHA_B)

    with dal.transaction(connection):
        connection.execute(
            """
            INSERT INTO near_duplicate_candidates (
                archive_a_id, archive_b_id, match_method, similarity_score,
                page_match_ratio, compared_page_count, page_count_a,
                page_count_b, average_dhash_distance, average_phash_distance,
                metrics_json, review_status
            ) VALUES (?, ?, 'phash', 0.9, 0.9, 10, 10, 10, 1.0, 1.0,
                      '{}', 'pending_review')
            """,
            (min(left, right), max(left, right)),
        )

    plan = _plan(
        database_path,
        policy=planner.RetentionPolicy(keep_previous_generations=0),
    )

    assert plan.totals["candidates"] == 0
    for row in plan.revisions:
        assert "open_review_work" in row.protection_reasons
        assert row.evidence_granularity == planner.GRANULARITY_ARCHIVE_PROXY


def test_a_disposition_event_protects_every_revision_as_proxy(
    connection, database_path: Path
) -> None:
    """Retirement is recorded through the disposition trigger, and protects."""
    archive_id = _new_archive(connection)
    _generation(connection, archive_id, SHA_A)

    with dal.transaction(connection):
        connection.execute(
            "INSERT INTO archive_retirements (archive_id, reason, evidence) "
            "VALUES (?, 'unreachable_source', 'test retirement')",
            (archive_id,),
        )

    plan = _plan(
        database_path,
        policy=planner.RetentionPolicy(keep_previous_generations=0),
    )

    assert plan.totals["candidates"] == 0
    for row in plan.revisions:
        assert "quarantine_or_resolution" in row.protection_reasons
        assert row.evidence_granularity == planner.GRANULARITY_ARCHIVE_PROXY


def test_proxy_granularity_wins_when_reasons_are_mixed(
    connection, database_path: Path
) -> None:
    """One proxy reason among revision-level ones still labels the row proxy.

    The label describes what a reader may conclude from the row as a whole.
    Reporting `revision` because a revision-level reason also applied would
    overstate what the planner actually established.
    """
    archive_id = _new_archive(connection)
    _generation(connection, archive_id, SHA_A)
    _add_job(connection, archive_id, "pending")

    current = next(
        row for row in _plan(database_path).revisions if row.is_current
    )

    assert current.protection_reasons == (
        "is_current_revision",
        "active_or_recoverable_job",
    )
    assert current.evidence_granularity == planner.GRANULARITY_ARCHIVE_PROXY


# --- failing closed -------------------------------------------------------


def test_an_unknown_job_status_fails_closed_to_residue(
    connection, database_path: Path
) -> None:
    """A status outside the declared vocabulary is never assumed benign.

    `jobs.status` carries no CHECK constraint, so an unrecognised value is a
    state the database can genuinely hold. Guessing that it is terminal would
    propose pruning evidence something may still be using; guessing that it is
    active would silently protect and claim the planner understood it. Residue
    says neither.
    """
    archive_id = _new_archive(connection)
    _generation(connection, archive_id, SHA_A)
    _generation(connection, archive_id, SHA_B)
    _add_job(connection, archive_id, "quiescing")

    plan = _plan(
        database_path,
        policy=planner.RetentionPolicy(keep_previous_generations=0),
    )

    noncurrent = [row for row in plan.revisions if not row.is_current]

    assert plan.totals["unexplained"] == len(noncurrent) == 2
    assert plan.totals["candidates"] == 0

    for row in noncurrent:
        assert row.policy_classification == planner.UNEXPLAINED
        assert row.unknown_evidence == ("job_status:quiescing",)


def test_residue_outranks_protection_for_a_noncurrent_revision(
    connection, database_path: Path
) -> None:
    """Being protected by another rule does not hide uninterpretable evidence.

    The revision is inside the retention window, so it would be reported as
    protected. It is reported as residue instead, because "kept, and we
    understood why" and "kept, and we could not read this archive's evidence"
    are different statements and only the second is true. The protection
    reasons are still carried on the row.
    """
    archive_id = _new_archive(connection)
    _generation(connection, archive_id, SHA_A)
    _add_job(connection, archive_id, "unknown-state")

    origin = next(
        row
        for row in _plan(database_path).revisions
        if row.revision_ordinal == 1
    )

    assert origin.policy_classification == planner.UNEXPLAINED
    assert origin.protection_reasons == ("retention_window",)
    assert origin.unknown_evidence == ("job_status:unknown-state",)


def test_an_archive_with_no_current_pointer_is_all_residue(
    connection, database_path: Path
) -> None:
    """A schema-014 impossibility still has to reach the report.

    `trg_current_revision_not_cleared` makes a NULL pointer unreachable, so
    the trigger is dropped here to build the state -- which is what a database
    restored from an older schema, or advanced by a future one, could look
    like. A planner that assumed its invariants held could not report that
    they did not.
    """
    archive_id = _new_archive(connection)
    _generation(connection, archive_id, SHA_A)

    with dal.transaction(connection):
        connection.execute("DROP TRIGGER trg_current_revision_not_cleared")
        connection.execute(
            "UPDATE archive_files SET current_revision_id = NULL WHERE id = ?",
            (archive_id,),
        )

    plan = _plan(database_path)

    assert plan.totals["current"] == 0
    assert plan.totals["unexplained"] == 2
    assert plan.totals["candidates"] == 0
    for row in plan.revisions:
        assert row.policy_classification == planner.UNEXPLAINED
        assert "archive_has_no_current_revision" in row.unknown_evidence


def test_the_reconciliation_refuses_a_doubly_classified_revision(
    connection, database_path: Path
) -> None:
    """The totals verify themselves rather than trusting the classifier.

    Double classification is impossible by construction, which is exactly why
    it is checked: the reconciliation is the artefact the operator is asked to
    trust, so it must not be able to report a clean total over a row it counted
    twice.
    """
    archive_id = _new_archive(connection)
    _generation(connection, archive_id, SHA_A)
    plan = _plan(database_path)

    duplicated = list(plan.revisions) + [plan.revisions[0]]

    with pytest.raises(planner.PlannerInvariantError, match="more than once"):
        planner.plan_totals(duplicated)


def test_the_reconciliation_refuses_totals_that_do_not_add_up(
    connection, database_path: Path
) -> None:
    """A classification outside the three buckets breaks the sum, loudly."""
    archive_id = _new_archive(connection)
    _generation(connection, archive_id, SHA_A)
    plan = _plan(database_path)

    noncurrent = next(row for row in plan.revisions if not row.is_current)
    corrupted = [
        row
        for row in plan.revisions
        if row.revision_id != noncurrent.revision_id
    ]
    corrupted.append(
        planner.RevisionPlan(
            **{
                **{
                    field: getattr(noncurrent, field)
                    for field in noncurrent.__dataclass_fields__
                },
                "policy_classification": "something_else",
            }
        )
    )

    with pytest.raises(planner.PlannerInvariantError, match="reconcile"):
        planner.plan_totals(corrupted)


# --- policy is not shaded by feasibility ----------------------------------


def test_a_candidate_is_reported_even_though_schema_014_refuses_it(
    connection, database_path: Path
) -> None:
    """Policy says prunable; the schema says no. Both are reported.

    Converting the candidate to `protected` because 014 cannot execute it
    would erase the only signal that migration 015 has work to do.
    """
    archive_id = _new_archive(connection)
    _generation(connection, archive_id, SHA_A)
    _generation(connection, archive_id, SHA_B)

    origin = next(
        row
        for row in _plan(database_path).revisions
        if row.revision_ordinal == 1
    )

    assert origin.policy_classification == planner.CANDIDATE
    assert origin.feasible_under_schema_014 is False
    assert (
        planner.INFEASIBLE_DELETE_GUARD in origin.infeasibility_reasons
    )
    assert (
        planner.INFEASIBLE_SUCCESSOR_REFERENCE in origin.infeasibility_reasons
    )


def test_feasibility_is_identical_for_a_protected_revision_in_the_same_position(
    connection, database_path: Path
) -> None:
    """Feasibility is a property of the schema, not of the verdict.

    Two archives with identical structure: in one the oldest revision is a
    candidate, in the other an operator pin protects it. The feasibility answer
    is byte-identical, which is what proves the two axes are computed
    independently rather than one being derived from the other.
    """
    free = _new_archive(connection)
    _generation(connection, free, SHA_A)
    _generation(connection, free, SHA_B)

    pinned_archive = _new_archive(connection)
    _generation(connection, pinned_archive, SHA_A)
    _generation(connection, pinned_archive, SHA_C)

    baseline = _plan(database_path)
    pinned_origin = next(
        row
        for row in baseline.revisions
        if row.archive_id == pinned_archive and row.revision_ordinal == 1
    )

    plan = _plan(
        database_path,
        pin_entries=[
            {"revision_id": pinned_origin.revision_id, "reason": "keep it"}
        ],
    )
    rows = _by_id(plan)

    free_origin = next(
        row
        for row in plan.revisions
        if row.archive_id == free and row.revision_ordinal == 1
    )
    protected = rows[pinned_origin.revision_id]

    assert free_origin.policy_classification == planner.CANDIDATE
    assert protected.policy_classification == planner.PROTECTED
    assert protected.protection_reasons == ("operator_pin",)

    # Same structural position, same feasibility answer, different verdicts.
    assert (
        free_origin.feasible_under_schema_014
        == protected.feasible_under_schema_014
        is False
    )
    assert (
        free_origin.infeasibility_reasons == protected.infeasibility_reasons
    )


def test_no_candidate_is_ever_executable_under_schema_014(
    connection, database_path: Path
) -> None:
    """The delete guard is unconditional, so the count is structurally zero."""
    archive_id = _new_archive(connection)
    for digest in (SHA_A, SHA_B, SHA_C):
        _generation(connection, archive_id, digest)

    plan = _plan(
        database_path,
        policy=planner.RetentionPolicy(keep_previous_generations=0),
    )

    assert plan.totals["candidates"] > 0
    assert plan.totals["candidates_feasible_under_schema_014"] == 0


def test_the_tip_revision_is_infeasible_for_the_delete_guard_alone(
    connection, database_path: Path
) -> None:
    """A revision with no successor still cannot be deleted.

    Separating the two reasons matters: relaxing the lineage foreign key alone
    would not make the tip prunable, because the delete guard is what refuses
    it.
    """
    archive_id = _new_archive(connection)
    first = _generation(connection, archive_id, SHA_A)
    _generation(connection, archive_id, SHA_B, promote=False)
    with dal.transaction(connection):
        dal.RevisionRepository(connection).set_current(archive_id, first)

    tip = max(_plan(database_path).revisions, key=lambda row: row.revision_ordinal)

    assert tip.infeasibility_reasons == (planner.INFEASIBLE_DELETE_GUARD,)


def test_execution_status_is_never_anything_but_not_performed(
    connection, database_path: Path
) -> None:
    """This slice has no apply path, asserted on every row."""
    archive_id = _new_archive(connection)
    _generation(connection, archive_id, SHA_A)

    plan = _plan(database_path)

    assert plan.as_dict()["execution_status"] == "not_performed"
    for row in plan.revisions:
        assert row.execution_status == "not_performed"


# --- pins -----------------------------------------------------------------


def test_a_pin_protects_the_named_revision(
    connection, database_path: Path
) -> None:
    archive_id = _new_archive(connection)
    _generation(connection, archive_id, SHA_A)
    _generation(connection, archive_id, SHA_B)

    origin = next(
        row
        for row in _plan(database_path).revisions
        if row.revision_ordinal == 1
    )
    plan = _plan(
        database_path,
        pin_entries=[
            {
                "revision_id": origin.revision_id,
                "archive_id": archive_id,
                "reason": "under investigation",
            }
        ],
    )

    row = _by_id(plan)[origin.revision_id]
    assert row.policy_classification == planner.PROTECTED
    assert row.protection_reasons == ("operator_pin",)
    assert row.evidence_granularity == planner.GRANULARITY_REVISION


def test_a_pin_naming_a_missing_revision_is_refused(
    connection, database_path: Path
) -> None:
    """The manifest and the database must describe the same state."""
    _new_archive(connection)

    with pytest.raises(planner.PinManifestError, match="no such revision"):
        _plan(
            database_path,
            pin_entries=[{"revision_id": 999_999, "reason": "ghost"}],
        )


def test_a_pin_whose_archive_disagrees_is_refused(
    connection, database_path: Path
) -> None:
    """Refused rather than resolved in either direction."""
    first = _new_archive(connection)
    second = _new_archive(connection)
    _generation(connection, first, SHA_A)

    origin = next(
        row
        for row in _plan(database_path).revisions
        if row.archive_id == first and row.revision_ordinal == 1
    )

    with pytest.raises(planner.PinManifestError, match="disagree about identity"):
        _plan(
            database_path,
            pin_entries=[
                {
                    "revision_id": origin.revision_id,
                    "archive_id": second,
                    "reason": "wrong archive",
                }
            ],
        )


def test_a_duplicated_pin_is_refused_rather_than_deduplicated(
    connection, database_path: Path
) -> None:
    archive_id = _new_archive(connection)
    _generation(connection, archive_id, SHA_A)
    origin = next(
        row
        for row in _plan(database_path).revisions
        if row.revision_ordinal == 1
    )
    entry = {"revision_id": origin.revision_id, "reason": "keep"}

    with pytest.raises(planner.PinManifestError, match="duplicated"):
        _plan(database_path, pin_entries=[entry, dict(entry)])


def test_a_blank_pin_reason_is_refused(
    connection, database_path: Path
) -> None:
    """Matching the schema's evidence rule, tabs and newlines included.

    SQLite's one-argument `trim()` strips spaces only, and a lone tab passing
    as evidence was a real defect on migration 012. The same hole is closed
    here rather than re-derived.
    """
    archive_id = _new_archive(connection)
    origin = _plan(database_path).revisions[0]

    for blank in ("", "   ", "\t", "\n  \t "):
        with pytest.raises(planner.PinManifestError, match="blank"):
            _plan(
                database_path,
                pin_entries=[
                    {"revision_id": origin.revision_id, "reason": blank}
                ],
            )


def test_a_boolean_revision_id_is_refused(
    connection, database_path: Path
) -> None:
    """`True` is an int in Python and would silently pin revision 1."""
    _new_archive(connection)

    with pytest.raises(planner.PinManifestError, match="must be an integer"):
        _plan(
            database_path,
            pin_entries=[{"revision_id": True, "reason": "oops"}],
        )


def test_pins_are_canonicalised_before_they_reach_the_digest(
    connection, database_path: Path
) -> None:
    """Order and incidental whitespace do not change the plan's identity.

    Two operators writing the same pins in a different order, or with
    different spacing in the reason, must be able to agree that they reviewed
    the same thing.
    """
    archive_id = _new_archive(connection)
    first = _generation(connection, archive_id, SHA_A)
    second = _generation(connection, archive_id, SHA_B)

    forward = _plan(
        database_path,
        pin_entries=[
            {"revision_id": first, "reason": "one"},
            {"revision_id": second, "reason": "two   words"},
        ],
    )
    reversed_and_spaced = _plan(
        database_path,
        pin_entries=[
            {"revision_id": second, "reason": "  two words  "},
            {"revision_id": first, "reason": "one"},
        ],
    )

    assert forward.snapshot_digest == reversed_and_spaced.snapshot_digest
    assert [pin.revision_id for pin in forward.pins.pins] == sorted(
        [first, second]
    )
    assert forward.pins.pins[-1].reason == "two words"


def test_a_pin_manifest_file_is_loaded_and_validated(
    connection, database_path: Path, tmp_path: Path
) -> None:
    archive_id = _new_archive(connection)
    _generation(connection, archive_id, SHA_A)
    origin = next(
        row
        for row in _plan(database_path).revisions
        if row.revision_ordinal == 1
    )

    manifest = tmp_path / "pins.json"
    manifest.write_text(
        json.dumps(
            {
                "pins": [
                    {"revision_id": origin.revision_id, "reason": "hold"}
                ]
            }
        ),
        encoding="utf-8",
    )

    entries = planner.load_pin_manifest(manifest)
    plan = _plan(database_path, pin_entries=entries, pin_source=str(manifest))

    # The retention window also covers this revision, so the pin is asserted
    # as present rather than as the only reason -- reasons accumulate, and a
    # rule that silently replaced the others would be hiding evidence.
    assert "operator_pin" in _by_id(plan)[origin.revision_id].protection_reasons
    assert plan.pins.source == str(manifest)
    assert plan.pins.reason_for(origin.revision_id) == "hold"


def test_an_unreadable_pin_manifest_is_refused_not_treated_as_empty(
    tmp_path: Path,
) -> None:
    """An empty pin set and an unreadable one mean opposite things."""
    manifest = tmp_path / "pins.json"
    manifest.write_text("{ not json", encoding="utf-8")

    with pytest.raises(planner.PinManifestError, match="could not be read"):
        planner.load_pin_manifest(manifest)

    manifest.write_text(json.dumps({"nope": []}), encoding="utf-8")
    with pytest.raises(planner.PinManifestError, match="must be a JSON list"):
        planner.load_pin_manifest(manifest)

    with pytest.raises(planner.PinManifestError, match="does not exist"):
        planner.load_pin_manifest(tmp_path / "absent.json")


# --- the snapshot digest --------------------------------------------------


def test_every_decision_bearing_input_moves_the_digest(
    connection, database_path: Path
) -> None:
    """Each mutation class changes the digest, checked one at a time.

    Written as a sequence rather than as independent cases on purpose: each
    step asserts against the digest immediately before it, so a mutation that
    changed nothing is caught at the step that introduced it rather than being
    masked by a later one.
    """
    archive_id = _new_archive(connection)
    digests = [_plan(database_path).snapshot_digest]

    def moved(label: str) -> None:
        current = _plan(database_path).snapshot_digest
        assert current != digests[-1], f"{label} did not change the digest"
        digests.append(current)

    _generation(connection, archive_id, SHA_A)
    moved("a new revision")

    with dal.transaction(connection):
        dal.RevisionRepository(connection).observe(revision_id=1)
    moved("an observation")

    _add_job(connection, archive_id, "pending")
    moved("a job")

    with dal.transaction(connection):
        connection.execute(
            "UPDATE jobs SET status = 'completed' WHERE archive_id = ?",
            (archive_id,),
        )
    moved("a job status change")

    with dal.transaction(connection):
        connection.execute(
            "INSERT INTO archive_quarantine "
            "(archive_id, source_path, quarantine_path, failure_category) "
            "VALUES (?, 'a', 'b', 'corrupt')",
            (archive_id,),
        )
    moved("a quarantine row")

    second = _new_archive(connection)
    moved("a new archive and its current pointer")

    first_revision = _plan(database_path).revisions[0].revision_id

    # Inputs that are not database rows at all.
    policy_digest = _plan(
        database_path,
        policy=planner.RetentionPolicy(keep_previous_generations=3),
    ).snapshot_digest
    assert policy_digest != digests[-1], "the policy did not change the digest"

    pinned_digest = _plan(
        database_path,
        pin_entries=[{"revision_id": first_revision, "reason": "hold"}],
    ).snapshot_digest
    assert pinned_digest != digests[-1], "a pin did not change the digest"

    assert len(set(digests)) == len(digests)


def test_an_empty_database_still_yields_a_digest(
    database_path: Path,
) -> None:
    """"Nothing to prune" is a statement an operator can bind."""
    plan = _plan(database_path)

    assert len(plan.snapshot_digest) == 64
    assert plan.totals["revisions"] == 0
    assert plan.totals["candidates"] == 0
    assert plan.totals["unexplained"] == 0


def test_the_digest_covers_a_supplied_but_empty_pin_manifest(
    connection, database_path: Path
) -> None:
    """An explicit empty manifest is not the same as no manifest.

    Both produce zero pins, so without the count in the payload the two runs
    would hash identically and an operator could not bind which they reviewed.
    """
    _new_archive(connection)

    assert (
        _plan(database_path, pin_entries=[]).snapshot_digest
        == _plan(database_path).snapshot_digest
    )

    lines = planner.canonical_snapshot_lines(
        planner._Inputs((), {}, {}, {}, {}, {}, {}),
        planner.RetentionPolicy(),
        planner.PinManifest(),
    )
    assert "pins|count=0" in lines


def test_the_digest_is_stable_across_repeated_reads(
    connection, database_path: Path
) -> None:
    archive_id = _new_archive(connection)
    _generation(connection, archive_id, SHA_A)

    assert (
        _plan(database_path).snapshot_digest
        == _plan(database_path).snapshot_digest
    )


# --- determinism ----------------------------------------------------------


def test_output_is_deterministic_across_insertion_order(
    tmp_path: Path,
) -> None:
    """Two databases with the same logical content, built in opposite orders.

    Row ids differ, so the digests legitimately differ; what must not differ
    is the *shape* of the plan -- the ordering of rows by (archive, ordinal)
    and every classification. A planner that leaked rowid order into its
    output would produce two different reports for the same library state.
    """
    shapes = []

    for order in (0, 1):
        path = tmp_path / f"order-{order}.db"
        conn = connect_database(path)
        apply_migrations(conn, MIGRATIONS)
        conn.commit()
        conn.close()

        conn = dal.open_connection(path)
        try:
            archives = [_new_archive(conn), _new_archive(conn)]
            pairs = list(zip(archives, (SHA_A, SHA_B)))

            for archive_id, digest in (pairs if order == 0 else pairs[::-1]):
                _generation(conn, archive_id, digest)
        finally:
            conn.close()

        plan = _plan(path)
        shapes.append(
            [
                (
                    row.archive_id,
                    row.revision_ordinal,
                    row.policy_classification,
                    row.protection_reasons,
                    row.evidence_granularity,
                )
                for row in plan.revisions
            ]
        )

    assert shapes[0] == shapes[1]


def test_json_and_csv_are_byte_identical_when_rewritten(
    connection, database_path: Path, tmp_path: Path
) -> None:
    """Deterministic output, asserted on bytes rather than on parsed content.

    The CSV writer's default line terminator is CRLF and Python's text layer
    would translate it again on Windows; both are pinned, so a plan written
    twice -- or on two platforms -- produces the same file.
    """
    archive_id = _new_archive(connection)
    _generation(connection, archive_id, SHA_A)
    _generation(connection, archive_id, SHA_B)

    plan = _plan(database_path)

    first_json = planner.write_json(plan, tmp_path / "a.json").read_bytes()
    second_json = planner.write_json(plan, tmp_path / "b.json").read_bytes()
    first_csv = planner.write_csv(plan, tmp_path / "a.csv").read_bytes()
    second_csv = planner.write_csv(plan, tmp_path / "b.csv").read_bytes()

    assert first_json == second_json
    assert first_csv == second_csv
    assert b"\r\n" not in first_csv
    assert first_json.endswith(b"\n")


def test_the_csv_carries_every_documented_field(
    connection, database_path: Path, tmp_path: Path
) -> None:
    """Each row is self-describing: it names its planner and its snapshot."""
    archive_id = _new_archive(connection)
    _generation(connection, archive_id, SHA_A)
    _generation(connection, archive_id, SHA_B)

    plan = _plan(database_path)
    path = planner.write_csv(plan, tmp_path / "plan.csv")

    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == len(plan.revisions)
    assert set(rows[0]) == set(planner.CSV_COLUMNS)

    for row in rows:
        assert row["planner_version"] == planner.PLANNER_VERSION
        assert row["snapshot_digest"] == plan.snapshot_digest
        assert row["execution_status"] == "not_performed"
        assert row["feasible_under_schema_014"] == "false"
        assert row["policy_classification"] in {
            planner.PROTECTED,
            planner.CANDIDATE,
            planner.UNEXPLAINED,
        }


def test_the_json_plan_declares_each_rule_granularity(
    connection, database_path: Path, tmp_path: Path
) -> None:
    """Every policy rule declares the granularity it was evaluated at.

    Published in the plan itself rather than only in documentation, so a
    report can be audited without the reader having to find this module.
    """
    _new_archive(connection)
    plan = _plan(database_path)
    payload = json.loads(
        planner.write_json(plan, tmp_path / "plan.json").read_text("utf-8")
    )

    declared = payload["rule_granularity"]
    assert set(declared) == set(planner.RULE_GRANULARITY)
    assert set(declared.values()) == {
        planner.GRANULARITY_REVISION,
        planner.GRANULARITY_ARCHIVE_PROXY,
    }

    # Four of the roadmap's rules can only be evaluated at archive
    # granularity until Step 4 adds revision-aware provenance.
    proxy = [
        rule
        for rule, granularity in declared.items()
        if granularity == planner.GRANULARITY_ARCHIVE_PROXY
    ]
    assert sorted(proxy) == [
        "active_or_recoverable_job",
        "open_review_work",
        "quarantine_or_resolution",
        "unresolved_failure",
    ]


# --- A -> B -> A, and the 37704 / 58201 trap ------------------------------


def test_returning_to_earlier_bytes_reuses_the_revision_without_merging(
    connection, database_path: Path
) -> None:
    """A -> B -> A is three sightings of two byte states, not three states.

    The planner must not invent a third generation, and must not treat the
    reused revision as new. After the pointer returns to A, A is current and B
    is the noncurrent generation -- so the plan describes two revisions, not
    three.
    """
    archive_id = _new_archive(connection)
    revision_a = _generation(connection, archive_id, SHA_A)
    revision_b = _generation(connection, archive_id, SHA_B)

    with dal.transaction(connection):
        revisions = dal.RevisionRepository(connection)
        reused, created = revisions.record_or_reuse(
            archive_id=archive_id,
            archive_sha256=SHA_A,
            evidence="bytes returned to generation A",
        )
        revisions.set_current(archive_id, reused)

    assert reused == revision_a
    assert created is False

    plan = _plan(database_path)
    established = [
        row for row in plan.revisions if row.identity_state == "established"
    ]

    assert len(established) == 2
    rows = _by_id(plan)
    assert rows[revision_a].is_current is True
    assert rows[revision_b].is_current is False

    # B is newer than the current pointer, so it is kept for roll-forward
    # rather than proposed for pruning.
    assert rows[revision_b].protection_reasons == ("newer_than_current",)
    assert plan.totals["revisions"] == 3


def test_37704_generations_are_revisions_and_58201_stays_a_separate_archive(
    connection, database_path: Path
) -> None:
    """The trap the model exists to avoid, asserted on the plan's output.

    Archive 37704 carries three byte generations of one work; archive 58201 is
    a distinct identity that shares a historical digest with it. The planner
    must report 37704's generations as revisions of one archive_id and must
    not merge 58201 into that lineage on the shared digest -- merging a
    supersession relationship with a revision relationship is the specific
    wrong edge migration 014 is shaped to prevent.
    """
    archive_37704 = _new_archive(connection, file_size=41 * 1024)
    for digest in (SHA_A, SHARED, SHA_C):
        _generation(connection, archive_37704, digest)

    archive_58201 = _new_archive(connection)
    _generation(connection, archive_58201, SHARED)

    plan = _plan(
        database_path,
        policy=planner.RetentionPolicy(keep_previous_generations=0),
    )

    rows_37704 = [
        row for row in plan.revisions if row.archive_id == archive_37704
    ]
    rows_58201 = [
        row for row in plan.revisions if row.archive_id == archive_58201
    ]

    # Three byte generations plus the provisional origin, all under one id.
    assert len(rows_37704) == 4
    assert [row.revision_ordinal for row in rows_37704] == [1, 2, 3, 4]
    assert {row.archive_id for row in rows_37704} == {archive_37704}

    # The shared digest appears in both archives and joins nothing.
    shared_rows = [
        row for row in plan.revisions if row.archive_sha256 == SHARED
    ]
    assert len(shared_rows) == 2
    assert {row.archive_id for row in shared_rows} == {
        archive_37704,
        archive_58201,
    }
    assert all(row.previous_revision_id is None for row in rows_58201[:1])

    # 58201's own current revision is protected as its own, not as part of
    # 37704's lineage.
    current_58201 = next(row for row in rows_58201 if row.is_current)
    assert current_58201.protection_reasons == ("is_current_revision",)
    assert plan.totals["archives"] == 2


# --- read-only ------------------------------------------------------------


def test_planning_does_not_write_to_the_database(
    connection, database_path: Path
) -> None:
    """No mutation, proven by `data_version` and by the file's own bytes.

    `data_version` is the authoritative gate under WAL -- a WAL commit can
    leave the main database's size and mtime untouched -- so it is checked
    first. The size and mtime are recorded too, as diagnostics rather than as
    proof, which is the distinction this repository draws everywhere else.
    """
    archive_id = _new_archive(connection)
    _generation(connection, archive_id, SHA_A)
    connection.close()

    before = database_path.stat()
    snapshot = planner.plan_from_database(database_path)
    after = database_path.stat()

    assert snapshot.data_version_before == snapshot.data_version_after
    assert snapshot.data_version_unchanged is True
    assert snapshot.quick_check == "ok"
    assert after.st_size == before.st_size
    assert after.st_mtime_ns == before.st_mtime_ns


def test_a_concurrent_commit_rejects_the_plan(
    connection, database_path: Path, monkeypatch
) -> None:
    """A writer landing inside the read window invalidates the report.

    Committed from a second connection at the integrity-check point, which is
    inside the guarded window. The plan is refused rather than returned,
    because a report that mixes pre- and post-change observations cannot be
    reconciled against either state.
    """
    from comic_automation.database import read_guards

    archive_id = _new_archive(connection)
    _generation(connection, archive_id, SHA_A)
    connection.close()

    real_quick_check = read_guards.quick_check

    def commit_midway(conn: sqlite3.Connection) -> str:
        """Commit from a second connection from inside the read window.

        The integrity read happens inside the guarded transaction, so wrapping
        it puts the write at the one moment the guard has to notice. Under WAL
        the commit can leave the main database file's size and mtime
        unchanged, which is why `data_version` is the only gate that can catch
        it.
        """
        writer = dal.open_connection(database_path)
        try:
            with dal.transaction(writer):
                dal.ArchiveRepository(writer).create(file_size=1)
        finally:
            writer.close()

        return real_quick_check(conn)

    monkeypatch.setattr(planner, "quick_check", commit_midway)

    with pytest.raises(read_guards.DatabaseChangedError) as raised:
        planner.plan_from_database(database_path)

    assert "revision retention planning" in str(raised.value)


def test_a_missing_database_is_refused_rather_than_created(
    tmp_path: Path,
) -> None:
    """An audit that reports zero rows against a database it created itself
    is worse than one that fails."""
    absent = tmp_path / "absent.db"

    with pytest.raises(FileNotFoundError):
        planner.plan_from_database(absent)

    assert not absent.exists()


def test_an_invalid_retention_window_is_refused(
    database_path: Path,
) -> None:
    for bad in (-1, "1", True):
        with pytest.raises(planner.RetentionPlannerError):
            planner.RetentionPolicy(keep_previous_generations=bad)


# --- the CLI --------------------------------------------------------------


def test_the_cli_reports_a_clean_plan_and_writes_both_formats(
    connection, database_path: Path, tmp_path: Path, capsys
) -> None:
    archive_id = _new_archive(connection)
    _generation(connection, archive_id, SHA_A)
    connection.close()

    json_out = tmp_path / "plan.json"
    csv_out = tmp_path / "plan.csv"

    code = cli.main(
        [
            "--database",
            str(database_path),
            "--json-out",
            str(json_out),
            "--csv-out",
            str(csv_out),
        ]
    )
    output = capsys.readouterr().out

    assert code == cli.EXIT_OK
    assert json_out.is_file() and csv_out.is_file()
    assert "not_performed" in output
    assert "Snapshot digest:" in output
    assert "unexplained:       0" in output


def test_the_cli_fails_on_unexplained_residue_by_default(
    connection, database_path: Path, capsys
) -> None:
    """Residue is never prunable, but a plan carrying any is not reconciled."""
    archive_id = _new_archive(connection)
    _generation(connection, archive_id, SHA_A)
    _generation(connection, archive_id, SHA_B)
    _add_job(connection, archive_id, "unrecognised")
    connection.close()

    code = cli.main(["--database", str(database_path), "--keep-previous", "0"])
    captured = capsys.readouterr()

    assert code == cli.EXIT_UNEXPLAINED
    assert "unexplained = 0" in captured.err


def test_the_cli_can_be_told_to_allow_residue(
    connection, database_path: Path, capsys
) -> None:
    """The downgrade is loud, not silent."""
    archive_id = _new_archive(connection)
    _generation(connection, archive_id, SHA_A)
    _generation(connection, archive_id, SHA_B)
    _add_job(connection, archive_id, "unrecognised")
    connection.close()

    code = cli.main(
        [
            "--database",
            str(database_path),
            "--keep-previous",
            "0",
            "--allow-unexplained",
        ]
    )
    output = capsys.readouterr().out

    assert code == cli.EXIT_OK
    assert "WARNING" in output


def test_the_cli_fails_cleanly_on_a_missing_database(
    tmp_path: Path, capsys
) -> None:
    code = cli.main(["--database", str(tmp_path / "absent.db")])

    assert code == cli.EXIT_FAILED
    assert "failed" in capsys.readouterr().err.lower()


def test_the_cli_has_no_apply_path(connection, database_path: Path) -> None:
    """Asserted on the parser, so adding one cannot pass unnoticed.

    A planner that grew a `--confirm` in a later edit would still pass every
    behavioural test above; this is the test that would fail.
    """
    options = {
        option
        for action in cli.build_parser()._actions
        for option in action.option_strings
    }

    for forbidden in ("--confirm", "--apply", "--prune", "--delete", "--write"):
        assert forbidden not in options


def test_the_cli_help_touches_no_database(capsys) -> None:
    """`--help` must do nothing but print.

    This repository has entry points that perform work during startup, and
    probing one with `--help` has scanned a live share before now.
    """
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])

    assert exit_info.value.code == 0
    assert "read-only" in capsys.readouterr().out.lower()
