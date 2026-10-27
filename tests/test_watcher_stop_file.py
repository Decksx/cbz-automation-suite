"""Graceful stop requests use temporary roots and finish whole admitted units."""

import logging
import os
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

from scripts import cbz_watcher as w
from comic_automation.library import watcher_instance as wi

REPO = Path(__file__).resolve().parents[1]


def book(root, name):
    folder = root / name
    folder.mkdir()
    with zipfile.ZipFile(folder / "book.cbz", "w") as archive:
        archive.writestr("001.jpg", b"fixture page bytes")
    return folder


def quiet_startup(monkeypatch, root):
    """Keep fixtures away from host routing, recovery and library resolvers."""
    monkeypatch.setattr(w, "WATCH_FOLDER", str(root))
    for name in ("_load_routing", "_load_routing_v2", "_load_series_resolvers",
                 "_clean_up_stale_rewrite_files"):
        monkeypatch.setattr(w, name, lambda *args: None)


@pytest.mark.parametrize("bad", ["relative", "missing-parent", "file-parent"])
def test_bad_stop_path_refuses_before_creating_watch_root(tmp_path, monkeypatch, capsys, bad):
    root = tmp_path / "absent-intake"
    parent_file = tmp_path / "file"
    parent_file.write_text("not a directory")
    paths = {"relative": Path("stop.request"),
             "missing-parent": tmp_path / "missing" / "stop.request",
             "file-parent": parent_file / "stop.request"}
    monkeypatch.setattr(w, "WATCH_FOLDER", str(root))
    monkeypatch.setattr(w, "watcher_instance_lock", lambda *args: pytest.fail("invalid stop path entered startup"))
    with pytest.raises(SystemExit) as caught:
        w.main(["--stop-file", str(paths[bad])])
    assert caught.value.code == 2
    assert "--stop-file" in capsys.readouterr().err
    assert not root.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows mutex ownership")
def test_preexisting_request_exits_before_startup_and_releases_ownership(tmp_path, monkeypatch, caplog):
    stop = tmp_path / "stop.request"
    stop.write_bytes(b"contents are deliberately ignored")
    monkeypatch.setattr(w, "WATCH_FOLDER", str(tmp_path))
    for name in ("_load_routing", "_load_routing_v2", "_load_series_resolvers",
                 "_clean_up_stale_rewrite_files", "_discover_startup_directories", "Observer"):
        monkeypatch.setattr(w, name, lambda *args: pytest.fail("stop permitted startup work"))
    with caplog.at_level(logging.INFO):
        assert w.main(["--stop-file", str(stop)]) is None
    assert stop.exists()
    assert caplog.text.count("Watcher stop request seen:") == 1
    assert "0 admitted directories still finishing" in caplog.text
    assert caplog.text.count("CBZ Watcher stopped by stop-file request:") == 1
    with wi.watcher_instance_lock(tmp_path):
        pass


def test_request_latches_even_if_caller_removes_it(tmp_path, monkeypatch, caplog):
    stop = tmp_path / "stop.request"
    tracker = w.DirectorySettleTracker(watch_root=tmp_path, stop_file=stop)
    try:
        assert not tracker.stop_requested()
        stop.touch()
        with caplog.at_level(logging.INFO):
            assert tracker.stop_requested()
            stop.unlink()
            with monkeypatch.context() as patch:
                patch.setattr(Path, "stat", lambda *args, **kwargs: pytest.fail("latched stop was polled again"))
                assert tracker.stop_requested()
        assert caplog.text.count("Watcher stop request seen:") == 1
    finally:
        tracker.close()


def test_transient_poll_error_warns_once_and_a_later_request_still_stops(tmp_path, monkeypatch, caplog):
    stop = tmp_path / "stop.request"
    tracker = w.DirectorySettleTracker(watch_root=tmp_path, stop_file=stop)
    original_stat = Path.stat
    failing = True
    def stat(path, *args, **kwargs):
        if path == stop and failing:
            raise PermissionError("injected state-drive interruption")
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", stat)
    try:
        assert not tracker.stop_requested()
        assert not tracker.stop_requested()
        assert caplog.text.count("Cannot poll watcher stop file") == 1
        assert not tracker._closed
        processed = []
        monkeypatch.setattr(w, "process_and_move_directory", processed.append)
        directory = book(tmp_path, "still-admitted")
        w._process_directory_when_ready(directory, tracker)
        assert processed == [directory]
        failing = False
        stop.touch()
        assert tracker.stop_requested()
    finally:
        tracker.close()


def test_no_stop_file_keeps_legacy_admission_without_polling(tmp_path, monkeypatch):
    tracker = w.DirectorySettleTracker(watch_root=tmp_path)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "stat", lambda *args, **kwargs: pytest.fail("disabled stop interface polled storage"))
            assert not tracker.stop_requested()
            with tracker._directory_admission() as admitted:
                assert admitted
    finally:
        tracker.close()


@pytest.mark.parametrize("entry", ["notify", "retry", "callback", "minimum-age"])
def test_timer_entry_polls_request_without_waiting_for_main_loop(tmp_path, monkeypatch, entry):
    stop = tmp_path / "stop.request"
    directory = tmp_path / "new-arrival"
    directory.mkdir()
    tracker = w.DirectorySettleTracker(watch_root=tmp_path, stop_file=stop)
    called = []
    monkeypatch.setattr(w, "MIN_AGE", 1000)
    try:
        stop.touch()
        if entry == "notify":
            tracker.notify(directory)
        elif entry == "retry":
            assert not tracker._schedule_readiness_retry(directory)
        elif entry == "callback":
            tracker._tracked_callback(lambda: called.append("entered"))()
        else:
            tracker._on_settled(directory)
        assert tracker._stop_latched and tracker._closed
        assert not called and not tracker._timers and not tracker._readiness_retry_timers
    finally:
        tracker.close()


def test_disappearing_parent_is_a_poll_error_and_does_not_latch(tmp_path, caplog):
    parent = tmp_path / "job"
    parent.mkdir()
    stop = parent / "stop.request"
    tracker = w.DirectorySettleTracker(watch_root=tmp_path, stop_file=stop)
    try:
        parent.rmdir()
        assert not tracker.stop_requested()
        assert not tracker.stop_requested()
        assert caplog.text.count("Cannot poll watcher stop file") == 1
        parent.mkdir()
        stop.touch()
        assert tracker.stop_requested()
    finally:
        tracker.close()


def test_stop_cancels_pending_timers_and_fences_late_callbacks_and_direct_gate(tmp_path, monkeypatch):
    stop = tmp_path / "stop.request"
    tracker = w.DirectorySettleTracker(settle_delay=60, watch_root=tmp_path, stop_file=stop)
    called = []
    monkeypatch.setattr(tracker, "_on_settled", lambda *args: called.append("settled"))
    monkeypatch.setattr(tracker, "_on_readiness_retry", lambda *args: called.append("retry"))
    tracker.notify(tmp_path / "settling")
    tracker._schedule_readiness_retry(tmp_path / "retrying")
    timers = [*tracker._timers.values(), *tracker._readiness_retry_timers.values()]
    try:
        stop.touch()
        assert tracker.stop_requested()
        assert not tracker._timers and not tracker._readiness_retry_timers
        for timer in timers:
            timer.join(timeout=2)
            assert not timer.is_alive()
            timer.function(*timer.args, **timer.kwargs)
        tracker.notify(tmp_path / "late")
        assert not tracker._schedule_readiness_retry(tmp_path / "late")
        monkeypatch.setattr(w, "probe_cbz_directory_readiness", lambda *args: pytest.fail("stopped directory was probed"))
        w._process_directory_when_ready(book(tmp_path, "late-book"), tracker)
        assert not called and not tracker._timers and not tracker._readiness_retry_timers
    finally:
        tracker.close()
        for timer in timers:
            timer.cancel()
            timer.join(timeout=2)


@pytest.mark.skipif(os.name != "nt", reason="Windows mutex ownership")
def test_startup_stop_finishes_first_directory_and_leaves_next_untouched(tmp_path, monkeypatch):
    root = tmp_path / "intake"
    root.mkdir()
    first, second = book(root, "01-first"), book(root, "02-second")
    original = (second / "book.cbz").read_bytes()
    stop = tmp_path / "stop.request"
    quiet_startup(monkeypatch, root)
    called = []
    def process(folder):
        called.append(folder)
        stop.touch()
        # A request arriving inside a directory must allow its operation to finish.
        (folder / "finished.txt").write_text("complete")
    monkeypatch.setattr(w, "process_and_move_directory", process)
    monkeypatch.setattr(w, "Observer", lambda: pytest.fail("observer started after startup stop"))
    assert w.main(["--stop-file", str(stop)]) is None
    assert called == [first]
    assert (first / "finished.txt").read_text() == "complete"
    assert (second / "book.cbz").read_bytes() == original
    assert not (second / "finished.txt").exists()
    assert stop.exists()


def mutex_probe(root):
    child = """
import sys
from comic_automation.library.watcher_instance import watcher_instance_lock, WatcherInstanceRefused
try:
    with watcher_instance_lock(sys.argv[1]): pass
except WatcherInstanceRefused:
    raise SystemExit(3)
"""
    return subprocess.run([sys.executable, "-c", child, str(root)], cwd=REPO,
                          capture_output=True, text=True, timeout=15).returncode


def test_close_waits_for_direct_directory_admission_without_a_timer(tmp_path, monkeypatch):
    """Startup and direct entry use the same draining rule as timer callbacks."""
    tracker = w.DirectorySettleTracker(watch_root=tmp_path)
    entered, release, waiting, closed = (threading.Event() for _ in range(4))
    original_wait = tracker._idle.wait
    def wait(*args, **kwargs):
        waiting.set()
        return original_wait(*args, **kwargs)
    def unit():
        with tracker._directory_admission() as admitted:
            assert admitted
            entered.set()
            assert release.wait(10)
    def close():
        tracker.close()
        closed.set()
    monkeypatch.setattr(tracker._idle, "wait", wait)
    worker = threading.Thread(target=unit)
    closer = threading.Thread(target=close)
    worker.start()
    try:
        assert entered.wait(3)
        closer.start()
        assert waiting.wait(3), "close did not wait for the admitted directory"
        assert not closed.is_set()
    finally:
        release.set()
        worker.join(timeout=5)
        if closer.ident is not None:
            closer.join(timeout=5)
        tracker.close()
    assert closed.is_set() and not worker.is_alive() and not closer.is_alive()


@pytest.mark.skipif(os.name != "nt", reason="Windows mutex ownership")
def test_stop_drains_admitted_directory_under_mutex_before_exiting(tmp_path, monkeypatch, caplog):
    root = tmp_path / "intake"
    root.mkdir()
    directory = book(root, "active")
    stop = tmp_path / "stop.request"
    quiet_startup(monkeypatch, root)
    entered, release, closing, finished = (threading.Event() for _ in range(4))
    failures, workers, exit_codes = [], [], []
    original_close = w.DirectorySettleTracker.close
    def process(folder):
        entered.set()
        assert release.wait(15), "test did not release admitted operation"
        (folder / "finished.txt").write_text("complete")
    def close(self):
        closing.set()
        original_close(self)
    def session(args, watch_root, tracker):
        worker = threading.Thread(target=tracker._tracked_callback(
            lambda: w._process_directory_when_ready(directory, tracker)))
        workers.append(worker)
        worker.start()
        deadline = time.monotonic() + 10
        while not tracker.stop_requested():
            assert time.monotonic() < deadline, "no stop request arrived"
            time.sleep(.01)
    def run():
        try:
            exit_codes.append(w.main(["--stop-file", str(stop)]))
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()
    monkeypatch.setattr(w, "process_and_move_directory", process)
    monkeypatch.setattr(w.DirectorySettleTracker, "close", close)
    monkeypatch.setattr(w, "_watch_session", session)
    with caplog.at_level(logging.INFO):
        owner = threading.Thread(target=run)
        owner.start()
        try:
            assert entered.wait(5)
            stop.touch()
            assert closing.wait(5)
            assert "1 admitted directories still finishing" in caplog.text
            assert not finished.is_set()
            assert mutex_probe(root) == 3
        finally:
            release.set()
            owner.join(timeout=15)
            for worker in workers:
                worker.join(timeout=5)
        assert not owner.is_alive() and not failures and exit_codes == [None]
    assert (directory / "finished.txt").read_text() == "complete"
    assert mutex_probe(root) == 0
    assert caplog.text.count("CBZ Watcher stopped by stop-file request:") == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows watchdog and mutex ownership")
def test_real_observer_process_stops_on_atomic_request_and_returns_zero(tmp_path):
    root = tmp_path / "intake"
    root.mkdir()
    ready, stop = tmp_path / "ready", tmp_path / "stop.request"
    child = r'''
import sys
from pathlib import Path
from scripts import cbz_watcher as w
w.WATCH_FOLDER = sys.argv[1]
for name in ("_load_routing", "_load_routing_v2", "_load_series_resolvers",
             "_clean_up_stale_rewrite_files", "_discover_startup_directories"):
    setattr(w, name, lambda *args: None)
original = w.Observer
def observer():
    instance = original()
    start = instance.start
    def started():
        start()
        Path(sys.argv[2]).touch()
    instance.start = started
    return instance
w.Observer = observer
raise SystemExit(w.main(["--stop-file", sys.argv[3]]))
'''
    process = subprocess.Popen([sys.executable, "-c", child, str(root), str(ready), str(stop)],
                               cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists(), "real observer did not start"
        temporary = tmp_path / "request.tmp"
        temporary.write_text("contents ignored")
        temporary.rename(stop)
        out, err = process.communicate(timeout=15)
        assert process.returncode == 0, out + err
        assert err.count("Watcher stop request seen:") == 1
        assert err.count("CBZ Watcher stopped by stop-file request:") == 1
        assert stop.exists()
        assert mutex_probe(root) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
