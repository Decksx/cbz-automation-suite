"""Claim-first processing: boundary witnesses for the contracts in claims.py.

Each arrival leaves the MEGA-synced watch folder by one same-volume rename
before anything changes it, is processed in a claim folder, and ends in the
library, review or quarantine. These tests check the boundaries Codex named
for the PR replacing #175: claim, record and rename failures; crashes before
and after the rename; a missing or corrupt record; a crash after an archive
rewrite and before its disposition; a failed disposition move; mixed drop
points; routing identity; recreated source paths; a partial library
landing; AI-backup custody; and quarantine retention. Crashes are real
process exits (``os._exit``) in a fresh process, and the next pass is a
fresh process too, so nothing in memory survives between them.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from comic_automation.library import claims
from comic_automation.library.number_style import NumberStyleRegistry
from scripts import cbz_core
from scripts import cbz_watcher as watcher


def _make_cbz(path: Path, payload_size: int = 100) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("page1.jpg", b"\0" * payload_size)
    return path


def _payload_size(path: Path) -> int:
    with zipfile.ZipFile(path) as zf:
        return len(zf.read("page1.jpg"))


def _payloads(*roots: Path) -> list[int]:
    found = []
    for root in roots:
        for path in root.rglob("*.cbz") if root.exists() else []:
            try:
                found.append(_payload_size(path))
            except (zipfile.BadZipFile, KeyError, OSError):
                pass  # an empty reservation or partial copy is not a book
    return sorted(found)


def _snapshot(folder: Path) -> dict[str, bytes]:
    return {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


@pytest.fixture
def watch(tmp_path, monkeypatch):
    watch_root, library = tmp_path / "incoming", tmp_path / "library"
    watch_root.mkdir()
    library.mkdir()
    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(watch_root))
    monkeypatch.setattr(watcher, "_routing_rules", [])
    monkeypatch.setattr(watcher, "_routing_default", str(library))
    monkeypatch.setattr(watcher, "POLL_INTERVAL", 0)
    monkeypatch.setattr(watcher, "wait_for_file_stable", lambda *a, **k: True)
    monkeypatch.setattr(cbz_core, "translate_metadata_text", lambda text: (text, None))
    return watch_root, library


# ---------------------------------------------------------------------------
# Contract 1: claim before changing watched content; refuse unsafe roots
# ---------------------------------------------------------------------------

def test_roots_inside_the_watch_root_or_a_library_are_refused(tmp_path):
    watch_root, library = tmp_path / "watch", tmp_path / "library"
    ok = {"processing": tmp_path / "p", "review": tmp_path / "r", "quarantine": tmp_path / "q"}
    claims.validate_roots(watch_root, ok["processing"], ok["review"], ok["quarantine"], [library])
    with pytest.raises(claims.ClaimConfigError, match="overlaps the watch root"):
        claims.validate_roots(watch_root, watch_root / "p", ok["review"], ok["quarantine"])
    with pytest.raises(claims.ClaimConfigError, match="overlaps the library root"):
        claims.validate_roots(watch_root, ok["processing"], library / "r", ok["quarantine"], [library])
    with pytest.raises(claims.ClaimConfigError, match="overlap"):
        claims.validate_roots(watch_root, ok["processing"], ok["processing"] / "r", ok["quarantine"])


def test_a_junction_alias_into_the_watch_root_is_refused(tmp_path):
    """Physical paths are compared: a junction that leads into the watch
    root is the watch root."""
    _winapi = pytest.importorskip("_winapi")
    watch_root = tmp_path / "watch"
    (watch_root / "inside").mkdir(parents=True)
    alias = tmp_path / "alias"
    _winapi.CreateJunction(str(watch_root / "inside"), str(alias))
    with pytest.raises(claims.ClaimConfigError, match="overlaps the watch root"):
        claims.validate_roots(watch_root, alias, tmp_path / "r", tmp_path / "q")


def test_a_root_on_another_volume_is_refused(tmp_path, monkeypatch):
    """A claim must be a same-volume rename, never copy and delete."""
    real = claims._volume
    monkeypatch.setattr(claims, "_volume",
                        lambda path: -1 if "elsewhere" in str(path) else real(path))
    with pytest.raises(claims.ClaimConfigError, match="not on the watch root's volume"):
        claims.validate_roots(tmp_path / "watch", tmp_path / "elsewhere", tmp_path / "r", tmp_path / "q")


@pytest.mark.parametrize("failure", ["record", "rename"])
def test_a_failed_claim_leaves_the_arrival_untouched_and_retryable(watch, tmp_path, monkeypatch, failure):
    watch_root, library = watch
    arrival = watch_root / "Berserk"
    _make_cbz(arrival / "Berserk Ch. 1.cbz", 101)
    before = _snapshot(arrival)
    if failure == "record":
        monkeypatch.setattr(claims, "write_record",
                            lambda *a: (_ for _ in ()).throw(PermissionError("disk full")))
    else:
        real_rename = claims.os.rename
        monkeypatch.setattr(claims.os, "rename",
                            lambda src, dst: (_ for _ in ()).throw(PermissionError("in use"))
                            if Path(src) == arrival else real_rename(src, dst))

    watcher.process_and_move_directory(watch_root)

    assert _snapshot(arrival) == before  # names and bytes unchanged
    assert claims.claim_dirs(tmp_path / "_processing") == []
    monkeypatch.undo()
    _rewire(monkeypatch, watch_root, library)
    watcher.process_and_move_directory(watch_root)
    assert (library / "Berserk" / "Berserk Ch. 1.cbz").is_file()


def _rewire(monkeypatch, watch_root, library):
    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(watch_root))
    monkeypatch.setattr(watcher, "_routing_rules", [])
    monkeypatch.setattr(watcher, "_routing_default", str(library))
    monkeypatch.setattr(watcher, "wait_for_file_stable", lambda *a, **k: True)
    monkeypatch.setattr(cbz_core, "translate_metadata_text", lambda text: (text, None))
    monkeypatch.setattr(watcher, "PROCESSING_ROOT", watch_root.parent / "_processing")
    monkeypatch.setattr(watcher, "REVIEW_ROOT", watch_root.parent / "_review")
    monkeypatch.setattr(watcher, "QUARANTINE_ROOT", watch_root.parent / "_quarantine")


# ---------------------------------------------------------------------------
# Contract 2: the record precedes the rename; recovery never guesses
# ---------------------------------------------------------------------------

def _claim_folder(tmp_path, watch_root, *, record=True, payload=None, state="claimed"):
    claim_dir = tmp_path / "_processing" / "20261109-000000-abcdef0123"
    claim_dir.mkdir(parents=True)
    if record:
        claims.write_record(claim_dir, {
            "version": 1, "id": claim_dir.name, "state": state, "kind": "directory",
            "watch_root": str(watch_root), "source": "Berserk", "relative": "Berserk",
            "route": ["", str(tmp_path / "library")], "payload_name": "Berserk"})
    if payload is not None:
        for name, size in payload.items():
            _make_cbz(claim_dir / "Berserk" / name, size)
    return claim_dir


def test_a_crash_before_the_rename_leaves_an_empty_claim_that_is_removed(watch, tmp_path):
    watch_root, library = watch
    arrival = _make_cbz(watch_root / "Berserk" / "Berserk Ch. 1.cbz", 101)
    claim_dir = _claim_folder(tmp_path, watch_root, state="claiming")

    watcher.process_and_move_directory(watch_root)

    assert not claim_dir.exists()
    assert (library / "Berserk" / "Berserk Ch. 1.cbz").is_file()
    assert not arrival.exists()


def test_a_claimed_payload_is_resumed_and_lands(watch, tmp_path):
    """A crash after the rename: the payload is the evidence, record or no
    record update."""
    watch_root, library = watch
    claim_dir = _claim_folder(tmp_path, watch_root, payload={"Berserk Ch. 1.cbz": 101})

    watcher.process_and_move_directory(watch_root)

    assert not claim_dir.exists()
    assert _payloads(library) == [101]


@pytest.mark.parametrize("record", ["missing", "corrupt"])
def test_a_claim_without_a_readable_record_is_preserved_whole_for_review(watch, tmp_path, record):
    watch_root, library = watch
    claim_dir = _claim_folder(tmp_path, watch_root, record=False, payload={"Berserk Ch. 1.cbz": 101})
    if record == "corrupt":
        (claim_dir / claims.RECORD_NAME).write_text("{not json", encoding="utf-8")

    watcher.process_and_move_directory(watch_root)

    preserved = tmp_path / "_review" / claims.UNRECOVERED_DIR / claim_dir.name
    assert _payloads(preserved) == [101]
    assert _payloads(library) == []


def test_a_recreated_source_path_is_a_new_arrival_beside_the_old_claim(watch, tmp_path):
    watch_root, library = watch
    _claim_folder(tmp_path, watch_root, payload={"Berserk Ch. 1.cbz": 101})
    _make_cbz(watch_root / "Berserk" / "Berserk Ch. 2.cbz", 102)

    watcher.process_and_move_directory(watch_root)

    assert _payloads(library) == [101, 102]
    assert claims.claim_dirs(tmp_path / "_processing") == []


def test_a_claim_for_another_watch_root_is_left_to_its_watcher(watch, tmp_path, monkeypatch):
    """Passed over untouched, and let go: its own watcher (here the same
    process, as with watcher roots sharing a processing root) then takes it."""
    watch_root, library = watch
    other_root = tmp_path / "another-root"
    other_root.mkdir()
    claim_dir = _claim_folder(tmp_path, other_root, payload={"Berserk Ch. 1.cbz": 101})

    watcher.process_and_move_directory(watch_root)
    assert _payloads(claim_dir) == [101]

    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(other_root))
    watcher.process_and_move_directory(other_root)
    assert _payloads(library) == [101]


def test_one_claim_has_one_owner(tmp_path):
    claim_dir = tmp_path / "claim"
    claim_dir.mkdir()
    first, second = claims.OwnerLock(claim_dir), claims.OwnerLock(claim_dir)
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


# ---------------------------------------------------------------------------
# Contracts 3 and 5: inventory, routing identity, drop points
# ---------------------------------------------------------------------------

def test_a_mixed_drop_points_loose_archives_and_subfolders_are_claimed_apart(watch, tmp_path):
    watch_root, library = watch
    drop = watch_root / "HentaiNexus"
    _make_cbz(drop / "Loose Book Ch. 1.cbz", 101)
    _make_cbz(drop / "Other" / "Other Ch. 1.cbz", 201)

    watcher.process_and_move_directory(drop)

    assert _payloads(library / "Other") == [201]
    assert _payloads(library) == [101, 201]
    assert drop.is_dir()  # the drop point itself is never taken


def test_a_series_folders_own_subfolders_stay_in_the_watch_folder(watch, tmp_path):
    """Sync history (``.stversions``) is not the arrival's to take."""
    watch_root, library = watch
    series = watch_root / "Berserk"
    _make_cbz(series / "Berserk Ch. 1.cbz", 101)
    history = series / ".stversions"
    history.mkdir()
    (history / "old.txt").write_text("keep me")

    watcher.process_and_move_directory(series)

    assert (history / "old.txt").read_text() == "keep me"
    assert _payloads(library) == [101]


def test_routing_follows_the_recorded_source_after_the_claim(watch, tmp_path, monkeypatch):
    """Routing is decided from the source folder the arrival came under,
    recorded at claim time, not from its processing path."""
    watch_root, library = watch
    special = tmp_path / "manga-library"
    special.mkdir()
    monkeypatch.setattr(watcher, "_routing_rules",
                        [{"match": "source", "pattern": "mangadex*", "dest": "manga"}])
    monkeypatch.setattr(watcher, "_routing_destinations", {"manga": str(special)})
    _make_cbz(watch_root / "MangaDex (ALL)" / "Usogui" / "Usogui Ch. 1.cbz", 101)

    watcher.process_and_move_directory(watch_root / "MangaDex (ALL)")

    assert _payloads(special) == [101]
    assert _payloads(library) == []


# ---------------------------------------------------------------------------
# Contract 4 and 6: dispositions, failed moves, crashes in fresh processes
# ---------------------------------------------------------------------------

def test_a_failed_disposition_move_keeps_the_archive_in_processing(watch, tmp_path, monkeypatch):
    watch_root, library = watch
    arrival = watch_root / "Berserk Ch. 4"
    _make_cbz(arrival / "volvox_Chapter (Colored) (Uncensored).cbz", 400)
    _make_cbz(arrival / "Berserk Ch. 5.cbz", 500)
    real_place = claims.place
    monkeypatch.setattr(claims, "place",
                        lambda *a: (_ for _ in ()).throw(PermissionError("review unreachable")))

    watcher.process_and_move_directory(arrival)

    assert _payloads(tmp_path / "_processing") == [400, 500]  # nothing landed either
    assert _payloads(library) == []
    monkeypatch.setattr(claims, "place", real_place)
    watcher.process_and_move_directory(watch_root)
    assert _payloads(tmp_path / "_review") == [400]
    assert _payloads(library) == [500]


_PASS_RUNNER = r"""
import json, os, shutil, sys
from pathlib import Path
args = json.loads(sys.argv[1])
sys.path.insert(0, args["repo"])
from comic_automation.library import claims
from scripts import cbz_core, cbz_watcher as watcher

root = Path(args["root"])
watcher.WATCH_FOLDER = str(root / "incoming")
watcher._routing_rules = []
watcher._routing_default = str(root / "library")
watcher.POLL_INTERVAL = 0
watcher.PROCESSING_ROOT = root / "_processing"
watcher.REVIEW_ROOT = root / "_review"
watcher.QUARANTINE_ROOT = root / "_quarantine"
watcher.wait_for_file_stable = lambda *a, **k: True
cbz_core.translate_metadata_text = lambda text: (text, None)
crash_at = args.get("crash_at")

def crash_before(module, name):
    def fake(*a, **k):
        os._exit(9)
    setattr(module, name, fake)

if args.get("cross_volume"):
    watcher._same_volume = lambda *a: False
if args.get("ai"):
    watcher.AI_DECENSOR_ENABLED = True
    watcher.destination_is_comix = lambda destination: True
    watcher.archive_completed_ai_backups = lambda *a: None
    count_file = root / "ai-calls.txt"
    def fake_ai(path, destination):
        with open(count_file, "a") as f:
            f.write(path.name + "\n")
        if crash_at == "inside-ai":
            os._exit(9)
        backup = root / "ai-backups" / path.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        return backup
    watcher.run_ai_decensor = fake_ai

if crash_at == "after-claim":
    crash_before(watcher, "_process_work")
elif crash_at == "before-place":
    crash_before(claims, "place")
elif crash_at == "after-ai":
    real_note = watcher._note_ai
    def note_then_crash(claim, name, entry):
        ok = real_note(claim, name, entry)
        if entry and entry.get("done"):
            os._exit(9)
        return ok
    watcher._note_ai = note_then_crash
elif crash_at == "mid-landing":
    real_replace = watcher.os.replace
    def crash_on_publish(src, dst):
        if str(src).endswith(watcher.LANDING_SUFFIX):
            os._exit(9)
        return real_replace(src, dst)
    watcher.os.replace = crash_on_publish
elif crash_at == "before-retire":
    crash_before(watcher, "_retire_claim")

target = Path(args["target"])
watcher.process_and_move_directory(target)
"""


def _run_pass(tmp_path, target: Path, **options) -> int:
    args = {"repo": str(Path(__file__).resolve().parents[1]), "root": str(tmp_path),
            "target": str(target), **options}
    return subprocess.run([sys.executable, "-c", _PASS_RUNNER, json.dumps(args)],
                          capture_output=True, text=True, timeout=300).returncode


def _everything(tmp_path) -> list[int]:
    return _payloads(tmp_path / "incoming", tmp_path / "library", tmp_path / "_processing",
                     tmp_path / "_review", tmp_path / "_quarantine")


@pytest.mark.parametrize("crash_at", ["after-claim", "before-place", "mid-landing", "before-retire"])
def test_a_crash_at_any_stage_is_finished_by_a_fresh_process(tmp_path, crash_at):
    """Alpha 1-3 and Beta 1-2 (a second translation), a refused placeholder
    and a numbered chapter. A real process exit at the named stage, then a
    fresh process: every original survives, and everything ends where it
    should."""
    (tmp_path / "library").mkdir()
    arrival = tmp_path / "incoming" / "Usogui"
    for n in (1, 2, 3):
        _make_cbz(arrival / f"Alpha Ch.{n}.cbz", 100 + n)
    for n in (1, 2):
        _make_cbz(arrival / f"Beta Ch.{n}.cbz", 900 + n)
    originals = _everything(tmp_path)

    assert _run_pass(tmp_path, arrival, crash_at=crash_at,
                     cross_volume=crash_at == "mid-landing") == 9
    assert _everything(tmp_path) == originals
    assert _run_pass(tmp_path, tmp_path / "incoming") == 0

    assert _everything(tmp_path) == originals
    assert _payloads(tmp_path / "library") == [101, 102, 103]
    assert _payloads(tmp_path / "_quarantine") == [901, 902]
    assert claims.claim_dirs(tmp_path / "_processing") == []
    leftovers = [p for p in (tmp_path / "library").rglob("*") if p.is_file()
                 and (p.stat().st_size == 0 or p.name.endswith(watcher.LANDING_SUFFIX))]
    assert leftovers == []


@pytest.mark.parametrize("crash_at", ["after-ai", "inside-ai"])
def test_ai_custody_survives_a_crash_without_repeating_a_rewrite(tmp_path, crash_at):
    """After the rewrite is recorded done, a resume never repeats it. A
    rewrite that started but was never recorded done is of unknown state:
    the archive goes to review rather than being rewritten again."""
    (tmp_path / "library").mkdir()
    arrival = tmp_path / "incoming" / "Berserk"
    _make_cbz(arrival / "Berserk Ch. 1.cbz", 101)

    assert _run_pass(tmp_path, arrival, ai=True, crash_at=crash_at) == 9
    assert _run_pass(tmp_path, tmp_path / "incoming", ai=True) == 0

    calls = (tmp_path / "ai-calls.txt").read_text().splitlines()
    assert calls == ["Berserk Ch. 1.cbz"]  # exactly one rewrite, never a second
    if crash_at == "after-ai":
        assert _payloads(tmp_path / "library") == [101]
    else:
        assert _payloads(tmp_path / "_review") == [101]
    assert claims.claim_dirs(tmp_path / "_processing") == []


# ---------------------------------------------------------------------------
# Retention: quarantine only, by placement date
# ---------------------------------------------------------------------------

def test_quarantine_is_purged_after_30_days_and_nothing_else_is(tmp_path):
    quarantine, review = tmp_path / "q", tmp_path / "r"
    today = dt.date(2026, 11, 9)
    old = _make_cbz(quarantine / "2026-10-09" / "Berserk" / "old.cbz")
    edge = _make_cbz(quarantine / "2026-10-10" / "Berserk" / "edge.cbz")
    hand = _make_cbz(quarantine / "hand-cleared" / "keep.cbz")
    compact = _make_cbz(quarantine / "20261009" / "keep.cbz")  # not a watcher placement
    kept_review = _make_cbz(review / "2026-01-01" / "Berserk" / "keep.cbz")

    removed = claims.purge_quarantine(quarantine, today=today)

    assert removed == [quarantine / "2026-10-09"]
    assert not old.exists() and edge.exists() and hand.exists() and kept_review.exists()
    assert compact.exists()


def test_a_locked_quarantine_folder_does_not_stop_the_purge(tmp_path, monkeypatch):
    """A purge that cannot delete one folder skips it (next start retries)
    and still purges the rest; startup must not fail on it."""
    quarantine = tmp_path / "q"
    locked = _make_cbz(quarantine / "2026-09-01" / "Berserk" / "locked.cbz")
    old = _make_cbz(quarantine / "2026-09-02" / "Berserk" / "old.cbz")
    real_rmtree = claims.shutil.rmtree

    def rmtree(path, *args, **kwargs):
        if Path(path).name == "2026-09-01":
            raise PermissionError("in use")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(claims.shutil, "rmtree", rmtree)
    removed = claims.purge_quarantine(quarantine, today=dt.date(2026, 11, 9))

    assert removed == [quarantine / "2026-09-02"]
    assert locked.exists() and not old.exists()


def test_unsafe_roots_refuse_the_watcher_before_anything_moves(watch, tmp_path, monkeypatch):
    """A processing root inside the watch root would be discovered as an
    arrival (and synced). The watcher refuses to start, with the start-refused
    exit, before it purges, cleans, resumes or discovers anything."""
    watch_root, _library = watch
    _make_cbz(watch_root / "Berserk" / "Berserk Ch. 1.cbz")
    before = _snapshot(watch_root)
    monkeypatch.setattr(watcher, "PROCESSING_ROOT", watch_root / "_processing")
    for name in ("_load_routing", "_load_routing_v2", "_load_series_resolvers"):
        monkeypatch.setattr(watcher, name, lambda *a, **k: None)
    discovered = []

    def discover(*a, **k):  # stop there: a start that got this far was not refused
        discovered.append(a)
        raise _Discovered

    monkeypatch.setattr(watcher, "_discover_startup_directories", discover)
    tracker = SimpleNamespace(stop_requested=lambda: False,
                              readiness_holds=SimpleNamespace(enabled=False))
    args = SimpleNamespace(readiness_stall_minutes=1.0, readiness_hold_root=tmp_path / "holds")

    with pytest.raises(watcher.WatcherInstanceRefused, match="overlaps the watch root"):
        watcher._watch_session(args, watch_root, tracker)

    assert discovered == []
    assert _snapshot(watch_root) == before
    assert not (watch_root / "_processing").exists()


def test_a_late_writer_seen_in_processing_waits_there(watch, tmp_path, monkeypatch):
    """Readiness is probed again inside the claim. A payload that is not
    ready there (a writer still finishing) is not processed or reviewed; it
    waits in processing and lands once it is complete."""
    watch_root, library = watch
    arrival = watch_root / "Berserk"
    _make_cbz(arrival / "Berserk Ch. 1.cbz", 101)
    complete = (arrival / "Berserk Ch. 1.cbz").read_bytes()
    real_claim = claims.claim_directory
    claimed = []

    def claim_mid_write(*args, **kwargs):
        claim = real_claim(*args, **kwargs)
        archive = claim.payload / "Berserk Ch. 1.cbz"
        archive.write_bytes(complete[: len(complete) // 2])
        claimed.append(archive)
        return claim

    monkeypatch.setattr(claims, "claim_directory", claim_mid_write)
    watcher.process_and_move_directory(arrival)

    archive, = claimed
    assert archive.read_bytes() == complete[: len(complete) // 2]  # untouched
    assert _payloads(library) == [] and not list(library.rglob("*.cbz"))
    assert not (tmp_path / "_review").exists()

    archive.write_bytes(complete)  # the writer finishes
    watcher.process_and_move_directory(watch_root)
    assert _payloads(library) == [101]
    assert claims.claim_dirs(tmp_path / "_processing") == []


def test_unrecordable_ai_custody_rewrites_nothing(watch, tmp_path, monkeypatch):
    """AI custody is written before the rewrite. When it cannot be written,
    the archive is not rewritten and the claim waits in processing."""
    watch_root, library = watch
    _make_cbz(watch_root / "Berserk" / "Berserk Ch. 1.cbz", 101)
    monkeypatch.setattr(watcher, "AI_DECENSOR_ENABLED", True)
    monkeypatch.setattr(watcher, "destination_is_comix", lambda destination: True)
    rewrites = []
    monkeypatch.setattr(watcher, "run_ai_decensor",
                        lambda path, destination: rewrites.append(path.name))
    real_write = claims.write_record

    def refuse_custody(claim_dir, record):
        if record.get("ai"):
            raise PermissionError("record not writable")
        return real_write(claim_dir, record)

    monkeypatch.setattr(claims, "write_record", refuse_custody)
    watcher.process_and_move_directory(watch_root / "Berserk")

    assert rewrites == []
    assert _payloads(tmp_path / "_processing") == [101]
    assert _payloads(library) == []


class _Discovered(Exception):
    """Startup reached discovery; the test stops it there."""


def test_startup_cleans_rewrite_leftovers_in_processing_never_in_the_watch_folder(
        watch, tmp_path, monkeypatch):
    """Rewrites happen in claims, so the startup cleanup runs there. The
    synced watch folder is not touched, even where the old cleanup would
    have deleted a leftover (its archive is present beside it)."""
    watch_root, _library = watch
    _make_cbz(watch_root / "Berserk" / "Berserk Ch. 1.cbz")
    _make_cbz(watch_root / "Berserk" / "Berserk Ch. 1.tmp.cbz")
    before = _snapshot(watch_root)
    processing = tmp_path / "_processing"
    _make_cbz(processing / "Berserk Ch. 2.cbz")
    _make_cbz(processing / "Berserk Ch. 2.tmp.cbz")
    for name in ("_load_routing", "_load_routing_v2", "_load_series_resolvers"):
        monkeypatch.setattr(watcher, name, lambda *a, **k: None)

    def discover(*a, **k):
        raise _Discovered

    monkeypatch.setattr(watcher, "_discover_startup_directories", discover)
    tracker = SimpleNamespace(stop_requested=lambda: False,
                              readiness_holds=SimpleNamespace(enabled=False))
    args = SimpleNamespace(readiness_stall_minutes=1.0, readiness_hold_root=tmp_path / "holds")

    with pytest.raises(_Discovered):
        watcher._watch_session(args, watch_root, tracker)

    assert _snapshot(watch_root) == before
    assert (processing / "Berserk Ch. 2.cbz").is_file()
    assert not (processing / "Berserk Ch. 2.tmp.cbz").exists()


def test_a_claim_owned_by_another_watcher_is_left_to_it(watch, tmp_path):
    """A claim whose owner lock is held elsewhere is not processed, resumed
    or retired; it is taken up once the owner lets go."""
    watch_root, library = watch
    arrival = watch_root / "Berserk"
    _make_cbz(arrival / "Berserk Ch. 1.cbz", 101)
    claim = claims.claim_directory(arrival, tmp_path / "_processing",
                                   watcher._claim_context(arrival))
    claim.release()  # its creator lets go, as after a crash
    owner = claims.OwnerLock(claim.dir)
    assert owner.acquire()
    try:
        watcher.process_and_move_directory(watch_root)
        assert _payloads(library) == []
        assert claims.claim_dirs(tmp_path / "_processing") == [claim.dir]
    finally:
        owner.release()
    watcher.process_and_move_directory(watch_root)
    assert _payloads(library) == [101]


def test_a_recorded_disposition_is_carried_out_before_anything_else(watch, tmp_path):
    """A decision written to the record by an earlier run is final: the
    resume moves the archive where it was sent, even if judging it again
    would land it."""
    watch_root, library = watch
    arrival = watch_root / "Berserk"
    _make_cbz(arrival / "Berserk Ch. 1.cbz", 101)
    _make_cbz(arrival / "Berserk Ch. 2.cbz", 102)
    claim = claims.claim_directory(arrival, tmp_path / "_processing",
                                   watcher._claim_context(arrival))
    claim.record["started"] = True
    claim.record["dispositions"] = {
        "Berserk Ch. 2.cbz": {"to": "review", "reason": "recorded decision", "series": "Berserk"}}
    claim.save()
    claim.release()  # an earlier run that recorded the decision, then stopped

    watcher.process_and_move_directory(watch_root)

    assert _payloads(library) == [101]
    assert _payloads(tmp_path / "_review") == [102]
    assert claims.claim_dirs(tmp_path / "_processing") == []


def test_an_unrecordable_decision_moves_nothing(watch, tmp_path, monkeypatch):
    """Decisions are written before any archive moves. If they cannot be,
    the refused archive stays in its claim rather than being moved without
    a record a crash could finish from."""
    watch_root, library = watch
    arrival = watch_root / "Berserk Ch. 4"
    _make_cbz(arrival / "volvox_Chapter (Colored) (Uncensored).cbz", 400)
    real_write = claims.write_record

    def refuse_decisions(claim_dir, record):
        if record.get("dispositions"):
            raise PermissionError("record not writable")
        return real_write(claim_dir, record)

    monkeypatch.setattr(claims, "write_record", refuse_decisions)
    watcher.process_and_move_directory(arrival)

    assert _payloads(tmp_path / "_processing") == [400]
    assert not (tmp_path / "_review").exists()
    assert _payloads(library) == []


# ---------------------------------------------------------------------------
# PR #177 review: claim lifetime, and AI custody across naming
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["directory", "files"])
def test_resume_cannot_retire_a_claim_its_creator_is_still_filling(watch, tmp_path,
                                                                  monkeypatch, kind):
    """Codex's #177 probe, adapted: the creator has published the record and
    is paused before its rename; a resume in another thread sees a claim
    with no arrival file. It must not retire it: the creator owns it."""
    watch_root, _library = watch
    arrival = watch_root / "Berserk"
    book = _make_cbz(arrival / "Berserk Ch.1.cbz", 101)
    context = watcher._claim_context(arrival)
    source = arrival if kind == "directory" else book
    record_ready, allow_rename = threading.Event(), threading.Event()
    real_rename = claims.os.rename

    def pause_rename(src, dst, *args, **kwargs):
        if Path(src) == source:
            record_ready.set()
            assert allow_rename.wait(timeout=10)
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(claims.os, "rename", pause_rename)

    def create():
        if kind == "directory":
            return claims.claim_directory(arrival, watcher.PROCESSING_ROOT, context)
        return claims.claim_files(arrival, [book], watcher.PROCESSING_ROOT, context, kind="files")

    with ThreadPoolExecutor(max_workers=2) as pool:
        creating = pool.submit(create)
        try:
            assert record_ready.wait(timeout=10)
            pool.submit(watcher._resume_claims).result(timeout=30)
        finally:
            allow_rename.set()
        claim = creating.result(timeout=10)

    assert claim is not None
    claim.release()
    assert _payloads(tmp_path) == [101]
    assert _payloads(claim.dir) == [101]


def test_a_claim_retired_before_its_creator_owns_it_leaves_the_arrival(watch, tmp_path,
                                                                      monkeypatch):
    """The publication gap: the creator has made the claim folder but not yet
    taken its lock. A resume may retire that empty folder; the creator then
    cannot own it, so the claim fails and nothing has moved."""
    watch_root, _library = watch
    arrival = watch_root / "Berserk"
    _make_cbz(arrival / "Berserk Ch.1.cbz", 101)
    before = _snapshot(watch_root)
    context = watcher._claim_context(arrival)
    folder_made, allow_lock = threading.Event(), threading.Event()
    creator = {}
    real_acquire = claims.OwnerLock.acquire

    def paused_acquire(self):
        if threading.get_ident() == creator.get("ident") and not folder_made.is_set():
            folder_made.set()
            assert allow_lock.wait(timeout=10)
        return real_acquire(self)

    monkeypatch.setattr(claims.OwnerLock, "acquire", paused_acquire)

    def create():
        creator["ident"] = threading.get_ident()
        return claims.claim_directory(arrival, watcher.PROCESSING_ROOT, context)

    with ThreadPoolExecutor(max_workers=2) as pool:
        creating = pool.submit(create)
        try:
            assert folder_made.wait(timeout=10)
            pool.submit(watcher._resume_claims).result(timeout=30)
            assert claims.claim_dirs(watcher.PROCESSING_ROOT) == []  # retired while unowned
        finally:
            allow_lock.set()
        assert creating.result(timeout=10) is None

    assert _snapshot(watch_root) == before
    assert claims.claim_dirs(watcher.PROCESSING_ROOT) == []


def _ai_naming_setup(tmp_path, monkeypatch, library):
    """Chapter naming plus an AI rewrite that changes the payload, as in
    Codex's #177 probe; returns the list of rewritten names."""
    registry = tmp_path / "number-styles.json"
    registry.write_text(json.dumps({"version": 1, "roots": [{"path": str(library), "style": "chapter"}]}))
    monkeypatch.setattr(watcher, "_number_style_registry", NumberStyleRegistry.load(registry))
    monkeypatch.setattr(watcher, "AI_DECENSOR_ENABLED", True)
    monkeypatch.setattr(watcher, "AI_DECENSOR_REPROCESS", True)
    monkeypatch.setattr(watcher, "destination_is_comix", lambda *a: True)
    monkeypatch.setattr(watcher, "archive_completed_ai_backups", lambda *a: None)
    calls = []

    def rewrite(path, destination):
        calls.append(path.name)
        backup = tmp_path / "ai-backups" / str(len(calls)) / path.name
        backup.parent.mkdir(parents=True)
        shutil.copy2(path, backup)
        rebuilt = path.with_suffix(".ai-tmp")
        with zipfile.ZipFile(path) as src, zipfile.ZipFile(rebuilt, "w") as dst:
            for info in src.infolist():
                data = src.read(info)
                dst.writestr(info, data + b"\1" if info.filename == "page1.jpg" else data)
        rebuilt.replace(path)
        return backup

    monkeypatch.setattr(watcher, "run_ai_decensor", rewrite)
    return calls


def test_completed_ai_follows_its_archive_through_naming(watch, tmp_path, monkeypatch):
    """Codex's #177 probe: AI-processed as 'Group Ch.1.cbz', named
    'Berserk ch. 1.cbz', landing refused once. The resume finds the custody
    under the new name and never rewrites it again."""
    watch_root, library = watch
    calls = _ai_naming_setup(tmp_path, monkeypatch, library)
    _make_cbz(watch_root / "Berserk" / "Group Ch.1.cbz", 101)
    real_move = watcher._move_cbz_dir
    monkeypatch.setattr(watcher, "_move_cbz_dir", lambda *a, **k: None)
    watcher.process_and_move_directory(watch_root / "Berserk")
    assert calls == ["Group Ch.1.cbz"]
    (claim_dir,) = claims.claim_dirs(watcher.PROCESSING_ROOT)
    record = claims.read_record(claim_dir)
    assert list(record["ai"]) == ["Berserk ch. 1.cbz"] and "ai_naming" not in record

    monkeypatch.setattr(watcher, "_move_cbz_dir", real_move)
    watcher.process_and_move_directory(watch_root)
    assert calls == ["Group Ch.1.cbz"]
    assert _payloads(library) == [102]  # the rewritten book, rewritten once


def test_a_rename_whose_custody_was_never_recorded_goes_to_review(watch, tmp_path, monkeypatch):
    """A crash after naming renamed the archive but before the custody was
    re-keyed: the record still carries the mark. The resume cannot tell
    whether the archive under its new name was rewritten, so it goes to
    review; it is never rewritten a second time."""
    watch_root, library = watch
    calls = _ai_naming_setup(tmp_path, monkeypatch, library)
    _make_cbz(watch_root / "Berserk" / "Group Ch.1.cbz", 101)
    real_move, real_rekey = watcher._move_cbz_dir, watcher._rekey_ai_custody
    monkeypatch.setattr(watcher, "_move_cbz_dir", lambda *a, **k: None)
    monkeypatch.setattr(watcher, "_rekey_ai_custody", lambda *a, **k: None)  # "crashed"
    watcher.process_and_move_directory(watch_root / "Berserk")
    (claim_dir,) = claims.claim_dirs(watcher.PROCESSING_ROOT)
    record = claims.read_record(claim_dir)
    assert record["ai_naming"] and list(record["ai"]) == ["Group Ch.1.cbz"]

    monkeypatch.setattr(watcher, "_move_cbz_dir", real_move)
    monkeypatch.setattr(watcher, "_rekey_ai_custody", real_rekey)
    watcher.process_and_move_directory(watch_root)
    assert calls == ["Group Ch.1.cbz"]
    assert _payloads(library) == []
    assert _payloads(tmp_path / "_review") == [102]


def test_retirement_rechecks_emptiness_once_it_owns_the_claim(watch, tmp_path, monkeypatch):
    """A resume saw the claim empty while its creator owned it, and waits
    for the lock. When it gets the lock the creator has filled the claim and
    let go; the re-check under ownership keeps the payload."""
    watch_root, _library = watch
    arrival = watch_root / "Berserk"
    _make_cbz(arrival / "Berserk Ch.1.cbz", 101)
    context = watcher._claim_context(arrival)
    record_ready, allow_rename = threading.Event(), threading.Event()
    retiree_waiting, allow_retiree = threading.Event(), threading.Event()
    retiree = {}
    real_rename, real_acquire = claims.os.rename, claims.OwnerLock.acquire

    def pause_rename(src, dst, *args, **kwargs):
        if Path(src) == arrival:
            record_ready.set()
            assert allow_rename.wait(timeout=10)
        return real_rename(src, dst, *args, **kwargs)

    def waiting_acquire(self):
        if threading.get_ident() == retiree.get("ident") and not retiree_waiting.is_set():
            retiree_waiting.set()
            assert allow_retiree.wait(timeout=10)
        return real_acquire(self)

    monkeypatch.setattr(claims.os, "rename", pause_rename)
    monkeypatch.setattr(claims.OwnerLock, "acquire", waiting_acquire)

    def retire_seen_empty(claim_dir):
        retiree["ident"] = threading.get_ident()
        assert not claims.has_arrival_files(claim_dir)  # what the resume saw
        return claims.retire(claim_dir)

    with ThreadPoolExecutor(max_workers=2) as pool:
        creating = pool.submit(claims.claim_directory, arrival, watcher.PROCESSING_ROOT, context)
        try:
            assert record_ready.wait(timeout=10)
            (claim_dir,) = claims.claim_dirs(watcher.PROCESSING_ROOT)
            retiring = pool.submit(retire_seen_empty, claim_dir)
            assert retiree_waiting.wait(timeout=10)
            allow_rename.set()
            claim = creating.result(timeout=10)
            claim.release()  # filled, then let go
        finally:
            allow_rename.set()
            allow_retiree.set()
        assert retiring.result(timeout=10) is False

    assert _payloads(claim.dir) == [101]


def test_a_creator_that_owns_a_just_released_folder_keeps_it(watch, tmp_path, monkeypatch):
    """A resume retired the empty folder of a claim being created and let go
    of the lock; before it removed the folder the creator took the lock and
    filled the claim. The removal after the release is only of the lock file
    and the empty folder, so it fails and the payload stays."""
    watch_root, _library = watch
    arrival = watch_root / "Berserk"
    _make_cbz(arrival / "Berserk Ch.1.cbz", 101)
    context = watcher._claim_context(arrival)
    folder_made, allow_lock = threading.Event(), threading.Event()
    released, allow_removal = threading.Event(), threading.Event()
    creator, retiree = {}, {}
    real_acquire, real_release = claims.OwnerLock.acquire, claims.OwnerLock.release

    def paused_acquire(self):
        if threading.get_ident() == creator.get("ident") and not folder_made.is_set():
            folder_made.set()
            assert allow_lock.wait(timeout=10)
        return real_acquire(self)

    def paused_release(self):
        real_release(self)
        if threading.get_ident() == retiree.get("ident") and not released.is_set():
            released.set()
            assert allow_removal.wait(timeout=10)

    monkeypatch.setattr(claims.OwnerLock, "acquire", paused_acquire)
    monkeypatch.setattr(claims.OwnerLock, "release", paused_release)

    def create():
        creator["ident"] = threading.get_ident()
        return claims.claim_directory(arrival, watcher.PROCESSING_ROOT, context)

    def retire_unowned():
        retiree["ident"] = threading.get_ident()
        (claim_dir,) = claims.claim_dirs(watcher.PROCESSING_ROOT)
        return claims.retire(claim_dir)

    with ThreadPoolExecutor(max_workers=2) as pool:
        creating = pool.submit(create)
        try:
            assert folder_made.wait(timeout=10)
            retiring = pool.submit(retire_unowned)
            assert released.wait(timeout=10)
            allow_lock.set()
            claim = creating.result(timeout=10)
            assert claim is not None
        finally:
            allow_lock.set()
            allow_removal.set()
        assert retiring.result(timeout=10) is False

    claim.release()
    assert _payloads(claim.dir) == [101]


def _recorded_ai_claim(watch_root, tmp_path, names, done):
    """A claim an earlier run left: archives *names*, custody for *done*
    (each with a real backup), owner released."""
    arrival = watch_root / "Berserk"
    for i, name in enumerate(names):
        _make_cbz(arrival / name, 101 + i)
    claim = claims.claim_directory(arrival, watcher.PROCESSING_ROOT,
                                   watcher._claim_context(arrival))
    custody = {}
    for name in done:
        backup = tmp_path / "ai-backups" / "earlier" / name
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(claim.payload / name, backup)
        custody[name] = {"started": True, "done": True, "backup": str(backup)}
    claim.record.update(started=True, ai=custody)
    claim.save()
    claim.release()
    return claim


def test_custody_follows_a_name_the_resume_cleans(watch, tmp_path, monkeypatch):
    """On a resume ``process_cbz_file`` may clean an archive's name again
    ('Group_Ch.1.cbz' -> 'Group Ch.1.cbz'). The custody recorded under the
    name it had is found and moved with it, so neither this resume nor one
    after a landing that fails again rewrites the archive twice."""
    watch_root, library = watch
    _recorded_ai_claim(watch_root, tmp_path, ["Group_Ch.1.cbz"], ["Group_Ch.1.cbz"])
    monkeypatch.setattr(watcher, "AI_DECENSOR_ENABLED", True)
    monkeypatch.setattr(watcher, "destination_is_comix", lambda *a: True)
    monkeypatch.setattr(watcher, "archive_completed_ai_backups", lambda *a: None)
    calls = []
    monkeypatch.setattr(watcher, "run_ai_decensor",
                        lambda path, destination: calls.append(path.name))
    real_move = watcher._move_cbz_dir
    monkeypatch.setattr(watcher, "_move_cbz_dir", lambda *a, **k: None)

    watcher.process_and_move_directory(watch_root)
    (claim_dir,) = claims.claim_dirs(watcher.PROCESSING_ROOT)
    assert list(claims.read_record(claim_dir)["ai"]) == ["Group Ch.1.cbz"]
    monkeypatch.setattr(watcher, "_move_cbz_dir", real_move)
    watcher.process_and_move_directory(watch_root)

    assert calls == []
    assert _payloads(library) == [101]
    assert claims.claim_dirs(watcher.PROCESSING_ROOT) == []


def test_a_failed_ai_pass_clears_the_mark_with_the_custody(watch, tmp_path, monkeypatch):
    """A resumed claim is marked (it holds custody) and then an AI rewrite
    fails: the originals are restored and the custody cleared. The mark goes
    with it, so the retry rewrites both archives and lands them instead of
    reviewing them as unresolved."""
    watch_root, library = watch
    _recorded_ai_claim(watch_root, tmp_path, ["Berserk Ch. 1.cbz", "Berserk Ch. 2.cbz"],
                       ["Berserk Ch. 1.cbz"])
    monkeypatch.setattr(watcher, "AI_DECENSOR_ENABLED", True)
    monkeypatch.setattr(watcher, "destination_is_comix", lambda *a: True)
    monkeypatch.setattr(watcher, "archive_completed_ai_backups", lambda *a: None)
    failing = {"on": True}
    calls = []

    def rewrite(path, destination):
        calls.append(path.name)
        if failing["on"]:
            raise watcher.AiDecensorError("injected")
        backup = tmp_path / "ai-backups" / "retry" / path.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        return backup

    monkeypatch.setattr(watcher, "run_ai_decensor", rewrite)
    watcher.process_and_move_directory(watch_root)
    assert calls == ["Berserk Ch. 2.cbz"]
    (claim_dir,) = claims.claim_dirs(watcher.PROCESSING_ROOT)
    record = claims.read_record(claim_dir)
    assert not record.get("ai") and "ai_naming" not in record

    failing["on"] = False
    watcher.process_and_move_directory(watch_root)
    assert calls == ["Berserk Ch. 2.cbz", "Berserk Ch. 1.cbz", "Berserk Ch. 2.cbz"]
    assert _payloads(library) == [101, 102]
    assert not (tmp_path / "_review").exists()


# PR #177 review round 2: a claim's record is read only under its ownership

def test_a_waiting_resumer_never_saves_a_record_it_read_before_owning(watch, tmp_path,
                                                                     monkeypatch):
    """Codex's round-2 probe, adapted. Pass A reads an unfinished claim's
    record (no AI custody yet) and is held there. Pass B, for a disjoint
    sibling folder, takes the claim, decensors the archive, records the
    custody, cannot land and lets go. A must not then act on, or save, what
    it read before it owned the claim: the archive is rewritten once."""
    watch_root, library = watch
    calls = _ai_naming_setup(tmp_path, monkeypatch, library)
    arrival = watch_root / "Berserk"
    _make_cbz(arrival / "Berserk ch. 1.cbz", 101)
    claim = claims.claim_directory(arrival, watcher.PROCESSING_ROOT,
                                   watcher._claim_context(arrival))
    claim.release()  # an earlier run claimed it, then stopped
    (watch_root / "callback-A").mkdir()
    (watch_root / "callback-B").mkdir()
    read_old, allow_old = threading.Event(), threading.Event()
    lagger = {}
    real_read = claims.read_record

    def paused_read(folder):
        record = real_read(folder)
        if threading.get_ident() == lagger.get("ident") and not read_old.is_set():
            read_old.set()
            assert allow_old.wait(timeout=10)
        return record

    monkeypatch.setattr(claims, "read_record", paused_read)
    monkeypatch.setattr(watcher, "_move_cbz_dir", lambda *a, **k: None)

    def stale_pass():
        lagger["ident"] = threading.get_ident()
        watcher.process_and_move_directory(watch_root / "callback-A")

    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(stale_pass)
        try:
            # A is held with the record it read: before owning the claim at
            # b979fd6 (B then takes it), while owning it now (B skips it).
            assert read_old.wait(timeout=10)
            watcher.process_and_move_directory(watch_root / "callback-B")
        finally:
            allow_old.set()
        waiting.result(timeout=30)

    assert len(calls) == 1, f"a stale record repeated the AI rewrite: {calls}"
    assert _payloads(claim.dir) == [102]
    custody = real_read(claim.dir)["ai"]
    assert [entry.get("done") for entry in custody.values()] == [True]


def test_processing_refuses_a_claim_that_is_not_owned(watch, tmp_path):
    """Every claim reaches processing owned (by its creator or the resume
    that read it under the lock). An unowned claim's record may be stale,
    so it is refused rather than locked and trusted."""
    watch_root, _library = watch
    arrival = watch_root / "Berserk"
    _make_cbz(arrival / "Berserk ch. 1.cbz", 101)
    claim = claims.claim_directory(arrival, watcher.PROCESSING_ROOT,
                                   watcher._claim_context(arrival))
    claim.release()
    with pytest.raises(ValueError, match="without its ownership"):
        next(watcher._claim_item(claim, resumed=True))
    assert _payloads(claim.dir) == [101]
