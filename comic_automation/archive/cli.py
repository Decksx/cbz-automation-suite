from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Sequence

from comic_automation.archive import InspectArchiveHandler
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.jobs import (
    JobQueue,
    JobWorker,
    WorkerOutcome,
)


DEFAULT_MIGRATION_DIRECTORY = (
    Path(__file__).resolve().parents[1]
    / "database"
    / "migrations"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Process a bounded number of pending read-only "
            "archive-inspection jobs."
        )
    )
    parser.add_argument(
        "--database",
        type=Path,
        required=True,
        help="SQLite database containing inspect_archive jobs.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of jobs to process.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=10,
        help="Print progress after this many processed jobs.",
    )
    parser.add_argument(
        "--verify-crc",
        action="store_true",
        help="Read every CBZ entry and verify ZIP CRC values.",
    )
    parser.add_argument(
        "--retry-delay-seconds",
        type=int,
        default=30,
        help="Delay before retrying transiently failed jobs.",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="Optional path for the JSON execution summary.",
    )

    return parser


def _validate_arguments(
    *,
    limit: int,
    progress_every: int,
    retry_delay_seconds: int,
) -> None:
    if limit < 1:
        raise ValueError("--limit must be at least 1.")

    if progress_every < 1:
        raise ValueError(
            "--progress-every must be at least 1."
        )

    if retry_delay_seconds < 0:
        raise ValueError(
            "--retry-delay-seconds cannot be negative."
        )


def _count_pending(connection) -> int:
    return int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM jobs
            WHERE job_type = 'inspect_archive'
              AND status = 'pending'
              AND available_at <= CURRENT_TIMESTAMP
            """
        ).fetchone()[0]
    )


def _status_counts(connection) -> dict[str, int]:
    return {
        str(row["status"]): int(row["count"])
        for row in connection.execute(
            """
            SELECT
                ai.status,
                COUNT(*) AS count
            FROM archive_inspections AS ai
            GROUP BY ai.status
            ORDER BY ai.status
            """
        ).fetchall()
    }


def run_inspection_jobs(
    *,
    database: Path,
    limit: int,
    progress_every: int,
    verify_crc: bool,
    retry_delay_seconds: int,
    json_output: Path | None = None,
    migration_directory: Path = DEFAULT_MIGRATION_DIRECTORY,
) -> dict:
    _validate_arguments(
        limit=limit,
        progress_every=progress_every,
        retry_delay_seconds=retry_delay_seconds,
    )

    database = database.resolve(strict=False)

    if not database.is_file():
        raise FileNotFoundError(
            f"Database does not exist: {database}"
        )

    started = time.perf_counter()
    processed = 0
    succeeded = 0
    retry_scheduled = 0
    terminally_failed = 0
    processed_job_ids: list[int] = []
    outcome_counts: Counter[str] = Counter()

    with database_connection(database) as connection:
        applied_migrations = apply_migrations(
            connection,
            migration_directory,
        )

        pending_before = _count_pending(connection)

        queue = JobQueue(connection)
        worker = JobWorker(
            queue,
            {
                "inspect_archive": InspectArchiveHandler(
                    connection,
                    verify_crc=verify_crc,
                )
            },
            worker_id=(
                f"{socket.gethostname()}:"
                "bounded-inspection-cli"
            ),
            poll_interval_seconds=0,
            retry_delay_seconds=retry_delay_seconds,
        )

        seen_job_ids: set[int] = set()

        while processed < limit:
            result = worker.run_once(
                excluded_job_ids=seen_job_ids,
            )

            if not result.processed:
                break

            processed += 1

            if result.job_id is not None:
                processed_job_ids.append(result.job_id)
                seen_job_ids.add(result.job_id)

            if result.outcome == WorkerOutcome.SUCCEEDED:
                succeeded += 1
                outcome_counts["succeeded"] += 1
            elif result.outcome == WorkerOutcome.RETRY_SCHEDULED:
                retry_scheduled += 1
                outcome_counts["retry_scheduled"] += 1
            else:
                terminally_failed += 1
                outcome_counts["terminally_failed"] += 1

            if (
                processed % progress_every == 0
                or processed == limit
            ):
                elapsed = time.perf_counter() - started
                rate = (
                    processed / elapsed
                    if elapsed > 0
                    else 0.0
                )

                print(
                    f"Progress: {processed:,} jobs; "
                    f"succeeded={succeeded:,}; "
                    f"retry_scheduled={retry_scheduled:,}; "
                    f"terminally_failed={terminally_failed:,}; "
                    f"rate={rate:.2f}/sec"
                )

        pending_after = _count_pending(connection)
        inspection_status_counts = _status_counts(connection)

    elapsed = time.perf_counter() - started
    jobs_per_second = (
        processed / elapsed
        if elapsed > 0
        else 0.0
    )

    output = {
        "database": str(database),
        "limit": limit,
        "processed": processed,
        "succeeded": succeeded,
        "retry_scheduled": retry_scheduled,
        "terminally_failed": terminally_failed,
        "failed": retry_scheduled + terminally_failed,
        "pending_before": pending_before,
        "remaining_pending": pending_after,
        "verify_crc": verify_crc,
        "retry_delay_seconds": retry_delay_seconds,
        "elapsed_seconds": round(elapsed, 6),
        "jobs_per_second": round(jobs_per_second, 4),
        "inspection_status_counts": (
            inspection_status_counts
        ),
        "outcome_counts": dict(outcome_counts),
        "processed_job_ids": processed_job_ids,
        "applied_migrations": applied_migrations,
    }

    if json_output is not None:
        json_output = json_output.resolve(strict=False)
        json_output.parent.mkdir(parents=True, exist_ok=True)
        json_output.write_text(
            json.dumps(
                output,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        output["json_output"] = str(json_output)

    return output


def print_summary(output: dict) -> None:
    print("Bounded archive inspection completed.")
    print(f"Database:          {output['database']}")
    print(f"Limit:             {output['limit']}")
    print(f"Processed:         {output['processed']}")
    print(f"Succeeded:         {output['succeeded']}")
    print(
        "Retry scheduled:   "
        f"{output['retry_scheduled']}"
    )
    print(
        "Terminally failed: "
        f"{output['terminally_failed']}"
    )
    print(
        "Remaining pending: "
        f"{output['remaining_pending']}"
    )
    print(
        "Verify CRC:        "
        f"{output['verify_crc']}"
    )
    print(
        "Elapsed:           "
        f"{output['elapsed_seconds']:.2f} seconds"
    )
    print(
        "Jobs per second:   "
        f"{output['jobs_per_second']:.2f}"
    )

    statuses = output["inspection_status_counts"]

    if statuses:
        print("Inspection statuses:")

        for status, count in statuses.items():
            print(f"  {status}: {count}")

    if output.get("json_output"):
        print(f"JSON output:       {output['json_output']}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        output = run_inspection_jobs(
            database=args.database,
            limit=args.limit,
            progress_every=args.progress_every,
            verify_crc=args.verify_crc,
            retry_delay_seconds=(
                args.retry_delay_seconds
            ),
            json_output=args.json_output,
        )
    except Exception as exc:
        print(
            f"Archive inspection failed: {exc}",
            file=sys.stderr,
        )
        return 1

    print_summary(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
