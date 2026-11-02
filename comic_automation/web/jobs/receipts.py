"""Durable exit evidence for a live claimed owner, without recovery authority.

The owned native process and the actual settled log pump are rechecked both
before publication and before ledger finalization. A JSON Boolean, an exit
code, or process absence alone cannot release admission. Slice 3 will supply
the separate dead-runner receipt import protocol; this module cannot do that.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat

from .. import state_dir
from .log_io import shared_read
from .logs import LogPump, MAX_INDEX_BYTES, validate_index
from .store import JobRefused, MAX_COMMAND_BYTES, _digest, _json
from .win32 import ContainedProcess

MAX_RECEIPT_BYTES = 64 * 1024


def _read(store, path: Path, limit: int) -> bytes:
    """Read one bounded private regular file, pinning its open identity."""
    state_dir.check_private(store.path, [path.parent, path])
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or not before.st_ino:
        raise JobRefused("RECEIPT_FILE_NOT_REGULAR")
    with shared_read(path) as stream:
        opened = os.fstat(stream.fileno())
        payload = stream.read(limit + 1)
        after = path.stat()
    state_dir.check_private(store.path, [path.parent, path])
    if (len(payload) > limit or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)):
        raise JobRefused("RECEIPT_FILE_CHANGED_OR_TOO_LARGE")
    return payload


def _identity(value) -> dict:
    value.validate()
    return dict(pid=value.pid, created_at=str(value.created_at), session_id=value.session_id)


def _proof(store, row, claim, process, pump) -> tuple[int, dict]:
    """Observe the retained native owner and its actual completed log threads.

    Only already-settled threads qualify. Calling LogPump.join(0) here would
    request cancellation of live work, so this is deliberately observational.
    """
    if row["stop_requested_ms"] is not None:
        raise JobRefused("RECEIPT_STOP_EVIDENCE_REQUIRED")
    if (not isinstance(process, ContainedProcess) or not isinstance(pump, LogPump)
            or pump.source is not process.stdout
            or process.identity.pid != row["child_pid"]
            or str(process.identity.created_at) != row["child_created_at"]
            or process.identity.session_id != claim.runner.session_id
            or process.name != row["job_object_name"]
            or process.name != f"Local\\cbz-console-{claim.job_id}"):
        raise JobRefused("RECEIPT_OWNER_MISMATCH")
    code = process.wait_root(0)
    if code is None or process.active_processes() != 0:
        raise JobRefused("RECEIPT_NATIVE_NOT_FINAL")
    if (pump.reader.ident is None or pump.writer.ident is None
            or pump.reader.is_alive() or pump.writer.is_alive()
            or pump.error is not None or not pump.queue.complete):
        raise JobRefused("RECEIPT_LOG_NOT_FINAL")
    directory = store.path / "jobs" / claim.job_id / "log"
    if pump.disk.directory != directory:
        raise JobRefused("RECEIPT_LOG_OWNER_MISMATCH")
    command = _read(store, store.path / "jobs" / claim.job_id / "command.json", MAX_COMMAND_BYTES)
    store._command_document(row, command)
    raw = _read(store, directory / "index.json", MAX_INDEX_BYTES)
    try:
        index = validate_index(json.loads(raw), claim.job_id, claim.nonce)
    except (ValueError, UnicodeError):
        raise JobRefused("RECEIPT_LOG_INDEX_INVALID") from None
    if (not index["complete"] or index != pump.disk.snapshot
            or pump.queue.offset != index["offset"]):
        raise JobRefused("RECEIPT_LOG_NOT_FINAL")
    return code, dict(reader_settled=True, writer_settled=True, complete=True,
        read_offset=pump.queue.offset, durable_offset=index["offset"], index_sha256=_digest(raw))


def _document(row, claim, process, code, logs, ended_ms, outcome) -> dict:
    if (type(ended_ms) is not int or not 0 <= ended_ms < 2**48
            or outcome not in ("succeeded", "failed")):
        raise JobRefused("INVALID_RECEIPT_RESULT")
    return dict(version=1, job_id=claim.job_id, nonce=claim.nonce,
        command_digest=row["command_digest"], seal_digest=row["seal_digest"],
        runner=_identity(claim.runner), child=_identity(process.identity),
        job_object_name=process.name, exit_code=code, state=outcome, ended_ms=ended_ms,
        descendants=dict(active_processes=0, session_id=claim.runner.session_id),
        logs=logs, stop=dict(requested=False, delivered=False))


def _outcome(mapper, code: int) -> str:
    if not callable(mapper):
        raise JobRefused("TRUSTED_OUTCOME_MAPPER_REQUIRED")
    value = mapper(code)
    if not isinstance(value, str) or value not in ("succeeded", "failed"):
        raise JobRefused("INVALID_RECEIPT_RESULT")
    return value


def publish(store, claim, version: int, process, pump, *, outcome_mapper=None) -> dict:
    """Fsync and atomically publish a complete receipt under a live running claim.

    The trusted outcome mapper supplies succeeded/failed and is required again
    at finalization. A receipt never selects its own outcome. Stopped results
    need policy/delivery evidence not implemented in this checkpoint and refuse.
    This synchronous persistence API does not supervise or bound stalled I/O;
    the forthcoming runner must keep native polling responsive independently.
    A failed publication leaves the running row holding admission.
    """
    from ...database import dal
    with store._home(job_id=claim.job_id):
        with dal.connection_scope(store.database, readonly=True) as db:
            row = dict(store._claimed_row(db, claim, version, "running"))
        code, logs = _proof(store, row, claim, process, pump)
        receipt = _document(row, claim, process, code, logs, store.clock_ms(), _outcome(outcome_mapper, code))
        payload = _json(receipt).encode()
        root = store.path / "jobs" / claim.job_id
        target, temporary = root / "exit.json", root / "exit.json.tmp"
        state_dir.check_private(store.path, [root, target, temporary])
        if os.path.lexists(target):
            raise JobRefused("EXISTING_EXIT_RECEIPT")
        with open(temporary, "xb") as stream:
            if stream.write(payload) != len(payload):
                raise OSError("Short exit receipt write")
            stream.flush()
            os.fsync(stream.fileno())
        state_dir.check_private(store.path, [root, target, temporary])
        if os.path.lexists(target):
            raise JobRefused("EXISTING_EXIT_RECEIPT")
        os.replace(temporary, target)
        if _read(store, target, MAX_RECEIPT_BYTES) != payload:
            raise JobRefused("EXIT_RECEIPT_READBACK_MISMATCH")
        return receipt


def read_for_finalization(store, row, claim, process, pump, outcome_mapper) -> tuple[dict, str]:
    """Validate published bytes against fresh live-owner proof, never import.

    Caller holds admission exclusion and authenticates the live running claim.
    Exact canonical bytes reject extra fields, duplicates and bool/int aliases.
    A valid late receipt cannot clear durable uncertain through this API.
    """
    raw = _read(store, store.path / "jobs" / claim.job_id / "exit.json", MAX_RECEIPT_BYTES)
    try:
        receipt = json.loads(raw)
        if not isinstance(receipt, dict):
            raise ValueError("Not an object")
        code, logs = _proof(store, row, claim, process, pump)
        expected = _document(row, claim, process, code, logs,
                             receipt.get("ended_ms"), _outcome(outcome_mapper, code))
        if raw != _json(expected).encode():
            raise JobRefused("EXIT_RECEIPT_MISMATCH")
    except (ValueError, UnicodeError):
        raise JobRefused("EXIT_RECEIPT_INVALID") from None
    return expected, _digest(raw)
