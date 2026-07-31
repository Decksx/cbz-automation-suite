from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from comic_automation.database.connection import (
    connect_database,
    database_connection,
)
from comic_automation.database.migrations import apply_migrations
from comic_automation.jobs import (
    InvalidJobTransitionError,
    JobQueue,
    JobStatus,
)
from comic_automation.jobs.abandoned_job_recovery import (
    MINIMUM_OLDER_THAN_SECONDS,
    RECOVERY_ERROR_MESSAGE,
    ExpectedCountMismatchError,
    ExpectedCountRequiredError,
    UnsafeOlderThanSecondsError,
    run_recovery,
)


MIGRATION_DIRECTORY = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)

# Fixed reference "now" so tests never depend on wall-clock timing.
NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)
OLDER_THAN_SECONDS = 3600  # 1 hour -- below MINIMUM_OLDER_THAN_SECONDS,
# so every test that recovers anything must pass allow_short_window=True.

STALE = "2026-07-30 10:00:00"
VERY_STALE = "2026-07-30 09:00:00"
FRESH = "2026-07-30 11:55:00"


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
    error_message: str | None = None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO jobs (
            job_type, status, archive_id, worker_id,
            attempts, max_attempts,
            claimed_at, started_at, completed_at, error_message
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            error_message,
        ),
    )
    return int(cursor.lastrowid)


def build_database(database: Path) -> None:
    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)


def _fetch_job(database: Path, job_id: int) -> sqlite3.Row:
    with database_connection(database) as connection:
        return connection.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()


# --- report-only mode is strictly read-only ------------------------------


def test_report_only_makes_zero_database_changes(tmp_path: Path) -> None:
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        seed_job(connection, status="claimed", claimed_at=STALE)

    before_bytes = database.read_bytes()

    output = run_recovery(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        allow_short_window=True,
        now=NOW,
    )

    after_bytes = database.read_bytes()

    assert output["mode"] == "report_only"
    assert output["applied"] is False
    assert output["would_recover_count"] == 1
    assert output["recovered_count"] == 0
    assert output["database_unchanged"] is True
    assert before_bytes == after_bytes


def test_report_only_ignores_expected_count(tmp_path: Path) -> None:
    """expected_count is only enforced for confirm=True mutations."""
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        seed_job(connection, status="claimed", claimed_at=STALE)

    output = run_recovery(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        allow_short_window=True,
        now=NOW,
        expected_count=999,  # deliberately wrong; must not matter here
    )

    assert output["applied"] is False
    assert output["would_recover_count"] == 1


# --- confirmation is required for any mutation ----------------------------


def test_confirm_without_expected_count_is_refused(
    tmp_path: Path,
) -> None:
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        seed_job(connection, status="claimed", claimed_at=STALE)

    before_bytes = database.read_bytes()

    with pytest.raises(ExpectedCountRequiredError):
        run_recovery(
            database=database,
            older_than_seconds=OLDER_THAN_SECONDS,
            allow_short_window=True,
            confirm=True,
            now=NOW,
        )

    assert database.read_bytes() == before_bytes


def test_mutation_never_happens_without_confirm_flag(
    tmp_path: Path,
) -> None:
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        job_id = seed_job(connection, status="claimed", claimed_at=STALE)

    run_recovery(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        allow_short_window=True,
        now=NOW,
        expected_count=1,
        confirm=False,
    )

    row = _fetch_job(database, job_id)
    assert row["status"] == "claimed"


# --- age-check guard -------------------------------------------------------


def test_short_window_is_rejected_without_override(
    tmp_path: Path,
) -> None:
    database = tmp_path / "recovery.db"
    build_database(database)

    with pytest.raises(UnsafeOlderThanSecondsError):
        run_recovery(
            database=database,
            older_than_seconds=MINIMUM_OLDER_THAN_SECONDS - 1,
            now=NOW,
        )


def test_short_window_permitted_with_override(tmp_path: Path) -> None:
    database = tmp_path / "recovery.db"
    build_database(database)

    output = run_recovery(
        database=database,
        older_than_seconds=1,
        allow_short_window=True,
        now=NOW,
    )

    assert output["would_recover_count"] == 0


def test_window_at_floor_does_not_require_override(
    tmp_path: Path,
) -> None:
    database = tmp_path / "recovery.db"
    build_database(database)

    output = run_recovery(
        database=database,
        older_than_seconds=MINIMUM_OLDER_THAN_SECONDS,
        now=NOW,
    )

    assert output["applied"] is False


# --- expected-count guard protects against a changed live set -------------


def test_expected_count_mismatch_is_refused_and_nothing_changes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        seed_job(connection, status="claimed", claimed_at=STALE)

    before_bytes = database.read_bytes()

    with pytest.raises(ExpectedCountMismatchError):
        run_recovery(
            database=database,
            older_than_seconds=OLDER_THAN_SECONDS,
            allow_short_window=True,
            confirm=True,
            expected_count=2,  # wrong: only one stale job exists
            now=NOW,
        )

    assert database.read_bytes() == before_bytes


def test_confirm_refuses_when_job_completed_after_review(
    tmp_path: Path,
) -> None:
    """
    A job whose status changes between the operator's report-only
    review and the --confirm call (e.g. a worker completes it) must
    never be recovered: the live re-check inside the write transaction
    must catch the mismatch and refuse to mutate anything.
    """
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        job_one = seed_job(connection, status="claimed", claimed_at=STALE)
        job_two = seed_job(connection, status="claimed", claimed_at=STALE)

    preview = run_recovery(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        allow_short_window=True,
        now=NOW,
    )
    assert preview["would_recover_count"] == 2

    # A worker completes job_one between the preview and the confirm
    # call -- exactly the race this guard exists to catch.
    with database_connection(database) as connection:
        JobQueue(connection).mark_completed(job_one, worker_id="worker-1")

    with pytest.raises(ExpectedCountMismatchError):
        run_recovery(
            database=database,
            older_than_seconds=OLDER_THAN_SECONDS,
            allow_short_window=True,
            confirm=True,
            expected_count=2,  # stale count reviewed a moment ago
            now=NOW,
        )

    # Neither job was touched: job_one remains completed (never
    # reverted to pending/failed by recovery), job_two remains claimed
    # (never recovered without a matching, reviewed count).
    completed_row = _fetch_job(database, job_one)
    claimed_row = _fetch_job(database, job_two)
    assert completed_row["status"] == "completed"
    assert claimed_row["status"] == "claimed"


# --- successful recovery ---------------------------------------------------


def test_recovery_recovers_retryable_and_exhausted_jobs(
    tmp_path: Path,
) -> None:
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        retryable_id = seed_job(
            connection,
            status="claimed",
            claimed_at=STALE,
            attempts=1,
            max_attempts=3,
        )
        exhausted_id = seed_job(
            connection,
            status="running",
            claimed_at=VERY_STALE,
            started_at=STALE,
            attempts=3,
            max_attempts=3,
        )

    output = run_recovery(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        allow_short_window=True,
        confirm=True,
        expected_count=2,
        now=NOW,
    )

    assert output["applied"] is True
    assert output["recovered_count"] == 2

    retryable_row = _fetch_job(database, retryable_id)
    exhausted_row = _fetch_job(database, exhausted_id)

    assert retryable_row["status"] == "pending"
    assert retryable_row["worker_id"] is None
    assert retryable_row["claimed_at"] is None
    assert retryable_row["started_at"] is None
    assert retryable_row["error_message"] == RECOVERY_ERROR_MESSAGE

    assert exhausted_row["status"] == "failed"
    assert exhausted_row["completed_at"] is not None
    assert exhausted_row["error_message"] == RECOVERY_ERROR_MESSAGE


def test_fresh_jobs_are_never_recovered(tmp_path: Path) -> None:
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        seed_job(connection, status="claimed", claimed_at=FRESH)

    output = run_recovery(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        allow_short_window=True,
        confirm=True,
        expected_count=0,
        now=NOW,
    )

    assert output["recovered_count"] == 0


# --- attempts and error_message preservation -------------------------------


def test_attempts_are_never_reset_by_recovery(tmp_path: Path) -> None:
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        job_id = seed_job(
            connection,
            status="claimed",
            claimed_at=STALE,
            attempts=2,
            max_attempts=5,
        )

    run_recovery(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        allow_short_window=True,
        confirm=True,
        expected_count=1,
        now=NOW,
    )

    row = _fetch_job(database, job_id)
    assert int(row["attempts"]) == 2


def test_error_message_is_already_null_before_recovery(
    tmp_path: Path,
) -> None:
    """
    Documents the "Design decision -- error_message on recovery" note
    in abandoned_job_recovery.py: a claimed/running job's error_message
    is always NULL by the time it could become a recovery candidate,
    because claim_next() clears it on every claim. This test proves
    that via the real claim_next() path (not just a seeded fixture), so
    the design decision is verified against the actual queue behavior
    it depends on.
    """
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        queue = JobQueue(connection)
        queue.enqueue("inspect_archive", max_attempts=3)
        claimed = queue.claim_next("worker-1")
        assert claimed is not None
        assert claimed.error_message is None

        connection.execute(
            "UPDATE jobs SET claimed_at = ? WHERE id = ?",
            (STALE, claimed.id),
        )
        job_id = claimed.id

    before_row = _fetch_job(database, job_id)
    assert before_row["error_message"] is None

    run_recovery(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        allow_short_window=True,
        confirm=True,
        expected_count=1,
        now=NOW,
    )

    after_row = _fetch_job(database, job_id)
    assert after_row["error_message"] == RECOVERY_ERROR_MESSAGE
    assert int(after_row["attempts"]) == 1


# --- idempotency -------------------------------------------------------


def test_second_immediate_run_recovers_nothing_new(
    tmp_path: Path,
) -> None:
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        job_id = seed_job(
            connection,
            status="claimed",
            claimed_at=STALE,
            attempts=1,
            max_attempts=3,
        )

    first = run_recovery(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        allow_short_window=True,
        confirm=True,
        expected_count=1,
        now=NOW,
    )
    assert first["recovered_count"] == 1

    preview_again = run_recovery(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        allow_short_window=True,
        now=NOW,
    )
    assert preview_again["would_recover_count"] == 0

    second = run_recovery(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        allow_short_window=True,
        confirm=True,
        expected_count=0,
        now=NOW,
    )
    assert second["recovered_count"] == 0

    row = _fetch_job(database, job_id)
    # Still pending from the first recovery; the second run did not
    # touch it again (it is no longer claimed/running).
    assert row["status"] == "pending"


# --- rollback is all-or-nothing for the batch ------------------------------


def test_exception_mid_batch_rolls_back_every_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    recover_abandoned()'s own transaction structure -- and this
    module's -- performs the whole batch inside one BEGIN IMMEDIATE /
    COMMIT. That guarantees all-or-nothing: if anything raises partway
    through, every row (including ones already updated earlier in the
    same loop) must be rolled back, not left half-recovered.
    """
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        first_id = seed_job(
            connection,
            status="claimed",
            claimed_at=STALE,
            attempts=1,
            max_attempts=3,
        )
        second_id = seed_job(
            connection,
            status="running",
            claimed_at=VERY_STALE,
            started_at=STALE,
            attempts=1,
            max_attempts=3,
        )

    import comic_automation.jobs.abandoned_job_recovery as recovery_module

    original = recovery_module._apply_recovery_row
    calls = {"count": 0}

    def poisoned(connection, *, job, now_text):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("simulated failure mid-batch")
        return original(connection, job=job, now_text=now_text)

    monkeypatch.setattr(
        recovery_module, "_apply_recovery_row", poisoned
    )

    with pytest.raises(RuntimeError, match="simulated failure"):
        run_recovery(
            database=database,
            older_than_seconds=OLDER_THAN_SECONDS,
            allow_short_window=True,
            confirm=True,
            expected_count=2,
            now=NOW,
        )

    assert calls["count"] == 2

    # Both jobs, including the one processed before the failure,
    # must remain exactly as they were: the transaction rolled back.
    first_row = _fetch_job(database, first_id)
    second_row = _fetch_job(database, second_id)

    assert first_row["status"] == "claimed"
    assert first_row["error_message"] is None
    assert second_row["status"] == "running"
    assert second_row["error_message"] is None


# --- concurrency: recovery must not race with worker writes ---------------


def test_recovery_transaction_blocks_concurrent_worker_write(
    tmp_path: Path,
) -> None:
    """
    Recovery takes its write lock with BEGIN IMMEDIATE before reading
    or writing any row (the same discipline JobQueue itself uses for
    claim_next()/recover_abandoned()). This test holds that lock open
    on one connection -- simulating the exact midpoint of a recovery
    transaction, after the stale-job SELECT but before COMMIT -- and
    proves a concurrent worker's mark_completed() on another connection
    blocks until the transaction ends, rather than interleaving with
    it. Once the lock is released, the worker's completion attempt
    correctly fails, because by then the job has already been recovered
    to 'pending' and is no longer completable -- proving the two
    operations can never both succeed on the same job.
    """
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        job_id = seed_job(
            connection,
            status="claimed",
            claimed_at=STALE,
            attempts=1,
            max_attempts=3,
        )

    # Manually replay the write-locking half of run_recovery(): open a
    # writable connection, take the write lock, and read the stale set
    # -- without committing -- to simulate holding the transaction open
    # mid-recovery.
    holder = connect_database(database)
    holder.execute("BEGIN IMMEDIATE")
    rows = holder.execute(
        """
        SELECT id FROM jobs
        WHERE status IN ('claimed', 'running')
          AND COALESCE(started_at, claimed_at) <= ?
        """,
        ("2026-07-30 11:00:00",),
    ).fetchall()
    assert len(rows) == 1

    result: dict = {}

    def worker_completes() -> None:
        with database_connection(database) as worker_connection:
            queue = JobQueue(worker_connection)
            try:
                job = queue.mark_completed(job_id, worker_id="worker-1")
                result["status"] = job.status
            except InvalidJobTransitionError as exc:
                result["error"] = str(exc)

    thread = threading.Thread(target=worker_completes)
    thread.start()

    # Give the worker thread time to attempt the write and block on
    # SQLite's busy handler; it must still be alive (blocked), proving
    # it did not interleave with the open transaction.
    time.sleep(0.3)
    assert thread.is_alive()

    # Finish the recovery-equivalent write and release the lock.
    now_text = "2026-07-30 12:00:00"
    holder.execute(
        """
        UPDATE jobs
        SET status = 'pending', available_at = ?, claimed_at = NULL,
            started_at = NULL, worker_id = NULL,
            error_message = ?, updated_at = ?
        WHERE id = ?
        """,
        (now_text, RECOVERY_ERROR_MESSAGE, now_text, job_id),
    )
    holder.execute("COMMIT")
    holder.close()

    thread.join(timeout=5)
    assert not thread.is_alive()

    # The worker's completion attempt must have failed: the job was
    # already recovered to 'pending' by the time its write unblocked,
    # so it is no longer in a completable (claimed/running) state.
    assert "error" in result
    assert "status" not in result

    row = _fetch_job(database, job_id)
    assert row["status"] == "pending"
    assert row["error_message"] == RECOVERY_ERROR_MESSAGE


# --- missing database -------------------------------------------------


def test_missing_database_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        run_recovery(
            database=tmp_path / "does-not-exist.db",
            older_than_seconds=OLDER_THAN_SECONDS,
            now=NOW,
        )
