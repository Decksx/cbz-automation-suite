from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path
from typing import Sequence

from comic_automation.archive.hashing import (
    ArchiveHashRepository,
    CalculateArchiveHashHandler,
)
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.jobs import JobQueue, JobWorker, WorkerOutcome


MIGRATIONS = (
    Path(__file__).resolve().parents[1] / "database" / "migrations"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run bounded, read-only SHA-256 archive hashing."
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--progress-every", type=int, default=10)
    parser.add_argument("--enqueue-missing", action="store_true")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--json-output", type=Path)
    return parser


def run_hashing(
    *,
    database: Path,
    limit: int,
    progress_every: int,
    enqueue_missing: bool,
    report_only: bool,
    json_output: Path | None,
) -> dict:
    if limit < 1:
        raise ValueError("--limit must be at least 1.")
    if progress_every < 1:
        raise ValueError("--progress-every must be at least 1.")

    database = database.resolve(strict=False)

    if not database.is_file():
        raise FileNotFoundError(f"Database does not exist: {database}")

    started = time.perf_counter()
    processed = succeeded = retry_scheduled = terminally_failed = 0

    with database_connection(database) as connection:
        migrations = apply_migrations(connection, MIGRATIONS)
        repository = ArchiveHashRepository(connection)
        enqueued = (
            repository.enqueue_missing(limit=limit)
            if enqueue_missing
            else 0
        )
        queue = JobQueue(connection)
        worker = JobWorker(
            queue,
            {
                "calculate_archive_hash": CalculateArchiveHashHandler(
                    connection
                )
            },
            worker_id=f"{socket.gethostname()}:bounded-hash-cli",
            poll_interval_seconds=0,
            retry_delay_seconds=30,
        )
        seen: set[int] = set()

        while not report_only and processed < limit:
            result = worker.run_once(excluded_job_ids=seen)

            if not result.processed:
                break

            processed += 1
            if result.job_id is not None:
                seen.add(result.job_id)

            if result.outcome == WorkerOutcome.SUCCEEDED:
                succeeded += 1
            elif result.outcome == WorkerOutcome.RETRY_SCHEDULED:
                retry_scheduled += 1
            else:
                terminally_failed += 1

            if processed % progress_every == 0 or processed == limit:
                print(
                    f"Progress: {processed:,}; succeeded={succeeded:,}; "
                    f"retry_scheduled={retry_scheduled:,}; "
                    f"terminally_failed={terminally_failed:,}"
                )

        # Run summary figures pulled directly from the database rather
        # than accumulated in Python, so they reflect the true state
        # even if enqueue_missing/report_only skipped the worker loop.
        remaining = connection.execute(
            """
            SELECT COUNT(*)
            FROM jobs
            WHERE job_type = 'calculate_archive_hash'
              AND status = 'pending'
            """
        ).fetchone()[0]
        hash_count = connection.execute(
            "SELECT COUNT(*) FROM archive_hashes"
        ).fetchone()[0]
        duplicates = repository.duplicate_groups()

    output = {
        "database": str(database),
        "processed": processed,
        "succeeded": succeeded,
        "retry_scheduled": retry_scheduled,
        "terminally_failed": terminally_failed,
        "enqueued": enqueued,
        "remaining_pending": int(remaining),
        "hash_count": int(hash_count),
        "duplicate_group_count": len(duplicates),
        "duplicate_groups": duplicates,
        "elapsed_seconds": round(time.perf_counter() - started, 6),
        "applied_migrations": migrations,
    }

    if json_output is not None:
        resolved = json_output.resolve(strict=False)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(
            json.dumps(output, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        output["json_output"] = str(resolved)

    return output


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        output = run_hashing(
            database=args.database,
            limit=args.limit,
            progress_every=args.progress_every,
            enqueue_missing=args.enqueue_missing,
            report_only=args.report_only,
            json_output=args.json_output,
        )
    except Exception as exc:
        print(f"Archive hashing failed: {exc}", file=sys.stderr)
        return 1

    print("Bounded archive hashing completed.")
    print(f"Processed:         {output['processed']}")
    print(f"Succeeded:         {output['succeeded']}")
    print(f"Retry scheduled:   {output['retry_scheduled']}")
    print(f"Terminally failed: {output['terminally_failed']}")
    print(f"Hashes stored:     {output['hash_count']}")
    print(f"Duplicate groups:  {output['duplicate_group_count']}")
    print(f"Remaining pending: {output['remaining_pending']}")
    if output.get("json_output"):
        print(f"JSON output:       {output['json_output']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
