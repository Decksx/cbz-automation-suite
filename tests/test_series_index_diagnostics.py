"""#60: a clean ambiguity gate must account for every observed directory.

All paths are synthetic. Collisions, non-series input and empty keys have
different causes, so these tests isolate them instead of testing one count.
"""

from dataclasses import FrozenInstanceError, replace
import json
import os
from pathlib import Path
import threading

import pytest

from scripts import cbz_routing as routing
from scripts.cbz_watcher_router import MODE_SHADOW, WatcherRouter


def _cfg(tmp_path, *, enabled=True):
    return routing.parse({
        "version": 2,
        "destinations": {"comix": str(tmp_path / "Comix"),
                         "manga": str(tmp_path / "Manga")},
        "default": "manga", "rules": [],
        "series_index": {"enabled": enabled,
                         "destinations": ["comix", "manga"]},
    })


def _built(tmp_path):
    """A real, complete empty census, rather than an unscanned manual index."""
    cfg = _cfg(tmp_path)
    for root in cfg.destinations.values():
        Path(root).mkdir()
    return cfg, routing.SeriesIndex.build(cfg)


def test_same_destination_keeps_both_paths_and_reports_the_collision(tmp_path):
    cfg, index = _built(tmp_path)
    paths = [tmp_path / "Comix" / name
             for name in ("Mr. Superconductor", "Mr. Superconductor - UNCENSORED")]
    for path in paths:
        index.add(path.name, "comix", path)
    assert index.is_ambiguous("Mr. Superconductor")
    assert {entry.path for entry in index.locations("Mr. Superconductor")} == set(paths)
    report = index.diagnostics()
    assert len(report.collisions) == 1
    assert report.collisions[0].key == routing.series_key(paths[0].name)
    assert {entry.path for entry in report.collisions[0].entries} == set(paths)
    decision = routing.resolve(cfg, {}, series_name=paths[0].name, index=index)
    assert decision.dest_key == "comix" and decision.ambiguous_series
    assert "identity unresolved" in decision.reason
    assert "multiple paths" in "\n".join(routing.explain(cfg, {}, paths[0].name, index))


@pytest.mark.parametrize("order", [("manga", "comix"), ("comix", "manga")])
def test_cross_destination_keeps_priority_and_every_contributing_path(tmp_path, order):
    cfg, index = _built(tmp_path)
    paths = {dest: Path(cfg.destinations[dest]) / "Shared" for dest in order}
    for dest in order:
        index.add("Shared", dest, paths[dest])
    assert index.lookup("Shared") == ("comix", paths["comix"])
    assert index.is_ambiguous("Shared")
    assert {entry.path for entry in index.diagnostics().collisions[0].entries} == set(paths.values())
    assert routing.resolve(cfg, {}, series_name="Shared", index=index).dest_key == "comix"


def test_lower_priority_and_same_root_contributors_are_not_lost(tmp_path):
    cfg, index = _built(tmp_path)
    for dest, name in [("comix", "Title"), ("manga", "Title"),
                       ("manga", "Title - uncensored")]:
        index.add(name, dest, Path(cfg.destinations[dest]) / name)
    assert len(index.locations("Title")) == 3
    assert len(index.diagnostics().collisions[0].entries) == 3


def test_destination_priority_is_configured_not_alphabetical(tmp_path):
    index = routing.SeriesIndex(priority=("manga", "comix"))
    for dest in ("comix", "manga"):
        index.add("Title", dest, tmp_path / dest / "Title")
    assert index.lookup("Title") == ("manga", tmp_path / "manga" / "Title")
    assert index.locations("Title")[0].dest_key == "manga"


def test_repeated_path_and_lexical_aliases_are_one_observation(tmp_path):
    _cfg_value, index = _built(tmp_path)
    path = tmp_path / "Comix" / "Title"
    for observed in (path, path, path.parent / "unused" / ".." / path.name):
        index.add("Title", "comix", observed)
    assert len(index.locations("Title")) == 1
    assert not index.is_ambiguous("Title")
    index.diagnostics().require_clear()


@pytest.mark.skipif(os.name != "nt", reason="Windows lexical path comparison")
def test_windows_case_aliases_are_one_path(tmp_path):
    _cfg_value, index = _built(tmp_path)
    path = tmp_path / "Comix" / "Title"
    index.add("Title", "comix", path)
    index.add("Title", "comix", Path(str(path).upper()))
    assert len(index.locations("Title")) == 1
    index.diagnostics().require_clear()


@pytest.mark.parametrize("order", [("manga", "comix"), ("comix", "manga")])
def test_two_destination_labels_for_the_same_path_do_not_invent_a_collision(tmp_path, order):
    _cfg_value, index = _built(tmp_path)
    path = tmp_path / "same-root" / "Title"
    for dest in order:
        index.add("Title", dest, path)
    assert index.lookup("Title") == ("comix", path)
    assert not index.is_ambiguous("Title")


def test_aliased_roots_and_later_lower_priority_move_keep_configured_label(tmp_path):
    """load permits root aliases: both the scan and incremental updates must
    keep the configured winner, even when its observation was not the last."""
    root = tmp_path / "Shared"
    path = root / "Title"
    path.mkdir(parents=True)
    config = tmp_path / "routing.json"
    config.write_text(json.dumps({
        "version": 2, "destinations": {"comix": str(root), "manga": str(root)},
        "default": "manga", "rules": [],
        "series_index": {"enabled": True, "destinations": ["comix", "manga"]},
    }), encoding="utf-8")
    router = WatcherRouter.load(config, mode=MODE_SHADOW)
    assert router.index.lookup("Title") == ("comix", path)
    assert len(router.index.locations("Title")) == 1

    # dest_key_for_path currently chooses the later label for aliased roots,
    # so this exercises the actual lower-priority update, not a direct add.
    assert router.dest_key_for_path(str(root)) == "manga"
    router.note_move("Title", str(root), path)
    assert router.index.lookup("Title") == ("comix", path)
    router.index_diagnostics().require_clear()


def test_canonical_unicode_names_do_not_collapse_distinct_path_spellings(tmp_path):
    _cfg_value, index = _built(tmp_path)
    names = ["Caf\u00e9", "Cafe\u0301"]
    for name in names:
        index.add(name, "comix", tmp_path / "Comix" / name)
    assert index.is_ambiguous(names[0])
    assert len(index.locations(names[0])) == 2


@pytest.mark.parametrize("name", [".stfolder", ".stversions", ".STFOLDER"])
def test_known_syncthing_directories_are_excluded_and_recorded(tmp_path, name):
    cfg = _cfg(tmp_path)
    for root in cfg.destinations.values():
        (Path(root) / name).mkdir(parents=True)
    index = routing.SeriesIndex.build(cfg)
    assert index.lookup(name) is None
    assert not index.is_ambiguous(name)
    assert len(index) == 0
    report = index.diagnostics()
    assert len(report.excluded) == 2
    assert {entry.path.name for entry in report.excluded} == {name}
    assert not report.collisions and not report.empty_keys
    report.require_clear()


@pytest.mark.parametrize("name", [".hack", "50a22a0e-0428-4daf-a058-cfcce32c358c", "Ordinary"])
def test_other_dot_titles_and_uuid_names_remain_real_candidates(tmp_path, name):
    cfg = _cfg(tmp_path)
    for root in cfg.destinations.values():
        (Path(root) / name).mkdir(parents=True)
    index = routing.SeriesIndex.build(cfg)
    assert index.is_ambiguous(name)
    report = index.diagnostics()
    assert not report.excluded and len(report.collisions) == 1


@pytest.mark.parametrize("name", ["Uncensored", "Decensored", "!!!"])
def test_empty_keys_block_the_gate_without_being_mislabelled_collisions(tmp_path, name):
    cfg, index = _built(tmp_path)
    path = Path(cfg.destinations["comix"]) / name
    index.add(name, "comix", path)
    assert index.lookup(name) is None
    assert not index.is_ambiguous(name), "an omitted key is not a collision"
    report = index.diagnostics()
    assert not report.collisions
    assert report.empty_keys == (routing.SeriesIndexEntry("comix", path),)
    assert report.scan_problems == ()
    with pytest.raises(routing.RoutingConfigError, match="1 empty-key"):
        report.require_clear()


def test_empty_key_directories_are_reported_by_build_not_only_incremental_add(tmp_path):
    cfg = _cfg(tmp_path)
    for root in cfg.destinations.values():
        (Path(root) / "Uncensored").mkdir(parents=True)
    index = routing.SeriesIndex.build(cfg)
    assert len(index.diagnostics().empty_keys) == 2
    assert len(index) == 0


def test_repeated_empty_key_path_is_reported_once(tmp_path):
    _cfg_value, index = _built(tmp_path)
    for _ in range(3):
        index.add("Uncensored", "comix", tmp_path / "Comix" / "Uncensored")
    assert len(index.diagnostics().empty_keys) == 1


def test_a_disabled_scan_cannot_certify_zero_ambiguity(tmp_path):
    def forbidden(_root):
        pytest.fail("disabled index enumerated a root")
    report = routing.SeriesIndex.build(_cfg(tmp_path, enabled=False), lister=forbidden).diagnostics()
    assert not report.clear and report.scan_problems
    with pytest.raises(routing.RoutingConfigError, match="scan problem"):
        report.require_clear()


def test_a_manual_index_cannot_masquerade_as_a_complete_scan():
    report = routing.SeriesIndex().diagnostics()
    assert not report.clear
    with pytest.raises(routing.RoutingConfigError):
        report.require_clear()


def test_missing_root_is_visible_even_when_no_key_is_ambiguous(tmp_path):
    cfg = _cfg(tmp_path)
    Path(cfg.destinations["comix"]).mkdir()
    index = routing.SeriesIndex.build(cfg)
    assert len(index) == 0
    report = index.diagnostics()
    assert len(report.scan_problems) == 1
    assert cfg.destinations["manga"] in report.scan_problems[0]
    with pytest.raises(routing.RoutingConfigError):
        report.require_clear()


def test_hand_built_config_with_undefined_destination_cannot_pass_the_gate(tmp_path):
    """parse/load reject this config, but direct RoutingConfig construction is
    possible: build's defensive branch must not silently certify the scan."""
    cfg, _index = _built(tmp_path)
    invalid = replace(cfg, series_index_destinations=("comix", "missing"))
    report = routing.SeriesIndex.build(invalid).diagnostics()
    assert report.scan_problems == ("no configured root for missing",)
    with pytest.raises(routing.RoutingConfigError, match="1 scan problem"):
        report.require_clear()


def test_root_disappearing_at_enumeration_is_not_a_successful_empty_scan(tmp_path, monkeypatch):
    cfg, _index = _built(tmp_path)
    real_lister = routing._default_lister

    def disappearing(root):
        root.rmdir()
        return real_lister(root)

    monkeypatch.setattr(routing, "_default_lister", disappearing)
    with pytest.raises(FileNotFoundError):
        routing.SeriesIndex.build(cfg)


def test_lister_failure_cannot_produce_a_clean_report(tmp_path):
    def unreadable(_root):
        raise PermissionError("unreadable root")
    with pytest.raises(PermissionError, match="unreadable"):
        routing.SeriesIndex.build(_cfg(tmp_path), lister=unreadable)


def test_gate_uses_full_diagnostics_even_if_ambiguity_lookup_lies(tmp_path, monkeypatch):
    _cfg_value, index = _built(tmp_path)
    for name in ("Title", "Title - uncensored"):
        index.add(name, "comix", tmp_path / "Comix" / name)
    monkeypatch.setattr(index, "is_ambiguous", lambda _name: False)
    report = index.diagnostics()
    assert not report.clear
    with pytest.raises(routing.RoutingConfigError, match="1 colliding key"):
        report.require_clear()


def test_complete_unambiguous_scan_preserves_routing_and_passes_gate(tmp_path):
    cfg = _cfg(tmp_path)
    for root in cfg.destinations.values():
        Path(root).mkdir()
    path = Path(cfg.destinations["comix"]) / "Title"
    path.mkdir()
    (Path(cfg.destinations["comix"]) / "not-a-series.txt").write_text("fixture")
    index = routing.SeriesIndex.build(cfg)
    report = index.diagnostics()
    assert report.clear
    report.require_clear()
    assert index.lookup("Title") == ("comix", path)
    decision = routing.resolve(cfg, {}, series_name="Title", index=index)
    assert decision.dest_key == "comix" and not decision.ambiguous_series
    assert decision.series_dir == path
    assert routing.resolve(cfg, {}, series_name="Absent", index=index).dest_key == cfg.default_key


def test_snapshot_is_immutable_and_does_not_change_after_incremental_updates(tmp_path):
    _cfg_value, index = _built(tmp_path)
    index.add("Title", "comix", tmp_path / "Comix" / "Title")
    before = index.diagnostics()
    index.add("Title", "manga", tmp_path / "Manga" / "Title")
    after = index.diagnostics()
    assert before.clear and not after.clear
    with pytest.raises(FrozenInstanceError):
        after.collisions = ()
    with pytest.raises(FrozenInstanceError):
        after.collisions[0].entries[0].dest_key = "wrong"
    exported = after.to_dict()
    exported["collisions"].clear()
    assert len(after.collisions) == 1, "export must not alias the frozen report"


def test_diagnostics_and_collision_selection_are_independent_of_add_order(tmp_path):
    observations = [("comix", "Title"), ("comix", "Title - uncensored"),
                    ("manga", "Title"), ("manga", "Uncensored"),
                    ("comix", ".stfolder")]
    indexes = []
    for values in (observations, list(reversed(observations))):
        index = routing.SeriesIndex.build(_cfg(tmp_path), lister=lambda _root: [])
        for dest, name in values:
            index.add(name, dest, tmp_path / dest / name)
        indexes.append(index)
    assert indexes[0].lookup("Title") == indexes[1].lookup("Title")
    assert indexes[0].diagnostics() == indexes[1].diagnostics()
    assert json.dumps(indexes[0].diagnostics().to_dict()) == json.dumps(indexes[1].diagnostics().to_dict())
    assert len(indexes[0]) == 1, "len remains the count of routable canonical keys"


def test_watcher_incremental_moves_retain_collision_and_deduplicate_repeats(tmp_path):
    cfg, index = _built(tmp_path)
    router = WatcherRouter(cfg, index, mode=MODE_SHADOW)
    for name in ("Title", "Title - uncensored", "Title"):
        router.note_move(name, cfg.destinations["comix"],
                         Path(cfg.destinations["comix"]) / name)
    report = router.index_diagnostics()
    assert len(report.collisions) == 1 and len(report.collisions[0].entries) == 2
    with pytest.raises(routing.RoutingConfigError):
        report.require_clear()


def test_json_report_preserves_each_kind_and_all_paths(tmp_path):
    cfg, index = _built(tmp_path)
    for name in ("Title", "Title - uncensored", "Uncensored", ".stfolder"):
        index.add(name, "comix", tmp_path / "Comix" / name)
    exported = json.loads(json.dumps(index.diagnostics().to_dict()))
    assert exported == {
        "collisions": [{"key": routing.series_key("Title"), "entries": [
            {"dest_key": "comix", "path": str(tmp_path / "Comix" / name)}
            for name in ("Title", "Title - uncensored")]}],
        "empty_keys": [{"dest_key": "comix", "path": str(tmp_path / "Comix" / "Uncensored")}],
        "excluded": [{"dest_key": "comix", "path": str(tmp_path / "Comix" / ".stfolder")}],
        "scan_problems": [], "clear": False,
    }


def test_watcher_load_reports_issues_without_enabling_moves(tmp_path, caplog):
    raw = {
        "version": 2, "destinations": {"comix": str(tmp_path / "Comix")},
        "default": "comix", "rules": [],
        "series_index": {"enabled": True, "destinations": ["comix"]},
    }
    config = tmp_path / "routing.json"
    config.write_text(json.dumps(raw), encoding="utf-8")
    root = tmp_path / "Comix"
    for name in ("Title", "Title - uncensored", "Uncensored", ".stfolder"):
        (root / name).mkdir(parents=True)
    router = WatcherRouter.load(config, mode=MODE_SHADOW)
    report = router.index_diagnostics()
    assert len(report.collisions) == len(report.empty_keys) == len(report.excluded) == 1
    assert router.mode == MODE_SHADOW
    assert "not an authority approval" in caplog.text
    assert "Uncensored" in caplog.text and "Title - uncensored" in caplog.text


def test_watcher_snapshot_excludes_a_concurrent_update(tmp_path, monkeypatch):
    """Exercise the same lock as note_move, not a private lock used by nobody."""
    cfg, index = _built(tmp_path)
    router = WatcherRouter(cfg, index, mode=MODE_SHADOW)
    inside, release, added = threading.Event(), threading.Event(), threading.Event()
    real_diagnostics, real_add = index.diagnostics, index.add
    errors = []

    def blocking_snapshot():
        inside.set()
        assert release.wait(5)
        return real_diagnostics()

    def observed_add(*args):
        added.set()
        return real_add(*args)

    def capture(operation):
        try:
            operation()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(index, "diagnostics", blocking_snapshot)
    monkeypatch.setattr(index, "add", observed_add)
    reader = threading.Thread(target=lambda: capture(router.index_diagnostics))
    writer = threading.Thread(target=lambda: capture(lambda: router.note_move(
        "Title", cfg.destinations["comix"], tmp_path / "Comix" / "Title")))
    reader.start()
    try:
        assert inside.wait(5)
        writer.start()
        assert not added.wait(0.2), "writer entered during a diagnostic snapshot"
    finally:
        release.set()
        reader.join(5)
        if writer.ident is not None:
            writer.join(5)
    assert not reader.is_alive() and not writer.is_alive()
    assert errors == [] and added.is_set()
