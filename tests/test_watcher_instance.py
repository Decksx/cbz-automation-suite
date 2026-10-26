"""Physical-root exclusion and real Win32 ownership, using only temp intake."""

import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from comic_automation.library import watcher_instance as wi

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Global mutex contract")
REPO = Path(__file__).resolve().parents[1]

# A held helper is a real separate interpreter. The main mode replaces only
# the already-locked work body, so it cannot touch routing or a live library.
# The script mode exercises the actual __main__ exit path with a temp constant.
CHILD = r'''
import json, sys
from pathlib import Path
from comic_automation.library import watcher_instance as wi
root, ready, mode, hold = sys.argv[1:]
def entered(instance=None):
    data = {"module": wi.__file__}
    if instance:
        data.update(name=instance.mutex_name, abandoned=instance.abandoned,
                    resolved=str(instance.resolved_root), identity=instance.identity)
    Path(ready).write_text(json.dumps(data), encoding="utf-8")
    if hold == "yes":
        sys.stdin.readline()
if mode == "script":
    source = Path("scripts/cbz_watcher.py").read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    matches = [i for i, line in enumerate(lines) if line.startswith("WATCH_FOLDER  = ")]
    assert len(matches) == 1
    lines[matches[0]] = "WATCH_FOLDER  = " + repr(root) + "\n"
    sys.argv = ["cbz_watcher.py"]
    exec(compile("".join(lines), "scripts/cbz_watcher.py", "exec"),
         {"__name__": "__main__", "__file__": str(Path("scripts/cbz_watcher.py").resolve())})
elif mode == "main":
    from scripts import cbz_watcher as watcher
    watcher.WATCH_FOLDER = root
    watcher._watch_with_config = lambda *args: entered()
    raise SystemExit(watcher.main([]))
else:
    try:
        with wi.watcher_instance_lock(root) as instance:
            entered(instance)
    except wi.WatcherInstanceRefused as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(3)
'''


def _command(root, ready, mode="lock", hold="no"):
    return [sys.executable, "-c", CHILD, str(root), str(ready), mode, hold]


@contextmanager
def holder(root, tmp_path, *, cwd=REPO, mode="lock"):
    """Bounded readiness handshake, with child teardown even on assertion failure."""
    ready = tmp_path / (uuid.uuid4().hex + ".json")
    process = subprocess.Popen(_command(root, ready, mode, "yes"), cwd=cwd,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 20
        while not ready.exists():
            if process.poll() is not None:
                out, err = process.communicate()
                pytest.fail(f"holder exited {process.returncode}: {out}\n{err}")
            if time.monotonic() > deadline:
                pytest.fail("holder did not become ready")
            time.sleep(.02)
        # The file may be visible before its one small write is complete.
        while True:
            try:
                evidence = json.loads(ready.read_text(encoding="utf-8"))
                break
            except json.JSONDecodeError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(.02)
        yield process, evidence
    finally:
        if process.poll() is None:
            try:
                process.communicate("\n", timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=10)
        else:
            process.communicate(timeout=10)


def contender(root, tmp_path, *, cwd=REPO, mode="lock"):
    ready = tmp_path / (uuid.uuid4().hex + ".json")
    result = subprocess.run(_command(root, ready, mode), cwd=cwd,
                            capture_output=True, text=True, timeout=20)
    return result, ready


@pytest.mark.parametrize("mode", ["main", "script"])
def test_second_watcher_is_refused_before_startup_and_returns_exit_three(tmp_path, mode):
    root = tmp_path / "intake"
    root.mkdir()
    with holder(root, tmp_path, mode="main"):
        result, ready = contender(root, tmp_path, mode=mode)
        assert result.returncode == 3, result.stderr
        assert "already running" in result.stderr
        assert not ready.exists()
        assert list(root.iterdir()) == []
    # A normal exit must release ownership in the same process lifetime too.
    result, ready = contender(root, tmp_path, mode="main")
    assert result.returncode == 0 and ready.exists(), result.stderr


@pytest.mark.parametrize("alias_kind", ["unc", "mapped", "junction", "source-copy"])
def test_two_processes_share_one_mutex_across_access_paths_and_source_copies(tmp_path, alias_kind):
    root = tmp_path / "intake"
    root.mkdir()
    alias, cwd, mapped = root, REPO, None
    if alias_kind == "unc":
        drive, tail = os.path.splitdrive(str(root))
        alias = Path("\\\\localhost\\" + drive[0] + "$" + tail)
        if not alias.is_dir():
            pytest.skip("localhost administrative share is unavailable")
    elif alias_kind == "junction":
        alias = tmp_path / "junction"
        subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(alias), str(root)],
                       check=True, capture_output=True)
    elif alias_kind == "mapped":
        # Allocate only an unused drive, and remove only the mapping we made.
        for letter in "WVUTSRQPONMLKJIH":
            drive = letter + ":"
            if Path(drive + "\\").exists():
                continue
            result = subprocess.run(["subst.exe", drive, str(root)], capture_output=True)
            if result.returncode == 0:
                mapped = drive
                alias = Path(drive + "\\")
                break
        if mapped is None:
            pytest.skip("no unused drive letter available for a temporary mapping")
    elif alias_kind == "source-copy":
        cwd = tmp_path / "second-checkout"
        target = cwd / "comic_automation/library/watcher_instance.py"
        target.parent.mkdir(parents=True)
        shutil.copyfile(wi.__file__, target)
    try:
        with holder(root, tmp_path) as (_, evidence):
            result, ready = contender(alias, tmp_path, cwd=cwd)
            assert result.returncode == 3, result.stderr
            assert evidence["name"] in result.stderr
            assert not ready.exists()
        if alias_kind == "source-copy":
            result, ready = contender(root, tmp_path, cwd=cwd)
            assert result.returncode == 0, result.stderr
            assert Path(json.loads(ready.read_text())["module"]).resolve() == target.resolve()
    finally:
        if mapped:
            subprocess.run(["subst.exe", mapped, "/D"], check=True, capture_output=True)
        if alias_kind == "junction":
            alias.rmdir()


def test_distinct_roots_can_be_owned_by_two_processes(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    with holder(first, tmp_path) as (p1, e1), holder(second, tmp_path) as (p2, e2):
        assert p1.pid != p2.pid and e1["name"] != e2["name"]
        assert p1.poll() is None and p2.poll() is None


def test_crashed_holder_grants_abandoned_mutex_to_the_next_process(tmp_path):
    root = tmp_path / "intake"
    root.mkdir()
    with holder(root, tmp_path) as (process, evidence):
        api = wi._kernel32()
        # Keep the named object alive WITHOUT waiting/owning it. Otherwise
        # killing its only handle would destroy it and test a new mutex instead.
        keepalive = api.CreateMutexW(None, False, evidence["name"])
        assert keepalive
        try:
            process.kill()
            process.wait(timeout=10)
            with holder(root, tmp_path) as (_, replacement):
                assert replacement["abandoned"] is True
                assert replacement["name"] == evidence["name"]
                result, _ = contender(root, tmp_path)
                assert result.returncode == 3
        finally:
            api.CloseHandle(keepalive)


def test_embedded_recursive_acquisition_is_refused_and_exception_releases(tmp_path):
    with pytest.raises(ValueError, match="body failed"):
        with wi.watcher_instance_lock(tmp_path):
            with pytest.raises(wi.WatcherInstanceRefused, match="already running"):
                with wi.watcher_instance_lock(tmp_path):
                    pytest.fail("recursive watcher was admitted")
            raise ValueError("body failed")
    with wi.watcher_instance_lock(tmp_path) as evidence:
        assert not evidence.abandoned


def test_global_name_and_noninherited_native_handle(tmp_path, monkeypatch):
    from ctypes import wintypes

    api = wi._kernel32()
    opened = []

    class ObservedKernel:
        def CreateMutexW(self, security, owner, name):
            assert security is None and owner is False
            handle = api.CreateMutexW(security, owner, name)
            opened.append(handle)
            return handle

        def __getattr__(self, name):
            return getattr(api, name)

    monkeypatch.setattr(wi, "_kernel32", ObservedKernel)
    with wi.watcher_instance_lock(tmp_path) as evidence:
        info = tmp_path.stat()
        assert evidence.identity == (info.st_dev, info.st_ino)
        assert evidence.mutex_name == f"Global\\CBZWatcher-{info.st_dev:x}-{info.st_ino:x}"
        get_flags = api.GetHandleInformation
        get_flags.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        get_flags.restype = wintypes.BOOL
        flags = wintypes.DWORD()
        assert get_flags(opened[0], ctypes.byref(flags))
        assert flags.value & 1 == 0  # HANDLE_FLAG_INHERIT


@pytest.mark.parametrize("bad", ["missing", "file", "zero-device", "zero-inode", "denied"])
def test_unusable_identity_never_opens_a_mutex(tmp_path, monkeypatch, bad):
    root = tmp_path / "intake"
    if bad == "file":
        root.write_text("not a directory")
    elif bad != "missing":
        root.mkdir()
    original = Path.stat

    def stat(path, *args, **kwargs):
        if path == root and bad == "denied":
            raise PermissionError("identity inaccessible")
        value = original(path, *args, **kwargs)
        if path == root and bad.startswith("zero-"):
            return SimpleNamespace(st_mode=value.st_mode,
                                   st_dev=0 if bad == "zero-device" else value.st_dev,
                                   st_ino=0 if bad == "zero-inode" else value.st_ino)
        return value

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(wi, "_kernel32", lambda: pytest.fail("uncertain identity opened the kernel"))
    with pytest.raises(wi.WatcherInstanceRefused, match="identity|identify"):
        with wi.watcher_instance_lock(root):
            pytest.fail("uncertain root was admitted")


class FakeKernel:
    """Fault injection checks handle ownership without leaking native handles."""

    def __init__(self, outcome=0, handle=123, release=True):
        self.outcome, self.handle, self.release = outcome, handle, release
        self.calls = []

    def CreateMutexW(self, security, owner, name):
        self.calls.append(("create", security, owner, name))
        ctypes.set_last_error(5)
        return self.handle

    def WaitForSingleObject(self, handle, timeout):
        self.calls.append(("wait", handle, timeout))
        ctypes.set_last_error(5)
        return self.outcome

    def ReleaseMutex(self, handle):
        self.calls.append(("release", handle))
        return self.release

    def CloseHandle(self, handle):
        self.calls.append(("close", handle))
        return True


@pytest.mark.parametrize("outcome,handle", [(0, None), (0x102, 123), (0xffffffff, 123), (1, 123)])
def test_kernel_errors_refuse_without_fallback_or_releasing_unowned_handles(tmp_path, monkeypatch, outcome, handle):
    api = FakeKernel(outcome=outcome, handle=handle)
    monkeypatch.setattr(wi, "_kernel32", lambda: api)
    with pytest.raises(wi.WatcherInstanceRefused):
        with wi.watcher_instance_lock(tmp_path):
            pytest.fail("unverified owner was admitted")
    assert not any(call[0] == "release" for call in api.calls)
    assert [call for call in api.calls if call[0] == "close"] == ([("close", 123)] if handle else [])
    assert len([call for call in api.calls if call[0] == "create"]) == 1


@pytest.mark.parametrize("failure", ["changed", "stat-error", "body", "release"])
def test_acquired_handle_is_released_and_closed_on_every_exit(tmp_path, monkeypatch, failure):
    api = FakeKernel(outcome=0x80, release=failure != "release")
    monkeypatch.setattr(wi, "_kernel32", lambda: api)
    original = wi._directory_identity
    count = 0

    def identity(path):
        nonlocal count
        count += 1
        if count >= 3 and failure == "stat-error":
            raise PermissionError("identity recheck failed")
        found = original(path)
        return (found[0], found[1] + 1) if count >= 3 and failure == "changed" else found

    monkeypatch.setattr(wi, "_directory_identity", identity)
    error = ValueError if failure == "body" else wi.WatcherInstanceRefused
    with pytest.raises(error):
        with wi.watcher_instance_lock(tmp_path) as evidence:
            assert evidence.abandoned
            if failure == "body":
                raise ValueError("body failed")
    assert api.calls[-2:] == [("release", 123), ("close", 123)]
    assert not wi._owned_names


def test_main_refusal_preserves_policy_and_never_enters_work(tmp_path, monkeypatch, caplog):
    from scripts import cbz_watcher as watcher

    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(tmp_path))
    monkeypatch.setattr(watcher, "_watch_with_config", lambda *args: pytest.fail("refused watcher did work"))
    previous = watcher._number_style_registry, watcher._number_style_requested
    with wi.watcher_instance_lock(tmp_path):
        assert watcher.main([]) == 3
    assert (watcher._number_style_registry, watcher._number_style_requested) == previous
    assert "already running" in caplog.text


def test_main_refuses_an_inaccessible_watch_root_without_doing_work(tmp_path, monkeypatch, caplog):
    from scripts import cbz_watcher as watcher

    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(tmp_path))

    def denied(*args, **kwargs):
        raise PermissionError("root inaccessible")

    monkeypatch.setattr(watcher.os, "makedirs", denied)
    monkeypatch.setattr(watcher, "_watch_with_config", lambda *args: pytest.fail("refused watcher did work"))
    assert watcher.main([]) == 3
    assert "Cannot prepare watch root" in caplog.text


def test_help_does_not_acquire_a_mutex_or_create_a_watch_root(tmp_path, monkeypatch):
    from scripts import cbz_watcher as watcher

    root = tmp_path / "absent"
    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(root))
    monkeypatch.setattr(watcher, "watcher_instance_lock", lambda *args: pytest.fail("help acquired a mutex"))
    with pytest.raises(SystemExit) as exc:
        watcher.main(["--help"])
    assert exc.value.code == 0 and not root.exists()


def test_main_keeps_mutex_until_active_import_callback_has_finished(tmp_path, monkeypatch):
    from scripts import cbz_watcher as watcher

    entered, release, closing, finished = (threading.Event() for _ in range(4))
    failures = []
    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(tmp_path))
    original_close = watcher.DirectorySettleTracker.close

    def callback(self, path):
        entered.set()
        assert release.wait(15), "test did not release callback"

    def close(self):
        closing.set()
        original_close(self)

    def session(args, root, tracker):
        tracker.settle_delay = 0
        tracker.notify(root)
        assert entered.wait(5)
        raise RuntimeError("session failed while an import was active")

    def run():
        try:
            watcher.main([])
        except RuntimeError as exc:
            failures.append(str(exc))
        finally:
            finished.set()

    monkeypatch.setattr(watcher.DirectorySettleTracker, "_on_settled", callback)
    monkeypatch.setattr(watcher.DirectorySettleTracker, "close", close)
    monkeypatch.setattr(watcher, "_watch_session", session)
    owner = threading.Thread(target=run)
    owner.start()
    try:
        assert closing.wait(5)
        assert not finished.is_set()
        result, _ = contender(tmp_path, tmp_path)
        assert result.returncode == 3, result.stderr
    finally:
        release.set()
        owner.join(timeout=10)
    assert not owner.is_alive() and failures == ["session failed while an import was active"]
    result, _ = contender(tmp_path, tmp_path)
    assert result.returncode == 0, result.stderr


def test_tracker_close_cancels_pending_and_late_timers_and_refuses_new_work(tmp_path, monkeypatch):
    from scripts import cbz_watcher as watcher

    tracker = watcher.DirectorySettleTracker(settle_delay=60, watch_root=tmp_path)
    calls = []
    monkeypatch.setattr(tracker, "_on_settled", lambda *args: calls.append("settled"))
    monkeypatch.setattr(tracker, "_on_readiness_retry", lambda *args: calls.append("retry"))
    tracker.notify(tmp_path / "settling")
    tracker._schedule_readiness_retry(tmp_path / "retrying")
    timers = [*tracker._timers.values(), *tracker._readiness_retry_timers.values()]
    try:
        tracker.close()
        for timer in timers:
            timer.join(timeout=2)
            assert not timer.is_alive()
            # A Timer that woke just before cancellation must still be fenced.
            timer.function(*timer.args, **timer.kwargs)
        tracker.notify(tmp_path / "new")
        assert tracker._schedule_readiness_retry(tmp_path / "new") is False
        assert not calls and not tracker._timers and not tracker._readiness_retry_timers
        tracker.close()  # idempotent
    finally:
        tracker.close()
        for timer in timers:
            timer.cancel()
            timer.join(timeout=2)


def test_minimum_age_callback_cannot_rearm_a_closed_tracker(tmp_path):
    from scripts import cbz_watcher as watcher

    tracker = watcher.DirectorySettleTracker(watch_root=tmp_path)
    tracker.close()
    try:
        tracker._on_settled(tmp_path)
        assert not tracker._timers
    finally:
        tracker.close()


@pytest.mark.parametrize("exit_point", ["start-error", "loop-error", "keyboard"])
def test_observer_and_tracker_cleanup_finish_while_mutex_is_still_owned(tmp_path, monkeypatch, exit_point):
    from scripts import cbz_watcher as watcher

    events = []
    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(tmp_path))
    for name in ("_load_routing", "_load_routing_v2", "_load_series_resolvers",
                 "_clean_up_stale_rewrite_files", "_discover_startup_directories"):
        monkeypatch.setattr(watcher, name, lambda *args: None)

    def still_owned():
        with pytest.raises(wi.WatcherInstanceRefused, match="already running"):
            with wi.watcher_instance_lock(tmp_path):
                pytest.fail("released before shutdown completed")

    class Observer:
        ident = None

        def schedule(self, *args, **kwargs):
            pass

        def start(self):
            if exit_point == "start-error":
                raise RuntimeError("start failed")
            self.ident = 1

        def stop(self):
            still_owned()
            events.append("stop")

        def join(self):
            still_owned()
            events.append("join")

    def sleep(*args):
        if exit_point == "keyboard":
            raise KeyboardInterrupt
        raise RuntimeError("loop failed")

    original_close = watcher.DirectorySettleTracker.close

    def close(self):
        still_owned()
        events.append("close")
        original_close(self)

    monkeypatch.setattr(watcher, "Observer", Observer)
    monkeypatch.setattr(watcher.time, "sleep", sleep)
    monkeypatch.setattr(watcher.DirectorySettleTracker, "close", close)
    if exit_point == "keyboard":
        watcher.main([])
    else:
        with pytest.raises(RuntimeError):
            watcher.main([])
    assert events == (["stop", "close"] if exit_point == "start-error" else ["stop", "join", "close"])
    with wi.watcher_instance_lock(tmp_path):
        pass
