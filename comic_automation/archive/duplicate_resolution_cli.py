from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from comic_automation.archive.duplicate_resolution import (
    DuplicateResolutionCandidate,
    DuplicateResolutionRepository,
    execute_duplicate_resolution,
    path_is_within,
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
            "Preview or perform guarded exact-duplicate resolution for "
            "archives under an _extraneous holding folder. Execution "
            "recalculates SHA-256 for both copies, moves only the "
            "_extraneous copy into a recoverable backup, and retires "
            "only its database location. Without --confirm this is a "
            "read-only preview."
        )
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument(
        "--extraneous-root",
        type=Path,
        required=True,
        help="Holding folder whose exact duplicate copies may be removed.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Maximum number of eligible duplicates to process.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Execute the recoverable moves and database updates.",
    )
    parser.add_argument(
        "--backup-directory",
        type=Path,
        help=(
            "Parent directory for a timestamped run backup containing "
            "a SQLite backup and the removed files. Required with "
            "--confirm and must be outside --extraneous-root."
        ),
    )
    parser.add_argument("--csv-output", type=Path)
    parser.add_argument("--json-output", type=Path)
    return parser


def _quick_check(connection: sqlite3.Connection) -> str:
    row = connection.execute("PRAGMA quick_check").fetchone()
    return str(row[0]) if row is not None else "unknown"


def _create_run_backup(
    connection: sqlite3.Connection,
    *,
    database: Path,
    backup_directory: Path,
) -> tuple[Path, Path]:
    timestamp = datetime.now(timezone.utc).strftime(
        "%Y%m%d-%H%M%S-%f"
    )
    run_root = (
        backup_directory.resolve(strict=False)
        / f"duplicate-resolution-{timestamp}"
    )
    run_root.mkdir(parents=True, exist_ok=False)
    database_backup = run_root / (
        f"{database.stem}-pre-duplicate-resolution.db"
    )

    destination = sqlite3.connect(database_backup)
    try:
        connection.backup(destination)
    finally:
        destination.close()

    return run_root, database_backup


def _candidate_row(
    candidate: DuplicateResolutionCandidate,
) -> dict:
    return {
        "source_archive_id": candidate.source_archive_id,
        "source_location_id": candidate.source_location_id,
        "source_path": str(candidate.source_path),
        "counterpart_archive_id": candidate.counterpart_archive_id,
        "counterpart_location_id": (
            candidate.counterpart_location_id
        ),
        "counterpart_path": (
            str(candidate.counterpart_path)
            if candidate.counterpart_path is not None
            else None
        ),
        "digest": candidate.digest,
        "file_size": candidate.file_size,
        "status": candidate.status,
        "backup_path": None,
        "error": candidate.error,
    }


def _write_json(path: Path, payload: object) -> Path:
    resolved = path.resolve(strict=False)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return resolved


def _write_csv(path: Path, rows: list[dict]) -> Path:
    resolved = path.resolve(strict=False)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "source_archive_id",
        "source_location_id",
        "source_path",
        "counterpart_archive_id",
        "counterpart_location_id",
        "counterpart_path",
        "digest",
        "file_size",
        "status",
        "backup_path",
        "error",
    ]

    with resolved.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return resolved


def run_duplicate_resolution(
    *,
    database: Path,
    extraneous_root: Path,
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
    extraneous_root = extraneous_root.resolve(strict=False)

    if not database.is_file():
        raise FileNotFoundError(f"Database does not exist: {database}")

    if not extraneous_root.is_dir():
        raise NotADirectoryError(
            f"_extraneous root does not exist: {extraneous_root}"
        )

    if backup_directory is not None:
        backup_directory = backup_directory.resolve(strict=False)
        if path_is_within(backup_directory, extraneous_root):
            raise ValueError(
                "--backup-directory must be outside --extraneous-root."
            )

    started = time.perf_counter()

    with database_connection(database) as connection:
        applied_migrations = apply_migrations(
            connection,
            migration_directory,
        )
        repository = DuplicateResolutionRepository(connection)
        plan = repository.build_plan(
            extraneous_root=extraneous_root,
        )
        eligible = [
            candidate
            for candidate in plan
            if candidate.status == "planned"
        ]
        bounded = eligible if limit is None else eligible[:limit]
        blocked = [
            candidate
            for candidate in plan
            if candidate.status == "blocked"
        ]

        quick_check_before = _quick_check(connection) if confirm else None
        run_backup_root: Path | None = None
        database_backup: Path | None = None
        results = []

        if confirm and bounded:
            if quick_check_before != "ok":
                raise RuntimeError(
                    "Refusing duplicate resolution: PRAGMA "
                    f"quick_check reported {quick_check_before!r}."
                )

            assert backup_directory is not None
            run_backup_root, database_backup = _create_run_backup(
                connection,
                database=database,
                backup_directory=backup_directory,
            )
            results = execute_duplicate_resolution(
                connection,
                bounded,
                extraneous_root=extraneous_root,
                removed_files_root=(
                    run_backup_root / "removed-files"
                ),
            )

        quick_check_after = (
            _quick_check(connection) if confirm else None
        )

    rows_by_id = {
        candidate.source_archive_id: _candidate_row(candidate)
        for candidate in plan
    }

    if confirm:
        for result in results:
            row = rows_by_id[result.source_archive_id]
            row["status"] = result.status
            row["backup_path"] = result.backup_path
            row["error"] = result.error

    rows = list(rows_by_id.values())
    processed = sum(
        1 for result in results if result.status == "backed_up"
    )
    errored = sum(
        1 for result in results if result.status == "error"
    )

    output = {
        "database": str(database),
        "extraneous_root": str(extraneous_root),
        "confirm": confirm,
        "candidate_count": len(plan),
        "eligible_count": len(eligible),
        "blocked_count": len(blocked),
        "planned_count": len(bounded),
        "backed_up": processed,
        "errored": errored,
        "quick_check_before": quick_check_before,
        "quick_check_after": quick_check_after,
        "run_backup_root": (
            str(run_backup_root) if run_backup_root else None
        ),
        "database_backup": (
            str(database_backup) if database_backup else None
        ),
        "applied_migrations": applied_migrations,
        "elapsed_seconds": round(time.perf_counter() - started, 6),
        "plan": rows,
    }

    if csv_output is not None:
        output["csv_output"] = str(_write_csv(csv_output, rows))

    if json_output is not None:
        output["json_output"] = str(_write_json(json_output, output))

    return output


def print_summary(output: dict) -> None:
    mode = "EXECUTED" if output["confirm"] else "PREVIEW (dry-run)"
    print(f"Exact duplicate resolution {mode}.")
    print(f"Database:          {output['database']}")
    print(f"_extraneous root: {output['extraneous_root']}")
    print(f"Entries found:     {output['candidate_count']}")
    print(f"Eligible:          {output['eligible_count']}")
    print(f"Blocked:           {output['blocked_count']}")
    print(f"Planned this run:  {output['planned_count']}")

    if output["confirm"]:
        print(f"Backed up:         {output['backed_up']}")
        print(f"Errors:            {output['errored']}")
        print(f"Run backup:        {output['run_backup_root']}")
        print(f"Database backup:   {output['database_backup']}")
        print(f"quick_check before:{output['quick_check_before']:>8}")
        print(f"quick_check after: {output['quick_check_after']:>8}")
    else:
        print(
            "quick_check:       not run (preview; checked before "
            "--confirm executes)"
        )

    if output.get("csv_output"):
        print(f"CSV output:        {output['csv_output']}")
    if output.get("json_output"):
        print(f"JSON output:       {output['json_output']}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        output = run_duplicate_resolution(
            database=args.database,
            extraneous_root=args.extraneous_root,
            limit=args.limit,
            confirm=args.confirm,
            backup_directory=args.backup_directory,
            csv_output=args.csv_output,
            json_output=args.json_output,
        )
    except Exception as exc:
        print(f"Duplicate resolution failed: {exc}", file=sys.stderr)
        return 1

    print_summary(output)
    return 1 if output["errored"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
