from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from comic_automation.archive.page_hashing import (
    ArchivePageHashRepository,
    calculate_page_hashes,
)
from comic_automation.archive.perceptual_hash_cli import main
from comic_automation.archive.perceptual_hashing import (
    ArchivePerceptualHashRepository,
    HashArchivePagesPerceptualHandler,
    calculate_perceptual_hashes,
    difference_hash,
    perceptual_hash,
)
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.jobs import (
    JobQueue,
    JobWorker,
    PermanentJobError,
    WorkerOutcome,
)


MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


def image_bytes(
    *,
    image_format: str = "PNG",
    size: tuple[int, int] = (96, 128),
) -> bytes:
    image = Image.new("RGB", size, "white")
    drawing = ImageDraw.Draw(image)
    drawing.rectangle((10, 15, 45, 100), fill="black")
    drawing.ellipse((50, 30, 85, 70), fill="gray")
    output = BytesIO()
    image.save(output, format=image_format, quality=95)
    return output.getvalue()


def create_cbz(
    path: Path,
    pages: list[tuple[str, bytes]],
) -> Path:
    with zipfile.ZipFile(
        path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for name, payload in pages:
            archive.writestr(name, payload)
    return path


def seed_page_inventory(connection, path: Path) -> int:
    stat = path.stat()
    archive = connection.execute(
        "INSERT INTO archive_files (file_size) VALUES (?)",
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
    exact = calculate_page_hashes(path)
    ArchivePageHashRepository(connection).save(
        archive_id=archive_id,
        location_id=int(location.lastrowid),
        result=exact,
    )
    return archive_id


def hamming_distance(first: str, second: str) -> int:
    return (int(first, 16) ^ int(second, 16)).bit_count()


def test_hashes_are_stable_across_image_encodings() -> None:
    with Image.open(BytesIO(image_bytes(image_format="PNG"))) as png:
        png.load()
        png_dhash = difference_hash(png)
        png_phash = perceptual_hash(png)

    with Image.open(BytesIO(image_bytes(image_format="JPEG"))) as jpeg:
        jpeg.load()
        jpeg_dhash = difference_hash(jpeg)
        jpeg_phash = perceptual_hash(jpeg)

    assert len(png_dhash) == 16
    assert len(png_phash) == 16
    assert hamming_distance(png_dhash, jpeg_dhash) <= 4
    assert hamming_distance(png_phash, jpeg_phash) <= 4


def test_calculate_perceptual_hashes_uses_natural_order(
    tmp_path: Path,
) -> None:
    archive = create_cbz(
        tmp_path / "issue.cbz",
        [
            ("10.jpg", image_bytes(size=(100, 140))),
            ("2.jpg", image_bytes(size=(90, 130))),
            ("1.jpg", image_bytes(size=(80, 120))),
        ],
    )

    result = calculate_perceptual_hashes(archive)

    assert [page.entry_name for page in result.pages] == [
        "1.jpg",
        "2.jpg",
        "10.jpg",
    ]
    assert [(page.width, page.height) for page in result.pages] == [
        (80, 120),
        (90, 130),
        (100, 140),
    ]
    assert all(len(page.dhash) == 16 for page in result.pages)
    assert all(len(page.phash) == 16 for page in result.pages)


def test_invalid_image_page_is_a_permanent_failure(
    tmp_path: Path,
) -> None:
    archive = create_cbz(
        tmp_path / "invalid-image.cbz",
        [("001.jpg", b"not an image")],
    )

    with pytest.raises(PermanentJobError) as caught:
        calculate_perceptual_hashes(archive)

    assert caught.value.category == "page_image_corrupt"
    assert "001.jpg" in str(caught.value)


def test_perceptual_hash_job_persists_both_algorithms(
    tmp_path: Path,
) -> None:
    database = tmp_path / "perceptual.db"
    archive = create_cbz(
        tmp_path / "issue.cbz",
        [
            ("001.png", image_bytes(size=(80, 120))),
            ("002.png", image_bytes(size=(90, 130))),
        ],
    )

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATIONS)
        archive_id = seed_page_inventory(connection, archive)
        repository = ArchivePerceptualHashRepository(connection)

        assert repository.enqueue_missing(limit=10) == 1
        assert repository.enqueue_missing(limit=10) == 0

        worker = JobWorker(
            JobQueue(connection),
            {
                "hash_archive_pages_perceptual":
                    HashArchivePagesPerceptualHandler(connection)
            },
            worker_id="test-perceptual-worker",
            poll_interval_seconds=0,
        )
        result = worker.run_once()

        hashes = connection.execute(
            """
            SELECT algorithm, COUNT(*) AS count
            FROM page_hashes AS ph
            JOIN archive_pages AS ap ON ap.id = ph.page_id
            WHERE ap.archive_id = ?
            GROUP BY algorithm
            ORDER BY algorithm
            """,
            (archive_id,),
        ).fetchall()
        dimensions = connection.execute(
            """
            SELECT width, height, image_format
            FROM archive_pages
            WHERE archive_id = ?
            ORDER BY page_index
            """,
            (archive_id,),
        ).fetchall()

        assert result.outcome == WorkerOutcome.SUCCEEDED
        assert [(row["algorithm"], row["count"]) for row in hashes] == [
            ("dhash", 2),
            ("phash", 2),
            ("sha256", 2),
        ]
        assert [
            (row["width"], row["height"], row["image_format"])
            for row in dimensions
        ] == [
            (80, 120, "PNG"),
            (90, 130, "PNG"),
        ]
        assert repository.enqueue_missing(limit=10) == 0

        connection.execute(
            """
            UPDATE archive_pages
            SET width = NULL, height = NULL
            WHERE archive_id = ?
            """,
            (archive_id,),
        )
        assert repository.enqueue_missing(limit=10) == 1


def test_perceptual_hash_cli_processes_a_bounded_batch(
    tmp_path: Path,
    capsys,
) -> None:
    database = tmp_path / "perceptual-cli.db"
    archive = create_cbz(
        tmp_path / "issue.cbz",
        [("001.png", image_bytes())],
    )

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATIONS)
        seed_page_inventory(connection, archive)

    result = main([
        "--database",
        str(database),
        "--limit",
        "1",
        "--progress-every",
        "1",
        "--enqueue-missing",
    ])
    captured = capsys.readouterr()

    assert result == 0
    assert "Succeeded:          1" in captured.out
    assert "Archives hashed:    1" in captured.out
    assert "Perceptual hashes: 2" in captured.out
