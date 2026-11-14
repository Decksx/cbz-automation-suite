"""Read-only filesystem observations for the #108 sync preview and verifier.

Explicit root pairs define the observed scope. Every file is inventoried before
hash selection, then every tree is inventoried again to expose observed drift.
These observations are non-atomic and carry no Syncthing configuration claim.
"""
from __future__ import annotations

from collections import Counter
import copy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import unicodedata

from comic_automation.library.canonical_apply.containment import is_reparse
from comic_automation.library.exclusions import DEFAULT_EXCLUDED_DIRECTORIES
from comic_automation.library.writer_gate import CONTROL_FILENAMES

CONTRACT = "sync-filesystem-observation-v1"
_PROTECTED = frozenset({"ai-decensor-originals", "ai_decensor_originals"})
# Writer-gate and canonical-naming control files are per-library state, never
# library content: a sync must not carry them and an inventory must not count them.
_EXCLUDED = DEFAULT_EXCLUDED_DIRECTORIES | _PROTECTED | {".stignore"} | CONTROL_FILENAMES
_BLOCK = 1024 * 1024


class ProgressError(RuntimeError):
    """A progress consumer failed and observation must stop."""


def _emit(callback, **event) -> None:
    if callback is None:
        return
    try:
        callback(copy.deepcopy(event))
    except Exception as error:
        # A failed renderer must escape the filesystem-error handlers, including
        # when its own exception is OSError or ValueError.
        raise ProgressError("progress consumer failed") from error


@dataclass(frozen=True)
class RootPair:
    """One folder ID's desired source and receiver, with excluded path prefixes.

    Prefixes are literal relative paths shared by both sides. Built-in history,
    control and protected-original names are excluded at every depth. The
    caller must separately establish effective Syncthing scope/configuration.
    """

    folder_id: str
    tower: Path
    target: Path
    exclude_prefixes: tuple[str, ...] = ()


def _key(relative: str) -> str:
    return unicodedata.normalize("NFC", relative).casefold()


def _portable(relative: str) -> None:
    # Reject names Windows cannot represent before a later report could
    # collapse two different source entries into one receiver path.
    for part in relative.split("/"):
        if (not part or part in {".", ".."} or part.endswith((".", " "))
                or PureWindowsPath(part).is_reserved()
                or any(c in '<>:"\\|?*' or ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in part)):
            raise ValueError("path cannot be represented on Windows")


def _prefixes(values: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("exclusions must be a sequence of path prefixes")
    normalized = []
    for value in values:
        if not isinstance(value, str) or not value:
            raise ValueError("exclusions must be non-empty relative path prefixes")
        value = value.replace("\\", "/")
        if PurePosixPath(value).is_absolute():
            raise ValueError("exclusions must be relative path prefixes")
        _portable(value)
        normalized.append(value)
    return tuple(sorted(set(normalized)))


def _stamp(info: os.stat_result) -> dict:
    return dict(device=info.st_dev, inode=info.st_ino, size=info.st_size,
                mtime_ns=info.st_mtime_ns, ctime_ns=info.st_ctime_ns)


def _identity(info: os.stat_result) -> tuple[int, int]:
    if not info.st_ino:
        raise ValueError("filesystem identity is unavailable")
    return info.st_dev, info.st_ino


def _bind(path: Path) -> tuple[Path, tuple[int, int], set[tuple[int, int]]]:
    path = path.expanduser()
    supplied = path if path.is_absolute() else Path.cwd() / path
    ancestors = set()
    for component in (*reversed(supplied.parents), supplied):
        info = component.lstat()
        if is_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("root or ancestor is a link or is not a directory")
        if component.name.casefold() in _PROTECTED:
            raise ValueError("protected originals cannot be an observed root")
        ancestors.add(_identity(info))
    if supplied.name.casefold() in _EXCLUDED:
        raise ValueError("excluded directory cannot be an observed root")
    resolved = supplied.resolve(strict=True)
    return resolved, _identity(resolved.stat()), ancestors


def _excluded(relative: str, prefixes: tuple[str, ...]) -> str | None:
    parts = relative.split("/")
    if any(part.casefold() in _EXCLUDED for part in parts):
        return "history, control or protected originals"
    if any(part.casefold().startswith(".syncthing.") and part.casefold().endswith(".tmp") for part in parts):
        return "Syncthing temporary name"
    folded = _key(relative)
    if any(folded == _key(p) or folded.startswith(_key(p) + "/") for p in prefixes):
        return "declared path prefix"
    return None


def _walk(root: Path, prefixes: tuple[str, ...], *, on_progress=None) -> tuple[list[dict], list[dict], list[dict]]:
    rows, excluded, problems = [], [], []
    names = {}
    files_seen = 0
    pending = [root]
    while pending:
        folder = pending.pop()
        relative = folder.relative_to(root).as_posix()
        try:
            info = folder.lstat()
            if is_reparse(info) or not stat.S_ISDIR(info.st_mode):
                raise ValueError("directory became a link or changed type")
            with os.scandir(folder) as listing:
                children = sorted(listing, key=lambda entry: entry.name)
        except (OSError, ValueError) as error:
            problems.append(dict(path=relative, reason=str(error)))
            continue
        rows.append(dict(relative_path=relative, kind="directory", **_stamp(info)))
        for child in children:
            path = folder / child.name
            relative = path.relative_to(root).as_posix()
            reason = _excluded(relative, prefixes)
            if reason:
                excluded.append(dict(path=relative, reason=reason))
                continue
            try:
                _portable(relative)
                key = _key(relative)
                if key in names:
                    raise ValueError(f"Windows path collision with {names[key]}")
                names[key] = relative
                info = path.lstat()
                _identity(info)
                if is_reparse(info):
                    raise ValueError("unsupported link or reparse point")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    if info.st_nlink > 1:
                        raise ValueError("hard-linked file is unsupported")
                    rows.append(dict(relative_path=relative, kind="file", **_stamp(info)))
                    files_seen += 1
                else:
                    raise ValueError("unsupported filesystem entry type")
            except (OSError, ValueError) as error:
                problems.append(dict(path=relative, reason=str(error)))
        if on_progress is not None:
            on_progress(dict(path=folder.relative_to(root).as_posix(), entries=len(rows),
                             files=files_seen, excluded=len(excluded), problems=len(problems)))
    order = lambda row: row.get("relative_path", row.get("path", ""))
    return sorted(rows, key=order), sorted(excluded, key=order), sorted(problems, key=order)


def _plain_chain(root: Path, path: Path) -> None:
    current = root
    for part in (".", *path.relative_to(root).parts):
        current /= part
        if is_reparse(current.lstat()):
            raise ValueError("path became a link or reparse point")


def _hash(root: Path, row: dict, *, on_bytes=None) -> tuple[str, int]:
    path = root / row["relative_path"]
    expected = {key: row[key] for key in ("device", "inode", "size", "mtime_ns", "ctime_ns")}
    _plain_chain(root, path)
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as source:
        if _stamp(os.fstat(source.fileno())) != expected:
            raise ValueError("file changed before hashing")
        # One extra byte detects growth while bounding reads even if another
        # process keeps appending. An incomplete read never supplies a hash.
        while total <= row["size"]:
            chunk = source.read(min(_BLOCK, row["size"] + 1 - total))
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            if on_bytes is not None:
                on_bytes(len(chunk))
        if total != row["size"] or _stamp(os.fstat(source.fileno())) != expected:
            raise ValueError("file changed during hashing")
    _plain_chain(root, path)
    if _stamp(path.lstat()) != expected:
        raise ValueError("file changed after hashing")
    return digest.hexdigest(), total


def observe_pairs(pairs: tuple[RootPair, ...], *, hash_content: bool = True,
                  hash_all_tower: bool = False, progress=None) -> dict:
    """Inventory explicit roots and hash each cross-side size candidate once.

    Returns a deterministic JSON-compatible observation with a content digest.
    Operational failures are retained in ``problems`` and make ``complete``
    false. Invalid pair declarations raise ValueError. Consumers must require
    complete coverage before making negative content or absence assertions.

    ``hash_content=False`` returns a sizes-only estimate with null hashes.
    ``hash_all_tower=True`` includes every Tower file, even when its size is
    absent from the receiver. This provides full desired-byte evidence for a
    verifier baseline; estimate mode budgets the same selection without reads.
    ``hash_plan`` records candidate file counts and logical payload bytes before
    hashing. The optional progress consumer receives metadata stages and hash
    read events, including intermediate reads within large files. Its failures
    raise ProgressError. Progress is excluded from the observation digest.
    """
    if type(hash_content) is not bool:
        raise ValueError("hash_content must be a boolean")
    if type(hash_all_tower) is not bool:
        raise ValueError("hash_all_tower must be a boolean")
    if progress is not None and not callable(progress):
        raise ValueError("progress must be callable")
    if not pairs or any(not isinstance(pair, RootPair) or not isinstance(pair.folder_id, str)
                        or not pair.folder_id.strip() for pair in pairs):
        raise ValueError("pairs need non-empty string folder IDs")
    if len({pair.folder_id for pair in pairs}) != len(pairs):
        raise ValueError("at least one pair with unique folder IDs is required")
    roots, bindings, files, excluded, problems = [], [], [], [], []
    for pair in sorted(pairs, key=lambda pair: pair.folder_id):
        prefixes = _prefixes(pair.exclude_prefixes)
        for side, supplied in (("tower", pair.tower), ("target", pair.target)):
            label = dict(folder_id=pair.folder_id, side=side)
            try:
                path, identity, ancestors = _bind(Path(supplied))
                for other in roots:
                    if identity in other["ancestors"] or other["identity"] in ancestors:
                        raise ValueError("observed roots overlap or alias")
                roots.append(dict(**label, path=path, identity=identity, ancestors=ancestors, prefixes=prefixes))
                bindings.append(dict(**label, path=str(path), identity=list(identity), exclude_prefixes=list(prefixes)))
            except (OSError, ValueError) as error:
                problems.append(dict(**label, path=str(supplied), reason=str(error)))
    observations = []
    # Topology must be established for every root before any payload is read.
    if not problems:
        for root in roots:
            label = {key: root[key] for key in ("folder_id", "side")}
            _emit(progress, phase="inventory", **label)
            if progress is None:
                before = _walk(root["path"], root["prefixes"])
            else:
                before = _walk(root["path"], root["prefixes"],
                               on_progress=lambda event: _emit(progress, phase="inventory_read", **label, **event))
            observations.append((root, before))
            files.extend(dict(**label, **row, sha256=None, bytes_read=0) for row in before[0] if row["kind"] == "file")
            excluded.extend(dict(**label, **row) for row in before[1])
            problems.extend(dict(**label, **row) for row in before[2])
    sizes = {side: {f["size"] for f in files if f["side"] == side} for side in ("tower", "target")}
    common_sizes = sizes["tower"] & sizes["target"]
    candidates = [row for row in files if row["size"] in common_sizes
                  or (hash_all_tower and row["side"] == "tower")]
    hash_plan = dict(candidate_files=len(candidates), candidate_bytes=sum(row["size"] for row in candidates),
                     inventory_complete=not problems,
                     by_side={side: dict(files=sum(row["side"] == side for row in candidates),
                                         bytes=sum(row["size"] for row in candidates if row["side"] == side))
                              for side in ("tower", "target")})
    _emit(progress, phase="hash_plan", hash_content=hash_content, **hash_plan)
    by_root = {(root["folder_id"], root["side"]): root for root in roots}
    bytes_attempted = 0
    files_hashed = 0
    if hash_content and not problems:
        for number, row in enumerate(candidates, 1):
            root = by_root[row["folder_id"], row["side"]]
            label = dict(folder_id=row["folder_id"], side=row["side"], path=row["relative_path"])
            _emit(progress, phase="hash_start", files_processed=number - 1, **label, **hash_plan)

            def read_progress(count):
                nonlocal bytes_attempted
                bytes_attempted += count
                _emit(progress, phase="hash_read", bytes_read=bytes_attempted,
                      candidate_bytes=hash_plan["candidate_bytes"], files_hashed=files_hashed, **label)

            try:
                if progress is None:
                    row["sha256"], row["bytes_read"] = _hash(root["path"], row)
                else:
                    row["sha256"], row["bytes_read"] = _hash(root["path"], row, on_bytes=read_progress)
                files_hashed += 1
            except (OSError, ValueError) as error:
                row["bytes_read"] = None
                problems.append(dict(folder_id=row["folder_id"], side=row["side"], path=row["relative_path"], reason=str(error)))
            _emit(progress, phase="hash_result", files_processed=number, files_hashed=files_hashed,
                  successful=row["sha256"] is not None, **label)
    for root, before in observations:
        _emit(progress, phase="recheck", folder_id=root["folder_id"], side=root["side"])
        try:
            _, identity, _ = _bind(root["path"])
            if progress is None:
                after = _walk(root["path"], root["prefixes"])
            else:
                after = _walk(root["path"], root["prefixes"],
                              on_progress=lambda event: _emit(progress, phase="recheck_read", folder_id=root["folder_id"], side=root["side"], **event))
            if identity != root["identity"] or before != after:
                raise ValueError("tree changed across observation")
        except (OSError, ValueError) as error:
            problems.append(dict(folder_id=root["folder_id"], side=root["side"], path=".", reason=str(error)))
    _emit(progress, phase="complete", complete=not problems, files_hashed=files_hashed)
    order = lambda row: (row["folder_id"], row["side"], row.get("relative_path", row.get("path", "")), row.get("reason", ""))
    result = dict(contract=CONTRACT, scope="filesystem-only", complete=not problems,
                  hash_mode="content" if hash_content else "estimate", hash_all_tower=hash_all_tower,
                  hash_plan=hash_plan,
                  exclusion_policy=dict(component_names=sorted(_EXCLUDED), temporary_name=".syncthing.*.tmp"),
                  roots=sorted(bindings, key=order), files=sorted(files, key=order),
                  excluded=sorted(excluded, key=order), problems=sorted(problems, key=order),
                  file_counts=dict(sorted(Counter(row["side"] for row in files).items())))
    payload = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    result["observation_digest"] = hashlib.sha256(payload).hexdigest()
    return result
