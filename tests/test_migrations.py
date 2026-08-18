"""Tests for comic_automation.database.migrations: applying the numbered
.sql migration files under comic_automation/database/migrations/ to build
up the application's schema, and confirming re-applying them is a no-op.
"""

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
    """Applying all migrations to a brand-new database should apply
    migrations 1 through 10 in order and create every table the package
    currently depends on -- spanning the operational foundation (jobs,
    processing runs, file tracking) as well as the archive/perceptual-hash
    tables added by later migrations.
    """
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

    assert applied == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]

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
        "archive_pages",
        "page_hashes",
        "archive_content_signatures",
        "near_duplicate_candidates",
        "archive_quarantine",
    }

    # Subset check (not equality) so future migrations adding more tables
    # don't break this test.
    assert expected_tables.issubset(tables)


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    """Applying the full migration set twice should apply all 10 the first
    time and zero the second time, with schema_migrations ending up with
    exactly one row per migration (no duplicate/re-applied entries).
    """
    database_path = tmp_path / "comics.db"

    with database_connection(database_path) as connection:
        first = apply_migrations(connection, MIGRATION_DIRECTORY)
        second = apply_migrations(connection, MIGRATION_DIRECTORY)

        migration_count = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]

    assert first == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
    assert second == []
    assert migration_count == 11
