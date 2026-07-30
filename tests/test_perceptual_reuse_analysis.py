from __future__ import annotations

import hashlib
import json
from pathlib import Path

from comic_automation.archive.perceptual_reuse_analysis import (
    analyze_reuse_opportunity,
)
from comic_automation.archive.perceptual_reuse_cli import main
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations


MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


def _insert_archive(
    connection,
    *,
    archive_id: int,
    page_digests: list[str],
    page_states: list[str],
) -> None:
    location = connection.execute(
        """
        INSERT INTO archive_files (id, file_size, page_count)
        VALUES (?, 1000, ?)
        """,
        (archive_id, len(page_digests)),
    )
    assert location.lastrowid is not None
    stored_location = connection.execute(
        """
        INSERT INTO file_locations (
            archive_id,
            path,
            file_size,
            modified_time_ns
        )
        VALUES (?, ?, 1000, 1)
        """,
        (archive_id, f"/library/{archive_id}.cbz"),
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
        VALUES (?, ?, 'sha256-page-sequence', '1', ?, ?, 900, 1000, 1)
        """,
        (
            archive_id,
            int(stored_location.lastrowid),
            f"signature-{archive_id}",
            len(page_digests),
        ),
    )

    for page_index, (digest, state) in enumerate(
        zip(page_digests, page_states)
    ):
        dimensions = (
            (1200, 1800, "PNG")
            if state in {"complete", "missing_phash"}
            else (None, None, None)
        )
        page = connection.execute(
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
            VALUES (?, ?, ?, ?, 100, 80, ?, ?, ?, ?)
            """,
            (
                archive_id,
                int(stored_location.lastrowid),
                page_index,
                f"{page_index:03}.png",
                page_index,
                *dimensions,
            ),
        )
        page_id = int(page.lastrowid)
        connection.execute(
            """
            INSERT INTO page_hashes (
                page_id,
                algorithm,
                algorithm_version,
                digest,
                bytes_read
            )
            VALUES (?, 'sha256', '1', ?, 100)
            """,
            (page_id, digest),
        )

        if state in {"complete", "missing_phash"}:
            connection.execute(
                """
                INSERT INTO page_hashes (
                    page_id,
                    algorithm,
                    algorithm_version,
                    digest,
                    bytes_read
                )
                VALUES (?, 'dhash', '1', ?, 100)
                """,
                (page_id, f"dhash-{digest}"),
            )

        if state == "complete":
            connection.execute(
                """
                INSERT INTO page_hashes (
                    page_id,
                    algorithm,
                    algorithm_version,
                    digest,
                    bytes_read
                )
                VALUES (?, 'phash', '1', ?, 100)
                """,
                (page_id, f"phash-{digest}"),
            )


def _database_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def seed_reuse_scenario(database: Path) -> None:
    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATIONS)

        _insert_archive(
            connection,
            archive_id=1,
            page_digests=["source-a", "source-b", "source-c"],
            page_states=["complete", "complete", "complete"],
        )
        _insert_archive(
            connection,
            archive_id=2,
            page_digests=["source-a", "source-b"],
            page_states=["missing_all", "missing_all"],
        )
        _insert_archive(
            connection,
            archive_id=3,
            page_digests=["source-c", "unique-b"],
            page_states=["missing_phash", "missing_all"],
        )
        _insert_archive(
            connection,
            archive_id=4,
            page_digests=["unique-c"],
            page_states=["missing_all"],
        )


def test_analysis_distinguishes_full_and_partial_reuse(
    tmp_path: Path,
) -> None:
    database = tmp_path / "reuse.db"
    seed_reuse_scenario(database)
    before_digest = _database_digest(database)
    before_stat = database.stat()

    result = analyze_reuse_opportunity(database)

    after_stat = database.stat()
    assert result["read_only"] is True
    assert result["quick_check"] == "ok"
    assert result["metrics"] == {
        "eligible_archives": 3,
        "eligible_pages": 5,
        "incomplete_pages": 5,
        "reusable_pages": 3,
        "fully_satisfied_archives": 1,
        "partially_satisfied_archives": 1,
        "pages_still_requiring_decode": 2,
        "archives_still_requiring_processing": 2,
        "archives_without_reuse": 1,
        "pages_avoided_by_full_archive_reuse": 2,
        "pages_decoded_by_current_worker_after_reuse": 3,
        "pages_avoided_with_selective_worker": 3,
        "needed_sha256_digests": 5,
        "sha256_digests_with_complete_source": 3,
        "unambiguous_source_sha256_digests": 3,
        "ambiguous_source_sha256_digests": 0,
        "incomplete_pages_without_sha256": 0,
    }
    assert result["database_snapshot"]["metadata_unchanged"] is True
    assert before_stat.st_size == after_stat.st_size
    assert before_stat.st_mtime_ns == after_stat.st_mtime_ns
    assert _database_digest(database) == before_digest


def test_failed_jobs_are_excluded_from_eligibility(
    tmp_path: Path,
) -> None:
    database = tmp_path / "failed.db"
    seed_reuse_scenario(database)

    with database_connection(database) as connection:
        connection.execute(
            """
            INSERT INTO jobs (
                job_type,
                archive_id,
                status
            )
            VALUES ('hash_archive_pages_perceptual', 4, 'failed')
            """
        )

    result = analyze_reuse_opportunity(database)

    assert result["metrics"]["eligible_archives"] == 2
    assert result["metrics"]["archives_without_reuse"] == 0


def test_cli_writes_json_report_without_modifying_database(
    tmp_path: Path,
    capsys,
) -> None:
    database = tmp_path / "cli.db"
    output = tmp_path / "report.json"
    seed_reuse_scenario(database)
    before_digest = _database_digest(database)

    exit_code = main([
        "--database",
        str(database),
        "--json-output",
        str(output),
    ])
    captured = capsys.readouterr()
    report = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert json.loads(captured.out)["metrics"]["reusable_pages"] == 3
    assert report["metrics"]["fully_satisfied_archives"] == 1
    assert report["read_only"] is True
    assert report["query"]["parameters"]["sha256_algorithm"] == "sha256"
    assert "WITH eligible_archives AS" in report["query"]["sql"]
    assert report["json_output"] == str(output.resolve())
    assert _database_digest(database) == before_digest
