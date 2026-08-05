"""Tests for the classify -> move -> index lock primitive (issue #32).

Every concurrency assertion here is deterministic. Nothing sleeps, nothing
waits for a deadlock, and nothing depends on which thread the scheduler runs
first:

* ordering violations are raised on a single thread at the moment of the
  request, so no second thread is needed to demonstrate them;
* contention is observed with a non-blocking acquire from a second thread,
  which answers "is this held right now" without waiting at all;
* every helper thread is joined before anything is asserted about it.

`docs/session_protocol.md` records three tests that passed locally and failed
in CI because they timed something instead of proving it. This file is
written against that.
"""

from __future__ import annotations

import threading

import pytest

from scripts.cbz_lock_order import (
    LOCK_CLASSES,
    LOCK_INDEX,
    LOCK_SERIES,
    LockOrderError,
    OrderedLock,
    SeriesLockRegistry,
    held_locks,
)


def _acquired_by_a_fresh_thread(lock: OrderedLock) -> bool:
    """Whether another thread can take *lock* right now.

    Non-blocking, on a thread that is joined before the answer is read, so
    this reports the lock's state rather than a race with the scheduler.
    """
    result: dict[str, bool] = {}

    def attempt() -> None:
        assert held_locks() == (), "a fresh thread must start holding nothing"
        got = lock.acquire(blocking=False)
        result["got"] = got
        if got:
            lock.release()

    worker = threading.Thread(target=attempt, name="probe")
    worker.start()
    worker.join()
    return result["got"]


# ── the ordering rule, both directions ───────────────────────────


def test_a_series_lock_may_take_the_index_lock():
    series = OrderedLock(LOCK_SERIES, "Berserk")
    index = OrderedLock(LOCK_INDEX)
    completed = False

    with series:
        with index:
            completed = True

    assert completed, "the permitted direction did not complete"
    assert held_locks() == ()


def test_the_index_lock_may_never_take_a_series_lock():
    """The forbidden direction, refused immediately rather than deadlocked."""
    index = OrderedLock(LOCK_INDEX)
    series = OrderedLock(LOCK_SERIES, "Berserk")

    with index:
        with pytest.raises(LockOrderError) as caught:
            series.acquire()

    message = str(caught.value)
    assert LOCK_INDEX in message, "the held lock class is not named"
    assert LOCK_SERIES in message, "the requested lock class is not named"
    assert "Berserk" in message, "the requested key is not named"
    assert held_locks() == (), "the failed acquisition left state behind"


def test_the_violation_is_raised_even_when_the_lock_is_free():
    """An ordering bug that only shows under contention is one that ships."""
    index = OrderedLock(LOCK_INDEX)
    series = OrderedLock(LOCK_SERIES, "Berserk")
    assert _acquired_by_a_fresh_thread(series) is True, "precondition: free"

    with index:
        with pytest.raises(LockOrderError):
            series.acquire()


def test_holding_one_series_lock_refuses_a_different_one():
    """The classic cycle: two threads each holding one, reaching for the other."""
    first = OrderedLock(LOCK_SERIES, "Berserk")
    second = OrderedLock(LOCK_SERIES, "Vinland Saga")

    with first:
        with pytest.raises(LockOrderError, match="Vinland Saga"):
            second.acquire()


def test_re_entering_the_same_lock_is_allowed():
    """Same lock, same thread -- an RLock, not a same-rank violation."""
    series = OrderedLock(LOCK_SERIES, "Berserk")
    with series:
        with series:
            assert len(held_locks()) == 2
    assert held_locks() == ()


def test_an_unknown_lock_class_is_refused():
    with pytest.raises(ValueError, match="unknown lock class"):
        OrderedLock("something-invented")


def test_the_declared_classes_are_exactly_the_two_in_the_topology():
    assert set(LOCK_CLASSES) == {LOCK_SERIES, LOCK_INDEX}


# ── serialization by resolved identity ───────────────────────────


def test_the_same_resolved_identity_serializes():
    registry = SeriesLockRegistry()
    lock = registry.for_series("Berserk")

    with lock:
        assert _acquired_by_a_fresh_thread(registry.for_series("Berserk")) \
            is False, "a second thread entered the same series"

    assert _acquired_by_a_fresh_thread(registry.for_series("Berserk")) is True


def test_different_resolved_identities_proceed_independently():
    registry = SeriesLockRegistry()

    with registry.for_series("Berserk"):
        assert _acquired_by_a_fresh_thread(registry.for_series("Vinland Saga")) \
            is True, "an unrelated series was blocked"


def test_the_registry_returns_one_lock_per_identity():
    """A fresh lock per call would serialize nothing while appearing to."""
    registry = SeriesLockRegistry()
    assert registry.for_series("Berserk") is registry.for_series("Berserk")
    assert registry.for_series("Berserk") is not registry.for_series("Kaiju")
    assert len(registry) == 2


def test_the_key_is_the_resolved_identity_and_nothing_else():
    """Two arrivals at different paths, one series: one lock.

    Keying on anything the arrival carries -- source path, case id,
    destination -- would let both through, which is the race #32 opens with.
    """
    registry = SeriesLockRegistry()
    from_watch_a = "Berserk"          # resolved from "Berserk Ch. 4"
    from_watch_b = "Berserk"          # resolved from "Berserk 5"
    assert registry.for_series(from_watch_a) is registry.for_series(from_watch_b)
    assert len(registry) == 1


def test_an_empty_identity_is_refused():
    with pytest.raises(ValueError, match="resolved identity"):
        SeriesLockRegistry().for_series("")


# ── release semantics ────────────────────────────────────────────


def test_an_exception_inside_the_critical_section_releases_the_lock():
    registry = SeriesLockRegistry()
    lock = registry.for_series("Berserk")

    with pytest.raises(RuntimeError, match="classify failed"):
        with lock:
            raise RuntimeError("classify failed")

    assert held_locks() == ()
    assert _acquired_by_a_fresh_thread(lock) is True, "the lock was not released"


def test_an_exception_releases_the_inner_lock_too():
    series = OrderedLock(LOCK_SERIES, "Berserk")
    index = OrderedLock(LOCK_INDEX)

    with pytest.raises(RuntimeError, match="index update failed"):
        with series:
            with index:
                raise RuntimeError("index update failed")

    assert held_locks() == ()
    assert _acquired_by_a_fresh_thread(series) is True
    assert _acquired_by_a_fresh_thread(index) is True


def test_the_context_manager_never_swallows_an_exception():
    with pytest.raises(KeyError):
        with OrderedLock(LOCK_SERIES, "Berserk"):
            raise KeyError("must propagate")


def test_held_state_is_per_thread():
    """One thread's held locks must not license another thread's ordering."""
    index = OrderedLock(LOCK_INDEX)
    seen: dict[str, tuple] = {}

    with index:
        assert held_locks() == ((LOCK_INDEX, ""),)

        def observe() -> None:
            seen["held"] = held_locks()
            # Forbidden for the holder, permitted here: this thread holds
            # nothing, so a series lock is a legal first acquisition.
            with OrderedLock(LOCK_SERIES, "Berserk"):
                seen["inner"] = held_locks()

        worker = threading.Thread(target=observe, name="observer")
        worker.start()
        worker.join()

    assert seen["held"] == ()
    assert seen["inner"] == ((LOCK_SERIES, "Berserk"),)
    assert held_locks() == ()


def test_a_refused_non_blocking_acquire_records_nothing():
    registry = SeriesLockRegistry()
    lock = registry.for_series("Berserk")

    with lock:
        result: dict[str, tuple] = {}

        def attempt() -> None:
            assert lock.acquire(blocking=False) is False
            result["held"] = held_locks()

        worker = threading.Thread(target=attempt, name="refused")
        worker.start()
        worker.join()

    assert result["held"] == (), "a failed acquire was recorded as held"
