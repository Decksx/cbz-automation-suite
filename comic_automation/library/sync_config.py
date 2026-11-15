"""Bind sanitized, operator-supplied Syncthing exports to filesystem evidence.

No HTTP or filesystem access occurs here. Effective ignores support only the
positive expanded globs exactly representing the observer's literal policy.
Anything else stays filesystem-only. This is never a sync/cutover preflight.
"""
from __future__ import annotations

import copy
from pathlib import PurePosixPath, PureWindowsPath
import re

from comic_automation.library import sync_inventory as inventory
from comic_automation.library.sync_preview import _digest, _validated
from comic_automation.library.sync_verify import _baseline, _scope

CONTRACT = "sync-config-bound-report-v1"
CAPTURE = "sync-config-capture-v1"
SIDES = ("tower", "target")


class ConfigurationRefused(ValueError):
    """Malformed artifacts cannot support a configuration binding."""


def _require(condition, field):
    if not condition:
        # Never echo raw export values, parser details, passwords or commands.
        raise ConfigurationRefused(f"invalid {field}")


def _fingerprint(value):
    return _digest(dict(value=value), "unused")


def _text(value, field):
    _require(isinstance(value, str) and bool(value) and not any(ord(c) < 32 for c in value), field)
    return value


def _path(value, windows):
    value = _text(value, "path")
    cls = PureWindowsPath if windows else PurePosixPath
    path = cls(value)
    _require(path.is_absolute() and ".." not in path.parts, "absolute path")
    # These are declared namespace mappings, not physical alias probes. Require
    # exact normalized spelling instead of guessing case/alias equivalence.
    return str(path)


def _filesystem(report):
    """Validate the producer digest and recover its actual root observation."""
    _require(isinstance(report, dict), "filesystem report")
    contract = report.get("contract")
    if contract == inventory.CONTRACT:
        obs = _validated(report)
        _require(obs["hash_mode"] == "content", "content observation")
        complete = obs["complete"] and not obs["problems"]
    else:
        kind = {"sync-filesystem-preview-v1": "preview", "sync-filesystem-verification-v1": "verification"}.get(contract)
        _require(kind is not None, "filesystem contract")
        _require(report.get(kind + "_digest") == _digest(report, kind + "_digest"), "filesystem digest")
        obs = _validated(report["observation"] if kind == "preview" else report["actual"])
        if kind == "verification":
            old = _baseline(report["expected"])
            _require(report.get("scope_matches") == (_scope(old) == _scope(obs)), "verification scope")
        flag = report.get("complete") if kind == "preview" else report.get("verified")
        _require(type(flag) is bool and report.get("sync_ready") is False, "filesystem flags")
        complete = flag and obs["complete"] and not obs["problems"]
    _scope(obs)
    return obs, bool(complete)


def required_expanded_patterns(observation):
    """Return the sufficient positive expanded-glob set for the current observer.

    Every unanchored component needs its root/nested and descendant forms.
    The extra .syncthing.tmp literal accounts for the observer's overlapping
    startswith/endswith temporary-name test. No general ignore engine is used.
    """
    policy = observation["exclusion_policy"]
    _require(policy == dict(component_names=sorted(inventory._EXCLUDED),
                            temporary_name=".syncthing.*.tmp"), "observer exclusion policy")
    result = set()
    for name in [*policy["component_names"], ".syncthing.*.tmp", ".syncthing.tmp"]:
        result.update((name, name + "/**", "**/" + name, "**/" + name + "/**"))
    return result


def _ignores(response, required, windows):
    _require(isinstance(response, dict), "ignore response")
    raw, expanded = response.get("ignore"), response.get("expanded")
    _require(isinstance(raw, list) and isinstance(expanded, list)
             and all(isinstance(x, str) for x in raw + expanded), "ignore arrays")
    normalized, supported = set(), not bool(response.get("error"))
    for pattern in expanded:
        insensitive = pattern.startswith("(?i)")
        value = (pattern[4:] if insensitive else pattern).removeprefix("/")
        if (not (windows or insensitive) or not value.isascii()
                or value.casefold() not in required):
            supported = False
        normalized.add(value.casefold())
    # Missing or extra ignores mean the filesystem report's scope differs.
    supported = supported and normalized == required
    return dict(source_digest=_fingerprint(raw), expanded_digest=_fingerprint(expanded),
                matches_observer_policy=bool(supported))


def _versioning(value):
    _require(isinstance(value, dict), "versioning")
    kind = value.get("type", "")
    _require(isinstance(kind, str), "versioning type")
    params = value.get("params", {})
    _require(isinstance(params, dict), "versioning parameters")
    allowed = {"": set(), "simple": {"keep", "cleanoutDays"},
               "staggered": {"maxAge"}, "trashcan": {"cleanoutDays"}}
    supported = (kind in allowed and set(params) <= allowed[kind]
                 and value.get("fsPath", "") == "" and value.get("fsType", "basic") == "basic")
    clean = {}
    for key in sorted(set(params) & allowed.get(kind, set())):
        number = params[key]
        _require(isinstance(number, str) and re.fullmatch(r"[0-9]+", number) is not None, "versioning number")
        clean[key] = int(number)
    cleanup = value.get("cleanupIntervalS", 0)
    _require(type(cleanup) is int and 0 <= cleanup <= 31536000, "versioning cleanup interval")
    return dict(type=kind if kind in allowed else "unsupported", params=clean,
                cleanup_interval=cleanup, supported=bool(supported))


def _phase(value, side, selected, mappings, observation):
    _require(isinstance(value, dict), "capture phase")
    status, config, ignores = value.get("status"), value.get("config"), value.get("ignores")
    _require(isinstance(status, dict) and isinstance(config, dict) and isinstance(ignores, dict), "phase exports")
    device = _text(status.get("myID"), "local device ID")
    _require(re.fullmatch(r"[A-Za-z0-9-]+", device) is not None, "local device ID")
    separator = status.get("pathSeparator")
    _require(separator in {"/", "\\"}, "path separator")
    windows = separator == "\\"
    folder_rows, device_rows = config.get("folders"), config.get("devices")
    _require(isinstance(folder_rows, list) and isinstance(device_rows, list), "config arrays")
    folders, devices = {}, {}
    for row in folder_rows:
        _require(isinstance(row, dict), "folder")
        key = _text(row.get("id"), "folder ID")
        _require(key not in folders, "duplicate folder ID")
        folders[key] = row
    for row in device_rows:
        _require(isinstance(row, dict), "device")
        key = _text(row.get("deviceID"), "device ID")
        _require(key not in devices and type(row.get("paused")) is bool, "device binding")
        devices[key] = row["paused"]
    rows, problems = [], []
    for root in observation["roots"]:
        if root["side"] != side:
            continue
        key = root["folder_id"]
        _require(key in folders and key in ignores, "selected folder export")
        folder, mapping = folders[key], mappings[key][side]
        _require(isinstance(mapping, dict), "path mapping")
        remote = _path(folder.get("path"), windows)
        matches = (remote == _path(mapping.get("syncthing_path"), windows)
                   and _path(mapping.get("observed_path"), True) == _path(root["path"], True))
        mode, paused = folder.get("type"), folder.get("paused")
        _require(isinstance(mode, str) and type(paused) is bool, "folder role/state")
        shares = folder.get("devices")
        _require(isinstance(shares, list) and all(isinstance(d, dict) for d in shares), "folder devices")
        peers = [_text(d.get("deviceID"), "folder device ID") for d in shares]
        _require(len(peers) == len(set(peers)) and all(p == device or p in devices for p in peers), "folder peer binding")
        _require(all(isinstance(d.get("encryptionPassword", ""), str) for d in shares), "folder encryption state")
        encrypted = any(bool(d.get("encryptionPassword")) for d in shares)
        required = required_expanded_patterns(observation)
        for prefix in root["exclude_prefixes"]:
            _require(prefix.isascii(), "literal ASCII exclusion prefix")
            required.update((prefix.casefold(), prefix.casefold() + "/**"))
        ignore = _ignores(ignores[key], required, windows)
        version = _versioning(folder.get("versioning"))
        if not (matches and ignore["matches_observer_policy"] and version["supported"]
                and mode in {"sendonly", "receiveonly", "sendreceive"} and not encrypted
                and folder.get("filesystemType") == "basic" and folder.get("markerName", ".stfolder") == ".stfolder"):
            problems.append(dict(side=side, folder_id=key, reason="unsupported or mismatched folder scope"))
        rows.append(dict(folder_id=key, side=side, syncthing_path=remote,
                         observed_path=root["path"], identity=root["identity"],
                         type=mode if mode in {"sendonly", "receiveonly", "sendreceive"} else "unsupported",
                         paused=paused, peers=sorted(peers), peer_paused={p: devices[p] for p in sorted(peers) if p != device},
                         filesystem_type="basic" if folder.get("filesystemType") == "basic" else "unsupported",
                         marker=".stfolder" if folder.get("markerName", ".stfolder") == ".stfolder" else "unsupported",
                         encrypted=encrypted, versioning=version, ignores=ignore))
    return dict(device_id=device, platform="windows" if windows else "posix", folders=rows,
                unmanaged_folder_ids=sorted(set(folders) - selected)), problems


def _expected(value, expected_observation):
    _require(isinstance(value, dict) and value.get("contract") == CONTRACT, "baseline configuration contract")
    _require(value.get("binding_digest") == _digest(value, "binding_digest"), "baseline configuration digest")
    _require(value.get("scope_bound") is True and value.get("complete") is True
             and not value.get("problems"), "baseline configuration coverage")
    report = value.get("filesystem_report")
    _require(isinstance(report, dict) and report.get("contract") == inventory.CONTRACT,
             "baseline configuration observation")
    old, complete = _filesystem(report)
    _require(complete and old["observation_digest"] == expected_observation["observation_digest"],
             "baseline observation binding")
    _require(value["configuration"]["capture_stable"] is True
             and value["configuration"]["scope"]["roots"] == old["roots"]
             and value["configuration"]["scope"]["exclusion_policy"] == old["exclusion_policy"],
             "baseline configuration root binding")
    _require(value.get("scope_fingerprint") == _fingerprint(value["configuration"]["scope"]), "baseline scope fingerprint")
    return value


def bind_configuration(report: dict, capture: dict, *, expected: dict | None = None) -> dict:
    """Attach sanitized two-boundary exports; preserve filesystem-only fallback.

    `expected` is the configuration-bound baseline when binding a verification.
    Operational state changes are diagnostic; folder/path/peer/ignore scope
    changes prevent scoped verification. Neither result authorizes any action.
    """
    try:
        obs, complete = _filesystem(report)
        _require(isinstance(capture, dict) and capture.get("contract") == CAPTURE, "capture contract")
        nodes, rows = capture.get("nodes"), capture.get("mappings")
        _require(isinstance(nodes, dict) and set(nodes) == set(SIDES) and isinstance(rows, list), "capture nodes/mappings")
        selected = {r["folder_id"] for r in obs["roots"]}
        _require(selected and {(r["folder_id"], r["side"]) for r in obs["roots"]}
                 == {(key, side) for key in selected for side in SIDES}, "paired observation scope")
        mappings = {}
        for row in rows:
            _require(isinstance(row, dict), "mapping row")
            key = _text(row.get("folder_id"), "mapping folder ID")
            _require(key not in mappings and all(side in row for side in SIDES), "unique mapping")
            mappings[key] = row
        _require(set(mappings) == selected, "mapping scope")
        snapshots, problems = {}, []
        # Syncthing's lowercase/glob semantics are not Python NFC+casefold.
        # This bounded adapter certifies ASCII relative names only; Unicode
        # files still retain their valid filesystem evidence in the fallback.
        if any(not row["relative_path"].isascii() for row in obs["files"]) or any(
                not row["path"].isascii() for row in obs["excluded"]):
            problems.append(dict(reason="Unicode ignore semantics are not modeled"))
        # Syncthing reserves either temporary prefix even without .tmp. The
        # observer has a narrower suffix rule, so any such counted file makes
        # a scoped Syncthing assertion unsupported rather than disappearing.
        if any(any(part.casefold().startswith((".syncthing.", "~syncthing~"))
                   for part in row["relative_path"].split("/")) for row in obs["files"]):
            problems.append(dict(reason="native temporary-file scope differs"))
        for phase in ("before", "after"):
            snapshots[phase] = {}
            for side in SIDES:
                _require(isinstance(nodes[side], dict), "capture node")
                clean, found = _phase(nodes[side].get(phase), side, selected, mappings, obs)
                snapshots[phase][side] = clean
                problems.extend(found)
        before, after = snapshots["before"], snapshots["after"]
        _require(before["tower"]["device_id"] != before["target"]["device_id"], "distinct devices")
        stable = before == after
        if not stable:
            problems.append(dict(reason="configuration changed during capture"))
        for side in SIDES:
            own, other = after[side]["device_id"], after[SIDES[1 - SIDES.index(side)]]["device_id"]
            for row in after[side]["folders"]:
                if set(row["peers"]) not in ({own, other}, {other}):
                    problems.append(dict(side=side, folder_id=row["folder_id"], reason="unexpected folder peers"))
            cls = PureWindowsPath if after[side]["platform"] == "windows" else PurePosixPath
            paths = [cls(row["syncthing_path"]) for row in after[side]["folders"]]
            if any(a == b or a in b.parents or b in a.parents
                   for i, a in enumerate(paths) for b in paths[i + 1:]):
                problems.append(dict(side=side, reason="selected remote folder roots overlap"))
        scope = dict(roots=obs["roots"], exclusion_policy=obs["exclusion_policy"], nodes={})
        for side in SIDES:
            scope["nodes"][side] = dict(device_id=after[side]["device_id"], platform=after[side]["platform"],
                folders=[{k: v for k, v in row.items() if k in {"folder_id", "side", "syncthing_path", "observed_path", "identity", "peers", "ignores"}}
                         for row in after[side]["folders"]])
        fingerprint = _fingerprint(scope)
        state_changed = None
        if report["contract"] == "sync-filesystem-verification-v1":
            if expected is None:
                problems.append(dict(reason="baseline configuration evidence missing"))
            else:
                old = _expected(expected, report["expected"])
                if old["scope_fingerprint"] != fingerprint:
                    problems.append(dict(reason="configuration scope differs from baseline"))
                state_changed = old["configuration"]["nodes"] != after
        else:
            _require(expected is None, "unexpected baseline configuration")
        # Duplicate before/after problems are one semantic unresolved condition.
        problems = sorted({str(sorted(p.items())): p for p in problems}.values(), key=lambda p: str(sorted(p.items())))
        bound = stable and not problems
        result = dict(contract=CONTRACT, scope="configuration-evidence-bound" if bound else "filesystem-only",
            scope_bound=bool(bound), complete=bool(bound and complete), sync_ready=False,
            scoped_verified=bool(bound and complete and report["contract"] == "sync-filesystem-verification-v1"),
            filesystem_report=copy.deepcopy(report), configuration=dict(nodes=after, scope=scope, capture_stable=stable),
            scope_fingerprint=fingerprint, state_changed_since_baseline=state_changed, problems=problems,
            limits=["Operator-supplied exports and namespace mappings, not authenticated live capture or mount proof.",
                    "Two matching boundaries are non-atomic evidence, not current configuration or index authority.",
                    "No backup/capacity, global index, control action or cutover readiness is established."])
        result["binding_digest"] = _digest(result, "binding_digest")
        return result
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        if isinstance(error, ConfigurationRefused):
            raise
        raise ConfigurationRefused("invalid configuration/report structure") from None
