"""Console-owned reconciliation and detached runner birth, without HTTP policy.

One service owns startup/scheduled passes for one pinned home. It never adopts
or stops native tools. Trusted verifier/mapper dependencies are internal; no
production registry or browser submission route is supplied by this slice.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import logging
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from ...database import dal
from ..config import REPO_ROOT
from ..state_dir import STATE_DIR_ENV
from . import recovery
from .store import JobRefused, JobStore

log = logging.getLogger(__name__)


class SpawnUnconfirmed(JobRefused):
    """Admission committed but spawn returned no proof; never retry that birth.

    The row retains admission until its claim deadline/reconciliation. Even an
    exception after native birth cannot authorize a replacement runner.
    """
    def __init__(self, job_id):
        super().__init__("RUNNER_SPAWN_UNCONFIRMED:" + job_id)
        self.job_id = job_id


def parent_job_membership() -> bool:
    """Measure current native containment; unknown is a refusal, never absence.

    A detached creation flag does not escape an inherited kill-on-close Job
    Object. We conservatively refuse birth under *any* parent job: querying
    only an innermost job's limits cannot exclude a killing outer ancestor.
    This deliberately does not request breakaway or change the parent's job.
    """
    if os.name != "nt":
        raise JobRefused("DETACHED_RUNNER_REQUIRES_WINDOWS")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.argtypes, kernel.GetCurrentProcess.restype = [], wintypes.HANDLE
    kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    kernel.IsProcessInJob.restype = wintypes.BOOL
    result = wintypes.BOOL()
    if not kernel.IsProcessInJob(kernel.GetCurrentProcess(), None, ctypes.byref(result)):
        raise JobRefused("PARENT_JOB_QUERY_FAILED:" + str(ctypes.get_last_error()))
    return bool(result.value)


class Supervisor:
    """Own one console's readiness, bounded passes and trusted submit seam.

    ``state`` must retain its console lock until close() reports settled. A
    failed bounded close requires wait_closed() before releasing that lock;
    it is not permission to cancel storage or stop detached runners.
    """
    def __init__(self, state, *, verifier=None, watcher_observation=None,
                 outcome_mapper=None, interval=30.0, page_size=100):
        if (type(interval) not in (int, float) or not math.isfinite(interval) or interval <= 0
                or type(page_size) is not int or not 1 <= page_size <= 1000):
            raise JobRefused("INVALID_SUPERVISOR_BOUNDS")
        if state._lock is None:
            raise JobRefused("CONSOLE_OWNERSHIP_REQUIRED")
        self.state = state
        self.store = JobStore(state, verifier=verifier, watcher_observation=watcher_observation)
        self.outcome_mapper = outcome_mapper
        self.interval, self.page_size = interval, page_size
        self._condition = threading.Condition()
        self._pass_guard = threading.Lock()
        self._stop = threading.Event()
        self._started = False
        self._starting = False
        self._ready = False
        self._active = 0
        self._thread = None
        self.error = None

    @property
    def ready(self):
        """Admission is possible only after a successful pass and before close."""
        with self._condition:
            return self._ready and not self._stop.is_set()

    @contextmanager
    def _operation(self, *, submission=False):
        with self._condition:
            if self._stop.is_set() or submission and not self._ready:
                raise JobRefused("SUPERVISOR_NOT_READY")
            if self.state._lock is None:
                raise JobRefused("CONSOLE_OWNERSHIP_REQUIRED")
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def reconcile_once(self):
        """Visit a bounded snapshot in sequence order; concurrent passes skip.

        Holding pages are detached snapshots, not authority. Each recovery
        call takes its own admission exclusion and refences its row. No outer
        admission lock or DB transaction spans a native query/recovery call.
        """
        if not self._pass_guard.acquire(blocking=False):
            return False
        try:
            with self._operation():
                try:
                    self.store.expire_admissions()
                    with self.store._home(), dal.connection_scope(self.store.database, readonly=True) as db:
                        cutoff = db.execute("SELECT COALESCE(MAX(admission_seq),0) FROM jobs").fetchone()[0]
                    after = 0
                    while not self._stop.is_set():
                        with self.store._home(), dal.connection_scope(self.store.database, readonly=True) as db:
                            rows = [dict(row) for row in db.execute(
                                "SELECT admission_seq,job_id,nonce,state_version FROM jobs "
                                "WHERE admission_seq>? AND admission_seq<=? AND state IN ('claimed','running') "
                                "ORDER BY admission_seq LIMIT ?", (after, cutoff, self.page_size))]
                        if not rows:
                            break
                        for row in rows:
                            if self._stop.is_set():
                                break
                            try:
                                recovery.reconcile(self.store, row["job_id"], row["nonce"],
                                    row["state_version"], outcome_mapper=self.outcome_mapper)
                            except JobRefused as exc:
                                # A live owner's intervening commit is expected;
                                # storage/native errors must still disable readiness.
                                if str(exc) not in ("RECOVERY_FENCED", "RECOVERY_STATE_INELIGIBLE"):
                                    raise
                        after = rows[-1]["admission_seq"]
                    with self._condition:
                        self.error = None
                        if self._started and not self._stop.is_set():
                            self._ready = True
                    return True
                except Exception as exc:
                    with self._condition:
                        self.error = type(exc).__name__ + ":" + str(exc)
                        self._ready = False
                    raise
        finally:
            self._pass_guard.release()

    def start(self):
        """Reconcile synchronously before readiness, then start the sole scheduler."""
        with self._condition:
            if self._started or self._starting or self._stop.is_set():
                raise JobRefused("SUPERVISOR_ALREADY_STARTED_OR_CLOSED")
            self._starting = True
            # Count startup through thread publication so close cannot release
            # ownership in the gap between the initial pass and scheduler birth.
            self._active += 1
        try:
            if not self.reconcile_once():
                raise JobRefused("STARTUP_PASS_ALREADY_IN_FLIGHT")
            with self._condition:
                if self._stop.is_set():
                    raise JobRefused("SUPERVISOR_CLOSED_DURING_STARTUP")
                self._thread = threading.Thread(target=self._schedule, name="console-reconciler", daemon=True)
                try:
                    self._thread.start()
                except Exception:
                    self._thread = None
                    raise
                self._started = self._ready = True
        finally:
            with self._condition:
                self._starting = False
                self._active -= 1
                self._condition.notify_all()

    def _schedule(self):
        while not self._stop.wait(self.interval):
            try:
                self.reconcile_once()
            except Exception:
                # Private logs retain details; HTTP DTOs must not expose paths.
                log.exception("Console reconciliation unavailable; admission disabled")

    def _spawn(self, row):
        # The cwd and interpreter are server code context, never command JSON.
        # No PYTHONPATH/user site or inherited browser-selected environment.
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP")}
        environment[STATE_DIR_ENV] = str(self.store.path)
        environment["CBZ_CONSOLE_STATE_IDENTITY"] = ":".join(map(str, self.store.identity))
        return subprocess.Popen([str(Path(sys.executable).resolve()), "-E", "-s", "-m",
            "comic_automation.web.jobs.runner", row["job_id"], row["nonce"]],
            cwd=REPO_ROOT, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)

    def submit(self, parameters):
        """Admit then attempt exactly one fixed runner spawn; no automatic retry.

        The default verifier is absent and refuses. M4 must supply production
        reconstruction in code; tests alone provide a disposable fixture seam.
        """
        with self._operation(submission=True):
            if self.store.verifier is None:
                raise JobRefused("TRUSTED_VERIFIER_REQUIRED")
            if parent_job_membership():
                raise JobRefused("DETACHED_RUNNER_PARENT_IN_JOB")
            row = self.store.admit(parameters)
            try:
                self._spawn(row)
            except Exception as exc:
                raise SpawnUnconfirmed(row["job_id"]) from exc
            return row

    def close(self, timeout=2.0):
        """Close admission and bounded-wait service work; never touch a runner.

        False means the caller must retain console ownership and wait_closed()
        or remain alive until external process death. Threads are not cancelled.
        """
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
            raise JobRefused("INVALID_SHUTDOWN_TIMEOUT")
        deadline = time.monotonic() + timeout
        self._stop.set()
        with self._condition:
            self._ready = False
            self._condition.wait_for(lambda: not self._active, timeout=max(0, deadline - time.monotonic()))
            settled = not self._active
        thread = self._thread
        if settled and thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0, deadline - time.monotonic()))
            settled = not thread.is_alive()
        return settled

    def wait_closed(self):
        """Retain the caller/console guard until all actual service work exits."""
        self._stop.set()
        with self._condition:
            self._condition.wait_for(lambda: not self._active)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join()
