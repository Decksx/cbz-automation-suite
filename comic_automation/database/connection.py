from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator
from contextlib import contextmanager


def connect_database(database_path: str | Path) -> sqlite3.Connection:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(
        path,
        timeout=30.0,
        # Disable pysqlite's implicit transaction handling so the
        # codebase's own BEGIN IMMEDIATE / COMMIT / ROLLBACK statements
        # (see jobs/queue.py, database/migrations.py, etc.) are always
        # the ones in control of transaction boundaries.
        isolation_level=None,
    )

    connection.row_factory = sqlite3.Row

    # Enforce declared FOREIGN KEY ... ON DELETE CASCADE/SET NULL
    # constraints; SQLite ignores them by default per-connection.
    connection.execute("PRAGMA foreign_keys = ON")
    # Write-Ahead Logging: readers don't block writers and vice versa,
    # which matters since this database may be read by CLI tools while
    # a worker is concurrently claiming/completing jobs.
    connection.execute("PRAGMA journal_mode = WAL")
    # NORMAL is safe under WAL (a crash can lose the most recent commit
    # but never corrupts the database) and is significantly faster than
    # FULL for this workload's write volume.
    connection.execute("PRAGMA synchronous = NORMAL")
    # Have SQLite retry internally for up to 30s on SQLITE_BUSY instead
    # of failing immediately when another connection holds a write lock
    # (for example during a worker's BEGIN IMMEDIATE transaction).
    connection.execute("PRAGMA busy_timeout = 30000")

    return connection


@contextmanager
def database_connection(
    database_path: str | Path,
) -> Iterator[sqlite3.Connection]:
    connection = connect_database(database_path)

    try:
        yield connection
    finally:
        connection.close()
