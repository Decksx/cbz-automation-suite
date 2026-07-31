from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from comic_automation.database.connection import (
    connect_database,
    database_connection,
)
from comic_automation.database.migrations import apply_migrations
from comic_automation.jobs import (
    InvalidJobTransitionError,
    JobQueue,
)
from comic_automation.jobs.abandoned_job_audit import (
    WORKER_LIVENESS_WARNING,
    OutputPathCollisionError,
)
from comic_automation.jobs.abandoned_job_recovery import (
    MINIMUM_OLDER_THAN_SECONDS,
    RECOVERY_ERROR_MESSAGE,
    SNAPSHOT_DIGEST_VERSION,
    ExpectedCountMismatchError,
    ExpectedCountRequiredError,
    ExpectedSnapshotRequiredError,
    OutputPathExistsError,
    SnapshotMismatchError,
    UnsafeOlderThanSecondsError,
    WorkersNotStoppedError,
    compute_snapshot_digest,
    run_recovery,
)


MIGRATION_DIRECTORY = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)

# Fixed reference "now" so tests never depend on wall-clock timing.
NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)
OLDER_THAN_SECONDS = 3600  # 1 hour -- below MINIMUM_OLDER_THAN_SECONDS,
# so every test that recovers anything must pass allow_short_window=True.

STALE = "2026-07-30 10:00:00"
VERY_STALE = "2026-07-30 09:00:00"
MIDDLING_STALE = "2026-07-30 10:30:00"
FRESH = "2026-07-30 11:55:00"


def seed_job(
    connection: sqlite3.Connection,
    *,
    job_type: str = "inspect_archive",
    status: str = "claimed",
    archive_id: int | None = None,
    worker_id: str | None = "worker-1",
    attempts: int = 1,
    max_attempts: int = 3,
    claimed_at: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    error_message: str | None = None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO jobs (
            job_type, status, archive_id, worker_id,
            attempts, max_attempts,
            claimed_at, started_at, completed_at, error_message
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_type,
            status,
            archive_id,
            worker_id,
            attempts,
            max_attempts,
            claimed_at,
            started_at,
            completed_at,
            error_message,
        ),
    )
    return int(cursor.lastrowid)


def build_database(database: Path) -> None:
    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)


def _fetch_job(database: Path, job_id: int) -> sqlite3.Row:
    with database_connection(database) as connection:
        return connection.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()


def _review(
    database: Path,
    *,
    older_than_seconds: int = OLDER_THAN_SECONDS,
) -> tuple[int, str]:
    """Replay the operator workflow: preview, then capture the guards.

    Returns the (count, snapshot digest) pair an operator would read off
    a report-only run and pass back to --confirm. Every confirming test
    below goes through this helper, so the tests exercise the same
    two-step review-then-confirm path a human uses rather than
    hand-computing the attestations.
    """
    output = run_recovery(
        database=database,
        older_than_seconds=older_than_seconds,
        allow_short_window=True,
        now=NOW,
    )
    return output["would_recover_count"], output["snapshot_digest"]


# --- report-only mode is strictly read-only ------------------------------


def test_report_only_makes_zero_database_changes(tmp_path: Path) -> None:
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        seed_job(connection, status="claimed", claimed_at=STALE)

    before_bytes = database.read_bytes()

    output = run_recovery(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        allow_short_window=True,
        now=NOW,
    )

    after_bytes = database.read_bytes()

    assert output["mode"] == "report_only"
    assert output["applied"] is False
    assert output["would_recover_count"] == 1
    assert output["recovered_count"] == 0
    assert output["database_unchanged"] is True
    assert before_bytes == after_bytes


def test_report_only_ignores_expected_count(tmp_path: Path) -> None:
    """expected_count is only enforced for confirm=True mutations."""
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        seed_job(connection, status="claimed", claimed_at=STALE)

    output = run_recovery(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        allow_short_window=True,
        now=NOW,
        expected_count=999,  # deliberately wrong; must not matter here
    )

    assert output["applied"] is False
    assert output["would_recover_count"] == 1


# --- confirmation is required for any mutation ----------------------------


def test_confirm_without_expected_count_is_refused(
    tmp_path: Path,
) -> None:
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        seed_job(connection, status="claimed", claimed_at=STALE)

    before_bytes = database.read_bytes()

    with pytest.raises(ExpectedCountRequiredError):
        run_recovery(
            database=database,
            older_than_seconds=OLDER_THAN_SECONDS,
            allow_short_window=True,
            confirm=True,
            workers_stopped=True,
            now=NOW,
        )

    assert database.read_bytes() == before_bytes


def test_mutation_never_happens_without_confirm_flag(
    tmp_path: Path,
) -> None:
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        job_id = seed_job(connection, status="claimed", claimed_at=STALE)

    count, digest = _review(database)

    run_recovery(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        allow_short_window=True,
        now=NOW,
        expected_count=count,
        expected_snapshot=digest,
        workers_stopped=True,
        confirm=False,
    )

    row = _fetch_job(database, job_id)
    assert row["status"] == "claimed"


# --- age-check guard -------------------------------------------------------


def test_short_window_is_rejected_without_override(
    tmp_path: Path,
) -> None:
    database = tmp_path / "recovery.db"
    build_database(database)

    with pytest.raises(UnsafeOlderThanSecondsError):
        run_recovery(
            database=database,
            older_than_seconds=MINIMUM_OLDER_THAN_SECONDS - 1,
            now=NOW,
        )


def test_short_window_permitted_with_override(tmp_path: Path) -> None:
    database = tmp_path / "recovery.db"
    build_database(database)

    output = run_recovery(
        database=database,
        older_than_seconds=1,
        allow_short_window=True,
        now=NOW,
    )

    assert output["would_recover_count"] == 0


def test_window_at_floor_does_not_require_override(
    tmp_path: Path,
) -> None:
    database = tmp_path / "recovery.db"
    build_database(database)

    output = run_recovery(
        database=database,
        older_than_seconds=MINIMUM_OLDER_THAN_SECONDS,
        now=NOW,
    )

    assert output["applied"] is False


# --- worker-liveness attestation (age is not proof of abandonment) --------


def test_confirm_without_workers_stopped_is_refused_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """
    Without leases or heartbeats, a stale claimed/running timestamp is
    equally consistent with a dead worker and a legitimately slow one
    (a large archive can take a long time to decode). The age floor is
    a typo guard, not evidence, so confirm mode must refuse outright
    unless the operator attests that workers are stopped -- even when
    every other guard is satisfied.
    """
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        job_id = seed_job(connection, status="claimed", claimed_at=STALE)

    count, digest = _review(database)
    before_bytes = database.read_bytes()

    with pytest.raises(WorkersNotStoppedError) as excinfo:
        run_recovery(
            database=database,
            older_than_seconds=OLDER_THAN_SECONDS,
            allow_short_window=True,
            confirm=True,
            expected_count=count,
            expected_snapshot=digest,
            now=NOW,
        )

    # The refusal must explain the limitation, not just name a flag.
    assert "--workers-stopped" in str(excinfo.value)
    assert WORKER_LIVENESS_WARNING in str(excinfo.value)

    assert database.read_bytes() == before_bytes
    assert _fetch_job(database, job_id)["status"] == "claimed"


def test_liveness_warning_is_surfaced_in_preview_and_json(
    tmp_path: Path,
) -> None:
    """The operator must meet the warning while deciding, not after."""
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        seed_job(connection, status="claimed", claimed_at=STALE)

    json_output = tmp_path / "reports" / "preview.json"

    output = run_recovery(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        allow_short_window=True,
        now=NOW,
        json_output=json_output,
    )

    assert output["worker_liveness_warning"] == WORKER_LIVENESS_WARNING
    assert output["workers_stopped_attested"] is False

    written = json.loads(json_output.read_text(encoding="utf-8"))
    assert written["worker_liveness_warning"] == WORKER_LIVENESS_WARNING
    assert written["snapshot_digest"] == output["snapshot_digest"]


# --- output path validation (a report must never clobber the database) ----


def _seeded_database(tmp_path: Path) -> Path:
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        seed_job(connection, status="claimed", claimed_at=STALE)

    return database


def test_json_output_cannot_equal_database_path(tmp_path: Path) -> None:
    database = _seeded_database(tmp_path)
    before_bytes = database.read_bytes()

    with pytest.raises(OutputPathCollisionError):
        run_recovery(
            database=database,
            older_than_seconds=OLDER_THAN_SECONDS,
            allow_short_window=True,
            now=NOW,
            json_output=database,
        )

    assert database.read_bytes() == before_bytes


def test_json_output_cannot_alias_database_via_hardlink(
    tmp_path: Path,
) -> None:
    database = _seeded_database(tmp_path)
    before_bytes = database.read_bytes()

    alias = tmp_path / "alias.json"

    try:
        os.link(database, alias)
    except (OSError, NotImplementedError, AttributeError) as exc:
        pytest.skip(f"hard links unsupported on this filesystem: {exc}")

    with pytest.raises(OutputPathCollisionError):
        run_recovery(
            database=database,
            older_than_seconds=OLDER_THAN_SECONDS,
            allow_short_window=True,
            now=NOW,
            json_output=alias,
        )

    assert database.read_bytes() == before_bytes


def test_existing_json_output_is_refused(tmp_path: Path) -> None:
    """
    A recovery report is the only durable evidence a destructive run
    happened; silently replacing a prior run's report would destroy it.
    """
    database = _seeded_database(tmp_path)

    existing = tmp_path / "previous-run.json"
    existing.write_text("prior evidence\n", encoding="utf-8")

    with pytest.raises(OutputPathExistsError):
        run_recovery(
            database=database,
            older_than_seconds=OLDER_THAN_SECONDS,
            allow_short_window=True,
            now=NOW,
            json_output=existing,
        )

    assert existing.read_text(encoding="utf-8") == "prior evidence\n"


def test_rejected_output_path_leaves_filesystem_untouched(
    tmp_path: Path,
) -> None:
    """
    Validation runs before the database is opened or fingerprinted and
    before any parent directory is created, so a rejected run must
    leave the filesystem byte-for-byte as it found it -- creating no
    directories and not touching the database.
    """
    database = _seeded_database(tmp_path)
    before_bytes = database.read_bytes()
    before_entries = sorted(entry.name for entry in tmp_path.iterdir())

    nested = tmp_path / "reports" / "nested" / "recovery.json"

    # Confirm mode with every other guard satisfied, so the only thing
    # that can stop this run is the output-path check -- and it must
    # stop it before mkdir -p of the report's parent directories and
    # before the database is opened.
    with pytest.raises(OutputPathCollisionError):
        run_recovery(
            database=database,
            older_than_seconds=OLDER_THAN_SECONDS,
            allow_short_window=True,
            confirm=True,
            workers_stopped=True,
            expected_count=1,
            expected_snapshot="0" * 64,
            now=NOW,
            json_output=database,
        )

    assert not nested.parent.exists()
    assert not (tmp_path / "reports").exists()
    assert database.read_bytes() == before_bytes

    # No directory (and nothing else) was created anywhere.
    assert sorted(entry.name for entry in tmp_path.iterdir()) == (
        before_entries
    )

    # And the same holds for a nested output path that is refused for
    # already existing.
    nested.parent.mkdir(parents=True)
    nested.write_text("prior evidence\n", encoding="utf-8")
    deeper = tmp_path / "reports" / "nested" / "deeper" / "recovery.json"

    with pytest.raises(OutputPathExistsError):
        run_recovery(
            database=database,
            older_than_seconds=OLDER_THAN_SECONDS,
            allow_short_window=True,
            now=NOW,
            json_output=nested,
        )

    assert not deeper.parent.exists()
    assert nested.read_text(encoding="utf-8") == "prior evidence\n"
    assert database.read_bytes() == before_bytes


# --- snapshot digest: identity of the reviewed set ------------------------


def test_snapshot_digest_matches_documented_serialization(
    tmp_path: Path,
) -> None:
    """
    Pins the canonical serialization documented in the module
    docstring, so any accidental change to the field list, ordering, or
    rendering fails loudly instead of silently invalidating digests
    operators may already be holding.
    """
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        job_id = seed_job(
            connection,
            status="claimed",
            claimed_at=STALE,
            attempts=1,
            max_attempts=3,
        )

    output = run_recovery(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        allow_short_window=True,
        now=NOW,
    )

    payload = (
        "\n".join(
            [
                SNAPSHOT_DIGEST_VERSION,
                f"job_id={job_id}|status=claimed|attempts=1|"
                f"max_attempts=3|effective_activity_at={STALE}|"
                "projected_outcome=pending",
            ]
        )
        + "\n"
    )
    expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    assert output["snapshot_digest"] == expected
    assert output["snapshot_digest_version"] == SNAPSHOT_DIGEST_VERSION


def test_snapshot_digest_sorts_by_job_id_not_query_order(
    tmp_path: Path,
) -> None:
    """
    collect_stale_jobs() orders by activity timestamp, which ties and
    is therefore not a stable identity ordering. The digest sorts by
    job_id so it is reproducible regardless of query order.
    """
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        # Lower id, newer activity: query order is [second, first],
        # canonical order must be [first, second].
        first = seed_job(connection, status="claimed", claimed_at=STALE)
        second = seed_job(
            connection, status="claimed", claimed_at=VERY_STALE
        )

    assert first < second

    output = run_recovery(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        allow_short_window=True,
        now=NOW,
    )

    assert [job["job_id"] for job in output["jobs"]] == [second, first]

    payload = (
        "\n".join(
            [
                SNAPSHOT_DIGEST_VERSION,
                f"job_id={first}|status=claimed|attempts=1|"
                f"max_attempts=3|effective_activity_at={STALE}|"
                "projected_outcome=pending",
                f"job_id={second}|status=claimed|attempts=1|"
                f"max_attempts=3|effective_activity_at={VERY_STALE}|"
                "projected_outcome=pending",
            ]
        )
        + "\n"
    )
    assert output["snapshot_digest"] == hashlib.sha256(
        payload.encode("utf-8")
    ).hexdigest()


def test_snapshot_digest_changes_when_attempts_change(
    tmp_path: Path,
) -> None:
    """
    attempts/max_attempts decide retry-versus-permanent-fail, so a
    change there changes what recovery would *do* to an unchanged set
    of job ids. The digest must move even though the ids do not.
    """
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        job_id = seed_job(
            connection,
            status="claimed",
            claimed_at=STALE,
            attempts=1,
            max_attempts=3,
        )

    _, before_digest = _review(database)

    with database_connection(database) as connection:
        connection.execute(
            "UPDATE jobs SET attempts = 3 WHERE id = ?", (job_id,)
        )

    count_after, after_digest = _review(database)

    assert count_after == 1  # the count guard sees nothing at all
    assert after_digest != before_digest


def test_equal_count_set_swap_is_rejected_by_digest(
    tmp_path: Path,
) -> None:
    """
    The exact scenario --expected-count cannot detect: one reviewed job
    leaves the stale set while a different, never-reviewed job enters
    it. The count is unchanged, so the count guard passes; only the
    digest binds the *identity* of the reviewed set. Nothing may be
    written.
    """
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        reviewed_a = seed_job(
            connection, status="claimed", claimed_at=STALE
        )
        reviewed_b = seed_job(
            connection, status="claimed", claimed_at=STALE
        )
        # Not stale at review time, so never part of the reviewed set.
        unreviewed = seed_job(
            connection, status="claimed", claimed_at=FRESH
        )

    reviewed_count, reviewed_digest = _review(database)
    assert reviewed_count == 2

    # The swap: a worker completes reviewed_a (it leaves the stale
    # set), and unreviewed crosses the age threshold (it enters).
    with database_connection(database) as connection:
        JobQueue(connection).mark_completed(reviewed_a, worker_id="worker-1")
        connection.execute(
            "UPDATE jobs SET claimed_at = ? WHERE id = ?",
            (MIDDLING_STALE, unreviewed),
        )

    live_count, live_digest = _review(database)

    # The count guard alone would have waved this through...
    assert live_count == reviewed_count
    # ...but the set is not the reviewed set.
    assert live_digest != reviewed_digest

    before_bytes = database.read_bytes()

    with pytest.raises(SnapshotMismatchError):
        run_recovery(
            database=database,
            older_than_seconds=OLDER_THAN_SECONDS,
            allow_short_window=True,
            confirm=True,
            workers_stopped=True,
            expected_count=reviewed_count,
            expected_snapshot=reviewed_digest,
            now=NOW,
        )

    assert database.read_bytes() == before_bytes
    assert _fetch_job(database, reviewed_a)["status"] == "completed"
    assert _fetch_job(database, reviewed_b)["status"] == "claimed"
    assert _fetch_job(database, unreviewed)["status"] == "claimed"


def test_confirm_without_expected_snapshot_is_refused(
    tmp_path: Path,
) -> None:
    database = _seeded_database(tmp_path)
    before_bytes = database.read_bytes()

    with pytest.raises(ExpectedSnapshotRequiredError):
        run_recovery(
            database=database,
            older_than_seconds=OLDER_THAN_SECONDS,
            allow_short_window=True,
            confirm=True,
            workers_stopped=True,
            expected_count=1,
            now=NOW,
        )

    assert database.read_bytes() == before_bytes


@pytest.mark.parametrize(
    "malformed",
    [
        "",
        "not-a-digest",
        "abc123",
        "0" * 63,
        "0" * 65,
        "z" * 64,
    ],
)
def test_malformed_expected_snapshot_is_refused(
    tmp_path: Path,
    malformed: str,
) -> None:
    """
    A truncated or mistyped digest is rejected as unusable input rather
    than falling through to SnapshotMismatchError, which would send the
    operator chasing a race that never happened.
    """
    database = _seeded_database(tmp_path)
    before_bytes = database.read_bytes()

    with pytest.raises(ExpectedSnapshotRequiredError):
        run_recovery(
            database=database,
            older_than_seconds=OLDER_THAN_SECONDS,
            allow_short_window=True,
            confirm=True,
            workers_stopped=True,
            expected_count=1,
            expected_snapshot=malformed,
            now=NOW,
        )

    assert database.read_bytes() == before_bytes


def test_expected_snapshot_accepts_copy_pasted_whitespace_and_case(
    tmp_path: Path,
) -> None:
    """Operators copy digests out of a terminal; be forgiving there."""
    database = _seeded_database(tmp_path)

    count, digest = _review(database)

    output = run_recovery(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        allow_short_window=True,
        confirm=True,
        workers_stopped=True,
        expected_count=count,
        expected_snapshot=f"  {digest.upper()}  ",
        now=NOW,
    )

    assert output["recovered_count"] == 1


def test_matching_snapshot_permits_recovery(tmp_path: Path) -> None:
    """Happy path: the reviewed set is unchanged, so recovery runs."""
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        job_id = seed_job(connection, status="claimed", claimed_at=STALE)

    count, digest = _review(database)

    output = run_recovery(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        allow_short_window=True,
        confirm=True,
        workers_stopped=True,
        expected_count=count,
        expected_snapshot=digest,
        now=NOW,
    )

    assert output["applied"] is True
    assert output["recovered_count"] == 1
    assert output["snapshot_digest"] == digest
    assert output["expected_snapshot"] == digest
    assert output["workers_stopped_attested"] is True
    assert _fetch_job(database, job_id)["status"] == "pending"


def test_empty_stale_set_has_a_stable_digest(tmp_path: Path) -> None:
    """
    "There is nothing to recover" is itself a claim worth binding, so
    the empty set has a well-defined digest rather than no digest.
    """
    database = tmp_path / "recovery.db"
    build_database(database)

    count, digest = _review(database)

    assert count == 0
    assert digest == compute_snapshot_digest([])

    output = run_recovery(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        allow_short_window=True,
        confirm=True,
        workers_stopped=True,
        expected_count=count,
        expected_snapshot=digest,
        now=NOW,
    )

    assert output["recovered_count"] == 0


# --- expected-count guard protects against a changed live set -------------


def test_expected_count_mismatch_is_refused_and_nothing_changes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        seed_job(connection, status="claimed", claimed_at=STALE)

    _, digest = _review(database)
    before_bytes = database.read_bytes()

    with pytest.raises(ExpectedCountMismatchError):
        run_recovery(
            database=database,
            older_than_seconds=OLDER_THAN_SECONDS,
            allow_short_window=True,
            confirm=True,
            workers_stopped=True,
            expected_count=2,  # wrong: only one stale job exists
            expected_snapshot=digest,
            now=NOW,
        )

    assert database.read_bytes() == before_bytes


def test_confirm_refuses_when_job_completed_after_review(
    tmp_path: Path,
) -> None:
    """
    A job whose status changes between the operator's report-only
    review and the --confirm call (e.g. a worker completes it) must
    never be recovered: the live re-check inside the write transaction
    must catch the mismatch and refuse to mutate anything.
    """
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        job_one = seed_job(connection, status="claimed", claimed_at=STALE)
        job_two = seed_job(connection, status="claimed", claimed_at=STALE)

    count, digest = _review(database)
    assert count == 2

    # A worker completes job_one between the preview and the confirm
    # call -- exactly the race this guard exists to catch.
    with database_connection(database) as connection:
        JobQueue(connection).mark_completed(job_one, worker_id="worker-1")

    with pytest.raises(ExpectedCountMismatchError):
        run_recovery(
            database=database,
            older_than_seconds=OLDER_THAN_SECONDS,
            allow_short_window=True,
            confirm=True,
            workers_stopped=True,
            expected_count=count,  # stale count reviewed a moment ago
            expected_snapshot=digest,
            now=NOW,
        )

    # Neither job was touched: job_one remains completed (never
    # reverted to pending/failed by recovery), job_two remains claimed
    # (never recovered without a matching, reviewed count).
    completed_row = _fetch_job(database, job_one)
    claimed_row = _fetch_job(database, job_two)
    assert completed_row["status"] == "completed"
    assert claimed_row["status"] == "claimed"


# --- successful recovery ---------------------------------------------------


def test_recovery_recovers_retryable_and_exhausted_jobs(
    tmp_path: Path,
) -> None:
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        retryable_id = seed_job(
            connection,
            status="claimed",
            claimed_at=STALE,
            attempts=1,
            max_attempts=3,
        )
        exhausted_id = seed_job(
            connection,
            status="running",
            claimed_at=VERY_STALE,
            started_at=STALE,
            attempts=3,
            max_attempts=3,
        )

    count, digest = _review(database)

    output = run_recovery(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        allow_short_window=True,
        confirm=True,
        workers_stopped=True,
        expected_count=count,
        expected_snapshot=digest,
        now=NOW,
    )

    assert output["applied"] is True
    assert output["recovered_count"] == 2

    retryable_row = _fetch_job(database, retryable_id)
    exhausted_row = _fetch_job(database, exhausted_id)

    assert retryable_row["status"] == "pending"
    assert retryable_row["worker_id"] is None
    assert retryable_row["claimed_at"] is None
    assert retryable_row["started_at"] is None
    assert retryable_row["error_message"] == RECOVERY_ERROR_MESSAGE

    assert exhausted_row["status"] == "failed"
    assert exhausted_row["completed_at"] is not None
    assert exhausted_row["error_message"] == RECOVERY_ERROR_MESSAGE


def test_fresh_jobs_are_never_recovered(tmp_path: Path) -> None:
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        seed_job(connection, status="claimed", claimed_at=FRESH)

    count, digest = _review(database)
    assert count == 0

    output = run_recovery(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        allow_short_window=True,
        confirm=True,
        workers_stopped=True,
        expected_count=count,
        expected_snapshot=digest,
        now=NOW,
    )

    assert output["recovered_count"] == 0


# --- attempts and error_message preservation -------------------------------


def test_attempts_are_never_reset_by_recovery(tmp_path: Path) -> None:
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        job_id = seed_job(
            connection,
            status="claimed",
            claimed_at=STALE,
            attempts=2,
            max_attempts=5,
        )

    count, digest = _review(database)

    run_recovery(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        allow_short_window=True,
        confirm=True,
        workers_stopped=True,
        expected_count=count,
        expected_snapshot=digest,
        now=NOW,
    )

    row = _fetch_job(database, job_id)
    assert int(row["attempts"]) == 2


def test_error_message_is_already_null_before_recovery(
    tmp_path: Path,
) -> None:
    """
    Documents the "Design decision -- error_message on recovery" note
    in abandoned_job_recovery.py: a claimed/running job's error_message
    is always NULL by the time it could become a recovery candidate,
    because claim_next() clears it on every claim. This test proves
    that via the real claim_next() path (not just a seeded fixture), so
    the design decision is verified against the actual queue behavior
    it depends on.
    """
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        queue = JobQueue(connection)
        queue.enqueue("inspect_archive", max_attempts=3)
        claimed = queue.claim_next("worker-1")
        assert claimed is not None
        assert claimed.error_message is None

        connection.execute(
            "UPDATE jobs SET claimed_at = ? WHERE id = ?",
            (STALE, claimed.id),
        )
        job_id = claimed.id

    before_row = _fetch_job(database, job_id)
    assert before_row["error_message"] is None

    count, digest = _review(database)

    run_recovery(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        allow_short_window=True,
        confirm=True,
        workers_stopped=True,
        expected_count=count,
        expected_snapshot=digest,
        now=NOW,
    )

    after_row = _fetch_job(database, job_id)
    assert after_row["error_message"] == RECOVERY_ERROR_MESSAGE
    assert int(after_row["attempts"]) == 1


# --- idempotency -------------------------------------------------------


def test_second_immediate_run_recovers_nothing_new(
    tmp_path: Path,
) -> None:
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        job_id = seed_job(
            connection,
            status="claimed",
            claimed_at=STALE,
            attempts=1,
            max_attempts=3,
        )

    count, digest = _review(database)

    first = run_recovery(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        allow_short_window=True,
        confirm=True,
        workers_stopped=True,
        expected_count=count,
        expected_snapshot=digest,
        now=NOW,
    )
    assert first["recovered_count"] == 1

    second_count, second_digest = _review(database)
    assert second_count == 0

    second = run_recovery(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        allow_short_window=True,
        confirm=True,
        workers_stopped=True,
        expected_count=second_count,
        expected_snapshot=second_digest,
        now=NOW,
    )
    assert second["recovered_count"] == 0

    row = _fetch_job(database, job_id)
    # Still pending from the first recovery; the second run did not
    # touch it again (it is no longer claimed/running).
    assert row["status"] == "pending"


def test_stale_digest_from_before_a_recovery_is_refused(
    tmp_path: Path,
) -> None:
    """
    Re-running an old --confirm command line after a successful
    recovery must not silently become a no-op "success": the digest
    from before the recovery no longer describes the live set.
    """
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        seed_job(connection, status="claimed", claimed_at=STALE)

    count, digest = _review(database)

    run_recovery(
        database=database,
        older_than_seconds=OLDER_THAN_SECONDS,
        allow_short_window=True,
        confirm=True,
        workers_stopped=True,
        expected_count=count,
        expected_snapshot=digest,
        now=NOW,
    )

    # The stale set is now empty, so the count guard catches this one
    # first; the digest would too.
    with pytest.raises(ExpectedCountMismatchError):
        run_recovery(
            database=database,
            older_than_seconds=OLDER_THAN_SECONDS,
            allow_short_window=True,
            confirm=True,
            workers_stopped=True,
            expected_count=count,
            expected_snapshot=digest,
            now=NOW,
        )


# --- rollback is all-or-nothing for the batch ------------------------------


def test_exception_mid_batch_rolls_back_every_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    recover_abandoned()'s own transaction structure -- and this
    module's -- performs the whole batch inside one BEGIN IMMEDIATE /
    COMMIT. That guarantees all-or-nothing: if anything raises partway
    through, every row (including ones already updated earlier in the
    same loop) must be rolled back, not left half-recovered.
    """
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        first_id = seed_job(
            connection,
            status="claimed",
            claimed_at=STALE,
            attempts=1,
            max_attempts=3,
        )
        second_id = seed_job(
            connection,
            status="running",
            claimed_at=VERY_STALE,
            started_at=STALE,
            attempts=1,
            max_attempts=3,
        )

    count, digest = _review(database)

    import comic_automation.jobs.abandoned_job_recovery as recovery_module

    original = recovery_module._apply_recovery_row
    calls = {"count": 0}

    def poisoned(connection, *, job, now_text):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("simulated failure mid-batch")
        return original(connection, job=job, now_text=now_text)

    monkeypatch.setattr(
        recovery_module, "_apply_recovery_row", poisoned
    )

    with pytest.raises(RuntimeError, match="simulated failure"):
        run_recovery(
            database=database,
            older_than_seconds=OLDER_THAN_SECONDS,
            allow_short_window=True,
            confirm=True,
            workers_stopped=True,
            expected_count=count,
            expected_snapshot=digest,
            now=NOW,
        )

    assert calls["count"] == 2

    # Both jobs, including the one processed before the failure,
    # must remain exactly as they were: the transaction rolled back.
    first_row = _fetch_job(database, first_id)
    second_row = _fetch_job(database, second_id)

    assert first_row["status"] == "claimed"
    assert first_row["error_message"] is None
    assert second_row["status"] == "running"
    assert second_row["error_message"] is None


# --- concurrency: recovery must not race with worker writes ---------------


def test_recovery_transaction_blocks_concurrent_worker_write(
    tmp_path: Path,
) -> None:
    """
    Recovery takes its write lock with BEGIN IMMEDIATE before reading
    or writing any row (the same discipline JobQueue itself uses for
    claim_next()/recover_abandoned()). This test holds that lock open
    on one connection -- simulating the exact midpoint of a recovery
    transaction, after the stale-job SELECT but before COMMIT -- and
    proves a concurrent worker's mark_completed() on another connection
    blocks until the transaction ends, rather than interleaving with
    it. Once the lock is released, the worker's completion attempt
    correctly fails, because by then the job has already been recovered
    to 'pending' and is no longer completable -- proving the two
    operations can never both succeed on the same job.
    """
    database = tmp_path / "recovery.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        job_id = seed_job(
            connection,
            status="claimed",
            claimed_at=STALE,
            attempts=1,
            max_attempts=3,
        )

    # Manually replay the write-locking half of run_recovery(): open a
    # writable connection, take the write lock, and read the stale set
    # -- without committing -- to simulate holding the transaction open
    # mid-recovery.
    holder = connect_database(database)
    holder.execute("BEGIN IMMEDIATE")
    rows = holder.execute(
        """
        SELECT id FROM jobs
        WHERE status IN ('claimed', 'running')
          AND COALESCE(started_at, claimed_at) <= ?
        """,
        ("2026-07-30 11:00:00",),
    ).fetchall()
    assert len(rows) == 1

    result: dict = {}

    def worker_completes() -> None:
        with database_connection(database) as worker_connection:
            queue = JobQueue(worker_connection)
            try:
                job = queue.mark_completed(job_id, worker_id="worker-1")
                result["status"] = job.status
            except InvalidJobTransitionError as exc:
                result["error"] = str(exc)

    thread = threading.Thread(target=worker_completes)
    thread.start()

    # Give the worker thread time to attempt the write and block on
    # SQLite's busy handler; it must still be alive (blocked), proving
    # it did not interleave with the open transaction.
    time.sleep(0.3)
    assert thread.is_alive()

    # Finish the recovery-equivalent write and release the lock.
    now_text = "2026-07-30 12:00:00"
    holder.execute(
        """
        UPDATE jobs
        SET status = 'pending', available_at = ?, claimed_at = NULL,
            started_at = NULL, worker_id = NULL,
            error_message = ?, updated_at = ?
        WHERE id = ?
        """,
        (now_text, RECOVERY_ERROR_MESSAGE, now_text, job_id),
    )
    holder.execute("COMMIT")
    holder.close()

    thread.join(timeout=5)
    assert not thread.is_alive()

    # The worker's completion attempt must have failed: the job was
    # already recovered to 'pending' by the time its write unblocked,
    # so it is no longer in a completable (claimed/running) state.
    assert "error" in result
    assert "status" not in result

    row = _fetch_job(database, job_id)
    assert row["status"] == "pending"
    assert row["error_message"] == RECOVERY_ERROR_MESSAGE


# --- missing database -------------------------------------------------


def test_missing_database_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        run_recovery(
            database=tmp_path / "does-not-exist.db",
            older_than_seconds=OLDER_THAN_SECONDS,
            now=NOW,
        )
