from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path


DEFAULT_ARCHIVE_EXTENSIONS = frozenset({
    ".cbz",
    ".cbr",
    ".cb7",
})


@dataclass(frozen=True)
class DiscoveredArchive:
    path: Path
    extension: str
    file_size: int
    modified_time_ns: int


@dataclass(frozen=True)
class DiscoverySummary:
    batch_id: int
    scanned: int
    new: int
    changed: int
    unchanged: int
    missing: int
    jobs_queued: int


def normalize_library_path(path: str | Path) -> Path:
    """
    Return a stable absolute path without requiring the target to exist.

    resolve(strict=False) does not open files and is safe for discovery.
    """
    return Path(path).expanduser().resolve(strict=False)


def discover_archives(
    root: str | Path,
    *,
    extensions: Iterable[str] = DEFAULT_ARCHIVE_EXTENSIONS,
) -> Iterator[DiscoveredArchive]:
    """
    Recursively enumerate supported archives beneath root.

    Only directory entries and stat metadata are read. Archive contents
    are never opened.
    """
    library_root = normalize_library_path(root)

    if not library_root.exists():
        raise FileNotFoundError(
            f"Library root does not exist: {library_root}"
        )

    if not library_root.is_dir():
        raise NotADirectoryError(
            f"Library root is not a directory: {library_root}"
        )

    supported = {
        value.lower()
        if value.startswith(".")
        else f".{value.lower()}"
        for value in extensions
    }

    for directory, directory_names, filenames in os.walk(
        library_root,
        topdown=True,
        followlinks=False,
    ):
        # Deterministic traversal makes test results and audit logs stable.
        directory_names.sort(key=str.casefold)
        filenames.sort(key=str.casefold)

        directory_path = Path(directory)

        for filename in filenames:
            path = directory_path / filename
            extension = path.suffix.lower()

            if extension not in supported:
                continue

            try:
                stat_result = path.stat()
            except FileNotFoundError:
                # The file disappeared between enumeration and stat.
                continue
            except OSError:
                # Permission and transient filesystem errors are handled
                # by later logging/service integration. A single file must
                # not abort the complete read-only crawl.
                continue

            if not path.is_file():
                continue

            yield DiscoveredArchive(
                path=normalize_library_path(path),
                extension=extension,
                file_size=int(stat_result.st_size),
                modified_time_ns=int(stat_result.st_mtime_ns),
            )
