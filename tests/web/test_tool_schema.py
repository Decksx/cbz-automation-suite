"""The tool meta-schema refuses malformed schemas, so a typo cannot drop an option silently.

Each case starts from the real maintenance schema and breaks exactly one rule,
so a passing case proves that specific guard is what refused it.
"""
from __future__ import annotations

import copy
import json

import pytest

from comic_automation.web.tool_schema import (
    SCHEMA_DIR, SchemaError, load_registry, load_tool, validate_tool,
)

BASE = json.loads((SCHEMA_DIR / "maintenance.json").read_text(encoding="utf-8"))


def _param(doc, key):
    return next(p for p in doc["params"] if p["key"] == key)


def _broken(change):
    doc = copy.deepcopy(BASE)
    change(doc)
    return doc


CASES = [
    ("unknown tool field", lambda d: d.update(colour="red"), "unknown tool fields"),
    ("missing parser", lambda d: d.pop("parser"), "missing tool fields"),
    ("bad id", lambda d: d.update(id="Maintenance"), "lower_snake"),
    ("bad module", lambda d: d.update(module="scripts/cbz_workflows"), "dotted"),
    ("bad kind", lambda d: d.update(kind="daemon"), "kind must be"),
    ("service effects on a task", lambda d: d.update(effects="service"), "service tool"),
    ("writer without dry run", lambda d: d.update(dry_run="unsupported"), "must offer a dry run"),
    ("dry run defaulting off", lambda d: _param(d, "dry_run").update(default=False), "default to true"),
    ("dry run param not bool", lambda d: d["dry_run"].update(param="sort"), "bool parameter"),
    ("unknown param field", lambda d: _param(d, "sort").update(colour="red"), "unknown fields"),
    ("unknown type", lambda d: _param(d, "sort").update(type="enum"), "unknown type"),
    ("style cannot express type", lambda d: _param(d, "dry_run")["emit"].update(style="option"), "cannot express"),
    ("flag not long form", lambda d: _param(d, "sort")["emit"].update(flag="-s"), "--long-name"),
    ("positional with flag", lambda d: _param(d, "root")["emit"].update(flag="--root"), "no flag"),
    ("omit_empty on a scalar", lambda d: _param(d, "sort")["emit"].update(omit_empty=True), "omit_empty"),
    ("duplicate choices", lambda d: _param(d, "sort").update(choices=["newest", "newest"]), "distinct"),
    ("comma in csv choice", lambda d: _param(d, "rules")["choices"].append("a,b"), "comma"),
    ("default outside choices", lambda d: _param(d, "sort").update(default="random"), "must be one of"),
    ("bool default as int", lambda d: _param(d, "restart").update(default=0), "true or false"),
    ("missing default", lambda d: _param(d, "sort").pop("default"), "needs a default"),
    ("min above max", lambda d: _param(d, "stages").update(max=0), "min is greater"),
    ("path without role", lambda d: _param(d, "root").pop("role"), "role"),
    ("path kind", lambda d: _param(d, "root").update(kind="link"), "dir or file"),
    ("must_exist and create_new", lambda d: _param(d, "root").update(create_new=True), "contradict"),
    ("duplicate key", lambda d: d["params"].append(copy.deepcopy(_param(d, "sort"))), "used twice"),
    ("duplicate flag", lambda d: _param(d, "names_only")["emit"].update(flag="--restart"), "same flag"),
    ("when to unknown key", lambda d: _param(d, "sort").update(when={"nope": True}), "unknown parameter"),
    ("when to itself", lambda d: _param(d, "sort").update(when={"sort": "newest"}), "unknown parameter"),
    ("when with a bad value", lambda d: _param(d, "sort").update(when={"dry_run": "yes"}), "true or false"),
]


@pytest.mark.parametrize("name,change,message", CASES, ids=[c[0] for c in CASES])
def test_each_meta_schema_rule_refuses_its_own_violation(name, change, message):
    with pytest.raises(SchemaError, match=message):
        validate_tool(_broken(change), origin="maintenance.json")


def test_the_shipped_schemas_load():
    tools = load_registry()
    assert "maintenance" in tools
    assert tools["maintenance"].dry_run_param == "dry_run"


def test_duplicate_json_keys_are_refused(tmp_path):
    # json.loads keeps the last duplicate silently; the registry must not.
    text = (SCHEMA_DIR / "maintenance.json").read_text(encoding="utf-8")
    text = text.replace('"kind": "task",', '"kind": "task", "kind": "service",', 1)
    path = tmp_path / "maintenance.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(SchemaError, match="duplicate JSON key"):
        load_tool(path)


def test_the_file_name_must_match_the_tool_id(tmp_path):
    path = tmp_path / "other.json"
    path.write_text(json.dumps(BASE), encoding="utf-8")
    with pytest.raises(SchemaError, match="must match the tool id"):
        load_tool(path)


def test_one_invalid_schema_refuses_the_whole_registry(tmp_path):
    (tmp_path / "maintenance.json").write_text(json.dumps(BASE), encoding="utf-8")
    (tmp_path / "broken.json").write_text(json.dumps(dict(BASE, id="broken", kind="nope")), encoding="utf-8")
    with pytest.raises(SchemaError):
        load_registry(tmp_path)


def test_an_empty_registry_is_refused(tmp_path):
    with pytest.raises(SchemaError, match="no tool schemas"):
        load_registry(tmp_path)
