"""Re-certification of legacy ordered content signatures (plan steps 2, 3, 5).

`comic_automation.archive.content_signature_recertify` classifies every
version-"1" ``ordered-page-sha256`` signature from stored rows, relabels only
the order-stable ones to version "2", and reports what remains. These tests
build real archives in ``tmp_path``, store them exactly as the pre-tie-break
code did (`_old_order_result`), and exercise each class, the digest-equality
claim that justifies a relabel, the read-only plan, every apply refusal,
per-row drift between plan and apply, idempotence, the rows apply must not
touch, and the postflight populations. No real database or library.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import types
import warnings
from pathlib import Path

import pytest

from comic_automation.archive import content_signature_recertify as rc
from comic_automation.archive.page_hashing import (
    ArchivePageHashRepository,
    calculate_page_hashes,
    page_order_key,
    signature_evidence_counts,
)
from comic_automation.archive.perceptual_hashing import (
    ArchivePerceptualHashRepository,
    calculate_perceptual_hashes,
)
from comic_automation.database.backup_cli import run_backup
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.database.read_guards import DatabaseChangedError
from tests import golden_corpus as gc
from tests.test_page_order_tie_break import _old_order_result


MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


# --- fixtures -------------------------------------------------------------


def _cbz(path: Path, names: list[str], seed: int = 1) -> Path:
    """An archive with one distinct decodable page per name, in this order."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # "Duplicate name"
        return gc.build_cbz(
            path,
            [(name, gc.page_image(seed * 100 + i)) for i, name in
             enumerate(names)],
        )


def _store(connection, path: Path, *, legacy: bool = True,
           perceptual: bool = False) -> int:
    """Seed identity + current location and store the page inventory.

    ``legacy=True`` stores the pre-tie-break inventory and labels the
    signature "1": exactly what a database written before the bump holds.
    """
    stat = path.stat()
    archive_id = int(connection.execute(
        "INSERT INTO archive_files (file_size) VALUES (?)", (stat.st_size,)
    ).lastrowid)
    location_id = int(connection.execute(
        "INSERT INTO file_locations (archive_id, path, file_size, "
        "modified_time_ns) VALUES (?, ?, ?, ?)",
        (archive_id, str(path.resolve()), stat.st_size, stat.st_mtime_ns),
    ).lastrowid)
    result = _old_order_result(path) if legacy else calculate_page_hashes(path)
    ArchivePageHashRepository(connection).save(
        archive_id=archive_id, location_id=location_id, result=result
    )
    if perceptual:
        ArchivePerceptualHashRepository(connection).save(
            archive_id=archive_id, result=calculate_perceptual_hashes(path)
        )
    if legacy:
        connection.execute(
            "UPDATE archive_content_signatures SET algorithm_version = '1' "
            "WHERE archive_id = ?", (archive_id,),
        )
    return archive_id


@pytest.fixture()
def library(tmp_path: Path):
    """One archive of every class, plus the database they live in.

    * ``stable``: 10/2/1 packed out of order; no tie, no repeat.
    * ``stable2``: a second order-stable archive, so per-row drift on one
      can be shown not to block the other.
    * ``tied``: a.png before A.png physically; "1" kept a.png first, "2"
      puts A.png first.
    * ``unresolved``: 1.png twice.
    * ``stale``: order-stable names, but its location's recorded mtime has
      moved since the signature was taken.
    """
    db = tmp_path / "pages.db"
    ids = {}
    with database_connection(db) as connection:
        apply_migrations(connection, MIGRATIONS)
        ids["stable"] = _store(connection, _cbz(
            tmp_path / "stable.cbz", ["10.png", "2.png", "1.png"]),
            perceptual=True)
        ids["stable2"] = _store(connection, _cbz(
            tmp_path / "stable2.cbz", ["p1.png", "p2.png"], seed=2))
        ids["tied"] = _store(connection, _cbz(
            tmp_path / "tied.cbz", ["a.png", "A.png"], seed=3))
        ids["unresolved"] = _store(connection, _cbz(
            tmp_path / "unresolved.cbz", ["1.png", "1.png", "2.png"], seed=4))
        ids["stale"] = _store(connection, _cbz(
            tmp_path / "stale.cbz", ["1.png", "2.png"], seed=5))
        connection.execute(
            "UPDATE file_locations SET modified_time_ns = modified_time_ns + 1"
            " WHERE archive_id = ?", (ids["stale"],),
        )
    return db, ids


def _dump(db: Path) -> list[str]:
    with database_connection(db) as connection:
        return list(connection.iterdump())


def _rows(db: Path, sql: str, params=()) -> list[tuple]:
    with database_connection(db) as connection:
        return [tuple(r) for r in connection.execute(sql, params)]


_BACKUPS = iter(range(1, 10_000))


def _backup(tmp_path: Path, db: Path) -> Path:
    """A genuine verified backup_cli backup of `db`, under a fresh name."""
    n = next(_BACKUPS)
    report = tmp_path / f"backup{n}.json"
    run_backup(database=db, backup=tmp_path / f"backup{n}.db",
               json_output=report)
    return report


def _report(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_plan(tmp_path: Path, db: Path) -> tuple[Path, dict]:
    document = rc.make_plan(db)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path, document


def _plan_and_backup(tmp_path: Path, db: Path) -> tuple[Path, dict, Path]:
    """Plan, then back up: the order the freshness rule requires."""
    plan_path, document = _write_plan(tmp_path, db)
    return plan_path, document, _backup(tmp_path, db)


def _apply(tmp_path, db, plan_path, document, **overrides):
    """Apply `document`; without a report, take a backup now (after it)."""
    if "backup_report" not in overrides:
        overrides["backup_report"] = _backup(tmp_path, db)
    arguments = dict(
        expect_digest=document["plan_digest"],
        expect_count=document["plan"]["counts"]["order_stable"],
    )
    arguments.update(overrides)
    return rc.apply_plan(db, plan_path, **arguments)


def _redigest(plan_path: Path, document: dict) -> None:
    """Write `document` back with a digest recomputed over its edited body.

    Simulates a *valid-looking* plan, so the guard under test is the one
    that refuses it rather than the digest check.
    """
    document["plan_digest"] = rc.plan_digest(document["plan"])
    plan_path.write_text(json.dumps(document), encoding="utf-8")


def _clone(source: Path, destination: Path) -> Path:
    """A SQLite-backup-API copy: same rows, different file."""
    with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)
    src.close()
    dst.close()
    return destination


def _version(db: Path, archive_id: int) -> str:
    return _rows(db, "SELECT algorithm_version FROM archive_content_signatures"
                     " WHERE archive_id = ?", (archive_id,))[0][0]


# --- classification -------------------------------------------------------


def test_each_legacy_signature_lands_in_exactly_its_class(library) -> None:
    db, ids = library
    body = rc.make_plan(db)["plan"]

    assert body["counts"] == {
        "legacy_total": 5, "stale": 1, "unresolved": 1, "tied": 1,
        "inconsistent": 0, "order_stable": 2, "reinventory_cost": 2,
    }
    assert [b["archive_id"] for b in body["order_stable"]] == [
        ids["stable"], ids["stable2"]]
    assert body["tied"] == [{"archive_id": ids["tied"],
                             "reason": "natural_key_tie",
                             "names": ["A.png", "a.png"]}]
    assert body["unresolved"] == [{"archive_id": ids["unresolved"],
                                   "reason": "repeated_entry_name",
                                   "names": ["1.png"]}]
    assert body["stale"] == [{"archive_id": ids["stale"],
                              "reason": "source_metadata_differs",
                              "names": []}]
    assert body["reinventory_archive_ids"] == sorted(
        [ids["tied"], ids["unresolved"]])


def test_current_version_rows_are_not_planned(library, tmp_path) -> None:
    """Only version "1" is legacy; a fresh "2" save is not a candidate."""
    db, _ = library
    with database_connection(db) as connection:
        _store(connection, _cbz(tmp_path / "fresh.cbz", ["1.png"], seed=9),
               legacy=False)
    assert rc.make_plan(db)["plan"]["counts"]["legacy_total"] == 5


@pytest.mark.parametrize(
    "mutation, reason",
    [
        ("DELETE FROM file_locations WHERE archive_id = :a",
         "no_current_location"),
        ("INSERT INTO file_locations (archive_id, path, file_size, "
         "modified_time_ns) SELECT archive_id, path || '.2', file_size, "
         "modified_time_ns FROM file_locations WHERE archive_id = :a",
         "multiple_current_locations"),
        ("UPDATE file_locations SET file_size = file_size + 1 "
         "WHERE archive_id = :a", "source_metadata_differs"),
        ("UPDATE file_locations SET modified_time_ns = NULL "
         "WHERE archive_id = :a", "source_metadata_differs"),
    ],
)
def test_stale_compares_recorded_size_and_mtime_of_the_current_location(
    library, mutation, reason
) -> None:
    """Recorded metadata of the single is_current location, nothing live."""
    db, ids = library
    with database_connection(db) as connection:
        connection.execute(mutation, {"a": ids["stable"]})
    stale = rc.make_plan(db)["plan"]["stale"]
    assert {"archive_id": ids["stable"], "reason": reason,
            "names": []} in stale


def test_a_non_current_location_does_not_bind_a_signature(library) -> None:
    db, ids = library
    with database_connection(db) as connection:
        connection.execute("UPDATE file_locations SET is_current = 0 "
                           "WHERE archive_id = ?", (ids["stable"],))
    reasons = {e["archive_id"]: e["reason"]
               for e in rc.make_plan(db)["plan"]["stale"]}
    assert reasons[ids["stable"]] == "no_current_location"


@pytest.mark.parametrize(
    "mutation, reason",
    [
        ("UPDATE archive_content_signatures SET page_count = page_count + 1 "
         "WHERE archive_id = :a", "page_count_differs"),
        ("UPDATE archive_pages SET page_index = page_index + 10 "
         "WHERE archive_id = :a AND page_index = 2", "page_index_not_contiguous"),
        ("DELETE FROM page_hashes WHERE algorithm = 'sha256' AND page_id = "
         "(SELECT id FROM archive_pages WHERE archive_id = :a "
         "AND page_index = 0)", "missing_page_sha256"),
        ("UPDATE archive_pages SET page_index = CASE page_index WHEN 0 THEN 1 "
         "WHEN 1 THEN 0 ELSE page_index END WHERE archive_id = :a",
         "stored_order_not_page_order"),
        ("UPDATE archive_content_signatures SET digest = "
         "'00' || substr(digest, 3) WHERE archive_id = :a",
         "digest_not_reproducible"),
    ],
)
def test_an_inventory_that_does_not_reproduce_its_digest_is_inconsistent(
    library, mutation, reason
) -> None:
    """The stored basis is verified before a relabel can certify it."""
    db, ids = library
    with database_connection(db) as connection:
        if "CASE page_index" in mutation:
            # UNIQUE(archive_id, page_index) forbids an in-place swap, so
            # park one row out of the way first.
            connection.execute(
                "UPDATE archive_pages SET page_index = -1 "
                "WHERE archive_id = ? AND page_index = 0", (ids["stable"],))
            connection.execute(
                "UPDATE archive_pages SET page_index = 0 "
                "WHERE archive_id = ? AND page_index = 1", (ids["stable"],))
            connection.execute(
                "UPDATE archive_pages SET page_index = 1 "
                "WHERE archive_id = ? AND page_index = -1", (ids["stable"],))
        else:
            connection.execute(mutation, {"a": ids["stable"]})
    body = rc.make_plan(db)["plan"]
    assert {"archive_id": ids["stable"], "reason": reason,
            "names": []} in body["inconsistent"]
    assert ids["stable"] in body["reinventory_archive_ids"]
    assert ids["stable"] not in [b["archive_id"] for b in body["order_stable"]]


# --- the claim that justifies a relabel -----------------------------------


def _v2_digest_from_stored_rows(db: Path, archive_id: int) -> str:
    """Recompute, independently of the tool, the version-2 digest.

    Stored page sha256 rows, ordered by `page_order_key` over the stored
    names (version-2 order), hashed as `calculate_page_hashes` does.
    """
    rows = _rows(
        db,
        "SELECT ap.entry_name, ph.digest FROM archive_pages ap JOIN "
        "page_hashes ph ON ph.page_id = ap.id AND ph.algorithm = 'sha256' "
        "WHERE ap.archive_id = ?", (archive_id,),
    )
    ordered = sorted(rows, key=lambda r: page_order_key(r[0]))
    signature = hashlib.sha256(len(ordered).to_bytes(8, "big"))
    for _, digest in ordered:
        signature.update(bytes.fromhex(digest))
    return signature.hexdigest()


def test_order_stable_stored_digest_equals_the_version_2_digest(
    library, tmp_path
) -> None:
    """The proof: for an order-stable row, old digest == v2 digest.

    Checked two independent ways -- recomputed from the stored page rows in
    version-2 order, and computed fresh from the archive bytes by the
    current code -- so the equality is not the tool agreeing with itself.
    """
    db, ids = library
    for name in ("stable", "stable2"):
        stored = _rows(db, "SELECT digest FROM archive_content_signatures "
                           "WHERE archive_id = ?", (ids[name],))[0][0]
        assert _v2_digest_from_stored_rows(db, ids[name]) == stored
        assert calculate_page_hashes(
            tmp_path / f"{name}.cbz").content_digest == stored


def test_a_tied_row_would_be_wrong_if_relabelled(library, tmp_path) -> None:
    """Contrapositive control: for a tie, the stored digest is NOT v2's."""
    db, ids = library
    stored = _rows(db, "SELECT digest FROM archive_content_signatures "
                       "WHERE archive_id = ?", (ids["tied"],))[0][0]
    assert _v2_digest_from_stored_rows(db, ids["tied"]) != stored
    assert calculate_page_hashes(tmp_path / "tied.cbz").content_digest != stored


def test_ordered_signature_digest_matches_calculate_page_hashes(
    tmp_path,
) -> None:
    result = calculate_page_hashes(_cbz(tmp_path / "x.cbz", ["2.png", "1.png"]))
    assert rc.ordered_signature_digest(
        p.digest for p in result.pages) == result.content_digest




# --- location references are diagnostics, foreign ones are inconsistent ---


def _relocate(connection, archive_id: int) -> int:
    """Move the archive's current location to a new row, same size/mtime.

    The signature and page rows keep pointing at the old, now non-current
    location -- what a relocation leaves behind.
    """
    old = connection.execute(
        "SELECT path, file_size, modified_time_ns FROM file_locations "
        "WHERE archive_id = ? AND is_current = 1", (archive_id,)).fetchone()
    connection.execute("UPDATE file_locations SET is_current = 0 "
                       "WHERE archive_id = ?", (archive_id,))
    return int(connection.execute(
        "INSERT INTO file_locations (archive_id, path, file_size, "
        "modified_time_ns) VALUES (?, ?, ?, ?)",
        (archive_id, old[0] + ".moved", old[1], old[2])).lastrowid)


def test_a_relocation_alone_stays_order_stable_with_diagnostics(library):
    db, ids = library
    with database_connection(db) as connection:
        new_location = _relocate(connection, ids["stable"])
    body = rc.make_plan(db)["plan"]
    planned = {b["archive_id"]: b for b in body["order_stable"]}
    basis = planned[ids["stable"]]
    assert basis["current_location_id"] == new_location
    assert basis["location_diagnostics"] == [
        "signature_location_not_current", "page_location_not_current"]
    assert body["location_diagnostics"] == {
        "page_location_not_current": 1, "signature_location_not_current": 1}
    assert planned[ids["stable2"]]["location_diagnostics"] == []


def test_a_null_signature_location_is_a_diagnostic(library):
    db, ids = library
    with database_connection(db) as connection:
        connection.execute("UPDATE archive_content_signatures SET location_id "
                           "= NULL WHERE archive_id = ?", (ids["stable"],))
    planned = {b["archive_id"]: b
               for b in rc.make_plan(db)["plan"]["order_stable"]}
    assert planned[ids["stable"]]["location_diagnostics"] == [
        "signature_location_null"]


@pytest.mark.parametrize(
    "table, reason",
    [("archive_content_signatures", "signature_location_foreign"),
     ("archive_pages", "page_location_foreign")],
)
def test_a_reference_to_another_archives_location_is_inconsistent(
    library, table, reason
) -> None:
    db, ids = library
    with database_connection(db) as connection:
        foreign = connection.execute(
            "SELECT id FROM file_locations WHERE archive_id = ?",
            (ids["stable2"],)).fetchone()[0]
        connection.execute(f"UPDATE {table} SET location_id = ? "
                           "WHERE archive_id = ?", (foreign, ids["stable"]))
    body = rc.make_plan(db)["plan"]
    assert {"archive_id": ids["stable"], "reason": reason,
            "names": []} in body["inconsistent"]


# --- plan is read-only ----------------------------------------------------


def test_plan_writes_nothing_and_brackets_its_read_with_data_version(
    library,
) -> None:
    db, _ = library
    before_dump = _dump(db)
    before_bytes = db.read_bytes()
    document = rc.make_plan(db)
    snapshot = document["snapshot"]
    assert snapshot["data_version_before"] == snapshot["data_version_after"]
    assert snapshot["concurrent_commit_detected"] is False
    assert _dump(db) == before_dump
    assert db.read_bytes() == before_bytes


def test_plan_is_rejected_when_another_connection_commits_mid_read(
    library, monkeypatch
) -> None:
    """A commit inside the read window must reject the plan, not mix states."""
    db, ids = library

    def committing_quick_check(connection):
        with database_connection(db) as writer:
            writer.execute("UPDATE archive_content_signatures SET "
                           "updated_at = 'x' WHERE archive_id = ?",
                           (ids["stable"],))
        return "ok"

    monkeypatch.setattr(rc, "quick_check", committing_quick_check)
    with pytest.raises(DatabaseChangedError):
        rc.make_plan(db)


def test_the_plan_digest_covers_binding_time_and_classification(library):
    """Canonical bytes; the target and plan time are inside the digest."""
    db, _ = library
    first, second = rc.make_plan(db), rc.make_plan(db)
    assert first["plan_digest"] == hashlib.sha256(json.dumps(
        first["plan"], sort_keys=True, separators=(",", ":"),
        ensure_ascii=True).encode("ascii")).hexdigest()
    assert first["plan"]["target"] == rc.file_identity(db)
    assert isinstance(first["plan"]["planned_at_ns"], int)
    assert "data_version_before" not in first["plan"]
    # Two runs differ only by their plan time.
    a, b = dict(first["plan"]), dict(second["plan"])
    a.pop("planned_at_ns"), b.pop("planned_at_ns")
    assert a == b


# --- the 1 -> 2 transition is pinned --------------------------------------


def test_every_mode_refuses_if_the_writer_moves_past_version_2(
    library, tmp_path, monkeypatch
) -> None:
    db, _ = library
    plan_path, document, report = _plan_and_backup(tmp_path, db)
    monkeypatch.setattr(rc, "CONTENT_SIGNATURE_VERSION", "3")
    before = _dump(db)
    with pytest.raises(rc.PlanRefusedError, match="1 -> 2"):
        rc.make_plan(db)
    with pytest.raises(rc.PlanRefusedError, match="1 -> 2"):
        _apply(tmp_path, db, plan_path, document, backup_report=report)
    with pytest.raises(rc.PlanRefusedError, match="1 -> 2"):
        rc.postflight(db)
    assert _dump(db) == before


# --- apply ----------------------------------------------------------------


def test_apply_relabels_exactly_the_order_stable_rows(library, tmp_path):
    db, ids = library
    plan_path, document = _write_plan(tmp_path, db)
    result = _apply(tmp_path, db, plan_path, document)

    assert result["relabelled"] == [ids["stable"], ids["stable2"]]
    assert result["skipped"] == [] and result["already_recertified"] == []
    assert {name: _version(db, i) for name, i in ids.items()} == {
        "stable": "2", "stable2": "2", "tied": "1", "unresolved": "1",
        "stale": "1",
    }


def test_a_relabelled_row_is_comparable_with_a_fresh_version_2_save(
    library, tmp_path
) -> None:
    """Positive control: after relabel, the gated reader groups it."""
    db, ids = library
    copy = tmp_path / "copy" / "stable.cbz"
    copy.parent.mkdir()
    copy.write_bytes((tmp_path / "stable.cbz").read_bytes())
    with database_connection(db) as connection:
        _store(connection, copy, legacy=False)
        assert ArchivePageHashRepository(
            connection).duplicate_content_groups() == []
    plan_path, document = _write_plan(tmp_path, db)
    _apply(tmp_path, db, plan_path, document)
    with database_connection(db) as connection:
        groups = ArchivePageHashRepository(connection).duplicate_content_groups()
    assert [g["archive_count"] for g in groups] == [2]


def test_page_rows_and_perceptual_rows_are_untouched(library, tmp_path):
    db, ids = library
    pages = "SELECT * FROM archive_pages ORDER BY id"
    hashes = "SELECT * FROM page_hashes ORDER BY id"
    signature_cols = ("SELECT id, archive_id, location_id, algorithm, digest,"
                      " page_count, image_bytes, source_file_size, "
                      "source_modified_time_ns, calculated_at, created_at "
                      "FROM archive_content_signatures ORDER BY id")
    files = "SELECT * FROM archive_files ORDER BY id"
    before = [_rows(db, q) for q in (pages, hashes, signature_cols, files)]
    perceptual = _rows(db, "SELECT COUNT(*) FROM page_hashes "
                           "WHERE algorithm != 'sha256'")[0][0]
    assert perceptual > 0  # precondition: there is something to protect

    plan_path, document = _write_plan(tmp_path, db)
    _apply(tmp_path, db, plan_path, document)

    assert [_rows(db, q) for q in (pages, hashes, signature_cols, files)] == (
        before)


def test_apply_twice_with_the_same_plan_and_backup_is_idempotent(
    library, tmp_path
) -> None:
    """Rerun policy: the original backup is still the preimage."""
    db, ids = library
    plan_path, document, report = _plan_and_backup(tmp_path, db)
    _apply(tmp_path, db, plan_path, document, backup_report=report)
    after_first = _dump(db)

    second = _apply(tmp_path, db, plan_path, document, backup_report=report)
    assert second["relabelled"] == []
    assert second["already_recertified"] == [ids["stable"], ids["stable2"]]
    assert second["skipped"] == []
    assert second["postflight"]["passed"] is True
    assert _dump(db) == after_first


@pytest.mark.parametrize(
    "change",
    [{"expect_digest": "0" * 64}, {"expect_count": 1}, {"expect_count": 3}],
)
def test_apply_refuses_entirely_on_digest_or_count_mismatch(
    library, tmp_path, change
) -> None:
    db, _ = library
    plan_path, document, report = _plan_and_backup(tmp_path, db)
    before = _dump(db)
    with pytest.raises(rc.PlanRefusedError):
        _apply(tmp_path, db, plan_path, document, backup_report=report,
               **change)
    assert _dump(db) == before


def test_apply_refuses_a_plan_edited_after_it_was_written(library, tmp_path):
    """The digest is recomputed from the body, not read from the file."""
    db, ids = library
    plan_path, document, report = _plan_and_backup(tmp_path, db)
    document["plan"]["order_stable"].append(
        dict(document["plan"]["order_stable"][0], archive_id=ids["tied"]))
    plan_path.write_text(json.dumps(document), encoding="utf-8")
    before = _dump(db)
    with pytest.raises(rc.PlanRefusedError, match="edited"):
        _apply(tmp_path, db, plan_path, document, expect_count=3,
               backup_report=report)
    assert _dump(db) == before


@pytest.mark.parametrize(
    "field, value",
    [("to_version", "3"), ("from_version", "0"), ("plan_format", "x/1"),
     ("algorithm", "other"), ("planned_at_ns", None)],
)
def test_apply_refuses_a_plan_for_another_format_or_transition(
    library, tmp_path, field, value
) -> None:
    db, _ = library
    plan_path, document, report = _plan_and_backup(tmp_path, db)
    document["plan"][field] = value
    _redigest(plan_path, document)
    with pytest.raises(rc.PlanRefusedError, match="another format"):
        _apply(tmp_path, db, plan_path, document, backup_report=report)


def test_apply_refuses_an_unreadable_plan(library, tmp_path) -> None:
    db, _ = library
    plan_path = tmp_path / "plan.json"
    plan_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(rc.PlanRefusedError, match="Unreadable plan"):
        rc.apply_plan(db, plan_path, expect_digest="0" * 64, expect_count=0,
                      backup_report=tmp_path / "unused.json")


# --- target binding -------------------------------------------------------


def test_a_plan_for_one_database_is_refused_on_a_clone(library, tmp_path):
    """Codex's probe: A's plan, a backup-API clone B with its own backup."""
    db, ids = library
    plan_path, document = _write_plan(tmp_path, db)
    clone = _clone(db, tmp_path / "clone.db")
    clone_report = _backup(tmp_path, clone)
    before = _dump(clone)
    with pytest.raises(rc.PlanRefusedError, match="different database"):
        rc.apply_plan(clone, plan_path, expect_digest=document["plan_digest"],
                      expect_count=2, backup_report=clone_report)
    assert _dump(clone) == before
    assert _version(db, ids["stable"]) == "1"


def test_a_restored_target_is_a_different_database(library, tmp_path):
    """Restore policy: replacing the file changes its identity; re-plan."""
    db, _ = library
    plan_path, document = _write_plan(tmp_path, db)
    clone = _clone(db, tmp_path / "restore.db")
    os.replace(clone, db)
    with pytest.raises(rc.PlanRefusedError, match="different database"):
        _apply(tmp_path, db, plan_path, document)


def test_an_altered_target_binding_is_refused(library, tmp_path):
    db, _ = library
    plan_path, document, report = _plan_and_backup(tmp_path, db)
    document["plan"]["target"]["st_ino"] += 1
    _redigest(plan_path, document)
    before = _dump(db)
    with pytest.raises(rc.PlanRefusedError, match="different database"):
        _apply(tmp_path, db, plan_path, document, backup_report=report,
               expect_digest=document["plan_digest"])
    assert _dump(db) == before


def test_a_hard_link_to_the_target_is_refused(library, tmp_path):
    """Alias policy: a hard link is another real path, so not accepted."""
    db, _ = library
    plan_path, document, report = _plan_and_backup(tmp_path, db)
    link = tmp_path / "link.db"
    os.link(db, link)
    with pytest.raises(rc.PlanRefusedError, match="different database"):
        rc.apply_plan(link, plan_path, expect_digest=document["plan_digest"],
                      expect_count=2, backup_report=report)


def test_an_alias_spelling_of_the_same_file_is_accepted(library, tmp_path):
    db, ids = library
    plan_path, document, report = _plan_and_backup(tmp_path, db)
    (tmp_path / "sub").mkdir()
    alias = Path(str(tmp_path / "sub" / ".." / db.name).upper())
    result = rc.apply_plan(alias, plan_path,
                           expect_digest=document["plan_digest"],
                           expect_count=2, backup_report=report)
    assert result["relabelled"] == [ids["stable"], ids["stable2"]]


def test_a_filesystem_without_file_identity_is_refused(
    library, monkeypatch
) -> None:
    db, _ = library
    real = os.stat

    def no_ino(path, *args, **kwargs):
        stat = real(path, *args, **kwargs)
        return os.stat_result((stat.st_mode, 0, *tuple(stat)[2:]))

    monkeypatch.setattr(rc, "_stat", no_ino)
    with pytest.raises(rc.PlanRefusedError, match="no file identity"):
        rc.make_plan(db)


def test_the_target_is_rechecked_under_the_write_lock(
    library, tmp_path, monkeypatch
) -> None:
    """The file is swapped after verification and before BEGIN IMMEDIATE."""
    db, _ = library
    plan_path, document, report = _plan_and_backup(tmp_path, db)
    real = rc.verify_backup

    def then_swap(database, body, backup_report):
        verified = real(database, body, backup_report)
        os.replace(_clone(db, tmp_path / "swap.db"), db)
        return verified

    monkeypatch.setattr(rc, "verify_backup", then_swap)
    with pytest.raises(rc.PlanRefusedError, match="different database"):
        _apply(tmp_path, db, plan_path, document, backup_report=report)
    assert {_version(db, i) for i in (1, 2)} == {"1"}


def test_postflight_with_a_plan_refuses_another_database(library, tmp_path):
    db, _ = library
    _, document = _write_plan(tmp_path, db)
    clone = _clone(db, tmp_path / "clone.db")
    with pytest.raises(rc.PlanRefusedError, match="different database"):
        rc.postflight(clone, document["plan"])


# --- backup artifact ------------------------------------------------------


def _edit_report(report: Path, **changes) -> Path:
    data = _report(report)
    data.update(changes)
    report.write_text(json.dumps(data), encoding="utf-8")
    return report


def _refused(tmp_path, db, plan_path, document, report, match):
    before = _dump(db)
    with pytest.raises(rc.PlanRefusedError, match=match):
        _apply(tmp_path, db, plan_path, document, backup_report=report)
    assert _dump(db) == before


def test_a_report_without_any_backup_artifact_is_refused(library, tmp_path):
    """Codex's probe: two fields and no backup ever created."""
    db, _ = library
    plan_path, document = _write_plan(tmp_path, db)
    report = tmp_path / "fake.json"
    report.write_text(json.dumps(
        {"verified": True, "source_database": str(db)}), encoding="utf-8")
    _refused(tmp_path, db, plan_path, document, report, "Unreadable backup")


def test_a_missing_report_is_refused(library, tmp_path):
    db, _ = library
    plan_path, document = _write_plan(tmp_path, db)
    _refused(tmp_path, db, plan_path, document, tmp_path / "none.json",
             "Unreadable backup")


def test_a_moved_backup_artifact_is_refused(library, tmp_path):
    """Codex's probe: a genuine report whose backup was moved away."""
    db, _ = library
    plan_path, document, report = _plan_and_backup(tmp_path, db)
    backup = Path(_report(report)["backup_database"])
    os.replace(backup, tmp_path / "elsewhere.db")
    _refused(tmp_path, db, plan_path, document, report, "does not exist")


def test_a_backup_replaced_after_verification_is_refused_under_the_lock(
    library, tmp_path, monkeypatch
) -> None:
    """The replacement check: the backup path now names another file.

    Simulated through `rc._stat`, because on Windows the held read-only
    connection keeps the real file from being moved at all.
    """
    db, ids = library
    plan_path, document, report = _plan_and_backup(tmp_path, db)
    backup = os.path.normcase(os.path.realpath(
        _report(report)["backup_database"]))
    real_verify, real_stat = rc.verify_backup, os.stat
    replaced = {"yes": False}

    def stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if replaced["yes"] and os.path.normcase(
                os.path.realpath(str(path))) == backup:
            # Another file at the same path: a new st_ino, all else equal.
            return types.SimpleNamespace(
                st_dev=result.st_dev, st_ino=result.st_ino + 1,
                st_size=result.st_size, st_mtime_ns=result.st_mtime_ns)
        return result

    def then_replace(database, body, backup_report):
        window = real_verify(database, body, backup_report)
        replaced["yes"] = True
        return window

    monkeypatch.setattr(rc, "_stat", stat)
    monkeypatch.setattr(rc, "verify_backup", then_replace)
    _refused(tmp_path, db, plan_path, document, report,
             "moved or was replaced after the live write lock")
    assert {_version(db, ids["stable"]), _version(db, ids["stable2"])} == {"1"}


@pytest.mark.parametrize("field", ["verified", "checks"])
def test_an_unverified_report_is_refused(library, tmp_path, field):
    db, _ = library
    plan_path, document, report = _plan_and_backup(tmp_path, db)
    if field == "verified":
        _edit_report(report, verified=False)
    else:
        checks = _report(report)["checks"]
        checks[next(iter(checks))] = False
        _edit_report(report, checks=checks)
    _refused(tmp_path, db, plan_path, document, report, "not verified")


def test_a_backup_of_another_database_is_refused(library, tmp_path):
    db, _ = library
    plan_path, document = _write_plan(tmp_path, db)
    other = _clone(db, tmp_path / "other.db")
    _refused(tmp_path, db, plan_path, document, _backup(tmp_path, other),
             "not the plan target")


@pytest.mark.parametrize("alias", ["same", "spelling", "hardlink", "wal"])
def test_the_live_database_cannot_pose_as_its_own_backup(
    library, tmp_path, alias
) -> None:
    db, _ = library
    plan_path, document, report = _plan_and_backup(tmp_path, db)
    if alias == "same":
        fake = db
    elif alias == "spelling":
        (tmp_path / "s").mkdir()
        fake = Path(str(tmp_path / "s" / ".." / db.name).upper())
    elif alias == "hardlink":
        fake = tmp_path / "hard.db"
        os.link(db, fake)
    else:
        fake = Path(str(db) + "-wal")

    def point_report_at_fake() -> None:
        stat = os.stat(fake)
        _edit_report(report, backup_database=str(fake),
                     backup_size_bytes=stat.st_size,
                     backup_modified_time_ns=stat.st_mtime_ns)

    if alias != "wal":
        point_report_at_fake()
        _refused(tmp_path, db, plan_path, document, report, "live database")
        return
    # The WAL exists only while a connection holds the database open, so the
    # refusal is checked with one open (the last close would remove it).
    with database_connection(db) as holder:
        holder.execute("PRAGMA user_version")
        assert fake.is_file()
        point_report_at_fake()
        with pytest.raises(rc.PlanRefusedError, match="live database"):
            _apply(tmp_path, db, plan_path, document, backup_report=report)
    assert _version(db, 1) == "1"


def test_a_backup_file_changed_after_its_report_is_refused(library, tmp_path):
    db, ids = library
    plan_path, document, report = _plan_and_backup(tmp_path, db)
    with database_connection(Path(_report(report)["backup_database"])) as c:
        c.execute("INSERT INTO archive_files (file_size) VALUES (1)")
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    _refused(tmp_path, db, plan_path, document, report,
             "not the file the report verified")


def test_a_backup_whose_rows_differ_from_its_report_is_refused(
    library, tmp_path
) -> None:
    db, _ = library
    plan_path, document, report = _plan_and_backup(tmp_path, db)
    counts = _report(report)["backup_table_counts"]
    counts["archive_files"] += 1
    _edit_report(report, backup_table_counts=counts)
    _refused(tmp_path, db, plan_path, document, report, "row counts")


def test_a_backup_taken_before_the_plan_is_stale(library, tmp_path):
    db, _ = library
    report = _backup(tmp_path, db)
    plan_path, document = _write_plan(tmp_path, db)
    _refused(tmp_path, db, plan_path, document, report, "predates the plan")


def test_a_fresh_backup_of_another_state_is_not_the_preimage(
    library, tmp_path
) -> None:
    """Taken after the plan, but the rows it holds are not the planned ones."""
    db, ids = library
    plan_path, document = _write_plan(tmp_path, db)
    with database_connection(db) as connection:
        connection.execute("UPDATE archive_pages SET entry_name = '3.png' "
                           "WHERE archive_id = ? AND entry_name = '2.png'",
                           (ids["stable"],))
    _refused(tmp_path, db, plan_path, document, _backup(tmp_path, db),
             "not this plan's preimage")


def test_a_backup_edited_in_place_is_not_the_preimage(library, tmp_path):
    """Size and mtime restored to the report's: only content betrays it."""
    db, ids = library
    plan_path, document, report = _plan_and_backup(tmp_path, db)
    data = _report(report)
    backup = Path(data["backup_database"])
    with database_connection(backup) as c:
        c.execute("UPDATE archive_content_signatures SET algorithm_version"
                  " = '2' WHERE archive_id = ?", (ids["stable"],))
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert backup.stat().st_size == data["backup_size_bytes"]
    os.utime(backup, ns=(data["backup_modified_time_ns"],
                         data["backup_modified_time_ns"]))
    _refused(tmp_path, db, plan_path, document, report,
             "not this plan's preimage")


def test_a_corrupt_backup_is_refused(library, tmp_path):
    db, _ = library
    plan_path, document, report = _plan_and_backup(tmp_path, db)
    data = _report(report)
    backup = Path(data["backup_database"])
    raw = bytearray(backup.read_bytes())
    # Everything after the header page: same size, unreadable b-trees.
    raw[4096:] = b"\xff" * (len(raw) - 4096)
    backup.write_bytes(bytes(raw))
    os.utime(backup, ns=(data["backup_modified_time_ns"],
                         data["backup_modified_time_ns"]))
    _refused(tmp_path, db, plan_path, document, report,
             "failed (verification|quick_check)")


def _on_backup(connection, backup: str) -> bool:
    return Path(connection.execute("PRAGMA database_list").fetchone()[2]
                ).name == Path(backup).name


def test_a_backup_failing_quick_check_is_refused(
    library, tmp_path, monkeypatch
) -> None:
    """quick_check's answer is enforced even when every later read works."""
    db, _ = library
    plan_path, document, report = _plan_and_backup(tmp_path, db)
    backup = _report(report)["backup_database"]
    real = rc.quick_check
    monkeypatch.setattr(
        rc, "quick_check",
        lambda c: "row 1 missing from index x" if _on_backup(c, backup)
        else real(c))
    _refused(tmp_path, db, plan_path, document, report, "failed quick_check")


def test_a_backup_read_error_is_a_refusal(library, tmp_path, monkeypatch):
    """Any SQLite error while reading the backup refuses, never escapes raw."""
    db, _ = library
    plan_path, document, report = _plan_and_backup(tmp_path, db)
    backup = _report(report)["backup_database"]
    real = rc.classify_legacy

    def failing(connection):
        if _on_backup(connection, backup):
            raise sqlite3.DatabaseError("simulated read failure")
        return real(connection)

    monkeypatch.setattr(rc, "classify_legacy", failing)
    _refused(tmp_path, db, plan_path, document, report,
             "failed verification: simulated read failure")


def test_a_backup_touched_during_verification_is_refused(
    library, tmp_path, monkeypatch
) -> None:
    """The replacement fingerprint moved inside the verification read."""
    db, _ = library
    plan_path, document, report = _plan_and_backup(tmp_path, db)
    backup = _report(report)["backup_database"]
    real = rc.classify_legacy

    def touching(connection):
        result = real(connection)
        if Path(connection.execute("PRAGMA database_list").fetchone()[2]
                ).name == Path(backup).name:
            os.utime(backup, ns=(time.time_ns(), time.time_ns()))
        return result

    monkeypatch.setattr(rc, "classify_legacy", touching)
    _refused(tmp_path, db, plan_path, document, report,
             "replaced during verification")


# --- the backup window: WAL-only commits ----------------------------------


@pytest.fixture()
def tracked_connections(monkeypatch):
    """Every connection the tool opens, so a test can prove each was closed."""
    opened = []
    real = rc.open_connection

    def tracking(*args, **kwargs):
        connection = real(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(rc, "open_connection", tracking)
    return opened


def _all_closed(connections) -> bool:
    for connection in connections:
        try:
            connection.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            continue
        return False
    return True


def _wal_holder(backup: Path) -> sqlite3.Connection:
    """A writer on the backup whose commits stay in -wal (Codex's probe).

    Auto-checkpoint is off, so the main file's size and mtime do not move:
    only data_version can see the commit.
    """
    holder = sqlite3.connect(backup, isolation_level=None)
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("PRAGMA wal_autocheckpoint=0")
    return holder


def _delete_planned_from_backup(holder, ids) -> tuple[dict, dict, int]:
    backup = holder.execute("PRAGMA database_list").fetchone()[2]
    before = rc._backup_fingerprint(backup)
    holder.execute("DELETE FROM archive_content_signatures "
                   "WHERE archive_id IN (?, ?)",
                   (ids["stable"], ids["stable2"]))
    return before, rc._backup_fingerprint(backup), os.stat(
        backup + "-wal").st_size


def test_a_wal_only_backup_commit_before_the_live_lock_refuses_apply(
    library, tmp_path, monkeypatch, tracked_connections
) -> None:
    """(a) Committed after verify_backup returns, before BEGIN IMMEDIATE.

    The reviewed counterexample: the main-file fingerprint is unchanged,
    so only the held connection's data_version can refuse it.
    """
    db, ids = library
    plan_path, document, report = _plan_and_backup(tmp_path, db)
    holder = _wal_holder(Path(_report(report)["backup_database"]))
    real = rc.verify_backup
    seen = {}

    def then_commit(database, body, backup_report):
        window = real(database, body, backup_report)
        seen["fingerprints"] = _delete_planned_from_backup(holder, ids)
        return window

    monkeypatch.setattr(rc, "verify_backup", then_commit)
    try:
        with pytest.raises(rc.PlanRefusedError,
                           match="committed to after the live write lock"):
            _apply(tmp_path, db, plan_path, document, backup_report=report)
    finally:
        holder.close()

    before, after, wal_bytes = seen["fingerprints"]
    assert before == after and wal_bytes > 0  # precondition: WAL-only
    assert {_version(db, ids["stable"]), _version(db, ids["stable2"])} == {"1"}
    assert len(tracked_connections) == 2
    assert _all_closed(tracked_connections)


def test_a_wal_only_backup_commit_while_rows_are_processed_rolls_back(
    library, tmp_path, monkeypatch, tracked_connections
) -> None:
    """(b) Committed after the first planned row was re-classified.

    The first relabel UPDATE has already executed inside the live
    transaction when the final check fires, so this proves rollback of
    every relabel, not just a refusal before any.
    """
    db, ids = library
    plan_path, document, report = _plan_and_backup(tmp_path, db)
    holder = _wal_holder(Path(_report(report)["backup_database"]))
    real = rc.classify_archive
    calls = []

    def committing(connection, archive_id):
        found = real(connection, archive_id)
        calls.append(archive_id)
        if len(calls) == 1:
            _delete_planned_from_backup(holder, ids)
        return found

    monkeypatch.setattr(rc, "classify_archive", committing)
    try:
        with pytest.raises(rc.PlanRefusedError,
                           match="committed to while planned rows"):
            _apply(tmp_path, db, plan_path, document, backup_report=report)
    finally:
        holder.close()

    assert calls == [ids["stable"], ids["stable2"]]
    assert {_version(db, ids["stable"]), _version(db, ids["stable2"])} == {"1"}
    assert len(tracked_connections) == 2
    assert _all_closed(tracked_connections)


def test_a_wal_only_backup_commit_during_verification_is_refused(
    library, tmp_path, monkeypatch, tracked_connections
) -> None:
    """Committed inside the preimage read: caught by the first check."""
    db, ids = library
    plan_path, document, report = _plan_and_backup(tmp_path, db)
    backup = Path(_report(report)["backup_database"])
    holder = _wal_holder(backup)
    real = rc.classify_legacy

    def committing(connection):
        result = real(connection)
        if Path(connection.execute("PRAGMA database_list").fetchone()[2]
                ).name == backup.name:
            _delete_planned_from_backup(holder, ids)
        return result

    monkeypatch.setattr(rc, "classify_legacy", committing)
    try:
        with pytest.raises(rc.PlanRefusedError,
                           match="committed to during verification"):
            _apply(tmp_path, db, plan_path, document, backup_report=report)
    finally:
        holder.close()
    assert _version(db, ids["stable"]) == "1"
    assert _all_closed(tracked_connections)


def test_a_normal_apply_and_its_rerun_close_every_connection(
    library, tmp_path, tracked_connections
) -> None:
    db, ids = library
    plan_path, document, report = _plan_and_backup(tmp_path, db)
    first = _apply(tmp_path, db, plan_path, document, backup_report=report)
    second = _apply(tmp_path, db, plan_path, document, backup_report=report)
    assert first["relabelled"] == [ids["stable"], ids["stable2"]]
    assert second["already_recertified"] == [ids["stable"], ids["stable2"]]
    assert set(first["backup"]) >= {"path", "data_version", "st_ino"}
    assert len(tracked_connections) == 4
    assert _all_closed(tracked_connections)


# --- per-row drift --------------------------------------------------------


@pytest.mark.parametrize(
    "drift, reason",
    [
        ("UPDATE archive_pages SET entry_name = '3.png' "
         "WHERE archive_id = :a AND entry_name = '2.png'", "basis_changed"),
        ("UPDATE file_locations SET file_size = file_size + 7 "
         "WHERE archive_id = :a", "now_stale:source_metadata_differs"),
        ("UPDATE archive_content_signatures SET source_modified_time_ns = 5 "
         "WHERE archive_id = :a", None),
        ("UPDATE archive_pages SET entry_name = '1.png' "
         "WHERE archive_id = :a AND entry_name = '2.png'",
         "now_unresolved:repeated_entry_name"),
        ("UPDATE page_hashes SET digest = '00' || substr(digest, 3) "
         "WHERE algorithm = 'sha256' AND page_id IN (SELECT id FROM "
         "archive_pages WHERE archive_id = :a)",
         "now_inconsistent:digest_not_reproducible"),
        ("UPDATE archive_content_signatures SET algorithm_version = '2', "
         "digest = '00' || substr(digest, 3) WHERE archive_id = :a",
         "now_inconsistent:digest_not_reproducible"),
        ("UPDATE archive_content_signatures SET algorithm = 'unknown' "
         "WHERE archive_id = :a", "algorithm_unknown"),
        ("UPDATE archive_content_signatures SET algorithm_version = '7' "
         "WHERE archive_id = :a", "version_7"),
        ("DELETE FROM archive_content_signatures WHERE archive_id = :a",
         "missing"),
    ],
)
def test_a_row_that_drifted_after_the_backup_is_skipped_not_relabelled(
    library, tmp_path, drift, reason
) -> None:
    """Re-verified per row inside the write transaction; others proceed."""
    db, ids = library
    plan_path, document, report = _plan_and_backup(tmp_path, db)
    with database_connection(db) as connection:
        connection.execute(drift, {"a": ids["stable"]})
        if reason is None:
            # Signature and location moved together: still bound, but not
            # to the metadata the plan certified.
            connection.execute(
                "UPDATE file_locations SET modified_time_ns = 5 "
                "WHERE archive_id = ?", (ids["stable"],))
            reason = "basis_changed"
    stored_before = _rows(db, "SELECT * FROM archive_content_signatures "
                              "WHERE archive_id = ?", (ids["stable"],))
    result = _apply(tmp_path, db, plan_path, document, backup_report=report)

    assert result["skipped"] == [{"archive_id": ids["stable"],
                                  "reason": reason}]
    assert result["relabelled"] == [ids["stable2"]]
    assert _rows(db, "SELECT * FROM archive_content_signatures "
                     "WHERE archive_id = ?", (ids["stable"],)) == stored_before
    post = result["postflight"]
    assert post["passed"] is False
    assert post["planned_failures"] == [{"archive_id": ids["stable"],
                                         "outcome": reason}]


def test_a_relabel_that_touches_no_row_rolls_the_whole_apply_back(
    library, tmp_path, monkeypatch
) -> None:
    """Fault injection: the UPDATE's own predicate is the last line.

    The re-check is made to report a row as legacy when it is already "2",
    so the UPDATE matches nothing; the apply must fail and the earlier
    relabel in the same transaction must not survive.
    """
    db, ids = library
    plan_path, document, report = _plan_and_backup(tmp_path, db)
    with database_connection(db) as connection:
        connection.execute("UPDATE archive_content_signatures SET "
                           "algorithm_version = '2' WHERE archive_id = ?",
                           (ids["stable2"],))
    real = rc.classify_archive

    def lying(connection, archive_id):
        found = real(connection, archive_id)
        return rc.Classification(found.archive_id, found.algorithm, "1",
                                 found.cls, found.reason, found.basis,
                                 found.detail, found.diagnostics)

    monkeypatch.setattr(rc, "classify_archive", lying)
    with pytest.raises(RuntimeError, match="touched 0 rows"):
        _apply(tmp_path, db, plan_path, document, backup_report=report)
    assert _version(db, ids["stable"]) == "1"


# --- postflight -----------------------------------------------------------


def test_postflight_names_its_populations_and_passes_after_apply(
    library, tmp_path
) -> None:
    db, ids = library
    plan_path, document = _write_plan(tmp_path, db)

    before = rc.postflight(db, document["plan"])
    assert before["passed"] is False
    assert before["planned_order_stable_still_legacy"] == [
        ids["stable"], ids["stable2"]]
    assert before["planned_outcome_counts"] == {"legacy_as_planned": 2}

    after = _apply(tmp_path, db, plan_path, document)["postflight"]
    assert after["passed"] is True
    assert after["planned_outcomes"] == [
        {"archive_id": ids["stable"], "outcome": "recertified"},
        {"archive_id": ids["stable2"], "outcome": "recertified"}]
    assert after["planned_failures"] == []
    # All rows, any location state: two relabelled, three still legacy.
    assert after["signature_evidence_counts"] == {
        "comparable": 2, "legacy_ordering_version": 3,
        "unknown_algorithm": 0, "unresolved_page_order": 0,
    }
    # Step 4's population remains legacy and is counted, not failed.
    assert after["legacy_rows_by_class"] == {
        "stale": 1, "unresolved": 1, "tied": 1, "inconsistent": 0,
        "order_stable": 0,
    }
    with database_connection(db) as connection:
        assert signature_evidence_counts(connection) == after[
            "signature_evidence_counts"]


@pytest.mark.parametrize(
    "mutation, outcome",
    [
        # Codex's probe: both planned rows moved to an unknown algorithm.
        ("UPDATE archive_content_signatures SET algorithm = 'unknown' "
         "WHERE archive_id = :a", "algorithm_unknown"),
        ("UPDATE archive_content_signatures SET algorithm_version = '9' "
         "WHERE archive_id = :a", "version_9"),
        ("DELETE FROM archive_content_signatures WHERE archive_id = :a",
         "missing"),
        ("UPDATE archive_pages SET entry_name = '3.png' "
         "WHERE archive_id = :a AND entry_name = '2.png'", "basis_changed"),
        ("UPDATE archive_content_signatures SET algorithm_version = '1' "
         "WHERE archive_id = :a", "legacy_as_planned"),
    ],
)
def test_planned_postflight_names_and_fails_every_unexpected_outcome(
    library, tmp_path, mutation, outcome
) -> None:
    """After a successful apply, any later change to a planned row fails."""
    db, ids = library
    plan_path, document = _write_plan(tmp_path, db)
    _apply(tmp_path, db, plan_path, document)
    with database_connection(db) as connection:
        connection.execute(mutation, {"a": ids["stable"]})
    report = rc.postflight(db, document["plan"])
    assert report["passed"] is False
    assert report["planned_failures"] == [{"archive_id": ids["stable"],
                                           "outcome": outcome}]
    assert rc.main(["postflight", "--database", str(db), "--plan",
                    str(plan_path)]) == rc.EXIT_INCOMPLETE


def test_planned_postflight_fails_when_every_planned_row_went_unknown(
    library, tmp_path
) -> None:
    """The exact reviewed counterexample, before any apply."""
    db, ids = library
    plan_path, document = _write_plan(tmp_path, db)
    with database_connection(db) as connection:
        connection.execute("UPDATE archive_content_signatures SET algorithm "
                           "= 'unknown' WHERE archive_id IN (?, ?)",
                           (ids["stable"], ids["stable2"]))
    assert rc.main(["postflight", "--database", str(db), "--plan",
                    str(plan_path)]) == rc.EXIT_INCOMPLETE


def test_planned_postflight_refuses_an_edited_plan(library, tmp_path):
    db, _ = library
    plan_path, document = _write_plan(tmp_path, db)
    document["plan"]["order_stable"] = []
    plan_path.write_text(json.dumps(document), encoding="utf-8")
    assert rc.main(["postflight", "--database", str(db), "--plan",
                    str(plan_path)]) == rc.EXIT_REFUSED


def test_postflight_without_a_plan_uses_the_current_location_scope(library):
    """Order-stable legacy rows still present fail it even with no plan."""
    db, _ = library
    report = rc.postflight(db)
    assert report["legacy_rows_by_class"]["order_stable"] == 2
    assert report["passed"] is False


# --- CLI ------------------------------------------------------------------


def test_cli_plan_apply_postflight_round_trip(library, tmp_path):
    db, _ = library
    plan = tmp_path / "cli-plan.json"
    assert rc.main(["plan", "--database", str(db), "--json-out",
                    str(plan)]) == rc.EXIT_OK
    document = json.loads(plan.read_text(encoding="utf-8"))
    backup = _backup(tmp_path, db)
    apply_args = ["apply", "--database", str(db), "--plan", str(plan),
                  "--expect-digest", document["plan_digest"],
                  "--backup-report", str(backup)]

    assert rc.main(apply_args + ["--expect-count", "1"]) == rc.EXIT_REFUSED
    assert rc.main(apply_args + ["--expect-count", "2"]) == rc.EXIT_OK
    assert rc.main(apply_args + ["--expect-count", "2"]) == rc.EXIT_OK
    assert rc.main(["postflight", "--database", str(db), "--plan",
                    str(plan)]) == rc.EXIT_OK


def test_cli_refuses_an_output_over_an_input_or_an_existing_file(
    library, tmp_path
) -> None:
    db, _ = library
    before = db.read_bytes()
    for target in (db, Path(str(db) + "-wal")):
        assert rc.main(["plan", "--database", str(db), "--json-out",
                        str(target)]) == rc.EXIT_REFUSED
    existing = tmp_path / "exists.json"
    existing.write_text("keep", encoding="utf-8")
    assert rc.main(["plan", "--database", str(db), "--json-out",
                    str(existing)]) == rc.EXIT_REFUSED
    assert existing.read_text(encoding="utf-8") == "keep"
    assert db.read_bytes() == before


def test_cli_exits_incomplete_when_apply_skips_a_row(library, tmp_path):
    db, ids = library
    plan = tmp_path / "cli-plan.json"
    rc.main(["plan", "--database", str(db), "--json-out", str(plan)])
    document = json.loads(plan.read_text(encoding="utf-8"))
    report = _backup(tmp_path, db)
    with database_connection(db) as connection:
        connection.execute("UPDATE file_locations SET file_size = 1 "
                           "WHERE archive_id = ?", (ids["stable"],))
    assert rc.main([
        "apply", "--database", str(db), "--plan", str(plan),
        "--expect-digest", document["plan_digest"], "--expect-count", "2",
        "--backup-report", str(report),
    ]) == rc.EXIT_INCOMPLETE
