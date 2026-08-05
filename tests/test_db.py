"""Tests for the legacy scripts.db SQLite helper module.

Covers applying the on-disk migration files in migrations/ to a fresh
database, confirming migrations are idempotent (safe to re-apply), and
confirming the connection is configured with WAL journaling and foreign
keys enabled as expected by get_status().
"""

from pathlib import Path

from scripts.db import apply_migrations, connect, current_schema_version, get_status


def migration_dir() -> Path:
    """Resolve the repo's migrations/ directory relative to this test file,
    so tests work regardless of the current working directory.
    """
    return Path(__file__).resolve().parents[1] / "migrations"


def test_initial_schema_applies(tmp_path: Path):
    """Applying migrations to a brand-new database should apply exactly
    migration 1, bump the schema version to 1, and create every table the
    application depends on.
    """
    db_path = tmp_path / "test.db"
    with connect(db_path) as conn:
        applied = apply_migrations(conn, migration_dir())
        assert [m.version for m in applied] == [1]
        assert current_schema_version(conn) == 1
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

    # Core tables the rest of the application relies on existing after
    # migration 1; using subset comparison (<=) so extra tables added by
    # future migrations don't break this test.
    assert {
        "series",
        "series_aliases",
        "archives",
        "pages",
        "processing_runs",
        "archive_series_matches",
        "dedupe_candidates",
        "quality_scores",
        "file_events",
        "routing_log",
        "repair_log",
        "review_queue",
        "application_settings",
        "schema_migrations",
    } <= tables


def test_migrations_are_idempotent(tmp_path: Path):
    """Re-running apply_migrations against an already-migrated database
    should apply zero additional migrations rather than erroring or
    reapplying migration 1.
    """
    db_path = tmp_path / "test.db"
    with connect(db_path) as conn:
        assert len(apply_migrations(conn, migration_dir())) == 1
        assert apply_migrations(conn, migration_dir()) == []


def test_status_uses_wal_and_foreign_keys(tmp_path: Path):
    """get_status() should report the schema as fully up to date and
    confirm the connection was opened with WAL journal mode and foreign
    key enforcement turned on, per the project's SQLite concurrency
    conventions.
    """
    db_path = tmp_path / "test.db"
    with connect(db_path) as conn:
        apply_migrations(conn, migration_dir())

    status = get_status(db_path, migration_dir())
    assert status.schema_version == 1
    assert status.pending_migrations == 0
    assert status.journal_mode.lower() == "wal"
    assert status.foreign_keys_enabled is True
