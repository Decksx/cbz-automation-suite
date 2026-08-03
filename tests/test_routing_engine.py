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
    SeriesIndex,
    series_key,
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


# ── series overrides and the series index ────────────────────────

V2_SERIES = {
    **{k: v for k, v in V2.items() if k != "rules"},
    "rules": list(V2["rules"]),
    "series_overrides": [
        {
            "canonical": "Rent-a-Girlfriend",
            "aliases": ["Kanojo Okarishimasu", "Kanojo, Okarishimasu"],
        },
        {"canonical": "Berserk of Gluttony", "aliases": [], "dest": "manga"},
    ],
    "series_index": {"enabled": True, "destinations": ["manga", "graphic_novels"]},
}


def _series_cfg(**overrides):
    raw = json.loads(json.dumps(V2_SERIES))
    raw.update(overrides)
    return parse(raw)


def _index_from(mapping: dict[str, list[str]]) -> SeriesIndex:
    """Build an index without touching the filesystem."""
    idx = SeriesIndex()
    for dest_key, names in mapping.items():
        for name in names:
            idx.add(name, dest_key, Path(f"X:/{dest_key}/{name}"))
    return idx


def test_series_key_matches_the_watchers_normalisation():
    # The index is only useful if "same series" means the same thing here as
    # it does in cbz_watcher._series_key.
    assert series_key("Kanojo, Okarishimasu!") == series_key("kanojo okarishimasu")
    assert series_key("Nagatoro (Uncensored)") == series_key("Nagatoro")


def test_existing_series_wins_over_rules():
    # Rules alone would send this to graphic_novels; the series already
    # lives in manga, so it must stay together.
    cfg = _series_cfg()
    idx = _index_from({"manga": ["Some Western Sounding Title"]})
    decision = resolve(cfg, build_context("Indie", "x"),
                       "Some Western Sounding Title", idx)
    assert decision.dest_key == "manga"
    assert decision.rule_name == "existing series"
    assert decision.series_dir is not None


def test_series_absent_from_index_falls_through_to_rules():
    cfg = _series_cfg()
    idx = _index_from({"manga": ["Something Else"]})
    decision = resolve(cfg, build_context("", "x", ), "Brand New Series", idx)
    assert decision.dest_key == "graphic_novels"


def test_series_in_two_libraries_is_ambiguous_and_defers_to_rules():
    cfg = _series_cfg()
    idx = _index_from({"manga": ["Split Series"], "graphic_novels": ["Split Series"]})
    decision = resolve(cfg, build_context("Asura Scans (EN)", "x"),
                       "Split Series", idx)
    assert decision.dest_key == "manga"          # from the rule, not the index
    assert decision.rule_name == "Asian origin"
    assert decision.ambiguous_series


def test_alias_resolves_to_the_canonical_series_folder():
    cfg = _series_cfg()
    idx = _index_from({"manga": ["Rent-a-Girlfriend"]})
    decision = resolve(cfg, build_context("", "x"), "Kanojo, Okarishimasu", idx)
    assert decision.dest_key == "manga"
    assert decision.canonical_series == "Rent-a-Girlfriend"
    assert decision.series_dir == Path("X:/manga/Rent-a-Girlfriend")


def test_alias_works_before_the_series_exists_anywhere():
    cfg = _series_cfg()
    decision = resolve(cfg, build_context("", "x"), "Kanojo Okarishimasu",
                       _index_from({}))
    assert decision.canonical_series == "Rent-a-Girlfriend"


def test_override_can_pin_a_destination_outright():
    cfg = _series_cfg()
    idx = _index_from({"graphic_novels": ["Berserk of Gluttony"]})
    decision = resolve(cfg, build_context("Indie", "x"),
                       "Berserk of Gluttony", idx)
    # Pinned dest outranks even an existing folder elsewhere.
    assert decision.dest_key == "manga"
    assert decision.rule_name == "series override"


def test_index_is_disabled_by_default():
    cfg = parse(json.loads(json.dumps(V2)))
    assert not cfg.series_index_enabled
    assert len(SeriesIndex.build(cfg)) == 0


def test_index_build_skips_filesystem_when_disabled(tmp_path: Path):
    raw = json.loads(json.dumps(V2_SERIES))
    raw["series_index"]["enabled"] = False
    cfg = parse(raw)
    called = []

    def lister(root):
        called.append(root)
        return []

    SeriesIndex.build(cfg, lister=lister)
    assert not called


def test_index_build_only_reads_configured_destinations():
    cfg = _series_cfg()
    seen = []

    def lister(root):
        seen.append(str(root))
        return []

    SeriesIndex.build(cfg, lister=lister)
    assert any("Manga" in s for s in seen)
    assert not any("Comix" in s for s in seen)


def test_duplicate_alias_across_overrides_raises():
    raw = json.loads(json.dumps(V2_SERIES))
    raw["series_overrides"].append(
        {"canonical": "Something Else", "aliases": ["Kanojo Okarishimasu"]}
    )
    with pytest.raises(RoutingConfigError, match="claimed by both"):
        parse(raw)


def test_override_with_unknown_destination_raises():
    raw = json.loads(json.dumps(V2_SERIES))
    raw["series_overrides"][1]["dest"] = "nowhere"
    with pytest.raises(RoutingConfigError, match="not defined"):
        parse(raw)


def test_override_that_does_nothing_raises():
    raw = json.loads(json.dumps(V2_SERIES))
    raw["series_overrides"].append({"canonical": "Inert", "aliases": []})
    with pytest.raises(RoutingConfigError, match="does nothing"):
        parse(raw)


def test_series_index_unknown_destination_raises():
    raw = json.loads(json.dumps(V2_SERIES))
    raw["series_index"]["destinations"] = ["nowhere"]
    with pytest.raises(RoutingConfigError, match="not defined"):
        parse(raw)


def test_series_config_round_trips_through_v2_dict():
    cfg = _series_cfg()
    again = parse(json.loads(json.dumps(to_v2_dict(cfg))))
    decision = resolve(again, build_context("", "x"), "Kanojo Okarishimasu",
                       _index_from({}))
    assert decision.canonical_series == "Rent-a-Girlfriend"
    assert again.series_index_enabled


def test_explain_shows_whether_index_or_rule_decided():
    cfg = _series_cfg()
    idx = _index_from({"manga": ["Rent-a-Girlfriend"]})
    lines = explain(cfg, build_context("", "x"), "Kanojo, Okarishimasu", idx)
    assert any("override" in line for line in lines)
    assert any("MATCH series index" in line for line in lines)
