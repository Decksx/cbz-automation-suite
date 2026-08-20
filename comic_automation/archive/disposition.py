"""The only application path that writes an archive disposition.

A *disposition* is a recorded decision about an archive identity:

``retired``
    This archive is out of scope for automated work. It names no
    successor.
``superseded``
    The work continues under another archive identity, named here.

Everything else this project knows about an archive -- where its file is,
whether that file matches what was recorded, whether its pages are
inventoried, what its jobs did, whether selection would enqueue it -- is an
*observation*. Observations are measured whenever they are needed and stored
nowhere.

The distinction is the whole point of this module, and it was learned the
expensive way. On 2026-07-28 a discovery scan swept 271 locations as missing
in twenty-nine seconds, flipping ``file_locations.is_current`` to 0. That was
a truthful observation about the disk. For the next three weeks 162 of those
archives were read as though the flip had *meant* something, and they became
the entire explanation for 5,342 unhashed pages that no report could see. A
scan that observes absence has said nothing about scope.

So there is no code path from an observation to a disposition table. Nothing
in the audits, the scans, the workers, the repair tools or the classifier
imports this module's writers. Writing a disposition requires an operator to
call one of these functions with a reason and evidence, and the database
refuses anything blank.

What this module does NOT enforce
---------------------------------

Almost nothing. Cycle prevention, the four retirement conflicts, immutability,
non-blank reason and evidence, and "a reversal needs its own reason" are all
enforced by migration 013's constraints and triggers, which fire for raw SQL
exactly as they do for this module. The functions here exist to make the
correct call *convenient and atomic*, not to be the guard. Their tests
deliberately bypass them and write SQL directly to prove the database still
refuses.

The one thing this module owns is the reversal-reason handshake: a ``DELETE``
trigger cannot be handed an argument, so a reversal writes its reason into
``disposition_reversal_context`` first and then deletes the row. That runs
inside a ``SAVEPOINT``, because ``connect_database`` sets
``isolation_level=None`` and the statements would otherwise autocommit one by
one, leaving a usable context row on disk if anything interrupted the sequence.
The context is consumed by migration 013's own trigger, so a raw-SQL reversal
cannot leave one behind either.

History is not written here either. Migration 013's triggers append to
``archive_disposition_events`` in the same statement that changes the
disposition, which makes the audit record atomic by construction and produces
exactly one event per action. Application code that also wrote an event would
produce two.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Sequence


RETIRED = "retired"
SUPERSEDED = "superseded"

DISPOSITIONS = (RETIRED, SUPERSEDED)

# The SAVEPOINT name used to make a reversal atomic. A savepoint rather than
# BEGIN/COMMIT because `connect_database` sets isolation_level=None: without
# one, the three statements of the reversal handshake would autocommit
# separately when no caller-supplied transaction is open, and an interruption
# between them could leave a usable context row behind. SAVEPOINT works
# identically in autocommit and inside an existing transaction, which is what
# lets this be correct for both callers.
_REVERSAL_SAVEPOINT = "disposition_reversal"


class DispositionError(RuntimeError):
    """A disposition could not be recorded or reversed."""


class SupersessionChainError(DispositionError):
    """A successor chain revisits a node, so it does not terminate.

    Migration 013's cycle trigger makes this unreachable through any path
    that respects the schema. It is still detected rather than assumed away,
    because a database restored from before 013 has the rows and none of the
    triggers.
    """


class ConflictingDispositionError(DispositionError):
    """One archive holds both a retirement and a supersession.

    Migration 013 forbids this in both insertion orders. Reaching it means the
    constraint was bypassed, and the two records disagree about what was
    decided -- which is not something a reader can be handed silently.
    """


@dataclass(frozen=True)
class Disposition:
    """The recorded disposition of one archive, if it has one."""

    archive_id: int
    disposition: str
    reason: str
    evidence: str | None
    recorded_at: str
    successor_archive_id: int | None = None

    @property
    def is_retired(self) -> bool:
        return self.disposition == RETIRED

    @property
    def is_superseded(self) -> bool:
        return self.disposition == SUPERSEDED


def _require_text(value: str, field: str) -> str:
    """Reject blank input before it reaches the database.

    The database CHECK is the guard; this exists so a caller gets a clear
    Python-level error naming the field instead of an IntegrityError quoting
    a trim() expression. Both use the same definition of blank: whitespace of
    any kind, not merely spaces.
    """
    if value is None or not str(value).strip():
        raise DispositionError(f"{field} must be non-blank")

    return str(value)


@contextmanager
def _reversal_reason(
    connection: sqlite3.Connection,
    *,
    archive_id: int,
    disposition: str,
    reason: str,
) -> Iterator[None]:
    """Publish a reversal reason for the DELETE trigger, atomically.

    The context row names the archive and the disposition it authorises, and
    migration 013's BEFORE DELETE trigger refuses any deletion it does not
    match. That is what stops a reason left behind by one reversal from
    silently labelling the next.

    The whole handshake runs inside a SAVEPOINT, which is what makes the
    atomicity claim true rather than aspirational. `connect_database` sets
    `isolation_level=None`, so without one the context insert, the delete and
    the cleanup would each autocommit separately whenever no caller-supplied
    transaction was open -- and an interruption between them would leave a
    usable context row committed on disk. A SAVEPOINT behaves the same way in
    autocommit mode (where it opens a transaction and RELEASE commits it) and
    inside an existing transaction (where it nests), so both callers get the
    same guarantee.

    On success the deletion's AFTER DELETE trigger has already consumed the
    context row; the explicit cleanup here covers the case where the deletion
    matched nothing, so no trigger fired and there is nothing to consume it.
    """
    connection.execute(f"SAVEPOINT {_REVERSAL_SAVEPOINT}")

    try:
        connection.execute("DELETE FROM disposition_reversal_context")
        connection.execute(
            """
            INSERT INTO disposition_reversal_context
                (id, archive_id, disposition, reason)
            VALUES (1, ?, ?, ?)
            """,
            (int(archive_id), disposition, reason),
        )

        yield

        connection.execute("DELETE FROM disposition_reversal_context")
    except BaseException:
        # ROLLBACK TO leaves the savepoint on the stack, so it still has to be
        # released afterwards or every later savepoint nests inside a dead one.
        connection.execute(f"ROLLBACK TO SAVEPOINT {_REVERSAL_SAVEPOINT}")
        connection.execute(f"RELEASE SAVEPOINT {_REVERSAL_SAVEPOINT}")
        raise

    connection.execute(f"RELEASE SAVEPOINT {_REVERSAL_SAVEPOINT}")


def retire(
    connection: sqlite3.Connection,
    archive_id: int,
    *,
    reason: str,
    evidence: str,
) -> None:
    """Record that *archive_id* is out of scope for automated work.

    Refused by the database if the archive is already retired, is a superseded
    predecessor, or is the successor of a live supersession -- retiring a
    successor would point live work at an identity declared out of scope.
    """
    reason = _require_text(reason, "reason")
    evidence = _require_text(evidence, "evidence")

    connection.execute(
        """
        INSERT INTO archive_retirements (archive_id, reason, evidence)
        VALUES (?, ?, ?)
        """,
        (int(archive_id), reason, evidence),
    )


def supersede(
    connection: sqlite3.Connection,
    predecessor_archive_id: int,
    successor_archive_id: int,
    *,
    reason: str,
    evidence: str,
) -> None:
    """Record that one archive identity continues as another.

    `evidence` is mandatory because supersession is a claim that specific
    bytes live somewhere else; the digest or path that proves it is what makes
    the record reviewable later.

    Several predecessors may share one successor -- that is what a
    reclassification folding a series into one re-discovered identity looks
    like. A predecessor may have only one successor, and chains are legal
    while cycles are not; both are enforced by migration 013.
    """
    reason = _require_text(reason, "reason")
    evidence = _require_text(evidence, "evidence")

    connection.execute(
        """
        INSERT INTO archive_supersessions (
            predecessor_archive_id, successor_archive_id, reason, evidence
        )
        VALUES (?, ?, ?, ?)
        """,
        (int(predecessor_archive_id), int(successor_archive_id),
         reason, evidence),
    )


def reverse_retirement(
    connection: sqlite3.Connection,
    archive_id: int,
    *,
    reason: str,
) -> None:
    """Un-retire *archive_id*, recording why.

    A reversal is a decision in its own right and carries its own reason
    rather than inheriting the one it undoes.
    """
    reason = _require_text(reason, "reason")

    with _reversal_reason(
        connection,
        archive_id=int(archive_id),
        disposition=RETIRED,
        reason=reason,
    ):
        cursor = connection.execute(
            "DELETE FROM archive_retirements WHERE archive_id = ?",
            (int(archive_id),),
        )

        if cursor.rowcount == 0:
            raise DispositionError(
                f"Archive {archive_id} is not retired; nothing to reverse."
            )


def reverse_supersession(
    connection: sqlite3.Connection,
    predecessor_archive_id: int,
    *,
    reason: str,
) -> None:
    """Un-supersede *predecessor_archive_id*, recording why."""
    reason = _require_text(reason, "reason")

    with _reversal_reason(
        connection,
        archive_id=int(predecessor_archive_id),
        disposition=SUPERSEDED,
        reason=reason,
    ):
        cursor = connection.execute(
            "DELETE FROM archive_supersessions "
            "WHERE predecessor_archive_id = ?",
            (int(predecessor_archive_id),),
        )

        if cursor.rowcount == 0:
            raise DispositionError(
                f"Archive {predecessor_archive_id} is not superseded; "
                "nothing to reverse."
            )


# --------------------------------------------------------------- read side
#
# Everything below is read-only and safe on a connection opened with
# `mode=ro` plus `PRAGMA query_only = ON`.


def retired_archive_ids(connection: sqlite3.Connection) -> set[int]:
    """Every durably retired archive."""
    return {
        int(row[0])
        for row in connection.execute(
            "SELECT archive_id FROM archive_retirements"
        )
    }


def superseded_archive_ids(connection: sqlite3.Connection) -> set[int]:
    """Every archive that has been superseded by another identity."""
    return {
        int(row[0])
        for row in connection.execute(
            "SELECT predecessor_archive_id FROM archive_supersessions"
        )
    }


def successor_map(connection: sqlite3.Connection) -> dict[int, int]:
    """predecessor -> immediate successor, for the whole table."""
    return {
        int(predecessor): int(successor)
        for predecessor, successor in connection.execute(
            "SELECT predecessor_archive_id, successor_archive_id "
            "FROM archive_supersessions"
        )
    }


def resolve_successor(
    connection: sqlite3.Connection,
    archive_id: int,
    *,
    successors: dict[int, int] | None = None,
) -> int:
    """Follow the supersession chain to its terminal identity.

    Returns *archive_id* itself when it has not been superseded. Pass
    `successors` to resolve many archives without re-reading the table.

    Raises `SupersessionChainError` rather than looping if the chain revisits
    a node, which can only happen if migration 013's cycle trigger was
    bypassed.

    The walk is unbounded on purpose. `successors` is a finite mapping and
    `seen` guarantees termination, so any hop limit would only be a second,
    weaker termination condition -- one that cannot tell a cycle from a long
    but perfectly valid chain, and would start rejecting real data at whatever
    length was guessed.
    """
    if successors is None:
        successors = successor_map(connection)

    current = int(archive_id)
    seen = {current}

    while True:
        nxt = successors.get(current)

        if nxt is None:
            return current

        if nxt in seen:
            raise SupersessionChainError(
                f"Supersession chain from archive {archive_id} revisits "
                f"archive {nxt}; the cycle constraint has been bypassed."
            )

        seen.add(nxt)
        current = nxt


def dispositions_for(
    connection: sqlite3.Connection,
    archive_ids: Sequence[int] | None = None,
) -> dict[int, Disposition]:
    """Recorded dispositions, keyed by archive id.

    Archives with no disposition are absent from the result rather than
    present with a null value: "active" is the absence of a decision, not a
    decision to be active. Pass `archive_ids` to scope the read; omit it for
    the whole library, which is what the classifier wants.
    """
    wanted = None if archive_ids is None else {int(a) for a in archive_ids}
    found: dict[int, Disposition] = {}

    for row in connection.execute(
        "SELECT archive_id, retired_at, reason, evidence "
        "FROM archive_retirements"
    ):
        archive_id = int(row[0])

        if wanted is None or archive_id in wanted:
            found[archive_id] = Disposition(
                archive_id=archive_id,
                disposition=RETIRED,
                reason=str(row[2]),
                evidence=row[3],
                recorded_at=str(row[1]),
            )

    for row in connection.execute(
        "SELECT predecessor_archive_id, successor_archive_id, superseded_at, "
        "reason, evidence FROM archive_supersessions"
    ):
        archive_id = int(row[0])

        if wanted is None or archive_id in wanted:
            # Migration 013 forbids an archive holding both dispositions, so
            # reaching this means the constraint was bypassed. Overwriting the
            # retirement recorded above would hand the caller one of two
            # contradictory decisions and no way to tell that the other exists,
            # so this refuses instead. `conflicting_dispositions()` is the
            # read that reports the same condition without raising, for a
            # census that needs to survive it.
            if archive_id in found:
                raise ConflictingDispositionError(
                    f"Archive {archive_id} is both retired and superseded; "
                    "migration 013's constraints have been bypassed."
                )

            found[archive_id] = Disposition(
                archive_id=archive_id,
                disposition=SUPERSEDED,
                reason=str(row[3]),
                evidence=row[4],
                recorded_at=str(row[2]),
                successor_archive_id=int(row[1]),
            )

    return found


def disposition_history(
    connection: sqlite3.Connection,
    archive_id: int | None = None,
) -> list[dict]:
    """The append-only record of every disposition action, oldest first."""
    if archive_id is None:
        rows = connection.execute(
            "SELECT * FROM archive_disposition_events ORDER BY id"
        )
    else:
        rows = connection.execute(
            "SELECT * FROM archive_disposition_events "
            "WHERE archive_id = ? ORDER BY id",
            (int(archive_id),),
        )

    return [dict(row) for row in rows]


def conflicting_dispositions(connection: sqlite3.Connection) -> list[int]:
    """Archives holding both a retirement and a supersession.

    Migration 013 makes this impossible through any path that respects the
    schema. It is read back anyway, because a constraint that is never
    verified from the outside is a constraint nobody notices losing -- a
    database restored from before 013 has the rows and none of the triggers.
    """
    return sorted(
        int(row[0])
        for row in connection.execute(
            """
            SELECT r.archive_id
            FROM archive_retirements AS r
            JOIN archive_supersessions AS s
              ON s.predecessor_archive_id = r.archive_id
            """
        )
    )
