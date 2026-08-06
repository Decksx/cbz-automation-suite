"""Every tool the GUI offers must actually be able to start.

`test_workflows.py` already checks that `cbz_gui.TOOLS` has the right shape --
that each entry names a script, that options are well formed. That is not the
property that matters. On 2026-08-05 commit 113985f added a module-level
`from scripts.cbz_lock_order import ...` to `cbz_watcher.py`, and from that
commit the GUI could not launch the watcher at all: invoking a script *by
path* puts `scripts/` on `sys.path[0]` instead of the repository root, so the
`scripts` package is not importable and the process dies with
`ModuleNotFoundError` before printing anything.

Every shape assertion still passed. The break shipped.

So these tests execute what the GUI builds rather than inspecting it. The
property is "this command can start", not "this dict has the right keys".

Two things they deliberately do not do:

* They never run a tool's `main()`. `cbz_watcher.main()` takes no arguments,
  ignores anything passed, creates its watch folder and begins processing a
  live library immediately -- so "just run it with --help" is not available
  here. Importing the module proves the import chain resolves, which is the
  failure mode being guarded, without touching a single archive.
* They do not hard-code the expected command form. The environment is derived
  from whatever command the GUI actually produces, so the test stays valid if
  the invocation changes for some other good reason -- and still fails if the
  change reintroduces an unimportable one.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from apps import cbz_gui

REPO_ROOT = Path(__file__).resolve().parents[1]

# The GUI runs its tools with cwd=REPO_ROOT (see CBZLauncherApp._start_tool),
# so every simulation below uses the same working directory.
TOOL_SCRIPTS = sorted({tool["script"] for tool in cbz_gui.TOOLS})


def _import_environment(command: list[str]) -> tuple[str, str]:
    """The sys.path[0] and import name *command* would produce at runtime.

    Mirrors CPython's own rules rather than assuming a form:

        python -m pkg.mod <args>   sys.path[0] is the working directory
        python path/to/mod.py      sys.path[0] is the script's own directory
    """
    if len(command) >= 3 and command[1] == "-m":
        return str(REPO_ROOT), command[2]
    script = Path(command[1])
    return str(script.parent), script.stem


def _import_succeeds(path0: str, name: str) -> subprocess.CompletedProcess:
    """Import *name* with *path0* leading sys.path, and nothing else added.

    `-P` matters. Without it the interpreter prepends the working directory
    for `-c`, which would put the repository root on the path no matter what
    the GUI's command does -- and every case would pass, including the broken
    one. That mistake was made once while writing this file; the test only
    became meaningful after it was corrected.
    """
    return subprocess.run(
        [sys.executable, "-P", "-c",
         f"import sys; sys.path.insert(0, r'''{path0}'''); import {name}"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.parametrize("script", TOOL_SCRIPTS)
def test_every_gui_tool_can_start(script):
    """The regression this file exists for.

    Fails with the operator's exact symptom -- ModuleNotFoundError at import
    -- if the GUI ever goes back to invoking scripts by path while any of
    them imports from the `scripts` package.
    """
    command = cbz_gui.CBZLauncherApp._script_command(script)
    path0, name = _import_environment(command)
    result = _import_succeeds(path0, name)

    assert result.returncode == 0, (
        f"the GUI's command for {script} cannot start:\n"
        f"  command : {subprocess.list2cmdline(command)}\n"
        f"  sys.path[0] would be : {path0}\n"
        f"  import that fails    : {name}\n"
        f"{result.stderr.strip()}"
    )


def test_the_tool_list_is_not_empty():
    """Guards the parametrization itself.

    A refactor that emptied TOOLS would make every test above vanish rather
    than fail, which is the quiet way a suite stops checking anything.
    """
    assert TOOL_SCRIPTS, "cbz_gui.TOOLS produced no scripts to check"
    assert len(cbz_gui.TOOLS) >= len(TOOL_SCRIPTS)


@pytest.mark.parametrize("script", TOOL_SCRIPTS)
def test_the_named_script_exists_on_disk(script):
    """An import failure and a missing file are different diagnoses."""
    assert (REPO_ROOT / "scripts" / script).is_file(), (
        f"cbz_gui.TOOLS names {script}, which is not in scripts/"
    )


def test_path_invocation_is_proven_to_break_the_import():
    """The control, so a pass above cannot be vacuous.

    If this ever stops failing, either no GUI tool imports from the `scripts`
    package any more -- in which case the guard above is no longer load
    bearing and should be reconsidered rather than trusted -- or the
    simulation has stopped reproducing what `python <path>` really does.
    """
    offenders = [
        s for s in TOOL_SCRIPTS
        if _import_succeeds(str(REPO_ROOT / "scripts"), Path(s).stem).returncode != 0
    ]
    assert offenders, (
        "no GUI tool fails under path invocation any more; "
        "test_every_gui_tool_can_start may no longer be guarding anything"
    )


def test_the_gui_runs_tools_from_the_repository_root():
    """`-m` only resolves if the process starts at the repository root.

    Asserted because the two halves are separable: someone could keep the
    `-m` form and change the working directory, and every import above would
    start failing again for a different reason.
    """
    source = Path(cbz_gui.__file__).read_text(encoding="utf-8", errors="replace")
    assert "cwd=str(REPO_ROOT)" in source, (
        "the GUI no longer launches tools with cwd set to the repository root"
    )
