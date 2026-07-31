"""Tests for JobQueue.enqueue_if_absent().

Every test uses a disposable `tmp_path` SQLite database. Nothing here
touches a production database, backup, report, or archive path.

The primitive under test inserts a job unless an active
('pending'/'claimed'/'running') job already exists for the same
(job_type, archive_id), using a single atomic
`INSERT ... ON CONFLICT ... DO NOTHING` statement resolved against the
`idx_jobs_unique_active` partial unique index from migration 010.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.jobs import (
    EnqueueOutcome,
    JobQueue,
    JobStatus,
)


MIGRATION_DIRECTORY = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)

ACTIVE_STATUSES = ("pending", "claimed", "running")
TERMINAL_STATUSES = ("completed", "failed", "cancelled", "blocked")


def seed_archive(connection: sqlite3.Connection) -> int:
    cursor = connection.execute(
        "INSERT INTO archive_files (file_size) VALUES (?)",
        (1024,),
    )
    return int(cursor.lastrowid)


def migrated_database(tmp_path: Path, name: str = "queue.db") -> Path:
    database = tmp_path / name

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)

    return database


def job_rows(connection: sqlite3.Connection) -> list[tuple]:
    return [
        tuple(row)
        for row in connection.execute(
            "SELECT * FROM jobs ORDER BY id"
        ).fetchall()
    ]


# --- created --------------------------------------------------------


def test_no_active_row_creates_job_with_all_supplied_fields(
    tmp_path: Path,
) -> None:
    database = migrated_database(tmp_path)
    available_at = datetime(2026, 8, 1, 9, 30, tzinfo=timezone.utc)

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        archive_id = seed_archive(connection)
        queue = JobQueue(connection)

        outcome = queue.enqueue_if_absent(
            "  inspect_archive  ",
            archive_id=archive_id,
            payload={"path": r"X:\Comics\issue.cbz"},
            priority=42,
            max_attempts=7,
            available_at=available_at,
        )

        row = connection.execute(
            "SELECT * FROM jobs WHERE archive_id = ?",
            (archive_id,),
        ).fetchone()

    assert outcome is EnqueueOutcome.CREATED
    # job_type is stripped, matching enqueue()'s own normalization.
    assert row["job_type"] == "inspect_archive"
    assert row["status"] == JobStatus.PENDING.value
    assert row["priority"] == 42
    assert row["archive_id"] == archive_id
    assert row["max_attempts"] == 7
    assert row["attempts"] == 0
    assert json.loads(row["payload_json"]) == {
        "path": r"X:\Comics\issue.cbz"
    }
    assert row["available_at"] == "2026-08-01 09:30:00"


def test_outcome_enum_has_exactly_two_members() -> None:
    assert list(EnqueueOutcome) == [
        EnqueueOutcome.CREATED,
        EnqueueOutcome.ALREADY_ACTIVE,
    ]


# --- already active --------------------------------------------------


@pytest.mark.parametrize("existing_status", ACTIVE_STATUSES)
def test_existing_active_row_reports_already_active(
    tmp_path: Path,
    existing_status: str,
) -> None:
    database = migrated_database(tmp_path, f"{existing_status}.db")

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)
        connection.execute(
            """
            INSERT INTO jobs (job_type, status, archive_id)
            VALUES ('inspect_archive', ?, ?)
            """,
            (existing_status, archive_id),
        )
        before = job_rows(connection)

        outcome = JobQueue(connection).enqueue_if_absent(
            "inspect_archive",
            archive_id=archive_id,
        )

        after = job_rows(connection)

    assert outcome is EnqueueOutcome.ALREADY_ACTIVE
    # No row inserted, and the existing row is untouched across every
    # column of the jobs table.
    assert after == before
    assert len(after) == 1


def test_collision_does_not_alter_the_existing_row(
    tmp_path: Path,
) -> None:
    database = migrated_database(tmp_path)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)
        queue = JobQueue(connection)

        original = queue.enqueue(
            "inspect_archive",
            archive_id=archive_id,
            payload={"original": True},
            priority=10,
            max_attempts=2,
        )
        before = job_rows(connection)

        # Deliberately different values for every field: none of them
        # may leak into the existing row.
        outcome = queue.enqueue_if_absent(
            "inspect_archive",
            archive_id=archive_id,
            payload={"original": False, "colliding": True},
            priority=999,
            max_attempts=9,
            available_at=datetime.now(timezone.utc)
            + timedelta(days=1),
        )

        after = job_rows(connection)
        unchanged = queue.get(original.id)

    assert outcome is EnqueueOutcome.ALREADY_ACTIVE
    assert after == before
    assert unchanged.priority == 10
    assert unchanged.max_attempts == 2
    assert unchanged.payload == {"original": True}


# --- terminal history does not block ---------------------------------


@pytest.mark.parametrize("terminal_status", TERMINAL_STATUSES)
def test_terminal_history_permits_a_new_job(
    tmp_path: Path,
    terminal_status: str,
) -> None:
    database = migrated_database(tmp_path, f"{terminal_status}.db")

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)

        # Two terminal rows of this status, to confirm history depth
        # doesn't matter either.
        for _ in range(2):
            connection.execute(
                """
                INSERT INTO jobs (job_type, status, archive_id)
                VALUES ('inspect_archive', ?, ?)
                """,
                (terminal_status, archive_id),
            )

        outcome = JobQueue(connection).enqueue_if_absent(
            "inspect_archive",
            archive_id=archive_id,
        )

        active_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM jobs
            WHERE archive_id = ?
              AND status IN ('pending', 'claimed', 'running')
            """,
            (archive_id,),
        ).fetchone()[0]
        total = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE archive_id = ?",
            (archive_id,),
        ).fetchone()[0]

    assert outcome is EnqueueOutcome.CREATED
    assert active_count == 1
    assert total == 3


def test_new_job_allowed_once_active_row_becomes_terminal(
    tmp_path: Path,
) -> None:
    database = migrated_database(tmp_path)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)
        queue = JobQueue(connection)

        first = queue.enqueue_if_absent(
            "inspect_archive",
            archive_id=archive_id,
        )
        blocked = queue.enqueue_if_absent(
            "inspect_archive",
            archive_id=archive_id,
        )

        connection.execute(
            "UPDATE jobs SET status = 'completed' WHERE archive_id = ?",
            (archive_id,),
        )

        after_completion = queue.enqueue_if_absent(
            "inspect_archive",
            archive_id=archive_id,
        )

    assert first is EnqueueOutcome.CREATED
    assert blocked is EnqueueOutcome.ALREADY_ACTIVE
    assert after_completion is EnqueueOutcome.CREATED


# --- independence across identities ----------------------------------


def test_different_job_type_or_archive_is_independent(
    tmp_path: Path,
) -> None:
    database = migrated_database(tmp_path)

    with database_connection(database) as connection:
        first_archive = seed_archive(connection)
        second_archive = seed_archive(connection)
        queue = JobQueue(connection)

        outcomes = [
            queue.enqueue_if_absent(
                "inspect_archive",
                archive_id=first_archive,
            ),
            # Same archive, different job type.
            queue.enqueue_if_absent(
                "calculate_archive_hash",
                archive_id=first_archive,
            ),
            # Same job type, different archive.
            queue.enqueue_if_absent(
                "inspect_archive",
                archive_id=second_archive,
            ),
            # Repeat of the very first one: now blocked.
            queue.enqueue_if_absent(
                "inspect_archive",
                archive_id=first_archive,
            ),
        ]

        total = connection.execute(
            "SELECT COUNT(*) FROM jobs"
        ).fetchone()[0]

    assert outcomes == [
        EnqueueOutcome.CREATED,
        EnqueueOutcome.CREATED,
        EnqueueOutcome.CREATED,
        EnqueueOutcome.ALREADY_ACTIVE,
    ]
    assert total == 3


# --- concurrency -----------------------------------------------------


def test_two_threads_racing_produce_one_created(
    tmp_path: Path,
) -> None:
    """Two threads, two real connections, released simultaneously.

    Each thread opens its own connection and waits on a
    `threading.Barrier`, so both call `enqueue_if_absent()` as close to
    simultaneously as the runtime allows -- genuinely concurrent SQLite
    behavior, not two sequential calls. Whichever thread wins the race
    is nondeterministic and deliberately not asserted; what must hold is
    that exactly one reports CREATED, the other ALREADY_ACTIVE, and only
    one active row exists afterwards.
    """
    database = tmp_path / "race.db"

    with database_connection(database) as setup:
        apply_migrations(setup, MIGRATION_DIRECTORY)
        archive_id = seed_archive(setup)

    barrier = threading.Barrier(2, timeout=15)
    results_lock = threading.Lock()
    outcomes: dict[str, EnqueueOutcome] = {}
    errors: dict[str, BaseException] = {}

    def race(name: str) -> None:
        try:
            # The connection is opened before the barrier so neither
            # thread is still paying connection/PRAGMA setup cost when
            # the race actually starts.
            with database_connection(database) as connection:
                queue = JobQueue(connection)
                barrier.wait()
                outcome = queue.enqueue_if_absent(
                    "inspect_archive",
                    archive_id=archive_id,
                )

            with results_lock:
                outcomes[name] = outcome
        except BaseException as exc:  # noqa: BLE001 - recorded, asserted below
            try:
                barrier.abort()
            except Exception:
                pass

            with results_lock:
                errors[name] = exc

    threads = [
        threading.Thread(target=race, args=(name,), name=name)
        for name in ("racer-a", "racer-b")
    ]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join(timeout=30)

    still_alive = [thread.name for thread in threads if thread.is_alive()]

    with database_connection(database) as connection:
        active_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM jobs
            WHERE archive_id = ?
              AND status IN ('pending', 'claimed', 'running')
            """,
            (archive_id,),
        ).fetchone()[0]
        total_count = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE archive_id = ?",
            (archive_id,),
        ).fetchone()[0]

    # No thread may hang (a deadlock or an un-retried SQLITE_BUSY would
    # show up here rather than as a confusing downstream failure).
    assert still_alive == [], f"threads still running: {still_alive}"
    assert errors == {}, f"thread errors: {errors}"
    assert set(outcomes) == {"racer-a", "racer-b"}
    assert Counter(outcomes.values()) == Counter(
        {
            EnqueueOutcome.CREATED: 1,
            EnqueueOutcome.ALREADY_ACTIVE: 1,
        }
    )
    assert active_count == 1
    assert total_count == 1


def test_stale_check_then_enqueue_race_is_resolved_by_the_database(
    tmp_path: Path,
) -> None:
    """The audited interleaving: B checks, A inserts, B enqueues.

    B's own "is there an active job?" query returns nothing, then A
    commits a job, then B calls enqueue_if_absent(). B must get
    ALREADY_ACTIVE despite its stale check having said otherwise.
    """
    database = tmp_path / "interleaved.db"

    with database_connection(database) as setup:
        apply_migrations(setup, MIGRATION_DIRECTORY)
        archive_id = seed_archive(setup)

    with (
        database_connection(database) as connection_a,
        database_connection(database) as connection_b,
    ):
        b_saw = connection_b.execute(
            """
            SELECT id
            FROM jobs
            WHERE archive_id = ?
              AND job_type = 'inspect_archive'
              AND status IN ('pending', 'claimed', 'running')
            LIMIT 1
            """,
            (archive_id,),
        ).fetchone()

        assert b_saw is None

        a_outcome = JobQueue(connection_a).enqueue_if_absent(
            "inspect_archive",
            archive_id=archive_id,
        )
        b_outcome = JobQueue(connection_b).enqueue_if_absent(
            "inspect_archive",
            archive_id=archive_id,
        )

        active_count = connection_b.execute(
            """
            SELECT COUNT(*)
            FROM jobs
            WHERE archive_id = ?
              AND status IN ('pending', 'claimed', 'running')
            """,
            (archive_id,),
        ).fetchone()[0]

    assert a_outcome is EnqueueOutcome.CREATED
    assert b_outcome is EnqueueOutcome.ALREADY_ACTIVE
    assert active_count == 1


# --- unrelated integrity errors propagate ----------------------------


def test_foreign_key_violation_propagates(tmp_path: Path) -> None:
    """An unknown archive_id must raise, not report ALREADY_ACTIVE."""
    database = migrated_database(tmp_path)

    with database_connection(database) as connection:
        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            JobQueue(connection).enqueue_if_absent(
                "inspect_archive",
                archive_id=999_999,
            )

        message = str(exc_info.value).lower()

        inserted = connection.execute(
            "SELECT COUNT(*) FROM jobs"
        ).fetchone()[0]

    assert "foreign key" in message
    assert "unique" not in message
    assert inserted == 0


def test_unrelated_not_null_violation_propagates(
    tmp_path: Path,
) -> None:
    """A different constraint entirely must also still raise.

    Guards the "do not catch or reinterpret unrelated integrity errors"
    requirement beyond the foreign-key case: the ON CONFLICT target is
    scoped to one index, so any other constraint failure surfaces
    normally.
    """
    database = migrated_database(tmp_path)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)

        # A trigger standing in for "some other constraint on jobs",
        # including one a future migration might add.
        connection.execute(
            """
            CREATE TRIGGER reject_inspect_archive
            BEFORE INSERT ON jobs
            WHEN NEW.job_type = 'inspect_archive'
            BEGIN
                SELECT RAISE(ABORT, 'unrelated constraint failure');
            END
            """
        )

        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            JobQueue(connection).enqueue_if_absent(
                "inspect_archive",
                archive_id=archive_id,
            )

    assert "unrelated constraint failure" in str(exc_info.value)


# --- validation happens before any insert ----------------------------


def test_null_archive_id_is_rejected_before_insert(
    tmp_path: Path,
) -> None:
    database = migrated_database(tmp_path)

    with database_connection(database) as connection:
        with pytest.raises(ValueError, match="archive_id"):
            JobQueue(connection).enqueue_if_absent(
                "inspect_archive",
                archive_id=None,
            )

        inserted = connection.execute(
            "SELECT COUNT(*) FROM jobs"
        ).fetchone()[0]

    assert inserted == 0


def test_blank_job_type_is_rejected_before_insert(
    tmp_path: Path,
) -> None:
    database = migrated_database(tmp_path)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)

        for blank in ("", "   ", "\t\n"):
            with pytest.raises(ValueError, match="job_type"):
                JobQueue(connection).enqueue_if_absent(
                    blank,
                    archive_id=archive_id,
                )

        inserted = connection.execute(
            "SELECT COUNT(*) FROM jobs"
        ).fetchone()[0]

    assert inserted == 0


def test_invalid_max_attempts_is_rejected_before_insert(
    tmp_path: Path,
) -> None:
    database = migrated_database(tmp_path)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)

        for invalid in (0, -1):
            with pytest.raises(ValueError, match="max_attempts"):
                JobQueue(connection).enqueue_if_absent(
                    "inspect_archive",
                    archive_id=archive_id,
                    max_attempts=invalid,
                )

        inserted = connection.execute(
            "SELECT COUNT(*) FROM jobs"
        ).fetchone()[0]

    assert inserted == 0


# --- transaction composition -----------------------------------------


def test_composes_inside_a_caller_owned_transaction(
    tmp_path: Path,
) -> None:
    """The helper must not commit or roll back the caller's work.

    It issues no transaction control of its own, so a caller that opened
    BEGIN IMMEDIATE stays inside that transaction across the call, and
    the caller's own ROLLBACK still discards everything -- including
    the job the helper inserted.
    """
    database = migrated_database(tmp_path)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)
        queue = JobQueue(connection)

        connection.execute("BEGIN IMMEDIATE")
        outcome = queue.enqueue_if_absent(
            "inspect_archive",
            archive_id=archive_id,
        )

        # Still inside the caller's transaction: the helper neither
        # committed nor rolled back.
        still_in_transaction = connection.in_transaction
        visible_inside = connection.execute(
            "SELECT COUNT(*) FROM jobs"
        ).fetchone()[0]

        connection.execute("ROLLBACK")

        after_rollback = connection.execute(
            "SELECT COUNT(*) FROM jobs"
        ).fetchone()[0]

    assert outcome is EnqueueOutcome.CREATED
    assert still_in_transaction is True
    assert visible_inside == 1
    # The caller's rollback discarded the insert, proving the helper
    # never committed it independently.
    assert after_rollback == 0


def test_caller_transaction_commit_persists_the_job(
    tmp_path: Path,
) -> None:
    database = migrated_database(tmp_path)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)
        queue = JobQueue(connection)

        connection.execute("BEGIN IMMEDIATE")
        first = queue.enqueue_if_absent(
            "inspect_archive",
            archive_id=archive_id,
        )
        # A second call inside the same transaction sees the first
        # insert and reports the conflict without ending the
        # transaction.
        second = queue.enqueue_if_absent(
            "inspect_archive",
            archive_id=archive_id,
        )
        assert connection.in_transaction is True
        connection.execute("COMMIT")

        persisted = connection.execute(
            "SELECT COUNT(*) FROM jobs"
        ).fetchone()[0]

    assert first is EnqueueOutcome.CREATED
    assert second is EnqueueOutcome.ALREADY_ACTIVE
    assert persisted == 1


# --- enqueue() is unchanged ------------------------------------------


def test_plain_enqueue_still_raises_on_duplicate_active_job(
    tmp_path: Path,
) -> None:
    """enqueue() keeps its existing behavior: it does not swallow the
    conflict, it raises. Only enqueue_if_absent() is conflict-safe.
    """
    database = migrated_database(tmp_path)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)
        queue = JobQueue(connection)

        queue.enqueue("inspect_archive", archive_id=archive_id)

        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            queue.enqueue("inspect_archive", archive_id=archive_id)

    assert "unique" in str(exc_info.value).lower()


def test_plain_enqueue_still_works_for_null_archive_id(
    tmp_path: Path,
) -> None:
    """enqueue() still accepts a NULL archive_id; only the new helper
    requires one. Confirms enqueue()'s signature/behavior is untouched.
    """
    database = migrated_database(tmp_path)

    with database_connection(database) as connection:
        queue = JobQueue(connection)

        first = queue.enqueue("inspect_archive")
        second = queue.enqueue("inspect_archive")

    assert first.archive_id is None
    assert second.archive_id is None
    assert first.id != second.id
