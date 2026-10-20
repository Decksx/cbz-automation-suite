"""Bounded retry of staging renames on transient Windows locks (#82).

A directory rename on Windows fails with ERROR_ACCESS_DENIED (WinError 5)
while any process holds a handle anywhere inside the directory; antivirus and
indexers take such handles briefly on freshly written trees. These tests pin
down what `_replace` retries, what it never retries, what it rechecks during
the wait, and that exhausting the retries moves nothing.

The unit tests drive a fake `os.replace` and record sleeps instead of waiting,
so they run on any OS. The held-handle tests use real Windows handles and are
skipped elsewhere.
"""

from __future__ import annotations

import os
import threading

import pytest

import scripts.cbz_classification_staging as staging
from scripts.cbz_classification_staging import (
    METHOD_COPY_VERIFY,
    METHOD_RENAME,
    REPLACE_RETRY_DELAYS,
    STATE_PENDING_REVIEW,
    SourceChangedError,
    execute_transfer,
)
from tests.test_classification_staging import DEFAULT, _forced_plan, _staged_files

windows_only = pytest.mark.skipif(os.name != "nt", reason="real Windows handle semantics")


class _Locked(PermissionError):
    """A PermissionError carrying a Windows error code, on any OS."""

    def __init__(self, winerror: int = 5):
        super().__init__(13, "Access is denied")
        self.winerror = winerror


class _OsProxy:
    """Stands in for the module's `os`, with only `replace` replaced."""

    def __init__(self, replace):
        self.replace = replace

    def __getattr__(self, name):
        return getattr(os, name)


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """A fake os.replace driven by a script of outcomes, and recorded sleeps."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    calls, sleeps, script = [], [], []

    def fake_replace(a, b):
        calls.append((a, b))
        outcome = script.pop(0) if script else None
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            outcome()
        os.replace(a, b)

    monkeypatch.setattr(staging, "os", _OsProxy(fake_replace))
    monkeypatch.setattr(staging.time, "sleep", sleeps.append)
    return src, dst, calls, sleeps, script


# ── what is retried ─────────────────────────────────────────────


@pytest.mark.parametrize("code", [5, 32])
def test_a_transient_lock_is_retried_until_the_rename_succeeds(harness, code):
    src, dst, calls, sleeps, script = harness
    script[:] = [_Locked(code), _Locked(code)]
    staging._replace(src, dst)
    assert len(calls) == 3
    assert sleeps == list(REPLACE_RETRY_DELAYS[:2])
    assert dst.is_dir() and not src.exists()


def test_the_retries_are_bounded_and_the_last_error_is_raised(harness):
    src, dst, calls, sleeps, script = harness
    errors = [_Locked() for _ in range(len(REPLACE_RETRY_DELAYS) + 1)]
    script[:] = list(errors)
    with pytest.raises(PermissionError) as caught:
        staging._replace(src, dst)
    assert caught.value is errors[-1]
    assert len(calls) == len(REPLACE_RETRY_DELAYS) + 1
    assert sleeps == list(REPLACE_RETRY_DELAYS)
    assert src.is_dir() and not dst.exists(), "exhausting the retries moved nothing"


# ── what is never retried ───────────────────────────────────────


@pytest.mark.parametrize("error", [
    PermissionError(13, "denied"),            # no Windows code: POSIX, or a real ACL denial
    _Locked(1314),                            # a Windows code that is not a transient lock
    FileNotFoundError(2, "gone"),
    OSError(18, "cross-device link"),
])
def test_other_errors_are_raised_at_once(harness, error):
    src, dst, calls, sleeps, script = harness
    script[:] = [error]
    with pytest.raises(type(error)) as caught:
        staging._replace(src, dst)
    assert caught.value is error
    assert len(calls) == 1 and sleeps == []


# ── what is rechecked during the wait ───────────────────────────


def test_a_source_that_vanished_during_the_wait_is_not_retried(harness, monkeypatch):
    src, dst, calls, sleeps, script = harness
    first = _Locked()

    def vanish(_delay):
        sleeps.append(_delay)
        src.rmdir()

    monkeypatch.setattr(staging.time, "sleep", vanish)
    script[:] = [first]
    with pytest.raises(PermissionError) as caught:
        staging._replace(src, dst)
    assert caught.value is first, "the original lock is reported, not masked"
    assert len(calls) == 1


def test_the_recheck_runs_before_every_retry(harness):
    src, dst, calls, sleeps, script = harness
    script[:] = [_Locked(), _Locked()]
    checks = []
    staging._replace(src, dst, recheck=lambda: checks.append(len(calls)))
    assert checks == [1, 2], "once after each failed attempt, before the next"
    assert len(calls) == 3


def test_a_failed_recheck_stops_the_rename(harness):
    src, dst, calls, sleeps, script = harness
    script[:] = [_Locked()]

    def changed():
        raise SourceChangedError("source changed during the wait")

    with pytest.raises(SourceChangedError):
        staging._replace(src, dst, recheck=changed)
    assert len(calls) == 1
    assert src.is_dir() and not dst.exists()


# ── real Windows handles ────────────────────────────────────────


def _hold(path, release_after: float | None):
    """Open *path* and keep the handle, closing it after *release_after* s."""
    handle = open(path, "rb")
    if release_after is not None:
        threading.Timer(release_after, handle.close).start()
    return handle


@windows_only
def test_a_handle_inside_a_directory_blocks_a_plain_rename(tmp_path):
    """The mechanism itself, measured rather than assumed."""
    src, dst = tmp_path / "partial", tmp_path / "pending"
    (src / "deep").mkdir(parents=True)
    (src / "deep" / "page.jpg").write_bytes(b"x")
    handle = _hold(src / "deep" / "page.jpg", None)
    try:
        with pytest.raises(PermissionError) as caught:
            os.replace(src, dst)
        assert caught.value.winerror == 5
    finally:
        handle.close()


@windows_only
def test_a_briefly_held_handle_no_longer_fails_the_rename(tmp_path):
    src, dst = tmp_path / "partial", tmp_path / "pending"
    (src / "deep").mkdir(parents=True)
    (src / "deep" / "page.jpg").write_bytes(b"x")
    _hold(src / "deep" / "page.jpg", 0.2)
    staging._replace(src, dst)
    assert (dst / "deep" / "page.jpg").read_bytes() == b"x"


@windows_only
def test_a_handle_held_past_the_retries_raises_with_nothing_moved(tmp_path, monkeypatch):
    monkeypatch.setattr(staging, "REPLACE_RETRY_DELAYS", (0.01, 0.01))
    src, dst = tmp_path / "partial", tmp_path / "pending"
    src.mkdir()
    (src / "page.jpg").write_bytes(b"x")
    handle = _hold(src / "page.jpg", None)
    try:
        with pytest.raises(PermissionError) as caught:
            staging._replace(src, dst)
        assert caught.value.winerror == 5
        assert (src / "page.jpg").exists() and not dst.exists()
    finally:
        handle.close()


@windows_only
@pytest.mark.parametrize("method", [METHOD_COPY_VERIFY, METHOD_RENAME])
def test_a_transfer_publishes_while_a_scanner_briefly_holds_the_partial_case(
        tmp_path, monkeypatch, method):
    """#82's failing call: publishing .partial while something holds a file in it."""
    _staging, _src, plan = _forced_plan(tmp_path, monkeypatch, method)
    partial = plan.layout.partial_case(plan.case_id)
    real_replace = os.replace
    held = []

    def scanner_first(a, b):
        if not held and os.fspath(a) == os.fspath(partial):
            target = next(p for p in partial.rglob("*") if p.is_file())
            held.append(_hold(target, 0.2))
        real_replace(a, b)

    monkeypatch.setattr(staging, "os", _OsProxy(scanner_first))
    manifest = execute_transfer(plan)
    assert held, "the scanner never held the partial case"
    assert manifest.state == STATE_PENDING_REVIEW
    assert _staged_files(plan.layout, plan.case_id, "src") == DEFAULT


@windows_only
def test_the_rename_path_revalidates_the_library_source_before_retrying(
        tmp_path, monkeypatch):
    """The source move waits on a lock; the source is checked again after the wait."""
    _staging, src, plan = _forced_plan(tmp_path, monkeypatch, METHOD_RENAME)
    real_replace, real_revalidate = os.replace, staging.revalidate_source
    held, checks = [], []

    def scanner_first(a, b):
        if not held and os.fspath(a) == os.fspath(src):
            held.append(_hold(next(p for p in src.rglob("*") if p.is_file()), 0.2))
        real_replace(a, b)

    def counted(path, expected):
        checks.append(os.fspath(path))
        return real_revalidate(path, expected)

    monkeypatch.setattr(staging, "os", _OsProxy(scanner_first))
    monkeypatch.setattr(staging, "revalidate_source", counted)
    manifest = execute_transfer(plan)
    assert held, "the scanner never held the source"
    assert manifest.state == STATE_PENDING_REVIEW
    assert checks.count(os.fspath(src)) >= 2, "revalidated before the move and again after a wait"
    assert not src.exists() and _staged_files(plan.layout, plan.case_id, "src") == DEFAULT


@windows_only
def test_recovery_revalidates_the_library_source_before_retrying_its_move(
        tmp_path, monkeypatch):
    """Recovery's rename restart moves the library source; a wait means a recheck."""
    from scripts.cbz_classification_staging import (
        RECOVERY_RESTART_FROM_SOURCE, STATE_PLANNED, assess_recovery, recover_case)
    _staging, src, plan = _forced_plan(tmp_path, monkeypatch, METHOD_RENAME)
    layout, case_id = plan.layout, plan.case_id
    for directory in layout.all_dirs():                       # as execute_transfer does first
        directory.mkdir(parents=True, exist_ok=True)
    plan.manifest.state = STATE_PLANNED
    plan.manifest.touch()
    plan.manifest.write(layout.manifest(case_id))
    layout.partial_payload(case_id).mkdir(parents=True)      # scaffolding only
    assert assess_recovery(layout, case_id).state == RECOVERY_RESTART_FROM_SOURCE

    real_replace, real_revalidate = os.replace, staging.revalidate_source
    held, checks = [], []

    def scanner_first(a, b):
        if not held and os.fspath(a) == os.fspath(src):
            held.append(_hold(next(p for p in src.rglob("*") if p.is_file()), 0.2))
        real_replace(a, b)

    def counted(path, expected):
        checks.append(os.fspath(path))
        return real_revalidate(path, expected)

    monkeypatch.setattr(staging, "os", _OsProxy(scanner_first))
    monkeypatch.setattr(staging, "revalidate_source", counted)
    outcome = recover_case(layout, case_id)
    assert held, "the scanner never held the source"
    assert outcome.acted is True
    assert checks.count(os.fspath(src)) >= 2, "revalidated before the move and again after a wait"
    assert _staged_files(layout, case_id, "src") == DEFAULT


def test_every_rename_in_the_staging_module_goes_through_the_retry():
    """No direct os.replace outside `_replace`, so no rename site can regress to unretried.

    The behaviour is proven on `_replace` above; this keeps every call site
    (publish, recovery, promote, reject, rollback, the manifest write) on it.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(staging))
    direct = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)) or func.name == "_replace":
            continue
        for node in ast.walk(func):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("replace", "rename")
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "os"):
                direct.append(f"{func.name}:{node.lineno}")
    assert direct == [], f"direct os.replace/os.rename bypasses the retry: {direct}"
