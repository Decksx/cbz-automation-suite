"""Tool schemas for the web console (design §8.1, milestone M4a).

A tool schema is a JSON file under ``comic_automation/web/tool_schemas/`` that
describes one operator tool: how it is launched, what it may change, and every
parameter the browser may set. This module holds the meta-schema, written in
Python rather than as a JSON Schema document so the rules can say *why*, and the
registry that loads every schema at startup.

Validation is strict on purpose. An unknown field, a parameter key used twice, an
emit style that cannot express its type, or a ``when`` that names a parameter
that does not exist is refused, and :func:`load_registry` refuses the whole
directory. A typo in a schema must stop the console from starting rather than
silently drop an option the operator thinks they set; the argv builder
(:mod:`comic_automation.web.argv`) relies on that, because it is the only code
that turns parameters into arguments and it trusts the shapes checked here.

Out of scope for M4a: filesystem checks for ``path`` parameters (``role``,
``must_exist`` and ``create_new`` are validated as declarations only; the path
allowlist enforces them in M4b), the preview seal (M4b), and the compatibility
matrix (M4c).
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

SCHEMA_DIR = Path(__file__).with_name("tool_schemas")

# Each type lists the emit styles that can represent it. A bool has no value to
# print, so only the presence-flag styles fit; a list needs csv or repeat. Any
# other pairing would make the argv builder guess, so it is refused here.
EMIT_STYLES: dict[str, frozenset[str]] = {
    "bool": frozenset({"flag", "flag_false"}),
    "choice": frozenset({"option", "equals"}),
    "multi_choice": frozenset({"csv", "repeat"}),
    "int": frozenset({"option", "equals"}),
    "number": frozenset({"option", "equals"}),
    "path": frozenset({"option", "equals", "positional"}),
    "text": frozenset({"option", "equals"}),
    "fixed": frozenset({"option", "equals"}),
}

# Fields a parameter may carry, by type. Common fields apply to every type.
_COMMON_PARAM_FIELDS = frozenset({
    "key", "type", "label", "help", "group", "description", "example",
    "expected_result", "default", "emit", "when",
})
_TYPE_FIELDS: dict[str, frozenset[str]] = {
    "bool": frozenset(),
    "choice": frozenset({"choices"}),
    "multi_choice": frozenset({"choices", "ordered", "unique", "min", "max"}),
    "int": frozenset({"min", "max"}),
    "number": frozenset({"min", "max"}),
    "path": frozenset({"role", "kind", "must_exist", "create_new", "optional"}),
    "text": frozenset({"max_len", "pattern", "optional"}),
    "fixed": frozenset({"source"}),
}
_EMIT_FIELDS = frozenset({"style", "flag", "omit_empty"})
_TOOL_FIELDS = frozenset({
    "id", "label", "description", "domain", "module", "subcommand", "parser",
    "kind", "effects", "dry_run", "stop", "exit_codes", "params",
})
_REQUIRED_TOOL_FIELDS = frozenset({
    "id", "label", "domain", "module", "parser", "kind", "effects", "dry_run",
    "stop", "params",
})
_KINDS = frozenset({"task", "service"})
_EFFECTS = frozenset({"read_only", "writes_files", "service"})
_ID = re.compile(r"[a-z][a-z0-9_]{0,63}")
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")
_PARSER = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*")
_FLAG = re.compile(r"--[a-z0-9][a-z0-9-]*")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class SchemaError(ValueError):
    """A tool schema does not satisfy the meta-schema; the console must not start."""


@dataclass(frozen=True)
class Emit:
    style: str
    flag: str | None
    omit_empty: bool


@dataclass(frozen=True)
class Param:
    key: str
    type: str
    emit: Emit
    raw: Mapping[str, Any]

    def get(self, name: str, default: Any = None) -> Any:
        return self.raw.get(name, default)


@dataclass(frozen=True)
class Tool:
    id: str
    module: str
    subcommand: str | None
    parser: str
    kind: str
    effects: str
    dry_run_param: str | None
    params: tuple[Param, ...]
    raw: Mapping[str, Any]

    def param(self, key: str) -> Param:
        for param in self.params:
            if param.key == key:
                return param
        raise KeyError(key)


def _fail(where: str, message: str) -> None:
    raise SchemaError(f"{where}: {message}")


def _is_int(value: Any) -> bool:
    # bool is an int subclass; True must never pass as 1.
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return (_is_int(value) or isinstance(value, float)) and math.isfinite(value)


def check_value(param: Param, value: Any, where: str) -> None:
    """Refuse a value that this parameter's declared type and constraints reject.

    Shared by default validation here and by the argv builder, so a schema
    default and an operator-supplied value obey exactly the same rules.
    """
    kind = param.type
    if kind == "bool":
        if not isinstance(value, bool):
            _fail(where, "must be true or false")
    elif kind == "choice":
        if not isinstance(value, str) or value not in param.get("choices"):
            _fail(where, f"must be one of {param.get('choices')}")
    elif kind == "multi_choice":
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            _fail(where, "must be a list of strings")
        unknown = [v for v in value if v not in param.get("choices")]
        if unknown:
            _fail(where, f"unknown choices {unknown}")
        if param.get("unique", True) and len(set(value)) != len(value):
            _fail(where, "repeats a choice")
        if len(value) < param.get("min", 0):
            _fail(where, f"needs at least {param.get('min')} choices")
        if param.get("max") is not None and len(value) > param.get("max"):
            _fail(where, f"allows at most {param.get('max')} choices")
    elif kind in ("int", "number"):
        ok = _is_int(value) if kind == "int" else _is_number(value)
        if not ok:
            _fail(where, f"must be a finite {kind}")
        if param.get("min") is not None and value < param.get("min"):
            _fail(where, f"must be at least {param.get('min')}")
        if param.get("max") is not None and value > param.get("max"):
            _fail(where, f"must be at most {param.get('max')}")
    elif kind in ("path", "text"):
        if not isinstance(value, str):
            _fail(where, "must be a string")
        if _CONTROL.search(value):
            # A control character in an argument is never intended and can
            # change how a log or a terminal displays the command.
            _fail(where, "contains a control character")
        if kind == "text":
            if param.get("max_len") is not None and len(value) > param.get("max_len"):
                _fail(where, f"is longer than {param.get('max_len')}")
            if param.get("pattern") is not None and not re.fullmatch(param.get("pattern"), value):
                _fail(where, "does not match the required pattern")
    elif kind == "fixed":
        if not isinstance(value, str) or not value:
            _fail(where, "fixed value must be a non-empty string")


def _check_param(raw: Any, index: int, tool_id: str) -> Param:
    where = f"{tool_id}.params[{index}]"
    if not isinstance(raw, dict):
        _fail(where, "must be an object")
    key = raw.get("key")
    if not isinstance(key, str) or not _ID.fullmatch(key):
        _fail(where, "key must be a lower_snake identifier")
    where = f"{tool_id}.{key}"
    kind = raw.get("type")
    if kind not in EMIT_STYLES:
        _fail(where, f"unknown type {kind!r}")
    allowed = _COMMON_PARAM_FIELDS | _TYPE_FIELDS[kind]
    extra = sorted(set(raw) - allowed)
    if extra:
        _fail(where, f"unknown fields {extra} for type {kind}")

    emit_raw = raw.get("emit")
    if not isinstance(emit_raw, dict):
        _fail(where, "emit must be an object")
    extra = sorted(set(emit_raw) - _EMIT_FIELDS)
    if extra:
        _fail(where, f"unknown emit fields {extra}")
    style = emit_raw.get("style")
    if style not in EMIT_STYLES[kind]:
        _fail(where, f"emit style {style!r} cannot express a {kind}")
    flag = emit_raw.get("flag")
    if style == "positional":
        if flag is not None:
            _fail(where, "a positional parameter has no flag")
    elif not isinstance(flag, str) or not _FLAG.fullmatch(flag):
        _fail(where, "emit flag must look like --long-name")
    omit_empty = emit_raw.get("omit_empty", False)
    if not isinstance(omit_empty, bool) or (omit_empty and style not in ("csv", "repeat")):
        _fail(where, "omit_empty applies only to csv or repeat lists")
    emit = Emit(style=style, flag=flag, omit_empty=omit_empty)

    if kind in ("choice", "multi_choice"):
        choices = raw.get("choices")
        if (not isinstance(choices, list) or not choices
                or not all(isinstance(c, str) and c and not _CONTROL.search(c) for c in choices)
                or len(set(choices)) != len(choices)):
            _fail(where, "choices must be a non-empty list of distinct strings")
        if kind == "multi_choice" and style == "csv" and any("," in c for c in choices):
            _fail(where, "a csv choice cannot contain a comma")
    for bool_field in ("ordered", "unique", "must_exist", "create_new", "optional"):
        if bool_field in raw and not isinstance(raw[bool_field], bool):
            _fail(where, f"{bool_field} must be true or false")
    if kind in ("int", "number", "multi_choice"):
        for bound in ("min", "max"):
            if bound in raw and not (_is_int(raw[bound]) if kind != "number" else _is_number(raw[bound])):
                _fail(where, f"{bound} must be a number")
        if "min" in raw and "max" in raw and raw["min"] > raw["max"]:
            _fail(where, "min is greater than max")
    if kind == "path":
        if not isinstance(raw.get("role"), str) or not _ID.fullmatch(raw["role"]):
            _fail(where, "a path needs a role identifier (the M4b allowlist key)")
        if raw.get("kind") not in ("dir", "file"):
            _fail(where, "path kind must be dir or file")
        if raw.get("must_exist") and raw.get("create_new"):
            _fail(where, "must_exist and create_new contradict each other")
    if kind == "text":
        if "max_len" in raw and not (_is_int(raw["max_len"]) and raw["max_len"] > 0):
            _fail(where, "max_len must be a positive integer")
        if "pattern" in raw:
            try:
                re.compile(raw["pattern"])
            except (re.error, TypeError):
                _fail(where, "pattern is not a valid regular expression")
    if kind == "fixed":
        if not isinstance(raw.get("source"), str) or not raw["source"]:
            _fail(where, "a fixed parameter needs a configuration source")
        if "default" in raw:
            _fail(where, "a fixed parameter takes its value from configuration, not a default")

    param = Param(key=key, type=kind, emit=emit, raw=raw)
    if "default" in raw:
        check_value(param, raw["default"], f"{where}.default")
    elif kind not in ("fixed",) and not raw.get("optional") and emit.style != "positional":
        # Without a default the builder could only omit the argument, which
        # hides the tool's own default from the preview the operator approves.
        _fail(where, "needs a default unless it is optional, positional or fixed")
    return param


def validate_tool(raw: Any, *, origin: str = "<schema>") -> Tool:
    """Check one parsed schema document against the meta-schema."""
    if not isinstance(raw, dict):
        _fail(origin, "a tool schema must be a JSON object")
    extra = sorted(set(raw) - _TOOL_FIELDS)
    if extra:
        _fail(origin, f"unknown tool fields {extra}")
    missing = sorted(_REQUIRED_TOOL_FIELDS - set(raw))
    if missing:
        _fail(origin, f"missing tool fields {missing}")
    tool_id = raw["id"]
    if not isinstance(tool_id, str) or not _ID.fullmatch(tool_id):
        _fail(origin, "id must be a lower_snake identifier")
    if not isinstance(raw["module"], str) or not _MODULE.fullmatch(raw["module"]):
        _fail(tool_id, "module must be a dotted Python module path")
    subcommand = raw.get("subcommand")
    if subcommand is not None and (not isinstance(subcommand, str) or not _ID.fullmatch(subcommand)):
        _fail(tool_id, "subcommand must be a lower_snake identifier")
    if not isinstance(raw["parser"], str) or not _PARSER.fullmatch(raw["parser"]):
        _fail(tool_id, "parser must be 'module:function'")
    if raw["kind"] not in _KINDS:
        _fail(tool_id, f"kind must be one of {sorted(_KINDS)}")
    if raw["effects"] not in _EFFECTS:
        _fail(tool_id, f"effects must be one of {sorted(_EFFECTS)}")
    if (raw["kind"] == "service") != (raw["effects"] == "service"):
        _fail(tool_id, "a service tool has service effects, and only a service tool does")
    if not isinstance(raw["stop"], dict) or not isinstance(raw["stop"].get("mode"), str):
        _fail(tool_id, "stop must be an object with a mode")
    exit_codes = raw.get("exit_codes", {})
    if not isinstance(exit_codes, dict) or not all(
            isinstance(k, str) and k.isdigit() and isinstance(v, str) for k, v in exit_codes.items()):
        _fail(tool_id, "exit_codes maps decimal strings to labels")
    if not isinstance(raw["params"], list) or not raw["params"]:
        _fail(tool_id, "params must be a non-empty list")

    params = tuple(_check_param(p, i, tool_id) for i, p in enumerate(raw["params"]))
    keys = [p.key for p in params]
    if len(set(keys)) != len(keys):
        _fail(tool_id, "a parameter key is used twice")
    flags = [p.emit.flag for p in params if p.emit.flag is not None]
    if len(set(flags)) != len(flags):
        # Two parameters writing the same flag would make the parser keep only
        # one of them, so the preview would not describe what runs.
        _fail(tool_id, "two parameters emit the same flag")
    by_key = {p.key: p for p in params}
    for param in params:
        when = param.get("when")
        if when is None:
            continue
        if not isinstance(when, dict) or not when:
            _fail(f"{tool_id}.{param.key}", "when must be a non-empty object")
        for other, expected in when.items():
            if other not in by_key or other == param.key:
                _fail(f"{tool_id}.{param.key}", f"when refers to unknown parameter {other!r}")
            check_value(by_key[other], expected, f"{tool_id}.{param.key}.when.{other}")

    dry_run = raw["dry_run"]
    if dry_run == "unsupported":
        dry_run_param = None
    elif isinstance(dry_run, dict) and set(dry_run) == {"param", "default"}:
        dry_run_param = dry_run["param"]
        if dry_run_param not in by_key or by_key[dry_run_param].type != "bool":
            _fail(tool_id, "dry_run.param must name a bool parameter")
        if dry_run["default"] is not True or by_key[dry_run_param].get("default") is not True:
            # A writing tool's first preview must be a dry run (design §8.1).
            _fail(tool_id, "dry_run must default to true, in the tool and in its parameter")
    else:
        _fail(tool_id, "dry_run must be 'unsupported' or {param, default}")
    if raw["effects"] == "writes_files" and dry_run_param is None:
        _fail(tool_id, "a tool that writes files must offer a dry run")

    return Tool(id=tool_id, module=raw["module"], subcommand=subcommand, parser=raw["parser"],
                kind=raw["kind"], effects=raw["effects"], dry_run_param=dry_run_param,
                params=params, raw=raw)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise SchemaError(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


def load_tool(path: Path) -> Tool:
    """Parse and validate one schema file; duplicate JSON keys are refused."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"{path.name}: not valid JSON ({exc})") from exc
    tool = validate_tool(raw, origin=path.name)
    if path.stem != tool.id:
        _fail(path.name, f"file name must match the tool id {tool.id!r}")
    return tool


def load_registry(directory: Path = SCHEMA_DIR) -> dict[str, Tool]:
    """Load every ``*.json`` schema; any invalid one refuses the whole registry."""
    tools: dict[str, Tool] = {}
    for path in sorted(directory.glob("*.json")):
        tool = load_tool(path)
        tools[tool.id] = tool
    if not tools:
        raise SchemaError(f"no tool schemas found in {directory}")
    return tools
