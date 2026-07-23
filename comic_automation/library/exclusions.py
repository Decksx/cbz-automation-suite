from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path


DEFAULT_EXCLUDED_DIRECTORIES = frozenset({
    ".stversions",
    ".stfolder",
    "@eadir",
    "$recycle.bin",
    "system volume information",
})


def normalize_excluded_directories(
    values: Iterable[str] | None = None,
) -> frozenset[str]:
    exclusions = set(DEFAULT_EXCLUDED_DIRECTORIES)

    for value in values or ():
        normalized = value.strip().replace("/", "\\").casefold()

        if normalized:
            exclusions.add(normalized)

    return frozenset(exclusions)


def is_excluded_directory(
    directory_name: str,
    *,
    excluded_directories: frozenset[str],
) -> bool:
    return directory_name.casefold() in excluded_directories


def path_contains_excluded_directory(
    path: str | Path,
    root: str | Path,
    *,
    excluded_directories: frozenset[str],
) -> bool:
    candidate = Path(path).resolve(strict=False)
    library_root = Path(root).resolve(strict=False)

    try:
        relative = candidate.relative_to(library_root)
    except ValueError:
        return False

    return any(
        part.casefold() in excluded_directories
        for part in relative.parts[:-1]
    )
