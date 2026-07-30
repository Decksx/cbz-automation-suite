from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from comic_automation.jobs.models import Job
from comic_automation.jobs.queue import JobQueue


log = logging.getLogger(__name__)

JobHandler = Callable[[Job], None]


class CategorizedJobError(RuntimeError):
    """A job failure with a stable machine-readable category."""

    def __init__(self, message: str, *, category: str) -> None:
        super().__init__(message)
        self.category = category


class PermanentJobError(CategorizedJobError):
    """A handler failure that cannot succeed when retried unchanged."""

    def __init__(
        self,
        message: str,
        *,
        category: str = "permanent_error",
    ) -> None:
        super().__init__(message, category=category)


class WorkerOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    RETRY_SCHEDULED = "retry_scheduled"
    TERMINALLY_FAILED = "terminally_failed"


@dataclass(frozen=True)
class WorkerResult:
    processed: bool
    job_id: int | None = None
    succeeded: bool | None = None
    outcome: WorkerOutcome | None = None


class JobWorker:
    def __init__(
        self,
        queue: JobQueue,
        handlers: Mapping[str, JobHandler],
        *,
        worker_id: str,
        stop_event: threading.Event | None = None,
        poll_interval_seconds: float = 5.0,
        retry_delay_seconds: int = 30,
    ) -> None:
        normalized_worker_id = worker_id.strip()

        if not normalized_worker_id:
            raise ValueError("worker_id cannot be empty.")

        if poll_interval_seconds < 0:
            raise ValueError(
                "poll_interval_seconds cannot be negative."
            )

        if retry_delay_seconds < 0:
            raise ValueError(
                "retry_delay_seconds cannot be negative."
            )

        self.queue = queue
        self.handlers = dict(handlers)
        self.worker_id = normalized_worker_id
        self.stop_event = stop_event or threading.Event()
        self.poll_interval_seconds = poll_interval_seconds
        self.retry_delay_seconds = retry_delay_seconds

    def run_once(
        self,
        *,
        excluded_job_ids: Iterable[int] | None = None,
    ) -> WorkerResult:
        """
        Claim and process at most one registered job.

        Returns a WorkerResult describing whether work was found and
        whether the handler succeeded.
        """
        if not self.handlers:
            return WorkerResult(processed=False)

        job = self.queue.claim_next(
            self.worker_id,
            job_types=self.handlers.keys(),
            excluded_job_ids=excluded_job_ids,
        )

        if job is None:
            return WorkerResult(processed=False)

        handler = self.handlers.get(job.job_type)

        if handler is None:
            # This should be unreachable because claim_next() is filtered
            # by the registered handler names.
            self.queue.mark_failed(
                job.id,
                f"No handler registered for {job.job_type!r}.",
                worker_id=self.worker_id,
            )
            return WorkerResult(
                processed=True,
                job_id=job.id,
                succeeded=False,
            )

        try:
            running_job = self.queue.mark_running(
                job.id,
                worker_id=self.worker_id,
            )

            handler(running_job)

            self.queue.mark_completed(
                running_job.id,
                worker_id=self.worker_id,
            )

            log.info(
                "Worker %s completed job %s (%s).",
                self.worker_id,
                running_job.id,
                running_job.job_type,
            )

            return WorkerResult(
                processed=True,
                job_id=running_job.id,
                succeeded=True,
                outcome=WorkerOutcome.SUCCEEDED,
            )

        except Exception as exc:
            log.exception(
                "Worker %s failed job %s (%s).",
                self.worker_id,
                job.id,
                job.job_type,
            )

            failed_job = self.queue.mark_failed(
                job.id,
                str(exc),
                failure_category=getattr(
                    exc,
                    "category",
                    "unclassified_error",
                ),
                retry_delay_seconds=self.retry_delay_seconds,
                worker_id=self.worker_id,
                permanent=isinstance(exc, PermanentJobError),
            )

            outcome = (
                WorkerOutcome.TERMINALLY_FAILED
                if failed_job.status.value == "failed"
                else WorkerOutcome.RETRY_SCHEDULED
            )

            return WorkerResult(
                processed=True,
                job_id=job.id,
                succeeded=False,
                outcome=outcome,
            )

    def run(self) -> None:
        """
        Poll until the shared stop event is set.
        """
        log.info("Worker %s started.", self.worker_id)

        try:
            while not self.stop_event.is_set():
                result = self.run_once()

                if not result.processed:
                    self.stop_event.wait(
                        self.poll_interval_seconds
                    )
        finally:
            log.info("Worker %s stopped.", self.worker_id)
