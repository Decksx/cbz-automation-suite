"""Claimed single-tool runner with responsive native supervision.

One dedicated host, one native birth, no unrelated process launches. All
post-birth ledger/receipt I/O runs in at most one worker, outside the polling
loop. An unsettled worker or log thread retains this object, claim and native
handles. There is deliberately no destructor/context-manager kill shortcut.
M3d owns detached spawning; M4's production registry remains unavailable.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import math
import threading
import time

from .. import state_dir
from . import receipts, win32
from .launch_spec import reconstruct
from .log_service import JobLogs
from .logs import LogPump
from .store import JobRefused, JobStore


@dataclass(frozen=True)
class Limits:
    """Internal supervision budgets; persistence is a slow-I/O notice threshold.

    Fixtures may shorten deadlines, never extend them. A persistence delay alone
    does not invalidate proof or cancel work; actual failures remain uncertain.
    """
    poll: float = .25
    descendants: float = 10
    termination: float = 30
    drain: float = 5
    cleanup: float = 5
    persistence: float = 5

    def __post_init__(self):
        for name, maximum in (("poll", .25), ("descendants", 10), ("termination", 30),
                              ("drain", 5), ("cleanup", 5), ("persistence", 5)):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= maximum:
                raise ValueError("Invalid runner budget: " + name)


class _IO:
    def __init__(self, kind, operation, now):
        self.kind, self.started = kind, now
        self.done = threading.Event()
        self.value = self.error = None
        def invoke():
            try:
                self.value = operation()
            except BaseException as exc:
                self.error = exc
            finally:
                self.done.set()
        self.thread = threading.Thread(target=invoke, name="cbz-runner-io", daemon=True)
        self.thread.start()


class Runner:
    """Live claimed ownership; call tick repeatedly or run in a dedicated host.

    registry is a trusted fixture/internal adapter, never a CLI/request option.
    A local uncertainty is sticky even if storage/logging later recovers.
    done means owned native/log/I/O activity settled and claim cleanup finished;
    it does not mean succeeded. row/phase/reason retain the actual outcome.
    """
    def __init__(self, store, claim, *, limits=Limits(), clock=time.monotonic):
        self.store, self.claim, self.limits, self.clock = store, claim, limits, clock
        self.process = self.pump = self.spec = self.io = None
        self.row = None
        self.version, self.phase, self.reason, self.done = 2, "claimed", None, False
        self.root_exit = self.zero_at = self.cleanup_at = self.terminate_at = None
        self.running_saved = self.uncertain_saved = self.published = self.terminal = False
        self.cancelled = False
        self.next_refresh = 0
        self.hold_after = 0

    @classmethod
    def start(cls, store, job_id, nonce, *, registry=None, limits=Limits(), clock=time.monotonic):
        """Claim exactly once, prepare logs and revalidate before one native birth."""
        claim = store.claim(job_id, nonce, win32.current_identity())
        owner = cls(store, claim, limits=limits, clock=clock)
        attempted = False
        try:
            command = store.read_command(claim, owner.version)
            owner.spec = reconstruct(command, registry)
            disk = JobLogs(store, job_id).create_writer(claim)
            # Repeat both ledger binding and registry/file checks after log setup.
            command = store.read_command(claim, owner.version)
            if store.get(job_id)["stop_requested_ms"] is not None:
                raise JobRefused("STOP_REQUESTED_BEFORE_BIRTH")
            owner.spec = reconstruct(command, registry)
            attempted = True
            spec = owner.spec
            owner.process = win32.launch(job_id, spec.executable, spec.arguments,
                cwd=spec.cwd, environment=dict(spec.environment))
            owner.pump = LogPump(owner.process.stdout, disk, sticky_failure=True)
            owner.pump.start()
            owner.phase = "running"
            owner._dispatch("running", lambda: store.mark_running(claim, 2, owner.process.identity))
        except Exception as exc:
            # Only a native adapter's explicit no-birth result proves refusal
            # once native launch was attempted. Collisions remain uncertain.
            no_birth = not attempted or (isinstance(exc, win32.NativeLaunchError)
                and not exc.child_created and not isinstance(exc, win32.JobCollision))
            if owner.process is None and no_birth:
                try:
                    owner.row = store.refuse_before_create(claim, 2, reason=type(exc).__name__ + ":" + str(exc))
                    owner.terminal = True
                    owner.phase = owner.row["state"]
                except Exception:
                    owner._uncertain("PREBIRTH_PERSIST_FAILED")
            else:
                owner._uncertain("LAUNCH_OR_START_FAILED:" + type(exc).__name__)
        return owner

    def _dispatch(self, kind, operation):
        assert self.io is None
        self.io = _IO(kind, operation, self.clock())

    def _uncertain(self, reason):
        if self.reason is None:
            self.reason = reason
        self.phase = "uncertain"

    def _hold(self):
        # A failed DB operation might have committed before reporting failure.
        # Read the current version rather than guessing which generation won.
        row = self.store.get(self.claim.job_id)
        if (row["nonce"] != self.claim.nonce or row["runner_pid"] != self.claim.runner.pid
                or row["runner_created_at"] != str(self.claim.runner.created_at)
                or row["runner_session_id"] != self.claim.runner.session_id):
            raise JobRefused("RUNNER_OWNER_CHANGED")
        if row["state"] == "uncertain":
            return row
        return self.store.hold_uncertain(self.claim.job_id, self.claim.nonce, row["state_version"],
            actor=f"runner:{self.claim.runner.pid}:{self.claim.runner.created_at}", reason=self.reason)

    def _collect(self, now):
        op = self.io
        if op is None:
            return
        if not op.done.is_set() or op.thread.is_alive():
            # An in-flight terminal commit has already passed final native/log
            # proof. Retain ownership until its result is known; never race it
            # with a second mutation or declare that it has been cancelled.
            if (now - op.started >= self.limits.persistence and self.reason is None
                    and op.kind not in ("finalize", "release")):
                # Latency is not failed persistence. Keep supervising and holding
                # admission until the actual result arrives, then require fresh
                # final proof. A recovered worker never clears real uncertainty.
                self.phase = "waiting_for_" + op.kind
            return
        self.io = None
        if op.error is not None:
            self._uncertain("PERSISTENCE_FAILED:" + op.kind + ":" + type(op.error).__name__)
            self.next_refresh = now + self.limits.poll
            self.hold_after = now + self.limits.poll
            return
        if op.kind == "publish":
            self.published = True
        elif op.kind == "release":
            self.done = True
        else:
            self.row = op.value
            self.version = self.row["state_version"]
            if op.kind == "running":
                self.running_saved = True
            elif op.kind == "hold":
                self.uncertain_saved = True
            elif op.kind == "finalize":
                self.terminal = True
                self.phase = self.row["state"]
            elif op.kind == "refresh":
                if self.row["state"] != "running" or self.row["stop_requested_ms"] is not None:
                    # A request is not an accepted interruption gate. Wait policy
                    # never terminates a live writing root, including on log loss.
                    self._uncertain("STOP_OR_STATE_RECHECK_REQUIRED")
            if op.kind in ("running", "refresh") and self.reason is None:
                self.phase = "running"
            self.next_refresh = now + self.limits.poll

    def _observe(self, now):
        if self.process is None:
            return True
        try:
            code = self.process.wait_root(0)
            if code is not None and self.root_exit is None:
                self.root_exit = now
            active = self.process.active_processes()
        except Exception as exc:
            self._uncertain("NATIVE_OBSERVATION_FAILED:" + type(exc).__name__)
            active = None
        if self.root_exit is None:
            return False
        if active != 0:
            if now - self.root_exit >= self.limits.descendants and self.terminate_at is None:
                self.terminate_at = now
                try:
                    self.process.terminate()
                except Exception as exc:
                    self._uncertain("TERMINATION_FAILED:" + type(exc).__name__)
            if self.terminate_at is not None and now - self.terminate_at >= self.limits.termination:
                self._uncertain("DESCENDANTS_NOT_PROVEN_ZERO")
            return False
        if self.zero_at is None:
            self.zero_at = now
        pump = self.pump
        if pump is None:
            self._uncertain("LOG_PUMP_NOT_STARTED")
            return True
        reader = pump.reader.is_alive()
        writer = pump.writer.is_alive()
        if reader and now - self.zero_at < self.limits.drain:
            return False
        if self.cleanup_at is None:
            self.cleanup_at = now
        if reader and not self.cancelled or (writer and now - self.cleanup_at >= self.limits.cleanup and not self.cancelled):
            self._uncertain("LOG_DRAIN_OR_CLEANUP_TIMEOUT")
            self.cancelled = True
            pump.join(0)  # advisory cancellation only, after verified job zero
        if reader or writer:
            return False
        if (pump.error is not None or not pump.queue.complete or not pump.disk.snapshot["complete"]):
            self._uncertain("LOG_UNKNOWN_TAIL")
        return True

    def tick(self):
        """Perform one nonblocking native tick; retain all unsettled ownership.

        No storage call, blocking thread join or pipe read runs inline. Repeated
        calls continue native supervision even if a worker stalls indefinitely.
        """
        if self.done:
            return
        now = self.clock()
        self._collect(now)
        if self.done:
            return
        if self.io is not None and self.io.kind in ("finalize", "release"):
            # These operations start only after native/log settlement. Finalize
            # repeats fresh proof itself; racing it with another observation/
            # mutation cannot safely cancel an already in-flight DB commit.
            return
        settled = self.terminal or self._observe(now)
        if not self.terminal and self.pump is not None and self.pump.error is not None:
            self._uncertain("LOG_UNKNOWN_TAIL:" + self.pump.error)
        if self.io is not None:
            return
        if self.reason is not None and not self.uncertain_saved and now >= self.hold_after:
            self._dispatch("hold", self._hold)
        elif settled and (self.terminal or self.uncertain_saved):
            def release():
                if self.process is not None:
                    self.process.close()
                self.claim.close()
            self._dispatch("release", release)
        elif settled and self.running_saved and self.reason is None:
            if not self.published:
                self.phase = "publishing"
                self._dispatch("publish", lambda: receipts.publish(self.store, self.claim, self.version,
                    self.process, self.pump, outcome_mapper=self.spec.outcome_mapper))
            else:
                self.phase = "finalizing"
                self._dispatch("finalize", lambda: self.store.finalize(self.claim, self.version,
                    self.process, self.pump, outcome_mapper=self.spec.outcome_mapper))
        elif self.running_saved and self.reason is None and now >= self.next_refresh:
            self._dispatch("refresh", lambda: self.store.get(self.claim.job_id))

    def run(self):
        """Keep the dedicated runner alive until every owned resource settles."""
        while not self.done:
            try:
                self.tick()
                time.sleep(self.limits.poll)
            except KeyboardInterrupt:
                self._uncertain("INTERRUPTION_REQUIRES_POLICY")
        return self.row


def main(argv=None):
    """Accept only job ID/nonce; inspect the existing pinned home, never init it.

    No production registry exists yet: an otherwise valid claim refuses before
    native birth. M4 supplies that adapter in code, never via argv/import path.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id")
    parser.add_argument("nonce")
    args = parser.parse_args(argv)
    path = state_dir.state_dir_from_env()
    st = path.stat()
    state = state_dir.inspect_state_dir(path, expected_identity=(st.st_dev, st.st_ino))
    runner = Runner.start(JobStore(state), args.job_id, args.nonce)
    row = runner.run()
    return 0 if row and row["state"] == "succeeded" else 2


if __name__ == "__main__":
    raise SystemExit(main())
