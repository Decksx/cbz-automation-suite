from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path


DEFAULT_ARCHIVE_EXTENSIONS = frozenset({
    ".cbz",
    ".cbr",
    ".cb7",
})

DiscoveryErrorHandler = Callable[[Path, OSError], None]


@dataclass(frozen=True)
class DiscoveredArchive:
    path: Path
    extension: str
    file_size: int
    modified_time_ns: int


@dataclass(frozen=True)
class DiscoverySummary:
    batch_id: int | None
    scanned: int
    new: int
    changed: int
    unchanged: int
    missing: int
    jobs_queued: int
    errors: int = 0
    resumed: bool = False
    limited: bool = False
    dry_run: bool = False
    elapsed_seconds: float = 0.0


def normalize_library_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def discover_archives(
    root: str | Path,
    *,
    extensions: Iterable[str] = DEFAULT_ARCHIVE_EXTENSIONS,
    on_error: DiscoveryErrorHandler | None = None,
) -> Iterator[DiscoveredArchive]:
    """
    Enumerate supported archives using directory and stat metadata only.

    Archive contents are never opened.
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

    def walk_error(error: OSError) -> None:
        if on_error is not None:
            filename = error.filename or str(library_root)
            on_error(Path(filename), error)

    for directory, directory_names, filenames in os.walk(
        library_root,
        topdown=True,
        followlinks=False,
        onerror=walk_error,
    ):
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

                if not path.is_file():
                    continue
            except FileNotFoundError:
                continue
            except OSError as exc:
                if on_error is not None:
                    on_error(path, exc)
                continue

            yield DiscoveredArchive(
                path=normalize_library_path(path),
                extension=extension,
                file_size=int(stat_result.st_size),
                modified_time_ns=int(stat_result.st_mtime_ns),
            )
