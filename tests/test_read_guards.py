"""Tests for the shared WAL-aware read guards.

The headline test here is
`test_wal_commit_is_detected_although_the_file_fingerprint_is_identical`:
it is the whole reason this module exists. Everything else guards the
supporting properties the audits rely on (the reads really do run in a
transaction, the connection really is read-only, the integrity check
really does run *inside* the change-detection window).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from comic_automation.database.connection import (
    connect_database,
    database_connection,
)
from comic_automation.database.read_guards import (
    SHM_SUFFIX,
    WAL_SUFFIX,
    ConsistentSnapshot,
    DatabaseChangedError,
    DatabaseFingerprint,
    DatabaseIntegrityError,
    DatabaseMutatedError,
    ReadGuardError,
    data_version,
    fingerprint_database,
    fingerprint_database_files,
    fingerprint_report_fields,
    quick_check,
    read_consistent_snapshot,
    readonly_database_connection,
)


def build_database(database: Path, *, rows: int = 400) -> None:
    """A small WAL-mode database with one table, big enough (several
    SQLite pages) that the last page can be clobbered to fail
    `quick_check` while the file still opens.
    """
    with database_connection(database) as connection:
        connection.execute(
            "CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)"
        )
        connection.executemany(
            "INSERT INTO widgets (name) VALUES (?)",
            [(f"widget-{index:04d}" + "x" * 64,) for index in range(rows)],
        )


def count_widgets(connection: sqlite3.Connection) -> int:
    return int(
        connection.execute("SELECT COUNT(*) FROM widgets").fetchone()[0]
    )


# --- the core WAL hazard -------------------------------------------------


def test_wal_commit_can_leave_the_main_file_fingerprint_unchanged(
    tmp_path: Path,
) -> None:
    """Documents the hazard the data_version guard exists to cover.

    In WAL mode a committed write is appended to the ``-wal`` sidecar;
    the main database file is only rewritten later, at checkpoint. So
    the main file's size and mtime can be byte-for-byte identical
    across another connection's commit.
    """
    database = tmp_path / "guard.db"
    build_database(database)

    before = fingerprint_database(database)

    # The writer is deliberately left open across the second stat:
    # closing it would checkpoint the WAL back into the main file and
    # change the fingerprint after the fact. The hazard is about what
    # is observable *at the moment of the commit*.
    writer = connect_database(database)
    try:
        assert (
            writer.execute("PRAGMA journal_mode").fetchone()[0].lower()
            == "wal"
        )
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO widgets (name) VALUES ('late')")
        writer.execute("COMMIT")

        after = fingerprint_database(database)
        assert (database.parent / "guard.db-wal").is_file()
    finally:
        writer.close()

    assert before == after


def test_wal_commit_is_detected_although_the_file_fingerprint_is_identical(
    tmp_path: Path,
) -> None:
    """The regression this whole module exists for.

    A second connection commits *between* two of the reads inside one
    guarded snapshot. The commit must be rejected via `data_version`,
    and the main file's size and mtime must be provably identical
    across it -- so a fingerprint-only guard demonstrably could not
    have caught it.
    """
    database = tmp_path / "guard.db"
    build_database(database)

    observed: dict[str, object] = {}

    def read(connection: sqlite3.Connection) -> tuple[int, int]:
        first = count_widgets(connection)

        observed["before"] = fingerprint_database(database)

        # A *different* connection commits mid-read. database_connection
        # opens in WAL mode, so this can land entirely in the -wal file.
        with database_connection(database) as other:
            other.execute("INSERT INTO widgets (name) VALUES ('injected')")

        observed["after"] = fingerprint_database(database)

        return first, count_widgets(connection)

    with pytest.raises(DatabaseChangedError) as raised:
        read_consistent_snapshot(database, read)

    # Specifically the data_version guard, not the weaker fingerprint
    # detector: DatabaseMutatedError is a *subclass*, so the exact type
    # is what distinguishes which one fired.
    assert type(raised.value) is DatabaseChangedError
    assert "data_version" in str(raised.value)

    # The commit really did happen...
    with database_connection(database) as connection:
        assert count_widgets(connection) == 401

    # ...and this is *why* data_version is required: at the moment of
    # the commit the main file's size and mtime were unchanged, so no
    # fingerprint comparison could have raised.
    assert observed["before"] == observed["after"]


def test_reads_inside_one_snapshot_do_not_see_the_external_commit(
    tmp_path: Path,
) -> None:
    """The snapshot is genuinely consistent, not merely change-detected.

    Both reads bracket an external commit and must still agree -- that
    is the property the audits' partition/reconciliation invariants
    depend on. The run is then rejected anyway, by data_version.
    """
    database = tmp_path / "guard.db"
    build_database(database)

    counts: list[int] = []

    def read(connection: sqlite3.Connection) -> None:
        counts.append(count_widgets(connection))

        with database_connection(database) as other:
            other.execute("INSERT INTO widgets (name) VALUES ('injected')")

        counts.append(count_widgets(connection))

    with pytest.raises(DatabaseChangedError):
        read_consistent_snapshot(database, read)

    assert counts == [400, 400]


# --- snapshot mechanics --------------------------------------------------


def test_returns_result_and_snapshot_metadata(tmp_path: Path) -> None:
    database = tmp_path / "guard.db"
    build_database(database)

    snapshot = read_consistent_snapshot(database, count_widgets)

    assert isinstance(snapshot, ConsistentSnapshot)
    assert snapshot.result == 400
    assert snapshot.quick_check == "ok"
    assert snapshot.data_version_before == snapshot.data_version_after
    assert snapshot.data_version_unchanged is True
    assert snapshot.database == database.resolve()


def test_report_fields_expose_the_boundary(tmp_path: Path) -> None:
    database = tmp_path / "guard.db"
    build_database(database)

    fields = read_consistent_snapshot(database, count_widgets).report_fields()

    assert fields["quick_check"] == "ok"
    assert fields["data_version_before"] == fields["data_version_after"]
    assert fields["concurrent_commit_detected"] is False
    assert "data_version" in fields["snapshot_guarantee"]


def test_reads_run_inside_a_transaction(tmp_path: Path) -> None:
    """Without the explicit BEGIN there is no shared snapshot at all."""
    database = tmp_path / "guard.db"
    build_database(database)

    seen: dict[str, object] = {}

    def read(connection: sqlite3.Connection) -> None:
        seen["in_transaction"] = connection.in_transaction
        seen["isolation_level"] = connection.isolation_level

    read_consistent_snapshot(database, read)

    assert seen["in_transaction"] is True
    # isolation_level=None is required, not cosmetic: with pysqlite's
    # default the driver's implicit transaction handling fights the
    # explicit BEGIN/END. One of the five copies this module replaced
    # had drifted and omitted it.
    assert seen["isolation_level"] is None


def test_transaction_is_closed_even_when_the_read_raises(
    tmp_path: Path,
) -> None:
    database = tmp_path / "guard.db"
    build_database(database)

    class Boom(RuntimeError):
        pass

    def read(connection: sqlite3.Connection) -> None:
        raise Boom("read failed")

    with pytest.raises(Boom):
        read_consistent_snapshot(database, read)

    # The database is still fully usable afterwards: no lock was left
    # behind by an unterminated read transaction.
    with database_connection(database) as connection:
        assert count_widgets(connection) == 400


def test_integrity_check_runs_inside_the_change_detection_window(
    tmp_path: Path,
) -> None:
    """A WAL commit landing during quick_check must still be caught.

    This is the exact gap that made `data_version_before` have to be
    sampled *before* the integrity check rather than after it.
    """
    database = tmp_path / "guard.db"
    build_database(database)

    observed: dict[str, object] = {}

    def integrity_check(connection: sqlite3.Connection) -> str:
        result = quick_check(connection)

        observed["before"] = fingerprint_database(database)

        with database_connection(database) as other:
            other.execute("INSERT INTO widgets (name) VALUES ('injected')")

        observed["after"] = fingerprint_database(database)

        return result

    with pytest.raises(DatabaseChangedError):
        read_consistent_snapshot(
            database,
            count_widgets,
            integrity_check=integrity_check,
        )

    assert observed["before"] == observed["after"]


def test_context_appears_in_the_error_message(tmp_path: Path) -> None:
    database = tmp_path / "guard.db"
    build_database(database)

    def read(connection: sqlite3.Connection) -> None:
        with database_connection(database) as other:
            other.execute("INSERT INTO widgets (name) VALUES ('injected')")

    with pytest.raises(DatabaseChangedError) as raised:
        read_consistent_snapshot(database, read, context="preflight")

    assert "preflight" in str(raised.value)


# --- integrity -----------------------------------------------------------


def test_quick_check_failure_raises_integrity_error(tmp_path: Path) -> None:
    database = tmp_path / "guard.db"
    build_database(database)

    # Clobber the final page. The schema stays readable, so the
    # database still opens and the guard gets far enough to run
    # quick_check -- which is the point: the integrity guard, not
    # sqlite3's own open-time errors, is what must fire.
    page_size = 4096
    data = bytearray(database.read_bytes())
    assert len(data) > page_size * 2
    data[-page_size:] = bytes([0x5A]) * page_size
    database.write_bytes(bytes(data))

    with pytest.raises(DatabaseIntegrityError) as raised:
        read_consistent_snapshot(database, count_widgets)

    assert "quick_check" in str(raised.value)


def test_reads_never_run_when_the_integrity_check_fails(
    tmp_path: Path,
) -> None:
    database = tmp_path / "guard.db"
    build_database(database)

    calls: list[int] = []

    def read(connection: sqlite3.Connection) -> None:
        calls.append(1)

    with pytest.raises(DatabaseIntegrityError):
        read_consistent_snapshot(
            database,
            read,
            integrity_check=lambda connection: "*** in database main ***",
        )

    assert calls == []


def test_quick_check_survives_a_raising_pragma(tmp_path: Path) -> None:
    """A database corrupt enough to make the pragma itself raise must
    be reported as an integrity failure, not crash the caller."""

    class Exploding:
        def execute(self, statement: str):
            raise sqlite3.DatabaseError("database disk image is malformed")

    assert quick_check(Exploding()).startswith("error: ")


# --- read-only guarantees ------------------------------------------------


def test_readonly_connection_rejects_writes(tmp_path: Path) -> None:
    database = tmp_path / "guard.db"
    build_database(database)

    with readonly_database_connection(database) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM widgets")


def test_missing_database_is_never_created(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.db"

    with pytest.raises(FileNotFoundError):
        read_consistent_snapshot(missing, count_widgets)

    with pytest.raises(FileNotFoundError):
        with readonly_database_connection(missing):
            pass

    assert not missing.exists()


def test_guarded_read_leaves_the_main_file_byte_identical(
    tmp_path: Path,
) -> None:
    database = tmp_path / "guard.db"
    build_database(database)

    before = fingerprint_database(database)
    before_bytes = database.read_bytes()

    read_consistent_snapshot(database, count_widgets)

    assert fingerprint_database(database) == before
    assert database.read_bytes() == before_bytes


# --- fingerprints are diagnostics ---------------------------------------


def test_sidecar_fingerprints_report_absence_without_raising(
    tmp_path: Path,
) -> None:
    database = tmp_path / "guard.db"
    build_database(database)

    # database_connection() checkpoints and removes the sidecars when
    # the last writer closes, so a freshly built database has none.
    files = fingerprint_database_files(database)

    assert isinstance(files.main, DatabaseFingerprint)
    assert files.wal.suffix == WAL_SUFFIX
    assert files.shm.suffix == SHM_SUFFIX
    assert files.wal.present is False
    assert files.wal.size_bytes is None
    assert files.wal.modified_time_ns is None


def test_a_read_only_run_itself_touches_the_sidecars(tmp_path: Path) -> None:
    """Why sidecar readings can never be a gate, and are not reported raw.

    Merely opening a WAL database read-only creates both sidecars --
    during a run that provably wrote nothing to the database itself
    (their mtimes can move on later reads too). Any guard built on them
    would fire constantly, and any report carrying their raw values
    would stop being reproducible.
    """
    database = tmp_path / "guard.db"
    build_database(database)

    before = fingerprint_database_files(database)
    read_consistent_snapshot(database, count_widgets)
    after = fingerprint_database_files(database)

    # The main file did not move -- the guarded read wrote nothing...
    assert before.main == after.main
    assert fingerprint_database(database) == before.main
    # ...yet both sidecars came into existence, so the sidecar
    # fingerprint changed during a provably read-only run.
    assert before.wal.present is False
    assert before.shm.present is False
    assert after.wal.present is True
    assert after.shm.present is True


def test_fingerprint_report_fields_never_overstate_the_guarantee(
    tmp_path: Path,
) -> None:
    database = tmp_path / "guard.db"
    build_database(database)

    fingerprint = fingerprint_database(database)
    files_before = fingerprint_database_files(database)
    read_consistent_snapshot(database, count_widgets)
    files_after = fingerprint_database_files(database)

    fields = fingerprint_report_fields(
        fingerprint_before=fingerprint,
        fingerprint_after=fingerprint,
        files_before=files_before,
        files_after=files_after,
    )

    # The historical key keeps its name and value...
    assert fields["database_unchanged"] is True
    assert fields["database_file_unchanged"] is True
    # ...but can no longer be read as a concurrency guarantee.
    assert fields["database_unchanged_is_diagnostic_only"] is True
    assert "data_version" in fields["fingerprint_diagnostic_note"]
    assert "WAL" in fields["fingerprint_diagnostic_note"]

    # Sidecar evidence is presence, not raw readings.
    assert fields["database_wal_sidecar_observed"] is True
    assert fields["database_shm_sidecar_observed"] is True
    assert "sidecar_diagnostic_note" in fields
    assert not any(
        key.startswith("database_files_") for key in fields
    )


def test_report_fields_are_deterministic_across_runs(tmp_path: Path) -> None:
    """Two guarded runs over unchanged data produce identical
    provenance -- the property that forbids raw sidecar timestamps."""
    database = tmp_path / "guard.db"
    build_database(database)

    def provenance() -> dict:
        fingerprint_before = fingerprint_database(database)
        files_before = fingerprint_database_files(database)
        snapshot = read_consistent_snapshot(database, count_widgets)
        fingerprint_after = fingerprint_database(database)
        files_after = fingerprint_database_files(database)

        fields = fingerprint_report_fields(
            fingerprint_before=fingerprint_before,
            fingerprint_after=fingerprint_after,
            files_before=files_before,
            files_after=files_after,
        )
        fields.update(snapshot.report_fields())
        return fields

    # The first run is allowed to differ: it is the one that creates
    # the sidecars. Every run after that must be identical.
    provenance()
    assert provenance() == provenance()


# --- error taxonomy ------------------------------------------------------


def test_error_hierarchy_lets_callers_catch_both_detectors() -> None:
    assert issubclass(DatabaseChangedError, ReadGuardError)
    assert issubclass(DatabaseIntegrityError, ReadGuardError)
    assert issubclass(ReadGuardError, RuntimeError)
    # The weaker file-fingerprint detector is a subclass of the
    # authoritative one, so `except DatabaseChangedError` gets both.
    assert issubclass(DatabaseMutatedError, DatabaseChangedError)


def test_data_version_is_frozen_inside_a_read_transaction(
    tmp_path: Path,
) -> None:
    """Why data_version must be sampled *outside* the transaction.

    Inside an open read transaction the counter does not move, so
    sampling it there would detect nothing at all.
    """
    database = tmp_path / "guard.db"
    build_database(database)

    with readonly_database_connection(database) as connection:
        connection.execute("BEGIN")
        try:
            connection.execute("SELECT COUNT(*) FROM widgets").fetchone()
            inside_before = data_version(connection)

            with database_connection(database) as other:
                other.execute("INSERT INTO widgets (name) VALUES ('x')")

            inside_after = data_version(connection)
        finally:
            connection.execute("END")

        outside_after = data_version(connection)

    assert inside_before == inside_after
    assert outside_after != inside_before


# --- the connection comes from the DAL (Step 5) ---------------------------
#
# `readonly_database_connection` used to call `sqlite3.connect` itself and
# now opens through `dal.open_connection(readonly=True)`. These pin the
# connection it hands back to the state the private copy produced --
# measured on that copy at master `f957bde` before it was replaced -- so the
# migration is shown to be behaviour-preserving rather than assumed to be.


def test_readonly_connection_state_matches_the_pre_dal_factory(
    tmp_path: Path,
) -> None:
    """Every connection property an audit can observe, pinned at once.

    Values measured on the private factory before migration: query_only
    1, busy_timeout 30000 (installed by ``timeout=30.0``, not by a
    pragma), isolation_level None, sqlite3.Row rows, no open transaction.
    The read-only path must *not* touch foreign_keys, journal_mode or
    synchronous: a read-only audit that changed connection settings the
    writable factory owns would be reading under a different regime than
    the one it reports on.
    """
    database = tmp_path / "guard.db"
    build_database(database)

    with readonly_database_connection(database) as connection:
        def pragma(name: str):
            return connection.execute(f"PRAGMA {name}").fetchone()[0]

        assert pragma("query_only") == 1
        assert pragma("busy_timeout") == 30000
        assert pragma("foreign_keys") == 0
        assert pragma("journal_mode") == "wal"
        assert pragma("synchronous") == 2  # FULL: SQLite's default, untouched
        assert connection.isolation_level is None
        assert connection.row_factory is sqlite3.Row
        assert connection.in_transaction is False

        row = connection.execute("SELECT id, name FROM widgets").fetchone()
        assert row["id"] == 1


def test_readonly_connection_refuses_writes_with_query_only_turned_off(
    tmp_path: Path,
) -> None:
    """`mode=ro` is the layer that survives a pragma being switched off.

    `query_only` is per-connection and any code holding the connection can
    clear it. If the DAL path ever dropped the URI flag, this write would
    succeed and the main-file bytes would change.
    """
    database = tmp_path / "guard.db"
    build_database(database)
    before = database.read_bytes()

    with readonly_database_connection(database) as connection:
        connection.execute("PRAGMA query_only = OFF")

        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM widgets")

    assert database.read_bytes() == before


def test_a_directory_is_refused_before_sqlite_is_touched(
    tmp_path: Path,
) -> None:
    """Same refusal, same message, as the private factory gave."""
    directory = tmp_path / "not-a-database"
    directory.mkdir()

    with pytest.raises(FileNotFoundError, match="Database does not exist"):
        with readonly_database_connection(directory):
            pass

    assert list(directory.iterdir()) == []


def test_readonly_connection_is_closed_on_exit_and_on_error(
    tmp_path: Path,
) -> None:
    """The close is the one thing this wrapper still owns itself."""
    database = tmp_path / "guard.db"
    build_database(database)

    with readonly_database_connection(database) as connection:
        pass

    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")

    with pytest.raises(RuntimeError, match="boom"):
        with readonly_database_connection(database) as failing:
            raise RuntimeError("boom")

    with pytest.raises(sqlite3.ProgrammingError):
        failing.execute("SELECT 1")


def test_readonly_connection_opens_through_the_dal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The factory is the DAL's, requested read-only.

    A spy rather than a source grep: it proves the call happens with
    ``readonly=True`` on the path every audit takes, not merely that the
    name appears in the file.
    """
    from comic_automation.database import dal, read_guards

    calls: list[tuple[object, dict]] = []
    real_open = dal.open_connection

    def spy(database_path, **kwargs):
        calls.append((database_path, kwargs))
        return real_open(database_path, **kwargs)

    monkeypatch.setattr(read_guards, "open_connection", spy)

    database = tmp_path / "guard.db"
    build_database(database)

    read_consistent_snapshot(database, count_widgets)

    assert len(calls) == 1
    assert calls[0][1] == {"readonly": True}


def test_read_guards_is_no_longer_a_sanctioned_connect_site() -> None:
    """A raw connect returning to read_guards.py must trip the guard.

    The allowlist entry was removed with the migration. If someone
    re-adds both a connect call and an entry, this is the test that says
    the migration was undone.
    """
    from comic_automation.database import dal

    repo_root = Path(__file__).resolve().parent.parent

    assert (
        "comic_automation/database/read_guards.py"
        not in dal.SANCTIONED_CONNECT_SITES
    )
    assert (
        "comic_automation/database/read_guards.py"
        not in dal.find_connect_sites(repo_root)
    )
