"""Tests for comic_automation.database.migrations: applying the numbered
.sql migration files under comic_automation/database/migrations/ to build
up the application's schema, and confirming re-applying them is a no-op.
"""

from pathlib import Path

from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import (
    apply_migrations,
    discover_migrations,
    migration_version,
)


MIGRATION_DIRECTORY = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


def all_versions() -> list[int]:
    """Every migration on disk, in order.

    Derived rather than hard-coded: this list was previously written out
    literally, so every new migration failed these two tests for no reason
    other than being new. What they are actually asserting is that
    apply_migrations() applies all of them once and none of them twice.
    """
    return [migration_version(path)
            for path in discover_migrations(MIGRATION_DIRECTORY)]


def test_apply_migrations_creates_foundation_schema(
    tmp_path: Path,
) -> None:
    """Applying all migrations to a brand-new database should apply every
    migration on disk in order and create every table the package currently
    depends on -- spanning the operational foundation (jobs, processing runs,
    file tracking) as well as the archive, perceptual-hash and disposition
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

    assert applied == all_versions()

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
        "archive_retirements",
        "archive_supersessions",
        "archive_disposition_events",
        "disposition_reversal_context",
    }

    # Subset check (not equality) so future migrations adding more tables
    # don't break this test.
    assert expected_tables.issubset(tables)


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    """Applying the full migration set twice should apply all of them the
    first time and zero the second time, with schema_migrations ending up
    with exactly one row per migration (no duplicate/re-applied entries).
    """
    database_path = tmp_path / "comics.db"

    with database_connection(database_path) as connection:
        first = apply_migrations(connection, MIGRATION_DIRECTORY)
        second = apply_migrations(connection, MIGRATION_DIRECTORY)

        migration_count = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]

    assert first == all_versions()
    assert second == []
    assert migration_count == len(all_versions())


# --- the sequence sentinel -----------------------------------------------
#
# Deriving the version list in the consumer tests above removed five
# repetitive edits per migration, but it also removed the only thing that was
# checking the sequence itself. `discover_migrations()` sorts filenames; it
# does not verify that the numbers are unique or contiguous, so a duplicate
# `013_` or a missing `011_` would now be normalized into whatever the
# consumers expect and pass silently.
#
# This is the one place that knows what the sequence should be. A new
# migration updates HIGHEST_MIGRATION here and nothing else.

HIGHEST_MIGRATION = 14


def test_migration_versions_are_unique_and_contiguous() -> None:
    """The authoritative statement of what the migration set must look like.

    Checked against the filenames on disk rather than against an applied
    database, so a numbering mistake is caught before anything runs it.
    """
    paths = discover_migrations(MIGRATION_DIRECTORY)
    versions = [migration_version(path) for path in paths]

    assert versions == list(range(1, HIGHEST_MIGRATION + 1)), (
        "migration versions must be unique and contiguous from 1 to "
        f"{HIGHEST_MIGRATION}; found {versions}"
    )
    # One file per version. A second `013_*.sql` would otherwise be applied
    # silently, and only one of the two would be recorded under version 13.
    assert len(paths) == len(set(versions))
