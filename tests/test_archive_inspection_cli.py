from __future__ import annotations

import csv
import json
import zipfile
from pathlib import Path

from comic_automation.archive.cli import main
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.jobs import JobQueue


MIGRATION_DIRECTORY = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


def create_cbz(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(
        path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.writestr("001.jpg", b"image")
        archive.writestr(
            "ComicInfo.xml",
            b"""
            <ComicInfo>
                <Title>CLI Issue</Title>
                <Series>CLI Series</Series>
            </ComicInfo>
            """,
        )

    return path


def seed_job(
    connection,
    archive_path: Path,
) -> int:
    stat = archive_path.stat()

    cursor = connection.execute(
        """
        INSERT INTO archive_files (file_size)
        VALUES (?)
        """,
        (stat.st_size,),
    )
    archive_id = int(cursor.lastrowid)

    connection.execute(
        """
        INSERT INTO file_locations (
            archive_id,
            path,
            file_size,
            modified_time_ns
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            archive_id,
            str(archive_path.resolve()),
            stat.st_size,
            stat.st_mtime_ns,
        ),
    )

    JobQueue(connection).enqueue(
        "inspect_archive",
        archive_id=archive_id,
    )

    return archive_id


def test_cli_processes_bounded_jobs(
    tmp_path: Path,
    capsys,
) -> None:
    database = tmp_path / "inspection.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)

        for number in range(3):
            seed_job(
                connection,
                create_cbz(
                    tmp_path
                    / "library"
                    / f"issue-{number}.cbz"
                ),
            )

    result = main(
        [
            "--database",
            str(database),
            "--limit",
            "2",
            "--progress-every",
            "1",
        ]
    )

    captured = capsys.readouterr()

    assert result == 0
    assert "Processed:         2" in captured.out
    assert "Succeeded:         2" in captured.out
    assert "Remaining pending: 1" in captured.out

    with database_connection(database) as connection:
        completed = connection.execute(
            """
            SELECT COUNT(*)
            FROM jobs
            WHERE status = 'completed'
            """
        ).fetchone()[0]

        pending = connection.execute(
            """
            SELECT COUNT(*)
            FROM jobs
            WHERE status = 'pending'
            """
        ).fetchone()[0]

        inspections = connection.execute(
            """
            SELECT COUNT(*)
            FROM archive_inspections
            """
        ).fetchone()[0]

    assert completed == 2
    assert pending == 1
    assert inspections == 2


def test_cli_writes_json_summary(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inspection.db"
    output = tmp_path / "logs" / "summary.json"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        seed_job(
            connection,
            create_cbz(tmp_path / "library" / "issue.cbz"),
        )

    result = main(
        [
            "--database",
            str(database),
            "--limit",
            "1",
            "--json-output",
            str(output),
        ]
    )

    payload = json.loads(output.read_text(encoding="utf-8"))

    assert result == 0
    assert payload["processed"] == 1
    assert payload["succeeded"] == 1
    assert payload["retry_scheduled"] == 0
    assert payload["terminally_failed"] == 0
    assert payload["failed"] == 0
    assert payload["remaining_pending"] == 0
    assert payload["inspection_status_counts"] == {
        "ok": 1
    }


def test_cli_rejects_missing_database(
    tmp_path: Path,
    capsys,
) -> None:
    result = main(
        [
            "--database",
            str(tmp_path / "missing.db"),
            "--limit",
            "1",
        ]
    )

    captured = capsys.readouterr()

    assert result == 1
    assert "Database does not exist" in captured.err


def test_cli_rejects_invalid_limit(
    tmp_path: Path,
    capsys,
) -> None:
    database = tmp_path / "inspection.db"
    database.touch()

    result = main(
        [
            "--database",
            str(database),
            "--limit",
            "0",
        ]
    )

    captured = capsys.readouterr()

    assert result == 1
    assert "--limit must be at least 1" in captured.err


def test_cli_processes_retryable_job_only_once_per_run(
    tmp_path: Path,
    capsys,
) -> None:
    database = tmp_path / "inspection.db"
    missing = tmp_path / "library" / "missing.cbz"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        archive_cursor = connection.execute(
            "INSERT INTO archive_files (file_size) VALUES (0)"
        )
        archive_id = int(archive_cursor.lastrowid)
        connection.execute(
            """
            INSERT INTO file_locations (
                archive_id, path, file_size, modified_time_ns
            )
            VALUES (?, ?, 0, 0)
            """,
            (archive_id, str(missing.resolve())),
        )
        JobQueue(connection).enqueue(
            "inspect_archive",
            archive_id=archive_id,
            max_attempts=3,
        )

    result = main(
        [
            "--database",
            str(database),
            "--limit",
            "10",
            "--retry-delay-seconds",
            "0",
        ]
    )
    captured = capsys.readouterr()

    assert result == 0
    assert "Processed:         1" in captured.out
    assert "Retry scheduled:   1" in captured.out

    with database_connection(database) as connection:
        row = connection.execute(
            "SELECT status, attempts FROM jobs"
        ).fetchone()

    assert row["status"] == "pending"
    assert row["attempts"] == 1


def test_cli_reports_corrupt_archive_as_terminal(
    tmp_path: Path,
    capsys,
) -> None:
    database = tmp_path / "inspection.db"
    archive = tmp_path / "library" / "corrupt.cbz"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"not a zip archive")

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        seed_job(connection, archive)

    result = main(
        [
            "--database",
            str(database),
            "--limit",
            "1",
        ]
    )
    captured = capsys.readouterr()

    assert result == 0
    assert "Terminally failed: 1" in captured.out

    with database_connection(database) as connection:
        row = connection.execute(
            "SELECT status, attempts FROM jobs"
        ).fetchone()

    assert row["status"] == "failed"
    assert row["attempts"] == 1


def test_cli_writes_structured_failure_reports(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inspection.db"
    archive = tmp_path / "library" / "corrupt.cbz"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"not a zip archive")
    json_output = tmp_path / "reports" / "failures.json"
    csv_output = tmp_path / "reports" / "failures.csv"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        seed_job(connection, archive)

    result = main([
        "--database",
        str(database),
        "--limit",
        "1",
        "--failure-json-output",
        str(json_output),
        "--failure-csv-output",
        str(csv_output),
        "--report-only",
    ])

    payload = json.loads(json_output.read_text(encoding="utf-8"))
    with csv_output.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))

    assert result == 0
    assert payload["terminal_failure_count"] == 0
    assert payload["failure_category_counts"] == {}
    assert rows == []


def test_cli_failure_reports_include_terminal_corruption(
    tmp_path: Path,
    capsys,
) -> None:
    database = tmp_path / "inspection.db"
    archive = tmp_path / "library" / "corrupt.cbz"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"not a zip archive")
    json_output = tmp_path / "reports" / "failures.json"
    csv_output = tmp_path / "reports" / "failures.csv"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        seed_job(connection, archive)

    assert main([
        "--database",
        str(database),
        "--limit",
        "1",
    ]) == 0
    assert main([
        "--database",
        str(database),
        "--failure-json-output",
        str(json_output),
        "--failure-csv-output",
        str(csv_output),
        "--report-only",
    ]) == 0
    captured = capsys.readouterr()

    payload = json.loads(json_output.read_text(encoding="utf-8"))
    with csv_output.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))

    assert payload["terminal_failure_count"] == 1
    assert payload["failure_category_counts"] == {
        "corrupt_archive": 1
    }
    assert payload["failures"][0]["failure_kind"] == "permanent"
    assert payload["failures"][0]["error_message"].startswith(
        "Invalid or corrupt CBZ archive:"
    )
    assert rows[0]["failure_category"] == "corrupt_archive"
    assert rows[0]["failure_kind"] == "permanent"
    assert "Recorded terminal failures: 1" in captured.out
    assert "corrupt_archive: 1" in captured.out


def test_failure_category_migration_backfills_legacy_errors(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inspection.db"

    with database_connection(database) as connection:
        for migration in sorted(MIGRATION_DIRECTORY.glob("00[1-4]_*.sql")):
            connection.executescript(
                migration.read_text(encoding="utf-8")
            )

        connection.execute(
            """
            INSERT INTO jobs (
                job_type, status, error_message
            )
            VALUES
                ('inspect_archive', 'failed',
                 'Invalid or corrupt CBZ archive: X:\\bad.cbz'),
                ('inspect_archive', 'failed', 'X:\\missing.cbz')
            """
        )
        migration_five = MIGRATION_DIRECTORY / (
            "005_job_failure_categories.sql"
        )
        connection.executescript(
            migration_five.read_text(encoding="utf-8")
        )
        categories = [
            row["failure_category"]
            for row in connection.execute(
                "SELECT failure_category FROM jobs ORDER BY id"
            )
        ]

    assert categories == [
        "corrupt_archive",
        "filesystem_not_found",
    ]
