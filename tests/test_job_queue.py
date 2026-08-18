"""Tests for comic_automation.jobs.JobQueue: the SQLite-backed persistent
job queue used by worker processes.

Covers the full job lifecycle -- enqueue, priority+FIFO claiming, job-type
filtering, running/completing/failing, retry-with-backoff, permanent
failure, abandoned-job recovery, and the SQLite-level guarantee that only
one connection can claim a given job even under concurrent access.
"""

from __future__ import annotations

import sqlite3
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


# ---------------------------------------------------------------------------
# Cancellation
#
# JobStatus.CANCELLED existed from the start with no transition reaching it.
# These tests pin the transition that closes that gap and, more importantly,
# the things it refuses to do.
# ---------------------------------------------------------------------------


class _FailingCommitConnection:
    """Delegates to a real connection but makes COMMIT fail.

    A proxy rather than a monkeypatched method because sqlite3.Connection is a
    C type and does not accept attribute assignment. Only the surface cancel()
    uses is forwarded, so a future call it does not cover surfaces as an
    AttributeError rather than silently bypassing the injected failure.
    """

    def __init__(self, real) -> None:
        self._real = real

    def execute(self, sql: str, *args):
        if sql.strip().upper().startswith("COMMIT"):
            raise sqlite3.OperationalError("disk I/O error")
        return self._real.execute(sql, *args)

    @property
    def in_transaction(self) -> bool:
        return self._real.in_transaction

    @property
    def row_factory(self):
        return self._real.row_factory


def _archive(queue: JobQueue, archive_id: int) -> int:
    """Create the archive_files row that jobs.archive_id references.

    These tests use real archive ids because idx_jobs_unique_active is a
    partial index on (job_type, archive_id) and does not constrain NULL, so a
    test that left archive_id unset would not exercise the identity behaviour
    cancellation depends on.
    """
    queue.connection.execute(
        "INSERT OR IGNORE INTO archive_files (id, file_size) VALUES (?, 1)",
        (archive_id,),
    )
    return archive_id


def _force_blocked(queue: JobQueue, job_id: int) -> None:
    """Force a job into 'blocked'; no public transition produces it."""
    queue.connection.execute(
        "UPDATE jobs SET status = ? WHERE id = ?",
        (JobStatus.BLOCKED.value, job_id),
    )


def test_cancel_retires_a_pending_job(queue: JobQueue) -> None:
    """The case this transition was written for: work that is impossible.

    Archive 45217's perceptual job cannot be repaired -- its content lives
    under another archive's current location -- and retrying it would burn
    attempts until it landed in the terminal-failure audit as a corruption it
    is not.
    """
    job = queue.enqueue("perceptual_hash", archive_id=_archive(queue, 45217))

    cancelled = queue.cancel(job.id, "content survives under another archive")

    assert cancelled.status is JobStatus.CANCELLED
    assert cancelled.cancellation_reason == (
        "content survives under another archive"
    )
    assert cancelled.cancelled_at is not None


def test_cancel_retires_a_blocked_job(queue: JobQueue) -> None:
    """'blocked' is nonterminal and has no other way out."""
    job = queue.enqueue("perceptual_hash", archive_id=_archive(queue, 7))
    _force_blocked(queue, job.id)

    cancelled = queue.cancel(job.id, "upstream dependency abandoned")

    assert cancelled.status is JobStatus.CANCELLED


@pytest.mark.parametrize("reason", ["", "   ", "\t\n"])
def test_cancel_requires_a_reason(queue: JobQueue, reason: str) -> None:
    """A cancelled job with no reason is indistinguishable from a mistake."""
    job = queue.enqueue("perceptual_hash", archive_id=_archive(queue, 8))

    with pytest.raises(ValueError):
        queue.cancel(job.id, reason)

    assert queue.get(job.id).status is JobStatus.PENDING


@pytest.mark.parametrize("terminal", ["completed", "failed"])
def test_cancel_refuses_a_terminal_job(queue: JobQueue, terminal: str) -> None:
    """Cancelling a finished job would rewrite history, not close it out."""
    job = queue.enqueue("perceptual_hash", archive_id=_archive(queue, 9))
    queue.claim_next("worker-1")

    if terminal == "completed":
        queue.mark_completed(job.id)
    else:
        queue.mark_failed(job.id, "corrupt", permanent=True)

    before = queue.get(job.id)

    with pytest.raises(InvalidJobTransitionError) as caught:
        queue.cancel(job.id, "changed my mind")

    assert terminal in str(caught.value)

    after = queue.get(job.id)
    assert after.status is before.status
    assert after.cancelled_at is None
    assert after.cancellation_reason is None


@pytest.mark.parametrize("owned", ["claimed", "running"])
def test_cancel_refuses_a_job_a_worker_owns(
    queue: JobQueue, owned: str
) -> None:
    """Nonterminal is not sufficient; a live worker still owns these.

    Cancelling underneath a worker turns an operator's decision into an
    unexplained worker error the next time it reports the job's outcome.
    """
    job = queue.enqueue("perceptual_hash", archive_id=_archive(queue, 10))
    queue.claim_next("worker-1")
    if owned == "running":
        queue.mark_running(job.id)

    with pytest.raises(InvalidJobTransitionError) as caught:
        queue.cancel(job.id, "operator retired it")

    # The refusal names the allowed statuses so the caller can act on it.
    assert "pending" in str(caught.value)
    assert queue.get(job.id).status is JobStatus(owned)


def test_a_worker_owned_job_is_cancellable_after_recovery(
    queue: JobQueue,
) -> None:
    """The documented two-step path: recover to 'pending', then cancel."""
    job = queue.enqueue("perceptual_hash", archive_id=_archive(queue, 11))
    queue.claim_next("worker-1")

    queue.recover_abandoned(older_than_seconds=0)
    assert queue.get(job.id).status is JobStatus.PENDING

    recovered = queue.cancel(job.id, "retired after recovery")
    assert recovered.status is JobStatus.CANCELLED


def test_repeat_cancellation_is_rejected_and_keeps_the_first_reason(
    queue: JobQueue,
) -> None:
    """A silent second cancel would discard or overwrite the audit trail."""
    job = queue.enqueue("perceptual_hash", archive_id=_archive(queue, 12))
    first = queue.cancel(job.id, "the real reason")

    with pytest.raises(InvalidJobTransitionError) as caught:
        queue.cancel(job.id, "a different reason")

    assert "cancelled" in str(caught.value)

    after = queue.get(job.id)
    assert after.cancellation_reason == "the real reason"
    assert after.cancelled_at == first.cancelled_at


def test_cancel_preserves_the_failure_evidence_and_timestamps(
    queue: JobQueue,
) -> None:
    """Closing a job out must not erase why it was stuck.

    This is the shape of the 79 relocation-blocked jobs: failed with
    filesystem_not_found, retried back to 'pending', then retired. That
    failure_category is the record of the incident, and cancelling is a new
    fact about the job rather than a replacement for the old one.
    """
    job = queue.enqueue("perceptual_hash", archive_id=_archive(queue, 45217))
    queue.claim_next("worker-1")
    queue.mark_running(job.id)
    queue.mark_failed(
        job.id,
        "source file missing",
        failure_category="filesystem_not_found",
    )

    before = queue.get(job.id)
    assert before.status is JobStatus.PENDING

    after = queue.cancel(job.id, "deduplicated; keeper is another archive")

    assert after.error_message == "source file missing"
    assert after.failure_category == "filesystem_not_found"
    assert after.attempts == before.attempts
    assert after.claimed_at == before.claimed_at
    assert after.started_at == before.started_at
    # A cancelled job did not complete, so completed_at stays as it was.
    assert after.completed_at == before.completed_at
    assert after.created_at == before.created_at


def test_cancellation_is_persisted_not_just_returned(tmp_path: Path) -> None:
    """Read it back on a second connection, not from the returned object."""
    database_path = tmp_path / "persist.db"

    with database_connection(database_path) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        queue = JobQueue(connection)
        job = queue.enqueue("perceptual_hash", archive_id=_archive(queue, 45217))
        queue.cancel(job.id, "retired: keeper is another archive")

    with database_connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT status, cancellation_reason, cancelled_at, completed_at
            FROM jobs WHERE id = ?
            """,
            (job.id,),
        ).fetchone()

    assert row[0] == JobStatus.CANCELLED.value
    assert row[1] == "retired: keeper is another archive"
    assert row[2] is not None
    assert row[3] is None


def test_cancelling_frees_the_active_job_identity(queue: JobQueue) -> None:
    """'cancelled' is outside idx_jobs_unique_active, so re-enqueue works.

    Without this a retired job would permanently block its archive from ever
    being enqueued again -- the opposite of retiring it.
    """
    job = queue.enqueue("perceptual_hash", archive_id=_archive(queue, 13))
    queue.cancel(job.id, "retired")

    replacement = queue.enqueue("perceptual_hash", archive_id=_archive(queue, 13))

    assert replacement.id != job.id
    assert replacement.status is JobStatus.PENDING


def test_a_failing_commit_leaves_the_job_untouched(tmp_path: Path) -> None:
    """COMMIT can fail on its own, and that must not skip the rollback.

    A full disk at commit time must leave the caller seeing the database as it
    was, with no transaction left open holding the change.
    """
    database_path = tmp_path / "rollback.db"

    with database_connection(database_path) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        queue = JobQueue(connection)
        job = queue.enqueue("perceptual_hash", archive_id=_archive(queue, 14))

        failing = JobQueue(_FailingCommitConnection(connection))

        with pytest.raises(sqlite3.OperationalError):
            failing.cancel(job.id, "retired")

        assert connection.in_transaction is False

        after = queue.get(job.id)

    assert after.status is JobStatus.PENDING
    assert after.cancelled_at is None
    assert after.cancellation_reason is None
