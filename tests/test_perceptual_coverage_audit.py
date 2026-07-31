from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from comic_automation.archive.perceptual_coverage_audit import (
    DatabaseChangedError,
    DatabaseIntegrityError,
    DatabaseMutatedError,
    OutputPathCollisionError,
    POPULATION_ORDER,
    classify_archives,
    failed_category_counts,
    fingerprint_database,
    main,
    population_counts,
    readonly_database_connection,
    run_audit,
    validate_output_paths,
)
from comic_automation.archive.perceptual_hashing import (
    DHASH_ALGORITHM,
    DHASH_ALGORITHM_VERSION,
    PHASH_ALGORITHM,
    PHASH_ALGORITHM_VERSION,
)
from comic_automation.database.connection import (
    connect_database,
    database_connection,
)
from comic_automation.database.migrations import apply_migrations


MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)

JOB_TYPE = "hash_archive_pages_perceptual"
FIXED_NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)


# --- fixture-building helpers -------------------------------------------


def seed_archive(
    connection: sqlite3.Connection,
    *,
    path: str | None,
    file_size: int = 2048,
    modified_time_ns: int = 1_000_000_000,
) -> int:
    """Insert an archive_files row and, optionally, its current
    file_locations row. path=None simulates an archive with no current
    location (orphaned / moved / deleted).
    """
    archive = connection.execute(
        "INSERT INTO archive_files (file_size) VALUES (?)",
        (file_size,),
    )
    archive_id = int(archive.lastrowid)

    if path is not None:
        connection.execute(
            """
            INSERT INTO file_locations (
                archive_id, path, file_size, modified_time_ns, is_current
            )
            VALUES (?, ?, ?, ?, 1)
            """,
            (archive_id, path, file_size, modified_time_ns),
        )

    return archive_id


def seed_content_signature(
    connection: sqlite3.Connection,
    *,
    archive_id: int,
    page_count: int,
    source_file_size: int = 2048,
    source_modified_time_ns: int = 1_000_000_000,
) -> None:
    connection.execute(
        """
        INSERT INTO archive_content_signatures (
            archive_id, algorithm, algorithm_version, digest,
            page_count, image_bytes, source_file_size,
            source_modified_time_ns
        )
        VALUES (?, 'sha256', '1', 'deadbeef', ?, 1024, ?, ?)
        """,
        (
            archive_id,
            page_count,
            source_file_size,
            source_modified_time_ns,
        ),
    )


def seed_page(
    connection: sqlite3.Connection,
    *,
    archive_id: int,
    page_index: int,
    dhash: bool,
    phash: bool,
    dimensions: bool,
) -> int:
    page = connection.execute(
        """
        INSERT INTO archive_pages (
            archive_id, page_index, entry_name, entry_size,
            compressed_size, crc32, width, height, image_format
        )
        VALUES (?, ?, ?, 1024, 512, 0, ?, ?, ?)
        """,
        (
            archive_id,
            page_index,
            f"page-{page_index:03d}.jpg",
            800 if dimensions else None,
            1200 if dimensions else None,
            "JPEG" if dimensions else None,
        ),
    )
    page_id = int(page.lastrowid)

    if dhash:
        connection.execute(
            """
            INSERT INTO page_hashes (
                page_id, algorithm, algorithm_version, digest, bytes_read
            )
            VALUES (?, ?, ?, 'aaaa', 1024)
            """,
            (page_id, DHASH_ALGORITHM, DHASH_ALGORITHM_VERSION),
        )

    if phash:
        connection.execute(
            """
            INSERT INTO page_hashes (
                page_id, algorithm, algorithm_version, digest, bytes_read
            )
            VALUES (?, ?, ?, 'bbbb', 1024)
            """,
            (page_id, PHASH_ALGORITHM, PHASH_ALGORITHM_VERSION),
        )

    return page_id


def seed_job(
    connection: sqlite3.Connection,
    *,
    archive_id: int,
    status: str,
    failure_category: str | None = None,
    error_message: str | None = None,
    attempts: int = 1,
    max_attempts: int = 3,
    claimed_at: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
) -> int:
    job = connection.execute(
        """
        INSERT INTO jobs (
            job_type, status, archive_id, attempts, max_attempts,
            failure_category, error_message, claimed_at, started_at,
            completed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            JOB_TYPE,
            status,
            archive_id,
            attempts,
            max_attempts,
            failure_category,
            error_message,
            claimed_at,
            started_at,
            completed_at,
        ),
    )
    return int(job.lastrowid)


def build_populated_database(database: Path) -> dict[str, int]:
    """Builds one archive for every population, plus one unexplained gap.

    Returns a name -> archive_id map so tests can assert on specific
    archives by role rather than by position.
    """
    ids: dict[str, int] = {}

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATIONS)

        # complete: fully covered, eligible, no job needed anymore.
        complete_id = seed_archive(connection, path=r"X:\Comics\A\complete.cbz")
        seed_content_signature(connection, archive_id=complete_id, page_count=2)
        seed_page(
            connection,
            archive_id=complete_id,
            page_index=0,
            dhash=True,
            phash=True,
            dimensions=True,
        )
        seed_page(
            connection,
            archive_id=complete_id,
            page_index=1,
            dhash=True,
            phash=True,
            dimensions=True,
        )
        ids["complete"] = complete_id

        # incomplete: eligible, partial coverage, an active *pending*
        # (not stale) job in flight.
        incomplete_id = seed_archive(
            connection, path=r"X:\Comics\B\incomplete.cbz"
        )
        seed_content_signature(
            connection, archive_id=incomplete_id, page_count=2
        )
        seed_page(
            connection,
            archive_id=incomplete_id,
            page_index=0,
            dhash=True,
            phash=True,
            dimensions=True,
        )
        seed_page(
            connection,
            archive_id=incomplete_id,
            page_index=1,
            dhash=False,
            phash=False,
            dimensions=False,
        )
        seed_job(connection, archive_id=incomplete_id, status="pending")
        ids["incomplete"] = incomplete_id

        # unexplained gap: eligible, zero coverage, *no* job ever.
        gap_id = seed_archive(connection, path=r"X:\Comics\C\gap.cbz")
        seed_content_signature(connection, archive_id=gap_id, page_count=1)
        seed_page(
            connection,
            archive_id=gap_id,
            page_index=0,
            dhash=False,
            phash=False,
            dimensions=False,
        )
        ids["unexplained_gap"] = gap_id

        # failed (corrupt_archives): eligible, terminal failure.
        failed_archive_id = seed_archive(
            connection, path=r"X:\Comics\D\failed-archive.cbz"
        )
        seed_content_signature(
            connection, archive_id=failed_archive_id, page_count=1
        )
        seed_job(
            connection,
            archive_id=failed_archive_id,
            status="failed",
            failure_category="archive_corrupt",
            error_message="Invalid or corrupt CBZ archive",
            attempts=3,
            completed_at="2026-07-29T12:00:00",
        )
        ids["failed_corrupt_archive"] = failed_archive_id

        # failed (corrupt_images): eligible, terminal failure, a
        # different stable category than the one above.
        failed_image_id = seed_archive(
            connection, path=r"X:\Comics\E\failed-image.cbz"
        )
        seed_content_signature(
            connection, archive_id=failed_image_id, page_count=1
        )
        seed_job(
            connection,
            archive_id=failed_image_id,
            status="failed",
            failure_category="page_image_corrupt",
            error_message="Invalid or unsupported image page",
            attempts=3,
            completed_at="2026-07-29T12:00:00",
        )
        ids["failed_corrupt_image"] = failed_image_id

        # stale: eligible, an old claimed job (older than the threshold).
        stale_id = seed_archive(connection, path=r"X:\Comics\F\stale.cbz")
        seed_content_signature(connection, archive_id=stale_id, page_count=1)
        old_claimed_at = (
            FIXED_NOW - timedelta(hours=6)
        ).strftime("%Y-%m-%d %H:%M:%S")
        seed_job(
            connection,
            archive_id=stale_id,
            status="claimed",
            claimed_at=old_claimed_at,
        )
        ids["stale"] = stale_id

        # ineligible: no current file location at all (orphaned).
        ineligible_no_location_id = seed_archive(connection, path=None)
        seed_content_signature(
            connection, archive_id=ineligible_no_location_id, page_count=1
        )
        ids["ineligible_no_location"] = ineligible_no_location_id

        # ineligible: has a current location, but the content signature
        # is stale relative to it (file changed on disk since exact
        # page hashing ran).
        ineligible_stale_signature_id = seed_archive(
            connection,
            path=r"X:\Comics\G\stale-signature.cbz",
            file_size=4096,
            modified_time_ns=2_000_000_000,
        )
        seed_content_signature(
            connection,
            archive_id=ineligible_stale_signature_id,
            page_count=1,
            source_file_size=1234,
            source_modified_time_ns=999,
        )
        ids["ineligible_stale_signature"] = ineligible_stale_signature_id

        # ineligible: has a current location, but no content signature
        # at all yet (exact page hashing never ran).
        ineligible_no_signature_id = seed_archive(
            connection, path=r"X:\Comics\H\no-signature.cbz"
        )
        ids["ineligible_no_signature"] = ineligible_no_signature_id

    return ids


# --- classification, per population -------------------------------------


def test_complete_archive_is_classified_complete(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"
    ids = build_populated_database(database)

    with readonly_database_connection(database) as connection:
        archives = classify_archives(
            connection, stale_older_than_seconds=3600, now=FIXED_NOW
        )

    by_id = {a["archive_id"]: a for a in archives}
    assert by_id[ids["complete"]]["population"] == "complete"
    assert by_id[ids["complete"]]["pages_missing"] == 0
    assert by_id[ids["complete"]]["unexplained_gap"] is False


def test_incomplete_archive_is_classified_incomplete(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"
    ids = build_populated_database(database)

    with readonly_database_connection(database) as connection:
        archives = classify_archives(
            connection, stale_older_than_seconds=3600, now=FIXED_NOW
        )

    by_id = {a["archive_id"]: a for a in archives}
    entry = by_id[ids["incomplete"]]
    assert entry["population"] == "incomplete"
    assert entry["pages_missing"] == 1
    assert entry["has_any_job"] is True
    assert entry["unexplained_gap"] is False


def test_unexplained_gap_is_incomplete_and_flagged(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"
    ids = build_populated_database(database)

    with readonly_database_connection(database) as connection:
        archives = classify_archives(
            connection, stale_older_than_seconds=3600, now=FIXED_NOW
        )

    by_id = {a["archive_id"]: a for a in archives}
    entry = by_id[ids["unexplained_gap"]]
    assert entry["population"] == "incomplete"
    assert entry["unexplained_gap"] is True
    assert entry["has_any_job"] is False
    assert entry["pages_covered"] == 0


def test_failed_archives_classified_failed_with_stable_categories(
    tmp_path: Path,
) -> None:
    database = tmp_path / "audit.db"
    ids = build_populated_database(database)

    with readonly_database_connection(database) as connection:
        archives = classify_archives(
            connection, stale_older_than_seconds=3600, now=FIXED_NOW
        )

    by_id = {a["archive_id"]: a for a in archives}

    corrupt_archive_entry = by_id[ids["failed_corrupt_archive"]]
    assert corrupt_archive_entry["population"] == "failed"
    assert corrupt_archive_entry["failed_stable_category"] == "corrupt_archives"

    corrupt_image_entry = by_id[ids["failed_corrupt_image"]]
    assert corrupt_image_entry["population"] == "failed"
    assert corrupt_image_entry["failed_stable_category"] == "corrupt_images"

    counts = failed_category_counts(archives)
    assert counts["corrupt_archives"] == 1
    assert counts["corrupt_images"] == 1
    assert counts["missing_files"] == 0


def test_stale_archive_is_classified_stale(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"
    ids = build_populated_database(database)

    with readonly_database_connection(database) as connection:
        archives = classify_archives(
            connection, stale_older_than_seconds=3600, now=FIXED_NOW
        )

    by_id = {a["archive_id"]: a for a in archives}
    entry = by_id[ids["stale"]]
    assert entry["population"] == "stale"
    assert entry["is_stale"] is True


def test_stale_archive_not_stale_under_a_longer_threshold(
    tmp_path: Path,
) -> None:
    database = tmp_path / "audit.db"
    ids = build_populated_database(database)

    with readonly_database_connection(database) as connection:
        archives = classify_archives(
            connection,
            stale_older_than_seconds=999_999,
            now=FIXED_NOW,
        )

    by_id = {a["archive_id"]: a for a in archives}
    entry = by_id[ids["stale"]]
    # Still has an active job and missing hash work, but the job is no
    # longer "stale" under a threshold larger than its age.
    assert entry["population"] == "incomplete"
    assert entry["is_stale"] is False


def test_ineligible_archives_classified_ineligible(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"
    ids = build_populated_database(database)

    with readonly_database_connection(database) as connection:
        archives = classify_archives(
            connection, stale_older_than_seconds=3600, now=FIXED_NOW
        )

    by_id = {a["archive_id"]: a for a in archives}

    for key in (
        "ineligible_no_location",
        "ineligible_stale_signature",
        "ineligible_no_signature",
    ):
        assert by_id[ids[key]]["population"] == "ineligible", key
        assert by_id[ids[key]]["structural_eligible"] is False, key


# --- partition invariant --------------------------------------------------


def test_populations_partition_all_archives(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"
    build_populated_database(database)

    with readonly_database_connection(database) as connection:
        archives = classify_archives(
            connection, stale_older_than_seconds=3600, now=FIXED_NOW
        )

    counts = population_counts(archives)
    assert set(counts.keys()) == set(POPULATION_ORDER)
    assert sum(counts.values()) == len(archives)

    # Every population actually has at least one member in this fixture.
    for population in POPULATION_ORDER:
        assert counts[population] >= 1, population


def test_run_audit_reports_matching_partition(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"
    build_populated_database(database)

    output = run_audit(
        database=database,
        stale_older_than_seconds=3600,
        now=FIXED_NOW,
    )

    assert output["population_partition_matches_total"] is True
    assert (
        output["population_partition_sum"] == output["total_archive_count"]
    )
    assert output["unexplained_gap_count"] == 1


# --- output generation -----------------------------------------------------


def test_run_audit_generates_json_and_csv(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"
    build_populated_database(database)

    json_output = tmp_path / "reports" / "coverage.json"
    csv_output = tmp_path / "reports" / "coverage.csv"

    output = run_audit(
        database=database,
        stale_older_than_seconds=3600,
        now=FIXED_NOW,
        json_output=json_output,
        csv_output=csv_output,
    )

    assert json_output.is_file()
    payload = json.loads(json_output.read_text(encoding="utf-8"))
    assert payload["total_archive_count"] == output["total_archive_count"]

    assert csv_output.is_file()
    csv_text = csv_output.read_text(encoding="utf-8-sig")
    header = csv_text.splitlines()[0]
    assert header.startswith("archive_id,population,unexplained_gap")
    # Header + one row per archive.
    assert len(csv_text.splitlines()) == 1 + output["total_archive_count"]


def test_cli_main_writes_reports(tmp_path: Path, capsys) -> None:
    database = tmp_path / "audit.db"
    build_populated_database(database)

    json_output = tmp_path / "coverage.json"
    csv_output = tmp_path / "coverage.csv"

    result = main(
        [
            "--database",
            str(database),
            "--stale-older-than-seconds",
            "3600",
            "--json-output",
            str(json_output),
            "--csv-output",
            str(csv_output),
        ]
    )
    captured = capsys.readouterr()

    assert result == 0
    assert "Populations:" in captured.out
    assert json_output.is_file()
    assert csv_output.is_file()


# --- output path collisions -------------------------------------------------


def test_validate_output_paths_rejects_json_matching_database(
    tmp_path: Path,
) -> None:
    database = tmp_path / "audit.db"
    build_populated_database(database)

    with pytest.raises(OutputPathCollisionError):
        validate_output_paths(
            database, json_output=database, csv_output=None
        )


def test_validate_output_paths_rejects_csv_matching_database(
    tmp_path: Path,
) -> None:
    database = tmp_path / "audit.db"
    build_populated_database(database)

    with pytest.raises(OutputPathCollisionError):
        validate_output_paths(
            database, json_output=None, csv_output=database
        )


def test_run_audit_rejects_output_path_colliding_with_database(
    tmp_path: Path,
) -> None:
    database = tmp_path / "audit.db"
    build_populated_database(database)

    before_bytes = database.read_bytes()

    with pytest.raises(OutputPathCollisionError):
        run_audit(
            database=database,
            stale_older_than_seconds=3600,
            now=FIXED_NOW,
            json_output=database,
        )

    # Nothing was written: the database is untouched.
    assert database.read_bytes() == before_bytes


# --- read-only preservation -------------------------------------------------


def test_read_only_connection_rejects_writes(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"
    build_populated_database(database)

    with readonly_database_connection(database) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute(
                "UPDATE jobs SET status = 'pending' WHERE id = 1"
            )


def test_run_audit_leaves_database_byte_identical(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"
    build_populated_database(database)

    before = fingerprint_database(database)
    before_bytes = database.read_bytes()

    run_audit(database=database, stale_older_than_seconds=3600, now=FIXED_NOW)

    after = fingerprint_database(database)
    after_bytes = database.read_bytes()

    assert before == after
    assert before_bytes == after_bytes


def test_run_audit_raises_if_database_mutated_mid_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "audit.db"
    build_populated_database(database)

    import comic_automation.archive.perceptual_coverage_audit as audit_module

    original_fingerprint = audit_module.fingerprint_database
    calls = {"count": 0}

    def mutating_fingerprint(path):
        calls["count"] += 1
        if calls["count"] == 2:
            database.write_bytes(database.read_bytes() + b"\x00")
        return original_fingerprint(path)

    monkeypatch.setattr(
        audit_module, "fingerprint_database", mutating_fingerprint
    )

    with pytest.raises(DatabaseMutatedError):
        run_audit(
            database=database,
            stale_older_than_seconds=3600,
            now=FIXED_NOW,
        )


# --- consistent-snapshot boundary -------------------------------------------


def test_run_audit_reports_snapshot_boundary(tmp_path: Path) -> None:
    """The report surfaces the integrity check and both data_versions."""
    database = tmp_path / "audit.db"
    build_populated_database(database)

    output = run_audit(
        database=database,
        stale_older_than_seconds=3600,
        now=FIXED_NOW,
    )

    assert output["quick_check"] == "ok"
    assert output["data_version_before"] == output["data_version_after"]
    assert output["database_unchanged"] is True


def test_external_commit_mid_classification_invalidates_the_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commit landing between two classification queries is rejected.

    `classify_archives` issues several separate queries, and the report
    claims its five populations partition the library. If a writer
    commits between two of those queries the report silently mixes pre-
    and post-change observations, so the run must fail instead.

    The commit is injected inside `_collect_page_coverage`, i.e. after
    the structural query has already run and before the job queries do
    -- squarely in the middle of the classification, which is exactly
    where the old fingerprint-only guard was blind.
    """
    database = tmp_path / "audit.db"
    ids = build_populated_database(database)

    import comic_automation.archive.perceptual_coverage_audit as audit_module

    real_collect_page_coverage = audit_module._collect_page_coverage
    fingerprint_at_commit: dict[str, object] = {}

    def collect_then_external_commit(connection):
        result = real_collect_page_coverage(connection)

        before = fingerprint_database(database)

        # A *different* connection commits while the audit is mid-read.
        # database_connection() opens in WAL mode, so this commit can
        # land entirely in the -wal file.
        with database_connection(database) as other:
            seed_job(
                other,
                archive_id=ids["unexplained_gap"],
                status="pending",
            )

        fingerprint_at_commit["before"] = before
        fingerprint_at_commit["after"] = fingerprint_database(database)

        return result

    monkeypatch.setattr(
        audit_module,
        "_collect_page_coverage",
        collect_then_external_commit,
    )

    with pytest.raises(DatabaseChangedError) as raised:
        run_audit(
            database=database,
            stale_older_than_seconds=3600,
            now=FIXED_NOW,
        )

    # Specifically the data_version guard, not the fingerprint fallback:
    # DatabaseMutatedError is a *subclass* of DatabaseChangedError, so
    # the exact type is what distinguishes which detector fired.
    assert type(raised.value) is DatabaseChangedError
    assert "data_version" in str(raised.value)

    # The commit really did happen...
    with database_connection(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE archive_id = ?",
                (ids["unexplained_gap"],),
            ).fetchone()[0]
            == 1
        )

    # ...and this is *why* data_version is required: at the moment of
    # the commit the main database file's size and mtime were entirely
    # unchanged, so the fingerprint comparison could not have raised.
    assert fingerprint_at_commit["before"] == fingerprint_at_commit["after"]


def test_wal_commit_can_leave_the_file_fingerprint_unchanged(
    tmp_path: Path,
) -> None:
    """Documents the hazard the data_version guard exists to cover.

    In WAL mode a committed write is appended to the ``-wal`` sidecar
    file; the main database file is only rewritten later, at
    checkpoint. So size + mtime of the database itself can be
    byte-for-byte identical across another connection's commit, and any
    audit relying on that fingerprint alone would report a mixed
    snapshot as clean.
    """
    database = tmp_path / "audit.db"
    ids = build_populated_database(database)

    before = fingerprint_database(database)

    # The writer is deliberately left open across the second stat:
    # closing it would checkpoint the WAL back into the main file and
    # change the fingerprint after the fact. The hazard is about what
    # is observable *at the moment of the commit*, which is when a
    # concurrent audit would be reading.
    writer = connect_database(database)
    try:
        assert (
            writer.execute("PRAGMA journal_mode").fetchone()[0].lower()
            == "wal"
        )
        writer.execute("BEGIN IMMEDIATE")
        seed_job(writer, archive_id=ids["complete"], status="pending")
        writer.execute("COMMIT")

        after = fingerprint_database(database)
        # The commit went to the sidecar, not to the database file.
        # (Closing the writer checkpoints and removes this file, which
        # is why it is asserted here rather than after the finally.)
        assert (database.parent / "audit.db-wal").is_file()
    finally:
        writer.close()

    assert before == after


def test_quick_check_failure_raises_integrity_error(tmp_path: Path) -> None:
    """A structurally damaged database must abort the audit."""
    database = tmp_path / "audit.db"
    build_populated_database(database)

    # Clobber the final page. The schema (page 1 and friends) stays
    # readable, so the database still opens and the audit gets far
    # enough to run quick_check -- which is the point: the integrity
    # guard, not sqlite3's own open-time errors, is what must fire.
    page_size = 4096
    data = bytearray(database.read_bytes())
    assert len(data) > page_size * 2
    data[-page_size:] = bytes([0x5A]) * page_size
    database.write_bytes(bytes(data))

    with pytest.raises(DatabaseIntegrityError) as raised:
        run_audit(
            database=database,
            stale_older_than_seconds=3600,
            now=FIXED_NOW,
        )

    assert "quick_check" in str(raised.value)


def test_missing_database_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        run_audit(
            database=tmp_path / "does-not-exist.db",
            stale_older_than_seconds=3600,
        )
