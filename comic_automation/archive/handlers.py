from __future__ import annotations

import sqlite3
from pathlib import Path

from comic_automation.archive.inspection import inspect_archive
from comic_automation.archive.repository import (
    ArchiveInspectionRepository,
)
from comic_automation.jobs.models import Job


class InvalidInspectionJobError(ValueError):
    """Raised when an inspection job lacks an archive reference."""


class InspectArchiveHandler:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        verify_crc: bool = False,
    ) -> None:
        self.repository = ArchiveInspectionRepository(connection)
        self.verify_crc = verify_crc

    def __call__(self, job: Job) -> None:
        if job.archive_id is None:
            raise InvalidInspectionJobError(
                f"Job {job.id} has no archive_id."
            )

        location = self.repository.current_location(job.archive_id)
        path = Path(str(location["path"]))

        result = inspect_archive(
            path,
            verify_crc=self.verify_crc,
        )

        self.repository.save(
            archive_id=job.archive_id,
            location_id=int(location["id"]),
            result=result,
            file_size=(
                int(location["file_size"])
                if location["file_size"] is not None
                else None
            ),
            modified_time_ns=(
                int(location["modified_time_ns"])
                if location["modified_time_ns"] is not None
                else None
            ),
        )
