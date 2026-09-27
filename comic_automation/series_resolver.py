"""Canonical comic-series lookup and conflict-safe import primitives.

The resolver indexes the established top-level directories in one library,
learns alternate titles from lightweight metadata and persistent alias seeds,
and resolves incoming titles without making a routing decision on ambiguous
matches.  It is deliberately independent of the watcher so the same registry
format remains usable by one-shot import and maintenance tools.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from comic_automation.library.exclusions import (
    iter_files_excluding_directories,
)


REGISTRY_NAME = "_series_registry.json"
ALIAS_SEED_NAME = "_series_aliases.json"


def normalized(value: str) -> str:
    """Return a comparison form insensitive to punctuation and accents."""
    value = unicodedata.normalize("NFKD", value).casefold()
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.replace("&", " and ").replace("×", "x")
    value = re.sub(r"['’]s\b", "s", value)
    # ``\w`` is Unicode-aware in Python. Keeping non-Latin letters lets an
    # original Japanese/Chinese/Korean alias resolve to its English canonical
    # folder instead of normalizing to an empty key.
    value = re.sub(r"[\W_]+", " ", value)
    return " ".join(value.split())


def alias_key(value: str) -> str:
    """Return the compact key stored in the alias registry."""
    return normalized(value).replace(" ", "")


def safe_name(value: str) -> str:
    """Make an incoming title safe for use as one Windows directory name."""
    value = re.sub(r'[<>:"/\\|?*]', " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value or "Untitled Series"


def unique_path(path: Path) -> Path:
    """Return *path* or a numbered sibling that does not already exist."""
    if not path.exists():
        return path
    index = 2
    while True:
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def _read_series_json(folder: Path) -> set[str]:
    path = folder / "series.json"
    if not path.is_file():
        return set()
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
        metadata = obj.get("metadata", obj)
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        return set()
    if not isinstance(metadata, dict):
        return set()
    result: set[str] = set()
    for key in ("name", "title", "series"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            result.add(value.strip())
    alternate_titles = metadata.get("alternate_titles", [])
    if isinstance(alternate_titles, list):
        for item in alternate_titles:
            value = item.get("title") if isinstance(item, dict) else item
            if (
                isinstance(value, str)
                and value.strip()
                and value.strip().casefold() not in {"null", "unknown"}
            ):
                result.add(value.strip())
    return result


def _comicinfo_aliases(folder: Path) -> set[str]:
    result: set[str] = set()
    archives = sorted(
        (
            path
            for path in iter_files_excluding_directories(folder)
            if path.suffix.casefold() == ".cbz"
        ),
        key=lambda path: path.name.casefold(),
    )[:1]
    for archive_path in archives:
        try:
            with zipfile.ZipFile(archive_path) as archive:
                member = next(
                    (
                        name
                        for name in archive.namelist()
                        if Path(name).name.casefold() == "comicinfo.xml"
                    ),
                    None,
                )
                if not member:
                    continue
                root = ET.fromstring(archive.read(member))
                for tag in ("Series", "AlternateSeries", "AltSeries"):
                    value = (root.findtext(tag) or "").strip()
                    if value:
                        result.add(value)
                summary = (root.findtext("Summary") or "").strip()
                patterns = (
                    r"Alternative (?:Title\(s\)|Names?):\s*(.*?)(?:\n\s*\n|\Z)",
                    r"also known as \(AKA\)\s+(.*?)\s+\.\s+This\b",
                )
                for pattern in patterns:
                    match = re.search(pattern, summary, re.IGNORECASE | re.DOTALL)
                    if not match:
                        continue
                    for line in match.group(1).splitlines():
                        for value in line.split("|"):
                            value = value.strip(" ,-\t")
                            if value:
                                result.add(value)
        except (OSError, zipfile.BadZipFile, ET.ParseError, KeyError):
            continue
    return result


def _logged_aliases(root: Path) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for path in root.glob("_quarantine_*/*.json"):
        try:
            obj = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        aliases = obj.get("aliases")
        if isinstance(aliases, dict):
            for canonical, values in aliases.items():
                if isinstance(canonical, str) and isinstance(values, list):
                    pairs.extend(
                        (alias, canonical) for alias in values if isinstance(alias, str)
                    )
        events = obj.get("events", obj.get("actions", []))
        if not isinstance(events, list):
            continue
        for event in events:
            if not isinstance(event, dict):
                continue
            action = event.get("action")
            if action in {
                "renamed_directory",
                "merged_directory",
                "merged_recreated_directory",
                "case_fixed_directory",
                "rename_directory",
            }:
                source = event.get("source")
                target = event.get("target")
            elif action == "remove_combined_directory":
                source = event.get("directory")
                target = event.get("english_directory")
            else:
                continue
            if isinstance(source, str) and isinstance(target, str):
                pairs.append((Path(source).name, Path(target).name))
    return pairs


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # TOWER's Samba configuration vetoes AppleDouble-style ``._*`` names:
    # mkstemp reports FileNotFoundError even though ordinary files in the same
    # directory are writable. Keep the temporary adjacent for atomic replace,
    # but do not prepend another dot to registry names that already start with
    # an underscore.
    handle, temporary_name = tempfile.mkstemp(
        prefix=f"{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def build_registry(root: Path) -> dict[str, object]:
    """Scan one library root and return its canonical series registry."""
    root = root.resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Library root not found: {root}")
    folders = sorted(
        (
            path
            for path in root.iterdir()
            if path.is_dir()
            and not path.name.startswith("_quarantine_")
            and path.name not in {".stfolder", ".stversions", "_automation"}
        ),
        key=lambda path: path.name.casefold(),
    )
    existing = {folder.name.casefold(): folder.name for folder in folders}
    history = _logged_aliases(root)
    redirects = {alias.casefold(): target for alias, target in history}

    def resolve_chain(target: str) -> str | None:
        seen: set[str] = set()
        current = target
        while current.casefold() not in seen:
            seen.add(current.casefold())
            if current.casefold() in existing:
                return existing[current.casefold()]
            next_value = redirects.get(current.casefold())
            if not next_value:
                return None
            current = next_value
        return None

    aliases_by_canonical = {folder.name: {folder.name} for folder in folders}
    for alias, target in history:
        canonical = resolve_chain(target)
        if canonical:
            aliases_by_canonical[canonical].update({alias, target})

    seed_path = root / ALIAS_SEED_NAME
    if seed_path.is_file():
        try:
            seed = json.loads(seed_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            seed = {}
        if isinstance(seed, dict):
            for target, values in seed.items():
                canonical = resolve_chain(str(target))
                if canonical and isinstance(values, list):
                    aliases_by_canonical[canonical].update(
                        alias.strip()
                        for alias in values
                        if isinstance(alias, str) and alias.strip()
                    )

    def metadata_for(folder: Path) -> tuple[str, set[str]]:
        # Opening one archive per directory is useful on small libraries but
        # unreasonable for the large Comix share. Persistent aliases still
        # provide translated and romanized-title mappings there.
        if len(folders) > 5000:
            return folder.name, set()
        values = _read_series_json(folder)
        if not values:
            values = _comicinfo_aliases(folder)
        return folder.name, values

    metadata_by_folder: dict[str, set[str]] = {}
    workers = min(16, max(1, len(folders)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for folder_name, values in executor.map(metadata_for, folders):
            metadata_by_folder[folder_name] = values

    series: list[dict[str, object]] = []
    key_targets: dict[str, set[str]] = {}
    for folder in folders:
        aliases = aliases_by_canonical[folder.name]
        aliases.update(metadata_by_folder.get(folder.name, set()))
        ordered = sorted(
            aliases,
            key=lambda value: (
                value.casefold() != folder.name.casefold(),
                value.casefold(),
            ),
        )
        series.append(
            {
                "canonical_title": folder.name,
                "relative_path": folder.name,
                "aliases": ordered,
            }
        )
        for alias in ordered:
            key = alias_key(alias)
            if key:
                key_targets.setdefault(key, set()).add(folder.name)

    aliases = {
        key: next(iter(paths)) for key, paths in key_targets.items() if len(paths) == 1
    }
    conflicts = {
        key: sorted(paths, key=str.casefold)
        for key, paths in key_targets.items()
        if len(paths) > 1
    }
    return {
        "version": 1,
        "generated_at": datetime.now().astimezone().isoformat(),
        "library_root": str(root),
        "metadata_mode": (
            "series_json_and_representative_comicinfo"
            if len(folders) <= 5000
            else "alias_seed_and_cleanup_history"
        ),
        "series_count": len(series),
        "alias_count": len(aliases),
        "series": series,
        "aliases_by_normalized_key": dict(sorted(aliases.items())),
        "ambiguous_aliases": dict(sorted(conflicts.items())),
    }


def write_registry(root: Path, registry: dict[str, object]) -> Path:
    """Atomically write *registry* below *root* and return its path."""
    path = root / REGISTRY_NAME
    _atomic_json(path, registry)
    return path


def load_registry(root: Path) -> dict[str, object]:
    """Load an existing registry for *root*."""
    path = root / REGISTRY_NAME
    if not path.is_file():
        raise FileNotFoundError(f"Registry not found: {path}")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def resolve_from_registry(
    registry: dict[str, object], title: str, threshold: float = 0.95
) -> dict[str, object]:
    """Resolve *title* against an already-loaded registry."""
    key = alias_key(title)
    aliases = registry.get("aliases_by_normalized_key", {})
    ambiguous = registry.get("ambiguous_aliases", {})
    if not isinstance(aliases, dict) or not isinstance(ambiguous, dict):
        raise ValueError("Invalid series registry: alias maps are missing")
    if key in ambiguous:
        return {
            "matched": False,
            "match_type": "ambiguous_exact",
            "title": title,
            "candidates": ambiguous[key],
        }
    if key in aliases:
        return {
            "matched": True,
            "match_type": "exact_alias",
            "confidence": 1.0,
            "title": title,
            "relative_path": aliases[key],
        }
    if len(key) < 10:
        return {
            "matched": False,
            "match_type": "none",
            "title": title,
            "suggested_name": safe_name(title),
        }
    scored: list[tuple[float, str, str]] = []
    for candidate_key, relative in aliases.items():
        if not isinstance(candidate_key, str) or not isinstance(relative, str):
            continue
        if len(candidate_key) < 10:
            continue
        score = SequenceMatcher(None, key, candidate_key, autojunk=False).ratio()
        if score >= threshold:
            scored.append((score, relative, candidate_key))
    scored.sort(reverse=True)
    if not scored:
        return {
            "matched": False,
            "match_type": "none",
            "title": title,
            "suggested_name": safe_name(title),
        }
    best_score, best_relative, best_key = scored[0]
    competing = [
        item
        for item in scored[1:]
        if item[1] != best_relative and best_score - item[0] < 0.02
    ]
    if competing:
        return {
            "matched": False,
            "match_type": "ambiguous_fuzzy",
            "title": title,
            "candidates": [
                {"relative_path": relative, "confidence": round(score, 4)}
                for score, relative, _ in scored[:5]
            ],
        }
    return {
        "matched": True,
        "match_type": "fuzzy_alias",
        "confidence": round(best_score, 4),
        "matched_alias_key": best_key,
        "title": title,
        "relative_path": best_relative,
    }


class SeriesResolver:
    """Thread-safe live resolver for one comic library root."""

    def __init__(
        self,
        root: Path,
        *,
        threshold: float = 0.95,
        quarantine_root: Path | None = None,
        refresh: bool = False,
    ) -> None:
        if not 0.0 < threshold <= 1.0:
            raise ValueError("resolver threshold must be greater than 0 and at most 1")
        self.root = root.resolve()
        if not self.root.is_dir():
            raise NotADirectoryError(f"Library root not found: {self.root}")
        self.threshold = threshold
        self.quarantine_root = (
            quarantine_root.resolve()
            if quarantine_root is not None
            else self.root.parent / "_resolver_quarantine" / self.root.name
        )
        self._guard = threading.RLock()
        registry_path = self.root / REGISTRY_NAME
        if refresh or not registry_path.is_file():
            self.registry = build_registry(self.root)
            write_registry(self.root, self.registry)
        else:
            self.registry = load_registry(self.root)

    def resolve(self, title: str) -> dict[str, object]:
        """Resolve an incoming title and add absolute destination hints."""
        with self._guard:
            result = resolve_from_registry(self.registry, title, self.threshold)
        if result.get("matched"):
            result["path"] = str(self.root / str(result["relative_path"]))
        elif result.get("match_type") == "none":
            result["suggested_path"] = str(self.root / str(result["suggested_name"]))
        return result

    def register(self, canonical: str, aliases: set[str] | None = None) -> None:
        """Persist a newly landed canonical folder and its observed aliases."""
        canonical = safe_name(canonical)
        values = {canonical}
        values.update(alias.strip() for alias in aliases or set() if alias.strip())
        with self._guard:
            series = self.registry.setdefault("series", [])
            if not isinstance(series, list):
                raise ValueError("Invalid series registry: series list is missing")
            entry = next(
                (
                    item
                    for item in series
                    if isinstance(item, dict)
                    and item.get("relative_path") == canonical
                ),
                None,
            )
            if entry is None:
                entry = {
                    "canonical_title": canonical,
                    "relative_path": canonical,
                    "aliases": [],
                }
                series.append(entry)
            known = {
                value
                for value in entry.get("aliases", [])
                if isinstance(value, str)
            }
            known.update(values)
            entry["aliases"] = sorted(
                known,
                key=lambda value: (
                    value.casefold() != canonical.casefold(),
                    value.casefold(),
                ),
            )

            alias_map = self.registry.setdefault("aliases_by_normalized_key", {})
            conflicts = self.registry.setdefault("ambiguous_aliases", {})
            if not isinstance(alias_map, dict) or not isinstance(conflicts, dict):
                raise ValueError("Invalid series registry: alias maps are missing")
            for value in known:
                key = alias_key(value)
                if not key:
                    continue
                candidates = set(conflicts.get(key, []))
                current = alias_map.get(key)
                if isinstance(current, str) and current != canonical:
                    candidates.update({current, canonical})
                    alias_map.pop(key, None)
                elif candidates:
                    candidates.add(canonical)
                else:
                    alias_map[key] = canonical
                if candidates:
                    conflicts[key] = sorted(candidates, key=str.casefold)

            self.registry["series_count"] = len(series)
            self.registry["alias_count"] = len(alias_map)
            self.registry["generated_at"] = datetime.now().astimezone().isoformat()
            write_registry(self.root, self.registry)
            self._write_alias_seed(canonical, known)

    def _write_alias_seed(self, canonical: str, aliases: set[str]) -> None:
        seed_path = self.root / ALIAS_SEED_NAME
        try:
            seed = json.loads(seed_path.read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            seed = {}
        if not isinstance(seed, dict):
            seed = {}
        current = seed.get(canonical, [])
        values = {value for value in current if isinstance(value, str)}
        values.update(aliases)
        seed[canonical] = sorted(values, key=str.casefold)
        _atomic_json(seed_path, seed)


def digest(path: Path) -> str:
    """Return the SHA-256 digest of one file without loading it into memory."""
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def settle_file_conflict(
    incoming: Path, existing: Path, quarantine: Path
) -> dict[str, object]:
    """Resolve one filename conflict, keeping the larger non-identical file.

    Exact duplicates are removed from the incoming side. Every non-identical
    loser is moved to a unique path below the caller-provided quarantine.
    """
    incoming_size = incoming.stat().st_size
    existing_size = existing.stat().st_size
    if incoming_size == existing_size and digest(incoming) == digest(existing):
        incoming.unlink()
        return {
            "action": "deleted_exact_duplicate",
            "kept": existing,
            "removed": incoming,
        }

    quarantine.parent.mkdir(parents=True, exist_ok=True)
    quarantine = unique_path(quarantine)
    if incoming_size > existing_size:
        shutil.move(str(existing), str(quarantine))
        shutil.move(str(incoming), str(existing))
        return {
            "action": "kept_larger_incoming",
            "kept": existing,
            "quarantined": quarantine,
        }

    shutil.move(str(incoming), str(quarantine))
    return {
        "action": "kept_larger_existing",
        "kept": existing,
        "quarantined": quarantine,
    }
