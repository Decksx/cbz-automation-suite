"""Disposable owner-death helper, never a production registry/runner option.

Only tests construct this private home and its known Python fixture seal. The
registry reconstruction below deliberately exists in test code alone.
"""
import json
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from comic_automation.web import state_dir as sd
from comic_automation.web.jobs import runner, win32
from comic_automation.web.jobs.launch_spec import FileSeal, LaunchSpec
from comic_automation.web.jobs.store import JobStore


def main():
    """Handshake at a requested fixture stage, then await owner death."""
    home, job_id, nonce, stage, ready = sys.argv[1:]
    path = Path(home)
    st = path.stat()
    state = sd.inspect_state_dir(path, expected_identity=(st.st_dev, st.st_ino), environ={})
    store = JobStore(state)
    def pause(child=None):
        payload = dict(runner=win32.current_identity().__dict__, child=child.__dict__ if child else None)
        target = Path(ready)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload))
        temporary.replace(target)
        if not threading.Event().wait(60):
            raise TimeoutError("Fixture parent did not kill its owner")
    if stage == "claim":
        original = store.read_command
        def before_read(*args):
            pause()
            return original(*args)
        store.read_command = before_read
    elif stage == "birth":
        original = store.mark_running
        def before_running(claim, version, child):
            pause(child)
            return original(claim, version, child)
        store.mark_running = before_running
    elif stage == "receipt":
        original = store.finalize
        def before_final(claim, version, process, pump, **kwargs):
            pause(process.identity)
            return original(claim, version, process, pump, **kwargs)
        store.finalize = before_final
    else:
        raise ValueError("Unknown fixture stage")
    def registry(command):
        seal = command["seal"]
        return LaunchSpec(Path(seal["executable"]), tuple(seal["arguments"]), Path(seal["cwd"]),
            seal["environment"], tuple(FileSeal(f["role"], Path(f["path"]), f["sha256"])
                                       for f in seal["files"]), seal["outcome_id"],
            lambda code: "succeeded" if code == 0 else "failed", seal["stop_policy"])
    runner.Runner.start(store, job_id, nonce, registry=registry).run()


if __name__ == "__main__":
    main()
