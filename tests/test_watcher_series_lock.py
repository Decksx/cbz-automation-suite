"""The classify -> move -> index transaction, as actually wired (issue #32).

`tests/test_lock_order.py` proves the primitive. This proves the call
sequence the watcher builds on it, which is where the guarantees either hold
or quietly do not:

    the identity is resolved before anything mutates
    nothing mutates under a provisional key that is later abandoned
    mutation happens only while the final series lock is held

The resolver's read-only-ness is pinned here rather than asserted inside
`stable_series_lock`, because no generic assertion can observe whether an
arbitrary callable had effects. What can be checked is this specific
resolver against this specific tree, which is what the last test does.

Identities are scripted rather than raced. The integration witnesses run
actual ingests with the real shadow router, index and mover on temporary
paths. Events establish the interleaving; timeouts only bound broken tests.
"""

from __future__ import annotations

import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import cbz_watcher as watcher
from scripts.cbz_lock_order import (
    LOCK_SERIES,
    SeriesLockRegistry,
    held_locks,
    lock_key,
)
from scripts.cbz_routing import SeriesIndex, parse
from scripts.cbz_watcher_router import MODE_SHADOW, WatcherRouter


def _make_cbz(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("page1.jpg", b"\0" * 64)


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A watcher pointed at tmp dirs, with every mutation recorded in order."""
    watch_root = tmp_path / "incoming"
    dest_root = tmp_path / "library"
    watch_root.mkdir()
    dest_root.mkdir()

    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(watch_root))
    monkeypatch.setattr(watcher, "_routing_rules", [])
    monkeypatch.setattr(watcher, "_routing_default", str(dest_root))
    monkeypatch.setattr(watcher, "POLL_INTERVAL", 0)
    # A registry per test, so one test's held locks cannot leak into another.
    monkeypatch.setattr(watcher, "_SERIES_LOCKS", SeriesLockRegistry())

    trace: list[tuple[str, str, tuple]] = []

    def record(kind: str, identity: str) -> None:
        trace.append((kind, identity, held_locks()))

    def fake_apply_fallback(parsed, series_name, dir_number):
        record("mutate:rename", series_name)

    def fake_shadow(comic_dir, dest, series_name, archives, results):
        record("classify", series_name)

    def fake_move_dir(comic_dir, dest, *, target_name, chapter_number=None):
        record("mutate:move", target_name)
        return Path(dest) / target_name

    def fake_move_loose(archives, dest, series_name):
        record("mutate:move_loose", series_name)
        return Path(dest) / series_name

    def fake_note_move(series_name, dest, landed):
        record("mutate:index", series_name)

    monkeypatch.setattr(watcher, "_apply_fallback_naming", fake_apply_fallback)
    monkeypatch.setattr(watcher, "_shadow_route", fake_shadow)
    monkeypatch.setattr(watcher, "_move_cbz_dir", fake_move_dir)
    monkeypatch.setattr(watcher, "_move_loose_files", fake_move_loose)
    monkeypatch.setattr(watcher, "_note_move", fake_note_move)

    arrival = watch_root / "Berserk Ch. 4"
    _make_cbz(arrival / "chapter.cbz")
    return watch_root, dest_root, arrival, trace, monkeypatch


def _script_identities(monkeypatch, *identities: str) -> list[str]:
    """Make the resolver return each identity in turn, then repeat the last."""
    seen: list[str] = []

    def scripted(comic_dir, cbz_files, dest_folder):
        index = min(len(seen), len(identities) - 1)
        name = identities[index]
        seen.append(name)
        return name, None

    monkeypatch.setattr(watcher, "_resolve_series_dir_name", scripted)
    return seen


def _mutations(trace) -> list[tuple[str, str]]:
    return [(kind, identity) for kind, identity, _ in trace
            if kind.startswith("mutate")]


def test_a_stable_identity_mutates_once_under_one_lock(wired):
    _, _, arrival, trace, monkeypatch = wired
    resolved = _script_identities(monkeypatch, "Berserk")

    watcher.process_and_move_directory(arrival)

    assert len(resolved) == 2, "expected one provisional and one confirming resolve"
    assert [kind for kind, _, _ in trace] == [
        "mutate:rename", "classify", "mutate:move", "mutate:index",
    ]
    assert all(identity == "Berserk" for _, identity, _ in trace)


def test_the_identity_is_resolved_before_anything_mutates(wired):
    _, _, arrival, trace, monkeypatch = wired
    order: list[str] = []

    def scripted(comic_dir, cbz_files, dest_folder):
        order.append("resolve")
        return "Berserk", None

    monkeypatch.setattr(watcher, "_resolve_series_dir_name", scripted)
    real_record_len = len(trace)

    watcher.process_and_move_directory(arrival)

    assert order, "the resolver never ran"
    assert len(trace) > real_record_len, "nothing mutated at all"
    # Both resolves precede every recorded mutation, by construction: the
    # trace is empty until the transaction yields.
    assert _mutations(trace)[0][0] == "mutate:rename"
    assert len(order) == 2


def test_nothing_mutates_under_an_abandoned_provisional_key(wired):
    """K1 -> K2. Every mutation must carry the final identity, not the first."""
    _, _, arrival, trace, monkeypatch = wired
    _script_identities(monkeypatch, "Berserk Ch. 4", "Berserk")

    watcher.process_and_move_directory(arrival)

    mutations = _mutations(trace)
    assert mutations, "nothing ran at all"
    assert all(identity == "Berserk" for _, identity in mutations), \
        f"a mutation ran under the abandoned key: {mutations}"


def test_every_mutation_holds_the_final_series_lock(wired):
    """Not merely 'a lock' -- the lock for the identity being acted on."""
    _, _, arrival, trace, monkeypatch = wired
    _script_identities(monkeypatch, "Berserk Ch. 4", "Berserk")

    watcher.process_and_move_directory(arrival)

    expected = (LOCK_SERIES, lock_key("Berserk"))
    for kind, identity, held in trace:
        assert expected in held, f"{kind} ran without the final series lock: {held}"
    assert held_locks() == (), "a lock outlived the transaction"


def test_an_unstable_identity_defers_without_mutating(wired):
    """K1 -> K2 -> K3. The arrival is left where it is for a later pass.

    The payload stays in the watch folder; its *filename* may already have
    been normalized, because per-file cleaning happens before the lock is
    taken. That is deliberate -- holding a series lock across file processing
    would serialize unrelated series on unrelated work -- and it is why the
    assertion is about the archive still being there rather than about it
    still having its original name.
    """
    watch_root, dest_root, arrival, trace, monkeypatch = wired
    _script_identities(monkeypatch, "First", "Second", "Third")

    watcher.process_and_move_directory(arrival)

    assert _mutations(trace) == [], "something mutated on an unsettled identity"
    assert arrival.is_dir(), "the arrival was consumed despite deferring"
    assert list(arrival.glob("*.cbz")), "the payload left the watch folder"
    assert list(dest_root.iterdir()) == [], "something reached the destination"
    assert held_locks() == ()


def test_an_unstable_identity_does_not_stop_the_watcher(wired):
    """Deferring is not crashing: the pass completes and the next one retries."""
    _, _, arrival, trace, monkeypatch = wired
    _script_identities(monkeypatch, "First", "Second", "Third")

    watcher.process_and_move_directory(arrival)          # must not raise

    _script_identities(monkeypatch, "Berserk")
    watcher.process_and_move_directory(arrival)
    assert _mutations(trace), "the retry did not proceed once identity settled"


def test_a_second_thread_cannot_acquire_the_series_lock_mid_move(wired):
    """A lock probe; the two-ingest witness below tests index handoff."""
    watch_root, _, arrival, trace, monkeypatch = wired
    _script_identities(monkeypatch, "Berserk")
    observed: dict[str, bool] = {}

    real_move = watcher._move_cbz_dir

    def move_and_probe(comic_dir, dest, *, target_name, chapter_number=None):
        lock = watcher._SERIES_LOCKS.for_series(target_name)

        def probe() -> None:
            observed["free"] = lock.acquire(blocking=False)
            if observed["free"]:
                lock.release()

        worker = threading.Thread(target=probe, name="second-arrival")
        worker.start()
        worker.join()
        return real_move(comic_dir, dest, target_name=target_name,
                         chapter_number=chapter_number)

    monkeypatch.setattr(watcher, "_move_cbz_dir", move_and_probe)
    watcher.process_and_move_directory(arrival)

    assert observed["free"] is False, \
        "another arrival could enter the same series mid-move"


def test_the_resolver_the_watcher_passes_is_read_only(tmp_path, monkeypatch):
    """The caller-specific half of the contract, checked against a real tree.

    `stable_series_lock` cannot observe whether an arbitrary callable had
    effects, so its purity requirement is documented rather than enforced.
    What is checkable is this resolver against this tree, which is what makes
    releasing a provisional lock safe in the wired path specifically.
    """
    watch_root = tmp_path / "incoming"
    dest_root = tmp_path / "library"
    watch_root.mkdir()
    dest_root.mkdir()
    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(watch_root))

    arrival = watch_root / "Berserk Ch. 4"
    _make_cbz(arrival / "chapter.cbz")
    (dest_root / "Berserk").mkdir()

    def snapshot() -> dict[str, bytes]:
        return {
            p.relative_to(tmp_path).as_posix(): (p.read_bytes() if p.is_file() else b"<dir>")
            for p in sorted(tmp_path.rglob("*"))
        }

    before = snapshot()
    name, _ = watcher._resolve_series_dir_name(
        arrival, sorted(arrival.glob("*.cbz")), str(dest_root)
    )

    assert name == "Berserk", "precondition: the resolver did resolve something"
    assert snapshot() == before, "the resolver mutated the tree"


@pytest.fixture
def ingest_handoff(tmp_path, monkeypatch):
    """Real ingest/router/index, with a barrier just before A publishes."""
    incoming = tmp_path / "incoming"
    library = tmp_path / "library"
    fallback = tmp_path / "fallback"
    for root in (incoming, library, fallback):
        root.mkdir()
    cfg = parse({
        "version": 2,
        "destinations": {"library": str(library), "fallback": str(fallback),
                         "review": str(tmp_path / "review")},
        "default": "fallback",
        "rules": [],
        "series_index": {"enabled": True,
                         "destinations": ["library", "fallback"]},
    })
    router = WatcherRouter(
        cfg, SeriesIndex(priority=cfg.series_index_destinations), mode=MODE_SHADOW)
    state = SimpleNamespace(
        library=library, router=router, pause_publication=True,
        a_progress=threading.Event(), a_paused=threading.Event(),
        b_progress=threading.Event(), release_publication=threading.Event(),
        b_lock_free=[], classifications=[], attempts=[], published=[],
        errors=[], workers=[], identities={},
    )

    class ProbedRLock:
        """Observe contention, then perform the same blocking acquisition."""
        def __init__(self):
            self.inner = threading.RLock()

        def acquire(self, blocking=True):
            if threading.current_thread().name == "ingest-b" and blocking:
                acquired = self.inner.acquire(blocking=False)
                state.b_lock_free.append(acquired)
                state.b_progress.set()
                if acquired:
                    return True
            return self.inner.acquire(blocking)

        def release(self):
            self.inner.release()

    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(incoming))
    monkeypatch.setattr(watcher, "_routing_rules", [])
    monkeypatch.setattr(watcher, "_routing_default", str(library))
    monkeypatch.setattr(watcher, "POLL_INTERVAL", 0)
    monkeypatch.setattr(watcher, "_router", router)
    monkeypatch.setattr(watcher, "_SERIES_LOCKS",
                        SeriesLockRegistry(factory=ProbedRLock))
    # Identity parsing and per-file normalization precede the transaction.
    # Keep this witness about its boundary, not those independent policies.
    monkeypatch.setattr(watcher, "_resolve_series_dir_name",
                        lambda *args: (state.identities[threading.current_thread().name], None))
    monkeypatch.setattr(watcher, "process_cbz_file", lambda path, **kw: (path, {}))
    monkeypatch.setattr(watcher, "_apply_fallback_naming", lambda *args: None)
    classify = router.classify
    note_move = router.note_move

    def observe_classification(*args, **kwargs):
        decision = classify(*args, **kwargs)
        state.classifications.append((threading.current_thread().name, decision))
        return decision

    def publish(series, destination, landed):
        name = threading.current_thread().name
        state.attempts.append((name, series, landed))
        if name == "ingest-a" and state.pause_publication:
            state.a_paused.set()
            state.a_progress.set()
            assert state.release_publication.wait(5), "publication was never released"
        note_move(series, destination, landed)
        state.published.append((name, series, landed))

    monkeypatch.setattr(router, "classify", observe_classification)
    monkeypatch.setattr(router, "note_move", publish)

    def start(label, series):
        name = f"ingest-{label}"
        state.identities[name] = series
        arrival = incoming / f"Arrival {label.upper()}"
        _make_cbz(arrival / f"{label}.cbz")

        def run():
            try:
                watcher.process_and_move_directory(arrival)
            except BaseException as exc:
                state.errors.append(exc)
            finally:
                # A missing lock/publication must fail without stranding the
                # test at a barrier that the mutated code never reaches.
                (state.a_progress if label == "a" else state.b_progress).set()

        worker = threading.Thread(target=run, name=name, daemon=True)
        state.workers.append(worker)
        worker.start()
        return worker

    state.start = start
    yield state
    state.release_publication.set()
    for worker in state.workers:
        worker.join(5)
    assert not any(worker.is_alive() for worker in state.workers), "ingest deadlocked"
    assert not state.errors, state.errors


def _finish_ingest(worker):
    worker.join(5)
    assert not worker.is_alive(), "ingest did not finish"


def test_waiting_ingest_classifies_after_the_first_index_publication(ingest_handoff):
    case = ingest_handoff
    first = case.start("a", "Berserk")
    assert case.a_progress.wait(5)
    assert case.a_paused.is_set(), "first ingest never reached index publication"
    assert case.router.index.lookup("Berserk") is None

    second = case.start("b", "Berserk")
    try:
        assert case.b_progress.wait(5)
        assert case.b_lock_free == [False], "second ingest did not contend for A's lock"
        assert [name for name, _ in case.classifications] == ["ingest-a"]
        assert case.published == []
    finally:
        case.release_publication.set()
    _finish_ingest(first)
    _finish_ingest(second)

    assert not case.errors
    assert [name for name, _ in case.classifications] == ["ingest-a", "ingest-b"]
    before, after = [decision for _, decision in case.classifications]
    assert before.dest_key == "fallback" and not before.authoritative
    assert after.dest_key == "library" and after.authoritative
    assert after.series_dir == case.library / "Berserk"
    assert [name for name, _, _ in case.published] == ["ingest-a", "ingest-b"]
    assert case.router.index.lookup("Berserk") == ("library", case.library / "Berserk")
    assert sorted(path.name for path in (case.library / "Berserk").glob("*.cbz")) == [
        "a.cbz", "b.cbz",
    ]


def test_unrelated_ingest_publishes_while_the_first_series_is_paused(ingest_handoff):
    case = ingest_handoff
    first = case.start("a", "Berserk")
    assert case.a_progress.wait(5)
    assert case.a_paused.is_set()
    second = case.start("b", "Claymore")
    try:
        assert case.b_progress.wait(5)
        assert case.b_lock_free == [True], "different identities share an operation lock"
        _finish_ingest(second)
        assert case.router.index.lookup("Berserk") is None
        assert case.router.index.lookup("Claymore") == ("library", case.library / "Claymore")
        assert [name for name, _, _ in case.published] == ["ingest-b"]
    finally:
        case.release_publication.set()
    _finish_ingest(first)
    assert not case.errors
    assert case.router.index.lookup("Berserk") == ("library", case.library / "Berserk")


def test_failed_move_publishes_nothing_and_the_next_ingest_acquires_the_lock(
        ingest_handoff, monkeypatch):
    case = ingest_handoff
    case.pause_publication = False
    move = watcher._move_cbz_dir

    def fail_first(*args, **kwargs):
        if threading.current_thread().name == "ingest-a":
            return None
        return move(*args, **kwargs)

    monkeypatch.setattr(watcher, "_move_cbz_dir", fail_first)
    _finish_ingest(case.start("a", "Berserk"))
    assert not case.errors
    assert case.attempts == [] and case.published == []
    assert case.router.index.lookup("Berserk") is None
    assert not (case.library / "Berserk").exists()

    _finish_ingest(case.start("b", "Berserk"))
    assert not case.errors
    assert case.b_lock_free == [True], "the failed move retained the series lock"
    assert [name for name, _, _ in case.published] == ["ingest-b"]
    assert case.router.index.lookup("Berserk") == ("library", case.library / "Berserk")
    assert [path.name for path in (case.library / "Berserk").glob("*.cbz")] == ["b.cbz"]
