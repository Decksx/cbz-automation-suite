"""Windows watcher exclusion shared by every launcher and checkout.

The kernel mutex names a physical watch directory, never its access spelling.
There is deliberately no local-session or file-lock fallback: mixing backends
would let two launchers each believe they exclusively own the same intake.
"""

from __future__ import annotations

import ctypes
import os
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

_WAIT_OBJECT_0 = 0
_WAIT_ABANDONED = 0x80
_WAIT_TIMEOUT = 0x102
_owners_lock = threading.Lock()
_owned_names: set[str] = set()


class WatcherInstanceRefused(RuntimeError):
    """Exclusive ownership of the watch root could not be established."""


@dataclass(frozen=True)
class WatcherInstance:
    """Evidence for the mutex held by the current watcher thread."""

    supplied_root: Path
    resolved_root: Path
    identity: tuple[int, int]
    mutex_name: str
    abandoned: bool


def _directory_identity(path: Path) -> tuple[int, int]:
    """Refuse missing, non-directory or uncertain filesystem identities."""
    info = path.stat()
    if (not stat.S_ISDIR(info.st_mode)
            or info.st_dev <= 0 or info.st_ino <= 0):
        raise WatcherInstanceRefused(f"Unusable watch-root identity: {path}")
    return info.st_dev, info.st_ino


def _kernel32():
    """Bind pointer-sized Win32 handles without loading DLLs on import."""
    if os.name != "nt":
        raise WatcherInstanceRefused("Watcher instance locking requires Windows")
    from ctypes import wintypes

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    api.CreateMutexW.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    api.ReleaseMutex.restype = wintypes.BOOL
    api.CloseHandle.argtypes = (wintypes.HANDLE,)
    api.CloseHandle.restype = wintypes.BOOL
    return api


@contextmanager
def watcher_instance_lock(root: str | Path) -> Iterator[WatcherInstance]:
    """Own the physical root's Global mutex until this context exits.

    Acquire and release must run on the same thread. Existing owners, access
    failures and unprovable identity all refuse; an abandoned mutex grants
    ownership without claiming that the previous import completed. Nothing is
    read or written inside the watch directory by this helper.
    """
    try:
        supplied = Path(os.path.abspath(root))
        resolved = supplied.resolve(strict=True)
        identity = _directory_identity(supplied)
        if _directory_identity(resolved) != identity:
            raise WatcherInstanceRefused(f"Watch-root identity changed: {supplied}")
    except (OSError, RuntimeError) as exc:
        raise WatcherInstanceRefused(f"Cannot identify watch root {root}: {exc}") from exc

    name = f"Global\\CBZWatcher-{identity[0]:x}-{identity[1]:x}"
    api = _kernel32()
    handle = None
    acquired = False
    registered = False
    try:
        # Win32 mutexes are recursive for their owning thread. Reject a second
        # embedded watcher in that same thread as well as other processes.
        with _owners_lock:
            if name in _owned_names:
                raise WatcherInstanceRefused(f"Watcher already running for {supplied} ({name})")
            handle = api.CreateMutexW(None, False, name)
            if not handle:
                error = ctypes.get_last_error()
                raise WatcherInstanceRefused(
                    f"Cannot create/open watcher mutex {name}: WinError {error}; "
                    "ownership is unverified, no fallback is allowed"
                )
            outcome = api.WaitForSingleObject(handle, 0)
            if outcome not in (_WAIT_OBJECT_0, _WAIT_ABANDONED):
                if outcome == _WAIT_TIMEOUT:
                    raise WatcherInstanceRefused(f"Watcher already running for {supplied} ({name})")
                error = ctypes.get_last_error()
                raise WatcherInstanceRefused(
                    f"Cannot acquire watcher mutex {name}: wait={outcome:#x}, WinError {error}"
                )
            acquired = True
            _owned_names.add(name)
            registered = True

        # Catch a junction/identity change across acquisition before any caller
        # performs recovery, discovery or starts an observer under the wrong key.
        try:
            if (_directory_identity(supplied) != identity
                    or _directory_identity(resolved) != identity):
                raise WatcherInstanceRefused(f"Watch-root identity changed: {supplied}")
        except OSError as exc:
            raise WatcherInstanceRefused(f"Cannot recheck watch root {supplied}: {exc}") from exc
        yield WatcherInstance(supplied, resolved, identity, name, outcome == _WAIT_ABANDONED)
    finally:
        with _owners_lock:
            try:
                if acquired and not api.ReleaseMutex(handle):
                    error = ctypes.get_last_error()
                    raise WatcherInstanceRefused(f"Cannot release watcher mutex {name}: WinError {error}")
            finally:
                if handle:
                    api.CloseHandle(handle)
                if registered:
                    _owned_names.discard(name)
