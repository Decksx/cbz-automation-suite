"""Tests for comic_automation.library: filesystem discovery of comic
archives (discover_archives) and the higher-level scan_library() that
inventories a library into the database, detects new/changed/missing/
restored files, and queues inspect_archive jobs accordingly.

scan_library() is designed to be read-only with respect to the archives
themselves (it only stats files, never opens/modifies them) and idempotent
across repeated runs -- unchanged files should not re-queue inspection
jobs, and an already-active inspection job should not be duplicated just
because the file changed again before the first job finished.
"""

from __future__ import annotations

import os
from pathlib import Path

from comic_automation.database.connection import (
    database_connection,
)
from comic_automation.database.migrations import (
    apply_migrations,
)
from comic_automation.library import (
    discover_archives,
    scan_library,
)


MIGRATION_DIRECTORY = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


def create_archive(
    path: Path,
    content: bytes = b"test archive",
) -> Path:
    """Write a file at `path` (parents created as needed). Content doesn't
    need to be a real archive -- discovery/scanning only look at file
    metadata, never archive contents.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_discovery_enumerates_supported_extensions(
    tmp_path: Path,
) -> None:
    """discover_archives should find files with supported comic archive
    extensions (.cbz, .CBR case-insensitively, .cb7) recursively, and skip
    unsupported files (e.g. notes.txt) sitting alongside them.
    """
    library = tmp_path / "library"

    first = create_archive(
        library / "Series A" / "Issue 01.cbz"
    )
    second = create_archive(
        library / "Series B" / "Issue 02.CBR"
    )
    third = create_archive(
        library / "Series C" / "Issue 03.cb7"
    )
    create_archive(
        library / "Series D" / "notes.txt"
    )

    discovered = list(discover_archives(library))
    discovered_paths = {
        item.path for item in discovered
    }

    assert discovered_paths == {
        first.resolve(),
        second.resolve(),
        third.resolve(),
    }


def test_discovery_reads_only_file_metadata(
    tmp_path: Path,
) -> None:
    """Each discovered item should report resolved path, extension, file
    size, and mtime -- all cheap filesystem stat() facts, with no archive
    content actually read.
    """
    library = tmp_path / "library"
    archive = create_archive(
        library / "Issue.cbz",
        b"123456789",
    )

    result = list(discover_archives(library))

    assert len(result) == 1
    assert result[0].path == archive.resolve()
    assert result[0].extension == ".cbz"
    assert result[0].file_size == 9
    assert result[0].modified_time_ns == (
        archive.stat().st_mtime_ns
    )


def test_first_scan_records_new_files_and_jobs(
    tmp_path: Path,
) -> None:
    """Scanning a library for the first time should record every archive
    as a new, current file_locations row and queue one pending
    inspect_archive job per file.
    """
    library = tmp_path / "library"
    create_archive(library / "A.cbz")
    create_archive(library / "nested" / "B.cbr")

    database = tmp_path / "inventory.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)

        summary = scan_library(
            connection,
            library,
            batch_size=1,
        )

        locations = connection.execute(
            """
            SELECT path, is_current
            FROM file_locations
            ORDER BY path
            """
        ).fetchall()

        jobs = connection.execute(
            """
            SELECT job_type, status
            FROM jobs
            ORDER BY id
            """
        ).fetchall()

    assert summary.scanned == 2
    assert summary.new == 2
    assert summary.changed == 0
    assert summary.unchanged == 0
    assert summary.missing == 0
    assert summary.jobs_queued == 2

    assert len(locations) == 2
    assert all(row["is_current"] == 1 for row in locations)

    assert len(jobs) == 2
    assert all(
        row["job_type"] == "inspect_archive"
        for row in jobs
    )
    assert all(row["status"] == "pending" for row in jobs)


def test_second_scan_marks_files_unchanged(
    tmp_path: Path,
) -> None:
    """Re-scanning without any filesystem changes should report everything
    as unchanged and queue zero additional jobs -- the original job from
    the first scan should be the only one in the table.
    """
    library = tmp_path / "library"
    create_archive(library / "Issue.cbz")
    database = tmp_path / "inventory.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)

        first = scan_library(connection, library)
        second = scan_library(connection, library)

        job_count = connection.execute(
            "SELECT COUNT(*) FROM jobs"
        ).fetchone()[0]

    assert first.new == 1
    assert second.scanned == 1
    assert second.new == 0
    assert second.changed == 0
    assert second.unchanged == 1
    assert second.jobs_queued == 0
    assert job_count == 1


def test_changed_file_queues_inspection(
    tmp_path: Path,
) -> None:
    """When a file's size/mtime changes since the last scan, the scan should
    detect it as changed, clear its stale sha256/page_count (since those no
    longer describe the current content), and queue a fresh inspect_archive
    job.
    """
    library = tmp_path / "library"
    archive = create_archive(
        library / "Issue.cbz",
        b"first",
    )
    database = tmp_path / "inventory.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        scan_library(connection, library)

        # Complete the existing inspection job so the changed file can
        # create a new active inspection request.
        connection.execute(
            """
            UPDATE jobs
            SET status = 'completed'
            """
        )

        archive.write_bytes(b"second version")
        os.utime(archive, None)

        summary = scan_library(connection, library)

        row = connection.execute(
            """
            SELECT
                fl.file_size,
                fl.modified_time_ns,
                af.sha256,
                af.page_count
            FROM file_locations AS fl
            JOIN archive_files AS af
              ON af.id = fl.archive_id
            """
        ).fetchone()

        job_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM jobs
            WHERE job_type = 'inspect_archive'
            """
        ).fetchone()[0]

    assert summary.changed == 1
    assert summary.jobs_queued == 1
    assert row["file_size"] == len(b"second version")
    assert row["sha256"] is None
    assert row["page_count"] is None
    assert job_count == 2


def test_active_inspection_job_is_not_duplicated(
    tmp_path: Path,
) -> None:
    """If a file changes again while its previous inspect_archive job is
    still pending (not yet completed), the scan should detect the change
    but must not queue a second job for the same file.
    """
    library = tmp_path / "library"
    archive = create_archive(
        library / "Issue.cbz",
        b"first",
    )
    database = tmp_path / "inventory.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        scan_library(connection, library)

        archive.write_bytes(b"changed")
        os.utime(archive, None)

        summary = scan_library(connection, library)

        job_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM jobs
            WHERE job_type = 'inspect_archive'
            """
        ).fetchone()[0]

    assert summary.changed == 1
    assert summary.jobs_queued == 0
    assert job_count == 1


def test_missing_file_is_marked_not_current(
    tmp_path: Path,
) -> None:
    """When a previously-scanned file disappears from disk, the next scan
    should flip its file_locations row to is_current=0 and record a
    "missing" file_events entry, rather than deleting the row outright.
    """
    library = tmp_path / "library"
    archive = create_archive(
        library / "Issue.cbz"
    )
    database = tmp_path / "inventory.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        scan_library(connection, library)

        archive.unlink()
        summary = scan_library(connection, library)

        location = connection.execute(
            """
            SELECT is_current
            FROM file_locations
            """
        ).fetchone()

        event = connection.execute(
            """
            SELECT event_type
            FROM file_events
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    assert summary.scanned == 0
    assert summary.missing == 1
    assert location["is_current"] == 0
    assert event["event_type"] == "missing"


def test_restored_file_is_treated_as_changed(
    tmp_path: Path,
) -> None:
    """A file that went missing and then reappears (e.g. restored from
    backup) should be treated as changed: is_current flips back to 1, a
    "restored" file_events entry is recorded, and a fresh inspection job is
    queued.
    """
    library = tmp_path / "library"
    archive = create_archive(
        library / "Issue.cbz"
    )
    database = tmp_path / "inventory.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        scan_library(connection, library)

        connection.execute(
            "UPDATE jobs SET status = 'completed'"
        )

        archive.unlink()
        scan_library(connection, library)

        create_archive(archive, b"restored")
        summary = scan_library(connection, library)

        location = connection.execute(
            """
            SELECT is_current
            FROM file_locations
            """
        ).fetchone()

        event = connection.execute(
            """
            SELECT event_type
            FROM file_events
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    assert summary.changed == 1
    assert summary.jobs_queued == 1
    assert location["is_current"] == 1
    assert event["event_type"] == "restored"


def test_scan_does_not_modify_archive_contents(
    tmp_path: Path,
) -> None:
    """Regression guard: scanning a library must never touch the archive
    files themselves -- content, size, and mtime must be byte-for-byte and
    timestamp-for-timestamp identical before and after a scan, even for a
    file whose content isn't a real archive at all.
    """
    library = tmp_path / "library"
    original = b"not a real zip but discovery must not care"
    archive = create_archive(
        library / "Issue.cbz",
        original,
    )
    database = tmp_path / "inventory.db"

    before_stat = archive.stat()

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        scan_library(connection, library)

    after_stat = archive.stat()

    assert archive.read_bytes() == original
    assert after_stat.st_size == before_stat.st_size
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns


def test_scan_records_completed_source_batch(
    tmp_path: Path,
) -> None:
    """Each scan_library() run should record a source_batches row marked
    completed, with a details_json summary (e.g. counts like "scanned": 1)
    for later auditing of what a given scan run actually did.
    """
    library = tmp_path / "library"
    create_archive(library / "Issue.cbz")
    database = tmp_path / "inventory.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        summary = scan_library(connection, library)

        batch = connection.execute(
            """
            SELECT status, details_json
            FROM source_batches
            WHERE id = ?
            """,
            (summary.batch_id,),
        ).fetchone()

    assert batch["status"] == "completed"
    assert '"scanned": 1' in batch["details_json"]
