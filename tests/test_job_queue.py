"""Tests for comic_automation.jobs.JobQueue: the SQLite-backed persistent
job queue used by worker processes.

Covers the full job lifecycle -- enqueue, priority+FIFO claiming, job-type
filtering, running/completing/failing, retry-with-backoff, permanent
failure, abandoned-job recovery, and the SQLite-level guarantee that only
one connection can claim a given job even under concurrent access.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.jobs import (
    InvalidJobTransitionError,
    JobQueue,
    JobStatus,
)


MIGRATION_DIRECTORY = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


@pytest.fixture
def queue(tmp_path: Path):
    """A JobQueue backed by a fresh, fully-migrated SQLite database, unique
    per test via tmp_path.
    """
    database_path = tmp_path / "queue.db"

    with database_connection(database_path) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        yield JobQueue(connection)


def test_enqueue_and_read_payload(queue: JobQueue) -> None:
    """A newly enqueued job should start life as PENDING with zero
    attempts, and round-trip its priority, max_attempts, and JSON payload
    exactly as given.
    """
    job = queue.enqueue(
        "inspect_archive",
        payload={"path": r"X:\Series\Issue.cbz"},
        priority=50,
        max_attempts=4,
    )

    assert job.status == JobStatus.PENDING
    assert job.priority == 50
    assert job.attempts == 0
    assert job.max_attempts == 4
    assert job.payload == {
        "path": r"X:\Series\Issue.cbz"
    }


def test_claim_uses_priority_then_fifo(
    queue: JobQueue,
) -> None:
    """claim_next() should honor priority first (lower number = claimed
    first), and fall back to FIFO (enqueue order) to break ties between
    jobs sharing the same priority.
    """
    normal = queue.enqueue(
        "inspect_archive",
        priority=100,
    )
    first_high = queue.enqueue(
        "calculate_archive_hash",
        priority=10,
    )
    second_high = queue.enqueue(
        "inventory_pages",
        priority=10,
    )

    claimed_one = queue.claim_next("worker-1")
    claimed_two = queue.claim_next("worker-1")
    claimed_three = queue.claim_next("worker-1")

    assert claimed_one is not None
    assert claimed_two is not None
    assert claimed_three is not None

    assert claimed_one.id == first_high.id
    assert claimed_two.id == second_high.id
    assert claimed_three.id == normal.id


def test_claim_can_filter_job_types(
    queue: JobQueue,
) -> None:
    """Passing job_types to claim_next() should restrict claiming to jobs
    of that type, skipping over a higher-priority job of a type the caller
    didn't ask for.
    """
    queue.enqueue("inspect_archive", priority=1)
    hash_job = queue.enqueue(
        "calculate_archive_hash",
        priority=100,
    )

    claimed = queue.claim_next(
        "hash-worker",
        job_types=["calculate_archive_hash"],
    )

    assert claimed is not None
    assert claimed.id == hash_job.id
    assert claimed.worker_id == "hash-worker"
    assert claimed.attempts == 1


def test_future_job_is_not_claimed(
    queue: JobQueue,
) -> None:
    """A job scheduled with available_at set in the future should not be
    claimable yet, even when nothing else is competing for the claim.
    """
    queue.enqueue(
        "inspect_archive",
        available_at=(
            datetime.now(timezone.utc)
            + timedelta(hours=1)
        ),
    )

    assert queue.claim_next("worker-1") is None


def test_job_lifecycle(queue: JobQueue) -> None:
    """End-to-end happy path: enqueue -> claim (CLAIMED) -> mark_running
    (RUNNING, started_at set) -> mark_completed (COMPLETED, completed_at
    set).
    """
    queued = queue.enqueue("inspect_archive")
    claimed = queue.claim_next("worker-1")

    assert claimed is not None
    assert claimed.id == queued.id
    assert claimed.status == JobStatus.CLAIMED

    running = queue.mark_running(
        claimed.id,
        worker_id="worker-1",
    )

    assert running.status == JobStatus.RUNNING
    assert running.started_at is not None

    completed = queue.mark_completed(
        running.id,
        worker_id="worker-1",
    )

    assert completed.status == JobStatus.COMPLETED
    assert completed.completed_at is not None


def test_wrong_worker_cannot_complete_job(
    queue: JobQueue,
) -> None:
    """A worker_id that doesn't match who actually claimed the job must be
    rejected with InvalidJobTransitionError -- prevents one worker from
    completing (or otherwise mutating) a job another worker owns.
    """
    queue.enqueue("inspect_archive")
    claimed = queue.claim_next("worker-1")

    assert claimed is not None

    with pytest.raises(InvalidJobTransitionError):
        queue.mark_completed(
            claimed.id,
            worker_id="worker-2",
        )


def test_failed_job_retries_until_max_attempts(
    queue: JobQueue,
) -> None:
    """A transient failure (mark_failed without permanent=True) should
    return the job to PENDING for another attempt, while releasing the
    worker_id claim. Once attempts reaches max_attempts, the next failure
    should move the job to a terminal FAILED state with the error message
    recorded.
    """
    queue.enqueue(
        "inspect_archive",
        max_attempts=2,
    )

    first_claim = queue.claim_next("worker-1")
    assert first_claim is not None

    retried = queue.mark_failed(
        first_claim.id,
        "Temporary read error",
        worker_id="worker-1",
    )

    assert retried.status == JobStatus.PENDING
    assert retried.attempts == 1
    assert retried.worker_id is None

    second_claim = queue.claim_next("worker-2")
    assert second_claim is not None
    assert second_claim.attempts == 2

    failed = queue.mark_failed(
        second_claim.id,
        "Permanent read error",
        worker_id="worker-2",
    )

    assert failed.status == JobStatus.FAILED
    assert failed.completed_at is not None
    assert failed.error_message == "Permanent read error"


def test_retry_delay_prevents_immediate_claim(
    queue: JobQueue,
) -> None:
    """mark_failed(retry_delay_seconds=...) should schedule the retried job
    into the future via available_at, so it isn't immediately claimable
    again by another worker.
    """
    queue.enqueue("inspect_archive")
    claimed = queue.claim_next("worker-1")

    assert claimed is not None

    retried = queue.mark_failed(
        claimed.id,
        "Try later",
        retry_delay_seconds=3600,
    )

    assert retried.status == JobStatus.PENDING
    assert queue.claim_next("worker-2") is None


def test_permanent_failure_skips_remaining_attempts(
    queue: JobQueue,
) -> None:
    """mark_failed(permanent=True) should move straight to FAILED
    regardless of remaining attempts budget -- for errors (e.g. a corrupt
    archive) that retrying can never fix.
    """
    queue.enqueue("inspect_archive", max_attempts=3)
    claimed = queue.claim_next("worker-1")

    assert claimed is not None

    failed = queue.mark_failed(
        claimed.id,
        "Corrupt archive",
        worker_id="worker-1",
        permanent=True,
    )

    assert failed.status == JobStatus.FAILED
    assert failed.attempts == 1
    assert failed.completed_at is not None


def test_claim_excludes_jobs_seen_by_caller(
    queue: JobQueue,
) -> None:
    """excluded_job_ids lets a caller skip over specific jobs it already
    knows about (e.g. jobs it just failed in-process), claiming the next
    eligible job instead.
    """
    first = queue.enqueue("inspect_archive")
    second = queue.enqueue("inspect_archive")

    claimed = queue.claim_next(
        "worker-1",
        excluded_job_ids=[first.id],
    )

    assert claimed is not None
    assert claimed.id == second.id


def test_recover_abandoned_job(queue: JobQueue) -> None:
    """recover_abandoned() should find jobs claimed long ago (simulated
    here by backdating claimed_at directly in SQL) and, if attempts budget
    remains, return them to PENDING with the claim released and a
    standard abandonment error message recorded.
    """
    queued = queue.enqueue(
        "inspect_archive",
        max_attempts=3,
    )
    claimed = queue.claim_next("dead-worker")

    assert claimed is not None
    assert claimed.id == queued.id

    # Simulate a worker that claimed the job and then died: backdate
    # claimed_at directly so recover_abandoned() sees it as stale.
    queue.connection.execute(
        """
        UPDATE jobs
        SET claimed_at = '2000-01-01 00:00:00'
        WHERE id = ?
        """,
        (claimed.id,),
    )

    recovered_count = queue.recover_abandoned(
        older_than_seconds=60,
    )
    recovered = queue.get(claimed.id)

    assert recovered_count == 1
    assert recovered.status == JobStatus.PENDING
    assert recovered.worker_id is None
    assert (
        recovered.error_message
        == "Recovered after worker abandonment."
    )


def test_recovery_fails_exhausted_job(
    queue: JobQueue,
) -> None:
    """If an abandoned job has already exhausted its attempts budget
    (max_attempts=1 here, already used), recover_abandoned() should move
    it to FAILED instead of retrying it again.
    """
    queue.enqueue(
        "inspect_archive",
        max_attempts=1,
    )
    claimed = queue.claim_next("dead-worker")

    assert claimed is not None

    queue.connection.execute(
        """
        UPDATE jobs
        SET claimed_at = '2000-01-01 00:00:00'
        WHERE id = ?
        """,
        (claimed.id,),
    )

    recovered_count = queue.recover_abandoned(
        older_than_seconds=60,
    )
    recovered = queue.get(claimed.id)

    assert recovered_count == 1
    assert recovered.status == JobStatus.FAILED
    assert recovered.completed_at is not None


def test_only_one_connection_can_claim_job(
    tmp_path: Path,
) -> None:
    """Concurrency guard: with two separate SQLite connections to the same
    database, only one of them should be able to claim a given pending job
    -- the second connection's claim_next() must return None rather than
    double-claiming it.
    """
    database_path = tmp_path / "concurrent.db"

    with database_connection(database_path) as first:
        apply_migrations(first, MIGRATION_DIRECTORY)
        first_queue = JobQueue(first)
        queued = first_queue.enqueue("inspect_archive")

    with (
        database_connection(database_path) as first,
        database_connection(database_path) as second,
    ):
        first_queue = JobQueue(first)
        second_queue = JobQueue(second)

        first_claim = first_queue.claim_next("worker-1")
        second_claim = second_queue.claim_next("worker-2")

        assert first_claim is not None
        assert first_claim.id == queued.id
        assert second_claim is None
