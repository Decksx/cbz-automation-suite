"""The argv builder and the maintenance schema against the tool's real parser and the Tk GUI.

Design §12's "argparse parity" is the property that matters: for generated
parameter sets (defaults, every choice, every bool flipped, stage subsets) the
argv the console would run must be accepted by the tool's own
``build_parser()`` and produce the expected namespace. The parser is called
directly, never the tool's ``main()``, so no library is touched.

Until the Tk GUI is retired the console must also produce the same tokens as
Tk's ``_build_command`` for equivalent inputs (ignoring order), which is what
the second half checks.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from apps import cbz_gui
from comic_automation.web.argv import ArgvError, build_argv, resolve_params
from comic_automation.web.tool_schema import load_registry

ROOT = r"\\tower\media\comics\Comix"
TOOL = load_registry()["maintenance"]
STAGES = TOOL.param("stages").get("choices")
RULES = TOOL.param("rules").get("choices")


def _parser():
    module, func = TOOL.parser.split(":")
    return getattr(importlib.import_module(module), func)()


def _cases():
    """Defaults, then each option varied on its own."""
    yield "defaults", {"root": ROOT}
    for choice in TOOL.param("sort").get("choices"):
        yield f"sort={choice}", {"root": ROOT, "sort": choice}
    for choice in TOOL.param("workers").get("choices"):
        yield f"workers={choice}", {"root": ROOT, "workers": choice}
    for key in ("dry_run", "full_rescan", "restart", "metadata_dedupe", "names_only"):
        default = TOOL.param(key).get("default")
        yield f"{key}={not default}", {"root": ROOT, key: not default}
    for stage in STAGES:
        yield f"stages=[{stage}]", {"root": ROOT, "stages": [stage]}
    yield "stages=reversed", {"root": ROOT, "stages": list(reversed(STAGES))}
    for rule in RULES:
        yield f"rules=[{rule}]", {"root": ROOT, "rules": [rule]}
    yield "rules=all", {"root": ROOT, "rules": list(reversed(RULES))}


CASES = list(_cases())


def test_defaults_build_the_documented_dry_run_command():
    # Design §8.2, with the sort token in Tk's --sort= form (see the schema).
    assert build_argv(TOOL, {"root": ROOT}) == [
        "-m", "scripts.cbz_workflows", "maintenance", ROOT, "--dry-run",
        "--stages=sanitize,archive,organize,metadata,names", "--sort=newest", "--workers", "8",
    ]


@pytest.mark.parametrize("name,params", CASES, ids=[c[0] for c in CASES])
def test_the_tools_own_parser_accepts_the_argv_and_reads_back_the_values(name, params):
    argv = build_argv(TOOL, params)
    assert argv[:2] == ["-m", TOOL.module]
    namespace = _parser().parse_args(argv[2:])
    values = resolve_params(TOOL, params)
    assert namespace.workflow == "maintenance"
    assert str(namespace.root) == ROOT
    assert namespace.dry_run is values["dry_run"]
    assert namespace.sort == values["sort"]
    assert namespace.workers == int(values["workers"])
    assert namespace.full_rescan is values["full_rescan"]
    assert namespace.restart is values["restart"]
    assert namespace.metadata_dedupe is values["metadata_dedupe"]
    assert namespace.names_only is values["names_only"]
    # The parser canonicalises stage order itself; rules keep schema order.
    assert sorted(namespace.stages) == sorted(values["stages"])
    assert namespace.rules == sorted(values["rules"], key=RULES.index)


class _Var:
    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value


def _tk_command(values: dict) -> list[str]:
    launcher = cbz_gui.CBZLauncherApp.__new__(cbz_gui.CBZLauncherApp)
    launcher._option_vars = {
        "scan_folder": _Var(values["root"]),
        "run_mode": _Var("Dry run" if values["dry_run"] else "Normal run"),
        "stages": {s: _Var(s in values["stages"]) for s in STAGES},
        "sort": _Var(values["sort"]),
        "full_rescan": _Var(values["full_rescan"]),
        "restart": _Var(values["restart"]),
        "rules": {r: _Var(r in values["rules"]) for r in RULES},
        "metadata_dedupe": _Var(values["metadata_dedupe"]),
        "names_only": _Var(values["names_only"]),
        "workers": _Var(values["workers"]),
    }
    tk_tool = next(t for t in cbz_gui.TOOLS if t["id"] == "maintenance_workflow")
    return launcher._build_command(tk_tool)


@pytest.mark.parametrize("name,params", CASES, ids=[c[0] for c in CASES])
def test_the_argv_matches_the_tk_gui_ignoring_order(name, params):
    tk = _tk_command(resolve_params(TOOL, params))
    assert tk[0] == sys.executable
    # Same tokens, same counts; order may differ (both parse identically).
    assert sorted(build_argv(TOOL, params)) == sorted(tk[1:])


def test_ordering_does_not_change_the_argv_for_an_unordered_list():
    # Two equivalent selections must give one argv, and so one future seal.
    assert build_argv(TOOL, {"root": ROOT, "rules": ["url", "brackets"]}) == \
        build_argv(TOOL, {"root": ROOT, "rules": ["brackets", "url"]})


@pytest.mark.parametrize("params,message", [
    ({}, "a value is required"),
    ({"root": ROOT, "dry_runn": True}, "unknown parameters"),
    ({"root": ROOT, "dry_run": 1}, "true or false"),
    ({"root": ROOT, "sort": "random"}, "must be one of"),
    ({"root": ROOT, "workers": 8}, "must be one of"),
    ({"root": ROOT, "stages": []}, "at least 1"),
    ({"root": ROOT, "stages": ["sanitize", "sanitize"]}, "repeats a choice"),
    ({"root": ROOT, "rules": ["url", "nope"]}, "unknown choices"),
    ({"root": "X:\\comix\nrm"}, "control character"),
])
def test_values_the_schema_forbids_are_refused_not_ignored(params, message):
    with pytest.raises(ArgvError, match=message):
        build_argv(TOOL, params)
