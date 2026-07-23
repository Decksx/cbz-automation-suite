from __future__ import annotations

from pathlib import Path

from comic_automation.database.connection import (
    database_connection,
)
from comic_automation.library.cli import main


def create_archive(
    path: Path,
    content: bytes = b"test archive",
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_discovery_cli_runs_end_to_end(
    tmp_path: Path,
    capsys,
) -> None:
    library = tmp_path / "library"
    database = tmp_path / "database" / "inventory.db"

    first = create_archive(
        library / "Series A" / "Issue 01.cbz",
        b"first archive",
    )
    second = create_archive(
        library / "Series B" / "Issue 02.cbr",
        b"second archive",
    )
    create_archive(
        library / "Series B" / "notes.txt",
        b"ignore this file",
    )

    first_before = first.stat()
    second_before = second.stat()

    result = main(
        [
            "--root",
            str(library),
            "--database",
            str(database),
            "--batch-size",
            "1",
        ]
    )

    captured = capsys.readouterr()

    assert result == 0
    assert database.is_file()
    assert "Read-only library discovery completed." in captured.out
    assert "Scanned:     2" in captured.out
    assert "New:         2" in captured.out
    assert "Jobs queued: 2" in captured.out
    assert captured.err == ""

    with database_connection(database) as connection:
        location_count = connection.execute(
            "SELECT COUNT(*) FROM file_locations"
        ).fetchone()[0]

        job_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM jobs
            WHERE job_type = 'inspect_archive'
            """
        ).fetchone()[0]

    assert location_count == 2
    assert job_count == 2

    assert first.read_bytes() == b"first archive"
    assert second.read_bytes() == b"second archive"
    assert first.stat().st_mtime_ns == first_before.st_mtime_ns
    assert second.stat().st_mtime_ns == second_before.st_mtime_ns


def test_discovery_cli_second_scan_reports_unchanged(
    tmp_path: Path,
    capsys,
) -> None:
    library = tmp_path / "library"
    database = tmp_path / "inventory.db"

    create_archive(library / "Issue.cbz")

    arguments = [
        "--root",
        str(library),
        "--database",
        str(database),
    ]

    assert main(arguments) == 0
    capsys.readouterr()

    assert main(arguments) == 0
    captured = capsys.readouterr()

    assert "Scanned:     1" in captured.out
    assert "New:         0" in captured.out
    assert "Changed:     0" in captured.out
    assert "Unchanged:   1" in captured.out
    assert "Jobs queued: 0" in captured.out


def test_discovery_cli_returns_failure_for_missing_root(
    tmp_path: Path,
    capsys,
) -> None:
    missing_root = tmp_path / "missing-library"
    database = tmp_path / "inventory.db"

    result = main(
        [
            "--root",
            str(missing_root),
            "--database",
            str(database),
        ]
    )

    captured = capsys.readouterr()

    assert result == 1
    assert captured.out == ""
    assert "Library discovery failed:" in captured.err
    assert "does not exist" in captured.err
