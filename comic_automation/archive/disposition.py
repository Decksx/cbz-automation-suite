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
``disposition_reversal_context`` first, deletes the row, and clears the
context -- all inside one transaction, so a caller cannot leave a stale reason
behind for the next reversal to borrow.

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

# Bound on how far `resolve_successor` will walk before declaring the chain
# unusable. Migration 013's cycle trigger makes a true cycle unreachable
# through any path that respects the schema, so hitting this bound means the
# constraint was bypassed (a database restored from before 013, or a table
# rewritten out from under it). Walking forever would turn that into a hang;
# raising turns it into a report.
MAX_CHAIN_DEPTH = 64


class DispositionError(RuntimeError):
    """A disposition could not be recorded or reversed."""


class SupersessionChainError(DispositionError):
    """A successor chain does not terminate within MAX_CHAIN_DEPTH."""


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
    """Publish a reversal reason for the DELETE trigger, then clear it.

    The context row names the archive and the disposition it authorises, and
    migration 013's BEFORE DELETE trigger refuses any deletion it does not
    match. That is what stops a reason left behind by one reversal from
    silently labelling the next.

    Cleared in a `finally` so a failed delete cannot leave the row in place.
    The caller is expected to hold a transaction; if the transaction rolls
    back, the context row goes with it either way.
    """
    connection.execute("DELETE FROM disposition_reversal_context")
    connection.execute(
        """
        INSERT INTO disposition_reversal_context
            (id, archive_id, disposition, reason)
        VALUES (1, ?, ?, ?)
        """,
        (int(archive_id), disposition, reason),
    )

    try:
        yield
    finally:
        connection.execute("DELETE FROM disposition_reversal_context")


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

    Raises `SupersessionChainError` rather than looping if the chain does not
    terminate, which can only happen if migration 013's cycle trigger was
    bypassed.
    """
    if successors is None:
        successors = successor_map(connection)

    current = int(archive_id)
    seen = {current}

    for _ in range(MAX_CHAIN_DEPTH):
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

    raise SupersessionChainError(
        f"Supersession chain from archive {archive_id} did not terminate "
        f"within {MAX_CHAIN_DEPTH} hops."
    )


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
            # this cannot overwrite a retirement recorded above. If it ever
            # does, the constraint was bypassed and the classifier's invariant
            # check is what reports it -- silently preferring one here would
            # hide exactly that.
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
