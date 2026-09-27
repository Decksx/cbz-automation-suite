from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from pathlib import Path


DEFAULT_EXCLUDED_DIRECTORIES = frozenset({
    ".stversion",
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


def iter_paths_excluding_directories(
    root: str | Path,
    *,
    excluded_directories: Iterable[str] | None = None,
) -> Iterator[Path]:
    """Yield descendants without entering excluded directory names.

    Matching is case-insensitive and always includes the built-in Syncthing
    exclusions.  Directories are yielded before their files, matching the
    pre-order behavior expected by the merge and inventory tools.
    """
    root_path = Path(root)
    exclusions = normalize_excluded_directories(excluded_directories)

    if is_excluded_directory(
        root_path.name,
        excluded_directories=exclusions,
    ):
        return

    for directory, directory_names, filenames in os.walk(
        root_path,
        topdown=True,
        followlinks=False,
    ):
        directory_names.sort(key=str.casefold)
        filenames.sort(key=str.casefold)
        directory_names[:] = [
            name
            for name in directory_names
            if not is_excluded_directory(
                name,
                excluded_directories=exclusions,
            )
        ]

        directory_path = Path(directory)
        for name in directory_names:
            yield directory_path / name
        for name in filenames:
            yield directory_path / name


def iter_files_excluding_directories(
    root: str | Path,
    *,
    excluded_directories: Iterable[str] | None = None,
) -> Iterator[Path]:
    """Yield files below *root* without entering excluded directories."""
    for path in iter_paths_excluding_directories(
        root,
        excluded_directories=excluded_directories,
    ):
        if path.is_file():
            yield path
