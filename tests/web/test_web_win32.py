"""Native disposable trees and deterministic failure/ownership witnesses."""
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import types

import pytest

from comic_automation.web import acl
from comic_automation.web.jobs import win32 as w
from comic_automation.web.jobs.log_io import cancel_thread_io
from comic_automation.web.jobs.store import ProcessIdentity

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows native containment")
FIXTURE = Path(__file__).with_name("native_job_fixture.py")


@pytest.fixture
def job_id():
    # Separate pytest workers/processes never contend on a fixed kernel name.
    import secrets
    return "0000000000" + "".join(secrets.choice("0123456789ABCDEFGHJKMNPQRSTVWXYZ") for _ in range(16))


def eventually(test, seconds=8):
    deadline = time.monotonic() + seconds
    while not test():
        assert time.monotonic() < deadline, "native condition not proved before deadline"
        time.sleep(0.01)


def read_output(child):
    result, errors = [], []
    def read():
        try:
            result.append(child.stdout.read())
        except OSError as exc:
            errors.append(exc)
    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    thread.join(5)
    if thread.is_alive():
        cancel_thread_io(thread.native_id)
        thread.join(3)
        pytest.fail("parent write reference leaked or native read did not settle")
    assert not errors
    return result[0]


def spawn(job_id, tmp_path, *arguments, environment=None):
    return w.launch(job_id, Path(sys.executable), list(arguments), cwd=tmp_path,
                    environment=dict(os.environ) if environment is None else environment)


def test_exact_job_membership_at_first_helper_action_and_windows_argv(job_id, tmp_path):
    arguments = ["space here", 'quote"here', "trailing\\", "", "unicode-é"]
    env = dict(os.environ, CBZ_FIXTURE_VALUE="explicit")
    with spawn(job_id, tmp_path, str(FIXTURE), "member", job_id, *arguments, environment=env) as child:
        assert child.wait_root(5) == 0
        report = json.loads(read_output(child))
        assert report["member"] and report["stdin_eof"]
        assert report["arguments"] == arguments and report["explicit_env"] == "explicit"
        assert report["identity"] == child.identity.__dict__
        assert w.observe_process(child.identity).state == "gone"
        eventually(lambda: child.active_processes() == 0)
        assert w.observe_job(job_id, child.identity.session_id).state == "present"
    assert w.observe_job(job_id, child.identity.session_id).state == "absent"


def test_explicit_handle_list_excludes_inheritable_parent_file(job_id, tmp_path):
    path = tmp_path / "sentinel.txt"
    with path.open("w+b") as sentinel:
        os.set_inheritable(sentinel.fileno(), True)
        import msvcrt
        env = dict(os.environ, CBZ_FIXTURE_HANDLE=str(msvcrt.get_osfhandle(sentinel.fileno())),
                   CBZ_FIXTURE_FILE=str(path))
        with spawn(job_id, tmp_path, str(FIXTURE), "member", job_id, environment=env) as child:
            assert child.wait_root(5) == 0
            assert json.loads(read_output(child))["leaked"] is False


def test_job_handle_noninheritance_private_dacl_and_collision_no_limit_change(job_id, tmp_path, monkeypatch):
    with spawn(job_id, tmp_path, "-c", "import time;time.sleep(15)") as child:
        flags = W.DWORD()
        assert w._kernel.GetHandleInformation(child._job.value, C.byref(flags))
        assert not flags.value & 1
        limits = w._Limits()
        assert w._kernel.QueryInformationJobObject(child._job.value, 9, C.byref(limits), C.sizeof(limits), None)
        assert limits.basic.flags == w._KILL_ON_CLOSE  # neither breakaway flag
        security = C.c_void_p()
        get = w._advapi.GetSecurityInfo
        get.argtypes = [W.HANDLE, C.c_int, W.DWORD, C.c_void_p, C.c_void_p,
                        C.c_void_p, C.c_void_p, C.POINTER(C.c_void_p)]
        get.restype = W.DWORD
        convert = w._advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
        convert.argtypes = [C.c_void_p, W.DWORD, W.DWORD, C.POINTER(W.LPWSTR), C.c_void_p]
        convert.restype = W.BOOL
        text = W.LPWSTR()
        try:
            assert get(child._job.value, 6, 4, None, None, None, None, C.byref(security)) == 0
            assert convert(security, 1, 4, C.byref(text), None)
            assert text.value.startswith("D:P")
            sids = {acl.canonical_sid(ace.split(";")[5]) for ace in re.findall(r"\(([^)]+)\)", text.value)}
            assert sids == {acl.operator_sid(), acl.SYSTEM_SID, acl.ADMINISTRATORS_SID}
        finally:
            if text:
                w._kernel.LocalFree(text)
            if security:
                w._kernel.LocalFree(security)
        # Existing limits must not be touched, even when the object is ours.
        monkeypatch.setattr(w._kernel, "SetInformationJobObject", lambda *a: pytest.fail("collision adopted"))
        with pytest.raises(w.JobCollision) as caught:
            spawn(job_id, tmp_path, "-c", "raise AssertionError('must not run')")
        assert not caught.value.child_created
        assert w.observe_process(child.identity).state == "alive"
        assert child.active_processes() >= 1
        child.terminate()
        assert child.wait_root(5) is not None
        eventually(lambda: child.active_processes() == 0)


def test_observations_keep_denial_and_wrong_session_unknown(job_id, monkeypatch):
    own = w.current_identity()
    assert w.observe_process(own).state == "alive"
    assert w.observe_process(ProcessIdentity(own.pid, own.created_at + 1, own.session_id)).state == "gone"
    assert w.observe_job(job_id, own.session_id + 1).state == "unknown"
    def denied(*args):
        C.set_last_error(5)
        return None
    monkeypatch.setattr(w._kernel, "OpenProcess", denied)
    monkeypatch.setattr(w._kernel, "OpenJobObjectW", denied)
    assert w.observe_process(own).state == "unknown"
    assert w.observe_job(job_id, own.session_id).state == "unknown"


def test_other_object_type_occupying_job_name_is_uncertain(job_id, tmp_path):
    create = w._kernel.CreateEventW
    create.argtypes, create.restype = [C.c_void_p, W.BOOL, W.BOOL, W.LPCWSTR], W.HANDLE
    event = create(None, True, False, w.job_name(job_id))
    assert event
    try:
        with pytest.raises(w.JobCollision) as caught:
            spawn(job_id, tmp_path, "-c", "raise AssertionError('must not run')")
        assert caught.value.code == 6 and not caught.value.child_created
        assert w.observe_job(job_id, w.current_identity().session_id).state == "unknown"
    finally:
        assert w._kernel.CloseHandle(event)


def test_inaccessible_creation_name_does_not_prove_absence(job_id, tmp_path, monkeypatch):
    def denied(*args):
        C.set_last_error(5)
        return None
    monkeypatch.setattr(w._kernel, "CreateJobObjectW", denied)
    with pytest.raises(w.JobCollision) as caught:
        spawn(job_id, tmp_path, "-c", "raise AssertionError('must not run')")
    assert caught.value.code == 5 and not caught.value.child_created


@pytest.mark.parametrize("exit_code", [0, 7, 259])
def test_wait_uses_signalled_process_before_exit_code(job_id, tmp_path, exit_code):
    with spawn(job_id, tmp_path, "-c", f"import sys;sys.exit({exit_code})") as child:
        assert child.wait_root(5) == exit_code
        read_output(child)
    with pytest.raises(w.NativeLaunchError, match="Closed job"):
        child.active_processes()


@pytest.mark.parametrize("stage", ["security", "crt_descriptor", "crt_stream",
    "SetInformationJobObject", "CreatePipe", "SetHandleInformation",
    "CreateFileW", "InitializeProcThreadAttributeList", "JOB_LIST", "HANDLE_LIST", "CreateProcessW",
    "after_create", "identity"])
def test_each_failure_closes_every_owned_handle_and_never_relaunches(job_id, tmp_path, monkeypatch, stage):
    owned, born_handles, creates = [], [], []
    original_create = w._kernel.CreateProcessW
    def record_create(*args):
        creates.append(True)
        return original_create(*args)
    monkeypatch.setattr(w._kernel, "CreateProcessW", record_create)
    base = w._Handle
    class Tracked(base):
        def __init__(self, value):
            super().__init__(value)
            owned.append((self, value))
    monkeypatch.setattr(w, "_Handle", Tracked)
    def fail(*args):
        C.set_last_error(5)
        return False
    if stage == "security":
        monkeypatch.setattr(w._advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW", fail)
    elif stage in ("crt_descriptor", "crt_stream"):
        def unavailable(*args, **kwargs):
            raise OSError("fixture CRT transfer failure")
        if stage == "crt_descriptor":
            monkeypatch.setattr(w, "msvcrt", types.SimpleNamespace(open_osfhandle=unavailable))
        else:
            proxy = types.SimpleNamespace(**vars(os))
            proxy.fdopen = unavailable
            monkeypatch.setattr(w, "os", proxy)
    elif stage == "CreateFileW":
        monkeypatch.setattr(w._kernel, stage, lambda *args: w._INVALID)
    elif stage == "InitializeProcThreadAttributeList":
        original = getattr(w._kernel, stage)
        def sizing(buffer, *args):
            return original(buffer, *args) if buffer is None else fail()
        monkeypatch.setattr(w._kernel, stage, sizing)
    elif stage in ("JOB_LIST", "HANDLE_LIST"):
        original = w._kernel.UpdateProcThreadAttribute
        target = w._JOB_LIST if stage == "JOB_LIST" else w._HANDLE_LIST
        def attribute(buffer, flags, key, *args):
            return fail() if key == target else original(buffer, flags, key, *args)
        monkeypatch.setattr(w._kernel, "UpdateProcThreadAttribute", attribute)
    elif stage == "after_create":
        original = w._kernel.CreateProcessW
        def created(*args):
            assert original(*args)
            information = C.cast(args[-1], C.POINTER(w._ProcessInfo)).contents
            born_handles.extend((information.process, information.thread))
            raise RuntimeError("exception immediately after native birth")
        monkeypatch.setattr(w._kernel, "CreateProcessW", created)
    elif stage == "identity":
        monkeypatch.setattr(w, "_identity", lambda *args: (_ for _ in ()).throw(w.NativeLaunchError("identity", 5)))
    elif stage == "CreateProcessW":
        def refused(*args):
            creates.append(True)
            return fail()
        monkeypatch.setattr(w._kernel, stage, refused)
    else:
        monkeypatch.setattr(w._kernel, stage, fail)
    with pytest.raises(w.NativeLaunchError) as caught:
        spawn(job_id, tmp_path, "-c", "import time;time.sleep(10)")
    assert caught.value.child_created == (stage in ("after_create", "identity"))
    assert len(creates) == (1 if stage in ("CreateProcessW", "after_create", "identity") else 0)
    flags = W.DWORD()
    assert owned or stage == "security"
    for owner, value in owned:
        assert owner.value is None
        assert not w._kernel.GetHandleInformation(value, C.byref(flags))
    for value in born_handles:
        assert not w._kernel.GetHandleInformation(value, C.byref(flags))
    # Restore Open/identity helpers before independently querying no retained job.
    monkeypatch.undo()
    session = w.current_identity().session_id
    eventually(lambda: w.observe_job(job_id, session).state == "absent")


@pytest.mark.parametrize("boundary", ["owner", "birth-owner"])
def test_owner_death_kills_root_and_grandchild_without_retained_query_handle(job_id, tmp_path, boundary):
    owner = subprocess.Popen([sys.executable, str(FIXTURE), boundary, str(tmp_path), job_id],
                             cwd=tmp_path, creationflags=subprocess.CREATE_NO_WINDOW)
    identities = []
    try:
        eventually(lambda: (tmp_path / "owner.json").exists(), 12)
        eventually(lambda: (tmp_path / "tree.json").exists() and (tmp_path / "leaf.json").exists(), 12)
        for name in ("tree", "leaf"):
            identities.append(ProcessIdentity(**json.loads((tmp_path / f"{name}.json").read_text())))
        assert all(w.observe_process(i).state == "alive" for i in identities)
        session = identities[0].session_id
        # Windows may include extra subprocesses; both witnessed identities
        # must be live and the eventual proof must cover the entire job.
        assert w.observe_job(job_id, session).active_processes >= 2
        assert owner.poll() is None
        owner.kill()  # Only this fixture Popen; no production PID or job is targeted.
        owner.wait(timeout=5)
        eventually(lambda: all(w.observe_process(i).state == "gone" for i in identities))
        eventually(lambda: w.observe_job(job_id, session).state == "absent")
    finally:
        (tmp_path / "release").touch()
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=5)
        if identities:
            eventually(lambda: all(w.observe_process(i).state == "gone" for i in identities), 8)


def test_native_abi_layouts():
    expected = {8: (112, 144, 48, 24), 4: (72, 112, 48, 16)}[C.sizeof(C.c_void_p)]
    assert tuple(C.sizeof(t) for t in (w._StartupEx, w._Limits, w._Accounting, w._ProcessInfo)) == expected


def test_root_exit_does_not_imply_descendants_or_pipe_eof(job_id, tmp_path):
    with spawn(job_id, tmp_path, str(FIXTURE), "orphan", str(tmp_path), job_id) as child:
        try:
            assert child.wait_root(5) == 0
            leaf = ProcessIdentity(**json.loads((tmp_path / "leaf.json").read_text()))
            assert w.observe_process(leaf).state == "alive"
            assert child.active_processes() >= 1
            child.terminate(9)
            eventually(lambda: child.active_processes() == 0)
            eventually(lambda: w.observe_process(leaf).state == "gone")
            assert read_output(child) == b""
        finally:
            (tmp_path / "release").touch()


@pytest.mark.parametrize("arguments,environment", [
    ("not a sequence of arguments", {"A": "b"}),
    (["nul\0argument"], {"A": "b"}),
    ([], {}), ([], {"a": "1", "A": "2"}), ([], {"bad=key": "v"}),
])
def test_invalid_launch_inputs_create_no_job(job_id, tmp_path, arguments, environment):
    with pytest.raises(ValueError):
        w.launch(job_id, Path(sys.executable), arguments, cwd=tmp_path, environment=environment)
    assert w.observe_job(job_id, w.current_identity().session_id).state == "absent"


def test_real_missing_executable_refuses_without_search_or_child(job_id, tmp_path):
    with pytest.raises(w.NativeLaunchError) as caught:
        w.launch(job_id, tmp_path / "missing.exe", [], cwd=tmp_path, environment=dict(os.environ))
    assert caught.value.stage == "CreateProcessW"
    assert caught.value.code == 2 and not caught.value.child_created
    assert w.observe_job(job_id, w.current_identity().session_id).state == "absent"
