from __future__ import annotations

import os
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from comic_automation.archive.hashing import (
    ArchiveHashRepository,
    calculate_archive_hash,
)
from comic_automation.archive.inspection import inspect_archive
from comic_automation.archive.page_hashing import (
    ArchivePageHashRepository,
    calculate_page_hashes,
)
from comic_automation.archive.perceptual_hashing import (
    ArchivePerceptualHashRepository,
    HashArchivePagesPerceptualHandler,
    calculate_perceptual_hashes,
)
from comic_automation.archive.repository import (
    ArchiveInspectionRepository,
)
from comic_automation.archive.source_drift_recovery import (
    JOB_TYPE,
    RecoveryPreconditionError,
    analyze_source_drift,
    apply_source_drift_recovery,
    fingerprint_database,
    main,
)
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.jobs import JobQueue, JobWorker, WorkerOutcome


MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


def image_bytes(
    image_format: str,
    *,
    color: str,
    size: tuple[int, int],
) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color).save(
        output,
        format=image_format,
        quality=95,
    )
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


def seed_stale_perceptual_job(
    database: Path,
    archive: Path,
) -> tuple[int, int]:
    stat = archive.stat()

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATIONS)
        archive_cursor = connection.execute(
            "INSERT INTO archive_files (file_size) VALUES (?)",
            (stat.st_size,),
        )
        archive_id = int(archive_cursor.lastrowid)
        location_cursor = connection.execute(
            """
            INSERT INTO file_locations (
                archive_id, path, file_size, modified_time_ns
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                archive_id,
                str(archive.resolve()),
                stat.st_size,
                stat.st_mtime_ns,
            ),
        )
        location_id = int(location_cursor.lastrowid)

        ArchiveHashRepository(connection).save(
            archive_id=archive_id,
            location_id=location_id,
            result=calculate_archive_hash(archive),
        )
        inspection = inspect_archive(archive)
        ArchiveInspectionRepository(connection).save(
            archive_id=archive_id,
            location_id=location_id,
            result=inspection,
            file_size=stat.st_size,
            modified_time_ns=stat.st_mtime_ns,
        )
        ArchivePageHashRepository(connection).save(
            archive_id=archive_id,
            location_id=location_id,
            result=calculate_page_hashes(archive),
        )
        ArchivePerceptualHashRepository(connection).save(
            archive_id=archive_id,
            result=calculate_perceptual_hashes(archive),
        )

        job = JobQueue(connection).enqueue(
            JOB_TYPE,
            archive_id=archive_id,
        )
        connection.execute(
            """
            UPDATE jobs
            SET
                attempts = 1,
                error_message = ?,
                failure_category = 'unclassified_error'
            WHERE id = ?
            """,
            (
                "Stored page inventory does not match the current "
                f"archive for archive_id={archive_id}.",
                job.id,
            ),
        )

    return archive_id, job.id


def replace_with_jpeg_revision(path: Path) -> None:
    previous = path.stat()
    create_cbz(
        path,
        [
            (
                "001.jpg",
                image_bytes(
                    "JPEG",
                    color="red",
                    size=(80, 120),
                ),
            ),
            (
                "002.jpg",
                image_bytes(
                    "JPEG",
                    color="blue",
                    size=(90, 130),
                ),
            ),
        ],
    )
    changed_mtime = max(
        path.stat().st_mtime_ns,
        previous.st_mtime_ns + 1_000_000_000,
    )
    os.utime(
        path,
        ns=(path.stat().st_atime_ns, changed_mtime),
    )


def build_drift_case(
    tmp_path: Path,
) -> tuple[Path, Path, int, int]:
    database = tmp_path / "source-drift.db"
    archive = create_cbz(
        tmp_path / "issue.cbz",
        [
            (
                "001.webp",
                image_bytes(
                    "WEBP",
                    color="green",
                    size=(64, 96),
                ),
            )
        ],
    )
    archive_id, job_id = seed_stale_perceptual_job(
        database,
        archive,
    )
    replace_with_jpeg_revision(archive)
    return database, archive, archive_id, job_id


def test_read_only_analysis_reports_source_drift(
    tmp_path: Path,
) -> None:
    database, archive, archive_id, job_id = build_drift_case(tmp_path)
    before = fingerprint_database(database)

    output = analyze_source_drift(
        database=database,
        job_id=job_id,
    )

    assert fingerprint_database(database) == before
    assert output["mode"] == "read_only_analysis"
    assert output["job"]["archive_id"] == archive_id
    assert output["recoverable"] is True
    assert output["metadata_drift"] is True
    assert output["inventory_matches"] is False
    assert output["stored_page_count"] == 1
    assert output["live_page_count"] == 2
    assert output["live_file"]["size_bytes"] == archive.stat().st_size
    assert output["inventory_differences"][0]["stored"][
        "entry_name"
    ] == "001.webp"
    assert output["inventory_differences"][0]["live"][
        "entry_name"
    ] == "001.jpg"


def test_recovery_refreshes_exact_evidence_atomically(
    tmp_path: Path,
) -> None:
    database, archive, archive_id, job_id = build_drift_case(tmp_path)
    live = archive.stat()

    output = apply_source_drift_recovery(
        database=database,
        job_id=job_id,
        expected_file_size=live.st_size,
        expected_modified_time_ns=live.st_mtime_ns,
    )

    assert output["mode"] == "applied"
    assert output["ready_for_perceptual_retry"] is True
    assert output["page_count"] == 2
    assert output["exact_page_hash_count"] == 2
    assert output["perceptual_hash_count"] == 0
    assert output["job_status"] == "pending"
    assert output["job_attempts"] == 1
    assert output["job_error_message"] is None
    assert output["job_failure_category"] is None

    with database_connection(database) as connection:
        location = connection.execute(
            """
            SELECT file_size, modified_time_ns
            FROM file_locations
            WHERE archive_id = ? AND is_current = 1
            """,
            (archive_id,),
        ).fetchone()
        inspection = connection.execute(
            """
            SELECT page_count, inspected_file_size,
                   inspected_modified_time_ns
            FROM archive_inspections
            WHERE archive_id = ?
            """,
            (archive_id,),
        ).fetchone()
        signature = connection.execute(
            """
            SELECT page_count, source_file_size,
                   source_modified_time_ns
            FROM archive_content_signatures
            WHERE archive_id = ?
            """,
            (archive_id,),
        ).fetchone()
        pages = connection.execute(
            """
            SELECT entry_name, width, height
            FROM archive_pages
            WHERE archive_id = ?
            ORDER BY page_index
            """,
            (archive_id,),
        ).fetchall()
        event_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM file_events
            WHERE archive_id = ?
              AND event_type = 'source_drift_recovered'
            """,
            (archive_id,),
        ).fetchone()[0]

        assert tuple(location) == (
            live.st_size,
            live.st_mtime_ns,
        )
        assert tuple(inspection) == (
            2,
            live.st_size,
            live.st_mtime_ns,
        )
        assert tuple(signature) == (
            2,
            live.st_size,
            live.st_mtime_ns,
        )
        assert [
            (row["entry_name"], row["width"], row["height"])
            for row in pages
        ] == [
            ("001.jpg", None, None),
            ("002.jpg", None, None),
        ]
        assert event_count == 1

        worker = JobWorker(
            JobQueue(connection),
            {
                JOB_TYPE: HashArchivePagesPerceptualHandler(
                    connection
                )
            },
            worker_id="source-drift-test",
            poll_interval_seconds=0,
        )
        result = worker.run_once()

        assert result.job_id == job_id
        assert result.outcome == WorkerOutcome.SUCCEEDED

        hashes = connection.execute(
            """
            SELECT ph.algorithm, COUNT(*) AS count
            FROM archive_pages AS ap
            JOIN page_hashes AS ph ON ph.page_id = ap.id
            WHERE ap.archive_id = ?
            GROUP BY ph.algorithm
            ORDER BY ph.algorithm
            """,
            (archive_id,),
        ).fetchall()
        assert [
            (row["algorithm"], row["count"]) for row in hashes
        ] == [
            ("dhash", 2),
            ("phash", 2),
            ("sha256", 2),
        ]


def test_recovery_rejects_changed_apply_guard(
    tmp_path: Path,
) -> None:
    database, archive, _, job_id = build_drift_case(tmp_path)
    live = archive.stat()
    before = fingerprint_database(database)

    with pytest.raises(RecoveryPreconditionError):
        apply_source_drift_recovery(
            database=database,
            job_id=job_id,
            expected_file_size=live.st_size + 1,
            expected_modified_time_ns=live.st_mtime_ns,
        )

    assert fingerprint_database(database) == before


def test_recovery_rolls_back_all_database_writes_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, archive, archive_id, job_id = build_drift_case(tmp_path)
    live = archive.stat()

    with database_connection(database) as connection:
        before = {
            "location": tuple(
                connection.execute(
                    """
                    SELECT file_size, modified_time_ns
                    FROM file_locations
                    WHERE archive_id = ? AND is_current = 1
                    """,
                    (archive_id,),
                ).fetchone()
            ),
            "archive_hash": tuple(
                connection.execute(
                    """
                    SELECT digest, file_size, modified_time_ns
                    FROM archive_hashes
                    WHERE archive_id = ?
                    """,
                    (archive_id,),
                ).fetchone()
            ),
            "pages": [
                tuple(row)
                for row in connection.execute(
                    """
                    SELECT page_index, entry_name
                    FROM archive_pages
                    WHERE archive_id = ?
                    ORDER BY page_index
                    """,
                    (archive_id,),
                )
            ],
            "job": tuple(
                connection.execute(
                    """
                    SELECT status, attempts, error_message,
                           failure_category
                    FROM jobs
                    WHERE id = ?
                    """,
                    (job_id,),
                ).fetchone()
            ),
        }

    original_save = ArchivePageHashRepository.save

    def fail_after_page_save(self, **kwargs):
        original_save(self, **kwargs)
        raise RuntimeError("simulated recovery interruption")

    monkeypatch.setattr(
        ArchivePageHashRepository,
        "save",
        fail_after_page_save,
    )

    with pytest.raises(
        RuntimeError,
        match="simulated recovery interruption",
    ):
        apply_source_drift_recovery(
            database=database,
            job_id=job_id,
            expected_file_size=live.st_size,
            expected_modified_time_ns=live.st_mtime_ns,
        )

    with database_connection(database) as connection:
        after = {
            "location": tuple(
                connection.execute(
                    """
                    SELECT file_size, modified_time_ns
                    FROM file_locations
                    WHERE archive_id = ? AND is_current = 1
                    """,
                    (archive_id,),
                ).fetchone()
            ),
            "archive_hash": tuple(
                connection.execute(
                    """
                    SELECT digest, file_size, modified_time_ns
                    FROM archive_hashes
                    WHERE archive_id = ?
                    """,
                    (archive_id,),
                ).fetchone()
            ),
            "pages": [
                tuple(row)
                for row in connection.execute(
                    """
                    SELECT page_index, entry_name
                    FROM archive_pages
                    WHERE archive_id = ?
                    ORDER BY page_index
                    """,
                    (archive_id,),
                )
            ],
            "job": tuple(
                connection.execute(
                    """
                    SELECT status, attempts, error_message,
                           failure_category
                    FROM jobs
                    WHERE id = ?
                    """,
                    (job_id,),
                ).fetchone()
            ),
        }
        event_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM file_events
            WHERE archive_id = ?
              AND event_type = 'source_drift_recovered'
            """,
            (archive_id,),
        ).fetchone()[0]

    assert after == before
    assert event_count == 0


def test_analysis_rejects_unrelated_pending_job(
    tmp_path: Path,
) -> None:
    database, _, _, job_id = build_drift_case(tmp_path)

    with database_connection(database) as connection:
        connection.execute(
            """
            UPDATE jobs
            SET error_message = 'Some other retryable failure.'
            WHERE id = ?
            """,
            (job_id,),
        )

    output = analyze_source_drift(
        database=database,
        job_id=job_id,
    )

    assert output["recoverable"] is False
    assert output["checks"]["source_drift_error_present"] is False


def test_cli_requires_apply_guards(
    tmp_path: Path,
    capsys,
) -> None:
    database, _, _, job_id = build_drift_case(tmp_path)

    exit_code = main(
        [
            "--database",
            str(database),
            "--job-id",
            str(job_id),
            "--apply",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "--apply requires" in captured.err
