"""Tests for comic_automation.archive.InspectArchiveHandler: the job-queue
handler that wraps inspect_archive() to persist results into the
archive_inspections table (and back-fill archive_files.page_count) as part
of the normal job pipeline.

Covers persisting a full inspection result (including parsed ComicInfo.xml
as JSON), upsert behavior so re-inspecting an archive doesn't leave
duplicate rows, integration with a real JobWorker for both the success and
failure paths, and failure-category classification: a missing source file
is a retryable "filesystem_not_found", while both a CRC/decompression
error and a plain corrupt archive are permanent "corrupt_archive" failures
that should not consume retry attempts.
"""

from __future__ import annotations

import json
import zipfile
import zlib
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
    """Build a .cbz with two images and a valid ComicInfo.xml at `path`."""
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
    """Insert minimal archive_files/file_locations rows for archive_path
    directly via SQL (bypassing the discovery scanner), returning
    (archive_id, location_id) for tests to build jobs against.
    """
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
    """Running the handler directly on an inspect_archive job should write
    a full archive_inspections row (status, format, page_count,
    comic_info flags, comic_info_json, inspected_file_size), and also
    back-fill archive_files.page_count for quick lookups elsewhere.
    """
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
    """Running the handler twice for the same archive (as would happen on
    re-inspection after a file change) should leave exactly one
    archive_inspections row -- the second run upserts rather than
    inserting a duplicate.
    """
    database = tmp_path / "inspection.db"
    archive = create_cbz(tmp_path / "library" / "issue.cbz")

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        archive_id, _ = seed_archive(connection, archive)

        queue = JobQueue(connection)
        handler = InspectArchiveHandler(connection)

        first_job = queue.enqueue(
            "inspect_archive",
            archive_id=archive_id,
        )
        handler(first_job)

        # Retire the first job before enqueueing its re-inspection.
        # This test drives the handler directly rather than through a
        # JobWorker, so nothing else advances the job past 'pending';
        # leaving it active while enqueueing a second job for the same
        # (job_type, archive_id) would violate the partial unique index
        # added in migration 010 and does not reflect a state the real
        # pipeline produces (a worker always completes or fails a job
        # before another for the same archive becomes active).
        connection.execute(
            "UPDATE jobs SET status = 'completed' WHERE id = ?",
            (first_job.id,),
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
    """Integration check: wiring InspectArchiveHandler into a real
    JobWorker and running once should complete the job successfully and
    produce exactly one inspection row.
    """
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
    """If the file_locations row points at a path that no longer exists on
    disk, the handler should fail the job cleanly (not raise/crash the
    worker) with failure_category "filesystem_not_found" and the missing
    filename in the error message.
    """
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
    assert failed.failure_category == "filesystem_not_found"


def test_crc_decompression_error_fails_permanently_on_first_attempt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """With verify_crc=True, a zlib decompression error surfaced by
    ZipFile.testzip (simulated here via monkeypatch to avoid needing a
    genuinely corrupt compressed stream) should fail the job immediately
    on the first attempt as failure_category "corrupt_archive" -- retrying
    a permanently corrupt archive wouldn't help.
    """
    database = tmp_path / "inspection.db"
    archive = create_cbz(
        tmp_path / "library" / "damaged-stream.cbz"
    )

    def raise_decompression_error(_archive):
        raise zlib.error(
            "Error -3 while decompressing data: "
            "invalid stored block lengths"
        )

    monkeypatch.setattr(
        zipfile.ZipFile,
        "testzip",
        raise_decompression_error,
    )

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
                    connection,
                    verify_crc=True,
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
    assert failed.failure_category == "corrupt_archive"
    assert "Invalid or corrupt CBZ archive" in failed.error_message


def test_corrupt_archive_fails_permanently_on_first_attempt(
    tmp_path: Path,
) -> None:
    """A file that isn't a valid zip archive at all should also fail
    immediately on the first attempt as "corrupt_archive", the same
    permanent-failure path as the CRC case above.
    """
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
    assert failed.failure_category == "corrupt_archive"
