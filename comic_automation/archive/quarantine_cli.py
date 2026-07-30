from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from comic_automation.archive.quarantine import (
    DEFAULT_QUARANTINE_CATEGORIES,
    QuarantineRepository,
    execute_quarantine,
)
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations


DEFAULT_MIGRATION_DIRECTORY = (
    Path(__file__).resolve().parents[1]
    / "database"
    / "migrations"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preview or perform a guarded quarantine move: relocate "
            "permanently-broken archives (corrupt_archive by default) "
            "out of the live library into a designated folder, "
            "renamed to show series name and chapter, so they can be "
            "re-downloaded. Without --confirm this only previews the "
            "plan; no files or database rows are touched."
        )
    )
    parser.add_argument(
        "--database",
        type=Path,
        required=True,
        help="SQLite database containing inspect_archive job failures.",
    )
    parser.add_argument(
        "--quarantine-root",
        type=Path,
        required=True,
        help=(
            "Destination folder for quarantined archives. Must be "
            "outside any library root that gets rescanned, or "
            "discovery will treat quarantined files as newly "
            "discovered archives."
        ),
    )
    parser.add_argument(
        "--category",
        action="append",
        dest="categories",
        help=(
            "Failure category to include (repeatable). Defaults to "
            "corrupt_archive. filesystem_not_found can never be "
            "included -- there is no file to move."
        ),
    )
    parser.add_argument(
        "--exclude-series",
        action="append",
        default=[],
        help=(
            "Series (immediate parent directory name) to skip, "
            "repeatable -- for series you're handling separately "
            "(e.g. deleting and re-downloading the whole folder)."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Maximum number of archives to move in this run.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help=(
            "Actually move files and update the database. Without "
            "this flag, the command only previews the plan."
        ),
    )
    parser.add_argument(
        "--backup-directory",
        type=Path,
        help=(
            "Directory to write a timestamped database backup to "
            "before making any change. Required with --confirm."
        ),
    )
    parser.add_argument(
        "--csv-output",
        type=Path,
        help="Optional CSV of the plan (preview) or results (confirm).",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="Optional JSON summary.",
    )

    return parser


def _quick_check(connection: sqlite3.Connection) -> str:
    row = connection.execute("PRAGMA quick_check").fetchone()
    return str(row[0]) if row is not None else "unknown"


def _backup_database(database: Path, backup_directory: Path) -> Path:
    backup_directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    destination = (
        backup_directory
        / f"{database.stem}-pre-quarantine-{timestamp}.db"
    )
    shutil.copy2(database, destination)
    return destination


def _write_json(path: Path, payload: object) -> Path:
    resolved = path.resolve(strict=False)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return resolved


def _write_plan_csv(path: Path, plan: list[dict]) -> Path:
    resolved = path.resolve(strict=False)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "archive_id",
        "job_id",
        "series_name",
        "failure_category",
        "attempts",
        "max_attempts",
        "source_path",
        "destination_path",
        "status",
        "error",
    ]

    with resolved.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(plan)

    return resolved


def run_quarantine(
    *,
    database: Path,
    quarantine_root: Path,
    categories: Sequence[str] | None,
    exclude_series: Sequence[str],
    limit: int | None,
    confirm: bool,
    backup_directory: Path | None,
    csv_output: Path | None = None,
    json_output: Path | None = None,
    migration_directory: Path = DEFAULT_MIGRATION_DIRECTORY,
) -> dict:
    if limit is not None and limit < 1:
        raise ValueError("--limit must be at least 1.")

    if confirm and backup_directory is None:
        raise ValueError("--backup-directory is required with --confirm.")

    database = database.resolve(strict=False)

    if not database.is_file():
        raise FileNotFoundError(f"Database does not exist: {database}")

    category_set = (
        frozenset(categories)
        if categories
        else DEFAULT_QUARANTINE_CATEGORIES
    )
    exclusion_set = frozenset(exclude_series)

    started = time.perf_counter()

    with database_connection(database) as connection:
        applied_migrations = apply_migrations(
            connection,
            migration_directory,
        )

        # quick_check on a large database is expensive (a full scan of
        # every page). It's only load-bearing as a pre-mutation safety
        # gate, so it's skipped for a plain preview and only paid for
        # when a real move is about to happen.
        quick_check_before = _quick_check(connection) if confirm else None
        repository = QuarantineRepository(connection)
        candidates = repository.find_candidates(
            categories=category_set,
            exclude_series=exclusion_set,
        )
        bounded = (
            candidates if limit is None else candidates[:limit]
        )

        backup_path: Path | None = None
        item_results = []

        if confirm:
            if quick_check_before != "ok":
                raise RuntimeError(
                    "Refusing to quarantine: PRAGMA quick_check "
                    f"reported {quick_check_before!r}, not 'ok'."
                )

            assert backup_directory is not None
            backup_path = _backup_database(database, backup_directory)

            item_results = execute_quarantine(
                connection,
                candidates,
                quarantine_root=quarantine_root,
                limit=limit,
            )

        quick_check_after = (
            _quick_check(connection) if confirm else quick_check_before
        )
        pending_redownload = repository.pending_redownload_count()

    elapsed = time.perf_counter() - started

    if confirm:
        plan_rows = [
            {
                "archive_id": result.archive_id,
                "job_id": next(
                    (
                        c.job_id
                        for c in bounded
                        if c.archive_id == result.archive_id
                    ),
                    None,
                ),
                "series_name": next(
                    (
                        c.series_name
                        for c in bounded
                        if c.archive_id == result.archive_id
                    ),
                    None,
                ),
                "failure_category": next(
                    (
                        c.failure_category
                        for c in bounded
                        if c.archive_id == result.archive_id
                    ),
                    None,
                ),
                "attempts": next(
                    (
                        c.attempts
                        for c in bounded
                        if c.archive_id == result.archive_id
                    ),
                    None,
                ),
                "max_attempts": next(
                    (
                        c.max_attempts
                        for c in bounded
                        if c.archive_id == result.archive_id
                    ),
                    None,
                ),
                "source_path": result.source_path,
                "destination_path": result.destination_path,
                "status": result.status,
                "error": result.error,
            }
            for result in item_results
        ]
        moved = sum(1 for r in item_results if r.status == "moved")
        errored = sum(1 for r in item_results if r.status == "error")
    else:
        plan_rows = [
            {
                "archive_id": c.archive_id,
                "job_id": c.job_id,
                "series_name": c.series_name,
                "failure_category": c.failure_category,
                "attempts": c.attempts,
                "max_attempts": c.max_attempts,
                "source_path": str(c.source_path),
                "destination_path": str(
                    quarantine_root / c.proposed_filename
                ),
                "status": "planned",
                "error": None,
            }
            for c in bounded
        ]
        moved = 0
        errored = 0

    output = {
        "database": str(database),
        "quarantine_root": str(quarantine_root),
        "categories": sorted(category_set),
        "exclude_series": sorted(exclusion_set),
        "confirm": confirm,
        "candidate_count": len(candidates),
        "planned_count": len(bounded),
        "moved": moved,
        "errored": errored,
        "pending_redownload_total": pending_redownload,
        "quick_check_before": quick_check_before,
        "quick_check_after": quick_check_after,
        "backup_path": str(backup_path) if backup_path else None,
        "applied_migrations": applied_migrations,
        "elapsed_seconds": round(elapsed, 6),
        "plan": plan_rows,
    }

    if csv_output is not None:
        output["csv_output"] = str(_write_plan_csv(csv_output, plan_rows))

    if json_output is not None:
        output["json_output"] = str(_write_json(json_output, output))

    return output


def print_summary(output: dict) -> None:
    mode = "EXECUTED" if output["confirm"] else "PREVIEW (dry-run)"
    print(f"Archive quarantine {mode}.")
    print(f"Database:              {output['database']}")
    print(f"Quarantine root:       {output['quarantine_root']}")
    print(f"Categories:            {', '.join(output['categories'])}")

    if output["exclude_series"]:
        print(
            "Excluded series:       "
            f"{', '.join(output['exclude_series'])}"
        )

    print(f"Candidates found:      {output['candidate_count']}")
    print(f"Planned this run:      {output['planned_count']}")

    if output["confirm"]:
        print(f"Moved:                 {output['moved']}")
        print(f"Errors:                {output['errored']}")
        print(f"Backup:                {output['backup_path']}")

    print(f"Pending redownload:    {output['pending_redownload_total']}")

    if output["confirm"]:
        print(f"quick_check before:    {output['quick_check_before']}")
        print(f"quick_check after:     {output['quick_check_after']}")
    else:
        print(
            "quick_check:           not run (dry-run preview; "
            "checked automatically before --confirm executes)"
        )

    print(f"Elapsed:               {output['elapsed_seconds']:.2f} seconds")

    if output.get("csv_output"):
        print(f"CSV output:            {output['csv_output']}")
    if output.get("json_output"):
        print(f"JSON output:           {output['json_output']}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        output = run_quarantine(
            database=args.database,
            quarantine_root=args.quarantine_root,
            categories=args.categories,
            exclude_series=args.exclude_series,
            limit=args.limit,
            confirm=args.confirm,
            backup_directory=args.backup_directory,
            csv_output=args.csv_output,
            json_output=args.json_output,
        )
    except Exception as exc:
        print(f"Archive quarantine failed: {exc}", file=sys.stderr)
        return 1

    print_summary(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
