"""Bounded, read-only job DTOs for the browser, never raw ledger records.

Reads retain the pinned home's admission guard only for one page. Detail and
events share a deferred SQLite snapshot; no transaction survives the response.
Commands, seals, process identities and arbitrary event details stay private.
"""
from __future__ import annotations

import re

from ...database import dal
from .store import JobRefused, validate_job_id

_COLUMNS = ("admission_seq,job_id,nonce,state_version,tool_id,kind,readonly,dry_run,"
            "state,admitted_ms,updated_ms,exit_code,outcome")
_LABEL = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_EVENTS = frozenset({"admitted", "claimed", "running", "admission_expired", "refused",
                     "succeeded", "failed", "stopped", "lost", "uncertain", "operator_recheck",
                     "archived", "archive_cleanup"})


def _summary(row) -> dict:
    """Whitelist fields; free-form refusal reasons are not display outcomes."""
    return {"job_id": row["job_id"], "admission_seq": str(row["admission_seq"]),
            "nonce": row["nonce"], "state_version": row["state_version"],
            "tool_id": row["tool_id"] if _LABEL.fullmatch(row["tool_id"]) else "unknown",
            "kind": row["kind"], "readonly": bool(row["readonly"]), "dry_run": bool(row["dry_run"]),
            "state": row["state"], "admitted_ms": row["admitted_ms"], "updated_ms": row["updated_ms"],
            "exit_code": row["exit_code"],
            "outcome": row["outcome"] if row["outcome"] in {"succeeded", "failed", "stopped", "unknown"} else None}


class JobViews:
    """Read redacted pages, including cold jobs whose ledger rows are retained."""

    def __init__(self, store):
        self.store = store

    def _read(self, read, *, job_id=None):
        with self.store._home(job_id=job_id), dal.connection_scope(self.store.database, readonly=True) as db:
            before = db.execute("PRAGMA data_version").fetchone()[0]
            db.execute("BEGIN")
            try:
                result = read(db)
            finally:
                db.execute("END")
            if db.execute("PRAGMA data_version").fetchone()[0] != before:
                raise JobRefused("JOB_VIEW_CHANGED")
            return result

    def list(self, *, before=None, limit=50) -> dict:
        """Newest-first keyset page; decimal string cursors preserve 64-bit IDs."""
        if (before is not None and (type(before) is not int or not 1 <= before < 2**63)
                or type(limit) is not int or not 1 <= limit <= 100):
            raise JobRefused("INVALID_JOB_PAGE")
        def read(db):
            where = "WHERE admission_seq<? " if before is not None else ""
            parameters = ((before,) if before is not None else ()) + (limit+1,)
            rows = db.execute(f"SELECT {_COLUMNS} FROM jobs {where}"
                              "ORDER BY admission_seq DESC LIMIT ?", parameters).fetchall()
            return {"jobs": [_summary(row) for row in rows[:limit]],
                    "next": str(rows[limit-1]["admission_seq"]) if len(rows) > limit else None}
        return self._read(read)

    def detail(self, job_id, *, after=0, limit=100) -> dict:
        """One summary and a forward event page from the same read snapshot."""
        validate_job_id(job_id)
        if type(after) is not int or not 0 <= after < 2**63 or type(limit) is not int or not 1 <= limit <= 1000:
            raise JobRefused("INVALID_EVENT_PAGE")
        def read(db):
            row = db.execute(f"SELECT {_COLUMNS} FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise JobRefused("JOB_NOT_FOUND")
            events = db.execute("SELECT event_seq,state_version,at_ms,event FROM job_events "
                                "WHERE job_id=? AND event_seq>? ORDER BY event_seq LIMIT ?",
                                (job_id, after, limit+1)).fetchall()
            return {"job": _summary(row), "events": [
                {"event_seq": str(event["event_seq"]), "state_version": event["state_version"],
                 "at_ms": event["at_ms"], "event": event["event"] if event["event"] in _EVENTS else "other"}
                for event in events[:limit]],
                "next": str(events[limit-1]["event_seq"]) if len(events) > limit else None}
        return self._read(read, job_id=job_id)
