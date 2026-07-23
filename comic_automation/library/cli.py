from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.library.repository import scan_library


DEFAULT_MIGRATION_DIRECTORY = (
    Path(__file__).resolve().parents[1]
    / "database"
    / "migrations"
)


def positive_integer(value: str) -> int:
    parsed = int(value)

    if parsed < 1:
        raise argparse.ArgumentTypeError(
            "value must be at least 1"
        )

    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Perform a read-only inventory scan of a comic library."
        )
    )

    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help="Root directory of the comic library to scan.",
    )

    parser.add_argument(
        "--database",
        type=Path,
        required=True,
        help="SQLite database used to record discovery results.",
    )

    parser.add_argument(
        "--batch-size",
        type=positive_integer,
        default=500,
        help=(
            "Maximum number of discovered files written in each "
            "database transaction. Default: 500."
        ),
    )

    return parser


def run_discovery(
    *,
    root: Path,
    database: Path,
    batch_size: int,
    migration_directory: Path = DEFAULT_MIGRATION_DIRECTORY,
) -> int:
    with database_connection(database) as connection:
        applied = apply_migrations(
            connection,
            migration_directory,
        )

        summary = scan_library(
            connection,
            root,
            batch_size=batch_size,
        )

    print("Read-only library discovery completed.")
    print(f"Root:        {root.resolve(strict=False)}")
    print(f"Database:    {database.resolve(strict=False)}")
    print(f"Batch ID:    {summary.batch_id}")
    print(f"Scanned:     {summary.scanned}")
    print(f"New:         {summary.new}")
    print(f"Changed:     {summary.changed}")
    print(f"Unchanged:   {summary.unchanged}")
    print(f"Missing:     {summary.missing}")
    print(f"Jobs queued: {summary.jobs_queued}")

    if applied:
        print(
            "Migrations:  "
            + ", ".join(str(version) for version in applied)
        )
    else:
        print("Migrations:  none")

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return run_discovery(
            root=args.root,
            database=args.database,
            batch_size=args.batch_size,
        )
    except Exception as exc:
        print(
            f"Library discovery failed: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
