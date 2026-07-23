from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Sequence

from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.library.discovery import discover_archives
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
            "Perform a production-safe read-only comic library audit."
        )
    )

    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)

    parser.add_argument(
        "--batch-size",
        type=positive_integer,
        default=500,
    )
    parser.add_argument(
        "--limit",
        type=positive_integer,
    )
    parser.add_argument(
        "--resume",
        action="store_true",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Enumerate metadata without writing to SQLite.",
    )
    parser.add_argument(
        "--progress-every",
        type=positive_integer,
        default=1000,
    )
    parser.add_argument(
        "--json-output",
        type=Path,
    )
    parser.add_argument(
        "--minimum-free-space-gb",
        type=float,
        default=5.0,
    )

    return parser


def preflight(
    root: Path,
    database: Path,
    *,
    minimum_free_space_gb: float,
    dry_run: bool,
) -> None:
    resolved_root = root.resolve(strict=False)

    if not resolved_root.exists():
        raise FileNotFoundError(
            f"Library root does not exist: {resolved_root}"
        )

    if not resolved_root.is_dir():
        raise NotADirectoryError(
            f"Library root is not a directory: {resolved_root}"
        )

    if minimum_free_space_gb < 0:
        raise ValueError(
            "minimum_free_space_gb cannot be negative."
        )

    if dry_run:
        return

    database_parent = database.resolve(
        strict=False
    ).parent
    database_parent.mkdir(parents=True, exist_ok=True)

    usage = shutil.disk_usage(database_parent)
    free_gb = usage.free / (1024 ** 3)

    if free_gb < minimum_free_space_gb:
        raise RuntimeError(
            f"Database volume has only {free_gb:.2f} GB free; "
            f"{minimum_free_space_gb:.2f} GB is required."
        )


def write_json_output(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def run_dry_scan(
    root: Path,
    *,
    limit: int | None,
    progress_every: int,
) -> dict:
    started = time.monotonic()
    scanned = 0
    errors = 0
    limited = False

    def on_error(path: Path, error: OSError) -> None:
        nonlocal errors
        errors += 1
        print(
            f"Warning: unable to inspect {path}: {error}",
            file=sys.stderr,
        )

    for archive in discover_archives(
        root,
        on_error=on_error,
    ):
        if limit is not None and scanned >= limit:
            limited = True
            break

        scanned += 1

        if scanned % progress_every == 0:
            print(
                f"Progress: {scanned:,} files; "
                f"current={archive.path}"
            )

    return {
        "batch_id": None,
        "scanned": scanned,
        "new": 0,
        "changed": 0,
        "unchanged": 0,
        "missing": 0,
        "jobs_queued": 0,
        "errors": errors,
        "resumed": False,
        "limited": limited,
        "dry_run": True,
        "elapsed_seconds": time.monotonic() - started,
    }


def run_discovery(
    *,
    root: Path,
    database: Path,
    batch_size: int,
    limit: int | None = None,
    resume: bool = False,
    dry_run: bool = False,
    progress_every: int = 1000,
    json_output: Path | None = None,
    minimum_free_space_gb: float = 5.0,
    migration_directory: Path = DEFAULT_MIGRATION_DIRECTORY,
) -> int:
    preflight(
        root,
        database,
        minimum_free_space_gb=minimum_free_space_gb,
        dry_run=dry_run,
    )

    if dry_run:
        output = run_dry_scan(
            root,
            limit=limit,
            progress_every=progress_every,
        )
    else:
        def progress(scanned: int, path: Path) -> None:
            if scanned % progress_every == 0:
                print(
                    f"Progress: {scanned:,} files; current={path}"
                )

        with database_connection(database) as connection:
            applied = apply_migrations(
                connection,
                migration_directory,
            )

            summary = scan_library(
                connection,
                root,
                batch_size=batch_size,
                limit=limit,
                resume=resume,
                progress_callback=progress,
            )

        output = {
            **summary.__dict__,
            "migrations": applied,
        }

    output["root"] = str(root.resolve(strict=False))
    output["database"] = str(
        database.resolve(strict=False)
    )

    print("Read-only library discovery completed.")
    print(f"Root:        {output['root']}")
    print(f"Database:    {output['database']}")
    print(f"Batch ID:    {output.get('batch_id')}")
    print(f"Scanned:     {output['scanned']}")
    print(f"New:         {output['new']}")
    print(f"Changed:     {output['changed']}")
    print(f"Unchanged:   {output['unchanged']}")
    print(f"Missing:     {output['missing']}")
    print(f"Jobs queued: {output['jobs_queued']}")
    print(f"Errors:      {output['errors']}")
    print(f"Resumed:     {output['resumed']}")
    print(f"Limited:     {output['limited']}")
    print(f"Dry run:     {output.get('dry_run', False)}")
    print(
        f"Elapsed:     {output['elapsed_seconds']:.2f} seconds"
    )

    if json_output is not None:
        write_json_output(json_output, output)
        print(
            f"JSON output: {json_output.resolve(strict=False)}"
        )

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return run_discovery(
            root=args.root,
            database=args.database,
            batch_size=args.batch_size,
            limit=args.limit,
            resume=args.resume,
            dry_run=args.dry_run,
            progress_every=args.progress_every,
            json_output=args.json_output,
            minimum_free_space_gb=(
                args.minimum_free_space_gb
            ),
        )
    except Exception as exc:
        print(
            f"Library discovery failed: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
