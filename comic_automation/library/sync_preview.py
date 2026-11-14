"""Deterministic logical file deltas from #108 filesystem observations.

This classifies evidence, never performs sync or predicts block transfers.
Syncthing configuration evidence and the fresh post-sync verifier are separate
deliveries. A complete filesystem comparison is not permission to sync.
"""
from __future__ import annotations

from collections import defaultdict
import copy
import hashlib
import json
from pathlib import PureWindowsPath
import re
import unicodedata

from comic_automation.library.sync_inventory import CONTRACT as OBSERVATION_CONTRACT

CONTRACT = "sync-filesystem-preview-v1"


class PreviewRefused(ValueError):
    """The supplied document cannot support a logical file comparison."""


def _digest(document, field):
    payload = {key: value for key, value in document.items() if key != field}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def _key(row):
    return row["folder_id"], unicodedata.normalize("NFC", row["relative_path"]).casefold()


def _ref(row):
    return {key: row[key] for key in ("folder_id", "relative_path", "size", "sha256")}


def _validated(observation):
    """Validate the producer contract and digest before deriving assertions.

    A digest detects changed artifacts, not authenticity. The observation
    producer's root, exclusion, identity and stability checks remain required.
    """
    if not isinstance(observation, dict):
        raise PreviewRefused("expected a filesystem observation document")
    doc = copy.deepcopy(observation)
    if (doc.get("contract") != OBSERVATION_CONTRACT or doc.get("scope") != "filesystem-only"
            or type(doc.get("complete")) is not bool or doc.get("hash_mode") != "content"):
        raise PreviewRefused("preview needs a content-mode filesystem observation")
    if doc.get("observation_digest") != _digest(doc, "observation_digest"):
        raise PreviewRefused("observation digest mismatch")
    if any(not isinstance(doc.get(key), list) for key in ("files", "roots", "excluded", "problems")):
        raise PreviewRefused("observation collections are malformed")
    bindings = set()
    for root in doc["roots"]:
        if (not isinstance(root, dict) or not isinstance(root.get("folder_id"), str)
                or not root["folder_id"] or root.get("side") not in {"tower", "target"}):
            raise PreviewRefused("invalid root binding")
        binding = root["folder_id"], root["side"]
        if binding in bindings:
            raise PreviewRefused("duplicate root binding")
        bindings.add(binding)
    if doc["complete"] and (not bindings or any((folder, side) not in bindings
            for folder, _ in bindings for side in ("tower", "target"))):
        raise PreviewRefused("complete observations need paired root bindings")
    seen = set()
    for row in doc["files"]:
        if not isinstance(row, dict):
            raise PreviewRefused("invalid file row")
        path = row.get("relative_path")
        if (not isinstance(row.get("folder_id"), str) or row.get("side") not in {"tower", "target"}
                or not isinstance(path, str) or not path or "\\" in path
                or any(part in {"", ".", ".."} or PureWindowsPath(part).is_reserved()
                       or PureWindowsPath(part).drive or ":" in part for part in path.split("/"))
                or type(row.get("size")) is not int or row["size"] < 0 or "sha256" not in row
                or (row["folder_id"], row["side"]) not in bindings):
            raise PreviewRefused("invalid or unbound file row")
        sha = row.get("sha256")
        if sha is not None and (not isinstance(sha, str) or re.fullmatch("[0-9a-f]{64}", sha) is None):
            raise PreviewRefused("invalid full-content hash")
        if sha is not None and (type(row.get("bytes_read")) is not int or row["bytes_read"] != row["size"]):
            raise PreviewRefused("hash has no certified full read")
        identity = row["side"], _key(row)
        if identity in seen:
            raise PreviewRefused("duplicate or colliding file paths")
        seen.add(identity)
    observed_counts = {side: sum(row["side"] == side for row in doc["files"])
                       for side in ("tower", "target")}
    if doc.get("file_counts") != {side: count for side, count in observed_counts.items() if count}:
        raise PreviewRefused("observation file counts disagree")
    sizes = {side: {row["size"] for row in doc["files"] if row["side"] == side}
             for side in ("tower", "target")}
    common = sizes["tower"] & sizes["target"]
    if doc["complete"] and any(row["size"] in common and not row["sha256"] for row in doc["files"]):
        raise PreviewRefused("complete observation lacks required content evidence")
    return doc


def build_preview(observation: dict) -> dict:
    """Classify every observed file, binding the full observation and counts.

    Keeps require equal stable SHA-256 at the exact corresponding path.
    Size inequality alone proves a replacement. A complete inventory proves
    path absence; other-path byte matches remain advisory groups, not keeps.
    Incomplete coverage makes every observed file unresolved. Case/Unicode
    spelling disagreements across sides are unresolved rather than proposing
    a write/removal pair that could address one Windows file.
    """
    doc = _validated(observation)
    rows = sorted(doc["files"], key=lambda row: (row["folder_id"], row["side"], row["relative_path"]))
    sides = {side: {_key(row): row for row in rows if row["side"] == side}
             for side in ("tower", "target")}
    routes = {side: {} for side in sides}
    kept, writes, removes, unresolved, metadata = [], [], [], [], []

    def hold(side, key, reason):
        row = sides[side][key]
        routes[side][key] = "unresolved"
        unresolved.append(dict(side=side, file=_ref(row), reason=reason))

    trustworthy = doc["complete"] and not doc["problems"]
    if not trustworthy:
        for side in sides:
            for key in sides[side]:
                hold(side, key, "incomplete observation coverage or stability")
    else:
        for key, tower in sides["tower"].items():
            target = sides["target"].get(key)
            if target is None:
                writes.append(dict(kind="new_path", tower=_ref(tower), old_target=None))
                routes["tower"][key] = "write"
            elif tower["relative_path"] != target["relative_path"]:
                for side in sides:
                    hold(side, key, "corresponding Windows path has different spelling")
            elif tower["size"] != target["size"]:
                writes.append(dict(kind="replacement", tower=_ref(tower), old_target=_ref(target)))
                routes["tower"][key], routes["target"][key] = "write", "replaced"
            elif tower["sha256"] == target["sha256"]:
                kept.append(dict(tower=_ref(tower), target=_ref(target)))
                routes["tower"][key] = routes["target"][key] = "keep"
                if tower.get("mtime_ns") != target.get("mtime_ns"):
                    metadata.append(dict(tower=_ref(tower), target=_ref(target), fields=["mtime_ns"]))
            else:
                writes.append(dict(kind="replacement", tower=_ref(tower), old_target=_ref(target)))
                routes["tower"][key], routes["target"][key] = "write", "replaced"
        for key, target in sides["target"].items():
            if key not in sides["tower"]:
                removes.append(dict(target=_ref(target)))
                routes["target"][key] = "remove"

    # Groups keep many-to-many mappings linear in files, avoiding a quadratic
    # list of Cartesian pairs. Group membership never changes path accounting.
    groups = defaultdict(lambda: dict(tower=[], target=[]))
    if trustworthy:
        for side in sides:
            for row in sides[side].values():
                if row["sha256"]:
                    groups[row["size"], row["sha256"]][side].append(_ref(row))
    matches = []
    for (size, sha), members in sorted(groups.items()):
        paths = {(row["folder_id"], row["relative_path"]) for side in sides for row in members[side]}
        if members["tower"] and members["target"] and len(paths) > 1:
            matches.append(dict(size=size, sha256=sha, **members))

    counts = {}
    reconciled = True
    for side in sides:
        slots = ("keep", "write", "unresolved") if side == "tower" else ("keep", "replaced", "remove", "unresolved")
        counts[side] = {slot: dict(files=0, bytes=0) for slot in slots}
        counts[side]["observed"] = dict(files=len(sides[side]), bytes=sum(row["size"] for row in sides[side].values()))
        for key, slot in routes[side].items():
            counts[side][slot]["files"] += 1
            counts[side][slot]["bytes"] += sides[side][key]["size"]
        reconciled = reconciled and routes[side].keys() == sides[side].keys()
    result = dict(contract=CONTRACT, scope="filesystem-only", complete=trustworthy and not unresolved and reconciled,
                  sync_ready=False, observation=doc, keep_in_place=kept, to_write=writes,
                  to_remove=removes, unresolved=unresolved, metadata_differences=metadata,
                  exact_matches_elsewhere=matches, counts=counts, reconciled=reconciled,
                  limits=["Logical file delta, not predicted network/disk I/O or Syncthing actions.",
                          "No bound Syncthing configuration evidence; sync readiness is not established.",
                          "Non-atomic observation; later changes require a fresh comparison."])
    result["preview_digest"] = _digest(result, "preview_digest")
    return result
