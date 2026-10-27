"""Durable console admission and runner claims, with no production run route.

Only a trusted server-side verifier can supply a request. Native creation and
terminal proof belong to subsequent M3 slices; this store never starts a tool.
All mutations own the admission byte lock and a DAL transaction. Runner claims
add a per-job byte lock retained by their lease, including after DB commit.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import time
from typing import Callable, Iterator, Mapping

from ...database import dal
from .. import acl, state_dir

SCHEMA_VERSION = "1"
CLAIM_BUDGET_MS = 60_000
MAX_COMMAND_BYTES = 64 * 1024
HOLDING = ("admitted", "claimed", "running", "uncertain")
_ULID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}\Z")
_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_POLICY = dal.ConnectionPolicy("console_jobs", False, 5.0,
    (("foreign_keys", "ON"), ("journal_mode", "WAL"), ("synchronous", "FULL"), ("busy_timeout", "5000")))
log = logging.getLogger("comic_automation.web")


class JobRefused(RuntimeError):
    """A missing adapter, incompatible admission or stale claim was refused."""


class LockBusy(JobRefused):
    """The bounded wait for an existing admission or runner owner expired."""


def validate_job_id(value: str) -> str:
    """Require a canonical ULID before using a job identity in a path."""
    if not isinstance(value, str) or not _ULID.fullmatch(value):
        raise JobRefused("INVALID_JOB_ID")
    return value


def _json(value: object) -> str:
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise JobRefused("INVALID_JSON") from None
    if len(text.encode("utf-8")) > MAX_COMMAND_BYTES:
        raise JobRefused("REQUEST_TOO_LARGE")
    return text


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class VerifiedRequest:
    """Trusted registry result, never built directly from browser parameters.

    M4 supplies the real verifier. Tests inject a fake registry; missing one
    refuses. Classification is registry-owned, including dry-run effects.
    """
    tool_id: str
    operator: str
    seal: Mapping
    kind: str = "task"
    readonly: bool = True
    dry_run: bool = True
    effects: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProcessIdentity:
    """Exact process identity; creation time is a Windows FILETIME integer."""
    pid: int
    created_at: int
    session_id: int

    def validate(self) -> None:
        """Require nonzero, bounded integer fields before persisting identity."""
        if (type(self.pid) is not int or not 0 < self.pid < 2**32 or type(self.created_at) is not int
                or not 0 < self.created_at < 2**64 or type(self.session_id) is not int
                or not 0 <= self.session_id < 2**32):
            raise JobRefused("INVALID_PROCESS_IDENTITY")


@dataclass
class RunnerClaim:
    """A committed claim whose runner byte lock remains held until close."""
    job_id: str
    nonce: str
    runner: ProcessIdentity
    _handle: object
    _release: Callable | None = None
    _closed: bool = False

    def close(self) -> None:
        """Release only the OS lock; the durable claimed row still holds admission."""
        if not self._closed:
            self._closed = True
            if self._release is not None:
                self._release(self)
                return
            try:
                state_dir._unlock(self._handle)
            finally:
                self._handle.close()

    def __enter__(self) -> RunnerClaim:
        if self._closed:
            raise JobRefused("CLAIM_CLOSED")
        return self

    def __exit__(self, kind, error, traceback) -> None:
        try:
            self.close()
        except BaseException as cleanup:
            if error is None:
                raise
            log.warning("Runner lock cleanup failed for %s: %s", self.job_id, cleanup)
            error.add_note(f"Runner lock cleanup also failed: {cleanup}")


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS jobs (
       admission_seq INTEGER PRIMARY KEY AUTOINCREMENT,
       job_id TEXT NOT NULL UNIQUE, nonce TEXT NOT NULL,
       tool_id TEXT NOT NULL, operator TEXT NOT NULL,
       kind TEXT NOT NULL CHECK(kind IN ('task','service')),
       readonly INTEGER NOT NULL CHECK(readonly IN (0,1)),
       dry_run INTEGER NOT NULL CHECK(dry_run IN (0,1)), effects_json TEXT NOT NULL,
       seal_json TEXT NOT NULL, seal_digest TEXT NOT NULL, command_digest TEXT NOT NULL,
       state TEXT NOT NULL CHECK(state IN ('admitted','claimed','running','uncertain',
           'admission_expired','refused','succeeded','failed','stopped','lost')),
       state_version INTEGER NOT NULL, admitted_ms INTEGER NOT NULL,
       claim_deadline_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
       runner_pid INTEGER, runner_created_at TEXT, runner_session_id INTEGER,
       child_pid INTEGER, child_created_at TEXT, job_object_name TEXT,
       exit_code INTEGER, outcome TEXT, progress_json TEXT, stop_requested_ms INTEGER)""",
    """CREATE TABLE IF NOT EXISTS job_events (
       event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
       job_id TEXT NOT NULL REFERENCES jobs(job_id), state_version INTEGER NOT NULL,
       at_ms INTEGER NOT NULL, actor TEXT NOT NULL, event TEXT NOT NULL, details_json TEXT NOT NULL)""",
    """CREATE TRIGGER IF NOT EXISTS job_events_no_update BEFORE UPDATE ON job_events
       BEGIN SELECT RAISE(ABORT, 'job_events are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS job_events_no_delete BEFORE DELETE ON job_events
       BEGIN SELECT RAISE(ABORT, 'job_events are append-only'); END""",
    "CREATE INDEX IF NOT EXISTS jobs_by_state ON jobs(state,admission_seq)",
    "CREATE INDEX IF NOT EXISTS events_by_job ON job_events(job_id,event_seq)",
    """CREATE TABLE IF NOT EXISTS job_archives (
       job_id TEXT PRIMARY KEY REFERENCES jobs(job_id), batch_file TEXT NOT NULL,
       batch_sha256 TEXT NOT NULL, archived_ms INTEGER NOT NULL, nonce TEXT NOT NULL,
       command_digest TEXT NOT NULL, terminal_state TEXT NOT NULL)""",
    """CREATE TRIGGER IF NOT EXISTS job_archives_no_update BEFORE UPDATE ON job_archives
       BEGIN SELECT RAISE(ABORT, 'archive pointers are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS job_archives_no_delete BEFORE DELETE ON job_archives
       BEGIN SELECT RAISE(ABORT, 'archive pointers are immutable'); END""",
)


class JobStore:
    """Store/admission service using one pinned home and no fallback database.

    ``verifier`` and ``watcher_observation`` are trusted server dependencies,
    not request parameters. None means unavailable. A missing watcher observer
    blocks writing tasks and services, while read-only tasks remain compatible.
    """
    def __init__(self, state: state_dir.StateDir, *, verifier: Callable | None = None,
                 watcher_observation: Callable[[], bool | None] | None = None,
                 clock_ms: Callable[[], int] | None = None, lock_timeout: float = 5.0):
        self.path, self.identity = state.path, state.identity
        if (type(lock_timeout) not in (int, float) or not math.isfinite(lock_timeout)
                or not 0 <= lock_timeout <= 60):
            raise JobRefused("INVALID_LOCK_TIMEOUT")
        self.verifier, self.watcher_observation = verifier, watcher_observation
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self.lock_timeout = lock_timeout
        with self._home(initialize=True), self._write() as db:
            row = db.execute("SELECT value FROM console_meta WHERE key='schema_version'").fetchone()
            if row is not None and row[0] != SCHEMA_VERSION:
                raise JobRefused("UNSUPPORTED_SCHEMA")
            for sql in _SCHEMA:
                db.execute(sql)
            db.execute("INSERT OR IGNORE INTO console_meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))

    @property
    def database(self) -> Path:
        """The one console ledger, always under the pinned state home."""
        return self.path / "console.sqlite3"

    def _root(self) -> None:
        st = os.stat(self.path)
        if (st.st_dev, st.st_ino) != self.identity or not all(self.identity):
            raise state_dir.StateDirRefused("State identity changed before acquiring the job lock.")
        if acl.privacy_problems(self.path):
            raise state_dir.StateDirRefused("State home is no longer private.")

    def _lock(self, relative: Path, *, retry: bool = True):
        self._root()
        target = self.path / "locks" / relative
        state_dir.check_private(self.path, [self.path / "locks", target.parent, target])
        target.parent.mkdir(exist_ok=True, parents=True)
        state_dir.check_private(self.path, [self.path / "locks", target.parent, target])
        handle = open(target, "a+b")
        deadline = time.monotonic() + self.lock_timeout
        while not state_dir._try_lock(handle):
            if not retry or time.monotonic() >= deadline:
                handle.close()
                raise LockBusy("JOB_LOCK_HELD")
            time.sleep(0.01)
        return handle

    @contextmanager
    def _home(self, *, initialize: bool = False, job_id: str | None = None) -> Iterator[state_dir.StateDir]:
        handle = self._lock(Path("admission.lock"))
        try:
            view = state_dir.inspect_state_dir(self.path, expected_identity=self.identity)
            if job_id is not None:
                self._check_job(job_id)
            with dal.connection_scope(self.database, readonly=True) as db:
                row = db.execute("SELECT value FROM console_meta WHERE key='schema_version'").fetchone()
            if (row is None and not initialize) or (row is not None and row[0] != SCHEMA_VERSION):
                raise JobRefused("UNSUPPORTED_SCHEMA")
            yield view
        finally:
            try:
                state_dir._unlock(handle)
            finally:
                handle.close()

    def _check_job(self, job_id: str) -> None:
        validate_job_id(job_id)
        root = self.path / "jobs" / job_id
        state_dir.check_private(self.path, [root, root / "command.json", root / "exit.json",
            root / "stop.request", root / "log", root / "logs",
            self.path / "locks" / "jobs", self.path / "locks" / "jobs" / f"{job_id}.runner.lock"])

    def _close_runner_lock(self, job_id: str, handle) -> None:
        """Called under admission exclusion; never unlink a different file."""
        path = self.path / "locks" / "jobs" / f"{job_id}.runner.lock"
        own = os.fstat(handle.fileno())
        try:
            state_dir.check_private(self.path, [path])
            current = path.stat()
            if (current.st_dev, current.st_ino) != (own.st_dev, own.st_ino):
                raise JobRefused("RUNNER_LOCK_IDENTITY_CHANGED")
        finally:
            try:
                state_dir._unlock(handle)
            finally:
                handle.close()
        try:
            current = path.stat()
        except FileNotFoundError:
            return
        if (current.st_dev, current.st_ino) != (own.st_dev, own.st_ino):
            raise JobRefused("RUNNER_LOCK_IDENTITY_CHANGED")
        path.unlink()

    def _release_claim(self, claim: RunnerClaim) -> None:
        try:
            with self._home(job_id=claim.job_id):
                self._close_runner_lock(claim.job_id, claim._handle)
        finally:
            if not claim._handle.closed:
                try:
                    state_dir._unlock(claim._handle)
                finally:
                    claim._handle.close()

    @contextmanager
    def _write(self):
        db = dal.open_connection(self.database, policy=_POLICY)
        try:
            with dal.transaction(db):
                yield db
        finally:
            db.close()

    def _time(self, db) -> int:
        now = self.clock_ms()
        if type(now) is not int or not 0 <= now < 2**48:
            raise JobRefused("INVALID_CLOCK")
        previous = db.execute("SELECT value FROM console_meta WHERE key='clock_high_water_ms'").fetchone()
        if previous is not None and now < int(previous[0]):
            raise JobRefused("CLOCK_MOVED_BACKWARD")
        db.execute("INSERT OR REPLACE INTO console_meta VALUES ('clock_high_water_ms', ?)", (str(now),))
        return now

    @staticmethod
    def _event(db, row, event: str, actor: str, now: int, details: object = None) -> None:
        db.execute("INSERT INTO job_events(job_id,state_version,at_ms,actor,event,details_json) VALUES(?,?,?,?,?,?)",
                   (row["job_id"], row["state_version"], now, actor, event, _json(details or {})))

    @staticmethod
    def _compatible(rows, request: VerifiedRequest, watcher: bool | None) -> None:
        if watcher is not None and type(watcher) is not bool:
            raise JobRefused("INVALID_WATCHER_OBSERVATION")
        if any(row["state"] == "uncertain" for row in rows):
            raise JobRefused("JOB_UNCERTAIN")
        tasks = [row for row in rows if row["kind"] == "task"]
        services = [row for row in rows if row["kind"] == "service"]
        if request.kind == "task":
            if tasks or (not request.readonly and (services or watcher is not False)):
                raise JobRefused("JOB_ACTIVE_OR_WATCHER_UNKNOWN")
        elif services or watcher is not False or any(not row["readonly"] for row in tasks):
            raise JobRefused("JOB_ACTIVE_OR_WATCHER_UNKNOWN")

    @staticmethod
    def _active(db):
        return db.execute("SELECT * FROM jobs WHERE state IN ('admitted','claimed','running','uncertain')").fetchall()

    def admit(self, parameters: Mapping) -> dict:
        """Verify and persist one admission; never spawn a runner or accept raw argv.

        The small immutable command is prepared under OS exclusion, before the
        DB transaction. This avoids racing the shared privacy scan with our own
        temporary files; fsync never happens inside the database transaction.
        """
        if self.verifier is None:
            raise JobRefused("TRUSTED_VERIFIER_REQUIRED")
        if not isinstance(parameters, Mapping) or {"argv", "cmd", "command"} & set(parameters):
            raise JobRefused("COMMAND_NOT_ACCEPTED")
        supplied = json.loads(_json(parameters))
        request = self.verifier(supplied)
        if not isinstance(request, VerifiedRequest):
            raise JobRefused("INVALID_VERIFIED_REQUEST")
        if (request.kind not in ("task", "service") or type(request.readonly) is not bool
                or type(request.dry_run) is not bool or not isinstance(request.seal, Mapping)
                or not isinstance(request.tool_id, str) or not 1 <= len(request.tool_id) <= 256
                or not isinstance(request.operator, str) or not 1 <= len(request.operator) <= 256
                or not isinstance(request.effects, (tuple, list))
                or any(not isinstance(item, str) or not 1 <= len(item) <= 64 for item in request.effects)):
            raise JobRefused("INVALID_VERIFIED_REQUEST")
        seal = _json(dict(request.seal))
        effects = _json(list(request.effects))
        with self._home():
            watcher = self.watcher_observation() if self.watcher_observation else None
            with dal.connection_scope(self.database, readonly=True) as db:
                self._compatible(self._active(db), request, watcher)
            now = self.clock_ms()
            if type(now) is not int or not 0 <= now < 2**48:
                raise JobRefused("INVALID_CLOCK")
            value = (now << 80) | secrets.randbits(80)
            job_id = "".join(_ALPHABET[(value >> (5 * n)) & 31] for n in reversed(range(26)))
            validate_job_id(job_id)
            nonce = secrets.token_hex(16)
            payload = _json({"job_id": job_id, "nonce": nonce, "state_identity": self.identity,
                             "seal": json.loads(seal),
                             "tool_id": request.tool_id, "kind": request.kind, "readonly": request.readonly,
                             "operator": request.operator, "dry_run": request.dry_run,
                             "effects": json.loads(effects)}).encode("utf-8")
            home = self.path / "jobs" / job_id
            home.parent.mkdir(exist_ok=True)
            home.mkdir()  # no existing directory is reused
            temporary = home / "command.json.tmp"
            with open(temporary, "xb") as command:
                command.write(payload)
                command.flush()
                os.fsync(command.fileno())
            os.replace(temporary, home / "command.json")
            self._check_job(job_id)
            if (home / "command.json").read_bytes() != payload:
                raise JobRefused("COMMAND_READBACK_MISMATCH")
            self._check_job(job_id)
            with self._write() as db:
                self._compatible(self._active(db), request, watcher)
                committed_at = self._time(db)
                if committed_at < now:
                    raise JobRefused("CLOCK_MOVED_BACKWARD")
                db.execute("""INSERT INTO jobs(job_id,nonce,tool_id,operator,kind,readonly,dry_run,effects_json,
                    seal_json,seal_digest,command_digest,state,state_version,admitted_ms,claim_deadline_ms,updated_ms)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,'admitted',1,?,?,?)""",
                    (job_id, nonce, request.tool_id, request.operator, request.kind, request.readonly,
                     request.dry_run, effects, seal, _digest(seal.encode()), _digest(payload),
                     committed_at, committed_at + CLAIM_BUDGET_MS, committed_at))
                row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                self._event(db, row, "admitted", request.operator, committed_at)
                return dict(row)

    def get(self, job_id: str) -> dict:
        """Read an internal job record; HTTP redaction belongs to the service layer."""
        validate_job_id(job_id)
        with self._home(job_id=job_id), dal.connection_scope(self.database, readonly=True) as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise JobRefused("JOB_NOT_FOUND")
            return dict(row)

    def events(self, job_id: str, *, after: int = 0, limit: int = 100) -> list[dict]:
        """Return a bounded append-only event page using the global event sequence."""
        validate_job_id(job_id)
        if type(after) is not int or not 0 <= after < 2**63 or type(limit) is not int or not 1 <= limit <= 1000:
            raise JobRefused("INVALID_EVENT_PAGE")
        with self._home(job_id=job_id), dal.connection_scope(self.database, readonly=True) as db:
            return [dict(row) for row in db.execute("SELECT * FROM job_events WHERE job_id=? AND event_seq>? "
                         "ORDER BY event_seq LIMIT ?", (job_id, after, limit))]

    def claim(self, job_id: str, nonce: str, runner: ProcessIdentity) -> RunnerClaim:
        """Commit exactly one unexpired claim, retaining its per-runner lock."""
        validate_job_id(job_id)
        runner.validate()
        if not isinstance(nonce, str) or not _NONCE.fullmatch(nonce):
            raise JobRefused("INVALID_NONCE")
        handle = None
        try:
            with self._home(job_id=job_id), self._write() as db:
                handle = self._lock(Path("jobs") / f"{job_id}.runner.lock", retry=False)
                self._check_job(job_id)
                now = self._time(db)
                changed = db.execute("""UPDATE jobs SET state='claimed',state_version=state_version+1,
                    updated_ms=?,runner_pid=?,runner_created_at=?,runner_session_id=?
                    WHERE job_id=? AND nonce=? AND state='admitted' AND admitted_ms<=? AND claim_deadline_ms>?""",
                    (now, runner.pid, str(runner.created_at), runner.session_id, job_id, nonce, now, now))
                if changed.rowcount != 1:
                    raise JobRefused("CLAIM_FENCED")
                row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                self._event(db, row, "claimed", f"runner:{runner.pid}:{runner.created_at}", now)
            return RunnerClaim(job_id, nonce, runner, handle, self._release_claim)
        except BaseException:
            if handle is not None:
                # Leave a closed failed-claim lock for retention; removing it
                # here would race claimers after admission exclusion unwinds.
                try:
                    state_dir._unlock(handle)
                finally:
                    handle.close()
            raise

    def expire_admissions(self) -> list[str]:
        """Fence only still-unclaimed rows past their persisted deadline."""
        expired = []
        with self._home(), self._write() as db:
            now = self._time(db)
            rows = db.execute("SELECT * FROM jobs WHERE state='admitted' AND claim_deadline_ms<=? AND admitted_ms<=?",
                              (now, now)).fetchall()
            for old in rows:
                self._check_job(old["job_id"])
                changed = db.execute("UPDATE jobs SET state='admission_expired',state_version=state_version+1,updated_ms=? "
                    "WHERE job_id=? AND nonce=? AND state='admitted' AND state_version=?",
                    (now, old["job_id"], old["nonce"], old["state_version"]))
                if changed.rowcount != 1:
                    raise JobRefused("EXPIRY_FENCED")
                row = db.execute("SELECT * FROM jobs WHERE job_id=?", (old["job_id"],)).fetchone()
                self._event(db, row, "admission_expired", "reconciler", now)
                expired.append(old["job_id"])
        return expired

    def hold_uncertain(self, job_id: str, nonce: str, version: int, *, actor: str, reason: str) -> dict:
        """Retain admission after inconclusive evidence; no automatic release API."""
        validate_job_id(job_id)
        if (not isinstance(nonce, str) or not _NONCE.fullmatch(nonce)
                or type(version) is not int or not 0 < version < 2**63
                or not isinstance(actor, str) or not 1 <= len(actor) <= 256
                or not isinstance(reason, str) or len(reason) > 4096):
            raise JobRefused("INVALID_UNCERTAIN_REQUEST")
        with self._home(job_id=job_id), self._write() as db:
            now = self._time(db)
            changed = db.execute("UPDATE jobs SET state='uncertain',state_version=state_version+1,updated_ms=? "
                "WHERE job_id=? AND nonce=? AND state_version=? AND state IN ('admitted','claimed','running')",
                (now, job_id, nonce, version))
            if changed.rowcount != 1:
                raise JobRefused("UNCERTAIN_FENCED")
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            self._event(db, row, "uncertain", actor, now, {"reason": reason})
            return dict(row)
