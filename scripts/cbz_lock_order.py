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
from contextlib import contextmanager
from dataclasses import dataclass

from scripts.cbz_routing import series_key

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


def lock_key(series_name: str) -> str:
    """The normalized identity two arrivals must share to contend for one lock.

    Built on `cbz_routing.series_key`, the identity routing itself files
    under, rather than on the display directory name -- the directory name is
    not guaranteed unique across the operation domain, and two arrivals whose
    folders differ only in punctuation resolve to one series.

    `series_key` lowercases, strips `uncensored`/`decensored` markers,
    replaces every non-word non-space character with a space, and collapses
    runs of whitespace. So these already agree:

        case            "BERSERK"        == "Berserk"
        punctuation     "Berserk!!"      == "Berserk"
        hyphens         "Attack-on-Titan" == "Attack on Titan"
        markers         "X (Uncensored)" == "X"
        outer space     "Berserk "       == "Berserk"

    Unicode composition used to be applied *here*, wrapping `series_key` in
    NFC on both sides, because `series_key` applied none and the NFD form of
    a title with a combining diaeresis keyed to `ka ntai` while its NFC form
    keyed to `kantai`-with-umlaut. **#44 moved NFC to the front of
    `series_key` itself**, so this function no longer adds any.

    Both halves of the old wrapper are gone deliberately, and neither removal
    rests on a measurement:

        inner   `series_key` normalizes its own input before touching it
        outer   `series_key` guarantees its *output* is NFC, as an explicit
                postcondition rather than as a side effect of the transforms
                in between

    The second point is the one that matters here. An earlier draft of this
    change removed the outer call on the strength of an exhaustive scan --
    14,800,248 probes, zero non-NFC outputs -- which is strong evidence about
    today's Unicode database and today's `str.lower()`, and no guarantee at
    all about tomorrow's. `.lower()` can emit combining sequences, so a
    revision to a single case mapping could have silently broken every caller
    that trusted this key. Making `series_key` close its own contract removes
    that exposure; the scan survives as corroboration in the PR record and in
    `docs/lock_topology.md`, and `test_series_key_output_is_always_nfc` keeps
    checking it.

    So this function holds no Unicode policy of its own -- not a duplicated
    rule, and not a hedge against its dependency misbehaving.

    This key is therefore no longer coarser than routing's own -- the two
    agree exactly, which is the point of #44. Equivalent composition forms
    now converge in the index as well as the lock domain, rather than the
    lock domain over-serializing to compensate for an index that split them.

    See `docs/lock_topology.md`.
    """
    return series_key(series_name)


class SeriesLockRegistry:
    """One `series-operation` lock per resolved series identity.

    Keyed by the *resolved, normalized* identity -- what the archives will
    actually be filed under -- and never by source path, staging case id, or
    destination directory. Two arrivals at different paths that resolve to the
    same series are the case this exists for, and keying on anything the
    arrival happens to carry would let both through.

    Normalization is applied by the registry rather than trusted to callers,
    so a caller that passes a display name still gets the right lock.

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

        Returning the same object for the same normalized identity is the
        whole contract: two threads that resolve to one series must contend
        for one lock, and a registry that minted a fresh lock per call would
        serialize nothing while appearing to.
        """
        key = lock_key(identity)
        if not key:
            raise ValueError(
                f"a series lock needs a resolved identity; {identity!r} "
                "normalizes to nothing"
            )
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = OrderedLock(LOCK_SERIES, key, factory=self._factory)
                self._locks[key] = lock
            return lock

    def __len__(self) -> int:
        with self._guard:
            return len(self._locks)


# ------------------------------------------------------- stable identity

MAX_KEYED_ACQUISITIONS = 2


class UnstableSeriesIdentityError(RuntimeError):
    """The resolved identity kept moving across successive locks.

    Structured rather than bare: `observed` is every identity seen, in order,
    so an operator can tell a genuine topology change from a resolver bug
    without reproducing it.
    """

    def __init__(self, observed: tuple[str, ...]) -> None:
        self.observed = observed
        super().__init__(
            "series identity did not settle after "
            f"{MAX_KEYED_ACQUISITIONS} keyed acquisitions; observed "
            + " -> ".join(repr(name) for name in observed)
            + ". Refusing rather than retrying again: a third identity means "
            "the destination is still changing, and looping would starve this "
            "operation and make the outcome depend on scheduling."
        )


@dataclass(frozen=True)
class SeriesTransaction:
    """The settled identity a critical section may act under."""

    identity: str
    key: str
    acquisitions: int


@contextmanager
def stable_series_lock(registry: SeriesLockRegistry, resolve):
    """Hold the series lock for an identity proven stable under that lock.

    The resolved identity is derived from the destination filesystem, which
    is exactly what another thread's move changes. So a single pre-lock
    resolution is not enough: it serializes operations that already agreed,
    without guaranteeing the identity is still authoritative once the
    critical section begins.

        1. resolve provisionally           -> K1
        2. acquire the series lock for K1
        3. re-resolve while holding K1
        4. still K1  -> the body runs, holding K1
        5. now K2    -> release K1 having mutated nothing, acquire K2
        6. re-resolve under K2; still K2 -> the body runs
        7. changed again -> UnstableSeriesIdentityError

    Two keyed acquisitions, never more. The second re-resolution is what
    covers another operation completing between releasing K1 and acquiring
    K2. A third distinct identity means the topology is still moving, and
    looping would starve this operation and make the result depend on
    scheduling rather than on the library.

    *resolve* is called once before any lock and once under each. It must be
    free of irreversible work: reading routing inputs, inspecting destination
    directories, and re-resolving identity are all fine, but it must not
    move or rename a payload, create authoritative staging state, update the
    index, or emit a completed routing decision. Nothing here can enforce
    that, so it is stated as the contract it is.
    """
    observed: list[str] = []
    identity = resolve()
    observed.append(identity)

    for attempt in range(1, MAX_KEYED_ACQUISITIONS + 1):
        lock = registry.for_series(identity)
        with lock:
            confirmed = resolve()
            if lock_key(confirmed) == lock_key(identity):
                yield SeriesTransaction(identity=confirmed, key=lock.key,
                                        acquisitions=attempt)
                return
            observed.append(confirmed)
            identity = confirmed
            # Falls out of the `with` having mutated nothing, which is what
            # makes releasing and re-acquiring safe rather than a rollback.

    raise UnstableSeriesIdentityError(tuple(observed))
