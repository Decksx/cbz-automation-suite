from pathlib import Path

from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations


MIGRATION_DIRECTORY = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


def test_apply_migrations_creates_foundation_schema(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "comics.db"

    with database_connection(database_path) as connection:
        applied = apply_migrations(connection, MIGRATION_DIRECTORY)

        tables = {
            row["name"]
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                """
            ).fetchall()
        }

    assert applied == [1, 2]

    expected_tables = {
        "schema_migrations",
        "application_settings",
        "processing_runs",
        "processing_stages",
        "processing_items",
        "source_batches",
        "archive_files",
        "file_locations",
        "file_events",
        "jobs",
    }

    assert expected_tables.issubset(tables)


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "comics.db"

    with database_connection(database_path) as connection:
        first = apply_migrations(connection, MIGRATION_DIRECTORY)
        second = apply_migrations(connection, MIGRATION_DIRECTORY)

        migration_count = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]

    assert first == [1, 2]
    assert second == []
    assert migration_count == 2
