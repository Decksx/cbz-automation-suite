"""The approved plan document that `apply` acts on (§5.1, §5.2).

A plan is one JSON object with contract ``canonical-naming-plan-v1``:

``library_root``
    The one root every path in the plan lives under.
``database_target``
    The identity of the database the plan was made against: its real path
    and file identity (``st_dev``, ``st_ino``), from
    `content_signature_recertify.file_identity`, the #117 target-binding
    idiom. A copy, SQLite backup clone, hard link or restored file is a
    different database and needs a new plan.
``quarantine_root``
    ``<library-root>\\.cbz-canonical-quarantine\\<YYYY-MM-DD>``: the dated,
    same-volume quarantine directory (§5.4, "Volumes and quarantine").
``renames``
    Ordinary renames, each a single-member unit: ``action_id``,
    ``archive_id``, ``location_id`` (the ``file_locations`` row), ``source``,
    ``destination``, ``sha256`` (the source strong identity), ``evidence``
    and optional ``pairings``.
``families``
    Supersession family units: ``family_id``, ``basis`` (always
    ``sha256``), ``accept_comicinfo_drop``, the step-5 ``snapshot``, the
    ``keeper`` (with an optional ``rename_to`` and its ``action_id``), and
    ``dependants``, each with its approved relation and index map and its
    ``quarantine_to`` path.
``review``
    Items held for a human; carried, never acted on.
``counts`` and ``plan_digest``
    The digest is the sha256 of the canonical bytes of every other key
    (`canonical_bytes`). `apply` requires the operator's
    ``--expect-digest``, ``--expect-renames`` and ``--expect-families``
    to match the plan exactly (§5.2).

`load_plan` re-derives the digest and the counts and refuses any
disagreement; it then checks the plan's structure (`PlanError`) before
anything is locked or touched. An archive scheduled by more than one unit
is not a plan error: it blocks every unit that names it (§5.3 phase 1, "no
other unit schedules the keeper for quarantine"), reported through
`Plan.conflicts`.

Step chains (`Action.steps`) are a pure function of the plan action and
the attempt id, so recovery and undo always re-derive exactly the chain the
forward attempt executed. A case-only rename is two steps through
``<new>.<attempt>.tmp`` (§2.3, §5.4).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from comic_automation.library.canonical_apply.journal import JOURNAL_DIRNAME
from comic_automation.library.canonical_apply.markers import PairingRef
from comic_automation.library.canonical_apply.scope_lock import (
    LOCK_FILENAME,
    OWNER_FILENAME,
)

PLAN_CONTRACT = "canonical-naming-plan-v1"
QUARANTINE_DIRNAME = ".cbz-canonical-quarantine"
TEMP_SUFFIX = ".tmp"

KIND_RENAME = "rename"
KIND_TEMP = "temp"
KIND_QUARANTINE = "quarantine"

ROLE_RENAME = "rename"
ROLE_KEEPER_RENAME = "keeper_rename"
ROLE_QUARANTINE = "quarantine"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_ACTION_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


class PlanError(ValueError):
    """The plan is malformed, or does not match what the operator approved."""


def canonical_bytes(document: Mapping[str, Any]) -> bytes:
    """The bytes the digest is taken over: every key but ``plan_digest``."""
    body = {key: value for key, value in document.items() if key != "plan_digest"}
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def compute_digest(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(document)).hexdigest()


def _norm(path: str) -> str:
    return os.path.normcase(path)


def is_case_only(source: str, destination: str) -> bool:
    """True when the two paths differ only by case (one file on NTFS)."""
    return source != destination and _norm(source) == _norm(destination)


def temp_path(destination: str, attempt_id: str) -> str:
    """The owned temporary name of a case-only rename's first step."""
    return f"{destination}.{attempt_id}{TEMP_SUFFIX}"


# --- the parsed plan ---------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """One filesystem step: ``os.rename(source, destination)``."""

    index: int
    kind: str
    source: str
    destination: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.index,
            "step_kind": self.kind,
            "source": self.source,
            "destination": self.destination,
        }


@dataclass(frozen=True)
class Action:
    """One owned action: a rename, a keeper rename or a quarantine move."""

    action_id: str
    unit_id: str
    role: str
    archive_id: int
    location_id: int
    sha256: str
    source: str
    destination: str
    pairings: tuple[PairingRef, ...] = ()

    def steps(self, attempt_id: str) -> tuple[Step, ...]:
        """The forward chain. Case-only renames are two steps."""
        if self.role == ROLE_QUARANTINE:
            return (Step(1, KIND_QUARANTINE, self.source, self.destination),)
        if is_case_only(self.source, self.destination):
            temp = temp_path(self.destination, attempt_id)
            return (
                Step(1, KIND_TEMP, self.source, temp),
                Step(2, KIND_RENAME, temp, self.destination),
            )
        return (Step(1, KIND_RENAME, self.source, self.destination),)

    def reverse_steps(self, forward_attempt_id: str, quarantine_root: str) -> tuple[Step, ...]:
        """The undo chain: the forward chain reversed, step by step.

        For a case-only rename this is new -> temp -> old, through the
        forward attempt's temporary name.
        """
        forward = self.steps(forward_attempt_id)
        reversed_steps = []
        for index, step in enumerate(reversed(forward), start=1):
            destination = step.source
            if destination.endswith(TEMP_SUFFIX) and destination != self.source:
                kind = KIND_TEMP
            elif _under(destination, quarantine_root):
                kind = KIND_QUARANTINE
            else:
                kind = KIND_RENAME
            reversed_steps.append(Step(index, kind, step.destination, destination))
        return tuple(reversed_steps)


@dataclass(frozen=True)
class Member:
    """One family member at its approved location, with its strong identity."""

    archive_id: int
    location_id: int
    path: str
    sha256: str


@dataclass(frozen=True)
class Dependant:
    member: Member
    relation: str
    outcome: str
    index_map: tuple[int, ...]
    action: Action


@dataclass(frozen=True)
class FamilyUnit:
    unit_id: str
    keeper: Member
    keeper_action: Action | None
    dependants: tuple[Dependant, ...]
    accept_comicinfo_drop: bool

    @property
    def actions(self) -> tuple[Action, ...]:
        head = () if self.keeper_action is None else (self.keeper_action,)
        return head + tuple(d.action for d in self.dependants)

    @property
    def members(self) -> tuple[Member, ...]:
        return (self.keeper,) + tuple(d.member for d in self.dependants)


@dataclass(frozen=True)
class RenameUnit:
    unit_id: str
    action: Action

    @property
    def actions(self) -> tuple[Action, ...]:
        return (self.action,)


@dataclass(frozen=True)
class Plan:
    document: Mapping[str, Any]
    digest: str
    library_root: str
    quarantine_root: str
    families: tuple[FamilyUnit, ...]
    renames: tuple[RenameUnit, ...]
    conflicts: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def database_target(self) -> Mapping[str, Any]:
        return self.document["database_target"]

    @property
    def units(self) -> tuple[FamilyUnit | RenameUnit, ...]:
        return self.families + self.renames

    @property
    def actions(self) -> dict[str, Action]:
        return {a.action_id: a for unit in self.units for a in unit.actions}

    def unit_of(self, action_id: str) -> FamilyUnit | RenameUnit:
        for unit in self.units:
            if any(a.action_id == action_id for a in unit.actions):
                return unit
        raise KeyError(action_id)


# --- building ------------------------------------------------------------------


@dataclass(frozen=True)
class RenameSpec:
    action_id: str
    archive_id: int
    location_id: int
    source: str
    destination: str
    sha256: str
    evidence: Mapping[str, Any] = field(default_factory=dict)
    pairings: tuple[PairingRef, ...] = ()


@dataclass(frozen=True)
class MemberSpec:
    archive_id: int
    location_id: int
    path: str
    sha256: str
    pairings: tuple[PairingRef, ...] = ()


@dataclass(frozen=True)
class DependantSpec:
    member: MemberSpec
    action_id: str
    quarantine_to: str
    index_map: tuple[int, ...]
    relation: str = "strict_prefix"
    outcome: str = "superseded"


@dataclass(frozen=True)
class FamilySpec:
    family_id: str
    keeper: MemberSpec
    dependants: tuple[DependantSpec, ...]
    keeper_rename_to: str | None = None
    keeper_action_id: str | None = None
    accept_comicinfo_drop: bool = False
    snapshot: Mapping[str, Any] = field(default_factory=dict)


def _pairings(refs: Sequence[PairingRef]) -> list[dict[str, Any]]:
    return [ref.as_dict() for ref in refs]


def build_plan(
    *,
    library_root: str,
    quarantine_root: str,
    database: str | Path,
    renames: Sequence[RenameSpec] = (),
    families: Sequence[FamilySpec] = (),
    review: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Assemble a plan document and seal it with its digest.

    Reads only the file identity of ``database``, which binds the plan to
    that database. The caller supplies strong identities computed at plan
    time (`family_spec_from_evaluation` does so from step-5 output).
    The result is validated by the same `parse_plan` that `apply` uses.
    """
    document: dict[str, Any] = {
        "contract": PLAN_CONTRACT,
        "library_root": library_root,
        "database_target": database_target(database),
        "quarantine_root": quarantine_root,
        "renames": [
            {
                "action_id": spec.action_id,
                "archive_id": spec.archive_id,
                "location_id": spec.location_id,
                "source": spec.source,
                "destination": spec.destination,
                "sha256": spec.sha256,
                "evidence": dict(spec.evidence),
                "pairings": _pairings(spec.pairings),
            }
            for spec in renames
        ],
        "families": [
            {
                "family_id": spec.family_id,
                "basis": "sha256",
                "accept_comicinfo_drop": spec.accept_comicinfo_drop,
                "snapshot": dict(spec.snapshot),
                "keeper": {
                    "archive_id": spec.keeper.archive_id,
                    "location_id": spec.keeper.location_id,
                    "path": spec.keeper.path,
                    "sha256": spec.keeper.sha256,
                    "rename_to": spec.keeper_rename_to,
                    "action_id": spec.keeper_action_id,
                    "pairings": _pairings(spec.keeper.pairings),
                },
                "dependants": [
                    {
                        "action_id": dep.action_id,
                        "archive_id": dep.member.archive_id,
                        "location_id": dep.member.location_id,
                        "path": dep.member.path,
                        "sha256": dep.member.sha256,
                        "relation": dep.relation,
                        "outcome": dep.outcome,
                        "index_map": list(dep.index_map),
                        "quarantine_to": dep.quarantine_to,
                        "pairings": _pairings(dep.member.pairings),
                    }
                    for dep in spec.dependants
                ],
            }
            for spec in families
        ],
        "review": [dict(item) for item in review],
    }
    document["counts"] = _counts(document)
    document["plan_digest"] = compute_digest(document)
    parse_plan(document)
    return document


def family_spec_from_evaluation(
    family: Any,
    folder: Any,
    *,
    family_id: str,
    locations: Mapping[str, tuple[int, int]],
    quarantine_root: str,
    keeper_rename_to: str | None = None,
    accept_comicinfo_drop: bool = False,
) -> FamilySpec:
    """A `FamilySpec` from a step-5 `FamilySupersession` verdict.

    Only an actionable verdict (``status == "supersession"``) can become a
    plan unit. ``locations`` maps each member's archive key (its path) to
    ``(archive_id, location_id)``. Strong identities and index maps come
    from the evaluation itself, which bound them to the bytes it read.
    """
    from comic_automation.archive import supersession as sv

    if family.status != sv.FAMILY_SUPERSESSION or family.keeper is None:
        raise PlanError(
            f"family {family_id} is {family.status!r}; only a supersession "
            "verdict is actionable"
        )
    by_key = {archive.archive_key: archive for archive in folder.archives}

    def member(key: str) -> MemberSpec:
        archive_id, location_id = locations[key]
        return MemberSpec(archive_id, location_id, key, by_key[key].derived.sha256)

    dependants = tuple(
        DependantSpec(
            member=member(pointer.older),
            action_id=f"{family_id}:quarantine:{index}",
            quarantine_to=str(Path(quarantine_root) / family_id / Path(pointer.older).name),
            index_map=tuple(pointer.index_map),
            relation=pointer.relation,
            outcome=pointer.outcome,
        )
        for index, pointer in enumerate(family.pointers, start=1)
    )
    return FamilySpec(
        family_id=family_id,
        keeper=member(family.keeper),
        dependants=dependants,
        keeper_rename_to=keeper_rename_to,
        keeper_action_id=None if keeper_rename_to is None else f"{family_id}:keeper",
        accept_comicinfo_drop=accept_comicinfo_drop,
        snapshot=dict(folder.snapshot),
    )


def database_target(database: str | Path) -> dict[str, Any]:
    """The #117 file identity of ``database``; `PlanError` if it has none."""
    from comic_automation.archive.content_signature_recertify import (
        PlanRefusedError,
        file_identity,
    )

    try:
        return file_identity(database)
    except PlanRefusedError as exc:
        raise PlanError(str(exc)) from exc


def _counts(document: Mapping[str, Any]) -> dict[str, int]:
    families = document.get("families") or []
    return {
        "renames": len(document.get("renames") or []),
        "families": len(families),
        "quarantines": sum(len(f.get("dependants") or []) for f in families),
        "keeper_renames": sum(
            1 for f in families if (f.get("keeper") or {}).get("rename_to") is not None
        ),
        "review": len(document.get("review") or []),
    }


# --- loading and validation ------------------------------------------------------


def load_plan(
    path: str | Path,
    *,
    library_root: str | Path,
    expect_digest: str,
    expect_renames: int,
    expect_families: int,
) -> Plan:
    """Read, re-seal and validate a plan against the operator's approval."""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanError(f"plan {path} cannot be read: {exc}") from exc
    plan = parse_plan(document)
    check_approval(
        plan,
        library_root=library_root,
        expect_digest=expect_digest,
        expect_renames=expect_renames,
        expect_families=expect_families,
    )
    return plan


def check_approval(
    plan: Plan,
    *,
    library_root: str | Path,
    expect_digest: str,
    expect_renames: int,
    expect_families: int,
) -> None:
    """Refuse unless the plan is exactly what the operator approved (§5.2)."""
    if plan.digest != expect_digest:
        raise PlanError(
            f"plan digest {plan.digest} does not match --expect-digest {expect_digest}"
        )
    counts = plan.document["counts"]
    if counts["renames"] != expect_renames:
        raise PlanError(
            f"plan has {counts['renames']} renames; --expect-renames is {expect_renames}"
        )
    if counts["families"] != expect_families:
        raise PlanError(
            f"plan has {counts['families']} families; --expect-families is {expect_families}"
        )
    if _norm(str(library_root)) != _norm(plan.library_root):
        raise PlanError(
            f"plan is for library root {plan.library_root}, not {library_root}"
        )


def _under(path: str, root: str) -> bool:
    try:
        return path != root and _norm(os.path.commonpath([root, path])) == _norm(root)
    except ValueError:
        return False


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PlanError(message)


def _path(value: Any, what: str) -> str:
    _require(isinstance(value, str) and value, f"{what} must be a non-empty string")
    _require(os.path.isabs(value), f"{what} must be absolute: {value}")
    _require(os.path.normpath(value) == value, f"{what} must be normalised: {value}")
    return value


def _int(value: Any, what: str) -> int:
    _require(isinstance(value, int) and not isinstance(value, bool), f"{what} must be an integer")
    return value


def _sha(value: Any, what: str) -> str:
    _require(isinstance(value, str) and bool(_SHA256.fullmatch(value)), f"{what} must be a sha256")
    return value


def _action_id(value: Any) -> str:
    _require(
        isinstance(value, str) and bool(_ACTION_ID.fullmatch(value)),
        f"action id {value!r} is not a plain identifier",
    )
    return value


def _pairing_refs(value: Any, what: str) -> tuple[PairingRef, ...]:
    _require(isinstance(value, list), f"{what} pairings must be a list")
    refs = []
    for item in value:
        _require(isinstance(item, dict) and set(item) == {"table", "id"}, f"{what} pairing malformed")
        _require(isinstance(item["table"], str) and item["table"], f"{what} pairing table")
        refs.append(PairingRef(item["table"], _int(item["id"], f"{what} pairing id")))
    return tuple(refs)


def parse_plan(document: Any) -> Plan:
    """Validate a plan document's seal and structure; no filesystem access."""
    _require(isinstance(document, dict), "plan must be a JSON object")
    _require(document.get("contract") == PLAN_CONTRACT, f"plan contract must be {PLAN_CONTRACT}")
    digest = compute_digest(document)
    _require(
        document.get("plan_digest") == digest,
        f"plan_digest {document.get('plan_digest')!r} does not match the canonical bytes ({digest})",
    )
    _require(document.get("counts") == _counts(document), "plan counts do not match its contents")

    root = _path(document.get("library_root"), "library_root")
    target = document.get("database_target")
    _require(
        isinstance(target, dict)
        and set(target) == {"path", "st_dev", "st_ino"}
        and isinstance(target["path"], str) and bool(target["path"])
        and all(isinstance(target[k], int) and not isinstance(target[k], bool) for k in ("st_dev", "st_ino"))
        and target["st_ino"] != 0,
        "database_target must be a file identity {path, st_dev, st_ino}",
    )
    quarantine_root = _path(document.get("quarantine_root"), "quarantine_root")
    _require(
        _norm(os.path.dirname(quarantine_root)) == _norm(os.path.join(root, QUARANTINE_DIRNAME))
        and bool(_DATE.fullmatch(os.path.basename(quarantine_root))),
        f"quarantine_root must be {os.path.join(root, QUARANTINE_DIRNAME)}\\<YYYY-MM-DD>",
    )
    internal = [
        os.path.join(root, JOURNAL_DIRNAME),
        os.path.join(root, LOCK_FILENAME),
        os.path.join(root, OWNER_FILENAME),
    ]

    def library_file(value: Any, what: str) -> str:
        path = _path(value, what)
        _require(_under(path, root), f"{what} is not under the library root: {path}")
        _require(
            not any(_norm(path) == _norm(p) or _under(path, p) for p in internal),
            f"{what} is inside the canonical-naming state directory: {path}",
        )
        return path

    def cbz_name(path: str, what: str) -> None:
        _require(path.casefold().endswith(".cbz"), f"{what} must be a .cbz path: {path}")

    def library_member(value: Any, what: str) -> str:
        path = library_file(value, what)
        _require(not _under(path, os.path.join(root, QUARANTINE_DIRNAME)), f"{what} is in quarantine: {path}")
        cbz_name(path, what)
        return path

    def rename_destination(source: str, value: Any, what: str) -> str:
        destination = library_member(value, what)
        _require(destination != source, f"{what} equals its source")
        # Canonical naming changes the file name only (§2): a rename that
        # changes directory is refused rather than treated as a move.
        _require(
            os.path.dirname(destination) == os.path.dirname(source),
            f"{what} must stay in its source directory: {destination}",
        )
        return destination

    families: list[FamilyUnit] = []
    renames: list[RenameUnit] = []
    action_ids: set[str] = set()

    def register(action_id: str) -> str:
        _require(action_id not in action_ids, f"duplicate action id {action_id}")
        action_ids.add(action_id)
        return action_id

    _require(isinstance(document.get("renames"), list), "renames must be a list")
    _require(isinstance(document.get("families"), list), "families must be a list")
    _require(isinstance(document.get("review"), list), "review must be a list")
    unit_ids: set[str] = set()

    for item in document["families"]:
        _require(isinstance(item, dict), "family must be an object")
        family_id = _action_id(item.get("family_id"))
        _require(family_id not in unit_ids, f"duplicate family id {family_id}")
        unit_ids.add(family_id)
        _require(item.get("basis") == "sha256", f"family {family_id}: only the exact basis proves supersession")
        _require(isinstance(item.get("accept_comicinfo_drop"), bool), f"family {family_id}: accept_comicinfo_drop")
        keeper_doc = item.get("keeper")
        _require(isinstance(keeper_doc, dict), f"family {family_id}: keeper")
        keeper = Member(
            _int(keeper_doc.get("archive_id"), "keeper archive_id"),
            _int(keeper_doc.get("location_id"), "keeper location_id"),
            library_member(keeper_doc.get("path"), f"family {family_id} keeper path"),
            _sha(keeper_doc.get("sha256"), "keeper sha256"),
        )
        keeper_action = None
        if keeper_doc.get("rename_to") is not None:
            keeper_action = Action(
                action_id=register(_action_id(keeper_doc.get("action_id"))),
                unit_id=family_id,
                role=ROLE_KEEPER_RENAME,
                archive_id=keeper.archive_id,
                location_id=keeper.location_id,
                sha256=keeper.sha256,
                source=keeper.path,
                destination=rename_destination(
                    keeper.path, keeper_doc["rename_to"], f"family {family_id} keeper rename_to"
                ),
                pairings=_pairing_refs(keeper_doc.get("pairings", []), "keeper"),
            )
        else:
            _require(keeper_doc.get("action_id") is None, f"family {family_id}: keeper action_id without rename_to")
            _require(not keeper_doc.get("pairings"), f"family {family_id}: keeper pairings without a rename")
        dependants = []
        _require(
            isinstance(item.get("dependants"), list) and item["dependants"],
            f"family {family_id}: at least one dependant",
        )
        for dep in item["dependants"]:
            _require(isinstance(dep, dict), "dependant must be an object")
            what = f"family {family_id} dependant"
            member = Member(
                _int(dep.get("archive_id"), f"{what} archive_id"),
                _int(dep.get("location_id"), f"{what} location_id"),
                library_member(dep.get("path"), f"{what} path"),
                _sha(dep.get("sha256"), f"{what} sha256"),
            )
            # Only a strict-prefix supersession is actionable (step 5 note 6).
            _require(
                (dep.get("relation"), dep.get("outcome")) == ("strict_prefix", "superseded"),
                f"{what}: only a strict-prefix supersession is actionable",
            )
            index_map = dep.get("index_map")
            _require(
                isinstance(index_map, list) and index_map
                and all(isinstance(i, int) and not isinstance(i, bool) for i in index_map),
                f"{what}: index_map",
            )
            quarantine_to = library_file(dep.get("quarantine_to"), f"{what} quarantine_to")
            _require(_under(quarantine_to, quarantine_root), f"{what} quarantine_to must be under quarantine_root")
            cbz_name(quarantine_to, f"{what} quarantine_to")
            action = Action(
                action_id=register(_action_id(dep.get("action_id"))),
                unit_id=family_id,
                role=ROLE_QUARANTINE,
                archive_id=member.archive_id,
                location_id=member.location_id,
                sha256=member.sha256,
                source=member.path,
                destination=quarantine_to,
                pairings=_pairing_refs(dep.get("pairings", []), what),
            )
            dependants.append(Dependant(member, dep["relation"], dep["outcome"], tuple(index_map), action))
        families.append(
            FamilyUnit(family_id, keeper, keeper_action, tuple(dependants), item["accept_comicinfo_drop"])
        )

    for item in document["renames"]:
        _require(isinstance(item, dict), "rename must be an object")
        action_id = register(_action_id(item.get("action_id")))
        _require(action_id not in unit_ids, f"duplicate unit id {action_id}")
        unit_ids.add(action_id)
        source = library_member(item.get("source"), f"rename {action_id} source")
        action = Action(
            action_id=action_id,
            unit_id=action_id,
            role=ROLE_RENAME,
            archive_id=_int(item.get("archive_id"), "archive_id"),
            location_id=_int(item.get("location_id"), "location_id"),
            sha256=_sha(item.get("sha256"), "sha256"),
            source=source,
            destination=rename_destination(source, item.get("destination"), f"rename {action_id} destination"),
            pairings=_pairing_refs(item.get("pairings", []), f"rename {action_id}"),
        )
        renames.append(RenameUnit(action_id, action))

    plan = Plan(
        document=document,
        digest=digest,
        library_root=root,
        quarantine_root=quarantine_root,
        families=tuple(families),
        renames=tuple(renames),
    )
    _check_paths(plan)
    return Plan(
        document=plan.document,
        digest=plan.digest,
        library_root=plan.library_root,
        quarantine_root=plan.quarantine_root,
        families=plan.families,
        renames=plan.renames,
        conflicts=_conflicts(plan),
    )


def _check_paths(plan: Plan) -> None:
    """Paths unique within a unit, destinations unique, none a source.

    A path named by two *different* units is not refused here: it is a
    conflict that blocks both units (`_conflicts`). A destination that is
    any source would make one action depend on another's completion, which
    the per-unit protocol does not model; it is refused outright. A
    case-only rename's destination is its own source by case and is the one
    exception.
    """
    sources: set[str] = set()
    for unit in plan.units:
        keys = [_norm(member_path) for member_path, _ in _unit_paths(unit)]
        _require(len(keys) == len(set(keys)), f"unit {unit.unit_id} names a path twice")
        sources.update(keys)
    destinations: set[str] = set()
    for action in plan.actions.values():
        key = _norm(action.destination)
        _require(key not in destinations, f"destination {action.destination} is named twice")
        destinations.add(key)
        if key in sources and not is_case_only(action.source, action.destination):
            raise PlanError(f"destination {action.destination} is another action's source")


def _unit_paths(unit: FamilyUnit | RenameUnit) -> list[tuple[str, str]]:
    if isinstance(unit, RenameUnit):
        return [(unit.action.source, unit.unit_id)]
    return [(member.path, unit.unit_id) for member in unit.members]


def _conflicts(plan: Plan) -> dict[str, tuple[str, ...]]:
    """Units that name an archive another unit also schedules.

    Keyed by unit id; each value lists the reasons. An archive that is one
    family's keeper and another unit's quarantine (or rename) source blocks
    both units: quarantining a keeper would lose the pages it carries for
    its own dependants.
    """
    seen: dict[tuple[str, int], list[tuple[str, str]]] = {}
    for unit in plan.units:
        if isinstance(unit, RenameUnit):
            members = [(unit.action.archive_id, unit.action.location_id, "rename source")]
        else:
            members = [(unit.keeper.archive_id, unit.keeper.location_id, "keeper")] + [
                (d.member.archive_id, d.member.location_id, "quarantine candidate")
                for d in unit.dependants
            ]
        for archive_id, location_id, role in members:
            seen.setdefault(("archive", archive_id), []).append((unit.unit_id, role))
            seen.setdefault(("location", location_id), []).append((unit.unit_id, role))
    reasons: dict[str, list[str]] = {}
    for (what, ident), owners in seen.items():
        units = sorted({unit_id for unit_id, _ in owners})
        if len(units) < 2:
            continue
        for unit_id, role in owners:
            others = [f"{u} ({r})" for u, r in owners if u != unit_id]
            message = f"{what} {ident} is this unit's {role} and is also scheduled by {others}"
            if role == "keeper" and any(r == "quarantine candidate" for u, r in owners if u != unit_id):
                message += ": the keeper is scheduled for quarantine by another unit"
            reasons.setdefault(unit_id, []).append(message)
    return {unit_id: tuple(sorted(set(r))) for unit_id, r in reasons.items()}
