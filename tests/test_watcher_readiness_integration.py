from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import cbz_watcher as watcher
from scripts.cbz_watcher import (
    CbzReadinessEntry,
    DirectoryReadinessResult,
    DirectorySettleTracker,
    ZipReadiness,
)


def _make_cbz(path: Path, page_count: int = 1, payload_size: int = 50) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for index in range(page_count):
            zf.writestr(f"page{index}.jpg", b"\0" * payload_size)


def _truncate(path: Path) -> None:
    original = path.read_bytes()
    path.write_bytes(original[: len(original) // 2])


def _ready_result(paths: list[Path]) -> DirectoryReadinessResult:
    entries = tuple(
        CbzReadinessEntry(
            path=path,
            status=ZipReadiness.READY,
            reason="ok",
            detail=None,
            page_count=1,
        )
        for path in paths
    )
    return DirectoryReadinessResult(
        status=ZipReadiness.READY,
        reason="ok",
        entries=entries,
        archive_count=len(paths),
        ready_count=len(paths),
        page_count=len(paths),
    )


def _not_ready_result(
    ready_paths: list[Path],
    blocked_path: Path,
    *,
    reason: str = "incomplete_central_directory",
    detail: str | None = "File is not a zip file",
) -> DirectoryReadinessResult:
    entries = tuple(
        CbzReadinessEntry(
            path=path,
            status=ZipReadiness.READY,
            reason="ok",
            detail=None,
            page_count=1,
        )
        for path in ready_paths
    ) + (
        CbzReadinessEntry(
            path=blocked_path,
            status=ZipReadiness.RETRY_LATER,
            reason=reason,
            detail=detail,
            page_count=None,
        ),
    )
    archive_count = len(ready_paths) + 1
    return DirectoryReadinessResult(
        status=ZipReadiness.RETRY_LATER,
        reason="not_all_ready",
        entries=entries,
        archive_count=archive_count,
        ready_count=len(ready_paths),
        page_count=len(ready_paths),
    )


class FakeTimer:
    """Deterministic stand-in for threading.Timer.

    .start() only records that it was started -- no real thread or delay
    is involved. Tests fire the scheduled callback explicitly via .fire(),
    which is a no-op if the timer was cancelled first, mirroring real
    threading.Timer semantics closely enough for these tests.
    """

    instances: list["FakeTimer"] = []

    def __init__(self, interval, function, args=None, kwargs=None):
        self.interval = interval
        self.function = function
        self.args = list(args or [])
        self.kwargs = dict(kwargs or {})
        self.cancelled = False
        self.started = False
        FakeTimer.instances.append(self)

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        if self.cancelled:
            return
        self.function(*self.args, **self.kwargs)


@pytest.fixture(autouse=True)
def _fake_timers():
    FakeTimer.instances = []
    yield
    FakeTimer.instances = []


def _patch_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watcher.threading, "Timer", FakeTimer)


# --- all ready -------------------------------------------------


def test_all_ready_processes_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    comic_dir = tmp_path / "Series A"
    cbz = comic_dir / "issue-01.cbz"
    _make_cbz(cbz)

    monkeypatch.setattr(
        watcher,
        "probe_cbz_directory_readiness",
        lambda paths, **kwargs: _ready_result(list(paths)),
    )
    process_mock = Mock()
    monkeypatch.setattr(watcher, "process_and_move_directory", process_mock)

    tracker = DirectorySettleTracker()
    watcher._process_directory_when_ready(comic_dir, tracker)

    process_mock.assert_called_once_with(comic_dir)


# --- one unready -------------------------------------------------


def test_one_unready_defers_and_schedules_one_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comic_dir = tmp_path / "Series B"
    ready_cbz = comic_dir / "issue-01.cbz"
    blocked_cbz = comic_dir / "issue-02.cbz"
    _make_cbz(ready_cbz)
    _make_cbz(blocked_cbz)

    monkeypatch.setattr(
        watcher,
        "probe_cbz_directory_readiness",
        lambda paths, **kwargs: _not_ready_result([ready_cbz], blocked_cbz),
    )
    process_mock = Mock()
    monkeypatch.setattr(watcher, "process_and_move_directory", process_mock)
    _patch_timer(monkeypatch)

    tracker = DirectorySettleTracker()
    watcher._process_directory_when_ready(comic_dir, tracker)

    process_mock.assert_not_called()
    assert len(FakeTimer.instances) == 1
    retry_timer = FakeTimer.instances[0]
    assert retry_timer.started
    assert retry_timer.interval == watcher.READINESS_RETRY_DELAY_SECONDS
    assert tracker._readiness_retry_timers[comic_dir] is retry_timer


# --- later retry becomes ready -------------------------------------------------


def test_later_retry_becomes_ready_processes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comic_dir = tmp_path / "Series C"
    ready_cbz = comic_dir / "issue-01.cbz"
    blocked_cbz = comic_dir / "issue-02.cbz"
    _make_cbz(ready_cbz)
    _make_cbz(blocked_cbz)

    state = {"ready": False}

    def fake_probe(paths, **kwargs):
        if state["ready"]:
            return _ready_result(list(paths))
        return _not_ready_result([ready_cbz], blocked_cbz)

    monkeypatch.setattr(watcher, "probe_cbz_directory_readiness", fake_probe)
    process_mock = Mock()
    monkeypatch.setattr(watcher, "process_and_move_directory", process_mock)
    _patch_timer(monkeypatch)

    tracker = DirectorySettleTracker()
    watcher._process_directory_when_ready(comic_dir, tracker)

    process_mock.assert_not_called()
    assert len(FakeTimer.instances) == 1

    # The archive that was blocking readiness has since become ready.
    state["ready"] = True
    FakeTimer.instances[0].fire()

    process_mock.assert_called_once_with(comic_dir)
    assert comic_dir not in tracker._readiness_retry_timers


# --- diagnostic reason identifies the blocking archive -------------------------------------------------


def test_diagnostic_log_identifies_blocking_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    comic_dir = tmp_path / "Series D"
    ready_cbz = comic_dir / "issue-01.cbz"
    blocked_cbz = comic_dir / "corrupt-issue.cbz"
    _make_cbz(ready_cbz)
    _make_cbz(blocked_cbz)

    monkeypatch.setattr(
        watcher,
        "probe_cbz_directory_readiness",
        lambda paths, **kwargs: _not_ready_result(
            [ready_cbz],
            blocked_cbz,
            reason="incomplete_central_directory",
            detail="File is not a zip file",
        ),
    )
    monkeypatch.setattr(watcher, "process_and_move_directory", Mock())
    _patch_timer(monkeypatch)

    tracker = DirectorySettleTracker()
    with caplog.at_level("INFO", logger=watcher.log.name):
        watcher._process_directory_when_ready(comic_dir, tracker)

    assert "corrupt-issue.cbz" in caplog.text
    assert "incomplete_central_directory" in caplog.text
    assert "File is not a zip file" in caplog.text
    # The archive that *was* ready should not be reported as a blocker.
    assert "issue-01.cbz" not in caplog.text


# --- new event timer wins over an older retry -------------------------------------------------


def test_new_event_cancels_stale_readiness_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_timer(monkeypatch)
    comic_dir = tmp_path / "Series E"
    comic_dir.mkdir()

    tracker = DirectorySettleTracker()
    tracker._schedule_readiness_retry(comic_dir)

    assert len(FakeTimer.instances) == 1
    stale_retry = FakeTimer.instances[0]
    assert not stale_retry.cancelled

    # A brand-new filesystem event arrives for the same directory.
    tracker.notify(comic_dir)

    assert stale_retry.cancelled
    assert comic_dir not in tracker._readiness_retry_timers
    assert len(FakeTimer.instances) == 2
    fresh_settle_timer = FakeTimer.instances[1]
    assert not fresh_settle_timer.cancelled
    assert tracker._timers[comic_dir] is fresh_settle_timer

    # Firing the stale (cancelled) retry must do nothing at all -- in
    # particular it must never reach _process_directory_when_ready.
    stale_retry.fire()
    assert comic_dir not in tracker._readiness_retry_timers


def test_stale_retry_callback_that_already_started_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_timer(monkeypatch)
    comic_dir = tmp_path / "Series E2"
    comic_dir.mkdir()
    dispatch_mock = Mock()

    tracker = DirectorySettleTracker()
    monkeypatch.setattr(tracker, "_dispatch", dispatch_mock)
    tracker._schedule_readiness_retry(comic_dir)
    stale_retry = FakeTimer.instances[0]
    stale_generation = stale_retry.args[1]

    # A new event invalidates the retry. Invoke the callback directly to
    # model a real Timer whose function had already started before cancel().
    tracker.notify(comic_dir)
    tracker._on_readiness_retry(comic_dir, stale_generation)

    dispatch_mock.assert_not_called()


def test_new_event_during_probe_discards_stale_probe_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_timer(monkeypatch)
    comic_dir = tmp_path / "Series E3"
    blocked_cbz = comic_dir / "issue-01.cbz"
    _make_cbz(blocked_cbz)
    process_mock = Mock()
    monkeypatch.setattr(watcher, "process_and_move_directory", process_mock)

    tracker = DirectorySettleTracker()
    initial_generation = tracker._current_readiness_generation(comic_dir)

    def probe_then_notify(paths, **kwargs):
        tracker.notify(comic_dir)
        return _not_ready_result([], blocked_cbz)

    monkeypatch.setattr(
        watcher,
        "probe_cbz_directory_readiness",
        probe_then_notify,
    )

    watcher._process_directory_when_ready(
        comic_dir,
        tracker,
        readiness_generation=initial_generation,
    )

    process_mock.assert_not_called()
    # Only notify()'s fresh settle timer exists. The stale probe did not add
    # a second readiness-retry timer after the new event.
    assert len(FakeTimer.instances) == 1
    assert tracker._timers[comic_dir] is FakeTimer.instances[0]
    assert comic_dir not in tracker._readiness_retry_timers


# --- missing/empty directory does not loop -------------------------------------------------


def test_missing_directory_stops_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe_mock = Mock()
    monkeypatch.setattr(watcher, "probe_cbz_directory_readiness", probe_mock)
    process_mock = Mock()
    monkeypatch.setattr(watcher, "process_and_move_directory", process_mock)
    _patch_timer(monkeypatch)

    tracker = DirectorySettleTracker()
    watcher._process_directory_when_ready(tmp_path / "does-not-exist", tracker)

    probe_mock.assert_not_called()
    process_mock.assert_not_called()
    assert FakeTimer.instances == []


def test_empty_directory_stops_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_dir = tmp_path / "Empty Series"
    empty_dir.mkdir()

    probe_mock = Mock()
    monkeypatch.setattr(watcher, "probe_cbz_directory_readiness", probe_mock)
    process_mock = Mock()
    monkeypatch.setattr(watcher, "process_and_move_directory", process_mock)
    _patch_timer(monkeypatch)

    tracker = DirectorySettleTracker()
    watcher._process_directory_when_ready(empty_dir, tracker)

    probe_mock.assert_not_called()
    process_mock.assert_not_called()
    assert FakeTimer.instances == []


# --- startup discovery uses the readiness path -------------------------------------------------


def test_startup_discovery_uses_readiness_gate_when_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watch_root = tmp_path / "incoming"
    comic_dir = watch_root / "Series F"
    ready_cbz = comic_dir / "issue-01.cbz"
    blocked_cbz = comic_dir / "issue-02.cbz"
    _make_cbz(ready_cbz)
    _make_cbz(blocked_cbz)

    monkeypatch.setattr(
        watcher,
        "probe_cbz_directory_readiness",
        lambda paths, **kwargs: _not_ready_result([ready_cbz], blocked_cbz),
    )
    process_mock = Mock()
    monkeypatch.setattr(watcher, "process_and_move_directory", process_mock)
    _patch_timer(monkeypatch)

    tracker = DirectorySettleTracker()
    watcher._discover_startup_directories(watch_root, tracker)

    process_mock.assert_not_called()
    assert len(FakeTimer.instances) == 1
    assert comic_dir in tracker._readiness_retry_timers


def test_startup_discovery_processes_ready_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watch_root = tmp_path / "incoming"
    comic_dir = watch_root / "Series G"
    cbz = comic_dir / "issue-01.cbz"
    _make_cbz(cbz)

    monkeypatch.setattr(
        watcher,
        "probe_cbz_directory_readiness",
        lambda paths, **kwargs: _ready_result(list(paths)),
    )
    process_mock = Mock()
    monkeypatch.setattr(watcher, "process_and_move_directory", process_mock)

    tracker = DirectorySettleTracker()
    watcher._discover_startup_directories(watch_root, tracker)

    process_mock.assert_called_once_with(comic_dir)


# --- no filesystem mutation before readiness passes -------------------------------------------------


def test_no_mutation_occurs_before_readiness_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comic_dir = tmp_path / "Series H"
    ready_cbz = comic_dir / "issue-01.cbz"
    truncated_cbz = comic_dir / "issue-02.cbz"
    _make_cbz(ready_cbz, payload_size=200)
    _make_cbz(truncated_cbz, payload_size=500)
    _truncate(truncated_cbz)

    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (ready_cbz, truncated_cbz)
    }
    before_listing = sorted(p.name for p in comic_dir.iterdir())

    # Use the real, unmocked probe_cbz_directory_readiness -- the truncated
    # file naturally reports RETRY_LATER without any mocking.
    process_mock = Mock()
    monkeypatch.setattr(watcher, "process_and_move_directory", process_mock)
    _patch_timer(monkeypatch)

    tracker = DirectorySettleTracker()
    watcher._process_directory_when_ready(comic_dir, tracker)

    process_mock.assert_not_called()

    after_listing = sorted(p.name for p in comic_dir.iterdir())
    assert after_listing == before_listing

    for path, (before_bytes, before_mtime_ns) in before.items():
        assert path.read_bytes() == before_bytes
        assert path.stat().st_mtime_ns == before_mtime_ns
