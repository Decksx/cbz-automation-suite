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


# ── comma-joined fields ──────────────────────────────────────────
#
# Publisher and Imprint hold lists, e.g. "Gangan Wing,Yen Press". Applying
# an anchored pattern to the whole value can only match when the publisher
# of interest happens to be listed first, so those two fields tokenise.
# Web does not: it holds one URL, and a URL may legitimately contain a
# comma.

TOKENS = {
    "version": 2,
    "destinations": {"manga": "X:\\Manga", "graphic_novels": "X:\\Graphic Novels"},
    "default": "graphic_novels",
    "lists": {
        "asian_publishers": ["yen press*", "line*", "takeshobo*"],
        "asian_sites": ["*mangadex*", "*a,b*"],
    },
    "signals": {
        "by_publisher": {
            "any": [{"field": "comicinfo.Publisher",
                     "glob_tokens_in_list": "asian_publishers"}]},
        "by_imprint": {
            "any": [{"field": "comicinfo.Imprint",
                     "glob_tokens_in_list": "asian_publishers"}]},
        "by_web": {
            "any": [{"field": "comicinfo.Web",
                     "glob_in_list": "asian_sites"}]},
        "by_publisher_untokenised": {
            "any": [{"field": "comicinfo.Publisher",
                     "glob_in_list": "asian_publishers"}]},
    },
    "rules": [],
}


def _matches(signal: str, **comicinfo) -> bool:
    raw = json.loads(json.dumps(TOKENS))
    raw["rules"] = [{"name": signal, "when": signal, "dest": "manga"}]
    cfg = parse(raw)
    return resolve(cfg, build_context("", "", comicinfo)).dest_key == "manga"


@pytest.mark.parametrize("value, expected", [
    ("Yen Press", True),
    ("Gangan Wing,Yen Press", True),
    ("Gangan Wing, Yen Press", True),
    ("Gangan Wing,, Yen Press", True),      # empty tokens are skipped
    ("Not Yen Pressed", False),             # the glob is still anchored
    ("LINE", True),
    ("Deadline Comics", False),             # "line*" must not match mid-token
])
def test_publisher_tokens_match_per_token(value, expected):
    assert _matches("by_publisher", Publisher=value) is expected


@pytest.mark.parametrize("value, expected", [
    ("Yen Press", True),
    ("Gangan Wing,Yen Press", True),
    ("Deadline Comics", False),
])
def test_imprint_tokenises_the_same_way(value, expected):
    # Imprint carries no data in the measured library, but it is the same
    # kind of field and must not behave differently from Publisher.
    assert _matches("by_imprint", Imprint=value) is expected


@pytest.mark.parametrize("value", ["", "   ", ",", "  ,  ,"])
def test_absent_and_empty_publisher_values_are_false(value):
    assert _matches("by_publisher", Publisher=value) is False


def test_missing_publisher_field_is_false_not_an_error():
    assert _matches("by_publisher") is False


def test_glob_in_list_still_matches_against_the_whole_value():
    # The behaviour deliberately left alone: a comma-joined value is still
    # matched whole, so an anchored pattern still fails on it. Changing this
    # is what the new operator exists to avoid having to do.
    assert _matches("by_publisher_untokenised",
                    Publisher="Gangan Wing,Yen Press") is False
    assert _matches("by_publisher_untokenised", Publisher="Yen Press") is True


def test_web_domain_matching_is_not_tokenised():
    # A URL containing a comma must still be matched as one string. If Web
    # were tokenised, "*a,b*" could never match any token.
    assert _matches("by_web", Web="https://reader.example/a,b/1") is True
    assert _matches("by_web", Web="https://mangadex.org/chapter/1") is True


# ── evidence strength is structured, not inferred from names ─────


def _strength_cfg(strength=None, name="some rule"):
    raw = json.loads(json.dumps(V2))
    rule = {"name": name, "when": "asian_origin", "dest": "manga"}
    if strength is not None:
        rule["strength"] = strength
    raw["rules"] = [rule]
    return parse(raw)


def test_matched_decision_carries_the_declared_strength():
    cfg = _strength_cfg("weak")
    decision = resolve(cfg, build_context("", "", {"LanguageISO": "ja"}))
    assert decision.evidence_strength == "weak"
    assert decision.authoritative is False


def test_an_undeclared_rule_is_strong():
    # Every rule in every config here behaved as strong before strength was
    # explicit, so the default has to preserve that rather than demote them.
    decision = resolve(_strength_cfg(), build_context("", "", {"LanguageISO": "ja"}))
    assert decision.evidence_strength == "strong"


def test_a_rule_named_weak_is_not_weak_unless_declared():
    decision = resolve(_strength_cfg(name="weak-looking name"),
                       build_context("", "", {"LanguageISO": "ja"}))
    assert decision.evidence_strength == "strong"


def test_the_default_carries_no_evidence():
    decision = resolve(_strength_cfg("strong"), build_context("", "", {}))
    assert decision.evidence_strength == "none"
    assert decision.authoritative is False
    assert not decision.matched


def test_an_override_is_authoritative_and_carries_no_evidence():
    # An override outranks evidence however strong, so it must not be
    # expressible on the same scale as evidence.
    raw = json.loads(json.dumps(V2))
    raw["series_overrides"] = [{"canonical": "Ice Cream Man", "aliases": [],
                                "dest": "graphic_novels"}]
    decision = resolve(parse(raw), build_context("", "", {"LanguageISO": "ja"}),
                       series_name="Ice Cream Man")
    assert decision.dest_key == "graphic_novels"
    assert decision.authoritative is True
    assert decision.evidence_strength == "none"


def test_an_existing_series_hit_is_authoritative():
    cfg = parse(json.loads(json.dumps(V2)))
    index = SeriesIndex(priority=("comix", "manga", "graphic_novels"))
    index.add("Berserk", "comix", Path("X:/Comix/Berserk"))
    decision = resolve(cfg, build_context("", "", {}),
                       series_name="Berserk", index=index)
    assert decision.dest_key == "comix"
    assert decision.authoritative is True
    assert decision.evidence_strength == "none"


@pytest.mark.parametrize("bad", ["none", "STRONG", "medium", ""])
def test_an_invalid_rule_strength_is_fatal(bad):
    # "none" is rejected too: a rule that matched is evidence by definition.
    with pytest.raises(RoutingConfigError, match="strength"):
        _strength_cfg(bad)


def test_unknown_list_is_still_rejected_for_the_new_operator():
    raw = json.loads(json.dumps(TOKENS))
    raw["signals"]["by_publisher"]["any"][0]["glob_tokens_in_list"] = "nope"
    raw["rules"] = [{"name": "x", "when": "by_publisher", "dest": "manga"}]
    with pytest.raises(RoutingConfigError, match="unknown list"):
        parse(raw)


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


def _index_from(mapping: dict[str, list[str]],
                priority: tuple[str, ...] = ("comix", "manga", "graphic_novels")
                ) -> SeriesIndex:
    """Build an index without touching the filesystem."""
    idx = SeriesIndex(priority=priority)
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
                       series_name="Some Western Sounding Title", index=idx)
    assert decision.dest_key == "manga"
    assert decision.rule_name == "existing series"
    assert decision.series_dir is not None


def test_series_absent_from_index_falls_through_to_rules():
    cfg = _series_cfg()
    idx = _index_from({"manga": ["Something Else"]})
    decision = resolve(cfg, build_context("", "x", ),
                       series_name="Brand New Series", index=idx)
    assert decision.dest_key == "graphic_novels"


def test_series_in_two_libraries_resolves_by_destination_priority():
    # Comix membership is a deliberate adult determination, so it outranks
    # the other libraries when a series somehow exists in both. The split is
    # still reported so it can be cleaned up rather than silently papered
    # over.
    cfg = _series_cfg()
    idx = _index_from({"comix": ["Split Series"], "manga": ["Split Series"]})
    decision = resolve(cfg, build_context("Asura Scans (EN)", "x"),
                       series_name="Split Series", index=idx)
    assert decision.dest_key == "comix"
    assert decision.rule_name == "existing series"
    assert decision.ambiguous_series


def test_destination_priority_is_independent_of_discovery_order():
    # Whichever library is enumerated first, priority decides.
    cfg = _series_cfg()
    a = _index_from({"manga": ["Dup"], "comix": ["Dup"]})
    b = _index_from({"comix": ["Dup"], "manga": ["Dup"]})
    assert a.lookup("Dup")[0] == "comix"
    assert b.lookup("Dup")[0] == "comix"


def test_curated_comix_membership_outranks_asian_origin_metadata():
    # The correction that motivated priority ordering: a series a person put
    # in Comix stays in Comix even when its metadata says Asian origin.
    cfg = _series_cfg()
    idx = _index_from({"comix": ["Some Adult Manga"]})
    decision = resolve(cfg, build_context("Asura Scans (EN)", "x"),
                       series_name="Some Adult Manga", index=idx)
    assert decision.dest_key == "comix"
    assert decision.rule_name == "existing series"


def test_alias_resolves_to_the_canonical_series_folder():
    cfg = _series_cfg()
    idx = _index_from({"manga": ["Rent-a-Girlfriend"]})
    decision = resolve(cfg, build_context("", "x"),
                       series_name="Kanojo, Okarishimasu", index=idx)
    assert decision.dest_key == "manga"
    assert decision.canonical_series == "Rent-a-Girlfriend"
    assert decision.series_dir == Path("X:/manga/Rent-a-Girlfriend")


def test_alias_works_before_the_series_exists_anywhere():
    cfg = _series_cfg()
    decision = resolve(cfg, build_context("", "x"),
                       series_name="Kanojo Okarishimasu", index=_index_from({}))
    assert decision.canonical_series == "Rent-a-Girlfriend"


def test_override_can_pin_a_destination_outright():
    cfg = _series_cfg()
    idx = _index_from({"graphic_novels": ["Berserk of Gluttony"]})
    decision = resolve(cfg, build_context("Indie", "x"),
                       series_name="Berserk of Gluttony", index=idx)
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
    decision = resolve(again, build_context("", "x"),
                       series_name="Kanojo Okarishimasu", index=_index_from({}))
    assert decision.canonical_series == "Rent-a-Girlfriend"
    assert again.series_index_enabled


def test_explain_shows_whether_index_or_rule_decided():
    cfg = _series_cfg()
    idx = _index_from({"manga": ["Rent-a-Girlfriend"]})
    lines = explain(cfg, build_context("", "x"), "Kanojo, Okarishimasu", idx)
    assert any("override" in line for line in lines)
    assert any("MATCH series index" in line for line in lines)
