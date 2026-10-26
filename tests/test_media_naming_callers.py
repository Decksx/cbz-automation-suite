"""Real fixture plans and watcher arrivals agree at the naming boundary."""

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from comic_automation.archive import field_resolution as fr
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.library import canonical_plan as cp
from comic_automation.library.number_style import NumberStyleRegistry
from scripts import cbz_watcher as watcher
from scripts.cbz_core import ALL_RULES, parse_comic_name


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    db = tmp_path / "catalog.db"
    with database_connection(db) as connection:
        apply_migrations(connection, Path(cp.__file__).parents[1] / "database/migrations")

    def book(name, series="Series", number="1", year=None, folder=None, volume=None):
        path = root / (folder or series) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        xml = f"<ComicInfo><Series>{series}</Series><Number>{number}</Number>"
        if year:
            xml += f"<Year>{year}</Year>"
        if volume:
            xml += f"<Volume>{volume}</Volume>"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("page.png", b"fixture page")
            archive.writestr("ComicInfo.xml", (xml + "</ComicInfo>").encode())
        with database_connection(db) as connection:
            aid = connection.execute("INSERT INTO archive_files(file_size) VALUES (?)", (path.stat().st_size,)).lastrowid
            connection.execute("INSERT INTO file_locations(archive_id,path,file_size,modified_time_ns) VALUES (?,?,?,?)",
                               (aid, str(path), path.stat().st_size, path.stat().st_mtime_ns))
        return path
    return root, db, book


def config(tmp_path, rows):
    path = tmp_path / "number-styles.json"
    path.write_text(json.dumps({"version": 1, "roots": rows}), encoding="utf-8")
    return path


@pytest.mark.parametrize("name,number,style,expected", [
    ("Series #3.cbz", "3", "issue", "Series #003 (2019).cbz"),
    ("Series Ch. 3.cbz", "3", "chapter", "Series ch. 3 (2019).cbz"),
    ("Series TPB 1.cbz", "1", "issue", "Series v01 (2019) (TPB).cbz"),
    ("Series Vol. 1 (Colored).cbz", "1", "issue", "Series v01 (2019) (TPB) (Colored).cbz"),
    ("Series Full 1.cbz", "1", "issue", "Series v01 (2019) (TPB).cbz"),
    ("Series Vol. 1.cbz", "1", "chapter", "Series v01 (2019).cbz"),
])
def test_planner_and_real_watcher_rename_have_identical_media_names(library, tmp_path, monkeypatch, name, number, style, expected):
    root, db, book = library
    source = book(name, number=number, year="2019",
                  volume="1" if name == "Series Vol. 1.cbz" and style == "chapter" else None)
    original = source.read_bytes()
    registry = config(tmp_path, [{"path": str(root), "style": style}])
    report = cp.build_plan(root, database=db, quarantine_date="2026-10-26", number_style_registry=registry)
    assert not report["review"], report["review"]
    assert Path(report["renames"][0]["destination"]).name == expected
    assert report["provenance"]["number_style"]["registry_sha256"] == hashlib.sha256(registry.read_bytes()).hexdigest()
    parsed = parse_comic_name(source, rules=ALL_RULES - {"translate"}, display_tokens=True)
    facts = {source: fr.archive_facts_from_file(source)}
    values = {source: parsed}
    monkeypatch.setattr(watcher, "_number_style_registry", NumberStyleRegistry.load(registry))
    monkeypatch.setattr(watcher, "_number_style_requested", "auto")
    monkeypatch.setattr(watcher, "process_comicinfo", lambda *a: None)
    refused, selection = watcher._apply_media_naming(values, facts, "Series", root)
    assert not refused and selection.style == style
    (landed,) = values
    assert landed.name == expected and landed.read_bytes() == original


@pytest.mark.parametrize("series,folder,expected", [("ICECREAMMAN", "Ice Cream Man", "Ice Cream Man #003.cbz"),
    ("68", "'68", "'68 #003.cbz"), ("Series Name", "Series! Name", "Series! Name #003.cbz")])
def test_planner_preserves_normalized_equal_folder_display(library, series, folder, expected):
    root, db, book = library
    book("old #3.cbz", series=series, folder=folder, number="3")
    doc = cp.build_plan(root, database=db, quarantine_date="2026-10-26", number_style="issue")
    assert not doc["review"] and Path(doc["renames"][0]["destination"]).name == expected


@pytest.mark.parametrize("kind", ["Full", "typo", "unknown_style"])
def test_uncertainty_produces_no_rename_or_quarantine(library, kind):
    root, db, book = library
    source = book("Series Full.cbz" if kind == "Full" else "old #1.cbz",
                  series="Tales for a HalloweeNight" if kind == "typo" else "Series",
                  folder="Tales for a Halloween Night" if kind == "typo" else "Series")
    original = source.read_bytes()
    if kind == "unknown_style":
        with pytest.raises(cp.PlanningRefused, match="exactly one"):
            cp.build_plan(root, database=db, quarantine_date="2026-10-26")
    else:
        doc = cp.build_plan(root, database=db, quarantine_date="2026-10-26", number_style="issue")
        assert doc["review"] and not doc["renames"] and not doc["families"]
    assert source.read_bytes() == original


def test_config_content_and_explicit_style_are_sealed_even_when_names_match(library, tmp_path):
    root, db, book = library
    book("old #3.cbz", number="3")
    path = config(tmp_path, [{"path": str(root), "style": "issue"}])
    first = cp.build_plan(root, database=db, quarantine_date="2026-10-26", number_style_registry=path)
    path.write_bytes(path.read_bytes() + b"\n")
    second = cp.build_plan(root, database=db, quarantine_date="2026-10-26", number_style_registry=path)
    explicit = cp.build_plan(root, database=db, quarantine_date="2026-10-26", number_style_registry=path, number_style="issue")
    assert first["renames"] == second["renames"] == explicit["renames"]
    assert len({doc["plan_digest"] for doc in (first, second, explicit)}) == 3


def test_a_series_subfolder_scan_keeps_the_same_display_and_style_as_the_whole_root(library, tmp_path):
    root, db, book = library
    source = book("old #3.cbz", series="ICECREAMMAN", folder="Ice Cream Man", number="3")
    path = config(tmp_path, [{"path": str(root), "style": "issue"}])
    whole = cp.build_plan(root, database=db, quarantine_date="2026-10-26", number_style_registry=path)
    part = cp.build_plan(source.parent, database=db, quarantine_date="2026-10-26", number_style_registry=path)
    assert not whole["review"] and not part["review"]
    assert part["renames"] == whole["renames"]


def test_a_subfolder_scan_cannot_bypass_a_substantive_folder_conflict(library, tmp_path):
    root, db, book = library
    source = book("old #1.cbz", series="Tales for a HalloweeNight", folder="Tales for a Halloween Night")
    path = config(tmp_path, [{"path": str(root), "style": "issue"}])
    for scope in (root, source.parent):
        doc = cp.build_plan(scope, database=db, quarantine_date="2026-10-26", number_style_registry=path)
        assert doc["review"] and not doc["renames"] and not doc["families"]


def test_wired_watcher_uses_final_destination_after_category_override(library, tmp_path, monkeypatch):
    source_root, db, book = library
    source = book("Series #3.cbz", number="3")
    arrival = source.parent
    comix, manga = tmp_path / "Comix", tmp_path / "Manga"
    comix.mkdir()
    manga.mkdir()
    path = config(tmp_path, [{"path": str(comix), "style": "issue"}, {"path": str(manga), "style": "chapter"}])
    monkeypatch.setattr(watcher, "_number_style_registry", NumberStyleRegistry.load(path))
    monkeypatch.setattr(watcher, "_number_style_requested", "auto")
    monkeypatch.setattr(watcher, "_series_resolvers", {})
    monkeypatch.setattr(watcher, "_router", None)
    monkeypatch.setattr(watcher, "_resolve_route", lambda p: ("comix", str(comix)))
    calls = []

    def target(category, name, legacy):
        calls.append(name)
        return "Series", str(comix if len(calls) == 1 else manga), {}, None

    monkeypatch.setattr(watcher, "_resolve_import_target", target)
    monkeypatch.setattr(watcher, "process_comicinfo", lambda *a: None)
    monkeypatch.setattr(watcher, "process_cbz_file", lambda p, **kw: (p,
        parse_comic_name(p, rules=ALL_RULES - {"translate"}, display_tokens=True)))
    monkeypatch.setattr(watcher, "_note_move", lambda *a: None)
    original = source.read_bytes()
    watcher.process_and_move_directory(arrival)
    landed = manga / "Series" / "Series ch. 3.cbz"
    assert len(calls) >= 3 and landed.read_bytes() == original
    assert not source.exists() and not list(comix.rglob("*.cbz"))


def test_failed_cli_session_does_not_leave_media_policy_in_later_embedded_calls(tmp_path, monkeypatch):
    registry = config(tmp_path, [])
    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(tmp_path / "incoming"))
    monkeypatch.setattr(watcher, "_number_style_registry", None)
    monkeypatch.setattr(watcher, "_number_style_requested", "auto")

    def fail_tracker(**kwargs):
        assert watcher._number_style_registry is not None
        raise RuntimeError("startup failed after policy initialization")

    monkeypatch.setattr(watcher, "DirectorySettleTracker", fail_tracker)
    with pytest.raises(RuntimeError, match="startup failed"):
        watcher.main(["--number-style-registry", str(registry), "--number-style", "issue"])
    assert watcher._number_style_registry is None
    assert watcher._number_style_requested == "auto"


def test_real_watcher_processing_preserves_tpb_year_and_its_rescan_fixpoint(library, tmp_path, monkeypatch):
    root, db, book = library
    source = book("Series TPB 1.cbz", year="2019")
    dest = tmp_path / "Comix"
    dest.mkdir()
    registry = config(tmp_path, [{"path": str(dest), "style": "issue"}])
    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(root))
    monkeypatch.setattr(watcher, "_routing_rules", [])
    monkeypatch.setattr(watcher, "_routing_default", str(dest))
    monkeypatch.setattr(watcher, "_series_resolvers", {})
    monkeypatch.setattr(watcher, "_router", None)
    monkeypatch.setattr(watcher, "_note_move", lambda *a: None)
    monkeypatch.setattr(watcher, "wait_for_file_stable", lambda p: True)
    monkeypatch.setattr(watcher, "_number_style_registry", NumberStyleRegistry.load(registry))
    monkeypatch.setattr(watcher, "_number_style_requested", "auto")
    watcher.process_and_move_directory(source.parent)
    landed = dest / "Series" / "Series v01 (2019) (TPB).cbz"
    assert landed.is_file() and not source.exists()
    # Re-read the actual XML written by the watcher, not the original fixture.
    fact = fr.archive_facts_from_file(landed)
    values = {landed: parse_comic_name(landed, rules=ALL_RULES - {"translate"}, display_tokens=True)}
    refused, _ = watcher._apply_media_naming(values, {landed: fact}, "Series", dest)
    assert not refused and list(values) == [landed]
