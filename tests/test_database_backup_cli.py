from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from comic_automation.database.backup_cli import (
    BackupDestinationExistsError,
    DatabaseIntegrityError,
    DatabaseMutatedError,
    OutputPathCollisionError,
    SchemaMismatchError,
    UNIQUE_ACTIVE_INDEX_NAME,
    run_backup,
)
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations


MIGRATION_DIRECTORY = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


def build_database(database: Path) -> None:
    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)

        connection.execute("BEGIN IMMEDIATE")
        archive_one = connection.execute(
            """
            INSERT INTO archive_files (sha256, file_size)
            VALUES ('sha-one', 1024)
            """
        ).lastrowid
        archive_two = connection.execute(
            """
            INSERT INTO archive_files (sha256, file_size)
            VALUES ('sha-two', 2048)
            """
        ).lastrowid
        connection.execute(
            """
            INSERT INTO file_locations (archive_id, path, file_size)
            VALUES (?, '/comics/one.cbz', 1024)
            """,
            (archive_one,),
        )
        connection.execute(
            """
            INSERT INTO file_locations (archive_id, path, file_size)
            VALUES (?, '/comics/two.cbz', 2048)
            """,
            (archive_two,),
        )
        connection.execute(
            """
            INSERT INTO jobs (job_type, status, archive_id, attempts, max_attempts)
            VALUES ('inspect_archive', 'pending', ?, 0, 3)
            """,
            (archive_one,),
        )
        connection.execute(
            """
            INSERT INTO jobs (job_type, status, archive_id, attempts, max_attempts)
            VALUES ('inspect_archive', 'completed', ?, 1, 3)
            """,
            (archive_two,),
        )
        connection.execute("COMMIT")


# --- happy path -----------------------------------------------------------


def test_backup_round_trip_succeeds_and_verifies(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    json_output = tmp_path / "report.json"
    build_database(database)

    report = run_backup(
        database=database,
        backup=backup,
        json_output=json_output,
    )

    assert report["verified"] is True
    assert all(report["checks"].values())
    assert backup.is_file()
    assert json_output.is_file()

    assert report["source_quick_check_before"] == "ok"
    assert report["source_quick_check_after"] == "ok"
    assert report["backup_quick_check"] == "ok"
    assert report["source_schema_versions"] == report["backup_schema_versions"]
    assert report["source_schema_versions"] == list(range(1, 11))
    assert set(report["source_index_names"]) == set(report["backup_index_names"])
    assert report["source_table_counts"] == report["backup_table_counts"]
    assert report["source_table_counts"]["jobs"] == 2
    assert report["source_table_counts"]["archive_files"] == 2

    assert report["source_unique_active_index_sql"] is not None
    assert (
        report["source_unique_active_index_sql"]
        == report["backup_unique_active_index_sql"]
    )
    assert "WHERE status IN ('pending', 'claimed', 'running')" in (
        report["source_unique_active_index_sql"]
    )

    # The JSON report on disk matches what run_backup returned.
    on_disk = json.loads(json_output.read_text(encoding="utf-8"))
    assert on_disk["verified"] is True
    assert on_disk["backup_database"] == report["backup_database"]

    # The source file itself was never touched by the backup.
    assert report["source_size_bytes_before"] == report["source_size_bytes_after"]
    assert (
        report["source_modified_time_ns_before"]
        == report["source_modified_time_ns_after"]
    )


def test_backup_produces_independent_verified_copy(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    build_database(database)

    run_backup(database=database, backup=backup)

    # The backup is a real, independently-openable SQLite database with
    # the same row contents -- not just a report claiming success.
    connection = sqlite3.connect(backup)
    try:
        row = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()
        assert row[0] == 2
    finally:
        connection.close()


# --- refuse to overwrite ---------------------------------------------------


def test_refuses_to_overwrite_existing_backup(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    build_database(database)
    backup.write_bytes(b"not a database, but it exists")

    with pytest.raises(BackupDestinationExistsError):
        run_backup(database=database, backup=backup)

    # The pre-existing file must be left exactly as it was.
    assert backup.read_bytes() == b"not a database, but it exists"


# --- output path collisions ------------------------------------------------


def test_rejects_backup_path_same_as_database(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    build_database(database)

    with pytest.raises(OutputPathCollisionError):
        run_backup(database=database, backup=database)


def test_rejects_json_output_same_as_database(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    build_database(database)

    with pytest.raises(OutputPathCollisionError):
        run_backup(database=database, backup=backup, json_output=database)

    assert not backup.exists()


def test_rejects_json_output_same_as_backup(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    build_database(database)

    with pytest.raises(OutputPathCollisionError):
        run_backup(database=database, backup=backup, json_output=backup)

    assert not backup.exists()


# --- source mutated mid-backup ---------------------------------------------


def test_detects_source_mutated_during_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate a writer landing on the source between the pre- and
    post-backup fingerprint samples by monkeypatching fingerprint_database
    to return a different value the second time it's called."""
    database = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    json_output = tmp_path / "report.json"
    build_database(database)

    import comic_automation.database.backup_cli as backup_cli

    real_fingerprint = backup_cli.fingerprint_database
    calls = {"count": 0}

    def flaky_fingerprint(path):
        calls["count"] += 1
        fingerprint = real_fingerprint(path)
        if calls["count"] == 2:
            # Pretend the file grew, as if a writer committed mid-backup.
            fingerprint = backup_cli.DatabaseFingerprint(
                size_bytes=fingerprint.size_bytes + 4096,
                modified_time_ns=fingerprint.modified_time_ns,
            )
        return fingerprint

    monkeypatch.setattr(backup_cli, "fingerprint_database", flaky_fingerprint)

    with pytest.raises(DatabaseMutatedError):
        run_backup(database=database, backup=backup, json_output=json_output)

    # A best-effort report is still written, marked unverified.
    assert json_output.is_file()
    on_disk = json.loads(json_output.read_text(encoding="utf-8"))
    assert on_disk["verified"] is False
    assert on_disk["checks"]["source_fingerprint_unchanged"] is False


def test_detects_source_data_version_changed_during_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    build_database(database)

    import comic_automation.database.backup_cli as backup_cli

    real_data_version = backup_cli._data_version
    calls = {"count": 0}

    def flaky_data_version(connection):
        calls["count"] += 1
        value = real_data_version(connection)
        if calls["count"] == 2:
            value += 1
        return value

    monkeypatch.setattr(backup_cli, "_data_version", flaky_data_version)

    with pytest.raises(DatabaseMutatedError):
        run_backup(database=database, backup=backup)


# --- corrupted databases fail the quick_check gate --------------------------


def test_corrupted_source_fails_quick_check_gate(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    build_database(database)

    # Corrupt the file on disk directly (bypassing SQLite) so
    # PRAGMA quick_check reports a real integrity failure.
    with open(database, "r+b") as handle:
        handle.seek(100)
        handle.write(b"\xff" * 200)

    with pytest.raises(DatabaseIntegrityError):
        run_backup(database=database, backup=backup)

    assert not backup.exists()


def test_corrupted_backup_fails_quick_check_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force the online backup step to write a corrupt file, then confirm
    the post-backup quick_check gate on the *backup* catches it."""
    database = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    json_output = tmp_path / "report.json"
    build_database(database)

    import comic_automation.database.backup_cli as backup_cli

    real_perform_backup = backup_cli._perform_backup

    def corrupting_perform_backup(source_connection, destination_connection):
        # Perform the real online backup, then corrupt the destination
        # file's pages directly (bypassing SQLite) so the backup file
        # on disk is genuinely damaged.
        real_perform_backup(source_connection, destination_connection)
        destination_connection.commit()
        with open(backup, "r+b") as handle:
            handle.seek(100)
            handle.write(b"\xff" * 200)

    monkeypatch.setattr(
        backup_cli, "_perform_backup", corrupting_perform_backup
    )

    with pytest.raises(DatabaseIntegrityError):
        run_backup(database=database, backup=backup, json_output=json_output)

    on_disk = json.loads(json_output.read_text(encoding="utf-8"))
    assert on_disk["verified"] is False
    assert on_disk["checks"]["backup_quick_check_ok"] is False


# --- schema / index mismatch -------------------------------------------------


def test_detects_index_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force the backup's index_definitions() to omit the unique active
    index, simulating a backup taken from a database whose schema drifted
    from the source (e.g. a partially-migrated copy)."""
    database = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    json_output = tmp_path / "report.json"
    build_database(database)

    import comic_automation.database.backup_cli as backup_cli

    real_index_definitions = backup_cli.index_definitions
    calls = {"count": 0}

    def flaky_index_definitions(connection):
        calls["count"] += 1
        result = real_index_definitions(connection)
        if calls["count"] == 2:
            # Second call is against the backup connection: drop one
            # index to simulate a schema mismatch.
            result = {
                name: sql
                for name, sql in result.items()
                if name != UNIQUE_ACTIVE_INDEX_NAME
            }
        return result

    monkeypatch.setattr(
        backup_cli, "index_definitions", flaky_index_definitions
    )

    with pytest.raises(SchemaMismatchError):
        run_backup(database=database, backup=backup, json_output=json_output)

    assert backup.is_file()  # the backup file itself was still created
    on_disk = json.loads(json_output.read_text(encoding="utf-8"))
    assert on_disk["verified"] is False
    assert on_disk["checks"]["index_names_match"] is False


def test_detects_table_count_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    build_database(database)

    import comic_automation.database.backup_cli as backup_cli

    real_table_counts = backup_cli.table_counts
    calls = {"count": 0}

    def flaky_table_counts(connection, tables):
        calls["count"] += 1
        result = real_table_counts(connection, tables)
        if calls["count"] == 2:
            result = dict(result)
            result["jobs"] = result["jobs"] + 1
        return result

    monkeypatch.setattr(backup_cli, "table_counts", flaky_table_counts)

    with pytest.raises(SchemaMismatchError):
        run_backup(database=database, backup=backup)
