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
        isolation_level=None,
    )

    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
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
