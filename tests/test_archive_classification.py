"""The shared classification contract.

Two kinds of test here, and the distinction matters.

*Seeded examples* build a database that reproduces a shape production
actually holds -- archive 45217's cancelled-and-retired pair, a quarantined
zero-page archive, an archive with several exclusion reasons at once -- and
assert the contract describes it the way a reader needs.

*Bypass proofs* go around a guard and assert the contract refuses rather than
reports. Every fail-closed claim is proven that way, because a guard
exercised only through the path that respects it has not been demonstrated.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from comic_automation.archive import classification as C
from comic_automation.archive import disposition
from comic_automation.archive.candidate_selection import (
    ARCHIVE_RETIRED,
    ARCHIVE_SUPERSEDED,
    PATH_MISSING,
)
from comic_automation.archive.perceptual_hashing import (
    DHASH_ALGORITHM,
    DHASH_ALGORITHM_VERSION,
    EXCLUSION_BLOCKING_JOB,
    EXCLUSION_MULTIPLE_CURRENT_LOCATIONS,
    EXCLUSION_NO_CONTENT_SIGNATURE,
    EXCLUSION_NO_CURRENT_LOCATION,
    EXCLUSION_NO_OUTSTANDING_PAGES,
    EXCLUSION_SIGNATURE_MTIME_MISMATCH,
    EXCLUSION_SIGNATURE_PAGE_COUNT_ZERO,
    EXCLUSION_SIGNATURE_SIZE_MISMATCH,
    PHASH_ALGORITHM,
    PHASH_ALGORITHM_VERSION,
    ArchivePerceptualHashRepository,
)
from comic_automation.database.connection import connect_database
from comic_automation.database.migrations import apply_migrations


MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


# --- fixture builders ----------------------------------------------------


@pytest.fixture()
def connection(tmp_path: Path):
    conn = connect_database(tmp_path / "classification.db")
    apply_migrations(conn, MIGRATIONS)

    try:
        yield conn
    finally:
        conn.close()


def add_archive(conn: sqlite3.Connection, archive_id: int) -> int:
    conn.execute(
        "INSERT INTO archive_files (id, file_size) VALUES (?, ?)",
        (archive_id, 4096),
    )
    return archive_id


def add_location(
    conn: sqlite3.Connection,
    archive_id: int,
    path: Path | str,
    *,
    is_current: bool = True,
    file_size: int = 4096,
    mtime: int = 111,
) -> None:
    conn.execute(
        """
        INSERT INTO file_locations
            (archive_id, path, is_current, file_size, modified_time_ns)
        VALUES (?, ?, ?, ?, ?)
        """,
        (archive_id, str(path), 1 if is_current else 0, file_size, mtime),
    )


def add_signature(
    conn: sqlite3.Connection,
    archive_id: int,
    *,
    page_count: int = 2,
    file_size: int = 4096,
    mtime: int = 111,
) -> None:
    conn.execute(
        """
        INSERT INTO archive_content_signatures (
            archive_id, algorithm, algorithm_version, digest, page_count,
            image_bytes, source_file_size, source_modified_time_ns
        )
        VALUES (?, 'ordered-page', '1', ?, ?, 1, ?, ?)
        """,
        (archive_id, f"digest-{archive_id}", page_count, file_size, mtime),
    )


def add_pages(
    conn: sqlite3.Connection,
    archive_id: int,
    count: int,
    *,
    hashed: int = 0,
) -> None:
    """Add `count` pages, the first `hashed` of them fully covered."""
    for index in range(count):
        cursor = conn.execute(
            """
            INSERT INTO archive_pages (
                archive_id, page_index, entry_name, entry_size,
                compressed_size, crc32, width, height
            )
            VALUES (?, ?, ?, 10, 10, 0, ?, ?)
            """,
            (
                archive_id,
                index,
                f"{index}.jpg",
                100 if index < hashed else None,
                100 if index < hashed else None,
            ),
        )
        if index < hashed:
            page_id = cursor.lastrowid
            for algorithm, version in (
                (DHASH_ALGORITHM, DHASH_ALGORITHM_VERSION),
                (PHASH_ALGORITHM, PHASH_ALGORITHM_VERSION),
            ):
                conn.execute(
                    """
                    INSERT INTO page_hashes
                        (page_id, algorithm, algorithm_version, digest,
                         bytes_read)
                    VALUES (?, ?, ?, ?, 1)
                    """,
                    (page_id, algorithm, version, f"h{page_id}{algorithm}"),
                )


def add_job(
    conn: sqlite3.Connection,
    archive_id: int,
    job_type: str,
    status: str,
    *,
    ended_at: str | None = None,
) -> int:
    """Add one job.

    `ended_at` sets the terminal timestamp -- `completed_at` for a completed
    job, `cancelled_at` for a cancelled one, matching migration 011's rule
    that a cancelled job did not complete and so leaves `completed_at` NULL.
    Chronology tests set it explicitly rather than relying on insertion order.
    """
    column = {
        "completed": "completed_at",
        "failed": "completed_at",
        "cancelled": "cancelled_at",
    }.get(status)

    if ended_at is not None and column is not None:
        cursor = conn.execute(
            f"INSERT INTO jobs (job_type, archive_id, status, {column}) "
            "VALUES (?, ?, ?, ?)",
            (job_type, archive_id, status, ended_at),
        )
    else:
        cursor = conn.execute(
            "INSERT INTO jobs (job_type, archive_id, status) VALUES (?, ?, ?)",
            (job_type, archive_id, status),
        )

    return int(cursor.lastrowid)


def quarantine(
    conn: sqlite3.Connection,
    archive_id: int,
    status: str = "pending_redownload",
) -> None:
    conn.execute(
        """
        INSERT INTO archive_quarantine
            (archive_id, source_path, quarantine_path, failure_category,
             status)
        VALUES (?, 'X:\\lib\\a.cbz', 'X:\\q\\a.cbz', 'corrupt_archive', ?)
        """,
        (archive_id, status),
    )


def only(result: C.ClassificationResult, archive_id: int):
    return next(a for a in result.archives if a.archive_id == archive_id)


# --- every identity appears exactly once ---------------------------------


def test_every_archive_appears_exactly_once(connection) -> None:
    for archive_id in range(1, 6):
        add_archive(connection, archive_id)

    result = C.classify(connection)
    ids = [archive.archive_id for archive in result.archives]

    assert ids == sorted(ids)
    assert len(ids) == len(set(ids)) == 5


def test_zero_page_archives_are_included(connection) -> None:
    """The 1,256-archive blind spot: no pages, so invisible to a page audit."""
    add_archive(connection, 1)

    result = C.classify(connection)

    assert len(result) == 1
    assert only(result, 1).inventory == C.INVENTORY_NOT_INVENTORIED
    assert only(result, 1).total_pages == 0


def test_axis_totals_each_sum_to_the_archive_count(connection) -> None:
    for archive_id in range(1, 8):
        add_archive(connection, archive_id)

    result = C.classify(connection)
    totals = C.axis_totals(result)

    for axis in C.AXES:
        assert sum(totals[axis].values()) == len(result), axis


def test_every_axis_value_is_reported_even_at_zero(connection) -> None:
    add_archive(connection, 1)

    totals = C.axis_totals(C.classify(connection))

    assert set(totals["availability"]) == set(C.AVAILABILITIES)
    assert set(totals["selection"]) == set(C.SELECTIONS)
    assert set(totals["not_inventoried_subreason"]) == set(
        C.NOT_INVENTORIED_SUBREASONS
    )


# --- disposition ---------------------------------------------------------


def test_disposition_defaults_to_active(connection) -> None:
    add_archive(connection, 1)

    assert only(C.classify(connection), 1).disposition == C.DISPOSITION_ACTIVE


def test_retired_and_superseded_are_reported(connection) -> None:
    for archive_id in (1, 2, 3):
        add_archive(connection, archive_id)

    disposition.retire(connection, 1, reason="out of scope", evidence="proof")
    disposition.supersede(connection, 2, 3, reason="moved", evidence="sha256")

    result = C.classify(connection)

    assert only(result, 1).disposition == C.DISPOSITION_RETIRED
    assert only(result, 2).disposition == C.DISPOSITION_SUPERSEDED
    assert only(result, 2).successor_archive_id == 3
    assert only(result, 3).disposition == C.DISPOSITION_ACTIVE


def test_a_filesystem_observation_creates_no_disposition(
    connection, tmp_path: Path
) -> None:
    """The 2026-07-28 lesson, as an assertion.

    An archive whose file has vanished is `missing` on the availability axis
    and `active` on the disposition axis. Absence is an observation; scope is
    a decision, and only an operator makes one.
    """
    root = tmp_path / "lib"
    root.mkdir()
    add_archive(connection, 1)
    add_location(connection, 1, root / "gone.cbz")

    result = C.classify(connection, scope=[str(root)])

    assert only(result, 1).availability == C.AVAILABILITY_MISSING
    assert only(result, 1).disposition == C.DISPOSITION_ACTIVE
    assert connection.execute(
        "SELECT COUNT(*) FROM archive_retirements"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM archive_supersessions"
    ).fetchone()[0] == 0


# --- availability --------------------------------------------------------


def test_availability_without_a_declared_scope_is_not_observed(
    connection, tmp_path: Path
) -> None:
    add_archive(connection, 1)
    add_location(connection, 1, tmp_path / "a.cbz")

    result = C.classify(connection)

    assert result.filesystem_consulted is False
    assert only(result, 1).availability == C.AVAILABILITY_NOT_OBSERVED


def test_present_matching_and_drifted_are_distinguished(
    connection, tmp_path: Path
) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    matching = root / "matching.cbz"
    matching.write_bytes(b"x" * 10)
    drifted = root / "drifted.cbz"
    drifted.write_bytes(b"x" * 10)

    add_archive(connection, 1)
    add_location(
        connection, 1, matching,
        file_size=10, mtime=matching.stat().st_mtime_ns,
    )
    add_archive(connection, 2)
    add_location(connection, 2, drifted, file_size=999, mtime=1)

    result = C.classify(connection, scope=[str(root)])

    assert only(result, 1).availability == C.AVAILABILITY_PRESENT_MATCHING
    assert only(result, 2).availability == C.AVAILABILITY_PRESENT_DRIFTED


def test_a_directory_is_non_regular_not_present(
    connection, tmp_path: Path
) -> None:
    root = tmp_path / "lib"
    (root / "a.cbz").mkdir(parents=True)

    add_archive(connection, 1)
    add_location(connection, 1, root / "a.cbz")

    result = C.classify(connection, scope=[str(root)])

    assert only(result, 1).availability == C.AVAILABILITY_NON_REGULAR


def test_an_unreadable_path_is_not_reported_as_missing(
    connection, tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    archive = root / "a.cbz"
    archive.write_bytes(b"x")

    add_archive(connection, 1)
    add_location(connection, 1, archive)

    def explode(path, *args, **kwargs):
        raise PermissionError(13, "denied")

    # The module-level seam, not os.stat: patching os.stat globally would
    # also break os.path.isdir and make the declared root look unreachable,
    # so the test would pass for the wrong reason.
    monkeypatch.setattr(C, "_stat", explode)

    result = C.classify(connection, scope=[str(root)])

    assert only(result, 1).availability == C.AVAILABILITY_UNREADABLE


def test_an_unavailable_root_is_never_called_missing(
    connection, tmp_path: Path
) -> None:
    """The Horrorsplat case: 95 current locations under a vanished folder.

    Calling this "missing" turns an observation about this machine into a
    claim about the content, which the census showed had already happened
    once -- "775 missing" silently included 95 unreachable rows.
    """
    absent_root = tmp_path / "not-mounted"

    add_archive(connection, 1)
    add_location(connection, 1, absent_root / "series" / "a.cbz")

    result = C.classify(connection, scope=[str(absent_root)])
    archive = only(result, 1)

    assert archive.availability == C.AVAILABILITY_UNAVAILABLE_SCOPE
    assert archive.availability != C.AVAILABILITY_MISSING
    assert "missing" not in (archive.availability_detail or "").lower()
    assert "gone" not in (archive.availability_detail or "").lower()


def test_a_path_outside_every_declared_root_is_undeclared_scope(
    connection, tmp_path: Path
) -> None:
    declared = tmp_path / "declared"
    declared.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "a.cbz").write_bytes(b"x")

    add_archive(connection, 1)
    add_location(connection, 1, elsewhere / "a.cbz")

    result = C.classify(connection, scope=[str(declared)])

    assert only(result, 1).availability == C.AVAILABILITY_UNDECLARED_SCOPE


def test_no_and_multiple_current_locations_are_distinguished(
    connection, tmp_path: Path
) -> None:
    add_archive(connection, 1)
    add_archive(connection, 2)
    add_location(connection, 2, tmp_path / "one.cbz")
    add_location(connection, 2, tmp_path / "two.cbz")

    result = C.classify(connection, scope=[str(tmp_path)])

    assert only(result, 1).availability == C.AVAILABILITY_NO_CURRENT_LOCATION
    assert (
        only(result, 2).availability
        == C.AVAILABILITY_MULTIPLE_CURRENT_LOCATIONS
    )


def test_scope_digest_changes_with_the_declared_set(tmp_path: Path) -> None:
    one = C.DeclaredScope.declare([str(tmp_path)])
    two = C.DeclaredScope.declare([str(tmp_path), str(tmp_path / "other")])

    assert one.digest != two.digest
    assert C.DeclaredScope.declare(None).consulted is False


# --- inventory sub-reasons -----------------------------------------------


@pytest.mark.parametrize(
    "status, expected",
    [
        ("pending", C.NOT_INVENTORIED_INSPECTION_ACTIVE),
        ("claimed", C.NOT_INVENTORIED_INSPECTION_ACTIVE),
        ("running", C.NOT_INVENTORIED_INSPECTION_ACTIVE),
        ("failed", C.NOT_INVENTORIED_INSPECTION_FAILED),
        ("cancelled", C.NOT_INVENTORIED_INSPECTION_CANCELLED),
        ("completed", C.NOT_INVENTORIED_COMPLETED_NO_IMAGES),
    ],
)
def test_not_inventoried_subreason_follows_the_inspection(
    connection, status: str, expected: str
) -> None:
    add_archive(connection, 1)
    add_job(connection, 1, C.INSPECT_JOB_TYPE, status)

    assert only(C.classify(connection), 1).not_inventoried_subreason == expected


def test_completed_inspection_with_a_signature_promising_pages_is_absent(
    connection,
) -> None:
    """Data-loss shape, kept apart from the ordinary one.

    Inspection completed and the content signature promises 4 pages, yet
    archive_pages holds none. Something lost the inventory. Reporting that as
    "no images" would hide it behind the benign case.
    """
    add_archive(connection, 1)
    add_signature(connection, 1, page_count=4)
    add_job(connection, 1, C.INSPECT_JOB_TYPE, "completed")

    assert (
        only(C.classify(connection), 1).not_inventoried_subreason
        == C.NOT_INVENTORIED_COMPLETED_INVENTORY_ABSENT
    )


def test_completed_inspection_with_no_promised_pages_is_no_images(
    connection,
) -> None:
    add_archive(connection, 1)
    add_signature(connection, 1, page_count=0)
    add_job(connection, 1, C.INSPECT_JOB_TYPE, "completed")

    assert (
        only(C.classify(connection), 1).not_inventoried_subreason
        == C.NOT_INVENTORIED_COMPLETED_NO_IMAGES
    )


def test_every_not_inventoried_subreason_is_reachable(connection) -> None:
    """A vocabulary value nothing can produce is a promise the code breaks.

    NOT_INVENTORIED_COMPLETED_INVENTORY_ABSENT was exactly that until the
    signature page count was wired into the sub-reason.
    """
    reachable = {
        C.NOT_INVENTORIED_INSPECTION_NEVER_ENQUEUED,
        C.NOT_INVENTORIED_INSPECTION_ACTIVE,
        C.NOT_INVENTORIED_INSPECTION_FAILED,
        C.NOT_INVENTORIED_INSPECTION_CANCELLED,
        C.NOT_INVENTORIED_COMPLETED_NO_IMAGES,
        C.NOT_INVENTORIED_COMPLETED_INVENTORY_ABSENT,
        C.NOT_INVENTORIED_QUARANTINE_PENDING_REDOWNLOAD,
    }
    produced = set()

    # 1 never enqueued; 2-5 one inspection status each; 6 signature promises
    # pages; 7 quarantined.
    add_archive(connection, 1)

    for archive_id, status in (
        (2, "running"), (3, "failed"), (4, "cancelled"), (5, "completed"),
    ):
        add_archive(connection, archive_id)
        add_job(connection, archive_id, C.INSPECT_JOB_TYPE, status)

    add_archive(connection, 6)
    add_signature(connection, 6, page_count=3)
    add_job(connection, 6, C.INSPECT_JOB_TYPE, "completed")

    add_archive(connection, 7)
    quarantine(connection, 7)

    for archive in C.classify(connection).archives:
        produced.add(archive.not_inventoried_subreason)

    assert reachable <= produced
    # unknown_residue is the only value with no constructible example, which
    # is correct: it exists for a job state nobody has invented yet.
    assert C.NOT_INVENTORIED_UNKNOWN not in produced


def test_never_inspected_is_its_own_subreason(connection) -> None:
    add_archive(connection, 1)

    assert (
        only(C.classify(connection), 1).not_inventoried_subreason
        == C.NOT_INVENTORIED_INSPECTION_NEVER_ENQUEUED
    )


def test_quarantine_outranks_the_inspection_subreason(connection) -> None:
    """`pending_redownload` says an operator already looked at this."""
    add_archive(connection, 1)
    add_job(connection, 1, C.INSPECT_JOB_TYPE, "failed")
    quarantine(connection, 1)

    archive = only(C.classify(connection), 1)

    assert (
        archive.not_inventoried_subreason
        == C.NOT_INVENTORIED_QUARANTINE_PENDING_REDOWNLOAD
    )
    assert archive.quarantine_status == "pending_redownload"


def test_quarantine_is_never_a_disposition(connection) -> None:
    """In-scope-and-broken is not out-of-scope.

    Folding quarantine into disposition would quietly remove archives from
    operational scope on the strength of an intention to fetch them back.
    """
    add_archive(connection, 1)
    quarantine(connection, 1)

    archive = only(C.classify(connection), 1)

    assert archive.disposition == C.DISPOSITION_ACTIVE
    assert archive.quarantine_status == "pending_redownload"
    assert C.axis_totals(C.classify(connection))["quarantine_status"] == {
        "pending_redownload": 1
    }


def test_covered_and_incomplete_inventories_are_distinguished(
    connection,
) -> None:
    add_archive(connection, 1)
    add_pages(connection, 1, 3, hashed=3)
    add_archive(connection, 2)
    add_pages(connection, 2, 3, hashed=1)

    result = C.classify(connection)

    assert only(result, 1).inventory == C.INVENTORY_COVERED
    assert only(result, 1).outstanding_pages == 0
    assert only(result, 2).inventory == C.INVENTORY_INCOMPLETE
    assert only(result, 2).outstanding_pages == 2


# --- perceptual work state ----------------------------------------------


@pytest.mark.parametrize(
    "status, expected",
    [
        ("pending", C.WORK_ACTIVE),
        ("claimed", C.WORK_ACTIVE),
        ("running", C.WORK_ACTIVE),
        ("completed", C.WORK_COMPLETED),
        ("failed", C.WORK_FAILED),
        ("cancelled", C.WORK_CANCELLED),
    ],
)
def test_every_job_history_stays_distinguishable(
    connection, status: str, expected: str
) -> None:
    add_archive(connection, 1)
    add_job(connection, 1, C.PERCEPTUAL_JOB_TYPE, status)

    assert only(C.classify(connection), 1).perceptual_work == expected


@pytest.mark.parametrize(
    "first, second, expected",
    [
        # The latest terminal job decides. Reducing jobs to status counts
        # threw chronology away, so failed-only, failed-then-completed and
        # completed-then-failed all came out `failed` -- and an operator was
        # sent to fix work that had already been redone.
        (("failed", "2026-01-01"), ("completed", "2026-02-01"),
         C.WORK_COMPLETED),
        (("completed", "2026-01-01"), ("failed", "2026-02-01"),
         C.WORK_FAILED),
        (("cancelled", "2026-01-01"), ("completed", "2026-02-01"),
         C.WORK_COMPLETED),
        (("completed", "2026-01-01"), ("cancelled", "2026-02-01"),
         C.WORK_CANCELLED),
    ],
)
def test_the_latest_terminal_job_decides_the_current_state(
    connection,
    first: tuple[str, str],
    second: tuple[str, str],
    expected: str,
) -> None:
    """Both orders, with explicit timestamps rather than insertion order."""
    add_archive(connection, 1)
    add_archive(connection, 2)

    # Seeded in one order for archive 1 and the reverse for archive 2, so a
    # implementation that silently fell back to row id would disagree with
    # one of them.
    for status, ended in (first, second):
        add_job(connection, 1, C.PERCEPTUAL_JOB_TYPE, status, ended_at=ended)

    for status, ended in (second, first):
        add_job(connection, 2, C.PERCEPTUAL_JOB_TYPE, status, ended_at=ended)

    result = C.classify(connection)

    assert only(result, 1).perceptual_work == expected
    assert only(result, 2).perceptual_work == expected


@pytest.mark.parametrize(
    "statuses, expected",
    [
        (("failed", "running"), C.WORK_ACTIVE),
        (("cancelled", "pending"), C.WORK_ACTIVE),
        (("completed", "claimed"), C.WORK_ACTIVE),
    ],
)
def test_active_work_outranks_any_terminal_history(
    connection, statuses: tuple[str, ...], expected: str
) -> None:
    """Anything in flight outranks history, because it is about to change."""
    add_archive(connection, 1)

    for status in statuses:
        add_job(connection, 1, C.PERCEPTUAL_JOB_TYPE, status)

    assert only(C.classify(connection), 1).perceptual_work == expected


def test_history_survives_beside_the_current_state(connection) -> None:
    """"Ever failed" stays answerable without the scalar state lying.

    An archive that failed and was then reprocessed reads `completed` now --
    which is true -- while `ever_failed` keeps the fact that it once did.
    """
    add_archive(connection, 1)
    add_job(
        connection, 1, C.PERCEPTUAL_JOB_TYPE, "failed", ended_at="2026-01-01"
    )
    add_job(
        connection, 1, C.PERCEPTUAL_JOB_TYPE, "cancelled",
        ended_at="2026-01-15",
    )
    add_job(
        connection, 1, C.PERCEPTUAL_JOB_TYPE, "completed",
        ended_at="2026-02-01",
    )

    archive = only(C.classify(connection), 1)

    assert archive.perceptual_work == C.WORK_COMPLETED
    assert archive.ever_failed is True
    assert archive.ever_cancelled is True


def test_a_failed_inspection_followed_by_a_clean_one_is_not_failed(
    connection,
) -> None:
    """The same chronology rule, applied to the inventory sub-reason.

    An archive whose inspection failed and was then re-run successfully holds
    no images; it is not `inspection_failed`, and reporting it that way sends
    an operator to re-run work that already succeeded.
    """
    add_archive(connection, 1)
    add_job(connection, 1, C.INSPECT_JOB_TYPE, "failed", ended_at="2026-01-01")
    add_job(
        connection, 1, C.INSPECT_JOB_TYPE, "completed", ended_at="2026-02-01"
    )

    assert (
        only(C.classify(connection), 1).not_inventoried_subreason
        == C.NOT_INVENTORIED_COMPLETED_NO_IMAGES
    )


def test_a_running_inspection_outranks_an_earlier_failure(
    connection,
) -> None:
    add_archive(connection, 1)
    add_job(connection, 1, C.INSPECT_JOB_TYPE, "failed", ended_at="2026-01-01")
    add_job(connection, 1, C.INSPECT_JOB_TYPE, "running")

    assert (
        only(C.classify(connection), 1).not_inventoried_subreason
        == C.NOT_INVENTORIED_INSPECTION_ACTIVE
    )


def test_every_blocking_status_is_reported_not_just_the_first(
    connection, tmp_path: Path
) -> None:
    """A failed job and a running one at once are two reasons, not one.

    `ORDER BY j.id LIMIT 1` returned whichever happened to be written first,
    which hid the more actionable of the two roughly half the time and broke
    the promise that an archive keeps every reason that applies.
    """
    add_archive(connection, 1)
    add_location(connection, 1, tmp_path / "a.cbz")
    add_signature(connection, 1)
    add_pages(connection, 1, 2, hashed=0)
    add_job(connection, 1, C.PERCEPTUAL_JOB_TYPE, "failed", ended_at="2026-01")
    add_job(connection, 1, C.PERCEPTUAL_JOB_TYPE, "running")

    reasons = only(C.classify(connection), 1).selection_reasons

    assert f"{EXCLUSION_BLOCKING_JOB}:failed" in reasons
    assert f"{EXCLUSION_BLOCKING_JOB}:running" in reasons


def test_the_45217_shape_is_cancelled_and_retired_at_once(
    connection, tmp_path: Path
) -> None:
    """Production's one retirement, reproduced.

    Archive 45217 has a cancelled perceptual job, a retirement recorded
    against the archive, a page inventory, and a file that is gone. Every one
    of those is on a different axis and all four survive -- which is the
    entire reason the axes are independent.
    """
    root = tmp_path / "lib"
    root.mkdir()

    add_archive(connection, 45217)
    add_location(connection, 45217, root / "gone.cbz")
    add_signature(connection, 45217, page_count=51)
    add_pages(connection, 45217, 3, hashed=0)
    add_job(connection, 45217, C.PERCEPTUAL_JOB_TYPE, "cancelled")
    disposition.retire(
        connection,
        45217,
        reason="deduplicated; content survives elsewhere",
        evidence="ordered-page signature held by archive 45213",
    )

    archive = only(C.classify(connection, scope=[str(root)]), 45217)

    assert archive.disposition == C.DISPOSITION_RETIRED
    assert archive.perceptual_work == C.WORK_CANCELLED
    assert archive.inventory == C.INVENTORY_INCOMPLETE
    assert archive.availability == C.AVAILABILITY_MISSING
    assert archive.selection == C.SELECTION_REFUSED
    assert ARCHIVE_RETIRED in archive.selection_reasons
    # The cancellation is not swallowed by the retirement, and the retirement
    # is not inferred from the vanished file.
    assert archive.perceptual_work != C.WORK_NEVER_ENQUEUED


# --- selection -----------------------------------------------------------


def test_an_eligible_archive_is_reported_eligible(
    connection, tmp_path: Path
) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    archive = root / "a.cbz"
    archive.write_bytes(b"x" * 4096)
    mtime = archive.stat().st_mtime_ns

    add_archive(connection, 1)
    add_location(connection, 1, archive, file_size=4096, mtime=mtime)
    add_signature(connection, 1, file_size=4096, mtime=mtime)
    add_pages(connection, 1, 2, hashed=0)

    result = C.classify(connection, scope=[str(root)])

    assert only(result, 1).selection == C.SELECTION_ELIGIBLE
    assert only(result, 1).selection_reasons == ()


def test_refusals_come_from_the_real_selection_path(
    connection, tmp_path: Path
) -> None:
    """Not a reimplementation: these slugs are candidate_selection's."""
    root = tmp_path / "lib"
    root.mkdir()

    for archive_id, present in ((1, False), (2, True), (3, True)):
        path = root / f"{archive_id}.cbz"
        if present:
            path.write_bytes(b"x" * 4096)
        add_archive(connection, archive_id)
        add_location(
            connection, archive_id, path, file_size=4096,
            mtime=path.stat().st_mtime_ns if present else 111,
        )
        add_signature(
            connection, archive_id, file_size=4096,
            mtime=path.stat().st_mtime_ns if present else 111,
        )
        add_pages(connection, archive_id, 2, hashed=0)

    add_archive(connection, 4)
    disposition.retire(connection, 2, reason="r", evidence="e")
    disposition.supersede(connection, 3, 4, reason="r", evidence="e")

    result = C.classify(connection, scope=[str(root)])

    assert only(result, 1).selection_reasons == (PATH_MISSING,)
    assert only(result, 2).selection_reasons == (ARCHIVE_RETIRED,)
    assert only(result, 3).selection_reasons == (ARCHIVE_SUPERSEDED,)
    assert all(
        only(result, i).selection == C.SELECTION_REFUSED for i in (1, 2, 3)
    )


def test_an_archive_can_carry_several_exclusion_reasons(connection) -> None:
    """Reporting one would send a reader after a single fix that isn't there."""
    add_archive(connection, 1)

    reasons = only(C.classify(connection), 1).selection_reasons

    assert set(reasons) == {
        EXCLUSION_NO_CONTENT_SIGNATURE,
        EXCLUSION_NO_CURRENT_LOCATION,
        EXCLUSION_NO_OUTSTANDING_PAGES,
    }


def test_signature_drift_reports_both_halves_separately(
    connection, tmp_path: Path
) -> None:
    add_archive(connection, 1)
    add_location(connection, 1, tmp_path / "a.cbz", file_size=10, mtime=10)
    add_signature(connection, 1, file_size=20, mtime=20)
    add_pages(connection, 1, 2, hashed=0)

    reasons = only(C.classify(connection), 1).selection_reasons

    assert EXCLUSION_SIGNATURE_SIZE_MISMATCH in reasons
    assert EXCLUSION_SIGNATURE_MTIME_MISMATCH in reasons


def test_a_blocking_job_reason_carries_its_status(
    connection, tmp_path: Path
) -> None:
    """"A worker is on it" and "it failed permanently" are opposites."""
    for archive_id, status in ((1, "running"), (2, "failed")):
        add_archive(connection, archive_id)
        add_location(
            connection, archive_id, tmp_path / f"{archive_id}.cbz"
        )
        add_signature(connection, archive_id)
        add_pages(connection, archive_id, 2, hashed=0)
        add_job(connection, archive_id, C.PERCEPTUAL_JOB_TYPE, status)

    result = C.classify(connection)

    assert f"{EXCLUSION_BLOCKING_JOB}:running" in only(
        result, 1
    ).selection_reasons
    assert f"{EXCLUSION_BLOCKING_JOB}:failed" in only(
        result, 2
    ).selection_reasons


def test_zero_page_count_signature_is_its_own_reason(
    connection, tmp_path: Path
) -> None:
    add_archive(connection, 1)
    add_location(connection, 1, tmp_path / "a.cbz")
    add_signature(connection, 1, page_count=0)

    reasons = only(C.classify(connection), 1).selection_reasons

    assert EXCLUSION_SIGNATURE_PAGE_COUNT_ZERO in reasons


def test_multiple_current_locations_is_an_exclusion_reason(
    connection, tmp_path: Path
) -> None:
    add_archive(connection, 1)
    add_location(connection, 1, tmp_path / "one.cbz")
    add_location(connection, 1, tmp_path / "two.cbz")

    reasons = only(C.classify(connection), 1).selection_reasons

    assert EXCLUSION_MULTIPLE_CURRENT_LOCATIONS in reasons


def test_eligible_and_excluded_partition_the_library(
    connection, tmp_path: Path
) -> None:
    """The invariant the whole contract rests on."""
    root = tmp_path / "lib"
    root.mkdir()

    for archive_id in range(1, 12):
        add_archive(connection, archive_id)

    # A deliberately varied population.
    add_location(connection, 2, root / "2.cbz")
    add_signature(connection, 3)
    add_pages(connection, 4, 2, hashed=2)
    add_pages(connection, 5, 2, hashed=0)
    add_job(connection, 6, C.PERCEPTUAL_JOB_TYPE, "failed")
    add_location(connection, 7, root / "7.cbz")
    add_signature(connection, 7)
    add_pages(connection, 7, 1, hashed=0)

    repository = ArchivePerceptualHashRepository(connection)
    eligible = {
        int(row["archive_id"])
        for row in repository._eligible_archive_rows(limit=None)
    }
    excluded = set(repository._archive_exclusion_reasons())
    everyone = {
        int(row[0]) for row in connection.execute("SELECT id FROM archive_files")
    }

    assert eligible & excluded == set()
    assert eligible | excluded == everyone

    result = C.classify(connection, scope=[str(root)])
    totals = C.axis_totals(result)

    assert totals["selection"][C.SELECTION_UNEXPLAINED] == 0


def test_unexplained_is_residue_and_not_a_predicate(
    connection, monkeypatch, tmp_path: Path
) -> None:
    """Nothing assigns `unexplained`; an archive lands there by exclusion.

    Proven by removing an archive's explanation and nothing else: it becomes
    unexplained without any code path having decided that it should.
    """
    add_archive(connection, 1)
    add_archive(connection, 2)

    real = ArchivePerceptualHashRepository._archive_exclusion_reasons

    def forgetful(self):
        reasons = dict(real(self))
        reasons.pop(2, None)
        return reasons

    monkeypatch.setattr(
        ArchivePerceptualHashRepository,
        "_archive_exclusion_reasons",
        forgetful,
    )

    result = C.classify(connection)

    assert only(result, 1).selection == C.SELECTION_EXCLUDED
    assert only(result, 2).selection == C.SELECTION_UNEXPLAINED
    assert only(result, 2).selection_reasons == ()
    assert C.axis_totals(result)["selection"][C.SELECTION_UNEXPLAINED] == 1


def test_without_a_scope_nothing_is_claimed_enqueueable(
    connection, tmp_path: Path
) -> None:
    """A precondition this run could not test must not be asserted either way.

    With no scope declared the filesystem is never consulted, so the archive
    is neither called eligible (nobody looked at the file) nor called
    path_missing (nobody looked at the file). It is refused for the honest
    reason that this run could not observe it.
    """
    add_archive(connection, 1)
    add_location(connection, 1, tmp_path / "gone.cbz")
    add_signature(connection, 1)
    add_pages(connection, 1, 2, hashed=0)

    result = C.classify(connection)

    assert result.filesystem_consulted is False
    assert only(result, 1).selection == C.SELECTION_REFUSED
    assert only(result, 1).selection_reasons == (C.REFUSAL_SCOPE_NOT_OBSERVED,)
    assert PATH_MISSING not in only(result, 1).selection_reasons

    scoped = C.classify(connection, scope=[str(tmp_path)])

    assert scoped.filesystem_consulted is True
    assert only(scoped, 1).selection_reasons == (PATH_MISSING,)


def test_an_eligible_archive_under_an_unreachable_root_is_not_path_missing(
    connection, tmp_path: Path, monkeypatch
) -> None:
    """The contradiction this contract exists to prevent.

    The archive is eligible in every database sense, and its root is not
    mounted. Statting it would raise FileNotFoundError and the refusal would
    read `path_missing` -- turning "I cannot look there" into "the file is
    gone", which is exactly the conflation the census found had already
    happened once.
    """
    absent_root = tmp_path / "not-mounted"

    add_archive(connection, 1)
    add_location(connection, 1, absent_root / "series" / "a.cbz")
    add_signature(connection, 1)
    add_pages(connection, 1, 2, hashed=0)

    def explode(path, *args, **kwargs):
        raise AssertionError(
            "classification must not stat a path under an unreachable root"
        )

    monkeypatch.setattr(C, "_stat", explode)

    archive = only(C.classify(connection, scope=[str(absent_root)]), 1)

    assert archive.availability == C.AVAILABILITY_UNAVAILABLE_SCOPE
    assert archive.selection == C.SELECTION_REFUSED
    assert archive.selection_reasons == (C.REFUSAL_SCOPE_UNAVAILABLE,)
    assert PATH_MISSING not in archive.selection_reasons


def test_an_eligible_archive_outside_every_root_is_not_stat_ed(
    connection, tmp_path: Path, monkeypatch
) -> None:
    declared = tmp_path / "declared"
    declared.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "a.cbz").write_bytes(b"x" * 4096)

    add_archive(connection, 1)
    add_location(connection, 1, elsewhere / "a.cbz")
    add_signature(connection, 1)
    add_pages(connection, 1, 2, hashed=0)

    def explode(path, *args, **kwargs):
        raise AssertionError("must not stat outside the declared scope")

    monkeypatch.setattr(C, "_stat", explode)

    archive = only(C.classify(connection, scope=[str(declared)]), 1)

    assert archive.availability == C.AVAILABILITY_UNDECLARED_SCOPE
    assert archive.selection_reasons == (C.REFUSAL_SCOPE_UNDECLARED,)


def test_a_disposition_still_bites_under_an_unreachable_root(
    connection, tmp_path: Path, monkeypatch
) -> None:
    """Retirement must not depend on what happens to be mounted."""
    absent_root = tmp_path / "not-mounted"

    add_archive(connection, 1)
    add_location(connection, 1, absent_root / "a.cbz")
    add_signature(connection, 1)
    add_pages(connection, 1, 2, hashed=0)
    disposition.retire(connection, 1, reason="r", evidence="e")

    monkeypatch.setattr(
        C, "_stat", lambda *a, **k: pytest.fail("must not stat")
    )

    archive = only(C.classify(connection, scope=[str(absent_root)]), 1)

    assert archive.selection_reasons == (ARCHIVE_RETIRED,)


@pytest.mark.parametrize("reverse", [False, True])
def test_the_most_specific_root_wins_regardless_of_order(
    tmp_path: Path, reverse: bool
) -> None:
    """Nested roots must not resolve by declaration order.

    With `X:\\` and `X:\\Horrorsplat` both declared, first-match made the
    answer depend on how the caller happened to type the list -- so a path
    could be judged against the mounted volume or the vanished folder
    arbitrarily.
    """
    parent = tmp_path / "volume"
    child = parent / "child"
    parent.mkdir()
    # `child` is deliberately not created, so the two roots differ in
    # reachability and the choice between them is observable.
    roots = [str(parent), str(child)]

    scope = C.DeclaredScope.declare(list(reversed(roots)) if reverse else roots)
    found = scope.containing_root(str(child / "series" / "a.cbz"))

    assert found == (str(child), False)

    # A path under the parent but outside the child still resolves to the
    # parent, so the specific root does not swallow its neighbours.
    assert scope.containing_root(str(parent / "other.cbz")) == (
        str(parent), True
    )


def test_multiple_current_locations_do_not_break_classification(
    connection, tmp_path: Path
) -> None:
    """Two matching current rows used to make the contract abort.

    The eligibility predicate joined every current location without requiring
    exactly one, so the archive was eligible *and* carried
    multiple_current_locations -- a contradiction that raised PartitionError
    and took the whole report down with it. The predicate now refuses
    ambiguity, which is what the selection path has always done.
    """
    root = tmp_path / "lib"
    root.mkdir()
    first = root / "one.cbz"
    second = root / "two.cbz"
    first.write_bytes(b"x" * 4096)
    second.write_bytes(b"x" * 4096)
    mtime = first.stat().st_mtime_ns

    add_archive(connection, 1)
    add_location(connection, 1, first, file_size=4096, mtime=mtime)
    add_location(connection, 1, second, file_size=4096, mtime=mtime)
    add_signature(connection, 1, file_size=4096, mtime=mtime)
    add_pages(connection, 1, 2, hashed=0)

    result = C.classify(connection, scope=[str(root)])
    archive = only(result, 1)

    assert archive.availability == C.AVAILABILITY_MULTIPLE_CURRENT_LOCATIONS
    assert archive.selection == C.SELECTION_EXCLUDED
    assert EXCLUSION_MULTIPLE_CURRENT_LOCATIONS in archive.selection_reasons

    repository = ArchivePerceptualHashRepository(connection)
    eligible = {
        int(row["archive_id"])
        for row in repository._eligible_archive_rows(limit=None)
    }

    assert eligible == set()


# --- presentation precedence --------------------------------------------


def test_presentation_precedence_does_not_change_axis_totals(
    connection, tmp_path: Path
) -> None:
    """The label is a convenience; the axes are the data."""
    root = tmp_path / "lib"
    root.mkdir()

    for archive_id in range(1, 7):
        add_archive(connection, archive_id)

    add_location(connection, 2, root / "gone.cbz")
    add_pages(connection, 3, 2, hashed=2)
    add_job(connection, 4, C.PERCEPTUAL_JOB_TYPE, "failed")
    disposition.retire(connection, 5, reason="r", evidence="e")

    result = C.classify(connection, scope=[str(root)])
    baseline = C.axis_totals(result)

    reversed_precedence = tuple(reversed(C.PRESENTATION_PRECEDENCE))
    labels_default = [C.presentation_label(a) for a in result.archives]
    labels_reversed = [
        C.presentation_label(a, reversed_precedence) for a in result.archives
    ]

    assert labels_default != labels_reversed
    assert C.axis_totals(result) == baseline


def test_a_disposition_outranks_every_observation_in_the_label(
    connection, tmp_path: Path
) -> None:
    root = tmp_path / "lib"
    root.mkdir()

    add_archive(connection, 1)
    add_location(connection, 1, root / "gone.cbz")
    disposition.retire(connection, 1, reason="r", evidence="e")

    archive = only(C.classify(connection, scope=[str(root)]), 1)

    assert C.presentation_label(archive) == "disposition:retired"
    # ...while the observation is still recorded on its own axis.
    assert archive.availability == C.AVAILABILITY_MISSING


# --- page accounting -----------------------------------------------------


def test_outstanding_pages_are_attributable_by_axis(connection) -> None:
    add_archive(connection, 1)
    add_pages(connection, 1, 5, hashed=1)
    add_archive(connection, 2)
    add_pages(connection, 2, 3, hashed=3)

    result = C.classify(connection)
    by_inventory = C.outstanding_pages_by_axis(result, "inventory")

    assert by_inventory[C.INVENTORY_INCOMPLETE] == 4
    assert by_inventory.get(C.INVENTORY_COVERED, 0) == 0


# --- bypass proofs: fail closed -----------------------------------------


def test_classification_refuses_a_bypassed_disposition_conflict(
    connection,
) -> None:
    connection.execute(
        "DROP TRIGGER trg_retirement_not_superseded_predecessor"
    )
    add_archive(connection, 1)
    add_archive(connection, 2)
    connection.execute(
        "INSERT INTO archive_supersessions (predecessor_archive_id, "
        "successor_archive_id, reason, evidence) VALUES (1, 2, 'r', 'e')"
    )
    connection.execute(
        "INSERT INTO archive_retirements (archive_id, reason, evidence) "
        "VALUES (1, 'r', 'e')"
    )

    with pytest.raises(C.ClassificationInvariantError):
        C.classify(connection)


def test_classification_refuses_a_bypassed_supersession_cycle(
    connection,
) -> None:
    connection.execute("DROP TRIGGER trg_supersession_no_cycle")
    add_archive(connection, 1)
    add_archive(connection, 2)
    connection.execute(
        "INSERT INTO archive_supersessions (predecessor_archive_id, "
        "successor_archive_id, reason, evidence) VALUES (1, 2, 'r', 'e')"
    )
    connection.execute(
        "INSERT INTO archive_supersessions (predecessor_archive_id, "
        "successor_archive_id, reason, evidence) VALUES (2, 1, 'r', 'e')"
    )

    with pytest.raises(C.ClassificationInvariantError, match="terminate"):
        C.classify(connection)


def test_classification_refuses_a_broken_partition(
    connection, monkeypatch
) -> None:
    """Both eligible and excluded is a contradiction, not a finding."""
    add_archive(connection, 1)

    real = ArchivePerceptualHashRepository._eligible_archive_rows

    def greedy(self, *, limit=None):
        return list(
            self.connection.execute(
                "SELECT id AS archive_id FROM archive_files"
            )
        )

    monkeypatch.setattr(
        ArchivePerceptualHashRepository, "_eligible_archive_rows", greedy
    )

    with pytest.raises(C.PartitionError, match="both eligible and excluded"):
        C.classify(connection)


def test_classification_never_writes(connection, tmp_path: Path) -> None:
    """Read-only in fact: the schema and every row are untouched."""
    root = tmp_path / "lib"
    root.mkdir()
    add_archive(connection, 1)
    add_location(connection, 1, root / "a.cbz")
    add_signature(connection, 1)
    add_pages(connection, 1, 2, hashed=1)
    add_job(connection, 1, C.PERCEPTUAL_JOB_TYPE, "completed")

    def fingerprint():
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
            )
        ] + [
            tuple(row)
            for table in (
                "archive_files", "file_locations", "archive_pages", "jobs",
                "archive_retirements", "archive_supersessions",
                "archive_disposition_events",
            )
            for row in connection.execute(f"SELECT * FROM {table}")
        ]

    before = fingerprint()
    C.classify(connection, scope=[str(root)])

    assert fingerprint() == before
