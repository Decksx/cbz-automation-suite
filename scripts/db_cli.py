"""CLI for initializing and inspecting the CBZ SQLite database."""

from __future__ import annotations

import argparse
from pathlib import Path

try:
    from scripts.db import DEFAULT_DATABASE_PATH, connect, get_status, initialize_database
except ModuleNotFoundError:
    from db import DEFAULT_DATABASE_PATH, connect, get_status, initialize_database


def main() -> int:
    parser = argparse.ArgumentParser(description="CBZ database utility")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("status")
    sub.add_parser("tables")
    args = parser.parse_args()

    if args.command == "init":
        applied = initialize_database(args.database)
        if not applied:
            print(f"Database already current: {args.database}")
        else:
            print(f"Database initialized: {args.database}")
            for item in applied:
                print(f"  applied {item.version:03d}: {item.name}")
        return 0

    if args.command == "status":
        status = get_status(args.database)
        print(f"Database: {status.database_path}")
        print(f"Schema version: {status.schema_version}")
        print(f"Available migrations: {status.available_migrations}")
        print(f"Pending migrations: {status.pending_migrations}")
        print(f"Journal mode: {status.journal_mode}")
        print(f"Foreign keys: {'enabled' if status.foreign_keys_enabled else 'disabled'}")
        return 0

    with connect(args.database, read_only=True) as conn:
        rows = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
        for row in rows:
            print(row["name"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
