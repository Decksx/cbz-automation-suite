"""Real stalled ZIPs and rename/receipt boundaries on temporary Windows paths."""

from __future__ import annotations

import errno
import io
import json
import os
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from comic_automation.library import readiness_hold as rh
from scripts import cbz_watcher as w

pytestmark = pytest.mark.skipif(os.name != "nt", reason="automatic holding uses Windows no-replace rename")


def _zip_bytes() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        for index in range(7):
            archive.writestr(zipfile.ZipInfo(f"{index}.jpg", (2026, 10, 25, 0, 0, 0)),
                             bytes([index + 1]) * 1000)
    return stream.getvalue()


class Clock:
    """A monotonic clock advanced explicitly; no timer sleeps in these tests."""
    value = 0.0

    def __call__(self):
        return self.value


@pytest.fixture
def env(tmp_path, monkeypatch):
    incoming, holding = tmp_path / "incoming", tmp_path / "holding"
    folder = incoming / "Lookism"
    folder.mkdir(parents=True)
    holding.mkdir()
    bad = folder / "Lookism part1.cbz"
    # Model extracting only part 1 of a seven-part RAR: a stable prefix of
    # the ZIP, with local file data but no central directory or EOCD.
    bad.write_bytes(_zip_bytes()[:len(_zip_bytes()) // 7])
    assert bad.read_bytes().startswith(b"PK\x03\x04")
    assert b"PK\x05\x06" not in bad.read_bytes()
    clock = Clock()
    monkeypatch.setattr(w.threading, "Timer", lambda *a, **k: Mock())
    probe = w.probe_cbz_zip_readiness
    monkeypatch.setattr(w, "probe_cbz_zip_readiness", lambda path, **kwargs: probe(path, settle_interval=0))
    normal_process = w.process_and_move_directory
    process = Mock()
    monkeypatch.setattr(w, "process_and_move_directory", process)
    tracker = w.DirectorySettleTracker(watch_root=incoming, readiness_hold_root=holding, clock=clock)
    return SimpleNamespace(incoming=incoming, holding=holding, folder=folder, bad=bad,
                           clock=clock, tracker=tracker, process=process, normal_process=normal_process)


def _gate(e):
    w._process_directory_when_ready(e.folder, e.tracker)


def _expire(e):
    _gate(e)
    e.clock.value += 1800
    _gate(e)


def _payloads(e):
    return list(e.holding.glob("readiness-*/*.cbz"))


def _reason(e, monkeypatch, reason):
    entry = w.CbzReadinessEntry(e.bad, w.ZipReadiness.RETRY_LATER, reason, None, None)
    result = w.DirectoryReadinessResult(w.ZipReadiness.RETRY_LATER, "not_all_ready", (entry,), 1, 0, 0)
    monkeypatch.setattr(w, "probe_cbz_directory_readiness", lambda paths: result)
    monkeypatch.setattr(w, "probe_cbz_zip_readiness", lambda path: SimpleNamespace(
        status=w.ZipReadiness.RETRY_LATER, reason=reason))


def test_stable_lookism_prefix_holds_at_boundary_with_bytes_and_sidecar(env, caplog):
    e = env
    original = e.bad.read_bytes()
    stamp = e.bad.stat().st_mtime_ns
    ready = e.folder / "complete.cbz"
    ready.write_bytes(_zip_bytes())
    _gate(e)
    e.clock.value = 1799.999
    _gate(e)
    assert e.bad.read_bytes() == original and e.bad.stat().st_mtime_ns == stamp
    assert not _payloads(e)
    e.process.assert_not_called()
    e.clock.value = 1800
    with caplog.at_level("WARNING", logger=w.log.name):
        _gate(e)
    (held,) = _payloads(e)
    assert not e.bad.exists() and held.read_bytes() == original
    assert held.name == e.bad.name and held.stat().st_mtime_ns == stamp
    receipt = json.loads(held.with_name(held.name + ".held.json").read_text(encoding="utf-8"))
    assert receipt["source_path"] == str(e.bad)
    assert receipt["destination_path"] == str(held)
    assert receipt["observed_elapsed_seconds"] == 1800
    assert receipt["reason"] == "incomplete_central_directory"
    assert receipt["watcher_version"] == w.WATCHER_SOURCE_VERSION
    assert receipt["watcher_version"].startswith("sha256:")
    assert receipt["written_before_rename"] is True
    assert ready.read_bytes() == _zip_bytes()
    e.process.assert_called_once_with(e.folder)
    assert "Readiness held:" in caplog.text and str(held) in caplog.text


@pytest.mark.parametrize("disabled", ["no-root", "zero-minutes"])
def test_holds_are_disabled_without_explicit_opt_in(env, disabled):
    e = env
    e.tracker = w.DirectorySettleTracker(
        watch_root=e.incoming, readiness_hold_root=None if disabled == "no-root" else e.holding,
        readiness_stall_minutes=0 if disabled == "zero-minutes" else 30, clock=e.clock,
    )
    assert not e.tracker.readiness_holds.enabled
    _expire(e)
    assert e.bad.exists() and list(e.holding.iterdir()) == []


def test_events_and_retries_do_not_reset_inactivity(env):
    e = env
    _gate(e)
    for moment in (300, 900, 1799):
        e.clock.value = moment
        e.tracker.notify(e.folder)
        _gate(e)
    e.clock.value = 1800
    _gate(e)
    assert len(_payloads(e)) == 1


@pytest.mark.parametrize("progress", ["size", "mtime", "replacement"])
def test_real_observed_progress_restarts_only_that_archives_budget(env, progress):
    e = env
    _gate(e)
    e.clock.value = 1700
    if progress == "size":
        e.bad.write_bytes(e.bad.read_bytes() + b"growth")
    elif progress == "mtime":
        old = e.bad.stat()
        os.utime(e.bad, ns=(old.st_atime_ns, old.st_mtime_ns + 1_000_000_000))
    else:
        other = e.bad.with_suffix(".replacement")
        other.write_bytes(e.bad.read_bytes())
        os.replace(other, e.bad)
    _gate(e)
    e.clock.value = 1800
    _gate(e)
    assert e.bad.exists() and not _payloads(e)
    e.clock.value = 3500
    _gate(e)
    assert len(_payloads(e)) == 1


def test_restart_starts_a_fresh_budget(env):
    e = env
    _gate(e)
    e.clock.value = 1800
    e.tracker = w.DirectorySettleTracker(watch_root=e.incoming, readiness_hold_root=e.holding, clock=e.clock)
    _gate(e)
    assert e.bad.exists() and not _payloads(e)
    e.clock.value = 3600
    _gate(e)
    assert len(_payloads(e)) == 1


@pytest.mark.parametrize("reason", ["unknown_future_reason", "sharing_violation", "stat_error", "transient_io_error"])
def test_expired_unlisted_reason_never_moves(env, monkeypatch, caplog, reason):
    e = env
    _reason(e, monkeypatch, reason)
    with caplog.at_level("WARNING", logger=w.log.name):
        _expire(e)
    assert e.bad.exists() and not _payloads(e)
    assert "expired but blocked" in caplog.text and reason in caplog.text


@pytest.mark.parametrize("fresh", [None, "sharing_violation", "unknown_future_reason"])
def test_fresh_probe_must_still_allow_the_hold(env, monkeypatch, fresh):
    e = env
    _gate(e)
    e.clock.value = 1800
    # Directory observation remains genuinely incomplete; the second probe
    # models becoming ready/locked/unknown before a move can be authorized.
    original = w.probe_cbz_directory_readiness
    result = original([e.bad])
    monkeypatch.setattr(w, "probe_cbz_directory_readiness", lambda paths: result)
    monkeypatch.setattr(w, "probe_cbz_zip_readiness", lambda path: SimpleNamespace(
        status=w.ZipReadiness.READY if fresh is None else w.ZipReadiness.RETRY_LATER,
        reason="ok" if fresh is None else fresh))
    _gate(e)
    assert e.bad.exists() and list(e.holding.iterdir()) == []


def test_new_event_during_fresh_probe_invalidates_the_move(env, monkeypatch):
    e = env
    _gate(e)
    result = w.probe_cbz_directory_readiness([e.bad])
    monkeypatch.setattr(w, "probe_cbz_directory_readiness", lambda paths: result)
    probe = w.probe_cbz_zip_readiness

    def notify(path):
        e.tracker.notify(e.folder)
        return probe(path)

    monkeypatch.setattr(w, "probe_cbz_zip_readiness", notify)
    e.clock.value = 1800
    _gate(e)
    assert e.bad.exists() and list(e.holding.iterdir()) == []
    assert e.folder in e.tracker._timers


def test_change_during_fresh_probe_cannot_move(env, monkeypatch):
    e = env
    _gate(e)
    result = w.probe_cbz_directory_readiness([e.bad])
    monkeypatch.setattr(w, "probe_cbz_directory_readiness", lambda paths: result)
    probe = w.probe_cbz_zip_readiness

    def grow(path):
        result = probe(path)
        path.write_bytes(path.read_bytes() + b"new data")
        return result

    monkeypatch.setattr(w, "probe_cbz_zip_readiness", grow)
    e.clock.value = 1800
    _gate(e)
    assert e.bad.exists() and not _payloads(e)


def test_change_during_receipt_io_cannot_move(env, monkeypatch):
    e = env
    original = e.tracker.readiness_holds._attempt

    def grow(*args):
        destination = original(*args)
        e.bad.write_bytes(e.bad.read_bytes() + b"late data")
        return destination

    monkeypatch.setattr(e.tracker.readiness_holds, "_attempt", grow)
    _expire(e)
    assert e.bad.exists() and not _payloads(e)
    assert len(list(e.holding.glob("readiness-*/*.held.json"))) == 1


@pytest.mark.parametrize("overlap", ["parent", "child"])
def test_processing_overlap_never_authorizes_a_hold(env, overlap):
    e = env
    _gate(e)
    occupied = e.incoming if overlap == "parent" else e.folder / "child"
    with w._processing_dirs_lock:
        w._processing_dirs.add(occupied)
    try:
        e.clock.value = 1800
        _gate(e)
        assert e.bad.exists() and list(e.holding.iterdir()) == []
    finally:
        with w._processing_dirs_lock:
            w._processing_dirs.discard(occupied)


def test_rename_owns_processing_exclusion_and_generation_lock(env, monkeypatch):
    e = env
    original = rh._rename_no_replace

    def checked(source, destination):
        assert e.folder in w._processing_dirs
        acquired = e.tracker._lock.acquire(blocking=False)
        if acquired:
            e.tracker._lock.release()
        assert not acquired, "notify must not interleave the final generation check and move"
        original(source, destination)

    monkeypatch.setattr(rh, "_rename_no_replace", checked)
    _expire(e)
    assert len(_payloads(e)) == 1
    assert e.folder not in w._processing_dirs


def test_occupied_destination_preserves_both_files(env, monkeypatch):
    e = env
    original = rh._rename_no_replace

    def occupied(source, destination):
        destination.write_bytes(b"foreign occupant")
        original(source, destination)

    monkeypatch.setattr(rh, "_rename_no_replace", occupied)
    before = e.bad.read_bytes()
    _expire(e)
    assert e.bad.read_bytes() == before
    (foreign,) = _payloads(e)
    assert foreign.read_bytes() == b"foreign occupant"
    e.process.assert_not_called()


def test_failed_rename_has_no_copy_fallback_and_reuses_its_intent(env, monkeypatch):
    e = env
    before = e.bad.read_bytes()
    monkeypatch.setattr(rh.os, "rename", Mock(side_effect=OSError(errno.EXDEV, "fixture cross-volume refusal")))
    _expire(e)
    _gate(e)
    assert e.bad.read_bytes() == before and not _payloads(e)
    assert len(list(e.holding.iterdir())) == 1
    assert len(list(e.holding.glob("readiness-*/*.held.json"))) == 1
    e.process.assert_not_called()


def test_sidecar_failure_prevents_the_rename(env, monkeypatch):
    e = env
    original = Path.open

    def fail(path, *args, **kwargs):
        if path.name.endswith(".held.json"):
            raise PermissionError("fixture receipt write denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail)
    _expire(e)
    assert e.bad.exists() and not _payloads(e)


@pytest.mark.parametrize("failure", ["open", "write", "fsync"])
def test_receipt_failures_reuse_one_attempt_and_recover_only_after_fsync(env, monkeypatch, failure):
    """Persistent receipt errors must not create an orphan every five seconds."""
    e = env
    before = e.bad.read_bytes()
    with monkeypatch.context() as fault:
        if failure == "open":
            original = Path.open

            def fail_open(path, *args, **kwargs):
                if path.name.endswith(".held.json"):
                    raise PermissionError("fixture receipt open denied")
                return original(path, *args, **kwargs)

            fault.setattr(Path, "open", fail_open)
        elif failure == "write":
            def partial(document, stream, **kwargs):
                stream.write("{partial")
                raise OSError("fixture interrupted receipt write")

            fault.setattr(rh.json, "dump", partial)
        else:
            fault.setattr(rh.os, "fsync", Mock(side_effect=OSError("fixture receipt fsync denied")))
        _expire(e)
        _gate(e)
        _gate(e)
        assert len(list(e.holding.iterdir())) == 1
        assert e.bad.read_bytes() == before and not _payloads(e)
        e.process.assert_not_called()
    # A transient failure can recover, but the incomplete receipt must first
    # be rewritten and fsynced. The completed receipt retains first-attempt time.
    e.clock.value = 1810
    _gate(e)
    (held,) = _payloads(e)
    assert held.read_bytes() == before and not e.bad.exists()
    assert len(list(e.holding.iterdir())) == 1
    receipt = json.loads(held.with_name(held.name + ".held.json").read_text(encoding="utf-8"))
    assert receipt["observed_elapsed_seconds"] == 1800


def test_replaced_partial_receipt_is_never_truncated_or_adopted(env, monkeypatch):
    e = env

    def partial(document, stream, **kwargs):
        stream.write("{partial")
        raise OSError("fixture interrupted receipt write")

    with monkeypatch.context() as fault:
        fault.setattr(rh.json, "dump", partial)
        _expire(e)
    (receipt,) = list(e.holding.glob("readiness-*/*.held.json"))
    receipt.rename(receipt.with_name("previous-partial.json"))
    receipt.write_bytes(b"foreign receipt occupant")
    _gate(e)
    assert e.bad.exists() and not _payloads(e)
    assert receipt.read_bytes() == b"foreign receipt occupant"
    assert len(list(e.holding.iterdir())) == 1


def test_missing_completed_receipt_cannot_authorize_a_later_retry(env, monkeypatch):
    e = env
    with monkeypatch.context() as fault:
        fault.setattr(rh, "_rename_no_replace", Mock(side_effect=PermissionError("fixture move denied")))
        _expire(e)
    (receipt,) = list(e.holding.glob("readiness-*/*.held.json"))
    receipt.unlink()
    _gate(e)
    assert e.bad.exists() and not _payloads(e)


def test_operator_intent_is_complete_and_fsynced_before_rename(env, monkeypatch):
    e = env
    events = []
    sync, rename = rh.os.fsync, rh._rename_no_replace

    def flushed(fd):
        sync(fd)
        events.append("fsync")

    def move(source, destination):
        receipt = destination.with_name(destination.name + ".held.json")
        assert json.loads(receipt.read_text(encoding="utf-8"))["source_path"] == str(source)
        assert events == ["fsync"]
        rename(source, destination)

    monkeypatch.setattr(rh.os, "fsync", flushed)
    monkeypatch.setattr(rh, "_rename_no_replace", move)
    _expire(e)
    assert len(_payloads(e)) == 1


def test_holding_root_replacement_during_receipt_io_refuses(env, monkeypatch):
    e = env
    original = e.tracker.readiness_holds._attempt

    def replace(*args):
        destination = original(*args)
        e.holding.rename(e.holding.with_name("old-holding"))
        e.holding.mkdir()
        destination.parent.mkdir()
        return destination

    monkeypatch.setattr(e.tracker.readiness_holds, "_attempt", replace)
    _expire(e)
    assert e.bad.exists() and not _payloads(e)
    assert not list(e.holding.with_name("old-holding").glob("readiness-*/*.cbz"))


@pytest.mark.parametrize("invalid", ["same", "inside", "parent", "missing", "file"])
def test_invalid_holding_roots_refuse_before_startup(env, invalid):
    e = env
    choices = {"same": e.incoming, "inside": e.folder, "parent": e.incoming.parent,
               "missing": e.holding / "missing", "file": e.bad}
    with pytest.raises((OSError, ValueError)):
        w.DirectorySettleTracker(watch_root=e.incoming, readiness_hold_root=choices[invalid])


def test_other_volume_holding_root_refuses(env, monkeypatch):
    e = env
    original = rh._plain

    def other_volume(path, **kwargs):
        info = original(path, **kwargs)
        if path == e.holding:
            return SimpleNamespace(st_dev=info.st_dev + 1, st_ino=info.st_ino)
        return info

    monkeypatch.setattr(rh, "_plain", other_volume)
    with pytest.raises(ValueError, match="same volume"):
        w.DirectorySettleTracker(watch_root=e.incoming, readiness_hold_root=e.holding)


def test_holding_root_must_be_actually_writable(env, monkeypatch):
    e = env
    monkeypatch.setattr(rh.tempfile, "TemporaryFile", Mock(side_effect=PermissionError("fixture unwritable")))
    with pytest.raises(PermissionError):
        w.DirectorySettleTracker(watch_root=e.incoming, readiness_hold_root=e.holding)


@pytest.mark.parametrize("which", ["holding", "source"])
def test_junction_paths_never_hold(env, tmp_path, which):
    e = env
    target = tmp_path / "junction-target"
    target.mkdir()
    link = tmp_path / "junction" if which == "holding" else e.folder
    if which == "source":
        _gate(e)
        e.bad.rename(target / e.bad.name)
        e.folder.rmdir()
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    try:
        if which == "holding":
            with pytest.raises(ValueError, match="reparse"):
                w.DirectorySettleTracker(watch_root=e.incoming, readiness_hold_root=link)
        else:
            e.clock.value = 1800
            _gate(e)
            assert (target / e.bad.name).exists() and not _payloads(e)
    finally:
        link.rmdir()


def test_ready_and_missing_archives_clear_stale_budgets(env):
    e = env
    _gate(e)
    assert e.tracker.readiness_holds._stalls
    e.bad.write_bytes(_zip_bytes())
    _gate(e)
    assert not e.tracker.readiness_holds._stalls
    e.bad.write_bytes(b"incomplete")
    _gate(e)
    e.bad.unlink()
    _gate(e)
    assert not e.tracker.readiness_holds._stalls


def test_startup_discovery_uses_the_same_hold_policy(env):
    e = env
    w._discover_startup_directories(e.incoming, e.tracker)
    assert e.bad.exists() and not _payloads(e)
    e.clock.value = 1800
    w._discover_startup_directories(e.incoming, e.tracker)
    assert len(_payloads(e)) == 1


@pytest.mark.parametrize("minutes", [-1, float("inf"), float("nan"), 1e308])
def test_invalid_minutes_refuse(env, minutes):
    with pytest.raises(ValueError, match="finite and nonnegative"):
        w.DirectorySettleTracker(readiness_stall_minutes=minutes)


def test_cli_defaults_and_explicit_hold_configuration():
    parser = w.build_argument_parser()
    default = parser.parse_args([])
    assert default.readiness_stall_minutes == 30 and default.readiness_hold_root is None
    chosen = parser.parse_args(["--readiness-stall-minutes", "0", "--readiness-hold-root", "holding"])
    assert chosen.readiness_stall_minutes == 0 and chosen.readiness_hold_root == Path("holding")


def test_cli_validates_holding_before_starting_observers(env, monkeypatch):
    e = env
    observer, routing = Mock(), Mock()
    monkeypatch.setattr(w, "WATCH_FOLDER", str(e.incoming))
    monkeypatch.setattr(w, "Observer", observer)
    monkeypatch.setattr(w, "_load_routing", routing)
    with pytest.raises(SystemExit) as error:
        w.main(["--readiness-hold-root", str(e.folder)])
    assert error.value.code == 2
    observer.assert_not_called()
    routing.assert_not_called()


def test_normal_import_cannot_enter_while_hold_owns_directory(env, monkeypatch):
    e = env
    inner = Mock()
    monkeypatch.setattr(w, "_process_and_move_directory_inner", inner)
    original = rh._rename_no_replace

    def competing_import(source, destination):
        e.normal_process(e.folder)
        inner.assert_not_called()
        original(source, destination)

    monkeypatch.setattr(rh, "_rename_no_replace", competing_import)
    _expire(e)
    assert len(_payloads(e)) == 1


@pytest.mark.parametrize("failure", ["stat-error", "no-file-id"])
def test_uncertain_source_identity_never_moves(env, monkeypatch, failure):
    e = env
    _gate(e)
    original = Path.lstat

    def uncertain(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == e.bad:
            if failure == "stat-error":
                raise PermissionError("fixture cannot identify source")
            fields = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
            fields["st_ino"] = 0
            return SimpleNamespace(**fields)
        return info

    monkeypatch.setattr(Path, "lstat", uncertain)
    e.clock.value = 1800
    _gate(e)
    e.clock.value = 3600
    _gate(e)
    assert e.bad.exists() and not _payloads(e)


@pytest.mark.parametrize("ready", [False, True])
def test_external_move_events_never_reimport_held_archives(env, monkeypatch, ready):
    e = env
    destination = e.holding / "readiness-attempt" / e.bad.name
    destination.parent.mkdir()
    destination.write_bytes(_zip_bytes() if ready else e.bad.read_bytes())
    notify = Mock()
    monkeypatch.setattr(e.tracker, "notify", notify)
    handler = w.CBZHandler(e.tracker)
    handler.on_moved(SimpleNamespace(is_directory=False, dest_path=str(destination)))
    notify.assert_not_called()
    handler.on_modified(SimpleNamespace(is_directory=False, src_path=str(e.bad)))
    notify.assert_called_once_with(e.folder)


def test_ready_gate_retries_after_hold_ownership_and_later_processes(env, monkeypatch):
    """A lost processing claim must not drop the only ready import event."""
    e = env
    e.bad.write_bytes(_zip_bytes())
    inner = Mock()
    monkeypatch.setattr(w, "process_and_move_directory", e.normal_process)
    monkeypatch.setattr(w, "_process_and_move_directory_inner", inner)
    with w._processing_dirs_lock:
        w._processing_dirs.add(e.folder)
    try:
        _gate(e)
        inner.assert_not_called()
        assert e.folder in e.tracker._readiness_retry_timers
    finally:
        with w._processing_dirs_lock:
            w._processing_dirs.discard(e.folder)
    generation = e.tracker._current_readiness_generation(e.folder)
    e.tracker._on_readiness_retry(e.folder, generation)
    inner.assert_called_once_with(e.folder)
    assert e.folder not in e.tracker._readiness_retry_timers


def test_ready_contention_retry_cannot_replace_a_newer_event(env, monkeypatch):
    e = env
    e.bad.write_bytes(_zip_bytes())

    def busy_with_new_event(path):
        e.tracker.notify(path)
        return False

    monkeypatch.setattr(w, "process_and_move_directory", busy_with_new_event)
    _gate(e)
    assert e.folder in e.tracker._timers
    assert e.folder not in e.tracker._readiness_retry_timers
