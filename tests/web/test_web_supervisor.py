"""Pinned service, startup/shutdown and actual parent/runner lifetime witnesses."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from comic_automation.database import dal
from comic_automation.web import server, state_dir as sd
from comic_automation.web.jobs import recovery, supervisor as s, win32
from comic_automation.web.jobs.store import JobRefused, JobStore, ProcessIdentity
from test_web_job_store import ledger, fake_registry, RUNNER
from test_web_runner import fixture
from web_support import private_dir


@pytest.fixture
def service(ledger):
    state, store, clock = ledger
    owner = s.Supervisor(state, verifier=fake_registry, watcher_observation=lambda: False)
    owner.store = store
    try:
        yield owner
    finally:
        owner.close(5)
        owner.wait_closed()


def test_startup_expires_unclaimed_without_launch_and_closes_admission(service, ledger, monkeypatch):
    _, store, clock = ledger
    row = store.admit({})
    clock[0] = row["claim_deadline_ms"]
    monkeypatch.setattr(service, "_spawn", lambda row: pytest.fail("reconciliation spawned a replacement"))
    assert not service.ready
    with pytest.raises(JobRefused, match="NOT_READY"):
        service.submit({})
    service.start()
    assert service.ready and store.get(row["job_id"])["state"] == "admission_expired"
    assert service.close() and not service.ready
    with pytest.raises(JobRefused, match="NOT_READY"):
        service.submit({})


def test_pass_uses_sequence_pages_without_outer_admission_lock(service, ledger, monkeypatch):
    _, store, _ = ledger
    first = store.admit({"readonly": True})
    store.claim(first["job_id"], first["nonce"], RUNNER).close()
    second = store.admit({"kind": "service", "readonly": True})
    store.claim(second["job_id"], second["nonce"], RUNNER).close()
    service.page_size = 1
    visited = []
    def visit(store, job_id, nonce, version, **kwargs):
        # Re-enter the real admission lock: a surrounding read transaction/lock
        # would deadlock here. Reads do not change the captured generation.
        row = store.get(job_id)
        assert row["nonce"] == nonce and row["state_version"] == version
        visited.append(row["admission_seq"])
    monkeypatch.setattr(recovery, "reconcile", visit)
    assert service.reconcile_once()
    assert visited == [first["admission_seq"], second["admission_seq"]]


def test_startup_and_scheduled_pass_share_nonblocking_guard(service, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = service.store.expire_admissions
    count = []
    def blocked():
        count.append(1)
        entered.set()
        assert release.wait(10)
        return original()
    monkeypatch.setattr(service.store, "expire_admissions", blocked)
    errors = []
    def start():
        try: service.start()
        except Exception as exc: errors.append(exc)
    worker = threading.Thread(target=start)
    worker.start()
    try:
        assert entered.wait(5) and not service.ready
        assert service.reconcile_once() is False
        assert count == [1]
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive() and not errors and service.ready


def test_storage_failure_disables_admission_until_actual_successful_pass(service, monkeypatch):
    service.start()
    original = service.store.expire_admissions
    def unavailable(): raise OSError("fixture unavailable home")
    monkeypatch.setattr(service.store, "expire_admissions", unavailable)
    with pytest.raises(OSError): service.reconcile_once()
    assert not service.ready and "OSError" in service.error
    with pytest.raises(JobRefused, match="NOT_READY"): service.submit({})
    monkeypatch.setattr(service.store, "expire_admissions", original)
    assert service.reconcile_once() and service.ready and service.error is None


@pytest.mark.parametrize("failure", ["RECOVERY_FENCED", "RECOVERY_STATE_INELIGIBLE", "HOME_REFUSED"])
def test_only_expected_recovery_races_are_tolerated(service, ledger, monkeypatch, failure):
    _, store, _ = ledger
    row = store.admit({})
    store.claim(row["job_id"], row["nonce"], RUNNER).close()
    def changed(*a, **k): raise JobRefused(failure)
    monkeypatch.setattr(recovery, "reconcile", changed)
    if failure == "HOME_REFUSED":
        with pytest.raises(JobRefused, match=failure): service.start()
        assert not service.ready
    else:
        service.start()
        assert service.ready


def test_failed_spawn_retains_one_committed_admission_and_never_retries(service, monkeypatch):
    service.start()
    monkeypatch.setattr(s, "parent_job_membership", lambda: False)
    calls = []
    def failed(row):
        calls.append(row["job_id"])
        raise OSError("birth result unavailable")
    monkeypatch.setattr(service, "_spawn", failed)
    with pytest.raises(s.SpawnUnconfirmed) as caught: service.submit({})
    row = service.store.get(caught.value.job_id)
    assert row["state"] == "admitted" and row["state_version"] == 1
    service.reconcile_once()
    assert calls == [row["job_id"]]
    with pytest.raises(JobRefused, match="JOB_ACTIVE"): service.submit({})
    assert calls == [row["job_id"]]


@pytest.mark.parametrize("membership", [True, "unknown"])
def test_parent_containment_or_unknown_refuses_before_admission(service, monkeypatch, membership):
    service.start()
    def query():
        if membership == "unknown": raise JobRefused("PARENT_JOB_QUERY_FAILED")
        return membership
    monkeypatch.setattr(s, "parent_job_membership", query)
    monkeypatch.setattr(service, "_spawn", lambda row: pytest.fail("contained parent spawned"))
    with pytest.raises(JobRefused, match="PARENT"): service.submit({})
    assert not (service.store.path / "jobs").exists()


def test_default_service_has_no_trusted_production_verifier(ledger, monkeypatch):
    state, _, _ = ledger
    owner = s.Supervisor(state)
    try:
        owner.start()
        monkeypatch.setattr(s, "parent_job_membership", lambda: pytest.fail("unconfigured admission probed native"))
        with pytest.raises(JobRefused, match="TRUSTED_VERIFIER_REQUIRED"): owner.submit({})
        assert not (state.path / "jobs").exists()
    finally:
        owner.close()
        owner.wait_closed()


def test_spawn_uses_original_admission_identity_and_clean_fixed_context(service, monkeypatch):
    service.start()
    monkeypatch.setattr(s, "parent_job_membership", lambda: False)
    monkeypatch.setenv("PYTHONPATH", "untrusted")
    monkeypatch.setenv("CBZ_CONSOLE_STATE_IDENTITY", "999:999")
    monkeypatch.setenv("CBZ_WEB_CONSOLE_HOME", "elsewhere")
    calls = []
    monkeypatch.setattr(s.subprocess, "Popen", lambda args, **kwargs: calls.append((args, kwargs)))
    row = service.submit({})
    args, options = calls.pop()
    assert args == [str(Path(sys.executable).resolve()), "-E", "-s", "-m", "comic_automation.web.jobs.runner", row["job_id"], row["nonce"]]
    assert options["env"]["CBZ_CONSOLE_STATE_IDENTITY"] == ":".join(map(str, service.store.identity))
    assert options["env"]["CBZ_WEB_CONSOLE_HOME"] == str(service.store.path)
    assert "PYTHONPATH" not in options["env"]
    assert options["cwd"] == s.REPO_ROOT and options["close_fds"] is True
    assert options["stdin"] == options["stdout"] == options["stderr"] == subprocess.DEVNULL
    assert options["creationflags"] == subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    assert service.store.get(row["job_id"])["state_version"] == 1


def test_unsettled_shutdown_retains_console_guard_until_real_storage_exit(service, ledger, monkeypatch):
    state, _, _ = ledger
    service.start()
    entered, release = threading.Event(), threading.Event()
    original = service.store.expire_admissions
    def blocked():
        entered.set()
        assert release.wait(10)
        return original()
    monkeypatch.setattr(service.store, "expire_admissions", blocked)
    worker = threading.Thread(target=service.reconcile_once)
    worker.start()
    try:
        assert entered.wait(5)
        assert service.close(.02) is False and not service.ready
        assert state._lock is not None
        with pytest.raises(sd.StateDirRefused): sd.open_state_dir(state.path, environ={})
    finally:
        release.set()
        worker.join(10)
        service.wait_closed()
    assert not worker.is_alive() and service.close()


def test_build_server_publishes_session_only_after_startup(ledger, monkeypatch):
    state, _, _ = ledger
    def fail(self):
        assert not state.session_file.exists()
        raise JobRefused("STARTUP_FAILED")
    monkeypatch.setattr(s.Supervisor, "start", fail)
    with pytest.raises(JobRefused, match="STARTUP_FAILED"): server.build_server(state, 0)
    assert not state.session_file.exists()


def test_build_server_closes_supervisor_if_session_publication_fails(ledger, monkeypatch):
    state, _, _ = ledger
    owners = []
    original = s.Supervisor.start
    def start(owner):
        owners.append(owner)
        original(owner)
    monkeypatch.setattr(s.Supervisor, "start", start)
    def fail(*a): raise OSError("fixture session publish denied")
    monkeypatch.setattr(server, "write_session_file", fail)
    with pytest.raises(OSError): server.build_server(state, 0)
    assert len(owners) == 1 and owners[0].close() and not owners[0].ready



def test_shutdown_during_startup_never_publishes_ready_or_scheduler(service, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = service.store.expire_admissions
    def blocked():
        entered.set()
        assert release.wait(10)
        return original()
    monkeypatch.setattr(service.store, "expire_admissions", blocked)
    errors = []
    def start():
        try: service.start()
        except JobRefused as exc: errors.append(str(exc))
    worker = threading.Thread(target=start)
    worker.start()
    try:
        assert entered.wait(5)
        assert not service.close(.01)
    finally:
        release.set()
        worker.join(10)
        service.wait_closed()
    assert errors == ["SUPERVISOR_CLOSED_DURING_STARTUP"]
    assert not service.ready and service._thread is None and service.close()


def test_scheduler_failure_recovers_only_on_a_later_successful_pass(service, monkeypatch):
    service.interval = .01
    original = service.store.expire_admissions
    count = []
    failed, release = threading.Event(), threading.Event()
    def sometimes():
        count.append(1)
        if len(count) == 2:
            failed.set()
            raise OSError("scheduled fixture failure")
        if len(count) > 2:
            assert release.wait(10)
        return original()
    monkeypatch.setattr(service.store, "expire_admissions", sometimes)
    service.start()
    try:
        assert failed.wait(5)
        # Wait on the error publication, not just entry into the fault injection.
        end = time.monotonic() + 5
        while service.error is None:
            assert time.monotonic() < end
            time.sleep(.001)
        assert not service.ready
        release.set()
        while not service.ready:
            assert time.monotonic() < end
            time.sleep(.001)
        assert service.error is None and len(count) >= 3
    finally:
        release.set()


def test_submission_refuses_raw_commands_and_does_not_override_closed_owner(service, monkeypatch):
    service.start()
    monkeypatch.setattr(s, "parent_job_membership", lambda: False)
    monkeypatch.setattr(service, "_spawn", lambda row: pytest.fail("raw command spawned"))
    with pytest.raises(JobRefused, match="COMMAND_NOT_ACCEPTED"):
        service.submit({"argv": ["untrusted"]})
    assert not (service.store.path / "jobs").exists()
    service.state.close()
    with pytest.raises(JobRefused, match="CONSOLE_OWNERSHIP_REQUIRED"):
        service.submit({})


def test_snapshot_cutoff_excludes_jobs_admitted_during_a_pass(service, ledger, monkeypatch):
    _, store, _ = ledger
    row = store.admit({})
    store.claim(row["job_id"], row["nonce"], RUNNER).close()
    visited = []
    def visit(store, job_id, nonce, version, **kwargs):
        visited.append(job_id)
        another = store.admit({"kind": "service"})
        store.claim(another["job_id"], another["nonce"], RUNNER).close()
    monkeypatch.setattr(recovery, "reconcile", visit)
    service.page_size = 1
    assert service.reconcile_once() and visited == [row["job_id"]]



def test_real_server_close_retains_guard_while_storage_worker_is_unsettled(ledger, monkeypatch):
    state, _, _ = ledger
    http, app = server.build_server(state, 0)
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    original = app.jobs.store.expire_admissions
    def blocked():
        entered.set()
        assert release.wait(15)
        return original()
    monkeypatch.setattr(app.jobs.store, "expire_admissions", blocked)
    worker = threading.Thread(target=app.jobs.reconcile_once)
    worker.start()
    def close():
        http.server_close()
        closed.set()
    closer = threading.Thread(target=close)
    try:
        assert entered.wait(5)
        closer.start()
        # server_close first uses its production2s bounded budget, then must
        # retain its caller/guard until the real storage worker exits.
        assert not closed.wait(2.3)
        assert state._lock is not None
        with pytest.raises(sd.StateDirRefused): sd.open_state_dir(state.path, environ={})
    finally:
        release.set()
        worker.join(10)
        if closer.ident is not None: closer.join(10)
        http.server_close()
    assert closed.is_set() and not worker.is_alive() and not closer.is_alive()


def wait_for(predicate, proc=None, timeout=25):
    """Wait for an explicit native fixture handshake, with a failure watchdog."""
    end = time.monotonic() + timeout
    while not predicate():
        if proc is not None and proc.poll() is not None:
            raise AssertionError((proc.returncode, proc.stderr.read().decode(errors="replace")))
        assert time.monotonic() < end, "native fixture handshake timed out"
        time.sleep(.01)


@pytest.mark.skipif(os.name != "nt", reason="native parent job")
def test_kill_on_close_parent_is_measured_and_refused_before_runner_birth(tmp_path):
    home = private_dir(tmp_path / "state")
    sd.open_state_dir(home, environ={}).close()
    helper = Path(__file__).with_name("supervisor_job_fixture.py")
    owner = win32.launch("01ARZ3NDEKTSV4RRFFQ69G5FAV", Path(sys.executable), (str(helper), "probe", str(home)),
        cwd=helper.parents[2], environment={"SYSTEMROOT": os.environ["SYSTEMROOT"], "TEMP": str(tmp_path), "TMP": str(tmp_path)})
    try:
        wait_for(lambda: (tmp_path / "parent-probe.json").exists())
        proof = json.loads((tmp_path / "parent-probe.json").read_text())
        assert proof == {"in_job": True, "result": "DETACHED_RUNNER_PARENT_IN_JOB"}
        assert owner.wait_root(10) == 0 and not (home / "jobs").exists()
    finally:
        owner.terminate()
        owner.wait_root(5)
        owner.stdout.close()
        owner.close()


@pytest.mark.skipif(os.name != "nt", reason="native detached server lifetime")
@pytest.mark.parametrize("death", ["clean", "abrupt"])
def test_server_death_keeps_exact_runner_and_child_then_restart_observes(fixture, tmp_path, death):
    host_flags = subprocess.CREATE_NO_WINDOW
    if s.parent_job_membership():
        # Test harness only: measure explicit BREAKAWAY_OK before constructing
        # an uncontained fixture server. Production submit never uses this flag.
        limits = win32._Limits()
        ok = win32._kernel.QueryInformationJobObject(None, 9, ctypes.byref(limits), ctypes.sizeof(limits), None)
        if not ok or not limits.basic.flags & 0x800:
            pytest.skip("Native host cannot create uncontained fixture context; conservative parent refusal tested separately")
        host_flags |= subprocess.CREATE_BREAKAWAY_FROM_JOB
    # Fixture registry injection is restricted to the test helper. All spawn
    # flags/environment and admission identity come from production _spawn.
    tool_ready, release = tmp_path / "tool-ready", tmp_path / "release"
    _, _, spec, _ = fixture(f"import pathlib,time,os\npathlib.Path({str(tool_ready)!r}).touch()\nwhile not pathlib.Path({str(release)!r}).exists():time.sleep(.01)\nos.write(1,b'done'+bytes([10]))")
    (tmp_path / "fixture-spec.json").write_text(json.dumps(spec.seal_document()))
    # A separate server home keeps the preparation fixture's console lock isolated.
    home = private_dir(tmp_path / "server-state")
    sd.open_state_dir(home, environ={}).close()
    helper = Path(__file__).with_name("supervisor_job_fixture.py")
    env = {**os.environ, "CBZ_WEB_CONSOLE_HOME": str(home)}
    host = subprocess.Popen([sys.executable, str(helper), "server", str(home)], cwd=helper.parents[2], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=host_flags)
    identity = child = None
    try:
        wait_for(lambda: (tmp_path / "server-ready.json").exists() or (tmp_path / "fixture-blocked.json").exists(), host)
        if (tmp_path / "fixture-blocked.json").exists():
            pytest.skip("Native measured server remains in an outer Job Object even after test-only breakaway; no safe uncontained context")
        birth = json.loads((tmp_path / "server-ready.json").read_text())
        assert birth["parent_in_job"] is False
        job_id = birth["row"]["job_id"]
        view = sd.inspect_state_dir(home, expected_identity=(home.stat().st_dev, home.stat().st_ino), environ={})
        live = JobStore(view)
        wait_for(lambda: tool_ready.exists(), host)
        wait_for(lambda: live.get(job_id)["state"] == "running", host)
        row = live.get(job_id)
        identity = ProcessIdentity(row["runner_pid"], int(row["runner_created_at"]), row["runner_session_id"])
        child = ProcessIdentity(row["child_pid"], int(row["child_created_at"]), identity.session_id)
        if death == "clean": (tmp_path / "server-stop").touch()
        else: host.kill()
        host.wait(15)
        assert win32.observe_process(identity).state == win32.observe_process(child).state == "alive"
        restarted = sd.open_state_dir(home, environ={})
        try:
            owner = s.Supervisor(restarted)
            owner.start()
            assert owner.ready and owner.store.get(job_id)["state"] == "running"
            assert owner.store.get(job_id)["runner_pid"] == identity.pid
            assert owner.close()
        finally:
            restarted.close()
        release.touch()
        wait_for(lambda: live.get(job_id)["state"] == "succeeded")
        wait_for(lambda: win32.observe_process(identity).state == "gone")
        assert live.get(job_id)["exit_code"] == 0
    finally:
        release.touch()
        if host.poll() is None: host.kill()
        host.wait(10)
        host.stdout.close()
        host.stderr.close()
        if identity is not None:
            wait_for(lambda: win32.observe_process(identity).state == "gone")
