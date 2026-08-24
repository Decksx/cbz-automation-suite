"""A reusable harness for injecting faults at existing seams.

Every helper here fails *deterministically* at a chosen call, rather than
racing two threads and hoping. A race that reproduces 11 times in 16 is not
a regression test; it is a flaky one that will eventually be deleted by
someone who cannot reproduce it. The 2026-08-02 same-size-replacement
finding was originally measured by racing and is now pinned deterministically
(see `tests/test_rewrite_content_guard.py`), and this module generalises that
approach so the next fault does not need its own bespoke scaffolding.

Nothing here patches production code permanently or requires production code
to grow a test hook. Each helper wraps a seam that already exists: `Path.stat`,
`Path.rename`, or a SQL statement issued through `sqlite3.Connection.execute`.

Patches are undone on block exit
--------------------------------

Every context manager scopes its patching through `monkeypatch.context()`,
so the patch is reverted when the `with` block ends rather than surviving
until pytest tears the fixture down at the end of the test. The earlier
version of this module patched before `yield` and never undid it, which
silently contradicted the `with` API: a test that injected a fault, exited
the block and then went on to assert something about normal behaviour was
still running against the patched function. Restoration is asserted by
`test_archive_fault_injection.py` rather than left to inspection.

`sqlite3.Connection` is a C type whose methods cannot be monkeypatched, which
is why `failing_execute` wraps a connection in a proxy rather than patching
the class -- the obvious approach raises `TypeError` and the workaround is
easy to get subtly wrong.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class InjectedFailure(RuntimeError):
    """Raised by an injected fault.

    A dedicated type so a test can assert it caught *its own* injected
    failure rather than an unrelated error that happened to surface at the
    same point -- which is how a fault-injection test quietly stops testing
    anything.
    """


class _FailingExecuteConnection:
    """A connection proxy that fails on a chosen SQL statement.

    Wraps rather than subclasses, because `sqlite3.Connection` cannot have
    attributes set on it and cannot be reliably subclassed for this purpose.
    Everything except `execute` is delegated untouched, so the object still
    behaves as a connection for the code under test.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        fail_on: str,
        after: int = 0,
    ) -> None:
        self._connection = connection
        self._fail_on = fail_on.casefold()
        self._after = after
        self.matches = 0

    def execute(self, sql: str, *args, **kwargs):
        if self._fail_on in sql.casefold():
            self.matches += 1

            if self.matches > self._after:
                raise InjectedFailure(
                    f"injected failure on statement matching "
                    f"{self._fail_on!r} (match {self.matches})"
                )

        return self._connection.execute(sql, *args, **kwargs)

    def __getattr__(self, item: str):
        return getattr(self._connection, item)


def failing_execute(
    connection: sqlite3.Connection, *, fail_on: str, after: int = 0
) -> _FailingExecuteConnection:
    """Wrap `connection` so statements containing `fail_on` raise.

    Matching is a case-insensitive substring test on the SQL text, which is
    enough to single out `COMMIT`, `INSERT INTO schema_migrations`, or a
    named table without needing a parser.

    Not a context manager: it patches nothing, it returns a different object.
    The caller decides what to hand to the code under test.
    """
    return _FailingExecuteConnection(
        connection, fail_on=fail_on, after=after
    )


@contextmanager
def frozen_stat(monkeypatch, target: Path) -> Iterator[None]:
    """Freeze `target`'s stat result while letting every other path through.

    Reproduces a coarse filesystem timestamp deterministically: the file's
    contents really do change, but size and mtime appear not to. This is the
    exact blind spot the content-based guards exist to cover, and freezing it
    is what turns a probabilistic race into a repeatable assertion.
    """
    original_stat = Path.stat
    frozen = original_stat(target)

    # Compared by direct equality, never by `resolve()`. `Path.resolve()`
    # calls `Path.stat()` internally, so resolving inside the patch
    # re-enters it and recurses until the interpreter gives up. This is the
    # same comparison the existing `_freeze_stat` in
    # `test_rewrite_content_guard.py` uses, for the same reason.
    def patched(self: Path, *args, **kwargs):
        if self == target:
            return frozen

        return original_stat(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "stat", patched)
        yield


@contextmanager
def failing_path_rename(
    monkeypatch, *, after: int = 0
) -> Iterator[dict[str, int]]:
    """Make `Path.rename` fail after `after` successful calls.

    Chosen over `os.replace` because the rewrite paths use `Path.rename`;
    patching the wrong one yields a test that passes because the fault never
    fired. `after` matters here: `write_comicinfo` renames twice -- original
    to backup, then temp to original -- and failing at each leaves the
    filesystem in a different state.

    Yields a counter so a test can assert the failure actually fired. A
    fault that never triggers leaves the code path passing for the wrong
    reason.
    """
    original = Path.rename
    state = {"calls": 0}

    def wrapper(self: Path, target, *args, **kwargs):
        state["calls"] += 1

        if state["calls"] > after:
            raise InjectedFailure(
                f"injected failure renaming {self} -> {target} "
                f"(call {state['calls']})"
            )

        return original(self, target, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", wrapper)
        yield state


# --- snapshots -----------------------------------------------------------


def table_snapshot(
    connection: sqlite3.Connection, tables: tuple[str, ...]
) -> dict[str, Any]:
    """Row counts plus a content digest for each table.

    Deliberately does **not** include `PRAGMA data_version`.
    `data_version` only changes when *another* connection commits; a write
    made and committed by the very connection being sampled leaves it
    untouched. Reporting it beside these counts implied a same-connection
    mutation check that it cannot provide, and the control test passed on
    the row count alone while appearing to validate the pragma.

    The content digest covers the case row counts cannot see: a row updated
    in place, or one deleted and another inserted. `observer_data_version`
    is the honest tool for the cross-connection question.
    """
    snapshot: dict[str, Any] = {}

    for table in tables:
        rows = connection.execute(
            f"SELECT * FROM {table} ORDER BY rowid"
        ).fetchall()
        digest = hashlib.sha256()

        for row in rows:
            digest.update(repr(tuple(row)).encode("utf-8"))

        snapshot[table] = (len(rows), digest.hexdigest())

    return snapshot


@contextmanager
def observer_data_version(database: Path) -> Iterator[Any]:
    """A *persistent* second connection that reports `PRAGMA data_version`.

    The observer connection has to stay open across the writes it is meant
    to observe. `data_version` is per-connection state: a freshly opened
    connection reports the current value with nothing to compare it to, so
    sampling through a new connection each time returns the same number
    either side of a commit and detects nothing. The first version of this
    helper did exactly that and its test failed, which is the only reason
    the distinction is documented here rather than being rediscovered.

    Yields a zero-argument callable so the caller samples explicitly at the
    points it cares about.
    """
    connection = sqlite3.connect(str(database))

    try:
        yield lambda: connection.execute(
            "PRAGMA data_version"
        ).fetchone()[0]
    finally:
        connection.close()


def file_snapshot(paths: list[Path]) -> dict[str, tuple[int, bytes]]:
    """Size and full bytes for each path, for exact non-mutation checks.

    Full bytes rather than a digest: these fixtures are small, and a
    mismatch is far easier to diagnose when the actual content is available
    rather than two hex strings that differ.
    """
    return {
        str(path): (path.stat().st_size, path.read_bytes())
        for path in paths
        if path.exists()
    }
