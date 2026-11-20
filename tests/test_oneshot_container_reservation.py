"""The shared container cannot become an ordinary series or repair target."""

import copy
import json
import zipfile
from pathlib import Path

import pytest

from comic_automation import series_resolver as sr
from comic_automation.library.exclusions import iter_files_excluding_directories
from scripts import cbz_watcher as watcher


def _registry():
    return {
        "version": 1, "series_count": 2, "alias_count": 2,
        "series": [
            {"canonical_title": "_oneshots", "relative_path": "_oneshots", "aliases": ["bad"]},
            {"canonical_title": "Real", "relative_path": "Real", "aliases": ["good"]},
        ],
        "aliases_by_normalized_key": {"bad": "_oneshots", "good": "Real"},
        "ambiguous_aliases": {"mixed": ["_oneshots", "Real"], "onlybad": ["_oneshots"]},
    }


@pytest.mark.parametrize("name", ["_oneshots", "_ONESHOTS", "_OneShots", " _oneshots. "])
def test_reserved_name_uses_safe_windows_name_comparison(name):
    assert sr.is_reserved_series_name(name)


@pytest.mark.parametrize("name", ["Oneshots", "_oneshots anthology", "_Special", "Real"])
def test_reservation_does_not_ban_other_titles_or_underscore_folders(name):
    assert not sr.is_reserved_series_name(name)


@pytest.mark.parametrize("container", ["_oneshots", "_ONESHOTS", "_OneShots"])
def test_rebuild_excludes_container_without_touching_its_books(tmp_path, container):
    root = tmp_path / "Comix"
    (root / container).mkdir(parents=True)
    book = root / container / "Other title.cbz"
    book.write_bytes(b"book-canary")
    (root / "Real").mkdir()
    (root / "_Special").mkdir()
    (root / sr.ALIAS_SEED_NAME).write_text(json.dumps({container: ["bogus alias"]}))
    registry = sr.build_registry(root)
    assert {row["canonical_title"] for row in registry["series"]} == {"Real", "_Special"}
    assert registry["series_count"] == 2
    assert "bogusalias" not in registry["aliases_by_normalized_key"]
    assert all(value != container for value in registry["aliases_by_normalized_key"].values())
    assert book.read_bytes() == b"book-canary"


def test_loading_legacy_container_cache_cleans_memory_without_rewriting(tmp_path):
    path = tmp_path / sr.REGISTRY_NAME
    path.write_text(json.dumps(_registry()))
    before = path.read_bytes()
    registry = sr.load_registry(tmp_path)
    assert registry["series"] == [_registry()["series"][1]]
    assert registry["series_count"] == 1
    assert registry["aliases_by_normalized_key"] == {"good": "Real"}
    assert registry["alias_count"] == 1
    assert registry["ambiguous_aliases"] == {"mixed": ["Real"]}
    assert path.read_bytes() == before


def test_live_registration_does_not_republish_legacy_container_entries(tmp_path):
    (tmp_path / sr.REGISTRY_NAME).write_text(json.dumps(_registry()))
    resolver = sr.SeriesResolver(tmp_path)
    resolver.register("Real", {"additional alias"})
    stored = json.loads((tmp_path / sr.REGISTRY_NAME).read_text())
    assert [row["canonical_title"] for row in stored["series"]] == ["Real"]
    assert stored["aliases_by_normalized_key"]["additionalalias"] == "Real"
    assert "_oneshots" not in stored["aliases_by_normalized_key"].values()


def test_container_books_remain_visible_to_generic_file_inventory(tmp_path):
    container = tmp_path / "_oneshots"
    container.mkdir()
    book = container / "Book.cbz"
    book.write_bytes(b"book")
    assert list(iter_files_excluding_directories(tmp_path)) == [book]


@pytest.mark.parametrize("version", [2, True, "1", None])
def test_future_registry_is_refused_without_dropping_durable_locations(tmp_path, version):
    registry = _registry()
    registry["version"] = version
    path = tmp_path / sr.REGISTRY_NAME
    path.write_text(json.dumps(registry))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="Unsupported series registry version"):
        sr.load_registry(tmp_path)
    with pytest.raises(ValueError, match="Unsupported series registry version"):
        sr.resolve_from_registry(registry, "good")
    assert path.read_bytes() == before


@pytest.mark.parametrize("name", ["_oneshots", "_ONESHOTS", " _oneshots. "])
def test_registration_refuses_container_without_any_metadata_write(tmp_path, name):
    resolver = sr.SeriesResolver(tmp_path)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    with pytest.raises(ValueError, match="cannot be registered"):
        resolver.register(name, {"some alias"})
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


@pytest.mark.parametrize("name", ["_oneshots", "_ONESHOTS", " _oneshots. "])
def test_reserved_query_never_suggests_a_new_series_directory(name):
    with pytest.raises(ValueError, match="not a series name"):
        sr.resolve_from_registry({"aliases_by_normalized_key": {}}, name)


@pytest.mark.parametrize("target", ["_oneshots", "_ONESHOTS", "_oneshots/book.cbz", "_oneshots\\book.cbz"])
def test_raw_legacy_exact_alias_cannot_return_container_target(target):
    registry = {"aliases_by_normalized_key": {"title": target}}
    with pytest.raises(ValueError, match="reserved oneshots"):
        sr.resolve_from_registry(registry, "Title")


def test_raw_legacy_ambiguity_retains_valid_candidates_without_mutating_input():
    registry = _registry()
    before = copy.deepcopy(registry)
    result = sr.resolve_from_registry(registry, "mixed")
    assert not result["matched"] and result["candidates"] == ["Real"]
    assert registry == before
    with pytest.raises(ValueError, match="reserved oneshots"):
        sr.resolve_from_registry(registry, "onlybad")


def test_legacy_candidate_objects_cannot_reintroduce_container_targets(tmp_path):
    registry = _registry()
    registry["ambiguous_aliases"]["objects"] = [
        {"relative_path": "_oneshots"}, {"relative_path": "Real"}]
    (tmp_path / sr.REGISTRY_NAME).write_text(json.dumps(registry))
    for candidate in (registry, sr.load_registry(tmp_path)):
        result = sr.resolve_from_registry(candidate, "objects")
        assert result["candidates"] == [{"relative_path": "Real"}]


def test_fuzzy_lookup_does_not_compete_with_a_reserved_container_target():
    registry = {"aliases_by_normalized_key": {
        sr.alias_key("The Long Book Title"): "Real",
        sr.alias_key("The Long Book Titlee"): "_oneshots",
    }}
    result = sr.resolve_from_registry(registry, "The Long Book Titles", threshold=0.8)
    assert result["matched"] and result["match_type"] == "fuzzy_alias"
    assert result["relative_path"] == "Real"


def test_only_fuzzy_container_candidate_cannot_be_returned_as_a_series():
    registry = {"aliases_by_normalized_key": {
        sr.alias_key("The Very Long Book Titles"): "_oneshots",
    }}
    result = sr.resolve_from_registry(registry, "The Very Long Book Title")
    assert not result["matched"] and result["match_type"] == "none"
    assert result["suggested_name"] == "The Very Long Book Title"


@pytest.mark.parametrize("container", ["_oneshots", "_ONESHOTS"])
def test_apostrophe_repair_leaves_container_book_paths_and_locations_unchanged(tmp_path, container):
    root = tmp_path / "Comix"
    protected = root / container
    protected.mkdir(parents=True)
    book = protected / "Reader’s Book.cbz"
    book.write_bytes(b"protected")
    locations = root / "_series_locations.json"
    locations.write_bytes(b"locations-canary")
    ordinary = root / "Ordinary’s Series"
    ordinary.mkdir()
    (ordinary / "Book.cbz").write_bytes(b"ordinary")
    result = sr.normalize_apostrophe_names(root, tmp_path / "quarantine", apply=True)
    assert book.read_bytes() == b"protected"
    assert locations.read_bytes() == b"locations-canary"
    assert (root / "Ordinary's Series" / "Book.cbz").read_bytes() == b"ordinary"
    assert all(container not in item["source"] for item in result["actions"])


@pytest.mark.parametrize("apply", [False, True])
def test_apostrophe_repair_cannot_be_pointed_directly_inside_container(tmp_path, apply):
    root = tmp_path / "Comix" / "_oneshots" / "nested"
    root.mkdir(parents=True)
    book = root / "Reader’s Book.cbz"
    book.write_bytes(b"protected")
    with pytest.raises(ValueError, match="state-aware relocation"):
        sr.normalize_apostrophe_names(root, tmp_path / "quarantine", apply=apply)
    assert book.read_bytes() == b"protected"


@pytest.mark.parametrize("resolver_mode", [False, True])
@pytest.mark.parametrize("name", ["_oneshots", "_ONESHOTS", " _oneshots. "])
def test_watcher_refuses_container_series_even_when_resolver_mode_is_off(tmp_path, monkeypatch, resolver_mode, name):
    resolvers = {"Comix": sr.SeriesResolver(tmp_path)} if resolver_mode else {}
    monkeypatch.setattr(watcher, "_series_resolvers", resolvers)
    with pytest.raises(ValueError, match="not an import series"):
        watcher._resolve_import_target("Comix", name, str(tmp_path))
    assert not (tmp_path / "_oneshots").exists()


@pytest.mark.parametrize("metadata_only", [False, True])
@pytest.mark.parametrize("defer_review", [False, True])
def test_reserved_arrival_is_a_durable_hold_while_sibling_lands_and_resume_is_safe(
    tmp_path, monkeypatch, metadata_only, defer_review,
):
    from scripts import cbz_core
    incoming = tmp_path / "incoming"
    library = tmp_path / "library"
    batch = incoming / "drop"
    bad_name = "Ordinary Bad" if metadata_only else "_oneshots"
    expected = {}
    for name in (bad_name, "Good"):
        archive = batch / name / f"{name}.cbz"
        archive.parent.mkdir(parents=True)
        with zipfile.ZipFile(archive, "w") as stream:
            stream.writestr("page1.jpg", name.encode())
        expected[name] = archive.read_bytes()
    library.mkdir()
    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(incoming))
    monkeypatch.setattr(watcher, "_routing_rules", [])
    monkeypatch.setattr(watcher, "_routing_default", str(library))
    monkeypatch.setattr(watcher, "_number_style_registry", None)
    monkeypatch.setattr(watcher, "_router", None)
    monkeypatch.setattr(watcher, "AI_DECENSOR_ENABLED", False)
    monkeypatch.setattr(watcher, "POLL_INTERVAL", 0)
    monkeypatch.setattr(watcher, "wait_for_file_stable", lambda *args, **kwargs: True)
    monkeypatch.setattr(cbz_core, "translate_metadata_text", lambda text: (text, None))
    monkeypatch.setattr(watcher, "_series_resolvers", {"comix": sr.SeriesResolver(library)} if metadata_only else {})
    if metadata_only:
        monkeypatch.setattr(watcher, "_consistent_comicinfo_series", lambda paths:
                            "_oneshots" if any(p.parent.name == bad_name for p in paths) else None)
    original_process = watcher.process_cbz_file
    processed = []

    def process(path, **kwargs):
        if not metadata_only:
            assert path.parent.name != bad_name, "Reserved arrival must be held before rewriting"
        processed.append(path.parent.name)
        return original_process(path, **kwargs)

    monkeypatch.setattr(watcher, "process_cbz_file", process)
    original_place = watcher._claims.place
    injected = []

    def place(path, root, series):
        if defer_review and root == watcher.REVIEW_ROOT and not injected:
            injected.append(path.name)
            raise OSError("fixture: review temporarily unavailable")
        return original_place(path, root, series)

    monkeypatch.setattr(watcher._claims, "place", place)
    watcher.process_and_move_directory(batch)
    assert list(library.rglob("*.cbz")) and "Good" in processed
    assert not (library / "_oneshots").exists()
    if defer_review:
        assert injected
        records = list(watcher.PROCESSING_ROOT.rglob(watcher._claims.RECORD_NAME))
        assert any(json.loads(p.read_text()).get("dispositions") for p in records)
    watcher._resume_claims()
    held = list(watcher.REVIEW_ROOT.rglob("*.cbz"))
    assert len(held) == 1
    if not metadata_only:
        assert held[0].read_bytes() == expected[bad_name]
    assert not list(watcher.PROCESSING_ROOT.rglob("*.cbz"))
    assert len(list(library.rglob("*.cbz"))) == 1
    with zipfile.ZipFile(held[0]) as stream:
        assert stream.read("page1.jpg") == bad_name.encode()
