"""Bounded, advisory log progress with a cursor independent of job lifecycle.

This is the persistence/consumer part of D3, not an HTTP route or scheduler.
Only the service consumer calls consume_once; GET/SSE readers use snapshot.
The additive schema leaves schema-v1 runners and their version fences alone.
"""
from __future__ import annotations

import json

from ...database import dal
from . import logs
from .log_service import JobLogs
from .store import JobRefused, _NONCE, _json, validate_job_id


_SCHEMA = """CREATE TABLE IF NOT EXISTS job_observations (
    job_id TEXT NOT NULL REFERENCES jobs(job_id), nonce TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation > 0), snapshot_json TEXT NOT NULL,
    PRIMARY KEY(job_id,nonce))"""


class JobObservations:
    """One durable parse position per exact job/nonce, with bounded catch-up."""
    def __init__(self, store):
        self.store = store

    def initialize(self) -> None:
        """Explicit additive initialization, including while an old runner owns a claim."""
        with self.store._home(), self.store._write() as db:
            db.execute(_SCHEMA)

    @staticmethod
    def _identity(db, job_id, nonce) -> None:
        row = db.execute("SELECT nonce FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise JobRefused("JOB_NOT_FOUND")
        if row[0] != nonce:
            raise JobRefused("OBSERVATION_NONCE_MISMATCH")

    @staticmethod
    def _validate(job_id, nonce) -> None:
        validate_job_id(job_id)
        if not isinstance(nonce, str) or not _NONCE.fullmatch(nonce):
            raise JobRefused("INVALID_NONCE")

    @staticmethod
    def _read(db, job_id, nonce) -> dict:
        row = db.execute("SELECT generation,snapshot_json FROM job_observations "
                         "WHERE job_id=? AND nonce=?", (job_id, nonce)).fetchone()
        if row is not None:
            return dict(json.loads(row[1]), generation=row[0])
        return dict(generation=0, cursor="0", proven_offset="0", log_status="unobserved",
                    eof=False, progress=None, gap_count=0, last_gap=None)

    def snapshot(self, job_id: str, nonce: str) -> dict:
        """Read only; an unobserved or missing tail never implies EOF or completion."""
        self._validate(job_id, nonce)
        with self.store._home(job_id=job_id), dal.connection_scope(self.store.database, readonly=True) as db:
            self._identity(db, job_id, nonce)
            return self._read(db, job_id, nonce)

    def consume_once(self, job_id: str, nonce: str, *, limit: int = 1000) -> dict:
        """Read at most one bounded log page, then atomically advance its parse cursor.

        No admission lock, SQLite transaction or segment handle spans the read
        and commit. A competing consumer or changed nonce refuses this page;
        retry from the durable snapshot on the next service tick, never rewind.
        Progress fragments are deliberately not assembled: parse_progress only
        accepts complete bounded records. A gap is retained as advisory loss,
        while lifecycle completion remains solely the runner's decision.
        """
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise logs.LogRefused("INVALID_LOG_PAGE")
        before = self.snapshot(job_id, nonce)
        try:
            page = JobLogs(self.store, job_id).read_page(cursor=before["cursor"], limit=limit)
        except logs.LogRefused:
            # Retain the last progress/cursor, but never leave an earlier EOF
            # looking current after mapped evidence becomes unreadable.
            self._commit(job_id, nonce, before, dict(before, log_status="unavailable", eof=False))
            raise
        if logs.decode_cursor(page["proven_offset"]) < logs.decode_cursor(before["proven_offset"]):
            raise logs.LogRefused("OBSERVATION_LOG_REGRESSED")
        after = dict(before, cursor=page["next_cursor"], proven_offset=page["proven_offset"],
                     log_status=page["log_status"], eof=page["eof"])
        for record in page["records"]:
            if record["type"] == "gap":
                after["gap_count"] += 1
                after["last_gap"] = record
            progress = logs.parse_progress(record)
            if progress is not None:
                after["progress"] = progress
        return self._commit(job_id, nonce, before, after)

    def _commit(self, job_id, nonce, before, after) -> dict:
        with self.store._home(job_id=job_id), self.store._write() as db:
            self._identity(db, job_id, nonce)
            current = self._read(db, job_id, nonce)
            if current["generation"] != before["generation"]:
                raise JobRefused("STALE_OBSERVATION")
            if after == before:
                return current
            if before["generation"] >= 2**63 - 1:
                raise JobRefused("OBSERVATION_GENERATION_EXHAUSTED")
            after["generation"] += 1
            db.execute("INSERT INTO job_observations VALUES(?,?,?,?) "
                       "ON CONFLICT(job_id,nonce) DO UPDATE SET "
                       "generation=excluded.generation,snapshot_json=excluded.snapshot_json",
                       (job_id, nonce, after["generation"], _json({key: value for key, value in after.items()
                                                                              if key != "generation"})))
            return after
