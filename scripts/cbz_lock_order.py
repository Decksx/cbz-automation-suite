"""Ordered locks for the classify -> move -> index transaction (issue #32).

Nothing here is wired into the watcher or the router. This is the primitive
and its contract; introducing it into the critical section is separate work.
See `docs/lock_topology.md` for the survey it was designed against.

Two lock classes, in acquisition order:

    series-operation   one per resolved series identity
    router-index       the WatcherRouter's SeriesIndex guard

    series-operation MAY acquire router-index
    router-index MUST NEVER acquire series-operation

The rule is enforced at acquisition against a thread-local record of what
the calling thread already holds, rather than demonstrated by letting two
threads deadlock. A deadlock test has to decide how long to wait before
declaring one, which makes it a timing test wearing a correctness costume:
it passes on an idle machine and flakes on a loaded one.
`docs/session_protocol.md` records three tests that failed in exactly that
way. This raises immediately, on one thread, naming both the lock it holds
and the one it asked for.

Locks of the *same* class are also ordered against each other, which matters
more than it looks. Two threads each holding one series lock and reaching for
the other is the classic cycle, and it is indistinguishable from correct code
until it hangs. Holding one series lock and requesting a different one is
therefore refused; re-entering the same lock is not.
"""

from __future__ import annotations

import threading

LOCK_SERIES = "series-operation"
LOCK_INDEX = "router-index"

# Lower rank is acquired first. Equal ranks are ordered against each other by
# refusing the second acquisition outright, since there is no total order
# between two series identities that every thread could agree on.
_RANK = {LOCK_SERIES: 1, LOCK_INDEX: 2}

LOCK_CLASSES = tuple(_RANK)


class LockOrderError(RuntimeError):
    """A thread requested a lock that would invert the declared order.

    Raised at the moment of the request, before blocking, so the traceback
    points at the offending acquisition rather than at a thread dump taken
    after everything stopped.
    """


_local = threading.local()


def _stack() -> list[tuple[str, str]]:
    stack = getattr(_local, "stack", None)
    if stack is None:
        stack = []
        _local.stack = stack
    return stack


def held_locks() -> tuple[tuple[str, str], ...]:
    """What the calling thread currently holds, outermost first."""
    return tuple(_stack())


def _describe(entries) -> str:
    return ", ".join(
        f"{cls}({key!r})" if key else cls for cls, key in entries
    ) or "nothing"


class OrderedLock:
    """A lock that refuses to be acquired out of order.

    Wraps an RLock so re-entry by the holding thread stays legal; the order
    check treats re-entering the same lock as a no-op rather than as a
    same-rank violation.
    """

    __slots__ = ("lock_class", "key", "_lock")

    def __init__(self, lock_class: str, key: str = "", *,
                 factory=threading.RLock) -> None:
        if lock_class not in _RANK:
            raise ValueError(
                f"unknown lock class {lock_class!r}; expected one of "
                f"{sorted(_RANK)}"
            )
        self.lock_class = lock_class
        self.key = key
        self._lock = factory()

    @property
    def rank(self) -> int:
        return _RANK[self.lock_class]

    def _check_order(self) -> None:
        stack = _stack()
        if (self.lock_class, self.key) in stack:
            return                      # re-entrant on the same lock
        blocking = [(cls, key) for cls, key in stack if _RANK[cls] >= self.rank]
        if blocking:
            worst = blocking[0]
            raise LockOrderError(
                f"lock order violation on thread "
                f"{threading.current_thread().name!r}: holds "
                f"{_describe(blocking)} and requested "
                f"{_describe([(self.lock_class, self.key)])}. "
                f"{self.lock_class} (rank {self.rank}) may not be acquired "
                f"while holding {worst[0]} (rank {_RANK[worst[0]]})."
            )

    def acquire(self, blocking: bool = True) -> bool:
        """Take the lock, or refuse. Order is checked before blocking.

        Checked first so a violation is reported even when the lock happens
        to be free: an ordering bug that only surfaces under contention is an
        ordering bug that ships.
        """
        self._check_order()
        acquired = self._lock.acquire(blocking)
        if acquired:
            _stack().append((self.lock_class, self.key))
        return acquired

    def release(self) -> None:
        _stack().remove((self.lock_class, self.key))
        self._lock.release()

    def __enter__(self) -> "OrderedLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> bool:
        self.release()
        return False                    # never swallow the exception

    def __repr__(self) -> str:
        return f"OrderedLock({self.lock_class!r}, {self.key!r})"


class SeriesLockRegistry:
    """One `series-operation` lock per resolved series identity.

    Keyed by the *resolved* identity -- what the archives will actually be
    filed under -- and never by source path, staging case id, or destination
    directory. Two arrivals at different paths that resolve to the same series
    are the case this exists for, and keying on anything the arrival happens
    to carry would let both through.

    The registry's own guard is deliberately not an OrderedLock. It is a leaf:
    held only for a dictionary lookup, never while acquiring anything else, so
    it cannot participate in a cycle.
    """

    def __init__(self, *, factory=threading.RLock) -> None:
        self._factory = factory
        self._locks: dict[str, OrderedLock] = {}
        self._guard = threading.Lock()

    def for_series(self, identity: str) -> OrderedLock:
        """The lock for *identity*, created once and reused.

        Returning the same object for the same identity is the whole contract:
        two threads that resolve to one series must contend for one lock, and
        a registry that minted a fresh lock per call would serialize nothing
        while appearing to.
        """
        if not identity:
            raise ValueError("a series lock needs a resolved identity")
        with self._guard:
            lock = self._locks.get(identity)
            if lock is None:
                lock = OrderedLock(LOCK_SERIES, identity, factory=self._factory)
                self._locks[identity] = lock
            return lock

    def __len__(self) -> int:
        with self._guard:
            return len(self._locks)
