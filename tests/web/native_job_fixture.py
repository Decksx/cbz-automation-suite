"""Disposable native process helpers; never imported by production launch paths."""
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from comic_automation.web.jobs import win32 as w


def identity():
    """Keep exact FILETIME/session evidence in fixture handshakes."""
    value = w.current_identity()
    return dict(pid=value.pid, created_at=value.created_at, session_id=value.session_id)


def wait_file(path, seconds=15):
    """Wait for a condition with a watchdog; elapsed sleeps are not crash proof."""
    deadline = time.monotonic() + seconds
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(str(path))
        time.sleep(0.01)


def publish(path, value):
    """Expose readiness only after the complete evidence payload is closed."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def run():
    """Run one temporary helper role, with a bounded escape for broken mutants."""
    mode = sys.argv[1]
    if mode == "member":
        job_id = sys.argv[2]
        handle = w._kernel.OpenJobObjectW(0x4, False, w.job_name(job_id))
        if not handle:
            raise C.WinError(C.get_last_error())
        check = w._kernel.IsProcessInJob
        check.argtypes, check.restype = [W.HANDLE, W.HANDLE, C.POINTER(W.BOOL)], W.BOOL
        member = W.BOOL()
        try:
            if not check(W.HANDLE(-1), handle, C.byref(member)):
                raise C.WinError(C.get_last_error())
        finally:
            w._kernel.CloseHandle(handle)
        sentinel = os.environ.get("CBZ_FIXTURE_HANDLE")
        leaked = False
        if sentinel:
            query = w._kernel.GetFinalPathNameByHandleW
            query.argtypes, query.restype = [W.HANDLE, W.LPWSTR, W.DWORD, W.DWORD], W.DWORD
            name = C.create_unicode_buffer(32768)
            if query(int(sentinel), name, len(name), 0):
                leaked = os.path.samefile(name.value, os.environ["CBZ_FIXTURE_FILE"])
        print(json.dumps(dict(identity=identity(), member=bool(member), arguments=sys.argv[3:],
                              stdin_eof=sys.stdin.buffer.read(1) == b"", leaked=leaked,
                              explicit_env=os.environ.get("CBZ_FIXTURE_VALUE"))), flush=True)
        return
    directory, job_id = Path(sys.argv[2]), sys.argv[3]
    if mode in ("owner", "birth-owner"):
        if mode == "birth-owner":
            original = w._identity
            def before_identity(handle):
                publish(directory / "owner.json", {"at": "after_create_before_identity"})
                wait_file(directory / "release", 60)
                return original(handle)
            w._identity = before_identity
        with w.launch(job_id, Path(sys.executable), [__file__, "tree", str(directory), job_id],
                      cwd=directory, environment=dict(os.environ)) as child:
            wait_file(directory / "leaf.json")
            wait_file(directory / "tree.json")
            publish(directory / "owner.json", dict(identity=identity(), child=child.identity.__dict__))
            wait_file(directory / "release", 60)
        return
    if mode in ("tree", "orphan"):
        subprocess.Popen([sys.executable, __file__, "leaf", str(directory), job_id],
                         stdout=sys.stdout, stderr=sys.stderr,
                         creationflags=subprocess.CREATE_NO_WINDOW)
        if mode == "orphan":
            wait_file(directory / "leaf.json")
            return
    publish(directory / f"{mode}.json", identity())
    # A failed mutation cannot leave an uncontained test child forever.
    deadline = time.monotonic() + 60
    while not (directory / "release").exists() and time.monotonic() < deadline:
        time.sleep(0.01)


if __name__ == "__main__":
    run()
