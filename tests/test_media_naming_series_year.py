"""Reboot series years are kept, confirmed or refused; never silently dropped.

Covers the PR #142 review findings: a filename-only volume year, reboot
scene names inside a year-qualified folder, David's #3003 series-year rule
(including the ComicVine ``<Volume>YYYY`` convention) and the planner Full
fixpoint. Watcher witnesses run the real ``process_and_move_directory``
with a configured registry; planner witnesses apply and re-plan.
"""

import json
import zipfile
from pathlib import Path

import pytest

from comic_automation.archive import field_resolution as fr
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.library import canonical_plan as cp
from comic_automation.library.media_naming import (
    KeepArrivalName, comicvine_series_year, decide_series_year, media_fields, watcher_fields,
    with_comicinfo_year,
)
from comic_automation.library.number_style import NumberStyleRegistry
from scripts import cbz_watcher as watcher
from scripts.cbz_canonical_name import CanonicalNameError, format_canonical_name
from scripts.cbz_core import ALL_RULES, parse_comic_name


def archive(path, ci=None, page=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("page.png", page or path.name.encode())
        if ci is not None:
            zf.writestr("ComicInfo.xml", ("<ComicInfo>" + "".join(
                f"<{k}>{v}</{k}>" for k, v in ci.items()) + "</ComicInfo>").encode())
    return path


def comicinfo(path):
    return fr.archive_facts_from_file(path).comicinfo.fields


def registry(tmp_path, rows):
    path = tmp_path / "number-styles.json"
    path.write_text(json.dumps({"version": 1, "roots": rows}), encoding="utf-8")
    return path


# --- pure decisions ----------------------------------------------------------

@pytest.mark.parametrize("series,folder,filename,ci_series,ci_volume,expected", [
    # A chosen year-qualified folder decides; a matching filename year is the series year.
    ("Batman", "Batman (2016)", "Batman (2016) #003 (2019).cbz", None, None, (2016, 2016)),
    ("Batman", "Batman (2016)", "Batman #003 (2019).cbz", None, None, (2016, None)),
    # David #3003: filename series year confirmed by the same ComicInfo Series.
    ("Batman (2016)", "Batman", "Batman (2016) #003.cbz", "Batman (2016)", None, (2016, 2016)),
    # ComicVine: Volume=YYYY is the series start year, with or without a filename year.
    ("Batman", "Batman", "Batman (2016) #003.cbz", "Batman", "2016", (2016, 2016)),
    ("Batman", "Batman", "Batman #003.cbz", "Batman", "2016", (2016, None)),
    ("Batman", None, "Batman #003.cbz", None, "2016", (2016, None)),
    # Plain series and ordinary volumes stay as they were.
    ("Saga", "Saga", "Saga #054 (2018).cbz", "Saga", None, (None, None)),
    ("Saga", "Saga", "Saga Vol. 2.cbz", "Saga", "2", (None, None)),
    ("Batman", "Batman", "Batman #003 (2016).cbz", None, None, (None, None)),
    # A year on a different ComicInfo title (an alias) never decides the chosen folder's identity.
    ("Detective Comics (2016)", "Batman", "Batman #003.cbz", "Detective Comics (2016)", None, (None, None)),
    # With no numbering after it, a title-adjacent year is the issue year.
    ("Series Full", "Series Full", "Series Full (2019).cbz", None, None, (None, None)),
])
def test_series_year_decisions(series, folder, filename, ci_series, ci_volume, expected):
    assert decide_series_year(series=series, folder=folder, filename=filename,
                              ci_series=ci_series, ci_volume=ci_volume) == expected


@pytest.mark.parametrize("filename", ["Batman (2016) #003.cbz", "Batman (2016) v01.cbz",
                                      "Batman (2016) 003 (2019) (Digital).cbz"])
def test_an_unconfirmed_filename_series_year_keeps_the_arrival_name(filename):
    with pytest.raises(KeepArrivalName):
        decide_series_year(series="Batman", folder="Batman", filename=filename)


@pytest.mark.parametrize("folder,filename,ci_series,ci_volume", [
    ("Batman (2016)", "Batman (2011) #003.cbz", None, None),
    ("Batman (2016)", "Batman #003.cbz", None, "2011"),
    ("Batman (2016)", "Batman #003.cbz", "Batman (2011)", None),
    ("Batman", "Batman (2016) #003.cbz", "Batman (2011)", None),
    ("Batman", "Batman (2016) #003.cbz", None, "2011"),
])
def test_conflicting_series_years_refuse(folder, filename, ci_series, ci_volume):
    with pytest.raises(CanonicalNameError) as caught:
        decide_series_year(series="Batman", folder=folder, filename=filename,
                           ci_series=ci_series, ci_volume=ci_volume)
    assert not isinstance(caught.value, KeepArrivalName)


@pytest.mark.parametrize("value,expected", [("2016", 2016), ("1900", 1900), ("2100", 2100),
    ("1899", None), ("2101", None), ("1", None), ("16", None), (None, None), ("20166", None)])
def test_comicvine_year_volume_bounds(value, expected):
    assert comicvine_series_year(value) == expected


def test_comicinfo_year_is_added_kept_or_refused():
    added = with_comicinfo_year("<ComicInfo><Series>Batman (2016)</Series></ComicInfo>", "2019")
    assert "<Year>2019</Year>" in added
    assert with_comicinfo_year("<ComicInfo><Year>2019</Year></ComicInfo>", "2019") is None
    assert "<Year>2019</Year>" in with_comicinfo_year("<ComicInfo><Year /></ComicInfo>", "2019")
    with pytest.raises(CanonicalNameError):
        with_comicinfo_year("<ComicInfo><Year>2018</Year></ComicInfo>", "2019")


def test_a_parenthesised_year_the_format_does_not_consume_is_unconsumed(tmp_path):
    # The canonical grammar reads (2016) as another title's series year; the
    # chosen Batman folder would format it away, so the arrival name is kept.
    path = archive(tmp_path / "Other (2016) #003.cbz", {"Series": "Batman", "Number": "3"})
    parsed = parse_comic_name(path, rules=ALL_RULES - {"translate"}, display_tokens=True)
    with pytest.raises(KeepArrivalName):
        watcher_fields(parsed, fr.archive_facts_from_file(path), series="Batman", style="issue")


def test_a_comicvine_year_volume_does_not_conflict_with_a_filename_volume(tmp_path):
    path = archive(tmp_path / "Batman v01.cbz", {"Series": "Batman", "Volume": "2016"})
    parsed = parse_comic_name(path, rules=ALL_RULES - {"translate"}, display_tokens=True)
    fields = watcher_fields(parsed, fr.archive_facts_from_file(path), series="Batman", style="issue")
    assert format_canonical_name(fields) == "Batman (2016) v01 (TPB).cbz"


# --- real watcher imports ----------------------------------------------------

@pytest.fixture
def import_issue(tmp_path, monkeypatch):
    """Run real watcher imports into a registered issue-style destination."""
    intake = tmp_path / "intake"
    dest = tmp_path / "Comix"
    intake.mkdir()
    dest.mkdir()
    path = registry(tmp_path, [{"path": str(dest), "style": "issue"}])
    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(intake))
    monkeypatch.setattr(watcher, "_routing_rules", [])
    monkeypatch.setattr(watcher, "_routing_default", str(dest))
    monkeypatch.setattr(watcher, "_series_resolvers", {})
    monkeypatch.setattr(watcher, "_router", None)
    monkeypatch.setattr(watcher, "AI_DECENSOR_ENABLED", False)
    monkeypatch.setattr(watcher, "_note_move", lambda *a: None)
    monkeypatch.setattr(watcher, "wait_for_file_stable", lambda p: True)
    monkeypatch.setattr(watcher, "_number_style_registry", NumberStyleRegistry.load(path))
    monkeypatch.setattr(watcher, "_number_style_requested", "auto")

    def run(folder, files):
        arrival = intake / folder
        for name, ci in files:
            archive(arrival / name, ci)
        watcher.process_and_move_directory(arrival)
        landed = {str(p.relative_to(dest)).replace("\\", "/"): p for p in dest.rglob("*.cbz")}
        left = sorted(p.name for p in intake.rglob("*.cbz"))
        return landed, left
    return run


@pytest.mark.parametrize("folder,name,expected", [
    ("Batman", "Batman (2016) #003.cbz", "Batman/Batman (2016) #003.cbz"),
    ("1602", "1602 (2003) #001.cbz", "1602/1602 (2003) #001.cbz"),
    ("Batman", "Batman (2016) v01.cbz", "Batman/Batman (2016) v01.cbz"),
])
def test_watcher_keeps_a_filename_only_series_year(import_issue, folder, name, expected):
    landed, left = import_issue(folder, [(name, None)])
    assert list(landed) == [expected] and not left


def test_two_reboots_arriving_together_both_land_with_their_years(import_issue):
    landed, left = import_issue("Batman", [
        ("Batman (2011) #003.cbz", {"Series": "Batman", "Number": "3"}),
        ("Batman (2016) #003.cbz", {"Series": "Batman", "Number": "3"}),
    ])
    assert sorted(landed) == ["Batman/Batman (2011) #003.cbz", "Batman/Batman (2016) #003.cbz"]
    assert not left
    for year in (2011, 2016):
        with zipfile.ZipFile(landed[f"Batman/Batman ({year}) #003.cbz"]) as zf:
            assert zf.read("page.png") == f"Batman ({year}) #003.cbz".encode()


@pytest.mark.parametrize("name,ci", [
    ("Batman (2016) #003.cbz", {"Series": "Batman (2016)", "Number": "3"}),
    ("Batman #003.cbz", {"Series": "Batman", "Number": "3", "Volume": "2016"}),
    ("Batman (2016) #003.cbz", {"Series": "Batman", "Number": "3", "Volume": "2016"}),
])
def test_comicinfo_confirmed_series_year_names_the_reboot_in_a_plain_folder(import_issue, name, ci):
    landed, left = import_issue("Batman", [(name, ci)])
    assert list(landed) == ["Batman/Batman (2016) #003.cbz"] and not left
    assert comicinfo(landed["Batman/Batman (2016) #003.cbz"])["Series"] == "Batman (2016)"


@pytest.mark.parametrize("folder,name,ci", [
    ("Batman (2016)", "Batman #003.cbz", {"Series": "Batman", "Number": "3", "Volume": "2011"}),
    ("Batman", "Batman (2016) #003.cbz", {"Series": "Batman (2011)", "Number": "3"}),
])
def test_conflicting_series_years_stay_in_intake(import_issue, folder, name, ci):
    landed, left = import_issue(folder, [(name, ci)])
    assert not landed and left == [name]


@pytest.mark.parametrize("name,ci", [
    ("Batman (2016) 003 (2019) (Digital) (Zone-Empire).cbz", {"Series": "Batman", "Number": "3", "Year": "2019"}),
    ("Batman (2016) #003 (2019).cbz", {"Series": "Batman", "Number": "3", "Year": "2019"}),
    ("Batman (2016) #003 (2019).cbz", None),
    ("Batman #003 (2019).cbz", None),
])
def test_scene_names_in_a_reboot_folder_land_with_the_issue_year_in_xml(import_issue, name, ci):
    landed, left = import_issue("Batman (2016)", [(name, ci)])
    assert list(landed) == ["Batman (2016)/Batman (2016) #003.cbz"] and not left
    fields = comicinfo(landed["Batman (2016)/Batman (2016) #003.cbz"])
    assert fields["Series"] == "Batman (2016)" and fields["Year"] == "2019"


def test_unconfirmed_bare_scene_number_in_a_reboot_folder_lands_under_its_arrival_name(import_issue):
    # Without ComicInfo the bare 003 is not guessed (D1); it lands, not held.
    landed, left = import_issue("Batman (2016)", [("Batman (2016) 003 (2019) (Digital) (Zone-Empire).cbz", None)])
    assert list(landed) == ["Batman (2016)/Batman 003 (2016) (2019).cbz"] and not left


@pytest.mark.parametrize("number,expected", [("3", "Series #003.cbz"), ("4", "Series 3.cbz")])
def test_bare_number_uses_media_name_only_when_comicinfo_number_agrees(import_issue, number, expected):
    landed, left = import_issue("Series", [("Series 3.cbz", {"Series": "Series", "Number": number})])
    assert list(landed) == [f"Series/{expected}"] and not left


@pytest.fixture
def failing_year_writes(monkeypatch):
    """PermissionError on every .tmp.cbz write while the watcher records Year.

    The real five-attempt writer runs (sleeps suppressed) and gives up
    without raising, exactly as on a locked SMB share.
    """
    state = {"on": True, "attempts": 0}
    real_zipfile, real_record = zipfile.ZipFile, watcher._record_comicinfo_year
    active = {"recording": False}

    class LockedZipFile(real_zipfile):
        def __init__(self, file, mode="r", *args, **kwargs):
            if (mode == "w" and active["recording"] and state["on"]
                    and str(file).endswith(".tmp.cbz")):
                state["attempts"] += 1
                raise PermissionError(13, "locked by test", str(file))
            super().__init__(file, mode, *args, **kwargs)

    def recording(*args, **kwargs):
        active["recording"] = True
        try:
            return real_record(*args, **kwargs)
        finally:
            active["recording"] = False

    monkeypatch.setattr(zipfile, "ZipFile", LockedZipFile)
    monkeypatch.setattr(watcher, "_record_comicinfo_year", recording)
    monkeypatch.setattr(watcher.time, "sleep", lambda s: None)
    return state


@pytest.mark.parametrize("name", ["Batman (2016) #003 (2019).cbz", "Batman #003 (2019).cbz"])
def test_a_failed_year_write_keeps_the_year_bearing_name_then_a_retry_lands(
        import_issue, failing_year_writes, tmp_path, name):
    landed, left = import_issue("Batman (2016)", [(name, None)])
    assert failing_year_writes["attempts"] == 5
    # Nothing landed; the intake archive still names 2019 (the XML lacks it).
    assert not landed and len(left) == 1 and "(2019)" in left[0]
    retained = next((tmp_path / "intake").rglob("*.cbz"))
    assert "Year" not in comicinfo(retained)
    # The lock clears; a retry (the watcher's next pass or a restart) lands it.
    failing_year_writes["on"] = False
    watcher.process_and_move_directory(retained.parent)
    final = tmp_path / "Comix" / "Batman (2016)" / "Batman (2016) #003.cbz"
    assert final.is_file() and not list((tmp_path / "intake").rglob("*.cbz"))
    assert comicinfo(final)["Year"] == "2019"
    assert [p.name for p in (tmp_path / "Comix").rglob("*.cbz")] == [final.name]


def test_year_recording_injects_and_verifies_comicinfo_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher.time, "sleep", lambda s: None)
    path = archive(tmp_path / "Batman #003 (2019).cbz")
    watcher._record_comicinfo_year(path, "2019")
    assert comicinfo(path)["Year"] == "2019"
    with zipfile.ZipFile(path) as zf:
        assert zf.read("page.png") == b"Batman #003 (2019).cbz"


# --- planner -----------------------------------------------------------------

@pytest.fixture
def plan_library(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    db = tmp_path / "catalog.db"
    with database_connection(db) as connection:
        apply_migrations(connection, Path(cp.__file__).parents[1] / "database/migrations")
    styles = registry(tmp_path, [{"path": str(root), "style": "issue"}])

    def book(rel, ci=None):
        path = archive(root / rel, ci)
        with database_connection(db) as connection:
            aid = connection.execute("INSERT INTO archive_files(file_size) VALUES (?)",
                                     (path.stat().st_size,)).lastrowid
            connection.execute("INSERT INTO file_locations(archive_id,path,file_size,modified_time_ns) "
                               "VALUES (?,?,?,?)", (aid, str(path), path.stat().st_size, path.stat().st_mtime_ns))
        return path

    def plan():
        return cp.build_plan(root, database=db, quarantine_date="2026-10-26", number_style_registry=styles)

    def apply(doc):
        for rename in doc["renames"]:
            source, target = Path(rename["source"]), Path(rename["destination"])
            source.rename(target)
            with database_connection(db) as connection:
                connection.execute("UPDATE file_locations SET path=?, modified_time_ns=? WHERE path=?",
                                   (str(target), target.stat().st_mtime_ns, str(source)))
    return book, plan, apply


def names(doc):
    return sorted(Path(r["destination"]).name for r in doc["renames"])


@pytest.mark.parametrize("files", [
    ["Batman/Batman (2016) #003.cbz"],
    ["Batman/Batman (2011) #003.cbz", "Batman/Batman (2016) #003.cbz"],
    ["1602/1602 (2003) #001.cbz"],
])
def test_planner_holds_an_unconfirmed_filename_series_year(plan_library, files):
    book, plan, _ = plan_library
    for rel in files:
        series = rel.split("/")[0]
        book(rel, {"Series": series, "Number": "3" if series == "Batman" else "1"})
    doc = plan()
    assert not doc["renames"] and len(doc["review"]) == len(files)


def test_planner_keeps_a_comicinfo_confirmed_reboot_name(plan_library):
    book, plan, _ = plan_library
    book("Batman/Batman (2016) #003.cbz", {"Series": "Batman (2016)", "Number": "3"})
    doc = plan()
    assert not doc["renames"] and not doc["review"]


def test_planner_names_a_comicvine_volume_year_and_reaches_a_fixpoint(plan_library):
    book, plan, apply = plan_library
    book("Batman/old #3.cbz", {"Series": "Batman", "Number": "3", "Volume": "2016"})
    doc = plan()
    assert names(doc) == ["Batman (2016) #003.cbz"] and not doc["review"]
    apply(doc)
    again = plan()
    assert not again["renames"] and not again["review"]


@pytest.mark.parametrize("rel,expected", [
    ("Series/Series Full.cbz", "Series v01 (TPB).cbz"),
    ("Batman (2016)/Batman Full.cbz", "Batman (2016) v01 (TPB).cbz"),
])
def test_unnumbered_full_without_comicinfo_is_a_fixpoint_after_apply(plan_library, rel, expected):
    book, plan, apply = plan_library
    book(rel)
    doc = plan()
    assert names(doc) == [expected] and not doc["review"]
    apply(doc)
    again = plan()
    assert not again["renames"] and not again["review"], again["review"]


@pytest.mark.parametrize("rel,ci", [
    ("Batman/Other (2016) #003.cbz", {"Series": "Batman", "Number": "3"}),
    # Folder authority replaces a ComicInfo/filename typo; its year still counts.
    ("Tales for a Halloween Night/Tales for a Haloween Nite (2016) #003.cbz",
     {"Series": "Tales for a HalloweeNight", "Number": "3"}),
])
def test_planner_holds_a_year_after_an_unmatched_or_replaced_title(plan_library, rel, ci):
    book, plan, _ = plan_library
    source = book(rel, ci)
    before = source.read_bytes()
    doc = plan()
    assert not doc["renames"] and len(doc["review"]) == 1
    assert source.read_bytes() == before


def test_shared_formatting_never_drops_a_year_after_an_unmatched_title():
    tokens = fr.parse_name_tokens("Other (2016) #003.cbz")
    with pytest.raises(KeepArrivalName):
        media_fields(series="Batman", folder="Batman", number="3", volume=None, style="issue", tokens=tokens)
    fields = media_fields(series="Batman", folder="Batman (2016)", number="3", volume=None,
                          style="issue", tokens=tokens)
    assert format_canonical_name(fields) == "Batman (2016) #003.cbz"


def test_other_canonical_tpb_volumes_gain_no_filename_authority(plan_library):
    book, plan, _ = plan_library
    book("Series/Series v02 (TPB).cbz")
    doc = plan()
    assert not doc["renames"] and doc["review"]
