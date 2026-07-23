from __future__ import annotations

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
