from __future__ import annotations

import json
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


def test_profiled_hashing_preserves_results_and_records_phases(
    tmp_path: Path,
) -> None:
    archive = create_cbz(
        tmp_path / "profiled.cbz",
        [
            ("001.png", image_bytes(size=(80, 120))),
            ("002.jpg", image_bytes(size=(110, 75))),
        ],
    )

    unprofiled = calculate_perceptual_hashes(archive)
    profiled = calculate_perceptual_hashes(
        archive,
        profile=True,
    )

    assert unprofiled.phase_timings is None
    assert profiled.pages == unprofiled.pages
    assert (
        profiled.source_file_size
        == unprofiled.source_file_size
    )
    assert (
        profiled.source_modified_time_ns
        == unprofiled.source_modified_time_ns
    )
    assert profiled.phase_timings is not None

    phase_values = (
        profiled.phase_timings
        .zip_open_and_inventory_seconds,
        profiled.phase_timings.zip_entry_read_seconds,
        profiled.phase_timings
        .image_open_and_decode_seconds,
        profiled.phase_timings.dhash_seconds,
        profiled.phase_timings.phash_seconds,
    )
    assert all(value >= 0 for value in phase_values)
    assert sum(phase_values) > 0


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
    assert "Profiled pages:" not in captured.out


def test_perceptual_hash_cli_profiles_internal_phases(
    tmp_path: Path,
    capsys,
) -> None:
    database = tmp_path / "perceptual-profile.db"
    report = tmp_path / "profile.json"
    archive = create_cbz(
        tmp_path / "profiled-issue.cbz",
        [
            ("001.png", image_bytes(size=(80, 120))),
            ("002.jpg", image_bytes(size=(110, 75))),
        ],
    )

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATIONS)
        seed_page_inventory(connection, archive)

    exit_code = main([
        "--database",
        str(database),
        "--limit",
        "1",
        "--progress-every",
        "1",
        "--enqueue-missing",
        "--profile",
        "--json-output",
        str(report),
    ])
    captured = capsys.readouterr()
    output = json.loads(report.read_text(encoding="utf-8"))
    timing = output["phase_timing"]

    assert exit_code == 0
    assert "Profiled pages:     2" in captured.out
    assert timing["enabled"] is True
    assert timing["profiled_archives"] == 1
    assert timing["profiled_pages"] == 2
    assert timing["profiled_bytes"] > 0
    assert timing["unprofiled_jobs"] == 0
    assert timing["milliseconds_per_page"] > 0
    assert timing["pages_per_timed_second"] > 0
    assert timing["timed_phase_seconds"] == pytest.approx(
        sum(timing["phase_seconds"].values()),
        abs=0.00001,
    )
    assert sum(timing["phase_percentages"].values()) == (
        pytest.approx(100.0, abs=0.01)
    )
    assert timing["timed_phase_seconds"] <= (
        timing["batch_elapsed_seconds"]
    )
    assert set(timing["phase_seconds"]) == {
        "zip_open_and_inventory_seconds",
        "zip_entry_read_seconds",
        "image_open_and_decode_seconds",
        "dhash_seconds",
        "phash_seconds",
        "database_lookup_seconds",
        "database_save_seconds",
    }


def test_profiled_report_only_handles_an_empty_batch(
    tmp_path: Path,
    capsys,
) -> None:
    database = tmp_path / "empty-profile.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATIONS)

    exit_code = main([
        "--database",
        str(database),
        "--limit",
        "1",
        "--progress-every",
        "1",
        "--report-only",
        "--profile",
    ])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Profiled pages:     0" in captured.out
    assert "Timed ms/page:" not in captured.out
