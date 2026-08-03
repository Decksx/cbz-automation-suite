"""Tests for the v2 routing engine.

Two things these pin down beyond ordinary rule evaluation:

* A v1 file must keep routing every archive exactly where it did before.
  The live routing.json is 55 consecutive source->manga globs, and the
  conversion is only safe because first-match-wins makes a consecutive
  same-destination run collapsible.
* An invalid config must raise. v1 responded to a parse error by setting
  its default destination to "", and Path("") / "Batman" is the relative
  path "Batman" -- comics silently landed in the watcher's working
  directory instead of the library.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.cbz_routing import (
    RoutingConfigError,
    build_context,
    explain,
    load,
    parse,
    resolve,
    to_v2_dict,
)

V2 = {
    "version": 2,
    "destinations": {
        "comix": "X:\\Comix",
        "manga": "X:\\Manga",
        "graphic_novels": "X:\\Graphic Novels",
    },
    "default": "graphic_novels",
    "lists": {
        "asian_origin_sources": ["Asura Scans (EN)", "Atsumaru*"],
        "asian_imprints": ["Ghost Ship", "Seven Seas"],
        "asian_languages": ["ja", "ko", "zh"],
    },
    "signals": {
        "explicitly_adult": {
            "any": [{"field": "comicinfo.AgeRating", "equals": "Adults Only 18+"}]
        },
        "asian_origin": {
            "any": [
                {"field": "comicinfo.LanguageISO", "in_list": "asian_languages"},
                {"field": "comicinfo.Manga", "in": ["Yes", "YesAndRightToLeft"]},
                {"field": "comicinfo.Imprint", "in_list": "asian_imprints"},
                {"field": "comicinfo.Genre", "contains_any": ["manga", "manhwa"]},
                {"field": "source", "glob_in_list": "asian_origin_sources"},
            ]
        },
    },
    "rules": [
        {"name": "explicit adult", "when": "explicitly_adult", "dest": "comix"},
        {"name": "Asian origin", "when": "asian_origin", "dest": "manga"},
    ],
}


def _route(source="", title="", **comicinfo):
    cfg = parse(json.loads(json.dumps(V2)))
    return resolve(cfg, build_context(source, title, comicinfo))


# ── the three-way precedence ─────────────────────────────────────


def test_unmatched_content_falls_through_to_graphic_novels():
    decision = _route(source="Some Indie Publisher", title="Saga")
    assert decision.dest_key == "graphic_novels"
    assert not decision.matched


def test_asian_origin_routes_to_manga_by_language():
    assert _route(title="Berserk", LanguageISO="ja").dest_key == "manga"


def test_asian_origin_routes_to_manga_by_source_glob():
    # "Atsumaru*" in the list must still glob, as it did under v1.
    assert _route(source="Atsumaru (EN)").dest_key == "manga"


def test_explicitly_adult_outranks_asian_origin():
    # Both signals fire; the adult rule is first, so Comix wins.
    decision = _route(LanguageISO="ja", AgeRating="Adults Only 18+")
    assert decision.dest_key == "comix"
    assert decision.rule_name == "explicit adult"


def test_ecchi_imprint_is_a_manga_signal_not_an_adult_one():
    # The constraint that motivated the redesign: Ghost Ship and similar
    # mature-but-not-explicit imprints must reach Manga, never Comix.
    decision = _route(title="Nagatoro", Imprint="Ghost Ship")
    assert decision.dest_key == "manga"


# ── metadata absence must never be an error ──────────────────────


def test_missing_comicinfo_routes_by_source_alone():
    assert _route(source="Asura Scans (EN)").dest_key == "manga"


def test_no_metadata_at_all_still_routes():
    assert _route().dest_key == "graphic_novels"


def test_empty_field_value_is_treated_as_absent():
    assert _route(LanguageISO="").dest_key == "graphic_novels"


def test_matching_is_case_insensitive_on_field_names_and_values():
    assert _route(languageiso="JA").dest_key == "manga"


# ── decisions are explainable ────────────────────────────────────


def test_decision_reports_the_matcher_that_fired():
    decision = _route(Genre="Action, Manga, Fantasy")
    assert decision.dest_key == "manga"
    assert "Genre" in decision.reason and "contains_any" in decision.reason


def test_explain_traces_every_rule_until_the_match():
    cfg = parse(json.loads(json.dumps(V2)))
    lines = explain(cfg, build_context("", "Nagatoro", {"Imprint": "Ghost Ship"}))
    assert any("MATCH" in line and "Asian origin" in line for line in lines)
    assert any("explicit adult" in line for line in lines)


# ── invalid configuration is fatal, never a silent default ───────


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda c: c.__setitem__("default", "nowhere"), "default"),
        (lambda c: c["rules"][0].__setitem__("dest", "nowhere"), "not defined"),
        (lambda c: c["rules"][0].__setitem__("when", "no_such_signal"), "unknown signal"),
        (lambda c: c["destinations"].__setitem__("comix", "Comix"), "absolute"),
        (lambda c: c["signals"]["asian_origin"]["any"][0].__setitem__(
            "in_list", "no_such_list"), "unknown list"),
        (lambda c: c["signals"]["asian_origin"]["any"][0].__setitem__(
            "wat", "x"), "exactly one operator"),
    ],
)
def test_invalid_config_raises(mutate, fragment):
    raw = json.loads(json.dumps(V2))
    mutate(raw)
    with pytest.raises(RoutingConfigError) as excinfo:
        parse(raw)
    assert fragment in str(excinfo.value)


def test_unknown_operator_raises():
    raw = json.loads(json.dumps(V2))
    raw["signals"]["explicitly_adult"]["any"][0] = {
        "field": "comicinfo.AgeRating", "regex": ".*"
    }
    with pytest.raises(RoutingConfigError, match="unknown operator"):
        parse(raw)


def test_self_referential_signal_raises():
    raw = json.loads(json.dumps(V2))
    raw["signals"]["loop"] = "loop"
    with pytest.raises(RoutingConfigError, match="references itself"):
        parse(raw)


def test_missing_file_raises_rather_than_defaulting(tmp_path: Path):
    with pytest.raises(RoutingConfigError, match="not found"):
        load(tmp_path / "absent.json")


def test_malformed_json_raises_rather_than_defaulting(tmp_path: Path):
    bad = tmp_path / "routing.json"
    bad.write_text("{ not json", encoding="utf-8")
    with pytest.raises(RoutingConfigError, match="not valid JSON"):
        load(bad)


# ── v1 compatibility ─────────────────────────────────────────────

V1 = {
    "destinations": {"comix": "X:\\Comix", "manga": "X:\\Manga"},
    "default": "comix",
    "rules": [
        {"_comment": "--- Manga sources ---"},
        {"match": "source", "pattern": "Asura Scans (EN)", "dest": "manga"},
        {"match": "source", "pattern": "Atsumaru*", "dest": "manga"},
        {"match": "title", "pattern": "Batman*", "dest": "comix"},
    ],
}


def test_v1_file_still_routes_identically():
    cfg = parse(json.loads(json.dumps(V1)))
    assert resolve(cfg, build_context("Asura Scans (EN)", "x")).dest_key == "manga"
    assert resolve(cfg, build_context("Atsumaru (EN)", "x")).dest_key == "manga"
    assert resolve(cfg, build_context("DC", "Batman Year One")).dest_key == "comix"
    assert resolve(cfg, build_context("DC", "Saga")).dest_key == "comix"


def test_v1_consecutive_same_dest_rules_collapse_to_one_list():
    cfg = parse(json.loads(json.dumps(V1)))
    # Two consecutive source->manga globs become a single list; the
    # title->comix rule is a separate run and stays separate.
    assert len(cfg.rules) == 2
    manga_lists = [v for v in cfg.lists.values() if "Atsumaru*" in v]
    assert manga_lists and len(manga_lists[0]) == 2


def test_v1_comment_only_entries_are_dropped():
    cfg = parse(json.loads(json.dumps(V1)))
    assert all("_comment" not in rule for rule in cfg.rules)


def test_v1_converts_to_serialisable_v2():
    cfg = parse(json.loads(json.dumps(V1)))
    round_tripped = parse(json.loads(json.dumps(to_v2_dict(cfg))))
    assert resolve(
        round_tripped, build_context("Atsumaru (EN)", "x")
    ).dest_key == "manga"
    assert round_tripped.source_version == 2


def test_live_routing_json_still_loads_if_present():
    # The real file is v1 and gitignored; skip where it does not exist.
    live = Path(__file__).resolve().parents[1] / "routing.json"
    if not live.exists():
        pytest.skip("routing.json not present in this checkout")
    cfg = load(live)
    assert cfg.destinations
    assert resolve(cfg, build_context("Asura Scans (EN)", "x")).dest_key == "manga"
