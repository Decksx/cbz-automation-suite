"""Opt-in, Windows rename-only holding for stalled incoming archives.

The watcher supplies fresh readiness probes and a generation commit guard
while owning its directory-processing exclusion. This is an observation
policy, not proof that a writer is absent. It never opens an operational DB.
"""

from __future__ import annotations

import json
import logging
import math
import os
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

HOLD_REASONS = frozenset({"incomplete_central_directory"})
DEFAULT_STALL_MINUTES = 30


def _plain(path: Path, *, directory: bool) -> os.stat_result:
    """Require an identified, non-reparse file or directory."""
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or (
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ValueError(f"readiness holding refuses reparse paths: {path}")
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or not info.st_ino:
        raise ValueError(f"readiness holding needs a physical file identity: {path}")
    return info


def _id(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _directory_chain(path: Path) -> list[tuple[int, int]]:
    return [_id(_plain(p, directory=True)) for p in (path, *path.parents)]


def _rename_no_replace(source: Path, destination: Path) -> None:
    """Windows rename refuses existing targets; never fall back to copying."""
    if os.name != "nt":
        raise OSError("automatic readiness holds require Windows no-replace rename")
    os.rename(source, destination)


@dataclass
class _Stall:
    fingerprint: tuple[int, int, int, int]
    since: float
    attempt: Path | None = None
    attempt_identity: tuple[int, int] | None = None
    receipt_identity: tuple[int, int] | None = None
    receipt_ready: bool = False
    receipt_elapsed: float | None = None


class ReadinessHolds:
    """Track per-archive inactivity and hold only allowlisted expired inputs.

    No root means no holding and no filesystem validation/writes. An explicit
    root must already exist, be writable, non-reparse, disjoint from the
    watched tree and on the same volume. Zero minutes also disables holds. Budgets
    are in-memory and start over on restart; events alone never reset them.
    """

    def __init__(
        self, watch_root: str | Path, *, hold_root: str | Path | None = None,
        stall_minutes: float = DEFAULT_STALL_MINUTES,
        watcher_version: str, clock: Callable[[], float] | None = None,
        logger: logging.Logger | None = None,
    ):
        if not math.isfinite(stall_minutes * 60) or stall_minutes < 0:
            raise ValueError("readiness stall minutes must be finite and nonnegative")
        self.watch_root = Path(os.path.abspath(watch_root))
        self.hold_root = Path(os.path.abspath(hold_root)) if hold_root is not None else None
        self.seconds = stall_minutes * 60
        self.enabled = self.hold_root is not None and self.seconds > 0
        self.watcher_version = watcher_version
        self.clock = clock or time.monotonic
        self.log = logger or logging.getLogger(__name__)
        self._stalls: dict[Path, _Stall] = {}
        self._lock = threading.Lock()
        if self.enabled:
            if os.name != "nt":
                raise ValueError("automatic readiness holds require Windows no-replace rename")
            self._roots = self._validate_roots()
            # Probe actual access; os.access alone does not establish writability.
            with tempfile.TemporaryFile(dir=self.hold_root) as stream:
                stream.write(b"readiness-hold validation")
                stream.flush()
                os.fsync(stream.fileno())

    def _validate_roots(self) -> tuple[tuple[int, int], tuple[int, int]]:
        watched = _directory_chain(self.watch_root)
        holding = _directory_chain(self.hold_root)
        if watched[0] in holding or holding[0] in watched:
            raise ValueError("readiness hold root and watched tree must not overlap")
        if watched[0][0] != holding[0][0]:
            raise ValueError("readiness hold root must be on the same volume")
        return watched[0], holding[0]

    def _fingerprint(self, path: Path) -> tuple[int, int, int, int]:
        path.relative_to(self.watch_root)
        _directory_chain(path.parent)
        info = _plain(path, directory=False)
        if info.st_dev != self._roots[0][0]:
            raise ValueError("readiness hold source is on a different volume")
        return (*_id(info), info.st_size, info.st_mtime_ns)

    def forget(self, scope: Path) -> None:
        """Drop budgets when a directory is gone, empty or fully ready."""
        with self._lock:
            for path in tuple(self._stalls):
                if path.is_relative_to(scope):
                    del self._stalls[path]

    def _attempt(self, path: Path, state: _Stall, reason: str, elapsed: float) -> Path:
        if state.attempt is None:
            # Retain the directory before any fallible receipt work, so a
            # persistent write error cannot allocate a directory every retry.
            state.attempt = Path(tempfile.mkdtemp(prefix="readiness-", dir=self.hold_root))
            state.attempt_identity = _id(_plain(state.attempt, directory=True))
            state.receipt_elapsed = elapsed
        # Reuse failed attempts, but never adopt a replaced/uncertain directory.
        if (_id(_plain(state.attempt, directory=True)) != state.attempt_identity
                or _directory_chain(state.attempt.parent)[0] != self._roots[1]):
            raise ValueError("readiness hold attempt directory changed")
        destination = state.attempt / path.name
        receipt_path = state.attempt / (path.name + ".held.json")
        if not state.receipt_ready:
            receipt = {
                "contract": "cbz-readiness-hold-v1", "source_path": str(path),
                "destination_path": str(destination), "reason": reason,
                "observed_elapsed_seconds": state.receipt_elapsed, "watcher_version": self.watcher_version,
                "observed_fingerprint": list(state.fingerprint),
                # This receipt survives a crash or a failed rename honestly.
                "written_before_rename": True,
            }
            # First create exclusively. Retry only the same file we created;
            # r+ opens without truncation until its handle identity is checked.
            mode = "x" if state.receipt_identity is None else "r+"
            with receipt_path.open(mode, encoding="utf-8", newline="\n") as stream:
                info = os.fstat(stream.fileno())
                if (not info.st_ino or not stat.S_ISREG(info.st_mode)
                        or (state.receipt_identity is not None and _id(info) != state.receipt_identity)):
                    raise ValueError("readiness hold receipt identity changed or unavailable")
                state.receipt_identity = _id(info)
                stream.seek(0)
                json.dump(receipt, stream, indent=2, ensure_ascii=False, allow_nan=False)
                stream.write("\n")
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
            state.receipt_ready = True
        if _id(_plain(receipt_path, directory=False)) != state.receipt_identity:
            raise ValueError("readiness hold receipt identity changed")
        return destination

    def consider(
        self, scope: Path, failures: Iterable[tuple[Path, str]], *,
        probe: Callable[[Path], str | None],
        commit_guard: Callable[[], AbstractContextManager[bool]],
    ) -> bool:
        """Re-probe expired failures under caller-owned processing exclusion.

        ``probe`` returns None for READY, otherwise its reason. The supplied
        commit guard holds the watcher generation lock through revalidation,
        receipt creation and rename. Return true only if a file was moved;
        the caller must then rescan instead of using its old directory probe.
        """
        if not self.enabled:
            return False
        with self._lock:
            failures = dict(failures)
            for path in tuple(self._stalls):
                if path.is_relative_to(scope) and path not in failures:
                    del self._stalls[path]
            moved = False
            for path, reason in failures.items():
                now = self.clock()
                try:
                    fingerprint = self._fingerprint(path)
                    state = self._stalls.get(path)
                    if state is None or state.fingerprint != fingerprint:
                        state = self._stalls[path] = _Stall(fingerprint, now)
                    elapsed = now - state.since
                    if elapsed < self.seconds:
                        continue
                    if reason not in HOLD_REASONS:
                        self.log.warning("Readiness expired but blocked: %s elapsed=%.1fs reason=%s", path, elapsed, reason)
                        continue
                    fresh_reason = probe(path)
                    if fresh_reason != reason or fresh_reason not in HOLD_REASONS:
                        self.log.info("Readiness hold deferred after fresh probe: %s reason=%s", path, fresh_reason)
                        continue
                    with commit_guard() as current:
                        if not current:
                            continue
                        if self._validate_roots() != self._roots:
                            raise ValueError("readiness hold root identity changed")
                        if self._fingerprint(path) != state.fingerprint:
                            continue
                        destination = self._attempt(path, state, reason, elapsed)
                        # Receipt I/O may block; recheck source and roots after it.
                        if self._validate_roots() != self._roots:
                            raise ValueError("readiness hold root identity changed")
                        if self._fingerprint(path) != state.fingerprint:
                            continue
                        _rename_no_replace(path, destination)
                    self._stalls.pop(path, None)
                    moved = True
                    self.log.warning("Readiness held: %s -> %s elapsed=%.1fs reason=%s", path, destination, elapsed, reason)
                except FileNotFoundError:
                    self._stalls.pop(path, None)
                    self.log.info("Readiness hold source or target disappeared: %s", path)
                except (OSError, ValueError) as exc:
                    self.log.warning("Readiness hold blocked: %s reason=%s error=%s", path, reason, exc)
            return moved
