"""Persisted file_events watermarks on discovery batches (issue #158, B2).

A batch's details carry start_event_id (the highest event id before any of its
discovery work) and, once completed, end_event_id. Event ids, not timestamps,
bound "what changed": codex-lead showed a SQL clock rollback hiding a new event
and same-second batches re-selecting an older one. These tests pin the bounds,
their survival through fail/resume/complete, the refusal to invent a bound for
legacy batches, and that a fault between writes leaves no usable batch.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.library import scan_library
from comic_automation.library.repository import LibraryRepository

MIGRATION_DIRECTORY = Path(__file__).resolve().parents[1] / "comic_automation" / "database" / "migrations"


def create_archive(path: Path, content: bytes = b"test archive") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def details(connection, batch_id: int) -> dict:
    row = connection.execute("SELECT status, details_json FROM source_batches WHERE id = ?", (batch_id,)).fetchone()
    return {"status": row["status"], **json.loads(row["details_json"])}


def event_ids(connection) -> list[int]:
    return [r[0] for r in connection.execute("SELECT id FROM file_events ORDER BY id")]


@pytest.fixture
def catalog(tmp_path):
    with database_connection(tmp_path / "catalog.db") as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        yield connection, tmp_path / "library"


def test_bounds_bracket_exactly_the_events_each_scan_recorded(catalog):
    connection, library = catalog
    create_archive(library / "Alpha" / "Alpha 01.cbz")
    first = scan_library(connection, library)
    after_first = event_ids(connection)
    create_archive(library / "Beta" / "Beta 01.cbz")
    second = scan_library(connection, library)
    after_second = event_ids(connection)

    one, two = details(connection, first.batch_id), details(connection, second.batch_id)
    assert one["status"] == two["status"] == "completed"
    assert one["start_event_id"] == 0 and one["end_event_id"] == max(after_first)
    assert two["start_event_id"] == one["end_event_id"]
    assert two["end_event_id"] == max(after_second)
    new_in_second = [i for i in after_second if i not in after_first]
    assert new_in_second and all(two["start_event_id"] < i <= two["end_event_id"] for i in new_in_second)


def test_a_back_dated_event_is_still_inside_its_batch_bounds(catalog):
    """Clock independence: an event recorded during the scan but stamped
    earlier than the batch start (a clock rollback) is still bounded by id."""
    connection, library = catalog
    create_archive(library / "Alpha" / "Alpha 01.cbz")
    summary = scan_library(connection, library)
    connection.execute("UPDATE file_events SET occurred_at = '2000-01-01 00:00:00'")
    bounds = details(connection, summary.batch_id)
    stamped = connection.execute("SELECT discovered_at FROM source_batches WHERE id = ?", (summary.batch_id,)).fetchone()[0]
    rows = connection.execute("SELECT id, occurred_at FROM file_events").fetchall()
    assert all(r["occurred_at"] < stamped for r in rows)  # a timestamp query would miss them all
    assert all(bounds["start_event_id"] < r["id"] <= bounds["end_event_id"] for r in rows)


def test_fail_resume_complete_keeps_the_original_start_marker(catalog):
    connection, library = catalog
    create_archive(library / "Alpha" / "Alpha 01.cbz")
    scan_library(connection, library)
    repository = LibraryRepository(connection)
    original = repository.start_batch(library)
    start = details(connection, original)["start_event_id"]

    repository.fail_batch(original, "simulated crash")
    assert details(connection, original)["status"] == "failed"
    assert details(connection, original)["start_event_id"] == start

    create_archive(library / "Beta" / "Beta 01.cbz")
    resumed = scan_library(connection, library, resume=True)
    assert resumed.batch_id == original and resumed.resumed is True
    final = details(connection, original)
    assert final["status"] == "completed" and final["resumed"] is True
    assert final["start_event_id"] == start
    assert final["end_event_id"] == max(event_ids(connection))


def test_a_legacy_batch_completes_without_an_invented_start_marker(catalog):
    connection, library = catalog
    library.mkdir(parents=True)
    cursor = connection.execute(
        "INSERT INTO source_batches (source_path, status, details_json) VALUES (?, 'running', ?)",
        (str(library), json.dumps({"mode": "read-only-discovery"})))
    LibraryRepository(connection).complete_batch(int(cursor.lastrowid), {"scanned": 0})
    legacy = details(connection, int(cursor.lastrowid))
    assert legacy["status"] == "completed"
    assert legacy["start_event_id"] is None and legacy["end_event_id"] == 0


def test_a_fault_while_starting_leaves_no_batch_behind(catalog):
    connection, library = catalog
    library.mkdir(parents=True)
    connection.execute("""CREATE TRIGGER refuse_checkpoint BEFORE INSERT ON discovery_checkpoints
                          BEGIN SELECT RAISE(ABORT, 'injected'); END""")
    with pytest.raises(sqlite3.DatabaseError):
        LibraryRepository(connection).start_batch(library)
    assert connection.execute("SELECT COUNT(*) FROM source_batches").fetchone()[0] == 0
    assert not connection.in_transaction


def test_a_fault_while_completing_leaves_the_batch_unusable(catalog):
    connection, library = catalog
    library.mkdir(parents=True)
    repository = LibraryRepository(connection)
    batch = repository.start_batch(library)
    connection.execute("""CREATE TRIGGER refuse_completion BEFORE UPDATE ON source_batches
                          WHEN NEW.status = 'completed' BEGIN SELECT RAISE(ABORT, 'injected'); END""")
    with pytest.raises(sqlite3.DatabaseError):
        repository.complete_batch(batch, {"scanned": 0})
    state = details(connection, batch)
    assert state["status"] == "running" and "end_event_id" not in state
    assert not connection.in_transaction


def _deny_one_commit(connection):
    """A real SQLite authorizer refusing the next COMMIT (ROLLBACK allowed)."""
    state = {"denied": False}

    def authorizer(action, arg1, arg2, database, trigger):
        if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT" and not state["denied"]:
            state["denied"] = True
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorizer)
    return state


@pytest.mark.parametrize("step", ["start", "complete"])
def test_a_rejected_commit_rolls_back_and_releases_the_lock(catalog, step):
    """PR #189 review R1: a refused COMMIT must not leave the owned write
    transaction open with a reported-failed mutation still pending."""
    connection, library = catalog
    library.mkdir(parents=True)
    repository = LibraryRepository(connection)
    batch = repository.start_batch(library) if step == "complete" else None
    state = _deny_one_commit(connection)
    with pytest.raises(sqlite3.DatabaseError):
        if step == "start":
            repository.start_batch(library)
        else:
            repository.complete_batch(batch, {"scanned": 0})
    connection.set_authorizer(None)
    assert state["denied"] and not connection.in_transaction
    rows = connection.execute("SELECT id, status FROM source_batches ORDER BY id").fetchall()
    if step == "start":
        assert rows == []
    else:
        assert [(r["id"], r["status"]) for r in rows] == [(batch, "running")]
        assert "end_event_id" not in details(connection, batch)


def test_a_failed_cleanup_rollback_keeps_the_original_error(catalog):
    """If COMMIT is refused and the cleanup ROLLBACK is refused too, the error
    raised is still the original COMMIT failure, not a ROLLBACK error chained
    on top of it. The transaction is then left for the connection owner."""
    connection, library = catalog
    library.mkdir(parents=True)
    denied = []

    def authorizer(action, arg1, arg2, database, trigger):
        if action == sqlite3.SQLITE_TRANSACTION and arg1 in ("COMMIT", "ROLLBACK") and arg1 not in denied:
            denied.append(arg1)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorizer)
    with pytest.raises(sqlite3.DatabaseError) as raised:
        LibraryRepository(connection).start_batch(library)
    connection.set_authorizer(None)
    assert denied == ["COMMIT", "ROLLBACK"]
    assert raised.value.__context__ is None  # the COMMIT failure itself, not a cleanup error
    connection.execute("ROLLBACK")


def test_sqlite_automatic_rollback_keeps_the_original_error(catalog):
    """PR #189 review R1: when SQLite itself ends the transaction, cleanup must
    not replace the real failure with 'no transaction is active'."""
    connection, library = catalog
    library.mkdir(parents=True)
    connection.execute("""CREATE TRIGGER rollback_checkpoint BEFORE INSERT ON discovery_checkpoints
                          BEGIN SELECT RAISE(ROLLBACK, 'checkpoint rollback witness'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="checkpoint rollback witness"):
        LibraryRepository(connection).start_batch(library)
    assert not connection.in_transaction
    assert connection.execute("SELECT COUNT(*) FROM source_batches").fetchone()[0] == 0


def test_bookkeeping_joins_a_transaction_the_caller_holds(catalog):
    connection, library = catalog
    library.mkdir(parents=True)
    repository = LibraryRepository(connection)
    connection.execute("BEGIN IMMEDIATE")
    batch = repository.start_batch(library)
    repository.complete_batch(batch, {"scanned": 0})
    assert connection.in_transaction  # the caller still owns it
    connection.execute("ROLLBACK")
    assert connection.execute("SELECT COUNT(*) FROM source_batches").fetchone()[0] == 0
