from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Sequence

from comic_automation.archive.near_duplicate import (
    DEFAULT_MAX_BLOCK_BUCKET_SIZE,
    DEFAULT_MAX_HAMMING_DISTANCE,
    DEFAULT_MAX_PAGE_COUNT_DELTA_RATIO,
    DEFAULT_MIN_PAGE_MATCH_RATIO,
    NearDuplicateRepository,
)
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations


MIGRATIONS = (
    Path(__file__).resolve().parents[1] / "database" / "migrations"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate conservative, review-only near-duplicate archive "
            "candidates from stored dHash and pHash page signatures."
        )
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--max-hamming-distance",
        type=int,
        default=DEFAULT_MAX_HAMMING_DISTANCE,
    )
    parser.add_argument(
        "--min-page-match-ratio",
        type=float,
        default=DEFAULT_MIN_PAGE_MATCH_RATIO,
    )
    parser.add_argument(
        "--max-page-count-delta-ratio",
        type=float,
        default=DEFAULT_MAX_PAGE_COUNT_DELTA_RATIO,
    )
    parser.add_argument(
        "--max-block-bucket-size",
        type=int,
        default=DEFAULT_MAX_BLOCK_BUCKET_SIZE,
    )
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--json-output", type=Path)
    return parser


def run_near_duplicate_candidates(
    *,
    database: Path,
    limit: int,
    max_hamming_distance: int,
    min_page_match_ratio: float,
    max_page_count_delta_ratio: float,
    max_block_bucket_size: int,
    report_only: bool,
    json_output: Path | None,
) -> dict:
    if limit < 1:
        raise ValueError("--limit must be at least 1.")
    if not 0.0 <= min_page_match_ratio <= 1.0:
        raise ValueError(
            "--min-page-match-ratio must be between 0 and 1."
        )
    if not 0.0 <= max_page_count_delta_ratio <= 1.0:
        raise ValueError(
            "--max-page-count-delta-ratio must be between 0 and 1."
        )
    if max_hamming_distance < 0:
        raise ValueError(
            "--max-hamming-distance cannot be negative."
        )

    database = database.resolve(strict=False)
    if not database.is_file():
        raise FileNotFoundError(f"Database does not exist: {database}")

    started = time.perf_counter()

    with database_connection(database) as connection:
        migrations = apply_migrations(connection, MIGRATIONS)
        repository = NearDuplicateRepository(connection)
        ready_archives = len(repository.load_fingerprints())
        candidates = (
            []
            if report_only
            else repository.generate_candidates(
                limit=limit,
                max_hamming_distance=max_hamming_distance,
                min_page_match_ratio=min_page_match_ratio,
                max_page_count_delta_ratio=(
                    max_page_count_delta_ratio
                ),
                max_bucket_size=max_block_bucket_size,
            )
        )
        review_summary = repository.review_summary()

    output = {
        "database": str(database),
        "ready_archives": ready_archives,
        "generated_candidates": len(candidates),
        "review_summary": review_summary,
        "report_only": report_only,
        "thresholds": {
            "max_hamming_distance": max_hamming_distance,
            "min_page_match_ratio": min_page_match_ratio,
            "max_page_count_delta_ratio": (
                max_page_count_delta_ratio
            ),
            "max_block_bucket_size": max_block_bucket_size,
        },
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
        output = run_near_duplicate_candidates(
            database=args.database,
            limit=args.limit,
            max_hamming_distance=args.max_hamming_distance,
            min_page_match_ratio=args.min_page_match_ratio,
            max_page_count_delta_ratio=(
                args.max_page_count_delta_ratio
            ),
            max_block_bucket_size=args.max_block_bucket_size,
            report_only=args.report_only,
            json_output=args.json_output,
        )
    except Exception as exc:
        print(
            f"Near-duplicate candidate generation failed: {exc}",
            file=sys.stderr,
        )
        return 1

    print("Near-duplicate candidate generation completed.")
    print(f"Ready archives:       {output['ready_archives']}")
    print(f"Generated candidates: {output['generated_candidates']}")
    print(
        "Pending review:       "
        f"{output['review_summary'].get('pending_review', 0)}"
    )
    print("No archive files were modified.")

    if output.get("json_output"):
        print(f"JSON output:          {output['json_output']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
