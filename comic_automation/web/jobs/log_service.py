"""Pinned-state adapters for M3b hot/cold logs; no HTTP or lifecycle authority."""
from __future__ import annotations

import os
import hashlib
from pathlib import Path
import re

from ...database import dal
from .. import state_dir
from . import archives, logs
from .history import JobHistory
from .log_io import shared_read

_NAME = re.compile(r"(?:index\.json|seg-[0-9a-f]{16}\.log)\Z")


class JobLogs:
    """A selected job's bounded log access under existing state/private guards."""
    def __init__(self, store, job_id: str):
        self.store, self.job_id = store, logs.validate_job_id(job_id)
        self.directory = store.path / "jobs" / job_id / "log"

    def _validate(self, path):
        state_dir.inspect_state_dir(self.store.path, expected_identity=self.store.identity)
        self.store._check_job(self.job_id)
        state_dir.check_private(self.store.path, [self.directory, path])

    def create_writer(self, claim, **options) -> logs.LogDisk:
        """Create once under the matching live claim; never restart old evidence.

        M3c must retain the claim until all log threads settle. LogDisk only
        writes artifacts and cannot commit a lifecycle result. Private checks
        run before each file access, without taking admission during disk I/O.
        """
        with self.store._home(job_id=self.job_id), dal.connection_scope(self.store.database, readonly=True) as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (self.job_id,)).fetchone()
            if (row is None or claim.job_id != self.job_id or claim._closed or claim._handle.closed
                    or row["nonce"] != claim.nonce or row["state"] not in ("claimed", "running")
                    or row["runner_pid"] != claim.runner.pid
                    or row["runner_created_at"] != str(claim.runner.created_at)
                    or row["runner_session_id"] != claim.runner.session_id):
                raise logs.LogRefused("LIVE_MATCHING_LOG_CLAIM_REQUIRED")
        def validate(path):
            if claim._closed or claim._handle.closed:
                raise logs.LogRefused("LOG_CLAIM_CLOSED")
            self._validate(path)
        return logs.LogDisk(self.directory, self.job_id, row["nonce"], service=row["kind"] == "service",
                            validate=validate, **options)

    def read_page(self, *, cursor="0", limit=1000) -> dict:
        """Read hot or independently ledger-pinned cold members, refuse disagreement.

        Admission protects the hot/archive handover for one bounded page. Cold
        data hashes only selected <=8MiB members, never the entire batch. Legacy
        archives lacking per-member pins refuse this new fast path; their command
        reader retains its original whole-batch verification contract.
        """
        with self.store._home(job_id=self.job_id), dal.connection_scope(self.store.database, readonly=True) as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (self.job_id,)).fetchone()
            if row is None:
                raise logs.LogRefused("JOB_NOT_FOUND")
            pointer = db.execute("SELECT * FROM job_archives WHERE job_id=?", (self.job_id,)).fetchone()
            pins = {r["member"]: dict(r) for r in db.execute(
                "SELECT * FROM job_archive_members WHERE job_id=?", (self.job_id,))}
            batch = None
            if pointer is not None:
                if (pointer["nonce"] != row["nonce"] or pointer["command_digest"] != row["command_digest"]
                        or pointer["terminal_state"] != row["state"]):
                    raise logs.LogRefused("ARCHIVED_LOG_POINTER_CHANGED")
                batch = JobHistory(self.store)._batch_path(pointer["batch_file"])
            cached = [None, None]
            hot_sizes = {}
            def read(name, offset, count):
                if not _NAME.fullmatch(name):
                    raise logs.LogRefused("INVALID_LOG_MEMBER_NAME")
                path = self.directory / name
                if cached[0] == name:
                    return cached[1][offset:offset + count]
                state_dir.check_private(self.store.path, [self.directory, path])
                cold = None
                if batch is not None:
                    pin = pins.get("log/" + name)
                    if pin is None:
                        raise logs.LogRefused("ARCHIVED_LOG_MEMBER_NOT_PINNED")
                    cold = archives.read_pinned_member(batch, self.job_id, "log/" + name,
                        pin["size"], pin["sha256"], 0, pin["size"])
                if os.path.lexists(path):
                    with shared_read(path) as stream:
                        hot = stream.read(pin["size"] + 1 if cold is not None else hot_sizes.get(name, logs.MAX_INDEX_BYTES + 1))
                    if cold is not None and (len(hot) != pin["size"] or hashlib.sha256(hot).hexdigest() != pin["sha256"]):
                        raise logs.LogRefused("HOT_COLD_LOG_DISAGREEMENT")
                    if cold is None:
                        cached[:] = [name, hot]
                        return hot[offset:offset + count]
                if cold is not None:
                    cached[:] = [name, cold]  # at most one <=8MiB member per page
                    return cold[offset:offset + count]
                raise logs.LogRefused("LOG_EVIDENCE_UNAVAILABLE")
            index = logs.load_index(read, self.job_id, row["nonce"])
            hot_sizes.update({logs._segment(s["start"]): s["end"] - s["start"] for s in index["segments"]})
            return logs.page(index, read, cursor=cursor, limit=limit,
                             live=row["state"] in ("claimed", "running"))
