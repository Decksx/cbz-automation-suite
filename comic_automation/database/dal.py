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

import ast
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Mapping


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

    Passing a `policy` whose mode disagrees with `readonly` is refused
    rather than resolved. Both resolutions are wrong in one direction --
    letting the policy win turns an explicit read-only request into a
    writable connection, and letting the flag win discards the policy the
    caller deliberately supplied. A safety request must never quietly
    become its opposite, so the disagreement is the caller's to settle.
    """
    if policy is not None and policy.readonly != readonly:
        raise DalError(
            f"Conflicting connection request: readonly={readonly} but "
            f"policy {policy.name!r} is readonly={policy.readonly}. "
            "Refused rather than resolved: silently preferring either one "
            "can hand back a writable connection to a caller that asked "
            "for a read-only one."
        )

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


# Connections currently inside a `transaction()` block.
#
# `require_transaction()` cannot rely on `connection.in_transaction` alone.
# That is true for *any* open transaction -- a raw ``BEGIN`` issued by a
# caller, or one inherited from unrelated code -- so a repository write
# would pass having never entered this module's boundary, and would be
# relying on a commit and rollback that nothing here owns.
#
# A plain set rather than a `WeakSet` because `sqlite3.Connection` supports
# neither weak references nor attribute assignment. It cannot leak:
# membership begins at ``BEGIN IMMEDIATE`` and is removed in a ``finally``,
# so it lasts exactly as long as the ``with`` block. Set mutation is atomic
# under the GIL, and a connection is single-threaded by default anyway.
_OWNED_TRANSACTIONS: set[sqlite3.Connection] = set()


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
    # Recorded only after BEGIN succeeds, so a connection that failed to
    # start a transaction is never treated as owning one.
    _OWNED_TRANSACTIONS.add(connection)

    try:
        yield connection
        connection.execute("COMMIT")
    except BaseException:
        # BaseException, not Exception: a KeyboardInterrupt in the middle
        # of a multi-step write must not leave the transaction open for
        # whatever runs next on this connection.
        #
        # COMMIT is inside this block because COMMIT is itself a statement
        # that fails: a deferred FOREIGN KEY is checked here and nowhere
        # earlier, and a busy database can refuse here too. SQLite leaves
        # the transaction *open* when COMMIT fails, so committing outside
        # this block left the caller holding the write lock, with the
        # failed change still visible on the connection and durable the
        # moment anything else on it committed.
        if connection.in_transaction:
            connection.execute("ROLLBACK")

        raise
    finally:
        # Ownership ends with the block on every path -- commit, rollback,
        # or a failure of the COMMIT itself.
        _OWNED_TRANSACTIONS.discard(connection)


def require_transaction(connection: sqlite3.Connection) -> None:
    """Refuse a write that this module's boundary does not own.

    Called by every repository write, and it makes two distinct refusals
    because there are two distinct ways to be outside the boundary.

    No transaction at all: `isolation_level=None` means each statement
    commits on its own, so a four-step change that fails on the fourth
    leaves three steps durable and nothing to undo.

    A transaction this module did not open -- a raw ``BEGIN``, or one
    inherited from unrelated code: `in_transaction` is true, so checking
    only that would let the write through. Nothing here owns that
    transaction's commit or rollback, so the guarantee the repository is
    relying on is simply absent. "One transaction owner" has to mean this
    owner, or it means nothing.
    """
    if connection in _OWNED_TRANSACTIONS:
        return

    if connection.in_transaction:
        raise TransactionRequiredError(
            "A transaction is open on this connection, but `transaction()` "
            "did not start it -- a raw BEGIN, or one inherited from other "
            "code. Repository writes require this module's boundary, which "
            "is what owns the commit and the rollback."
        )

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


@dataclass(frozen=True)
class RevisionRecord:
    """One immutable byte state of one logical archive.

    `archive_sha256` is None exactly when `identity_state` is 'provisional',
    which means the archive's bytes have never been hashed -- 147 archives
    were in that state when migration 014 was written, and some of them have
    no reachable file, so it is a state to be carried rather than a gap to be
    filled in later.
    """

    revision_id: int
    archive_id: int
    revision_ordinal: int
    identity_state: str
    archive_sha256: str | None
    content_signature: str | None
    file_size: int | None
    page_count: int | None
    previous_revision_id: int | None
    evidence: str
    source: str

    @property
    def is_established(self) -> bool:
        return self.identity_state == "established"

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "RevisionRecord":
        return cls(
            revision_id=int(row["id"]),
            archive_id=int(row["archive_id"]),
            revision_ordinal=int(row["revision_ordinal"]),
            identity_state=row["identity_state"],
            archive_sha256=row["archive_sha256"],
            content_signature=row["content_signature"],
            file_size=(
                int(row["file_size"]) if row["file_size"] is not None else None
            ),
            page_count=(
                int(row["page_count"])
                if row["page_count"] is not None
                else None
            ),
            previous_revision_id=(
                int(row["previous_revision_id"])
                if row["previous_revision_id"] is not None
                else None
            ),
            evidence=row["evidence"],
            source=row["source"],
        )


_REVISION_COLUMNS = """
    id, archive_id, revision_ordinal, identity_state, archive_sha256,
    content_signature, file_size, page_count, previous_revision_id,
    evidence, source
"""


class RevisionRepository(_Repository):
    """Read and append archive revisions, and move the current pointer.

    Two rules shape every method here, and both come from the schema rather
    than from this class: a revision is never updated, and the pointer at
    `archive_files.current_revision_id` is the only statement of which
    revision is current. Nothing in this class writes an `is_current` flag,
    because there is no such column to write.
    """

    # --- reads ------------------------------------------------------------

    def get(self, revision_id: int) -> RevisionRecord | None:
        row = self.connection.execute(
            f"SELECT {_REVISION_COLUMNS} FROM archive_revisions WHERE id = ?",
            (revision_id,),
        ).fetchone()
        return RevisionRecord.from_row(row) if row is not None else None

    def current_for(self, archive_id: int) -> RevisionRecord | None:
        """The archive's current revision, resolved through the pointer.

        Deliberately joined from `archive_files.current_revision_id` rather
        than by taking the highest ordinal. The two agree today, but "current"
        is an operator-controlled decision -- rolling back to an earlier
        generation is a legitimate act -- and reading it as "the newest" would
        silently disagree the moment anyone does that.
        """
        row = self.connection.execute(
            f"""
            SELECT {_REVISION_COLUMNS}
            FROM archive_revisions
            WHERE id = (
                SELECT current_revision_id FROM archive_files WHERE id = ?
            )
            """,
            (archive_id,),
        ).fetchone()
        return RevisionRecord.from_row(row) if row is not None else None

    def lineage_for(self, archive_id: int) -> list[RevisionRecord]:
        """Every revision of one archive, oldest first.

        This is the three-generation view: for archive 37704 it returns three
        rows, and the fact that they are three rows rather than one overwritten
        row is the whole point of the table.
        """
        return [
            RevisionRecord.from_row(row)
            for row in self.connection.execute(
                f"""
                SELECT {_REVISION_COLUMNS}
                FROM archive_revisions
                WHERE archive_id = ?
                ORDER BY revision_ordinal
                """,
                (archive_id,),
            )
        ]

    def revision_with_digest(
        self, archive_id: int, archive_sha256: str
    ) -> RevisionRecord | None:
        """The revision of *archive_id* holding these exact bytes, if any."""
        row = self.connection.execute(
            f"""
            SELECT {_REVISION_COLUMNS}
            FROM archive_revisions
            WHERE archive_id = ? AND archive_sha256 = ?
            """,
            (archive_id, archive_sha256),
        ).fetchone()
        return RevisionRecord.from_row(row) if row is not None else None

    def archives_sharing_digest(self, archive_sha256: str) -> list[int]:
        """Every archive holding these bytes in some revision.

        A list, and the digest is not globally unique: 888 exact-duplicate
        groups were measured in production. Returning one id would be the
        first step towards merging two archives that are deliberately
        distinct.
        """
        return [
            int(row["archive_id"])
            for row in self.connection.execute(
                """
                SELECT DISTINCT archive_id FROM archive_revisions
                WHERE archive_sha256 = ?
                ORDER BY archive_id
                """,
                (archive_sha256,),
            )
        ]

    # --- writes -----------------------------------------------------------

    def record_or_reuse(
        self,
        *,
        archive_id: int,
        archive_sha256: str,
        evidence: str,
        content_signature: str | None = None,
        file_size: int | None = None,
        page_count: int | None = None,
    ) -> tuple[int, bool]:
        """Append a revision for these bytes, or return the existing one.

        Returns ``(revision_id, created)``.

        Re-seeing bytes an archive already has is not a new revision -- the
        roadmap is explicit that a revision is a content state, not a sighting.
        Appending one anyway would violate UNIQUE(archive_id, archive_sha256)
        and, worse, would make a file that keeps being rediscovered look like
        it kept changing. The caller records an observation instead.
        """
        self._require_transaction()

        existing = self.revision_with_digest(archive_id, archive_sha256)

        if existing is not None:
            return existing.revision_id, False

        # A provisional revision is a placeholder for exactly these unknown
        # bytes, so learning the digest resolves it rather than adding a
        # generation beside it.
        provisional = self.connection.execute(
            "SELECT id FROM archive_revisions "
            "WHERE archive_id = ? AND identity_state = 'provisional'",
            (archive_id,),
        ).fetchone()

        if provisional is not None:
            return (
                self._establish(
                    archive_id=archive_id,
                    provisional_id=int(provisional["id"]),
                    archive_sha256=archive_sha256,
                    evidence=evidence,
                    content_signature=content_signature,
                    file_size=file_size,
                    page_count=page_count,
                ),
                True,
            )

        return (
            self._append(
                archive_id=archive_id,
                archive_sha256=archive_sha256,
                evidence=evidence,
                content_signature=content_signature,
                file_size=file_size,
                page_count=page_count,
            ),
            True,
        )

    def _append(
        self,
        *,
        archive_id: int,
        archive_sha256: str | None,
        evidence: str,
        content_signature: str | None,
        file_size: int | None,
        page_count: int | None,
        identity_state: str = "established",
    ) -> int:
        """Add the next generation, linked to the one before it."""
        tip = self.connection.execute(
            "SELECT id, revision_ordinal FROM archive_revisions "
            "WHERE archive_id = ? ORDER BY revision_ordinal DESC LIMIT 1",
            (archive_id,),
        ).fetchone()

        ordinal = 1 if tip is None else int(tip["revision_ordinal"]) + 1
        previous = None if tip is None else int(tip["id"])

        cursor = self.connection.execute(
            """
            INSERT INTO archive_revisions (
                archive_id, revision_ordinal, identity_state, archive_sha256,
                content_signature, file_size, page_count,
                previous_revision_id, evidence, source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'runtime')
            """,
            (
                archive_id,
                ordinal,
                identity_state,
                archive_sha256,
                content_signature,
                file_size,
                page_count,
                previous,
                evidence,
            ),
        )
        return int(cursor.lastrowid)

    def _establish(
        self,
        *,
        archive_id: int,
        provisional_id: int,
        archive_sha256: str,
        evidence: str,
        content_signature: str | None,
        file_size: int | None,
        page_count: int | None,
    ) -> int:
        """Replace a provisional revision with the bytes it stood in for.

        Delete-then-insert rather than an update, because revisions are
        immutable and the schema enforces it. The provisional row is the one
        kind this schema permits deleting, precisely so this path exists.

        The insert reuses the placeholder's ordinal and predecessor, so
        establishing an identity does not invent a generation: the archive had
        one unknown byte state and now has one known one.
        """
        placeholder = self.connection.execute(
            "SELECT revision_ordinal, previous_revision_id "
            "FROM archive_revisions WHERE id = ?",
            (provisional_id,),
        ).fetchone()

        # Clear the pointer first: the column's foreign key is deferred, but
        # leaving it aimed at a row being deleted makes the intermediate state
        # depend on that deferral. Doing it explicitly keeps the sequence
        # readable and correct either way.
        self.connection.execute(
            "UPDATE archive_files SET current_revision_id = NULL "
            "WHERE id = ? AND current_revision_id = ?",
            (archive_id, provisional_id),
        )
        self.connection.execute(
            "DELETE FROM archive_revisions WHERE id = ?", (provisional_id,)
        )

        cursor = self.connection.execute(
            """
            INSERT INTO archive_revisions (
                archive_id, revision_ordinal, identity_state, archive_sha256,
                content_signature, file_size, page_count,
                previous_revision_id, evidence, source
            )
            VALUES (?, ?, 'established', ?, ?, ?, ?, ?, ?, 'runtime')
            """,
            (
                archive_id,
                int(placeholder["revision_ordinal"]),
                archive_sha256,
                content_signature,
                file_size,
                page_count,
                placeholder["previous_revision_id"],
                evidence,
            ),
        )
        return int(cursor.lastrowid)

    def set_current(self, archive_id: int, revision_id: int) -> None:
        """Point an archive at one of its own revisions.

        The ownership trigger rejects a foreign revision, so this does not
        re-check it: duplicating the rule here would let the two drift, and
        the database is the one that cannot be bypassed.
        """
        self._require_transaction()
        self.connection.execute(
            "UPDATE archive_files SET current_revision_id = ? WHERE id = ?",
            (revision_id, archive_id),
        )

    def observe(
        self,
        *,
        revision_id: int,
        location_id: int | None = None,
        run_id: int | None = None,
        file_size: int | None = None,
        modified_time_ns: int | None = None,
    ) -> int:
        """Record a sighting of known bytes. Appends; rewrites nothing."""
        self._require_transaction()
        cursor = self.connection.execute(
            """
            INSERT INTO archive_revision_observations (
                revision_id, location_id, run_id, file_size, modified_time_ns
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (revision_id, location_id, run_id, file_size, modified_time_ns),
        )
        return int(cursor.lastrowid)

    def observations_for(self, revision_id: int) -> list[int]:
        """Observation ids for one revision, oldest first."""
        return [
            int(row["id"])
            for row in self.connection.execute(
                "SELECT id FROM archive_revision_observations "
                "WHERE revision_id = ? ORDER BY id",
                (revision_id,),
            )
        ]


# --- the connection-factory guard ----------------------------------------
#
# Recorded here rather than in the test so the list and the reasons live
# beside the policy they are exceptions to.
#
# Each entry pins an exact *call count*, not just a filename. A filename
# allowlist would let any number of further `sqlite3.connect` calls be
# added inside an already-sanctioned file -- which is most of the files
# that touch the database -- so the guard would pass while the bypass it
# exists to catch walked in through the front door.


@dataclass(frozen=True)
class SanctionedSite:
    """One allowlisted file, with the number of calls it is allowed."""

    calls: int
    reason: str


SANCTIONED_CONNECT_SITES: dict[str, SanctionedSite] = {
    "comic_automation/database/dal.py": SanctionedSite(
        calls=2,
        reason=(
            "This module. The policy has to call sqlite3.connect somewhere, "
            "once for the read-only URI form and once for the writable one."
        ),
    ),
    "comic_automation/database/connection.py": SanctionedSite(
        calls=1,
        reason=(
            "The pre-existing writable factory, retained so no call site "
            "changes behaviour in this PR."
        ),
    ),
    "comic_automation/database/read_guards.py": SanctionedSite(
        calls=1,
        reason="The pre-existing read-only factory used by every audit.",
    ),
    "comic_automation/database/backup_cli.py": SanctionedSite(
        calls=1,
        reason=(
            "Opens the backup *destination*, which is a new file being "
            "created rather than the operational database."
        ),
    ),
    "comic_automation/archive/duplicate_resolution_cli.py": SanctionedSite(
        calls=1,
        reason=(
            "Opens the backup destination it is about to create, not the "
            "operational database, so the read/write policy does not apply."
        ),
    ),
    "comic_automation/archive/perceptual_reuse_analysis.py": SanctionedSite(
        calls=1,
        reason=(
            "A fourth read-only implementation with its own 60s timeout. "
            "Known drift; consolidating it changes behaviour and is out of "
            "scope for this PR."
        ),
    ),
    "scripts/db.py": SanctionedSite(
        calls=2,
        reason=(
            "A legacy helper whose read-only mode sets no query_only and "
            "whose connections keep the driver's default isolation_level. "
            "Known drift; migrating its callers is a separate change."
        ),
    ),
}


class UnparseableModuleError(DalError):
    """A production module could not be parsed, so it could not be scanned.

    Raised rather than skipped. A file the guard cannot read is a file the
    guard cannot clear, and passing silently over it is exactly the blind
    spot the guard exists to remove.
    """


def _connect_call_lines(source: str) -> list[int]:
    """Line numbers of every `sqlite3.connect` call in *source*.

    Parsed rather than grepped. The textual version matched the literal
    string ``sqlite3.connect(``, which missed three real shapes:

    - ``import sqlite3 as sq`` then ``sq.connect(...)``;
    - ``from sqlite3 import connect`` then ``connect(...)``, with or
      without an ``as`` alias;
    - a call split across lines, or spaced as ``sqlite3 . connect (``.

    The AST sees the call, not its spelling, so all three are counted.

    ``sqlite3`` is treated as a module alias whether or not an ``import``
    was found, because ``sqlite3.connect(...)`` is a bypass however that
    name came to be bound.

    Known limit: a fully dynamic call -- ``getattr(sqlite3, "conn" + "ect")``
    or an ``importlib`` lookup -- is not detected. Defeating this guard on
    purpose is possible; it is aimed at the bypass added without noticing,
    which is the one that actually happens.
    """
    tree = ast.parse(source)

    module_aliases = {"sqlite3"}
    function_aliases: set[str] = set()

    # Collected in a full pass first: an import inside a function body still
    # binds the name for calls written above it in the file.
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sqlite3":
                    module_aliases.add(alias.asname or "sqlite3")
        elif isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
            for alias in node.names:
                if alias.name == "connect":
                    function_aliases.add(alias.asname or "connect")

    lines: list[int] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        func = node.func

        if isinstance(func, ast.Attribute) and func.attr == "connect":
            if (
                isinstance(func.value, ast.Name)
                and func.value.id in module_aliases
            ):
                lines.append(node.lineno)
        elif isinstance(func, ast.Name) and func.id in function_aliases:
            lines.append(node.lineno)

    return sorted(lines)


def find_connect_sites(root: Path) -> dict[str, int]:
    """Every production file calling `sqlite3.connect`, with a call count.

    `tests/` is excluded: test files legitimately open their own temporary
    databases, and holding them to the production policy would only teach
    people to add allowlist entries.
    """
    found: dict[str, int] = {}

    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()

        if relative.startswith("tests/") or relative.startswith(".venv/"):
            continue

        source = path.read_text(encoding="utf-8", errors="replace")

        try:
            lines = _connect_call_lines(source)
        except SyntaxError as error:
            raise UnparseableModuleError(f"{relative}: {error}") from error

        if lines:
            found[relative] = len(lines)

    return found


def unsanctioned_connect_sites(
    root: Path,
    sanctioned: Mapping[str, SanctionedSite | int] | None = None,
) -> list[str]:
    """Every bypass the allowlist does not account for, described.

    Two kinds, because a filename-level allowlist only catches the first:
    a file that opens its own connection and is not listed at all, and a
    listed file that has *grown* a call beyond the count recorded for it.

    A file with fewer calls than recorded is not reported here; the
    allowlist going stale is its own test.
    """
    entries = SANCTIONED_CONNECT_SITES if sanctioned is None else sanctioned
    allowed = {
        name: entry.calls if isinstance(entry, SanctionedSite) else int(entry)
        for name, entry in entries.items()
    }

    violations: list[str] = []

    for name, count in sorted(find_connect_sites(root).items()):
        if name not in allowed:
            violations.append(
                f"{name}: {count} sqlite3.connect call(s), not on the allowlist"
            )
        elif count > allowed[name]:
            violations.append(
                f"{name}: {count} sqlite3.connect call(s), allowlist records "
                f"{allowed[name]}"
            )

    return violations
