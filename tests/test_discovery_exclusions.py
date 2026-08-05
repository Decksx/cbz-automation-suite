"""Tests for directory-exclusion support in library discovery
(comic_automation.library.discover_archives/scan_library and the CLI).

Covers the default exclusion of Syncthing's history folders (.stversions,
.stfolder), user-supplied custom exclusions, exclusion-directory counting
for reporting, the important edge case that a file sitting in a
newly-excluded directory should NOT be marked missing (it's being
deliberately skipped, not actually gone), and the CLI's --exclude-directory
flag (repeatable) combined with --dry-run.
"""

from __future__ import annotations

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
from comic_automation.library.cli import main


MIGRATION_DIRECTORY = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


def create_archive(path: Path) -> Path:
    """Write a placeholder archive file at `path` (parents created as
    needed); only discovery metadata matters here, not real archive
    content.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"archive")
    return path


def test_default_syncthing_history_is_excluded(
    tmp_path: Path,
) -> None:
    """Without any custom configuration, discover_archives should skip
    Syncthing's .stversions history folder by default, finding only the
    active archive.
    """
    library = tmp_path / "library"
    active = create_archive(library / "Series" / "Issue.cbz")
    create_archive(
        library / ".stversions" / "Series" / "Old.cbz"
    )

    discovered = list(discover_archives(library))

    assert [item.path for item in discovered] == [
        active.resolve()
    ]


def test_custom_directory_exclusion(
    tmp_path: Path,
) -> None:
    """Passing excluded_directories should skip any directory by that name,
    not just the built-in Syncthing defaults.
    """
    library = tmp_path / "library"
    active = create_archive(library / "Active" / "Issue.cbz")
    create_archive(
        library / "_extraneous" / "Excluded.cbz"
    )

    discovered = list(
        discover_archives(
            library,
            excluded_directories=["_extraneous"],
        )
    )

    assert [item.path for item in discovered] == [
        active.resolve()
    ]


def test_excluded_directories_are_counted(
    tmp_path: Path,
) -> None:
    """The on_excluded_directory callback should fire once per skipped
    directory (covering both default exclusions here), letting callers
    report how many directories were skipped.
    """
    library = tmp_path / "library"
    create_archive(library / "Active" / "Issue.cbz")
    create_archive(library / ".stversions" / "Old.cbz")
    create_archive(library / ".stfolder" / "Marker.cbz")

    excluded: list[Path] = []

    list(
        discover_archives(
            library,
            on_excluded_directory=excluded.append,
        )
    )

    assert {path.name for path in excluded} == {
        ".stversions",
        ".stfolder",
    }


def test_excluded_existing_location_is_not_marked_missing(
    tmp_path: Path,
) -> None:
    """If a file was previously scanned and then its directory becomes
    excluded on a later scan, that file must NOT be marked missing --
    being skipped by exclusion is a deliberate choice, not evidence the
    file is actually gone, so is_current should stay 1.
    """
    library = tmp_path / "library"
    database = tmp_path / "inventory.db"
    archive = create_archive(
        library / "_extraneous" / "Issue.cbz"
    )

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)

        first = scan_library(
            connection,
            library,
            excluded_directories=[],
        )
        assert first.new == 1

        second = scan_library(
            connection,
            library,
            excluded_directories=["_extraneous"],
        )

        location = connection.execute(
            """
            SELECT is_current
            FROM file_locations
            WHERE path = ?
            """,
            (str(archive.resolve()),),
        ).fetchone()

    assert second.missing == 0
    assert location["is_current"] == 1


def test_cli_accepts_repeatable_exclusions(
    tmp_path: Path,
    capsys,
) -> None:
    """--exclude-directory should be repeatable on the CLI (one flag per
    directory name), and combined with --dry-run should report the
    exclusion count in its summary while never creating the database file.
    """
    library = tmp_path / "library"
    database = tmp_path / "inventory.db"

    create_archive(library / "Active" / "Issue.cbz")
    create_archive(library / "_extraneous" / "Old.cbz")
    create_archive(library / ".stversions" / "History.cbz")

    result = main(
        [
            "--root",
            str(library),
            "--database",
            str(database),
            "--dry-run",
            "--exclude-directory",
            "_extraneous",
            "--exclude-directory",
            ".stversions",
        ]
    )

    captured = capsys.readouterr()

    assert result == 0
    assert "Scanned:     1" in captured.out
    assert "Excluded:    2 directories" in captured.out
    assert database.exists() is False
