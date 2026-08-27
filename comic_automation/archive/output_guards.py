"""Shared output-path protection for read-only planners.

Every read-only planner in this package has the same hole at the same place:
the read-only guarantee stops at the connection. `mode=ro` plus
`PRAGMA query_only` make it impossible for the *reader* to modify the
database, and none of that survives contact with an output path that names
the database, one of its sidecars, or an alias of either -- the guarded read
closes cleanly and the report writer then truncates the file the plan
describes. The read-only claim stays true and the database is still gone.

This module holds the path logic that closes that hole, factored out of
`revision_retention_cli` where it was written and proven. It deliberately
exports **predicates and enumerations, not refusals**: each caller raises its
own exception type with its own message, because `revision_retention` and
`provenance_backfill_planner` have separate error hierarchies and a shared
refusal would have to pick one. Sharing the logic is what matters; sharing
the wording is not.

Nothing here requires a path to exist. That is the property the whole module
turns on -- an output file is checked *before* it is created, so every
comparison has to work against a name rather than against a file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Sequence


def path_identity(path: str | Path) -> str:
    """A comparable identity for a path that may not exist yet.

    `realpath` resolves symlinks, junctions and `..` segments; `normcase`
    folds case and separators, which matters because this repository's
    library volumes are case-insensitive and `X:/a` and `x:\\a` are one file.

    Neither operation requires the path to exist, and that is the point.
    `Path.resolve(strict=True)` would raise on the ordinary case -- an output
    file that has not been created yet -- and `Path.resolve(strict=False)`
    on a **broken** symlink returns the link's own path rather than its
    target, which is precisely the escape this function exists to close: a
    link in an allowed directory pointing at a nonexistent file beside the
    database resolves to the link, passes a parent-directory check, and then
    creates the target on write.
    """
    return os.path.normcase(os.path.realpath(str(path)))


def resolved_parent(path: str | Path) -> str:
    """The identity of the directory a write to `path` would land in.

    Derived from the resolved target, never from the typed path's lexical
    parent. `Path(...).parent` on `allowed/link.json` answers `allowed/` no
    matter where `link.json` points, so a directory check built on it is
    answering a question about the name rather than about the write.
    """
    return os.path.normcase(os.path.dirname(os.path.realpath(str(path))))


def same_file(left: str | Path, right: str | Path) -> bool:
    """True when two paths reach the same file.

    Two tests, because neither alone is sufficient. The textual comparison
    catches paths that do not exist yet, which is the ordinary case for an
    output file. `os.path.samefile` catches what text cannot: hard links and
    distinct paths onto the same volume that resolve differently but share an
    inode or file index. It needs both files to exist, so it only supplements.
    """
    if path_identity(left) == path_identity(right):
        return True

    try:
        return os.path.samefile(str(left), str(right))
    except OSError:
        # One of them does not exist, or is not stat-able. The textual test
        # above has already had its say; an unreadable path is not evidence
        # of sameness.
        return False


def protected_database_paths(
    database: str | Path,
    extra: Sequence[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """Every path an output must not collide with, and what each one is.

    Deduplicated by resolved identity, and returned rather than consumed in
    place so the set itself is inspectable. A caller raises on its first
    match, which means a duplicated entry has no observable effect there --
    the deduplication is only checkable by looking at the list, so the list is
    something you can look at.

    The WAL and SHM sidecars are included. They are not the database file, but
    truncating either one destroys uncommitted state or forces recovery, and
    an operator who typed one of those paths did not mean to.

    Sidecars are derived from **both** the typed path and its fully resolved
    target, and that is not belt-and-braces. `read_guards` opens the database
    through `path.resolve(strict=True)`, so when the typed path is a symlink
    or sits under a junction, SQLite works against the resolved file and puts
    its WAL and SHM beside *that* -- while a sidecar name built by
    concatenating onto the typed path names a different, quite possibly
    nonexistent, file. Protecting only the typed form leaves the real WAL
    unguarded: it matches neither the typed sidecar nor the database itself,
    `samefile` cannot help while it does not yet exist, and the writers'
    SQLite-header check does not recognise a WAL or SHM file either, since
    neither begins with the database magic. Every layer misses it, so the
    resolved names are enumerated here.

    For an ordinary path the two derivations name the same files and collapse
    to three entries; only a link makes them five.

    `extra` carries caller-specific inputs -- a pin manifest, a prior plan --
    in the same (path, description) shape, so they are deduplicated and
    described by the same machinery rather than checked separately.
    """
    database = str(database)
    protected: list[tuple[str, str]] = [(database, "the database being read")]

    # os.path.realpath rather than Path.resolve: it does not raise on a
    # missing path, and the database's existence is SQLite's to complain
    # about, with a better message than this function could give.
    for base in (database, os.path.realpath(database)):
        protected.extend(
            (base + suffix, "a database sidecar")
            for suffix in ("-wal", "-shm")
        )

    if extra:
        protected.extend(extra)

    return deduplicate_by_identity(protected)


def deduplicate_by_identity(
    entries: Iterable[tuple[str, str]]
) -> list[tuple[str, str]]:
    """Collapse (path, description) pairs that name the same file.

    First occurrence wins, which is why callers put the operator's own typed
    path first: a collision is then reported under the name they wrote rather
    than under a resolved form they have never seen.
    """
    seen: set[str] = set()
    deduplicated: list[tuple[str, str]] = []

    for path, description in entries:
        key = path_identity(path)

        if key in seen:
            continue

        seen.add(key)
        deduplicated.append((path, description))

    return deduplicated
