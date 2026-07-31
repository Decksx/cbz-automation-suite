"""Tests for the read-only active-job duplicate preflight.

Every test uses a disposable `tmp_path` SQLite database. Nothing here
touches a production database, backup, report, or archive path.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.jobs.active_job_duplicate_audit import (
    ACTIVE_STATUSES,
    EXIT_BLOCKING_DUPLICATES,
    EXIT_FAILURE,
    EXIT_OK,
    UNIQUE_ACTIVE_INDEX_NAME,
    DatabaseChangedError,
    fingerprint_database,
    main,
    readonly_database_connection,
    run_preflight,
)


MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)

TERMINAL_STATUSES = ("completed", "failed", "cancelled", "blocked")

UNIQUE_ACTIVE_INDEX_SQL = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {UNIQUE_ACTIVE_INDEX_NAME}
    ON jobs(job_type, archive_id)
    WHERE status IN ('pending', 'claimed', 'running')
"""


def migrated(tmp_path: Path, name: str = "preflight.db") -> Path:
    database = tmp_path / name

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATIONS)

    return database


def seed_archive(connection: sqlite3.Connection) -> int:
    return int(
        connection.execute(
            "INSERT INTO archive_files (file_size) VALUES (1024)"
        ).lastrowid
    )


def insert_job(
    connection: sqlite3.Connection,
    *,
    job_type: str = "inspect_archive",
    status: str = "pending",
    archive_id: int | None,
    payload_json: str | None = None,
    error_message: str | None = None,
    worker_id: str | None = None,
) -> int:
    return int(
        connection.execute(
            """
            INSERT INTO jobs (
                job_type, status, archive_id, payload_json,
                error_message, worker_id
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                job_type,
                status,
                archive_id,
                payload_json,
                error_message,
                worker_id,
            ),
        ).lastrowid
    )


# --- clean database passes -------------------------------------------


def test_clean_migrated_database_passes(tmp_path: Path) -> None:
    database = migrated(tmp_path)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)
        insert_job(connection, archive_id=archive_id)

    report = run_preflight(database=database)

    assert report["quick_check"] == "ok"
    assert report["applied_schema_versions"] == [
        1, 2, 3, 4, 5, 6, 7, 8, 9,
    ]
    assert report["unique_active_index_exists"] is False
    assert report["migration_blocked"] is False
    assert report["blocking_groups"] == []
    assert report["blocking_group_count"] == 0
    assert report["blocking_row_count"] == 0
    assert report["total_active_jobs"] == 1
    assert report["database_unchanged"] is True


def test_empty_database_passes(tmp_path: Path) -> None:
    database = migrated(tmp_path, "empty.db")

    report = run_preflight(database=database)

    assert report["migration_blocked"] is False
    assert report["total_active_jobs"] == 0
    assert report["active_by_status"] == {}
    assert report["active_by_job_type"] == {}
    assert report["null_archive_active_jobs"]["total"] == 0


# --- blocking duplicates ---------------------------------------------


@pytest.mark.parametrize(
    ("first_status", "second_status"),
    [
        ("pending", "running"),
        ("pending", "claimed"),
        ("claimed", "running"),
        ("pending", "pending"),
        ("claimed", "claimed"),
        ("running", "running"),
    ],
)
def test_active_status_pairings_block(
    tmp_path: Path,
    first_status: str,
    second_status: str,
) -> None:
    database = migrated(
        tmp_path,
        f"{first_status}_{second_status}.db",
    )

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)
        first_id = insert_job(
            connection,
            status=first_status,
            archive_id=archive_id,
        )
        second_id = insert_job(
            connection,
            status=second_status,
            archive_id=archive_id,
        )

    report = run_preflight(database=database)

    assert report["migration_blocked"] is True
    assert report["blocking_group_count"] == 1
    assert report["blocking_row_count"] == 2

    group = report["blocking_groups"][0]
    assert group["job_type"] == "inspect_archive"
    assert group["archive_id"] == archive_id
    assert group["active_count"] == 2
    assert group["job_ids"] == sorted([first_id, second_id])

    expected_statuses: dict[str, int] = {}
    for status in (first_status, second_status):
        expected_statuses[status] = expected_statuses.get(status, 0) + 1
    assert group["status_counts"] == expected_statuses


def test_three_way_duplicate_reports_all_rows(tmp_path: Path) -> None:
    database = migrated(tmp_path)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)
        ids = [
            insert_job(
                connection,
                status=status,
                archive_id=archive_id,
            )
            for status in ACTIVE_STATUSES
        ]

    report = run_preflight(database=database)
    group = report["blocking_groups"][0]

    assert group["active_count"] == 3
    assert group["job_ids"] == sorted(ids)
    assert group["status_counts"] == {
        "claimed": 1,
        "pending": 1,
        "running": 1,
    }
    assert report["blocking_row_count"] == 3


# --- non-conflicts ----------------------------------------------------


def test_different_job_types_do_not_conflict(tmp_path: Path) -> None:
    database = migrated(tmp_path)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)

        for job_type in (
            "inspect_archive",
            "calculate_archive_hash",
            "hash_archive_pages",
            "hash_archive_pages_perceptual",
        ):
            insert_job(
                connection,
                job_type=job_type,
                archive_id=archive_id,
            )

    report = run_preflight(database=database)

    assert report["migration_blocked"] is False
    assert report["total_active_jobs"] == 4
    assert report["active_by_job_type"] == {
        "calculate_archive_hash": 1,
        "hash_archive_pages": 1,
        "hash_archive_pages_perceptual": 1,
        "inspect_archive": 1,
    }


def test_different_archive_ids_do_not_conflict(tmp_path: Path) -> None:
    database = migrated(tmp_path)

    with database_connection(database) as connection:
        for _ in range(3):
            insert_job(
                connection,
                archive_id=seed_archive(connection),
            )

    report = run_preflight(database=database)

    assert report["migration_blocked"] is False
    assert report["total_active_jobs"] == 3


def test_terminal_history_does_not_block(tmp_path: Path) -> None:
    database = migrated(tmp_path)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)

        # Many terminal rows for one identity, plus a single active row.
        for status in TERMINAL_STATUSES:
            for _ in range(3):
                insert_job(
                    connection,
                    status=status,
                    archive_id=archive_id,
                )

        insert_job(connection, status="running", archive_id=archive_id)

    report = run_preflight(database=database)

    assert report["migration_blocked"] is False
    assert report["blocking_groups"] == []
    # Terminal rows are not counted as active at all.
    assert report["total_active_jobs"] == 1
    assert report["active_by_status"] == {"running": 1}


# --- NULL archive_id --------------------------------------------------


def test_null_archive_active_rows_are_reported_but_nonblocking(
    tmp_path: Path,
) -> None:
    database = migrated(tmp_path)

    with database_connection(database) as connection:
        insert_job(connection, status="pending", archive_id=None)
        insert_job(connection, status="pending", archive_id=None)
        insert_job(connection, status="running", archive_id=None)
        insert_job(
            connection,
            job_type="calculate_archive_hash",
            status="claimed",
            archive_id=None,
        )

    report = run_preflight(database=database)
    null_section = report["null_archive_active_jobs"]

    assert report["migration_blocked"] is False
    assert report["blocking_groups"] == []
    assert null_section["blocking"] is False
    assert null_section["total"] == 4
    assert null_section["by_status"] == {
        "claimed": 1,
        "pending": 2,
        "running": 1,
    }
    assert null_section["by_job_type"] == {
        "calculate_archive_hash": 1,
        "inspect_archive": 3,
    }
    assert "NULL" in null_section["limitation"]


def test_null_archive_rows_do_not_mask_a_real_blocking_group(
    tmp_path: Path,
) -> None:
    database = migrated(tmp_path)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)
        insert_job(connection, status="pending", archive_id=archive_id)
        insert_job(connection, status="running", archive_id=archive_id)
        insert_job(connection, status="pending", archive_id=None)
        insert_job(connection, status="pending", archive_id=None)

    report = run_preflight(database=database)

    assert report["migration_blocked"] is True
    assert report["blocking_group_count"] == 1
    assert report["blocking_groups"][0]["archive_id"] == archive_id
    assert report["null_archive_active_jobs"]["total"] == 2


# --- determinism ------------------------------------------------------


def test_report_is_deterministic_across_runs(tmp_path: Path) -> None:
    database = migrated(tmp_path)

    with database_connection(database) as connection:
        first_archive = seed_archive(connection)
        second_archive = seed_archive(connection)

        # Inserted out of order on purpose.
        insert_job(
            connection,
            job_type="inspect_archive",
            status="running",
            archive_id=second_archive,
        )
        insert_job(
            connection,
            job_type="calculate_archive_hash",
            status="pending",
            archive_id=second_archive,
        )
        insert_job(
            connection,
            job_type="inspect_archive",
            status="pending",
            archive_id=second_archive,
        )
        insert_job(
            connection,
            job_type="calculate_archive_hash",
            status="claimed",
            archive_id=second_archive,
        )
        insert_job(
            connection,
            job_type="inspect_archive",
            status="pending",
            archive_id=first_archive,
        )
        insert_job(
            connection,
            job_type="inspect_archive",
            status="claimed",
            archive_id=first_archive,
        )

    first_report = run_preflight(database=database)
    second_report = run_preflight(database=database)

    from comic_automation.jobs.active_job_duplicate_audit import (
        render_json,
    )

    # Volatile-by-design keys aside, the rendered JSON must be
    # byte-identical between runs.
    assert render_json(first_report) == render_json(second_report)

    # Groups ordered by (job_type, archive_id); ids ascending within.
    identities = [
        (group["job_type"], group["archive_id"])
        for group in first_report["blocking_groups"]
    ]
    assert identities == sorted(identities)
    assert identities == [
        ("calculate_archive_hash", second_archive),
        ("inspect_archive", first_archive),
        ("inspect_archive", second_archive),
    ]

    for group in first_report["blocking_groups"]:
        assert group["job_ids"] == sorted(group["job_ids"])

    assert first_report["blocking_group_count"] == 3
    assert first_report["blocking_row_count"] == 6


# --- read-only enforcement -------------------------------------------


def test_readonly_connection_rejects_writes(tmp_path: Path) -> None:
    database = migrated(tmp_path)

    with readonly_database_connection(database) as connection:
        for statement in (
            "INSERT INTO jobs (job_type, status) VALUES ('x', 'pending')",
            "UPDATE jobs SET status = 'failed'",
            "DELETE FROM jobs",
            "CREATE TABLE scratch (id INTEGER)",
        ):
            with pytest.raises(sqlite3.OperationalError):
                connection.execute(statement)


def test_preflight_applies_no_migrations(tmp_path: Path) -> None:
    """An un-migrated database is read as-is, never upgraded."""
    database = tmp_path / "bare.db"

    with database_connection(database) as connection:
        connection.execute(
            """
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY,
                job_type TEXT NOT NULL,
                status TEXT NOT NULL,
                archive_id INTEGER
            )
            """
        )

    report = run_preflight(database=database)

    assert report["applied_schema_versions"] == []
    assert report["unique_active_index_exists"] is False
    assert report["migration_blocked"] is False

    with database_connection(database) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    # No migration ran: none of the migrated schema appeared.
    assert "schema_migrations" not in tables
    assert "archive_files" not in tables


def test_database_bytes_and_metadata_are_unchanged(
    tmp_path: Path,
) -> None:
    database = migrated(tmp_path)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)
        insert_job(connection, status="pending", archive_id=archive_id)
        insert_job(connection, status="running", archive_id=archive_id)

    before_bytes = database.read_bytes()
    before_fingerprint = fingerprint_database(database)

    report = run_preflight(database=database)

    after_bytes = database.read_bytes()
    after_fingerprint = fingerprint_database(database)

    assert before_bytes == after_bytes
    assert before_fingerprint == after_fingerprint
    assert report["database_unchanged"] is True
    assert (
        report["database_size_bytes_before"]
        == report["database_size_bytes_after"]
    )
    assert (
        report["database_modified_time_ns_before"]
        == report["database_modified_time_ns_after"]
    )


# --- concurrent change invalidates the run ----------------------------


def test_external_commit_during_audit_invalidates_the_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commit from another connection mid-run must be rejected."""
    database = migrated(tmp_path)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)

    import comic_automation.jobs.active_job_duplicate_audit as module

    real_collect = module.collect_blocking_groups

    def collect_then_external_write(connection):
        result = real_collect(connection)

        # A different connection commits while the audit is mid-read.
        with database_connection(database) as other:
            insert_job(other, status="pending", archive_id=archive_id)

        return result

    monkeypatch.setattr(
        module,
        "collect_blocking_groups",
        collect_then_external_write,
    )

    with pytest.raises(DatabaseChangedError):
        run_preflight(database=database)


def test_external_wal_commit_during_quick_check_is_detected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A WAL commit landing during quick_check must invalidate the run.

    Regression test for a real gap: quick_check used to run *before*
    `data_version_before` was sampled, leaving it outside the
    change-detection window. A WAL commit there was accepted silently,
    because a WAL write can modify only the `-wal` file -- the main
    database's size and mtime stay identical, so the fingerprint
    comparison cannot catch it either. The snapshot boundary now
    encloses quick_check.
    """
    database = migrated(tmp_path)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)

    import comic_automation.jobs.active_job_duplicate_audit as module

    real_quick_check = module.quick_check
    fingerprint_at_commit: dict[str, object] = {}

    def quick_check_then_external_wal_commit(connection):
        result = real_quick_check(connection)

        before = fingerprint_database(database)

        # database_connection() opens in WAL mode (PRAGMA journal_mode
        # = WAL), so this commit may land entirely in the -wal file.
        with database_connection(database) as other:
            insert_job(other, status="pending", archive_id=archive_id)

        fingerprint_at_commit["before"] = before
        fingerprint_at_commit["after"] = fingerprint_database(database)

        return result

    monkeypatch.setattr(
        module,
        "quick_check",
        quick_check_then_external_wal_commit,
    )

    with pytest.raises(DatabaseChangedError):
        run_preflight(database=database)

    # The commit really did happen...
    with database_connection(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM jobs"
            ).fetchone()[0]
            == 1
        )

    # ...and this proves *why* data_version is required: the WAL commit
    # left the main database file's size and mtime completely
    # unchanged, so the fingerprint check could not have raised. The
    # DatabaseChangedError above therefore came from the corrected
    # data_version boundary, not from the fingerprint fallback.
    assert (
        fingerprint_at_commit["before"]
        == fingerprint_at_commit["after"]
    )


# --- works before and after the index exists --------------------------


def test_works_after_the_unique_index_already_exists(
    tmp_path: Path,
) -> None:
    database = migrated(tmp_path)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)
        insert_job(connection, status="pending", archive_id=archive_id)
        # Terminal duplicates are permitted by the index.
        insert_job(
            connection,
            status="completed",
            archive_id=archive_id,
        )
        connection.execute(UNIQUE_ACTIVE_INDEX_SQL)

    report = run_preflight(database=database)

    assert report["unique_active_index_exists"] is True
    assert report["migration_blocked"] is False
    assert report["total_active_jobs"] == 1


def test_reports_index_absent_before_migration(tmp_path: Path) -> None:
    database = migrated(tmp_path)

    report = run_preflight(database=database)

    assert report["unique_active_index_exists"] is False
    assert (
        report["unique_active_index_name"] == UNIQUE_ACTIVE_INDEX_NAME
    )


# --- privacy ----------------------------------------------------------


def test_report_contains_no_sensitive_content(tmp_path: Path) -> None:
    database = migrated(tmp_path)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)
        insert_job(
            connection,
            status="pending",
            archive_id=archive_id,
            payload_json=json.dumps(
                {"path": r"X:\Comics\Series\issue.cbz"}
            ),
            error_message=r"Failed at \\tower\media\comics\issue.cbz",
            worker_id="tower:4242:cpu-1",
        )
        insert_job(
            connection,
            status="running",
            archive_id=archive_id,
            payload_json=json.dumps({"path": "/mnt/library/other.cbz"}),
            error_message="Permission denied reading /mnt/library",
            worker_id="tower:4242:cpu-2",
        )

    report = run_preflight(database=database)
    serialized = json.dumps(report)

    assert report["migration_blocked"] is True

    for forbidden in (
        "Comics",
        "issue.cbz",
        "tower",
        "Permission denied",
        "Failed at",
        "/mnt/library",
        "cpu-1",
        "payload",
        "error_message",
        "worker_id",
    ):
        assert forbidden not in serialized, forbidden

    group = report["blocking_groups"][0]
    assert set(group) == {
        "job_type",
        "archive_id",
        "active_count",
        "status_counts",
        "job_ids",
    }


# --- missing database -------------------------------------------------


def test_missing_database_fails_without_creating_anything(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "nested" / "absent.db"

    with pytest.raises(FileNotFoundError):
        run_preflight(database=missing)

    assert not missing.exists()
    assert not missing.parent.exists()


def test_missing_database_via_readonly_connection(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "absent.db"

    with pytest.raises(FileNotFoundError):
        with readonly_database_connection(missing):
            pass

    assert not missing.exists()


# --- CLI exit codes ---------------------------------------------------


def test_cli_exits_zero_and_prints_json_when_clean(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = migrated(tmp_path)

    with database_connection(database) as connection:
        insert_job(connection, archive_id=seed_archive(connection))

    exit_code = main(["--database", str(database)])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == EXIT_OK
    assert payload["migration_blocked"] is False
    assert payload["quick_check"] == "ok"


def test_cli_exits_two_when_blocking_duplicates_exist(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = migrated(tmp_path)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)
        insert_job(connection, status="pending", archive_id=archive_id)
        insert_job(connection, status="claimed", archive_id=archive_id)

    exit_code = main(["--database", str(database)])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == EXIT_BLOCKING_DUPLICATES
    assert payload["migration_blocked"] is True
    assert payload["blocking_group_count"] == 1


def test_cli_exits_nonzero_for_missing_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        ["--database", str(tmp_path / "does-not-exist.db")]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == EXIT_FAILURE
    assert exit_code not in (EXIT_OK, EXIT_BLOCKING_DUPLICATES)
    assert payload["error"] == "FileNotFoundError"


def test_cli_output_is_deterministic(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = migrated(tmp_path)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)
        insert_job(connection, status="pending", archive_id=archive_id)
        insert_job(connection, status="running", archive_id=archive_id)

    first_code = main(["--database", str(database)])
    first_out = capsys.readouterr().out
    second_code = main(["--database", str(database)])
    second_out = capsys.readouterr().out

    assert first_code == second_code == EXIT_BLOCKING_DUPLICATES
    assert first_out == second_out
