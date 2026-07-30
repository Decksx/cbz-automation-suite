from __future__ import annotations

import hashlib
from pathlib import Path

from comic_automation.archive.hash_cli import main
from comic_automation.archive.hashing import calculate_archive_hash
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations


MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


def seed_inspected_archive(connection, path: Path) -> int:
    stat = path.stat()
    archive = connection.execute(
        "INSERT INTO archive_files (file_size, page_count) VALUES (?, 1)",
        (stat.st_size,),
    )
    archive_id = int(archive.lastrowid)
    location = connection.execute(
        """
        INSERT INTO file_locations (
            archive_id, path, file_size, modified_time_ns
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            archive_id,
            str(path.resolve()),
            stat.st_size,
            stat.st_mtime_ns,
        ),
    )
    connection.execute(
        """
        INSERT INTO archive_inspections (
            archive_id, location_id, inspected_path, archive_format,
            status, entry_count, page_count, directory_count,
            result_json, inspected_file_size, inspected_modified_time_ns
        )
        VALUES (?, ?, ?, 'cbz', 'ok', 1, 1, 0, '{}', ?, ?)
        """,
        (
            archive_id,
            int(location.lastrowid),
            str(path.resolve()),
            stat.st_size,
            stat.st_mtime_ns,
        ),
    )
    return archive_id


def test_calculate_archive_hash(tmp_path: Path) -> None:
    archive = tmp_path / "issue.cbz"
    archive.write_bytes(b"archive bytes")

    result = calculate_archive_hash(archive, chunk_size=3)

    assert result.algorithm == "sha256"
    assert result.digest == hashlib.sha256(
        b"archive bytes"
    ).hexdigest()
    assert result.bytes_read == len(b"archive bytes")


def test_hash_cli_detects_exact_duplicates(
    tmp_path: Path,
    capsys,
) -> None:
    database = tmp_path / "hashes.db"
    first = tmp_path / "first.cbz"
    second = tmp_path / "second.cbz"
    third = tmp_path / "third.cbz"
    first.write_bytes(b"identical")
    second.write_bytes(b"identical")
    third.write_bytes(b"different")

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATIONS)
        seed_inspected_archive(connection, first)
        seed_inspected_archive(connection, second)
        seed_inspected_archive(connection, third)

    result = main([
        "--database",
        str(database),
        "--limit",
        "3",
        "--progress-every",
        "1",
        "--enqueue-missing",
    ])
    captured = capsys.readouterr()

    assert result == 0
    assert "Succeeded:         3" in captured.out
    assert "Duplicate groups:  1" in captured.out

    with database_connection(database) as connection:
        hashes = connection.execute(
            "SELECT COUNT(*) FROM archive_hashes"
        ).fetchone()[0]
        duplicate_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT digest
                FROM archive_hashes
                GROUP BY digest
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]

    assert hashes == 3
    assert duplicate_count == 1


def test_enqueue_missing_is_idempotent(
    tmp_path: Path,
) -> None:
    database = tmp_path / "hashes.db"
    archive = tmp_path / "issue.cbz"
    archive.write_bytes(b"archive")

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATIONS)
        seed_inspected_archive(connection, archive)

    assert main([
        "--database",
        str(database),
        "--limit",
        "1",
        "--enqueue-missing",
    ]) == 0
    assert main([
        "--database",
        str(database),
        "--limit",
        "1",
        "--enqueue-missing",
        "--report-only",
    ]) == 0

    with database_connection(database) as connection:
        job_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM jobs
            WHERE job_type = 'calculate_archive_hash'
            """
        ).fetchone()[0]

    assert job_count == 1


def test_hash_refreshes_changed_metadata_and_queues_reinspection(
    tmp_path: Path,
) -> None:
    database = tmp_path / "hashes.db"
    archive = tmp_path / "issue.cbz"
    archive.write_bytes(b"original")

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATIONS)
        archive_id = seed_inspected_archive(connection, archive)

    archive.write_bytes(b"changed archive bytes")

    with database_connection(database) as connection:
        from comic_automation.archive.hashing import (
            ArchiveHashRepository,
            CalculateArchiveHashHandler,
        )
        from comic_automation.jobs import JobQueue, JobWorker

        queue = JobQueue(connection)
        job = queue.enqueue(
            "calculate_archive_hash",
            archive_id=archive_id,
        )
        worker = JobWorker(
            queue,
            {
                "calculate_archive_hash": (
                    CalculateArchiveHashHandler(connection)
                )
            },
            worker_id="hash-test",
            poll_interval_seconds=0,
        )

        result = worker.run_once()
        location = connection.execute(
            """
            SELECT file_size, modified_time_ns
            FROM file_locations
            WHERE archive_id = ?
            """,
            (archive_id,),
        ).fetchone()
        reinspection = connection.execute(
            """
            SELECT COUNT(*)
            FROM jobs
            WHERE archive_id = ?
              AND job_type = 'inspect_archive'
              AND status = 'pending'
            """,
            (archive_id,),
        ).fetchone()[0]
        rehash_count = ArchiveHashRepository(
            connection
        ).enqueue_missing()

    stat = archive.stat()
    assert result.succeeded is True
    assert location["file_size"] == stat.st_size
    assert location["modified_time_ns"] == stat.st_mtime_ns
    assert reinspection == 1
    assert rehash_count == 0
