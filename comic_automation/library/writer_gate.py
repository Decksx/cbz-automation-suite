"""Library writer gate and registered coordination roots (#158 B3, slice 1).

Design: ``docs/library_writer_coordination.md``. This slice supplies the
primitive and the root resolver only; no writer uses them yet, so nothing
here changes how the watcher, Camelia or an apply behaves.

One zero-byte file per registered root, ``<root>\\.cbz-library-writers.lock``,
carries Windows byte-range locks (``LockFileEx``):

* **byte 0, the root gate**: shared for an ordinary write transaction,
  exclusive for a maintenance run;
* **byte 1 + (h mod 2**62)**, one per series key, exclusive for an
  ordinary transaction. A hash collision only causes false contention.

Every request fails immediately rather than waiting, the gate is taken
before the series byte, and a thread may not take the gate twice for one
root (byte-range locks are not reentrant). The OS drops a lock when its
handle closes, including on kill, so nothing ever removes the lock file and
the owner file is informational only: it never proves quiescence.

Acquiring the exclusive gate proves only that no *participating* writer is
mid-transaction. Callers keep today's operator attestation until a root is
marked ``enforced`` (design, "rollout").
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import stat
import threading
import unicodedata

PROTOCOL_VERSION = 1
REGISTRY_CONTRACT = "library-writer-roots-v1"
GATE_LOCK = ".cbz-library-writers.lock"
GATE_OWNER = ".cbz-library-writers.owner.json"
CANONICAL_LOCK = ".cbz-canonical-naming.lock"
CANONICAL_OWNER = ".cbz-canonical-naming.owner.json"
# Every control file a sync or inventory must never carry or count.
CONTROL_FILENAMES = frozenset({GATE_LOCK, GATE_OWNER, CANONICAL_LOCK, CANONICAL_OWNER})
# Casefolded paths cannot contain NUL, so no real series can produce this key.
ROOT_KEY = "\x00root"

_SHARED, _EXCLUSIVE, _FAIL_NOW = 0x0, 0x2, 0x1
_held = threading.local()
log = logging.getLogger(__name__)


class GateError(RuntimeError):
    """Base class for writer-gate refusals."""


class GateBusy(GateError):
    """Another writer holds a conflicting lock; defer (ordinary) or retry (maintenance)."""


class GateRefused(GateError):
    """The request is invalid: unregistered or ambiguous root, reentry, platform."""


@dataclass(frozen=True)
class CoordinationRoot:
    """One registered library root: every writer under it shares its gate."""

    path: Path
    series_depth: int
    enforced: bool
    identity: tuple[int, int]


def _identity(path: Path) -> tuple[int, int]:
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_dev <= 0 or info.st_ino <= 0:
        raise GateRefused(f"unusable directory identity: {path}")
    return info.st_dev, info.st_ino


def load_registry(path: str | Path) -> tuple[CoordinationRoot, ...]:
    """Read and validate the registry; refuse overlapping or aliased roots.

    Overlap is judged by physical directory identity of every ancestor, so a
    drive letter, UNC spelling or junction naming the same tree is caught.
    """
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GateRefused(f"writer-root registry is unreadable: {exc}") from exc
    if (not isinstance(document, dict) or set(document) != {"contract", "roots"}
            or document["contract"] != REGISTRY_CONTRACT or not isinstance(document["roots"], list)
            or not document["roots"]):
        raise GateRefused(f"expected {REGISTRY_CONTRACT} with a non-empty roots array")
    roots = []
    for item in document["roots"]:
        if (not isinstance(item, dict) or set(item) != {"path", "series_depth", "enforced"}
                or not isinstance(item["path"], str) or type(item["series_depth"]) is not int
                or item["series_depth"] < 1 or type(item["enforced"]) is not bool):
            raise GateRefused("each root needs path, a positive series_depth and boolean enforced")
        root = Path(item["path"])
        if not root.is_absolute():
            raise GateRefused(f"registered root must be absolute: {root}")
        try:
            identity = _identity(root)
        except OSError as exc:
            raise GateRefused(f"registered root is unavailable: {root}: {exc}") from exc
        roots.append(CoordinationRoot(root, item["series_depth"], item["enforced"], identity))
    for root in roots:
        lineage = _lineage(root.path)
        for other in roots:
            if other is not root and other.identity in {identity for identity, _ in lineage}:
                raise GateRefused(f"registered roots overlap or alias: {other.path} and {root.path}")
    return tuple(roots)


def _lineage(path: Path) -> list[tuple[tuple[int, int], tuple[str, ...]]]:
    """(identity, components below it) for ``path``'s existing ancestors, nearest first.

    Missing tail components (a destination not created yet) are carried as
    relative parts of the nearest existing ancestor.
    """
    path = Path(os.path.abspath(path))
    tail: list[str] = []
    current = path
    while not current.exists():
        if current.parent == current:
            raise GateRefused(f"no existing ancestor for {path}")
        tail.insert(0, current.name)
        current = current.parent
    lineage = []
    below = list(tail)
    while True:
        info = current.stat()
        if stat.S_ISDIR(info.st_mode) and info.st_dev > 0 and info.st_ino > 0:
            lineage.append(((info.st_dev, info.st_ino), tuple(below)))
        if current.parent == current:
            return lineage
        below.insert(0, current.name)
        current = current.parent


def coordination_root(registry: tuple[CoordinationRoot, ...], path: str | Path) -> tuple[CoordinationRoot, tuple[str, ...]]:
    """Map ``path`` to the one registered root physically containing it.

    Returns the root and ``path``'s components below it. A caller rooted
    inside a registered root (an apply at a series folder) resolves to the
    enclosing root; one above or outside every root is refused.
    """
    by_identity = {root.identity: root for root in registry}
    for identity, below in _lineage(Path(path)):
        if identity in by_identity:
            return by_identity[identity], below
    raise GateRefused(f"path is under no registered library root: {path}")


def series_key(root: CoordinationRoot, below: tuple[str, ...], *, folder: bool) -> str:
    """Directory components only: a file drops its name, a folder landing keeps itself.

    ``below`` is the destination relative to ``root`` (from `coordination_root`).
    Fewer directory components than ``series_depth`` gives the reserved root key.
    """
    directories = list(below) if folder else list(below[:-1])
    if len(directories) < root.series_depth:
        return ROOT_KEY
    return "/".join(unicodedata.normalize("NFC", part).casefold() for part in directories[:root.series_depth])


def _offset(key: str) -> int:
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return 1 + (int.from_bytes(digest[:8], "big") % (1 << 62))


def _kernel32():
    if os.name != "nt":
        raise GateRefused("the writer gate requires Windows")
    import ctypes
    from ctypes import wintypes

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.LockFileEx.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                               wintypes.DWORD, ctypes.POINTER(OVERLAPPED))
    api.LockFileEx.restype = wintypes.BOOL
    api.UnlockFileEx.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.POINTER(OVERLAPPED))
    api.UnlockFileEx.restype = wintypes.BOOL
    return api, OVERLAPPED, ctypes


class _GateFile:
    """One handle on the root's lock file; every lock it takes dies with it."""

    def __init__(self, root: CoordinationRoot):
        self._api, self._overlapped, self._ctypes = _kernel32()
        if _identity(root.path) != root.identity:
            raise GateRefused(f"registered root changed identity: {root.path}")
        self._fd = os.open(root.path / GATE_LOCK, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
        import msvcrt
        self._handle = msvcrt.get_osfhandle(self._fd)
        self._held: list[int] = []

    def _range(self, offset: int):
        return self._overlapped(Offset=offset & 0xFFFFFFFF, OffsetHigh=offset >> 32)

    def lock(self, offset: int, exclusive: bool, busy: str) -> None:
        flags = _FAIL_NOW | (_EXCLUSIVE if exclusive else _SHARED)
        if not self._api.LockFileEx(self._handle, flags, 0, 1, 0, self._ctypes.byref(self._range(offset))):
            raise GateBusy(busy)
        self._held.append(offset)

    def close(self) -> None:
        # Unlock in reverse; closing the handle would release them anyway.
        try:
            for offset in reversed(self._held):
                self._api.UnlockFileEx(self._handle, 0, 1, 0, self._ctypes.byref(self._range(offset)))
        finally:
            self._held.clear()
            os.close(self._fd)


@contextmanager
def _once_per_thread(root: CoordinationRoot):
    held = getattr(_held, "roots", None)
    if held is None:
        held = _held.roots = set()
    if root.identity in held:
        raise GateRefused(f"this thread already holds the writer gate for {root.path}")
    held.add(root.identity)
    try:
        yield
    finally:
        held.discard(root.identity)


@contextmanager
def ordinary_unit(root: CoordinationRoot, key: str):
    """Hold the shared gate plus this series' byte for one write transaction.

    Raises `GateBusy` without waiting when maintenance holds the gate or
    another transaction holds this series; the caller defers its unit.
    """
    with _once_per_thread(root):
        gate = _GateFile(root)
        try:
            gate.lock(0, False, f"maintenance in progress on {root.path}")
            gate.lock(_offset(key), True, f"series busy under {root.path}")
            yield
        finally:
            gate.close()


@contextmanager
def maintenance_run(root: CoordinationRoot, owner: dict):
    """Hold the exclusive gate for a whole maintenance run.

    Raises `GateBusy` without waiting while any ordinary transaction or
    other maintenance run holds the gate. The owner record is written after
    the lock is held, for diagnostics only.
    """
    with _once_per_thread(root):
        gate = _GateFile(root)
        try:
            gate.lock(0, True, f"writers active on {root.path}; retry")
            record = dict(owner, pid=os.getpid(), protocol_version=PROTOCOL_VERSION,
                          started_at=datetime.now(timezone.utc).isoformat())
            try:
                (root.path / GATE_OWNER).write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
            except OSError as exc:  # informational: never fail the run over it
                log.warning("writer gate: owner record not written: %s", exc)
            yield
        finally:
            gate.close()
