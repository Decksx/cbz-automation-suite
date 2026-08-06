"""One definition of "same series", enforced (issue #44).

Until #44 three modules each carried their own implementation of the series
identity rule, with their own copies of the same three regexes. They agreed
across a 31-case corpus, so nothing failed -- but nothing compared them
either, and a change to one would have desynchronized the watcher from the
lock registry and SeriesIndex silently.

These tests exist because that failure mode is invisible by construction: a
reintroduced private copy passes every other test in the suite, right up
until someone edits it.

The guard is asserted two ways on purpose. Object identity catches a copy
that is wired in; the AST check catches one that is defined but not yet used,
which is the state a half-finished edit leaves behind.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from scripts import cbz_library_maintenance as maintenance
from scripts import cbz_routing as routing
from scripts import cbz_watcher as watcher

CANONICAL = routing.series_key

# Module -> the name that must resolve to the canonical implementation, and
# the regex constants that must not be redefined alongside a private copy.
CONSUMERS = {
    "scripts/cbz_watcher.py": ("_series_key",
                               ("_SERIES_KEY_PUNCT_RE", "_SERIES_KEY_SPACE_RE")),
    "scripts/cbz_library_maintenance.py": ("normalise_series_key",
                                           ("_PUNCT_RE",)),
}


def _module_ast(relative: str) -> ast.Module:
    return ast.parse(Path(relative).read_text(encoding="utf-8"))


# ── the canonical rule is the only implementation ────────────────


def test_the_watcher_uses_the_canonical_implementation():
    assert watcher._series_key is CANONICAL


def test_maintenance_uses_the_canonical_implementation():
    assert maintenance.normalise_series_key is CANONICAL


def test_the_lock_key_is_built_on_the_canonical_implementation():
    """lock_key adds NFC around it but must not reimplement it."""
    from scripts import cbz_lock_order

    source = inspect.getsource(cbz_lock_order.lock_key)
    assert "series_key(" in source, "lock_key no longer delegates"
    assert cbz_lock_order.series_key is CANONICAL


@pytest.mark.parametrize("relative, expected", [
    (path, name) for path, (name, _) in CONSUMERS.items()
])
def test_no_module_redefines_the_rule_as_a_function(relative, expected):
    """AST-level, so a private copy is caught even before it is wired in.

    A source-text search for the regex body would pass the moment someone
    reformatted it. This asserts on the parsed structure: no `def` and no
    `lambda` bound to the consumer's name anywhere in the module.
    """
    tree = _module_ast(relative)
    offenders = [
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == expected
    ]
    assert offenders == [], (
        f"{relative} defines {expected} as a function again; it must be the "
        f"canonical scripts.cbz_routing.series_key"
    )


@pytest.mark.parametrize("relative, gone", [
    (path, constants) for path, (_, constants) in CONSUMERS.items()
])
def test_the_duplicate_regex_constants_are_not_reintroduced(relative, gone):
    """The regexes only existed to serve the private copies.

    `_MARKER_WORDS_RE` in both modules and `_SPACES_RE` in maintenance are
    deliberately *not* listed: each is used independently for a question other
    than series identity, so their presence is correct.
    """
    tree = _module_ast(relative)
    assigned = {
        target.id
        for node in ast.walk(tree) if isinstance(node, ast.Assign)
        for target in node.targets if isinstance(target, ast.Name)
    }
    reintroduced = sorted(assigned & set(gone))
    assert reintroduced == [], (
        f"{relative} redefines {reintroduced}, which existed only to serve a "
        f"private series-key implementation"
    )


def test_the_consumer_list_matches_the_modules_that_import_the_rule():
    """Fails when a fourth consumer appears without being guarded.

    A new module importing series_key is fine; one that imports it and then
    shadows it is the thing this suite exists to prevent, and it would go
    unnoticed unless the list is forced to stay current.
    """
    importers = set()
    for path in Path("scripts").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "scripts.cbz_routing":
                if any(alias.name == "series_key" for alias in node.names):
                    importers.add(path.as_posix())
    expected = {
        "scripts/cbz_watcher.py",
        "scripts/cbz_library_maintenance.py",
        "scripts/cbz_library_reclassify.py",
        "scripts/cbz_lock_order.py",
    }
    assert importers == expected, (
        f"the set of modules importing series_key changed: {sorted(importers)}. "
        "Add the new one to CONSUMERS above if it can shadow the name."
    )


# ── behaviour is unchanged by the consolidation ──────────────────


CORPUS = [
    "Berserk", "BERSERK", "Berserk!!", "Berserk (Uncensored)", "Berserk  ",
    "Attack on Titan", "Attack-on-Titan", "Attack_on_Titan",
    "Re:Zero", "Fate/Zero", "Kanojo, Okarishimasu!", "Nagatoro (Decensored)",
    "One-Punch Man", "Dr. STONE", "JoJo's Bizarre Adventure",
    "Mob Psycho 100", "5 Centimeters per Second", "", "   ", "!!!",
]


@pytest.mark.parametrize("name", CORPUS)
def test_every_consumer_produces_the_same_key(name):
    assert watcher._series_key(name) == CANONICAL(name)
    assert maintenance.normalise_series_key(name) == CANONICAL(name)


@pytest.mark.parametrize("name, expected", [
    ("BERSERK", "berserk"),
    ("Berserk!!", "berserk"),
    ("Berserk (Uncensored)", "berserk"),
    ("  Berserk  ", "berserk"),
    ("Attack-on-Titan", "attack on titan"),
    ("Kanojo, Okarishimasu!", "kanojo okarishimasu"),
    ("", ""),
    ("!!!", ""),
])
def test_the_documented_normalization_rules_hold(name, expected):
    """Pins the contract the canonical docstring states, so the two cannot drift."""
    assert CANONICAL(name) == expected


def test_the_underscore_exception_is_gone():
    """Updated deliberately by PR B, which is why the old assertion existed.

    Before #44's separator change this asserted the opposite -- that `_`
    survived while `-` did not. It was written that way so the change could
    not land by a test quietly starting to pass; it had to be edited by
    someone who had decided to edit it.
    """
    assert CANONICAL("Attack_on_Titan") == "attack on titan"
    assert CANONICAL("Attack-on-Titan") == "attack on titan"
    assert CANONICAL("Attack_on_Titan") == CANONICAL("Attack on Titan")


# ── the None compatibility decision ──────────────────────────────


def test_the_canonical_rule_tolerates_none():
    """An explicit compatibility decision, not incidental cleanup.

    Consolidation gives the watcher and maintenance the canonical function's
    `name or ""` guard, so both stop raising TypeError on None. That widening
    is recorded here rather than discovered later.
    """
    assert CANONICAL(None) == ""
    assert watcher._series_key(None) == ""
    assert maintenance.normalise_series_key(None) == ""


def test_no_call_site_actually_depends_on_the_none_tolerance():
    """None is impossible by contract at every call site, and stays that way.

    The one place it could plausibly arrive is maintenance's ComicInfo reader,
    whose `field()` helper is annotated `-> str` and returns "" for a missing
    element. So the tolerance above is a floor, not a behaviour to rely on --
    if that helper ever starts returning None, this fails and the decision
    gets made deliberately rather than absorbed silently.
    """
    tree = _module_ast("scripts/cbz_library_maintenance.py")
    field_fns = [n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "field"]
    assert field_fns, "maintenance no longer defines the ComicInfo field() helper"
    for fn in field_fns:
        assert isinstance(fn.returns, ast.Name) and fn.returns.id == "str", (
            "field() no longer promises str; None could now reach series_key "
            "and the tolerance would stop being a floor"
        )
