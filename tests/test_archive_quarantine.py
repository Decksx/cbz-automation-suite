from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import pytest

from comic_automation.archive.quarantine import (
    QuarantineRepository,
    UnsupportedQuarantineCategoryError,
    execute_quarantine,
    propose_quarantine_filename,
    resolve_destination_path,
)
from comic_automation.archive.quarantine_cli import main, run_quarantine
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations


MIGRATION_DIRECTORY = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


# --- propose_quarantine_filename -------------------------------------


def test_filename_unchanged_when_series_already_present() -> None:
    assert (
        propose_quarantine_filename(
            "Superior Day", "Superior Day Chapter 12.cbz"
        )
        == "Superior Day Chapter 12.cbz"
    )


def test_filename_prefixed_when_series_missing() -> None:
    assert (
        propose_quarantine_filename(
            "The Young Wife", "Manhwa18 2 Chapter 39.cbz"
        )
        == "The Young Wife - Manhwa18 2 Chapter 39.cbz"
    )


def test_filename_case_insensitive_match() -> None:
    assert (
        propose_quarantine_filename(
            "blood lad", "Blood Lad v6tmp.cbz"
        )
        == "Blood Lad v6tmp.cbz"
    )


def test_filename_no_extension() -> None:
    assert (
        propose_quarantine_filename("Thanatos", "Thanatos")
        == "Thanatos"
    )


# --- resolve_destination_path -----------------------------------------


def test_resolve_destination_path_no_collision(tmp_path: Path) -> None:
    existing: set[str] = set()

    destination = resolve_destination_path(
        tmp_path, "Series Chapter 1.cbz", existing_names=existing
    )

    assert destination == tmp_path / "Series Chapter 1.cbz"
    assert "series chapter 1.cbz" in existing


def test_resolve_destination_path_disambiguates_collision(
    tmp_path: Path,
) -> None:
    existing = {"series chapter 1.cbz"}

    destination = resolve_destination_path(
        tmp_path, "Series Chapter 1.cbz", existing_names=existing
    )

    assert destination == tmp_path / "Series Chapter 1 (2).cbz"


# --- helpers -----------------------------------------------------------


def _seed_failed_job(
    connection: sqlite3.Connection,
    *,
    path: Path,
    failure_category: str,
    create_file: bool = True,
) -> tuple[int, int]:
    if create_file:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not a real archive")

    archive_cursor = connection.execute(
        "INSERT INTO archive_files (file_size) VALUES (0)"
    )
    archive_id = int(archive_cursor.lastrowid)

    connection.execute(
        """
        INSERT INTO file_locations (
            archive_id, path, is_current, file_size, modified_time_ns
        )
        VALUES (?, ?, 1, 0, 0)
        """,
        (archive_id, str(path)),
    )

    job_cursor = connection.execute(
        """
        INSERT INTO jobs (
            job_type, status, archive_id, attempts, max_attempts,
            failure_category, error_message
        )
        VALUES (
            'inspect_archive', 'failed', ?, 3, 3, ?, ?
        )
        """,
        (
            archive_id,
            failure_category,
            f"Invalid or corrupt CBZ archive: {path}",
        ),
    )
    job_id = int(job_cursor.lastrowid)

    return archive_id, job_id


# --- QuarantineRepository.find_candidates ------------------------------


def test_find_candidates_filters_by_category(tmp_path: Path) -> None:
    database = tmp_path / "inspection.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)

        _seed_failed_job(
            connection,
            path=tmp_path / "library" / "Series A" / "issue1.cbz",
            failure_category="corrupt_archive",
        )
        _seed_failed_job(
            connection,
            path=tmp_path / "library" / "Series B" / "issue2.cbz",
            failure_category="filesystem_not_found",
            create_file=False,
        )

        repository = QuarantineRepository(connection)
        candidates = repository.find_candidates()

    assert len(candidates) == 1
    assert candidates[0].failure_category == "corrupt_archive"
    assert candidates[0].series_name == "Series A"


def test_find_candidates_rejects_no_source_file_category(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inspection.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        repository = QuarantineRepository(connection)

        with pytest.raises(UnsupportedQuarantineCategoryError):
            repository.find_candidates(
                categories=frozenset({"filesystem_not_found"})
            )


def test_find_candidates_excludes_series(tmp_path: Path) -> None:
    database = tmp_path / "inspection.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)

        _seed_failed_job(
            connection,
            path=tmp_path / "library" / "Feng Shen Ji III" / "a.cbz",
            failure_category="corrupt_archive",
        )
        _seed_failed_job(
            connection,
            path=tmp_path / "library" / "Superior Day" / "b.cbz",
            failure_category="corrupt_archive",
        )

        repository = QuarantineRepository(connection)
        candidates = repository.find_candidates(
            exclude_series=frozenset({"feng shen ji iii"})
        )

    assert len(candidates) == 1
    assert candidates[0].series_name == "Superior Day"


def test_find_candidates_excludes_already_quarantined(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inspection.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)

        archive_id, job_id = _seed_failed_job(
            connection,
            path=tmp_path / "library" / "Series A" / "issue1.cbz",
            failure_category="corrupt_archive",
        )
        connection.execute(
            """
            INSERT INTO archive_quarantine (
                archive_id, source_path, quarantine_path,
                failure_category, job_id
            )
            VALUES (?, 'x', 'y', 'corrupt_archive', ?)
            """,
            (archive_id, job_id),
        )

        repository = QuarantineRepository(connection)
        candidates = repository.find_candidates()

    assert candidates == []


# --- execute_quarantine -------------------------------------------------


def test_execute_quarantine_moves_file_and_updates_database(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inspection.db"
    source = tmp_path / "library" / "Series A" / "Series A Chapter 1.cbz"
    quarantine_root = tmp_path / "quarantine"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        _seed_failed_job(
            connection,
            path=source,
            failure_category="corrupt_archive",
        )

        repository = QuarantineRepository(connection)
        candidates = repository.find_candidates()

        results = execute_quarantine(
            connection,
            candidates,
            quarantine_root=quarantine_root,
        )

        assert len(results) == 1
        assert results[0].status == "moved"
        assert not source.exists()
        destination = Path(results[0].destination_path)
        assert destination.is_file()
        assert destination.parent == quarantine_root

        location = connection.execute(
            "SELECT is_current FROM file_locations WHERE path = ?",
            (str(source),),
        ).fetchone()
        assert location["is_current"] == 0

        quarantine_row = connection.execute(
            "SELECT status, quarantine_path FROM archive_quarantine"
        ).fetchone()
        assert quarantine_row["status"] == "pending_redownload"
        assert quarantine_row["quarantine_path"] == str(destination)

        event = connection.execute(
            "SELECT event_type, source_path, destination_path "
            "FROM file_events WHERE event_type = 'quarantined'"
        ).fetchone()
        assert event["source_path"] == str(source)
        assert event["destination_path"] == str(destination)


def test_execute_quarantine_reports_missing_source_without_db_changes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inspection.db"
    source = tmp_path / "library" / "Series A" / "issue1.cbz"
    quarantine_root = tmp_path / "quarantine"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        _seed_failed_job(
            connection,
            path=source,
            failure_category="corrupt_archive",
            create_file=False,
        )

        repository = QuarantineRepository(connection)
        candidates = repository.find_candidates()

        results = execute_quarantine(
            connection,
            candidates,
            quarantine_root=quarantine_root,
        )

        assert results[0].status == "error"
        assert "no longer exists" in results[0].error

        assert (
            connection.execute(
                "SELECT COUNT(*) FROM archive_quarantine"
            ).fetchone()[0]
            == 0
        )


def test_execute_quarantine_respects_limit(tmp_path: Path) -> None:
    database = tmp_path / "inspection.db"
    quarantine_root = tmp_path / "quarantine"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)

        for number in range(3):
            _seed_failed_job(
                connection,
                path=(
                    tmp_path
                    / "library"
                    / "Series A"
                    / f"issue{number}.cbz"
                ),
                failure_category="corrupt_archive",
            )

        repository = QuarantineRepository(connection)
        candidates = repository.find_candidates()

        results = execute_quarantine(
            connection,
            candidates,
            quarantine_root=quarantine_root,
            limit=2,
        )

    assert len(results) == 2


# --- CLI ------------------------------------------------------------


def test_cli_dry_run_does_not_touch_filesystem_or_database(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inspection.db"
    source = tmp_path / "library" / "Series A" / "Series A Chapter 1.cbz"
    quarantine_root = tmp_path / "quarantine"
    csv_output = tmp_path / "plan.csv"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        _seed_failed_job(
            connection,
            path=source,
            failure_category="corrupt_archive",
        )

    result = main(
        [
            "--database",
            str(database),
            "--quarantine-root",
            str(quarantine_root),
            "--csv-output",
            str(csv_output),
        ]
    )

    assert result == 0
    assert source.is_file()
    assert not quarantine_root.exists()

    with csv_output.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))

    assert len(rows) == 1
    assert rows[0]["status"] == "planned"

    with database_connection(database) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM archive_quarantine"
        ).fetchone()[0]
    assert count == 0


def test_cli_confirm_requires_backup_directory(tmp_path: Path) -> None:
    database = tmp_path / "inspection.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)

    result = main(
        [
            "--database",
            str(database),
            "--quarantine-root",
            str(tmp_path / "quarantine"),
            "--confirm",
        ]
    )

    assert result == 1


def test_cli_confirm_moves_files_and_backs_up_database(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inspection.db"
    source = tmp_path / "library" / "Series A" / "Series A Chapter 1.cbz"
    quarantine_root = tmp_path / "quarantine"
    backup_directory = tmp_path / "backups"
    json_output = tmp_path / "result.json"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        _seed_failed_job(
            connection,
            path=source,
            failure_category="corrupt_archive",
        )

    result = main(
        [
            "--database",
            str(database),
            "--quarantine-root",
            str(quarantine_root),
            "--backup-directory",
            str(backup_directory),
            "--json-output",
            str(json_output),
            "--confirm",
        ]
    )

    assert result == 0
    assert not source.exists()
    assert list(quarantine_root.glob("*.cbz"))
    assert list(backup_directory.glob("*.db"))

    payload = json.loads(json_output.read_text(encoding="utf-8"))
    assert payload["moved"] == 1
    assert payload["errored"] == 0
    assert payload["quick_check_before"] == "ok"
    assert payload["quick_check_after"] == "ok"
    assert payload["pending_redownload_total"] == 1


def test_cli_rejects_filesystem_not_found_category(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inspection.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)

    result = main(
        [
            "--database",
            str(database),
            "--quarantine-root",
            str(tmp_path / "quarantine"),
            "--category",
            "filesystem_not_found",
        ]
    )

    assert result == 1


def test_run_quarantine_excludes_series(tmp_path: Path) -> None:
    database = tmp_path / "inspection.db"
    quarantine_root = tmp_path / "quarantine"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        _seed_failed_job(
            connection,
            path=tmp_path / "library" / "Feng Shen Ji III" / "a.cbz",
            failure_category="corrupt_archive",
        )
        _seed_failed_job(
            connection,
            path=tmp_path / "library" / "Superior Day" / "b.cbz",
            failure_category="corrupt_archive",
        )

    output = run_quarantine(
        database=database,
        quarantine_root=quarantine_root,
        categories=None,
        exclude_series=["Feng Shen Ji III"],
        limit=None,
        confirm=False,
        backup_directory=None,
    )

    assert output["candidate_count"] == 1
    assert output["plan"][0]["series_name"] == "Superior Day"
