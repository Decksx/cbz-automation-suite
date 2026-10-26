"""Real fixture plans and watcher arrivals agree at the naming boundary."""

import hashlib
import json
import os
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

    def book(name, series="Series", number="1", year=None, folder=None, volume=None,
             page=b"fixture page", title=None):
        path = root / (folder or series) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        xml = f"<ComicInfo><Series>{series}</Series><Number>{number}</Number>"
        if title is not None:
            xml += f"<Title>{title}</Title>"
        if year:
            xml += f"<Year>{year}</Year>"
        if volume:
            xml += f"<Volume>{volume}</Volume>"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("page.png", page)
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


@pytest.mark.parametrize("kind", ["unknown_style"])
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


def test_parent_and_registered_subfolder_choose_the_same_authoritative_folder(library, tmp_path):
    root, db, book = library
    source = book("old #1.cbz", series="Tales for a HalloweeNight", folder="Tales for a Halloween Night")
    path = config(tmp_path, [{"path": str(root), "style": "issue"}])
    for scope in (root, source.parent):
        doc = cp.build_plan(scope, database=db, quarantine_date="2026-10-26", number_style_registry=path)
        assert not doc["review"] and len(doc["renames"]) == 1
        assert Path(doc["renames"][0]["destination"]).name == "Tales for a Halloween Night #001.cbz"


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


@pytest.fixture
def configured_watcher(library, tmp_path, monkeypatch):
    """Real normalization, XML rewriting and moves with media naming enabled."""
    root, _, _ = library
    dest = tmp_path / "destination"
    dest.mkdir()
    registry = config(tmp_path, [{"path": str(dest), "style": "chapter"}])
    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(root))
    monkeypatch.setattr(watcher, "_routing_rules", [])
    monkeypatch.setattr(watcher, "_routing_default", str(dest))
    monkeypatch.setattr(watcher, "_series_resolvers", {})
    monkeypatch.setattr(watcher, "_router", None)
    monkeypatch.setattr(watcher, "AI_DECENSOR_ENABLED", False)
    monkeypatch.setattr(watcher, "_note_move", lambda *a: None)
    monkeypatch.setattr(watcher, "wait_for_file_stable", lambda p: True)
    monkeypatch.setattr(watcher, "_number_style_registry", NumberStyleRegistry.load(registry))
    monkeypatch.setattr(watcher, "_number_style_requested", "auto")
    return dest


@pytest.mark.parametrize("series,name,style,expected", [
    ("Naruto", "Naruto 700.cbz", "chapter", "Naruto 700.cbz"),
    ("Naruto", "Naruto - 700 [Scans].cbz", "chapter", "Naruto - 700.cbz"),
    ("Saga", "Saga 054 (2018) (Digital) (Zone-Empire).cbz", "issue", "Saga 054 (2018).cbz"),
])
def test_configured_real_watcher_never_discards_an_unconsumed_bare_number(
        library, configured_watcher, monkeypatch, series, name, style, expected):
    _, _, book = library
    source = book(name, series=series, number="", title=Path(name).stem)
    monkeypatch.setattr(watcher, "_number_style_requested", style)
    watcher.process_and_move_directory(source.parent)
    landed = configured_watcher / series / expected
    assert landed.is_file() and not source.exists()
    with zipfile.ZipFile(landed) as archive:
        assert archive.read("page.png") == b"fixture page"


@pytest.mark.parametrize("separate_batches", [False, True])
def test_two_bare_number_arrivals_keep_distinct_names_and_both_land(
        library, configured_watcher, separate_batches):
    _, _, book = library
    first = book("Naruto 700.cbz", series="Naruto", number="", page=b"chapter700")
    if separate_batches:
        watcher.process_and_move_directory(first.parent)
    second = book("Naruto 701.cbz", series="Naruto", number="", page=b"chapter701")
    watcher.process_and_move_directory(second.parent)
    assert {p.name for p in (configured_watcher / "Naruto").glob("*.cbz")} == {
        "Naruto 700.cbz", "Naruto 701.cbz"}
    assert not first.exists() and not second.exists()
    for number in (700, 701):
        with zipfile.ZipFile(configured_watcher / "Naruto" / f"Naruto {number}.cbz") as archive:
            assert archive.read("page.png") == f"chapter{number}".encode()


@pytest.mark.skipif(os.name != "nt", reason="Windows case-insensitive rename witness")
@pytest.mark.parametrize("name,style,expected", [
    ("Series Ch. 3.cbz", "chapter", "Series ch. 3.cbz"),
    ("series #003.cbz", "issue", "Series #003.cbz"),
])
def test_configured_real_watcher_lands_case_only_names_through_two_steps(
        library, configured_watcher, monkeypatch, name, style, expected):
    _, _, book = library
    source = book(name, number="3")
    assert source.with_name(expected).samefile(source)
    monkeypatch.setattr(watcher, "_number_style_requested", style)
    original = Path.rename
    steps = []

    def observe(path, target):
        if path.name.startswith(".media-case-") or Path(target).name.startswith(".media-case-"):
            steps.append((path, Path(target)))
        return original(path, target)

    monkeypatch.setattr(Path, "rename", observe)
    watcher.process_and_move_directory(source.parent)
    landed = configured_watcher / "Series" / expected
    assert landed.is_file() and landed.name in [p.name for p in landed.parent.iterdir()]
    assert len(steps) == 2 and steps[0][1] == steps[1][0]
    assert not list(configured_watcher.parent.rglob(".media-case-*"))


@pytest.mark.parametrize("series,folder", [("Tales for a HalloweeNight", "Tales for a Halloween Night"),
    ("Batman", "Batman (2016)")])
@pytest.mark.parametrize("style", ["issue", "chapter"])
def test_explicit_style_never_erases_series_root_display_conflict(library, style, series, folder):
    _, db, book = library
    source = book("old #1.cbz", series=series, folder=folder)
    doc = cp.build_plan(source.parent, database=db, quarantine_date="2026-10-26", number_style=style)
    assert doc["review"] and not doc["renames"] and not doc["families"]


def test_parent_plan_refuses_a_nested_registered_style_before_reading_archives(library, tmp_path, monkeypatch):
    root, db, book = library
    book("One Piece #3.cbz", series="One Piece", folder="Manga/One Piece", number="3")
    path = config(tmp_path, [{"path": str(root), "style": "issue"},
                             {"path": str(root / "Manga"), "style": "chapter"}])
    monkeypatch.setattr(cp.sv, "derive_archive", lambda *a, **kw: pytest.fail("nested scope was not refused before archive reads"))
    with pytest.raises(cp.PlanningRefused, match="nested"):
        cp.build_plan(root, database=db, quarantine_date="2026-10-26", number_style_registry=path)


def test_empty_registry_runs_real_legacy_watcher_import_without_stalling(library, configured_watcher, tmp_path, monkeypatch):
    _, _, book = library
    source = book("Series Ch. 3.cbz", number="3")
    monkeypatch.setattr(watcher, "_number_style_registry", NumberStyleRegistry.load(config(tmp_path, [])))
    monkeypatch.setattr(watcher, "_apply_media_naming", lambda *a: pytest.fail("empty registry enabled media naming"))
    monkeypatch.setattr(watcher._media_fr, "archive_facts_from_file", lambda *a: pytest.fail("legacy pass read media facts"))
    watcher.process_and_move_directory(source.parent)
    assert not source.exists() and len(list(configured_watcher.rglob("*.cbz"))) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows no-replace rollback protocol")
def test_media_refusal_tracks_the_retained_archive_after_case_rollback_collision(
        library, configured_watcher, monkeypatch):
    _, _, book = library
    source = book("Series Ch. 3.cbz", number="3")
    facts = {source: fr.archive_facts_from_file(source)}
    values = {source: parse_comic_name(source, rules=ALL_RULES - {"translate"}, display_tokens=True)}
    original = Path.rename

    def collide(path, destination):
        if path.name.startswith(".media-case-") and Path(destination).name == "Series ch. 3.cbz":
            source.write_bytes(b"interloper")
            raise PermissionError("final step blocked")
        return original(path, destination)

    monkeypatch.setattr(Path, "rename", collide)
    refused, _ = watcher._apply_media_naming(values, facts, "Series", configured_watcher)
    assert refused == set(values) and len(refused) == 1
    retained = next(iter(refused))
    assert retained != source and retained.name.startswith(".media-case-")
    with zipfile.ZipFile(retained) as archive:
        assert archive.read("page.png") == b"fixture page"
    assert source.read_bytes() == b"interloper"


@pytest.mark.parametrize("series,folder,year,expected", [
    ("English Alias", "Kaguya-sama", None, "Kaguya-sama #003.cbz"),
    ("Tales for a HalloweeNight", "Tales for a Halloween Night", None, "Tales for a Halloween Night #003.cbz"),
    ("Batman", "Batman (2016)", None, "Batman (2016) #003.cbz"),
    ("Batman", "Batman (2016)", "2016", "Batman (2016) #003.cbz"),
    ("Batman", "Batman (2016)", "2020", "Batman (2016) #003.cbz"),
])
def test_real_planner_and_watcher_honor_selected_folder_and_reboot_identity(
        library, configured_watcher, tmp_path, monkeypatch, series, folder, year, expected):
    root, db, book = library
    source = book("old #3.cbz", series=series, folder=folder, number="3", year=year)
    path = config(tmp_path, [{"path": str(root), "style": "issue"},
                             {"path": str(configured_watcher), "style": "issue"}])
    monkeypatch.setattr(watcher, "_number_style_registry", NumberStyleRegistry.load(path))
    doc = cp.build_plan(root, database=db, quarantine_date="2026-10-26", number_style_registry=path)
    assert not doc["review"], doc["review"]
    assert Path(doc["renames"][0]["destination"]).name == expected
    watcher.process_and_move_directory(source.parent)
    landed = configured_watcher / folder / expected
    assert landed.is_file()
    written = fr.archive_facts_from_file(landed).comicinfo
    assert written.fields["Series"] == folder
    if year is not None:
        assert written.fields["Year"] == year
    values = {landed: parse_comic_name(landed, rules=ALL_RULES - {"translate"}, display_tokens=True)}
    refused, _ = watcher._apply_media_naming(values, {landed: fr.archive_facts_from_file(landed)}, folder, configured_watcher)
    assert not refused and list(values) == [landed]


@pytest.mark.parametrize("filename", ["Series Full.cbz", "Series (Full).cbz", "Full.cbz"])
def test_unnumbered_full_default_is_bound_to_source_and_survives_real_watcher_rewrite(
        library, configured_watcher, tmp_path, monkeypatch, filename):
    root, db, book = library
    source = book(filename, number="")
    path = config(tmp_path, [{"path": str(root), "style": "issue"},
                             {"path": str(configured_watcher), "style": "issue"}])
    monkeypatch.setattr(watcher, "_number_style_registry", NumberStyleRegistry.load(path))
    doc = cp.build_plan(root, database=db, quarantine_date="2026-10-26", number_style_registry=path)
    assert not doc["review"], doc["review"]
    assert Path(doc["renames"][0]["destination"]).name == "Series v01 (TPB).cbz"
    assert doc["renames"][0]["evidence"]["fields"]["volume"]["source"] == fr.SOURCE_POLICY
    watcher.process_and_move_directory(source.parent)
    assert (configured_watcher / "Series/Series v01 (TPB).cbz").is_file()


def test_unregistered_explicit_series_root_requires_and_records_its_scope_assertion(library):
    _, db, book = library
    source = book("old #3.cbz", series="Batman", folder="Batman (2016)", number="3")
    doc = cp.build_plan(source.parent, database=db, quarantine_date="2026-10-26",
                        number_style="issue", series_folder_root=True)
    assert not doc["review"] and doc["provenance"]["series_folder_root"] is True
    assert Path(doc["renames"][0]["destination"]).name == "Batman (2016) #003.cbz"


def test_reboots_in_two_folders_produce_distinct_plan_units(library):
    root, db, book = library
    for year in (2016, 2026):
        book("old #3.cbz", series="Batman", folder=f"Batman ({year})", number="3")
    doc = cp.build_plan(root, database=db, quarantine_date="2026-10-26", number_style="issue")
    assert not doc["review"] and len(doc["renames"]) == 2
    assert {Path(r["destination"]).name for r in doc["renames"]} == {
        "Batman (2016) #003.cbz", "Batman (2026) #003.cbz"}


def test_registered_flat_root_never_becomes_series_authority(library, tmp_path):
    root, db, book = library
    book("old #3.cbz", series="", folder=".", number="3")
    path = config(tmp_path, [{"path": str(root), "style": "issue"}])
    doc = cp.build_plan(root, database=db, quarantine_date="2026-10-26", number_style_registry=path)
    assert doc["review"] and not doc["renames"]
