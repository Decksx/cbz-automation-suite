"""Service entry point: workspace setup, migrations, and workers.

Startup is deliberately non-mutating with respect to the job queue.
Earlier revisions called `JobQueue.recover_abandoned()` unconditionally
from `initialize()`, which meant every service restart rewrote any
`claimed`/`running` job whose last activity timestamp was older than
300 seconds -- resetting it to `pending`, or marking it permanently
`failed` once its attempts were exhausted.

That was unsafe. The queue schema has no leases and no heartbeats, so
age alone cannot distinguish a worker that died from one that is
legitimately still working: see
`comic_automation.jobs.abandoned_job_audit.WORKER_LIVENESS_WARNING`,
and `docs/production_handoff_2026-07-30.md`, which warns explicitly
against inferring failure from an interval simply taking longer than a
previous one. Some archives in this library are large and slow to
decode. A restart during an active batch could therefore silently
discard or permanently fail live, healthy work, unattended, with no
preview, no operator review, and no record of which rows were touched.

Startup now *observes* instead: it detects the same stale jobs, using
the audit's own predicate, over a strictly read-only connection, and
logs a warning pointing at the guarded operator CLI
(`scripts/comic_job_abandoned_recovery.py`), which requires a reviewed
preview, an expected count, a snapshot digest, an explicit
`--workers-stopped` attestation, and `--confirm` before it changes
anything. Recovery is now always a human decision.
"""

from __future__ import annotations

import logging
import os
import socket
import sqlite3
import threading
from collections.abc import Mapping
from pathlib import Path

from comic_automation.config import (
    AppConfig,
    ensure_workspace,
    load_config,
)
from comic_automation.database.connection import (
    database_connection,
)
from comic_automation.database.migrations import (
    apply_migrations,
)
from comic_automation.jobs import (
    JobHandler,
    JobQueue,
    JobWorker,
)
from comic_automation.jobs.abandoned_job_audit import (
    WORKER_LIVENESS_WARNING,
    collect_stale_jobs,
    readonly_database_connection,
)


log = logging.getLogger(__name__)

DEFAULT_MIGRATION_DIRECTORY = (
    Path(__file__).resolve().parent
    / "database"
    / "migrations"
)

# The guarded, human-run counterpart to the recovery this service no
# longer performs. Named here (rather than inline in the message) so
# the startup warning, the CLI, and any future caller cannot drift.
RECOVERY_CLI_PATH = "scripts/comic_job_abandoned_recovery.py"


class ComicAutomationService:
    def __init__(
        self,
        config_path: str | Path,
        *,
        handlers: Mapping[str, JobHandler] | None = None,
        migration_directory: str | Path | None = None,
        stop_event: threading.Event | None = None,
        abandoned_after_seconds: int = 300,
    ) -> None:
        """
        `abandoned_after_seconds` is a *detection* threshold, not a
        recovery window.

        It used to be the age at which startup would rewrite a stale
        `claimed`/`running` job. Nothing at startup writes to `jobs`
        any more, so the value now only decides which jobs the
        read-only startup check reports as stale (it is passed
        verbatim to `abandoned_job_audit.collect_stale_jobs()`, the
        same helper the audit and the guarded recovery CLI use).

        The name is kept rather than replaced because renaming it
        would break existing keyword callers -- including
        `tests/test_service.py` -- for no behavioral gain, and because
        the value still answers the same question ("how old must a
        claimed/running job be before we treat it as suspicious?").
        `stale_job_threshold_seconds` is exposed as a clearer alias
        for new code; it is a read-only view of the same number, not a
        second setting, so the two can never disagree.

        Detection is deliberately not made skippable. It is a single
        indexed SELECT over a read-only connection, it cannot modify
        anything, and the case it protects against -- a crash leaving
        work stranded and nobody noticing -- is precisely the case
        where an operator would have turned the check off and
        forgotten. A knob here would only add a way to lose the
        warning.
        """
        if abandoned_after_seconds < 0:
            raise ValueError(
                "abandoned_after_seconds cannot be negative."
            )

        self.config_path = Path(config_path)
        self.config: AppConfig = load_config(self.config_path)
        self.handlers = dict(handlers or {})
        self.migration_directory = Path(
            migration_directory
            or DEFAULT_MIGRATION_DIRECTORY
        )
        self.stop_event = stop_event or threading.Event()
        self.abandoned_after_seconds = (
            abandoned_after_seconds
        )
        self._threads: list[threading.Thread] = []

    @property
    def stale_job_threshold_seconds(self) -> int:
        """
        Clearer alias for `abandoned_after_seconds`.

        Read-only on purpose: it is a second *name* for one setting,
        never a second setting. See `__init__`'s docstring.
        """
        return self.abandoned_after_seconds

    def initialize(self) -> list[int]:
        """
        Create workspace directories, apply migrations, and report --
        without changing -- any jobs that currently look stale.

        This method never mutates a `jobs` row. Migrations may write
        (that is their job, and only on a schema upgrade); the
        stale-job check that follows cannot, by construction. See
        `_warn_about_stale_jobs()` for how that is enforced, and this
        module's docstring for why unattended recovery was removed.
        """
        ensure_workspace(self.config)

        with database_connection(
            self.config.workspace.database
        ) as connection:
            applied = apply_migrations(
                connection,
                self.migration_directory,
            )

        if applied:
            log.info(
                "Applied database migrations: %s",
                ", ".join(str(version) for version in applied),
            )
        else:
            log.info("Database schema is current.")

        self._warn_about_stale_jobs()

        return applied

    def _warn_about_stale_jobs(self) -> None:
        """
        Log a warning naming any stale `claimed`/`running` jobs, and
        change nothing.

        Read-only by construction, in three layers:

        - The read/write connection `initialize()` uses for migrations
          is closed *before* this runs. Detection therefore cannot
          reach a connection that is capable of writing, even by
          accident or by a future edit to this method.
        - `readonly_database_connection()` (reused from
          `abandoned_job_audit`) opens a separate connection with
          SQLite's `mode=ro` URI flag -- read-only at the VFS level,
          and refusing to create the file rather than silently
          conjuring an empty database -- plus `PRAGMA query_only = ON`,
          which rejects any writing statement at the statement level.
        - No migrations are applied on that connection; the schema is
          taken exactly as migrations just left it.

        The staleness predicate is not re-derived here.
        `collect_stale_jobs()` is imported from the audit and called
        with the configured threshold, so the service, the audit CLI,
        and the guarded recovery CLI can never disagree about what
        "stale" means -- a disagreement would be exactly the kind of
        silent drift that makes an operator recover the wrong rows.
        """
        database_path = self.config.workspace.database

        try:
            with readonly_database_connection(
                database_path
            ) as connection:
                stale_jobs = collect_stale_jobs(
                    connection,
                    older_than_seconds=(
                        self.abandoned_after_seconds
                    ),
                )
        except FileNotFoundError:
            # mode=ro will not create a missing database. Normally
            # unreachable, because applying migrations just created
            # it; reachable if the file was removed in between. There
            # is nothing to inspect and nothing to warn about.
            log.debug(
                "Skipped startup stale-job detection: database %s "
                "does not exist.",
                database_path,
            )
            return
        except sqlite3.Error as exc:
            # A missing/locked/unreadable `jobs` table must not stop a
            # service from starting: this check is an observation, not
            # a precondition. Logged loudly so a silently skipped
            # check is still visible in the logs.
            log.warning(
                "Startup stale-job detection could not run against "
                "%s (%s). No jobs were inspected and nothing was "
                "changed; run %s in report-only mode to check the "
                "queue manually.",
                database_path,
                exc,
                RECOVERY_CLI_PATH,
            )
            return

        if not stale_jobs:
            return

        log.warning(
            "Detected %s job(s) still marked claimed/running with no "
            "activity for at least %s second(s). They were NOT "
            "recovered: service startup does not modify job rows, and "
            "nothing in this database was changed by this check. %s "
            "Do not assume these jobs are dead. To recover them, an "
            "operator must run %s, which requires reviewing a "
            "report-only preview and then re-running with explicit "
            "confirmation (--confirm plus a matching --expected-count "
            "and --expected-snapshot, and a --workers-stopped "
            "attestation). Stale job ids: %s.",
            len(stale_jobs),
            self.abandoned_after_seconds,
            WORKER_LIVENESS_WARNING,
            RECOVERY_CLI_PATH,
            ", ".join(
                str(job["job_id"]) for job in stale_jobs
            ),
        )

    def run(self) -> None:
        """
        Initialize the service and run worker threads until stopped.

        Workers claim only job types with registered handlers. With no
        handlers registered, the service remains idle and cannot modify
        the comic library.
        """
        self.initialize()

        worker_count = max(
            1,
            self.config.service.cpu_workers,
        )

        log.info(
            "Starting ComicAutomation service with %s worker(s) "
            "in %s mode.",
            worker_count,
            self.config.service.operating_mode,
        )

        self._threads = [
            threading.Thread(
                target=self._run_worker,
                args=(index,),
                name=f"comic-worker-{index + 1}",
                daemon=False,
            )
            for index in range(worker_count)
        ]

        for thread in self._threads:
            thread.start()

        try:
            for thread in self._threads:
                thread.join()
        except KeyboardInterrupt:
            log.info("Keyboard interrupt received.")
            self.request_stop()

            for thread in self._threads:
                thread.join()

        log.info("ComicAutomation service stopped.")

    def request_stop(self) -> None:
        self.stop_event.set()

    def _run_worker(self, index: int) -> None:
        worker_id = self._worker_id(index)

        with database_connection(
            self.config.workspace.database
        ) as connection:
            worker = JobWorker(
                JobQueue(connection),
                self.handlers,
                worker_id=worker_id,
                stop_event=self.stop_event,
                poll_interval_seconds=(
                    self.config.service
                    .poll_interval_seconds
                ),
            )
            worker.run()

    @staticmethod
    def _worker_id(index: int) -> str:
        return (
            f"{socket.gethostname()}:"
            f"{os.getpid()}:"
            f"cpu-{index + 1}"
        )
