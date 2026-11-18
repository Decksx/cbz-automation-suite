"""The argv builder for console tools (design §8.1, milestone M4a).

This is the only code that turns operator parameters into a tool's arguments.
It returns a list, never a string, so no value is ever re-split or interpreted
by a shell; the preview shows ``subprocess.list2cmdline`` of the same list.

The builder trusts the *shape* of a :class:`~comic_automation.web.tool_schema.Tool`
(already checked by the meta-schema) but not the *values*: every supplied value
goes through the same :func:`~comic_automation.web.tool_schema.check_value` as
schema defaults, and an unknown parameter key is refused rather than ignored, so
a typo in a request cannot silently fall back to a default.

Out of scope for M4a: interpreter and cwd selection, filesystem checks for
paths (the M4b allowlist), and the seal. The result starts at ``-m``.
"""
from __future__ import annotations

from typing import Any, Mapping

from .tool_schema import Param, SchemaError, Tool, check_value


class ArgvError(ValueError):
    """The supplied parameters cannot be turned into a command for this tool."""


def resolve_params(tool: Tool, supplied: Mapping[str, Any],
                   fixed: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return every parameter's effective value: supplied, else default, else fixed.

    ``fixed`` maps a ``fixed`` parameter's configuration ``source`` to its value;
    the browser can never supply one. Parameters inactive under ``when`` are
    still resolved here, so the seal records the full effective set; the builder
    decides what to emit.
    """
    fixed = fixed or {}
    known = {p.key for p in tool.params}
    unknown = sorted(set(supplied) - known)
    if unknown:
        raise ArgvError(f"{tool.id}: unknown parameters {unknown}")
    values: dict[str, Any] = {}
    for param in tool.params:
        where = f"{tool.id}.{param.key}"
        if param.type == "fixed":
            if param.key in supplied:
                raise ArgvError(f"{where}: is fixed by configuration and cannot be set")
            source = param.get("source")
            if source not in fixed:
                raise ArgvError(f"{where}: configuration value {source!r} is not available")
            value = fixed[source]
        elif param.key in supplied:
            value = supplied[param.key]
        elif "default" in param.raw:
            value = param.raw["default"]
        elif param.get("optional"):
            value = None
        else:
            raise ArgvError(f"{where}: a value is required")
        if value is not None:
            try:
                check_value(param, value, where)
            except SchemaError as exc:
                raise ArgvError(str(exc)) from exc
        values[param.key] = value
    return values


def _active(param: Param, values: Mapping[str, Any]) -> bool:
    when = param.get("when")
    return when is None or all(values.get(k) == v for k, v in when.items())


def _scalar(value: Any) -> str:
    return value if isinstance(value, str) else repr(value) if isinstance(value, float) else str(value)


def _list_values(param: Param, value: list[str]) -> list[str]:
    if param.get("ordered"):
        # Order is meaningful (for example the watcher's Camelia stage chain).
        return list(value)
    # Otherwise emit in the schema's canonical order, so two equivalent
    # selections give the same argv and the same seal.
    order = param.get("choices")
    return sorted(value, key=order.index)


def build_argv(tool: Tool, supplied: Mapping[str, Any],
               fixed: Mapping[str, str] | None = None) -> list[str]:
    """Build ``["-m", module, (subcommand), ...]`` for the supplied parameters."""
    values = resolve_params(tool, supplied, fixed)
    argv = ["-m", tool.module]
    if tool.subcommand:
        argv.append(tool.subcommand)
    for param in tool.params:
        value = values[param.key]
        if value is None or not _active(param, values):
            continue
        style, flag = param.emit.style, param.emit.flag
        if style == "flag":
            if value:
                argv.append(flag)
        elif style == "flag_false":
            if not value:
                argv.append(flag)
        elif style == "positional":
            argv.append(_scalar(value))
        elif style == "option":
            argv.extend([flag, _scalar(value)])
        elif style == "equals":
            argv.append(f"{flag}={_scalar(value)}")
        elif style in ("csv", "repeat"):
            items = _list_values(param, value)
            if not items:
                if param.emit.omit_empty:
                    continue
                raise ArgvError(f"{tool.id}.{param.key}: an empty list cannot be emitted")
            if style == "csv":
                argv.append(f"{flag}={','.join(items)}")
            else:
                for item in items:
                    argv.extend([flag, item])
        else:  # pragma: no cover - the meta-schema admits only the styles above
            raise ArgvError(f"{tool.id}.{param.key}: unsupported emit style {style!r}")
    return argv
