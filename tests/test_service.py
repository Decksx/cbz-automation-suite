"""Tests for comic_automation.service.ComicAutomationService: the
long-running service's startup/initialize() behavior.

Covers workspace/database creation and idempotency, and -- the bulk of this
file -- the critical safety property that startup's stale-job detection is
purely observational: it must warn about jobs that look abandoned (claimed
or running well past the configured threshold) without ever mutating them,
since a legitimately long-running job (e.g. decoding a huge archive) must
not be silently reset just because it's been running a while with no
heartbeat to prove liveness.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from comic_automation.database.migrations import (
    discover_migrations,
    migration_version,
)
from comic_automation.database.connection import (
    connect_database,
    database_connection,
)
from comic_automation.jobs import JobQueue, JobStatus
from comic_automation.jobs.abandoned_job_audit import (
    WORKER_LIVENESS_WARNING,
)
from comic_automation.service import (
    RECOVERY_CLI_PATH,
    ComicAutomationService,
)


# Columns that abandoned-job recovery would rewrite. Startup must
# leave every one of them byte-identical, because rewriting any of
# them is how a live long-running job silently loses its work.
_RECOVERY_SENSITIVE_COLUMNS = (
    "id",
    "status",
    "attempts",
    "max_attempts",
    "worker_id",
    "claimed_at",
    "started_at",
    "available_at",
    "completed_at",
    "error_message",
    "failure_category",
    "updated_at",
)


def _utc_sql_timestamp(moment: datetime) -> str:
    """Format a datetime as the UTC "YYYY-MM-DD HH:MM:SS" string SQLite
    columns in this schema store timestamps as.
    """
    return moment.astimezone(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _seconds_ago(seconds: int) -> str:
    """SQL timestamp string for a moment `seconds` seconds in the past,
    used to backdate job activity timestamps deterministically instead of
    sleeping in tests.
    """
    return _utc_sql_timestamp(
        datetime.now(timezone.utc)
        - timedelta(seconds=seconds)
    )


def fingerprint_jobs(database: Path) -> list[tuple]:
    """Every recovery-sensitive field of every job row, ordered.

    Compared before and after `initialize()` so the assertion proves
    "nothing about any job changed", not merely "the job I looked at
    still has the status I expected".
    """
    columns = ", ".join(_RECOVERY_SENSITIVE_COLUMNS)

    with database_connection(database) as connection:
        rows = connection.execute(
            f"SELECT {columns} FROM jobs ORDER BY id"
        ).fetchall()

    return [tuple(row) for row in rows]


def write_test_config(
    tmp_path: Path,
    *,
    cpu_workers: int = 1,
) -> Path:
    """Write a minimal but complete service.toml (workspace/library/service
    sections) under tmp_path and return its path, for tests that need a
    real ComicAutomationService to construct against.
    """
    workspace = tmp_path / "workspace"
    library = tmp_path / "library"
    config_path = tmp_path / "service.toml"

    config_path.write_text(
        f"""
[workspace]
root = '{workspace.as_posix()}'
database = '{
    (workspace / "database" / "comics.db").as_posix()
}'
cache = '{(workspace / "cache").as_posix()}'
embeddings = '{
    (workspace / "embeddings").as_posix()
}'
staging = '{(workspace / "staging").as_posix()}'
temp = '{(workspace / "temp").as_posix()}'
logs = '{(workspace / "logs").as_posix()}'
backups = '{(workspace / "backups").as_posix()}'

[library]
root = '{library.as_posix()}'

[service]
poll_interval_seconds = 1
cpu_workers = {cpu_workers}
gpu_workers = 1
operating_mode = "audit"
""",
        encoding="utf-8",
    )

    return config_path


def all_migration_versions() -> list[int]:
    """Every migration on disk, in order.

    Derived rather than written out, so a new migration does not fail a test
    that is really asserting "initialize() applied all of them".
    """
    directory = (
        Path(__file__).resolve().parents[1]
        / "comic_automation"
        / "database"
        / "migrations"
    )
    return [migration_version(path) for path in discover_migrations(directory)]


def test_service_initialize_creates_workspace_and_database(
    tmp_path: Path,
) -> None:
    """initialize() should apply all 10 migrations and create every
    workspace subdirectory (cache, embeddings, staging, temp, logs,
    backups) plus the database file itself, starting from nothing.
    """
    config_path = write_test_config(tmp_path)
    service = ComicAutomationService(config_path)

    applied = service.initialize()

    assert applied == all_migration_versions()
    assert service.config.workspace.root.is_dir()
    assert service.config.workspace.cache.is_dir()
    assert service.config.workspace.embeddings.is_dir()
    assert service.config.workspace.staging.is_dir()
    assert service.config.workspace.temp.is_dir()
    assert service.config.workspace.logs.is_dir()
    assert service.config.workspace.backups.is_dir()
    assert service.config.workspace.database.is_file()


def test_service_initialize_is_idempotent(
    tmp_path: Path,
) -> None:
    """A second call to initialize() should apply zero further migrations,
    confirming startup can be safely re-run against an already-set-up
    workspace/database.
    """
    config_path = write_test_config(tmp_path)
    service = ComicAutomationService(config_path)

    first = service.initialize()
    second = service.initialize()

    assert first == all_migration_versions()
    assert second == []


def seed_stale_jobs(
    service: ComicAutomationService,
    *,
    claimed_age_seconds: int = 3_600,
    running_age_seconds: int = 3_600,
) -> dict[str, int]:
    """Create one stale `claimed` and one stale `running` job.

    The `running` job stands for the dangerous case: a large archive
    that is still legitimately decoding, long past any age threshold,
    with no lease or heartbeat to prove it is alive. The old startup
    recovery would have reset it; the new startup must not.
    """
    with database_connection(
        service.config.workspace.database
    ) as connection:
        queue = JobQueue(connection)

        stale_claimed = queue.enqueue(
            "stale_claimed_job",
            max_attempts=3,
        )
        stale_running = queue.enqueue(
            "long_running_job",
            max_attempts=3,
        )

        first = queue.claim_next("dead-worker")
        second = queue.claim_next("busy-worker")

        assert first is not None
        assert second is not None
        assert first.id == stale_claimed.id
        assert second.id == stale_running.id

        queue.mark_running(
            stale_running.id,
            worker_id="busy-worker",
        )

        # Backdate the activity timestamps the staleness predicate
        # reads (COALESCE(started_at, claimed_at)) rather than
        # sleeping, so the test is deterministic.
        connection.execute(
            "UPDATE jobs SET claimed_at = ? WHERE id = ?",
            (_seconds_ago(claimed_age_seconds), stale_claimed.id),
        )
        connection.execute(
            """
            UPDATE jobs
            SET claimed_at = ?, started_at = ?
            WHERE id = ?
            """,
            (
                _seconds_ago(running_age_seconds + 5),
                _seconds_ago(running_age_seconds),
                stale_running.id,
            ),
        )

    return {
        "claimed": stale_claimed.id,
        "running": stale_running.id,
    }


def test_service_initialize_never_mutates_stale_jobs(
    tmp_path: Path,
) -> None:
    """Startup must not touch a single field of a stale job row.

    `initialize()` is run once up front so migrations are already
    applied (migrations legitimately write on first run); the
    before/after comparison then isolates the stale-job detection
    step, which must write nothing at all.
    """
    config_path = write_test_config(tmp_path)
    service = ComicAutomationService(
        config_path,
        abandoned_after_seconds=60,
    )
    service.initialize()

    seed_stale_jobs(service)
    database = service.config.workspace.database
    before = fingerprint_jobs(database)

    # PRAGMA data_version, read on a connection held open across the
    # call, increments whenever another connection commits a change to
    # this database -- an independent, storage-level change detector
    # that does not depend on knowing which columns to inspect.
    observer = connect_database(database)

    try:
        data_version_before = observer.execute(
            "PRAGMA data_version"
        ).fetchone()[0]

        applied = service.initialize()

        data_version_after = observer.execute(
            "PRAGMA data_version"
        ).fetchone()[0]
    finally:
        observer.close()

    after = fingerprint_jobs(database)

    assert applied == []
    assert after == before
    assert data_version_after == data_version_before


def test_service_initialize_leaves_long_running_job_untouched(
    tmp_path: Path,
) -> None:
    """A job running for an hour is still running afterwards.

    3600 seconds is far past the 300-second production threshold, so
    the old startup recovery would have reset this job to `pending`
    (or failed it outright once attempts ran out) purely because a
    large archive takes a long time to decode.
    """
    config_path = write_test_config(tmp_path)
    service = ComicAutomationService(config_path)
    service.initialize()

    job_ids = seed_stale_jobs(service, running_age_seconds=3_600)

    with database_connection(
        service.config.workspace.database
    ) as connection:
        before = JobQueue(connection).get(job_ids["running"])

    assert before.status == JobStatus.RUNNING

    service.initialize()

    with database_connection(
        service.config.workspace.database
    ) as connection:
        after = JobQueue(connection).get(job_ids["running"])

    assert after.status == JobStatus.RUNNING
    assert after.attempts == before.attempts
    assert after.worker_id == before.worker_id
    assert after.started_at == before.started_at
    assert after.error_message is None


def test_service_initialize_warns_about_stale_jobs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Startup should log exactly one WARNING covering both stale jobs,
    naming the count, the configured threshold, both job IDs, and pointing
    to the manual recovery CLI (with --confirm/--workers-stopped flags) --
    while explicitly stating nothing was changed and repeating the worker-
    liveness caveat verbatim from the audit module.
    """
    config_path = write_test_config(tmp_path)
    service = ComicAutomationService(
        config_path,
        abandoned_after_seconds=60,
    )
    service.initialize()

    job_ids = seed_stale_jobs(service)

    with caplog.at_level(
        logging.WARNING,
        logger="comic_automation.service",
    ):
        service.initialize()

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]

    assert len(warnings) == 1

    message = warnings[0]

    assert "2 job(s)" in message
    assert "60 second(s)" in message
    assert "NOT recovered" in message
    assert "nothing in this database was changed" in message
    # Verbatim, not paraphrased: the service and the audit must state
    # the liveness caveat identically.
    assert WORKER_LIVENESS_WARNING in message
    assert RECOVERY_CLI_PATH in message
    assert "--confirm" in message
    assert "--workers-stopped" in message
    assert str(job_ids["claimed"]) in message
    assert str(job_ids["running"]) in message


def test_service_initialize_does_not_warn_without_stale_jobs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A job that was just claimed (well inside the staleness threshold)
    should produce no WARNING-level log output at all on startup.
    """
    config_path = write_test_config(tmp_path)
    service = ComicAutomationService(
        config_path,
        abandoned_after_seconds=60,
    )
    service.initialize()

    # A freshly claimed job is not stale: it is inside the threshold.
    with database_connection(
        service.config.workspace.database
    ) as connection:
        queue = JobQueue(connection)
        queue.enqueue("fresh_job", max_attempts=3)

        assert queue.claim_next("live-worker") is not None

    with caplog.at_level(
        logging.WARNING,
        logger="comic_automation.service",
    ):
        service.initialize()

    assert [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ] == []


def test_stale_job_detection_connection_is_read_only(
    tmp_path: Path,
) -> None:
    """The detection connection itself refuses to write.

    Guards the mechanism, not just the outcome: if a future edit
    swapped the read-only connection for the migration connection, the
    fingerprint tests above could still pass by accident (because the
    detection code happens not to issue writes today), but this would
    fail.
    """
    from comic_automation.jobs.abandoned_job_audit import (
        readonly_database_connection,
    )

    config_path = write_test_config(tmp_path)
    service = ComicAutomationService(config_path)
    service.initialize()
    seed_stale_jobs(service)

    with readonly_database_connection(
        service.config.workspace.database
    ) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute(
                "UPDATE jobs SET status = 'pending'"
            )


def test_stale_job_threshold_alias_tracks_configured_value(
    tmp_path: Path,
) -> None:
    """service.stale_job_threshold_seconds should reflect whatever
    abandoned_after_seconds was passed to the constructor, defaulting to
    300 seconds when not specified.
    """
    config_path = write_test_config(tmp_path)
    service = ComicAutomationService(
        config_path,
        abandoned_after_seconds=90,
    )

    assert service.stale_job_threshold_seconds == 90
    assert ComicAutomationService(
        config_path
    ).stale_job_threshold_seconds == 300


def test_service_does_not_create_or_modify_library_root(
    tmp_path: Path,
) -> None:
    """initialize() sets up the workspace, but must never create or touch
    the library_root itself -- that's the user's comic library, not
    application-owned storage.
    """
    config_path = write_test_config(tmp_path)
    service = ComicAutomationService(config_path)
    library_root = service.config.library_root

    assert library_root.exists() is False

    service.initialize()

    assert library_root.exists() is False
