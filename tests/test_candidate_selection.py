"""Tests for the shared candidate-selection path.

Two incidents produced this module, and the tests are written against both.

The 2026-08-17 batch failed 79 jobs `filesystem_not_found` because eligibility
compared database rows to database rows and never stat'd the filesystem. The
2026-08-18 retirement of archive 45217 then raised eligibility from 12,554 to
12,555, because retirement had been recorded against the *job* and the
predicate only excludes archives holding an active job.

So the tests below pin two different things, and the second is the one easy to
get wrong: a retired archive must be refused **whether or not its file
exists**. A gate that only rejects missing files would pass a test suite that
never puts a real file behind a retired archive.
"""

from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from comic_automation.archive.candidate_selection import (
    ARCHIVE_RETIRED,
    MULTIPLE_CURRENT_LOCATIONS,
    NO_CURRENT_LOCATION,
    PATH_MISSING,
    PATH_NOT_A_REGULAR_FILE,
    PATH_UNREADABLE,
    REJECTION_REASONS,
    revalidate_for_enqueue,
    select_candidates,
)
from comic_automation.archive.page_hashing import (
    ArchivePageHashRepository,
    calculate_page_hashes,
)
from comic_automation.archive.perceptual_hashing import (
    ArchivePerceptualHashRepository,
)
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations

MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


def create_cbz(path: Path, *, pages: int = 2) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (64, 96), "white")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    payload = buffer.getvalue()

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for index in range(pages):
            archive.writestr("%03d.png" % (index + 1), payload)

    return path


@pytest.fixture()
def connection(tmp_path: Path):
    with database_connection(tmp_path / "selection.db") as conn:
        apply_migrations(conn, MIGRATIONS)
        yield conn


def seed(conn, path: Path | str, *, current: int = 1) -> int:
    """One archive with one location row pointing at *path*."""
    archive_id = int(
        conn.execute(
            "INSERT INTO archive_files (file_size) VALUES (1)"
        ).lastrowid
    )
    add_location(conn, archive_id, path, current=current)
    return archive_id


def add_location(conn, archive_id: int, path: Path | str, *, current: int = 1):
    conn.execute(
        """
        INSERT INTO file_locations (
            archive_id, path, is_current, file_size, modified_time_ns
        )
        VALUES (?, ?, ?, 1, 1)
        """,
        (archive_id, str(path), current),
    )


def retire(
    conn,
    archive_id: int,
    reason: str = "duplicate; keeper elsewhere",
    evidence: str = "ordered-page signature held by archive 999",
):
    # Evidence is mandatory as of migration 013: a retirement is a recorded
    # decision, and one without proof is not reviewable later.
    conn.execute(
        "INSERT INTO archive_retirements (archive_id, reason, evidence) "
        "VALUES (?, ?, ?)",
        (archive_id, reason, evidence),
    )


def only_rejection(selection):
    assert selection.accepted == []
    assert len(selection.rejected) == 1
    return selection.rejected[0]


# --- the accepting case ---------------------------------------------------


def test_a_real_file_with_one_current_location_is_accepted(
    connection, tmp_path: Path
) -> None:
    archive = create_cbz(tmp_path / "lib" / "a.cbz")
    archive_id = seed(connection, archive)

    selection = select_candidates(connection, [archive_id])

    assert selection.rejected == []
    assert selection.accepted_ids == [archive_id]
    assert selection.accepted[0].path == str(archive)


# --- filesystem refusals --------------------------------------------------


def test_a_missing_path_is_rejected(connection, tmp_path: Path) -> None:
    """The 79. Recorded state said current; the file was not there."""
    archive_id = seed(connection, tmp_path / "gone" / "a.cbz")

    rejection = only_rejection(select_candidates(connection, [archive_id]))

    assert rejection.reason == PATH_MISSING


def test_a_directory_is_not_a_file(connection, tmp_path: Path) -> None:
    """`os.path.exists` is true for a directory; opening one is not."""
    directory = tmp_path / "lib" / "a.cbz"
    directory.mkdir(parents=True)
    archive_id = seed(connection, directory)

    rejection = only_rejection(select_candidates(connection, [archive_id]))

    assert rejection.reason == PATH_NOT_A_REGULAR_FILE
    assert "directory" in rejection.detail


def test_an_unreadable_path_is_not_reported_as_missing(
    connection, tmp_path: Path, monkeypatch
) -> None:
    """An access error is not evidence of absence.

    Conflating the two is what previously sent location repair hunting for a
    replacement file that had never gone anywhere.
    """
    archive = create_cbz(tmp_path / "lib" / "a.cbz")
    archive_id = seed(connection, archive)

    import comic_automation.archive.candidate_selection as module

    def refuse(path, **kwargs):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(module, "_stat", refuse)

    rejection = only_rejection(select_candidates(connection, [archive_id]))

    assert rejection.reason == PATH_UNREADABLE
    assert rejection.reason != PATH_MISSING


# --- location refusals ----------------------------------------------------


def test_no_current_location_is_rejected(connection, tmp_path: Path) -> None:
    archive = create_cbz(tmp_path / "lib" / "a.cbz")
    archive_id = seed(connection, archive, current=0)

    rejection = only_rejection(select_candidates(connection, [archive_id]))

    assert rejection.reason == NO_CURRENT_LOCATION


def test_multiple_current_locations_are_ambiguous(
    connection, tmp_path: Path
) -> None:
    """Two current rows is a question, not a candidate; picking one guesses."""
    first = create_cbz(tmp_path / "lib" / "a.cbz")
    second = create_cbz(tmp_path / "lib" / "copy" / "a.cbz")
    archive_id = seed(connection, first)
    add_location(connection, archive_id, second)

    rejection = only_rejection(select_candidates(connection, [archive_id]))

    assert rejection.reason == MULTIPLE_CURRENT_LOCATIONS


# --- retirement -----------------------------------------------------------


def test_a_retired_archive_is_rejected_even_though_its_file_exists(
    connection, tmp_path: Path
) -> None:
    """The distinction the whole module exists for.

    A live-path gate would accept this archive: its file is right there. Only
    a durable, archive-level retirement keeps it out, and only that survives
    the file coming back.
    """
    archive = create_cbz(tmp_path / "lib" / "a.cbz")
    archive_id = seed(connection, archive)
    retire(connection, archive_id)

    assert archive.exists()

    rejection = only_rejection(select_candidates(connection, [archive_id]))

    assert rejection.reason == ARCHIVE_RETIRED
    assert "filesystem state not consulted" in rejection.detail


def test_retirement_is_checked_before_the_filesystem(
    connection, tmp_path: Path, monkeypatch
) -> None:
    """A retired archive must not cost a stat, nor depend on one succeeding."""
    archive_id = seed(connection, tmp_path / "gone" / "a.cbz")
    retire(connection, archive_id)

    import comic_automation.archive.candidate_selection as module

    def explode(path, **kwargs):
        raise AssertionError("the filesystem was consulted for a retired archive")

    monkeypatch.setattr(module, "_stat", explode)

    rejection = only_rejection(select_candidates(connection, [archive_id]))

    assert rejection.reason == ARCHIVE_RETIRED


def test_retirement_survives_the_file_coming_back(
    connection, tmp_path: Path
) -> None:
    """Restoring the file must not un-retire the archive.

    This is the failure a filesystem-only gate produces: retirement that
    quietly expires the moment a restore, re-sync, or rename puts the path
    back.
    """
    path = tmp_path / "lib" / "a.cbz"
    archive_id = seed(connection, path)
    retire(connection, archive_id)

    assert only_rejection(
        select_candidates(connection, [archive_id])
    ).reason == ARCHIVE_RETIRED

    create_cbz(path)

    assert only_rejection(
        select_candidates(connection, [archive_id])
    ).reason == ARCHIVE_RETIRED


def test_a_blank_retirement_reason_is_refused_by_the_database(
    connection, tmp_path: Path
) -> None:
    """A retirement with no reason is indistinguishable later from a mistake."""
    import sqlite3

    archive_id = seed(connection, create_cbz(tmp_path / "lib" / "a.cbz"))

    for blank in ("", "   ", "\t"):
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO archive_retirements "
                "(archive_id, reason, evidence) VALUES (?, ?, ?)",
                (archive_id, blank, "evidence"),
            )


def test_an_archive_cannot_be_retired_twice(connection, tmp_path: Path) -> None:
    """Re-retiring is rejected, not a silent overwrite of the first reason."""
    import sqlite3

    archive_id = seed(connection, create_cbz(tmp_path / "lib" / "a.cbz"))
    retire(connection, archive_id, "the real reason")

    with pytest.raises(sqlite3.IntegrityError):
        retire(connection, archive_id, "a different reason")

    stored = connection.execute(
        "SELECT reason FROM archive_retirements WHERE archive_id = ?",
        (archive_id,),
    ).fetchone()[0]
    assert stored == "the real reason"


# --- reporting ------------------------------------------------------------


def test_every_rejection_is_returned_not_dropped(
    connection, tmp_path: Path
) -> None:
    """A candidate that vanishes silently cannot be told from an ineligible one."""
    good = seed(connection, create_cbz(tmp_path / "lib" / "good.cbz"))
    missing = seed(connection, tmp_path / "gone.cbz")
    retired = seed(connection, create_cbz(tmp_path / "lib" / "retired.cbz"))
    retire(connection, retired)

    selection = select_candidates(connection, [good, missing, retired])

    assert selection.accepted_ids == [good]
    assert len(selection.rejected) == 2
    assert {r.archive_id for r in selection.rejected} == {missing, retired}

    grouped = selection.rejections_by_reason()
    # Reasons with nothing in them are present, so a report cannot lose a
    # category by having no examples today.
    assert set(grouped) == set(REJECTION_REASONS)
    assert grouped[PATH_MISSING] == [missing]
    assert grouped[ARCHIVE_RETIRED] == [retired]
    assert grouped[PATH_UNREADABLE] == []


def test_check_filesystem_false_still_applies_retirement(
    connection, tmp_path: Path
) -> None:
    """Skipping the disk check must not skip the checks that never used it."""
    missing = seed(connection, tmp_path / "gone.cbz")
    retired = seed(connection, create_cbz(tmp_path / "lib" / "a.cbz"))
    retire(connection, retired)

    selection = select_candidates(
        connection, [missing, retired], check_filesystem=False
    )

    assert selection.accepted_ids == [missing]
    assert only_rejection_reason(selection) == ARCHIVE_RETIRED


def only_rejection_reason(selection) -> str:
    assert len(selection.rejected) == 1
    return selection.rejected[0].reason


# --- the selection/enqueue race ------------------------------------------


def test_an_archive_retired_after_selection_is_refused_at_enqueue(
    connection, tmp_path: Path
) -> None:
    """Preflight and enqueue are separated in time, and the database moves."""
    archive_id = seed(connection, create_cbz(tmp_path / "lib" / "a.cbz"))

    selection = select_candidates(connection, [archive_id])
    assert selection.accepted_ids == [archive_id]

    retire(connection, archive_id)

    rejection = revalidate_for_enqueue(connection, archive_id)
    assert rejection is not None
    assert rejection.reason == ARCHIVE_RETIRED
    assert "between selection and enqueue" in rejection.detail


def test_a_location_removed_after_selection_is_refused_at_enqueue(
    connection, tmp_path: Path
) -> None:
    archive_id = seed(connection, create_cbz(tmp_path / "lib" / "a.cbz"))
    assert select_candidates(connection, [archive_id]).accepted_ids == [archive_id]

    connection.execute(
        "UPDATE file_locations SET is_current = 0 WHERE archive_id = ?",
        (archive_id,),
    )

    rejection = revalidate_for_enqueue(connection, archive_id)
    assert rejection is not None
    assert rejection.reason == NO_CURRENT_LOCATION


def test_a_second_location_after_selection_is_refused_at_enqueue(
    connection, tmp_path: Path
) -> None:
    archive_id = seed(connection, create_cbz(tmp_path / "lib" / "a.cbz"))
    assert select_candidates(connection, [archive_id]).accepted_ids == [archive_id]

    add_location(connection, archive_id, tmp_path / "lib" / "copy.cbz")

    rejection = revalidate_for_enqueue(connection, archive_id)
    assert rejection is not None
    assert rejection.reason == MULTIPLE_CURRENT_LOCATIONS


def test_a_file_deleted_after_selection_is_left_to_the_worker(
    connection, tmp_path: Path
) -> None:
    """Revalidation deliberately does not re-stat.

    Re-statting would move the race rather than remove it, and cost a syscall
    per archive for a guarantee it still could not give. The worker reports
    this case as filesystem_not_found, which is what that handling is for.
    """
    archive = create_cbz(tmp_path / "lib" / "a.cbz")
    archive_id = seed(connection, archive)
    assert select_candidates(connection, [archive_id]).accepted_ids == [archive_id]

    archive.unlink()

    assert revalidate_for_enqueue(connection, archive_id) is None


# --- integration: enqueue_missing uses the same path ---------------------


def eligible_archive(connection, path: Path) -> int:
    """An archive the perceptual database predicate accepts."""
    stat = path.stat()
    archive_id = int(
        connection.execute(
            "INSERT INTO archive_files (file_size) VALUES (?)",
            (stat.st_size,),
        ).lastrowid
    )
    location_id = int(
        connection.execute(
            """
            INSERT INTO file_locations (
                archive_id, path, is_current, file_size, modified_time_ns
            )
            VALUES (?, ?, 1, ?, ?)
            """,
            (archive_id, str(path), stat.st_size, stat.st_mtime_ns),
        ).lastrowid
    )
    ArchivePageHashRepository(connection).save(
        archive_id=archive_id,
        location_id=location_id,
        result=calculate_page_hashes(path),
    )
    return archive_id


def test_enqueue_missing_skips_retired_and_absent_archives(
    connection, tmp_path: Path
) -> None:
    """The end the whole change exists for.

    Before this, all three of these would have been enqueued and two would
    have failed in a worker.
    """
    good = eligible_archive(connection, create_cbz(tmp_path / "lib" / "good.cbz"))

    retired_path = create_cbz(tmp_path / "lib" / "retired.cbz")
    retired = eligible_archive(connection, retired_path)
    retire(connection, retired)

    absent_path = create_cbz(tmp_path / "lib" / "absent.cbz")
    absent = eligible_archive(connection, absent_path)
    absent_path.unlink()

    repository = ArchivePerceptualHashRepository(connection)

    # All three satisfy the database rules alone.
    assert repository.count_eligible() == 3

    selection = repository.select_enqueueable()
    assert selection.accepted_ids == [good]
    grouped = selection.rejections_by_reason()
    assert grouped[ARCHIVE_RETIRED] == [retired]
    assert grouped[PATH_MISSING] == [absent]

    assert repository.enqueue_missing() == 1

    enqueued = {
        int(row[0])
        for row in connection.execute(
            "SELECT archive_id FROM jobs "
            "WHERE job_type = 'hash_archive_pages_perceptual'"
        )
    }
    assert enqueued == {good}
