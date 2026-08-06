# Development log — 2026-08-06

## The GUI could not launch the CBZ Watcher

Reported by the operator: clicking **CBZ Watcher** in `apps/cbz_gui.py` died
immediately with

```text
ModuleNotFoundError: No module named 'scripts'
  from scripts.cbz_routing import series_key   (cbz_watcher.py:28)
```

while `python -m scripts.cbz_watcher` from the repository root started
cleanly.

### Cause

`CBZLauncherApp._build_command` invoked tools **by path**:

```python
script = SCRIPT_DIR / tool["script"]
cmd = [sys.executable, str(script)]
```

CPython puts the *script's own directory* at `sys.path[0]` for a path
invocation, so `scripts/` led the path and the repository root was absent
entirely. Any module-level `from scripts...` import then fails before the
tool prints anything. `Popen` was already using `cwd=REPO_ROOT`, so only the
invocation form was wrong.

Fixed by invoking as a module: `python -m scripts.<name>`.

### When it broke, measured rather than assumed

The initial hypothesis was PR #30's shadow-routing integration. That is
wrong. #30 imports the router *inside functions*
(`cbz_watcher.py:904`, `:1494`), which never execute at import time.

```text
git log -S "from scripts.cbz_lock_order import" -- scripts/cbz_watcher.py
113985f  Serialize classify-move-index by resolved series identity
```

`113985f` is the per-series lock wiring from **PR #46**, merged as `99f8f3d`
on 2026-08-05. That commit added the first module-level `scripts.*` import to
the watcher, and the GUI could not launch it from that moment. It was
reported the following morning.

### A second, still-unmerged instance of the same break

PR #49 (`feature/series-key-consolidation`, open) adds
`from scripts.cbz_routing import series_key` to
`scripts/cbz_library_maintenance.py`. Under path invocation that breaks the
**Library Maintenance** tools in the GUI exactly as the watcher broke.

Measured across every script `cbz_gui.TOOLS` can launch:

```text
script                          master  PR #49 branch
cbz_watcher.py                       1              2   broken since 113985f
cbz_library_maintenance.py           0              1   would break on merge
cbz_gap_checker.py                   0              0
cbz_workflows.py                     0              0
```

The fix in this branch covers both, because it changes the invocation form
rather than any individual script.

### Why nothing caught it

`tests/test_workflows.py` covers `cbz_gui.TOOLS` — that entries name a
script, that options are well formed. Every one of those assertions passed
while the tool was unlaunchable, because none of them ever *executed* what
the command builder produced. The property under test was the shape of a
dict, not "this can start".

`tests/test_gui_tool_commands.py` now runs the built command's import in the
`sys.path` environment that command form actually creates.

## Two mistakes made while investigating, recorded because both were instructive

**A probe started a real watcher against the live library.** Checking whether
each built command could start, the probe appended `--help`. `cbz_watcher`
has no argument parsing at all: `main()` ignores argv, calls
`os.makedirs(WATCH_FOLDER)`, and begins processing immediately. It ran
against `C:\Temp\Mega\Mega Uploads\book2` until a two-minute timeout killed
it, concurrently with the operator's own watcher which had been running since
10:58:55 and continued afterwards. 83 files under that tree were modified in
the window; the two processes' work cannot be told apart from the log, which
shows no interleaving errors and only the same benign "target already exists"
warnings seen on 2026-08-05.

Two lessons. Checking for a `__main__` guard is not the same as checking what
`main()` does with its arguments — and a tool that ignores argv will run
whatever you pass it. The test written here therefore never executes a tool's
`main()`; it imports the module, which is sufficient to catch the failure
mode and cannot touch an archive.

The second lesson is architectural. The per-series lock added in #46 is
**process-local by design** (`docs/lock_topology.md`). It serializes
classify → move → index within one watcher and offers nothing at all between
two watcher processes. Today was the first time that mattered.

**The first attempt to reproduce the bug proved nothing.** Simulating path
invocation with `python -c "import sys; sys.path.insert(0, scripts_dir);
import cbz_watcher"` passed — because `-c` prepends the working directory, so
the repository root was on `sys.path` regardless of the simulation. Every
case passed, including the broken one. Adding `-P` suppresses that and the
simulation began reproducing the real failure. The test carries a comment
recording this, because a test that cannot fail is worse than no test and
this one silently could not.

## State

```text
branch   fix/gui-invokes-scripts-as-modules  (off master c6c1644)
suite    1034 -> 1045
open     PR #49 unchanged, still awaiting an explicit merge decision
```
