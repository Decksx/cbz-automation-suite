"""Disposable D1 process helper; registry injection exists only in test code.

The production runner command and detachment arguments are constructed by the
service. Only this helper substitutes its fixture runner entry point so no
production argv/environment option can opt into a test registry.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from comic_automation.web import server, state_dir as sd
from comic_automation.web.jobs import runner, supervisor, win32
from comic_automation.web.jobs.launch_spec import FileSeal, LaunchSpec
from comic_automation.web.jobs.store import JobStore, VerifiedRequest


def spec_for(home):
    """Read only the disposable fixture's prebuilt registry seal."""
    seal = json.loads((home.parent / "fixture-spec.json").read_text())
    return LaunchSpec(Path(seal["executable"]), tuple(seal["arguments"]), Path(seal["cwd"]),
        seal["environment"], tuple(FileSeal(f["role"], Path(f["path"]), f["sha256"]) for f in seal["files"]),
        seal["outcome_id"], lambda code: "succeeded" if code == 0 else "failed")


def main():
    """Run a fixture server/runner or measure a parent's actual job containment."""
    mode, home_text = sys.argv[1:3]
    home = Path(home_text)
    if mode == "runner":
        state = sd.inspect_state_dir(home, expected_identity=runner.admission_identity(), environ={})
        runner.Runner.start(JobStore(state), *sys.argv[3:], registry=lambda _: spec_for(home)).run()
        return
    if mode == "probe":
        # This process can itself be born in a kill-on-close fixture job.
        measured = supervisor.parent_job_membership()
        state = sd.open_state_dir(home, environ={})
        service = supervisor.Supervisor(state, verifier=lambda _: VerifiedRequest("fixture", "tester", {}))
        try:
            service.start()
            try:
                service.submit({})
                result = "UNEXPECTED_BIRTH"
            except Exception as exc:
                result = str(exc)
            (home.parent / "parent-probe.json").write_text(json.dumps(dict(in_job=measured, result=result)))
        finally:
            service.close()
            state.close()
        return
    if mode != "server":
        raise ValueError(mode)
    if supervisor.parent_job_membership():
        (home.parent / "fixture-blocked.json").write_text(json.dumps(dict(reason="PARENT_STILL_CONTAINED_AFTER_TEST_BREAKAWAY")))
        return
    state = sd.open_state_dir(home, environ={})
    http, app = server.build_server(state, 0)
    service = app.jobs
    real_popen = subprocess.Popen
    def fixture_entry(command, **kwargs):
        assert command[1:5] == ["-E", "-s", "-m", "comic_automation.web.jobs.runner"]
        replaced = [command[0], str(Path(__file__).resolve()), "runner", str(home), *command[5:]]
        return real_popen(replaced, **kwargs)
    # Keep the production environment, identity transport and native flags.
    supervisor.subprocess.Popen = fixture_entry
    spec = spec_for(home)
    service.store.verifier = lambda _: VerifiedRequest("fixture", "tester", spec.seal_document())
    try:
        row = service.submit({})
        (home.parent / "server-ready.json").write_text(json.dumps(dict(
            row=row, parent_in_job=supervisor.parent_job_membership(), server=win32.current_identity().__dict__)))
        while not (home.parent / "server-stop").exists():
            time.sleep(.01)
    finally:
        http.server_close()
        state.close()


if __name__ == "__main__":
    main()
