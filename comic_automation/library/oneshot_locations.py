"""Read durable oneshot identities without rebuilding or changing the library.

The version-1 locations file is the source of identity for books in the shared
container, not a disposable cache. Invalid data must stop a rebuild instead of
silently forgetting series. Writers must publish complete files by atomic replace
under the metadata lock specified in ``docs/comix_oneshots_contract.md``. This
reader opens one file generation and never checks book existence: missing books
remain represented so the resolver can report stale evidence without deleting it.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import stat
from pathlib import Path
import unicodedata


LOCATIONS_NAME = "_series_locations.json"
ONESHOTS_DIRECTORY = "_oneshots"
_RESERVED_DEVICES = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])$", re.I)


class LocationsError(ValueError):
    """Durable locations cannot be trusted; callers must stop rather than rebuild."""


@dataclass(frozen=True)
class OneshotLocation:
    """One series' durable book location, including its last relocation evidence."""

    canonical_title: str
    relative_path: str
    original_folder: str | None
    aliases: tuple[str, ...]
    size: int
    mtime_ns: int
    sha256: str
    operation_id: str


@dataclass(frozen=True)
class LocationsSnapshot:
    """One immutable generation; an absent locations file is generation zero."""

    generation: int
    oneshots: tuple[OneshotLocation, ...]


def _key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise LocationsError(f"{field} must be nonempty text without control characters")
    return value


def _component(value: object, field: str) -> str:
    name = _text(value, field)
    if (
        name in {".", ".."}
        or any(c in name for c in '<>:"/\\|?*')
        or name.endswith((" ", "."))
        or _RESERVED_DEVICES.fullmatch(name.split(".", 1)[0].rstrip(" ."))
    ):
        raise LocationsError(f"{field} must be one safe Windows path component")
    return name


def _natural(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise LocationsError(f"{field} must be a nonnegative integer")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise LocationsError(f"Duplicate JSON member: {name!r}")
        result[name] = value
    return result


def _fields(value: object, expected: set[str], field: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != expected:
        raise LocationsError(f"{field} must contain exactly: {', '.join(sorted(expected))}")
    return value


def parse_locations(document: str) -> LocationsSnapshot:
    """Validate version 1 without I/O, retaining stale records and ambiguous aliases.

    Canonical identities and physical targets must be unique under the contract's
    NFC/casefold comparison. Aliases may intentionally overlap across series: the
    resolver, rather than this reader, must retain and report that ambiguity.
    """
    try:
        value = json.loads(document, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise LocationsError(f"Invalid locations JSON: {exc}") from exc
    root = _fields(value, {"version", "generation", "oneshots"}, "locations")
    if type(root["version"]) is not int or root["version"] != 1:
        raise LocationsError("Unsupported locations version")
    generation = _natural(root["generation"], "generation")
    records = root["oneshots"]
    if not isinstance(records, dict):
        raise LocationsError("oneshots must be an object keyed by canonical title")
    identities: set[str] = set()
    targets: set[str] = set()
    result: list[OneshotLocation] = []
    for canonical, raw in records.items():
        canonical = _component(canonical, "canonical title")
        if _key(canonical) == ONESHOTS_DIRECTORY:
            raise LocationsError("The oneshots container cannot be a canonical series")
        if _key(canonical) in identities:
            raise LocationsError(f"Duplicate normalized canonical title: {canonical!r}")
        identities.add(_key(canonical))
        entry = _fields(raw, {
            "relative_path", "original_folder", "aliases", "size", "mtime_ns",
            "sha256", "operation_id",
        }, f"location for {canonical!r}")
        relative = _text(entry["relative_path"], "relative_path")
        parts = relative.split("/")
        if len(parts) != 2 or _key(parts[0]) != ONESHOTS_DIRECTORY:
            raise LocationsError("relative_path must be _oneshots/<one filename>")
        _component(parts[1], "book filename")
        if _key(relative) in targets:
            raise LocationsError(f"Duplicate normalized target: {relative!r}")
        targets.add(_key(relative))
        original = entry["original_folder"]
        if original is not None:
            original = _component(original, "original_folder")
            if _key(original) == ONESHOTS_DIRECTORY:
                raise LocationsError("original_folder cannot be the oneshots container")
        aliases = entry["aliases"]
        if not isinstance(aliases, list):
            raise LocationsError("aliases must be an array of strings")
        aliases = tuple(_text(alias, "alias") for alias in aliases)
        digest = entry["sha256"]
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
            raise LocationsError("sha256 must be a 64-character hexadecimal digest")
        result.append(OneshotLocation(
            canonical, relative, original, aliases,
            _natural(entry["size"], "size"),
            _natural(entry["mtime_ns"], "mtime_ns"),
            digest.lower(), _text(entry["operation_id"], "operation_id"),
        ))
    return LocationsSnapshot(generation, tuple(sorted(result, key=lambda row: _key(row.canonical_title))))


def load_locations(root: Path) -> LocationsSnapshot:
    """Read a trusted library root's locations, without creating or changing files.

    Only absence means an empty snapshot. Invalid or unreadable files, directories
    and reparse-point control files are errors, never permission to invent a new
    registry. The caller supplies its already-bound library root; book paths are
    validated as data here and must be re-bound before any later filesystem action.
    """
    path = Path(root) / LOCATIONS_NAME
    try:
        before = path.lstat()
    except FileNotFoundError:
        if not Path(root).is_dir():
            raise LocationsError(f"Library root is unavailable: {root}")
        return LocationsSnapshot(0, ())
    except OSError as exc:
        raise LocationsError(f"Cannot inspect locations: {path}: {exc}") from exc
    if not stat.S_ISREG(before.st_mode) or getattr(before, "st_file_attributes", 0) & 0x400:
        raise LocationsError("Locations must be a regular file, not a reparse point")
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise LocationsError("Locations changed before opening; retry the read")
            document = stream.read().decode("utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise LocationsError(f"Cannot read locations: {path}: {exc}") from exc
    return parse_locations(document)
