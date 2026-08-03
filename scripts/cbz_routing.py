"""Routing configuration and rule evaluation for the watcher.

Replaces the flat glob list that routing.json v1 used. That format could
express exactly one idea -- "this source folder name means manga" -- and the
live file said it 55 times. The three-way split (explicitly adult -> Comix,
otherwise Asian origin -> Manga, otherwise Graphic Novels) is a predicate
over content attributes, which v1 had no vocabulary for.

The v2 shape is:

    lists    -- plain data, e.g. every Asian-origin source folder name
    signals  -- named predicate trees over fields, referencing lists
    rules    -- ordered signal -> destination, first match wins

so adding a scanlation site is appending one string rather than a four-line
rule object, and the whole precedence order is two rules and a default.

Three behaviours are deliberate:

* A missing ComicInfo field makes a matcher false, never an error. Archives
  with no metadata still route, they just fall through to source/title.
* An invalid config is fatal at load. v1 set its default destination to ""
  on any parse error, and `Path("") / "Batman"` is the *relative* path
  "Batman" -- a typo silently moved comics into the watcher's working
  directory instead of the library. Refusing to start is the correct
  failure.
* Every decision carries the rule and the matcher that produced it, so a
  misroute is diagnosable without reading the whole file by eye.

v1 files keep working: load() converts them in memory and logs a
deprecation notice.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VALID_OPERATORS = frozenset(
    {"equals", "in", "in_list", "glob", "glob_in_list", "contains_any"}
)
COMBINATORS = frozenset({"any", "all", "not"})
COMICINFO_PREFIX = "comicinfo."


class RoutingConfigError(ValueError):
    """Raised for any structurally invalid routing configuration."""


@dataclass(frozen=True)
class RoutingDecision:
    dest_key: str
    dest_path: str
    rule_name: str | None       # None means nothing matched; the default won
    reason: str

    @property
    def matched(self) -> bool:
        return self.rule_name is not None


@dataclass
class RoutingConfig:
    destinations: dict[str, str]
    default_key: str
    lists: dict[str, list[str]] = field(default_factory=dict)
    signals: dict[str, dict] = field(default_factory=dict)
    rules: list[dict] = field(default_factory=list)
    source_version: int = 2

    @property
    def default_path(self) -> str:
        return self.destinations[self.default_key]


# ---------------------------------------------------------------- context

def build_context(
    source: str,
    title: str,
    comic_info: dict[str, str] | None = None,
) -> dict[str, str]:
    """Flatten the routable facts into `field name -> value`.

    ComicInfo element names are matched case-insensitively, since the field
    references in a hand-edited config should not have to reproduce the XML's
    exact casing to work.
    """
    context = {"source": source or "", "title": title or ""}
    for key, value in (comic_info or {}).items():
        if value is None:
            continue
        context[f"{COMICINFO_PREFIX}{key.casefold()}"] = str(value)
    return context


def _lookup(context: dict[str, str], field_name: str) -> str | None:
    name = field_name.strip()
    if name.casefold().startswith(COMICINFO_PREFIX):
        key = COMICINFO_PREFIX + name[len(COMICINFO_PREFIX):].casefold()
    else:
        key = name.casefold()
    value = context.get(key)
    return value if value not in (None, "") else None


# ------------------------------------------------------------- evaluation

def _evaluate(node: Any, context: dict[str, str],
              cfg: RoutingConfig) -> tuple[bool, str]:
    """Evaluate a predicate node. Returns (result, human-readable reason)."""
    if isinstance(node, str):
        # A bare string references a named signal.
        signal = cfg.signals.get(node)
        if signal is None:
            raise RoutingConfigError(f"unknown signal: {node!r}")
        ok, why = _evaluate(signal, context, cfg)
        return ok, f"{node}({why})" if ok else why

    if not isinstance(node, dict):
        raise RoutingConfigError(f"predicate must be an object or signal name: {node!r}")

    for combinator in ("any", "all", "not"):
        if combinator in node:
            children = node[combinator]
            if combinator == "not":
                ok, why = _evaluate(children, context, cfg)
                return (not ok), f"not({why})"
            if not isinstance(children, list) or not children:
                raise RoutingConfigError(f"'{combinator}' needs a non-empty list")
            reasons = []
            for child in children:
                ok, why = _evaluate(child, context, cfg)
                if combinator == "any" and ok:
                    return True, why
                if combinator == "all" and not ok:
                    return False, f"all failed at {why}"
                reasons.append(why)
            return (combinator == "all"), (
                "; ".join(reasons) if combinator == "all" else "no match"
            )

    return _evaluate_matcher(node, context, cfg)


def _evaluate_matcher(node: dict, context: dict[str, str],
                      cfg: RoutingConfig) -> tuple[bool, str]:
    field_name = node.get("field")
    if not field_name:
        raise RoutingConfigError(f"matcher missing 'field': {node!r}")

    operators = [k for k in node if k != "field"]
    if len(operators) != 1:
        raise RoutingConfigError(
            f"matcher on {field_name!r} needs exactly one operator, got {operators}"
        )
    op = operators[0]
    if op not in VALID_OPERATORS:
        raise RoutingConfigError(f"unknown operator {op!r} on field {field_name!r}")

    value = _lookup(context, field_name)
    if value is None:
        # Absent metadata is a false matcher, never an error.
        return False, f"{field_name} absent"

    operand = node[op]
    folded = value.casefold()

    def named_list(name: str) -> list[str]:
        items = cfg.lists.get(name)
        if items is None:
            raise RoutingConfigError(f"unknown list: {name!r}")
        return items

    if op == "equals":
        ok = folded == str(operand).casefold()
    elif op == "in":
        ok = folded in {str(x).casefold() for x in operand}
    elif op == "in_list":
        ok = folded in {str(x).casefold() for x in named_list(str(operand))}
    elif op == "glob":
        ok = fnmatch.fnmatch(folded, str(operand).casefold())
    elif op == "glob_in_list":
        ok = any(fnmatch.fnmatch(folded, str(p).casefold())
                 for p in named_list(str(operand)))
    else:  # contains_any -- for comma-joined free text like Genre/Tags
        ok = any(str(x).casefold() in folded for x in operand)

    return ok, f"{field_name}={value!r} {op} {operand!r}" if ok else (
        f"{field_name}={value!r} !{op}"
    )


def resolve(cfg: RoutingConfig, context: dict[str, str]) -> RoutingDecision:
    """Evaluate rules top to bottom; first match wins, else the default."""
    for rule in cfg.rules:
        ok, why = _evaluate(rule["when"], context, cfg)
        if ok:
            key = rule["dest"]
            return RoutingDecision(key, cfg.destinations[key],
                                   rule.get("name", key), why)
    return RoutingDecision(cfg.default_key, cfg.default_path, None,
                           "no rule matched; default")


def explain(cfg: RoutingConfig, context: dict[str, str]) -> list[str]:
    """Full evaluation trace: every rule, whether it fired, and why."""
    lines = [f"context: {context}"]
    for rule in cfg.rules:
        ok, why = _evaluate(rule["when"], context, cfg)
        mark = "MATCH " if ok else "  --  "
        lines.append(f"{mark}{rule.get('name', rule['dest'])}: {why}")
        if ok:
            lines.append(f"       -> {rule['dest']} = {cfg.destinations[rule['dest']]}")
            return lines
    lines.append(f"  --  (default) -> {cfg.default_key} = {cfg.default_path}")
    return lines


# ------------------------------------------------------------- loading

def _convert_v1(raw: dict) -> dict:
    """Fold a v1 file into v2 without changing which files go where.

    v1 rules are first-match-wins globs, so a *consecutive* run sharing the
    same (match, dest) can be collapsed into one list plus one rule with no
    semantic change. The live file is 55 consecutive source->manga rules, so
    this yields exactly one list and one rule.
    """
    lists: dict[str, list[str]] = {}
    signals: dict[str, dict] = {}
    rules: list[dict] = []

    runs: list[tuple[str, str, list[str]]] = []
    for rule in raw.get("rules", []):
        if "pattern" not in rule or "dest" not in rule:
            continue                       # v1 allowed bare _comment entries
        match_on = rule.get("match", "source")
        dest = rule["dest"]
        if runs and runs[-1][0] == match_on and runs[-1][1] == dest:
            runs[-1][2].append(rule["pattern"])
        else:
            runs.append((match_on, dest, [rule["pattern"]]))

    for index, (match_on, dest, patterns) in enumerate(runs, start=1):
        suffix = "" if len(runs) == 1 else f"_{index}"
        list_name = f"v1_{dest}_{match_on}_patterns{suffix}"
        signal_name = f"v1_{dest}_by_{match_on}{suffix}"
        lists[list_name] = patterns
        signals[signal_name] = {
            "any": [{"field": match_on, "glob_in_list": list_name}]
        }
        rules.append({
            "name": f"migrated v1 {match_on} rules -> {dest}",
            "when": signal_name,
            "dest": dest,
        })

    return {
        "version": 2,
        "destinations": dict(raw.get("destinations", {})),
        "default": raw.get("default", ""),
        "lists": lists,
        "signals": signals,
        "rules": rules,
    }


def parse(raw: dict) -> RoutingConfig:
    source_version = int(raw.get("version", 1))
    if source_version == 1:
        raw = _convert_v1(raw)
    elif source_version != 2:
        raise RoutingConfigError(f"unsupported routing config version: {source_version}")

    destinations = raw.get("destinations") or {}
    if not destinations:
        raise RoutingConfigError("routing config defines no destinations")
    for key, value in destinations.items():
        if not value or not Path(value).is_absolute():
            raise RoutingConfigError(
                f"destination {key!r} must be an absolute path, got {value!r}"
            )

    default_key = raw.get("default") or ""
    if default_key not in destinations:
        raise RoutingConfigError(
            f"default {default_key!r} is not one of {sorted(destinations)}"
        )

    cfg = RoutingConfig(
        destinations=destinations,
        default_key=default_key,
        lists={k: list(v) for k, v in (raw.get("lists") or {}).items()},
        signals=dict(raw.get("signals") or {}),
        rules=list(raw.get("rules") or []),
        source_version=source_version,
    )

    for index, rule in enumerate(cfg.rules):
        if "when" not in rule:
            raise RoutingConfigError(f"rule {index} has no 'when'")
        if rule.get("dest") not in destinations:
            raise RoutingConfigError(
                f"rule {index} destination {rule.get('dest')!r} is not defined"
            )
        # Validate the predicate now rather than on the first archive that
        # happens to reach it.
        _validate_predicate(rule["when"], cfg)

    for name, signal in cfg.signals.items():
        _validate_predicate(signal, cfg, _seen={name})

    return cfg


def _validate_predicate(node: Any, cfg: RoutingConfig,
                        _seen: set[str] | None = None) -> None:
    seen = set(_seen or ())
    if isinstance(node, str):
        if node not in cfg.signals:
            raise RoutingConfigError(f"unknown signal: {node!r}")
        if node in seen:
            raise RoutingConfigError(f"signal {node!r} references itself")
        _validate_predicate(cfg.signals[node], cfg, seen | {node})
        return

    if not isinstance(node, dict):
        raise RoutingConfigError(f"predicate must be an object or signal name: {node!r}")

    for combinator in COMBINATORS:
        if combinator in node:
            children = node[combinator]
            if combinator == "not":
                _validate_predicate(children, cfg, seen)
                return
            if not isinstance(children, list) or not children:
                raise RoutingConfigError(f"'{combinator}' needs a non-empty list")
            for child in children:
                _validate_predicate(child, cfg, seen)
            return

    if "field" not in node:
        raise RoutingConfigError(f"matcher missing 'field': {node!r}")
    operators = [k for k in node if k != "field"]
    if len(operators) != 1:
        raise RoutingConfigError(
            f"matcher on {node['field']!r} needs exactly one operator, got {operators}"
        )
    op = operators[0]
    if op not in VALID_OPERATORS:
        raise RoutingConfigError(f"unknown operator {op!r} on field {node['field']!r}")
    if op in ("in_list", "glob_in_list") and str(node[op]) not in cfg.lists:
        raise RoutingConfigError(f"unknown list: {node[op]!r}")


def load(path: Path) -> RoutingConfig:
    """Read and validate a routing config. Raises rather than degrading."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError as exc:
        raise RoutingConfigError(f"routing config not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RoutingConfigError(f"routing config is not valid JSON: {exc}") from exc
    return parse(raw)


def to_v2_dict(cfg: RoutingConfig) -> dict:
    """Serialise back to the v2 on-disk shape, for --migrate-routing."""
    return {
        "version": 2,
        "destinations": cfg.destinations,
        "default": cfg.default_key,
        "lists": cfg.lists,
        "signals": cfg.signals,
        "rules": cfg.rules,
    }
