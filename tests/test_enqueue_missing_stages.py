"""Tests for the ingest pipeline sequencer.

Every stage pulls its own work via ``enqueue_missing()``, and nothing pushes
work from one stage to the next. That makes each stage restartable but leaves
newly imported archives waiting for an operator to run four commands in the
right order. These tests pin the sequencer that closes that gap -- and pin that
it stays a sequencer, never a second definition of any stage's eligibility.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.jobs import enqueue_missing_stages as stages_module
from comic_automation.jobs.enqueue_missing_stages import (
    STAGE_JOB_TYPES,
    STAGE_ORDER,
    enqueue_missing_stages,
)


MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


@pytest.fixture()
def database(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    with database_connection(path) as connection:
        apply_migrations(connection, MIGRATIONS)
    return path


def test_nothing_is_executed_only_enqueued(database: Path):
    """The contract is enqueue-only, and it is worth pinning.

    An earlier revision documented this module as running each stage's worker
    and as carrying an archive from discovery to fully hashed in one
    invocation. Neither was true of the code. A caller who believed it would
    conclude imports had progressed when nothing had run, so the absence of
    execution is asserted rather than assumed.
    """
    with database_connection(database) as connection:
        archive_id = int(
            connection.execute(
                "INSERT INTO archive_files (file_size) VALUES (10)"
            ).lastrowid
        )
        connection.execute(
            """
            INSERT INTO jobs (job_type, archive_id, status, priority, attempts,
                              max_attempts, available_at, created_at, updated_at)
            VALUES ('hash_archive_pages_perceptual', ?, 'pending', 100, 0, 3,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (archive_id,),
        )
        connection.commit()

    enqueue_missing_stages(database=database)

    with database_connection(database) as connection:
        statuses = dict(
            connection.execute("SELECT status, COUNT(*) FROM jobs GROUP BY 1")
        )

    # Still pending: no worker claimed it, ran it, completed it or failed it.
    assert statuses == {"pending": 1}


def test_stages_run_in_dependency_order(database: Path, monkeypatch):
    """Order is fixed by data dependencies, not by the caller's argument order.

    A later stage's eligibility predicate matches nothing until the earlier
    stage has produced its evidence, so running them out of order silently
    does nothing rather than failing loudly.
    """
    called: list[str] = []

    def fake_enqueue(connection, stage, limit):
        called.append(stage)
        return 0

    monkeypatch.setattr(stages_module, "_enqueue_for_stage", fake_enqueue)

    # Deliberately reversed: the sequencer must ignore this ordering.
    enqueue_missing_stages(database=database, stages=list(reversed(STAGE_ORDER)))

    assert called == list(STAGE_ORDER)


def test_restricting_stages_runs_only_those(database: Path, monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(
        stages_module,
        "_enqueue_for_stage",
        lambda connection, stage, limit: called.append(stage) or 0,
    )

    enqueue_missing_stages(database=database, stages=["perceptual_hash", "page_hash"])

    assert called == ["page_hash", "perceptual_hash"]


def test_unknown_stage_is_rejected(database: Path):
    """A typo must fail loudly rather than silently enqueueing nothing."""
    with pytest.raises(ValueError, match="Unknown stage"):
        enqueue_missing_stages(database=database, stages=["not_a_stage"])


def test_limit_is_passed_through_to_each_stage(database: Path, monkeypatch):
    """A bounded run must stay bounded at every stage, not just the first."""
    seen: list[int | None] = []
    monkeypatch.setattr(
        stages_module,
        "_enqueue_for_stage",
        lambda connection, stage, limit: seen.append(limit) or 0,
    )

    enqueue_missing_stages(database=database, limit=25)

    assert seen == [25] * len(STAGE_ORDER)


def test_inspect_stage_enqueues_nothing_itself(database: Path):
    """inspect_archive has no "missing" predicate to call.

    Discovery enqueues it when an archive is first seen, and the hash stage
    re-enqueues it when a file changed underneath its inspection. Inventing a
    third source here would double-enqueue; the report says so explicitly
    rather than silently reporting zero.
    """
    report = enqueue_missing_stages(database=database, stages=["inspect"])

    stage = report["stages"][0]
    assert stage["enqueued"] == 0
    assert stage["skipped_reason"]


def test_report_records_queue_movement_per_stage(database: Path, monkeypatch):
    """The report must show which queue moved, not only that a function ran."""
    monkeypatch.setattr(
        stages_module, "_enqueue_for_stage", lambda connection, stage, limit: 3
    )

    report = enqueue_missing_stages(database=database)

    assert report["total_enqueued"] == 3 * len(STAGE_ORDER)
    for stage in report["stages"]:
        assert stage["job_type"] == STAGE_JOB_TYPES[stage["stage"]]
        assert "pending_before" in stage
        assert "pending_after" in stage


def test_pending_counts_reflect_real_queue_state(database: Path):
    """pending_before/after are measured, not assumed.

    A stage that enqueues nothing because the work already exists must still
    report the queue depth truthfully, otherwise a caller cannot tell "nothing
    to do" from "nothing happened".
    """
    with database_connection(database) as connection:
        archive_id = int(
            connection.execute(
                "INSERT INTO archive_files (file_size) VALUES (10)"
            ).lastrowid
        )
        connection.execute(
            """
            INSERT INTO jobs (job_type, archive_id, status, priority, attempts,
                              max_attempts, available_at, created_at, updated_at)
            VALUES ('hash_archive_pages_perceptual', ?, 'pending', 100, 0, 3,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (archive_id,),
        )
        connection.commit()

    report = enqueue_missing_stages(database=database, stages=["perceptual_hash"])

    assert report["stages"][0]["pending_before"] == 1


def test_pipeline_applies_migrations_so_a_fresh_database_works(tmp_path: Path):
    """A database that has never been migrated must not crash the sequencer."""
    fresh = tmp_path / "fresh.db"

    report = enqueue_missing_stages(database=fresh, stages=["perceptual_hash"])

    assert report["stages"][0]["enqueued"] == 0
    with sqlite3.connect(fresh) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "jobs" in tables
