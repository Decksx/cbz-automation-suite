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
    deep = wg.CoordinationRoot(root, 2, False, flat.identity)
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
