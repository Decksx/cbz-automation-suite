"""Fresh recovery evidence and fenced store transitions, without scheduling.

Every mutation samples native facts itself under admission exclusion. Reports
are diagnostics, never transferable authority to release admission. No path
adopts a named job, kills a PID, launches a replacement or clears uncertainty
implicitly. M3d owns protected routes and scheduling.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import os

from ...database import dal
from . import receipts, win32
from .store import JobRefused, ProcessIdentity, validate_job_id


def _process(pid, created, session):
    identity = ProcessIdentity(pid, int(created), session)
    identity.validate()
    return identity


def observe(row) -> dict:
    """Sample exact owner/child and same-session job facts, closing query handles.

    PID reuse proves only the recorded identity gone. A present empty job is
    still present; session mismatch/access denial is unknown. This report has
    no lifetime or commit authority and must not be cached for a mutation.
    """
    result = dict(job_id=row["job_id"], nonce=row["nonce"], state_version=row["state_version"],
                  runner=dict(state="unknown"), child=dict(state="unknown"), job=dict(state="unknown"),
                  recorded=dict(runner=dict(pid=row["runner_pid"], created_at=row["runner_created_at"],
                      session_id=row["runner_session_id"]), child=dict(pid=row["child_pid"],
                      created_at=row["child_created_at"], session_id=row["runner_session_id"]),
                      job_object_name=row["job_object_name"], expected_job_name=win32.job_name(row["job_id"])))
    try:
        runner = _process(row["runner_pid"], row["runner_created_at"], row["runner_session_id"])
        for key, identity in (("runner", runner), ("child", None if row["child_pid"] is None else
                _process(row["child_pid"], row["child_created_at"], runner.session_id))):
            if identity is None:
                result[key] = dict(state="not_recorded")
                continue
            item = asdict(win32.observe_process(identity))
            if item["identity"] is not None:
                item["identity"]["created_at"] = str(item["identity"]["created_at"])
            result[key] = item
        expected_name = win32.job_name(row["job_id"])
        if (row["job_object_name"] != expected_name and not
                (row["state"] == "claimed" and row["child_pid"] is None and row["job_object_name"] is None)):
            result["job"] = dict(state="unknown", reason="FOREIGN_JOB_NAME")
        else:
            result["job"] = asdict(win32.observe_job(row["job_id"], runner.session_id))
    except (OSError, JobRefused, ValueError, TypeError) as exc:
        result["error"] = type(exc).__name__ + ":" + str(exc)
    return result


def _gone(evidence, *, before_child=False):
    return ("error" not in evidence and evidence["runner"]["state"] == "gone"
            and (evidence["child"]["state"] == "gone" or
                 before_child and evidence["child"]["state"] == "not_recorded")
            and evidence["job"]["state"] == "absent")


def _read_row(store, job_id):
    with dal.connection_scope(store.database, readonly=True) as db:
        row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        raise JobRefused("JOB_NOT_FOUND")
    return dict(row)


def _fence(row, nonce, version):
    if (type(version) is not int or not 0 < version < 2**63 or row["nonce"] != nonce
            or row["state_version"] != version):
        raise JobRefused("RECOVERY_FENCED")


def _commit(store, old, state, event, actor, details, *, receipt=None):
    # Native queries and all receipt reads have finished before this short write.
    with store._write() as db:
        current = dict(db.execute("SELECT * FROM jobs WHERE job_id=?", (old["job_id"],)).fetchone())
        if current != old:
            raise JobRefused("RECOVERY_FENCED")
        now = store._time(db)
        if receipt is not None and now < receipt["ended_ms"]:
            raise JobRefused("RECEIPT_FROM_FUTURE")
        db.execute("UPDATE jobs SET state=?,state_version=state_version+1,updated_ms=?,outcome=?,exit_code=? "
                   "WHERE job_id=?", (state, now, receipt["state"] if receipt else
                       ("unknown" if state == "lost" else old["outcome"]),
                       receipt["exit_code"] if receipt else None if state == "lost" else old["exit_code"], old["job_id"]))
        row = db.execute("SELECT * FROM jobs WHERE job_id=?", (old["job_id"],)).fetchone()
        store._event(db, row, event, actor, now, details)
        return dict(row)


def reconcile(store, job_id: str, nonce: str, version: int, *, outcome_mapper=None) -> dict:
    """Recover one eligible row using fresh native proof and bounded private reads.

    A matching live runner retains ownership. Dead-owner running/claimed rows
    become lost only on complete proof; a valid published receipt may instead
    import its trusted mapped result. Durable uncertain is never auto-cleared.
    Repeating the same receipt import returns the prior row without another
    event. M4 supplies the trusted outcome mapper; JSON cannot select it.
    """
    validate_job_id(job_id)
    with store._home(job_id=job_id):
        old = _read_row(store, job_id)
        if old["nonce"] == nonce and old["state"] in ("succeeded", "failed"):
            with dal.connection_scope(store.database, readonly=True) as db:
                event = db.execute("SELECT details_json FROM job_events WHERE job_id=? AND "
                    "event='recovered_receipt' AND state_version=?", (job_id, old["state_version"])).fetchone()
            if (type(version) is int and event is not None
                    and version in (old["state_version"], json.loads(event[0])["source_version"])):
                return old
        _fence(old, nonce, version)
        if old["state"] == "uncertain":
            return old
        if old["state"] not in ("claimed", "running"):
            raise JobRefused("RECOVERY_STATE_INELIGIBLE")
        evidence = observe(old)
        if evidence["runner"]["state"] == "alive":
            return old
        state, reason, receipt, digest = "uncertain", "NATIVE_RECOVERY_INCONCLUSIVE", None, None
        if _gone(evidence, before_child=old["state"] == "claimed"):
            root = store.path / "jobs" / job_id
            if os.path.lexists(root / "exit.json.tmp"):
                reason = "ORPHAN_EXIT_RECEIPT_TEMP"
            elif os.path.lexists(root / "exit.json"):
                try:
                    if old["state"] != "running":
                        raise JobRefused("RECEIPT_REQUIRES_RECORDED_CHILD")
                    receipt, digest = receipts.read_for_recovery(store, old, outcome_mapper)
                    if store.clock_ms() < receipt["ended_ms"]:
                        raise JobRefused("RECEIPT_FROM_FUTURE")
                    # File inspection can take time; sample native absence again
                    # immediately before committing, still under admission.
                    evidence = observe(old)
                    if not _gone(evidence):
                        raise JobRefused("RECOVERY_PROOF_CHANGED")
                    state, reason = receipt["state"], "MATCHING_DURABLE_RECEIPT"
                except (OSError, JobRefused) as exc:
                    receipt = None
                    reason = type(exc).__name__ + ":" + str(exc)
            else:
                state, reason = "lost", "OWNER_GONE_OUTCOME_UNKNOWN"
        if state == "lost":
            evidence = observe(old)
            if not _gone(evidence, before_child=old["state"] == "claimed"):
                state, reason = "uncertain", "RECOVERY_PROOF_CHANGED"
        details = dict(reason=reason, evidence=evidence, source_version=version)
        if receipt is not None:
            details.update(receipt_sha256=digest, durable_offset=receipt["logs"]["durable_offset"])
        return _commit(store, old, state, "recovered_receipt" if receipt else state,
                       "reconciler", details, receipt=receipt)


def recheck(store, job_id: str, nonce: str, version: int, *, actor: str, note: str) -> dict:
    """Explicit attributed uncertain-to-lost acknowledgement, never guessed success.

    M3d must call only after authenticated operator intent, including log-only
    uncertainty. Every attempt uses fresh proof and records an event; a failed
    attempt retains uncertainty and increments the version to fence replay.
    No desired outcome or caller-supplied native evidence is accepted.
    """
    validate_job_id(job_id)
    if (not isinstance(actor, str) or not actor.strip() or len(actor) > 256
            or not isinstance(note, str) or not note.strip() or len(note) > 4096):
        raise JobRefused("OPERATOR_ACKNOWLEDGEMENT_REQUIRED")
    with store._home(job_id=job_id):
        old = _read_row(store, job_id)
        _fence(old, nonce, version)
        if old["state"] != "uncertain":
            raise JobRefused("RECHECK_REQUIRES_UNCERTAIN")
        evidence = observe(old)
        state = "lost" if _gone(evidence) else "uncertain"
        return _commit(store, old, state, "operator_recheck", actor,
            dict(note=note, evidence=evidence, acknowledged_unknown_outcome=state == "lost"))
