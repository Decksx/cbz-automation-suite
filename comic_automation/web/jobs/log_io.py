"""Native shared log reads and best-effort cancellation of synchronous I/O.

Readers must allow deletion as well as writes: the writer never renames an
open segment, but retention can unlink a closed segment during a page read.
The CRT's ordinary open() sharing mode does not provide that guarantee.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path

if os.name == "nt":
    import ctypes
    from ctypes import wintypes
    import msvcrt

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _kernel.CreateFileW.restype = wintypes.HANDLE
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel.CloseHandle.restype = wintypes.BOOL
    _kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel.OpenThread.restype = wintypes.HANDLE
    _kernel.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
    _kernel.CancelSynchronousIo.restype = wintypes.BOOL
    _INVALID_HANDLE = ctypes.c_void_p(-1).value


@contextmanager
def shared_read(path: Path):
    """Open a noninheritable read handle with READ/WRITE/DELETE sharing.

    Ownership transfers to the CRT descriptor exactly once. Every failure
    before that transfer closes the native handle; fdopen failures close fd.
    Callers validate private containment before passing the path.
    """
    if os.name != "nt":
        with open(path, "rb", buffering=0) as stream:
            yield stream
        return
    handle = _kernel.CreateFileW(str(path), 0x80000000, 0x1 | 0x2 | 0x4, None, 3, 0x80, None)
    if handle == _INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
    except BaseException:
        _kernel.CloseHandle(handle)
        raise
    try:
        stream = os.fdopen(fd, "rb", buffering=0)
    except BaseException:
        os.close(fd)
        raise
    with stream:
        yield stream


def cancel_thread_io(native_id: int) -> bool:
    """Request cancellation; True is a request receipt, never completion proof.

    No pending operation returns False. Other errors propagate for the runner
    to classify, and a driver may ignore cancellation. Bounded joins and native
    process finalization remain the runner's responsibility.
    """
    if type(native_id) is not int or not 0 < native_id < 2**32:
        raise ValueError("Invalid native thread id")
    if os.name != "nt":
        return False
    handle = _kernel.OpenThread(0x1, False, native_id)  # THREAD_TERMINATE
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if _kernel.CancelSynchronousIo(handle):
            return True
        error = ctypes.get_last_error()
        if error == 1168:  # ERROR_NOT_FOUND, nothing pending
            return False
        raise ctypes.WinError(error)
    finally:
        _kernel.CloseHandle(handle)
