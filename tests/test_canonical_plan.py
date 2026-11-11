"""Planner entry-point witnesses: real tmp CBZ files and a migrated tmp DB.

These assert decisions and refusal behaviour, rather than reproducing the
planner's implementation. All database and executor operations use fixtures.
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
        return cp.build_plan(root, database=db, quarantine_date="2026-10-24", number_style="chapter", **kwargs)

    return root, db, book, plan


def test_ordinary_rename_uses_current_bytes_catalog_ids_and_resolved_fields(env):
    root, db, book, plan = env
    source = book()
    original = source.read_bytes()
    database_bytes = db.read_bytes()
    report = plan()
    (rename,) = report["renames"]
    assert rename["source"] == str(source)
    assert rename["destination"] == str(source.with_name("Series Ch. 7.cbz"))
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
    assert family["keeper"]["rename_to"] == str(newer.with_name("Series Ch. 1 [v2].cbz"))
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
        source = book("Series Ch. 7.cbz", ci=b"")
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
    assert plan()["renames"][0]["destination"].endswith("Series Ch. 7.cbz")


def test_originals_root_is_refused_before_any_archive_is_derived(env, monkeypatch):
    root, db, book, plan = env
    source = book(folder="ai-decensor-originals")

    def forbidden(*args, **kwargs):
        pytest.fail("originals must not be scanned")

    monkeypatch.setattr(cp.sv, "derive_archive", forbidden)
    with pytest.raises(cp.PlanningRefused, match="originals is preserved"):
        cp.build_plan(source.parent, database=db, quarantine_date="2026-10-24", number_style="chapter")


@pytest.mark.parametrize("originals", ["ai-decensor-originals", "AI-Decensor-Originals"])
def test_nested_originals_are_excluded_before_descent_or_archive_reads(env, monkeypatch, originals):
    root, db, book, plan = env
    original = book(folder=f"nested/{originals}/Series")
    before = original.read_bytes()
    source = book()
    derive = cp.sv.derive_archive
    seen = []

    def guarded(path, **kwargs):
        assert "ai-decensor-originals" not in {part.casefold() for part in Path(path).parts}
        seen.append(path)
        return derive(path, **kwargs)

    monkeypatch.setattr(cp.sv, "derive_archive", guarded)
    report = plan()
    assert seen == [source] and len(report["renames"]) == 1
    assert original.read_bytes() == before
    assert {"path": str(Path("nested") / originals),
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
        report = cp.build_plan(source.parent, database=db, quarantine_date="2026-10-24", number_style="chapter")
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
        report = cp.build_plan(source.parent, database=db, quarantine_date="2026-10-24", number_style="chapter")
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
    destination = source.with_name("Series Ch. 7.cbz")
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
    book("oldA Ch. 7.cbz", series="Straße", folder="STRASSE")
    book("oldB Ch. 7.cbz", series="STRASSE", folder="STRASSE")
    report = plan()
    assert report["renames"] == []
    assert len(report["review"]) == 2


def test_a_keeper_collision_holds_the_entire_family(env):
    root, db, book, plan = env
    book("Series ch. 1 v1.cbz", number="1")
    newer = book("Series ch. 1 v2.cbz", number="1", pages=(b"a", b"b"))
    newer.with_name("Series Ch. 1 [v2].cbz").mkdir()
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
    assert report["renames"][0]["destination"].endswith("Series Ch. 7.cbz")


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
    book("Straße ch. 7.cbz", series="STRASSE", folder="STRASSE")
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
    expected = "Series v01.cbz" if number is None else "Series v01 Ch. 3.cbz"
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
    assert plan()["unchanged"][0]["path"].endswith("Series v02 Ch. 12.cbz")
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
    assert unchanged["path"].endswith("Series Ch. 1 [v2].cbz")
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
    book("Series Ch. 1 [v2].cbz", number="2")
    report = plan()
    assert report["review"] and not report["unchanged"] and not report["renames"]


def test_another_book_in_the_same_identity_group_restores_normal_version_evidence_rules(env):
    """A co-member still requires the full family proof, even for a no-op."""
    root, db, book, plan = env
    source = book("Series Ch. 1 [v2].cbz", number="1")
    book("old Ch. 1.cbz", number="1", pages=(b"different",))
    report = plan()
    assert report["diagnostics"]["unverified_version_labels"] == 0
    assert any(str(source) in item.get("members", []) or item.get("path") == str(source)
               for item in report["review"])


def test_a_canonical_version_with_an_unverified_label_is_a_zero_exit_noop(env, capsys):
    root, db, book, plan = env
    book("Series Ch. 1 [v2].cbz", number="1")
    assert main(["--library-root", str(root), "--database", str(db),
                 "--quarantine-date", "2026-10-24", "--number-style", "chapter"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["diagnostics"]["unverified_version_labels"] == 1
    assert not report["families"] and not report["renames"]


def test_an_already_canonical_archive_is_unchanged(env):
    root, db, book, plan = env
    source = book("Series Ch. 7.cbz")
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
    args = ["--library-root", str(root), "--database", str(db), "--quarantine-date", "2026-10-24", "--number-style", "chapter"]
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
                     "--quarantine-date", "2026-10-24", "--number-style", "chapter"]) == 2
    streams = capsys.readouterr()
    assert not streams.out and "injected evaluation failure" in streams.err


def test_cli_output_is_new_utf8_json_that_the_executor_can_read(env, tmp_path, capsys):
    root, db, book, plan = env
    book(series="作品", folder="作品")
    output = tmp_path / "plan.json"
    args = ["--library-root", str(root), "--database", str(db),
            "--quarantine-date", "2026-10-24", "--number-style", "chapter", "--output", str(output)]
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
                 "--quarantine-date", "2026-10-24", "--number-style", "chapter", "--output", str(output)]) == 2
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
                     "--quarantine-date", "2026-10-24", "--number-style", "chapter", "--output", str(output)]) == 2
    assert output.read_bytes() == b"foreign occupant" and "already exists" in capsys.readouterr().err


def test_cli_exclusive_create_refuses_a_file_appearing_after_the_last_check(env, tmp_path, capsys, monkeypatch):
    root, db, book, plan = env
    output = tmp_path / "plan.json"
    original_open = Path.open

    def appears(path, mode="r", *args, **kwargs):
        if path == output and mode in {"x", "w"}:
            with original_open(output, "wb") as stream:
                stream.write(b"late foreign occupant")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", appears)
    assert main(["--library-root", str(root), "--database", str(db),
                 "--quarantine-date", "2026-10-24", "--number-style", "chapter", "--output", str(output)]) == 2
    assert output.read_bytes() == b"late foreign occupant" and capsys.readouterr().err


def _junction(link: Path, target: Path) -> None:
    """Use a real filesystem alias so lexical containment mutants are tested."""
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize("series,number", [("Series", "2"), ("Other", "1")])
def test_lone_label_is_scoped_to_its_identity_group(env, series, number):
    """Other chapters or series in one folder do not certify or block a label."""
    root, db, book, plan = env
    source = book("Series Ch. 1 [v2].cbz", number="1")
    book(f"{series} Ch. {number}.cbz", series=series, number=number, folder=series)
    report = plan()
    assert not report["review"] and not report["renames"] and not report["families"]
    assert report["diagnostics"] == {"unverified_version_labels": 1}
    item = next(row for row in report["unchanged"] if row["path"] == str(source))
    assert item["diagnostic"] == "unverified_version_label"


@pytest.mark.skipif(os.name != "nt", reason="the executor rename protocol is Windows-only")
def test_family_apply_replans_in_a_multi_chapter_folder(env):
    """A real family and ordinary renames settle together after actual apply."""
    root, db, book, plan = env
    book("Series ch. 1 v1.cbz", number="1")
    book("Series ch. 1 v2.cbz", number="1", pages=(b"a", b"b"))
    book("old Ch. 2.cbz", number="2")
    book("old Ch. 3.cbz", number="3")
    markers.install_schema(db)
    first = plan()
    assert first["counts"]["renames"] == 2 and first["counts"]["families"] == 1
    forward = executor.apply(parse_plan(first), library_root=root, database=db,
                             quiescence=lambda _: None)
    assert forward.stopped is None
    assert all(row["status"] == executor.UNIT_COMPLETED for row in forward.units)
    second = plan()
    assert not second["review"] and not second["renames"] and not second["families"]
    assert len(second["unchanged"]) == 3
    assert second["diagnostics"]["unverified_version_labels"] == 1
    # A new co-member restores family evidence even beside other chapters.
    book("Series ch. 1 v3.cbz", number="1", pages=(b"a", b"b", b"c"))
    third = plan()
    assert not third["review"] and third["counts"]["families"] == 1
    assert third["diagnostics"]["unverified_version_labels"] == 0


@pytest.mark.parametrize("family", [False, True])
def test_proposed_names_must_survive_resolution_again(env, family):
    """Title text may acquire optional-field candidates after formatting.

    Both ordinary and family proposals must hold before a rename strands the
    next plan in review; a family hold must also prevent quarantine.
    """
    root, db, book, plan = env
    book("old Ch. 7 v1.cbz" if family else "old Ch. 7.cbz", series="Series Vol. 2", folder="Series Vol. 2")
    if family:
        book("old Ch. 7 v2.cbz", series="Series Vol. 2", folder="Series Vol. 2", pages=(b"a", b"b"))
    report = plan()
    assert report["review"] and not report["renames"] and not report["families"]
    assert any("fixpoint" in reason for item in report["review"] for reason in item["reasons"])


@pytest.mark.parametrize("alias", [False, True])
def test_catalog_inside_library_is_refused_before_any_read(env, tmp_path, monkeypatch, alias):
    """No SQLite open may create WAL/SHM in the library, even through an alias."""
    root, db, book, plan = env
    inside = root / "catalog.db"
    inside.write_bytes(db.read_bytes())
    database = inside
    if alias:
        link = tmp_path / "db-alias"
        _junction(link, root)
        database = link / inside.name
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    def forbidden(*args, **kwargs):
        pytest.fail("must refuse before opening SQLite")

    monkeypatch.setattr(cp, "read_consistent_snapshot", forbidden)
    with pytest.raises(cp.PlanningRefused, match="database.*outside the library"):
        cp.build_plan(root, database=database, quarantine_date="2026-10-24", number_style="chapter")
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_originals_root_hidden_by_a_junction_is_refused(env, tmp_path, monkeypatch):
    """A root's lexical parts cannot establish that it is outside originals."""
    root, db, book, plan = env
    source = book(folder="AI-Decensor-Originals/Series")
    link = tmp_path / "innocent-root"
    _junction(link, source.parent)

    def forbidden(*args, **kwargs):
        pytest.fail("originals may not be opened")

    monkeypatch.setattr(cp.sv, "derive_archive", forbidden)
    with pytest.raises(cp.PlanningRefused, match="originals is preserved"):
        cp.build_plan(link, database=db, quarantine_date="2026-10-24", number_style="chapter")


def test_output_alias_into_library_is_refused_before_planning(env, tmp_path, monkeypatch, capsys):
    """An external report directory may still resolve into the library."""
    from scripts import cbz_canonical_plan as cli
    root, db, book, plan = env
    source = book()
    link = tmp_path / "report-alias"
    _junction(link, source.parent)
    output = link / "plan.json"

    def forbidden(*args, **kwargs):
        pytest.fail("output refusal must precede planning")

    monkeypatch.setattr(cli, "build_plan", forbidden)
    assert cli.main(["--library-root", str(root), "--database", str(db),
                     "--quarantine-date", "2026-10-24", "--number-style", "chapter", "--output", str(output)]) == 2
    assert not output.exists() and "outside the library" in capsys.readouterr().err


def test_saved_receipt_preserves_nonzero_diagnostics(env, tmp_path, capsys):
    """The receipt must expose the same unverified-label count as its file."""
    root, db, book, plan = env
    book("Series Ch. 1 [v2].cbz", number="1")
    output = tmp_path / "report.json"
    assert main(["--library-root", str(root), "--database", str(db),
                 "--quarantine-date", "2026-10-24", "--number-style", "chapter", "--output", str(output)]) == 0
    receipt = json.loads(capsys.readouterr().out)
    document = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["diagnostics"] == document["diagnostics"] == {"unverified_version_labels": 1}


# --- scoped planning (#158 B1) -------------------------------------------------


def _two_series(book):
    """Two series folders, each with one book that plans as a rename."""
    alpha = book(series="Alpha", folder="Alpha")
    beta = book(series="Beta", folder="Beta")
    return alpha, beta


def _mutate_once(monkeypatch, mutate):
    """Run ``mutate`` once, after the first archive has been derived.

    Folders are planned in sorted order, so the first derivation is Alpha's
    and the mutation lands mid-plan, between the two inventory walks.
    """
    original = cp.sv.derive_archive
    done = []

    def derive(*args, **kwargs):
        result = original(*args, **kwargs)
        if not done:
            done.append(True)
            mutate()
        return result

    monkeypatch.setattr(cp.sv, "derive_archive", derive)


def test_scoped_mode_skips_only_the_series_folder_that_changed(env, monkeypatch):
    """A book arriving in Beta drops Beta's rename and keeps Alpha's.

    The arrival also changes Beta's own directory mtime, so this would
    refuse outright if a series folder's row were treated as root level.
    """
    root, db, book, plan = env
    alpha, beta = _two_series(book)
    _mutate_once(monkeypatch, lambda: (root / "Beta" / "late.cbz").write_bytes(b"new"))
    report = plan(scoped=True)
    assert [r["source"] for r in report["renames"]] == [str(alpha)]
    assert report["skipped_scopes"] == [{"scope": "Beta", "path": str(root / "Beta"),
        "reason": "series folder inventory changed during planning"}]
    assert report["planning_scope"] == {"mode": "series", "requested": None}
    assert report["diagnostics"]["skipped_scopes"] == 1
    assert report["counts"]["renames"] == 1 and not report["review"]
    assert parse_plan(report).digest == report["plan_digest"]


def test_scoped_mode_drops_holds_from_a_skipped_folder(env, monkeypatch):
    """A skipped folder contributes nothing to the plan, not even its holds."""
    root, db, book, plan = env
    book(series="Alpha", folder="Alpha")
    book(series="Beta", folder="Beta", catalog=False)
    assert len(plan(scoped=True)["review"]) == 1
    _mutate_once(monkeypatch, lambda: (root / "Beta" / "late.txt").write_text("x"))
    report = plan(scoped=True)
    assert report["review"] == [] and len(report["renames"]) == 1
    assert [s["scope"] for s in report["skipped_scopes"]] == ["Beta"]


def test_scoped_mode_skips_a_change_deep_inside_a_series_folder(env, monkeypatch):
    root, db, book, plan = env
    alpha = book(series="Alpha", folder="Alpha")
    book(series="Beta", folder="Beta/Volume 1")
    _mutate_once(monkeypatch, lambda: (root / "Beta" / "Volume 1" / "late.txt").write_text("x"))
    report = plan(scoped=True)
    assert [r["source"] for r in report["renames"]] == [str(alpha)]
    assert [s["scope"] for s in report["skipped_scopes"]] == ["Beta"]


def test_a_series_folder_appearing_during_scoped_planning_is_reported(env, monkeypatch):
    root, db, book, plan = env
    _two_series(book)
    _mutate_once(monkeypatch, lambda: (root / "Gamma").mkdir())
    report = plan(scoped=True)
    assert len(report["renames"]) == 2
    assert report["skipped_scopes"] == [{"scope": "Gamma", "path": str(root / "Gamma"),
        "reason": "series folder appeared during planning"}]


def test_strict_mode_still_refuses_a_change_inside_one_series_folder(env, monkeypatch):
    root, db, book, plan = env
    _two_series(book)
    _mutate_once(monkeypatch, lambda: (root / "Beta" / "late.cbz").write_bytes(b"new"))
    with pytest.raises(cp.PlanningRefused, match="library inventory changed"):
        plan()


def test_strict_mode_documents_carry_no_scoped_keys(env):
    """Scoped planning is opt-in; a default plan's contract is unchanged."""
    root, db, book, plan = env
    _two_series(book)
    report = plan()
    assert "skipped_scopes" not in report and "planning_scope" not in report
    assert report["diagnostics"] == {"unverified_version_labels": 0}


@pytest.mark.parametrize("arrival", ["late.cbz", "notes.txt", ".stversions"])
def test_scoped_mode_refuses_a_root_level_change_outside_every_series(env, monkeypatch, arrival):
    """The root level is not a series and cannot be skipped.

    Loose root books are planned under root-wide authority, and an arrival
    there belongs to no single series, so scoped mode refuses like strict.
    """
    root, db, book, plan = env
    _two_series(book)

    def arrive():
        target = root / arrival
        if arrival.startswith("."):
            target.mkdir()
        else:
            target.write_bytes(b"new")

    _mutate_once(monkeypatch, arrive)
    with pytest.raises(cp.PlanningRefused, match="root level changed"):
        plan(scoped=True)


def test_named_scopes_plan_only_those_folders_and_ignore_others(env, monkeypatch):
    """With --scope, unnamed series folders are neither planned nor watched.

    Loose root books are still observed (a root-level change refuses) but
    are not planned, because the operator named only Alpha.
    """
    root, db, book, plan = env
    alpha, beta = _two_series(book)
    loose = book(series="Loose", folder=".")
    _mutate_once(monkeypatch, lambda: (root / "Beta" / "late.cbz").write_bytes(b"new"))
    report = plan(scopes=["Alpha"])
    assert [r["source"] for r in report["renames"]] == [str(alpha)]
    assert report["skipped_scopes"] == [] and report["review"] == []
    assert report["planning_scope"] == {"mode": "series", "requested": ["Alpha"]}
    assert not any(row["path"].startswith("Beta") for row in report["provenance"]["inventory"])
    assert any(row["path"] == loose.name for row in report["provenance"]["inventory"])


@pytest.mark.parametrize("name, message", [
    ("Missing", "not a folder directly under the root"),
    ("..", "one folder name"),
    ("Alpha/x", "one folder name"),
    (".stversions", "excluded"),
    ("alpha", "not a folder directly under the root"),
])
def test_named_scopes_must_be_exact_plain_series_folders(env, name, message):
    root, db, book, plan = env
    _two_series(book)
    (root / ".stversions").mkdir()
    with pytest.raises(cp.PlanningRefused, match=message):
        plan(scopes=[name])


def test_the_digest_seals_the_skipped_scopes(env, monkeypatch):
    """An approved digest cannot silently differ in what was left unplanned."""
    from comic_automation.library.canonical_apply.plan import PlanError
    root, db, book, plan = env
    _two_series(book)
    _mutate_once(monkeypatch, lambda: (root / "Beta" / "late.txt").write_text("x"))
    report = plan(scoped=True)
    for forged in ([], [{**report["skipped_scopes"][0], "scope": "Gamma"}]):
        with pytest.raises(PlanError, match="plan_digest"):
            parse_plan({**report, "skipped_scopes": forged})


def test_cli_scoped_plan_with_a_skipped_folder_is_not_a_clean_exit(env, capsys, monkeypatch):
    root, db, book, plan = env
    _two_series(book)
    args = ["--library-root", str(root), "--database", str(db),
            "--quarantine-date", "2026-10-24", "--number-style", "chapter"]
    assert main([*args, "--scope", "Alpha", "--scope", "Beta"]) == 0
    assert json.loads(capsys.readouterr().out)["skipped_scopes"] == []
    _mutate_once(monkeypatch, lambda: (root / "Beta" / "late.txt").write_text("x"))
    assert main([*args, "--scoped"]) == 1
    assert [s["scope"] for s in json.loads(capsys.readouterr().out)["skipped_scopes"]] == ["Beta"]


def _vanish_during_walk(monkeypatch, root, walk, target, away, *, listing=False):
    """Move ``target`` to ``away`` mid-listing, inside inventory walk ``walk``.

    The hook runs only while `_inventory` call number ``walk`` is listing
    ``target``'s parent: the real children are listed first, then the target
    moves before they are returned, so the walk's own lstat finds it gone.
    That is the watcher-moves-a-book race, made deterministic. With
    ``listing`` the target folder instead moves after its lstat but just
    before its own listing, so the walk's iterdir is what fails.
    """
    import contextlib
    original_inventory, original_scandir = cp._inventory, cp.os.scandir
    calls, active = [], []

    def inventory(*args, **kwargs):
        calls.append(True)
        active.append(len(calls) == walk)
        try:
            return original_inventory(*args, **kwargs)
        finally:
            active.pop()

    def scandir(folder):
        here = Path(folder)
        if not (active and active[-1]):
            return original_scandir(folder)
        if listing and here == target and target.exists():
            target.rename(away)  # gone before its own listing starts
        if not listing and here == target.parent and target.exists():
            with original_scandir(folder) as it:
                entries = list(it)  # listed (types cached) before the move
            target.rename(away)
            return contextlib.nullcontext(entries)
        return original_scandir(folder)

    monkeypatch.setattr(cp, "_inventory", inventory)
    monkeypatch.setattr(cp.os, "scandir", scandir)


@pytest.mark.parametrize("walk", [1, 2])
@pytest.mark.parametrize("what", ["book", "folder", "listing"])
def test_scoped_mode_skips_a_series_whose_entry_vanishes_mid_listing(env, monkeypatch, tmp_path, walk, what):
    """Codex's #159 R1: a move between listing and lstat cost the whole plan.

    Whether the book or the whole Beta folder moves, and whether it happens
    during the first or the final walk, Alpha's rename survives and Beta is
    skipped with a reason, instead of FileNotFoundError aborting every scope.
    """
    root, db, book, plan = env
    alpha, beta = _two_series(book)
    target = beta if what == "book" else root / "Beta"
    _vanish_during_walk(monkeypatch, root, walk, target, tmp_path / f"moved-{what}",
                        listing=what == "listing")
    report = plan(scoped=True)
    assert [r["source"] for r in report["renames"]] == [str(alpha)]
    assert report["skipped_scopes"] == [{"scope": "Beta", "path": str(root / "Beta"),
        "reason": "series folder changed while it was being listed"}]
    assert parse_plan(report).digest == report["plan_digest"]


def test_strict_mode_still_aborts_when_an_entry_vanishes_mid_listing(env, monkeypatch, tmp_path):
    """Tolerance is scoped-only; strict mode keeps its all-or-nothing contract."""
    root, db, book, plan = env
    alpha, beta = _two_series(book)
    _vanish_during_walk(monkeypatch, root, 2, beta, tmp_path / "moved")
    with pytest.raises((FileNotFoundError, cp.PlanningRefused)):
        plan()


def test_a_skipped_scope_drops_its_family_and_quarantine(env, monkeypatch):
    """Codex's #159 R2: the family filter had no direct witness.

    Beta holds a two-version family that would quarantine its older member.
    Once Beta changes mid-plan, neither the family nor its quarantine may
    survive, while Alpha's ordinary rename still does.
    """
    root, db, book, plan = env
    alpha = book(series="Alpha", folder="Alpha")
    book("Beta ch. 1 v1.cbz", series="Beta", number="1", pages=(b"a",), folder="Beta")
    book("Beta ch. 1 v2.cbz", series="Beta", number="1", pages=(b"a", b"b"), folder="Beta")
    baseline = plan(scoped=True)
    assert len(baseline["families"]) == 1 and baseline["counts"]["quarantines"] == 1
    _mutate_once(monkeypatch, lambda: (root / "Beta" / "late.txt").write_text("x"))
    report = plan(scoped=True)
    assert report["families"] == [] and report["counts"]["quarantines"] == 0
    assert [r["source"] for r in report["renames"]] == [str(alpha)]
    assert [s["scope"] for s in report["skipped_scopes"]] == ["Beta"]


def test_a_skipped_scope_drops_its_unchanged_rows(env, monkeypatch):
    """The unchanged-row filter had no direct witness either (#159 R2)."""
    root, db, book, plan = env
    alpha = book(series="Alpha", folder="Alpha")
    book("Beta Ch. 7.cbz", series="Beta", folder="Beta")
    baseline = plan(scoped=True)
    assert any(Path(u["path"]).parent.name == "Beta" for u in baseline["unchanged"])
    _mutate_once(monkeypatch, lambda: (root / "Beta" / "late.txt").write_text("x"))
    report = plan(scoped=True)
    assert not any(Path(u["path"]).parent.name == "Beta" for u in report["unchanged"])
    assert [r["source"] for r in report["renames"]] == [str(alpha)]



@pytest.mark.parametrize("walk", [1, 2])
@pytest.mark.parametrize("scopes", [None, ["Alpha"]])
@pytest.mark.parametrize("entry", ["loose.txt", ".cbz-canonical-naming", "ai-decensor-originals"])
def test_a_root_entry_vanishing_mid_listing_refuses_scoped_planning(env, monkeypatch, tmp_path, walk, scopes, entry):
    """Codex's #159 re-review: a vanished root entry is root-level evidence.

    Its type was never observed as a plannable series folder (a loose file,
    the planner's own history folder, protected decensor originals), so it
    cannot be confined to a scope and must refuse, with or without --scope.
    """
    root, db, book, plan = env
    _two_series(book)
    target = root / entry
    if entry.endswith(".txt"):
        target.write_text("x")
    else:
        target.mkdir()
    _vanish_during_walk(monkeypatch, root, walk, target, tmp_path / "moved-root-entry")
    with pytest.raises(cp.PlanningRefused, match="root level changed"):
        plan(scoped=True) if scopes is None else plan(scopes=scopes)


def test_an_unrequested_series_folder_vanishing_is_not_this_plans_concern(env, monkeypatch, tmp_path):
    """With --scope Alpha, Beta is never observed; its folder moving changes nothing."""
    root, db, book, plan = env
    alpha, beta = _two_series(book)
    _vanish_during_walk(monkeypatch, root, 1, root / "Beta", tmp_path / "moved-beta")
    report = plan(scopes=["Alpha"])
    assert [r["source"] for r in report["renames"]] == [str(alpha)]
    assert report["skipped_scopes"] == []


# PR (C): the canonical chapter token is "Ch." (David, 2026-11-10). A library
# file the formatter named before, with "ch.", is renamed once; never back.


def test_an_earlier_lowercase_chapter_name_is_renamed_once(env):
    root, db, book, plan = env
    source = book("Series ch. 7.cbz")
    first = plan()
    assert not first["review"] and len(first["renames"]) == 1
    destination = Path(first["renames"][0]["destination"])
    assert destination.name == "Series Ch. 7.cbz" and destination.parent == source.parent
    source.rename(destination)
    with database_connection(db) as connection:
        connection.execute("UPDATE file_locations SET path=? WHERE path=?", (str(destination), str(source)))
    second = plan()
    assert not second["renames"] and not second["review"] and not second["families"]
    assert second["unchanged"][0]["path"] == str(destination)
    assert [p.name for p in destination.parent.iterdir()] == ["Series Ch. 7.cbz"]


@pytest.mark.skipif(os.name != "nt", reason="the executor rename protocol is Windows-only")
def test_the_lowercase_rename_applies_through_the_real_executor_and_settles(env):
    root, db, book, plan = env
    source = book("Series ch. 7 (2019).cbz",
                  ci=b"<ComicInfo><Series>Series</Series><Number>7</Number><Year>2019</Year></ComicInfo>")
    markers.install_schema(db)
    forward = executor.apply(parse_plan(plan()), library_root=root, database=db, quiescence=lambda _: None)
    assert forward.stopped is None and forward.units[0]["status"] == executor.UNIT_COMPLETED
    assert [p.name for p in source.parent.iterdir()] == ["Series Ch. 7 (2019).cbz"]
    again = plan()
    assert not again["renames"] and not again["review"]
    assert again["unchanged"][0]["path"].endswith("Series Ch. 7 (2019).cbz")


def test_a_lone_lowercase_name_with_an_unverified_version_label_keeps_its_name(env):
    """A lone filename-only version label is a no-op in either spelling: a
    rename could not certify the label, so the "ch." name is left as it is
    (unchanged, with the diagnostic), neither renamed nor held for review."""
    root, db, book, plan = env
    source = book("Series ch. 1 [v2].cbz", number="1")
    report = plan()
    assert not report["renames"] and not report["families"] and not report["review"]
    (unchanged,) = report["unchanged"]
    assert unchanged["path"] == str(source)
    assert unchanged["diagnostic"] == "unverified_version_label"
    assert report["diagnostics"] == {"unverified_version_labels": 1}
