"""Tests for the read-only batch postflight reconciliation command.

Every test uses a disposable `tmp_path` SQLite database and a disposable
JSON batch-report file. Nothing here touches a production database,
backup, report, or archive path. Every test that runs `run_postflight`
fingerprints the working (and, where relevant, backup) database before
and after the call and asserts nothing changed -- this tool must never
write, no matter which gate fails.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.jobs.active_job_duplicate_audit import (
    fingerprint_database,
)
from comic_automation.jobs.batch_postflight import (
    EXIT_FAILURE,
    EXIT_GATE_FAILURE,
    EXIT_OK,
    EXIT_REQUIRED_GATE_SKIPPED,
    GATE_FAIL,
    GATE_PASS,
    GATE_SKIPPED,
    OutputPathCollisionError,
    OutputPathExistsError,
    main,
    run_postflight,
)


MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


def migrated(
    tmp_path: Path,
    name: str = "postflight.db",
    *,
    through_version: int = 10,
) -> Path:
    database = tmp_path / name
    selected_migrations = (
        tmp_path / f"{database.stem}-migrations-through-{through_version}"
    )
    selected_migrations.mkdir()

    for migration in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql")):
        version = int(migration.stem.split("_", 1)[0])

        if version <= through_version:
            shutil.copyfile(
                migration,
                selected_migrations / migration.name,
            )

    with database_connection(database) as connection:
        apply_migrations(connection, selected_migrations)

    return database


def seed_archive(connection: sqlite3.Connection, file_size: int = 1024) -> int:
    return int(
        connection.execute(
            "INSERT INTO archive_files (file_size) VALUES (?)",
            (file_size,),
        ).lastrowid
    )


def add_page(
    connection: sqlite3.Connection,
    *,
    archive_id: int,
    page_index: int,
) -> int:
    return int(
        connection.execute(
            """
            INSERT INTO archive_pages (
                archive_id, page_index, entry_name, entry_size,
                compressed_size, crc32
            )
            VALUES (?, ?, ?, 100, 100, 0)
            """,
            (archive_id, page_index, f"page-{archive_id}-{page_index}.jpg"),
        ).lastrowid
    )


def add_page_hash(
    connection: sqlite3.Connection,
    *,
    page_id: int,
    algorithm: str,
    algorithm_version: str = "1",
    digest: str = "deadbeef",
) -> None:
    connection.execute(
        """
        INSERT INTO page_hashes (
            page_id, algorithm, algorithm_version, digest, bytes_read
        )
        VALUES (?, ?, ?, ?, 100)
        """,
        (page_id, algorithm, algorithm_version, digest),
    )


def insert_job(
    connection: sqlite3.Connection,
    *,
    job_type: str = "hash_archive_pages_perceptual",
    status: str = "completed",
    archive_id: int | None,
    failure_category: str | None = None,
    error_message: str | None = None,
) -> int:
    return int(
        connection.execute(
            """
            INSERT INTO jobs (
                job_type, status, archive_id, failure_category,
                error_message
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_type, status, archive_id, failure_category, error_message),
        ).lastrowid
    )


def write_batch_report(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def corrupt_database_bytes(path: Path) -> None:
    """Flip every byte after the 100-byte SQLite header.

    Reliably makes `PRAGMA quick_check` fail (or raise
    sqlite3.DatabaseError outright) regardless of the database's exact
    contents/layout -- unlike overwriting a small fixed byte range,
    which can land entirely in unallocated/free space and leave
    quick_check reporting "ok".
    """
    data = bytearray(path.read_bytes())
    for index in range(100, len(data)):
        data[index] ^= 0xFF
    path.write_bytes(bytes(data))


def base_batch_report(**overrides) -> dict:
    payload = {
        "processed": 4,
        "succeeded": 3,
        "terminally_failed": 1,
        "retry_scheduled": 0,
        "enqueued": 4,
    }
    payload.update(overrides)
    return payload


def seed_all_pass_scenario(database: Path) -> None:
    """3 succeeded + 1 terminally-failed hash_archive_pages_perceptual
    jobs, 5 sha256 page-hash rows, 3 aligned dhash/phash v1 rows, no
    active jobs, no near-duplicate candidates, no eligible archives."""
    with database_connection(database) as connection:
        succeeded_archives = [seed_archive(connection) for _ in range(3)]
        failed_archive = seed_archive(connection)

        for archive_id in succeeded_archives:
            insert_job(connection, status="completed", archive_id=archive_id)

        insert_job(
            connection,
            status="failed",
            archive_id=failed_archive,
            failure_category="page_image_corrupt",
            error_message="corrupt page",
        )

        # 5 sha256 page rows (page content hashing, unrelated to this
        # batch and expected to stay exactly at 5).
        page_ids = []
        for index in range(5):
            archive_id = succeeded_archives[index % len(succeeded_archives)]
            page_id = add_page(
                connection, archive_id=archive_id, page_index=index
            )
            page_ids.append(page_id)
            add_page_hash(connection, page_id=page_id, algorithm="sha256")

        # 3 aligned dhash/phash v1 rows for the 3 succeeded archives.
        for index, archive_id in enumerate(succeeded_archives):
            page_id = add_page(
                connection, archive_id=archive_id, page_index=100 + index
            )
            add_page_hash(connection, page_id=page_id, algorithm="dhash")
            add_page_hash(connection, page_id=page_id, algorithm="phash")


def make_git_repository(path: Path) -> Path:
    """A clean, committed git repository for repository_state gate tests."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "file.txt").write_text("hello", encoding="utf-8")
    subprocess.run(
        ["git", "add", "file.txt"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    return path


def run_all_pass(
    tmp_path: Path,
    database: Path,
    *,
    backup_database: Path | None = None,
    **overrides,
) -> dict:
    """Non-production baseline run: every gate that *can* be evaluated
    from a single working database passes.

    The two relaxations are explicit, which is exactly the point of the
    strict-by-default design: no backup is supplied and `tmp_path` is
    not a git checkout, so a production-mode run of this same scenario
    would (correctly) not pass. Tests that care about production mode
    use `run_production_pass` instead.
    """
    report_path = write_batch_report(
        tmp_path / "batch-report.json", base_batch_report()
    )

    kwargs = dict(
        database=database,
        backup_database=backup_database,
        batch_report=report_path,
        expected_processed=4,
        expected_enqueued=4,
        expected_job_population_before=0,
        expected_completed_before=0,
        expected_failed_before=0,
        expected_eligible_remaining=0,
        expected_page_sha256_count=5,
        expected_near_duplicate_count=0,
        repository=tmp_path,  # not a git repo: undeterminable
        allow_missing_backup=True,
        allow_undeterminable_repository=True,
    )
    kwargs.update(overrides)
    return run_postflight(**kwargs)


def run_production_pass(tmp_path: Path, database: Path, **overrides) -> dict:
    """A production-mode run with every required input present.

    Supplies a protected backup plus its real fingerprint and a clean
    git checkout, so no gate is skipped and no relaxation flag is used.
    """
    backup = tmp_path / "production-backup.db"
    shutil.copyfile(database, backup)
    backup_fingerprint = fingerprint_database(backup)
    repository = make_git_repository(tmp_path / "production-repo")

    kwargs = dict(
        backup_database=backup,
        expected_backup_size_bytes=backup_fingerprint.size_bytes,
        expected_backup_modified_time_ns=(
            backup_fingerprint.modified_time_ns
        ),
        repository=repository,
        allow_missing_backup=False,
        allow_undeterminable_repository=False,
    )
    kwargs.update(overrides)
    return run_all_pass(tmp_path, database, **kwargs)


def assert_no_gate_failed(report: dict, *, except_for: tuple[str, ...] = ()) -> None:
    """No gate has status "fail" (skipped gates are tolerated here).

    Also enforces the invariant that makes the tri-state worth having:
    a gate is reported as `pass: True` if and only if its status is
    exactly "pass", so a skipped gate can never masquerade as a passing
    one.
    """
    for name, gate in report["gates"].items():
        assert gate["pass"] is (gate["status"] == GATE_PASS), (
            f"gate {name} has inconsistent status/pass: {gate}"
        )

        if name in except_for:
            continue

        assert gate["status"] != GATE_FAIL, (
            f"gate {name} unexpectedly failed: {gate['detail']}"
        )


# --- all-pass scenario -------------------------------------------------


ALL_GATE_NAMES = {
    "batch_report_processed",
    "batch_report_outcome_reconciliation",
    "enqueue_and_population",
    "cumulative_outcome_reconciliation",
    "no_unexpected_active_jobs",
    "eligibility_recount",
    "hash_alignment",
    "page_sha256_count",
    "near_duplicate_candidates",
    "quick_check",
    "backup_quick_check",
    "active_job_duplicate_audit",
    "backup_active_job_duplicate_audit",
    "backup_fingerprint",
    "repository_state",
    "perceptual_failure_audit",
}


def test_all_gates_pass_on_a_clean_reconciled_batch(tmp_path: Path) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)

    before = fingerprint_database(database)
    report = run_all_pass(tmp_path, database)
    after = fingerprint_database(database)

    assert before == after
    assert report["overall_pass"] is True
    assert_no_gate_failed(report)
    assert set(report["gates"]) == ALL_GATE_NAMES
    # Non-production run: the backup gates are honestly skipped, and
    # the operator opted out of requiring them.
    assert report["summary"]["required_gates_skipped"] == []
    assert set(report["summary"]["skipped_gates"]) == {
        "backup_quick_check",
        "backup_active_job_duplicate_audit",
        "backup_fingerprint",
        "repository_state",
    }


def test_production_run_with_every_required_input_passes(
    tmp_path: Path,
) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)

    before = fingerprint_database(database)
    report = run_production_pass(tmp_path, database)
    after = fingerprint_database(database)

    assert before == after
    assert report["mode"]["production"] is True
    assert report["overall_pass"] is True
    assert_no_gate_failed(report)
    assert set(report["gates"]) == ALL_GATE_NAMES
    # Nothing skipped at all: every gate in the checklist was actually
    # evaluated, which is the only shape a production postflight may
    # report as passing.
    assert report["summary"]["skipped_gates"] == []
    assert report["summary"]["required_gates_skipped"] == []
    assert report["summary"]["failed_gates"] == []
    assert report["summary"]["passed_count"] == len(ALL_GATE_NAMES)
    assert all(
        gate["status"] == GATE_PASS for gate in report["gates"].values()
    )


def test_cli_exits_zero_on_all_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)
    report_path = write_batch_report(
        tmp_path / "batch-report.json", base_batch_report()
    )

    exit_code = main(
        [
            "--database",
            str(database),
            "--batch-report",
            str(report_path),
            "--expected-processed",
            "4",
            "--expected-enqueued",
            "4",
            "--expected-job-population-before",
            "0",
            "--expected-completed-before",
            "0",
            "--expected-failed-before",
            "0",
            "--expected-eligible-remaining",
            "0",
            "--expected-page-sha256-count",
            "5",
            "--repository",
            str(tmp_path),
            "--allow-missing-backup",
            "--allow-undeterminable-repository",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == EXIT_OK
    assert payload["overall_pass"] is True
    assert payload["mode"]["production"] is False


# --- individual gate failures -------------------------------------------


def test_mismatched_processed_count_fails_gate_only(tmp_path: Path) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)

    before = fingerprint_database(database)
    report = run_all_pass(
        tmp_path, database, expected_processed=999
    )
    after = fingerprint_database(database)

    assert before == after
    assert report["overall_pass"] is False
    assert report["gates"]["batch_report_processed"]["pass"] is False
    assert_no_gate_failed(
        report, except_for=("batch_report_processed",)
    )


def test_outcome_reconciliation_fails_when_report_is_internally_inconsistent(
    tmp_path: Path,
) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)
    bad_report = write_batch_report(
        tmp_path / "bad-report.json",
        base_batch_report(succeeded=2),  # 2 + 1 + 0 != 4
    )

    before = fingerprint_database(database)
    report = run_postflight(
        database=database,
        batch_report=bad_report,
        expected_processed=4,
        expected_enqueued=4,
        expected_job_population_before=0,
        expected_completed_before=0,
        expected_failed_before=0,
        expected_eligible_remaining=0,
        expected_page_sha256_count=5,
        repository=tmp_path,
    )
    after = fingerprint_database(database)

    assert before == after
    assert report["overall_pass"] is False
    assert (
        report["gates"]["batch_report_outcome_reconciliation"]["pass"]
        is False
    )


def test_blocking_active_duplicate_group_fails_its_gate(
    tmp_path: Path,
) -> None:
    # Migration 010's unique active-job index makes a true blocking
    # duplicate group impossible in a schema-10 database (SQLite
    # refuses to build a UNIQUE index over data that already violates
    # it), so this exercises the pre-migration-010 shape: a backup or
    # working database at schema 9, still missing the index, that also
    # has two active jobs sharing an identity. Either condition alone
    # already fails this gate; here both are true at once.
    database = migrated(tmp_path, through_version=9)
    seed_all_pass_scenario(database)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)
        insert_job(
            connection,
            job_type="inspect_archive",
            status="pending",
            archive_id=archive_id,
        )
        insert_job(
            connection,
            job_type="inspect_archive",
            status="running",
            archive_id=archive_id,
        )

    before = fingerprint_database(database)
    report = run_all_pass(tmp_path, database)
    after = fingerprint_database(database)

    assert before == after
    assert report["overall_pass"] is False
    assert report["gates"]["active_job_duplicate_audit"]["pass"] is False
    detail = report["gates"]["active_job_duplicate_audit"]["detail"]
    assert detail["blocking_group_count"] == 1
    assert detail["unique_active_index_exists"] is False
    assert_no_gate_failed(
        report, except_for=("active_job_duplicate_audit",)
    )


def test_quick_check_failure_is_reported_and_never_written(
    tmp_path: Path,
) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)

    # Corrupt the file to make PRAGMA quick_check fail, without ever
    # opening it read/write ourselves.
    corrupt_database_bytes(database)

    before = fingerprint_database(database)
    report = run_all_pass(tmp_path, database)
    after = fingerprint_database(database)

    assert before == after
    assert report["overall_pass"] is False
    assert report["gates"]["quick_check"]["pass"] is False


def test_dhash_phash_misalignment_fails_hash_alignment_gate(
    tmp_path: Path,
) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)
        page_id = add_page(connection, archive_id=archive_id, page_index=0)
        # An extra, unmatched dhash row: dhash count now exceeds phash.
        add_page_hash(connection, page_id=page_id, algorithm="dhash")

    before = fingerprint_database(database)
    report = run_all_pass(tmp_path, database)
    after = fingerprint_database(database)

    assert before == after
    assert report["overall_pass"] is False
    assert report["gates"]["hash_alignment"]["pass"] is False
    detail = report["gates"]["hash_alignment"]["detail"]
    assert detail["dhash_v1_count"] != detail["phash_v1_count"]
    assert_no_gate_failed(report, except_for=("hash_alignment",))


def test_unclassified_failure_category_flags_investigation_gate(
    tmp_path: Path,
) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)
        insert_job(
            connection,
            status="failed",
            archive_id=archive_id,
            failure_category=None,  # -> "legacy_unclassified" -> unclassified
            error_message="mystery failure",
        )

    before = fingerprint_database(database)
    # The extra failed job is treated as a pre-existing failure from
    # before this batch (not part of its own reported outcome), so the
    # "before" baselines are bumped by one to keep every other gate
    # reconciling and isolate the failure to perceptual_failure_audit.
    report = run_all_pass(
        tmp_path,
        database,
        expected_job_population_before=1,
        expected_failed_before=1,
    )
    after = fingerprint_database(database)

    assert before == after
    assert report["overall_pass"] is False
    gate = report["gates"]["perceptual_failure_audit"]
    assert gate["pass"] is False
    assert gate["detail"]["categories_needing_investigation_now"][
        "unclassified"
    ] == 1
    assert_no_gate_failed(report, except_for=("perceptual_failure_audit",))


def test_missing_permissions_and_unsupported_categories_also_flag(
    tmp_path: Path,
) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)

    with database_connection(database) as connection:
        for category in (
            "filesystem_not_found",
            "filesystem_permission",
            "unsupported_archive_format",
        ):
            archive_id = seed_archive(connection)
            insert_job(
                connection,
                status="failed",
                archive_id=archive_id,
                failure_category=category,
                error_message="x",
            )

    report = run_all_pass(
        tmp_path,
        database,
        expected_job_population_before=3,
        expected_failed_before=3,
    )

    assert report["overall_pass"] is False
    needs_investigation = report["gates"]["perceptual_failure_audit"][
        "detail"
    ]["categories_needing_investigation_now"]
    assert needs_investigation == {
        "missing_files": 1,
        "permissions": 1,
        "unsupported_formats": 1,
    }


def test_backup_fingerprint_mismatch_fails_only_that_gate(
    tmp_path: Path,
) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)

    backup = tmp_path / "backup.db"
    shutil.copyfile(database, backup)
    real_fingerprint = fingerprint_database(backup)

    before = fingerprint_database(database)
    backup_before = fingerprint_database(backup)

    report = run_all_pass(
        tmp_path,
        database,
        backup_database=backup,
        expected_backup_size_bytes=real_fingerprint.size_bytes + 1,
        expected_backup_modified_time_ns=(
            real_fingerprint.modified_time_ns
        ),
    )

    after = fingerprint_database(database)
    backup_after = fingerprint_database(backup)

    assert before == after
    assert backup_before == backup_after
    assert report["overall_pass"] is False
    assert report["gates"]["backup_fingerprint"]["pass"] is False
    assert_no_gate_failed(report, except_for=("backup_fingerprint",))


def test_backup_quick_check_failure_fails_only_the_backup_gate(
    tmp_path: Path,
) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)

    backup = tmp_path / "backup.db"
    shutil.copyfile(database, backup)
    corrupt_database_bytes(backup)

    report = run_all_pass(tmp_path, database, backup_database=backup)

    assert report["overall_pass"] is False
    # The working database is fine; only the backup's own gate fails.
    # Reporting the two databases as separate gates is what makes this
    # distinguishable at all.
    assert report["gates"]["quick_check"]["status"] == GATE_PASS
    assert report["gates"]["quick_check"]["detail"]["quick_check"] == "ok"
    assert report["gates"]["backup_quick_check"]["status"] == GATE_FAIL
    # The corrupt backup is also unreadable by the duplicate audit; no
    # working-database gate is affected either way.
    assert "quick_check" not in report["summary"]["failed_gates"]
    assert "active_job_duplicate_audit" not in report["summary"][
        "failed_gates"
    ]


def test_dirty_git_tree_fails_repository_state_gate(
    tmp_path: Path,
) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)

    repo = make_git_repository(tmp_path / "repo")

    # Clean and committed: gate should pass.
    clean_report = run_all_pass(tmp_path, database, repository=repo)
    assert clean_report["gates"]["repository_state"]["pass"] is True
    assert clean_report["gates"]["repository_state"]["detail"]["clean"] is True

    # Now dirty the tree.
    (repo / "file.txt").write_text("modified", encoding="utf-8")

    dirty_report = run_all_pass(tmp_path, database, repository=repo)
    assert dirty_report["overall_pass"] is False
    assert dirty_report["gates"]["repository_state"]["pass"] is False
    assert (
        dirty_report["gates"]["repository_state"]["detail"]["clean"] is False
    )
    assert_no_gate_failed(dirty_report, except_for=("repository_state",))


def test_undeterminable_repository_is_a_skip_when_explicitly_allowed(
    tmp_path: Path,
) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)

    report = run_all_pass(
        tmp_path,
        database,
        repository=tmp_path,
        allow_undeterminable_repository=True,
    )

    gate = report["gates"]["repository_state"]
    # Explicitly opted out, so it does not sink the run -- but it is
    # still recorded as skipped, never as a pass.
    assert gate["status"] == GATE_SKIPPED
    assert gate["pass"] is False
    assert gate["required"] is False
    assert gate["detail"]["determinable"] is False
    assert gate["detail"]["warning"]
    assert "repository_state" in report["summary"]["skipped_gates"]
    assert "repository_state" not in report["summary"][
        "required_gates_skipped"
    ]


def test_expected_commit_mismatch_fails_gate(tmp_path: Path) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)

    repo = make_git_repository(tmp_path / "repo2")

    report = run_all_pass(
        tmp_path,
        database,
        repository=repo,
        expected_commit="0" * 40,
    )

    assert report["overall_pass"] is False
    assert report["gates"]["repository_state"]["pass"] is False


def test_eligibility_recount_mismatch_fails_its_gate(tmp_path: Path) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)

    report = run_all_pass(
        tmp_path, database, expected_eligible_remaining=1234
    )

    assert report["overall_pass"] is False
    assert report["gates"]["eligibility_recount"]["pass"] is False
    assert_no_gate_failed(report, except_for=("eligibility_recount",))


def test_page_sha256_mismatch_fails_its_gate(tmp_path: Path) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)

    report = run_all_pass(
        tmp_path, database, expected_page_sha256_count=6
    )

    assert report["overall_pass"] is False
    assert report["gates"]["page_sha256_count"]["pass"] is False
    assert_no_gate_failed(report, except_for=("page_sha256_count",))


def test_near_duplicate_mismatch_fails_its_gate(tmp_path: Path) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)

    with database_connection(database) as connection:
        archive_a = seed_archive(connection)
        archive_b = seed_archive(connection)
        connection.execute(
            """
            INSERT INTO near_duplicate_candidates (
                archive_a_id, archive_b_id, match_method,
                similarity_score, page_match_ratio, compared_page_count,
                page_count_a, page_count_b, average_dhash_distance,
                average_phash_distance, metrics_json
            )
            VALUES (?, ?, 'phase5', 0.9, 0.9, 1, 1, 1, 1.0, 1.0, '{}')
            """,
            (min(archive_a, archive_b), max(archive_a, archive_b)),
        )

    report = run_all_pass(tmp_path, database)

    assert report["overall_pass"] is False
    assert report["gates"]["near_duplicate_candidates"]["pass"] is False
    assert_no_gate_failed(report, except_for=("near_duplicate_candidates",))


def test_unexpected_active_job_fails_its_gate_unless_acknowledged(
    tmp_path: Path,
) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)
        insert_job(
            connection,
            status="pending",
            archive_id=archive_id,
        )

    report = run_all_pass(
        tmp_path, database, expected_job_population_before=1
    )

    assert report["overall_pass"] is False
    assert report["gates"]["no_unexpected_active_jobs"]["pass"] is False
    assert_no_gate_failed(
        report, except_for=("no_unexpected_active_jobs",)
    )


def test_acknowledged_retry_scheduled_job_passes(tmp_path: Path) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)

    with database_connection(database) as connection:
        archive_id = seed_archive(connection)
        insert_job(connection, status="pending", archive_id=archive_id)

    report_path = write_batch_report(
        tmp_path / "batch-report-retry.json",
        base_batch_report(retry_scheduled=1, processed=5),
    )

    report = run_postflight(
        database=database,
        batch_report=report_path,
        expected_processed=5,
        expected_enqueued=4,
        expected_job_population_before=0,
        expected_completed_before=0,
        expected_failed_before=0,
        expected_eligible_remaining=0,
        expected_page_sha256_count=5,
        acknowledge_retry_scheduled=True,
        repository=tmp_path,
    )

    assert report["gates"]["no_unexpected_active_jobs"]["pass"] is True


def test_enqueued_count_mismatch_fails_gate(tmp_path: Path) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)

    report = run_all_pass(tmp_path, database, expected_enqueued=999)

    assert report["overall_pass"] is False
    assert report["gates"]["enqueue_and_population"]["pass"] is False


def test_cumulative_before_mismatch_fails_gate(tmp_path: Path) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)

    report = run_all_pass(tmp_path, database, expected_completed_before=100)

    assert report["overall_pass"] is False
    assert (
        report["gates"]["cumulative_outcome_reconciliation"]["pass"]
        is False
    )


# --- read-only enforcement across every scenario ------------------------


def test_missing_database_raises_without_creating_anything(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "nested" / "absent.db"
    report_path = write_batch_report(
        tmp_path / "batch-report.json", base_batch_report()
    )

    with pytest.raises(FileNotFoundError):
        run_postflight(
            database=missing,
            batch_report=report_path,
            expected_processed=4,
            expected_enqueued=4,
            expected_job_population_before=0,
            expected_completed_before=0,
            expected_failed_before=0,
            expected_eligible_remaining=0,
            expected_page_sha256_count=5,
        )

    assert not missing.exists()
    assert not missing.parent.exists()


def test_cli_missing_database_exits_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report_path = write_batch_report(
        tmp_path / "batch-report.json", base_batch_report()
    )

    exit_code = main(
        [
            "--database",
            str(tmp_path / "absent.db"),
            "--batch-report",
            str(report_path),
            "--expected-processed",
            "4",
            "--expected-enqueued",
            "4",
            "--expected-job-population-before",
            "0",
            "--expected-completed-before",
            "0",
            "--expected-failed-before",
            "0",
            "--expected-eligible-remaining",
            "0",
            "--expected-page-sha256-count",
            "5",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == EXIT_FAILURE
    assert payload["error"] == "FileNotFoundError"


def test_cli_exits_gate_failure_code_when_a_gate_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)
    report_path = write_batch_report(
        tmp_path / "batch-report.json", base_batch_report()
    )

    exit_code = main(
        [
            "--database",
            str(database),
            "--batch-report",
            str(report_path),
            "--expected-processed",
            "999",  # deliberately wrong
            "--expected-enqueued",
            "4",
            "--expected-job-population-before",
            "0",
            "--expected-completed-before",
            "0",
            "--expected-failed-before",
            "0",
            "--expected-eligible-remaining",
            "0",
            "--expected-page-sha256-count",
            "5",
            "--repository",
            str(tmp_path),
            "--allow-missing-backup",
            "--allow-undeterminable-repository",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == EXIT_GATE_FAILURE
    assert payload["overall_pass"] is False
    assert payload["summary"]["failed_gates"] == ["batch_report_processed"]


def test_output_path_colliding_with_database_is_rejected(
    tmp_path: Path,
) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)
    report_path = write_batch_report(
        tmp_path / "batch-report.json", base_batch_report()
    )

    before_bytes = database.read_bytes()

    with pytest.raises(OutputPathCollisionError):
        run_postflight(
            database=database,
            batch_report=report_path,
            expected_processed=4,
            expected_enqueued=4,
            expected_job_population_before=0,
            expected_completed_before=0,
            expected_failed_before=0,
            expected_eligible_remaining=0,
            expected_page_sha256_count=5,
            failure_audit_json_output=database,
            repository=tmp_path,
        )

    assert database.read_bytes() == before_bytes


# --- production mode: required gates may never be silently skipped ------


def test_production_mode_without_backup_does_not_pass(tmp_path: Path) -> None:
    """The defect this mode exists to prevent: a run with no protected
    backup used to report overall_pass true because every backup gate
    was recorded as `{"skipped": True}` alongside `pass: True`."""
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)
    repository = make_git_repository(tmp_path / "repo")

    before = fingerprint_database(database)
    report = run_all_pass(
        tmp_path,
        database,
        repository=repository,
        allow_missing_backup=False,
        allow_undeterminable_repository=False,
    )
    after = fingerprint_database(database)

    assert before == after
    assert report["mode"]["production"] is True
    assert report["overall_pass"] is False
    # Nothing actively disagreed with reality; the run is simply not
    # complete enough to grade, and the report says so explicitly.
    assert report["summary"]["failed_gates"] == []
    assert report["summary"]["required_gates_skipped"] == [
        "backup_active_job_duplicate_audit",
        "backup_fingerprint",
        "backup_quick_check",
    ]

    for name in report["summary"]["required_gates_skipped"]:
        gate = report["gates"][name]
        assert gate["status"] == GATE_SKIPPED
        assert gate["pass"] is False
        assert gate["required"] is True
        assert gate["detail"]["reason"]


def test_production_mode_without_backup_fingerprint_values_does_not_pass(
    tmp_path: Path,
) -> None:
    """Supplying the backup file but not its expected size/mtime is
    still an incomplete production postflight: checklist step 12 was
    not performed."""
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)
    backup = tmp_path / "backup.db"
    shutil.copyfile(database, backup)
    repository = make_git_repository(tmp_path / "repo")

    report = run_all_pass(
        tmp_path,
        database,
        backup_database=backup,
        repository=repository,
        allow_missing_backup=False,
        allow_undeterminable_repository=False,
    )

    assert report["overall_pass"] is False
    assert report["summary"]["failed_gates"] == []
    assert report["summary"]["required_gates_skipped"] == [
        "backup_fingerprint"
    ]
    assert report["gates"]["backup_quick_check"]["status"] == GATE_PASS


def test_production_mode_with_undeterminable_repository_fails(
    tmp_path: Path,
) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)
    backup = tmp_path / "backup.db"
    shutil.copyfile(database, backup)
    backup_fingerprint = fingerprint_database(backup)

    report = run_all_pass(
        tmp_path,
        database,
        backup_database=backup,
        expected_backup_size_bytes=backup_fingerprint.size_bytes,
        expected_backup_modified_time_ns=(
            backup_fingerprint.modified_time_ns
        ),
        repository=tmp_path,  # not a git checkout
        allow_missing_backup=False,
        allow_undeterminable_repository=False,
    )

    assert report["mode"]["production"] is True
    assert report["overall_pass"] is False
    gate = report["gates"]["repository_state"]
    # A failure, not a warning: in production the state was required
    # and could not be established.
    assert gate["status"] == GATE_FAIL
    assert gate["pass"] is False
    assert gate["detail"]["determinable"] is False
    assert gate["detail"]["warning"]
    assert report["summary"]["failed_gates"] == ["repository_state"]
    assert report["summary"]["required_gates_skipped"] == []


def test_no_backup_and_no_git_never_reports_overall_pass(
    tmp_path: Path,
) -> None:
    """The exact regression: no backup + no git access must not pass."""
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)

    report = run_all_pass(
        tmp_path,
        database,
        repository=tmp_path,
        allow_missing_backup=False,
        allow_undeterminable_repository=False,
    )

    assert report["overall_pass"] is False
    assert report["summary"]["failed_gates"] == ["repository_state"]
    assert report["summary"]["required_gates_skipped"] == [
        "backup_active_job_duplicate_audit",
        "backup_fingerprint",
        "backup_quick_check",
    ]


def test_skipped_gates_are_never_reported_as_passing(tmp_path: Path) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)

    for relaxed in (True, False):
        report = run_all_pass(
            tmp_path,
            database,
            repository=tmp_path,
            allow_missing_backup=relaxed,
            allow_undeterminable_repository=relaxed,
        )

        assert report["summary"]["skipped_count"] > 0

        for name, gate in report["gates"].items():
            assert gate["status"] in (GATE_PASS, GATE_FAIL, GATE_SKIPPED)

            if gate["status"] == GATE_SKIPPED:
                assert gate["pass"] is False, (
                    f"skipped gate {name} reported as passing"
                )

        counts = report["summary"]
        assert (
            counts["passed_count"]
            + counts["failed_count"]
            + counts["skipped_count"]
            == counts["gate_count"]
            == len(report["gates"])
        )


def test_non_production_mode_still_fails_real_gate_failures(
    tmp_path: Path,
) -> None:
    """Relaxations are per-concern: opting out of the backup and
    repository gates must not soften anything else."""
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)

    report = run_all_pass(tmp_path, database, expected_page_sha256_count=999)

    assert report["mode"]["production"] is False
    assert report["overall_pass"] is False
    assert report["summary"]["failed_gates"] == ["page_sha256_count"]


def test_cli_exits_required_gate_skipped_code_in_production_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)
    repository = make_git_repository(tmp_path / "repo")
    report_path = write_batch_report(
        tmp_path / "batch-report.json", base_batch_report()
    )

    exit_code = main(
        [
            "--database",
            str(database),
            "--batch-report",
            str(report_path),
            "--expected-processed",
            "4",
            "--expected-enqueued",
            "4",
            "--expected-job-population-before",
            "0",
            "--expected-completed-before",
            "0",
            "--expected-failed-before",
            "0",
            "--expected-eligible-remaining",
            "0",
            "--expected-page-sha256-count",
            "5",
            "--repository",
            str(repository),
            "--production",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    # Distinct from EXIT_GATE_FAILURE: nothing failed, the run was
    # incomplete.
    assert exit_code == EXIT_REQUIRED_GATE_SKIPPED
    assert exit_code != EXIT_GATE_FAILURE
    assert payload["overall_pass"] is False
    assert payload["summary"]["failed_gates"] == []
    assert payload["summary"]["required_gates_skipped"]


def test_cli_production_run_with_every_input_exits_ok(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)
    backup = tmp_path / "backup.db"
    shutil.copyfile(database, backup)
    backup_fingerprint = fingerprint_database(backup)
    repository = make_git_repository(tmp_path / "repo")
    report_path = write_batch_report(
        tmp_path / "batch-report.json", base_batch_report()
    )

    exit_code = main(
        [
            "--database",
            str(database),
            "--backup-database",
            str(backup),
            "--batch-report",
            str(report_path),
            "--expected-processed",
            "4",
            "--expected-enqueued",
            "4",
            "--expected-job-population-before",
            "0",
            "--expected-completed-before",
            "0",
            "--expected-failed-before",
            "0",
            "--expected-eligible-remaining",
            "0",
            "--expected-page-sha256-count",
            "5",
            "--expected-backup-size-bytes",
            str(backup_fingerprint.size_bytes),
            "--expected-backup-modified-time-ns",
            str(backup_fingerprint.modified_time_ns),
            "--repository",
            str(repository),
            "--production",
            "--json-output",
            str(tmp_path / "reports" / "postflight.json"),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == EXIT_OK
    assert payload["overall_pass"] is True
    assert payload["summary"]["skipped_gates"] == []
    assert (tmp_path / "reports" / "postflight.json").is_file()


def test_cli_rejects_production_combined_with_a_relaxation(
    tmp_path: Path,
) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)
    report_path = write_batch_report(
        tmp_path / "batch-report.json", base_batch_report()
    )

    with pytest.raises(SystemExit):
        main(
            [
                "--database",
                str(database),
                "--batch-report",
                str(report_path),
                "--expected-processed",
                "4",
                "--expected-enqueued",
                "4",
                "--expected-job-population-before",
                "0",
                "--expected-completed-before",
                "0",
                "--expected-failed-before",
                "0",
                "--expected-eligible-remaining",
                "0",
                "--expected-page-sha256-count",
                "5",
                "--production",
                "--allow-missing-backup",
            ]
        )


# --- unified input/output path validation -------------------------------


def _postflight_kwargs(database: Path, report_path: Path, **overrides) -> dict:
    kwargs = dict(
        database=database,
        batch_report=report_path,
        expected_processed=4,
        expected_enqueued=4,
        expected_job_population_before=0,
        expected_completed_before=0,
        expected_failed_before=0,
        expected_eligible_remaining=0,
        expected_page_sha256_count=5,
        repository=database.parent,
        allow_missing_backup=True,
        allow_undeterminable_repository=True,
    )
    kwargs.update(overrides)
    return kwargs


def test_output_path_colliding_with_batch_report_is_rejected(
    tmp_path: Path,
) -> None:
    """The genuine gap in the first revision: the batch report was not
    in the collision set, so a failure-audit output could overwrite the
    very input being reconciled against."""
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)
    report_path = write_batch_report(
        tmp_path / "batch-report.json", base_batch_report()
    )
    report_bytes_before = report_path.read_bytes()

    with pytest.raises(OutputPathCollisionError) as excinfo:
        run_postflight(
            **_postflight_kwargs(
                database,
                report_path,
                failure_audit_json_output=report_path,
            )
        )

    assert "--batch-report" in str(excinfo.value)
    assert report_path.read_bytes() == report_bytes_before


def test_csv_output_colliding_with_batch_report_is_rejected(
    tmp_path: Path,
) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)
    report_path = write_batch_report(
        tmp_path / "batch-report.json", base_batch_report()
    )
    report_bytes_before = report_path.read_bytes()

    with pytest.raises(OutputPathCollisionError):
        run_postflight(
            **_postflight_kwargs(
                database,
                report_path,
                failure_audit_csv_output=report_path,
            )
        )

    assert report_path.read_bytes() == report_bytes_before


def test_output_path_colliding_with_backup_is_rejected(
    tmp_path: Path,
) -> None:
    """Already correct before this change; asserted so it stays that
    way."""
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)
    backup = tmp_path / "backup.db"
    shutil.copyfile(database, backup)
    report_path = write_batch_report(
        tmp_path / "batch-report.json", base_batch_report()
    )
    backup_bytes_before = backup.read_bytes()

    with pytest.raises(OutputPathCollisionError) as excinfo:
        run_postflight(
            **_postflight_kwargs(
                database,
                report_path,
                backup_database=backup,
                failure_audit_json_output=backup,
            )
        )

    assert "--backup-database" in str(excinfo.value)
    assert backup.read_bytes() == backup_bytes_before


def test_postflight_report_output_colliding_with_an_input_is_rejected(
    tmp_path: Path,
) -> None:
    """The command's own --json-output is validated too, and before any
    database is opened -- validating it in main() after the run would
    be too late, the failure audit would already have written."""
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)
    report_path = write_batch_report(
        tmp_path / "batch-report.json", base_batch_report()
    )
    database_bytes_before = database.read_bytes()

    with pytest.raises(OutputPathCollisionError) as excinfo:
        run_postflight(
            **_postflight_kwargs(
                database,
                report_path,
                report_json_output=database,
            )
        )

    assert "--json-output" in str(excinfo.value)
    assert database.read_bytes() == database_bytes_before


def test_two_outputs_colliding_with_each_other_are_rejected(
    tmp_path: Path,
) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)
    report_path = write_batch_report(
        tmp_path / "batch-report.json", base_batch_report()
    )
    shared = tmp_path / "audit-output"

    with pytest.raises(OutputPathCollisionError):
        run_postflight(
            **_postflight_kwargs(
                database,
                report_path,
                failure_audit_json_output=shared,
                failure_audit_csv_output=shared,
            )
        )

    assert not shared.exists()


def test_existing_output_path_is_refused_rather_than_overwritten(
    tmp_path: Path,
) -> None:
    """"Always use new output paths": a previous run's evidence is
    never silently replaced."""
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)
    report_path = write_batch_report(
        tmp_path / "batch-report.json", base_batch_report()
    )
    existing = tmp_path / "previous-failure-audit.json"
    existing.write_text('{"previous": "run"}', encoding="utf-8")

    with pytest.raises(OutputPathExistsError):
        run_postflight(
            **_postflight_kwargs(
                database,
                report_path,
                failure_audit_json_output=existing,
            )
        )

    assert json.loads(existing.read_text(encoding="utf-8")) == {
        "previous": "run"
    }


def test_rejected_output_path_creates_no_file_or_directory(
    tmp_path: Path,
) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)
    report_path = write_batch_report(
        tmp_path / "batch-report.json", base_batch_report()
    )
    # A valid output whose parent does not exist yet, paired with an
    # invalid one. Validation runs before any mkdir, so the valid
    # output's directory must not be created either.
    valid_output = tmp_path / "new-reports" / "audit.json"

    with pytest.raises(OutputPathCollisionError):
        run_postflight(
            **_postflight_kwargs(
                database,
                report_path,
                failure_audit_json_output=valid_output,
                failure_audit_csv_output=report_path,
            )
        )

    assert not valid_output.exists()
    assert not valid_output.parent.exists()


def test_databases_are_byte_identical_after_a_rejected_run(
    tmp_path: Path,
) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)
    backup = tmp_path / "backup.db"
    shutil.copyfile(database, backup)
    report_path = write_batch_report(
        tmp_path / "batch-report.json", base_batch_report()
    )

    database_bytes = database.read_bytes()
    backup_bytes = backup.read_bytes()
    database_fingerprint = fingerprint_database(database)
    backup_fingerprint = fingerprint_database(backup)

    with pytest.raises(OutputPathCollisionError):
        run_postflight(
            **_postflight_kwargs(
                database,
                report_path,
                backup_database=backup,
                failure_audit_json_output=report_path,
            )
        )

    assert database.read_bytes() == database_bytes
    assert backup.read_bytes() == backup_bytes
    assert fingerprint_database(database) == database_fingerprint
    assert fingerprint_database(backup) == backup_fingerprint


def test_cli_rejects_json_output_colliding_with_batch_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = migrated(tmp_path)
    seed_all_pass_scenario(database)
    report_path = write_batch_report(
        tmp_path / "batch-report.json", base_batch_report()
    )
    report_bytes_before = report_path.read_bytes()

    exit_code = main(
        [
            "--database",
            str(database),
            "--batch-report",
            str(report_path),
            "--expected-processed",
            "4",
            "--expected-enqueued",
            "4",
            "--expected-job-population-before",
            "0",
            "--expected-completed-before",
            "0",
            "--expected-failed-before",
            "0",
            "--expected-eligible-remaining",
            "0",
            "--expected-page-sha256-count",
            "5",
            "--repository",
            str(tmp_path),
            "--allow-missing-backup",
            "--allow-undeterminable-repository",
            "--json-output",
            str(report_path),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == EXIT_FAILURE
    assert payload["error"] == "OutputPathCollisionError"
    assert report_path.read_bytes() == report_bytes_before
