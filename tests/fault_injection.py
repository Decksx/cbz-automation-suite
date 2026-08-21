"""A reusable harness for injecting faults at existing seams.

Every helper here fails *deterministically* at a chosen call, rather than
racing two threads and hoping. A race that reproduces 11 times in 16 is not
a regression test; it is a flaky one that will eventually be deleted by
someone who cannot reproduce it. The 2026-08-02 same-size-replacement
finding was originally measured by racing and is now pinned deterministically
(see `tests/test_rewrite_content_guard.py`), and this module generalises that
approach so the next fault does not need its own bespoke scaffolding.

Nothing here patches production code permanently or requires production code
to grow a test hook. Each helper wraps a seam that already exists: a
module-level attribute, `Path.stat`, `os.replace`, or a SQL statement issued
through `sqlite3.Connection.execute`.

`sqlite3.Connection` is a C type whose methods cannot be monkeypatched, which
is why `failing_execute` wraps a connection in a proxy rather than patching
the class -- the obvious approach raises `TypeError` and the workaround is
easy to get subtly wrong.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator


class InjectedFailure(RuntimeError):
    """Raised by an injected fault.

    A dedicated type so a test can assert it caught *its own* injected
    failure rather than an unrelated error that happened to surface at the
    same point -- which is how a fault-injection test quietly stops testing
    anything.
    """


@contextmanager
def fail_after_calls(
    monkeypatch,
    target: Any,
    name: str,
    *,
    after: int = 0,
    exception: type[BaseException] = InjectedFailure,
) -> Iterator[dict[str, int]]:
    """Let `after` calls of `target.name` through, then raise.

    Yields a counter dict so a test can assert the failure fired at all.
    A fault that never triggers leaves the code path passing for the wrong
    reason, so the count is worth checking explicitly.
    """
    original = getattr(target, name)
    state = {"calls": 0}

    def wrapper(*args, **kwargs):
        state["calls"] += 1

        if state["calls"] > after:
            raise exception(
                f"injected failure at {name} call {state['calls']}"
            )

        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, wrapper)
    yield state


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

    monkeypatch.setattr(Path, "stat", patched)
    yield


@contextmanager
def failing_replace(
    monkeypatch, *, after: int = 0
) -> Iterator[dict[str, int]]:
    """Make `os.replace` fail, simulating a crash before the final rename.

    The read-rebuild rewrite paths write a temporary file and then rename it
    over the original. Failing the rename is the closest deterministic stand-in
    for the process dying at its most dangerous moment: after the new bytes
    exist, before they are the ones anybody reads.
    """
    original = os.replace
    state = {"calls": 0}

    def wrapper(src, dst, *args, **kwargs):
        state["calls"] += 1

        if state["calls"] > after:
            raise InjectedFailure(
                f"injected failure replacing {src} -> {dst}"
            )

        return original(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", wrapper)
    yield state


@contextmanager
def failing_path_rename(
    monkeypatch, *, after: int = 0
) -> Iterator[dict[str, int]]:
    """Make `Path.rename` fail after `after` successful calls.

    Separate from `failing_replace` because the rewrite paths use
    `Path.rename` rather than `os.replace`, and patching the wrong one
    yields a test that passes because the fault never fired. `after` matters
    here: `write_comicinfo` renames twice -- original to backup, then temp to
    original -- and failing at each leaves the filesystem in a different
    state.
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

    monkeypatch.setattr(Path, "rename", wrapper)
    yield state


@contextmanager
def observed_calls(
    monkeypatch, target: Any, name: str
) -> Iterator[list[tuple]]:
    """Record calls to `target.name` without changing behaviour.

    Used to assert a dry run reached the point of *deciding* to write and
    then did not write -- which is a different, stronger claim than the file
    merely being unchanged, since a run that silently did nothing at all
    would also leave it unchanged.
    """
    original = getattr(target, name)
    calls: list[tuple] = []

    def wrapper(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, wrapper)
    yield calls


def database_snapshot(
    connection: sqlite3.Connection, tables: tuple[str, ...]
) -> dict[str, Any]:
    """Row counts plus `data_version`, for before/after comparison.

    `data_version` is included because row counts alone cannot see a write
    that was made and then undone by an equal and opposite write, and
    cannot see changes to rows that leave the count identical.
    """
    snapshot: dict[str, Any] = {
        "data_version": connection.execute(
            "PRAGMA data_version"
        ).fetchone()[0]
    }

    for table in tables:
        snapshot[table] = connection.execute(
            f"SELECT COUNT(*) FROM {table}"
        ).fetchone()[0]

    return snapshot


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
