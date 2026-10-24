"""The database half of the step protocol: `rename_attempt_steps` (§5.4 step 4).

A file journal and a SQLite transaction cannot be one transaction, so the
database carries its own durable marker. For every filesystem step, one
SQLite transaction

* conditionally updates the ``file_locations`` row, matched on its stable
  id, its archive id **and** its preimage path, required to affect exactly
  one row;
* conditionally updates every pairing record registered for the action
  (§5.6) the same way; and
* inserts the ``rename_attempt_steps`` marker keyed by
  ``(attempt_id, action_id, step)`` with the preimage and postimage paths.

Any update that does not affect exactly one row raises `StepRefused`, which
rolls the whole transaction back through `dal.transaction`, so the marker
lands only together with every path update.

Why the table is not an ordinary numbered migration
---------------------------------------------------

Migration 015 is declared protected (`protected_migrations`), the Slice 4
executor requires the ledger to equal exactly ``{v < 15}`` before it applies
015 (`docs/slice4_migration_design.md` §12.1 step 3), and
`tests/test_migrations.py` requires contiguous version numbers. A ``016``
would therefore either fail the contiguity sentinel or, once 015 exists,
make every database that ran it unable ever to apply 015. So the marker
table is installed explicitly by `install_schema` (``CREATE TABLE IF NOT
EXISTS`` in one ``BEGIN IMMEDIATE``), outside the migration ledger, and
`require_schema` refuses any apply, recovery or undo against a database
where it is missing or differs. Folding it into the ledger is left to the
reviewer once the Slice 4 numbering is settled (step 7 note 1).

Reads go through `read_guards.read_consistent_snapshot` (``mode=ro``,
``query_only``, ``data_version`` bracketing). Writes use a writable DAL
connection whose policy differs from `dal.WRITABLE_POLICY` in one pragma,
``synchronous = FULL``: under WAL, ``NORMAL`` may lose the most recent
commit on power loss, and the journal's ``confirmed`` record is written
right after that commit.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from comic_automation.database import dal
from comic_automation.database.read_guards import read_consistent_snapshot

MARKER_TABLE = "rename_attempt_steps"

MARKER_SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS rename_attempt_steps (
    attempt_id TEXT NOT NULL CHECK (length(attempt_id) > 0),
    action_id TEXT NOT NULL CHECK (length(action_id) > 0),
    step INTEGER NOT NULL CHECK (step >= 1),
    kind TEXT NOT NULL CHECK (kind IN ('rename', 'temp', 'quarantine')),
    location_id INTEGER NOT NULL,
    preimage_path TEXT NOT NULL,
    postimage_path TEXT NOT NULL CHECK (postimage_path <> preimage_path),
    pairings_json TEXT NOT NULL DEFAULT '[]',
    committed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (attempt_id, action_id, step)
)
""".strip(),
    """
CREATE TRIGGER IF NOT EXISTS rename_attempt_steps_no_update
BEFORE UPDATE ON rename_attempt_steps
BEGIN
    SELECT RAISE(ABORT, 'rename_attempt_steps is append-only');
END
""".strip(),
    """
CREATE TRIGGER IF NOT EXISTS rename_attempt_steps_no_delete
BEFORE DELETE ON rename_attempt_steps
BEGIN
    SELECT RAISE(ABORT, 'rename_attempt_steps is append-only');
END
""".strip(),
)

# The apply connection: `dal.WRITABLE_POLICY` with synchronous FULL (module
# docstring). A caller-side policy, the precedent being the reuse analysis's
# longer busy timeout.
DURABLE_WRITABLE_POLICY = dal.ConnectionPolicy(
    name="canonical_apply_durable",
    readonly=False,
    timeout_seconds=30.0,
    pragmas=(
        ("foreign_keys", "ON"),
        ("journal_mode", "WAL"),
        ("synchronous", "FULL"),
        ("busy_timeout", "30000"),
    ),
)

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class MarkerError(RuntimeError):
    """Base class for marker-table refusals."""


class SchemaMissingError(MarkerError):
    """``rename_attempt_steps`` is absent or not the expected shape."""


class StepRefused(MarkerError):
    """A conditional update did not affect exactly one row; rolled back."""


@dataclass(frozen=True)
class PairingColumn:
    """A registered pairing record: which table and column hold a path.

    The plan names only the table (a key into the caller's registry) and a
    row id. Table and column identifiers come from this registry, never
    from the plan, so a plan cannot address an arbitrary column.
    """

    table: str
    path_column: str
    id_column: str = "id"

    def __post_init__(self) -> None:
        for name in (self.table, self.path_column, self.id_column):
            if not _IDENTIFIER.fullmatch(name):
                raise ValueError(f"not a plain SQL identifier: {name!r}")


@dataclass(frozen=True)
class PairingRef:
    """One pairing row an action must carry along: registry key and row id."""

    table: str
    row_id: int

    def as_dict(self) -> dict[str, Any]:
        return {"table": self.table, "id": self.row_id}


def open_durable(database: str | Path) -> sqlite3.Connection:
    return dal.open_connection(database, policy=DURABLE_WRITABLE_POLICY)


def install_schema(database: str | Path) -> None:
    """Create the marker table and its append-only triggers, idempotently."""
    connection = open_durable(database)
    try:
        with dal.transaction(connection):
            for statement in MARKER_SCHEMA:
                connection.execute(statement)
    finally:
        connection.close()


def _schema_rows(connection: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return [
        (row[0], row[1], row[2])
        for row in connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE tbl_name = ? ORDER BY type, name",
            (MARKER_TABLE,),
        )
        if row[2] is not None
    ]


def expected_schema_rows() -> list[tuple[str, str, str]]:
    rows = []
    for statement in MARKER_SCHEMA:
        # sqlite_master stores the statement without IF NOT EXISTS.
        stored = statement.replace(" IF NOT EXISTS", "", 1)
        kind = "table" if stored.startswith("CREATE TABLE") else "trigger"
        name = stored.split()[2]
        rows.append((kind, name, stored))
    return sorted(rows)


def require_schema(connection: sqlite3.Connection) -> None:
    """Refuse unless the marker table and both triggers exist exactly."""
    actual = sorted(_schema_rows(connection))
    if actual != expected_schema_rows():
        raise SchemaMissingError(
            f"{MARKER_TABLE} is missing or differs from the expected schema; "
            "install it with markers.install_schema (it is deliberately not a "
            "numbered migration, see the module docstring)"
        )


@dataclass(frozen=True)
class Marker:
    attempt_id: str
    action_id: str
    step: int
    kind: str
    location_id: int
    preimage_path: str
    postimage_path: str
    pairings: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class LocationRow:
    location_id: int
    archive_id: int
    path: str
    is_current: int


@dataclass(frozen=True)
class DatabaseView:
    """Markers and the rows an action's validation needs, from one snapshot."""

    markers: Mapping[tuple[str, int], Marker]
    locations: Mapping[int, LocationRow]
    pairings: Mapping[tuple[str, int], str | None]
    snapshot: Mapping[str, Any]


def read_view(
    database: str | Path,
    attempt_id: str | None,
    location_ids: Sequence[int],
    pairing_refs: Sequence[PairingRef],
    registry: Mapping[str, PairingColumn],
) -> DatabaseView:
    """One guarded read of markers, location rows and pairing rows."""

    def read(connection: sqlite3.Connection) -> DatabaseView:
        require_schema(connection)
        markers: dict[tuple[str, int], Marker] = {}
        if attempt_id is not None:
            for row in connection.execute(
                "SELECT attempt_id, action_id, step, kind, location_id, "
                "preimage_path, postimage_path, pairings_json "
                f"FROM {MARKER_TABLE} WHERE attempt_id = ?",
                (attempt_id,),
            ):
                markers[(row[1], int(row[2]))] = Marker(
                    row[0], row[1], int(row[2]), row[3], int(row[4]),
                    row[5], row[6], tuple(json.loads(row[7])),
                )
        locations: dict[int, LocationRow] = {}
        for location_id in sorted(set(location_ids)):
            row = connection.execute(
                "SELECT id, archive_id, path, is_current FROM file_locations "
                "WHERE id = ?",
                (location_id,),
            ).fetchone()
            if row is not None:
                locations[location_id] = LocationRow(
                    int(row[0]), int(row[1]), row[2], int(row[3])
                )
        pairings: dict[tuple[str, int], str | None] = {}
        for ref in pairing_refs:
            column = registry.get(ref.table)
            if column is None:
                raise MarkerError(f"pairing table {ref.table!r} is not registered")
            row = connection.execute(
                f"SELECT {column.path_column} FROM {column.table} "
                f"WHERE {column.id_column} = ?",
                (ref.row_id,),
            ).fetchone()
            pairings[(ref.table, ref.row_id)] = None if row is None else row[0]
        return DatabaseView(markers, locations, pairings, {})

    snapshot = read_consistent_snapshot(
        database, read, context="canonical-naming database view"
    )
    view = snapshot.result
    return DatabaseView(
        view.markers, view.locations, view.pairings, snapshot.report_fields()
    )


def commit_step(
    database: str | Path,
    *,
    attempt_id: str,
    action_id: str,
    step: int,
    kind: str,
    archive_id: int,
    location_id: int,
    preimage: str,
    postimage: str,
    pairings: Sequence[PairingRef],
    registry: Mapping[str, PairingColumn],
    revalidate: Callable[[], None],
) -> None:
    """§5.4 step 4: conditional updates plus the marker, in one transaction.

    ``revalidate`` runs after ``BEGIN IMMEDIATE`` has the write lock, so a
    wait for that lock is always followed by a fresh check of the file
    (§5.3: revalidation after every lock wait). It raises to refuse, which
    rolls everything back.
    """
    connection = open_durable(database)
    try:
        require_schema(connection)
        with dal.transaction(connection):
            revalidate()
            existing = connection.execute(
                f"SELECT 1 FROM {MARKER_TABLE} WHERE attempt_id = ? AND "
                "action_id = ? AND step = ?",
                (attempt_id, action_id, step),
            ).fetchone()
            if existing is not None:
                raise StepRefused(
                    f"marker for {attempt_id}/{action_id}/step {step} already exists"
                )
            try:
                cursor = connection.execute(
                    "UPDATE file_locations SET path = ?, "
                    "last_seen_at = CURRENT_TIMESTAMP "
                    "WHERE id = ? AND archive_id = ? AND path = ? AND is_current = 1",
                    (postimage, location_id, archive_id, preimage),
                )
            except sqlite3.IntegrityError as exc:
                raise StepRefused(
                    f"file_locations row {location_id} cannot move to {postimage}: {exc}"
                ) from exc
            if cursor.rowcount != 1:
                raise StepRefused(
                    f"file_locations row {location_id} (archive {archive_id}) is "
                    f"not at its preimage {preimage}: {cursor.rowcount} rows matched, "
                    "exactly one required"
                )
            for ref in pairings:
                column = registry.get(ref.table)
                if column is None:
                    raise StepRefused(f"pairing table {ref.table!r} is not registered")
                cursor = connection.execute(
                    f"UPDATE {column.table} SET {column.path_column} = ? "
                    f"WHERE {column.id_column} = ? AND {column.path_column} = ?",
                    (postimage, ref.row_id, preimage),
                )
                if cursor.rowcount != 1:
                    raise StepRefused(
                        f"pairing {ref.table}/{ref.row_id} is not at its preimage "
                        f"{preimage}: {cursor.rowcount} rows matched, exactly one "
                        "required"
                    )
            connection.execute(
                f"INSERT INTO {MARKER_TABLE} (attempt_id, action_id, step, kind, "
                "location_id, preimage_path, postimage_path, pairings_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attempt_id, action_id, step, kind, location_id, preimage,
                    postimage, json.dumps([p.as_dict() for p in pairings], sort_keys=True),
                ),
            )
    finally:
        connection.close()
