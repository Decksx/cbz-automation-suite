"""Emit styles and conditions the maintenance schema does not use, on a synthetic tool.

The watcher schema (M6) will need ``when``, ordered ``repeat`` lists, ``fixed``
configuration values, optional paths and numbers; these tests pin that
behaviour before any real schema depends on it.
"""
from __future__ import annotations

import pytest

from comic_automation.web.argv import ArgvError, build_argv
from comic_automation.web.tool_schema import validate_tool

TOOL = validate_tool({
    "id": "fixture", "label": "Fixture", "domain": "test", "module": "scripts.fixture",
    "parser": "scripts.fixture:build_parser", "kind": "service", "effects": "service",
    "dry_run": "unsupported", "stop": {"mode": "none"},
    "params": [
        {"key": "enabled", "type": "bool", "default": False,
         "emit": {"style": "flag", "flag": "--enabled"}},
        {"key": "models", "type": "multi_choice", "ordered": True, "unique": True, "min": 1, "max": 3,
         "choices": ["a", "b", "c", "d"], "default": ["a"], "when": {"enabled": True},
         "emit": {"style": "repeat", "flag": "--model"}},
        {"key": "hold", "type": "path", "role": "readiness_hold", "kind": "dir", "optional": True,
         "emit": {"style": "option", "flag": "--hold"}},
        {"key": "minutes", "type": "number", "min": 0, "max": 1440, "default": 30,
         "emit": {"style": "option", "flag": "--minutes"}},
        {"key": "count", "type": "int", "min": 1, "max": 9, "default": 2,
         "emit": {"style": "equals", "flag": "--count"}},
        {"key": "tag", "type": "text", "max_len": 8, "pattern": "[a-z]+", "default": "abc",
         "emit": {"style": "option", "flag": "--tag"}},
        {"key": "registry", "type": "fixed", "source": "naming.registry",
         "emit": {"style": "option", "flag": "--registry"}},
    ],
}, origin="fixture.json")
FIXED = {"naming.registry": r"C:\cfg\number-styles.json"}
HEAD = ["-m", "scripts.fixture"]
TAIL = ["--minutes", "30", "--count=2", "--tag", "abc", "--registry", r"C:\cfg\number-styles.json"]


def test_a_false_when_omits_the_dependent_parameter():
    assert build_argv(TOOL, {}, FIXED) == HEAD + TAIL


def test_a_true_when_emits_it_and_repeat_keeps_the_given_order():
    argv = build_argv(TOOL, {"enabled": True, "models": ["c", "a", "b"]}, FIXED)
    assert argv == HEAD + ["--enabled", "--model", "c", "--model", "a", "--model", "b"] + TAIL


def test_an_optional_path_is_emitted_only_when_given():
    argv = build_argv(TOOL, {"hold": r"D:\hold"}, FIXED)
    assert argv[argv.index("--hold") + 1] == r"D:\hold"


def test_null_omits_only_an_optional_parameter():
    assert "--hold" not in build_argv(TOOL, {"hold": None}, FIXED)
    for key in ("enabled", "minutes", "count", "tag", "models"):
        with pytest.raises(ArgvError, match="null is not accepted"):
            build_argv(TOOL, {key: None}, FIXED)
    # A fixed value that configuration resolves to null is refused too, not omitted.
    with pytest.raises(ArgvError, match="null is not accepted"):
        build_argv(TOOL, {}, {"naming.registry": None})


def test_a_fixed_value_comes_from_configuration_and_cannot_be_supplied():
    with pytest.raises(ArgvError, match="fixed by configuration"):
        build_argv(TOOL, {"registry": r"C:\evil.json"}, FIXED)
    with pytest.raises(ArgvError, match="not available"):
        build_argv(TOOL, {}, {})


def test_an_empty_list_is_refused_unless_the_schema_allows_omitting_it():
    # Silently dropping the flag would hand the tool its own default instead of
    # the empty selection the operator asked for; only omit_empty permits that.
    strict = validate_tool({
        "id": "strict", "label": "Strict", "domain": "test", "module": "scripts.fixture",
        "parser": "scripts.fixture:build_parser", "kind": "task", "effects": "read_only",
        "dry_run": "unsupported", "stop": {"mode": "none"},
        "params": [{"key": "items", "type": "multi_choice", "choices": ["a", "b"], "default": ["a"],
                    "emit": {"style": "csv", "flag": "--items"}}],
    }, origin="strict.json")
    with pytest.raises(ArgvError, match="empty list cannot be emitted"):
        build_argv(strict, {"items": []})


@pytest.mark.parametrize("params,message", [
    ({"enabled": True, "models": ["a", "b", "c", "d"]}, "at most 3"),
    ({"minutes": float("inf")}, "finite number"),
    ({"minutes": 1441}, "at most 1440"),
    ({"count": True}, "finite int"),
    ({"count": 2.0}, "finite int"),
    ({"tag": "ABC"}, "pattern"),
    ({"tag": "abcdefghi"}, "longer than"),
])
def test_type_constraints_apply_to_supplied_values(params, message):
    with pytest.raises(ArgvError, match=message):
        build_argv(TOOL, params, FIXED)
