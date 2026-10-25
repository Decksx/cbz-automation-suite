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
    assert plan()["renames"][0]["destination"].endswith("Series ch. 7 (English).cbz")


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


def test_case_only_rename_remains_a_proposal_for_the_executor(env):
    root, db, book, plan = env
    book("SERIES CH. 7.cbz")
    report = plan()
    assert len(report["renames"]) == 1 and not report["review"]
    assert report["renames"][0]["destination"].endswith("Series ch. 7.cbz")


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
