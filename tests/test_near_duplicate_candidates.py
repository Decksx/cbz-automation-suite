from __future__ import annotations

from pathlib import Path

import pytest

from comic_automation.archive.near_duplicate import (
    ArchiveFingerprint,
    NearDuplicateRepository,
    PageFingerprint,
    compare_archive_fingerprints,
    hamming_distance,
)
from comic_automation.archive.near_duplicate_cli import main
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations


MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


def digest(value: int) -> str:
    return f"{value & ((1 << 64) - 1):016x}"


def fingerprint(
    archive_id: int,
    *,
    content_signature: str,
    page_count: int = 10,
    bit_offset: int = 0,
    unrelated_after_cover: bool = False,
) -> ArchiveFingerprint:
    pages = []

    for page_index in range(page_count):
        value = (page_index + 1) * 0x0101010101010101
        if bit_offset:
            value ^= 1 << ((page_index + bit_offset) % 64)
        if unrelated_after_cover and page_index > 0:
            value ^= 0xFFFFFFFFFFFFFFFF
        pages.append(
            PageFingerprint(
                page_index=page_index,
                dhash=digest(value),
                phash=digest(value ^ 0x00FF00FF00FF00FF),
                width=1200 + archive_id * 10,
                height=1800 + archive_id * 15,
            )
        )

    return ArchiveFingerprint(
        archive_id=archive_id,
        content_signature=content_signature,
        pages=tuple(pages),
    )


def seed_fingerprint(
    connection,
    archive: ArchiveFingerprint,
) -> None:
    connection.execute(
        """
        INSERT INTO archive_files (id, file_size, page_count)
        VALUES (?, ?, ?)
        """,
        (archive.archive_id, 1000, archive.page_count),
    )
    location = connection.execute(
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
            archive.archive_id,
            f"/library/{archive.archive_id}.cbz",
            1000,
            1,
        ),
    )
    connection.execute(
        """
        INSERT INTO archive_content_signatures (
            archive_id,
            location_id,
            algorithm,
            algorithm_version,
            digest,
            page_count,
            image_bytes,
            source_file_size,
            source_modified_time_ns
        )
        VALUES (?, ?, 'sha256-page-sequence', '1', ?, ?, ?, ?, ?)
        """,
        (
            archive.archive_id,
            int(location.lastrowid),
            archive.content_signature,
            archive.page_count,
            900,
            1000,
            1,
        ),
    )

    for page in archive.pages:
        stored_page = connection.execute(
            """
            INSERT INTO archive_pages (
                archive_id,
                location_id,
                page_index,
                entry_name,
                entry_size,
                compressed_size,
                crc32,
                width,
                height,
                image_format
            )
            VALUES (?, ?, ?, ?, 90, 80, 1, ?, ?, 'PNG')
            """,
            (
                archive.archive_id,
                int(location.lastrowid),
                page.page_index,
                f"{page.page_index:03}.png",
                page.width,
                page.height,
            ),
        )

        for algorithm, value in (
            ("dhash", page.dhash),
            ("phash", page.phash),
        ):
            connection.execute(
                """
                INSERT INTO page_hashes (
                    page_id,
                    algorithm,
                    algorithm_version,
                    digest,
                    bytes_read
                )
                VALUES (?, ?, '1', ?, 90)
                """,
                (int(stored_page.lastrowid), algorithm, value),
            )


def test_hamming_distance_validates_and_counts_bits() -> None:
    assert hamming_distance("0000000000000000", "0000000000000003") == 2

    with pytest.raises(ValueError, match="equal lengths"):
        hamming_distance("00", "0000")

    with pytest.raises(ValueError, match="hexadecimal"):
        hamming_distance("zz", "00")


def test_ordered_comparison_accepts_resolution_variant() -> None:
    first = fingerprint(1, content_signature="exact-a")
    second = fingerprint(
        2,
        content_signature="exact-b",
        bit_offset=3,
    )

    comparison = compare_archive_fingerprints(first, second)

    assert comparison is not None
    assert comparison.page_match_ratio == 1.0
    assert comparison.average_dhash_distance == 1.0
    assert comparison.average_phash_distance == 1.0
    assert comparison.dimension_match_ratio == 1.0
    assert comparison.median_pixel_area_b > (
        comparison.median_pixel_area_a
    )


def test_ordered_comparison_rejects_cover_only_match() -> None:
    first = fingerprint(1, content_signature="exact-a")
    second = fingerprint(
        2,
        content_signature="exact-b",
        unrelated_after_cover=True,
    )

    assert compare_archive_fingerprints(first, second) is None


def test_repository_creates_review_only_candidate(
    tmp_path: Path,
) -> None:
    database = tmp_path / "candidates.db"
    first = fingerprint(1, content_signature="exact-a")
    second = fingerprint(
        2,
        content_signature="exact-b",
        bit_offset=2,
    )
    cover_only = fingerprint(
        3,
        content_signature="exact-c",
        unrelated_after_cover=True,
    )

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATIONS)
        for archive in (first, second, cover_only):
            seed_fingerprint(connection, archive)
        connection.execute(
            """
            INSERT INTO file_locations (
                archive_id,
                path,
                file_size,
                modified_time_ns
            )
            VALUES (1, '/library/copy-of-1.cbz', 1000, 1)
            """
        )

        repository = NearDuplicateRepository(connection)
        candidates = repository.generate_candidates(limit=10)
        stored = connection.execute(
            """
            SELECT archive_a_id, archive_b_id, review_status
            FROM near_duplicate_candidates
            ORDER BY archive_a_id, archive_b_id
            """
        ).fetchall()

    assert [(item.archive_a_id, item.archive_b_id) for item in candidates] == [
        (1, 2)
    ]
    assert [
        (
            row["archive_a_id"],
            row["archive_b_id"],
            row["review_status"],
        )
        for row in stored
    ] == [(1, 2, "pending_review")]


def test_generator_preserves_reviewed_decision(tmp_path: Path) -> None:
    database = tmp_path / "reviewed.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATIONS)
        seed_fingerprint(
            connection,
            fingerprint(1, content_signature="exact-a"),
        )
        seed_fingerprint(
            connection,
            fingerprint(
                2,
                content_signature="exact-b",
                bit_offset=1,
            ),
        )
        repository = NearDuplicateRepository(connection)
        repository.generate_candidates(limit=10)
        connection.execute(
            """
            UPDATE near_duplicate_candidates
            SET review_status = 'confirmed_duplicate',
                similarity_score = 0.5
            """
        )
        repository.generate_candidates(limit=10)
        row = connection.execute(
            """
            SELECT review_status, similarity_score
            FROM near_duplicate_candidates
            """
        ).fetchone()

    assert row["review_status"] == "confirmed_duplicate"
    assert row["similarity_score"] == 0.5


def test_cli_reports_generation_and_safety(
    tmp_path: Path,
    capsys,
) -> None:
    database = tmp_path / "candidate-cli.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATIONS)
        seed_fingerprint(
            connection,
            fingerprint(1, content_signature="exact-a"),
        )
        seed_fingerprint(
            connection,
            fingerprint(
                2,
                content_signature="exact-b",
                bit_offset=4,
            ),
        )

    result = main([
        "--database",
        str(database),
        "--limit",
        "10",
    ])
    captured = capsys.readouterr()

    assert result == 0
    assert "Generated candidates: 1" in captured.out
    assert "Pending review:       1" in captured.out
    assert "No archive files were modified." in captured.out
