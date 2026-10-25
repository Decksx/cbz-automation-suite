"""The OS-held scope lock shared by every canonical-naming mutator (§5.4).

One stable path per library root, ``<library-root>\\.cbz-canonical-naming.lock``,
is locked by every `apply`, `recover` and `undo` on that root for the whole
of its run.

* **Non-blocking.** The lock is an exclusive byte-range lock on byte 0 of
  the lock file, taken with ``msvcrt.locking(..., LK_NBLCK, 1)`` (which is
  ``LockFileEx`` underneath). A process that cannot take it gets
  `ScopeLockHeld` at once; nothing here ever waits for the holder.
* **Released by the OS.** Windows drops a byte-range lock when the handle
  that owns it is closed, which includes the process exiting or being
  killed. So there is no stale-lock takeover, no liveness check and no
  unlink: nothing in this module, or anywhere else, removes the lock file.
  Removing it would be actively harmful, since a successor that had just
  locked a fresh file at the same path would then share "the" lock with a
  process still holding the old, unlinked one.
* **Owner metadata is informational.** After the lock is held, the attempt
  id, pid and start time are written to a separate owner file,
  ``.cbz-canonical-naming.owner.json``. It is never read for exclusion:
  the OS lock is the only exclusion, and a stale owner file left by a
  crashed holder is simply overwritten by the next holder.

Windows only. The design (and `os.rename`'s refusal of an occupied
destination, which the step protocol relies on) is Windows-specific, so
taking the lock anywhere else raises `UnsupportedPlatformError` rather than
silently falling back to an advisory lock with different semantics.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOCK_FILENAME = ".cbz-canonical-naming.lock"
OWNER_FILENAME = ".cbz-canonical-naming.owner.json"


class ScopeLockError(RuntimeError):
    """Base class for scope-lock refusals."""


class ScopeLockHeld(ScopeLockError):
    """Another handle (this process or another) holds the scope lock."""


class UnsupportedPlatformError(ScopeLockError):
    """The canonical-naming apply path runs on Windows only."""


def require_windows() -> None:
    """Refuse on any platform but Windows.

    The step protocol depends on two Windows behaviours: `os.rename`
    refusing an existing destination, and byte-range locks released by the
    OS when the owning handle closes. POSIX ``rename`` silently replaces
    its destination, so running there would break the execution-time
    occupancy check without any error.
    """
    if sys.platform != "win32":
        raise UnsupportedPlatformError(
            "canonical-naming apply/recover/undo run on Windows only: POSIX "
            "rename replaces an existing destination, which would defeat "
            "the execution-time occupancy check (§5.4 step 2)."
        )


def lock_path(library_root: str | Path) -> Path:
    """The one stable lock path for ``library_root``."""
    return Path(library_root) / LOCK_FILENAME


def owner_path(library_root: str | Path) -> Path:
    """The informational owner file beside the lock."""
    return Path(library_root) / OWNER_FILENAME


@dataclass
class ScopeLock:
    """An exclusive, non-blocking, OS-held lock on one library root.

    Use as a context manager, or call `acquire` / `release`. ``attempt_id``
    and ``role`` only go into the owner file.
    """

    library_root: Path
    attempt_id: str
    role: str
    _fd: int | None = None

    @property
    def path(self) -> Path:
        return lock_path(self.library_root)

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> "ScopeLock":
        """Take the lock or raise `ScopeLockHeld`; never waits."""
        require_windows()
        import msvcrt

        if self._fd is not None:
            raise ScopeLockError("this ScopeLock already holds the lock")
        if not Path(self.library_root).is_dir():
            raise ScopeLockError(
                f"library root is not a directory: {self.library_root}"
            )
        # O_CREAT without O_TRUNC or O_EXCL: the file is created once and
        # then reused for ever, by every holder. Its content is irrelevant;
        # only the byte-range lock on byte 0 matters.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            os.close(fd)
            raise ScopeLockHeld(
                f"the canonical-naming scope lock {self.path} is held by "
                "another attempt, recovery or undo; refusing rather than "
                f"waiting ({exc})"
            ) from exc
        self._fd = fd
        # Written only now that the lock is ours, so an owner file can never
        # name a process that did not hold the lock when it was written. If
        # writing it fails, the lock is released before the error escapes:
        # a raising ``__enter__`` never reaches ``__exit__``, so otherwise
        # the handle would hold the root locked until the process exits.
        try:
            self._write_owner()
        except BaseException:
            self.release()
            raise
        return self

    def _write_owner(self) -> None:
        record: dict[str, Any] = {
            "attempt_id": self.attempt_id,
            "role": self.role,
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "note": "informational only; the OS byte-range lock on "
            f"{LOCK_FILENAME} is the sole exclusion",
        }
        with open(owner_path(self.library_root), "w", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())

    def release(self) -> None:
        """Unlock and close the handle. The lock file is left in place."""
        if self._fd is None:
            return
        import msvcrt

        fd, self._fd = self._fd, None
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            # Closing the handle releases the lock even if the explicit
            # unlock failed; never unlink the file (see the module docstring).
            os.close(fd)

    def __enter__(self) -> "ScopeLock":
        return self.acquire()

    def __exit__(self, *exc_info: object) -> None:
        self.release()
