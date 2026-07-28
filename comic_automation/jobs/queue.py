from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from comic_automation.jobs.models import (
    Job,
    JobStatus,
    encode_payload,
)


class JobNotFoundError(LookupError):
    pass


class InvalidJobTransitionError(RuntimeError):
    pass


def _utc_sql_timestamp(
    value: datetime | None = None,
) -> str:
    moment = value or datetime.now(timezone.utc)

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    return moment.astimezone(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=int(row["id"]),
        job_type=str(row["job_type"]),
        status=JobStatus(row["status"]),
        priority=int(row["priority"]),
        archive_id=(
            int(row["archive_id"])
            if row["archive_id"] is not None
            else None
        ),
        payload_json=row["payload_json"],
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        available_at=str(row["available_at"]),
        claimed_at=row["claimed_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        worker_id=row["worker_id"],
        error_message=row["error_message"],
        failure_category=row["failure_category"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


class JobQueue:
    """
    Thin wrapper around the `jobs` table (see
    database/migrations/001_operational_foundation.sql) implementing the
    job lifecycle: pending -> claimed -> running -> completed/failed,
    with retry and abandoned-job recovery.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def enqueue(
        self,
        job_type: str,
        *,
        payload: Mapping[str, Any] | None = None,
        priority: int = 100,
        archive_id: int | None = None,
        max_attempts: int = 3,
        available_at: datetime | None = None,
    ) -> Job:
        normalized_type = job_type.strip()

        if not normalized_type:
            raise ValueError("job_type cannot be empty.")

        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1.")

        # Insert a new row in the 'pending' state. available_at defaults
        # to now, so the job is immediately eligible for claim_next();
        # callers pass a future timestamp to schedule delayed work.
        cursor = self.connection.execute(
            """
            INSERT INTO jobs (
                job_type,
                status,
                priority,
                archive_id,
                payload_json,
                max_attempts,
                available_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_type,
                JobStatus.PENDING.value,
                priority,
                archive_id,
                encode_payload(payload),
                max_attempts,
                _utc_sql_timestamp(available_at),
            ),
        )

        return self.get(int(cursor.lastrowid))

    def get(self, job_id: int) -> Job:
        # Simple primary-key lookup; used internally after every
        # mutation to return the caller a fresh Job snapshot.
        row = self.connection.execute(
            """
            SELECT *
            FROM jobs
            WHERE id = ?
            """,
            (job_id,),
        ).fetchone()

        if row is None:
            raise JobNotFoundError(
                f"Job does not exist: {job_id}"
            )

        return _row_to_job(row)

    def claim_next(
        self,
        worker_id: str,
        *,
        job_types: Iterable[str] | None = None,
        excluded_job_ids: Iterable[int] | None = None,
    ) -> Job | None:
        normalized_worker = worker_id.strip()

        if not normalized_worker:
            raise ValueError("worker_id cannot be empty.")

        normalized_types = tuple(
            value.strip()
            for value in (job_types or ())
            if value.strip()
        )
        excluded_ids = tuple(
            dict.fromkeys(int(value) for value in (excluded_job_ids or ()))
        )

        try:
            # BEGIN IMMEDIATE takes the write lock before the SELECT, so
            # the "find a candidate, then claim it" sequence below can't
            # race with another worker doing the same thing concurrently.
            self.connection.execute("BEGIN IMMEDIATE")

            parameters: list[Any] = [
                JobStatus.PENDING.value,
                _utc_sql_timestamp(),
            ]

            type_clause = ""

            if normalized_types:
                placeholders = ",".join(
                    "?" for _ in normalized_types
                )
                type_clause = (
                    f" AND job_type IN ({placeholders})"
                )
                parameters.extend(normalized_types)

            excluded_clause = ""

            if excluded_ids:
                placeholders = ",".join("?" for _ in excluded_ids)
                excluded_clause = f" AND id NOT IN ({placeholders})"
                parameters.extend(excluded_ids)

            # Pick the single best candidate job: pending, due
            # (available_at has passed), optionally restricted to
            # certain job_types, and excluding job IDs this worker has
            # already seen in the current run (used to avoid reclaiming
            # a job that was just retried with a future available_at).
            # Ordered by priority first (lower number = higher
            # priority), then FIFO within the same priority.
            row = self.connection.execute(
                f"""
                SELECT id
                FROM jobs
                WHERE status = ?
                  AND available_at <= ?
                  {type_clause}
                  {excluded_clause}
                ORDER BY
                    priority ASC,
                    created_at ASC,
                    id ASC
                LIMIT 1
                """,
                parameters,
            ).fetchone()

            if row is None:
                self.connection.execute("COMMIT")
                return None

            job_id = int(row["id"])
            now = _utc_sql_timestamp()

            # Transition pending -> claimed. The trailing
            # "AND status = ?" guards against a lost race even though
            # BEGIN IMMEDIATE already serializes writers; rowcount is
            # checked below rather than trusted implicitly.
            cursor = self.connection.execute(
                """
                UPDATE jobs
                SET
                    status = ?,
                    claimed_at = ?,
                    started_at = NULL,
                    completed_at = NULL,
                    worker_id = ?,
                    attempts = attempts + 1,
                    error_message = NULL,
                    updated_at = ?
                WHERE id = ?
                  AND status = ?
                """,
                (
                    JobStatus.CLAIMED.value,
                    now,
                    normalized_worker,
                    now,
                    job_id,
                    JobStatus.PENDING.value,
                ),
            )

            if cursor.rowcount != 1:
                self.connection.execute("ROLLBACK")
                return None

            self.connection.execute("COMMIT")
            return self.get(job_id)

        except Exception:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def mark_running(
        self,
        job_id: int,
        *,
        worker_id: str | None = None,
    ) -> Job:
        # Transition claimed -> running. worker_id, if given, is an
        # ownership check: the update only affects the row if it's
        # still claimed by this worker.
        clauses = [
            "id = ?",
            "status = ?",
        ]
        parameters: list[Any] = [
            job_id,
            JobStatus.CLAIMED.value,
        ]

        if worker_id is not None:
            clauses.append("worker_id = ?")
            parameters.append(worker_id)

        now = _utc_sql_timestamp()

        cursor = self.connection.execute(
            f"""
            UPDATE jobs
            SET
                status = ?,
                started_at = ?,
                updated_at = ?
            WHERE {" AND ".join(clauses)}
            """,
            [
                JobStatus.RUNNING.value,
                now,
                now,
                *parameters,
            ],
        )

        if cursor.rowcount != 1:
            self._raise_transition_error(
                job_id,
                "claimed",
                "running",
            )

        return self.get(job_id)

    def mark_completed(
        self,
        job_id: int,
        *,
        worker_id: str | None = None,
    ) -> Job:
        # Transition claimed or running -> completed. Both source
        # statuses are accepted since a caller may skip mark_running()
        # and complete a job directly after claiming it.
        clauses = [
            "id = ?",
            "status IN (?, ?)",
        ]
        parameters: list[Any] = [
            job_id,
            JobStatus.CLAIMED.value,
            JobStatus.RUNNING.value,
        ]

        if worker_id is not None:
            clauses.append("worker_id = ?")
            parameters.append(worker_id)

        now = _utc_sql_timestamp()

        cursor = self.connection.execute(
            f"""
            UPDATE jobs
            SET
                status = ?,
                completed_at = ?,
                updated_at = ?
            WHERE {" AND ".join(clauses)}
            """,
            [
                JobStatus.COMPLETED.value,
                now,
                now,
                *parameters,
            ],
        )

        if cursor.rowcount != 1:
            self._raise_transition_error(
                job_id,
                "claimed or running",
                "completed",
            )

        return self.get(job_id)

    def mark_failed(
        self,
        job_id: int,
        error_message: str,
        *,
        failure_category: str = "unclassified_error",
        retry_delay_seconds: int = 0,
        worker_id: str | None = None,
        permanent: bool = False,
    ) -> Job:
        if retry_delay_seconds < 0:
            raise ValueError(
                "retry_delay_seconds cannot be negative."
            )

        try:
            self.connection.execute("BEGIN IMMEDIATE")
            job = self.get(job_id)

            if job.status not in {
                JobStatus.CLAIMED,
                JobStatus.RUNNING,
            }:
                raise InvalidJobTransitionError(
                    f"Job {job_id} cannot fail from "
                    f"status {job.status.value!r}."
                )

            if (
                worker_id is not None
                and job.worker_id != worker_id
            ):
                raise InvalidJobTransitionError(
                    f"Job {job_id} is owned by worker "
                    f"{job.worker_id!r}, not {worker_id!r}."
                )

            now = datetime.now(timezone.utc)

            if not permanent and job.attempts < job.max_attempts:
                # Retry path: reset back to 'pending' with a delayed
                # available_at, clearing claim/run bookkeeping so the
                # job looks freshly enqueued to claim_next() once the
                # delay elapses.
                next_available = now + timedelta(
                    seconds=retry_delay_seconds
                )

                self.connection.execute(
                    """
                    UPDATE jobs
                    SET
                        status = ?,
                        available_at = ?,
                        claimed_at = NULL,
                        started_at = NULL,
                        completed_at = NULL,
                        worker_id = NULL,
                        error_message = ?,
                        failure_category = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        JobStatus.PENDING.value,
                        _utc_sql_timestamp(next_available),
                        error_message,
                        failure_category,
                        _utc_sql_timestamp(now),
                        job_id,
                    ),
                )
            else:
                # Terminal path: attempts are exhausted, or the caller
                # explicitly marked this as a permanent failure (for
                # example a corrupt archive that will never succeed on
                # retry). No further claim_next() will pick this up.
                self.connection.execute(
                    """
                    UPDATE jobs
                    SET
                        status = ?,
                        completed_at = ?,
                        error_message = ?,
                        failure_category = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        JobStatus.FAILED.value,
                        _utc_sql_timestamp(now),
                        error_message,
                        failure_category,
                        _utc_sql_timestamp(now),
                        job_id,
                    ),
                )

            self.connection.execute("COMMIT")
            return self.get(job_id)

        except Exception:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def recover_abandoned(
        self,
        *,
        older_than_seconds: int,
    ) -> int:
        if older_than_seconds < 0:
            raise ValueError(
                "older_than_seconds cannot be negative."
            )

        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=older_than_seconds
        )
        cutoff_text = _utc_sql_timestamp(cutoff)

        try:
            self.connection.execute("BEGIN IMMEDIATE")

            # Find jobs still marked claimed/running whose most recent
            # activity (started_at, falling back to claimed_at) is
            # older than the cutoff -- these belong to a worker that
            # crashed or was killed without ever calling
            # mark_completed/mark_failed.
            rows = self.connection.execute(
                """
                SELECT *
                FROM jobs
                WHERE status IN (?, ?)
                  AND COALESCE(started_at, claimed_at) <= ?
                ORDER BY id
                """,
                (
                    JobStatus.CLAIMED.value,
                    JobStatus.RUNNING.value,
                    cutoff_text,
                ),
            ).fetchall()

            recovered = 0
            now = _utc_sql_timestamp()

            for row in rows:
                job = _row_to_job(row)

                # Give abandoned jobs the same attempts-remaining
                # treatment as a normal failure: retry if attempts
                # remain, otherwise mark permanently failed.
                if job.attempts < job.max_attempts:
                    status = JobStatus.PENDING.value
                    completed_at = None
                else:
                    status = JobStatus.FAILED.value
                    completed_at = now

                self.connection.execute(
                    """
                    UPDATE jobs
                    SET
                        status = ?,
                        available_at = ?,
                        claimed_at = NULL,
                        started_at = NULL,
                        completed_at = ?,
                        worker_id = NULL,
                        error_message = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        now,
                        completed_at,
                        "Recovered after worker abandonment.",
                        now,
                        job.id,
                    ),
                )

                recovered += 1

            self.connection.execute("COMMIT")
            return recovered

        except Exception:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def _raise_transition_error(
        self,
        job_id: int,
        expected: str,
        target: str,
    ) -> None:
        try:
            job = self.get(job_id)
        except JobNotFoundError:
            raise

        raise InvalidJobTransitionError(
            f"Job {job_id} cannot transition from "
            f"{job.status.value!r} to {target!r}; "
            f"expected {expected}."
        )
