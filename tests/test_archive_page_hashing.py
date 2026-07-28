from __future__ import annotations

import zipfile
import zlib
from pathlib import Path
from unittest.mock import patch

import pytest

from comic_automation.archive.page_hash_cli import main
from comic_automation.archive.page_hashing import calculate_page_hashes
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.jobs import PermanentJobError


MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


def create_cbz(
    path: Path,
    pages: list[tuple[str, bytes]],
    *,
    metadata: str = "<ComicInfo />",
    compression: int = zipfile.ZIP_DEFLATED,
) -> Path:
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for name, payload in pages:
            archive.writestr(name, payload)
        archive.writestr("ComicInfo.xml", metadata)
    return path


def seed_hashed_archive(connection, path: Path) -> int:
    stat = path.stat()
    archive = connection.execute(
        "INSERT INTO archive_files (file_size) VALUES (?)",
        (stat.st_size,),
    )
    archive_id = int(archive.lastrowid)
    connection.execute(
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
        INSERT INTO archive_hashes (
            archive_id,
            algorithm,
            algorithm_version,
            digest,
            file_size,
            modified_time_ns,
            bytes_read
        )
        VALUES (?, 'sha256', '1', ?, ?, ?, ?)
        """,
        (
            archive_id,
            f"{archive_id:064x}",
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_size,
        ),
    )
    return archive_id


def test_calculate_page_hashes_uses_natural_page_order(
    tmp_path: Path,
) -> None:
    archive = create_cbz(
        tmp_path / "issue.cbz",
        [
            ("10.jpg", b"ten"),
            ("2.jpg", b"two"),
            ("1.jpg", b"one"),
        ],
    )

    result = calculate_page_hashes(archive, chunk_size=2)

    assert [page.entry_name for page in result.pages] == [
        "1.jpg",
        "2.jpg",
        "10.jpg",
    ]
    assert result.page_count == 3
    assert result.image_bytes == 9


def test_content_signature_ignores_zip_metadata_and_compression(
    tmp_path: Path,
) -> None:
    pages = [("001.jpg", b"one"), ("002.jpg", b"two")]
    first = create_cbz(
        tmp_path / "first.cbz",
        pages,
        metadata="<ComicInfo><Title>First</Title></ComicInfo>",
        compression=zipfile.ZIP_STORED,
    )
    second = create_cbz(
        tmp_path / "second.cbz",
        pages,
        metadata="<ComicInfo><Title>Second</Title></ComicInfo>",
        compression=zipfile.ZIP_DEFLATED,
    )

    first_result = calculate_page_hashes(first)
    second_result = calculate_page_hashes(second)

    assert first.read_bytes() != second.read_bytes()
    assert (
        first_result.content_digest
        == second_result.content_digest
    )


def test_decompression_error_is_permanent_archive_corruption(
    tmp_path: Path,
) -> None:
    archive = create_cbz(
        tmp_path / "corrupt-stream.cbz",
        [("001.jpg", b"image bytes")],
    )

    with patch(
        "zipfile.ZipExtFile.read",
        side_effect=zlib.error(
            "Error -3 while decompressing data"
        ),
    ):
        with pytest.raises(PermanentJobError) as caught:
            calculate_page_hashes(archive)

    assert caught.value.category == "archive_corrupt"
    assert "Invalid or corrupt CBZ archive" in str(caught.value)


def test_page_hash_cli_detects_content_duplicates(
    tmp_path: Path,
    capsys,
) -> None:
    database = tmp_path / "pages.db"
    pages = [("1.jpg", b"one"), ("2.jpg", b"two")]
    first = create_cbz(
        tmp_path / "first.cbz",
        pages,
        metadata="<ComicInfo><Title>First</Title></ComicInfo>",
        compression=zipfile.ZIP_STORED,
    )
    second = create_cbz(
        tmp_path / "second.cbz",
        pages,
        metadata="<ComicInfo><Title>Second</Title></ComicInfo>",
        compression=zipfile.ZIP_DEFLATED,
    )
    third = create_cbz(
        tmp_path / "third.cbz",
        [("1.jpg", b"different")],
    )

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATIONS)
        seed_hashed_archive(connection, first)
        seed_hashed_archive(connection, second)
        seed_hashed_archive(connection, third)

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
    assert "Archives hashed:   3" in captured.out
    assert "Pages hashed:      5" in captured.out
    assert "Duplicate groups:  1" in captured.out

    with database_connection(database) as connection:
        page_count = connection.execute(
            "SELECT COUNT(*) FROM archive_pages"
        ).fetchone()[0]
        signature_count = connection.execute(
            "SELECT COUNT(*) FROM archive_content_signatures"
        ).fetchone()[0]
        duplicate_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT digest
                FROM archive_content_signatures
                GROUP BY digest, page_count
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]

    assert page_count == 5
    assert signature_count == 3
    assert duplicate_count == 1


def test_enqueue_missing_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "pages.db"
    archive = create_cbz(
        tmp_path / "issue.cbz",
        [("1.jpg", b"one")],
    )

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATIONS)
        seed_hashed_archive(connection, archive)

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
            WHERE job_type = 'hash_archive_pages'
            """
        ).fetchone()[0]

    assert job_count == 1


def test_zero_page_archives_are_not_duplicate_candidates(
    tmp_path: Path,
    capsys,
) -> None:
    database = tmp_path / "pages.db"
    first = create_cbz(tmp_path / "first.cbz", [])
    second = create_cbz(tmp_path / "second.cbz", [])

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATIONS)
        seed_hashed_archive(connection, first)
        seed_hashed_archive(connection, second)

    result = main([
        "--database",
        str(database),
        "--limit",
        "2",
        "--enqueue-missing",
    ])
    captured = capsys.readouterr()

    assert result == 0
    assert "Archives hashed:   2" in captured.out
    assert "Pages hashed:      0" in captured.out
    assert "Duplicate groups:  0" in captured.out


def test_corrupt_cbz_fails_page_hash_job_permanently(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pages.db"
    archive = tmp_path / "corrupt.cbz"
    archive.write_bytes(b"not a zip")

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATIONS)
        archive_id = seed_hashed_archive(connection, archive)

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
        job = connection.execute(
            """
            SELECT status, attempts, failure_category
            FROM jobs
            WHERE archive_id = ?
              AND job_type = 'hash_archive_pages'
            """,
            (archive_id,),
        ).fetchone()
        job_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM jobs
            WHERE archive_id = ?
              AND job_type = 'hash_archive_pages'
            """,
            (archive_id,),
        ).fetchone()[0]

    assert job["status"] == "failed"
    assert job["attempts"] == 1
    assert job["failure_category"] == "archive_corrupt"
    assert job_count == 1
