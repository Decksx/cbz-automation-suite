"""Workflow child tools are launched as modules, never by filesystem path.

`cbz_workflows` shells out to the maintenance, sanitizer, and compilation
tools. Launching one by path puts the script's own directory on `sys.path[0]`,
so `scripts/` leads the path and the repository root is absent entirely. A
child with a module-level `from scripts...` import then dies during import:

    python scripts\\cbz_library_maintenance.py organize-series ...
    -> ModuleNotFoundError: No module named 'scripts'

`cwd=REPO_ROOT` does not fix this. Executing a file directly prepends the
*script's* directory to `sys.path`; `-m` prepends the *current working
directory*. The repository root is importable in the module form, not merely
because a working directory was passed alongside a script path.

PR #49 exposed this by adding the first such import to a child. PR #50 fixed
the analogous GUI -> script boundary but did not cover workflow -> child
launches. This module guards the remaining boundary.

**These tests never invoke the real child CLIs.** Two of them are not safe to
run for a smoke test: `cbz_sanitizer` has no `--help` and scans
`\\\\tower\\media\\comics\\manga` by default, and `cbz_compilation_resolver`
treats an unrecognized flag as a directory. So the subprocess proof builds a
throwaway package in a tmpdir that reproduces the *mechanism* -- path
execution versus `-m` execution of a module with a package-relative import --
with no possibility of touching the library.
"""

from __future__ import annotations

import os
import subprocess
import sys
from argparse import Namespace
from importlib.util import find_spec
from pathlib import Path

import pytest

from scripts.cbz_workflows import (
    build_maintenance_commands,
    build_series_commands,
)


# Every child module the workflows can launch, via every stage that launches
# one. Both builders are exercised with all stages selected so a new call site
# cannot be added without appearing here.
def _all_generated_commands() -> list[tuple[str, list[str]]]:
    series = Namespace(
        root=Path("Library"),
        stages=["organize", "stage", "review", "compilations"],
        workers=4,
        dry_run=True,
        metadata_dedupe=False,
        uncensored_check=True,
        move_which="both",
        series_common_words=2,
        series_min_group_size=3,
        out=Path("proposal.json"),
    )
    maintenance = Namespace(
        root=Path("Library"),
        stages=["sanitize", "archive", "organize", "metadata", "names"],
        workers=8,
        dry_run=True,
        metadata_dedupe=False,
        uncensored_check=True,
        move_which="both",
        sort="alpha",
        full_rescan=True,
        restart=True,
        rules=["comicinfo"],
        names_only=True,
    )
    return build_series_commands(series) + build_maintenance_commands(maintenance)


EXPECTED_CHILD_MODULES = {
    "scripts.cbz_library_maintenance",
    "scripts.cbz_sanitizer",
    "scripts.cbz_compilation_resolver",
}


# ── command shape ────────────────────────────────────────────────


def test_every_generated_command_uses_module_invocation():
    """The shape assertion, on every command both builders can produce."""
    commands = _all_generated_commands()
    assert commands, "no commands generated; the fixture stopped covering anything"

    for label, command in commands:
        assert command[0] == sys.executable, f"{label}: not the running interpreter"
        assert command[1] == "-m", (
            f"{label}: expected module invocation, got {command[1]!r}. A child "
            f"launched by path cannot import the top-level 'scripts' package.")
        assert command[2].startswith("scripts."), f"{label}: {command[2]!r}"


def test_no_generated_command_contains_a_script_path():
    """The negative form, which catches a call site that regressed alone.

    Checked across the whole argv rather than only at index 1, because a
    reintroduced path could appear anywhere -- and the arguments themselves
    legitimately contain paths, so this looks specifically for a .py under a
    directory, which is what a script invocation looks like.
    """
    for label, command in _all_generated_commands():
        for token in command:
            assert not (token.endswith(".py") and (os.sep in token or "/" in token)), (
                f"{label}: {token!r} is a filesystem script path")


def test_all_three_child_modules_are_covered():
    """Guards the fixture, not the helper.

    If a stage stopped emitting a command, the shape assertions above would
    still pass over a smaller set and quietly cover less. Named explicitly so
    losing a child module fails rather than shrinks the test.
    """
    launched = {command[2] for _, command in _all_generated_commands()}
    assert launched == EXPECTED_CHILD_MODULES


@pytest.mark.parametrize("module", sorted(EXPECTED_CHILD_MODULES))
def test_each_launched_module_actually_resolves(module):
    """A stem typo would produce a command that fails only at runtime.

    `find_spec` locates the module without executing it, which matters here:
    importing these children has side effects, and two of them would reach
    the live library.
    """
    assert find_spec(module) is not None, f"{module} does not resolve as a module"


# ── the failure mode itself, reproduced ──────────────────────────


@pytest.fixture
def throwaway_package(tmp_path):
    """A minimal stand-in for `scripts/`: a package whose module imports a sibling.

    This is the whole mechanism under test. `child.py` has a package-relative
    import at module level, exactly like `cbz_library_maintenance`'s
    `from scripts.cbz_routing import series_key`, so it can only run if the
    package's *parent* is on `sys.path`.
    """
    pkg = tmp_path / "toolpkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "helper.py").write_text("VALUE = 'imported'\n", encoding="utf-8")
    (pkg / "child.py").write_text(
        "from toolpkg.helper import VALUE\n"
        "print(VALUE)\n",
        encoding="utf-8",
    )
    return tmp_path, pkg


def _run(command, cwd):
    """Run *command* with a clean import environment.

    PYTHONPATH is stripped so an inherited path cannot make the path-invoked
    case succeed and silently turn this proof into a no-op.
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run(command, cwd=cwd, env=env,
                          capture_output=True, text=True)


def test_path_invocation_reproduces_the_modulenotfounderror(throwaway_package):
    """The defect, reproduced end to end rather than asserted about.

    Running the module *by path* leaves its own directory on `sys.path[0]`,
    so the package it belongs to cannot be found -- even though cwd is the
    directory that contains that package.
    """
    root, pkg = throwaway_package

    result = _run([sys.executable, str(pkg / "child.py")], cwd=root)

    assert result.returncode != 0, (
        "path invocation unexpectedly succeeded; this test would no longer "
        "prove anything about the module form")
    assert "ModuleNotFoundError" in result.stderr
    assert "toolpkg" in result.stderr


def test_module_invocation_succeeds_where_path_invocation_failed(throwaway_package):
    """The fix, on the identical file, differing only in invocation form."""
    root, pkg = throwaway_package

    result = _run([sys.executable, "-m", "toolpkg.child"], cwd=root)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "imported"


def test_the_two_invocation_forms_disagree_on_the_same_file(throwaway_package):
    """States the contrast as one assertion, so the reason is unmissable.

    Both commands execute the same bytes. Only the invocation form differs,
    and that alone decides whether the import resolves.
    """
    root, pkg = throwaway_package

    by_path = _run([sys.executable, str(pkg / "child.py")], cwd=root)
    by_module = _run([sys.executable, "-m", "toolpkg.child"], cwd=root)

    assert by_path.returncode != 0
    assert by_module.returncode == 0
    assert by_path.returncode != by_module.returncode, (
        "the invocation forms no longer differ, so the helper's choice "
        "between them would not matter")


def test_the_helper_emits_the_form_that_works(throwaway_package):
    """Ties the reproduction to the production helper.

    The tests above prove `-m` is the form that works for a package-relative
    import; this asserts that is the form `_python_command` actually emits,
    so the two halves cannot drift apart.
    """
    from scripts.cbz_workflows import _python_command

    command = _python_command("cbz_library_maintenance.py", "organize-series")

    assert command[1:3] == ["-m", "scripts.cbz_library_maintenance"]
    assert not any(token.endswith(".py") for token in command)
