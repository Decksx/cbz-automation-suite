from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from comic_automation.archive.perceptual_failure_audit import (
    JOB_TYPE,
    DatabaseChangedError,
    DatabaseIntegrityError,
    DatabaseMutatedError,
    category_counts,
    collect_failures,
    fingerprint_database,
    group_by_category,
    main,
    readonly_database_connection,
    run_audit,
    stable_category,
)
from comic_automation.database.connection import (
    connect_database,
    database_connection,
)
from comic_automation.database.migrations import apply_migrations


MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


def seed_archive(
    connection: sqlite3.Connection,
    *,
    path: str | None,
) -> int:
    """Insert an archive_files row and, if path is given, its current
    file_locations row. Passing path=None simulates an archive whose
    current location has since been moved/deleted (no is_current = 1
    row), which the audit must still report with current_path = None.
    """
    archive = connection.execute(
        "INSERT INTO archive_files (file_size) VALUES (?)",
        (1024,),
    )
    archive_id = int(archive.lastrowid)

    if path is not None:
        connection.execute(
            """
            INSERT INTO file_locations (
                archive_id, path, file_size, is_current
            )
            VALUES (?, ?, ?, 1)
            """,
            (archive_id, path, 1024),
        )

    return archive_id


def seed_failed_job(
    connection: sqlite3.Connection,
    *,
    archive_id: int,
    failure_category: str | None,
    error_message: str,
    attempts: int = 3,
    job_type: str = JOB_TYPE,
) -> int:
    job = connection.execute(
        """
        INSERT INTO jobs (
            job_type, status, archive_id, attempts, max_attempts,
            failure_category, error_message, completed_at
        )
        VALUES (?, 'failed', ?, ?, 3, ?, ?, '2026-07-29T12:00:00')
        """,
        (
            job_type,
            archive_id,
            attempts,
            failure_category,
            error_message,
        ),
    )
    return int(job.lastrowid)


def build_populated_database(database: Path) -> None:
    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATIONS)

        corrupt_image_archive = seed_archive(
            connection, path=r"X:\Comics\Series A\issue-01.cbz"
        )
        seed_failed_job(
            connection,
            archive_id=corrupt_image_archive,
            failure_category="page_image_corrupt",
            error_message="Invalid or unsupported image page 'p1.jpg'",
        )

        corrupt_archive_archive = seed_archive(
            connection, path=r"X:\Comics\Series B\issue-02.cbz"
        )
        seed_failed_job(
            connection,
            archive_id=corrupt_archive_archive,
            failure_category="archive_corrupt",
            error_message="Invalid or corrupt CBZ archive",
        )

        # Archive whose current location no longer exists.
        missing_location_archive = seed_archive(connection, path=None)
        seed_failed_job(
            connection,
            archive_id=missing_location_archive,
            failure_category="filesystem_not_found",
            error_message="No such file or directory",
        )

        permission_archive = seed_archive(
            connection, path=r"X:\Comics\Series C\issue-03.cbz"
        )
        seed_failed_job(
            connection,
            archive_id=permission_archive,
            failure_category="filesystem_permission",
            error_message="Permission denied",
        )

        unclassified_archive = seed_archive(
            connection, path=r"X:\Comics\Series D\issue-04.cbz"
        )
        seed_failed_job(
            connection,
            archive_id=unclassified_archive,
            failure_category=None,
            error_message="Something went wrong before categorization",
        )

        # A failed job of a *different* job_type must never appear in
        # the perceptual-hashing audit.
        other_job_type_archive = seed_archive(
            connection, path=r"X:\Comics\Series E\issue-05.cbz"
        )
        seed_failed_job(
            connection,
            archive_id=other_job_type_archive,
            failure_category="corrupt_archive",
            error_message="Invalid or corrupt CBZ archive",
            job_type="inspect_archive",
        )

        # A non-terminal (pending) job for the same job_type must also
        # never appear in the audit.
        pending_archive = seed_archive(
            connection, path=r"X:\Comics\Series F\issue-06.cbz"
        )
        connection.execute(
            """
            INSERT INTO jobs (job_type, status, archive_id)
            VALUES (?, 'pending', ?)
            """,
            (JOB_TYPE, pending_archive),
        )


# --- stable_category -------------------------------------------------


def test_stable_category_maps_known_raw_categories() -> None:
    assert stable_category("page_image_corrupt") == "corrupt_images"
    assert stable_category("archive_corrupt") == "corrupt_archives"
    assert stable_category("archive_unreadable") == "corrupt_archives"
    assert stable_category("filesystem_not_found") == "missing_files"
    assert stable_category("filesystem_permission") == "permissions"
    assert (
        stable_category("unsupported_archive_format")
        == "unsupported_formats"
    )


def test_stable_category_falls_back_to_unclassified() -> None:
    assert stable_category(None) == "unclassified"
    assert stable_category("filesystem_io") == "unclassified"
    assert stable_category("some_future_category") == "unclassified"


# --- category grouping -------------------------------------------------


def test_category_grouping(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"
    build_populated_database(database)

    with readonly_database_connection(database) as connection:
        failures = collect_failures(connection)

    assert len(failures) == 5  # excludes other job_type and pending

    grouped = group_by_category(failures)
    assert [f["failure_category"] for f in grouped["corrupt_images"]] == [
        "page_image_corrupt"
    ]
    assert [f["failure_category"] for f in grouped["corrupt_archives"]] == [
        "archive_corrupt"
    ]
    assert [f["failure_category"] for f in grouped["missing_files"]] == [
        "filesystem_not_found"
    ]
    assert [f["failure_category"] for f in grouped["permissions"]] == [
        "filesystem_permission"
    ]
    assert grouped["unsupported_formats"] == []
    assert [f["failure_category"] for f in grouped["unclassified"]] == [
        "legacy_unclassified"
    ]

    counts = category_counts(failures)
    assert counts == {
        "corrupt_images": 1,
        "corrupt_archives": 1,
        "missing_files": 1,
        "permissions": 1,
        "unsupported_formats": 0,
        "unclassified": 1,
    }


# --- missing locations -------------------------------------------------


def test_missing_location_reports_none_path(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"
    build_populated_database(database)

    with readonly_database_connection(database) as connection:
        failures = collect_failures(connection)

    missing = [
        f for f in failures if f["failure_category"] == "filesystem_not_found"
    ]
    assert len(missing) == 1
    assert missing[0]["current_path"] is None
    assert missing[0]["archive_id"] is not None


# --- output generation -------------------------------------------------


def test_run_audit_generates_json_and_csv(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"
    build_populated_database(database)

    json_output = tmp_path / "reports" / "failures.json"
    csv_output = tmp_path / "reports" / "failures.csv"

    output = run_audit(
        database=database,
        json_output=json_output,
        csv_output=csv_output,
    )

    assert output["terminal_failure_count"] == 5
    assert output["job_type"] == JOB_TYPE
    assert output["status"] == "failed"

    assert json_output.is_file()
    payload = json.loads(json_output.read_text(encoding="utf-8"))
    assert payload["terminal_failure_count"] == 5
    assert len(payload["failures"]) == 5

    assert csv_output.is_file()
    csv_text = csv_output.read_text(encoding="utf-8-sig")
    header = csv_text.splitlines()[0]
    assert header == (
        "job_id,archive_id,current_path,failure_category,"
        "stable_category,failure_message,attempts,completed_at"
    )
    # Header + 5 data rows.
    assert len(csv_text.splitlines()) == 6


def test_cli_main_writes_reports(tmp_path: Path, capsys) -> None:
    database = tmp_path / "audit.db"
    build_populated_database(database)

    json_output = tmp_path / "failures.json"
    csv_output = tmp_path / "failures.csv"

    result = main(
        [
            "--database",
            str(database),
            "--json-output",
            str(json_output),
            "--csv-output",
            str(csv_output),
        ]
    )
    captured = capsys.readouterr()

    assert result == 0
    assert "Terminal failures:   5" in captured.out
    assert json_output.is_file()
    assert csv_output.is_file()


# --- read-only preservation -------------------------------------------------


def test_read_only_connection_rejects_writes(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"
    build_populated_database(database)

    with readonly_database_connection(database) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute(
                "UPDATE jobs SET status = 'pending' WHERE id = 1"
            )


def test_run_audit_leaves_database_byte_identical(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"
    build_populated_database(database)

    before = fingerprint_database(database)
    before_bytes = database.read_bytes()

    run_audit(database=database)

    after = fingerprint_database(database)
    after_bytes = database.read_bytes()

    assert before == after
    assert before_bytes == after_bytes


def test_run_audit_raises_if_database_mutated_mid_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "audit.db"
    build_populated_database(database)

    import comic_automation.archive.perceptual_failure_audit as audit_module

    original_fingerprint = audit_module.fingerprint_database
    calls = {"count": 0}

    def mutating_fingerprint(path):
        calls["count"] += 1
        if calls["count"] == 2:
            # Simulate the file having been touched between the
            # "before" and "after" snapshots.
            database.write_bytes(database.read_bytes() + b"\x00")
        return original_fingerprint(path)

    monkeypatch.setattr(
        audit_module, "fingerprint_database", mutating_fingerprint
    )

    with pytest.raises(DatabaseMutatedError):
        run_audit(database=database)


def test_missing_database_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        run_audit(database=tmp_path / "does-not-exist.db")


# --- WAL-aware consistent-snapshot guards -------------------------------


def test_wal_commit_can_leave_the_file_fingerprint_unchanged(
    tmp_path: Path,
) -> None:
    """Documents the hazard the data_version guard exists to cover.

    In WAL mode a committed write is appended to the ``-wal`` sidecar;
    the main database file is only rewritten later, at checkpoint. So
    size + mtime of the database itself can be byte-for-byte identical
    across another connection's commit, and this audit's old
    fingerprint-only check would have reported a mixed snapshot as
    clean.
    """
    database = tmp_path / "audit.db"
    build_populated_database(database)

    before = fingerprint_database(database)

    # The writer is deliberately left open across the second stat:
    # closing it would checkpoint the WAL back into the main file and
    # change the fingerprint after the fact. The hazard is about what
    # is observable *at the moment of the commit*.
    writer = connect_database(database)
    try:
        assert (
            writer.execute("PRAGMA journal_mode").fetchone()[0].lower()
            == "wal"
        )
        writer.execute("BEGIN IMMEDIATE")
        archive_id = seed_archive(
            writer, path=r"X:\Comics\Series Z\issue-99.cbz"
        )
        seed_failed_job(
            writer,
            archive_id=archive_id,
            failure_category="archive_corrupt",
            error_message="Invalid or corrupt CBZ archive",
        )
        writer.execute("COMMIT")

        after = fingerprint_database(database)
        assert (database.parent / "audit.db-wal").is_file()
    finally:
        writer.close()

    assert before == after


def test_external_wal_commit_mid_audit_invalidates_the_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commit from another connection mid-read must be rejected."""
    database = tmp_path / "audit.db"
    build_populated_database(database)

    import comic_automation.archive.perceptual_failure_audit as audit_module

    real_collect = audit_module.collect_failures
    fingerprint_at_commit: dict[str, object] = {}

    def collect_then_external_commit(connection):
        result = real_collect(connection)

        before = fingerprint_database(database)

        # A *different* connection commits while the audit is mid-read.
        # database_connection() opens in WAL mode, so this commit can
        # land entirely in the -wal file.
        with database_connection(database) as other:
            archive_id = seed_archive(
                other, path=r"X:\Comics\Series Z\issue-99.cbz"
            )
            seed_failed_job(
                other,
                archive_id=archive_id,
                failure_category="archive_corrupt",
                error_message="Invalid or corrupt CBZ archive",
            )

        fingerprint_at_commit["before"] = before
        fingerprint_at_commit["after"] = fingerprint_database(database)

        return result

    monkeypatch.setattr(
        audit_module, "collect_failures", collect_then_external_commit
    )

    with pytest.raises(DatabaseChangedError) as raised:
        run_audit(database=database)

    # Specifically the data_version guard, not the fingerprint fallback:
    # DatabaseMutatedError is a *subclass* of DatabaseChangedError, so
    # the exact type is what distinguishes which detector fired.
    assert type(raised.value) is DatabaseChangedError
    assert "data_version" in str(raised.value)

    # ...and this is *why* data_version is required: at the moment of
    # the commit the main database file's size and mtime were entirely
    # unchanged, so the fingerprint comparison could not have raised.
    assert fingerprint_at_commit["before"] == fingerprint_at_commit["after"]


def test_quick_check_failure_raises_integrity_error(tmp_path: Path) -> None:
    """A structurally damaged database must abort the audit."""
    database = tmp_path / "audit.db"
    build_populated_database(database)

    # Clobber the final page. The schema stays readable, so the
    # database still opens and the audit gets far enough to run
    # quick_check -- which is the point: the integrity guard, not
    # sqlite3's own open-time errors, is what must fire.
    page_size = 4096
    data = bytearray(database.read_bytes())
    assert len(data) > page_size * 2
    data[-page_size:] = bytes([0x5A]) * page_size
    database.write_bytes(bytes(data))

    with pytest.raises(DatabaseIntegrityError) as raised:
        run_audit(database=database)

    assert "quick_check" in str(raised.value)


def test_report_surfaces_the_snapshot_boundary(tmp_path: Path) -> None:
    """The report states the guarantee it actually has."""
    database = tmp_path / "audit.db"
    build_populated_database(database)

    output = run_audit(database=database)

    assert output["quick_check"] == "ok"
    assert output["data_version_before"] == output["data_version_after"]
    assert output["concurrent_commit_detected"] is False
    assert output["database_unchanged"] is True
    assert output["database_file_unchanged"] is True
    assert output["database_unchanged_is_diagnostic_only"] is True
    assert "data_version" in output["fingerprint_diagnostic_note"]
