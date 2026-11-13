"""Series folders whose catalog events fall inside a discovery batch's bounds (#158 B2, part B).

Read only. A completed batch carries persisted file_events watermarks
(``start_event_id``/``end_event_id``, part A). This module turns the events
between those bounds into the series folders a scoped plan should cover, so a
daily plan reads only what the watcher or decensoring touched instead of every
byte of the library.

The bounds describe events **recorded in the catalog**, not everything that
may have changed on disk; a fresh discovery is still how a change reaches the
catalog. Event ids, not timestamps, bound the selection: codex-lead showed a
clock rollback hiding a new event and same-second batches re-selecting an
older one (issue #158 review).

Two meanings, kept explicit:

* ``since=False`` ("that batch"): ``start_event_id < id <= end_event_id``.
* ``since=True`` ("since that batch"): ``start_event_id < id <= upper``, where
  ``upper`` is the highest event id in the same read snapshot.

A batch that cannot bound its own coverage is refused, never defaulted:
missing or malformed bounds (every batch recorded before part A), any status
but ``completed``, a different library root, errors during the scan, a
resumed scan (it skips missing-file detection) and a limited scan.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from comic_automation.database.read_guards import DatabaseChangedError, quick_check, read_consistent_snapshot
from comic_automation.library.discovery import normalize_library_path

EVENT_TYPES = ("discovered", "changed", "restored", "missing")


class ChangedScopesRefused(ValueError):
    """The batch cannot bound a trustworthy changed-series selection."""


@dataclass(frozen=True)
class ChangedScopes:
    """A bounded selection; every omission stays visible beside ``planned``."""

    batch_id: int
    root: str
    mode: str
    start_event_id: int
    upper_event_id: int
    events: int
    planned: list[str] = field(default_factory=list)
    gone: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    root_level: list[str] = field(default_factory=list)
    outside_root: int = 0

    @property
    def omitted(self) -> list[str]:
        """Changed places this selection could not plan: unreadable series
        folders and changed root-level books (only an unscoped plan covers
        those). ``gone`` and ``excluded`` hold nothing plannable;
        ``outside_root`` counts events under other library roots."""
        return [*self.unavailable, *self.root_level]

    def as_dict(self) -> dict:
        """JSON-ready form, bound into plan provenance before the digest."""
        return asdict(self)


def _key(path: str | Path) -> str:
    return str(normalize_library_path(path)).replace("/", "\\").casefold()


def _bound(details: dict, name: str) -> int:
    value = details.get(name)
    if type(value) is not int or value < 0:
        raise ChangedScopesRefused(
            f"batch has no valid {name}; run a fresh complete discovery (or a full plan)")
    return value


def _coverage(details: dict) -> None:
    """Refuse a batch whose own scan was not complete; never assume it was."""
    errors, resumed, limited = details.get("errors"), details.get("resumed"), details.get("limited")
    if type(errors) is not int or type(resumed) is not bool or type(limited) is not bool:
        raise ChangedScopesRefused("batch coverage metadata is missing or malformed; run a fresh complete discovery")
    if errors:
        raise ChangedScopesRefused(f"batch recorded {errors} scan error(s); run a fresh complete discovery")
    if resumed:
        raise ChangedScopesRefused("batch was resumed (missing-file detection skipped); run a fresh complete discovery")
    if limited:
        raise ChangedScopesRefused("batch was limited; run a fresh complete discovery")


def _classify(root: Path, name: str, internal) -> str:
    """Group one series folder name taken from a resolved event path.

    `normalize_library_path` resolves every event path, so ``name`` is
    already the on-disk spelling of a folder that still exists (the planner
    requires exact spellings) and any link has already been followed: an
    event under a junction or symlink counts toward its target, or toward
    ``outside_root`` when the target is elsewhere.
    """
    if name.casefold() in internal or name.casefold() == "ai-decensor-originals":
        return "excluded"
    try:
        with os.scandir(root / name) as entries:
            next(entries, None)
    except FileNotFoundError:
        return "gone"
    except OSError:
        # Unreadable, or a file now stands where the folder was
        # (NotADirectoryError): changed, but not plannable.
        return "unavailable"
    return "planned"


def changed_scopes(database: str | Path, library_root: str | Path, *, batch_id: int,
                   since: bool, internal=frozenset()) -> ChangedScopes:
    """Select the series folders with catalog events inside the batch bounds.

    One WAL-aware read through `read_consistent_snapshot` (strictly
    read-only, ``quick_check``, ``data_version`` around one deferred
    transaction); a concurrent catalog commit refuses the selection.
    ``internal`` names folders the planner never plans (its ``_INTERNAL``).
    """
    if type(batch_id) is not int or batch_id < 1:
        raise ChangedScopesRefused("batch id must be a positive integer")
    root = normalize_library_path(library_root)

    def read(connection) -> tuple[int, int, list[str]]:
        row = connection.execute(
            "SELECT source_path, status, details_json FROM source_batches WHERE id = ?", (batch_id,)).fetchone()
        if row is None:
            raise ChangedScopesRefused(f"no discovery batch {batch_id}")
        source_path, status, details_json = row
        if status != "completed":
            raise ChangedScopesRefused(f"batch {batch_id} is {status!r}, not completed")
        if _key(source_path) != _key(root):
            raise ChangedScopesRefused(f"batch {batch_id} scanned {source_path}, not {root}")
        try:
            details = json.loads(details_json)
        except (TypeError, ValueError) as exc:
            raise ChangedScopesRefused(f"batch {batch_id} details are unreadable") from exc
        if not isinstance(details, dict):
            raise ChangedScopesRefused(f"batch {batch_id} details are unreadable")
        start, end = _bound(details, "start_event_id"), _bound(details, "end_event_id")
        if end < start:
            raise ChangedScopesRefused(f"batch {batch_id} bounds are out of order")
        _coverage(details)
        latest = connection.execute("SELECT COALESCE(MAX(id), 0) FROM file_events").fetchone()[0]
        if latest < end:
            raise ChangedScopesRefused(f"batch {batch_id} end marker is beyond the event log")
        upper = latest if since else end
        marks = ",".join("?" * len(EVENT_TYPES))
        paths = [r[0] for r in connection.execute(
            f"SELECT source_path FROM file_events WHERE id > ? AND id <= ? AND event_type IN ({marks})"
            " AND source_path IS NOT NULL ORDER BY id", (start, upper, *EVENT_TYPES))]
        return start, upper, paths

    try:
        # quick_check is passed as this module's binding so a test can
        # commit from another connection inside the guarded window.
        start, upper, paths = read_consistent_snapshot(
            database, read, context="changed-series selection", integrity_check=quick_check).result
    except DatabaseChangedError as exc:
        raise ChangedScopesRefused("the catalog changed while the selection was read; run it again") from exc

    root_key = _key(root)
    prefix = root_key.rstrip("\\") + "\\"
    scopes: dict[str, str] = {}
    root_level: set[str] = set()
    outside = 0
    for path in paths:
        key = _key(path)
        if not key.startswith(prefix):
            outside += 1
            continue
        parts = Path(str(normalize_library_path(path))).parts[len(root.parts):]
        if len(parts) == 1:
            root_level.add(parts[0])
        elif parts:
            scopes.setdefault(parts[0].casefold(), parts[0])
    groups = {"planned": [], "gone": [], "unavailable": [], "excluded": []}
    internal = {name.casefold() for name in internal}
    for name in sorted(scopes.values(), key=str.casefold):
        groups[_classify(root, name, internal)].append(name)
    return ChangedScopes(batch_id=batch_id, root=str(root), mode="since" if since else "batch",
                         start_event_id=start, upper_event_id=upper, events=len(paths),
                         root_level=sorted(root_level, key=str.casefold), outside_root=outside, **groups)
