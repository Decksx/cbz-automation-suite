"""Verified, series-organized offload of Camelia's pre-processing backups.

Camelia first keeps a local rollback copy while the watcher processes/routes a
directory. Only after routing succeeds may the watcher call ``archive_backup``.
This module also migrates older UUID-named backup folders with an opt-in CLI.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree


REPO_ROOT = Path(__file__).resolve().parents[1]
LOCAL_BACKUP_ROOT = REPO_ROOT / "data" / "ai-decensor-originals"
ARCHIVE_ROOT = Path(r"F:\ai-decensor-originals")
COPY_CHUNK = 4 * 1024 * 1024
MAX_COMICINFO_BYTES = 8 * 1024 * 1024
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def _fs(path: Path) -> str:
    """Permit deep series paths on Windows without changing displayed paths."""
    value = str(path.absolute())
    if os.name != "nt" or value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def _exists(path: Path) -> bool:
    return os.path.exists(_fs(path))


def safe_series_name(value: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    name = re.sub(r"\s+", " ", name)
    if not name or name in {".", ".."}:
        raise ValueError("ComicInfo Series is empty or cannot name a directory")
    if name.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        name = "_" + name
    if len(name) > 200:
        name = name[:190].rstrip(" .") + "~" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return name


def comicinfo_series(source: Path) -> str:
    """Read only ComicInfo.xml; refuse to guess when a legacy backup lacks it."""
    with zipfile.ZipFile(_fs(source)) as archive:
        members = [
            member for member in archive.infolist()
            if not member.is_dir()
            and member.filename.replace("\\", "/").rsplit("/", 1)[-1].casefold() == "comicinfo.xml"
        ]
        if len(members) != 1:
            raise ValueError(f"Expected one ComicInfo.xml in {source}; found {len(members)}")
        member = members[0]
        if member.file_size > MAX_COMICINFO_BYTES:
            raise ValueError(f"ComicInfo.xml is too large in {source}")
        root = ElementTree.fromstring(archive.read(member))
        values = [
            (element.text or "").strip() for element in root.iter()
            if element.tag.rsplit("}", 1)[-1].casefold() == "series"
            and (element.text or "").strip()
        ]
        if len(values) != 1:
            raise ValueError(f"Expected one nonempty ComicInfo Series in {source}; found {len(values)}")
        return safe_series_name(values[0])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(_fs(path), "rb") as stream:
        for chunk in iter(lambda: stream.read(COPY_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _versioned_name(source: Path, original: Path, attempt: int) -> Path:
    stamp = datetime.fromtimestamp(os.stat(_fs(source)).st_mtime).strftime("%Y-%m-%d %H%M%S")
    suffix = f" (backup {stamp})" + (f" ({attempt})" if attempt > 1 else "")
    stem = original.stem[: max(1, 240 - len(suffix) - len(original.suffix))].rstrip(" .")
    return original.with_name(f"{stem}{suffix}{original.suffix}")


def archive_backup(source: Path, destination_root: Path, series: str) -> Path:
    """Copy, hash-verify, atomically install, then remove one C: rollback copy.

    A matching destination is deduplicated. Different books with the same name
    get a readable timestamp suffix. Any failure before verified installation
    leaves the source in place for recovery/retry.
    """
    if source.is_symlink():
        raise ValueError(f"Symbolic-link backup is not supported: {source}")
    source = source.resolve()
    destination_root = destination_root.resolve()
    if not source.is_file() or source.suffix.casefold() != ".cbz":
        raise ValueError(f"Expected a regular CBZ backup: {source}")
    try:
        common = os.path.commonpath((str(source), str(destination_root)))
    except ValueError:  # Different Windows drives.
        common = None
    if common is not None and os.path.normcase(common) == os.path.normcase(str(destination_root)):
        raise ValueError("Destination root cannot contain the source backup")
    series_dir = destination_root / safe_series_name(series)
    os.makedirs(_fs(series_dir), exist_ok=True)
    original = series_dir / source.name
    temporary = series_dir / f".{source.stem[:80]}.copy-{uuid.uuid4().hex}.partial"
    source_stat = os.stat(_fs(source))
    try:
        digest = hashlib.sha256()
        with open(_fs(source), "rb") as incoming, open(_fs(temporary), "xb") as staged:
            for chunk in iter(lambda: incoming.read(COPY_CHUNK), b""):
                staged.write(chunk)
                digest.update(chunk)
            staged.flush()
            os.fsync(staged.fileno())
        if _sha256(temporary) != digest.hexdigest():
            raise OSError(f"Copied backup failed SHA-256 verification: {temporary}")
        current_stat = os.stat(_fs(source))
        if (current_stat.st_size, current_stat.st_mtime_ns) != (source_stat.st_size, source_stat.st_mtime_ns):
            raise OSError(f"Source backup changed during copy: {source}")
        if _sha256(source) != digest.hexdigest():
            raise OSError(f"Source backup changed during verified copy: {source}")

        for attempt in range(0, 1000):
            candidate = original if attempt == 0 else _versioned_name(source, original, attempt)
            if _exists(candidate):
                if _sha256(candidate) == digest.hexdigest():
                    installed = candidate
                    break
                continue
            try:
                # Same-volume hard link is atomic and never overwrites a
                # destination that appears after the existence check.
                os.link(_fs(temporary), _fs(candidate))
                installed = candidate
                break
            except FileExistsError:
                if _sha256(candidate) == digest.hexdigest():
                    installed = candidate
                    break
        else:
            raise FileExistsError(f"Too many backup filename collisions for {source}")

        if _sha256(installed) != digest.hexdigest():
            raise OSError(f"Installed backup failed SHA-256 verification: {installed}")
        os.unlink(_fs(source))
        try:
            os.rmdir(_fs(source.parent))
        except OSError:
            pass  # Keep nonempty or non-job directories unchanged.
        return installed
    finally:
        if _exists(temporary):
            os.unlink(_fs(temporary))


def legacy_backups(source_root: Path) -> list[Path]:
    """Only UUID/job-id CBZs from Camelia's known legacy layout."""
    source_root = source_root.absolute()
    if source_root.is_symlink():
        raise ValueError(f"Source backup root is a symbolic link: {source_root}")
    if not source_root.is_dir():
        return []
    result = []
    for job_dir in source_root.iterdir():
        if not job_dir.is_dir() or job_dir.is_symlink():
            continue
        try:
            uuid.UUID(job_dir.name)
        except ValueError:
            continue
        result.extend(
            path for path in job_dir.iterdir()
            if path.is_file() and not path.is_symlink() and path.suffix.casefold() == ".cbz"
        )
    return sorted(result, key=lambda path: str(path).casefold())


def migrate_existing(
    source_root: Path, destination_root: Path, *, apply: bool = False, limit: int | None = None
) -> tuple[int, int]:
    files = legacy_backups(source_root)
    if limit is not None:
        if limit < 1:
            raise ValueError("--limit must be at least 1")
        files = files[:limit]
    if apply and files:
        drive = Path(destination_root).resolve().anchor
        if not drive or not Path(drive).exists():
            raise FileNotFoundError(f"Backup drive is unavailable: {drive or destination_root}")
        required = sum(os.stat(_fs(path)).st_size for path in files)
        if shutil.disk_usage(drive).free < required + 1024 * 1024 * 1024:
            raise OSError("Backup drive has insufficient free space for safe migration")
    moved = failed = 0
    for index, source in enumerate(files, start=1):
        try:
            series = comicinfo_series(source)
            destination = destination_root / series / source.name
            if apply:
                destination = archive_backup(source, destination_root, series)
                moved += 1
                print(f"MOVED {index}/{len(files)} {source} -> {destination}", flush=True)
            else:
                print(f"PLAN {index}/{len(files)} {source} -> {destination}", flush=True)
        except (OSError, ValueError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
            failed += 1
            print(f"FAILED {source}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    print(f"{'MIGRATED' if apply else 'PLANNED'} {moved if apply else len(files)-failed}; failed {failed}", flush=True)
    return (moved if apply else len(files) - failed), failed


def offload_one(source: Path, source_root: Path, destination_root: Path) -> Path:
    """Offload one known Camelia rollback copy; never accept an arbitrary file."""
    if source.is_symlink() or source.parent.is_symlink() or source_root.is_symlink():
        raise ValueError("Symbolic links are not valid Camelia backup paths")
    root = source_root.resolve(strict=True)
    candidate = source.resolve(strict=True)
    if candidate.parent.parent != root or candidate.suffix.casefold() != ".cbz":
        raise ValueError(f"Backup is not directly inside a Camelia job directory: {source}")
    try:
        uuid.UUID(candidate.parent.name)
    except ValueError as exc:
        raise ValueError(f"Backup directory is not a Camelia job UUID: {source}") from exc
    return archive_backup(candidate, destination_root, comicinfo_series(candidate))


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Move verified Camelia originals to series folders on F:")
    parser.add_argument("--source-root", type=Path, default=LOCAL_BACKUP_ROOT)
    parser.add_argument("--destination-root", type=Path, default=ARCHIVE_ROOT)
    parser.add_argument("--apply", action="store_true", help="Actually migrate; the default is preview only")
    parser.add_argument("--limit", type=int, help="Process only the first N backups (useful for a small trial)")
    parser.add_argument("--source-file", type=Path, help="Offload only this verified Camelia job backup")
    args = parser.parse_args(argv)
    if args.source_file is not None:
        if not args.apply:
            parser.error("--source-file requires --apply")
        try:
            destination = offload_one(args.source_file, args.source_root, args.destination_root)
        except (OSError, ValueError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
            parser.exit(1, f"Backup offload refused: {type(exc).__name__}: {exc}\n")
        print(f"MOVED {args.source_file} -> {destination}", flush=True)
        return 0
    _, failed = migrate_existing(args.source_root, args.destination_root, apply=args.apply, limit=args.limit)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
