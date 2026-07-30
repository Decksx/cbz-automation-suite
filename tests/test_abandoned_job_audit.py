from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.jobs.abandoned_job_audit import (
    PROJECTED_OUTCOME_RETRYABLE,
    PROJECTED_OUTCOME_TERMINAL,
    OutputPathCollisionError,
    collect_stale_jobs,
    fingerprint_database,
    readonly_database_connection,
    run_audit,
)


MIGRATION_DIRECTORY = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)

# Fixed reference "now" so tests never depend on wall-clock timing.
NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)
OLDER_THAN_SECONDS = 3600  # 1 hour
CUTOFF = NOW - timedelta(seconds=OLDER_THAN_SECONDS)  # 11:00:00

FRESH = "2026-07-30 11:30:00"  # inside the window, not stale
AT_CUTOFF = "2026-07-30 11:00:00"  # exactly on the boundary
STALE = "2026-07-30 10:00:00"  # comfortably outside the window
VERY_STALE = "2026-07-30 09:00:00"


def seed_job(
    connection: sqlite3.Connection,
    *,
    job_type: str = "inspect_archive",
    status: str = "claimed",
    archive_id: int | None = None,
    worker_id: str | None = "worker-1",
    attempts: int = 1,
    max_attempts: int = 3,
    claimed_at: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO jobs (
            job_type, status, archive_id, worker_id,
            attempts, max_attempts,
            claimed_at, started_at, completed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_type,
            status,
            archive_id,
            worker_id,
            attempts,
            max_attempts,
            claimed_at,
            started_at,
            completed_at,
        ),
    )
    return int(cursor.lastrowid)


def build_database(database: Path) -> None:
    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)


# --- basic inclusion/exclusion ------------------------------------------


def test_no_stale_jobs_produces_empty_report(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"
    build_database(database)

    output = run_audit(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        now=NOW,
    )

    assert output["stale_job_count"] == 0
    assert output["jobs"] == []
    assert output["status_counts"] == {}
    assert output["job_type_counts"] == {}
    assert output["projected_outcome_counts"] == {}


def test_stale_claimed_job_is_included(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        job_id = seed_job(
            connection,
            status="claimed",
            claimed_at=STALE,
            started_at=None,
        )

    output = run_audit(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        now=NOW,
    )

    assert output["stale_job_count"] == 1
    assert output["jobs"][0]["job_id"] == job_id
    assert output["jobs"][0]["status"] == "claimed"


def test_stale_running_job_is_included(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        job_id = seed_job(
            connection,
            status="running",
            claimed_at=VERY_STALE,
            started_at=STALE,
        )

    output = run_audit(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        now=NOW,
    )

    assert output["stale_job_count"] == 1
    assert output["jobs"][0]["job_id"] == job_id
    assert output["jobs"][0]["status"] == "running"


def test_fresh_claimed_and_running_jobs_are_excluded(
    tmp_path: Path,
) -> None:
    database = tmp_path / "audit.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        seed_job(
            connection,
            status="claimed",
            claimed_at=FRESH,
            started_at=None,
        )
        seed_job(
            connection,
            status="running",
            claimed_at=STALE,
            started_at=FRESH,
        )

    output = run_audit(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        now=NOW,
    )

    assert output["stale_job_count"] == 0


def test_other_statuses_are_never_candidates(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)

        for status in (
            "pending",
            "completed",
            "failed",
            "cancelled",
            "blocked",
        ):
            seed_job(
                connection,
                status=status,
                claimed_at=VERY_STALE,
                started_at=VERY_STALE,
                completed_at=(
                    VERY_STALE
                    if status in ("completed", "failed", "cancelled")
                    else None
                ),
            )

    output = run_audit(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        now=NOW,
    )

    assert output["stale_job_count"] == 0
    assert output["jobs"] == []


# --- effective activity timestamp ---------------------------------------


def test_started_at_takes_precedence_over_claimed_at(
    tmp_path: Path,
) -> None:
    database = tmp_path / "audit.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)

        # Claimed long ago, but actively running recently: the fresh
        # started_at must win, so this job is NOT reported as stale.
        actively_running_id = seed_job(
            connection,
            status="running",
            claimed_at=VERY_STALE,
            started_at=FRESH,
        )

        # Claimed recently (e.g. reclaimed), but its started_at is
        # stale: the stale started_at must win, so this job IS
        # reported as stale, using started_at as the effective
        # timestamp rather than the more recent claimed_at.
        stale_running_id = seed_job(
            connection,
            status="running",
            claimed_at=FRESH,
            started_at=STALE,
        )

    output = run_audit(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        now=NOW,
    )

    reported_ids = {job["job_id"] for job in output["jobs"]}
    assert actively_running_id not in reported_ids
    assert stale_running_id in reported_ids

    stale_job = next(
        job
        for job in output["jobs"]
        if job["job_id"] == stale_running_id
    )
    assert stale_job["effective_activity_at"] == STALE


# --- cutoff boundary ------------------------------------------------------


def test_cutoff_boundary_is_inclusive(tmp_path: Path) -> None:
    """
    A job whose effective activity timestamp lands exactly on the
    cutoff (age == older_than_seconds) is documented and tested here
    as stale, matching JobQueue.recover_abandoned()'s own `<=`
    predicate exactly.
    """
    database = tmp_path / "audit.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        on_boundary_id = seed_job(
            connection,
            status="claimed",
            claimed_at=AT_CUTOFF,
            started_at=None,
        )
        just_inside_id = seed_job(
            connection,
            status="claimed",
            claimed_at="2026-07-30 11:00:01",
            started_at=None,
        )

    output = run_audit(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        now=NOW,
    )

    reported_ids = {job["job_id"] for job in output["jobs"]}
    assert on_boundary_id in reported_ids
    assert just_inside_id not in reported_ids

    boundary_job = next(
        job for job in output["jobs"] if job["job_id"] == on_boundary_id
    )
    assert boundary_job["age_seconds"] == float(OLDER_THAN_SECONDS)
    assert output["cutoff_utc"] == AT_CUTOFF


# --- projected outcome (informational only) -------------------------------


def test_attempts_remaining_projects_pending(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        job_id = seed_job(
            connection,
            status="claimed",
            claimed_at=STALE,
            attempts=1,
            max_attempts=3,
        )

    output = run_audit(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        now=NOW,
    )

    job = next(j for j in output["jobs"] if j["job_id"] == job_id)
    assert job["projected_outcome"] == PROJECTED_OUTCOME_RETRYABLE
    assert job["projected_outcome"] == "pending"


def test_exhausted_attempts_projects_failed(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        job_id = seed_job(
            connection,
            status="running",
            claimed_at=VERY_STALE,
            started_at=STALE,
            attempts=3,
            max_attempts=3,
        )

    output = run_audit(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        now=NOW,
    )

    job = next(j for j in output["jobs"] if j["job_id"] == job_id)
    assert job["projected_outcome"] == PROJECTED_OUTCOME_TERMINAL
    assert job["projected_outcome"] == "failed"


# --- ordering --------------------------------------------------------------


def test_ordering_is_stable_by_activity_then_id(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)

        # Inserted out of chronological order on purpose.
        middle_id = seed_job(
            connection, status="claimed", claimed_at="2026-07-30 10:30:00"
        )
        oldest_id = seed_job(
            connection, status="claimed", claimed_at=VERY_STALE
        )
        newest_id = seed_job(
            connection, status="claimed", claimed_at=STALE
        )
        # Two jobs tied on the same effective timestamp: must break
        # the tie by ascending job id.
        tie_low_id = seed_job(
            connection, status="claimed", claimed_at="2026-07-30 08:00:00"
        )
        tie_high_id = seed_job(
            connection, status="claimed", claimed_at="2026-07-30 08:00:00"
        )

    output = run_audit(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        now=NOW,
    )

    # Chronological order (ascending): tie_low/tie_high (08:00, tied,
    # broken by id), oldest (VERY_STALE=09:00), newest (STALE=10:00),
    # middle (10:30).
    ordered_ids = [job["job_id"] for job in output["jobs"]]
    assert ordered_ids == [
        tie_low_id,
        tie_high_id,
        oldest_id,
        newest_id,
        middle_id,
    ]


# --- aggregate reconciliation -----------------------------------------


def test_aggregate_counts_reconcile_with_detail_rows(
    tmp_path: Path,
) -> None:
    database = tmp_path / "audit.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        seed_job(
            connection,
            job_type="inspect_archive",
            status="claimed",
            claimed_at=STALE,
            attempts=1,
            max_attempts=3,
        )
        seed_job(
            connection,
            job_type="inspect_archive",
            status="running",
            claimed_at=VERY_STALE,
            started_at=STALE,
            attempts=3,
            max_attempts=3,
        )
        seed_job(
            connection,
            job_type="calculate_archive_hash",
            status="claimed",
            claimed_at=VERY_STALE,
            attempts=2,
            max_attempts=2,
        )

    output = run_audit(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        now=NOW,
    )

    assert output["stale_job_count"] == 3
    assert sum(output["status_counts"].values()) == 3
    assert sum(output["job_type_counts"].values()) == 3
    assert sum(output["projected_outcome_counts"].values()) == 3
    assert output["status_counts"] == {"claimed": 2, "running": 1}
    assert output["job_type_counts"] == {
        "calculate_archive_hash": 1,
        "inspect_archive": 2,
    }
    assert output["projected_outcome_counts"] == {
        "failed": 2,
        "pending": 1,
    }


# --- output generation ------------------------------------------------


def test_json_and_csv_contain_the_same_candidates(
    tmp_path: Path,
) -> None:
    database = tmp_path / "audit.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        seed_job(connection, status="claimed", claimed_at=STALE)
        seed_job(
            connection,
            status="running",
            claimed_at=VERY_STALE,
            started_at=STALE,
        )

    json_output = tmp_path / "reports" / "stale.json"
    csv_output = tmp_path / "reports" / "stale.csv"

    output = run_audit(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        now=NOW,
        json_output=json_output,
        csv_output=csv_output,
    )

    assert output["stale_job_count"] == 2

    payload = json.loads(json_output.read_text(encoding="utf-8"))
    json_ids = {job["job_id"] for job in payload["jobs"]}

    import csv as csv_module

    with csv_output.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv_module.DictReader(stream))
    csv_ids = {int(row["job_id"]) for row in rows}

    assert json_ids == csv_ids
    assert json_ids == {job["job_id"] for job in output["jobs"]}
    assert len(rows) == 2


# --- output path collision safety ---------------------------------------


def _seeded_database(tmp_path: Path) -> Path:
    database = tmp_path / "audit.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        seed_job(connection, status="claimed", claimed_at=STALE)

    return database


def test_json_output_cannot_equal_database_path(tmp_path: Path) -> None:
    database = _seeded_database(tmp_path)
    before_bytes = database.read_bytes()

    with pytest.raises(OutputPathCollisionError):
        run_audit(
            database=database,
            older_than_seconds=OLDER_THAN_SECONDS,
            now=NOW,
            json_output=database,
        )

    assert database.read_bytes() == before_bytes


def test_json_output_cannot_alias_database_via_hardlink(
    tmp_path: Path,
) -> None:
    database = _seeded_database(tmp_path)
    before_bytes = database.read_bytes()

    alias = tmp_path / "alias.json"
    os.link(database, alias)

    with pytest.raises(OutputPathCollisionError):
        run_audit(
            database=database,
            older_than_seconds=OLDER_THAN_SECONDS,
            now=NOW,
            json_output=alias,
        )

    assert database.read_bytes() == before_bytes


def test_csv_output_cannot_equal_database_path(tmp_path: Path) -> None:
    database = _seeded_database(tmp_path)
    before_bytes = database.read_bytes()

    with pytest.raises(OutputPathCollisionError):
        run_audit(
            database=database,
            older_than_seconds=OLDER_THAN_SECONDS,
            now=NOW,
            csv_output=database,
        )

    assert database.read_bytes() == before_bytes


def test_csv_output_cannot_alias_database_via_hardlink(
    tmp_path: Path,
) -> None:
    database = _seeded_database(tmp_path)
    before_bytes = database.read_bytes()

    alias = tmp_path / "alias.csv"
    os.link(database, alias)

    with pytest.raises(OutputPathCollisionError):
        run_audit(
            database=database,
            older_than_seconds=OLDER_THAN_SECONDS,
            now=NOW,
            csv_output=alias,
        )

    assert database.read_bytes() == before_bytes


def test_json_and_csv_outputs_cannot_target_the_same_file(
    tmp_path: Path,
) -> None:
    database = _seeded_database(tmp_path)
    before_bytes = database.read_bytes()

    shared = tmp_path / "shared-report"

    with pytest.raises(OutputPathCollisionError):
        run_audit(
            database=database,
            older_than_seconds=OLDER_THAN_SECONDS,
            now=NOW,
            json_output=shared,
            csv_output=shared,
        )

    assert database.read_bytes() == before_bytes
    # Neither the report path nor anything else was created: the
    # collision must be rejected before any output writing begins.
    assert not shared.exists()


def test_rejected_collision_creates_no_output_directories(
    tmp_path: Path,
) -> None:
    database = _seeded_database(tmp_path)

    nested_json = tmp_path / "reports" / "nested" / "stale.json"

    with pytest.raises(OutputPathCollisionError):
        run_audit(
            database=database,
            older_than_seconds=OLDER_THAN_SECONDS,
            now=NOW,
            json_output=database,
            csv_output=nested_json,
        )

    # The database-colliding json_output is checked first and raises
    # before csv_output's parent directories would be created.
    assert not nested_json.parent.exists()


# --- privacy / safety ---------------------------------------------------


def test_report_excludes_payload_error_and_paths(
    tmp_path: Path,
) -> None:
    database = tmp_path / "audit.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        connection.execute(
            """
            INSERT INTO jobs (
                job_type, status, worker_id, attempts, max_attempts,
                claimed_at, payload_json, error_message
            )
            VALUES (
                'inspect_archive', 'claimed', 'worker-1', 1, 3,
                ?, ?, ?
            )
            """,
            (
                STALE,
                '{"path": "X:\\\\Comics\\\\Series\\\\issue.cbz"}',
                "Something failed at X:\\Comics\\Series\\issue.cbz",
            ),
        )

    output = run_audit(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        now=NOW,
    )

    assert output["stale_job_count"] == 1
    job = output["jobs"][0]

    assert "payload_json" not in job
    assert "payload" not in job
    assert "error_message" not in job
    assert "current_path" not in job

    serialized = json.dumps(output)
    assert "Comics" not in serialized
    assert "issue.cbz" not in serialized


# --- read-only guarantees -----------------------------------------------


def test_read_only_connection_rejects_writes(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"
    build_database(database)

    with readonly_database_connection(database) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute(
                "UPDATE jobs SET status = 'pending' WHERE id = 1"
            )


def test_run_audit_leaves_database_byte_identical(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        seed_job(connection, status="claimed", claimed_at=STALE)
        seed_job(
            connection,
            status="running",
            claimed_at=VERY_STALE,
            started_at=STALE,
        )

    before = fingerprint_database(database)
    before_bytes = database.read_bytes()

    output = run_audit(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        now=NOW,
    )

    after = fingerprint_database(database)
    after_bytes = database.read_bytes()

    assert before == after
    assert before_bytes == after_bytes
    assert output["database_unchanged"] is True
    assert (
        output["database_size_bytes_before"]
        == output["database_size_bytes_after"]
    )
    assert (
        output["database_modified_time_ns_before"]
        == output["database_modified_time_ns_after"]
    )


def test_missing_database_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        run_audit(
            database=tmp_path / "does-not-exist.db",
            older_than_seconds=OLDER_THAN_SECONDS,
        )


# --- collect_stale_jobs (module-level helper used directly) -----------


def test_collect_stale_jobs_rejects_negative_older_than_seconds(
    tmp_path: Path,
) -> None:
    database = tmp_path / "audit.db"
    build_database(database)

    with readonly_database_connection(database) as connection:
        with pytest.raises(ValueError):
            collect_stale_jobs(
                connection,
                older_than_seconds=-1,
                now=NOW,
            )
