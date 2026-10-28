"""Immutable archive batches; the store owns privacy, admission and ledger order.

This codec never deletes hot files or changes lifecycle state. Its caller
checks private paths under admission exclusion and commits archive pointers
before any cleanup. Bounded streaming verification uses the same opened batch
handle for hashing and member reads, including command reads after archiving.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
from typing import Mapping, Sequence
import zipfile

from .store import JobRefused, MAX_COMMAND_BYTES, validate_job_id

TERMINAL = frozenset({"succeeded", "failed", "stopped", "lost", "admission_expired", "refused"})
CHUNK_BYTES = 64 * 1024
MAX_JOBS = 200
MAX_MEMBERS = 4096
MAX_JOB_ENTRIES = 256
MAX_BATCH_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class ArchiveRefused(JobRefused):
    """A batch or its source failed validation; keep its evidence unchanged."""


@dataclass(frozen=True)
class ArchiveJob:
    """Terminal ledger evidence and the already-scoped hot source directory."""
    job_id: str
    state: str
    command_digest: str
    root: Path


@dataclass(frozen=True)
class BatchReceipt:
    """Verified publication, not permission to remove hot artifacts yet."""
    file_name: str
    sha256: str
    manifest: dict


def _plain(path: Path) -> os.stat_result:
    value = path.lstat()
    if stat.S_ISLNK(value.st_mode) or getattr(value, "st_file_attributes", 0) & 0x400:
        raise ArchiveRefused(f"Reparse or symlink archive source: {path}")
    return value


def _hash(stream, *, limit: int = MAX_BATCH_BYTES) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    while True:
        block = stream.read(CHUNK_BYTES)
        if not block:
            break
        size += len(block)
        if size > limit:
            raise ArchiveRefused("Archive byte limit exceeded")
        digest.update(block)
    return digest.hexdigest(), size


def _files(root: Path, directories: list[Path] | None = None) -> list[Path]:
    if not stat.S_ISDIR(_plain(root).st_mode):
        raise ArchiveRefused("Job archive source is not a directory")
    pending, files, count = [root], [], 0
    if directories is not None:
        directories.append(root)
    while pending:
        for entry in pending.pop().iterdir():
            count += 1
            if count > MAX_JOB_ENTRIES:
                raise ArchiveRefused("Job archive entry limit exceeded")
            mode = _plain(entry).st_mode
            if stat.S_ISDIR(mode):
                pending.append(entry)
                if directories is not None:
                    directories.append(entry)
            elif stat.S_ISREG(mode):
                files.append(entry)
            else:
                raise ArchiveRefused("Unsupported job archive source")
    return sorted(files)


def _member_name(name: str, job_id: str) -> None:
    parts = PurePosixPath(name).parts
    if (not name.startswith(job_id + "/") or "\\" in name or "\x00" in name
            or any(p in ("", ".", "..") for p in name.split("/"))
            or len(parts) < 2 or PurePosixPath(name).is_absolute()):
        raise ArchiveRefused("Unsafe archive member name")


def _directory_sync(path: Path) -> None:
    # The Windows CRT cannot open a directory for fsync. File data is fsynced;
    # the verified archive pointer still gates cleanup after a restart.
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def write_batch(directory: Path, batch_id: str, jobs: Sequence[ArchiveJob], archived_ms: int) -> BatchReceipt:
    """Publish, reopen and verify one new batch; leave hot sources untouched.

    The archive directory must already exist. Names are never reused, including
    orphan temporary/final names. Failures leave evidence for reconciliation.
    """
    validate_job_id(batch_id)
    if (not 1 <= len(jobs) <= MAX_JOBS or len({job.job_id for job in jobs}) != len(jobs)
            or type(archived_ms) is not int or not 0 <= archived_ms < 2**63):
        raise ArchiveRefused("Invalid archive batch request")
    if not stat.S_ISDIR(_plain(directory).st_mode):
        raise ArchiveRefused("Archive directory is unavailable")
    target = directory / f"jobs-{batch_id}.zip"
    temporary = directory / f"jobs-{batch_id}.zip.tmp"
    if os.path.lexists(target):
        raise ArchiveRefused("Archive batch name already exists")
    manifest = {"schema_version": 1, "batch_id": batch_id, "jobs": []}
    total_bytes = total_members = 0
    with open(temporary, "xb") as raw:
        with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_DEFLATED) as batch:
            for job in jobs:
                validate_job_id(job.job_id)
                if job.state not in TERMINAL or not _SHA.fullmatch(job.command_digest):
                    raise ArchiveRefused("Invalid terminal ledger evidence")
                record = {"job_id": job.job_id, "state": job.state, "command_digest": job.command_digest,
                          "archived_ms": archived_ms, "members": []}
                for source in _files(job.root):
                    name = job.job_id + "/" + source.relative_to(job.root).as_posix()
                    _member_name(name, job.job_id)
                    total_members += 1
                    if total_members > MAX_MEMBERS:
                        raise ArchiveRefused("Archive member limit exceeded")
                    digest, size = hashlib.sha256(), 0
                    with open(source, "rb") as inp, batch.open(name, "w") as out:
                        while True:
                            block = inp.read(CHUNK_BYTES)
                            if not block:
                                break
                            size += len(block)
                            total_bytes += len(block)
                            if total_bytes > MAX_BATCH_BYTES:
                                raise ArchiveRefused("Archive byte limit exceeded")
                            digest.update(block)
                            out.write(block)
                    if name == job.job_id + "/command.json" and digest.hexdigest() != job.command_digest:
                        raise ArchiveRefused("Command digest differs from the ledger")
                    record["members"].append({"path": name, "size": size, "sha256": digest.hexdigest()})
                if not any(m["path"] == job.job_id + "/command.json" for m in record["members"]):
                    raise ArchiveRefused("Archive job has no command")
                manifest["jobs"].append(record)
            data = json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            if len(data) > MAX_MANIFEST_BYTES:
                raise ArchiveRefused("Archive manifest limit exceeded")
            batch.writestr("manifest.json", data)
        raw.flush()
        os.fsync(raw.fileno())
    os.replace(temporary, target)
    _directory_sync(directory)
    with open(target, "rb") as raw:
        digest, _ = _hash(raw, limit=MAX_BATCH_BYTES + MAX_MANIFEST_BYTES + 4 * 1024 * 1024)
    verify_batch(target, digest, jobs={job.job_id: job for job in jobs}, check_sources=True)
    return BatchReceipt(target.name, digest, manifest)


def _verify(path: Path, expected_digest: str, jobs: Mapping[str, ArchiveJob] | None,
            check_sources: bool, requested: str | None,
            allow_missing_sources: bool = False) -> tuple[dict, bytes | None]:
    if allow_missing_sources and (not check_sources or jobs is None):
        raise ArchiveRefused("Cleanup verification needs source and ledger evidence")
    if not _SHA.fullmatch(expected_digest):
        raise ArchiveRefused("Invalid archive digest")
    if not stat.S_ISREG(_plain(path).st_mode):
        raise ArchiveRefused("Archive is not a regular file")
    with open(path, "rb") as raw:
        actual, _ = _hash(raw, limit=MAX_BATCH_BYTES + MAX_MANIFEST_BYTES + 4 * 1024 * 1024)
        if actual != expected_digest:
            raise ArchiveRefused("Archive batch digest mismatch")
        raw.seek(0)
        with zipfile.ZipFile(raw) as batch:
            infos = batch.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)) or len(names) > MAX_MEMBERS + 1:
                raise ArchiveRefused("Duplicate or excessive archive members")
            info = batch.getinfo("manifest.json")
            if info.file_size > MAX_MANIFEST_BYTES:
                raise ArchiveRefused("Archive manifest limit exceeded")
            manifest = json.loads(batch.read(info))
            if manifest.get("schema_version") != 1:
                raise ArchiveRefused("Unknown archive schema")
            batch_id = validate_job_id(manifest["batch_id"])
            if path.name != f"jobs-{batch_id}.zip":
                raise ArchiveRefused("Archive batch name mismatch")
            records = manifest["jobs"]
            if not isinstance(records, list) or not 1 <= len(records) <= MAX_JOBS:
                raise ArchiveRefused("Invalid archive job list")
            ids = [validate_job_id(record["job_id"]) for record in records]
            if len(ids) != len(set(ids)) or jobs is not None and set(ids) != set(jobs):
                raise ArchiveRefused("Archive job set mismatch")
            described, total, selected = {"manifest.json"}, 0, None
            for record in records:
                job_id = record["job_id"]
                if record["state"] not in TERMINAL or not _SHA.fullmatch(record["command_digest"]):
                    raise ArchiveRefused("Invalid archive job evidence")
                if jobs is not None and (record["state"] != jobs[job_id].state
                        or record["command_digest"] != jobs[job_id].command_digest):
                    raise ArchiveRefused("Archive ledger evidence mismatch")
                members = record["members"]
                if not isinstance(members, list) or not members:
                    raise ArchiveRefused("Empty archive job")
                if check_sources:
                    if jobs is None:
                        raise ArchiveRefused("Source evidence required")
                    root = jobs[job_id].root
                    current = (set() if allow_missing_sources and not os.path.lexists(root) else
                        {job_id + "/" + source.relative_to(root).as_posix() for source in _files(root)})
                    expected = {member["path"] for member in members}
                    if (not allow_missing_sources and current != expected) or not current.issubset(expected):
                        raise ArchiveRefused("Hot source member set changed during archiving")
                command_seen = False
                for member in members:
                    name = member["path"]
                    _member_name(name, job_id)
                    if name in described or type(member["size"]) is not int or member["size"] < 0:
                        raise ArchiveRefused("Invalid archive member record")
                    described.add(name)
                    size = member["size"]
                    total += size
                    if total > MAX_BATCH_BYTES or batch.getinfo(name).file_size != size:
                        raise ArchiveRefused("Archive byte limit or size mismatch")
                    if name == job_id + "/command.json":
                        command_seen = True
                        if size > MAX_COMMAND_BYTES or member["sha256"] != record["command_digest"]:
                            raise ArchiveRefused("Archived command differs from the ledger")
                    with batch.open(name) as stream:
                        if name == requested:
                            if size > MAX_COMMAND_BYTES:
                                raise ArchiveRefused("Requested command too large")
                            selected = stream.read(MAX_COMMAND_BYTES + 1)
                            digest, actual_size = hashlib.sha256(selected).hexdigest(), len(selected)
                        else:
                            digest, actual_size = _hash(stream, limit=size)
                    if actual_size != size or digest != member["sha256"]:
                        raise ArchiveRefused("Archive member digest mismatch")
                    if check_sources:
                        if jobs is None:
                            raise ArchiveRefused("Source evidence required")
                        source = jobs[job_id].root.joinpath(*PurePosixPath(name).parts[1:])
                        if allow_missing_sources and not os.path.lexists(source):
                            continue
                        if not stat.S_ISREG(_plain(source).st_mode):
                            raise ArchiveRefused("Source changed during archiving")
                        with open(source, "rb") as hot:
                            if _hash(hot, limit=size) != (digest, size):
                                raise ArchiveRefused("Source changed during archiving")
                if not command_seen:
                    raise ArchiveRefused("Archive job has no command")
            if described != set(names):
                raise ArchiveRefused("Undescribed archive members")
            return manifest, selected


def verify_batch(path: Path, expected_digest: str, *, jobs: Mapping[str, ArchiveJob] | None = None,
                 check_sources: bool = False, allow_missing_sources: bool = False) -> dict:
    """Verify a sealed batch and optional current hot/ledger evidence.

    Missing-source mode is only for cleanup after a committed archive pointer:
    it allows an interrupted deletion to resume, while every remaining hot file
    must still be described and match. This function never authorizes deletion
    or supplies the caller's durable-pointer proof.
    """
    try:
        return _verify(path, expected_digest, jobs, check_sources, None, allow_missing_sources)[0]
    except (KeyError, TypeError, ValueError, zipfile.BadZipFile) as exc:
        raise ArchiveRefused(f"Invalid archive: {exc}") from None


def read_command(path: Path, expected_digest: str, job_id: str, command_digest: str) -> bytes:
    """Return one bounded command only after full same-handle batch verification."""
    validate_job_id(job_id)
    try:
        manifest, data = _verify(path, expected_digest, None, False, job_id + "/command.json")
        record = next((r for r in manifest["jobs"] if r["job_id"] == job_id), None)
        if record is None or data is None or record["command_digest"] != command_digest:
            raise ArchiveRefused("Archived command does not match the requested ledger row")
        return data
    except (KeyError, TypeError, ValueError, zipfile.BadZipFile) as exc:
        raise ArchiveRefused(f"Invalid archive: {exc}") from None


def read_pinned_member(path: Path, job_id: str, member: str, size: int, digest: str,
                       offset: int, count: int) -> bytes:
    """Verify one member against immutable ledger evidence, return a bounded slice.

    Unlike read_command's original whole-seal contract this explicitly requires
    a durable per-member digest/size recorded during verified publication. It
    never trusts a manifest as its own authority. Hashing one <=8MiB segment
    avoids hashing unrelated jobs/logs in a <=512MiB batch for each page.
    """
    validate_job_id(job_id)
    name = job_id + "/" + member
    _member_name(name, job_id)
    if (type(size) is not int or not 0 <= size <= 8 * 1024 * 1024
            or not _SHA.fullmatch(digest) or type(offset) is not int or not 0 <= offset <= size
            or type(count) is not int or not 0 <= count <= 8 * 1024 * 1024):
        raise ArchiveRefused("Invalid pinned member request")
    value = _plain(path)
    if not stat.S_ISREG(value.st_mode) or value.st_size > MAX_BATCH_BYTES + MAX_MANIFEST_BYTES + 4 * 1024 * 1024:
        raise ArchiveRefused("Invalid pinned batch file")
    try:
        with open(path, "rb") as raw:
            # Bound central-directory allocation before ZipFile parses it. Our
            # format never needs ZIP64: <=4097 members and <=512MiB payload.
            raw.seek(max(0, value.st_size - 65557))
            tail = raw.read(65557)
            position = tail.rfind(b"PK\x05\x06")
            if position < 0 or len(tail) - position < 22:
                raise ArchiveRefused("Missing bounded ZIP directory")
            end = struct.unpack_from("<4s4H2LH", tail, position)
            if (end[1] or end[2] or end[3] != end[4] or end[4] > MAX_MEMBERS + 1
                    or end[5] > 2 * 1024 * 1024 or end[6] + end[5] > value.st_size
                    or position + 22 + end[7] != len(tail)):
                raise ArchiveRefused("Excessive ZIP directory")
            raw.seek(0)
            with zipfile.ZipFile(raw) as batch:
                infos = batch.infolist()
                if len(infos) > MAX_MEMBERS + 1 or sum(info.filename == name for info in infos) != 1:
                    raise ArchiveRefused("Missing or duplicate pinned member")
                info = batch.getinfo(name)
                if (info.file_size != size or info.compress_size > size + 128 * 1024
                        or info.compress_type != zipfile.ZIP_DEFLATED):
                    raise ArchiveRefused("Pinned member size mismatch")
                hashed, total, selected = hashlib.sha256(), 0, bytearray()
                with batch.open(info) as stream:
                    while True:
                        block = stream.read(CHUNK_BYTES)
                        if not block:
                            break
                        if total + len(block) > size:
                            raise ArchiveRefused("Pinned member exceeds its bound")
                        hashed.update(block)
                        lower, upper = max(offset, total), min(offset + count, total + len(block))
                        if lower < upper:
                            selected.extend(block[lower - total:upper - total])
                        total += len(block)
                if total != size or hashed.hexdigest() != digest:
                    raise ArchiveRefused("Pinned member digest mismatch")
                return bytes(selected)
    except (KeyError, TypeError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, ArchiveRefused):
            raise
        raise ArchiveRefused(f"Invalid pinned member: {exc}") from None
