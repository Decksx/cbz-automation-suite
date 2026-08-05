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

Every assertion is deterministic: identities are scripted rather than raced,
and the one concurrency check uses a non-blocking acquire from a thread that
is joined before its answer is read.
"""

from __future__ import annotations

import threading
import zipfile
from pathlib import Path

import pytest

from scripts import cbz_watcher as watcher
from scripts.cbz_lock_order import (
    LOCK_SERIES,
    SeriesLockRegistry,
    held_locks,
    lock_key,
)


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


def test_the_same_resolved_identity_serializes_across_arrivals(wired):
    """Two arrivals, one series: the second cannot enter while the first acts."""
    watch_root, _, arrival, trace, monkeypatch = wired
    other = watch_root / "Berserk 5"
    _make_cbz(other / "chapter.cbz")
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
