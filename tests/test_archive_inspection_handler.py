from __future__ import annotations

import json
import zipfile
from pathlib import Path

from comic_automation.archive import InspectArchiveHandler
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.jobs import JobQueue, JobWorker, JobStatus


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
        archive.writestr("001.jpg", b"first image")
        archive.writestr("002.webp", b"second image")
        archive.writestr(
            "ComicInfo.xml",
            b"""
            <ComicInfo>
                <Title>Persisted Issue</Title>
                <Series>Persistence Test</Series>
                <Number>4</Number>
                <Writer>Test Writer</Writer>
            </ComicInfo>
            """,
        )

    return path


def seed_archive(
    connection,
    archive_path: Path,
) -> tuple[int, int]:
    stat = archive_path.stat()

    archive_cursor = connection.execute(
        """
        INSERT INTO archive_files (
            file_size
        )
        VALUES (?)
        """,
        (stat.st_size,),
    )
    archive_id = int(archive_cursor.lastrowid)

    location_cursor = connection.execute(
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

    return archive_id, int(location_cursor.lastrowid)


def test_handler_persists_inspection_result(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inspection.db"
    archive = create_cbz(tmp_path / "library" / "issue.cbz")

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        archive_id, location_id = seed_archive(
            connection,
            archive,
        )

        queue = JobQueue(connection)
        job = queue.enqueue(
            "inspect_archive",
            archive_id=archive_id,
        )

        handler = InspectArchiveHandler(connection)
        handler(job)

        row = connection.execute(
            """
            SELECT *
            FROM archive_inspections
            WHERE archive_id = ?
            """,
            (archive_id,),
        ).fetchone()

        archive_row = connection.execute(
            """
            SELECT page_count
            FROM archive_files
            WHERE id = ?
            """,
            (archive_id,),
        ).fetchone()

    assert row is not None
    assert row["location_id"] == location_id
    assert row["status"] == "ok"
    assert row["archive_format"] == "cbz"
    assert row["page_count"] == 2
    assert row["comic_info_present"] == 1
    assert row["comic_info_valid"] == 1
    assert row["inspected_file_size"] == archive.stat().st_size
    assert archive_row["page_count"] == 2

    comic_info = json.loads(row["comic_info_json"])
    assert comic_info["title"] == "Persisted Issue"
    assert comic_info["series"] == "Persistence Test"
    assert comic_info["number"] == "4"


def test_handler_upserts_latest_result(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inspection.db"
    archive = create_cbz(tmp_path / "library" / "issue.cbz")

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        archive_id, _ = seed_archive(connection, archive)

        queue = JobQueue(connection)
        handler = InspectArchiveHandler(connection)

        handler(
            queue.enqueue(
                "inspect_archive",
                archive_id=archive_id,
            )
        )
        handler(
            queue.enqueue(
                "inspect_archive",
                archive_id=archive_id,
            )
        )

        count = connection.execute(
            """
            SELECT COUNT(*)
            FROM archive_inspections
            WHERE archive_id = ?
            """,
            (archive_id,),
        ).fetchone()[0]

    assert count == 1


def test_worker_completes_inspection_job(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inspection.db"
    archive = create_cbz(tmp_path / "library" / "issue.cbz")

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        archive_id, _ = seed_archive(connection, archive)

        queue = JobQueue(connection)
        queued = queue.enqueue(
            "inspect_archive",
            archive_id=archive_id,
        )

        worker = JobWorker(
            queue,
            {
                "inspect_archive": InspectArchiveHandler(
                    connection
                )
            },
            worker_id="inspection-test-worker",
            poll_interval_seconds=0,
        )

        result = worker.run_once()
        completed = queue.get(queued.id)

        inspection_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM archive_inspections
            WHERE archive_id = ?
            """,
            (archive_id,),
        ).fetchone()[0]

    assert result.processed is True
    assert result.succeeded is True
    assert completed.status == JobStatus.COMPLETED
    assert inspection_count == 1


def test_worker_fails_missing_archive_without_crashing(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inspection.db"
    missing = tmp_path / "library" / "missing.cbz"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)

        archive_cursor = connection.execute(
            """
            INSERT INTO archive_files (file_size)
            VALUES (0)
            """
        )
        archive_id = int(archive_cursor.lastrowid)

        connection.execute(
            """
            INSERT INTO file_locations (
                archive_id,
                path,
                file_size,
                modified_time_ns
            )
            VALUES (?, ?, 0, 0)
            """,
            (archive_id, str(missing.resolve())),
        )

        queue = JobQueue(connection)
        queued = queue.enqueue(
            "inspect_archive",
            archive_id=archive_id,
            max_attempts=1,
        )

        worker = JobWorker(
            queue,
            {
                "inspect_archive": InspectArchiveHandler(
                    connection
                )
            },
            worker_id="inspection-test-worker",
            poll_interval_seconds=0,
            retry_delay_seconds=0,
        )

        result = worker.run_once()
        failed = queue.get(queued.id)

    assert result.processed is True
    assert result.succeeded is False
    assert failed.status == JobStatus.FAILED
    assert "missing.cbz" in failed.error_message


def test_corrupt_archive_fails_permanently_on_first_attempt(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inspection.db"
    archive = tmp_path / "library" / "corrupt.cbz"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"not a zip archive")

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        archive_id, _ = seed_archive(connection, archive)
        queue = JobQueue(connection)
        queued = queue.enqueue(
            "inspect_archive",
            archive_id=archive_id,
            max_attempts=3,
        )
        worker = JobWorker(
            queue,
            {
                "inspect_archive": InspectArchiveHandler(
                    connection
                )
            },
            worker_id="inspection-test-worker",
            poll_interval_seconds=0,
        )

        result = worker.run_once()
        failed = queue.get(queued.id)

    assert result.processed is True
    assert result.succeeded is False
    assert failed.status == JobStatus.FAILED
    assert failed.attempts == 1
