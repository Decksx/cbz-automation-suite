from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath


# Only categories with an actual file on disk to move are eligible.
# filesystem_not_found (the file is already gone) can never be
# quarantined -- there is nothing to relocate.
DEFAULT_QUARANTINE_CATEGORIES = frozenset({"corrupt_archive"})

NO_SOURCE_FILE_CATEGORIES = frozenset({"filesystem_not_found"})


class UnsupportedQuarantineCategoryError(ValueError):
    """Raised when asked to quarantine a category with no source file."""


@dataclass(frozen=True)
class QuarantineCandidate:
    archive_id: int
    job_id: int
    source_path: Path
    failure_category: str
    attempts: int
    max_attempts: int
    series_name: str
    proposed_filename: str


@dataclass(frozen=True)
class QuarantineItemResult:
    archive_id: int
    source_path: str
    destination_path: str
    status: str  # "moved" | "error"
    error: str | None = None


def propose_quarantine_filename(series_name: str, filename: str) -> str:
    """
    Build a self-describing quarantine filename: the series name plus
    whatever chapter/volume/episode text the original filename already
    carries.

    If the series name is already present in the filename
    (case-insensitively -- true for the large majority of archives),
    the filename is left unchanged rather than creating a redundant
    "Series - Series Chapter 1.cbz" name. Otherwise the series name is
    prefixed so the file is identifiable outside of its original
    folder structure.
    """
    normalized_series = series_name.strip()

    if "." in filename:
        stem, _, extension = filename.rpartition(".")
        suffix = f".{extension}"
    else:
        stem, suffix = filename, ""

    if not normalized_series:
        return filename

    if normalized_series.casefold() in stem.casefold():
        return filename

    return f"{normalized_series} - {stem}{suffix}"


def resolve_destination_path(
    quarantine_root: Path,
    proposed_filename: str,
    *,
    existing_names: set[str],
) -> Path:
    """
    Pick a collision-free destination path within quarantine_root.

    existing_names is mutated: the chosen name is added to it, so a
    caller resolving destinations for several candidates in one batch
    never proposes the same filename twice, even if two candidates
    started with an identical proposed_filename.
    """
    if "." in proposed_filename:
        stem, _, extension = proposed_filename.rpartition(".")
        suffix = f".{extension}"
    else:
        stem, suffix = proposed_filename, ""

    candidate = proposed_filename
    counter = 2

    while candidate.casefold() in existing_names:
        candidate = f"{stem} ({counter}){suffix}"
        counter += 1

    existing_names.add(candidate.casefold())
    return quarantine_root / candidate


class QuarantineRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def find_candidates(
        self,
        *,
        categories: frozenset[str] = DEFAULT_QUARANTINE_CATEGORIES,
        exclude_series: frozenset[str] = frozenset(),
    ) -> list[QuarantineCandidate]:
        unsupported = categories & NO_SOURCE_FILE_CATEGORIES

        if unsupported:
            raise UnsupportedQuarantineCategoryError(
                "Cannot quarantine categories with no source file to "
                f"move: {sorted(unsupported)}"
            )

        if not categories:
            return []

        placeholders = ",".join("?" for _ in categories)

        # NOT EXISTS against archive_quarantine keeps an
        # already-quarantined archive from being offered again on a
        # later run.
        rows = self.connection.execute(
            f"""
            SELECT
                j.id AS job_id,
                j.archive_id,
                j.failure_category,
                j.attempts,
                j.max_attempts,
                fl.path
            FROM jobs AS j
            JOIN file_locations AS fl
              ON fl.archive_id = j.archive_id
             AND fl.is_current = 1
            WHERE j.job_type = 'inspect_archive'
              AND j.status = 'failed'
              AND j.failure_category IN ({placeholders})
              AND NOT EXISTS (
                  SELECT 1
                  FROM archive_quarantine AS q
                  WHERE q.archive_id = j.archive_id
              )
            ORDER BY fl.path
            """,
            tuple(categories),
        ).fetchall()

        normalized_exclusions = {
            value.casefold() for value in exclude_series
        }

        candidates: list[QuarantineCandidate] = []

        for row in rows:
            raw_path = str(row["path"])

            # Stored paths are always Windows-style (this project is
            # Windows-first; see docs/architecture.md), regardless of
            # what platform this code happens to run on. Parsing with
            # PureWindowsPath -- rather than the ambient Path, which
            # would be PurePosixPath on a non-Windows host and would
            # treat a whole "X:\Manga\Series\file.cbz" string as one
            # unsplit component -- keeps series/filename extraction
            # correct everywhere. PureWindowsPath also accepts forward
            # slashes, so this doesn't break on POSIX-style test
            # fixtures either. Actual filesystem operations still go
            # through the platform-native Path below.
            parsed = PureWindowsPath(raw_path)
            series_name = parsed.parent.name
            filename = parsed.name

            if series_name.casefold() in normalized_exclusions:
                continue

            candidates.append(
                QuarantineCandidate(
                    archive_id=int(row["archive_id"]),
                    job_id=int(row["job_id"]),
                    source_path=Path(raw_path),
                    failure_category=str(row["failure_category"]),
                    attempts=int(row["attempts"]),
                    max_attempts=int(row["max_attempts"]),
                    series_name=series_name,
                    proposed_filename=propose_quarantine_filename(
                        series_name,
                        filename,
                    ),
                )
            )

        return candidates

    def record_quarantine(
        self,
        *,
        archive_id: int,
        job_id: int,
        source_path: Path,
        destination_path: Path,
        failure_category: str,
    ) -> None:
        location = self.connection.execute(
            """
            SELECT id
            FROM file_locations
            WHERE archive_id = ?
              AND is_current = 1
            """,
            (archive_id,),
        ).fetchone()

        # The archive is leaving the live library entirely, so its
        # current file_locations row is retired rather than replaced --
        # a quarantine folder is intentionally not part of any scanned
        # library root.
        if location is not None:
            self.connection.execute(
                """
                UPDATE file_locations
                SET is_current = 0, last_seen_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (int(location["id"]),),
            )

        self.connection.execute(
            """
            INSERT INTO archive_quarantine (
                archive_id,
                source_path,
                quarantine_path,
                failure_category,
                job_id
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                archive_id,
                str(source_path),
                str(destination_path),
                failure_category,
                job_id,
            ),
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
            VALUES (?, 'quarantined', ?, ?, ?)
            """,
            (
                archive_id,
                str(source_path),
                str(destination_path),
                json.dumps(
                    {
                        "failure_category": failure_category,
                        "job_id": job_id,
                    },
                    sort_keys=True,
                ),
            ),
        )

    def pending_redownload_count(self) -> int:
        return int(
            self.connection.execute(
                """
                SELECT COUNT(*)
                FROM archive_quarantine
                WHERE status = 'pending_redownload'
                """
            ).fetchone()[0]
        )


def execute_quarantine(
    connection: sqlite3.Connection,
    candidates: list[QuarantineCandidate],
    *,
    quarantine_root: Path,
    limit: int | None = None,
) -> list[QuarantineItemResult]:
    """
    Physically move each candidate's file into quarantine_root and
    record the move, up to `limit` items (a bounded batch, matching
    the rest of this project's guarded operations).

    Each item is handled independently: a failure moving or recording
    one archive does not stop the batch, and is reported as an
    "error" result rather than raised. If the filesystem move
    succeeds but the database update fails, the file is moved back to
    its original location so the filesystem and database never
    disagree about where an archive lives.
    """
    quarantine_root.mkdir(parents=True, exist_ok=True)

    existing_names = {
        path.name.casefold()
        for path in quarantine_root.iterdir()
        if path.is_file()
    }

    repository = QuarantineRepository(connection)
    bounded = (
        candidates
        if limit is None
        else candidates[:limit]
    )
    results: list[QuarantineItemResult] = []

    for candidate in bounded:
        destination = resolve_destination_path(
            quarantine_root,
            candidate.proposed_filename,
            existing_names=existing_names,
        )

        if not candidate.source_path.is_file():
            results.append(
                QuarantineItemResult(
                    archive_id=candidate.archive_id,
                    source_path=str(candidate.source_path),
                    destination_path=str(destination),
                    status="error",
                    error="Source file no longer exists.",
                )
            )
            continue

        try:
            shutil.move(
                str(candidate.source_path),
                str(destination),
            )
        except OSError as exc:
            results.append(
                QuarantineItemResult(
                    archive_id=candidate.archive_id,
                    source_path=str(candidate.source_path),
                    destination_path=str(destination),
                    status="error",
                    error=str(exc),
                )
            )
            continue

        try:
            connection.execute("BEGIN IMMEDIATE")
            repository.record_quarantine(
                archive_id=candidate.archive_id,
                job_id=candidate.job_id,
                source_path=candidate.source_path,
                destination_path=destination,
                failure_category=candidate.failure_category,
            )
            connection.execute("COMMIT")
        except Exception as exc:
            if connection.in_transaction:
                connection.execute("ROLLBACK")

            # Database update failed after the file was already moved:
            # move it back so the filesystem doesn't drift from what
            # the database (still) believes.
            try:
                shutil.move(
                    str(destination),
                    str(candidate.source_path),
                )
            except OSError:
                pass

            results.append(
                QuarantineItemResult(
                    archive_id=candidate.archive_id,
                    source_path=str(candidate.source_path),
                    destination_path=str(destination),
                    status="error",
                    error=f"Database update failed: {exc}",
                )
            )
            continue

        results.append(
            QuarantineItemResult(
                archive_id=candidate.archive_id,
                source_path=str(candidate.source_path),
                destination_path=str(destination),
                status="moved",
            )
        )

    return results
