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
    ReportDestinationExistsError,
    SchemaMismatchError,
    UNIQUE_ACTIVE_INDEX_NAME,
    _write_json,
    run_backup,
)
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import (
    apply_migrations,
    discover_migrations,
    migration_version,
)


def all_migration_versions() -> list[int]:
    """Every migration on disk, in order.

    Derived rather than written out, so a new migration does not fail a
    test that is really asserting "backup and source agree".
    """
    directory = (
        Path(__file__).resolve().parents[1]
        / "comic_automation"
        / "database"
        / "migrations"
    )
    return [
        migration_version(path) for path in discover_migrations(directory)
    ]


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
    assert report["source_schema_versions"] == all_migration_versions()
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


# --- schema discovery ------------------------------------------------------
#
# The comparison set must come from the source database at runtime. A
# hand-maintained table list goes stale the moment a migration adds a
# table, and a table nobody compares can differ between source and
# backup while the run still reports itself verified.


def damage_backup_after_copy(
    monkeypatch: pytest.MonkeyPatch, backup: Path, statements: list[str]
) -> None:
    """Let the real online backup run, then mutate the backup's schema.

    Simulates a backup whose schema drifted from the source (a
    partially-migrated copy, a restore from the wrong generation)
    without having to corrupt bytes on disk -- the resulting file is a
    perfectly healthy database that simply is not a faithful copy.
    """
    import comic_automation.database.backup_cli as backup_cli

    real_perform_backup = backup_cli._perform_backup

    def damaging_perform_backup(source_connection, destination_connection):
        real_perform_backup(source_connection, destination_connection)
        destination_connection.commit()
        writable = sqlite3.connect(backup)
        try:
            for statement in statements:
                writable.execute(statement)
            writable.commit()
        finally:
            writable.close()

    monkeypatch.setattr(
        backup_cli, "_perform_backup", damaging_perform_backup
    )


def test_discovers_tables_not_present_in_any_hard_coded_list(
    tmp_path: Path,
) -> None:
    """A table this module has never heard of is still compared.

    Stands in for "migration 011 adds a table": nothing in backup_cli
    names `future_migration_table`, so if it is discovered and counted,
    discovery is genuinely reading the source's schema.
    """
    database = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    build_database(database)

    with database_connection(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE future_migration_table (
                id INTEGER PRIMARY KEY,
                note TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO future_migration_table (note) VALUES ('kept')"
        )
        connection.execute(
            "INSERT INTO future_migration_table (note) VALUES ('also kept')"
        )
        connection.execute("COMMIT")

    report = run_backup(database=database, backup=backup)

    assert report["verified"] is True
    assert "future_migration_table" in report["source_tables"]
    assert "future_migration_table" in report["tables_compared"]
    assert report["source_table_counts"]["future_migration_table"] == 2
    assert report["backup_table_counts"]["future_migration_table"] == 2
    assert report["table_selection"] == "discovered_from_source"


def test_discovered_tables_cover_every_migration_table(
    tmp_path: Path,
) -> None:
    """Discovery finds every table the migrations create, including
    `schema_migrations`, which is compared like any other user table."""
    database = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    build_database(database)

    raw = sqlite3.connect(database)
    try:
        expected = {
            row[0]
            for row in raw.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
            if not row[0].startswith("sqlite_")
        }
    finally:
        raw.close()

    report = run_backup(database=database, backup=backup)

    assert set(report["source_tables"]) == expected
    assert set(report["tables_compared"]) == expected
    assert "schema_migrations" in report["tables_compared"]
    assert report["source_table_counts"]["schema_migrations"] == len(
        all_migration_versions()
    )


def test_excludes_sqlite_internal_tables_from_discovery(
    tmp_path: Path,
) -> None:
    """`sqlite_sequence`, `sqlite_stat1` and friends are SQLite's own
    bookkeeping: excluded from table discovery, but still present in
    the whole-schema comparison."""
    database = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    build_database(database)

    with database_connection(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        # AUTOINCREMENT forces SQLite to create `sqlite_sequence`.
        connection.execute(
            """
            CREATE TABLE autoincrementing (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                value TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO autoincrementing (value) VALUES ('x')"
        )
        connection.execute("COMMIT")
        # ANALYZE creates `sqlite_stat1`.
        connection.execute("ANALYZE")

    report = run_backup(database=database, backup=backup)

    assert report["verified"] is True
    assert not [
        name for name in report["source_tables"] if name.startswith("sqlite_")
    ]
    assert not [
        name for name in report["tables_compared"] if name.startswith("sqlite_")
    ]
    assert "autoincrementing" in report["tables_compared"]

    # ...but they are still part of the verbatim schema comparison.
    schema_names = {item["name"] for item in report["source_schema_objects"]}
    assert "sqlite_sequence" in schema_names
    assert "sqlite_stat1" in schema_names


def test_detects_table_missing_from_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backup missing a table entirely must fail loudly."""
    database = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    json_output = tmp_path / "report.json"
    build_database(database)

    damage_backup_after_copy(
        monkeypatch, backup, ["DROP TABLE archive_quarantine"]
    )

    with pytest.raises(SchemaMismatchError):
        run_backup(database=database, backup=backup, json_output=json_output)

    on_disk = json.loads(json_output.read_text(encoding="utf-8"))
    assert on_disk["verified"] is False
    assert on_disk["checks"]["tables_match"] is False
    assert on_disk["checks"]["schema_objects_match"] is False
    assert "archive_quarantine" in on_disk["source_tables"]
    assert "archive_quarantine" not in on_disk["backup_tables"]
    # The missing table shows up as a difference, not as a crash.
    assert on_disk["checks"]["table_counts_match"] is False


def test_detects_altered_index_sql_in_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backup whose index definition text differs is caught by the
    verbatim whole-schema comparison, with no per-index special case."""
    database = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    json_output = tmp_path / "report.json"
    build_database(database)

    damage_backup_after_copy(
        monkeypatch,
        backup,
        [
            f"DROP INDEX {UNIQUE_ACTIVE_INDEX_NAME}",
            # Same name, same columns -- only the partial predicate
            # differs, which a name-only comparison would miss.
            f"""
            CREATE UNIQUE INDEX {UNIQUE_ACTIVE_INDEX_NAME}
                ON jobs(job_type, archive_id)
                WHERE status IN ('pending', 'claimed')
            """,
        ],
    )

    with pytest.raises(SchemaMismatchError):
        run_backup(database=database, backup=backup, json_output=json_output)

    on_disk = json.loads(json_output.read_text(encoding="utf-8"))
    assert on_disk["verified"] is False
    assert on_disk["checks"]["schema_objects_match"] is False
    # Index *names* still match -- only the SQL text drifted.
    assert on_disk["checks"]["index_names_match"] is True
    differing = on_disk["schema_object_differences"]["differing"]
    assert [item["name"] for item in differing] == [UNIQUE_ACTIVE_INDEX_NAME]


def test_detects_extra_schema_object_in_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Schema drift in either direction fails, including objects the
    backup has but the source does not (e.g. a stray view or trigger)."""
    database = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    json_output = tmp_path / "report.json"
    build_database(database)

    damage_backup_after_copy(
        monkeypatch,
        backup,
        ["CREATE VIEW stray_view AS SELECT id FROM jobs"],
    )

    with pytest.raises(SchemaMismatchError):
        run_backup(database=database, backup=backup, json_output=json_output)

    on_disk = json.loads(json_output.read_text(encoding="utf-8"))
    assert on_disk["checks"]["schema_objects_match"] is False
    assert [
        item["name"]
        for item in on_disk["schema_object_differences"]["only_in_backup"]
    ] == ["stray_view"]


# --- optional --tables override ---------------------------------------------


def test_table_override_narrows_row_counts_only(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    build_database(database)

    report = run_backup(
        database=database, backup=backup, tables=["jobs"]
    )

    assert report["verified"] is True
    assert report["table_selection"] == "explicit_override"
    assert report["tables_compared"] == ["jobs"]
    assert report["source_table_counts"] == {"jobs": 2}
    # Discovery still ran: the full table set is reported and compared,
    # and the whole schema is compared regardless of the override.
    assert "archive_files" in report["source_tables"]
    assert report["source_tables"] == report["backup_tables"]
    assert report["source_schema_objects"] == report["backup_schema_objects"]


def test_table_override_rejects_unknown_table(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    build_database(database)

    with pytest.raises(ValueError, match="do not exist in the source"):
        run_backup(
            database=database, backup=backup, tables=["not_a_real_table"]
        )


# --- the verification report is evidence, not scratch space -----------------


def test_rejects_pre_existing_json_output_before_doing_any_work(
    tmp_path: Path,
) -> None:
    """A prior report proves an earlier backup was sound; overwriting it
    destroys the audit trail. The rejection must happen before the
    backup is made, not at write time."""
    database = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    json_output = tmp_path / "report.json"
    build_database(database)
    json_output.write_text("earlier evidence\n", encoding="utf-8")

    with pytest.raises(ReportDestinationExistsError):
        run_backup(database=database, backup=backup, json_output=json_output)

    # No backup work happened, and the old report is untouched.
    assert not backup.exists()
    assert json_output.read_text(encoding="utf-8") == "earlier evidence\n"


def test_rejects_pre_existing_json_output_before_creating_directories(
    tmp_path: Path,
) -> None:
    """The rejection lands before any filesystem mutation at all: the
    backup's parent directory must not have been created."""
    database = tmp_path / "source.db"
    backup = tmp_path / "not_yet_created" / "backup.db"
    reports = tmp_path / "reports"
    reports.mkdir()
    json_output = reports / "report.json"
    build_database(database)
    json_output.write_text("earlier evidence\n", encoding="utf-8")

    with pytest.raises(ReportDestinationExistsError):
        run_backup(database=database, backup=backup, json_output=json_output)

    assert not backup.parent.exists()
    assert json_output.read_text(encoding="utf-8") == "earlier evidence\n"


def test_write_json_refuses_to_overwrite_an_existing_file(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "report.json"
    existing.write_text("earlier evidence\n", encoding="utf-8")

    with pytest.raises(ReportDestinationExistsError):
        _write_json(existing, {"verified": True})

    assert existing.read_text(encoding="utf-8") == "earlier evidence\n"


def test_failure_path_report_cannot_clobber_a_file_created_mid_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The best-effort failure report is still not allowed to overwrite.

    Simulates the narrow race the up-front check cannot cover: the
    report path is free at validation time but occupied by the time the
    failing run tries to write. The original failure must still be what
    surfaces, and the file must survive.
    """
    database = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    json_output = tmp_path / "report.json"
    build_database(database)

    import comic_automation.database.backup_cli as backup_cli

    real_fingerprint = backup_cli.fingerprint_database
    calls = {"count": 0}

    def racing_fingerprint(path):
        calls["count"] += 1
        fingerprint = real_fingerprint(path)
        if calls["count"] == 2:
            # Someone else claims the report path mid-run...
            json_output.write_text("earlier evidence\n", encoding="utf-8")
            # ...and the source appears to have been written to, so the
            # run fails and reaches the best-effort report writer.
            fingerprint = backup_cli.DatabaseFingerprint(
                size_bytes=fingerprint.size_bytes + 4096,
                modified_time_ns=fingerprint.modified_time_ns,
            )
        return fingerprint

    monkeypatch.setattr(backup_cli, "fingerprint_database", racing_fingerprint)

    with pytest.raises(DatabaseMutatedError):
        run_backup(database=database, backup=backup, json_output=json_output)

    assert json_output.read_text(encoding="utf-8") == "earlier evidence\n"
