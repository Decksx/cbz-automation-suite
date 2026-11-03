"""Disposable native runner witnesses, with explicit release/ready handshakes.

The real private ledger, contained child, pipe and log workers are used unless
a test explicitly isolates a denied native observation. No live tool adapter.
"""
import dataclasses
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from comic_automation.database import dal
from comic_automation.web import state_dir as sd
from comic_automation.web.jobs import runner as r, win32, receipts
from comic_automation.web.jobs.launch_spec import FileSeal, LaunchSpec
from comic_automation.web.jobs.logs import LogDisk, LogPump
from comic_automation.web.jobs.store import JobRefused, JobStore, VerifiedRequest, ProcessIdentity
from web_support import private_dir

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows contained runner")
SHORT = r.Limits(poll=.01, descendants=.3, termination=.5, drain=1, cleanup=.3)
NORMAL = r.Limits(poll=.01)


def until(owner, predicate, timeout=15):
    """Poll an actual condition; timeout is only a failing-test watchdog."""
    end = time.monotonic() + timeout
    while not predicate():
        owner.tick()
        assert time.monotonic() < end, (owner.phase, owner.reason, owner.row,
                                       owner.io.kind if owner.io else None)
        time.sleep(.005)


@pytest.fixture
def fixture(tmp_path):
    state = sd.open_state_dir(private_dir(tmp_path / "state"), environ={})
    owners = []
    def prepare(body="import os;os.write(1,b'fixture\\n')", *, readonly=True, limits=NORMAL, clock=time.monotonic):
        source = tmp_path / "tool.py"
        registry = tmp_path / "registry.json"
        source.write_text(body, encoding="utf-8")
        registry.write_text('{"fixture":true}', encoding="utf-8")
        exe = Path(sys.executable)
        files = tuple(FileSeal(role, path, hashlib.sha256(path.read_bytes()).hexdigest())
                      for role, path in (("interpreter", exe), ("source", source), ("registry", registry)))
        spec = LaunchSpec(exe, (str(source),), tmp_path,
            {"SYSTEMROOT": os.environ["SYSTEMROOT"], "TEMP": str(tmp_path), "TMP": str(tmp_path)},
            files, "fixture-v1", lambda code: "succeeded" if code == 0 else "failed")
        store = JobStore(state, verifier=lambda _: VerifiedRequest("fixture", "tester", spec.seal_document(),
            readonly=readonly), watcher_observation=lambda: False)
        row = store.admit({})
        def start(registry=lambda command: spec):
            owner = r.Runner.start(store, row["job_id"], row["nonce"], registry=registry,
                                   limits=limits, clock=clock)
            owners.append(owner)
            return owner
        return store, row, spec, start
    yield prepare
    # Only disposable fixture children are force-cleaned after assertions.
    for owner in owners:
        if owner.process is not None and owner.process._job.value:
            owner.process.terminate()
            owner.process.wait_root(5)
            end = time.monotonic() + 5
            while owner.process.active_processes() and time.monotonic() < end:
                time.sleep(.01)
            if owner.pump:
                owner.pump.join(5)
            owner.process.close()
        if not owner.claim._closed:
            owner.claim.close()
    state.close()


@pytest.mark.parametrize("code,state", [(0, "succeeded"), (7, "failed")])
def test_runner_commits_only_after_native_log_and_receipt_proof(fixture, code, state):
    store, row, _, start = fixture(f"import os,sys;os.write(1,b'hello\\n');sys.exit({code})")
    owner = start()
    until(owner, lambda: owner.done)
    final = store.get(row["job_id"])
    assert final["state"] == state and final["exit_code"] == code, dict(
        reason=owner.reason, phase=owner.phase, log_error=owner.pump.error,
        budgets=dataclasses.asdict(owner.limits), events=store.events(row["job_id"]))
    assert owner.claim._closed and not owner.pump.reader.is_alive() and not owner.pump.writer.is_alive()
    receipt = json.loads((store.path / "jobs" / row["job_id"] / "exit.json").read_bytes())
    assert receipt["logs"]["complete"] and receipt["logs"]["durable_offset"] == 6
    assert [e["event"] for e in store.events(row["job_id"])] == ["admitted", "claimed", "running", state]


@pytest.mark.parametrize("role", ["source", "interpreter", "registry"])
def test_changed_file_seal_refuses_without_native_birth(fixture, monkeypatch, role):
    store, row, spec, start = fixture()
    files = tuple(dataclasses.replace(f, sha256="0" * 64) if f.role == role else f for f in spec.files)
    # Keep the immutable admission seal intact; the reconstructed bytes disagree.
    monkeypatch.setattr(win32, "launch", lambda *a, **k: pytest.fail("unexpected birth"))
    owner = start(lambda command: dataclasses.replace(spec, files=files))
    until(owner, lambda: owner.done)
    assert store.get(row["job_id"])["state"] == "refused"


@pytest.mark.parametrize("change", ["argv", "environment", "cwd", "outcome", "policy", "missing"])
def test_reconstruction_or_missing_adapter_cannot_select_new_launch(fixture, monkeypatch, change):
    store, row, spec, start = fixture()
    values = dict(argv={"arguments": ("-c", "raise Exception")},
                  environment={"environment": {"SYSTEMROOT": "elsewhere"}},
                  cwd={"cwd": spec.cwd.parent}, outcome={"outcome_id": "other"},
                  policy={"stop_policy": "force"})
    monkeypatch.setattr(win32, "launch", lambda *a, **k: pytest.fail("unexpected birth"))
    registry = None if change == "missing" else lambda command: dataclasses.replace(spec, **values[change])
    owner = start(registry)
    until(owner, lambda: owner.done)
    assert store.get(row["job_id"])["state"] == "refused"


def test_source_change_after_log_preparation_is_rehashed_before_birth(fixture, monkeypatch):
    store, row, spec, start = fixture()
    create = r.JobLogs.create_writer
    def changed(self, *args, **kwargs):
        result = create(self, *args, **kwargs)
        next(f.path for f in spec.files if f.role == "source").write_text("raise Exception('changed')")
        return result
    monkeypatch.setattr(r.JobLogs, "create_writer", changed)
    monkeypatch.setattr(win32, "launch", lambda *a, **k: pytest.fail("unexpected birth"))
    owner = start()
    until(owner, lambda: owner.done)
    assert store.get(row["job_id"])["state"] == "refused"


@pytest.mark.parametrize("failure", ["collision", "possible_birth", "known_no_birth"])
def test_native_failure_classification_never_relaunches(fixture, monkeypatch, failure):
    store, row, _, start = fixture()
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        if failure == "collision":
            raise win32.JobCollision("fixture", 183)
        raise win32.NativeLaunchError("fixture", 5, child_created=failure == "possible_birth")
    monkeypatch.setattr(win32, "launch", fail)
    owner = start()
    until(owner, lambda: owner.done)
    assert calls == [1]
    assert store.get(row["job_id"])["state"] == ("refused" if failure == "known_no_birth" else "uncertain")


def test_postbirth_running_commit_failure_retains_admission_without_relaunch(fixture, monkeypatch):
    store, row, _, start = fixture()
    def fail(*args):
        raise OSError("injected running commit failure")
    monkeypatch.setattr(store, "mark_running", fail)
    native = win32.launch
    calls = []
    def count(*args, **kwargs):
        calls.append(1)
        return native(*args, **kwargs)
    monkeypatch.setattr(win32, "launch", count)
    owner = start()
    until(owner, lambda: owner.done)
    assert calls == [1] and owner.process.identity.pid > 0
    assert store.get(row["job_id"])["state"] == "uncertain"
    assert not (store.path / "jobs" / row["job_id"] / "exit.json").exists()
    with pytest.raises(JobRefused, match="UNCERTAIN"):
        store.admit({})


def test_failed_writer_keeps_draining_a_writing_child_beyond_pipe_capacity(fixture, monkeypatch, tmp_path):
    ready, release, finished = (tmp_path / n for n in ("ready", "release", "finished"))
    # Writer fails on the first chunk; only then does the test release the child
    # to emit 4 MiB. Premature reader cancellation strands this child in os.write.
    body = (f"import os,time,pathlib;os.write(1,b'first');pathlib.Path({str(ready)!r}).touch()\n"
            f"while not pathlib.Path({str(release)!r}).exists(): time.sleep(.005)\n"
            "for _ in range(1024): os.write(1,b'x'*4096)\n"
            f"pathlib.Path({str(finished)!r}).touch()\n")
    store, row, _, start = fixture(body, readonly=False)
    failed = threading.Event()
    def boundary_failure(*args):
        failed.set()
        raise sd.StateDirRefused("injected boundary failure")
    monkeypatch.setattr(LogDisk, "append", boundary_failure)
    owner = start()
    until(owner, lambda: ready.exists() and failed.is_set() and owner.uncertain_saved)
    assert owner.process.wait_root(0) is None and not owner.pump.abort.is_set()
    release.touch()
    until(owner, lambda: owner.done)
    assert finished.exists() and owner.pump.queue.offset == 5 + 4 * 1024 * 1024
    assert store.get(row["job_id"])["state"] == "uncertain"
    assert owner.pump.queue.complete and not owner.pump.disk.snapshot["complete"]


@pytest.mark.parametrize("holds_pipe", [True, False])
def test_descendant_grace_starts_at_root_exit_independent_of_eof(fixture, monkeypatch, tmp_path, holds_pipe):
    marker = tmp_path / "descendant-ready"
    child = (f"import pathlib,time;pathlib.Path({str(marker)!r}).touch();time.sleep(60)")
    redirection = "" if holds_pipe else ",stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL"
    body = (f"import subprocess,sys,time,pathlib;subprocess.Popen([sys.executable,'-c',{child!r}]"
            f"{redirection})\nwhile not pathlib.Path({str(marker)!r}).exists(): time.sleep(.005)\n")
    store, row, _, start = fixture(body, limits=SHORT)
    owner = start()
    until(owner, lambda: owner.root_exit is not None)
    assert marker.exists() and owner.process.active_processes() > 0
    assert owner.pump.reader.is_alive() if holds_pipe else True
    until(owner, lambda: owner.done)
    assert owner.terminate_at is not None
    assert owner.terminate_at - owner.root_exit >= SHORT.descendants
    assert store.get(row["job_id"])["state"] == "succeeded"


def test_stalled_running_commit_cannot_block_descendant_supervision(fixture, monkeypatch, tmp_path):
    ready = tmp_path / "ready"
    child = f"import pathlib,time;pathlib.Path({str(ready)!r}).touch();time.sleep(60)"
    body = (f"import subprocess,sys,pathlib,time;subprocess.Popen([sys.executable,'-c',{child!r}])\n"
            f"while not pathlib.Path({str(ready)!r}).exists(): time.sleep(.005)\n")
    store, row, _, start = fixture(body, limits=SHORT)
    entered, release = threading.Event(), threading.Event()
    original = store.mark_running
    def blocked(*args):
        entered.set()
        assert release.wait(15)
        return original(*args)
    monkeypatch.setattr(store, "mark_running", blocked)
    owner = start()
    try:
        until(owner, lambda: entered.is_set() and owner.terminate_at is not None)
        assert owner.io.kind == "running" and not owner.claim._closed
        assert owner.process.wait_root(0) is not None
    finally:
        release.set()
    until(owner, lambda: owner.done)
    assert store.get(row["job_id"])["state"] == "succeeded"


def test_slow_receipt_publication_retains_owner_and_waits_for_actual_proof(fixture, monkeypatch):
    now = [0.]
    store, row, _, start = fixture(clock=lambda: now[0])
    entered, release = threading.Event(), threading.Event()
    original = receipts.publish
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(15)
        return original(*args, **kwargs)
    monkeypatch.setattr(receipts, "publish", blocked)
    owner = start()
    try:
        until(owner, lambda: entered.is_set())
        now[0] = 6
        owner.tick()
        assert owner.reason is None and owner.phase == "waiting_for_publish"
        assert not owner.claim._closed
        assert not owner.done and owner.process._job.value
    finally:
        release.set()
    until(owner, lambda: owner.done)
    assert store.get(row["job_id"])["state"] == "succeeded"
    assert (store.path / "jobs" / row["job_id"] / "exit.json").exists()


def test_stop_intent_does_not_force_a_live_writing_root(fixture, monkeypatch, tmp_path):
    ready, release = tmp_path / "ready", tmp_path / "release"
    body = (f"import pathlib,time;pathlib.Path({str(ready)!r}).touch()\n"
            f"while not pathlib.Path({str(release)!r}).exists(): time.sleep(.005)\n")
    store, row, _, start = fixture(body, readonly=False)
    owner = start()
    until(owner, lambda: ready.exists() and owner.running_saved)
    with dal.connection_scope(store.database) as db, dal.transaction(db):
        db.execute("UPDATE jobs SET stop_requested_ms=1 WHERE job_id=?", (row["job_id"],))
    until(owner, lambda: owner.uncertain_saved)
    assert owner.process.wait_root(0) is None and owner.terminate_at is None
    release.touch()
    until(owner, lambda: owner.done)
    assert store.get(row["job_id"])["state"] == "uncertain"


def test_cli_accepts_no_registry_import_or_raw_argv():
    with pytest.raises(SystemExit) as result:
        r.main(["job", "nonce", "--registry", "anything"])
    assert result.value.code == 2


@pytest.mark.parametrize("role", ["source", "interpreter", "registry"])
def test_actual_input_bytes_are_hashed_not_just_seal_metadata(fixture, monkeypatch, role):
    store, row, spec, start = fixture()
    target = next(f.path for f in spec.files if f.role == role)
    original = Path.open
    def changed(self, *args, **kwargs):
        return io.BytesIO(b"changed bytes") if self == target else original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", changed)
    monkeypatch.setattr(win32, "launch", lambda *a, **k: pytest.fail("unexpected birth"))
    owner = start()
    until(owner, lambda: owner.done)
    assert store.get(row["job_id"])["state"] == "refused"


def test_prebirth_log_boundary_failure_refuses_without_a_tool(fixture, monkeypatch):
    store, row, _, start = fixture()
    def fail(*args, **kwargs):
        raise sd.StateDirRefused("injected preparation boundary")
    monkeypatch.setattr(r.JobLogs, "create_writer", fail)
    monkeypatch.setattr(win32, "launch", lambda *a, **k: pytest.fail("unexpected birth"))
    owner = start()
    until(owner, lambda: owner.done)
    assert store.get(row["job_id"])["state"] == "refused"


@pytest.mark.parametrize("worker", ["reader", "writer"])
def test_cleanup_deadline_requests_cancellation_but_retains_unsettled_owner(fixture, monkeypatch, worker):
    now = [0.]
    store, row, _, start = fixture(clock=lambda: now[0], limits=SHORT)
    entered, release = threading.Event(), threading.Event()
    original = LogPump._read if worker == "reader" else LogDisk.append
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(15)
        return original(*args, **kwargs)
    monkeypatch.setattr(LogPump if worker == "reader" else LogDisk,
                        "_read" if worker == "reader" else "append", blocked)
    owner = start()
    try:
        until(owner, lambda: entered.is_set() and owner.zero_at is not None)
        if worker == "writer":
            until(owner, lambda: owner.cleanup_at is not None)
        now[0] = 2
        owner.tick()
        assert owner.cancelled and owner.reason == "LOG_DRAIN_OR_CLEANUP_TIMEOUT"
        assert not owner.done and not owner.claim._closed and owner.process._job.value
        until(owner, lambda: owner.uncertain_saved)
        assert not owner.done  # CancelSynchronousIo cannot settle an Event wait.
        owner.tick()
        assert owner.io is None and not owner.claim._closed
    finally:
        release.set()
    until(owner, lambda: owner.done)
    assert store.get(row["job_id"])["state"] == "uncertain"


def test_denied_job_query_and_termination_request_never_prove_zero(fixture, monkeypatch):
    now = [0.]
    store, row, _, start = fixture(clock=lambda: now[0], limits=SHORT)
    owner = start()
    query = owner.process.active_processes
    requests = []
    def denied():
        raise PermissionError("injected denied query")
    monkeypatch.setattr(owner.process, "active_processes", denied)
    monkeypatch.setattr(owner.process, "terminate", lambda: requests.append(1))
    until(owner, lambda: owner.root_exit is not None)
    now[0] = 1
    owner.tick()
    now[0] = 2
    owner.tick()
    until(owner, lambda: owner.uncertain_saved)
    assert requests == [1] and owner.zero_at is None
    assert not owner.done and not owner.claim._closed
    monkeypatch.setattr(owner.process, "active_processes", query)
    until(owner, lambda: owner.done)
    assert store.get(row["job_id"])["state"] == "uncertain"


def test_termination_deadline_does_not_turn_a_request_into_zero(fixture, monkeypatch):
    now = [0.]
    store, row, _, start = fixture(clock=lambda: now[0], limits=SHORT)
    owner = start()
    query = owner.process.active_processes
    requests = []
    monkeypatch.setattr(owner.process, "active_processes", lambda: 1)
    monkeypatch.setattr(owner.process, "terminate", lambda: requests.append(1))
    until(owner, lambda: owner.root_exit is not None)
    now[0] = 1
    owner.tick()
    assert requests == [1] and owner.reason is None
    now[0] = 2
    owner.tick()
    assert owner.reason == "DESCENDANTS_NOT_PROVEN_ZERO" and owner.zero_at is None
    until(owner, lambda: owner.uncertain_saved)
    assert not owner.claim._closed
    monkeypatch.setattr(owner.process, "active_processes", query)
    until(owner, lambda: owner.done)
    assert store.get(row["job_id"])["state"] == "uncertain"


@pytest.mark.parametrize("stage", ["publish", "finalize"])
def test_receipt_or_terminal_commit_failure_keeps_admission(fixture, monkeypatch, stage):
    store, row, _, start = fixture()
    def fail(*args, **kwargs):
        raise OSError("injected persistence failure")
    monkeypatch.setattr(receipts if stage == "publish" else store, stage, fail)
    owner = start()
    until(owner, lambda: owner.done)
    assert store.get(row["job_id"])["state"] == "uncertain"
    path = store.path / "jobs" / row["job_id"] / "exit.json"
    assert path.exists() == (stage == "finalize")
    with pytest.raises(JobRefused, match="UNCERTAIN"):
        store.admit({})


def test_duplicate_runner_never_makes_a_second_native_birth(fixture, monkeypatch, tmp_path):
    ready, release = tmp_path / "ready", tmp_path / "release"
    body = (f"import pathlib,time;pathlib.Path({str(ready)!r}).touch()\n"
            f"while not pathlib.Path({str(release)!r}).exists(): time.sleep(.005)\n")
    store, row, _, start = fixture(body)
    owner = start()
    until(owner, lambda: ready.exists() and owner.running_saved)
    native = win32.launch
    monkeypatch.setattr(win32, "launch", lambda *a, **k: pytest.fail("duplicate native birth"))
    try:
        with pytest.raises(JobRefused):
            start()
    finally:
        monkeypatch.setattr(win32, "launch", native)
        release.touch()
    until(owner, lambda: owner.done)
    assert store.get(row["job_id"])["state"] == "succeeded"


def test_registry_cannot_mutate_the_ledger_seal_it_is_being_checked_against(fixture, monkeypatch):
    store, row, spec, start = fixture()
    def changed(command):
        other = dataclasses.replace(spec, arguments=("-c", "raise Exception('other')"))
        command["seal"] = other.seal_document()
        return other
    monkeypatch.setattr(win32, "launch", lambda *a, **k: pytest.fail("unexpected birth"))
    owner = start(changed)
    until(owner, lambda: owner.done)
    assert store.get(row["job_id"])["state"] == "refused"


def test_recovered_writer_does_not_restore_eof_or_clear_uncertain(fixture, monkeypatch, tmp_path):
    release = tmp_path / "release"
    body = (f"import os,pathlib,time;os.write(1,b'hello')\n"
            f"while not pathlib.Path({str(release)!r}).exists(): time.sleep(.005)\n")
    store, row, _, start = fixture(body)
    failed, recover, recovered = threading.Event(), threading.Event(), threading.Event()
    append = LogDisk.append
    def retry(self, *args):
        if not recover.is_set():
            failed.set()
            raise OSError("injected transient storage failure")
        result = append(self, *args)
        recovered.set()
        return result
    monkeypatch.setattr(LogDisk, "append", retry)
    owner = start()
    until(owner, lambda: failed.is_set() and owner.uncertain_saved)
    recover.set()
    until(owner, lambda: recovered.is_set())
    release.touch()
    until(owner, lambda: owner.done)
    assert owner.pump.queue.complete and not owner.pump.disk.snapshot["complete"]
    assert store.get(row["job_id"])["state"] == "uncertain"
    assert r.JobLogs(store, row["job_id"]).read_page()["log_status"] == "unknown_tail"


def test_launch_environment_is_copied_and_frozen(fixture):
    _, _, spec, _ = fixture()
    original = {"SYSTEMROOT": os.environ["SYSTEMROOT"]}
    frozen = dataclasses.replace(spec, environment=original)
    original["SYSTEMROOT"] = "changed"
    assert frozen.environment["SYSTEMROOT"] == os.environ["SYSTEMROOT"]
    with pytest.raises(TypeError):
        frozen.environment["NEW"] = "value"


def test_prebirth_stop_intent_refuses_without_starting_a_writing_tool(fixture, monkeypatch):
    store, row, _, start = fixture(readonly=False)
    with dal.connection_scope(store.database) as db, dal.transaction(db):
        db.execute("UPDATE jobs SET stop_requested_ms=1 WHERE job_id=?", (row["job_id"],))
    monkeypatch.setattr(win32, "launch", lambda *a, **k: pytest.fail("unexpected birth"))
    owner = start()
    until(owner, lambda: owner.done)
    assert store.get(row["job_id"])["state"] == "refused"


@pytest.mark.parametrize("stage", ["claim", "birth", "receipt"])
def test_owner_death_keeps_ledger_and_durable_evidence_at_explicit_crash_points(fixture, tmp_path, stage):
    ready, tool_ready, release = (tmp_path / n for n in ("owner-ready", "tool-ready", "release"))
    body = f"import pathlib,time;pathlib.Path({str(tool_ready)!r}).touch()\n"
    if stage == "birth":
        body += f"while not pathlib.Path({str(release)!r}).exists(): time.sleep(.005)\n"
    store, row, _, _ = fixture(body)
    helper = Path(__file__).with_name("runner_job_fixture.py")
    process = subprocess.Popen([sys.executable, str(helper), str(store.path), row["job_id"], row["nonce"],
                                stage, str(ready)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() or stage == "birth" and not tool_ready.exists():
            assert process.poll() is None, process.communicate(timeout=1)
            assert time.monotonic() < deadline, "Owner handshake not observed"
            time.sleep(.01)
        report = json.loads(ready.read_text())
        process.kill()
        process.communicate(timeout=5)
        if report["child"]:
            child = ProcessIdentity(**report["child"])
            deadline = time.monotonic() + 8
            while win32.observe_process(child).state != "gone":
                assert time.monotonic() < deadline
                time.sleep(.01)
        deadline = time.monotonic() + 8
        while win32.observe_job(row["job_id"], report["runner"]["session_id"]).state != "absent":
            assert time.monotonic() < deadline
            time.sleep(.01)
        current = store.get(row["job_id"])
        assert current["state"] == ("running" if stage == "receipt" else "claimed")
        assert (store.path / "jobs" / row["job_id"] / "exit.json").exists() == (stage == "receipt")
        assert tool_ready.exists() == (stage != "claim")
        with pytest.raises(JobRefused):
            store.admit({})
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_inflight_terminal_commit_retains_owner_and_is_not_raced_by_polling(fixture, monkeypatch):
    now = [0.]
    store, row, _, start = fixture(clock=lambda: now[0])
    entered, release = threading.Event(), threading.Event()
    original = store.finalize
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(15)
        return original(*args, **kwargs)
    monkeypatch.setattr(store, "finalize", blocked)
    owner = start()
    try:
        until(owner, lambda: entered.is_set())
        query = owner.process.active_processes
        def query_only_from_committing_worker():
            assert threading.current_thread() is not threading.main_thread(), "commit raced by poller"
            return query()
        monkeypatch.setattr(owner.process, "active_processes", query_only_from_committing_worker)
        now[0] = 6
        owner.tick()
        assert owner.reason is None and owner.io.kind == "finalize"
        assert not owner.claim._closed and not owner.done
    finally:
        release.set()
    until(owner, lambda: owner.done)
    assert store.get(row["job_id"])["state"] == "succeeded"


def test_actual_native_uncertainty_during_slow_publication_stays_sticky(fixture, monkeypatch):
    now = [0.]
    store, row, _, start = fixture(clock=lambda: now[0])
    entered, release = threading.Event(), threading.Event()
    original = receipts.publish
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(15)
        return original(*args, **kwargs)
    monkeypatch.setattr(receipts, "publish", blocked)
    owner = start()
    try:
        until(owner, lambda: entered.is_set())
        query = owner.process.active_processes
        def denied():
            raise PermissionError("injected unknown native observation")
        monkeypatch.setattr(owner.process, "active_processes", denied)
        owner.tick()
        monkeypatch.setattr(owner.process, "active_processes", query)
        assert owner.reason == "NATIVE_OBSERVATION_FAILED:PermissionError"
        now[0] = 6
        owner.tick()
        assert owner.phase == "uncertain" and not owner.claim._closed
    finally:
        release.set()
    until(owner, lambda: owner.done)
    assert store.get(row["job_id"])["state"] == "uncertain"
    assert (store.path / "jobs" / row["job_id"] / "exit.json").exists()
