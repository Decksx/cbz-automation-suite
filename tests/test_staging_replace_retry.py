"""Bounded retry of staging renames on transient Windows locks (#82).

A directory rename on Windows fails with ERROR_ACCESS_DENIED (WinError 5)
while a process holds a handle anywhere inside the directory that was opened
without FILE_SHARE_DELETE (as Python's `open` does). Which process held it in
#82's flaky runs was not identified; a scanner or indexer is a hypothesis.
These tests pin down what `_replace` retries, what it never retries, what it
rechecks during the wait, that exhausting the retries moves nothing, and that
no destructive or decision-recording step trusts a check made before a wait.

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


# ── the copied source is never deleted after a wait it was not checked through ──
# codex-lead's #94 review: with delete_copied_source, a lock-retry wait is time in
# which a new chapter can land in the source. The copy predates it, so deleting the
# source would lose it. Each test fails one rename once, adds a chapter during the
# wait, and requires the deletion to be refused with the new bytes intact.

NEW_CHAPTER = ("ch03.cbz", b"arrived during the wait")


def _lock_once_then_add_chapter(monkeypatch, src, matches):
    """Fail the first rename whose (src, dst) satisfies *matches*; during that
    retry's wait, write a new chapter into the library source."""
    real_replace, failed = os.replace, []

    def replace(a, b):
        if not failed and matches(os.fspath(a), os.fspath(b)):
            failed.append((a, b))
            raise _Locked(5)
        real_replace(a, b)

    def sleep(_delay):
        (src / NEW_CHAPTER[0]).write_bytes(NEW_CHAPTER[1])

    monkeypatch.setattr(staging, "os", _OsProxy(replace))
    monkeypatch.setattr(staging.time, "sleep", sleep)
    return failed


def _source_kept_with_new_chapter(src):
    assert src.is_dir(), "the source was deleted"
    assert (src / NEW_CHAPTER[0]).read_bytes() == NEW_CHAPTER[1]
    assert (src / "ch01.cbz").read_bytes() == DEFAULT["ch01.cbz"]


def test_transfer_keeps_a_source_that_changed_during_a_publish_retry(tmp_path, monkeypatch):
    _staging, src, plan = _forced_plan(tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    pending = os.fspath(plan.layout.pending_case(plan.case_id))
    failed = _lock_once_then_add_chapter(monkeypatch, src, lambda a, b: b == pending)
    with pytest.raises(SourceChangedError):
        execute_transfer(plan, delete_copied_source=True)
    assert failed, "the publish was never locked"
    _source_kept_with_new_chapter(src)
    assert _staged_files(plan.layout, plan.case_id, "src") == DEFAULT


def test_transfer_keeps_a_source_that_changed_during_a_manifest_write_retry(tmp_path, monkeypatch):
    _staging, src, plan = _forced_plan(tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    manifest = os.fspath(plan.layout.manifest(plan.case_id))
    published = plan.layout.pending_case(plan.case_id)
    failed = _lock_once_then_add_chapter(
        monkeypatch, src, lambda a, b: b == manifest and published.exists())
    with pytest.raises(SourceChangedError):
        execute_transfer(plan, delete_copied_source=True)
    assert failed, "the post-publication manifest write was never locked"
    _source_kept_with_new_chapter(src)


def test_recovery_resume_keeps_a_source_that_changed_during_a_publish_retry(tmp_path, monkeypatch):
    from scripts.cbz_classification_staging import (
        RECOVERY_RESUME_PUBLICATION, assess_recovery, recover_case)
    from tests.test_classification_staging import _interrupt_before_publication, _rebuilt_from_disk
    _staging, src, plan = _interrupt_before_publication(tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    layout, case_id = _rebuilt_from_disk(tmp_path / "review")
    assert assess_recovery(layout, case_id).state == RECOVERY_RESUME_PUBLICATION
    pending = os.fspath(layout.pending_case(case_id))
    failed = _lock_once_then_add_chapter(monkeypatch, src, lambda a, b: b == pending)
    with pytest.raises(SourceChangedError):
        recover_case(layout, case_id, delete_copied_source=True)
    assert failed, "the resumed publish was never locked"
    _source_kept_with_new_chapter(src)


def test_recovery_restart_keeps_a_source_that_changed_during_a_publish_retry(tmp_path, monkeypatch):
    from scripts.cbz_classification_staging import (
        RECOVERY_RESTART_FROM_SOURCE, assess_recovery, recover_case)
    from tests.test_classification_staging import _interrupt_before_publication, _rebuilt_from_disk
    _staging, src, plan = _interrupt_before_publication(tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    layout, case_id = _rebuilt_from_disk(tmp_path / "review")
    (layout.partial_payload(case_id) / "src" / "ch01.cbz").write_bytes(b"unusable copy")
    assert assess_recovery(layout, case_id).state == RECOVERY_RESTART_FROM_SOURCE
    pending = os.fspath(layout.pending_case(case_id))
    failed = _lock_once_then_add_chapter(monkeypatch, src, lambda a, b: b == pending)
    with pytest.raises(SourceChangedError):
        recover_case(layout, case_id, delete_copied_source=True)
    assert failed, "the restarted publish was never locked"
    _source_kept_with_new_chapter(src)


def test_an_unchanged_source_is_still_released_after_a_retry(tmp_path, monkeypatch):
    """The guard refuses only a changed source: a lock alone still completes the release."""
    _staging, src, plan = _forced_plan(tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    pending = os.fspath(plan.layout.pending_case(plan.case_id))
    real_replace, failed = os.replace, []

    def replace(a, b):
        if not failed and os.fspath(b) == pending:
            failed.append(1)
            raise _Locked(5)
        real_replace(a, b)

    monkeypatch.setattr(staging, "os", _OsProxy(replace))
    monkeypatch.setattr(staging.time, "sleep", lambda _d: None)
    manifest = execute_transfer(plan, delete_copied_source=True)
    assert failed and manifest.state == STATE_PENDING_REVIEW
    assert not src.exists(), "an unchanged copied source is released as asked"
    assert _staged_files(plan.layout, plan.case_id, "src") == DEFAULT


# ── operator moves recheck their preconditions after a lock wait ────
# codex-lead's #94 review: promote/rollback verified the staged payload once, then a
# retried move carried whatever was there after the wait. Each test changes something
# during the wait and asserts the refusal and where the bytes actually are.


def _operator_lock_once(monkeypatch, dst, during_wait):
    """Fail the first rename onto *dst*; run *during_wait* in the retry's wait."""
    real_replace, failed = os.replace, []

    def replace(a, b):
        if not failed and os.fspath(b) == os.fspath(dst):
            failed.append(1)
            raise _Locked(5)
        real_replace(a, b)

    monkeypatch.setattr(staging, "os", _OsProxy(replace))
    monkeypatch.setattr(staging.time, "sleep", lambda _d: during_wait())
    return failed


def _published_case(tmp_path, monkeypatch):
    from tests.test_classification_staging import _published
    from scripts.cbz_classification_staging import show_case
    _s, src, layout, case_id = _published(tmp_path, monkeypatch, METHOD_RENAME)
    return src, layout, case_id, show_case(layout, case_id).record_digest


def test_a_promotion_retry_refuses_a_payload_changed_during_the_wait(tmp_path, monkeypatch):
    from scripts.cbz_classification_staging import promote_case
    src, layout, case_id, digest = _published_case(tmp_path, monkeypatch)
    staged = layout.payload(case_id) / "src" / "ch01.cbz"
    destination = tmp_path / "library" / "src"
    failed = _operator_lock_once(monkeypatch, destination,
                                 lambda: staged.write_bytes(b"changed during the wait"))
    outcome = promote_case(layout, case_id, expected_record_digest=digest,
                           destination_root=tmp_path / "library")
    assert failed, "the promotion was never locked"
    assert outcome.acted is False and "does not match" in outcome.detail
    assert staged.read_bytes() == b"changed during the wait", "the changed bytes stay staged"
    assert not destination.exists(), "nothing unverified reached the library"
    assert layout.pending_case(case_id).exists()


def test_a_rollback_retry_refuses_a_payload_changed_during_the_wait(tmp_path, monkeypatch):
    from scripts.cbz_classification_staging import rollback_case
    src, layout, case_id, digest = _published_case(tmp_path, monkeypatch)
    assert not src.exists(), "precondition: the rename moved the source"
    staged = layout.payload(case_id) / "src" / "ch01.cbz"
    failed = _operator_lock_once(monkeypatch, src,
                                 lambda: staged.write_bytes(b"changed during the wait"))
    outcome = rollback_case(layout, case_id, expected_record_digest=digest)
    assert failed, "the rollback was never locked"
    assert outcome.acted is False and "does not match" in outcome.detail
    assert staged.read_bytes() == b"changed during the wait"
    assert not src.exists(), "nothing unverified went back to the original path"


def test_a_rejection_retry_refuses_a_destination_that_appeared_during_the_wait(
        tmp_path, monkeypatch):
    from scripts.cbz_classification_staging import reject_case
    src, layout, case_id, digest = _published_case(tmp_path, monkeypatch)
    destination = layout.rejected / case_id
    failed = _operator_lock_once(monkeypatch, destination,
                                 lambda: (destination / "someone-else").mkdir(parents=True))
    outcome = reject_case(layout, case_id, expected_record_digest=digest)
    assert failed, "the rejection was never locked"
    assert outcome.acted is False and "appeared while waiting" in outcome.detail
    assert layout.pending_case(case_id).exists(), "the case stays pending"
    assert (destination / "someone-else").is_dir(), "the newcomer is not merged into"


def test_an_unchanged_case_is_still_promoted_after_a_lock_wait(tmp_path, monkeypatch):
    from scripts.cbz_classification_staging import STATE_PROMOTED, promote_case
    src, layout, case_id, digest = _published_case(tmp_path, monkeypatch)
    destination = tmp_path / "library" / "src"
    failed = _operator_lock_once(monkeypatch, destination, lambda: None)
    outcome = promote_case(layout, case_id, expected_record_digest=digest,
                           destination_root=tmp_path / "library")
    assert failed and outcome.acted is True and outcome.manifest.state == STATE_PROMOTED
    assert {p.name: p.read_bytes() for p in destination.iterdir() if p.is_file()} == {
        k: v for k, v in DEFAULT.items() if "/" not in k}


def test_a_promotion_retry_refuses_a_record_changed_during_the_wait(tmp_path, monkeypatch):
    """The decision was made on the record the operator was shown, not on a later one."""
    from scripts.cbz_classification_staging import CaseManifest, promote_case
    src, layout, case_id, digest = _published_case(tmp_path, monkeypatch)
    destination = tmp_path / "library" / "src"

    def amend_record():
        manifest = CaseManifest.from_json(layout.manifest(case_id).read_text(encoding="utf-8"))
        manifest.decision_reason = "a note added while the operator's move waited"
        manifest.write(layout.manifest(case_id))

    failed = _operator_lock_once(monkeypatch, destination, amend_record)
    outcome = promote_case(layout, case_id, expected_record_digest=digest,
                           destination_root=tmp_path / "library")
    assert failed, "the promotion was never locked"
    assert outcome.acted is False and "decide again" in outcome.detail
    assert not destination.exists() and layout.pending_case(case_id).exists()
