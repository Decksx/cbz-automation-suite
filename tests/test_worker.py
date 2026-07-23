from __future__ import annotations

import threading
from pathlib import Path

from comic_automation.database.connection import (
    database_connection,
)
from comic_automation.database.migrations import (
    apply_migrations,
)
from comic_automation.jobs import (
    JobQueue,
    JobStatus,
    JobWorker,
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
