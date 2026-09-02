"""Tests for the protected-migration guard (slice 4A).

`docs/slice4_migration_design.md` section 4 requires that migration 015 be
unreachable by an ordinary command: `apply_migrations()` must **abort**
while a protected migration is pending rather than skip it and carry on
against schema 014, a strictly read-only diagnostic path must keep
working, and the protected executor must enter through an explicit seam.

Migration 015 does not exist yet, which is exactly why almost every test
here builds its own migrations directory in `tmp_path`. Protection is
declared by version number in `PROTECTED_MIGRATIONS`, not by filename or
file content, so a synthetic `015_*.sql` is protected for the same reason
the real one will be -- and the mechanism can therefore be proven now,
before the file it protects is written.

What each group is guarding
---------------------------

* the declaration -- that 15 is protected at all, and carries a reason;
* the snapshot -- what counts as pending, that asking the question
  creates nothing, and that an ambiguous directory is refused;
* the apply-set invariant -- protected versions cannot reach the SQL;
* `apply_migrations()` -- refuses, refuses *entirely*, mutates nothing on
  the refusal path, is not splittable by a file arriving mid-run, and
  resumes once the version is recorded;
* representative entry points -- one per call-site shape, plus a source
  census proving the eleven are still eleven;
* the read-only path -- unaffected, and structurally incapable of
  migrating;
* migration roots -- the two independent roots stay disjoint, and a
  protected id cannot hide under the wrong one;
* the seam -- authorizes, and applies nothing.
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

from comic_automation.database import migrations as migrations_module
from comic_automation.database import protected_migrations
from comic_automation.database import read_guards
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import (
    applied_versions,
    apply_migrations,
    discover_migrations,
    migration_version,
)
from comic_automation.database.protected_migrations import (
    PROTECTED_MIGRATION_REASONS,
    PROTECTED_MIGRATION_ROOT,
    PROTECTED_MIGRATIONS,
    AmbiguousMigrationError,
    MigrationSnapshot,
    ProtectedExecutionAuthorization,
    ProtectedMigrationError,
    assert_no_pending_protected,
    assert_no_protected_in_apply_set,
    is_protected,
    recorded_versions,
    resolve_protected_execution,
    take_migration_snapshot,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

REAL_MIGRATION_ROOT = (
    REPOSITORY_ROOT / "comic_automation" / "database" / "migrations"
)


# --- synthetic migration roots -------------------------------------------
#
# Each synthetic migration creates one uniquely named table, so a test can
# tell not just *that* something was applied but *which* file did it. That
# distinction is the whole of "refuses rather than skips": a guard that
# skipped 015 and continued would leave the table belonging to 016 behind.


def _write_migration(
    directory: Path,
    version: int,
    suffix: str = "synthetic",
) -> Path:
    """Write a migration creating a table named after its version.

    `suffix` exists so a test can put two differently named files at the
    same version, which is the ambiguity `take_migration_snapshot()` has
    to refuse.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{version:03d}_{suffix}.sql"
    path.write_text(
        f"CREATE TABLE synthetic_{version:03d}_{suffix} "
        "(id INTEGER PRIMARY KEY);\n",
        encoding="utf-8",
    )
    return path


def _migration_root(tmp_path: Path, versions: tuple[int, ...]) -> Path:
    """A migrations directory holding exactly `versions`."""
    directory = tmp_path / "migrations"

    for version in versions:
        _write_migration(directory, version)

    return directory


def _protected_version() -> int:
    """One declared protected version, for tests exercising the mechanism.

    Read from the declaration rather than hard-coded to 15, so these tests
    describe the mechanism and not one instance of it. The separate
    assertion that 15 specifically is protected lives in
    `test_migration_015_is_declared_protected`, which is where a change to
    the *policy* should be forced to show up.
    """
    return min(PROTECTED_MIGRATIONS)


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }


def _snapshot(database: Path, directory: Path) -> MigrationSnapshot:
    """Take a snapshot through a short-lived connection."""
    with database_connection(database) as connection:
        return take_migration_snapshot(connection, directory)


def _schema_and_ledger(database: Path) -> tuple[list[tuple], list[tuple]]:
    """Every schema object and every ledger row, read strictly read-only.

    The evidence behind "a refusal makes no schema or ledger mutation".
    Read through a separate read-only connection so the observation cannot
    itself be what created the ledger, and returned as the full
    ``sqlite_master`` rows rather than a table-name set so a changed
    *definition* is caught as well as a changed set of names.
    """
    with read_guards.readonly_database_connection(database) as connection:
        schema = [
            tuple(row)
            for row in connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "ORDER BY type, name"
            ).fetchall()
        ]

        if any(row[1] == "schema_migrations" for row in schema):
            ledger = [
                tuple(row)
                for row in connection.execute(
                    "SELECT version, name FROM schema_migrations "
                    "ORDER BY version"
                ).fetchall()
            ]
        else:
            ledger = []

    return schema, ledger


# --- the declaration ------------------------------------------------------


def test_migration_015_is_declared_protected() -> None:
    """The policy statement: 015 may never be applied automatically.

    Separate from every mechanism test below, which derives its version
    from the declaration. Removing 15 from the set has to fail exactly
    one test, and it has to be this one, so the failure names the
    decision that was reversed rather than a mechanism that still works.
    """
    assert 15 in PROTECTED_MIGRATIONS


def test_every_protected_version_records_why_it_is_protected() -> None:
    """A refusal has to tell an operator what to do instead.

    Without a reason the message reduces to "refused", and the next step
    (ask the operator to run the protected executor) is not guessable
    from it.
    """
    missing = sorted(PROTECTED_MIGRATIONS - set(PROTECTED_MIGRATION_REASONS))

    assert missing == [], (
        f"protected versions with no recorded reason: {missing}"
    )


def test_is_protected_matches_the_declaration() -> None:
    for version in PROTECTED_MIGRATIONS:
        assert is_protected(version)

    assert not is_protected(14)
    assert not is_protected(max(PROTECTED_MIGRATIONS) + 1)


# --- the snapshot ---------------------------------------------------------


def test_nothing_is_pending_when_the_protected_file_does_not_exist(
    tmp_path: Path,
) -> None:
    """A protected version with no file is not pending.

    This is the repository's state today, and the reason wiring the guard
    into `apply_migrations()` changed no existing behaviour: 015 is
    declared and absent.
    """
    snapshot = _snapshot(
        tmp_path / "comics.db", _migration_root(tmp_path, (1, 2))
    )

    assert snapshot.pending_protected() == []
    # And therefore the guard is silent.
    assert_no_pending_protected(snapshot)


def test_a_present_unapplied_protected_migration_is_pending(
    tmp_path: Path,
) -> None:
    protected = _protected_version()
    snapshot = _snapshot(
        tmp_path / "comics.db", _migration_root(tmp_path, (1, protected))
    )

    assert snapshot.pending_protected() == [protected]


def test_a_recorded_protected_migration_is_no_longer_pending(
    tmp_path: Path,
) -> None:
    """Once the executor has recorded it, ordinary commands work again.

    The guard is keyed on pending-ness, not on the version existing. A
    guard that ignored the ledger would brick every entry point in the
    system permanently the moment 015 landed -- migrated and unusable,
    which is one half of the split brain design section 12.1 describes.
    """
    protected = _protected_version()
    database_path = tmp_path / "comics.db"
    root = _migration_root(tmp_path, (1, protected))

    with database_connection(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
            (protected, f"{protected:03d}_synthetic.sql"),
        )

        snapshot = take_migration_snapshot(connection, root)

    assert snapshot.pending_protected() == []
    assert_no_pending_protected(snapshot)


def test_a_recorded_protected_version_stays_out_of_the_apply_plan(
    tmp_path: Path,
) -> None:
    """The plan's protected filter covers the case the guard cannot.

    A recorded protected version is not *pending*, so the guard has
    nothing to say about it -- and it must still never be re-applied by
    the ordinary path. The filter in `ordinary_apply_plan()` is what
    covers that, and this is the only test that can see it.
    """
    protected = _protected_version()
    root = _migration_root(tmp_path, (1, protected))
    snapshot = MigrationSnapshot(
        directory=root,
        discovered={
            1: root / "001_synthetic.sql",
            protected: root / f"{protected:03d}_synthetic.sql",
        },
        # Neither is recorded, so both are pending; only the protected
        # one may be filtered out.
        recorded=frozenset(),
    )

    assert snapshot.pending() == [1, protected]
    assert [version for version, _ in snapshot.ordinary_apply_plan()] == [1]


def test_asking_the_question_does_not_create_the_ledger(
    tmp_path: Path,
) -> None:
    """Taking a snapshot reads the ledger without creating it.

    `applied_versions()` calls `ensure_migration_table()`, so building the
    snapshot on it would have made the refusal path create
    ``schema_migrations`` -- a schema mutation performed by a run that
    refused to do anything. Asserted on a database with no ledger at all,
    which is where the claim is most obviously expected to hold.
    """
    protected = _protected_version()
    database_path = tmp_path / "comics.db"
    root = _migration_root(tmp_path, (protected,))

    with database_connection(database_path) as connection:
        assert "schema_migrations" not in _table_names(connection)

        assert recorded_versions(connection) == set()
        snapshot = take_migration_snapshot(connection, root)
        assert snapshot.pending_protected() == [protected]

        with pytest.raises(ProtectedMigrationError):
            assert_no_pending_protected(snapshot)

        assert "schema_migrations" not in _table_names(connection)


def test_recorded_versions_does_not_hide_a_broken_ledger(
    tmp_path: Path,
) -> None:
    """A structurally broken ledger raises; it does not read as empty.

    The tempting implementation -- catch `sqlite3.OperationalError` around
    the SELECT and return an empty set -- would swallow a corrupted or
    renamed ledger and present the database as freshly initialised, so a
    protected migration would look pending on a database whose real state
    is unknown. Simulated here with a ``schema_migrations`` that exists
    but has no ``version`` column.
    """
    database_path = tmp_path / "comics.db"

    with database_connection(database_path) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (wrong_column TEXT)"
        )

        with pytest.raises(sqlite3.OperationalError):
            recorded_versions(connection)


def test_two_files_claiming_one_version_are_refused(
    tmp_path: Path,
) -> None:
    """An ambiguous directory is refused, not silently collapsed.

    The version-to-path mapping used to be a dictionary comprehension,
    which kept whichever file sorted last and dropped the other without a
    word. Which of two migrations ran was therefore decided by filename
    sort order, and the loser was left permanently unapplied *and*
    unrecorded -- ``schema_migrations.version`` is an INTEGER PRIMARY KEY,
    so the ledger cannot even represent the state.
    """
    directory = tmp_path / "migrations"
    _write_migration(directory, 1)
    _write_migration(directory, 7, suffix="a")
    _write_migration(directory, 7, suffix="b")

    database_path = tmp_path / "comics.db"

    with database_connection(database_path) as connection:
        with pytest.raises(AmbiguousMigrationError) as caught:
            take_migration_snapshot(connection, directory)

    message = str(caught.value)

    # Both names, so the operator can go and look at them.
    assert "007_a.sql" in message
    assert "007_b.sql" in message


def test_a_duplicated_protected_version_is_refused_as_ambiguous(
    tmp_path: Path,
) -> None:
    """Ambiguity is refused before protection is even considered.

    Worth its own test because the two refusals could be confused: a
    duplicated *protected* version must not be reported as "a protected
    migration is pending", which would send an operator to the protected
    executor to run a file that cannot be identified.
    """
    protected = _protected_version()
    directory = tmp_path / "migrations"
    _write_migration(directory, protected, suffix="a")
    _write_migration(directory, protected, suffix="b")

    database_path = tmp_path / "comics.db"

    with database_connection(database_path) as connection:
        with pytest.raises(AmbiguousMigrationError):
            take_migration_snapshot(connection, directory)


def test_apply_migrations_refuses_a_duplicate_version_directory(
    tmp_path: Path,
) -> None:
    """And the ordinary path inherits the refusal, applying nothing.

    Previously it applied whichever file came first and then skipped the
    second as "already applied", so half an ambiguous directory ran and
    nothing said so.
    """
    directory = tmp_path / "migrations"
    _write_migration(directory, 1)
    _write_migration(directory, 7, suffix="a")
    _write_migration(directory, 7, suffix="b")

    database_path = tmp_path / "comics.db"

    with database_connection(database_path) as connection:
        with pytest.raises(AmbiguousMigrationError):
            apply_migrations(connection, directory)

        assert _table_names(connection) == set()


def test_the_refusal_names_the_version_and_its_reason(
    tmp_path: Path,
) -> None:
    protected = _protected_version()
    snapshot = _snapshot(
        tmp_path / "comics.db", _migration_root(tmp_path, (protected,))
    )

    with pytest.raises(ProtectedMigrationError) as caught:
        assert_no_pending_protected(snapshot)

    message = str(caught.value)

    assert f"migration {protected:03d}" in message
    assert PROTECTED_MIGRATION_REASONS[protected] in message
    # And it points at the path that still works, since the operator who
    # hit this usually wanted to read something.
    assert "readonly_database_connection" in message


# --- the apply-set invariant ---------------------------------------------


def test_the_apply_set_invariant_rejects_a_protected_version(
    tmp_path: Path,
) -> None:
    """The last check before any SQL runs, tested directly.

    By the time this runs the guard has already returned, so nothing else
    is looking. It exists for the case where `ordinary_apply_plan()`
    stops filtering -- which no other test in this file can reach,
    because the plan builder currently works.
    """
    protected = _protected_version()
    root = _migration_root(tmp_path, (1, protected))

    assert_no_protected_in_apply_set(((1, root / "001_synthetic.sql"),))

    with pytest.raises(ProtectedMigrationError) as caught:
        assert_no_protected_in_apply_set(
            (
                (1, root / "001_synthetic.sql"),
                (protected, root / f"{protected:03d}_synthetic.sql"),
            )
        )

    # Named as the invariant violation it is, so an operator is not sent
    # looking for a pending migration that is not the problem.
    assert "invariant violation" in str(caught.value)


def test_a_broken_plan_builder_is_stopped_before_any_sql_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fault injection for the invariant, through the production path.

    The plan builder is replaced with one that has stopped filtering
    protected versions -- the failure the invariant exists for. The
    ledger records the protected version first, so the guard passes and
    the invariant is genuinely the only thing left between the plan and
    the SQL.

    `synthetic_001` must not exist afterwards either: the invariant is
    checked against the whole plan before the loop starts, so a broken
    plan applies nothing at all rather than applying the ordinary
    migrations and then tripping.
    """
    protected = _protected_version()
    database_path = tmp_path / "comics.db"
    root = _migration_root(tmp_path, (1, protected))

    def unfiltered_plan(
        self: MigrationSnapshot,
    ) -> tuple[tuple[int, Path], ...]:
        return tuple(
            (version, self.discovered[version])
            for version in sorted(self.discovered)
        )

    with database_connection(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
            (protected, f"{protected:03d}_synthetic.sql"),
        )

        # The guard passes: the protected version is recorded, so it is
        # not pending.
        assert_no_pending_protected(
            take_migration_snapshot(connection, root)
        )

        monkeypatch.setattr(
            MigrationSnapshot, "ordinary_apply_plan", unfiltered_plan
        )

        with pytest.raises(ProtectedMigrationError):
            apply_migrations(connection, root)

        tables = _table_names(connection)

    assert "synthetic_001_synthetic" not in tables
    assert f"synthetic_{protected:03d}_synthetic" not in tables


# --- apply_migrations fails closed ---------------------------------------


def test_apply_migrations_refuses_while_a_protected_migration_is_pending(
    tmp_path: Path,
) -> None:
    protected = _protected_version()
    database_path = tmp_path / "comics.db"
    root = _migration_root(tmp_path, (1, protected))

    with database_connection(database_path) as connection:
        with pytest.raises(ProtectedMigrationError):
            apply_migrations(connection, root)


def test_apply_migrations_refuses_rather_than_skipping_the_protected_one(
    tmp_path: Path,
) -> None:
    """R6, stated as the negative it is.

    A guard that skipped 015 and continued would apply 001 and 016 and
    report success, leaving every entry point running producer code
    against a schema that is neither 014 nor 015 while the operator
    believes the migration is merely queued. Each synthetic migration
    creates a differently named table, so "nothing was skipped past" is
    asserted against the tables that would exist, not only against the
    ledger.
    """
    protected = _protected_version()
    later = protected + 1
    assert not is_protected(later), (
        "this test needs an UNPROTECTED migration above the protected one"
    )

    database_path = tmp_path / "comics.db"
    root = _migration_root(tmp_path, (1, protected, later))

    with database_connection(database_path) as connection:
        with pytest.raises(ProtectedMigrationError):
            apply_migrations(connection, root)

        tables = _table_names(connection)

    # Neither the migration below the protected one nor the one above it.
    assert "synthetic_001_synthetic" not in tables
    assert f"synthetic_{later:03d}_synthetic" not in tables
    assert f"synthetic_{protected:03d}_synthetic" not in tables


def test_a_protected_file_arriving_after_the_guard_is_not_applied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fail-open defect this whole snapshot design exists to close.

    `apply_migrations()` used to scan the directory for the guard and
    then scan it again to build its apply list. A `015_*.sql` created
    between those two scans was invisible to the first and visible to the
    second, so an ordinary command applied a protected migration and
    wrote its ledger row. Reproduced in review as
    ``result [1, 15]``, ``ledger [(1, ...), (15, '015_arrived_late.sql')]``.

    The file is planted at `ensure_migration_table()`, which is a real
    production call site sitting between the guard and the apply loop.
    Deterministic: no threads, no sleeps, no racing.

    The last assertion is what makes this a test of coherence rather than
    of luck. The planted file really is on disk and really is
    discoverable -- a *second* call refuses because of it. It was
    excluded from the first run because that run's plan was fixed before
    it arrived, not because nothing could see it.
    """
    protected = _protected_version()
    database_path = tmp_path / "comics.db"
    root = _migration_root(tmp_path, (1,))

    real_ensure = migrations_module.ensure_migration_table
    planted: list[Path] = []

    def plant_then_delegate(connection: sqlite3.Connection) -> None:
        if not planted:
            planted.append(
                _write_migration(root, protected, suffix="arrived_late")
            )
        return real_ensure(connection)

    monkeypatch.setattr(
        migrations_module, "ensure_migration_table", plant_then_delegate
    )

    with database_connection(database_path) as connection:
        applied = apply_migrations(connection, root)

        tables = _table_names(connection)
        ledger = set(applied_versions(connection))

        # The injection happened, and the file is still there.
        assert planted and planted[0].is_file()

        assert applied == [1]
        assert ledger == {1}
        assert f"synthetic_{protected:03d}_arrived_late" not in tables

        with pytest.raises(ProtectedMigrationError):
            apply_migrations(connection, root)


def test_a_refusal_mutates_neither_schema_nor_ledger(
    tmp_path: Path,
) -> None:
    """The refusal is inert, on a database that already has a ledger.

    The companion to
    `test_a_refusal_on_a_fresh_database_creates_no_ledger`: that one
    proves nothing is created from nothing, this one proves nothing
    already present is touched. Compared over full ``sqlite_master`` rows,
    so a changed table *definition* would be caught as well as a changed
    set of names.
    """
    protected = _protected_version()
    database_path = tmp_path / "comics.db"

    ordinary_root = _migration_root(tmp_path / "before", (1, 2))
    with database_connection(database_path) as connection:
        assert apply_migrations(connection, ordinary_root) == [1, 2]

    before = _schema_and_ledger(database_path)

    protected_root = _migration_root(tmp_path / "after", (1, 2, protected))
    with database_connection(database_path) as connection:
        with pytest.raises(ProtectedMigrationError):
            apply_migrations(connection, protected_root)

    assert _schema_and_ledger(database_path) == before


def test_a_refusal_on_a_fresh_database_creates_no_ledger(
    tmp_path: Path,
) -> None:
    """The refusal path of `apply_migrations()` itself creates nothing.

    Distinct from `test_asking_the_question_does_not_create_the_ledger`,
    which calls the guard directly, and from
    `test_a_refusal_mutates_neither_schema_nor_ledger`, whose database
    already has a ledger so `ensure_migration_table()` is a no-op there.
    Neither of those notices where the guard sits relative to
    `ensure_migration_table()`, and moving it after that call failed no
    test until this one existed -- found by bypassing the ordering and
    watching nothing break.

    A fresh database is the only state that can tell the two orderings
    apart: refusing *after* ensure_migration_table() leaves behind a
    schema_migrations table created by a run that applied nothing.
    """
    protected = _protected_version()
    database_path = tmp_path / "comics.db"
    root = _migration_root(tmp_path, (1, protected))

    with database_connection(database_path) as connection:
        assert "schema_migrations" not in _table_names(connection)

        with pytest.raises(ProtectedMigrationError):
            apply_migrations(connection, root)

        assert _table_names(connection) == set(), (
            "a refusing run created schema objects; the guard must be "
            "checked before ensure_migration_table()"
        )


def test_apply_migrations_resumes_once_the_protected_version_is_recorded(
    tmp_path: Path,
) -> None:
    """After the protected executor records 015, ordinary work continues.

    Stands in for the executor without being it: the ledger row is written
    directly, which is the only part of section 12.1 this slice's state
    depends on. The point is that the guard releases, so the system is not
    permanently bricked by a migration it has already run.
    """
    protected = _protected_version()
    later = protected + 1
    database_path = tmp_path / "comics.db"
    root = _migration_root(tmp_path, (1, protected, later))

    with database_connection(database_path) as connection:
        with pytest.raises(ProtectedMigrationError):
            apply_migrations(connection, root)

        # What the protected executor will do inside its own transaction.
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            f"CREATE TABLE synthetic_{protected:03d}_synthetic "
            "(id INTEGER PRIMARY KEY)"
        )
        connection.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
            (protected, f"{protected:03d}_synthetic.sql"),
        )

        assert apply_migrations(connection, root) == [1, later]
        assert applied_versions(connection) == {1, protected, later}


def test_unprotected_migrations_are_unaffected(tmp_path: Path) -> None:
    """Ordinary behaviour, preserved: apply once, then a no-op."""
    database_path = tmp_path / "comics.db"
    root = _migration_root(tmp_path, (1, 2, 3))

    with database_connection(database_path) as connection:
        assert apply_migrations(connection, root) == [1, 2, 3]
        assert apply_migrations(connection, root) == []
        assert _table_names(connection) >= {
            "synthetic_001_synthetic",
            "synthetic_002_synthetic",
            "synthetic_003_synthetic",
        }


def test_the_real_migration_set_still_applies(tmp_path: Path) -> None:
    """The guard is silent against the tree as it stands.

    The link between the mechanism and today's repository: 015 is declared
    protected and absent, so nothing is pending and the real 001..014 set
    applies exactly as it did before the guard existed.
    """
    database_path = tmp_path / "comics.db"
    expected = [
        migration_version(path)
        for path in discover_migrations(REAL_MIGRATION_ROOT)
    ]

    with database_connection(database_path) as connection:
        snapshot = take_migration_snapshot(connection, REAL_MIGRATION_ROOT)
        assert snapshot.pending_protected() == []
        assert apply_migrations(connection, REAL_MIGRATION_ROOT) == expected


# --- representative entry points ------------------------------------------
#
# The guard lives inside `apply_migrations()`, so all eleven call sites
# inherit it at one place. These cover the three call-site *shapes* rather
# than all eleven: a directory passed as an argument, a module-level
# constant, and the long-running service.


def test_entry_point_with_an_argument_directory_fails_closed(
    tmp_path: Path,
) -> None:
    """`archive.cli.run_inspection_jobs` -- the parameterized shape.

    Shared with duplicate_resolution_cli, quarantine_cli and library/cli.
    The database is an empty file: the refusal must happen before any work
    that would need a real schema, so an empty database is enough to reach
    it and proves nothing ran first.
    """
    from comic_automation.archive import cli as archive_cli

    protected = _protected_version()
    database_path = tmp_path / "comics.db"
    database_path.touch()
    root = _migration_root(tmp_path, (protected,))

    with pytest.raises(ProtectedMigrationError):
        archive_cli.run_inspection_jobs(
            database=database_path,
            limit=1,
            progress_every=1,
            verify_crc=False,
            retry_delay_seconds=30,
            migration_directory=root,
        )


def test_entry_point_with_a_module_constant_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`archive.hash_cli` -- the module-constant shape.

    Shared with near_duplicate_cli, page_hash_cli, perceptual_hash_cli and
    the profiling benchmark. These read a module-level `MIGRATIONS`, so the
    test redirects that constant rather than passing a directory.
    """
    from comic_automation.archive import hash_cli

    protected = _protected_version()
    database_path = tmp_path / "comics.db"
    database_path.touch()
    root = _migration_root(tmp_path, (protected,))

    monkeypatch.setattr(hash_cli, "MIGRATIONS", root)

    with pytest.raises(ProtectedMigrationError):
        hash_cli.run_hashing(
            database=database_path,
            limit=1,
            progress_every=1,
            enqueue_missing=False,
            report_only=False,
            json_output=None,
        )


def test_the_service_fails_closed_at_startup(tmp_path: Path) -> None:
    """`ComicAutomationService.initialize()` -- the long-running shape.

    The one that matters most: a service that started, skipped 015 and
    began running handlers against schema 014 is the failure R6 exists to
    prevent, and it is the only entry point that would then keep writing
    for hours without anybody re-reading a CLI's output.

    The config is written here rather than reused from `test_service.py`
    so this file does not fail when that one's helper changes shape.
    """
    from comic_automation.service import ComicAutomationService

    protected = _protected_version()
    workspace = tmp_path / "workspace"
    library = tmp_path / "library"
    library.mkdir()
    config_path = tmp_path / "service.toml"
    config_path.write_text(
        f"""
[workspace]
root = '{workspace.as_posix()}'
database = '{(workspace / "database" / "comics.db").as_posix()}'
cache = '{(workspace / "cache").as_posix()}'
embeddings = '{(workspace / "embeddings").as_posix()}'
staging = '{(workspace / "staging").as_posix()}'
temp = '{(workspace / "temp").as_posix()}'
logs = '{(workspace / "logs").as_posix()}'
backups = '{(workspace / "backups").as_posix()}'

[library]
root = '{library.as_posix()}'

[service]
poll_interval_seconds = 1
cpu_workers = 1
gpu_workers = 1
operating_mode = "audit"
""",
        encoding="utf-8",
    )

    service = ComicAutomationService(
        config_path,
        migration_directory=_migration_root(tmp_path, (protected,)),
    )

    with pytest.raises(ProtectedMigrationError):
        service.initialize()


# --- the read-only path ---------------------------------------------------


def test_the_read_only_path_works_while_a_protected_migration_is_pending(
    tmp_path: Path,
) -> None:
    """Fail-closed must not mean fail-blind.

    An operator facing a pending 015 needs to be able to look at the
    database -- that is most of what they will want to do -- so the
    strictly read-only path is asserted to keep working under exactly the
    condition that stops every writing entry point, and to leave schema
    and ledger identical.
    """
    protected = _protected_version()
    database_path = tmp_path / "comics.db"

    with database_connection(database_path) as connection:
        apply_migrations(connection, _migration_root(tmp_path / "a", (1, 2)))

    protected_root = _migration_root(tmp_path / "b", (1, 2, protected))

    assert _snapshot(database_path, protected_root).pending_protected() == [
        protected
    ]

    before = _schema_and_ledger(database_path)

    snapshot = read_guards.read_consistent_snapshot(
        database_path,
        lambda connection: sorted(_table_names(connection)),
        context="protected-pending read",
    )

    assert "synthetic_001_synthetic" in snapshot.result
    assert snapshot.quick_check == "ok"
    assert snapshot.data_version_unchanged
    assert _schema_and_ledger(database_path) == before


def test_the_read_only_path_cannot_migrate_at_all() -> None:
    """Structural, not behavioural: `read_guards` never migrates.

    A functional test can only show that migration did not happen on the
    path it exercised. Reading the module's own source shows it *cannot*
    happen on any path, which is the property design section 4.2 asks for
    ("a query-only path that never calls apply_migrations").
    """
    source = Path(read_guards.__file__).read_text(encoding="utf-8")

    assert "apply_migrations" not in source
    assert "ensure_migration_table" not in source


def test_the_read_only_connection_rejects_writes(tmp_path: Path) -> None:
    """`query_only` is what makes "read-only" more than a naming claim."""
    database_path = tmp_path / "comics.db"

    with database_connection(database_path) as connection:
        apply_migrations(connection, _migration_root(tmp_path, (1,)))

    with read_guards.readonly_database_connection(database_path) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE intruder (id INTEGER)")


# --- migration roots ------------------------------------------------------
#
# Design section 4.1: `scripts/db.py` has its own `apply_migrations` over
# its own root and will never discover 015. That is settled -- but it is
# settled only for as long as the two roots stay apart, and a guard can
# only protect the root it is pointed at.


def _protected_files_under(root: Path) -> list[Path]:
    """Migration files under `root` whose version is declared protected.

    The check itself, factored out so the fault-injection test below can
    prove it fires rather than being trusted to.
    """
    if not root.exists():
        return []

    return [
        path
        for path in discover_migrations(root)
        if is_protected(migration_version(path))
    ]


def _apply_migrations_call_sites() -> dict[str, int]:
    """Census of every call to the package's `apply_migrations`.

    Parsed rather than grepped, so a call inside a string or a comment
    cannot inflate the count and a line-wrapped call cannot escape it.

    A module qualifies only if it *imports* `apply_migrations` from
    `comic_automation.database.migrations`, which is what separates the
    guarded implementation from `scripts/db.py`'s independent one of the
    same name (design section 4.1) and from the definition itself.
    """
    roots = (
        REPOSITORY_ROOT / "comic_automation",
        REPOSITORY_ROOT / "scripts",
    )
    census: dict[str, int] = {}

    for directory in roots:
        for path in sorted(directory.rglob("*.py")):
            tree = ast.parse(
                path.read_text(encoding="utf-8"), filename=str(path)
            )

            imports_it = any(
                isinstance(node, ast.ImportFrom)
                and node.module == "comic_automation.database.migrations"
                and any(
                    alias.name == "apply_migrations" for alias in node.names
                )
                for node in ast.walk(tree)
            )

            if not imports_it:
                continue

            calls = sum(
                1
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "apply_migrations"
            )

            if calls:
                census[
                    path.relative_to(REPOSITORY_ROOT).as_posix()
                ] = calls

    return census


def test_the_two_migration_roots_are_distinct() -> None:
    from scripts.db import DEFAULT_MIGRATIONS_DIR

    assert DEFAULT_MIGRATIONS_DIR.resolve() != PROTECTED_MIGRATION_ROOT
    assert PROTECTED_MIGRATION_ROOT == REAL_MIGRATION_ROOT


def test_neither_migration_root_contains_the_other() -> None:
    """Disjoint, not merely unequal.

    A nested root would make one runner's discovery a superset of the
    other's, which is how a protected file ends up being applied by the
    runner that has no guard.
    """
    from scripts.db import DEFAULT_MIGRATIONS_DIR

    scripts_root = DEFAULT_MIGRATIONS_DIR.resolve()

    assert PROTECTED_MIGRATION_ROOT not in scripts_root.parents
    assert scripts_root not in PROTECTED_MIGRATION_ROOT.parents

    scripts_files = {path.name for path in discover_migrations(scripts_root)}
    protected_files = {
        path.name for path in discover_migrations(PROTECTED_MIGRATION_ROOT)
    }

    assert scripts_files.isdisjoint(protected_files)


def test_no_protected_migration_lives_under_the_scripts_root() -> None:
    """The assertion section 4.1 asks for, as a test rather than a comment.

    `scripts/db.py` applies its root with `executescript()` and no guard.
    A protected id appearing there would be applied with none of section
    8's protocol and nobody would find out from this repository's tests.
    """
    from scripts.db import DEFAULT_MIGRATIONS_DIR

    found = _protected_files_under(DEFAULT_MIGRATIONS_DIR.resolve())

    assert found == [], (
        "a protected migration is sitting under the unguarded scripts/db.py "
        f"root: {found}"
    )


def test_the_wrong_root_check_actually_fires(tmp_path: Path) -> None:
    """Fault injection for the check above.

    `test_no_protected_migration_lives_under_the_scripts_root` passes
    today because that directory holds one file, and it would pass just as
    quietly if `_protected_files_under` were broken. Seeding a protected
    file under a stand-in root proves the check reports it.
    """
    protected = _protected_version()
    wrong_root = tmp_path / "wrong_root"
    _write_migration(wrong_root, 1)
    planted = _write_migration(wrong_root, protected)

    assert _protected_files_under(wrong_root) == [planted]


def test_every_entry_point_points_at_the_protected_root() -> None:
    """All eleven `apply_migrations()` call sites use the guarded root.

    The guard computes its pending set from the directory it is handed, so
    an entry point aimed elsewhere would find no protected file, see an
    empty pending set, and pass -- fail-*open*, silently. This is the
    assertion that keeps the "eleven call sites, one guard" claim true.

    The list below is written by hand, and an earlier revision claimed a
    twelfth call site would fail its count. It would not have: a
    hand-written dictionary is length 11 whatever the source tree does.
    `_apply_migrations_call_sites()` is a real AST census of the tree, and
    comparing the two is what actually detects a new caller -- the census
    grows, the hand-written map does not, and the sets differ.
    """
    from comic_automation.archive import cli as archive_cli
    from comic_automation.archive import duplicate_resolution_cli
    from comic_automation.archive import hash_cli
    from comic_automation.archive import near_duplicate_cli
    from comic_automation.archive import page_hash_cli
    from comic_automation.archive import perceptual_hash_cli
    from comic_automation.archive import quarantine_cli
    from comic_automation.jobs import enqueue_missing_stages
    from comic_automation.library import cli as library_cli
    from comic_automation import service
    from scripts import benchmark_perceptual_hash_profiling as benchmark

    roots = {
        "comic_automation/archive/cli.py": (
            archive_cli.DEFAULT_MIGRATION_DIRECTORY
        ),
        "comic_automation/archive/duplicate_resolution_cli.py": (
            duplicate_resolution_cli.DEFAULT_MIGRATION_DIRECTORY
        ),
        "comic_automation/archive/hash_cli.py": hash_cli.MIGRATIONS,
        "comic_automation/archive/near_duplicate_cli.py": (
            near_duplicate_cli.MIGRATIONS
        ),
        "comic_automation/archive/page_hash_cli.py": (
            page_hash_cli.MIGRATIONS
        ),
        "comic_automation/archive/perceptual_hash_cli.py": (
            perceptual_hash_cli.MIGRATIONS
        ),
        "comic_automation/archive/quarantine_cli.py": (
            quarantine_cli.DEFAULT_MIGRATION_DIRECTORY
        ),
        "comic_automation/jobs/enqueue_missing_stages.py": (
            enqueue_missing_stages.MIGRATIONS_DIRECTORY
        ),
        "comic_automation/library/cli.py": (
            library_cli.DEFAULT_MIGRATION_DIRECTORY
        ),
        "comic_automation/service.py": service.DEFAULT_MIGRATION_DIRECTORY,
        "scripts/benchmark_perceptual_hash_profiling.py": (
            benchmark.MIGRATIONS
        ),
    }

    census = _apply_migrations_call_sites()

    assert set(census) == set(roots), (
        "the source tree's apply_migrations() callers and this test's "
        "hand-written root map disagree; a caller was added or removed "
        f"without updating this test. only in source: "
        f"{sorted(set(census) - set(roots))}; only in this test: "
        f"{sorted(set(roots) - set(census))}"
    )

    assert sum(census.values()) == 11, (
        "design section 4 measured eleven apply_migrations() call sites; "
        f"the tree now has {sum(census.values())}. Update this test and "
        "the design's count together."
    )

    wrong = {
        name: str(root)
        for name, root in roots.items()
        if Path(root).resolve() != PROTECTED_MIGRATION_ROOT
    }

    assert wrong == {}, (
        "these entry points migrate a directory the protected-migration "
        f"guard does not cover: {wrong}"
    )


def test_the_call_site_census_detects_a_new_caller(tmp_path: Path) -> None:
    """Fault injection for the census, so it is not trusted on faith.

    The census is what makes the test above future-detecting rather than
    decorative, so it needs its own proof that it sees a caller it has
    not seen before. Parsed here from source text rather than by writing
    a file into the package, which would pollute the tree the census
    scans.
    """
    module = (
        "from comic_automation.database.migrations import apply_migrations\n"
        "\n"
        "def go(connection, directory):\n"
        "    return apply_migrations(connection, directory)\n"
    )
    tree = ast.parse(module)

    imports_it = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "comic_automation.database.migrations"
        and any(alias.name == "apply_migrations" for alias in node.names)
        for node in ast.walk(tree)
    )
    calls = sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "apply_migrations"
    )

    assert imports_it and calls == 1

    # And the shape the census must NOT count: scripts/db.py's own
    # implementation, which is called without importing the package's.
    other = ast.parse(
        "def apply_migrations(conn, d):\n"
        "    return []\n"
        "\n"
        "def go(conn, d):\n"
        "    return apply_migrations(conn, d)\n"
    )
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module == "comic_automation.database.migrations"
        for node in ast.walk(other)
    )


def test_the_protected_root_is_where_protected_ids_belong(
    tmp_path: Path,
) -> None:
    """Protected ids may exist under the protected root, and nowhere else.

    Stated as a permission rather than as "the root contains every
    protected id", because 015 does not exist yet: asserting its presence
    would fail until the migration lands and would then be asserting the
    wrong thing anyway -- a protected version is allowed to be declared
    before its file is written, and this slice is exactly that state.
    """
    assert _protected_files_under(PROTECTED_MIGRATION_ROOT) == []

    stand_in = tmp_path / "stand_in_root"
    planted = _write_migration(stand_in, _protected_version())

    # Discovery does find it when it exists; nothing about the protected
    # root is special to `discover_migrations`, which is precisely why the
    # root has to be asserted separately from the version.
    assert planted in discover_migrations(stand_in)


# --- the protected-execution seam -----------------------------------------


def _authorization(
    versions: object = None,
) -> ProtectedExecutionAuthorization:
    return ProtectedExecutionAuthorization(
        versions=frozenset(
            versions if versions is not None else {_protected_version()}
        ),
        operator="lead",
        reason="slice 4 execution",
    )


def test_an_authorization_must_name_at_least_one_version() -> None:
    with pytest.raises(ProtectedMigrationError):
        _authorization(versions=set())


def test_an_authorization_may_only_name_protected_versions() -> None:
    """The seam cannot be used to smuggle an ordinary migration through.

    Without this an authorization could name 14, and a future executor
    written against `authorization.versions` would apply an unprotected
    migration outside `apply_migrations()` -- with no ledger row from the
    ordinary path and none of section 12.1's accounting either.
    """
    with pytest.raises(ProtectedMigrationError):
        _authorization(versions={14})

    with pytest.raises(ProtectedMigrationError):
        _authorization(versions={_protected_version(), 14})


def test_an_authorization_must_name_its_operator() -> None:
    """The postflight artifact has to say on whose authority it ran.

    Carried, not verified -- this module cannot check that "lead" is a
    person -- but an empty string is a claim nobody made, and it would
    reach the artifact looking like one that had been recorded.
    """
    with pytest.raises(ProtectedMigrationError):
        ProtectedExecutionAuthorization(
            versions=frozenset({_protected_version()}),
            operator="   ",
            reason="slice 4 execution",
        )


def test_the_seam_resolves_the_authorized_file(tmp_path: Path) -> None:
    protected = _protected_version()
    root = _migration_root(tmp_path, (1, protected))
    snapshot = _snapshot(tmp_path / "comics.db", root)

    resolved = resolve_protected_execution(snapshot, _authorization())

    assert resolved == (root / f"{protected:03d}_synthetic.sql",)


def test_the_seam_refuses_an_authorization_that_is_not_pending(
    tmp_path: Path,
) -> None:
    """Stale or wrong: authorized for 015, but 015 is not pending.

    Covers today's repository directly -- 015 is declared and absent, so
    an authorization for it against the real root must refuse rather than
    resolve to nothing and read as success.
    """
    snapshot = _snapshot(tmp_path / "comics.db", REAL_MIGRATION_ROOT)

    with pytest.raises(ProtectedMigrationError):
        resolve_protected_execution(snapshot, _authorization())


def test_the_seam_refuses_when_a_pending_version_is_unauthorized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other direction: something is pending that nobody approved.

    A subset test passes the not-pending case above, so only this
    direction can prove the comparison is set *equality* -- and it was
    unproven until this test stopped skipping: replacing the equality with
    `authorization.versions <= pending` failed no test at all.

    Expressing it needs two pending protected versions, which today's
    declaration cannot supply. The *declaration* is therefore what gets
    patched, not the mechanism: `is_protected` reads the module global at
    call time, so both the pending-set computation and the
    authorization's own validation see the extended policy, and the code
    under test is the shipped code.
    """
    protected = _protected_version()
    second = max(PROTECTED_MIGRATIONS) + 1
    monkeypatch.setattr(
        protected_migrations,
        "PROTECTED_MIGRATIONS",
        frozenset(PROTECTED_MIGRATIONS | {second}),
    )

    root = _migration_root(tmp_path, (protected, second))
    snapshot = _snapshot(tmp_path / "comics.db", root)

    # Both are pending; the authorization names only one of them.
    assert snapshot.pending_protected() == [protected, second]

    with pytest.raises(ProtectedMigrationError):
        resolve_protected_execution(
            snapshot, _authorization(versions={protected})
        )


def test_the_seam_cannot_be_split_by_a_second_protected_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam's half of the fail-open defect found in review.

    It used to take a connection and a directory and scan twice: once to
    compute the pending set, once to map versions back to paths. A second
    protected file arriving between those scans let an authorization for
    ``{15}`` succeed while the real pending set was ``{15, 16}`` -- a
    migration nobody approved, waved through by a comparison made against
    a directory that no longer existed in that form.

    Now the seam takes a snapshot, so there is no second scan to split.
    Both halves are asserted: a snapshot that saw both files refuses, and
    a snapshot taken before the second file arrived still resolves --
    because the snapshot *is* the state being judged. Keeping the
    filesystem still while the executor runs is design section 8's
    quiescence obligation, not this function's, and pretending otherwise
    would be the same mistake in the opposite direction.
    """
    protected = _protected_version()
    second = max(PROTECTED_MIGRATIONS) + 1
    monkeypatch.setattr(
        protected_migrations,
        "PROTECTED_MIGRATIONS",
        frozenset(PROTECTED_MIGRATIONS | {second}),
    )

    database_path = tmp_path / "comics.db"
    root = _migration_root(tmp_path, (protected,))

    early = _snapshot(database_path, root)

    _write_migration(root, second, suffix="arrived_late")

    late = _snapshot(database_path, root)

    # The snapshot that saw both refuses the one-version authorization.
    assert late.pending_protected() == [protected, second]
    with pytest.raises(ProtectedMigrationError):
        resolve_protected_execution(
            late, _authorization(versions={protected})
        )

    # The snapshot taken before it arrived is internally consistent and
    # still resolves, against the state it actually read.
    assert early.pending_protected() == [protected]
    assert resolve_protected_execution(
        early, _authorization(versions={protected})
    ) == (root / f"{protected:03d}_synthetic.sql",)


def test_the_seam_applies_nothing(tmp_path: Path) -> None:
    """It authorizes. It does not execute.

    The property that keeps this slice's scope honest: the executor is a
    later slice, and until it exists resolving an authorization must leave
    the database exactly as it was -- no ledger row, no schema change, no
    open transaction handed back to the caller.
    """
    protected = _protected_version()
    database_path = tmp_path / "comics.db"

    ordinary_root = _migration_root(tmp_path / "a", (1, 2))
    with database_connection(database_path) as connection:
        apply_migrations(connection, ordinary_root)

    before = _schema_and_ledger(database_path)
    root = _migration_root(tmp_path / "b", (1, 2, protected))

    with database_connection(database_path) as connection:
        snapshot = take_migration_snapshot(connection, root)
        resolve_protected_execution(snapshot, _authorization())

        assert not connection.in_transaction

    assert _schema_and_ledger(database_path) == before


def test_the_seam_returns_paths_in_version_order(tmp_path: Path) -> None:
    """Ordering matters to the executor, so it is fixed here.

    With one declared protected version this asserts the shape rather than
    a comparison, which is worth having anyway: the tuple is what a future
    executor iterates, and an executor applying migrations out of order is
    a failure no test in this slice could otherwise catch.
    """
    protected = _protected_version()
    snapshot = _snapshot(
        tmp_path / "comics.db", _migration_root(tmp_path, (protected,))
    )

    resolved = resolve_protected_execution(snapshot, _authorization())
    versions = [migration_version(path) for path in resolved]

    assert versions == sorted(versions)
    assert versions == sorted(_authorization().versions)
