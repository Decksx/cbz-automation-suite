from pathlib import Path

from comic_automation.database.connection import database_connection


def test_database_connection_enables_required_pragmas(tmp_path: Path) -> None:
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
