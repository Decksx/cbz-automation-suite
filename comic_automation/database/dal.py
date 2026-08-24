"""One connection policy, one transaction boundary, narrow repositories.

This is infrastructure, not behaviour. Nothing here changes what the
application does; it gives the revision work a single place to open a
connection, a single owner of commit and rollback, and repositories that
cannot quietly commit on someone else's behalf.

Why this exists
---------------

Four independent connection implementations were in the tree when this was
written, and they had already drifted from each other:

===============================  ===============  ============  ===============
implementation                   read-only guard  busy_timeout  isolation_level
===============================  ===============  ============  ===============
`database/connection.py`         n/a (writable)   30000         ``None``
`database/read_guards.py`        ``query_only``   *absent*      ``None``
`scripts/db.py`                  *none*           30000         *driver default*
`perceptual_reuse_analysis.py`   ``query_only``   60000         ``None``
===============================  ===============  ============  ===============

Two of those differences are not cosmetic. A "read-only" connection without
``PRAGMA query_only`` is only read-only by convention. And a connection left
on the driver's default ``isolation_level`` has pysqlite inserting implicit
transactions underneath the codebase's own ``BEGIN IMMEDIATE`` / ``COMMIT``
statements, which is the kind of thing that works until the day it does not.

`ConnectionPolicy` makes the pragma set data rather than four copies of a
sequence of ``execute`` calls, so a future divergence is a changed constant
that shows up in a diff instead of a line quietly missing from one of them.

Transaction ownership
---------------------

`transaction()` owns the boundary: it issues ``BEGIN IMMEDIATE``, commits on
success, and rolls back on **any** exception before re-raising. Callers do
not write ``ROLLBACK``, and repositories must not.

That last rule is enforced rather than documented. Every repository write
calls `require_transaction()` first, so a caller that forgets the boundary
gets a `TransactionRequiredError` instead of an implicit single-statement
commit. Implicit commits are how a half-finished multi-step change becomes
durable: three of four writes land, the fourth raises, and there is nothing
to roll back because each one committed as it went.

Scope
-----

Deliberately narrow. No schema migration, no revision tables, no change to
any existing call site's behaviour. The repositories cover only the two
entities the revision migration builds on -- archive identity, and the
content signature a revision will be keyed to -- both over tables that
already exist.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Sequence


class DalError(RuntimeError):
    """Base class for this module's refusals."""


class TransactionRequiredError(DalError):
    """A repository write was attempted outside a transaction.

    Raised rather than silently proceeding, because pysqlite would
    otherwise commit the statement on its own and a multi-step change would
    become partially durable with nothing left to roll back.
    """


class ReadOnlyConnectionError(DalError):
    """A write was attempted on a connection opened read-only."""


@dataclass(frozen=True)
class ConnectionPolicy:
    """The complete pragma and connection policy for one access mode.

    Held as data so that a difference between two modes is visible as a
    difference between two constants, rather than as a line missing from
    one of several near-identical functions.
    """

    name: str
    readonly: bool
    timeout_seconds: float
    pragmas: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def apply(self, connection: sqlite3.Connection) -> None:
        for pragma, value in self.pragmas:
            connection.execute(f"PRAGMA {pragma} = {value}")

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "readonly": self.readonly,
            "timeout_seconds": self.timeout_seconds,
            "pragmas": {name: value for name, value in self.pragmas},
        }


# Matches `database/connection.py` exactly, pragma for pragma and value for
# value. A test asserts that equivalence, so this is a consolidation rather
# than a redefinition -- if the two ever disagree, the test says so instead
# of the application behaving differently depending on which door it came
# through.
WRITABLE_POLICY = ConnectionPolicy(
    name="writable",
    readonly=False,
    timeout_seconds=30.0,
    pragmas=(
        # SQLite ignores declared FOREIGN KEY constraints unless this is
        # on, and it is per-connection rather than stored in the file.
        ("foreign_keys", "ON"),
        # Readers do not block writers under WAL, which matters because
        # CLI tools read this database while a worker is writing.
        ("journal_mode", "WAL"),
        # Safe under WAL: a crash can lose the most recent commit but
        # cannot corrupt the database.
        ("synchronous", "NORMAL"),
        # Retry internally on SQLITE_BUSY rather than failing at once.
        ("busy_timeout", "30000"),
    ),
)

# Both layers are set, and they fail in opposite directions. `query_only`
# rejects writes at the statement level but is a pragma, so any code on the
# connection can turn it back off -- measured, and a write then succeeds.
# `mode=ro` refuses at the VFS layer and survives that. Neither alone is
# sufficient, which is why dropping either one is covered by its own test.
READ_ONLY_POLICY = ConnectionPolicy(
    name="read_only",
    readonly=True,
    timeout_seconds=30.0,
    pragmas=(
        ("query_only", "ON"),
        ("busy_timeout", "30000"),
    ),
)


def open_connection(
    database_path: str | Path,
    *,
    readonly: bool = False,
    policy: ConnectionPolicy | None = None,
) -> sqlite3.Connection:
    """Open one connection under the policy for its mode.

    Read-only connections use SQLite's ``mode=ro`` URI flag, which refuses
    to create the file if it does not exist. A plain `sqlite3.connect` on a
    missing path silently creates an empty database, and an audit that
    reports zero rows against a database it just created itself is worse
    than one that fails.

    `isolation_level=None` on both modes: the codebase issues its own
    ``BEGIN IMMEDIATE`` / ``COMMIT`` / ``ROLLBACK``, and the driver's
    implicit transaction handling fights them.
    """
    resolved_policy = policy or (
        READ_ONLY_POLICY if readonly else WRITABLE_POLICY
    )
    path = Path(database_path)

    if resolved_policy.readonly:
        # Checked before touching SQLite, so a missing path can never
        # result in a created file.
        if not path.is_file():
            raise FileNotFoundError(f"Database does not exist: {path}")

        target: str | Path = f"{path.resolve(strict=True).as_uri()}?mode=ro"
        connection = sqlite3.connect(
            target,
            uri=True,
            timeout=resolved_policy.timeout_seconds,
            isolation_level=None,
        )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            path,
            timeout=resolved_policy.timeout_seconds,
            isolation_level=None,
        )

    connection.row_factory = sqlite3.Row
    resolved_policy.apply(connection)
    return connection


@contextmanager
def connection_scope(
    database_path: str | Path, *, readonly: bool = False
) -> Iterator[sqlite3.Connection]:
    """Open a connection and close it, whatever happens."""
    connection = open_connection(database_path, readonly=readonly)

    try:
        yield connection
    finally:
        connection.close()


def is_readonly(connection: sqlite3.Connection) -> bool:
    """True when this connection rejects writes at the statement level."""
    return bool(
        connection.execute("PRAGMA query_only").fetchone()[0]
    )


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Own one write transaction: begin, then commit or roll back.

    ``BEGIN IMMEDIATE`` rather than a bare ``BEGIN``: the write lock is
    taken up front, so two writers collide at the start instead of one
    discovering halfway through that it cannot upgrade.

    Rollback ownership lives here and nowhere else. Any exception rolls
    back and re-raises, so a caller never has to remember, and a repository
    is never in a position to decide.

    Nesting is refused rather than silently joined. SQLite has no nested
    transactions, so an inner block that "committed" would really be
    committing the outer one's work as well -- and the outer block would
    then roll back nothing.
    """
    if connection.in_transaction:
        raise DalError(
            "A transaction is already open on this connection. SQLite has "
            "no nested transactions, so joining silently would let an "
            "inner block commit the outer block's work."
        )

    if is_readonly(connection):
        raise ReadOnlyConnectionError(
            "Cannot open a write transaction on a read-only connection."
        )

    connection.execute("BEGIN IMMEDIATE")

    try:
        yield connection
    except BaseException:
        # BaseException, not Exception: a KeyboardInterrupt in the middle
        # of a multi-step write must not leave the transaction open for
        # whatever runs next on this connection.
        if connection.in_transaction:
            connection.execute("ROLLBACK")

        raise

    connection.execute("COMMIT")


def require_transaction(connection: sqlite3.Connection) -> None:
    """Refuse a write that no transaction owns.

    Called by every repository write. Without it, `isolation_level=None`
    means each statement commits on its own, so a four-step change that
    fails on the fourth leaves three steps durable and nothing to undo.
    """
    if not connection.in_transaction:
        raise TransactionRequiredError(
            "This write must run inside `transaction()`. Writing outside "
            "one commits each statement on its own, which makes a partial "
            "multi-step change durable with nothing left to roll back."
        )


# --- repositories --------------------------------------------------------
#
# Only the two entities the revision migration builds on. Both sit over
# tables that already exist; neither introduces schema.


@dataclass(frozen=True)
class ArchiveRecord:
    """One archive identity, as `archive_files` holds it today."""

    archive_id: int
    file_size: int
    sha256: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ArchiveRecord":
        return cls(
            archive_id=int(row["id"]),
            file_size=int(row["file_size"]),
            sha256=row["sha256"],
        )


@dataclass(frozen=True)
class ContentSignatureRecord:
    """The ordered-page signature a revision will be keyed to.

    `digest` is the identity a future revision row points at; it is
    deliberately *not* unique in this schema, and sixteen groups in
    production share a digest across several archives. Anything built on
    top has to treat it as a many-to-one key rather than a primary one.
    """

    archive_id: int
    digest: str
    page_count: int
    source_file_size: int
    source_modified_time_ns: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ContentSignatureRecord":
        return cls(
            archive_id=int(row["archive_id"]),
            digest=row["digest"],
            page_count=int(row["page_count"]),
            source_file_size=int(row["source_file_size"]),
            source_modified_time_ns=int(row["source_modified_time_ns"]),
        )


class _Repository:
    """Shared plumbing: a connection, and the transaction rule."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def _require_transaction(self) -> None:
        require_transaction(self.connection)


class ArchiveRepository(_Repository):
    """Read and write archive identities.

    Reads deliberately do not require a transaction: an audit opening a
    read-only connection has no business starting a write one.
    """

    def get(self, archive_id: int) -> ArchiveRecord | None:
        row = self.connection.execute(
            "SELECT id, file_size, sha256 FROM archive_files WHERE id = ?",
            (archive_id,),
        ).fetchone()
        return ArchiveRecord.from_row(row) if row is not None else None

    def exists(self, archive_id: int) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM archive_files WHERE id = ?", (archive_id,)
            ).fetchone()
            is not None
        )

    def count(self) -> int:
        return int(
            self.connection.execute(
                "SELECT COUNT(*) FROM archive_files"
            ).fetchone()[0]
        )

    def identity_ids(self) -> list[int]:
        """Every archive id, ordered. The identity census, as data."""
        return [
            int(row["id"])
            for row in self.connection.execute(
                "SELECT id FROM archive_files ORDER BY id"
            )
        ]

    def create(self, *, file_size: int, sha256: str | None = None) -> int:
        self._require_transaction()
        cursor = self.connection.execute(
            "INSERT INTO archive_files (file_size, sha256) VALUES (?, ?)",
            (file_size, sha256),
        )
        return int(cursor.lastrowid)

    def set_sha256(self, archive_id: int, sha256: str | None) -> None:
        self._require_transaction()
        self.connection.execute(
            "UPDATE archive_files SET sha256 = ? WHERE id = ?",
            (sha256, archive_id),
        )


class ContentSignatureRepository(_Repository):
    """Read and write the per-archive content signature."""

    def for_archive(
        self, archive_id: int
    ) -> ContentSignatureRecord | None:
        row = self.connection.execute(
            """
            SELECT archive_id, digest, page_count, source_file_size,
                   source_modified_time_ns
            FROM archive_content_signatures
            WHERE archive_id = ?
            """,
            (archive_id,),
        ).fetchone()
        return (
            ContentSignatureRecord.from_row(row) if row is not None else None
        )

    def archives_sharing_digest(self, digest: str) -> list[int]:
        """Every archive carrying `digest`.

        Returns a list, not one id, because the column is not unique and
        treating it as though it were is how a revision would silently
        attach itself to the wrong archive.
        """
        return [
            int(row["archive_id"])
            for row in self.connection.execute(
                "SELECT archive_id FROM archive_content_signatures "
                "WHERE digest = ? ORDER BY archive_id",
                (digest,),
            )
        ]

    def duplicate_digests(self) -> list[tuple[str, int]]:
        """Digests held by more than one archive, with their counts."""
        return [
            (row["digest"], int(row["archives"]))
            for row in self.connection.execute(
                """
                SELECT digest, COUNT(*) AS archives
                FROM archive_content_signatures
                GROUP BY digest
                HAVING COUNT(*) > 1
                ORDER BY digest
                """
            )
        ]

    def save(
        self,
        *,
        archive_id: int,
        digest: str,
        page_count: int,
        image_bytes: int,
        source_file_size: int,
        source_modified_time_ns: int,
        algorithm: str = "ordered-page",
        algorithm_version: str = "1",
    ) -> None:
        self._require_transaction()
        self.connection.execute(
            """
            INSERT INTO archive_content_signatures (
                archive_id, algorithm, algorithm_version, digest,
                page_count, image_bytes, source_file_size,
                source_modified_time_ns
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(archive_id) DO UPDATE SET
                algorithm = excluded.algorithm,
                algorithm_version = excluded.algorithm_version,
                digest = excluded.digest,
                page_count = excluded.page_count,
                image_bytes = excluded.image_bytes,
                source_file_size = excluded.source_file_size,
                source_modified_time_ns = excluded.source_modified_time_ns
            """,
            (
                archive_id,
                algorithm,
                algorithm_version,
                digest,
                page_count,
                image_bytes,
                source_file_size,
                source_modified_time_ns,
            ),
        )


# --- the connection-factory allowlist ------------------------------------
#
# Recorded here rather than in the test so the list and the reasons live
# beside the policy they are exceptions to.

SANCTIONED_CONNECT_SITES: dict[str, str] = {
    "comic_automation/database/dal.py": (
        "This module. The policy has to call sqlite3.connect somewhere."
    ),
    "comic_automation/database/connection.py": (
        "The pre-existing writable factory, retained so no call site "
        "changes behaviour in this PR."
    ),
    "comic_automation/database/read_guards.py": (
        "The pre-existing read-only factory used by every audit."
    ),
    "comic_automation/database/backup_cli.py": (
        "Opens the backup *destination*, which is a new file being "
        "created rather than the operational database."
    ),
    "comic_automation/archive/duplicate_resolution_cli.py": (
        "Opens the backup destination it is about to create, not the "
        "operational database, so the read/write policy does not apply."
    ),
    "comic_automation/archive/perceptual_reuse_analysis.py": (
        "A fourth read-only implementation with its own 60s timeout. "
        "Known drift; consolidating it changes behaviour and is out of "
        "scope for this PR."
    ),
    "scripts/db.py": (
        "A legacy helper whose read-only mode sets no query_only and "
        "whose connections keep the driver's default isolation_level. "
        "Known drift; migrating its callers is a separate change."
    ),
}


def find_connect_sites(root: Path) -> dict[str, int]:
    """Every production file calling `sqlite3.connect`, with a count.

    Deliberately textual and deliberately narrow: it exists to notice a
    *new* bypass appearing, not to police how the sanctioned ones work.
    """
    found: dict[str, int] = {}

    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()

        if relative.startswith("tests/"):
            continue

        text = path.read_text(encoding="utf-8", errors="replace")
        count = sum(
            1
            for line in text.splitlines()
            if "sqlite3.connect(" in line
            and not line.lstrip().startswith("#")
        )

        if count:
            found[relative] = count

    return found


def unsanctioned_connect_sites(
    root: Path, sanctioned: Sequence[str] | None = None
) -> list[str]:
    """Files calling `sqlite3.connect` that are not on the allowlist."""
    allowed = set(
        sanctioned
        if sanctioned is not None
        else SANCTIONED_CONNECT_SITES
    )
    return sorted(set(find_connect_sites(root)) - allowed)
