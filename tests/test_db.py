from pathlib import Path

from scripts.db import apply_migrations, connect, current_schema_version, get_status


def migration_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "migrations"


def test_initial_schema_applies(tmp_path: Path):
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
    db_path = tmp_path / "test.db"
    with connect(db_path) as conn:
        assert len(apply_migrations(conn, migration_dir())) == 1
        assert apply_migrations(conn, migration_dir()) == []


def test_status_uses_wal_and_foreign_keys(tmp_path: Path):
    db_path = tmp_path / "test.db"
    with connect(db_path) as conn:
        apply_migrations(conn, migration_dir())

    status = get_status(db_path, migration_dir())
    assert status.schema_version == 1
    assert status.pending_migrations == 0
    assert status.journal_mode.lower() == "wal"
    assert status.foreign_keys_enabled is True
