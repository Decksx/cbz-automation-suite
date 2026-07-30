from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import comic_automation.archive.duplicate_resolution as duplicate_resolution
from comic_automation.archive.duplicate_resolution import (
    DuplicateResolutionRepository,
)
from comic_automation.archive.duplicate_resolution_cli import main
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations


MIGRATION_DIRECTORY = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


def _seed_hashed_archive(
    connection: sqlite3.Connection,
    path: Path,
    content: bytes,
) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    stat = path.stat()
    digest = hashlib.sha256(content).hexdigest()

    archive_cursor = connection.execute(
        "INSERT INTO archive_files (file_size) VALUES (?)",
        (stat.st_size,),
    )
    archive_id = int(archive_cursor.lastrowid)
    location_cursor = connection.execute(
        """
        INSERT INTO file_locations (
            archive_id, path, is_current, file_size, modified_time_ns
        )
        VALUES (?, ?, 1, ?, ?)
        """,
        (archive_id, str(path), stat.st_size, stat.st_mtime_ns),
    )
    location_id = int(location_cursor.lastrowid)
    connection.execute(
        """
        INSERT INTO archive_hashes (
            archive_id, location_id, algorithm, algorithm_version,
            digest, file_size, modified_time_ns, bytes_read
        )
        VALUES (?, ?, 'sha256', '1', ?, ?, ?, ?)
        """,
        (
            archive_id,
            location_id,
            digest,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_size,
        ),
    )
    return archive_id


def test_plan_requires_one_organized_counterpart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inventory.db"
    extraneous_root = tmp_path / "library" / "_extraneous"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        _seed_hashed_archive(
            connection,
            extraneous_root / "Issue.cbz",
            b"same",
        )
        _seed_hashed_archive(
            connection,
            tmp_path / "library" / "Manga" / "Issue.cbz",
            b"same",
        )
        plan = DuplicateResolutionRepository(
            connection
        ).build_plan(extraneous_root=extraneous_root)

    assert len(plan) == 1
    assert plan[0].status == "planned"
    assert plan[0].counterpart_path == (
        tmp_path / "library" / "Manga" / "Issue.cbz"
    )


def test_plan_blocks_multiple_organized_counterparts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inventory.db"
    extraneous_root = tmp_path / "library" / "_extraneous"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        _seed_hashed_archive(
            connection,
            extraneous_root / "Issue.cbz",
            b"same",
        )
        _seed_hashed_archive(
            connection,
            tmp_path / "library" / "Manga" / "Issue.cbz",
            b"same",
        )
        _seed_hashed_archive(
            connection,
            tmp_path / "library" / "Comix" / "Issue.cbz",
            b"same",
        )
        plan = DuplicateResolutionRepository(
            connection
        ).build_plan(extraneous_root=extraneous_root)

    assert plan[0].status == "blocked"
    assert "found 2" in str(plan[0].error)


def test_plan_blocks_stale_hash(tmp_path: Path) -> None:
    database = tmp_path / "inventory.db"
    extraneous_root = tmp_path / "library" / "_extraneous"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        source_id = _seed_hashed_archive(
            connection,
            extraneous_root / "Issue.cbz",
            b"same",
        )
        _seed_hashed_archive(
            connection,
            tmp_path / "library" / "Manga" / "Issue.cbz",
            b"same",
        )
        connection.execute(
            """
            UPDATE file_locations
            SET modified_time_ns = modified_time_ns + 1
            WHERE archive_id = ?
            """,
            (source_id,),
        )
        plan = DuplicateResolutionRepository(
            connection
        ).build_plan(extraneous_root=extraneous_root)

    assert plan[0].status == "blocked"
    assert "stale" in str(plan[0].error)


def test_plan_blocks_active_job(tmp_path: Path) -> None:
    database = tmp_path / "inventory.db"
    extraneous_root = tmp_path / "library" / "_extraneous"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        source_id = _seed_hashed_archive(
            connection,
            extraneous_root / "Issue.cbz",
            b"same",
        )
        _seed_hashed_archive(
            connection,
            tmp_path / "library" / "Manga" / "Issue.cbz",
            b"same",
        )
        connection.execute(
            """
            INSERT INTO jobs (job_type, status, archive_id)
            VALUES ('inspect_archive', 'pending', ?)
            """,
            (source_id,),
        )
        plan = DuplicateResolutionRepository(
            connection
        ).build_plan(extraneous_root=extraneous_root)

    assert plan[0].status == "blocked"
    assert "active job" in str(plan[0].error)


def test_plan_indexes_hashes_without_resolving_every_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "inventory.db"
    extraneous_root = tmp_path / "library" / "_extraneous"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)

        for index in range(4):
            content = f"duplicate-{index}".encode()
            _seed_hashed_archive(
                connection,
                extraneous_root / f"Issue-{index}.cbz",
                content,
            )
            _seed_hashed_archive(
                connection,
                tmp_path
                / "library"
                / "Manga"
                / f"Issue-{index}.cbz",
                content,
            )

        for index in range(200):
            _seed_hashed_archive(
                connection,
                tmp_path
                / "library"
                / "Unrelated"
                / f"Issue-{index}.cbz",
                f"unrelated-{index}".encode(),
            )

        def fail_if_resolved(*args, **kwargs):
            raise AssertionError(
                "Planning must not resolve every stored filesystem path."
            )

        monkeypatch.setattr(
            duplicate_resolution,
            "path_is_within",
            fail_if_resolved,
        )
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        plan = DuplicateResolutionRepository(
            connection
        ).build_plan(extraneous_root=extraneous_root)
        connection.set_trace_callback(None)

    assert len(plan) == 4
    assert all(candidate.status == "planned" for candidate in plan)
    job_queries = [
        statement
        for statement in statements
        if "FROM jobs" in statement
    ]
    assert len(job_queries) == 1


def test_cli_preview_is_read_only(tmp_path: Path) -> None:
    database = tmp_path / "inventory.db"
    extraneous_root = tmp_path / "library" / "_extraneous"
    source = extraneous_root / "Issue.cbz"
    report = tmp_path / "preview.json"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        _seed_hashed_archive(connection, source, b"same")
        _seed_hashed_archive(
            connection,
            tmp_path / "library" / "Manga" / "Issue.cbz",
            b"same",
        )

    result = main(
        [
            "--database",
            str(database),
            "--extraneous-root",
            str(extraneous_root),
            "--json-output",
            str(report),
        ]
    )

    assert result == 0
    assert source.is_file()
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["eligible_count"] == 1
    assert payload["backed_up"] == 0
    assert payload["quick_check_before"] is None


def test_cli_confirm_requires_backup_directory(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inventory.db"
    extraneous_root = tmp_path / "library" / "_extraneous"
    extraneous_root.mkdir(parents=True)

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)

    result = main(
        [
            "--database",
            str(database),
            "--extraneous-root",
            str(extraneous_root),
            "--confirm",
        ]
    )

    assert result == 1


def test_cli_confirm_moves_only_extraneous_copy_and_audits(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inventory.db"
    extraneous_root = tmp_path / "library" / "_extraneous"
    source = extraneous_root / "Series" / "Issue.cbz"
    organized = tmp_path / "library" / "Manga" / "Issue.cbz"
    backup_directory = tmp_path / "backups"
    report = tmp_path / "result.json"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        source_id = _seed_hashed_archive(
            connection,
            source,
            b"same",
        )
        organized_id = _seed_hashed_archive(
            connection,
            organized,
            b"same",
        )

    result = main(
        [
            "--database",
            str(database),
            "--extraneous-root",
            str(extraneous_root),
            "--backup-directory",
            str(backup_directory),
            "--json-output",
            str(report),
            "--confirm",
        ]
    )

    assert result == 0
    assert not source.exists()
    assert organized.is_file()

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["backed_up"] == 1
    assert payload["errored"] == 0
    assert payload["quick_check_before"] == "ok"
    assert payload["quick_check_after"] == "ok"
    assert Path(payload["database_backup"]).is_file()
    assert Path(payload["plan"][0]["backup_path"]).is_file()

    with database_connection(database) as connection:
        source_current = connection.execute(
            """
            SELECT is_current FROM file_locations
            WHERE archive_id = ?
            """,
            (source_id,),
        ).fetchone()[0]
        organized_current = connection.execute(
            """
            SELECT is_current FROM file_locations
            WHERE archive_id = ?
            """,
            (organized_id,),
        ).fetchone()[0]
        event = connection.execute(
            """
            SELECT source_path, destination_path, details_json
            FROM file_events
            WHERE archive_id = ? AND event_type = 'duplicate_removed'
            """,
            (source_id,),
        ).fetchone()

    assert source_current == 0
    assert organized_current == 1
    assert event["source_path"] == str(source)
    assert Path(event["destination_path"]).is_file()
    details = json.loads(event["details_json"])
    assert details["organized_archive_id"] == organized_id
    assert details["organized_path"] == str(organized)
