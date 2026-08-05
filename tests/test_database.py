"""Tests for comic_automation.database.connection: the low-level SQLite
connection factory used throughout the comic_automation package.

database_connection() is expected to create the database file (and its
parent directory) on first use, and to configure every connection with the
project's standard concurrency-safety pragmas: foreign key enforcement, WAL
journaling, NORMAL synchronous mode, and a generous busy_timeout so
concurrent job-queue workers don't fail immediately on lock contention.
"""

from pathlib import Path

from comic_automation.database.connection import database_connection


def test_database_connection_enables_required_pragmas(tmp_path: Path) -> None:
    """Opening a connection to a database path whose parent directory
    doesn't exist yet should create it, and the resulting connection should
    report foreign_keys=1, journal_mode=WAL, synchronous=1 (NORMAL), and a
    busy_timeout of 30000ms -- the pragma set the rest of the package relies
    on for safe concurrent access.
    """
    database_path = tmp_path / "database" / "test.db"

    with database_connection(database_path) as connection:
        foreign_keys = connection.execute(
            "PRAGMA foreign_keys"
        ).fetchone()[0]

        journal_mode = connection.execute(
            "PRAGMA journal_mode"
        ).fetchone()[0]

        synchronous = connection.execute(
            "PRAGMA synchronous"
        ).fetchone()[0]

        busy_timeout = connection.execute(
            "PRAGMA busy_timeout"
        ).fetchone()[0]

    assert database_path.exists()
    assert foreign_keys == 1
    assert journal_mode.lower() == "wal"
    assert synchronous == 1
    assert busy_timeout == 30000
