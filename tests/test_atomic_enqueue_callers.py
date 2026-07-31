"""Tests for the five production enqueue paths after their migration
to JobQueue.enqueue_if_absent().

Every test uses a disposable `tmp_path` SQLite database. Nothing here
touches a production database, backup, report, or archive path.

The five paths:

1. LibraryRepository._enqueue_inspection_if_absent  (inspect_archive)
2. ArchiveHashRepository._enqueue_reinspection_if_absent (inspect_archive)
3. ArchiveHashRepository.enqueue_missing        (calculate_archive_hash)
4. ArchivePageHashRepository.enqueue_missing    (hash_archive_pages)
5. ArchivePerceptualHashRepository.enqueue_missing
                                    (hash_archive_pages_perceptual)
"""

from __future__ import annotations

import json
import sqlite3
import threading
import zipfile
from collections import Counter
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from comic_automation.archive.hashing import (
    ArchiveHashRepository,
    calculate_archive_hash,
)
from comic_automation.archive.page_hashing import (
    ArchivePageHashRepository,
    calculate_page_hashes,
)
from comic_automation.archive.perceptual_hashing import (
    ArchivePerceptualHashRepository,
)
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.jobs import EnqueueOutcome, JobQueue
from comic_automation.library.discovery import DiscoveredArchive
from comic_automation.library.repository import LibraryRepository


MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)

ACTIVE_STATUSES = ("pending", "claimed", "running")


# --- fixtures / seeding ----------------------------------------------


def image_bytes() -> bytes:
    image = Image.new("RGB", (96, 128), "white")
    drawing = ImageDraw.Draw(image)
    drawing.rectangle((10, 15, 45, 100), fill="black")
    drawing.ellipse((50, 30, 85, 70), fill="gray")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def create_cbz(path: Path, *, pages: int = 2) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = image_bytes()

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for index in range(pages):
            archive.writestr(f"{index + 1:03d}.png", payload)

    return path


def migrated(tmp_path: Path, name: str = "callers.db") -> Path:
    database = tmp_path / name

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATIONS)

    return database


def seed_location(connection, path: Path) -> tuple[int, int]:
    stat = path.stat()
    archive = connection.execute(
        "INSERT INTO archive_files (file_size) VALUES (?)",
        (stat.st_size,),
    )
    archive_id = int(archive.lastrowid)
    location = connection.execute(
        """
        INSERT INTO file_locations (
            archive_id, path, file_size, modified_time_ns
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            archive_id,
            str(path.resolve()),
            stat.st_size,
            stat.st_mtime_ns,
        ),
    )
    return archive_id, int(location.lastrowid)


def seed_inspected(connection, path: Path) -> int:
    """Archive eligible for ArchiveHashRepository.enqueue_missing."""
    stat = path.stat()
    archive_id, location_id = seed_location(connection, path)
    connection.execute(
        """
        INSERT INTO archive_inspections (
            archive_id, location_id, inspected_path, archive_format,
            status, entry_count, page_count, directory_count,
            result_json, inspected_file_size,
            inspected_modified_time_ns
        )
        VALUES (?, ?, ?, 'cbz', 'ok', 1, 1, 0, '{}', ?, ?)
        """,
        (
            archive_id,
            location_id,
            str(path.resolve()),
            stat.st_size,
            stat.st_mtime_ns,
        ),
    )
    return archive_id


def seed_hashed(connection, path: Path) -> int:
    """Archive eligible for ArchivePageHashRepository.enqueue_missing."""
    stat = path.stat()
    archive_id, _ = seed_location(connection, path)
    connection.execute(
        """
        INSERT INTO archive_hashes (
            archive_id, algorithm, algorithm_version, digest,
            file_size, modified_time_ns, bytes_read
        )
        VALUES (?, 'sha256', '1', ?, ?, ?, ?)
        """,
        (
            archive_id,
            f"{archive_id:064x}",
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_size,
        ),
    )
    return archive_id


def seed_page_inventory(connection, path: Path) -> int:
    """Archive eligible for perceptual enqueue_missing (pages exist,
    perceptual hashes do not)."""
    archive_id, location_id = seed_location(connection, path)
    ArchivePageHashRepository(connection).save(
        archive_id=archive_id,
        location_id=location_id,
        result=calculate_page_hashes(path),
    )
    return archive_id


def insert_job(
    connection,
    *,
    job_type: str,
    status: str,
    archive_id: int,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO jobs (job_type, status, archive_id)
        VALUES (?, ?, ?)
        """,
        (job_type, status, archive_id),
    )
    return int(cursor.lastrowid)


def job_count(connection, *, job_type: str, archive_id: int) -> int:
    return connection.execute(
        """
        SELECT COUNT(*)
        FROM jobs
        WHERE job_type = ? AND archive_id = ?
        """,
        (job_type, archive_id),
    ).fetchone()[0]


class RecordingQueue(JobQueue):
    """JobQueue that records which enqueue API each caller used.

    `enqueue()` raises: any production path still calling it is a
    regression, since only `enqueue_if_absent()` is race-safe.
    """

    def __init__(self, connection) -> None:
        super().__init__(connection)
        self.if_absent_calls: list[tuple] = []

    def enqueue(self, *args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError(
            "Production code must call enqueue_if_absent(), not "
            f"enqueue(); got args={args!r} kwargs={kwargs!r}"
        )

    def enqueue_if_absent(self, job_type, **kwargs):
        self.if_absent_calls.append((job_type, kwargs))
        return super().enqueue_if_absent(job_type, **kwargs)


# --- 1. discovery inspection path -------------------------------------


def test_discovery_uses_enqueue_if_absent_and_keeps_payload(
    tmp_path: Path,
) -> None:
    database = migrated(tmp_path)
    archive = create_cbz(tmp_path / "library" / "issue.cbz")

    with database_connection(database) as connection:
        archive_id, _ = seed_location(connection, archive)
        repository = LibraryRepository(connection)
        recording = RecordingQueue(connection)
        repository.queue = recording

        created = repository._enqueue_inspection_if_absent(
            archive_id,
            str(archive),
        )
        duplicate = repository._enqueue_inspection_if_absent(
            archive_id,
            str(archive),
        )

        row = connection.execute(
            "SELECT * FROM jobs WHERE archive_id = ?",
            (archive_id,),
        ).fetchone()
        total = job_count(
            connection,
            job_type="inspect_archive",
            archive_id=archive_id,
        )

    assert created is True
    assert duplicate is False
    assert len(recording.if_absent_calls) == 2
    assert total == 1
    # Priority and payload preserved exactly.
    assert row["priority"] == 100
    assert json.loads(row["payload_json"]) == {"path": str(archive)}


def test_discovery_returns_false_for_each_active_status(
    tmp_path: Path,
) -> None:
    for status in ACTIVE_STATUSES:
        database = migrated(tmp_path, f"discovery_{status}.db")
        archive = create_cbz(tmp_path / status / "issue.cbz")

        with database_connection(database) as connection:
            archive_id, _ = seed_location(connection, archive)
            insert_job(
                connection,
                job_type="inspect_archive",
                status=status,
                archive_id=archive_id,
            )

            queued = LibraryRepository(
                connection
            )._enqueue_inspection_if_absent(archive_id, str(archive))

            total = job_count(
                connection,
                job_type="inspect_archive",
                archive_id=archive_id,
            )

        assert queued is False, status
        assert total == 1, status


def test_discovery_scan_still_runs_inside_caller_transaction(
    tmp_path: Path,
) -> None:
    """record_archive() is called from scan_library()'s BEGIN IMMEDIATE
    batch; the migrated path must still compose inside it.
    """
    database = migrated(tmp_path)
    archive = create_cbz(tmp_path / "library" / "issue.cbz")
    stat = archive.stat()

    with database_connection(database) as connection:
        repository = LibraryRepository(connection)
        discovered = DiscoveredArchive(
            path=archive.resolve(),
            file_size=stat.st_size,
            modified_time_ns=stat.st_mtime_ns,
            extension=archive.suffix.lower(),
        )

        connection.execute("BEGIN IMMEDIATE")
        classification, queued = repository.record_archive(discovered)
        in_transaction = connection.in_transaction
        connection.execute("COMMIT")

        total = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE job_type = 'inspect_archive'"
        ).fetchone()[0]

    assert classification == "new"
    assert queued is True
    assert in_transaction is True
    assert total == 1


# --- 2. hash-triggered reinspection path ------------------------------


def test_reinspection_uses_enqueue_if_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = migrated(tmp_path)
    archive = create_cbz(tmp_path / "library" / "issue.cbz")

    # This path constructs its own JobQueue, so intercept at the class.
    if_absent_calls: list[tuple[str, dict]] = []
    real_if_absent = JobQueue.enqueue_if_absent

    def forbidden_enqueue(self, *args, **kwargs):
        raise AssertionError(
            "Reinspection must call enqueue_if_absent(), not enqueue()"
        )

    def recording_if_absent(self, job_type_arg, **kwargs):
        if_absent_calls.append((job_type_arg, dict(kwargs)))
        return real_if_absent(self, job_type_arg, **kwargs)

    monkeypatch.setattr(JobQueue, "enqueue", forbidden_enqueue)
    monkeypatch.setattr(
        JobQueue, "enqueue_if_absent", recording_if_absent
    )

    with database_connection(database) as connection:
        archive_id, _ = seed_location(connection, archive)
        repository = ArchiveHashRepository(connection)

        created = repository._enqueue_reinspection_if_absent(archive_id)
        duplicate = repository._enqueue_reinspection_if_absent(
            archive_id
        )

        monkeypatch.undo()

        assert if_absent_calls == [
            ("inspect_archive", {"archive_id": archive_id, "priority": 100}),
            ("inspect_archive", {"archive_id": archive_id, "priority": 100}),
        ]

        row = connection.execute(
            "SELECT * FROM jobs WHERE archive_id = ?",
            (archive_id,),
        ).fetchone()
        total = job_count(
            connection,
            job_type="inspect_archive",
            archive_id=archive_id,
        )

    assert created is True
    assert duplicate is False
    assert total == 1
    assert row["priority"] == 100
    # This path deliberately carries no payload, as before.
    assert row["payload_json"] is None


def test_reinspection_returns_false_for_each_active_status(
    tmp_path: Path,
) -> None:
    for status in ACTIVE_STATUSES:
        database = migrated(tmp_path, f"reinspect_{status}.db")
        archive = create_cbz(tmp_path / f"re_{status}" / "issue.cbz")

        with database_connection(database) as connection:
            archive_id, _ = seed_location(connection, archive)
            insert_job(
                connection,
                job_type="inspect_archive",
                status=status,
                archive_id=archive_id,
            )

            queued = ArchiveHashRepository(
                connection
            )._enqueue_reinspection_if_absent(archive_id)

            total = job_count(
                connection,
                job_type="inspect_archive",
                archive_id=archive_id,
            )

        assert queued is False, status
        assert total == 1, status


def test_save_enqueues_reinspection_only_when_absent(
    tmp_path: Path,
) -> None:
    """The real save() path (metadata changed -> reinspection)."""
    database = migrated(tmp_path)
    archive = create_cbz(tmp_path / "library" / "issue.cbz")

    with database_connection(database) as connection:
        archive_id, location_id = seed_location(connection, archive)

        # Change the file so save() sees drifted metadata.
        archive.write_bytes(archive.read_bytes() + b"\x00")
        result = calculate_archive_hash(archive)

        repository = ArchiveHashRepository(connection)
        repository.save(
            archive_id=archive_id,
            location_id=location_id,
            result=result,
        )
        first_total = job_count(
            connection,
            job_type="inspect_archive",
            archive_id=archive_id,
        )

        # Saving again with drifted metadata must not stack a second
        # active reinspection job.
        archive.write_bytes(archive.read_bytes() + b"\x00")
        repository.save(
            archive_id=archive_id,
            location_id=location_id,
            result=calculate_archive_hash(archive),
        )
        second_total = job_count(
            connection,
            job_type="inspect_archive",
            archive_id=archive_id,
        )

    assert first_total == 1
    assert second_total == 1


# --- 3-5. bulk enqueue_missing paths ----------------------------------


BULK_CASES = (
    ("hash", "calculate_archive_hash", 200),
    ("page", "hash_archive_pages", 300),
    ("perceptual", "hash_archive_pages_perceptual", 250),
)


def bulk_repository(kind: str, connection):
    if kind == "hash":
        return ArchiveHashRepository(connection)
    if kind == "page":
        return ArchivePageHashRepository(connection)
    return ArchivePerceptualHashRepository(connection)


def seed_bulk_candidate(kind: str, connection, path: Path) -> int:
    if kind == "hash":
        return seed_inspected(connection, path)
    if kind == "page":
        return seed_hashed(connection, path)
    return seed_page_inventory(connection, path)


@pytest.mark.parametrize(
    ("kind", "job_type", "priority"),
    BULK_CASES,
)
def test_bulk_paths_use_enqueue_if_absent_and_keep_priority(
    tmp_path: Path,
    kind: str,
    job_type: str,
    priority: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = migrated(tmp_path, f"{kind}_bulk.db")
    archive = create_cbz(tmp_path / kind / "issue.cbz")

    # The bulk paths build their own JobQueue internally, so intercept
    # at the class: enqueue() must never be reached, and every insert
    # must go through enqueue_if_absent().
    if_absent_calls: list[tuple[str, dict]] = []
    real_if_absent = JobQueue.enqueue_if_absent

    def forbidden_enqueue(self, *args, **kwargs):
        raise AssertionError(
            "Production code must call enqueue_if_absent(), not "
            f"enqueue(); got args={args!r} kwargs={kwargs!r}"
        )

    def recording_if_absent(self, job_type_arg, **kwargs):
        if_absent_calls.append((job_type_arg, dict(kwargs)))
        return real_if_absent(self, job_type_arg, **kwargs)

    monkeypatch.setattr(JobQueue, "enqueue", forbidden_enqueue)
    monkeypatch.setattr(
        JobQueue, "enqueue_if_absent", recording_if_absent
    )

    with database_connection(database) as connection:
        archive_id = seed_bulk_candidate(kind, connection, archive)
        repository = bulk_repository(kind, connection)

        created = repository.enqueue_missing()

        monkeypatch.undo()

        row = connection.execute(
            "SELECT * FROM jobs WHERE archive_id = ?",
            (archive_id,),
        ).fetchone()

    assert created == 1
    assert if_absent_calls == [
        (
            job_type,
            {"archive_id": archive_id, "priority": priority},
        )
    ]
    assert row["job_type"] == job_type
    assert row["priority"] == priority


@pytest.mark.parametrize(("kind", "job_type", "_priority"), BULK_CASES)
def test_bulk_paths_are_idempotent_across_repeated_calls(
    tmp_path: Path,
    kind: str,
    job_type: str,
    _priority: int,
) -> None:
    database = migrated(tmp_path, f"{kind}_idempotent.db")
    archive = create_cbz(tmp_path / f"{kind}_idem" / "issue.cbz")

    with database_connection(database) as connection:
        archive_id = seed_bulk_candidate(kind, connection, archive)
        repository = bulk_repository(kind, connection)

        first = repository.enqueue_missing()
        second = repository.enqueue_missing()
        third = repository.enqueue_missing()

        total = job_count(
            connection,
            job_type=job_type,
            archive_id=archive_id,
        )

    assert first == 1
    # Already-active candidates are filtered out up front, so repeat
    # calls create nothing and report zero.
    assert second == 0
    assert third == 0
    assert total == 1


@pytest.mark.parametrize(("kind", "job_type", "_priority"), BULK_CASES)
def test_bulk_count_reflects_rows_created_not_candidates(
    tmp_path: Path,
    kind: str,
    job_type: str,
    _priority: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate losing a race: the helper reports ALREADY_ACTIVE for one
    of two candidates, so the returned count must be 1, not len(rows).
    """
    database = migrated(tmp_path, f"{kind}_race_count.db")
    first_archive = create_cbz(tmp_path / f"{kind}_rc" / "a.cbz")
    second_archive = create_cbz(tmp_path / f"{kind}_rc" / "b.cbz", pages=3)

    with database_connection(database) as connection:
        seed_bulk_candidate(kind, connection, first_archive)
        seed_bulk_candidate(kind, connection, second_archive)
        repository = bulk_repository(kind, connection)

        real_enqueue_if_absent = JobQueue.enqueue_if_absent
        seen: list[int] = []

        def flaky(self, job_type_arg, **kwargs):
            seen.append(kwargs["archive_id"])

            # The first candidate "loses the race": pretend another
            # connection created its job in between.
            if len(seen) == 1:
                return EnqueueOutcome.ALREADY_ACTIVE

            return real_enqueue_if_absent(self, job_type_arg, **kwargs)

        monkeypatch.setattr(JobQueue, "enqueue_if_absent", flaky)

        created = repository.enqueue_missing()

        monkeypatch.undo()

        actual_rows = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE job_type = ?",
            (job_type,),
        ).fetchone()[0]

    # Two candidates considered, one reported ALREADY_ACTIVE.
    assert len(seen) == 2
    assert created == 1
    assert actual_rows == 1


@pytest.mark.parametrize(("kind", "job_type", "_priority"), BULK_CASES)
def test_active_candidate_does_not_consume_bounded_limit(
    tmp_path: Path,
    kind: str,
    job_type: str,
    _priority: int,
) -> None:
    """A candidate with active work must not eat a `limit` slot.

    The advisory NOT EXISTS filter excludes it in SQL, so limit=1 is
    spent on the next eligible archive instead of being wasted on an
    ALREADY_ACTIVE outcome.
    """
    database = migrated(tmp_path, f"{kind}_limit.db")
    busy_archive = create_cbz(tmp_path / f"{kind}_lim" / "a.cbz")
    free_archive = create_cbz(tmp_path / f"{kind}_lim" / "b.cbz", pages=3)

    with database_connection(database) as connection:
        busy_id = seed_bulk_candidate(kind, connection, busy_archive)
        free_id = seed_bulk_candidate(kind, connection, free_archive)

        # busy_id already has active work of this type (lowest
        # archive_id, so it would be selected first by the ORDER BY).
        insert_job(
            connection,
            job_type=job_type,
            status="running",
            archive_id=busy_id,
        )

        created = bulk_repository(kind, connection).enqueue_missing(
            limit=1
        )

        busy_total = job_count(
            connection,
            job_type=job_type,
            archive_id=busy_id,
        )
        free_total = job_count(
            connection,
            job_type=job_type,
            archive_id=free_id,
        )

    assert busy_id < free_id
    assert created == 1
    # The busy archive still has only its pre-existing job...
    assert busy_total == 1
    # ...and the limit was spent on the eligible one.
    assert free_total == 1


@pytest.mark.parametrize(("kind", "job_type", "_priority"), BULK_CASES)
def test_active_statuses_block_bulk_enqueue(
    tmp_path: Path,
    kind: str,
    job_type: str,
    _priority: int,
) -> None:
    for status in ACTIVE_STATUSES:
        database = migrated(tmp_path, f"{kind}_{status}_bulk.db")
        archive = create_cbz(
            tmp_path / f"{kind}_{status}" / "issue.cbz"
        )

        with database_connection(database) as connection:
            archive_id = seed_bulk_candidate(kind, connection, archive)
            insert_job(
                connection,
                job_type=job_type,
                status=status,
                archive_id=archive_id,
            )

            created = bulk_repository(
                kind, connection
            ).enqueue_missing()

            total = job_count(
                connection,
                job_type=job_type,
                archive_id=archive_id,
            )

        assert created == 0, f"{kind}/{status}"
        assert total == 1, f"{kind}/{status}"


# --- failed-status policy is preserved, and still differs -------------


def test_failed_hash_job_still_permits_re_enqueue(
    tmp_path: Path,
) -> None:
    """calculate_archive_hash: 'failed' does NOT block re-enqueue."""
    database = migrated(tmp_path)
    archive = create_cbz(tmp_path / "failed_hash" / "issue.cbz")

    with database_connection(database) as connection:
        archive_id = seed_inspected(connection, archive)
        insert_job(
            connection,
            job_type="calculate_archive_hash",
            status="failed",
            archive_id=archive_id,
        )

        created = ArchiveHashRepository(connection).enqueue_missing()

        total = job_count(
            connection,
            job_type="calculate_archive_hash",
            archive_id=archive_id,
        )

    assert created == 1
    assert total == 2  # the failed row plus a fresh pending one


@pytest.mark.parametrize(
    ("kind", "job_type"),
    [
        ("page", "hash_archive_pages"),
        ("perceptual", "hash_archive_pages_perceptual"),
    ],
)
def test_failed_page_and_perceptual_jobs_still_block_re_enqueue(
    tmp_path: Path,
    kind: str,
    job_type: str,
) -> None:
    """Page/perceptual hashing: 'failed' DOES block re-enqueue."""
    database = migrated(tmp_path, f"{kind}_failed.db")
    archive = create_cbz(tmp_path / f"{kind}_failed" / "issue.cbz")

    with database_connection(database) as connection:
        archive_id = seed_bulk_candidate(kind, connection, archive)
        insert_job(
            connection,
            job_type=job_type,
            status="failed",
            archive_id=archive_id,
        )

        created = bulk_repository(kind, connection).enqueue_missing()

        total = job_count(
            connection,
            job_type=job_type,
            archive_id=archive_id,
        )

    assert created == 0
    assert total == 1  # only the failed row; nothing new


def test_failed_policies_remain_different_across_job_types(
    tmp_path: Path,
) -> None:
    """The three bulk paths' terminal policies are unchanged by this
    migration: hashing re-enqueues after failure, page and perceptual
    hashing do not.
    """
    outcomes: dict[str, int] = {}

    for kind, job_type, _priority in BULK_CASES:
        database = migrated(tmp_path, f"{kind}_policy.db")
        archive = create_cbz(tmp_path / f"{kind}_policy" / "issue.cbz")

        with database_connection(database) as connection:
            archive_id = seed_bulk_candidate(kind, connection, archive)
            insert_job(
                connection,
                job_type=job_type,
                status="failed",
                archive_id=archive_id,
            )
            outcomes[kind] = bulk_repository(
                kind, connection
            ).enqueue_missing()

    assert outcomes == {"hash": 1, "page": 0, "perceptual": 0}


# --- real cross-path race ---------------------------------------------


def test_discovery_and_reinspection_race_creates_one_active_job(
    tmp_path: Path,
) -> None:
    """The audit's headline race, now resolved by the database.

    Discovery (path #1, inside its own BEGIN IMMEDIATE) and
    hash-triggered reinspection (path #2, no transaction) both target
    inspect_archive for the same archive from two real connections in
    two threads. Exactly one active job may result, and neither caller
    may raise.
    """
    database = migrated(tmp_path, "cross_race.db")
    archive = create_cbz(tmp_path / "race" / "issue.cbz")

    with database_connection(database) as setup:
        archive_id, _ = seed_location(setup, archive)

    barrier = threading.Barrier(2, timeout=15)
    lock = threading.Lock()
    results: dict[str, bool] = {}
    errors: dict[str, BaseException] = {}

    def discovery() -> None:
        try:
            with database_connection(database) as connection:
                repository = LibraryRepository(connection)
                barrier.wait()
                connection.execute("BEGIN IMMEDIATE")
                queued = repository._enqueue_inspection_if_absent(
                    archive_id,
                    str(archive),
                )
                connection.execute("COMMIT")

            with lock:
                results["discovery"] = queued
        except BaseException as exc:  # noqa: BLE001 - asserted below
            try:
                barrier.abort()
            except Exception:
                pass
            with lock:
                errors["discovery"] = exc

    def reinspection() -> None:
        try:
            with database_connection(database) as connection:
                repository = ArchiveHashRepository(connection)
                barrier.wait()
                queued = repository._enqueue_reinspection_if_absent(
                    archive_id
                )

            with lock:
                results["reinspection"] = queued
        except BaseException as exc:  # noqa: BLE001 - asserted below
            try:
                barrier.abort()
            except Exception:
                pass
            with lock:
                errors["reinspection"] = exc

    threads = [
        threading.Thread(target=discovery, name="discovery"),
        threading.Thread(target=reinspection, name="reinspection"),
    ]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join(timeout=30)

    still_alive = [t.name for t in threads if t.is_alive()]

    with database_connection(database) as connection:
        active_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM jobs
            WHERE archive_id = ?
              AND job_type = 'inspect_archive'
              AND status IN ('pending', 'claimed', 'running')
            """,
            (archive_id,),
        ).fetchone()[0]
        total_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM jobs
            WHERE archive_id = ? AND job_type = 'inspect_archive'
            """,
            (archive_id,),
        ).fetchone()[0]

    assert still_alive == [], f"threads still running: {still_alive}"
    assert errors == {}, f"thread errors: {errors}"
    assert set(results) == {"discovery", "reinspection"}
    # Exactly one caller created the job; the other saw it as active.
    assert Counter(results.values()) == Counter({True: 1, False: 1})
    assert active_count == 1
    assert total_count == 1


def test_two_threads_racing_bulk_enqueue_create_one_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine race between two concurrent enqueue_missing() calls.

    A barrier is installed *inside* enqueue_if_absent(), so neither
    thread can perform its atomic insert until both have already
    finished their candidate SELECT. That is the interleaving the
    advisory NOT EXISTS filter cannot prevent -- both threads see the
    archive as eligible -- and it must still yield exactly one job,
    decided by the database.
    """
    database = migrated(tmp_path, "bulk_race.db")
    archive = create_cbz(tmp_path / "bulk_race" / "issue.cbz")

    with database_connection(database) as setup:
        archive_id = seed_inspected(setup, archive)

    barrier = threading.Barrier(2, timeout=15)
    real_if_absent = JobQueue.enqueue_if_absent

    def barriered_if_absent(self, job_type_arg, **kwargs):
        # Both candidate queries have run by the time either thread
        # gets here; release them into the insert together.
        barrier.wait()
        return real_if_absent(self, job_type_arg, **kwargs)

    monkeypatch.setattr(
        JobQueue, "enqueue_if_absent", barriered_if_absent
    )

    lock = threading.Lock()
    counts: dict[str, int] = {}
    errors: dict[str, BaseException] = {}

    def bulk(name: str) -> None:
        try:
            with database_connection(database) as connection:
                created = ArchiveHashRepository(
                    connection
                ).enqueue_missing()

            with lock:
                counts[name] = created
        except BaseException as exc:  # noqa: BLE001 - asserted below
            try:
                barrier.abort()
            except Exception:
                pass
            with lock:
                errors[name] = exc

    threads = [
        threading.Thread(target=bulk, args=(name,), name=name)
        for name in ("bulk-a", "bulk-b")
    ]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join(timeout=30)

    still_alive = [t.name for t in threads if t.is_alive()]
    monkeypatch.undo()

    with database_connection(database) as connection:
        total = job_count(
            connection,
            job_type="calculate_archive_hash",
            archive_id=archive_id,
        )
        active = connection.execute(
            """
            SELECT COUNT(*)
            FROM jobs
            WHERE archive_id = ?
              AND job_type = 'calculate_archive_hash'
              AND status IN ('pending', 'claimed', 'running')
            """,
            (archive_id,),
        ).fetchone()[0]

    assert still_alive == [], f"threads still running: {still_alive}"
    assert errors == {}, f"thread errors: {errors}"
    assert set(counts) == {"bulk-a", "bulk-b"}
    # One thread created the job; the other's candidate lost the race
    # and contributed nothing to its count.
    assert Counter(counts.values()) == Counter({1: 1, 0: 1})
    assert total == 1
    assert active == 1


def test_sequential_bulk_calls_across_two_connections_are_idempotent(
    tmp_path: Path,
) -> None:
    """Two connections calling enqueue_missing() one after the other.

    Distinct from the racing test above: here the first call fully
    completes before the second begins, so the second's candidate
    query already excludes the archive via the advisory NOT EXISTS
    filter and reports zero without ever reaching an insert.
    """
    database = migrated(tmp_path, "bulk_sequential.db")
    archive = create_cbz(tmp_path / "bulk_sequential" / "issue.cbz")

    with database_connection(database) as setup:
        archive_id = seed_inspected(setup, archive)

    with (
        database_connection(database) as first,
        database_connection(database) as second,
    ):
        first_created = ArchiveHashRepository(first).enqueue_missing()
        second_created = ArchiveHashRepository(second).enqueue_missing()

        total = job_count(
            second,
            job_type="calculate_archive_hash",
            archive_id=archive_id,
        )

    assert first_created == 1
    assert second_created == 0
    assert total == 1
