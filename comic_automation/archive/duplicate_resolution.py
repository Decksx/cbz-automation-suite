from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from comic_automation.archive.hashing import calculate_archive_hash


ACTIVE_JOB_STATUSES = frozenset({"pending", "claimed", "running"})


@dataclass(frozen=True)
class DuplicateResolutionCandidate:
    source_archive_id: int
    source_location_id: int
    source_path: Path
    counterpart_archive_id: int | None
    counterpart_location_id: int | None
    counterpart_path: Path | None
    digest: str | None
    file_size: int
    source_modified_time_ns: int | None
    counterpart_modified_time_ns: int | None
    status: str
    error: str | None = None


@dataclass(frozen=True)
class DuplicateResolutionResult:
    source_archive_id: int
    source_path: str
    counterpart_path: str | None
    backup_path: str | None
    status: str
    error: str | None = None


def path_is_within(path: Path, root: Path) -> bool:
    resolved_path = path.resolve(strict=False)
    resolved_root = root.resolve(strict=False)

    try:
        resolved_path.relative_to(resolved_root)
    except ValueError:
        return False

    return True


class DuplicateResolutionRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def build_plan(
        self,
        *,
        extraneous_root: Path,
    ) -> list[DuplicateResolutionCandidate]:
        rows = self.connection.execute(
            """
            SELECT
                af.id AS archive_id,
                af.file_size AS archive_file_size,
                fl.id AS location_id,
                fl.path,
                fl.file_size AS location_file_size,
                fl.modified_time_ns,
                ah.location_id AS hash_location_id,
                ah.algorithm,
                ah.digest,
                ah.file_size AS hash_file_size,
                ah.modified_time_ns AS hash_modified_time_ns
            FROM archive_files AS af
            JOIN file_locations AS fl
              ON fl.archive_id = af.id
             AND fl.is_current = 1
            LEFT JOIN archive_hashes AS ah
              ON ah.archive_id = af.id
            ORDER BY fl.path
            """
        ).fetchall()

        records = [dict(row) for row in rows]
        plan: list[DuplicateResolutionCandidate] = []

        for source in records:
            source_path = Path(str(source["path"]))

            if not path_is_within(source_path, extraneous_root):
                continue

            error = self._stored_hash_error(source)
            counterparts: list[dict] = []

            if error is None:
                counterparts = [
                    record
                    for record in records
                    if int(record["archive_id"])
                    != int(source["archive_id"])
                    and not path_is_within(
                        Path(str(record["path"])),
                        extraneous_root,
                    )
                    and self._stored_hash_error(record) is None
                    and record["algorithm"] == source["algorithm"]
                    and record["digest"] == source["digest"]
                    and int(record["hash_file_size"])
                    == int(source["hash_file_size"])
                ]

                if len(counterparts) != 1:
                    error = (
                        "Expected exactly one current organized "
                        "counterpart with the same SHA-256 and size; "
                        f"found {len(counterparts)}."
                    )

            counterpart = (
                counterparts[0]
                if len(counterparts) == 1
                else None
            )

            if error is None and counterpart is not None:
                active_jobs = self._active_job_count(
                    int(source["archive_id"]),
                    int(counterpart["archive_id"]),
                )
                if active_jobs:
                    error = (
                        "Source or organized counterpart has "
                        f"{active_jobs} active job(s)."
                    )

            plan.append(
                DuplicateResolutionCandidate(
                    source_archive_id=int(source["archive_id"]),
                    source_location_id=int(source["location_id"]),
                    source_path=source_path,
                    counterpart_archive_id=(
                        int(counterpart["archive_id"])
                        if counterpart is not None
                        else None
                    ),
                    counterpart_location_id=(
                        int(counterpart["location_id"])
                        if counterpart is not None
                        else None
                    ),
                    counterpart_path=(
                        Path(str(counterpart["path"]))
                        if counterpart is not None
                        else None
                    ),
                    digest=(
                        str(source["digest"])
                        if source["digest"] is not None
                        else None
                    ),
                    file_size=int(source["location_file_size"]),
                    source_modified_time_ns=(
                        int(source["modified_time_ns"])
                        if source["modified_time_ns"] is not None
                        else None
                    ),
                    counterpart_modified_time_ns=(
                        int(counterpart["modified_time_ns"])
                        if counterpart is not None
                        and counterpart["modified_time_ns"] is not None
                        else None
                    ),
                    status="blocked" if error else "planned",
                    error=error,
                )
            )

        return plan

    def _stored_hash_error(self, record: dict) -> str | None:
        if (
            record["digest"] is None
            or record["algorithm"] != "sha256"
        ):
            return "Missing a stored SHA-256 hash."

        if record["location_file_size"] is None:
            return "Current location has no stored file size."

        if record["modified_time_ns"] is None:
            return "Current location has no stored modified time."

        if (
            record["hash_location_id"] != record["location_id"]
            or record["hash_file_size"]
            != record["location_file_size"]
            or record["hash_modified_time_ns"]
            != record["modified_time_ns"]
            or record["archive_file_size"]
            != record["location_file_size"]
        ):
            return "Stored SHA-256 or file metadata is stale."

        return None

    def _active_job_count(
        self,
        source_archive_id: int,
        counterpart_archive_id: int,
    ) -> int:
        placeholders = ",".join("?" for _ in ACTIVE_JOB_STATUSES)
        parameters: tuple[object, ...] = (
            source_archive_id,
            counterpart_archive_id,
            *sorted(ACTIVE_JOB_STATUSES),
        )
        return int(
            self.connection.execute(
                f"""
                SELECT COUNT(*)
                FROM jobs
                WHERE archive_id IN (?, ?)
                  AND status IN ({placeholders})
                """,
                parameters,
            ).fetchone()[0]
        )

    def validate_candidate(
        self,
        candidate: DuplicateResolutionCandidate,
    ) -> None:
        if (
            candidate.status != "planned"
            or candidate.counterpart_archive_id is None
            or candidate.counterpart_location_id is None
            or candidate.counterpart_path is None
            or candidate.digest is None
            or candidate.source_modified_time_ns is None
            or candidate.counterpart_modified_time_ns is None
        ):
            raise RuntimeError(
                candidate.error or "Candidate is not executable."
            )

        rows = self.connection.execute(
            """
            SELECT
                fl.archive_id,
                fl.id AS location_id,
                fl.path,
                fl.file_size,
                fl.modified_time_ns,
                ah.location_id AS hash_location_id,
                ah.algorithm,
                ah.digest,
                ah.file_size AS hash_file_size,
                ah.modified_time_ns AS hash_modified_time_ns
            FROM file_locations AS fl
            JOIN archive_hashes AS ah
              ON ah.archive_id = fl.archive_id
            WHERE fl.id IN (?, ?)
              AND fl.is_current = 1
            ORDER BY fl.id
            """,
            (
                candidate.source_location_id,
                candidate.counterpart_location_id,
            ),
        ).fetchall()

        if len(rows) != 2:
            raise RuntimeError(
                "A source or counterpart location is no longer current."
            )

        expected = {
            candidate.source_location_id: (
                candidate.source_archive_id,
                str(candidate.source_path),
                candidate.source_modified_time_ns,
            ),
            candidate.counterpart_location_id: (
                candidate.counterpart_archive_id,
                str(candidate.counterpart_path),
                candidate.counterpart_modified_time_ns,
            ),
        }

        for row in rows:
            location_id = int(row["location_id"])
            archive_id, path, modified_time_ns = expected[location_id]

            if (
                int(row["archive_id"]) != archive_id
                or str(row["path"]) != path
                or int(row["file_size"]) != candidate.file_size
                or int(row["modified_time_ns"]) != modified_time_ns
                or int(row["hash_location_id"]) != location_id
                or str(row["algorithm"]) != "sha256"
                or str(row["digest"]) != candidate.digest
                or int(row["hash_file_size"]) != candidate.file_size
                or int(row["hash_modified_time_ns"])
                != modified_time_ns
            ):
                raise RuntimeError(
                    "Candidate metadata changed after planning."
                )

        if self._active_job_count(
            candidate.source_archive_id,
            candidate.counterpart_archive_id,
        ):
            raise RuntimeError(
                "Source or organized counterpart now has an active job."
            )

    def record_removal(
        self,
        candidate: DuplicateResolutionCandidate,
        *,
        backup_path: Path,
    ) -> None:
        cursor = self.connection.execute(
            """
            UPDATE file_locations
            SET is_current = 0, last_seen_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND archive_id = ?
              AND is_current = 1
            """,
            (
                candidate.source_location_id,
                candidate.source_archive_id,
            ),
        )

        if cursor.rowcount != 1:
            raise RuntimeError(
                "Source location was not current during retirement."
            )

        self.connection.execute(
            """
            INSERT INTO file_events (
                archive_id,
                event_type,
                source_path,
                destination_path,
                details_json
            )
            VALUES (?, 'duplicate_removed', ?, ?, ?)
            """,
            (
                candidate.source_archive_id,
                str(candidate.source_path),
                str(backup_path),
                json.dumps(
                    {
                        "algorithm": "sha256",
                        "digest": candidate.digest,
                        "file_size": candidate.file_size,
                        "organized_archive_id": (
                            candidate.counterpart_archive_id
                        ),
                        "organized_path": str(
                            candidate.counterpart_path
                        ),
                        "removal_mode": "recoverable_backup_move",
                    },
                    sort_keys=True,
                ),
            ),
        )

    def removal_is_consistent(
        self,
        candidate: DuplicateResolutionCandidate,
        *,
        backup_path: Path,
    ) -> bool:
        row = self.connection.execute(
            """
            SELECT is_current
            FROM file_locations
            WHERE id = ? AND archive_id = ?
            """,
            (
                candidate.source_location_id,
                candidate.source_archive_id,
            ),
        ).fetchone()
        counterpart = self.connection.execute(
            """
            SELECT is_current
            FROM file_locations
            WHERE id = ? AND archive_id = ?
            """,
            (
                candidate.counterpart_location_id,
                candidate.counterpart_archive_id,
            ),
        ).fetchone()

        return (
            row is not None
            and int(row["is_current"]) == 0
            and counterpart is not None
            and int(counterpart["is_current"]) == 1
            and not candidate.source_path.exists()
            and backup_path.is_file()
            and candidate.counterpart_path is not None
            and candidate.counterpart_path.is_file()
        )


def _verify_live_files(
    candidate: DuplicateResolutionCandidate,
    *,
    extraneous_root: Path,
) -> None:
    counterpart = candidate.counterpart_path

    if counterpart is None or candidate.digest is None:
        raise RuntimeError("Candidate has no organized counterpart.")

    if not path_is_within(candidate.source_path, extraneous_root):
        raise RuntimeError(
            "Source path escapes the configured _extraneous root."
        )

    if path_is_within(counterpart, extraneous_root):
        raise RuntimeError(
            "Organized counterpart is inside the _extraneous root."
        )

    if not candidate.source_path.is_file():
        raise FileNotFoundError(
            f"Source no longer exists: {candidate.source_path}"
        )

    if not counterpart.is_file():
        raise FileNotFoundError(
            f"Organized counterpart no longer exists: {counterpart}"
        )

    source_stat = candidate.source_path.stat()
    counterpart_stat = counterpart.stat()

    if (
        source_stat.st_size != candidate.file_size
        or source_stat.st_mtime_ns
        != candidate.source_modified_time_ns
        or counterpart_stat.st_size != candidate.file_size
        or counterpart_stat.st_mtime_ns
        != candidate.counterpart_modified_time_ns
    ):
        raise RuntimeError(
            "Source or counterpart metadata changed after planning."
        )

    source_hash = calculate_archive_hash(candidate.source_path)
    counterpart_hash = calculate_archive_hash(counterpart)

    if (
        source_hash.digest != candidate.digest
        or counterpart_hash.digest != candidate.digest
        or source_hash.digest != counterpart_hash.digest
        or source_hash.file_size != candidate.file_size
        or counterpart_hash.file_size != candidate.file_size
    ):
        raise RuntimeError(
            "Fresh SHA-256 verification did not match the stored "
            "duplicate digest and size."
        )


def execute_duplicate_resolution(
    connection: sqlite3.Connection,
    candidates: list[DuplicateResolutionCandidate],
    *,
    extraneous_root: Path,
    removed_files_root: Path,
) -> list[DuplicateResolutionResult]:
    repository = DuplicateResolutionRepository(connection)
    results: list[DuplicateResolutionResult] = []

    for candidate in candidates:
        if candidate.status != "planned":
            continue

        backup_path: Path | None = None

        try:
            relative_path = candidate.source_path.resolve(
                strict=False
            ).relative_to(extraneous_root.resolve(strict=False))
            backup_path = removed_files_root / relative_path

            if backup_path.exists():
                raise FileExistsError(
                    f"Backup destination already exists: {backup_path}"
                )

            repository.validate_candidate(candidate)
            _verify_live_files(
                candidate,
                extraneous_root=extraneous_root,
            )
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(candidate.source_path), str(backup_path))
        except Exception as exc:
            results.append(
                DuplicateResolutionResult(
                    source_archive_id=candidate.source_archive_id,
                    source_path=str(candidate.source_path),
                    counterpart_path=(
                        str(candidate.counterpart_path)
                        if candidate.counterpart_path is not None
                        else None
                    ),
                    backup_path=(
                        str(backup_path)
                        if backup_path is not None
                        else None
                    ),
                    status="error",
                    error=str(exc),
                )
            )
            continue

        assert backup_path is not None

        try:
            connection.execute("BEGIN IMMEDIATE")
            repository.record_removal(
                candidate,
                backup_path=backup_path,
            )
            connection.execute("COMMIT")
        except Exception as exc:
            if connection.in_transaction:
                connection.execute("ROLLBACK")

            restore_error: str | None = None
            try:
                candidate.source_path.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )
                shutil.move(
                    str(backup_path),
                    str(candidate.source_path),
                )
            except OSError as restore_exc:
                restore_error = str(restore_exc)

            error = f"Database update failed: {exc}"
            if restore_error is not None:
                error += f"; source restore also failed: {restore_error}"

            results.append(
                DuplicateResolutionResult(
                    source_archive_id=candidate.source_archive_id,
                    source_path=str(candidate.source_path),
                    counterpart_path=str(candidate.counterpart_path),
                    backup_path=str(backup_path),
                    status="error",
                    error=error,
                )
            )
            continue

        if not repository.removal_is_consistent(
            candidate,
            backup_path=backup_path,
        ):
            results.append(
                DuplicateResolutionResult(
                    source_archive_id=candidate.source_archive_id,
                    source_path=str(candidate.source_path),
                    counterpart_path=str(candidate.counterpart_path),
                    backup_path=str(backup_path),
                    status="error",
                    error=(
                        "Post-removal filesystem/database consistency "
                        "check failed."
                    ),
                )
            )
            continue

        results.append(
            DuplicateResolutionResult(
                source_archive_id=candidate.source_archive_id,
                source_path=str(candidate.source_path),
                counterpart_path=str(candidate.counterpart_path),
                backup_path=str(backup_path),
                status="backed_up",
            )
        )

    return results
