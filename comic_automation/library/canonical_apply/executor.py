"""`apply`, `recover` and `undo` with attempt ownership (§5.3, §5.4).

Every run is an **attempt** with a unique id, its own fsynced journal
(`journal`) and its own database markers (`markers`), and every run holds
the library root's OS scope lock (`scope_lock`) from before its first read
of the journal until it returns.

The step protocol (§5.4), per filesystem step
---------------------------------------------

1. ``intent`` journal record, fsynced;
2. ``os.rename(src, dst)`` -- never ``os.replace``: on Windows it refuses an
   existing destination, which is the execution-time occupancy check;
3. ``done`` journal record, fsynced: the ownership receipt for ``dst``;
4. one SQLite transaction (`markers.commit_step`): the conditional
   ``file_locations`` and pairing updates, each ``WHERE id = ? AND path =
   <preimage>`` and required to affect exactly one row, plus the
   ``rename_attempt_steps`` marker; the destination is re-verified *after*
   the write lock is held;
5. ``confirmed`` journal record, fsynced.

Ownership is the receipt, never content: a destination that holds the
right bytes but has no ``done`` receipt is ambiguous and is refused.

Units and phases (§5.3)
-----------------------

A family is one unit and any failure blocks the whole of it. Phase 1
validates every member at its journal-proven current location against its
strong identity and re-derives every approved relation from the bytes;
phase 2 renames the keeper (when the plan does) and freshly validates it at
the result; phase 3 re-validates keeper and candidate before each
quarantine. An ordinary rename is a single-member unit. Validation runs
after the unit gate returns (a lock wait) and the quiescence probe is
re-checked at every batch boundary, along with a run-wide drift fraction
that stops the run early as an *additional* stop.

Strong identity and relations come from step 5
(`comic_automation.archive.supersession`): `derive_archive` binds the
sha256, pages and non-image members to one owned byte source, and
`compare_pair` re-derives the §4.4 relation. Nothing here re-implements
either.

Recovery (§5.4, "Recovery works on the action's frontier")
------------------------------------------------------------

`recover` validates each action's chain as prefix-closed (`chain_state`),
treats completed steps as history, finds the latest proven location,
checks it with fresh content and the database rows, and resolves only the
frontier step through the §5.4 matrix. Every state the matrix does not
list is refused. Each resolution advances the frontier by one record and
the next round re-derives everything from scratch, so recovery is
idempotent. Recovery never starts a step that has no ``intent``; an
``intent``-only step whose source is intact is reported as not done and is
retried by ``apply --resume`` under full unit validation.

Undo is a new attempt that plans the reverse chain of fully confirmed
forward actions and runs it through the same protocol.

Crash injection
---------------

``fault`` is called at named boundaries (`FAULT_POINTS`) with a context
mapping. Tests raise from it to simulate a process dying at that point. It
is a keyword of the library functions only; the CLI never passes it.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from comic_automation.archive.supersession import (
    OUTCOME_SUPERSEDED,
    DerivedArchive,
    compare_pair,
    derive_archive,
)
from comic_automation.library.canonical_apply import journal as jn
from comic_automation.library.canonical_apply.markers import (
    DatabaseView,
    MarkerError,
    PairingColumn,
    PairingRef,
    StepRefused,
    commit_step,
    read_view,
)
from comic_automation.library.canonical_apply.plan import (
    Action,
    FamilyUnit,
    Plan,
    RenameUnit,
    Step,
)
from comic_automation.library.canonical_apply.scope_lock import (
    ScopeLock,
    require_windows,
)

Fault = Callable[[str, Mapping[str, Any]], None]
Quiescence = Callable[[Path], "str | None"]
UnitGate = Callable[[str], None]

FAULT_POINTS = (
    "before_intent",
    "after_intent",
    "after_rename",
    "after_done",
    "after_db_lock",
    "after_db_commit",
    "after_confirmed",
    "after_keeper_rename",
    "after_dependant",
)

DEFAULT_BATCH_SIZE = 25
# Stop the run once more than this fraction of the units processed so far
# was blocked by drift. An additional stop, never a substitute for the
# per-unit validation (§5.3). Conservative, not calibrated.
DEFAULT_MAX_DRIFT_FRACTION = 0.05

UNIT_COMPLETED = "completed"
UNIT_ALREADY_COMPLETE = "already_complete"
UNIT_BLOCKED = "blocked"
UNIT_NOT_REACHED = "not_reached"

RES_NOTHING = "nothing_to_do"
RES_COMPLETE = "complete"
RES_BOUNDARY = "at_step_boundary"
RES_NOT_DONE = "not_done_retryable"
RES_DB_COMMITTED = "db_committed"
RES_CONFIRMED = "confirmed_written"
RES_REFUSED = "refused"


class ExecutorError(RuntimeError):
    """Base class for apply/recover/undo refusals."""


class Refused(ExecutorError):
    """Refused before anything was moved or written by this run."""


class RunStopped(ExecutorError):
    """The run stopped with a step in flight; recovery is required."""


class ChainRefused(ExecutorError):
    """An action's receipts and markers are not a prefix-closed chain."""


def _no_fault(point: str, context: Mapping[str, Any]) -> None:
    return None


def _no_gate(unit_id: str) -> None:
    return None


# --- filesystem facts --------------------------------------------------------


def _exact_file(path: str) -> bool:
    """A regular file whose directory entry has exactly this name.

    ``Path.exists`` is case-insensitive on Windows, so it cannot tell a
    case-only rename's source from its destination; the directory listing
    can.
    """
    parent, name = os.path.split(path)
    try:
        entries = os.listdir(parent)
    except OSError:
        return False
    return name in entries and os.path.isfile(path) and not os.path.islink(path)


def _occupied(path: str) -> bool:
    """Anything at this path in any letter case (what ``os.rename`` refuses)."""
    return os.path.lexists(path)


def _derive(path: str) -> DerivedArchive | None:
    if not _exact_file(path):
        return None
    return derive_archive(path)


def _identity_problem(path: str, sha256: str) -> str | None:
    """None when ``path`` holds exactly the bytes of ``sha256``."""
    derived = _derive(path)
    if derived is None:
        return f"{path} is not present"
    if derived.sha256 != sha256:
        return f"{path} has sha256 {derived.sha256}, approved {sha256}"
    return None


def _same_volume(source: str, destination: str) -> bool:
    return os.stat(source).st_dev == os.stat(os.path.dirname(destination)).st_dev


# --- chain state -------------------------------------------------------------


@dataclass(frozen=True)
class ChainState:
    """Where one action stands, from its receipts and markers alone.

    ``completed`` steps form a prefix with intent, done, marker and
    confirmed each; ``receipts`` are the frontier step's records (``()``,
    ``("intent",)`` or ``("intent", "done")``) and ``marker`` whether the
    frontier step has its database marker.
    """

    action_id: str
    steps: tuple[Step, ...]
    completed: int
    receipts: tuple[str, ...]
    marker: bool
    first_seq: int | None

    @property
    def complete(self) -> bool:
        return self.completed == len(self.steps)

    @property
    def started(self) -> bool:
        return self.completed > 0 or bool(self.receipts)

    @property
    def in_flight(self) -> bool:
        return bool(self.receipts)

    @property
    def frontier(self) -> Step | None:
        return None if self.complete else self.steps[self.completed]

    @property
    def location(self) -> str:
        """The latest proven location (§5.4 recovery step 3)."""
        if jn.KIND_DONE in self.receipts:
            return self.steps[self.completed].destination
        if self.completed:
            return self.steps[self.completed - 1].destination
        return self.steps[0].source


def chain_state(
    action: Action,
    steps: tuple[Step, ...],
    records: Sequence[jn.JournalRecord],
    view: DatabaseView,
) -> ChainState:
    """Validate one action's chain as prefix-closed, or raise `ChainRefused`.

    Every step before the frontier has intent, done, a DB marker and
    confirmed, in order; the frontier step has a prefix of intent, done
    (and a marker only once done exists); no later step has any receipt or
    marker. Every receipt must describe exactly the plan-derived step.
    """
    own = list(jn.step_records(tuple(records), action.action_id))
    for position, record in enumerate(own):
        expected_step = position // 3 + 1
        expected_kind = jn.STEP_KINDS[position % 3]
        if record.step != expected_step or record.kind != expected_kind:
            raise ChainRefused(
                f"{action.action_id}: journal record {record.seq} is "
                f"{record.kind} for step {record.step}, expected "
                f"{expected_kind} for step {expected_step}: a gap, reordering "
                "or receipt beyond the frontier"
            )
        if expected_step > len(steps):
            raise ChainRefused(
                f"{action.action_id}: receipt for step {expected_step} beyond "
                f"the {len(steps)}-step chain"
            )
        step = steps[expected_step - 1]
        payload = record.data
        if (
            payload.get("source") != step.source
            or payload.get("destination") != step.destination
            or payload.get("step_kind") != step.kind
            or payload.get("sha256") != action.sha256
            or payload.get("steps_total") != len(steps)
        ):
            raise ChainRefused(
                f"{action.action_id}: journal record {record.seq} does not "
                f"describe plan step {expected_step} ({step.source} -> "
                f"{step.destination})"
            )
    completed, remainder = divmod(len(own), 3)
    receipts = tuple(r.kind for r in own[len(own) - remainder:]) if remainder else ()

    markers = {
        step: marker
        for (action_id, step), marker in view.markers.items()
        if action_id == action.action_id
    }
    for number, marker in markers.items():
        if number < 1 or number > len(steps):
            raise ChainRefused(f"{action.action_id}: marker for step {number} beyond the chain")
        step = steps[number - 1]
        if (
            marker.preimage_path != step.source
            or marker.postimage_path != step.destination
            or marker.kind != step.kind
            or marker.location_id != action.location_id
        ):
            raise ChainRefused(
                f"{action.action_id}: marker for step {number} does not match the plan step"
            )
    for number in range(1, completed + 1):
        if number not in markers:
            raise ChainRefused(
                f"{action.action_id}: step {number} is confirmed but has no "
                "database marker"
            )
    frontier = completed + 1
    has_marker = frontier in markers
    if has_marker and jn.KIND_DONE not in receipts:
        raise ChainRefused(
            f"{action.action_id}: step {frontier} has a database marker but no "
            "done receipt"
        )
    beyond = [n for n in markers if n > frontier]
    if beyond:
        raise ChainRefused(f"{action.action_id}: markers beyond the frontier: {sorted(beyond)}")
    return ChainState(
        action_id=action.action_id,
        steps=steps,
        completed=completed,
        receipts=receipts,
        marker=has_marker,
        first_seq=own[0].seq if own else None,
    )


# --- database rows -----------------------------------------------------------


def _rows_at(view: DatabaseView, action: Action, path: str) -> bool:
    """The location row and every pairing row hold ``path``."""
    row = view.locations.get(action.location_id)
    if row is None or row.archive_id != action.archive_id or row.is_current != 1:
        return False
    if row.path != path:
        return False
    return all(view.pairings.get((p.table, p.row_id)) == path for p in action.pairings)


def _rows_state(view: DatabaseView, action: Action, pre: str, post: str) -> str:
    if _rows_at(view, action, pre):
        return "preimage"
    if _rows_at(view, action, post):
        return "postimage"
    return "neither"


# --- the run context -----------------------------------------------------------


@dataclass
class _Run:
    plan: Plan
    library_root: Path
    database: Path
    attempt_id: str
    registry: Mapping[str, PairingColumn]
    fault: Fault
    chains: dict[str, tuple[Step, ...]]
    actions: dict[str, Action]
    writer: jn.JournalWriter | None = None

    def records(self) -> tuple[jn.JournalRecord, ...]:
        return jn.read_journal(self.library_root, self.attempt_id)

    def view(self, actions: Sequence[Action], locations: Sequence[int] = ()) -> DatabaseView:
        """One guarded snapshot: this attempt's markers, the actions' rows,
        and any further ``locations`` (a keeper that is not renamed)."""
        refs: list[PairingRef] = [p for a in actions for p in a.pairings]
        return read_view(
            self.database,
            self.attempt_id,
            [a.location_id for a in actions] + list(locations),
            refs,
            self.registry,
        )

    def state(self, action: Action, records=None, view=None) -> ChainState:
        records = self.records() if records is None else records
        view = self.view([action]) if view is None else view
        return chain_state(action, self.chains[action.action_id], records, view)

    def note(self, note: str, **data: Any) -> None:
        assert self.writer is not None
        self.writer.append(jn.KIND_NOTE, {"note": note, **data})

    def _receipt(self, kind: str, action: Action, step: Step) -> None:
        assert self.writer is not None
        self.writer.append(
            kind,
            {
                "action_id": action.action_id,
                "steps_total": len(self.chains[action.action_id]),
                "archive_id": action.archive_id,
                "location_id": action.location_id,
                "sha256": action.sha256,
                **step.as_dict(),
            },
        )

    def _context(self, action: Action, step: Step) -> dict[str, Any]:
        return {"attempt_id": self.attempt_id, "action_id": action.action_id, **step.as_dict()}

    def run_step(self, action: Action, step: Step, *, intent_exists: bool = False) -> None:
        """The five-part step protocol for one filesystem step.

        ``intent_exists`` resumes a step whose ``intent`` is already
        durable (recovery found it not done): the same intent stands, since
        it was validated against the plan-derived step.
        """
        context = self._context(action, step)
        problem = _identity_problem(step.source, action.sha256)
        if problem is not None:
            if intent_exists:
                raise RunStopped(f"{action.action_id}: source changed before retry: {problem}")
            raise Refused(f"{action.action_id}: {problem}")
        try:
            same_volume = _same_volume(step.source, step.destination)
        except OSError as exc:
            raise Refused(f"{action.action_id}: cannot stat for the volume check: {exc}") from exc
        if not same_volume:
            # Renames never cross volumes (§5.4): a cross-volume os.rename
            # would fail anyway, and a copy is never a substitute.
            raise Refused(f"{action.action_id}: {step.destination} is on another volume")
        if not intent_exists:
            if _occupied(step.destination):
                raise Refused(
                    f"{action.action_id}: destination {step.destination} is occupied"
                )
            self.fault("before_intent", context)
            self._receipt(jn.KIND_INTENT, action, step)
        self.fault("after_intent", context)
        # Re-verified after the intent is durable and immediately before the
        # rename, so a source replaced in that window is never moved.
        problem = _identity_problem(step.source, action.sha256)
        if problem is not None:
            raise RunStopped(f"{action.action_id}: source changed after intent: {problem}")
        try:
            os.rename(step.source, step.destination)
        except OSError as exc:
            raise RunStopped(
                f"{action.action_id}: os.rename({step.source!r}, "
                f"{step.destination!r}) refused ({exc}); intent written, "
                "nothing moved -- run recover"
            ) from exc
        self.fault("after_rename", context)
        self._receipt(jn.KIND_DONE, action, step)
        self.fault("after_done", context)
        self.commit(action, step)
        self.fault("after_db_commit", context)
        self._receipt(jn.KIND_CONFIRMED, action, step)
        self.fault("after_confirmed", context)

    def commit(self, action: Action, step: Step) -> None:
        context = self._context(action, step)

        def revalidate() -> None:
            self.fault("after_db_lock", context)
            problem = _identity_problem(step.destination, action.sha256)
            if problem is not None:
                raise StepRefused(f"destination drifted under the write lock: {problem}")
            if _occupied(step.source):
                raise StepRefused(f"source {step.source} reappeared")

        try:
            commit_step(
                self.database,
                attempt_id=self.attempt_id,
                action_id=action.action_id,
                step=step.index,
                kind=step.kind,
                archive_id=action.archive_id,
                location_id=action.location_id,
                preimage=step.source,
                postimage=step.destination,
                pairings=action.pairings,
                registry=self.registry,
                revalidate=revalidate,
            )
        except (MarkerError, OSError) as exc:
            raise RunStopped(
                f"{action.action_id}: database step {step.index} refused and "
                f"rolled back ({exc}); the done receipt stands -- run recover"
            ) from exc


# --- recovery ------------------------------------------------------------------


@dataclass(frozen=True)
class Resolution:
    action_id: str
    status: str
    detail: str
    step: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"action_id": self.action_id, "status": self.status, "detail": self.detail, "step": self.step}


def _resolve_once(run: _Run, action: Action) -> Resolution:
    """One §5.4 matrix resolution for ``action``'s frontier, from scratch."""
    try:
        records = run.records()
        view = run.view([action])
        state = chain_state(action, run.chains[action.action_id], records, view)
    except (ChainRefused, jn.JournalError) as exc:
        return Resolution(action.action_id, RES_REFUSED, str(exc))
    if not state.started:
        return Resolution(action.action_id, RES_NOTHING, "no receipt for this action")

    location = state.location
    if state.complete or not state.in_flight:
        problem = _identity_problem(location, action.sha256)
        if problem is None and not _rows_at(view, action, location):
            problem = f"database rows are not at the proven location {location}"
        if problem is not None:
            return Resolution(action.action_id, RES_REFUSED, f"proven location drifted: {problem}")
        status = RES_COMPLETE if state.complete else RES_BOUNDARY
        return Resolution(action.action_id, status, f"at {location}", state.completed)

    step = state.frontier
    assert step is not None
    rows = _rows_state(view, action, step.source, step.destination)
    if state.receipts == (jn.KIND_INTENT,):
        source_present = _exact_file(step.source)
        destination_present = _occupied(step.destination)
        if source_present and not destination_present:
            problem = _identity_problem(step.source, action.sha256)
            if problem is not None:
                return Resolution(action.action_id, RES_REFUSED, f"source changed: {problem}; operator", step.index)
            if rows != "preimage":
                return Resolution(action.action_id, RES_REFUSED, "database rows are not at the step's preimage; operator", step.index)
            return Resolution(action.action_id, RES_NOT_DONE, "intent only, source intact: not done; apply --resume retries", step.index)
        if destination_present and not source_present:
            return Resolution(
                action.action_id, RES_REFUSED,
                f"ambiguous: {step.destination} exists but there is no done "
                "receipt, so it is not owned even if byte-identical; operator",
                step.index,
            )
        return Resolution(
            action.action_id, RES_REFUSED,
            f"intent only with source {'present' if source_present else 'absent'} "
            f"and destination {'present' if destination_present else 'absent'}; operator",
            step.index,
        )

    # done receipt present.
    problem = _identity_problem(step.destination, action.sha256)
    if problem is not None:
        return Resolution(action.action_id, RES_REFUSED, f"owned destination missing or changed: {problem}; operator", step.index)
    if _occupied(step.source):
        return Resolution(action.action_id, RES_REFUSED, f"source {step.source} is occupied again after the done receipt; operator", step.index)
    if not state.marker:
        if rows != "preimage":
            return Resolution(action.action_id, RES_REFUSED, f"database rows are {rows}, not at the preimage; operator", step.index)
        try:
            run.commit(action, step)
        except RunStopped as exc:
            return Resolution(action.action_id, RES_REFUSED, str(exc), step.index)
        return Resolution(action.action_id, RES_DB_COMMITTED, "owned destination: database step committed", step.index)
    if rows != "postimage":
        return Resolution(action.action_id, RES_REFUSED, f"marker present but database rows are {rows}; operator", step.index)
    run._receipt(jn.KIND_CONFIRMED, action, step)
    return Resolution(action.action_id, RES_CONFIRMED, "database already committed: confirmed written", step.index)


def _recover_actions(run: _Run, actions: Sequence[Action], *, single_round: bool) -> list[Resolution]:
    results = []
    for action in actions:
        resolution = _resolve_once(run, action)
        while not single_round and resolution.status in (RES_DB_COMMITTED, RES_CONFIRMED):
            resolution = _resolve_once(run, action)
        results.append(resolution)
    return results


# --- sessions --------------------------------------------------------------------


def _check_quiescent(quiescence: Quiescence, root: Path) -> None:
    reason = quiescence(root)
    if reason:
        raise Refused(f"library root {root} is not quiesced: {reason}")


def _unfinished_attempts(root: Path, exclude: str | None) -> list[str]:
    """Attempts with an action in flight or part-way along its chain.

    Decided from journals alone (every receipt names its chain length), so
    it needs no plan. A journal that cannot be read counts as unfinished.
    """
    unfinished = []
    for attempt_id in jn.list_attempts(root):
        if attempt_id == exclude:
            continue
        try:
            records = jn.read_journal(root, attempt_id)
        except jn.JournalError:
            unfinished.append(attempt_id)
            continue
        last: dict[str, jn.JournalRecord] = {}
        for record in records:
            if record.kind in jn.STEP_KINDS:
                last[record.action_id] = record
        for record in last.values():
            if record.kind != jn.KIND_CONFIRMED or record.step != record.data.get("steps_total"):
                unfinished.append(attempt_id)
                break
    return unfinished


@dataclass
class RunReport:
    """What one apply/undo/recover run did, serialisable for the operator."""

    attempt_id: str
    mode: str
    journal: str
    units: list[dict[str, Any]] = field(default_factory=list)
    resolutions: list[dict[str, Any]] = field(default_factory=list)
    stopped: str | None = None

    def unit(self, unit_id: str) -> dict[str, Any]:
        return next(u for u in self.units if u["unit_id"] == unit_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "mode": self.mode,
            "journal": self.journal,
            "units": self.units,
            "resolutions": self.resolutions,
            "stopped": self.stopped,
        }


# --- family and rename units -------------------------------------------------------


class _Blocked(Exception):
    """A unit failed validation before (or between) its actions."""


def _validate_rows(view: DatabaseView, action_or_member: Any, path: str, label: str) -> None:
    row = view.locations.get(action_or_member.location_id)
    if (
        row is None
        or row.archive_id != action_or_member.archive_id
        or row.is_current != 1
        or row.path != path
    ):
        raise _Blocked(
            f"{label}: file_locations row {action_or_member.location_id} is "
            f"{None if row is None else (row.archive_id, row.path, row.is_current)}, "
            f"expected archive {action_or_member.archive_id} current at {path}"
        )


def _derive_member(path: str, sha256: str, label: str) -> DerivedArchive:
    derived = _derive(path)
    if derived is None:
        raise _Blocked(f"{label} vanished: {path} is not present")
    if derived.sha256 != sha256:
        raise _Blocked(f"{label} drifted: {path} has sha256 {derived.sha256}, approved {sha256}")
    if derived.unresolved_reason is not None:
        raise _Blocked(f"{label} is not comparable: {derived.unresolved_reason}")
    return derived


def _relation_problem(unit: FamilyUnit, dependant: Any, older: DerivedArchive, keeper: DerivedArchive) -> str | None:
    relation = compare_pair(
        "dependant", older, "keeper", keeper,
        accept_comicinfo_drop=unit.accept_comicinfo_drop,
    )
    if (
        relation.outcome != OUTCOME_SUPERSEDED
        or relation.older != "dependant"
        or relation.relation != dependant.relation
        or relation.index_map != dependant.index_map
    ):
        return (
            f"approved relation of {dependant.member.path} to the keeper no "
            f"longer holds: re-derived {relation.relation}/{relation.outcome} "
            f"{relation.index_map}, approved {dependant.relation}/"
            f"{dependant.outcome} {dependant.index_map}"
        )
    return None


def _member_location(states: Mapping[str, ChainState], action: Action | None, default: str) -> str:
    if action is None:
        return default
    return states[action.action_id].location


def _states(run: _Run, unit: FamilyUnit | RenameUnit) -> tuple[dict[str, ChainState], DatabaseView]:
    records = run.records()
    members = unit.members if isinstance(unit, FamilyUnit) else ()
    view = run.view(unit.actions, [m.location_id for m in members])
    states = {}
    for action in unit.actions:
        state = chain_state(action, run.chains[action.action_id], records, view)
        if state.in_flight and state.receipts != (jn.KIND_INTENT,):
            raise RunStopped(f"{action.action_id} is in flight; run recover first")
        states[action.action_id] = state
    return states, view


def _continue_action(run: _Run, action: Action, state: ChainState) -> None:
    """Run the remaining steps of ``action`` from its frontier."""
    first = True
    for step in state.steps[state.completed:]:
        run.run_step(
            action, step,
            intent_exists=first and state.receipts == (jn.KIND_INTENT,),
        )
        first = False


def _run_family(run: _Run, unit: FamilyUnit) -> str:
    states, view = _states(run, unit)
    if all(states[a.action_id].complete for a in unit.actions):
        return UNIT_ALREADY_COMPLETE

    # Phase 1: every member at its journal-proven location, every relation
    # re-derived from bytes, every row where the journal says it is.
    keeper_at = _member_location(states, unit.keeper_action, unit.keeper.path)
    keeper = _derive_member(keeper_at, unit.keeper.sha256, "keeper")
    _validate_rows(view, unit.keeper, keeper_at, "keeper")
    for dependant in unit.dependants:
        at = states[dependant.action.action_id].location
        older = _derive_member(at, dependant.member.sha256, f"dependant {dependant.action.action_id}")
        _validate_rows(view, dependant.member, at, f"dependant {dependant.action.action_id}")
        problem = _relation_problem(unit, dependant, older, keeper)
        if problem is not None:
            raise _Blocked(problem)
    for action in unit.actions:
        state = states[action.action_id]
        # The first step's destination, not the action's: a case-only
        # rename's final name is its own source in another letter case.
        if not state.started and _occupied(state.steps[0].destination):
            raise _Blocked(f"{action.action_id}: destination {state.steps[0].destination} is occupied")

    # Phase 2: the keeper rename, confirmed, then the keeper freshly
    # validated at its resulting path before anything depends on it.
    if unit.keeper_action is not None:
        state = states[unit.keeper_action.action_id]
        if not state.complete:
            _continue_action(run, unit.keeper_action, state)
        keeper_at = unit.keeper_action.destination
        fresh = _derive_member(keeper_at, unit.keeper.sha256, "renamed keeper")
        if fresh.page_digests != keeper.page_digests:
            raise _Blocked("renamed keeper's re-derived sequence differs")
        _validate_rows(run.view([unit.keeper_action]), unit.keeper, keeper_at, "renamed keeper")
        keeper = fresh
        run.fault("after_keeper_rename", {"unit_id": unit.unit_id})

    # Phase 3: before each quarantine, keeper and candidate re-validated.
    for dependant in unit.dependants:
        state = run.state(dependant.action)
        if state.complete:
            continue
        keeper = _derive_member(keeper_at, unit.keeper.sha256, "keeper")
        older = _derive_member(state.location, dependant.member.sha256, f"dependant {dependant.action.action_id}")
        problem = _relation_problem(unit, dependant, older, keeper)
        if problem is not None:
            raise _Blocked(problem)
        os.makedirs(os.path.dirname(dependant.action.destination), exist_ok=True)
        _continue_action(run, dependant.action, state)
        run.fault("after_dependant", {"unit_id": unit.unit_id, "action_id": dependant.action.action_id})
    return UNIT_COMPLETED


def _run_rename(run: _Run, unit: RenameUnit) -> str:
    states, view = _states(run, unit)
    action = unit.action
    state = states[action.action_id]
    if state.complete:
        return UNIT_ALREADY_COMPLETE
    problem = _identity_problem(state.location, action.sha256)
    if problem is not None:
        raise _Blocked(f"rename source drifted or vanished: {problem}")
    _validate_rows(view, action, state.location, "rename source")
    if not state.started and _occupied(state.steps[0].destination):
        raise _Blocked(f"destination {state.steps[0].destination} is occupied")
    _continue_action(run, action, state)
    return UNIT_COMPLETED


def _run_units(
    run: _Run,
    report: RunReport,
    plan: Plan,
    *,
    quiescence: Quiescence,
    unit_gate: UnitGate,
    batch_size: int,
    max_drift_fraction: float,
) -> None:
    units = list(plan.units)
    processed = blocked = 0
    for index, unit in enumerate(units):
        if index and index % batch_size == 0:
            # Batch boundary: re-check quiescence and the run-wide drift.
            reason = quiescence(run.library_root)
            if reason:
                report.stopped = f"quiescence lost at a batch boundary: {reason}"
                break
            if processed and blocked / processed > max_drift_fraction:
                report.stopped = (
                    f"run-wide drift {blocked}/{processed} units exceeds "
                    f"{max_drift_fraction}; stopping early"
                )
                break
        conflict = plan.conflicts.get(unit.unit_id)
        if conflict:
            report.units.append({"unit_id": unit.unit_id, "status": UNIT_BLOCKED, "reasons": list(conflict)})
            run.note("unit_blocked", unit_id=unit.unit_id, reasons=list(conflict))
            processed += 1
            blocked += 1
            continue
        # A wait (series lock, operator mode) may happen here; validation
        # below always runs after it returns (§5.3).
        unit_gate(unit.unit_id)
        try:
            if isinstance(unit, FamilyUnit):
                status = _run_family(run, unit)
            else:
                status = _run_rename(run, unit)
            report.units.append({"unit_id": unit.unit_id, "status": status, "reasons": []})
        except (_Blocked, Refused) as exc:
            report.units.append({"unit_id": unit.unit_id, "status": UNIT_BLOCKED, "reasons": [str(exc)]})
            run.note("unit_blocked", unit_id=unit.unit_id, reasons=[str(exc)])
            blocked += 1
        except (RunStopped, ChainRefused) as exc:
            report.units.append({"unit_id": unit.unit_id, "status": UNIT_BLOCKED, "reasons": [str(exc)]})
            report.stopped = str(exc)
            break
        processed += 1
    reached = {u["unit_id"] for u in report.units}
    for unit in units:
        if unit.unit_id not in reached:
            report.units.append({"unit_id": unit.unit_id, "status": UNIT_NOT_REACHED, "reasons": []})
    run.note("run_finished" if report.stopped is None else "run_stopped", stopped=report.stopped)


# --- public entry points -------------------------------------------------------------


def _new_attempt_id() -> str:
    return uuid.uuid4().hex


def apply(
    plan: Plan,
    *,
    library_root: str | Path,
    database: str | Path,
    quiescence: Quiescence,
    resume_attempt: str | None = None,
    pairing_columns: Mapping[str, PairingColumn] | None = None,
    unit_gate: UnitGate = _no_gate,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_drift_fraction: float = DEFAULT_MAX_DRIFT_FRACTION,
    fault: Fault = _no_fault,
    attempt_id: str | None = None,
) -> RunReport:
    """Apply an approved plan (already checked by `plan.load_plan`).

    A new attempt refuses while any other attempt under the root is
    unfinished, and refuses a plan digest that an earlier attempt already
    started (use ``resume_attempt``). A resumed attempt first runs recovery
    and refuses if any action is refused by it.
    """
    require_windows()
    root = Path(library_root)
    registry = dict(pairing_columns or {})
    _check_pairings(plan, registry)
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    attempt = resume_attempt or attempt_id or _new_attempt_id()
    with ScopeLock(root, attempt, "apply"):
        _check_quiescent(quiescence, root)
        run = _Run(
            plan, root, Path(database), attempt, registry, fault,
            {a.action_id: a.steps(attempt) for a in plan.actions.values()},
            plan.actions,
        )
        report = RunReport(attempt, jn.MODE_APPLY, str(jn.journal_path(root, attempt)))
        if resume_attempt is None:
            _refuse_new_attempt(root, plan)
            run.view([])  # schema present and the database readable
            run.writer = jn.JournalWriter.create(
                root, attempt,
                {"mode": jn.MODE_APPLY, "plan_digest": plan.digest, "library_root": str(root), "pid": os.getpid()},
            )
        else:
            header = _header(root, resume_attempt)
            if header.get("mode") != jn.MODE_APPLY or header.get("plan_digest") != plan.digest:
                raise Refused(f"attempt {resume_attempt} is not an apply of plan {plan.digest}")
            unfinished = _unfinished_attempts(root, exclude=resume_attempt)
            if unfinished:
                raise Refused(f"other attempts are unfinished: {unfinished}")
            run.writer = jn.JournalWriter.open_existing(root, resume_attempt)
            resolutions = _recover_actions(run, list(plan.actions.values()), single_round=False)
            report.resolutions = [r.as_dict() for r in resolutions]
            refused = [r for r in resolutions if r.status == RES_REFUSED]
            if refused:
                run.writer.close()
                raise Refused(f"recovery refused {[r.action_id for r in refused]}: {refused[0].detail}")
        try:
            _run_units(
                run, report, plan,
                quiescence=quiescence, unit_gate=unit_gate,
                batch_size=batch_size, max_drift_fraction=max_drift_fraction,
            )
        finally:
            run.writer.close()
    return report


def _check_pairings(plan: Plan, registry: Mapping[str, PairingColumn]) -> None:
    unknown = sorted({p.table for a in plan.actions.values() for p in a.pairings} - set(registry))
    if unknown:
        raise Refused(
            f"plan carries pairing updates for unregistered tables {unknown}; "
            "no pairing table is registered by default"
        )


def _refuse_new_attempt(root: Path, plan: Plan) -> None:
    unfinished = _unfinished_attempts(root, exclude=None)
    if unfinished:
        raise Refused(f"attempts {unfinished} are unfinished; recover them first")
    for attempt_id in jn.list_attempts(root):
        header = _header(root, attempt_id)
        if header.get("mode") == jn.MODE_APPLY and header.get("plan_digest") == plan.digest:
            raise Refused(
                f"plan {plan.digest} was already applied by attempt {attempt_id}; "
                "use --resume with that attempt"
            )


def _header(root: Path, attempt_id: str) -> Mapping[str, Any]:
    return jn.read_journal(root, attempt_id)[0].data


def _attempt_run(plan: Plan, root: Path, database: Path, attempt_id: str, registry, fault) -> tuple[_Run, Mapping[str, Any]]:
    header = _header(root, attempt_id)
    if header.get("plan_digest") != plan.digest:
        raise Refused(f"attempt {attempt_id} belongs to plan {header.get('plan_digest')}, not {plan.digest}")
    actions = plan.actions
    if header.get("mode") == jn.MODE_APPLY:
        chains = {a.action_id: a.steps(attempt_id) for a in actions.values()}
        selected = dict(actions)
    elif header.get("mode") == jn.MODE_UNDO:
        forward = header["forward_attempt_id"]
        selected = {action_id: actions[action_id] for action_id in header["action_ids"]}
        chains = {
            action_id: action.reverse_steps(forward, plan.quarantine_root)
            for action_id, action in selected.items()
        }
    else:
        raise Refused(f"attempt {attempt_id} has unknown mode {header.get('mode')!r}")
    return _Run(plan, root, database, attempt_id, registry, fault, chains, selected), header


def recover(
    plan: Plan,
    *,
    library_root: str | Path,
    database: str | Path,
    attempt_id: str,
    quiescence: Quiescence,
    pairing_columns: Mapping[str, PairingColumn] | None = None,
    single_round: bool = False,
    fault: Fault = _no_fault,
) -> RunReport:
    """Resolve every action's frontier for ``attempt_id`` (apply or undo).

    Under the scope lock and after re-verifying quiescence. Writes only
    what the §5.4 matrix resolves: a database step for an owned destination
    or a ``confirmed`` record. With ``single_round`` each action advances by
    at most one record.
    """
    require_windows()
    root = Path(library_root)
    registry = dict(pairing_columns or {})
    _check_pairings(plan, registry)
    with ScopeLock(root, attempt_id, "recover"):
        _check_quiescent(quiescence, root)
        run, header = _attempt_run(plan, root, Path(database), attempt_id, registry, fault)
        report = RunReport(attempt_id, f"recover:{header.get('mode')}", str(jn.journal_path(root, attempt_id)))
        run.writer = jn.JournalWriter.open_existing(root, attempt_id)
        try:
            resolutions = _recover_actions(run, list(run.actions.values()), single_round=single_round)
        finally:
            run.writer.close()
        report.resolutions = [r.as_dict() for r in resolutions]
    return report


def undo(
    plan: Plan,
    *,
    library_root: str | Path,
    database: str | Path,
    forward_attempt: str,
    quiescence: Quiescence,
    action_ids: Sequence[str] | None = None,
    resume_attempt: str | None = None,
    pairing_columns: Mapping[str, PairingColumn] | None = None,
    fault: Fault = _no_fault,
    attempt_id: str | None = None,
) -> RunReport:
    """Reverse fully confirmed forward actions as a new attempt (§5.4 Undo).

    The precondition for every action is its complete confirmed forward
    chain plus fresh content and database rows at its final location; if
    any requested action fails it, nothing is undone. Actions are reversed
    latest first.
    """
    require_windows()
    root = Path(library_root)
    registry = dict(pairing_columns or {})
    _check_pairings(plan, registry)
    attempt = resume_attempt or attempt_id or _new_attempt_id()
    with ScopeLock(root, attempt, "undo"):
        _check_quiescent(quiescence, root)
        forward_run, forward_header = _attempt_run(plan, root, Path(database), forward_attempt, registry, fault)
        if forward_header.get("mode") != jn.MODE_APPLY:
            raise Refused(f"attempt {forward_attempt} is not a forward apply; an undo is not undone")
        if resume_attempt is None:
            unfinished = _unfinished_attempts(root, exclude=None)
            if unfinished:
                raise Refused(f"attempts {unfinished} are unfinished; recover them first")
            records = forward_run.records()
            view = forward_run.view(list(forward_run.actions.values()))
            states = {
                a.action_id: chain_state(a, forward_run.chains[a.action_id], records, view)
                for a in forward_run.actions.values()
            }
            partial = [s.action_id for s in states.values() if s.started and not s.complete]
            if partial:
                raise Refused(f"forward actions {partial} are not fully confirmed")
            requested = list(action_ids) if action_ids is not None else [
                s.action_id for s in states.values() if s.complete
            ]
            problems = []
            for action_id in requested:
                if action_id not in states or not states[action_id].complete:
                    problems.append(f"{action_id}: forward chain is not complete")
                    continue
                action = forward_run.actions[action_id]
                final = states[action_id].location
                problem = _identity_problem(final, action.sha256)
                if problem is None and not _rows_at(view, action, final):
                    problem = f"database rows are not at {final}"
                if problem is not None:
                    problems.append(f"{action_id}: {problem}")
            if problems:
                raise Refused("undo refused, nothing moved: " + "; ".join(problems))
            ordered = sorted(requested, key=lambda a: states[a].first_seq, reverse=True)
            writer = jn.JournalWriter.create(
                root, attempt,
                {
                    "mode": jn.MODE_UNDO, "plan_digest": plan.digest, "library_root": str(root),
                    "forward_attempt_id": forward_attempt, "action_ids": ordered, "pid": os.getpid(),
                },
            )
            writer.close()
        run, header = _attempt_run(plan, root, Path(database), attempt, registry, fault)
        if header.get("mode") != jn.MODE_UNDO or header.get("forward_attempt_id") != forward_attempt:
            raise Refused(f"attempt {attempt} is not an undo of {forward_attempt}")
        report = RunReport(attempt, jn.MODE_UNDO, str(jn.journal_path(root, attempt)))
        run.writer = jn.JournalWriter.open_existing(root, attempt)
        try:
            if resume_attempt is not None:
                unfinished = _unfinished_attempts(root, exclude=attempt)
                if unfinished:
                    raise Refused(f"other attempts are unfinished: {unfinished}")
                resolutions = _recover_actions(run, list(run.actions.values()), single_round=False)
                report.resolutions = [r.as_dict() for r in resolutions]
                refused = [r for r in resolutions if r.status == RES_REFUSED]
                if refused:
                    raise Refused(f"recovery refused {[r.action_id for r in refused]}: {refused[0].detail}")
            for action_id in header["action_ids"]:
                action = run.actions[action_id]
                try:
                    state = run.state(action)
                    if state.in_flight and state.receipts != (jn.KIND_INTENT,):
                        raise RunStopped(f"{action_id} is in flight; run recover first")
                    if state.complete:
                        report.units.append({"unit_id": action_id, "status": UNIT_ALREADY_COMPLETE, "reasons": []})
                        continue
                    problem = _identity_problem(state.location, action.sha256)
                    if problem is not None:
                        raise Refused(f"{action_id}: {problem}")
                    _continue_action(run, action, state)
                    report.units.append({"unit_id": action_id, "status": UNIT_COMPLETED, "reasons": []})
                except (Refused, RunStopped, ChainRefused) as exc:
                    report.units.append({"unit_id": action_id, "status": UNIT_BLOCKED, "reasons": [str(exc)]})
                    report.stopped = str(exc)
                    break
            run.note("run_finished" if report.stopped is None else "run_stopped", stopped=report.stopped)
        finally:
            run.writer.close()
    return report
