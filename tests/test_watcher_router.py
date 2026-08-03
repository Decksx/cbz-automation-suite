"""Tests for the watcher-facing v2 router.

This object exists so the watcher can be observed classifying with v2 before
v2 is allowed to move anything. The tests that matter most are therefore the
ones proving it cannot influence a move, and the two invariants that would be
silent if broken:

* a review destination must never be indexed, or a restart would promote a
  staged unresolved case into an authoritative index hit;
* a completed move must update the in-memory index, or a series first seen
  mid-session would not be sticky until the watcher restarted.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from scripts.cbz_routing import RoutingConfigError, SeriesIndex, parse
from scripts.cbz_watcher_router import (
    MODE_OFF,
    MODE_SHADOW,
    WatcherRouter,
    summarise,
)


def _cbz(path: Path, **fields: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"<{k}>{v}</{k}>" for k, v in fields.items())
    with zipfile.ZipFile(path, "w") as zf:
        if body:
            zf.writestr("ComicInfo.xml", f"<ComicInfo>{body}</ComicInfo>")
        zf.writestr("001.jpg", b"page")


def _raw(tmp_path: Path, **overrides) -> dict:
    raw = {
        "version": 2,
        "destinations": {
            "comix": str(tmp_path / "Comix"),
            "manga": str(tmp_path / "Manga"),
            "graphic_novels": str(tmp_path / "GN"),
            "review": str(tmp_path / "_classification_review"),
        },
        "default": "graphic_novels",
        "lists": {"asian_languages": ["ja"]},
        "signals": {
            "strong_origin": {"any": [{"field": "comicinfo.LanguageISO",
                                       "in_list": "asian_languages"}]},
            "weak_origin": {"any": [{"field": "comicinfo.Genre",
                                     "contains_any": ["seinen"]}]},
        },
        "rules": [
            {"name": "origin (strong)", "when": "strong_origin",
             "dest": "manga", "strength": "strong"},
            {"name": "origin (weak)", "when": "weak_origin",
             "dest": "manga", "strength": "weak"},
        ],
        "series_index": {"enabled": True,
                         "destinations": ["comix", "manga", "graphic_novels"]},
    }
    raw.update(overrides)
    return raw


def _router(tmp_path: Path, *, mode=MODE_SHADOW, index=None, **overrides):
    cfg = parse(_raw(tmp_path, **overrides))
    return WatcherRouter(cfg, index or SeriesIndex(), mode=mode)


# ── the review destination must never be indexed ─────────────────


@pytest.mark.parametrize("destinations", [
    ["comix", "review"],        # explicit
    [],                         # implicit: empty means index every destination
])
def test_an_indexed_review_destination_refuses_to_start(tmp_path, destinations):
    with pytest.raises(RoutingConfigError, match="series_index destination"):
        _router(tmp_path,
                unresolved={"destination": "review"},
                series_index={"enabled": True, "destinations": destinations})


def test_an_absent_destinations_list_also_counts_as_indexing_review():
    # "destinations" omitted means index everything, review included.
    tmp = Path.cwd()
    raw = _raw(tmp, unresolved={"destination": "review"},
               series_index={"enabled": True})
    with pytest.raises(RoutingConfigError, match="series_index destination"):
        WatcherRouter(parse(raw), SeriesIndex())


def test_review_excluded_from_the_index_is_accepted(tmp_path):
    router = _router(tmp_path, unresolved={"destination": "review"})
    assert router.cfg.unresolved_destination == "review"


def test_a_disabled_index_cannot_capture_review(tmp_path):
    router = _router(tmp_path, unresolved={"destination": "review"},
                     series_index={"enabled": False,
                                   "destinations": ["comix", "review"]})
    assert router.cfg.series_index_enabled is False


def test_no_unresolved_destination_means_nothing_to_check(tmp_path):
    assert _router(tmp_path).cfg.unresolved_destination is None


# ── classification matches the reclassifier's contract ───────────


def test_a_strong_signal_decides_the_series(tmp_path):
    series = tmp_path / "src" / "Berserk"
    _cbz(series / "ch00.cbz")
    _cbz(series / "ch01.cbz", LanguageISO="ja")
    decision = _router(tmp_path).classify(series, "Atsumaru")
    assert decision.dest_key == "manga"
    assert decision.evidence_strength == "strong"


def test_a_later_weak_signal_beats_an_earlier_no_match(tmp_path):
    # The same ranking the reclassifier uses, via the shared contract.
    series = tmp_path / "src" / "Gannibal"
    _cbz(series / "ch00.cbz")
    _cbz(series / "ch01.cbz", Genre="seinen")
    decision = _router(tmp_path).classify(series, "src")
    assert decision.dest_key == "manga"
    assert decision.evidence_strength == "weak"


def test_nothing_classified_reports_unresolved(tmp_path):
    series = tmp_path / "src" / "Saga"
    _cbz(series / "ch00.cbz")
    decision = _router(tmp_path).classify(series, "src")
    assert decision.confidence == "unresolved"
    assert decision.dest_key == "graphic_novels"


def test_an_empty_directory_still_produces_a_decision(tmp_path):
    series = tmp_path / "src" / "Empty"
    series.mkdir(parents=True)
    decision = _router(tmp_path).classify(series, "src")
    assert decision.confidence == "unresolved"


def test_an_existing_series_outranks_metadata(tmp_path):
    index = SeriesIndex(priority=("comix", "manga", "graphic_novels"))
    index.add("Some Adult Manga", "comix", tmp_path / "Comix" / "Some Adult Manga")
    series = tmp_path / "src" / "Some Adult Manga"
    _cbz(series / "ch00.cbz", LanguageISO="ja")
    decision = _router(tmp_path, index=index).classify(series, "src")
    assert decision.dest_key == "comix"
    assert decision.authoritative is True


def test_classification_never_routes_to_the_review_destination(tmp_path):
    # This branch must not let v2 name a destination the watcher cannot yet
    # handle; route_unresolved=False keeps the compatibility default.
    series = tmp_path / "src" / "Saga"
    _cbz(series / "ch00.cbz")
    router = _router(tmp_path, unresolved={"destination": "review"})
    decision = router.classify(series, "src")
    assert decision.dest_key == "graphic_novels"
    assert decision.confidence == "unresolved"


# ── shadow mode influences nothing ───────────────────────────────


def test_shadow_mode_off_classifies_nothing(tmp_path):
    series = tmp_path / "src" / "Berserk"
    _cbz(series / "ch00.cbz", LanguageISO="ja")
    router = _router(tmp_path, mode=MODE_OFF)
    assert router.enabled is False
    assert router.shadow(series, "src", str(tmp_path / "GN")) is None


def test_shadow_records_a_disagreement(tmp_path):
    series = tmp_path / "src" / "Berserk"
    _cbz(series / "ch00.cbz", LanguageISO="ja")
    comparison = _router(tmp_path).shadow(series, "src", str(tmp_path / "GN"))
    assert comparison.agrees is False
    assert comparison.decision.dest_key == "manga"
    assert "DIFFER" in comparison.describe()


def test_the_router_keeps_no_comparison_state(tmp_path):
    # This runs in a daemon. A per-directory record kept for the process
    # lifetime is an unbounded list and a cross-pass tally nobody asked for.
    router = _router(tmp_path)
    series = tmp_path / "src" / "Berserk"
    _cbz(series / "ch00.cbz", LanguageISO="ja")
    for _ in range(5):
        router.shadow(series, "src", str(tmp_path / "GN"))
    assert not any(isinstance(v, list) and v
                   for v in vars(router).values())


def test_shadow_records_agreement_by_path_not_by_key(tmp_path):
    # v1 yields a path and v2 a key; the path is the only shared vocabulary.
    series = tmp_path / "src" / "Berserk"
    _cbz(series / "ch00.cbz", LanguageISO="ja")
    comparison = _router(tmp_path).shadow(series, "src", str(tmp_path / "Manga"))
    assert comparison.agrees is True
    assert "agree" in comparison.describe()


def test_an_empty_legacy_destination_never_counts_as_agreement(tmp_path):
    series = tmp_path / "src" / "Berserk"
    _cbz(series / "ch00.cbz", LanguageISO="ja")
    assert _router(tmp_path).shadow(series, "src", "").agrees is False


def test_the_summary_counts_differences_and_unresolved(tmp_path):
    router = _router(tmp_path)
    results = []
    for name, fields in [("Berserk", {"LanguageISO": "ja"}), ("Saga", {})]:
        series = tmp_path / "src" / name
        _cbz(series / "ch00.cbz", **fields)
        results.append(router.shadow(series, "src", str(tmp_path / "GN")))
    summary = summarise(results)
    assert "2 classified" in summary
    assert "1 would differ" in summary
    assert "1 unresolved" in summary


def test_the_summary_is_safe_with_nothing_classified():
    assert "no directories" in summarise([])


def test_two_passes_summarise_independently(tmp_path):
    # Results from the first pass must not appear in the second.
    router = _router(tmp_path)
    first, second = [], []
    for name, fields, bucket in [("Berserk", {"LanguageISO": "ja"}, first),
                                 ("Gannibal", {"LanguageISO": "ja"}, first),
                                 ("Saga", {}, second)]:
        series = tmp_path / "src" / name
        _cbz(series / "ch00.cbz", **fields)
        bucket.append(router.shadow(series, "src", str(tmp_path / "GN")))

    assert "2 classified" in summarise(first)
    assert "2 would differ" in summarise(first)
    assert "1 classified" in summarise(second)
    assert "0 would differ" in summarise(second)
    assert "1 unresolved" in summarise(second)


# ── index upkeep ─────────────────────────────────────────────────


def test_a_completed_move_makes_the_series_sticky(tmp_path):
    # Without this a second chapter arriving later in the same session would
    # be classified from metadata again and could land somewhere else.
    router = _router(tmp_path)
    assert router.index.lookup("New Series") is None

    landed = tmp_path / "Manga" / "New Series"
    router.note_move("New Series", str(tmp_path / "Manga"), landed)
    assert router.index.lookup("New Series") == ("manga", landed)

    series = tmp_path / "src" / "New Series"
    _cbz(series / "ch01.cbz")                 # no metadata at all
    decision = router.classify(series, "src")
    assert decision.dest_key == "manga"
    assert decision.authoritative is True


def test_the_indexed_path_is_the_directory_the_files_landed_in(tmp_path):
    # The mover merges into a pre-existing folder when one matches, and that
    # folder's name -- not a re-derived guess -- is what a lookup must find.
    router = _router(tmp_path)
    landed = tmp_path / "Manga" / "Berserk (Deluxe)"
    router.note_move("Berserk", str(tmp_path / "Manga"), landed)
    assert router.index.lookup("Berserk")[1] == landed


def test_a_move_to_an_unknown_destination_is_not_indexed(tmp_path):
    router = _router(tmp_path)
    router.note_move("Whatever", str(tmp_path / "Nowhere"), tmp_path / "x")
    assert router.index.lookup("Whatever") is None


def test_a_disabled_index_is_never_populated(tmp_path):
    # resolve() consults whatever index it is handed without re-checking the
    # flag, so populating a disabled index would manufacture authority the
    # configuration explicitly withheld.
    router = _router(tmp_path, series_index={"enabled": False,
                                             "destinations": ["manga"]})
    router.note_move("New Series", str(tmp_path / "Manga"),
                     tmp_path / "Manga" / "New Series")
    assert router.index.lookup("New Series") is None
    assert len(router.index) == 0


def test_the_destination_path_maps_back_to_its_key(tmp_path):
    router = _router(tmp_path)
    assert router.dest_key_for_path(str(tmp_path / "Manga")) == "manga"
    assert router.dest_key_for_path(str(tmp_path / "Nowhere")) is None


# ── the classified identity and archive scope ────────────────────


def test_a_chapter_folder_resolves_the_existing_series_authoritatively(tmp_path):
    # "Berserk Ch. 4" is what arrives; "Berserk" is what the watcher files it
    # under. Classifying the arrival name misses the index hit entirely.
    index = SeriesIndex(priority=("comix", "manga", "graphic_novels"))
    index.add("Berserk", "manga", tmp_path / "Manga" / "Berserk")
    router = _router(tmp_path, index=index)

    arrival = tmp_path / "src" / "Berserk Ch. 4"
    _cbz(arrival / "ch04.cbz")

    by_arrival_name = router.classify(arrival, "src")
    assert by_arrival_name.authoritative is False

    by_resolved_name = router.classify(arrival, "src", series_name="Berserk")
    assert by_resolved_name.dest_key == "manga"
    assert by_resolved_name.authoritative is True


def test_loose_files_are_not_classified_from_a_nested_series(tmp_path):
    # A mixed drop point: loose archives beside a nested series directory.
    # Walking the parent would classify the loose files from the nested
    # series' metadata.
    drop = tmp_path / "src" / "SomeSource"
    loose = drop / "loose01.cbz"
    _cbz(loose)                                     # no metadata
    _cbz(drop / "Nested Manga" / "ch00.cbz", LanguageISO="ja")

    router = _router(tmp_path)
    contaminated = router.classify(drop, "SomeSource")
    assert contaminated.dest_key == "manga"         # swept in the nested file

    scoped = router.classify(drop, "SomeSource", archives=[loose])
    assert scoped.dest_key == "graphic_novels"
    assert scoped.confidence == "unresolved"


def test_shadow_reads_the_archive_paths_it_is_given(tmp_path):
    # Post-processing paths, including files the watcher renamed: a stale
    # pre-rename path no longer exists and would read no metadata at all.
    series = tmp_path / "src" / "Berserk"
    stale = series / "original.cbz"
    _cbz(stale, LanguageISO="ja")
    renamed = series / "Berserk Ch. 1.cbz"
    stale.rename(renamed)

    comparison = _router(tmp_path).shadow(
        series, "src", str(tmp_path / "GN"), archives=[renamed])
    assert comparison.decision.dest_key == "manga"
    assert comparison.decision.evidence_strength == "strong"


# ── configuration surface ────────────────────────────────────────


def test_an_invalid_mode_is_rejected(tmp_path):
    with pytest.raises(RoutingConfigError, match="routing mode"):
        _router(tmp_path, mode="active")


def test_load_builds_the_index_once_from_disk(tmp_path):
    path = tmp_path / "routing.json"
    path.write_text(json.dumps(_raw(tmp_path)), encoding="utf-8")
    calls: list[Path] = []

    def lister(root: Path):
        calls.append(root)
        return []

    router = WatcherRouter.load(path, mode=MODE_SHADOW, lister=lister)
    assert router.mode == MODE_SHADOW
    # One enumeration per indexed destination, at startup, and no more.
    assert len(calls) == 3


def test_load_rejects_an_indexed_review_destination(tmp_path):
    path = tmp_path / "routing.json"
    path.write_text(json.dumps(_raw(
        tmp_path,
        unresolved={"destination": "review"},
        series_index={"enabled": True, "destinations": ["review"]},
    )), encoding="utf-8")
    with pytest.raises(RoutingConfigError, match="series_index destination"):
        WatcherRouter.load(path, lister=lambda p: [])


def test_the_staged_config_satisfies_the_review_invariant():
    # Gate on the real file: it must stay loadable by the watcher adapter.
    root = Path(__file__).resolve().parents[1]
    router = WatcherRouter.load(root / "config" / "routing.v2.json",
                                lister=lambda p: [])
    assert router.mode == MODE_OFF
    assert router.enabled is False
