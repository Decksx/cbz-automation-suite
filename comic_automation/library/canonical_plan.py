"""Read-only canonical naming planner (#112 step 8).

Emits ``canonical-naming-plan-v1`` without importing the unmerged step-7
executor. Root inventory, catalog mapping, step-4 field resolution and step-5
byte-bound supersession are covered by one outer WAL-aware read window. The
filesystem is observed, not locked: apply must still revalidate every hash.
Only private temporary copies used by ``derive_archive`` may be written.
No schema, journal, library directory or database row is created or changed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import unicodedata
from collections import Counter, defaultdict
from datetime import date
from ntpath import normcase as windows_normcase
from pathlib import Path
from typing import Any

from comic_automation.archive import field_resolution as fr
from comic_automation.archive import supersession as sv
from comic_automation.archive.content_signature_recertify import file_identity
from comic_automation.database.read_guards import read_consistent_snapshot
from scripts.cbz_canonical_name import (
    CanonicalFields, CanonicalNameError, format_canonical_name,
)
from scripts.cbz_routing import NON_SERIES_DIRECTORIES

CONTRACT = "canonical-naming-plan-v1"
PLANNER_VERSION = 1
_INTERNAL = NON_SERIES_DIRECTORIES | {
    ".cbz-canonical-naming", ".cbz-canonical-quarantine",
}
_DECENSOR = re.compile(r"\b(?:uncensored|decensored)\b", re.I)


class PlanningRefused(ValueError):
    """No trustworthy complete plan can be emitted for this observation."""


def canonical_bytes(document: dict[str, Any]) -> bytes:
    """The executor's digest bytes, including all provenance and review rows."""
    return json.dumps(
        {k: v for k, v in document.items() if k != "plan_digest"},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _key(path: str | Path) -> str:
    # Windows destination collisions must be detected even on a POSIX test host.
    return unicodedata.normalize("NFC", os.path.abspath(path)).casefold()


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _safe_destination(path: str, root: Path) -> bool:
    """Refuse existing reparse components, including excluded state trees."""
    target = Path(path)
    if not _under(target, root) or not _under(target.resolve(), root.resolve()):
        return False
    current = root
    for component in target.relative_to(root).parts:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if _reparse(info):
            return False
    return True


def _inventory(root: Path) -> tuple[list[dict], list[Path], list[dict]]:
    """Walk without following reparse points; unreadable directories abort."""
    rows, books, excluded = [], [], []
    pending = [root]
    while pending:
        folder = pending.pop()
        for child in sorted(folder.iterdir(), key=lambda p: p.name):
            info = child.lstat()
            relative = str(child.relative_to(root))
            if child.name.casefold() in _INTERNAL:
                excluded.append({"path": relative, "reason": "internal/history"})
                continue
            if _reparse(info):
                excluded.append({"path": relative, "reason": "reparse point"})
                continue
            directory = stat.S_ISDIR(info.st_mode)
            rows.append({
                "path": relative, "kind": "directory" if directory else "file",
                "size": info.st_size, "mtime_ns": info.st_mtime_ns,
                "st_dev": info.st_dev, "st_ino": info.st_ino,
            })
            if directory:
                pending.append(child)
            elif stat.S_ISREG(info.st_mode) and child.suffix.casefold() == ".cbz":
                books.append(child)
    return sorted(rows, key=lambda r: r["path"]), sorted(books), sorted(
        excluded, key=lambda r: r["path"]
    )


def _destination(resolution: fr.ArchiveResolution) -> str:
    """Format resolved fields, refusing any offered but unresolved field."""
    if not resolution.renamable:
        raise PlanningRefused("; ".join(resolution.review))
    fields = resolution.fields
    tokens = fr.parse_name_tokens(resolution.filename)
    # The resolver retains a bare vN as filename-grade volume evidence even
    # after classifying that occurrence as a version. Its explicit verdict,
    # not omission of an unresolved optional field, permits this conversion.
    version_only_volume_candidate = (
        fields["volume"].reason == "filename_candidate_only"
        and bool(resolution.v_tokens) and not tokens.volume_words
        and all(v.classification == fr.V_VERSION for v in resolution.v_tokens)
    )
    volume_only_version_candidate = (
        fields["version"].reason == "filename_candidate_only"
        and bool(resolution.v_tokens) and not tokens.version_markers
        and all(v.classification == fr.V_VOLUME for v in resolution.v_tokens)
    )
    for field in fields.values():
        if field.field == "volume" and version_only_volume_candidate:
            continue
        if field.field == "version" and volume_only_version_candidate:
            continue
        if field.has_evidence and not field.resolved:
            raise PlanningRefused(f"{field.field} unresolved ({field.reason})")
    series = fields["series"].display_value or fields["series"].value
    markers = fr.effective_markers(tokens, series)
    if markers.ambiguity or tokens.other_tags:
        raise PlanningRefused(markers.ambiguity or f"unclassified tags: {sorted(tokens.other_tags)}")
    editions = markers.editions
    if len(editions) > 1 or len(markers.specials) > 1:
        raise PlanningRefused("multiple edition/special markers cannot fit the grammar")
    value = lambda field: fields[field].display_value or fields[field].value
    volume, number = value("volume"), value("number")
    return format_canonical_name(CanonicalFields(
        series=series, volume=volume,
        issue=number if volume is not None else None,
        chapter=number if volume is None else None,
        year=value("year"), version=value("version"),
        edition=next(iter(editions), None), special=next(iter(markers.specials), None),
    ))


def _counts(doc: dict) -> dict[str, int]:
    return {
        "renames": len(doc["renames"]), "families": len(doc["families"]),
        "quarantines": sum(len(f["dependants"]) for f in doc["families"]),
        "keeper_renames": sum(f["keeper"]["rename_to"] is not None for f in doc["families"]),
        "review": len(doc["review"]),
    }


def _remove_collisions(doc: dict, inventory: list[dict], root: Path) -> None:
    """Hold every unit in a collision, including a whole dependent family."""
    occupied = defaultdict(list)
    for row in inventory:
        occupied[_key(root / row["path"])].append(str(root / row["path"]))
    candidates = []
    for row in doc["renames"]:
        candidates.append((row["action_id"], row["source"], row["destination"]))
    for family in doc["families"]:
        keeper = family["keeper"]
        if keeper["rename_to"] is not None:
            candidates.append((family["family_id"], keeper["path"], keeper["rename_to"]))
        candidates.extend((family["family_id"], d["path"], d["quarantine_to"]) for d in family["dependants"])
    destinations = defaultdict(list)
    blocked = defaultdict(list)
    for unit, source, destination in candidates:
        destinations[_key(destination)].append(unit)
        if not _safe_destination(destination, root):
            blocked[unit].append(f"destination escapes or traverses a reparse point: {destination}")
        occupants = occupied[_key(destination)]
        own_case_only = (
            occupants == [source]
            and windows_normcase(source) == windows_normcase(destination)
        )
        if occupants and not own_case_only:
            blocked[unit].append(f"occupied destination: {destination}")
        # Quarantine contents are excluded from traversal but never assumed empty.
        if not occupants and os.path.lexists(destination):
            blocked[unit].append(f"existing destination: {destination}")
    for destination, units in destinations.items():
        if len(units) > 1:
            for unit in units:
                blocked[unit].append(f"colliding proposed destination: {destination}")
    for kind, id_field in (("renames", "action_id"), ("families", "family_id")):
        kept = []
        for row in doc[kind]:
            unit = row[id_field]
            if unit in blocked:
                doc["review"].append({"unit": unit, "reasons": sorted(set(blocked[unit])), "proposal": row})
            else:
                kept.append(row)
        doc[kind] = kept


def build_plan(
    library_root: str | Path, *, database: str | Path,
    quarantine_date: str, accept_comicinfo_drop: bool = False,
    max_held_bytes: int = sv.DEFAULT_MAX_HELD_BYTES,
    max_copy_bytes: int | None = None, temp_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Produce a sealed executor-compatible plan; read only, no live locks.

    No folder name is promoted to decided series authority. Human decision
    manifests and decensor pairing adapters are not implemented; uncertain
    fields and decensor-marked paths are held. All supplied paths are exact
    catalog spellings (drive/UNC aliases are not guessed). Any concurrent DB
    commit, target replacement or changed root inventory refuses the run.
    """
    if date.fromisoformat(quarantine_date).isoformat() != quarantine_date:
        raise PlanningRefused("quarantine_date must be YYYY-MM-DD")
    root = Path(os.path.abspath(library_root))
    if not root.is_dir():
        raise PlanningRefused(f"library root is not a directory: {root}")
    if any(part.casefold() == "ai-decensor-originals" for part in root.resolve().parts):
        raise PlanningRefused("ai-decensor-originals is preserved and outside planner scope")
    temporary = Path(temp_dir if temp_dir is not None else tempfile.gettempdir()).resolve(strict=True)
    if not temporary.is_dir() or _under(temporary, root.resolve(strict=True)):
        raise PlanningRefused("private temporary copies must be outside the library")
    if max_held_bytes < 0 or (max_copy_bytes is not None and max_copy_bytes < 0):
        raise PlanningRefused("byte limits must be nonnegative")
    target = file_identity(database)
    root_identity = (root.stat().st_dev, root.stat().st_ino, str(root.resolve(strict=True)))

    def read(connection: sqlite3.Connection) -> dict:
        if file_identity(database) != target:
            raise PlanningRefused("database target changed before catalog read")
        before, books, excluded = _inventory(root)
        catalog = [dict(row) for row in connection.execute(
            "SELECT l.id, l.archive_id, l.path, a.id AS owner "
            "FROM file_locations l LEFT JOIN archive_files a ON a.id=l.archive_id "
            "WHERE l.is_current=1 ORDER BY l.id"
        )]
        by_path = defaultdict(list)
        owners = Counter(row["archive_id"] for row in catalog)
        for row in catalog:
            by_path[os.path.normcase(row["path"])].append(row)
        mapping, mapping_problems = {}, {}
        for path in books:
            rows = by_path[os.path.normcase(str(path))]
            if len(rows) != 1 or rows[0]["owner"] is None:
                mapping_problems[str(path)] = "no unique current catalog location"
            elif owners[rows[0]["archive_id"]] != 1:
                mapping_problems[str(path)] = "archive has multiple current locations"
            else:
                mapping[str(path)] = rows[0]
        ids = sorted({row["archive_id"] for row in mapping.values()})
        # This nested reader reuses the established page-evidence contract. The
        # outer connection remains open across it AND every later derivation,
        # so different per-folder reads cannot silently combine DB generations.
        pages = fr.load_page_evidence(database, ids).result
        doc = {
            "contract": CONTRACT, "library_root": str(root), "database_target": target,
            "quarantine_root": str(root / ".cbz-canonical-quarantine" / quarantine_date),
            "renames": [], "families": [], "review": [], "unchanged": [],
        }
        folders = defaultdict(list)
        for path in books:
            folders[path.parent].append(path)
        for folder, paths in sorted(folders.items()):
            derived, evaluations, facts = {}, {}, []
            for path in paths:
                key = str(path)
                row = mapping.get(key)
                archive_id = None if row is None else row["archive_id"]
                bound = sv.derive_archive(path, max_held_bytes=max_held_bytes,
                    max_copy_bytes=max_copy_bytes, temp_dir=temporary)
                derived[key] = bound
                evaluations[key] = sv.ArchiveEvaluation(key, path.name, archive_id, bound,
                    sv.stored_rows_verdict(bound, pages.get(archive_id), archive_id))
                facts.append(fr.ArchiveFacts(key, path.name,
                    fr.comicinfo_facts(bound.comicinfo_documents), bound.page_evidence()))
            resolved = fr.resolve_folder(fr.FolderFacts(None, tuple(facts)))
            resolutions = resolved.by_key()
            reasons, destinations = {}, {}
            for path in paths:
                key = str(path)
                problems = []
                if key in mapping_problems:
                    problems.append(mapping_problems[key])
                if derived[key].unresolved_reason:
                    problems.append(derived[key].unresolved_reason)
                series = resolutions[key].fields["series"].value or ""
                # Underscores delimit the same markers the resolver accepts
                # (e.g. Series_Uncensored); regex word boundaries alone do not.
                identity_text = " ".join((root.name, str(path.relative_to(root)), series)).replace("_", " ")
                if _DECENSOR.search(identity_text):
                    problems.append("decensor identity/pairing adapter required")
                try:
                    destinations[key] = str(path.with_name(_destination(resolutions[key])))
                except (PlanningRefused, CanonicalNameError) as exc:
                    problems.append(str(exc))
                reasons[key] = problems
            claimed = set()
            for family in resolved.families:
                members = sorted(family.members)
                # Step 4 emits candidate groups even for singleton, unmarked
                # books. Such a group must not swallow an ordinary rename.
                if not any(
                    fr.parse_name_tokens(resolutions[key].filename).version_markers
                    or any(v.classification != fr.V_VOLUME for v in resolutions[key].v_tokens)
                    for key in members
                ):
                    continue
                claimed.update(members)
                verdict = sv.evaluate_family(family, evaluations, resolutions,
                    accept_comicinfo_drop=accept_comicinfo_drop)
                problems = list(verdict.reasons)
                problems.extend(f"{key}: {p}" for key in members for p in reasons[key])
                if verdict.status != sv.FAMILY_SUPERSESSION:
                    problems.append(f"family status: {verdict.status}")
                if verdict.label is None or verdict.label.status != sv.LABEL_RESOLVED:
                    problems.append("keeper version label is not resolved")
                if problems:
                    doc["review"].append({"members": members, "reasons": problems,
                        "evaluation": verdict.as_dict()})
                    continue
                family_id = "family-" + hashlib.sha256("\0".join(members).encode()).hexdigest()[:24]

                def member(key: str) -> dict:
                    row = mapping[key]
                    return {"archive_id": row["archive_id"], "location_id": row["id"],
                        "path": key, "sha256": derived[key].sha256, "pairings": []}

                keeper = member(verdict.keeper)
                rename_to = destinations[verdict.keeper]
                keeper["rename_to"] = None if rename_to == verdict.keeper else rename_to
                keeper["action_id"] = None if keeper["rename_to"] is None else f"{family_id}:keeper"
                dependants = []
                for index, pointer in enumerate(verdict.pointers, 1):
                    dep = member(pointer.older)
                    dep.update(action_id=f"{family_id}:quarantine:{index}",
                        quarantine_to=str(Path(doc["quarantine_root"]) / family_id /
                            f"{dep['archive_id']}-{Path(pointer.older).name}"),
                        relation=pointer.relation, outcome=pointer.outcome,
                        index_map=list(pointer.index_map))
                    dependants.append(dep)
                doc["families"].append({"family_id": family_id, "basis": "sha256",
                    "accept_comicinfo_drop": accept_comicinfo_drop, "snapshot": {},
                    "keeper": keeper, "dependants": dependants})
            for path in paths:
                key = str(path)
                if key in claimed:
                    continue
                if reasons[key]:
                    doc["review"].append({"path": key, "reasons": reasons[key],
                        "resolution": resolutions[key].as_dict()})
                elif destinations[key] == key:
                    doc["unchanged"].append({"path": key, "sha256": derived[key].sha256})
                else:
                    row = mapping[key]
                    doc["renames"].append({"action_id": f"rename-{row['id']}",
                        "archive_id": row["archive_id"], "location_id": row["id"],
                        "source": key, "destination": destinations[key],
                        "sha256": derived[key].sha256, "pairings": [],
                        "evidence": resolutions[key].as_dict()})
        _remove_collisions(doc, before, root)
        after, after_books, after_excluded = _inventory(root)
        if (before, books, excluded) != (after, after_books, after_excluded):
            raise PlanningRefused("library inventory changed during planning")
        doc["provenance"] = {
            "planner_version": PLANNER_VERSION, "inventory": before,
            "excluded": excluded, "catalog": catalog,
            "series_folder_authority": "none; directory names are not decisions",
            "filesystem_snapshot": "non-atomic observation; apply revalidates strong hashes",
            "accept_comicinfo_drop": accept_comicinfo_drop,
            "max_held_bytes": max_held_bytes, "max_copy_bytes": max_copy_bytes,
            "content_signature_algorithm": fr.CONTENT_SIGNATURE_ALGORITHM,
            "content_signature_version": fr.CONTENT_SIGNATURE_VERSION,
        }
        return doc

    snapshot = read_consistent_snapshot(database, read, context="canonical naming plan")
    if file_identity(database) != target:
        raise PlanningRefused("database target changed during planning")
    if root_identity != (root.stat().st_dev, root.stat().st_ino, str(root.resolve(strict=True))):
        raise PlanningRefused("library root identity changed during planning")
    doc = snapshot.result
    doc["provenance"]["snapshot"] = snapshot.report_fields()
    for family in doc["families"]:
        family["snapshot"] = {"database": target, **snapshot.report_fields()}
    doc["counts"] = _counts(doc)
    doc["plan_digest"] = hashlib.sha256(canonical_bytes(doc)).hexdigest()
    return doc
