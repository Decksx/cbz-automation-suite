from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from comic_automation.jobs.models import Job
from comic_automation.jobs.queue import InvalidJobTransitionError, JobQueue


log = logging.getLogger(__name__)

JobHandler = Callable[[Job], None]

MISSING_HANDLER_CATEGORY = "missing_job_handler"


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


class JobWorkerStateError(RuntimeError):
    """
    Raised when a job failed processing and the subsequent attempt to
    persist that failure via `JobQueue.mark_failed()` itself failed.

    This happens when the job's row has moved out of a failable status
    (for example another worker already reclaimed it) between the
    original processing exception and the attempt to record it. Neither
    error is safe to discard, so both are preserved:

    - `processing_exception` is the original error raised by the
      handler or by a status transition during processing.
    - `transition_exception` is the error raised while trying to record
      that failure (typically `InvalidJobTransitionError`).

    `transition_exception` is also set as this exception's `__cause__`
    (via `raise ... from transition_exception`), so the underlying
    transition failure remains inspectable via the normal traceback
    chain in addition to the explicit attribute.
    """

    def __init__(
        self,
        message: str,
        *,
        job_id: int,
        worker_id: str,
        processing_exception: BaseException,
        transition_exception: BaseException,
    ) -> None:
        super().__init__(message)
        self.job_id = job_id
        self.worker_id = worker_id
        self.processing_exception = processing_exception
        self.transition_exception = transition_exception


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
            # by the registered handler names. Treated as a permanent,
            # stably-categorized failure rather than a retryable one:
            # retrying without a handler registered can never succeed.
            self.queue.mark_failed(
                job.id,
                f"No handler registered for {job.job_type!r}.",
                failure_category=MISSING_HANDLER_CATEGORY,
                worker_id=self.worker_id,
                permanent=True,
            )
            return WorkerResult(
                processed=True,
                job_id=job.id,
                succeeded=False,
                outcome=WorkerOutcome.TERMINALLY_FAILED,
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

            try:
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
            except InvalidJobTransitionError as transition_exc:
                # The job already failed processing, and now recording
                # that failure has *also* failed (for example the job's
                # row moved out of a failable status). Neither error can
                # be silently dropped, and we must not return a
                # WorkerResult implying a clean succeeded/retried/failed
                # outcome when the job's persisted state is unknown, so
                # both are surfaced via a dedicated exception rather than
                # a normal return.
                log.error(
                    "Worker %s: job %s (%s) failed processing "
                    "(%s), and the subsequent attempt to record "
                    "that failure also failed (%s). Job state is "
                    "ambiguous and needs manual review. See the "
                    "preceding log.exception() for the original "
                    "processing traceback.",
                    self.worker_id,
                    job.id,
                    job.job_type,
                    type(exc).__name__,
                    type(transition_exc).__name__,
                )
                raise JobWorkerStateError(
                    f"Job {job.id} failed processing and its failure "
                    "state could not be persisted "
                    f"(worker {self.worker_id!r}).",
                    job_id=job.id,
                    worker_id=self.worker_id,
                    processing_exception=exc,
                    transition_exception=transition_exc,
                ) from transition_exc

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
