"""Planner entry-point witnesses: real tmp CBZ files and a migrated tmp DB.

These assert decisions and refusal behaviour, rather than reproducing the
planner's implementation. No live library, executor or schema installer is used.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from comic_automation.archive.page_hashing import ArchivePageHashRepository, calculate_page_hashes
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.database.read_guards import DatabaseChangedError
from comic_automation.library import canonical_plan as cp
from comic_automation.library.canonical_apply import executor, markers
from comic_automation.library.canonical_apply.plan import parse_plan
from scripts.cbz_canonical_plan import main

MIGRATIONS = Path(__file__).resolve().parents[1] / "comic_automation/database/migrations"


@pytest.fixture
def env(tmp_path):
    """A library with catalogued books, plus helpers to seed real page evidence."""
    root = tmp_path / "library"
    root.mkdir()
    db = tmp_path / "catalog.db"
    with database_connection(db) as connection:
        apply_migrations(connection, MIGRATIONS)

    def book(name="old Ch. 7.cbz", *, series="Series", number="7", pages=(b"a",),
             ci=None, catalog=True, stored=True, folder="Series"):
        path = root / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        xml = ci if ci is not None else (
            f"<ComicInfo><Series>{series}</Series><Number>{number}</Number></ComicInfo>"
        ).encode()
        with zipfile.ZipFile(path, "w") as archive:
            for index, payload in enumerate(pages):
                archive.writestr(f"{index:03d}.png", payload)
            if xml:
                archive.writestr("ComicInfo.xml", xml)
        if catalog:
            result = calculate_page_hashes(path)
            with database_connection(db) as connection:
                archive_id = connection.execute(
                    "INSERT INTO archive_files(file_size) VALUES (?)", (path.stat().st_size,)
                ).lastrowid
                location_id = connection.execute(
                    "INSERT INTO file_locations(archive_id,path,file_size,modified_time_ns) VALUES (?,?,?,?)",
                    (archive_id, str(path), path.stat().st_size, path.stat().st_mtime_ns),
                ).lastrowid
                if stored:
                    ArchivePageHashRepository(connection).save(
                        archive_id=archive_id, location_id=location_id, result=result)
        return path

    def plan(**kwargs):
        return cp.build_plan(root, database=db, quarantine_date="2026-10-24", **kwargs)

    return root, db, book, plan


def test_ordinary_rename_uses_current_bytes_catalog_ids_and_resolved_fields(env):
    root, db, book, plan = env
    source = book()
    original = source.read_bytes()
    database_bytes = db.read_bytes()
    report = plan()
    (rename,) = report["renames"]
    assert rename["source"] == str(source)
    assert rename["destination"] == str(source.with_name("Series ch. 7.cbz"))
    assert rename["archive_id"] == rename["location_id"] == 1
    assert rename["sha256"] == hashlib.sha256(original).hexdigest()
    assert report["counts"] == dict(renames=1, families=0, quarantines=0, keeper_renames=0, review=0)
    assert source.read_bytes() == original and not Path(rename["destination"]).exists()
    assert db.read_bytes() == database_bytes
    assert not (root / ".cbz-canonical-naming").exists()
    assert not (root / ".cbz-canonical-quarantine").exists()
    assert report["database_target"] == cp.file_identity(db)
    body = {k: v for k, v in report.items() if k != "plan_digest"}
    assert report["plan_digest"] == hashlib.sha256(json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def test_identical_observation_is_deterministic_and_an_unaffected_name_binds_the_digest(env):
    root, db, book, plan = env
    book()
    extra = root / "notes.txt"
    extra.write_text("notes")
    first = plan()
    assert first == plan()
    extra.rename(root / "other.txt")
    second = plan()
    assert first["counts"] == second["counts"]
    assert first["plan_digest"] != second["plan_digest"]


def test_a_strict_prefix_family_has_one_keeper_and_owned_quarantine_proposals(env):
    root, db, book, plan = env
    older = book("Series ch. 1 v1.cbz", number="1", pages=(b"a",))
    newer = book("Series ch. 1 v2.cbz", number="1", pages=(b"a", b"b"))
    report = plan()
    assert report["renames"] == [] and report["review"] == []
    (family,) = report["families"]
    assert family["basis"] == "sha256"
    assert family["keeper"]["path"] == str(newer)
    assert family["keeper"]["rename_to"] == str(newer.with_name("Series ch. 1 [v2].cbz"))
    (dep,) = family["dependants"]
    assert dep["path"] == str(older) and dep["index_map"] == [0]
    assert (dep["relation"], dep["outcome"]) == ("strict_prefix", "superseded")
    assert Path(dep["quarantine_to"]).is_relative_to(Path(report["quarantine_root"]))
    assert report["counts"]["quarantines"] == report["counts"]["keeper_renames"] == 1
    assert older.exists() and newer.exists()


@pytest.mark.parametrize("kind", ["fork", "legacy", "missing", "stale"])
def test_an_unproved_family_never_becomes_independent_rename_or_removal_units(env, kind):
    root, db, book, plan = env
    older = book("Series ch. 1 v1.cbz", number="1", pages=(b"a",), stored=kind != "missing")
    book("Series ch. 1 v2.cbz", number="1", pages=(b"x", b"b") if kind == "fork" else (b"a", b"b"))
    if kind in {"legacy", "stale"}:
        with database_connection(db) as connection:
            if kind == "legacy":
                connection.execute("UPDATE archive_content_signatures SET algorithm_version=1")
            else:
                connection.execute("UPDATE page_hashes SET digest=?", ("0" * 64,))
    report = plan()
    assert report["families"] == report["renames"] == []
    assert report["review"] and older.exists()


@pytest.mark.parametrize("kind", ["unknown", "multiple", "historical", "orphan"])
def test_only_one_current_location_of_one_catalogued_archive_can_be_acted_on(env, kind):
    root, db, book, plan = env
    source = book(catalog=kind != "unknown")
    if kind != "unknown":
        with database_connection(db) as connection:
            if kind == "multiple":
                connection.execute("INSERT INTO file_locations(archive_id,path) VALUES (1,?)", (str(root / "elsewhere.cbz"),))
            elif kind == "historical":
                connection.execute("UPDATE file_locations SET is_current=0")
            else:
                connection.execute("PRAGMA foreign_keys=OFF")
                connection.execute("UPDATE file_locations SET archive_id=99")
    report = plan()
    assert report["renames"] == report["families"] == []
    assert len(report["review"]) == 1 and source.exists()


@pytest.mark.parametrize("kind", ["directory_is_not_authority", "unknown_tag", "year", "decensor"])
def test_unresolved_or_protected_identity_is_reviewed_instead_of_silently_dropped(env, kind):
    root, db, book, plan = env
    if kind == "directory_is_not_authority":
        source = book("Series ch. 7.cbz", ci=b"")
    elif kind == "unknown_tag":
        source = book("old Ch. 7 [Unknown Edition].cbz")
    elif kind == "year":
        source = book("old Ch. 7 (2019).cbz")
    else:
        source = book("old Ch. 7 (Decensored).cbz")
    report = plan()
    assert report["renames"] == report["families"] == []
    assert report["review"] and source.exists()


def test_the_current_formatter_owns_edition_vocabulary(env):
    root, db, book, plan = env
    book("old Ch. 7 (English).cbz")
    assert plan()["renames"][0]["destination"].endswith("Series ch. 7.cbz")


def test_originals_root_is_refused_before_any_archive_is_derived(env, monkeypatch):
    root, db, book, plan = env
    source = book(folder="ai-decensor-originals")

    def forbidden(*args, **kwargs):
        pytest.fail("originals must not be scanned")

    monkeypatch.setattr(cp.sv, "derive_archive", forbidden)
    with pytest.raises(cp.PlanningRefused, match="originals is preserved"):
        cp.build_plan(source.parent, database=db, quarantine_date="2026-10-24")


def test_nested_originals_are_excluded_before_descent_or_archive_reads(env, monkeypatch):
    root, db, book, plan = env
    original = book(folder="nested/ai-decensor-originals/Series")
    before = original.read_bytes()
    source = book()
    derive = cp.sv.derive_archive
    seen = []

    def guarded(path, **kwargs):
        assert "ai-decensor-originals" not in Path(path).parts
        seen.append(path)
        return derive(path, **kwargs)

    monkeypatch.setattr(cp.sv, "derive_archive", guarded)
    report = plan()
    assert seen == [source] and len(report["renames"]) == 1
    assert original.read_bytes() == before
    assert {"path": str(Path("nested/ai-decensor-originals")),
            "reason": "protected decensor originals"} in report["provenance"]["excluded"]


@pytest.mark.parametrize("metadata", [
    "<Tags>uncensored, camelia:black_bars</Tags>",
    "<Tags>uncensored, black_bars</Tags>",
    "<Title>Uncensored</Title>",
    "<Tags>camelia:white_bars</Tags>",
    "<Tags>Uncen</Tags>",
    "<Title>Decensor</Title>",
    '<c:Tags xmlns:c="urn:test">uncensored</c:Tags>',
])
def test_in_place_camelia_metadata_holds_a_book_with_an_unmarked_filename(env, metadata):
    root, db, book, plan = env
    source = book(ci=("<ComicInfo><Series>Series</Series><Number>7</Number>"
                      + metadata + "</ComicInfo>").encode())
    before = source.read_bytes()
    report = plan()
    assert report["renames"] == report["families"] == []
    assert "decensor identity/pairing adapter required (ComicInfo" in str(report["review"])
    assert source.read_bytes() == before


@pytest.mark.parametrize("folder", ["AI Decensor", "Decensor", "Uncen", "Uncensor", "Series_Decensored"])
def test_decensor_stem_spellings_are_held(env, folder):
    root, db, book, plan = env
    book(folder=folder)
    report = plan()
    assert report["renames"] == report["families"] == [] and report["review"]


def test_per_member_decensor_problems_block_the_entire_supersession_family(env):
    root, db, book, plan = env
    book("Series ch. 1 v1.cbz", number="1", folder="Decensored")
    book("Series ch. 1 v2.cbz", number="1", folder="Decensored", pages=(b"a", b"b"))
    report = plan()
    assert report["families"] == report["renames"] == []
    assert report["counts"]["quarantines"] == 0 and report["review"]


def test_catalog_spelling_must_equal_the_path_the_executor_will_compare(env):
    root, db, book, plan = env
    source = book()
    with database_connection(db) as connection:
        connection.execute("UPDATE file_locations SET path=?", (str(source).upper(),))
    report = plan()
    assert not report["renames"]
    assert "catalog path spelling differs" in str(report["review"])


@pytest.mark.parametrize("marker", ["root", "comicinfo"])
def test_decensor_identity_in_root_or_comicinfo_alone_is_preserved(env, marker):
    root, db, book, plan = env
    if marker == "root":
        source = book(folder="Decensored")
        report = cp.build_plan(source.parent, database=db, quarantine_date="2026-10-24")
    else:
        source = book(series="Series Decensored")
        report = plan()
    assert report["renames"] == report["families"] == []
    assert "decensor identity/pairing adapter required" in str(report["review"])


@pytest.mark.parametrize("marker", ["filename", "root", "comicinfo"])
def test_underscore_delimited_decensor_markers_still_require_pairing_evidence(env, marker):
    root, db, book, plan = env
    if marker == "root":
        source = book(folder="Series_Decensored")
        report = cp.build_plan(source.parent, database=db, quarantine_date="2026-10-24")
    elif marker == "comicinfo":
        book(series="Series_Decensored")
        report = plan()
    else:
        book("Series_Uncensored ch. 7.cbz")
        report = plan()
    assert report["renames"] == report["families"] == []
    assert "decensor identity/pairing adapter required" in str(report["review"])


@pytest.mark.parametrize("occupant", ["file", "directory", "case_variant"])
def test_an_occupied_canonical_destination_is_held_even_with_identical_bytes(env, occupant):
    root, db, book, plan = env
    source = book()
    destination = source.with_name("Series ch. 7.cbz")
    if occupant == "directory":
        destination.mkdir()
    elif occupant == "case_variant":
        destination.with_name("SERIES CH. 7.CBZ").write_bytes(source.read_bytes())
    else:
        destination.write_bytes(source.read_bytes())
    report = plan()
    assert report["renames"] == [] and report["review"]


def test_two_proposals_colliding_under_casefold_hold_both_units(env):
    root, db, book, plan = env
    book("oldA Ch. 7.cbz", series="Straße")
    book("oldB Ch. 7.cbz", series="STRASSE")
    report = plan()
    assert report["renames"] == []
    assert len(report["review"]) == 2


def test_a_keeper_collision_holds_the_entire_family(env):
    root, db, book, plan = env
    book("Series ch. 1 v1.cbz", number="1")
    newer = book("Series ch. 1 v2.cbz", number="1", pages=(b"a", b"b"))
    newer.with_name("Series ch. 1 [v2].cbz").mkdir()
    report = plan()
    assert report["families"] == report["renames"] == []
    assert report["counts"]["quarantines"] == 0


def test_a_keeper_with_no_own_version_label_holds_every_dependant(env):
    root, db, book, plan = env
    book("Series ch. 1 v1.cbz", number="1")
    book("Series ch. 1.cbz", number="1", pages=(b"a", b"b"))
    report = plan()
    assert report["families"] == report["renames"] == []
    assert "keeper version label is not resolved" in str(report["review"])


def test_quarantine_through_a_real_junction_is_not_proposed(env, tmp_path):
    root, db, book, plan = env
    book("Series ch. 1 v1.cbz", number="1")
    book("Series ch. 1 v2.cbz", number="1", pages=(b"a", b"b"))
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = root / ".cbz-canonical-quarantine"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                       check=True, capture_output=True)
    else:
        junction.symlink_to(outside, target_is_directory=True)
    report = plan()
    assert report["families"] == report["renames"] == []
    assert "reparse point" in str(report["review"])
    assert list(outside.iterdir()) == []


def test_a_junction_back_inside_the_library_still_blocks_a_family(env):
    root, db, book, plan = env
    book("Series ch. 1 v1.cbz", number="1")
    book("Series ch. 1 v2.cbz", number="1", pages=(b"a", b"b"))
    target = root / "holding"
    target.mkdir()
    junction = root / ".cbz-canonical-quarantine"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)],
                       check=True, capture_output=True)
    else:
        junction.symlink_to(target, target_is_directory=True)
    report = plan()
    assert not report["families"] and report["review"]
    assert list(target.iterdir()) == []


def test_an_occupied_quarantine_destination_in_the_excluded_tree_blocks_the_family(env):
    root, db, book, plan = env
    book("Series ch. 1 v1.cbz", number="1")
    book("Series ch. 1 v2.cbz", number="1", pages=(b"a", b"b"))
    first = plan()
    destination = Path(first["families"][0]["dependants"][0]["quarantine_to"])
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"foreign occupant")
    report = plan()
    assert not report["families"] and report["review"]
    assert destination.read_bytes() == b"foreign occupant"


def test_case_only_rename_remains_a_proposal_for_the_executor(env):
    root, db, book, plan = env
    book("SERIES CH. 7.cbz")
    report = plan()
    assert len(report["renames"]) == 1 and not report["review"]
    assert report["renames"][0]["destination"].endswith("Series ch. 7.cbz")


def test_case_only_rename_uses_windows_semantics_when_host_normcase_is_identity(env, monkeypatch):
    root, db, book, plan = env
    book("SERIES CH. 7.cbz")
    # POSIX normcase is identity. The plan targets Windows executor semantics
    # regardless of the host generating the read-only document.
    monkeypatch.setattr(cp.os.path, "normcase", lambda path: path)
    report = plan()
    assert len(report["renames"]) == 1 and not report["review"]


def test_casefold_equivalence_alone_does_not_grant_the_case_only_exception(env):
    root, db, book, plan = env
    book("Straße ch. 7.cbz", series="STRASSE")
    report = plan()
    assert not report["renames"] and report["review"]


@pytest.mark.parametrize("number", [None, "3"])
def test_conclusively_volume_classified_v_is_not_an_unresolved_version(env, number):
    root, db, book, plan = env
    ci = b"<ComicInfo><Series>Series</Series><Volume>1</Volume>" + (
        b"" if number is None else b"<Number>3</Number>"
    ) + b"</ComicInfo>"
    source = book("Series v1.cbz" if number is None else "Series v1 Ch. 3.cbz", ci=ci)
    report = plan()
    assert not report["review"] and len(report["renames"]) == 1
    expected = "Series v01.cbz" if number is None else "Series v01 #003.cbz"
    assert report["renames"][0]["destination"] == str(source.with_name(expected))


def test_a_real_unresolved_version_marker_beside_a_volume_is_still_held(env):
    root, db, book, plan = env
    book("Series v1 [v2].cbz", ci=b"<ComicInfo><Series>Series</Series><Volume>1</Volume></ComicInfo>")
    report = plan()
    assert not report["renames"] and not report["families"] and report["review"]


def test_an_applied_volume_name_replans_as_unchanged(env):
    root, db, book, plan = env
    source = book("Series Vol. 2 Ch. 12.cbz", ci=b"<ComicInfo><Series>Series</Series><Volume>2</Volume><Number>12</Number></ComicInfo>")
    first = plan()
    destination = Path(first["renames"][0]["destination"])
    source.rename(destination)
    with database_connection(db) as connection:
        connection.execute("UPDATE file_locations SET path=? WHERE path=?", (str(destination), str(source)))
    second = plan()
    assert not second["renames"] and not second["review"]
    assert second["unchanged"][0]["path"] == str(destination)


@pytest.mark.skipif(os.name != "nt", reason="the executor rename protocol is Windows-only")
def test_a_generated_volume_plan_applies_recovers_and_undoes_through_the_real_executor(env):
    root, db, book, plan = env
    source = book("Series Vol. 2 Ch. 12.cbz", ci=b"<ComicInfo><Series>Series</Series><Volume>2</Volume><Number>12</Number></ComicInfo>")
    markers.install_schema(db)
    document = plan()
    parsed = parse_plan(document)
    forward = executor.apply(parsed, library_root=root, database=db, quiescence=lambda _: None)
    assert forward.stopped is None and forward.units[0]["status"] == executor.UNIT_COMPLETED
    assert plan()["unchanged"][0]["path"].endswith("Series v02 #012.cbz")
    recovered = executor.recover(parsed, library_root=root, database=db,
        attempt_id=forward.attempt_id, quiescence=lambda _: None)
    assert recovered.resolutions[0]["status"] == executor.RES_COMPLETE
    undone = executor.undo(parsed, library_root=root, database=db,
        forward_attempt=forward.attempt_id, quiescence=lambda _: None)
    assert undone.stopped is None and source.exists()


@pytest.mark.skipif(os.name != "nt", reason="the executor rename protocol is Windows-only")
def test_an_applied_family_keeper_is_unchanged_without_certifying_its_lone_version(env):
    root, db, book, plan = env
    older = book("Series ch. 1 v1.cbz", number="1")
    newer = book("Series ch. 1 v2.cbz", number="1", pages=(b"a", b"b"))
    markers.install_schema(db)
    first = plan()
    assert first["counts"]["families"] == 1
    forward = executor.apply(parse_plan(first), library_root=root, database=db,
                             quiescence=lambda _: None)
    assert forward.stopped is None and forward.units[0]["status"] == executor.UNIT_COMPLETED
    assert not older.exists() and not newer.exists()
    second = plan()
    assert not second["families"] and not second["renames"] and not second["review"]
    (unchanged,) = second["unchanged"]
    assert unchanged["path"].endswith("Series ch. 1 [v2].cbz")
    assert unchanged["diagnostic"] == "unverified_version_label"
    assert "version" not in unchanged and "family" not in unchanged
    assert second["diagnostics"] == {"unverified_version_labels": 1}
    # A sibling cancels the no-op shortcut: both books must pass the full
    # byte-bound family proof, even though the old keeper looks canonical.
    book("Series ch. 1 v3.cbz", number="1", pages=(b"a", b"b", b"c"))
    third = plan()
    assert not third["review"] and third["counts"]["families"] == 1
    assert third["diagnostics"]["unverified_version_labels"] == 0
    assert third["families"][0]["keeper"]["path"].endswith("v3.cbz")


def test_a_lone_canonical_version_is_reviewed_when_a_resolved_field_would_change_it(env):
    root, db, book, plan = env
    book("Series ch. 1 [v2].cbz", number="2")
    report = plan()
    assert report["review"] and not report["unchanged"] and not report["renames"]


def test_another_book_in_the_folder_restores_normal_version_evidence_rules(env):
    root, db, book, plan = env
    source = book("Series ch. 1 [v2].cbz", number="1")
    book("Other ch. 2.cbz", series="Other", number="2")
    report = plan()
    assert report["diagnostics"]["unverified_version_labels"] == 0
    assert any(str(source) in item.get("members", []) or item.get("path") == str(source)
               for item in report["review"])


def test_a_canonical_version_with_an_unverified_label_is_a_zero_exit_noop(env, capsys):
    root, db, book, plan = env
    book("Series ch. 1 [v2].cbz", number="1")
    assert main(["--library-root", str(root), "--database", str(db),
                 "--quarantine-date", "2026-10-24"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["diagnostics"]["unverified_version_labels"] == 1
    assert not report["families"] and not report["renames"]


def test_an_already_canonical_archive_is_unchanged(env):
    root, db, book, plan = env
    source = book("Series ch. 7.cbz")
    report = plan()
    assert report["renames"] == report["review"] == []
    assert report["unchanged"][0]["path"] == str(source)


def test_internal_history_is_excluded_and_never_opened(env):
    root, db, book, plan = env
    book()
    history = root / ".stversions"
    history.mkdir()
    (history / "corrupt.cbz").write_bytes(b"not a zip")
    report = plan()
    assert report["counts"]["renames"] == 1 and not report["review"]
    assert report["provenance"]["excluded"] == [{"path": ".stversions", "reason": "internal/history"}]


def test_inventory_change_during_derivation_refuses_the_entire_report(env, monkeypatch):
    root, db, book, plan = env
    book()
    original = cp.sv.derive_archive

    def derive(*args, **kwargs):
        result = original(*args, **kwargs)
        (root / "late.txt").write_text("arrived")
        return result

    monkeypatch.setattr(cp.sv, "derive_archive", derive)
    with pytest.raises(cp.PlanningRefused, match="inventory changed"):
        plan()


def test_a_wal_commit_during_byte_derivation_invalidates_the_global_plan(env, monkeypatch):
    root, db, book, plan = env
    book()
    original = cp.sv.derive_archive

    def derive(*args, **kwargs):
        result = original(*args, **kwargs)
        with database_connection(db) as connection:
            connection.execute("UPDATE archive_files SET page_count=123")
        return result

    monkeypatch.setattr(cp.sv, "derive_archive", derive)
    with pytest.raises(DatabaseChangedError):
        plan()


def test_a_database_replacement_is_not_silently_rebound_at_the_end(env, monkeypatch):
    root, db, book, plan = env
    book()
    identity = cp.file_identity
    calls = 0

    def replaced(path):
        nonlocal calls
        calls += 1
        result = identity(path)
        if calls >= 3:
            result["st_ino"] += 1
        return result

    monkeypatch.setattr(cp, "file_identity", replaced)
    with pytest.raises(cp.PlanningRefused, match="database target changed"):
        plan()


def test_private_copy_is_outside_the_library_and_leaves_no_files(env, tmp_path):
    root, db, book, plan = env
    book()
    temporary = tmp_path / "private"
    temporary.mkdir()
    report = plan(max_held_bytes=0, temp_dir=temporary)
    assert report["counts"]["renames"] == 1 and list(temporary.iterdir()) == []
    with pytest.raises(cp.PlanningRefused, match="outside the library"):
        plan(max_held_bytes=0, temp_dir=root)


def test_copy_budget_refusal_never_produces_an_action(env):
    root, db, book, plan = env
    book()
    report = plan(max_held_bytes=0, max_copy_bytes=1)
    assert report["renames"] == [] and report["review"]


@pytest.mark.parametrize("day", ["2026-1-1", "2026-02-30", "../../escape"])
def test_quarantine_date_is_a_real_iso_date(env, day):
    root, db, book, plan = env
    with pytest.raises(ValueError):
        cp.build_plan(root, database=db, quarantine_date=day)


@pytest.mark.parametrize("kind, expected", [("clear", 0), ("review", 1), ("error", 2)])
def test_cli_prints_only_a_complete_plan_and_meaningful_exit_status(env, capsys, kind, expected):
    root, db, book, plan = env
    book(catalog=kind != "review")
    args = ["--library-root", str(root), "--database", str(db), "--quarantine-date", "2026-10-24"]
    if kind == "error":
        args[3] = str(db.with_name("missing.db"))
    assert main(args) == expected
    streams = capsys.readouterr()
    if kind == "error":
        assert streams.out == "" and "Plan not produced" in streams.err
        assert not db.with_name("missing.db").exists()
    else:
        assert json.loads(streams.out)["contract"] == cp.CONTRACT and streams.err == ""


def test_cli_unexpected_failure_never_masquerades_as_a_complete_review_plan(env, capsys, monkeypatch):
    from scripts import cbz_canonical_plan as cli
    root, db, book, plan = env

    def fail(*args, **kwargs):
        raise RuntimeError("injected evaluation failure")

    monkeypatch.setattr(cli, "build_plan", fail)
    assert cli.main(["--library-root", str(root), "--database", str(db),
                     "--quarantine-date", "2026-10-24"]) == 2
    streams = capsys.readouterr()
    assert not streams.out and "injected evaluation failure" in streams.err


def test_cli_output_is_new_utf8_json_that_the_executor_can_read(env, tmp_path, capsys):
    root, db, book, plan = env
    book(series="作品")
    output = tmp_path / "plan.json"
    args = ["--library-root", str(root), "--database", str(db),
            "--quarantine-date", "2026-10-24", "--output", str(output)]
    assert main(args) == 0
    data = output.read_bytes()
    assert data.startswith(b"{") and "作品" in data.decode("utf-8")
    report = json.loads(data.decode("utf-8"))
    assert json.loads(capsys.readouterr().out)["plan_digest"] == report["plan_digest"]
    assert main(args) == 2
    assert output.read_bytes() == data and "already exists" in capsys.readouterr().err


def test_cli_never_writes_a_plan_into_the_library(env, capsys):
    root, db, book, plan = env
    output = root / "plan.json"
    assert main(["--library-root", str(root), "--database", str(db),
                 "--quarantine-date", "2026-10-24", "--output", str(output)]) == 2
    assert not output.exists() and "outside the library" in capsys.readouterr().err


def test_cli_output_that_appears_during_planning_is_not_overwritten(env, tmp_path, capsys, monkeypatch):
    from scripts import cbz_canonical_plan as cli
    root, db, book, plan = env
    output = tmp_path / "plan.json"
    original = cli.build_plan

    def appears(*args, **kwargs):
        result = original(*args, **kwargs)
        output.write_bytes(b"foreign occupant")
        return result

    monkeypatch.setattr(cli, "build_plan", appears)
    assert cli.main(["--library-root", str(root), "--database", str(db),
                     "--quarantine-date", "2026-10-24", "--output", str(output)]) == 2
    assert output.read_bytes() == b"foreign occupant" and "already exists" in capsys.readouterr().err
