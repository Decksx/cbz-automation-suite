"""Shared WAL-aware read guards for the strictly read-only audits.

Every read-only audit in this codebase makes the same promise: the
numbers in its report all describe one and the same database state.
Under WAL -- which `comic_automation/database/connection.py` turns on
for every production connection -- that promise is *not* provable from
the database file's size and mtime.

The hazard, stated precisely
----------------------------

A commit made by another connection in WAL mode is appended to the
``-wal`` sidecar file. The main ``.db`` file is only rewritten later,
at checkpoint time. So across another connection's commit the main
database file's ``st_size`` and ``st_mtime_ns`` can be *byte-for-byte
identical*. An audit that fingerprints only the main file therefore
cannot detect a concurrent writer at all, and a multi-query audit can
happily emit a report that mixes pre-change and post-change
observations while reporting ``database_unchanged: true``.

``PRAGMA data_version`` is the detector that actually holds: SQLite
increments it whenever *another* connection commits, and it is frozen
for the duration of a read transaction on this connection. Sampling it
immediately before opening the read transaction and immediately after
closing it therefore brackets every read the report depends on.

The proven sequence, which `read_consistent_snapshot` encapsulates:

    data_version_before   (sampled outside, before the transaction)
    BEGIN                 (deferred read transaction)
    quick_check + all reads
    END                   (in a finally block; END is not a write and
                           is permitted under query_only)
    data_version_after    (sampled outside, after the transaction)

``data_version_before`` has to be sampled *outside* the transaction and
*before* ``quick_check``. Sampling it after ``quick_check`` would leave
that read outside the change-detection window, and a WAL commit landing
there would go undetected -- with no fingerprint change to fall back on.

Fingerprints are diagnostics, not the gate
------------------------------------------

File fingerprints are still worth taking, and `fingerprint_database_files`
extends them to cover the ``-wal`` and ``-shm`` sidecars -- strictly
more informative than main-file-only, because a WAL commit that leaves
the main file untouched *does* move the ``-wal`` file. That is what
makes the sidecar readings valuable to the WAL regression tests, which
use them to prove the main file stayed identical across a commit.

But no fingerprint, main or sidecar, can be the gate:

- the main fingerprint can miss a concurrent commit entirely (WAL, as
  above), and
- the sidecar fingerprints move for reasons that are not commits at
  all. Measured, not assumed: merely opening a WAL database read-only
  *creates* both sidecars if they are absent and bumps ``-shm``'s
  mtime on every read. So they change during a run that provably wrote
  nothing.

So callers treat ``data_version`` as the authoritative concurrency gate
and report fingerprints as supporting evidence, clearly labelled.
`fingerprint_report_fields` emits exactly that shape: the historical
main-file keys, the ``database_unchanged_is_diagnostic_only`` flag,
`FINGERPRINT_DIAGNOSTIC_NOTE`, and -- for the sidecars -- observed
*presence* rather than raw readings, because presence is the part that
is both deterministic and meaningful to a reader ("this database is in
WAL mode, therefore the fingerprint above cannot see a concurrent
commit"). See `SIDECAR_DIAGNOSTIC_NOTE`.

The historical ``database_unchanged`` key is preserved verbatim (same
name, same value: main-file size+mtime equality) because existing
reports and their consumers key off it -- but it now travels with the
note above and with the ``database_file_unchanged`` alias whose name
does not overstate what was actually compared.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Generic, Iterator, TypeVar


T = TypeVar("T")

WAL_SUFFIX = "-wal"
SHM_SUFFIX = "-shm"

SNAPSHOT_GUARANTEE = (
    "Every read in this report was issued inside a single deferred "
    "read transaction, bracketed by PRAGMA data_version readings taken "
    "outside it. data_version counts commits made by other "
    "connections, so an unchanged pair proves no other connection "
    "committed between the first and the last read -- the guarantee "
    "the file fingerprints below cannot provide under WAL."
)

FINGERPRINT_DIAGNOSTIC_NOTE = (
    "database_unchanged / database_file_unchanged compare only the "
    "main database file's size and mtime and are DIAGNOSTIC EVIDENCE "
    "ONLY, never the concurrency gate. In WAL mode another "
    "connection's commit is appended to the -wal sidecar and can leave "
    "the main file byte-identical, so a true value here does not prove "
    "nobody wrote to the database. The authoritative check is "
    "data_version_before == data_version_after, which this run "
    "enforces by raising DatabaseChangedError when it fails."
)

SIDECAR_DIAGNOSTIC_NOTE = (
    "database_wal_sidecar_observed / database_shm_sidecar_observed "
    "report whether a -wal / -shm sidecar existed at any point during "
    "this run. Their presence means the database is in WAL mode, which "
    "is precisely why the main-file fingerprint above cannot be the "
    "concurrency gate. Raw sidecar sizes and mtimes are deliberately "
    "NOT reported: merely opening a WAL database read-only creates "
    "both sidecars if they are absent, and can bump their mtimes on "
    "later reads, so those readings change even when nothing was "
    "written -- which makes them useless as evidence and would break "
    "these reports' guarantee that two runs over unchanged data "
    "produce byte-identical output. Callers that want the raw readings "
    "(the WAL regression tests do, to prove -wal moved while the main "
    "file did not) call read_guards.fingerprint_database_files()."
)


class ReadGuardError(RuntimeError):
    """Base class for conditions that invalidate a guarded read."""


class DatabaseChangedError(ReadGuardError):
    """Another connection committed while a guarded read was running.

    Detected via ``PRAGMA data_version``, which counts commits made by
    *other* connections. This is the guard that actually holds under
    WAL: a WAL commit can be entirely contained in the ``-wal`` file,
    leaving the main database file's size and mtime untouched, so a
    fingerprint comparison can miss it completely. If the counter
    moved, the report may mix pre- and post-change observations, so the
    run is rejected instead of reported as trustworthy.
    """


class DatabaseMutatedError(DatabaseChangedError):
    """A database file changed size or mtime during a read-only run.

    The audits are read-only by construction (mode=ro + query_only),
    so this check is defense in depth: if the underlying file was
    touched by *anything* (this process or another) while the audit
    ran, the run is treated as untrustworthy rather than silently
    reporting a possibly-inconsistent snapshot.

    It is a subclass of `DatabaseChangedError` because it reports the
    same class of problem through a strictly weaker detector: callers
    who want "the database did not change under me" can catch the base
    class and get both guards. Callers check `data_version` *first*, so
    a concurrent commit is always reported as the more precise
    `DatabaseChangedError` even when the file also happened to change.
    """


class DatabaseIntegrityError(ReadGuardError):
    """``PRAGMA quick_check`` did not return 'ok'.

    Reporting out of a structurally damaged database would produce
    numbers that look authoritative but are not, so the run is
    abandoned.
    """


@dataclass(frozen=True)
class DatabaseFingerprint:
    """Size + mtime of the *main* database file.

    Diagnostic only: see `FINGERPRINT_DIAGNOSTIC_NOTE`. Deliberately
    kept to these two fields because every existing report, test and
    caller in the codebase compares exactly this value.
    """

    size_bytes: int
    modified_time_ns: int


@dataclass(frozen=True)
class SidecarFingerprint:
    """Size + mtime of a ``-wal`` / ``-shm`` sidecar, if it exists.

    A sidecar is routinely absent (no WAL mode, or a fully
    checkpointed and closed database), which is why `present` is
    reported explicitly rather than being inferred from null sizes.
    """

    suffix: str
    present: bool
    size_bytes: int | None
    modified_time_ns: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "suffix": self.suffix,
            "present": self.present,
            "size_bytes": self.size_bytes,
            "modified_time_ns": self.modified_time_ns,
        }


@dataclass(frozen=True)
class DatabaseFileFingerprint:
    """Main file plus both WAL sidecars, sampled at one moment."""

    main: DatabaseFingerprint
    wal: SidecarFingerprint
    shm: SidecarFingerprint

    def as_dict(self) -> dict[str, Any]:
        return {
            "main": {
                "size_bytes": self.main.size_bytes,
                "modified_time_ns": self.main.modified_time_ns,
            },
            "wal": self.wal.as_dict(),
            "shm": self.shm.as_dict(),
        }


def fingerprint_database(database_path: str | Path) -> DatabaseFingerprint:
    """Fingerprint the main database file only.

    Diagnostic. Cannot detect a WAL-only commit -- use
    `read_consistent_snapshot` for the concurrency gate.
    """
    stat = Path(database_path).stat()
    return DatabaseFingerprint(
        size_bytes=stat.st_size,
        modified_time_ns=stat.st_mtime_ns,
    )


def fingerprint_sidecar(
    database_path: str | Path,
    suffix: str,
) -> SidecarFingerprint:
    """Fingerprint one sidecar next to `database_path`.

    A missing sidecar is reported as ``present=False`` rather than
    raising: absence is a normal, informative state, and this whole
    family of readings is diagnostic anyway.
    """
    sidecar = Path(str(Path(database_path)) + suffix)

    try:
        stat = sidecar.stat()
    except OSError:
        return SidecarFingerprint(
            suffix=suffix,
            present=False,
            size_bytes=None,
            modified_time_ns=None,
        )

    return SidecarFingerprint(
        suffix=suffix,
        present=True,
        size_bytes=stat.st_size,
        modified_time_ns=stat.st_mtime_ns,
    )


def fingerprint_database_files(
    database_path: str | Path,
) -> DatabaseFileFingerprint:
    """Fingerprint the main file and its ``-wal`` / ``-shm`` sidecars.

    Strictly more informative than `fingerprint_database` -- a WAL
    commit that leaves the main file identical usually does move the
    ``-wal`` file -- but still only diagnostic evidence: sidecars also
    move for reasons that are not commits (merely opening a WAL
    database read-only creates or touches ``-shm``), so this can no
    more be the concurrency gate than the main fingerprint can.
    """
    return DatabaseFileFingerprint(
        main=fingerprint_database(database_path),
        wal=fingerprint_sidecar(database_path, WAL_SUFFIX),
        shm=fingerprint_sidecar(database_path, SHM_SUFFIX),
    )


@contextmanager
def readonly_database_connection(
    database_path: str | Path,
) -> Iterator[sqlite3.Connection]:
    """Open `database_path` strictly read-only.

    The single implementation for every read-only audit in this
    codebase (there used to be five near-copies, which had already
    drifted on the `isolation_level` argument).

    Three deliberately layered safeguards:

    - The `mode=ro` SQLite URI flag opens the connection read-only at
      the OS/VFS level and refuses to create the file if it does not
      already exist (unlike a plain `sqlite3.connect`, which would
      silently create an empty database).
    - `PRAGMA query_only = ON` rejects any statement that would modify
      the database *at the statement level*, in case a future edit
      accidentally introduces a write.
    - `isolation_level=None` disables pysqlite's implicit transaction
      handling. This is required, not cosmetic: with the default
      isolation level the driver's own bookkeeping fights the explicit
      BEGIN/END that bracket a consistent snapshot.

    No migrations are applied and no schema is created: the database is
    read exactly as found, so this is safe to point at a live or
    protected backup database.
    """
    path = Path(database_path)

    # Checked before touching SQLite at all, so a missing path can
    # never result in a created file or directory.
    if not path.is_file():
        raise FileNotFoundError(f"Database does not exist: {path}")

    resolved = path.resolve(strict=True)
    uri = f"{resolved.as_uri()}?mode=ro"
    connection = sqlite3.connect(
        uri,
        uri=True,
        timeout=30.0,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row

    try:
        connection.execute("PRAGMA query_only = ON")
        yield connection
    finally:
        connection.close()


def data_version(connection: sqlite3.Connection) -> int:
    """SQLite's counter of commits made by *other* connections.

    Frozen for the duration of a read transaction on this connection,
    which is precisely why `read_consistent_snapshot` samples it
    outside and around the transaction: a difference between the two
    readings means someone else committed while the audit was reading.
    """
    return int(connection.execute("PRAGMA data_version").fetchone()[0])


def quick_check(connection: sqlite3.Connection) -> str:
    """``PRAGMA quick_check`` output, joined into a single string.

    'ok' means the database passed. Anything else is the error text
    SQLite produced, reported verbatim. A sufficiently corrupted
    database can make the pragma itself raise rather than return a
    non-'ok' row; either outcome has to be treated as an integrity
    failure, not an unhandled crash.
    """
    try:
        rows = connection.execute("PRAGMA quick_check").fetchall()
    except sqlite3.DatabaseError as exc:
        return f"error: {exc}"

    return "\n".join(str(row[0]) for row in rows)


@dataclass(frozen=True)
class ConsistentSnapshot(Generic[T]):
    """The result of a guarded read, plus its snapshot metadata.

    `result` is whatever the caller's read function returned. The rest
    is what the caller needs to put in its report so that the *claimed*
    guarantee matches the *actual* one.
    """

    database: Path
    result: T
    quick_check: str
    data_version_before: int
    data_version_after: int

    @property
    def data_version_unchanged(self) -> bool:
        """Always True for a snapshot that was returned rather than
        raised on -- kept so reports can state the gate affirmatively.
        """
        return self.data_version_before == self.data_version_after

    def report_fields(self) -> dict[str, Any]:
        """The snapshot half of a report's provenance block."""
        return {
            "quick_check": self.quick_check,
            "data_version_before": self.data_version_before,
            "data_version_after": self.data_version_after,
            "concurrent_commit_detected": not self.data_version_unchanged,
            "snapshot_guarantee": SNAPSHOT_GUARANTEE,
        }


def read_consistent_snapshot(
    database: str | Path,
    read: Callable[[sqlite3.Connection], T],
    *,
    context: str = "read",
    integrity_check: Callable[[sqlite3.Connection], str] = quick_check,
) -> ConsistentSnapshot[T]:
    """Run `read` against one provably-consistent read-only snapshot.

    `read` is called with an open, strictly read-only connection inside
    a deferred transaction, and may issue as many queries as it likes:
    they all see the same snapshot, so counts derived from different
    queries can never disagree because a writer landed between two of
    them.

    `context` only shapes the error message ("during the {context}").

    `integrity_check` defaults to this module's `quick_check` and
    exists so a caller can pass its own module-level binding: the
    integrity read runs *inside* the guarded window, and the audits'
    WAL regression tests wrap it to commit from another connection at
    exactly that point.

    Raises:

    - `FileNotFoundError` if `database` does not exist (checked before
      SQLite is touched, so nothing can be created);
    - `DatabaseIntegrityError` if ``PRAGMA quick_check`` did not return
      'ok' (checked inside the transaction, so it is covered by the
      change-detection window like every other read);
    - `DatabaseChangedError` if ``PRAGMA data_version`` moved across
      the transaction -- the authoritative concurrency gate, and the
      only one that holds under WAL.

    Anything `read` itself raises propagates unchanged, after the read
    transaction has been closed.
    """
    resolved = Path(database)

    if not resolved.is_file():
        raise FileNotFoundError(f"Database does not exist: {resolved}")

    resolved = resolved.resolve(strict=True)

    with readonly_database_connection(resolved) as connection:
        # Sampled *outside* and *before* the transaction so the
        # change-detection window covers every read the report depends
        # on -- including quick_check. Sampling it after quick_check
        # would leave that read outside the window, and a WAL commit
        # landing there would go undetected: a WAL write can touch only
        # the -wal file, leaving the main database's size and mtime
        # identical, so no fingerprint comparison can catch it either.
        version_before = data_version(connection)

        # One deferred read transaction: every observation below comes
        # from the same snapshot.
        connection.execute("BEGIN")

        try:
            integrity = integrity_check(connection)

            if integrity != "ok":
                raise DatabaseIntegrityError(
                    f"PRAGMA quick_check failed for {resolved}: {integrity}"
                )

            result = read(connection)
        finally:
            # A read transaction still has to be ended; END is not a
            # write and is permitted under query_only. Wrapped because
            # a corrupt database can leave the connection unusable, and
            # the integrity result already captured above is what
            # matters -- not this cleanup.
            try:
                connection.execute("END")
            except sqlite3.DatabaseError:
                pass

        version_after = data_version(connection)

    if version_before != version_after:
        raise DatabaseChangedError(
            "Another connection committed to the database during the "
            f"{context} (data_version {version_before} -> "
            f"{version_after}); the report would mix pre- and "
            "post-change observations and is not trustworthy."
        )

    return ConsistentSnapshot(
        database=resolved,
        result=result,
        quick_check=integrity,
        data_version_before=version_before,
        data_version_after=version_after,
    )


def fingerprint_report_fields(
    *,
    fingerprint_before: DatabaseFingerprint,
    fingerprint_after: DatabaseFingerprint,
    files_before: DatabaseFileFingerprint | None = None,
    files_after: DatabaseFileFingerprint | None = None,
) -> dict[str, Any]:
    """The fingerprint half of a report's provenance block.

    Emits the historical flat keys unchanged (existing reports and
    their consumers key off them), plus WAL-sidecar evidence, plus the
    labelling that keeps the reported guarantee honest:
    `database_unchanged` is retained verbatim but is now always
    accompanied by `database_unchanged_is_diagnostic_only: true` and
    `FINGERPRINT_DIAGNOSTIC_NOTE`, and by the `database_file_unchanged`
    alias whose name says what was actually compared.

    Sidecar evidence is reported as *observed presence*, not as raw
    sizes and mtimes -- see `SIDECAR_DIAGNOSTIC_NOTE` for why a
    read-only run makes the raw readings both meaningless and
    non-deterministic. Presence is the part that actually matters to a
    reader: it says "this database is in WAL mode, so the fingerprint
    above cannot see another connection's commit".
    """
    fields: dict[str, Any] = {
        "database_size_bytes_before": fingerprint_before.size_bytes,
        "database_size_bytes_after": fingerprint_after.size_bytes,
        "database_modified_time_ns_before": (
            fingerprint_before.modified_time_ns
        ),
        "database_modified_time_ns_after": fingerprint_after.modified_time_ns,
        # Same name and same value as before this module existed, so
        # nothing that reads these reports breaks -- but never again
        # presented as the concurrency guarantee.
        "database_unchanged": fingerprint_after == fingerprint_before,
        "database_file_unchanged": fingerprint_after == fingerprint_before,
        "database_unchanged_is_diagnostic_only": True,
        "fingerprint_diagnostic_note": FINGERPRINT_DIAGNOSTIC_NOTE,
    }

    samples = [
        sample for sample in (files_before, files_after) if sample is not None
    ]

    if samples:
        fields["database_wal_sidecar_observed"] = any(
            sample.wal.present for sample in samples
        )
        fields["database_shm_sidecar_observed"] = any(
            sample.shm.present for sample in samples
        )
        fields["sidecar_diagnostic_note"] = SIDECAR_DIAGNOSTIC_NOTE

    return fields
