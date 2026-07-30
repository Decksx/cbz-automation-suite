from __future__ import annotations

import logging
import os
import socket
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


log = logging.getLogger(__name__)

DEFAULT_MIGRATION_DIRECTORY = (
    Path(__file__).resolve().parent
    / "database"
    / "migrations"
)


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

    def initialize(self) -> list[int]:
        """
        Create workspace directories, apply migrations, and recover
        jobs abandoned by interrupted workers.
        """
        ensure_workspace(self.config)

        with database_connection(
            self.config.workspace.database
        ) as connection:
            applied = apply_migrations(
                connection,
                self.migration_directory,
            )

            recovered = JobQueue(
                connection
            ).recover_abandoned(
                older_than_seconds=(
                    self.abandoned_after_seconds
                )
            )

        if applied:
            log.info(
                "Applied database migrations: %s",
                ", ".join(str(version) for version in applied),
            )
        else:
            log.info("Database schema is current.")

        if recovered:
            log.warning(
                "Recovered %s abandoned job(s).",
                recovered,
            )

        return applied

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
