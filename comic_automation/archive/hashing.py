from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from comic_automation.archive.repository import (
    ArchiveInspectionRepository,
)
from comic_automation.database.dal import (
    RevisionRepository,
    require_transaction,
    transaction,
)
from comic_automation.archive.repository import (
    ArchiveLocationNotFoundError,
)
from comic_automation.jobs import (
    CategorizedJobError,
    EnqueueOutcome,
    Job,
    JobQueue,
)


class SourceChangedError(OSError):
    """The archive or its recorded location moved after it was hashed.

    An OSError subclass so it classifies as `filesystem_io` alongside every
    other transient source problem, and is retried rather than treated as a
    permanent failure: the next attempt re-hashes whatever is there now.
    """


def _as_categorized(error: OSError) -> CategorizedJobError:
    """Map a filesystem error onto the queue's stable categories.

    Collected in one place so the checks added around the write transaction
    classify identically to the hashing read that precedes them -- two
    spellings of the same failure would be triaged as two different problems.
    """
    if isinstance(error, FileNotFoundError):
        category = "filesystem_not_found"
    elif isinstance(error, PermissionError):
        category = "filesystem_permission"
    else:
        category = "filesystem_io"

    return CategorizedJobError(str(error), category=category)


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
        enqueue_reinspection: bool = True,
    ) -> None:
        # This writes four things that have to agree: the archive_hashes row,
        # the location's observed size/mtime, the denormalized file_size on
        # archive_files, and the archive's revision. Before revisions existed
        # these were three independent autocommits and a crash between them
        # left the database disagreeing with itself; now that the current
        # revision is derived from the same digest, a partial write would mean
        # `archive_hashes.digest` and `current_revision_id` naming different
        # bytes -- two answers to "what is this archive now?".
        require_transaction(self.connection)

        # Compare against the file_size/modified_time_ns already
        # recorded for this location to detect whether the underlying
        # file changed since the last time it was seen (as opposed to
        # just being hashed for the first time).
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

        # One archive_hashes row per archive_id: insert on first hash,
        # or overwrite on rehash via the ON CONFLICT upsert.
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
        # Keep the location's own size/mtime fields current so future
        # comparisons (e.g. in save() above, or enqueue_missing() below)
        # use the value observed at hash time.
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
        # Same for the denormalized file_size on archive_files.
        self.connection.execute(
            """
            UPDATE archive_files
            SET file_size = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (result.file_size, archive_id),
        )

        # The revision is the authoritative statement of byte identity, and
        # archive_hashes is the mutable record of the most recent hash. They
        # are written together so they cannot disagree.
        #
        # Three cases, all handled by record_or_reuse:
        #
        #   first hash       -- the archive's current revision is still the
        #                       provisional one it was created with, so this
        #                       appends the established generation and the
        #                       pointer moves;
        #   unchanged rehash -- the digest is already a revision of this
        #                       archive, so it is reused and only an
        #                       observation is added. A file rediscovered
        #                       unchanged must not look like a file that
        #                       changed;
        #   changed rehash   -- new bytes, so a new generation is appended
        #                       *beside* the old one rather than overwriting
        #                       it, which is what archive_hashes alone did.
        revisions = RevisionRepository(self.connection)
        revision_id, _ = revisions.record_or_reuse(
            archive_id=archive_id,
            archive_sha256=result.digest,
            evidence=(
                f"{result.algorithm} v{result.algorithm_version} over "
                f"{result.bytes_read} bytes, location {location_id}"
            ),
            file_size=result.file_size,
        )

        # Promoted unconditionally, unlike an ordinary append. This digest was
        # just measured from the file on disk, so it *is* the archive's
        # current byte state -- there is no operator judgement to preserve
        # here, and leaving the pointer elsewhere would recreate exactly the
        # disagreement this method exists to prevent.
        revisions.set_current(archive_id, revision_id)

        revisions.observe(
            revision_id=revision_id,
            location_id=location_id,
            file_size=result.file_size,
            modified_time_ns=result.modified_time_ns,
        )

        if metadata_changed and enqueue_reinspection:
            self._enqueue_reinspection_if_absent(archive_id)

    def _enqueue_reinspection_if_absent(
        self,
        archive_id: int,
    ) -> bool:
        # If the file changed size/mtime since it was last inspected,
        # the stored structural inspection (page count, ComicInfo,
        # etc.) may now be stale, so schedule a fresh inspect_archive
        # job -- unless one is already in flight.
        #
        # The separate active-job SELECT this method used to run has
        # been removed in favor of JobQueue.enqueue_if_absent(), which
        # checks and inserts atomically. That matters most here: this
        # method runs from inside a JobWorker handler
        # (CalculateArchiveHashHandler), outside any transaction, so its
        # old check-then-insert could interleave with a concurrent
        # discovery scan enqueueing the same inspect_archive job. The
        # return contract is unchanged: True when this call created the
        # job, False when one was already active.
        outcome = JobQueue(self.connection).enqueue_if_absent(
            "inspect_archive",
            archive_id=archive_id,
            priority=100,
        )

        return outcome is EnqueueOutcome.CREATED

    def enqueue_missing(self, *, limit: int | None = None) -> int:
        limit_clause = ""
        parameters: list[int] = []

        if limit is not None:
            if limit < 1:
                raise ValueError("limit must be at least 1.")
            limit_clause = " LIMIT ?"
            parameters.append(limit)

        # Candidates for hashing: archives that passed inspection
        # (status is one of the "safe to hash" states), whose current
        # file_size/modified_time_ns still match what was recorded at
        # inspection time (i.e. haven't changed since), that either
        # have no hash yet or whose stored hash is for different
        # file_size/modified_time_ns (i.e. stale), and that don't
        # already have a calculate_archive_hash job in flight.
        #
        # The NOT EXISTS clause below is an *advisory* candidate filter,
        # not the duplicate guard: enqueue_if_absent() is the
        # authoritative, race-safe gate. Keeping the filter here still
        # matters, because it decides which rows a bounded `limit`
        # is spent on -- an archive that already has active work is
        # excluded up front rather than consuming a limit slot and
        # yielding ALREADY_ACTIVE. It also carries this job type's
        # terminal-status policy: 'failed' is deliberately NOT excluded,
        # so a permanently-failed calculate_archive_hash job still
        # permits a fresh one (unlike page/perceptual hashing, which do
        # exclude 'failed'). That difference is intentional and
        # preserved as-is here.
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
        created = 0

        for row in rows:
            outcome = queue.enqueue_if_absent(
                "calculate_archive_hash",
                archive_id=int(row["archive_id"]),
                priority=200,
            )

            if outcome is EnqueueOutcome.CREATED:
                created += 1

        # Count rows actually inserted, not candidates considered: a
        # candidate can still lose a race to a concurrent enqueue
        # between the SELECT above and its insert, in which case
        # enqueue_if_absent() reports ALREADY_ACTIVE and no job was
        # created by this call.
        return created

    def duplicate_groups(self) -> list[dict]:
        # Group archives (restricted to their current, live location)
        # by algorithm/digest/file_size; any group with more than one
        # member is a set of byte-for-byte identical archives.
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
        self.connection = connection
        self.locations = ArchiveInspectionRepository(connection)
        self.hashes = ArchiveHashRepository(connection)
        self.chunk_size = chunk_size

    def __call__(self, job: Job) -> None:
        if job.archive_id is None:
            raise ValueError(f"Job {job.id} has no archive_id.")

        location = self.locations.current_location(job.archive_id)
        path = Path(str(location["path"]))

        location_id = int(location["id"])

        try:
            result = calculate_archive_hash(
                path,
                chunk_size=self.chunk_size,
            )
        except OSError as exc:
            raise _as_categorized(exc) from exc

        # `calculate_archive_hash` proves the file held still only while it
        # was being read. Everything below re-establishes that the bytes it
        # measured are still the archive's current bytes at the moment they
        # are recorded -- otherwise the hash row and the revision would agree
        # with each other while both described a file that no longer exists
        # in that form, which is a more convincing kind of wrong than a plain
        # disagreement.
        #
        # The handler runs outside any transaction, so it owns this one.
        try:
            with transaction(self.connection):
                self._assert_still_current(
                    archive_id=job.archive_id,
                    location_id=location_id,
                    path=path,
                )
                # Checked before the writes as well as after them. For
                # correctness the pre-commit check below subsumes this one --
                # removing this line alone fails no test, measured -- because
                # anything it would catch is still caught before COMMIT and
                # rolled back. It is kept as fail-fast: without it a
                # replacement noticed before any work began would still write
                # four rows, enqueue a reinspection, and undo all of it.
                # Recorded here because an overlap nobody wrote down is how
                # one of the two gets deleted later as dead weight.
                self._assert_file_matches(path, result)

                self.hashes.save(
                    archive_id=job.archive_id,
                    location_id=location_id,
                    result=result,
                )

                # Re-stat after every write but before COMMIT. The archive is
                # outside SQLite's transaction boundary, so a replacement
                # during the write window is invisible to the database; the
                # same pattern guards the reviewed source-drift recovery.
                self._assert_file_matches(path, result)
        except OSError as exc:
            raise _as_categorized(exc) from exc

    def _assert_still_current(
        self,
        *,
        archive_id: int,
        location_id: int,
        path: Path,
    ) -> None:
        """Refuse if the archive was relocated while it was being hashed.

        The location row is read before hashing and the promotion happens
        after, so a relocation in between would attach a digest measured at
        the old path to whatever the archive points at now.
        """
        try:
            current = self.locations.current_location(archive_id)
        except ArchiveLocationNotFoundError as exc:
            raise SourceChangedError(
                f"Archive {archive_id} lost its current location while it "
                "was being hashed."
            ) from exc

        if int(current["id"]) != location_id or (
            str(current["path"]) != str(path)
        ):
            raise SourceChangedError(
                f"Archive {archive_id} was relocated while it was being "
                f"hashed: location {location_id} ({path}) is no longer "
                f"current."
            )

    @staticmethod
    def _assert_file_matches(path: Path, result: ArchiveHash) -> None:
        """Refuse if the file no longer matches what was hashed.

        Size and mtime only -- the same evidence `calculate_archive_hash`
        compares either side of its own read. It cannot see a same-length
        replacement inside one filesystem timestamp tick; re-hashing to close
        that would double every archive's read cost, and the window here is
        the write transaction rather than the whole file read.
        """
        stat = path.stat()

        if (
            stat.st_size != result.file_size
            or stat.st_mtime_ns != result.modified_time_ns
        ):
            raise SourceChangedError(
                f"{path} changed after it was hashed and before the result "
                "was recorded; the rewrite was abandoned."
            )
