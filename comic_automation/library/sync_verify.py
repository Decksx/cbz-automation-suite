"""Fresh file-level verification against a fully observed desired baseline.

The public API collects the actual observation itself; a saved actual document
cannot be substituted for a fresh read. Both observations and their scope are
bound into the result. This has no Syncthing control or configuration authority.
"""
from __future__ import annotations

from comic_automation.library import sync_inventory as inventory
from comic_automation.library.sync_preview import (
    PreviewRefused, _digest, _directory_holds, _ref, _validated,
)

CONTRACT = "sync-filesystem-verification-v1"


class VerificationRefused(ValueError):
    """The expected baseline or a producer document cannot support verification."""


def _path(row):
    # Spelling is part of the desired layout. Equal bytes at a different path,
    # including a case-only spelling, must not turn into a successful verify.
    return row["folder_id"], row["relative_path"]


def _scope(doc):
    """The same roots, physical identities and exclusions must be compared."""
    policy = doc.get("exclusion_policy")
    if (not isinstance(policy, dict) or not isinstance(policy.get("component_names"), list)
            or not all(isinstance(name, str) for name in policy["component_names"])
            or not isinstance(policy.get("temporary_name"), str) or not policy["temporary_name"]):
        raise VerificationRefused("observation exclusion policy is malformed")
    for root in doc["roots"]:
        identity = root.get("identity")
        if (not isinstance(root.get("path"), str) or not root["path"]
                or not isinstance(identity, list) or len(identity) != 2
                or not all(type(value) is int for value in identity) or not identity[1]
                or not isinstance(root.get("exclude_prefixes"), list)
                or not all(isinstance(value, str) for value in root["exclude_prefixes"])):
            raise VerificationRefused("observation root binding is malformed")
    return dict(roots=sorted(doc["roots"], key=lambda row: (row["folder_id"], row["side"])),
                exclusion_policy=policy)


def _baseline(document):
    try:
        doc = _validated(document)
    except PreviewRefused as error:
        raise VerificationRefused(f"expected baseline: {error}") from error
    _scope(doc)
    if not doc["complete"] or doc["problems"]:
        raise VerificationRefused("expected baseline must have complete coverage")
    tower = [row for row in doc["files"] if row["side"] == "tower"]
    if any(not row["sha256"] for row in tower):
        raise VerificationRefused("expected Tower files need full hashes; collect a baseline with hash_all_tower=True")
    if _directory_holds(tower):
        raise VerificationRefused("expected Tower directory spellings conflict")
    return doc


def verify_pairs(expected: dict, pairs: tuple[inventory.RootPair, ...], *, progress=None) -> dict:
    """Take a fresh read and verify the expected Tower file layout and bytes.

    Validate the complete, fully hashed baseline before payload reads. Collect
    fresh full-Tower evidence, compare scope, detect source additions/removals
    and byte changes, and compare each expected path on the receiver. Metadata
    changes with equal strong bytes are separate diagnostics. Incomplete actual
    coverage or changed scope never establishes absence. A verified result is
    file-level evidence, not an atomic snapshot or permission to sync/cut over.
    """
    baseline = _baseline(expected)
    try:
        actual = _validated(inventory.observe_pairs(pairs, hash_all_tower=True, progress=progress))
    except PreviewRefused as error:
        raise VerificationRefused(f"actual observation: {error}") from error
    scope_matches = _scope(baseline) == _scope(actual)
    coverage = actual["complete"] and not actual["problems"]
    desired = {_path(row): row for row in baseline["files"] if row["side"] == "tower"}
    source = {_path(row): row for row in actual["files"] if row["side"] == "tower"}
    target = {_path(row): row for row in actual["files"] if row["side"] == "target"}
    sets = dict(expected_tower=desired, current_tower=source, target=target)
    routes = {side: {} for side in sets}
    matched, missing, mismatched, extra, unresolved, drift = [], [], [], [], [], []
    source_metadata, target_metadata = [], []

    def hold(side, key, reason):
        routes[side][key] = "unresolved"
        unresolved.append(dict(side=side, file=_ref(sets[side][key]), reason=reason))

    if not coverage or not scope_matches:
        reason = "actual coverage incomplete" if not coverage else "root/path/exclusion scope differs"
        for side, rows in sets.items():
            for key in rows:
                hold(side, key, reason)
    else:
        for key, row in source.items():
            old = desired.get(key)
            if not row["sha256"]:
                hold("current_tower", key, "fresh source lacks full-content evidence")
            elif old is None:
                routes["current_tower"][key] = "added"
                drift.append(dict(kind="added", expected=None, actual=_ref(row)))
            elif row["size"] != old["size"] or row["sha256"] != old["sha256"]:
                routes["current_tower"][key] = "changed"
                drift.append(dict(kind="changed", expected=_ref(old), actual=_ref(row)))
            else:
                routes["current_tower"][key] = "unchanged"
                fields = [field for field in ("device", "inode", "mtime_ns", "ctime_ns")
                          if row.get(field) != old.get(field)]
                if fields:
                    source_metadata.append(dict(file=_ref(row), fields=fields))
        for key, old in desired.items():
            if key not in source:
                drift.append(dict(kind="removed", expected=_ref(old), actual=None))
        for key, old in desired.items():
            received = target.get(key)
            if received is None:
                routes["expected_tower"][key] = "missing"
                missing.append(_ref(old))
            elif old["size"] != received["size"]:
                routes["expected_tower"][key] = routes["target"][key] = "mismatched"
                mismatched.append(dict(expected=_ref(old), target=_ref(received), reason="size differs"))
            elif not received["sha256"]:
                hold("expected_tower", key, "receiver lacks required full-content evidence")
                hold("target", key, "receiver lacks required full-content evidence")
            elif old["sha256"] != received["sha256"]:
                routes["expected_tower"][key] = routes["target"][key] = "mismatched"
                mismatched.append(dict(expected=_ref(old), target=_ref(received), reason="content differs"))
            else:
                routes["expected_tower"][key] = routes["target"][key] = "matched"
                matched.append(dict(expected=_ref(old), target=_ref(received)))
                if old.get("mtime_ns") != received.get("mtime_ns"):
                    target_metadata.append(dict(file=_ref(received), fields=["mtime_ns"]))
        for key, row in target.items():
            if key not in desired:
                routes["target"][key] = "extra"
                extra.append(_ref(row))

    counts = {}
    reconciled = True
    slots = dict(expected_tower=("matched", "missing", "mismatched", "unresolved"),
                 current_tower=("unchanged", "added", "changed", "unresolved"),
                 target=("matched", "mismatched", "extra", "unresolved"))
    for side, rows in sets.items():
        counts[side] = {slot: dict(files=0, bytes=0) for slot in slots[side]}
        counts[side]["observed"] = dict(files=len(rows), bytes=sum(row["size"] for row in rows.values()))
        for key, slot in routes[side].items():
            counts[side][slot]["files"] += 1
            counts[side][slot]["bytes"] += rows[key]["size"]
        reconciled = reconciled and routes[side].keys() == rows.keys()
    verified = bool(coverage and scope_matches and reconciled
                    and not (drift or missing or mismatched or extra or unresolved))
    result = dict(contract=CONTRACT, scope="filesystem-only", verified=verified, sync_ready=False,
                  expected=baseline, actual=actual, scope_matches=scope_matches,
                  coverage=dict(expected=True, actual=bool(coverage)), source_drift=drift,
                  matched=matched, missing=missing, mismatched=mismatched, extra=extra,
                  unresolved=unresolved, source_metadata_differences=source_metadata,
                  target_metadata_differences=target_metadata, counts=counts, reconciled=reconciled,
                  excluded=dict(expected=baseline["excluded"], actual=actual["excluded"]),
                  limits=["Fresh non-atomic file observations; later changes require a new comparison.",
                          "Empty directories and directory-operation ordering are outside file accounting.",
                          "No bound Syncthing configuration evidence or cutover authority."])
    result["verification_digest"] = _digest(result, "verification_digest")
    inventory._emit(progress, phase="verification", verified=verified, counts=counts,
                    source_drift=len(drift), missing=len(missing), mismatched=len(mismatched), extra=len(extra))
    return result
