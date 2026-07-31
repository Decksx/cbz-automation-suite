from __future__ import annotations

import threading
from pathlib import Path

import pytest

from comic_automation.database.connection import (
    database_connection,
)
from comic_automation.database.migrations import (
    apply_migrations,
)
from comic_automation.jobs import (
    InvalidJobTransitionError,
    JobQueue,
    JobStatus,
    JobWorker,
    JobWorkerStateError,
    PermanentJobError,
    WorkerOutcome,
)


MIGRATION_DIRECTORY = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


def test_worker_completes_successful_job(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "worker.db"
    handled: list[int] = []

    with database_connection(database_path) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        queue = JobQueue(connection)
        queued = queue.enqueue("test_success")

        worker = JobWorker(
            queue,
            {
                "test_success": (
                    lambda job: handled.append(job.id)
                )
            },
            worker_id="worker-1",
            poll_interval_seconds=0,
        )

        result = worker.run_once()
        completed = queue.get(queued.id)

    assert result.processed is True
    assert result.succeeded is True
    assert handled == [queued.id]
    assert completed.status == JobStatus.COMPLETED


def test_worker_retries_handler_failure(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "worker.db"

    def failing_handler(job) -> None:
        raise RuntimeError("Test handler failure")

    with database_connection(database_path) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        queue = JobQueue(connection)
        queued = queue.enqueue(
            "test_failure",
            max_attempts=2,
        )

        worker = JobWorker(
            queue,
            {"test_failure": failing_handler},
            worker_id="worker-1",
            poll_interval_seconds=0,
            retry_delay_seconds=0,
        )

        first_result = worker.run_once()
        retried = queue.get(queued.id)

        second_result = worker.run_once()
        failed = queue.get(queued.id)

    assert first_result.succeeded is False
    assert retried.status == JobStatus.PENDING
    assert retried.attempts == 1

    assert second_result.succeeded is False
    assert failed.status == JobStatus.FAILED
    assert failed.attempts == 2
    assert failed.error_message == "Test handler failure"


def test_worker_permanent_failure_is_terminal_immediately(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "worker.db"

    def permanent_failure(job) -> None:
        raise PermanentJobError("Corrupt archive")

    with database_connection(database_path) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        queue = JobQueue(connection)
        queued = queue.enqueue(
            "inspect_archive",
            max_attempts=3,
        )
        worker = JobWorker(
            queue,
            {"inspect_archive": permanent_failure},
            worker_id="worker-1",
            poll_interval_seconds=0,
        )

        result = worker.run_once()
        failed = queue.get(queued.id)

    assert result.outcome == WorkerOutcome.TERMINALLY_FAILED
    assert failed.status == JobStatus.FAILED
    assert failed.attempts == 1


def test_worker_reports_retry_scheduled(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "worker.db"

    def transient_failure(job) -> None:
        raise OSError("Temporary filesystem error")

    with database_connection(database_path) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        queue = JobQueue(connection)
        queued = queue.enqueue(
            "inspect_archive",
            max_attempts=3,
        )
        worker = JobWorker(
            queue,
            {"inspect_archive": transient_failure},
            worker_id="worker-1",
            poll_interval_seconds=0,
            retry_delay_seconds=60,
        )

        result = worker.run_once()
        retried = queue.get(queued.id)

    assert result.outcome == WorkerOutcome.RETRY_SCHEDULED
    assert retried.status == JobStatus.PENDING
    assert retried.attempts == 1


def test_worker_claims_only_registered_job_types(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "worker.db"

    with database_connection(database_path) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        queue = JobQueue(connection)
        unhandled = queue.enqueue("unknown_type")

        worker = JobWorker(
            queue,
            {"known_type": lambda job: None},
            worker_id="worker-1",
            poll_interval_seconds=0,
        )

        result = worker.run_once()
        unchanged = queue.get(unhandled.id)

    assert result.processed is False
    assert unchanged.status == JobStatus.PENDING


def test_worker_without_handlers_remains_idle(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "worker.db"

    with database_connection(database_path) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        queue = JobQueue(connection)
        queue.enqueue("inspect_archive")

        worker = JobWorker(
            queue,
            {},
            worker_id="worker-1",
            poll_interval_seconds=0,
        )

        result = worker.run_once()

    assert result.processed is False


def test_worker_loop_stops_cleanly(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "worker.db"
    stop_event = threading.Event()

    with database_connection(database_path) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)

        worker = JobWorker(
            JobQueue(connection),
            {},
            worker_id="worker-1",
            stop_event=stop_event,
            poll_interval_seconds=0.01,
        )

        thread = threading.Thread(target=worker.run)
        thread.start()

        stop_event.set()
        thread.join(timeout=2)

    assert thread.is_alive() is False


def test_worker_missing_handler_is_terminal_with_stable_category(
    tmp_path: Path,
) -> None:
    """
    claim_next() filters by registered handler names, so
    `handler is None` inside run_once() should be unreachable in
    normal operation. This test forces that branch anyway (by handing
    the worker an already-claimed job of an unregistered type via a
    patched claim_next()) to confirm it is handled as a permanent,
    stably-categorized failure rather than a retryable one.
    """
    database_path = tmp_path / "worker.db"

    with database_connection(database_path) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        queue = JobQueue(connection)
        queued = queue.enqueue("ghost_type", max_attempts=3)
        claimed = queue.claim_next("worker-1")

        assert claimed is not None

        worker = JobWorker(
            queue,
            {"known_type": lambda job: None},
            worker_id="worker-1",
            poll_interval_seconds=0,
        )
        worker.queue.claim_next = lambda *args, **kwargs: claimed

        result = worker.run_once()
        failed = queue.get(queued.id)

    assert result.processed is True
    assert result.succeeded is False
    assert result.outcome == WorkerOutcome.TERMINALLY_FAILED
    assert failed.status == JobStatus.FAILED
    assert failed.attempts == 1
    assert failed.failure_category == "missing_job_handler"


def test_worker_raises_dedicated_error_when_failure_state_cannot_be_persisted(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "worker.db"
    handler_calls: list[int] = []

    def failing_handler(job) -> None:
        handler_calls.append(job.id)
        raise RuntimeError("Handler blew up")

    def broken_mark_failed(*args, **kwargs):
        raise InvalidJobTransitionError(
            "Simulated persistence failure."
        )

    with database_connection(database_path) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        queue = JobQueue(connection)
        queued = queue.enqueue(
            "test_double_failure",
            max_attempts=3,
        )

        worker = JobWorker(
            queue,
            {"test_double_failure": failing_handler},
            worker_id="worker-1",
            poll_interval_seconds=0,
        )
        worker.queue.mark_failed = broken_mark_failed

        with pytest.raises(JobWorkerStateError) as exc_info:
            worker.run_once()

        # The job's row was never updated, since mark_failed() itself
        # failed before any write could commit -- it must not be left
        # looking like a clean success/retry/terminal outcome.
        untouched = queue.get(queued.id)

    error = exc_info.value

    assert error.job_id == queued.id
    assert error.worker_id == "worker-1"
    assert isinstance(error.processing_exception, RuntimeError)
    assert str(error.processing_exception) == "Handler blew up"
    assert isinstance(
        error.transition_exception, InvalidJobTransitionError
    )
    assert error.__cause__ is error.transition_exception
    assert handler_calls == [queued.id]
    assert untouched.status == JobStatus.RUNNING
