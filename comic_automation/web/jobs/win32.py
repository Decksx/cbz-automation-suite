"""Owned Windows containment primitives, without ledger or launch-policy authority.

M3c slice 1: CreateProcessW receives JOB_LIST before the initial thread runs.
Only stdout/stderr and NUL stdin inherit; no suspended/assign-later fallback.
Callers must validate their trusted command and retain the returned owner until
process supervision and log threads have settled. Closing it is destructive:
kill-on-last-close is the crash boundary, not an accepted writing-tool stop.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import subprocess
from typing import BinaryIO, Mapping, Sequence

from .. import acl
from .store import ProcessIdentity, validate_job_id


class NativeLaunchError(OSError):
    """Native setup failed; child_created distinguishes uncertain post-birth failure."""

    def __init__(self, stage: str, code: int, *, child_created: bool = False):
        super().__init__(code, f"{stage}: WinError {code}")
        self.stage, self.code, self.child_created = stage, code, child_created


class JobCollision(NativeLaunchError):
    """A foreign existing name must retain uncertain admission, never be adopted."""


@dataclass(frozen=True)
class ProcessObservation:
    """Exact old-process evidence: alive, gone (including reuse), or unknown."""

    state: str
    identity: ProcessIdentity | None = None
    error: int | None = None


@dataclass(frozen=True)
class JobObservation:
    """Same-session named-object evidence; a present empty job is still present."""

    state: str
    active_processes: int | None = None
    error: int | None = None


def job_name(job_id: str) -> str:
    """Return the one case-sensitive Local namespace name for a canonical job ID."""
    return "Local\\cbz-console-" + validate_job_id(job_id)


def _windows() -> None:
    if os.name != "nt":
        raise NativeLaunchError("Windows containment unavailable", 50)


if os.name == "nt":
    import ctypes as C
    from ctypes import wintypes as W
    import msvcrt

    class _SA(C.Structure):
        _fields_ = [("length", W.DWORD), ("descriptor", C.c_void_p), ("inherit", W.BOOL)]

    class _BasicLimits(C.Structure):
        _fields_ = [("process_time", C.c_longlong), ("job_time", C.c_longlong),
                    ("flags", W.DWORD), ("min_working_set", C.c_size_t),
                    ("max_working_set", C.c_size_t), ("active_limit", W.DWORD),
                    ("affinity", C.c_size_t), ("priority", W.DWORD), ("scheduling", W.DWORD)]

    class _IOCounters(C.Structure):
        _fields_ = [(name, C.c_ulonglong) for name in
                    ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

    class _Limits(C.Structure):
        _fields_ = [("basic", _BasicLimits), ("io", _IOCounters),
                    ("process_memory", C.c_size_t), ("job_memory", C.c_size_t),
                    ("peak_process_memory", C.c_size_t), ("peak_job_memory", C.c_size_t)]

    class _Accounting(C.Structure):
        _fields_ = [(name, C.c_longlong) for name in
                    ("user", "kernel", "period_user", "period_kernel")] + [
                    (name, W.DWORD) for name in ("faults", "total", "active", "terminated")]

    class _Startup(C.Structure):
        _fields_ = [("cb", W.DWORD), ("reserved", W.LPWSTR), ("desktop", W.LPWSTR),
                    ("title", W.LPWSTR)] + [(name, W.DWORD) for name in
                    ("x", "y", "x_size", "y_size", "x_chars", "y_chars", "fill", "flags")] + [
                    ("show", W.WORD), ("reserved_size", W.WORD), ("reserved_bytes", C.c_void_p),
                    ("stdin", W.HANDLE), ("stdout", W.HANDLE), ("stderr", W.HANDLE)]

    class _StartupEx(C.Structure):
        _fields_ = [("startup", _Startup), ("attributes", C.c_void_p)]

    class _ProcessInfo(C.Structure):
        _fields_ = [("process", W.HANDLE), ("thread", W.HANDLE),
                    ("pid", W.DWORD), ("tid", W.DWORD)]

    _kernel = C.WinDLL("kernel32", use_last_error=True)
    _advapi = C.WinDLL("advapi32", use_last_error=True)
    _signatures = {
        "CloseHandle": ([W.HANDLE], W.BOOL),
        "CreateJobObjectW": ([C.POINTER(_SA), W.LPCWSTR], W.HANDLE),
        "OpenJobObjectW": ([W.DWORD, W.BOOL, W.LPCWSTR], W.HANDLE),
        "SetInformationJobObject": ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
        "QueryInformationJobObject": ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.c_void_p], W.BOOL),
        "TerminateJobObject": ([W.HANDLE, W.UINT], W.BOOL),
        "CreatePipe": ([C.POINTER(W.HANDLE), C.POINTER(W.HANDLE), C.POINTER(_SA), W.DWORD], W.BOOL),
        "SetHandleInformation": ([W.HANDLE, W.DWORD, W.DWORD], W.BOOL),
        "GetHandleInformation": ([W.HANDLE, C.POINTER(W.DWORD)], W.BOOL),
        "CreateFileW": ([W.LPCWSTR, W.DWORD, W.DWORD, C.POINTER(_SA), W.DWORD, W.DWORD, W.HANDLE], W.HANDLE),
        "InitializeProcThreadAttributeList": ([C.c_void_p, W.DWORD, W.DWORD, C.POINTER(C.c_size_t)], W.BOOL),
        "UpdateProcThreadAttribute": ([C.c_void_p, W.DWORD, C.c_size_t, C.c_void_p,
                                      C.c_size_t, C.c_void_p, C.c_void_p], W.BOOL),
        "DeleteProcThreadAttributeList": ([C.c_void_p], None),
        "CreateProcessW": ([W.LPCWSTR, W.LPWSTR, C.c_void_p, C.c_void_p, W.BOOL, W.DWORD,
                           C.c_void_p, W.LPCWSTR, C.POINTER(_StartupEx), C.POINTER(_ProcessInfo)], W.BOOL),
        "GetProcessTimes": ([W.HANDLE, C.POINTER(W.FILETIME), C.POINTER(W.FILETIME),
                             C.POINTER(W.FILETIME), C.POINTER(W.FILETIME)], W.BOOL),
        "GetProcessId": ([W.HANDLE], W.DWORD),
        "GetCurrentProcessId": ([], W.DWORD),
        "ProcessIdToSessionId": ([W.DWORD, C.POINTER(W.DWORD)], W.BOOL),
        "OpenProcess": ([W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
        "WaitForSingleObject": ([W.HANDLE, W.DWORD], W.DWORD),
        "GetExitCodeProcess": ([W.HANDLE, C.POINTER(W.DWORD)], W.BOOL),
        "LocalFree": ([C.c_void_p], C.c_void_p),
    }
    for _name, (_args, _result) in _signatures.items():
        _fn = getattr(_kernel, _name)
        _fn.argtypes, _fn.restype = _args, _result
    _advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p), C.c_void_p]
    _advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = W.BOOL

    _JOB_LIST = 0x0002000D
    _HANDLE_LIST = 0x00020002
    _KILL_ON_CLOSE = 0x2000
    _INVALID = C.c_void_p(-1).value


def _require(ok, stage: str) -> None:
    if not ok:
        raise NativeLaunchError(stage, C.get_last_error())


class _Handle:
    def __init__(self, value):
        self.value = value

    def close(self):
        if self.value is not None:
            _require(_kernel.CloseHandle(self.value), "CloseHandle")
            self.value = None


def _session(pid: int) -> int:
    result = W.DWORD()
    _require(_kernel.ProcessIdToSessionId(pid, C.byref(result)), "ProcessIdToSessionId")
    return result.value


def _identity(handle) -> ProcessIdentity:
    created, exited, kernel, user = (W.FILETIME() for _ in range(4))
    _require(_kernel.GetProcessTimes(handle, C.byref(created), C.byref(exited),
                                    C.byref(kernel), C.byref(user)), "GetProcessTimes")
    pid = _kernel.GetProcessId(handle)
    _require(pid, "GetProcessId")
    return ProcessIdentity(pid, (created.dwHighDateTime << 32) | created.dwLowDateTime, _session(pid))


def current_identity() -> ProcessIdentity:
    """Observe this process using a real query handle and exact FILETIME ticks."""
    _windows()
    handle = _kernel.OpenProcess(0x1000, False, os.getpid())
    _require(handle, "OpenProcess(current)")
    owner = _Handle(handle)
    try:
        return _identity(handle)
    finally:
        owner.close()


def _active(handle) -> int:
    if handle is None:
        # NULL queries the caller's own job, which is not this closed owner.
        raise NativeLaunchError("Closed job handle", 6)
    info = _Accounting()
    _require(_kernel.QueryInformationJobObject(handle, 1, C.byref(info), C.sizeof(info), None),
             "QueryInformationJobObject")
    return info.active


def observe_process(expected: ProcessIdentity) -> ProcessObservation:
    """Observe an exact identity, without taking control of or terminating a PID."""
    _windows()
    expected.validate()
    handle = _kernel.OpenProcess(0x1000 | 0x100000, False, expected.pid)
    if not handle:
        error = C.get_last_error()
        return ProcessObservation("gone" if error == 87 else "unknown", error=error)
    owner = _Handle(handle)
    try:
        actual = _identity(handle)
        if actual.created_at != expected.created_at:
            return ProcessObservation("gone", actual)
        if actual.session_id != expected.session_id:
            return ProcessObservation("unknown", actual)
        wait = _kernel.WaitForSingleObject(handle, 0)
        if wait not in (0, 258):
            return ProcessObservation("unknown", actual, C.get_last_error())
        return ProcessObservation("gone" if wait == 0 else "alive", actual)
    except NativeLaunchError as exc:
        return ProcessObservation("unknown", error=exc.code)
    finally:
        owner.close()


def observe_job(job_id: str, expected_session: int) -> JobObservation:
    """Query and immediately close a same-session job; absence elsewhere is unknown."""
    _windows()
    name = job_name(job_id)
    if type(expected_session) is not int or not 0 <= expected_session < 2**32:
        raise ValueError("Invalid session identity")
    try:
        if _session(os.getpid()) != expected_session:
            return JobObservation("unknown")
        handle = _kernel.OpenJobObjectW(0x4, False, name)  # JOB_OBJECT_QUERY only
        if not handle:
            error = C.get_last_error()
            return JobObservation("absent" if error == 2 else "unknown", error=error)
        owner = _Handle(handle)
        try:
            return JobObservation("present", _active(handle))
        finally:
            owner.close()
    except NativeLaunchError as exc:
        return JobObservation("unknown", error=exc.code)


@dataclass
class ContainedProcess:
    """Own process/job/pipe handles; callers settle pipe users before close.

    close() is explicit kill-on-last-close cleanup, not a stop-policy decision.
    Query handles obtained by observers must never be retained by the caller.
    """

    identity: ProcessIdentity
    name: str
    stdout: BinaryIO
    _job: _Handle
    _process: _Handle

    def active_processes(self) -> int:
        """Return fresh job accounting; query failures propagate as uncertainty."""
        return _active(self._job.value)

    def wait_root(self, timeout: float = 0) -> int | None:
        """Bound a root wait and return its DWORD exit code, including valid 259."""
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 30:
            raise ValueError("Invalid native wait timeout")
        status = _kernel.WaitForSingleObject(self._process.value, math.ceil(timeout * 1000))
        if status == 258:
            return None
        if status != 0:
            raise NativeLaunchError("WaitForSingleObject", C.get_last_error())
        code = W.DWORD()
        _require(_kernel.GetExitCodeProcess(self._process.value, C.byref(code)), "GetExitCodeProcess")
        return code.value

    def terminate(self, exit_code: int = 1) -> None:
        """Request whole-job termination; callers still prove zero and enforce policy."""
        if type(exit_code) is not int or not 0 <= exit_code < 2**32:
            raise ValueError("Invalid exit code")
        _require(_kernel.TerminateJobObject(self._job.value, exit_code), "TerminateJobObject")

    def close(self) -> None:
        """Release all owned resources, attempting every close even after an error."""
        errors = []
        # Closing the job first avoids leaving a child stuck on its output pipe.
        for resource in (self._job, self._process, self.stdout):
            try:
                resource.close()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise errors[0]

    def __enter__(self):
        return self

    def __exit__(self, kind, error, traceback):
        try:
            self.close()
        except BaseException as cleanup:
            if error is None:
                raise
            error.add_note(f"Native cleanup also failed: {cleanup}")


def launch(job_id: str, executable: Path, arguments: Sequence[str], *, cwd: Path,
           environment: Mapping[str, str]) -> ContainedProcess:
    """Create one owned process inside a new private kill-on-close job.

    This internal primitive is not a trusted registry or a public submission
    endpoint. The runner must check seals and policy before calling it.
    Its dedicated host must not concurrently launch another process while
    these temporary write/NUL handles are inheritable: HANDLE_LIST constrains
    this creation only, not unrelated broad-inheritance calls in the host.
    Native setup never falls back to uncontained or suspended execution.
    A post-birth setup exception kills/closes the new job and reports
    child_created=True; it cannot be represented as a no-tool refusal.
    """
    _windows()
    name = job_name(job_id)
    executable, cwd = Path(executable), Path(cwd)
    if ("\0" in str(executable) or "\0" in str(cwd) or not executable.is_absolute()
            or not cwd.is_absolute() or not cwd.is_dir()):
        raise ValueError("Explicit absolute executable and existing cwd required")
    if (not isinstance(arguments, Sequence) or isinstance(arguments, (str, bytes))
            or any(not isinstance(a, str) or "\0" in a for a in arguments)):
        raise ValueError("Arguments must be strings without NUL")
    if (not environment or any(not isinstance(k, str) or not k or "=" in k or "\0" in k
                              or not isinstance(v, str) or "\0" in v for k, v in environment.items())
            or len({k.casefold() for k in environment}) != len(environment)):
        raise ValueError("Explicit, case-unique environment required")
    command = subprocess.list2cmdline([str(executable), *arguments])
    if len(command.encode("utf-16-le")) // 2 >= 32767:
        raise ValueError("Windows command line too long")
    resources, attributes, descriptor = [], None, C.c_void_p()
    info = _ProcessInfo()
    created = False
    try:
        sddl = f"D:P(A;;GA;;;{acl.operator_sid()})(A;;GA;;;SY)(A;;GA;;;BA)"
        _require(_advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, C.byref(descriptor), None), "ConvertSecurityDescriptor")
        private = _SA(C.sizeof(_SA), descriptor, False)
        C.set_last_error(0)
        raw_job = _kernel.CreateJobObjectW(C.byref(private), name)
        error = C.get_last_error()
        if not raw_job and error in (5, 6):
            # An inaccessible name or a name occupied by another object type
            # is not evidence of absence, even though our child was not born.
            raise JobCollision("Job name inaccessible or wrong object type", error)
        _require(raw_job, "CreateJobObjectW")
        job = _Handle(raw_job)
        resources.append(job)
        if error == 183:
            raise JobCollision("Existing job object", error)
        limits = _Limits()
        limits.basic.flags = _KILL_ON_CLOSE
        _require(_kernel.SetInformationJobObject(raw_job, 9, C.byref(limits), C.sizeof(limits)),
                 "SetInformationJobObject")
        read, write = W.HANDLE(), W.HANDLE()
        inherited = _SA(C.sizeof(_SA), descriptor, True)
        _require(_kernel.CreatePipe(C.byref(read), C.byref(write), C.byref(inherited), 0), "CreatePipe")
        reader, writer = _Handle(read.value), _Handle(write.value)
        resources.extend((reader, writer))
        _require(_kernel.SetHandleInformation(read.value, 1, 0), "Noninheritable read pipe")
        raw_nul = _kernel.CreateFileW("NUL", 0x80000000, 0x3, C.byref(inherited), 3, 0x80, None)
        if raw_nul == _INVALID:
            raise NativeLaunchError("CreateFileW(NUL)", C.get_last_error())
        nul = _Handle(raw_nul)
        resources.append(nul)
        fd = msvcrt.open_osfhandle(read.value, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
        reader.value = None  # descriptor now owns exactly one native reference
        try:
            stream = os.fdopen(fd, "rb", buffering=0)
        except BaseException:
            os.close(fd)
            raise
        resources.append(stream)

        size = C.c_size_t()
        C.set_last_error(0)
        ok = _kernel.InitializeProcThreadAttributeList(None, 2, 0, C.byref(size))
        if ok or C.get_last_error() != 122 or not size.value:
            raise NativeLaunchError("Attribute list sizing", C.get_last_error())
        buffer = C.create_string_buffer(size.value)
        _require(_kernel.InitializeProcThreadAttributeList(buffer, 2, 0, C.byref(size)),
                 "InitializeProcThreadAttributeList")
        attributes = buffer
        jobs = (W.HANDLE * 1)(raw_job)
        handles = (W.HANDLE * 2)(write.value, raw_nul)
        _require(_kernel.UpdateProcThreadAttribute(buffer, 0, _JOB_LIST, jobs,
                                                   C.sizeof(jobs), None, None), "JOB_LIST")
        _require(_kernel.UpdateProcThreadAttribute(buffer, 0, _HANDLE_LIST, handles,
                                                   C.sizeof(handles), None, None), "HANDLE_LIST")
        startup = _StartupEx()
        startup.startup.cb = C.sizeof(startup)
        startup.startup.flags = 0x100  # STARTF_USESTDHANDLES
        startup.startup.stdin, startup.startup.stdout, startup.startup.stderr = raw_nul, write.value, write.value
        startup.attributes = C.cast(buffer, C.c_void_p)
        env = C.create_unicode_buffer("\0".join(f"{k}={v}" for k, v in
            sorted(environment.items(), key=lambda item: item[0].upper())) + "\0\0")
        _require(_kernel.CreateProcessW(str(executable), C.create_unicode_buffer(command), None, None,
                 True, 0x08000000 | 0x00080000 | 0x00000400, env, str(cwd),
                 C.byref(startup), C.byref(info)), "CreateProcessW")
        created = True
        process, thread = _Handle(info.process), _Handle(info.thread)
        resources.extend((process, thread))
        thread.close()
        info.thread = None
        writer.close()
        nul.close()
        identity = _identity(info.process)
        result = ContainedProcess(identity, name, stream, job, process)
        resources.clear()  # ownership transfers only after every setup step succeeds
        return result
    except BaseException as exc:
        # A native wrapper/async exception can arrive after the OS populated
        # PROCESS_INFORMATION but before Python records successful return.
        # Recover these owned handles too, and never call that a no-child error.
        created = created or bool(info.process or info.thread)
        for value in (info.process, info.thread):
            if value and not any(isinstance(r, _Handle) and r.value == value for r in resources):
                resources.append(_Handle(value))
        if isinstance(exc, NativeLaunchError):
            exc.child_created = created
        else:
            wrapped = NativeLaunchError("Native ownership setup", getattr(exc, "winerror", 0) or 0,
                                        child_created=created)
            wrapped.__cause__ = exc
            exc = wrapped
        # Job first: any created process remains contained while failure cleanup
        # releases its pipe/process handles, including post-birth identity failure.
        for resource in resources:
            try:
                resource.close()
            except BaseException as cleanup:
                exc.add_note(f"Cleanup failed: {cleanup}")
        raise exc
    finally:
        if attributes is not None:
            _kernel.DeleteProcThreadAttributeList(attributes)
        if descriptor:
            _kernel.LocalFree(descriptor)
