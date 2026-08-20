"""Migration 013 and the disposition writer.

The tests that matter most here are the bypass proofs. Every invariant this
PR claims is enforced by migration 013's constraints and triggers rather than
by `disposition.py`, and the only way to show that is to go around the writer
and issue the SQL directly. A guard that has only been exercised through the
function that respects it has not been demonstrated at all.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from comic_automation.archive import disposition
from comic_automation.archive.candidate_selection import (
    ARCHIVE_SUPERSEDED,
    REJECTION_REASONS,
    select_candidates,
    revalidate_for_enqueue,
)
from comic_automation.database.connection import connect_database
from comic_automation.database.migrations import (
    apply_migrations,
    discover_migrations,
    iter_sql_statements,
    migration_version,
)


MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)

INSERT_SUPERSESSION = (
    "INSERT INTO archive_supersessions "
    "(predecessor_archive_id, successor_archive_id, reason, evidence) "
    "VALUES (?, ?, ?, ?)"
)
INSERT_RETIREMENT = (
    "INSERT INTO archive_retirements (archive_id, reason, evidence) "
    "VALUES (?, ?, ?)"
)

# Whitespace that SQLite's one-argument trim() does NOT strip. Migration 012
# shipped with `length(trim(reason)) > 0`, which accepted every one of these
# as a reason; 013 reuses the corrected form and these prove it holds for
# evidence too.
BLANK_VARIANTS = [" ", "\t", "\n", "\r", " \t\n\r "]


# --- fixtures ------------------------------------------------------------


def seed_archives(connection: sqlite3.Connection, count: int = 8) -> list[int]:
    ids = []

    for index in range(1, count + 1):
        connection.execute(
            "INSERT INTO archive_files (id, file_size) VALUES (?, ?)",
            (index, 1024),
        )
        ids.append(index)

    return ids


@pytest.fixture()
def connection(tmp_path: Path):
    conn = connect_database(tmp_path / "supersession.db")
    apply_migrations(conn, MIGRATIONS)
    seed_archives(conn)

    try:
        yield conn
    finally:
        conn.close()


def apply_through(conn: sqlite3.Connection, limit: int) -> None:
    """Apply every migration up to and including `limit`.

    Deliberately mirrors apply_migrations() rather than calling it, because
    the point is to reach a database that is genuinely at version `limit`
    with no later object present.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
        "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )

    for path in discover_migrations(MIGRATIONS):
        version = migration_version(path)

        if version > limit:
            continue

        for statement in iter_sql_statements(
            path.read_text(encoding="utf-8-sig")
        ):
            conn.execute(statement)

        conn.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
            (version, path.name),
        )


# --- schema creation and upgrade -----------------------------------------


def test_fresh_schema_creates_every_013_object(tmp_path: Path) -> None:
    conn = connect_database(tmp_path / "fresh.db")
    applied = apply_migrations(conn, MIGRATIONS)

    assert 13 in applied

    names = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN "
            "('table', 'index', 'trigger')"
        )
    }

    assert {
        "archive_supersessions",
        "archive_disposition_events",
        "disposition_reversal_context",
        "idx_archive_supersessions_successor",
        "idx_archive_supersessions_superseded_at",
        "trg_supersession_no_cycle",
        "trg_supersession_predecessor_not_retired",
        "trg_supersession_successor_not_retired",
        "trg_supersession_immutable",
        "trg_supersession_reversal_needs_reason",
        "trg_supersession_recorded_history",
        "trg_supersession_reversed_history",
        "trg_retirement_requires_evidence",
        "trg_retirement_not_superseded_predecessor",
        "trg_retirement_not_live_successor",
        "trg_retirement_immutable",
        "trg_retirement_reversal_needs_reason",
        "trg_retirement_recorded_history",
        "trg_retirement_reversed_history",
    } <= names


def test_migrations_remain_idempotent_through_013(tmp_path: Path) -> None:
    conn = connect_database(tmp_path / "twice.db")
    apply_migrations(conn, MIGRATIONS)

    assert apply_migrations(conn, MIGRATIONS) == []


def test_upgrade_from_12_backfills_the_existing_retirement(
    tmp_path: Path,
) -> None:
    """The 45217 case: a retirement recorded before any history existed."""
    conn = connect_database(tmp_path / "upgrade.db")
    apply_through(conn, 12)
    seed_archives(conn, 2)

    conn.execute(
        "INSERT INTO archive_retirements "
        "(archive_id, retired_at, reason, evidence) VALUES (?, ?, ?, ?)",
        (1, "2026-08-19 04:55:30", "deduplicated", "signature held by 45213"),
    )

    assert apply_migrations(conn, MIGRATIONS) == [13]

    events = disposition.disposition_history(conn)

    assert len(events) == 1
    assert events[0]["archive_id"] == 1
    assert events[0]["disposition"] == "retired"
    assert events[0]["action"] == "recorded"
    # Reconstructed from the retirement row, never invented.
    assert events[0]["reason"] == "deduplicated"
    assert events[0]["evidence"] == "signature held by 45213"
    assert events[0]["occurred_at"] == "2026-08-19 04:55:30"
    # And marked, so it is never read as a contemporaneous record.
    assert events[0]["source"] == "migration_backfill"


def test_backfill_does_not_duplicate_on_re_application(
    tmp_path: Path,
) -> None:
    conn = connect_database(tmp_path / "reapply.db")
    apply_through(conn, 12)
    seed_archives(conn, 2)
    conn.execute(INSERT_RETIREMENT, (1, "reason", "evidence"))
    apply_migrations(conn, MIGRATIONS)

    conn.execute("DELETE FROM schema_migrations WHERE version = 13")
    apply_migrations(conn, MIGRATIONS)

    assert len(disposition.disposition_history(conn)) == 1


def test_upgrade_with_no_retirements_backfills_nothing(
    tmp_path: Path,
) -> None:
    conn = connect_database(tmp_path / "empty.db")
    apply_through(conn, 12)
    apply_migrations(conn, MIGRATIONS)

    assert disposition.disposition_history(conn) == []


# --- the happy shapes ----------------------------------------------------


def test_supersede_records_the_successor(connection) -> None:
    disposition.supersede(
        connection, 1, 2, reason="reclassified", evidence="sha256 abc"
    )

    found = disposition.dispositions_for(connection)

    assert found[1].is_superseded
    assert found[1].successor_archive_id == 2
    assert found[1].evidence == "sha256 abc"


def test_one_successor_may_absorb_several_predecessors(connection) -> None:
    """Fan-in is legal and required.

    A reclassification that folds several chapters into one re-discovered
    identity produces exactly this shape, so successor_archive_id carries an
    index rather than a UNIQUE constraint.
    """
    disposition.supersede(connection, 1, 3, reason="r", evidence="e")
    disposition.supersede(connection, 2, 3, reason="r", evidence="e")
    disposition.supersede(connection, 4, 3, reason="r", evidence="e")

    successors = disposition.successor_map(connection)

    assert successors == {1: 3, 2: 3, 4: 3}


def test_acyclic_chains_resolve_to_the_terminal_identity(connection) -> None:
    disposition.supersede(connection, 1, 2, reason="r", evidence="e")
    disposition.supersede(connection, 2, 3, reason="r", evidence="e")
    disposition.supersede(connection, 3, 4, reason="r", evidence="e")

    assert disposition.resolve_successor(connection, 1) == 4
    assert disposition.resolve_successor(connection, 3) == 4
    # An archive that was never superseded resolves to itself.
    assert disposition.resolve_successor(connection, 5) == 5


# --- bypass proofs: cycles ----------------------------------------------
#
# Each of these issues the INSERT directly against the connection, going
# around disposition.supersede() entirely. The rejection therefore comes from
# migration 013, which is the claim being tested.


def test_raw_sql_self_link_is_rejected(connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(INSERT_SUPERSESSION, (1, 1, "r", "e"))


def test_raw_sql_two_node_cycle_is_rejected(connection) -> None:
    connection.execute(INSERT_SUPERSESSION, (1, 2, "r", "e"))

    with pytest.raises(sqlite3.IntegrityError, match="cycle"):
        connection.execute(INSERT_SUPERSESSION, (2, 1, "r", "e"))


def test_raw_sql_long_cycle_is_rejected(connection) -> None:
    connection.execute(INSERT_SUPERSESSION, (1, 2, "r", "e"))
    connection.execute(INSERT_SUPERSESSION, (2, 3, "r", "e"))
    connection.execute(INSERT_SUPERSESSION, (3, 4, "r", "e"))
    connection.execute(INSERT_SUPERSESSION, (4, 5, "r", "e"))

    with pytest.raises(sqlite3.IntegrityError, match="cycle"):
        connection.execute(INSERT_SUPERSESSION, (5, 1, "r", "e"))


def test_a_cycle_cannot_be_closed_through_a_fan_in_node(connection) -> None:
    connection.execute(INSERT_SUPERSESSION, (1, 3, "r", "e"))
    connection.execute(INSERT_SUPERSESSION, (2, 3, "r", "e"))
    connection.execute(INSERT_SUPERSESSION, (3, 4, "r", "e"))

    with pytest.raises(sqlite3.IntegrityError, match="cycle"):
        connection.execute(INSERT_SUPERSESSION, (4, 1, "r", "e"))


def test_a_predecessor_may_hold_only_one_successor(connection) -> None:
    connection.execute(INSERT_SUPERSESSION, (1, 2, "r", "e"))

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(INSERT_SUPERSESSION, (1, 3, "r", "e"))


# --- bypass proofs: retirement conflicts, both orders --------------------


def test_cannot_supersede_a_retired_predecessor(connection) -> None:
    connection.execute(INSERT_RETIREMENT, (1, "r", "e"))

    with pytest.raises(sqlite3.IntegrityError, match="retired archive"):
        connection.execute(INSERT_SUPERSESSION, (1, 2, "r", "e"))


def test_cannot_supersede_into_a_retired_successor(connection) -> None:
    connection.execute(INSERT_RETIREMENT, (2, "r", "e"))

    with pytest.raises(sqlite3.IntegrityError, match="retired successor"):
        connection.execute(INSERT_SUPERSESSION, (1, 2, "r", "e"))


def test_cannot_retire_a_superseded_predecessor(connection) -> None:
    connection.execute(INSERT_SUPERSESSION, (1, 2, "r", "e"))

    with pytest.raises(sqlite3.IntegrityError, match="is superseded"):
        connection.execute(INSERT_RETIREMENT, (1, "r", "e"))


def test_cannot_retire_a_successor_with_live_predecessors(connection) -> None:
    connection.execute(INSERT_SUPERSESSION, (1, 2, "r", "e"))

    with pytest.raises(sqlite3.IntegrityError, match="superseded into"):
        connection.execute(INSERT_RETIREMENT, (2, "r", "e"))


def test_retiring_a_former_successor_is_allowed_after_reversal(
    connection,
) -> None:
    """The conflict is with a *live* supersession, not a historical one."""
    connection.execute(INSERT_SUPERSESSION, (1, 2, "r", "e"))
    disposition.reverse_supersession(connection, 1, reason="wrong successor")

    disposition.retire(connection, 2, reason="r", evidence="e")

    assert disposition.dispositions_for(connection)[2].is_retired


# --- bypass proofs: immutability ----------------------------------------


def test_a_supersession_row_cannot_be_updated(connection) -> None:
    connection.execute(INSERT_SUPERSESSION, (1, 2, "r", "e"))

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "UPDATE archive_supersessions SET successor_archive_id = 3 "
            "WHERE predecessor_archive_id = 1"
        )


def test_an_update_cannot_bypass_the_cycle_check(connection) -> None:
    """The reason immutability exists: UPDATE fires no INSERT trigger."""
    connection.execute(INSERT_SUPERSESSION, (1, 2, "r", "e"))
    connection.execute(INSERT_SUPERSESSION, (2, 3, "r", "e"))

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "UPDATE archive_supersessions SET successor_archive_id = 1 "
            "WHERE predecessor_archive_id = 2"
        )

    assert disposition.successor_map(connection) == {1: 2, 2: 3}


def test_a_retirement_row_cannot_be_updated(connection) -> None:
    connection.execute(INSERT_RETIREMENT, (1, "r", "e"))

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "UPDATE archive_retirements SET reason = 'x' WHERE archive_id = 1"
        )


# --- bypass proofs: reason and evidence ---------------------------------


@pytest.mark.parametrize("blank", BLANK_VARIANTS)
def test_whitespace_only_supersession_reason_is_refused(
    connection, blank: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(INSERT_SUPERSESSION, (1, 2, blank, "e"))


@pytest.mark.parametrize("blank", BLANK_VARIANTS)
def test_whitespace_only_supersession_evidence_is_refused(
    connection, blank: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(INSERT_SUPERSESSION, (1, 2, "r", blank))


def test_null_supersession_evidence_is_refused(connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO archive_supersessions "
            "(predecessor_archive_id, successor_archive_id, reason) "
            "VALUES (1, 2, 'r')"
        )


@pytest.mark.parametrize("blank", BLANK_VARIANTS)
def test_whitespace_only_retirement_evidence_is_refused(
    connection, blank: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="evidence"):
        connection.execute(INSERT_RETIREMENT, (1, "r", blank))


def test_null_retirement_evidence_is_refused(connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="evidence"):
        connection.execute(
            "INSERT INTO archive_retirements (archive_id, reason) "
            "VALUES (1, 'r')"
        )


@pytest.mark.parametrize("blank", BLANK_VARIANTS)
def test_the_writer_refuses_blank_input_before_the_database(
    connection, blank: str
) -> None:
    with pytest.raises(disposition.DispositionError):
        disposition.supersede(connection, 1, 2, reason=blank, evidence="e")

    with pytest.raises(disposition.DispositionError):
        disposition.supersede(connection, 1, 2, reason="r", evidence=blank)


# --- ON DELETE RESTRICT -------------------------------------------------


def test_a_predecessor_cannot_be_deleted_while_superseded(connection) -> None:
    connection.execute(INSERT_SUPERSESSION, (1, 2, "r", "e"))

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM archive_files WHERE id = 1")


def test_a_successor_cannot_be_deleted_while_referenced(connection) -> None:
    connection.execute(INSERT_SUPERSESSION, (1, 2, "r", "e"))

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM archive_files WHERE id = 2")


def test_history_outlives_the_archive_it_describes(connection) -> None:
    """Why the FKs are RESTRICT and the history table has none at all."""
    connection.execute(INSERT_SUPERSESSION, (1, 2, "r", "e"))
    disposition.reverse_supersession(connection, 1, reason="undo")
    connection.execute("DELETE FROM archive_files WHERE id = 1")

    history = disposition.disposition_history(connection, 1)

    assert [row["action"] for row in history] == ["recorded", "reversed"]


# --- reversal -----------------------------------------------------------


def test_reversal_requires_a_reason_at_the_database_level(
    connection,
) -> None:
    connection.execute(INSERT_SUPERSESSION, (1, 2, "r", "e"))

    with pytest.raises(sqlite3.IntegrityError, match="reversal reason"):
        connection.execute(
            "DELETE FROM archive_supersessions "
            "WHERE predecessor_archive_id = 1"
        )


def test_a_context_for_another_archive_does_not_authorise_this_delete(
    connection,
) -> None:
    """A stale reason must not silently label the next reversal."""
    connection.execute(INSERT_SUPERSESSION, (1, 2, "r", "e"))
    connection.execute(INSERT_SUPERSESSION, (3, 4, "r", "e"))
    connection.execute(
        "INSERT INTO disposition_reversal_context "
        "(id, archive_id, disposition, reason) VALUES (1, 3, 'superseded', 'x')"
    )

    with pytest.raises(sqlite3.IntegrityError, match="reversal reason"):
        connection.execute(
            "DELETE FROM archive_supersessions "
            "WHERE predecessor_archive_id = 1"
        )


def test_a_retirement_context_does_not_authorise_a_supersession_delete(
    connection,
) -> None:
    connection.execute(INSERT_SUPERSESSION, (1, 2, "r", "e"))
    connection.execute(
        "INSERT INTO disposition_reversal_context "
        "(id, archive_id, disposition, reason) VALUES (1, 1, 'retired', 'x')"
    )

    with pytest.raises(sqlite3.IntegrityError, match="reversal reason"):
        connection.execute(
            "DELETE FROM archive_supersessions "
            "WHERE predecessor_archive_id = 1"
        )


@pytest.mark.parametrize("blank", BLANK_VARIANTS)
def test_a_blank_reversal_reason_is_refused(connection, blank: str) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO disposition_reversal_context "
            "(id, archive_id, disposition, reason) "
            "VALUES (1, 1, 'superseded', ?)",
            (blank,),
        )


def test_reversal_clears_its_context(connection) -> None:
    connection.execute(INSERT_SUPERSESSION, (1, 2, "r", "e"))
    disposition.reverse_supersession(connection, 1, reason="undo")

    remaining = connection.execute(
        "SELECT COUNT(*) FROM disposition_reversal_context"
    ).fetchone()[0]

    assert remaining == 0


def test_a_failed_reversal_still_clears_its_context(connection) -> None:
    with pytest.raises(disposition.DispositionError):
        disposition.reverse_supersession(connection, 1, reason="undo")

    remaining = connection.execute(
        "SELECT COUNT(*) FROM disposition_reversal_context"
    ).fetchone()[0]

    assert remaining == 0


def test_reversing_a_retirement_records_its_own_reason(connection) -> None:
    disposition.retire(connection, 1, reason="out of scope", evidence="proof")
    disposition.reverse_retirement(connection, 1, reason="operator error")

    history = disposition.disposition_history(connection, 1)

    assert [row["action"] for row in history] == ["recorded", "reversed"]
    assert history[0]["reason"] == "out of scope"
    # The reversal carries its own reason, not the one it undoes.
    assert history[1]["reason"] == "operator error"
    assert disposition.dispositions_for(connection) == {}


# --- history ------------------------------------------------------------


def test_recording_a_disposition_writes_exactly_one_event(
    connection,
) -> None:
    disposition.supersede(connection, 1, 2, reason="r", evidence="e")
    disposition.retire(connection, 3, reason="r", evidence="e")

    assert len(disposition.disposition_history(connection, 1)) == 1
    assert len(disposition.disposition_history(connection, 3)) == 1


def test_the_event_names_the_successor(connection) -> None:
    disposition.supersede(connection, 1, 2, reason="r", evidence="sha256 abc")

    event = disposition.disposition_history(connection, 1)[0]

    assert event["counterpart_archive_id"] == 2
    assert event["evidence"] == "sha256 abc"
    assert event["source"] == "application"


def test_history_is_atomic_with_the_disposition(connection) -> None:
    """A rolled-back disposition leaves no event behind."""
    connection.execute("BEGIN")
    disposition.supersede(connection, 1, 2, reason="r", evidence="e")
    connection.execute("ROLLBACK")

    assert disposition.dispositions_for(connection) == {}
    assert disposition.disposition_history(connection) == []


def test_raw_sql_writes_are_recorded_too(connection) -> None:
    """History is a trigger, so it cannot be skipped by not using the writer."""
    connection.execute(INSERT_SUPERSESSION, (1, 2, "r", "e"))

    assert len(disposition.disposition_history(connection, 1)) == 1


# --- chain resolution ---------------------------------------------------


def test_resolve_successor_reports_a_bypassed_cycle_rather_than_looping(
    connection,
) -> None:
    """Proof for the reader, since the writer and the trigger both refuse.

    The trigger is dropped first, which is what a database restored from
    before migration 013 looks like: the rows are there and the guard is not.
    """
    connection.execute("DROP TRIGGER trg_supersession_no_cycle")
    connection.execute(INSERT_SUPERSESSION, (1, 2, "r", "e"))
    connection.execute(INSERT_SUPERSESSION, (2, 1, "r", "e"))

    with pytest.raises(disposition.SupersessionChainError):
        disposition.resolve_successor(connection, 1)


def test_conflicting_dispositions_are_detectable_after_a_bypass(
    connection,
) -> None:
    connection.execute("DROP TRIGGER trg_retirement_not_superseded_predecessor")
    connection.execute(INSERT_SUPERSESSION, (1, 2, "r", "e"))
    connection.execute(INSERT_RETIREMENT, (1, "r", "e"))

    assert disposition.conflicting_dispositions(connection) == [1]


# --- selection ----------------------------------------------------------


def test_superseded_is_a_stable_rejection_reason() -> None:
    assert ARCHIVE_SUPERSEDED in REJECTION_REASONS


def test_a_superseded_archive_is_refused_without_touching_the_disk(
    connection, tmp_path: Path, monkeypatch
) -> None:
    """The refusal must not depend on the filesystem, so it must not stat.

    `candidate_selection._stat` exists as an explicit seam for exactly this;
    replacing it here proves the superseded archive is refused before the one
    call that touches the disk.
    """
    archive = tmp_path / "present.cbz"
    archive.write_bytes(b"data")
    connection.execute(
        "INSERT INTO file_locations (archive_id, path, is_current) "
        "VALUES (1, ?, 1)",
        (str(archive),),
    )
    disposition.supersede(connection, 1, 2, reason="r", evidence="e")

    calls: list[str] = []

    def explode(path):
        calls.append(path)
        raise AssertionError("select_candidates must not stat a superseded "
                             "archive")

    monkeypatch.setattr(
        "comic_automation.archive.candidate_selection._stat", explode
    )

    selection = select_candidates(connection, [1])

    assert calls == []
    assert selection.accepted == []
    assert [r.reason for r in selection.rejected] == [ARCHIVE_SUPERSEDED]


def test_supersession_survives_the_file_coming_back(
    connection, tmp_path: Path
) -> None:
    archive = tmp_path / "restored.cbz"
    archive.write_bytes(b"data")
    connection.execute(
        "INSERT INTO file_locations (archive_id, path, is_current) "
        "VALUES (1, ?, 1)",
        (str(archive),),
    )
    disposition.supersede(connection, 1, 2, reason="r", evidence="e")

    selection = select_candidates(connection, [1])

    assert selection.accepted_ids == []


def test_an_archive_superseded_after_selection_is_refused_at_enqueue(
    connection, tmp_path: Path
) -> None:
    archive = tmp_path / "late.cbz"
    archive.write_bytes(b"data")
    connection.execute(
        "INSERT INTO file_locations (archive_id, path, is_current) "
        "VALUES (1, ?, 1)",
        (str(archive),),
    )

    assert select_candidates(connection, [1]).accepted_ids == [1]

    disposition.supersede(connection, 1, 2, reason="r", evidence="e")
    rejection = revalidate_for_enqueue(connection, 1)

    assert rejection is not None
    assert rejection.reason == ARCHIVE_SUPERSEDED
    assert "superseded by archive 2" in rejection.detail


def test_every_rejection_reason_is_grouped_even_when_empty(
    connection,
) -> None:
    grouped = select_candidates(connection, []).rejections_by_reason()

    assert ARCHIVE_SUPERSEDED in grouped
    assert grouped[ARCHIVE_SUPERSEDED] == []
