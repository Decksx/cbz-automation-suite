"""Writer gate primitive and registered-root resolver (#158 B3, slice 1).

Cross-process cases run a real child that holds a lock and waits, so they
exercise the OS byte-range locks the design relies on (measured on X: and
the Tower share on 2026-11-13), not an in-process stand-in.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from comic_automation.library import writer_gate as wg

pytestmark = pytest.mark.skipif(os.name != "nt", reason="the writer gate is Windows-only")
REPO = Path(__file__).resolve().parents[1]

CHILD = r"""
import json, sys
from pathlib import Path
from comic_automation.library import writer_gate as wg
registry, mode, key = sys.argv[1], sys.argv[2], sys.argv[3]
root = wg.load_registry(registry)[0]
try:
    holder = wg.ordinary_unit(root, key) if mode == "ordinary" else wg.maintenance_run(root, {"who": "child"})
    with holder:
        print("held", flush=True)
        sys.stdin.readline()
except wg.GateBusy:
    print("busy", flush=True)
"""


def write_registry(path: Path, roots) -> Path:
    path.write_text(json.dumps({"contract": wg.REGISTRY_CONTRACT, "roots": [
        {"path": str(root), "series_depth": depth, "enforced": False} for root, depth in roots]}), encoding="utf-8")
    return path


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "library"
    (root / "Series").mkdir(parents=True)
    registry = write_registry(tmp_path / "roots.json", [(root, 1)])
    return root, registry, wg.load_registry(registry)[0]


class Holder:
    """A child process holding one lock until released or killed."""

    def __init__(self, registry: Path, mode: str, key: str = "series"):
        self.process = subprocess.Popen([sys.executable, "-c", CHILD, str(registry), mode, key], cwd=REPO,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.state = self.process.stdout.readline().strip()

    def release(self):
        if self.process.poll() is None:
            self.process.stdin.write("\n")
            self.process.stdin.flush()
        self.process.wait(timeout=20)


def attempt(manager) -> str:
    try:
        with manager:
            return "granted"
    except wg.GateBusy:
        return "busy"


@pytest.mark.parametrize("held, request_kind, request_key, expected", [
    ("ordinary", "ordinary", "other", "granted"),      # shared gate, different series
    ("ordinary", "ordinary", "series", "busy"),        # same series byte
    ("ordinary", "maintenance", None, "busy"),         # a transaction blocks maintenance
    ("maintenance", "ordinary", "other", "busy"),      # maintenance blocks every series
    ("maintenance", "maintenance", None, "busy"),
])
def test_cross_process_conflicts(library, held, request_kind, request_key, expected):
    root, registry, coordination = library
    holder = Holder(registry, held)
    try:
        assert holder.state == "held"
        manager = (wg.ordinary_unit(coordination, request_key) if request_kind == "ordinary"
                   else wg.maintenance_run(coordination, {"who": "test"}))
        assert attempt(manager) == expected
    finally:
        holder.release()


def test_a_killed_holder_releases_at_once(library):
    root, registry, coordination = library
    holder = Holder(registry, "maintenance")
    assert holder.state == "held"
    holder.process.kill()
    holder.process.wait(timeout=20)
    assert attempt(wg.ordinary_unit(coordination, "series")) == "granted"


def test_the_lock_file_never_grows_and_is_never_removed(library):
    root, registry, coordination = library
    with wg.ordinary_unit(coordination, "series"):
        pass
    with wg.maintenance_run(coordination, {"who": "test"}):
        pass
    assert (root / wg.GATE_LOCK).stat().st_size == 0
    assert json.loads((root / wg.GATE_OWNER).read_text())["who"] == "test"


def test_series_hash_collisions_only_cause_false_contention(library, monkeypatch):
    """Two keys forced onto one byte contend; they are never both admitted."""
    root, registry, coordination = library
    monkeypatch.setattr(wg, "_offset", lambda key: 7)
    results = []
    with wg.ordinary_unit(coordination, "alpha"):
        thread = threading.Thread(target=lambda: results.append(attempt(wg.ordinary_unit(coordination, "beta"))))
        thread.start()
        thread.join()
    assert results == ["busy"]


def test_a_thread_cannot_take_the_gate_twice(library):
    root, registry, coordination = library
    with wg.ordinary_unit(coordination, "series"):
        with pytest.raises(wg.GateRefused, match="already holds"):
            with wg.ordinary_unit(coordination, "other"):
                pass
    with wg.ordinary_unit(coordination, "series"):  # released afterwards
        pass


def test_a_replaced_root_is_refused(library):
    root, registry, coordination = library
    root.rename(root.with_name("moved"))
    root.mkdir()
    with pytest.raises(wg.GateRefused, match="changed identity"):
        with wg.ordinary_unit(coordination, "series"):
            pass


# --- registry and resolver --------------------------------------------------

def _junction(link: Path, target: Path) -> None:
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)


def test_overlapping_and_aliased_registrations_are_refused(tmp_path):
    outer = tmp_path / "X"
    (outer / "Manga").mkdir(parents=True)
    with pytest.raises(wg.GateRefused, match="overlap"):
        wg.load_registry(write_registry(tmp_path / "a.json", [(outer, 1), (outer / "Manga", 1)]))
    _junction(tmp_path / "alias", outer)
    with pytest.raises(wg.GateRefused, match="overlap"):
        wg.load_registry(write_registry(tmp_path / "b.json", [(outer, 1), (tmp_path / "alias", 1)]))


@pytest.mark.parametrize("document", [
    {"contract": "other", "roots": []},
    {"contract": wg.REGISTRY_CONTRACT, "roots": []},
    {"contract": wg.REGISTRY_CONTRACT, "roots": [{"path": "relative", "series_depth": 1, "enforced": False}]},
    {"contract": wg.REGISTRY_CONTRACT, "roots": [{"path": "C:\\", "series_depth": 0, "enforced": False}]},
    {"contract": wg.REGISTRY_CONTRACT, "roots": [{"path": "C:\\", "series_depth": 1, "enforced": "no"}]},
    {"contract": wg.REGISTRY_CONTRACT, "roots": [{"path": "C:\\", "series_depth": 1, "enforced": False, "x": 1}]},
])
def test_malformed_registries_are_refused(tmp_path, document):
    path = tmp_path / "roots.json"
    path.write_text(json.dumps(document))
    with pytest.raises(wg.GateRefused):
        wg.load_registry(path)


def test_resolution_is_physical_and_nested_callers_share_the_enclosing_gate(library, tmp_path):
    root, registry, coordination = library
    _junction(tmp_path / "alias", root)
    via_alias, below = wg.coordination_root((coordination,), tmp_path / "alias" / "Series" / "new.cbz")
    assert via_alias is coordination and below == ("Series", "new.cbz")
    nested, below = wg.coordination_root((coordination,), root / "Series")
    assert nested is coordination and below == ("Series",)
    with pytest.raises(wg.GateRefused, match="no registered"):
        wg.coordination_root((coordination,), tmp_path)          # above the root
    with pytest.raises(wg.GateRefused, match="no registered"):
        wg.coordination_root((coordination,), tmp_path / "elsewhere" / "x.cbz")


def test_series_keys_use_directory_components_only(tmp_path):
    root = tmp_path / "X"
    (root / "Manga" / "Berserk").mkdir(parents=True)
    flat = wg.load_registry(write_registry(tmp_path / "flat.json", [(root / "Manga", 1)]))[0]
    _, file_below = wg.coordination_root((flat,), root / "Manga" / "Berserk" / "Berserk 01.cbz")
    _, folder_below = wg.coordination_root((flat,), root / "Manga" / "Berserk")
    assert wg.series_key(flat, file_below, folder=False) == wg.series_key(flat, folder_below, folder=True) == "berserk"
    _, loose = wg.coordination_root((flat,), root / "Manga" / "Loose 01.cbz")
    assert wg.series_key(flat, loose, folder=False) == wg.ROOT_KEY      # a filename is never a series
    deep = wg.CoordinationRoot(root, 2, False, flat.identity, root)
    assert wg.series_key(deep, ("Manga", "Berserk", "Berserk 01.cbz"), folder=False) == "manga/berserk"
    assert wg.series_key(deep, ("Manga", "Loose.cbz"), folder=False) == wg.ROOT_KEY
    # Case and Unicode composition never split one Windows folder into two keys.
    assert wg.series_key(deep, ("MANGA", "Café"), folder=True) == wg.series_key(deep, ("manga", "Café"), folder=True)


def test_sync_observations_never_carry_or_count_control_files(tmp_path):
    """All four control filenames are excluded at the root and below it."""
    from comic_automation.library.sync_inventory import RootPair, observe_pairs
    roots = {side: tmp_path / side for side in ("tower", "target")}
    for root in roots.values():
        (root / "Series").mkdir(parents=True)
        (root / "Series" / "a.cbz").write_bytes(b"book")
        for name in wg.CONTROL_FILENAMES:
            (root / name).write_bytes(b"")
            (root / "Series" / name).write_bytes(b"")
    report = observe_pairs((RootPair("m", roots["tower"], roots["target"]),))
    assert report["complete"] and [row["relative_path"] for row in report["files"]] == ["Series/a.cbz"] * 2
    excluded = {row["path"] for row in report["excluded"]}
    assert excluded == {name for name in wg.CONTROL_FILENAMES} | {f"Series/{name}" for name in wg.CONTROL_FILENAMES}
    assert set(wg.CONTROL_FILENAMES) <= set(report["exclusion_policy"]["component_names"])


# --- physical binding and acquisition window (codex-lead review of #195) ----

def test_an_interior_alias_gets_the_physical_series_key_and_cannot_double_admit(library, tmp_path):
    root, registry, coordination = library
    _junction(root / "Alias", root / "Series")
    normal, below = wg.coordination_root((coordination,), root / "Series" / "book.cbz")
    alias, alias_below = wg.coordination_root((coordination,), root / "Alias" / "book.cbz")
    assert alias is normal and alias_below == below == ("Series", "book.cbz")
    results = []
    with wg.ordinary_unit(normal, wg.series_key(normal, below, folder=False)):
        thread = threading.Thread(target=lambda: results.append(
            attempt(wg.ordinary_unit(alias, wg.series_key(alias, alias_below, folder=False)))))
        thread.start()
        thread.join(timeout=10)
    assert results == ["busy"]


def test_a_junction_escaping_into_another_root_uses_that_roots_gate(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    (second / "Series").mkdir(parents=True)
    _junction(first / "Escape", second / "Series")
    roots = wg.load_registry(write_registry(tmp_path / "r.json", [(first, 1), (second, 1)]))
    chosen, below = wg.coordination_root(roots, first / "Escape" / "new.cbz")
    assert chosen.path == second and below == ("Series", "new.cbz")


def test_an_external_alias_straight_to_a_series_resolves_to_the_enclosing_root(library, tmp_path):
    root, registry, coordination = library
    _junction(tmp_path / "Shortcut", root / "Series")
    chosen, below = wg.coordination_root((coordination,), tmp_path / "Shortcut" / "Missing Dir" / "new.cbz")
    assert chosen is coordination and below == ("Series", "Missing Dir", "new.cbz")
    assert wg.series_key(chosen, below, folder=False) == "series"


def test_a_retargeted_alias_is_caught_by_still_bound(library, tmp_path):
    root, registry, coordination = library
    (root / "Other").mkdir()
    _junction(root / "Alias", root / "Series")
    chosen, below = wg.coordination_root((coordination,), root / "Alias" / "book.cbz")
    assert wg.still_bound((coordination,), root / "Alias" / "book.cbz", chosen, below)
    (root / "Alias").rmdir()                      # removes the junction, not its target
    _junction(root / "Alias", root / "Other")
    assert not wg.still_bound((coordination,), root / "Alias" / "book.cbz", chosen, below)


@pytest.mark.parametrize("kind", ["ordinary", "maintenance"])
def test_a_root_swapped_during_the_gate_open_is_refused_and_released(library, tmp_path, monkeypatch, kind):
    root, registry, coordination = library
    real_open = os.open
    swapped = []

    def open_after_swap(path, *args, **kwargs):
        if Path(path) == root / wg.GATE_LOCK and not swapped:
            swapped.append(True)
            root.rename(tmp_path / "old-library")
            root.mkdir()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(wg.os, "open", open_after_swap)
    manager = wg.ordinary_unit(coordination, "series") if kind == "ordinary" else wg.maintenance_run(coordination, {"who": "t"})
    with pytest.raises(wg.GateRefused, match="changed during acquisition"):
        with manager:
            pytest.fail("ownership was granted on a replacement root")
    monkeypatch.setattr(wg.os, "open", real_open)
    replacement = wg.load_registry(write_registry(tmp_path / "again.json", [(root, 1)]))[0]
    assert attempt(wg.maintenance_run(replacement, {"who": "t"})) == "granted"   # nothing left locked


def test_a_link_still_on_the_physical_chain_is_refused(library, monkeypatch):
    """Simulates an alias created or retargeted after resolution: the chain is
    walked again with lstat, and a remaining reparse point is refused."""
    root, registry, coordination = library
    _junction(root / "Alias", root / "Series")
    monkeypatch.setattr(wg.os.path, "realpath", lambda path: path)
    with pytest.raises(wg.GateRefused, match="remains on the physical path"):
        wg.coordination_root((coordination,), root / "Alias" / "book.cbz")


def test_a_held_lock_file_cannot_be_renamed_or_deleted(library):
    """The property that makes swapping the lock file under a holder impossible:
    Python's os.open grants no delete sharing, so Windows refuses both."""
    root, registry, coordination = library
    with wg.ordinary_unit(coordination, "series"):
        with pytest.raises(PermissionError):
            os.replace(root / wg.GATE_LOCK, root / "orphaned.lock")
        with pytest.raises(PermissionError):
            (root / wg.GATE_LOCK).unlink()
    os.replace(root / wg.GATE_LOCK, root / "after.lock")   # free once released


def test_a_retargeted_registered_root_alias_cannot_move_a_held_transaction(tmp_path):
    """The registered root is itself a junction, repointed while a unit is held.

    Writes still go to the bound physical tree (where the gate is held), and
    the binding is reported broken, so the writer refuses before mutating.
    """
    first, second, alias = tmp_path / "first", tmp_path / "second", tmp_path / "registered"
    for root in (first, second):
        (root / "Series").mkdir(parents=True)
    _junction(alias, first)
    registry = wg.load_registry(write_registry(tmp_path / "r.json", [(alias, 1)]))
    original = first / "Series" / "new.cbz"
    root, below = wg.coordination_root(registry, original)
    with wg.ordinary_unit(root, wg.series_key(root, below, folder=False)):
        assert wg.still_bound(registry, original, root, below)
        alias.rmdir()
        _junction(alias, second)
        assert not wg.still_bound(registry, original, root, below)
        assert wg.destination(root, below).parent.stat().st_ino == (first / "Series").stat().st_ino
        assert (first / wg.GATE_LOCK).exists() and not (second / wg.GATE_LOCK).exists()
    with pytest.raises(wg.GateRefused, match="changed identity"):
        with wg.ordinary_unit(root, "series"):
            pass


def test_a_registered_alias_root_locks_and_writes_in_its_physical_tree(tmp_path):
    target = tmp_path / "physical"
    (target / "Series").mkdir(parents=True)
    _junction(tmp_path / "registered", target)
    registry = wg.load_registry(write_registry(tmp_path / "r.json", [(tmp_path / "registered", 1)]))
    root, below = wg.coordination_root(registry, tmp_path / "registered" / "Series" / "a.cbz")
    assert Path(os.path.realpath(wg.destination(root, below))) == wg.destination(root, below)
    with wg.maintenance_run(root, {"who": "t"}):
        pass
    assert (target / wg.GATE_OWNER).exists()


def test_an_alias_flipped_away_and_back_around_the_open_cannot_misplace_the_lock(tmp_path, monkeypatch):
    """ABA on a registered root alias: point it at another tree only for the
    instant of the gate open, then back. The lock must still land in the bound tree."""
    first, second, alias = tmp_path / "first", tmp_path / "second", tmp_path / "registered"
    first.mkdir()
    second.mkdir()
    _junction(alias, first)
    root = wg.load_registry(write_registry(tmp_path / "r.json", [(alias, 1)]))[0]
    real_open = os.open

    def flip_around_open(path, *args, **kwargs):
        if Path(path).name == wg.GATE_LOCK:
            alias.rmdir()
            _junction(alias, second)
            try:
                return real_open(path, *args, **kwargs)
            finally:
                alias.rmdir()
                _junction(alias, first)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(wg.os, "open", flip_around_open)
    with wg.ordinary_unit(root, "series"):
        assert (first / wg.GATE_LOCK).exists() and not (second / wg.GATE_LOCK).exists()


def test_control_files_never_enter_an_all_tower_baseline_or_block_its_verification(tmp_path):
    """Combined observation policy (#195 exclusions with #196's all-Tower baseline):
    gate files created on the receiver after the baseline neither appear in
    the evidence nor turn into verifier differences."""
    from comic_automation.library.sync_inventory import RootPair, observe_pairs
    from comic_automation.library.sync_verify import verify_pairs
    roots = {side: tmp_path / side for side in ("tower", "target")}
    for root in roots.values():
        (root / "Series").mkdir(parents=True)
        (root / "Series" / "a.cbz").write_bytes(b"book")
    (roots["tower"] / wg.GATE_LOCK).write_bytes(b"")
    pair = (RootPair("m", roots["tower"], roots["target"]),)
    baseline = observe_pairs(pair, hash_all_tower=True)
    assert baseline["complete"] and [row["relative_path"] for row in baseline["files"]] == ["Series/a.cbz"] * 2
    for name in wg.CONTROL_FILENAMES:          # a writer gate starts being used on the receiver
        (roots["target"] / name).write_bytes(b"")
    result = verify_pairs(baseline, pair)
    assert result["verified"] and result["extra"] == [] and len(result["matched"]) == 1
