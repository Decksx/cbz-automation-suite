"""R15 ledger admission, before a protected executor exists (issue #102).

All SQL files and databases are synthetic tmp_path inputs. No test applies
even the synthetic 015 file: these are predicate/seam contracts, not proof of
backup, quiescence, execution, ledger insertion or postflight correctness.
"""

from contextlib import closing
import sqlite3

import pytest

from comic_automation.database import protected_migrations as protected
from comic_automation.database.migrations import ensure_migration_table


EARLIER = tuple(range(1, 15))
FILES = tuple(range(1, 16))


@pytest.fixture
def migration_state(tmp_path):
    connections = []

    def make(*, discovered=FILES, recorded=EARLIER):
        directory = tmp_path / f"case-{len(connections)}"
        directory.mkdir()
        for version in discovered:
            (directory / f"{version:03d}_fixture.sql").write_text(
                f"CREATE TABLE must_never_run_{version} (id INTEGER);\n", encoding="utf-8")
        connection = sqlite3.connect(directory / "ledger.db")
        connections.append(connection)
        ensure_migration_table(connection)
        connection.executemany(
            "INSERT INTO schema_migrations(version,name) VALUES (?,?)",
            [(version, f"{version:03d}_fixture.sql") for version in recorded],
        )
        connection.commit()
        return connection, directory

    yield make
    for connection in connections:
        connection.close()


def _authorization():
    return protected.ProtectedExecutionAuthorization(
        versions=frozenset({15}), operator="fixture operator", reason="fixture only")


def _state(connection, directory):
    return (tuple(connection.iterdump()), connection.total_changes, connection.in_transaction,
            {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()})


def test_exact_predecessor_ledger_passes_without_applying_or_recording_015(migration_state):
    connection, directory = migration_state()
    snapshot = protected.take_migration_snapshot(connection, directory)
    before = _state(connection, directory)
    assert protected.require_slice4_ledger(snapshot) is None
    assert protected.resolve_protected_execution(snapshot, _authorization()) == (
        directory / "015_fixture.sql",
    )
    assert _state(connection, directory) == before
    assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [
        ("schema_migrations",),
    ]


@pytest.mark.parametrize("recorded,missing,unexpected", [
    ((14,), list(range(1, 14)), []),
    (tuple(range(1, 14)), [14], []),
    (tuple(v for v in EARLIER if v != 5), [5], []),
    ((), list(EARLIER), []),
    (EARLIER + (15,), [], [15]),
    (EARLIER + (16,), [], [16]),
    (EARLIER + (99,), [], [99]),
    (EARLIER + (0,), [], [0]),
    ((14, 16), list(range(1, 14)), [16]),
])
def test_ledger_disagreement_refuses_and_preserves_inputs(
        migration_state, recorded, missing, unexpected):
    connection, directory = migration_state(recorded=recorded)
    snapshot = protected.take_migration_snapshot(connection, directory)
    before = _state(connection, directory)
    with pytest.raises(protected.ProtectedMigrationError) as exc:
        protected.require_slice4_ledger(snapshot)
    assert f"missing={missing}" in str(exc.value)
    assert f"unexpected={unexpected}" in str(exc.value)
    assert _state(connection, directory) == before


@pytest.mark.parametrize("discovered", [FILES, FILES + (16,)])
def test_pending_set_alone_accepts_the_r15_counterexample_but_ledger_guard_refuses(
        migration_state, discovered):
    """016 is invalid whether its file is unknown or already discovered."""
    connection, directory = migration_state(discovered=discovered, recorded=EARLIER + (16,))
    snapshot = protected.take_migration_snapshot(connection, directory)
    # This generic seam deliberately does not claim the ledger predicate.
    assert protected.resolve_protected_execution(snapshot, _authorization()) == (
        directory / "015_fixture.sql",
    )
    with pytest.raises(protected.ProtectedMigrationError, match=r"unexpected=\[16\]"):
        protected.require_slice4_ledger(snapshot)


def test_recorded_file_removed_from_discovery_is_not_silently_ignored(migration_state):
    connection, directory = migration_state()
    (directory / "004_fixture.sql").unlink()
    snapshot = protected.take_migration_snapshot(connection, directory)
    with pytest.raises(protected.ProtectedMigrationError, match=r"unexpected=\[4\]"):
        protected.require_slice4_ledger(snapshot)


def test_later_unprotected_file_remains_pending_and_is_not_selected(migration_state):
    connection, directory = migration_state(discovered=FILES + (16,))
    snapshot = protected.take_migration_snapshot(connection, directory)
    protected.require_slice4_ledger(snapshot)
    assert snapshot.pending() == [15, 16]
    assert protected.resolve_protected_execution(snapshot, _authorization()) == (
        directory / "015_fixture.sql",
    )


def test_ledger_check_does_not_replace_the_explicit_protected_set_guard(migration_state, monkeypatch):
    connection, directory = migration_state(discovered=FILES + (16,))
    monkeypatch.setattr(protected, "PROTECTED_MIGRATIONS", frozenset({15, 16}))
    snapshot = protected.take_migration_snapshot(connection, directory)
    protected.require_slice4_ledger(snapshot)
    with pytest.raises(protected.ProtectedMigrationError, match="pending protected"):
        protected.resolve_protected_execution(snapshot, _authorization())


def test_passing_ledger_does_not_even_prove_015_exists(migration_state):
    connection, directory = migration_state(discovered=EARLIER)
    snapshot = protected.take_migration_snapshot(connection, directory)
    protected.require_slice4_ledger(snapshot)
    with pytest.raises(protected.ProtectedMigrationError, match="pending protected"):
        protected.resolve_protected_execution(snapshot, _authorization())


def test_comparison_uses_discovered_predecessors_not_a_synthesized_range(migration_state):
    connection, directory = migration_state(discovered=(3, 7, 15), recorded=(3, 7))
    # The predicate matches section 12.1's equation. Trust in the migration
    # checkout/complete release file set is a separate executor prerequisite.
    protected.require_slice4_ledger(protected.take_migration_snapshot(connection, directory))


def test_predicate_uses_the_supplied_snapshot_without_refreshing_it(migration_state, monkeypatch):
    connection, directory = migration_state()
    snapshot = protected.take_migration_snapshot(connection, directory)
    connection.execute("INSERT INTO schema_migrations(version,name) VALUES (16,'016_later.sql')")
    connection.commit()
    current = protected.take_migration_snapshot(connection, directory)

    def forbidden(*args, **kwargs):
        pytest.fail("predicate tried to re-read instead of using its supplied snapshot")

    monkeypatch.setattr(protected, "recorded_versions", forbidden)
    monkeypatch.setattr(protected, "discover_migrations", forbidden)
    protected.require_slice4_ledger(snapshot)  # deliberately stale; no freshness claim
    with pytest.raises(protected.ProtectedMigrationError, match=r"unexpected=\[16\]"):
        protected.require_slice4_ledger(current)


def test_a_missing_ledger_is_not_created_by_the_refusal(tmp_path):
    directory = tmp_path / "migrations"
    directory.mkdir()
    for version in FILES:
        (directory / f"{version:03d}_fixture.sql").write_text("-- never applied\n", encoding="utf-8")
    with closing(sqlite3.connect(tmp_path / "empty.db")) as connection:
        snapshot = protected.take_migration_snapshot(connection, directory)
        before = _state(connection, directory)
        with pytest.raises(protected.ProtectedMigrationError, match="missing="):
            protected.require_slice4_ledger(snapshot)
        assert _state(connection, directory) == before
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
