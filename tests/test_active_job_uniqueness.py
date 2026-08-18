"""Tests for migration 010's partial unique index on active jobs.

Every test here uses a disposable `tmp_path` SQLite database. Nothing in
this file touches a production database, backup, report, or archive
path.

The invariant under test: at most one row with status in
('pending', 'claimed', 'running') may exist per non-null
(job_type, archive_id). Terminal rows ('completed', 'failed',
'cancelled', 'blocked') are outside the index predicate and are never
constrained.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import (
    apply_migrations,
    discover_migrations,
)


MIGRATION_DIRECTORY = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)

INDEX_NAME = "idx_jobs_unique_active"
ACTIVE_STATUSES = ("pending", "claimed", "running")
TERMINAL_STATUSES = ("completed", "failed", "cancelled", "blocked")

ALL_MIGRATIONS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]


def migrated_database(tmp_path: Path, name: str = "jobs.db") -> Path:
    database = tmp_path / name

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)

    return database


def seed_archive(connection: sqlite3.Connection) -> int:
    cursor = connection.execute(
        "INSERT INTO archive_files (file_size) VALUES (?)",
        (1024,),
    )
    return int(cursor.lastrowid)


def insert_job(
    connection: sqlite3.Connection,
    *,
    job_type: str = "inspect_archive",
    status: str = "pending",
    archive_id: int | None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO jobs (job_type, status, archive_id)
        VALUES (?, ?, ?)
        """,
        (job_type, status, archive_id),
    )
    return int(cursor.lastrowid)


def snapshot_jobs(
    connection: sqlite3.Connection,
) -> tuple[list[str], list[tuple]]:
    """Every column of every jobs row, for exact before/after comparison.

    Deliberately `SELECT *` rather than a hand-picked column list: the
    point of the upgrade tests is that migration 010 changes *nothing*
    about existing rows, so the comparison must cover every current
    columns and must also fail if a future migration adds, removes, or
    reorders one. The column names are returned alongside the values so
    a schema change is caught as well as a data change.
    """
    cursor = connection.execute("SELECT * FROM jobs ORDER BY id")
    rows = cursor.fetchall()
    column_names = [description[0] for description in cursor.description]

    return column_names, [tuple(row) for row in rows]


def build_partial_migration_directory(
    tmp_path: Path,
    *,
    through_version: int,
) -> Path:
    """Copy migrations 1..through_version into a scratch directory.

    Used to build a database at an older schema version so migration 10
    can then be applied to it as a genuine upgrade.
    """
    directory = tmp_path / f"migrations_through_{through_version}"
    directory.mkdir()

    for path in discover_migrations(MIGRATION_DIRECTORY):
        version = int(path.stem.split("_", 1)[0])

        if version <= through_version:
            shutil.copy(path, directory / path.name)

    return directory


# --- migration application ------------------------------------------


def test_fresh_database_applies_every_migration(
    tmp_path: Path,
) -> None:
    database = tmp_path / "fresh.db"

    with database_connection(database) as connection:
        applied = apply_migrations(connection, MIGRATION_DIRECTORY)

    assert applied == ALL_MIGRATIONS


def test_reapplying_migrations_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "idempotent.db"

    with database_connection(database) as connection:
        first = apply_migrations(connection, MIGRATION_DIRECTORY)
        second = apply_migrations(connection, MIGRATION_DIRECTORY)
        third = apply_migrations(connection, MIGRATION_DIRECTORY)

        versions = [
            int(row["version"])
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]

    assert first == ALL_MIGRATIONS
    assert second == []
    assert third == []
    assert versions == ALL_MIGRATIONS


def test_partial_unique_index_exists_with_active_status_predicate(
    tmp_path: Path,
) -> None:
    database = migrated_database(tmp_path)

    with database_connection(database) as connection:
        row = connection.execute(
            """
            SELECT sql, "unique", partial
            FROM sqlite_master
            JOIN pragma_index_list('jobs') AS il
              ON il.name = sqlite_master.name
            WHERE sqlite_master.type = 'index'
              AND sqlite_master.name = ?
            """,
            (INDEX_NAME,),
        ).fetchone()

        columns = [
            info["name"]
            for info in connection.execute(
                f"PRAGMA index_info('{INDEX_NAME}')"
            ).fetchall()
        ]

    assert row is not None, f"{INDEX_NAME} was not created"

    # It must be both UNIQUE and partial -- a non-unique index would
    # enforce nothing, and a non-partial one would wrongly constrain
    # terminal history too.
    assert row["unique"] == 1
    assert row["partial"] == 1
    assert columns == ["job_type", "archive_id"]

    normalized = " ".join(row["sql"].split())
    assert "CREATE UNIQUE INDEX" in normalized
    assert "ON jobs(job_type, archive_id)" in normalized
    assert (
        "WHERE status IN ('pending', 'claimed', 'running')"
        in normalized
    )
    # The predicate must not accidentally include a terminal status.
    for terminal in TERMINAL_STATUSES:
        assert terminal not in normalized


# --- rejected: duplicate active rows ---------------------------------


def test_two_pending_jobs_for_same_identity_are_rejected(
    tmp_path: Path,
) -> None:
    database = migrated_database(tmp_path)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)
        insert_job(connection, status="pending", archive_id=archive_id)

        with pytest.raises(sqlite3.IntegrityError):
            insert_job(
                connection,
                status="pending",
                archive_id=archive_id,
            )


@pytest.mark.parametrize(
    ("first_status", "second_status"),
    [
        ("pending", "pending"),
        ("pending", "claimed"),
        ("pending", "running"),
        ("claimed", "claimed"),
        ("claimed", "running"),
        ("running", "running"),
    ],
)
def test_every_active_status_pairing_is_rejected(
    tmp_path: Path,
    first_status: str,
    second_status: str,
) -> None:
    database = tmp_path / f"{first_status}_{second_status}.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        archive_id = seed_archive(connection)
        insert_job(
            connection,
            status=first_status,
            archive_id=archive_id,
        )

        with pytest.raises(sqlite3.IntegrityError):
            insert_job(
                connection,
                status=second_status,
                archive_id=archive_id,
            )

        # The rejected insert must not have landed.
        remaining = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE archive_id = ?",
            (archive_id,),
        ).fetchone()[0]

    assert remaining == 1


# --- allowed: distinct identities ------------------------------------


def test_different_job_types_for_same_archive_are_allowed(
    tmp_path: Path,
) -> None:
    database = migrated_database(tmp_path)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)

        for job_type in (
            "inspect_archive",
            "calculate_archive_hash",
            "hash_archive_pages",
            "hash_archive_pages_perceptual",
        ):
            insert_job(
                connection,
                job_type=job_type,
                status="pending",
                archive_id=archive_id,
            )

        count = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE archive_id = ?",
            (archive_id,),
        ).fetchone()[0]

    assert count == 4


def test_same_job_type_for_different_archives_is_allowed(
    tmp_path: Path,
) -> None:
    database = migrated_database(tmp_path)

    with database_connection(database) as connection:
        first_archive = seed_archive(connection)
        second_archive = seed_archive(connection)

        insert_job(connection, archive_id=first_archive)
        insert_job(connection, archive_id=second_archive)

        count = connection.execute(
            "SELECT COUNT(*) FROM jobs"
        ).fetchone()[0]

    assert count == 2


# --- allowed: terminal history ---------------------------------------


def test_multiple_terminal_rows_for_same_identity_are_allowed(
    tmp_path: Path,
) -> None:
    database = migrated_database(tmp_path)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)

        # Two of every terminal status for the same identity.
        for status in TERMINAL_STATUSES:
            insert_job(
                connection,
                status=status,
                archive_id=archive_id,
            )
            insert_job(
                connection,
                status=status,
                archive_id=archive_id,
            )

        count = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE archive_id = ?",
            (archive_id,),
        ).fetchone()[0]

    assert count == len(TERMINAL_STATUSES) * 2


def test_one_active_row_alongside_terminal_history_is_allowed(
    tmp_path: Path,
) -> None:
    database = migrated_database(tmp_path)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)

        for status in TERMINAL_STATUSES:
            insert_job(
                connection,
                status=status,
                archive_id=archive_id,
            )

        active_id = insert_job(
            connection,
            status="running",
            archive_id=archive_id,
        )

        # ...but still only one active row.
        with pytest.raises(sqlite3.IntegrityError):
            insert_job(
                connection,
                status="pending",
                archive_id=archive_id,
            )

        active_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM jobs
            WHERE archive_id = ?
              AND status IN ('pending', 'claimed', 'running')
            """,
            (archive_id,),
        ).fetchone()[0]

    assert active_id > 0
    assert active_count == 1


def test_new_active_row_allowed_after_previous_becomes_terminal(
    tmp_path: Path,
) -> None:
    database = migrated_database(tmp_path)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)
        first_id = insert_job(
            connection,
            status="running",
            archive_id=archive_id,
        )

        # Blocked while the first row is still active...
        with pytest.raises(sqlite3.IntegrityError):
            insert_job(
                connection,
                status="pending",
                archive_id=archive_id,
            )

        # ...and allowed once it reaches a terminal status. Both the
        # 'completed' and 'failed' terminal paths are exercised, since
        # the two differ in caller-level re-enqueue policy (out of
        # scope for this index, which treats them identically).
        for terminal in ("completed", "failed"):
            connection.execute(
                "UPDATE jobs SET status = ? WHERE id = ?",
                (terminal, first_id),
            )
            new_id = insert_job(
                connection,
                status="pending",
                archive_id=archive_id,
            )
            connection.execute(
                "UPDATE jobs SET status = 'completed' WHERE id = ?",
                (new_id,),
            )
            first_id = new_id

        total = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE archive_id = ?",
            (archive_id,),
        ).fetchone()[0]

    assert total == 3


# --- documented limitation: NULL archive_id ---------------------------


def test_null_archive_id_active_rows_remain_allowed(
    tmp_path: Path,
) -> None:
    """Documents a KNOWN LIMITATION, it does not assert a fix.

    SQL treats every NULL as distinct for uniqueness purposes, so the
    partial unique index does not deduplicate active jobs whose
    archive_id IS NULL. No production job type currently enqueues with
    a NULL archive_id (see docs/job_enqueue_idempotency_audit.md); this
    test exists so the gap is visible and fails loudly if a future
    change is expected to close it.
    """
    database = migrated_database(tmp_path)

    with database_connection(database) as connection:
        insert_job(connection, status="pending", archive_id=None)
        insert_job(connection, status="pending", archive_id=None)
        insert_job(connection, status="running", archive_id=None)

        null_active = connection.execute(
            """
            SELECT COUNT(*)
            FROM jobs
            WHERE archive_id IS NULL
              AND status IN ('pending', 'claimed', 'running')
            """
        ).fetchone()[0]

    # Three active NULL-archive rows coexist: the index does not and
    # cannot constrain them.
    assert null_active == 3


# --- upgrade from an older database ----------------------------------


def test_upgrade_from_migrations_one_through_nine_preserves_rows(
    tmp_path: Path,
) -> None:
    database = tmp_path / "upgrade.db"
    older = build_partial_migration_directory(
        tmp_path,
        through_version=9,
    )

    with database_connection(database) as connection:
        applied = apply_migrations(connection, older)
        archive_id = seed_archive(connection)

        # A valid mix: one active row plus terminal history.
        active_id = insert_job(
            connection,
            status="running",
            archive_id=archive_id,
        )
        completed_id = insert_job(
            connection,
            status="completed",
            archive_id=archive_id,
        )
        failed_id = insert_job(
            connection,
            status="failed",
            archive_id=archive_id,
        )
        before_columns, before_rows = snapshot_jobs(connection)

    assert applied == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert len(before_rows) == 3

    with database_connection(database) as connection:
        upgraded = apply_migrations(connection, MIGRATION_DIRECTORY)

        after_columns, after_rows = snapshot_jobs(connection)

        index_exists = connection.execute(
            """
            SELECT COUNT(*)
            FROM sqlite_master
            WHERE type = 'index' AND name = ?
            """,
            (INDEX_NAME,),
        ).fetchone()[0]

    assert upgraded == [10, 11]
    assert index_exists == 1
    # Column-for-column comparison across every column of every row.
    # Migration 010 adds an index and 011 adds two nullable columns; neither
    # may alter an existing row. The new columns land at the end holding NULL
    # for every row written before they existed, which is what "nothing was
    # cancelled" looks like.
    assert before_columns == after_columns[:17]
    assert after_columns[17:] == ["cancelled_at", "cancellation_reason"]
    assert len(after_columns) == 19
    assert after_rows == [row + (None, None) for row in before_rows]
    assert {active_id, completed_id, failed_id} == {
        row[0] for row in after_rows
    }


def test_upgrade_fails_when_duplicate_active_rows_exist(
    tmp_path: Path,
) -> None:
    """A pre-existing duplicate must block migration 10 atomically.

    The migration must not be recorded, the index must not be created,
    and -- critically -- neither duplicate row may be deleted,
    cancelled, merged, or rewritten. Resolving the duplicate is a
    human decision, not something the migration performs. The
    before/after comparison covers every column of every row
    (`SELECT *`), so a change to any field, not just the few a
    hand-picked column list would cover, fails this test.
    """
    database = tmp_path / "duplicates.db"
    older = build_partial_migration_directory(
        tmp_path,
        through_version=9,
    )

    with database_connection(database) as connection:
        apply_migrations(connection, older)
        archive_id = seed_archive(connection)

        first_id = insert_job(
            connection,
            status="pending",
            archive_id=archive_id,
        )
        second_id = insert_job(
            connection,
            status="running",
            archive_id=archive_id,
        )
        before_columns, before_rows = snapshot_jobs(connection)

    assert first_id != second_id
    assert len(before_rows) == 2

    with database_connection(database) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            apply_migrations(connection, MIGRATION_DIRECTORY)

    with database_connection(database) as connection:
        versions = [
            int(row["version"])
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]

        index_exists = connection.execute(
            """
            SELECT COUNT(*)
            FROM sqlite_master
            WHERE type = 'index' AND name = ?
            """,
            (INDEX_NAME,),
        ).fetchone()[0]

        after_columns, after_rows = snapshot_jobs(connection)

    # Migration 10 was not recorded...
    assert 10 not in versions
    # ...migrations 1-9 remain intact...
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    # ...the index was not created...
    assert index_exists == 0
    # ...and both duplicate rows are untouched, compared
    # column-for-column across every column of the jobs table (not a
    # hand-picked subset). "Column-for-column" rather than
    # "byte-for-byte": SQLite may represent the same logical row
    # differently on disk, so the meaningful guarantee is that every
    # returned value is identical, not that the file bytes are.
    assert after_columns == before_columns
    assert len(after_columns) == 17
    assert after_rows == before_rows
    assert len(after_rows) == 2


# --- unrelated integrity errors stay unrelated ------------------------


def test_unrelated_integrity_errors_are_not_duplicate_job_errors(
    tmp_path: Path,
) -> None:
    """A FOREIGN KEY violation must stay a FOREIGN KEY violation.

    Guards against a future `enqueue_if_absent()` (not implemented in
    this change) conflating any IntegrityError with "a duplicate active
    job already exists".
    """
    database = migrated_database(tmp_path)

    with database_connection(database) as connection:
        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            insert_job(connection, archive_id=999_999)

        message = str(exc_info.value).lower()

        # Distinctly a foreign-key failure, not a uniqueness failure.
        assert "foreign key" in message
        assert "unique" not in message

        # A genuine duplicate-active violation looks different.
        archive_id = seed_archive(connection)
        insert_job(connection, archive_id=archive_id)

        with pytest.raises(sqlite3.IntegrityError) as duplicate_info:
            insert_job(connection, archive_id=archive_id)

        duplicate_message = str(duplicate_info.value).lower()

    assert "unique" in duplicate_message
    assert "jobs.job_type" in duplicate_message
    assert "jobs.archive_id" in duplicate_message


# --- concurrency: the database, not an app-level pre-check ------------


def test_second_connection_cannot_insert_duplicate_active_job(
    tmp_path: Path,
) -> None:
    """Two real connections to the same file, no mocks, no pre-check.

    Neither connection performs any application-level "does an active
    job already exist" query -- each simply inserts. The second insert
    must fail because the database itself rejects it.
    """
    database = tmp_path / "concurrent.db"

    with database_connection(database) as setup:
        apply_migrations(setup, MIGRATION_DIRECTORY)
        archive_id = seed_archive(setup)

    with (
        database_connection(database) as first,
        database_connection(database) as second,
    ):
        first_id = insert_job(first, archive_id=archive_id)

        with pytest.raises(sqlite3.IntegrityError):
            insert_job(second, archive_id=archive_id)

        # The second connection may still enqueue a *different* job
        # type for the same archive -- the constraint is scoped to
        # (job_type, archive_id), not to the archive alone.
        other_id = insert_job(
            second,
            job_type="calculate_archive_hash",
            archive_id=archive_id,
        )

        rows = second.execute(
            """
            SELECT id, job_type
            FROM jobs
            WHERE archive_id = ?
            ORDER BY id
            """,
            (archive_id,),
        ).fetchall()

    assert [tuple(row) for row in rows] == [
        (first_id, "inspect_archive"),
        (other_id, "calculate_archive_hash"),
    ]


def test_concurrent_interleaved_check_then_insert_is_blocked(
    tmp_path: Path,
) -> None:
    """Reproduces the audited check-then-insert race, then proves the
    index closes it.

    This is the exact interleaving from
    docs/job_enqueue_idempotency_audit.md: connection B runs its
    "is there an active job?" check and sees none; connection A then
    inserts and commits; connection B, acting on its now-stale check,
    inserts. Before migration 010 both inserts succeed. With the index
    in place, B's insert is rejected by the database even though B's
    own application-level check said it was safe.
    """
    database = tmp_path / "interleaved.db"

    with database_connection(database) as setup:
        apply_migrations(setup, MIGRATION_DIRECTORY)
        archive_id = seed_archive(setup)

    with (
        database_connection(database) as connection_a,
        database_connection(database) as connection_b,
    ):
        # 1. B checks and sees no active job.
        b_saw = connection_b.execute(
            """
            SELECT id
            FROM jobs
            WHERE archive_id = ?
              AND job_type = 'inspect_archive'
              AND status IN ('pending', 'claimed', 'running')
            LIMIT 1
            """,
            (archive_id,),
        ).fetchone()

        assert b_saw is None

        # 2. A inserts and commits.
        insert_job(connection_a, archive_id=archive_id)

        # 3. B acts on its stale check -- and is stopped by the index,
        #    not by any application-level guard.
        with pytest.raises(sqlite3.IntegrityError):
            insert_job(connection_b, archive_id=archive_id)

        active_count = connection_b.execute(
            """
            SELECT COUNT(*)
            FROM jobs
            WHERE archive_id = ?
              AND status IN ('pending', 'claimed', 'running')
            """,
            (archive_id,),
        ).fetchone()[0]

    assert active_count == 1
