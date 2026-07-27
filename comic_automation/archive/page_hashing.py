from __future__ import annotations

import hashlib
import re
import sqlite3
import zipfile
from dataclasses import dataclass
from pathlib import Path

from comic_automation.archive.inspection import IMAGE_EXTENSIONS
from comic_automation.archive.repository import (
    ArchiveInspectionRepository,
)
from comic_automation.jobs import (
    CategorizedJobError,
    Job,
    JobQueue,
    PermanentJobError,
)


PAGE_HASH_ALGORITHM = "sha256"
PAGE_HASH_ALGORITHM_VERSION = "1"
CONTENT_SIGNATURE_ALGORITHM = "ordered-page-sha256"
CONTENT_SIGNATURE_VERSION = "1"
DEFAULT_CHUNK_SIZE = 1024 * 1024
_NATURAL_PART_RE = re.compile(r"(\d+)")


@dataclass(frozen=True)
class PageContentHash:
    page_index: int
    entry_name: str
    entry_size: int
    compressed_size: int
    crc32: int
    digest: str
    bytes_read: int


@dataclass(frozen=True)
class ArchivePageHashes:
    pages: tuple[PageContentHash, ...]
    content_digest: str
    image_bytes: int
    source_file_size: int
    source_modified_time_ns: int

    @property
    def page_count(self) -> int:
        return len(self.pages)


def _natural_key(value: str) -> tuple[tuple[int, object], ...]:
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part.casefold())
        for part in _NATURAL_PART_RE.split(value.replace("\\", "/"))
        if part
    )


def calculate_page_hashes(
    path: str | Path,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> ArchivePageHashes:
    if chunk_size < 1:
        raise ValueError("chunk_size must be at least 1.")

    archive_path = Path(path)

    if archive_path.suffix.casefold() != ".cbz":
        raise ValueError(
            f"Unsupported archive format: "
            f"{archive_path.suffix or '<none>'}"
        )

    before = archive_path.stat()
    pages: list[PageContentHash] = []

    try:
        with zipfile.ZipFile(archive_path, mode="r") as archive:
            entries = sorted(
                (
                    entry
                    for entry in archive.infolist()
                    if not entry.is_dir()
                    and Path(entry.filename).suffix.casefold()
                    in IMAGE_EXTENSIONS
                ),
                key=lambda entry: _natural_key(entry.filename),
            )

            for page_index, entry in enumerate(entries):
                digest = hashlib.sha256()
                bytes_read = 0

                with archive.open(entry, mode="r") as stream:
                    while chunk := stream.read(chunk_size):
                        digest.update(chunk)
                        bytes_read += len(chunk)

                pages.append(
                    PageContentHash(
                        page_index=page_index,
                        entry_name=entry.filename,
                        entry_size=int(entry.file_size),
                        compressed_size=int(entry.compress_size),
                        crc32=int(entry.CRC),
                        digest=digest.hexdigest(),
                        bytes_read=bytes_read,
                    )
                )
    except zipfile.BadZipFile as exc:
        raise PermanentJobError(
            f"Invalid or corrupt CBZ archive: {archive_path}",
            category="archive_corrupt",
        ) from exc
    except RuntimeError as exc:
        raise PermanentJobError(
            f"Unreadable CBZ entry in {archive_path}: {exc}",
            category="archive_unreadable",
        ) from exc

    after = archive_path.stat()

    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise OSError(
            f"Archive changed while hashing pages: {archive_path}"
        )

    signature = hashlib.sha256()
    signature.update(len(pages).to_bytes(8, "big"))

    for page in pages:
        signature.update(bytes.fromhex(page.digest))

    return ArchivePageHashes(
        pages=tuple(pages),
        content_digest=signature.hexdigest(),
        image_bytes=sum(page.bytes_read for page in pages),
        source_file_size=int(after.st_size),
        source_modified_time_ns=int(after.st_mtime_ns),
    )


class ArchivePageHashRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def save(
        self,
        *,
        archive_id: int,
        location_id: int,
        result: ArchivePageHashes,
    ) -> None:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                "DELETE FROM archive_pages WHERE archive_id = ?",
                (archive_id,),
            )

            for page in result.pages:
                cursor = self.connection.execute(
                    """
                    INSERT INTO archive_pages (
                        archive_id,
                        location_id,
                        page_index,
                        entry_name,
                        entry_size,
                        compressed_size,
                        crc32
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        archive_id,
                        location_id,
                        page.page_index,
                        page.entry_name,
                        page.entry_size,
                        page.compressed_size,
                        page.crc32,
                    ),
                )
                self.connection.execute(
                    """
                    INSERT INTO page_hashes (
                        page_id,
                        algorithm,
                        algorithm_version,
                        digest,
                        bytes_read
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        int(cursor.lastrowid),
                        PAGE_HASH_ALGORITHM,
                        PAGE_HASH_ALGORITHM_VERSION,
                        page.digest,
                        page.bytes_read,
                    ),
                )

            self.connection.execute(
                """
                INSERT INTO archive_content_signatures (
                    archive_id,
                    location_id,
                    algorithm,
                    algorithm_version,
                    digest,
                    page_count,
                    image_bytes,
                    source_file_size,
                    source_modified_time_ns
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(archive_id) DO UPDATE SET
                    location_id = excluded.location_id,
                    algorithm = excluded.algorithm,
                    algorithm_version = excluded.algorithm_version,
                    digest = excluded.digest,
                    page_count = excluded.page_count,
                    image_bytes = excluded.image_bytes,
                    source_file_size = excluded.source_file_size,
                    source_modified_time_ns =
                        excluded.source_modified_time_ns,
                    calculated_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    archive_id,
                    location_id,
                    CONTENT_SIGNATURE_ALGORITHM,
                    CONTENT_SIGNATURE_VERSION,
                    result.content_digest,
                    result.page_count,
                    result.image_bytes,
                    result.source_file_size,
                    result.source_modified_time_ns,
                ),
            )
            self.connection.execute(
                """
                UPDATE archive_files
                SET
                    content_signature = ?,
                    page_count = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    result.content_digest,
                    result.page_count,
                    archive_id,
                ),
            )
            self.connection.execute("COMMIT")
        except Exception:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

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
            SELECT ah.archive_id
            FROM archive_hashes AS ah
            JOIN file_locations AS fl
              ON fl.archive_id = ah.archive_id
             AND fl.is_current = 1
            LEFT JOIN archive_content_signatures AS acs
              ON acs.archive_id = ah.archive_id
             AND acs.source_file_size = fl.file_size
             AND acs.source_modified_time_ns = fl.modified_time_ns
            WHERE ah.file_size = fl.file_size
              AND ah.modified_time_ns = fl.modified_time_ns
              AND acs.id IS NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM jobs AS j
                  WHERE j.archive_id = ah.archive_id
                    AND j.job_type = 'hash_archive_pages'
                    AND j.status IN ('pending', 'claimed', 'running')
              )
            ORDER BY ah.archive_id
            {limit_clause}
            """,
            parameters,
        ).fetchall()
        queue = JobQueue(self.connection)

        for row in rows:
            queue.enqueue(
                "hash_archive_pages",
                archive_id=int(row["archive_id"]),
                priority=300,
            )

        return len(rows)

    def duplicate_content_groups(self) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT
                acs.algorithm,
                acs.algorithm_version,
                acs.digest,
                acs.page_count,
                COUNT(*) AS archive_count,
                GROUP_CONCAT(fl.path, char(10)) AS paths
            FROM archive_content_signatures AS acs
            JOIN file_locations AS fl
              ON fl.archive_id = acs.archive_id
             AND fl.is_current = 1
            GROUP BY
                acs.algorithm,
                acs.algorithm_version,
                acs.digest,
                acs.page_count
            HAVING COUNT(*) > 1
            ORDER BY archive_count DESC, acs.page_count DESC, acs.digest
            """
        ).fetchall()

        return [
            {
                "algorithm": row["algorithm"],
                "algorithm_version": row["algorithm_version"],
                "digest": row["digest"],
                "page_count": int(row["page_count"]),
                "archive_count": int(row["archive_count"]),
                "paths": str(row["paths"]).splitlines(),
            }
            for row in rows
        ]


class HashArchivePagesHandler:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        self.locations = ArchiveInspectionRepository(connection)
        self.pages = ArchivePageHashRepository(connection)
        self.chunk_size = chunk_size

    def __call__(self, job: Job) -> None:
        if job.archive_id is None:
            raise ValueError(f"Job {job.id} has no archive_id.")

        location = self.locations.current_location(job.archive_id)
        path = Path(str(location["path"]))

        try:
            result = calculate_page_hashes(
                path,
                chunk_size=self.chunk_size,
            )
        except PermanentJobError:
            raise
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

        self.pages.save(
            archive_id=job.archive_id,
            location_id=int(location["id"]),
            result=result,
        )
