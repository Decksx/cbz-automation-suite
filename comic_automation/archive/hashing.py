from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from comic_automation.archive.repository import (
    ArchiveInspectionRepository,
)
from comic_automation.jobs import CategorizedJobError, Job, JobQueue


HASH_ALGORITHM = "sha256"
HASH_ALGORITHM_VERSION = "1"
DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024


@dataclass(frozen=True)
class ArchiveHash:
    algorithm: str
    algorithm_version: str
    digest: str
    file_size: int
    modified_time_ns: int
    bytes_read: int


def calculate_archive_hash(
    path: str | Path,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> ArchiveHash:
    if chunk_size < 1:
        raise ValueError("chunk_size must be at least 1.")

    archive_path = Path(path)
    before = archive_path.stat()
    digest = hashlib.sha256()
    bytes_read = 0

    with archive_path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
            bytes_read += len(chunk)

    after = archive_path.stat()

    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise OSError(
            f"Archive changed while hashing: {archive_path}"
        )

    return ArchiveHash(
        algorithm=HASH_ALGORITHM,
        algorithm_version=HASH_ALGORITHM_VERSION,
        digest=digest.hexdigest(),
        file_size=int(after.st_size),
        modified_time_ns=int(after.st_mtime_ns),
        bytes_read=bytes_read,
    )


class ArchiveHashRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def save(
        self,
        *,
        archive_id: int,
        location_id: int,
        result: ArchiveHash,
    ) -> None:
        previous = self.connection.execute(
            """
            SELECT file_size, modified_time_ns
            FROM file_locations
            WHERE id = ?
            """,
            (location_id,),
        ).fetchone()
        metadata_changed = (
            previous is not None
            and (
                previous["file_size"] != result.file_size
                or previous["modified_time_ns"]
                != result.modified_time_ns
            )
        )

        self.connection.execute(
            """
            INSERT INTO archive_hashes (
                archive_id,
                location_id,
                algorithm,
                algorithm_version,
                digest,
                file_size,
                modified_time_ns,
                bytes_read
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(archive_id) DO UPDATE SET
                location_id = excluded.location_id,
                algorithm = excluded.algorithm,
                algorithm_version = excluded.algorithm_version,
                digest = excluded.digest,
                file_size = excluded.file_size,
                modified_time_ns = excluded.modified_time_ns,
                bytes_read = excluded.bytes_read,
                hashed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                archive_id,
                location_id,
                result.algorithm,
                result.algorithm_version,
                result.digest,
                result.file_size,
                result.modified_time_ns,
                result.bytes_read,
            ),
        )
        self.connection.execute(
            """
            UPDATE file_locations
            SET
                file_size = ?,
                modified_time_ns = ?,
                last_seen_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                result.file_size,
                result.modified_time_ns,
                location_id,
            ),
        )
        self.connection.execute(
            """
            UPDATE archive_files
            SET file_size = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (result.file_size, archive_id),
        )

        if metadata_changed:
            self._enqueue_reinspection_if_absent(archive_id)

    def _enqueue_reinspection_if_absent(
        self,
        archive_id: int,
    ) -> bool:
        existing = self.connection.execute(
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

        if existing is not None:
            return False

        JobQueue(self.connection).enqueue(
            "inspect_archive",
            archive_id=archive_id,
            priority=100,
        )
        return True

    def enqueue_missing(self, *, limit: int | None = None) -> int:
        limit_clause = ""
        parameters: list[int] = []

        if limit is not None:
            if limit < 1:
                raise ValueError("limit must be at least 1.")
            limit_clause = " LIMIT ?"
            parameters.append(limit)

        rows = self.connection.execute(
            f"""
            SELECT ai.archive_id
            FROM archive_inspections AS ai
            JOIN file_locations AS fl
              ON fl.archive_id = ai.archive_id
             AND fl.is_current = 1
            LEFT JOIN archive_hashes AS ah
              ON ah.archive_id = ai.archive_id
             AND ah.file_size = fl.file_size
             AND ah.modified_time_ns = fl.modified_time_ns
            WHERE ai.status IN ('ok', 'no_images', 'empty_archive')
              AND ai.inspected_file_size = fl.file_size
              AND ai.inspected_modified_time_ns = fl.modified_time_ns
              AND ah.id IS NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM jobs AS j
                  WHERE j.archive_id = ai.archive_id
                    AND j.job_type = 'calculate_archive_hash'
                    AND j.status IN ('pending', 'claimed', 'running')
              )
            ORDER BY ai.archive_id
            {limit_clause}
            """,
            parameters,
        ).fetchall()

        queue = JobQueue(self.connection)

        for row in rows:
            queue.enqueue(
                "calculate_archive_hash",
                archive_id=int(row["archive_id"]),
                priority=200,
            )

        return len(rows)

    def duplicate_groups(self) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT
                ah.algorithm,
                ah.digest,
                ah.file_size,
                COUNT(*) AS archive_count,
                GROUP_CONCAT(fl.path, char(10)) AS paths
            FROM archive_hashes AS ah
            JOIN file_locations AS fl
              ON fl.archive_id = ah.archive_id
             AND fl.is_current = 1
            GROUP BY ah.algorithm, ah.digest, ah.file_size
            HAVING COUNT(*) > 1
            ORDER BY archive_count DESC, ah.file_size DESC, ah.digest
            """
        ).fetchall()

        return [
            {
                "algorithm": row["algorithm"],
                "digest": row["digest"],
                "file_size": int(row["file_size"]),
                "archive_count": int(row["archive_count"]),
                "paths": str(row["paths"]).splitlines(),
            }
            for row in rows
        ]


class CalculateArchiveHashHandler:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        self.locations = ArchiveInspectionRepository(connection)
        self.hashes = ArchiveHashRepository(connection)
        self.chunk_size = chunk_size

    def __call__(self, job: Job) -> None:
        if job.archive_id is None:
            raise ValueError(f"Job {job.id} has no archive_id.")

        location = self.locations.current_location(job.archive_id)
        path = Path(str(location["path"]))

        try:
            result = calculate_archive_hash(
                path,
                chunk_size=self.chunk_size,
            )
        except FileNotFoundError as exc:
            raise CategorizedJobError(
                str(exc),
                category="filesystem_not_found",
            ) from exc
        except PermissionError as exc:
            raise CategorizedJobError(
                str(exc),
                category="filesystem_permission",
            ) from exc
        except OSError as exc:
            raise CategorizedJobError(
                str(exc),
                category="filesystem_io",
            ) from exc

        self.hashes.save(
            archive_id=job.archive_id,
            location_id=int(location["id"]),
            result=result,
        )
