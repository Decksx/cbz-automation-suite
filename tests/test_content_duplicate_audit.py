"""Tests for the read-only exact content-duplicate audit.

The audit exists because duplicate control was believed to be gated on the
perceptual backfill and is not: archives sharing an ordered-page content
signature are decidable from data already stored. These tests pin the two
properties that make the report trustworthy -- that it groups on content
rather than on names or metadata, and that it never offers a copy whose
recorded location is no longer current.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from comic_automation.archive.content_duplicate_audit import (
    SIGNATURE_ALGORITHM,
    SIGNATURE_VERSION,
    collect_duplicate_groups,
    run_audit,
    summarize,
)
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations


MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


def _seed(
    connection: sqlite3.Connection,
    *,
    path: str | None,
    digest: str,
    page_count: int = 10,
    file_size: int = 2048,
    archive_sha256: str | None = None,
) -> int:
    """Insert one archive with a content signature and optional current location.

    path=None models an archive whose location is not current -- moved, deleted,
    or otherwise unresolvable. Such an archive must never be offered as a
    deletable duplicate, because nothing proves the file is still there.
    """
    archive_id = int(
        connection.execute(
            "INSERT INTO archive_files (file_size) VALUES (?)", (file_size,)
        ).lastrowid
    )
    if path is not None:
        connection.execute(
            """
            INSERT INTO file_locations (
                archive_id, path, file_size, modified_time_ns, is_current
            )
            VALUES (?, ?, ?, 1000, 1)
            """,
            (archive_id, path, file_size),
        )
    connection.execute(
        """
        INSERT INTO archive_content_signatures (
            archive_id, algorithm, algorithm_version, digest,
            page_count, image_bytes, source_file_size, source_modified_time_ns
        )
        VALUES (?, ?, ?, ?, ?, 1024, ?, 1000)
        """,
        (
            archive_id,
            SIGNATURE_ALGORITHM,
            SIGNATURE_VERSION,
            digest,
            page_count,
            file_size,
        ),
    )
    if archive_sha256 is not None:
        connection.execute(
            """
            INSERT INTO archive_hashes (
                archive_id, algorithm, algorithm_version, digest, file_size,
                modified_time_ns, bytes_read
            )
            VALUES (?, 'sha256', '1', ?, ?, 1000, ?)
            """,
            (archive_id, archive_sha256, file_size, file_size),
        )
    return archive_id


@pytest.fixture()
def database(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    with database_connection(path) as connection:
        apply_migrations(connection, MIGRATIONS)
    return path


def _groups(database: Path) -> list[dict]:
    with database_connection(database) as connection:
        connection.row_factory = sqlite3.Row
        return collect_duplicate_groups(connection)


def test_identical_signatures_group_regardless_of_filename(database: Path):
    """Content decides membership -- unrelated filenames still group.

    This is the whole point: the names below share nothing, and the ComicInfo
    metadata that misled the 2026-08-17 maintenance run is not consulted at all.
    """
    with database_connection(database) as connection:
        _seed(connection, path=r"X:\Comix\Alpha\Alpha Ch 1.cbz", digest="aaa")
        _seed(connection, path=r"X:\Comix\Beta\totally different name.cbz", digest="aaa")

    groups = _groups(database)

    assert len(groups) == 1
    assert groups[0]["member_count"] == 2
    assert groups[0]["redundant_count"] == 1


def test_differing_signatures_never_group(database: Path):
    """Different page content is never a duplicate, however similar the names."""
    with database_connection(database) as connection:
        _seed(connection, path=r"X:\Comix\S\Chapter 1.cbz", digest="aaa")
        _seed(connection, path=r"X:\Comix\S\Chapter  1.cbz", digest="bbb")

    assert _groups(database) == []


def test_archive_without_current_location_is_excluded(database: Path):
    """A copy whose location is not current cannot be offered for deletion.

    Otherwise the report would propose reclaiming space by deleting a file that
    may already be gone, and would count phantom bytes as reclaimable.
    """
    with database_connection(database) as connection:
        _seed(connection, path=r"X:\Comix\Here\real.cbz", digest="aaa")
        _seed(connection, path=None, digest="aaa")

    # Only one located member remains, so there is no group at all.
    assert _groups(database) == []


def test_reclaimable_bytes_measured_against_the_largest_member(database: Path):
    """Reclaim excludes the copy most likely to be kept.

    Members of a group can differ in size because identical pages compress
    differently. Counting every member would overstate the saving by one whole
    archive.
    """
    with database_connection(database) as connection:
        _seed(connection, path=r"X:\a.cbz", digest="aaa", file_size=1000)
        _seed(connection, path=r"X:\b.cbz", digest="aaa", file_size=400)
        _seed(connection, path=r"X:\c.cbz", digest="aaa", file_size=300)

    group = _groups(database)[0]

    assert group["member_count"] == 3
    assert group["reclaimable_bytes"] == 700  # 1000+400+300 minus the 1000 kept


def test_byte_identical_flag_distinguishes_recompressed_copies(database: Path):
    """Same pages but different container bytes must not be called byte-identical.

    The production library's 886 groups contain zero byte-identical groups, so
    a report that conflated the two would describe it wrongly.
    """
    with database_connection(database) as connection:
        _seed(connection, path=r"X:\a.cbz", digest="aaa", archive_sha256="ff00")
        _seed(connection, path=r"X:\b.cbz", digest="aaa", archive_sha256="ff00")
        _seed(connection, path=r"X:\c.cbz", digest="bbb", archive_sha256="1111")
        _seed(connection, path=r"X:\d.cbz", digest="bbb", archive_sha256="2222")

    by_signature = {g["signature"]: g for g in _groups(database)}

    assert by_signature["aaa"]["byte_identical"] is True
    assert by_signature["bbb"]["byte_identical"] is False


def test_members_are_ordered_most_complete_first(database: Path):
    """Ordering is stable and puts the fullest copy first.

    A reviewer reading top-down should see the copy worth keeping first, and
    two runs over unchanged data must produce identical output.
    """
    with database_connection(database) as connection:
        small = _seed(connection, path=r"X:\small.cbz", digest="aaa", page_count=5)
        big = _seed(connection, path=r"X:\big.cbz", digest="aaa", page_count=50)

    members = _groups(database)[0]["members"]

    assert [m["archive_id"] for m in members] == [big, small]


def test_zero_page_archives_are_ignored(database: Path):
    """Page-count-zero archives share a trivially equal signature and are not dupes."""
    with database_connection(database) as connection:
        _seed(connection, path=r"X:\a.cbz", digest="empty", page_count=0)
        _seed(connection, path=r"X:\b.cbz", digest="empty", page_count=0)

    assert _groups(database) == []


def test_run_audit_is_read_only_and_reports_provenance(database: Path, tmp_path: Path):
    """The audit reports its own guard evidence and leaves the database alone."""
    with database_connection(database) as connection:
        _seed(connection, path=r"X:\a.cbz", digest="aaa")
        _seed(connection, path=r"X:\b.cbz", digest="aaa")

    output = run_audit(database=database, json_output=tmp_path / "report.json")

    assert output["quick_check"] == "ok"
    assert output["data_version_before"] == output["data_version_after"]
    assert output["database_file_unchanged"] is True
    assert output["summary"]["group_count"] == 1
    assert output["summary"]["redundant_copies"] == 1
    assert (tmp_path / "report.json").is_file()


def test_report_refuses_to_overwrite_existing_output(database: Path, tmp_path: Path):
    """An earlier report is evidence, not scratch space."""
    existing = tmp_path / "report.json"
    existing.write_text("{}", encoding="utf-8")

    with pytest.raises(FileExistsError):
        run_audit(database=database, json_output=existing)


def test_summarize_totals_match_the_groups(database: Path):
    with database_connection(database) as connection:
        _seed(connection, path=r"X:\a.cbz", digest="aaa", file_size=500)
        _seed(connection, path=r"X:\b.cbz", digest="aaa", file_size=500)
        _seed(connection, path=r"X:\c.cbz", digest="ccc", file_size=100)
        _seed(connection, path=r"X:\d.cbz", digest="ccc", file_size=100)
        _seed(connection, path=r"X:\e.cbz", digest="ccc", file_size=100)

    summary = summarize(_groups(database))

    assert summary["group_count"] == 2
    assert summary["redundant_copies"] == 3
    assert summary["largest_group"] == 3
