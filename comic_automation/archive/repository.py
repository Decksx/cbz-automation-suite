from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from comic_automation.archive.inspection import ArchiveInspection


class ArchiveLocationNotFoundError(LookupError):
    """Raised when an archive has no current filesystem location."""


class ArchiveInspectionRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def current_location(
        self,
        archive_id: int,
    ) -> sqlite3.Row:
        row = self.connection.execute(
            """
            SELECT
                id,
                archive_id,
                path,
                file_size,
                modified_time_ns
            FROM file_locations
            WHERE archive_id = ?
              AND is_current = 1
            ORDER BY last_seen_at DESC, id DESC
            LIMIT 1
            """,
            (archive_id,),
        ).fetchone()

        if row is None:
            raise ArchiveLocationNotFoundError(
                f"Archive {archive_id} has no current location."
            )

        return row

    def save(
        self,
        *,
        archive_id: int,
        location_id: int,
        result: ArchiveInspection,
        file_size: int | None,
        modified_time_ns: int | None,
    ) -> None:
        comic_info_json = (
            json.dumps(
                asdict(result.comic_info),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if result.comic_info is not None
            else None
        )

        self.connection.execute(
            """
            INSERT INTO archive_inspections (
                archive_id,
                location_id,
                inspected_path,
                archive_format,
                status,
                entry_count,
                page_count,
                directory_count,
                encrypted,
                comic_info_present,
                comic_info_valid,
                comic_info_error,
                comic_info_json,
                crc_verified,
                inspected_file_size,
                inspected_modified_time_ns,
                result_json,
                inspected_at,
                created_at,
                updated_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            )
            ON CONFLICT(archive_id) DO UPDATE SET
                location_id = excluded.location_id,
                inspected_path = excluded.inspected_path,
                archive_format = excluded.archive_format,
                status = excluded.status,
                entry_count = excluded.entry_count,
                page_count = excluded.page_count,
                directory_count = excluded.directory_count,
                encrypted = excluded.encrypted,
                comic_info_present = excluded.comic_info_present,
                comic_info_valid = excluded.comic_info_valid,
                comic_info_error = excluded.comic_info_error,
                comic_info_json = excluded.comic_info_json,
                crc_verified = excluded.crc_verified,
                inspected_file_size = excluded.inspected_file_size,
                inspected_modified_time_ns =
                    excluded.inspected_modified_time_ns,
                result_json = excluded.result_json,
                inspected_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                archive_id,
                location_id,
                str(Path(result.path)),
                result.archive_format,
                result.status,
                result.entry_count,
                result.page_count,
                result.directory_count,
                int(result.encrypted),
                int(result.comic_info_present),
                int(result.comic_info_valid),
                result.comic_info_error,
                comic_info_json,
                int(result.crc_verified),
                file_size,
                modified_time_ns,
                result.to_json(),
            ),
        )

        self.connection.execute(
            """
            UPDATE archive_files
            SET
                page_count = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (result.page_count, archive_id),
        )
