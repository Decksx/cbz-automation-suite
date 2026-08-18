"""Run the ingest stages in dependency order so new imports finish arriving.

The problem
-----------

Every stage in this system uses a *pull* model: ``enqueue_missing()`` scans for
archives that are eligible for that stage and enqueues them. Nothing pushes
work from one stage to the next when a job completes. Discovery enqueues
``inspect_archive``; the remaining three stages only receive work when an
operator runs their CLI with ``--enqueue-missing``.

That design is sound -- it makes every stage restartable and idempotent, and it
means a crashed run loses nothing. What it lacks is a way to say "carry the
newly imported archives all the way through", which is why the perceptual
backfill behaves like a treadmill: imports keep arriving, and the backlog is
only drained when somebody remembers to run four commands in the right order.

This module is that missing step. It is deliberately a thin sequencer over the
existing stage implementations, not a scheduler and not a new execution model:
each stage is the same ``enqueue_missing()`` plus worker drain an operator
would run by hand, in the order the data dependencies require.

Order and why it is fixed
-------------------------

::

    inspect_archive            structural inspection; everything else needs it
    calculate_archive_hash     whole-file SHA-256; identity and repair need it
    hash_archive_pages         per-page SHA-256 -> the content signature
    hash_archive_pages_perceptual   dHash/pHash; needs the signature to exist

Running these out of order is not an error, it is a no-op: a later stage's
eligibility predicate simply matches nothing yet. Running them in order is what
lets one invocation take an archive from "just discovered" to "fully hashed".

Bounded by construction
-----------------------

Every stage takes the same ``--limit``, so a run does a bounded amount of work
and stops. This is not a daemon: it processes at most ``limit`` archives per
stage and exits, which keeps it safe to schedule and safe to interrupt.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations

MIGRATIONS_DIRECTORY = (
    Path(__file__).resolve().parents[1] / "database" / "migrations"
)

# Dependency order. Each entry names the stage and the job type it produces, so
# a report says which queue moved rather than only which function ran.
STAGE_ORDER = (
    "inspect",
    "archive_hash",
    "page_hash",
    "perceptual_hash",
)

STAGE_JOB_TYPES = {
    "inspect": "inspect_archive",
    "archive_hash": "calculate_archive_hash",
    "page_hash": "hash_archive_pages",
    "perceptual_hash": "hash_archive_pages_perceptual",
}


@dataclass
class StageResult:
    stage: str
    job_type: str
    enqueued: int
    pending_before: int
    pending_after: int
    elapsed_seconds: float
    skipped_reason: str | None = None


def _pending(connection, job_type: str) -> int:
    return int(
        connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE job_type = ? "
            "AND status IN ('pending','claimed','running')",
            (job_type,),
        ).fetchone()[0]
    )


def _enqueue_for_stage(connection, stage: str, limit: int | None) -> int:
    """Call the stage's own ``enqueue_missing()``.

    Imported lazily and per stage so that a stage whose module has an
    unrelated import problem cannot prevent the others from running, and so
    this sequencer never becomes a second definition of any stage's
    eligibility rules -- each stage remains the single source of truth for
    what it considers missing.
    """
    if stage == "inspect":
        # Inspection is enqueued by discovery when an archive is first seen and
        # by the hash stage when a file changes underneath its inspection.
        # There is no separate "missing inspections" predicate to call, so this
        # stage only drains what those paths already queued.
        return 0
    if stage == "archive_hash":
        from comic_automation.archive.hashing import ArchiveHashRepository

        return ArchiveHashRepository(connection).enqueue_missing(limit=limit)
    if stage == "page_hash":
        from comic_automation.archive.page_hashing import ArchivePageHashRepository

        return ArchivePageHashRepository(connection).enqueue_missing(limit=limit)
    if stage == "perceptual_hash":
        from comic_automation.archive.perceptual_hashing import (
            ArchivePerceptualHashRepository,
        )

        return ArchivePerceptualHashRepository(connection).enqueue_missing(limit=limit)
    raise ValueError(f"Unknown stage: {stage}")


def run_pipeline(
    *,
    database: Path,
    limit: int | None = None,
    stages: Sequence[str] = STAGE_ORDER,
) -> dict:
    """Enqueue missing work for each stage in dependency order.

    Returns a per-stage report. This function enqueues only -- it does not run
    workers, so that "what work exists" and "do the work" stay separable
    exactly as the individual stage CLIs keep them separable via
    ``--report-only``.
    """
    unknown = [stage for stage in stages if stage not in STAGE_JOB_TYPES]
    if unknown:
        raise ValueError(f"Unknown stage(s): {', '.join(unknown)}")

    started = time.perf_counter()
    results: list[StageResult] = []

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATIONS_DIRECTORY)

        for stage in STAGE_ORDER:
            if stage not in stages:
                continue
            job_type = STAGE_JOB_TYPES[stage]
            stage_started = time.perf_counter()
            before = _pending(connection, job_type)
            enqueued = _enqueue_for_stage(connection, stage, limit)
            connection.commit()
            after = _pending(connection, job_type)
            results.append(
                StageResult(
                    stage=stage,
                    job_type=job_type,
                    enqueued=enqueued,
                    pending_before=before,
                    pending_after=after,
                    elapsed_seconds=round(time.perf_counter() - stage_started, 3),
                    skipped_reason=(
                        "enqueued by discovery and the hash stage, not here"
                        if stage == "inspect"
                        else None
                    ),
                )
            )

    return {
        "database": str(database),
        "limit": limit,
        "stages": [asdict(result) for result in results],
        "total_enqueued": sum(result.enqueued for result in results),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Enqueue missing work for every ingest stage in dependency order, "
            "so newly imported archives progress from discovery through "
            "perceptual hashing instead of waiting for four separate commands. "
            "Enqueues only; run each stage's worker to execute."
        )
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument(
        "--limit",
        type=int,
        help="Per-stage cap on archives enqueued. Omit for no cap.",
    )
    parser.add_argument(
        "--stage",
        action="append",
        choices=STAGE_ORDER,
        help="Restrict to these stages. Repeatable. Order is always the "
        "dependency order regardless of the order given.",
    )
    parser.add_argument("--json-output", type=Path)
    return parser


def print_summary(report: dict) -> None:
    print("Ingest pipeline: enqueue missing work by stage.")
    print(f"  database: {report['database']}")
    for stage in report["stages"]:
        note = f"   ({stage['skipped_reason']})" if stage["skipped_reason"] else ""
        print(
            f"  {stage['stage']:<16} enqueued {stage['enqueued']:>7,}   "
            f"pending {stage['pending_before']:,} -> {stage['pending_after']:,}{note}"
        )
    print(f"  total enqueued: {report['total_enqueued']:,}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_pipeline(
        database=args.database,
        limit=args.limit,
        stages=args.stage or STAGE_ORDER,
    )
    print_summary(report)
    if args.json_output:
        args.json_output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"  json: {args.json_output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
