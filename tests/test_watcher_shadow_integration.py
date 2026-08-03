"""The watcher's v2 shadow wiring, driven through real processing.

The router is unit-tested separately; what these pin down is the wiring, and
every one of them guards a defect that produces *misleading observations*
rather than an error:

* classifying before the series identity is resolved, so a per-chapter
  arrival misses the existing-series index hit it should have got;
* classifying by walking a mixed drop point, so loose files are decided by a
  nested series' metadata;
* never recording completed moves, so a series is not sticky until restart;
* recording a move that failed;
* accumulating comparisons across passes in a long-running daemon.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from scripts import cbz_watcher as watcher
from scripts.cbz_routing import SeriesIndex, parse
from scripts.cbz_watcher_router import MODE_SHADOW, WatcherRouter


def _cbz(path: Path, **fields: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"<{k}>{v}</{k}>" for k, v in fields.items())
    with zipfile.ZipFile(path, "w") as zf:
        if body:
            zf.writestr("ComicInfo.xml", f"<ComicInfo>{body}</ComicInfo>")
        zf.writestr("page1.jpg", b"\0" * 50)


def _wire(monkeypatch, tmp_path, *, index=None, index_enabled=True):
    """A watcher pointed at tmp dirs, with a shadow router attached."""
    watch_root, dest_root = tmp_path / "incoming", tmp_path / "library"
    manga_root = tmp_path / "Manga"
    for d in (watch_root, dest_root, manga_root):
        d.mkdir(exist_ok=True)

    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(watch_root))
    monkeypatch.setattr(watcher, "_routing_rules", [])
    monkeypatch.setattr(watcher, "_routing_default", str(dest_root))
    monkeypatch.setattr(watcher, "POLL_INTERVAL", 0)

    cfg = parse({
        "version": 2,
        "destinations": {"graphic_novels": str(dest_root),
                         "manga": str(manga_root)},
        "default": "graphic_novels",
        "lists": {"asian_languages": ["ja"]},
        "signals": {"origin": {"any": [{"field": "comicinfo.LanguageISO",
                                        "in_list": "asian_languages"}]}},
        "rules": [{"name": "origin", "when": "origin", "dest": "manga",
                   "strength": "strong"}],
        "series_index": {"enabled": index_enabled,
                         "destinations": ["manga", "graphic_novels"]},
    })
    router = WatcherRouter(cfg, index or SeriesIndex(
        priority=("manga", "graphic_novels")), mode=MODE_SHADOW)
    monkeypatch.setattr(watcher, "_router", router)
    return watch_root, dest_root, manga_root, router


def _shadow_lines(caplog):
    """Per-directory comparison lines only, never the per-pass summary."""
    return [r.getMessage() for r in caplog.records if "[shadow:" in r.getMessage()]


# ── the classified identity ──────────────────────────────────────


def test_a_chapter_arrival_is_classified_as_the_resolved_series(
        tmp_path, monkeypatch, caplog):
    # "Berserk Ch. 4" is filed under "Berserk". Classifying the arrival name
    # misses the index hit that should have decided it.
    index = SeriesIndex(priority=("manga", "graphic_novels"))
    index.add("Berserk", "manga", tmp_path / "Manga" / "Berserk")
    watch_root, _, _, _ = _wire(monkeypatch, tmp_path, index=index)

    arrival = watch_root / "Berserk Ch. 4"
    _cbz(arrival / "Berserk Ch. 4.cbz")            # no metadata of its own

    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(arrival)

    lines = _shadow_lines(caplog)
    assert lines, "no shadow comparison was logged"
    assert "'Berserk'" in lines[0]
    assert "authoritative=True" in lines[0]


def test_loose_files_are_not_classified_from_a_nested_series(
        tmp_path, monkeypatch, caplog):
    watch_root, _, _, _ = _wire(monkeypatch, tmp_path)
    drop = watch_root / "SomeSource"
    _cbz(drop / "Loose Title.cbz")                                # no metadata
    _cbz(drop / "Nested Series" / "Nested Series.cbz", LanguageISO="ja")

    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(drop)

    loose = [l for l in _shadow_lines(caplog) if "Nested" not in l]
    assert loose, "the loose files were never classified"
    assert "confidence=unresolved" in loose[0]
    assert "key=manga" not in loose[0]


# ── index upkeep after real moves ────────────────────────────────


def test_a_successful_move_becomes_an_authoritative_hit_in_the_same_process(
        tmp_path, monkeypatch, caplog):
    watch_root, dest_root, _, router = _wire(monkeypatch, tmp_path)

    first = watch_root / "New Series"
    _cbz(first / "New Series Ch. 1.cbz")
    watcher.process_and_move_directory(first)

    hit = router.index.lookup("New Series")
    assert hit is not None, "a completed move was never recorded"
    assert hit[0] == "graphic_novels"
    assert hit[1] == dest_root / "New Series"

    # A later chapter, still untagged, is now decided by the index.
    second = watch_root / "New Series Ch. 2"
    _cbz(second / "New Series Ch. 2.cbz")
    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(second)

    lines = _shadow_lines(caplog)
    assert lines and "authoritative=True" in lines[-1]


def test_a_failed_move_is_not_recorded(tmp_path, monkeypatch):
    _, _, _, router = _wire(monkeypatch, tmp_path)
    watch_root = Path(watcher.WATCH_FOLDER)
    series = watch_root / "Doomed Series"
    _cbz(series / "Doomed Series.cbz")

    monkeypatch.setattr(watcher, "_move_cbz_dir", lambda *a, **k: None)
    watcher.process_and_move_directory(series)

    assert router.index.lookup("Doomed Series") is None


def test_a_disabled_index_is_not_populated_by_moves(tmp_path, monkeypatch):
    _, _, _, router = _wire(monkeypatch, tmp_path, index_enabled=False)
    watch_root = Path(watcher.WATCH_FOLDER)
    series = watch_root / "New Series"
    _cbz(series / "New Series.cbz")

    watcher.process_and_move_directory(series)

    assert len(router.index) == 0


# ── per-pass summary, bounded ────────────────────────────────────


def test_each_pass_summarises_only_its_own_results(
        tmp_path, monkeypatch, caplog):
    watch_root, _, _, router = _wire(monkeypatch, tmp_path)
    for name in ("Alpha", "Beta"):
        _cbz(watch_root / "Batch One" / name / f"{name}.cbz")
    _cbz(watch_root / "Batch Two" / "Gamma" / "Gamma.cbz")

    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(watch_root / "Batch One")
    first = [m for m in (r.getMessage() for r in caplog.records)
             if "Routing v2 shadow:" in m]
    caplog.clear()

    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(watch_root / "Batch Two")
    second = [m for m in (r.getMessage() for r in caplog.records)
              if "Routing v2 shadow:" in m]

    assert first and "2 classified" in first[-1]
    assert second and "1 classified" in second[-1]


def test_the_router_accumulates_nothing_across_passes(tmp_path, monkeypatch):
    watch_root, _, _, router = _wire(monkeypatch, tmp_path)
    for n in range(3):
        series = watch_root / f"Series {n}"
        _cbz(series / f"Series {n}.cbz")
        watcher.process_and_move_directory(series)
    assert not any(isinstance(v, list) and v for v in vars(router).values())


# ── the mode banner never overstates ─────────────────────────────


def test_a_failed_v2_load_reports_off_not_the_requested_mode(
        tmp_path, monkeypatch, caplog):
    missing = tmp_path / "does-not-exist.json"
    monkeypatch.setattr(watcher, "ROUTING_V2_FILE", missing)
    monkeypatch.setattr(watcher, "ROUTING_V2_MODE", "shadow")
    monkeypatch.setattr(watcher, "_router", "sentinel")

    with caplog.at_level("ERROR"):
        watcher._load_routing_v2()

    assert watcher._router is None
    assert any("Routing v2 disabled" in r.getMessage() for r in caplog.records)


def test_shadow_failure_never_stops_processing(tmp_path, monkeypatch, caplog):
    watch_root, dest_root, _, router = _wire(monkeypatch, tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr(router, "shadow", boom)
    series = watch_root / "Resilient"
    _cbz(series / "Resilient.cbz")

    with caplog.at_level("ERROR"):
        watcher.process_and_move_directory(series)

    assert (dest_root / "Resilient" / "Resilient.cbz").exists()
    assert any("shadow failed" in r.getMessage() for r in caplog.records)
