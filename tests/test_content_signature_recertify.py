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
import sqlite3
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


def _backup(tmp_path: Path, db: Path) -> Path:
    report = tmp_path / "backup.json"
    run_backup(database=db, backup=tmp_path / "backup.db", json_output=report)
    return report


def _write_plan(tmp_path: Path, db: Path) -> tuple[Path, dict]:
    document = rc.make_plan(db)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path, document


def _apply(tmp_path, db, plan_path, document, **overrides):
    arguments = dict(
        expect_digest=document["plan_digest"],
        expect_count=document["plan"]["counts"]["order_stable"],
        backup_report=_backup(tmp_path, db)
        if "backup_report" not in overrides else overrides.pop("backup_report"),
    )
    arguments.update(overrides)
    return rc.apply_plan(db, plan_path, **arguments)


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


def test_the_plan_digest_is_over_canonical_bytes_and_repeatable(
    library,
) -> None:
    db, _ = library
    first, second = rc.make_plan(db), rc.make_plan(db)
    assert first["plan_digest"] == second["plan_digest"]
    assert first["plan_digest"] == hashlib.sha256(json.dumps(
        first["plan"], sort_keys=True, separators=(",", ":"),
        ensure_ascii=True).encode("ascii")).hexdigest()
    assert "data_version_before" not in first["plan"]


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
    """Positive control: after relabel, the gated reader groups it.

    The same pages stored fresh under "2" and relabelled from "1" must
    form one duplicate group -- the relabel produced canonical evidence.
    """
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


def test_apply_twice_is_idempotent(library, tmp_path):
    db, ids = library
    plan_path, document = _write_plan(tmp_path, db)
    _apply(tmp_path, db, plan_path, document)
    after_first = _dump(db)

    second = rc.apply_plan(
        db, plan_path, expect_digest=document["plan_digest"],
        expect_count=2, backup_report=tmp_path / "backup.json",
    )
    assert second["relabelled"] == []
    assert second["already_recertified"] == [ids["stable"], ids["stable2"]]
    assert second["skipped"] == []
    assert _dump(db) == after_first


@pytest.mark.parametrize(
    "change",
    [
        {"expect_digest": "0" * 64},
        {"expect_count": 1},
        {"expect_count": 3},
    ],
)
def test_apply_refuses_entirely_on_digest_or_count_mismatch(
    library, tmp_path, change
) -> None:
    db, _ = library
    plan_path, document = _write_plan(tmp_path, db)
    backup = _backup(tmp_path, db)
    before = _dump(db)
    with pytest.raises(rc.PlanRefusedError):
        _apply(tmp_path, db, plan_path, document, backup_report=backup,
               **change)
    assert _dump(db) == before


def test_apply_refuses_a_plan_edited_after_it_was_written(library, tmp_path):
    """The digest is recomputed from the body, not read from the file."""
    db, ids = library
    plan_path, document = _write_plan(tmp_path, db)
    document["plan"]["order_stable"].append(
        dict(document["plan"]["order_stable"][0], archive_id=ids["tied"]))
    document["plan"]["counts"]["order_stable"] = 3
    plan_path.write_text(json.dumps(document), encoding="utf-8")
    before = _dump(db)
    with pytest.raises(rc.PlanRefusedError, match="edited"):
        _apply(tmp_path, db, plan_path, document, expect_count=3)
    assert _dump(db) == before


def test_apply_refuses_a_plan_for_another_version_pair(library, tmp_path):
    db, _ = library
    plan_path, document = _write_plan(tmp_path, db)
    document["plan"]["to_version"] = "3"
    document["plan_digest"] = rc.plan_digest(document["plan"])
    plan_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(rc.PlanRefusedError, match="version"):
        _apply(tmp_path, db, plan_path, document,
               expect_digest=document["plan_digest"])


@pytest.mark.parametrize("problem", ["missing", "unverified", "other_db"])
def test_apply_refuses_without_a_verified_backup_of_this_database(
    library, tmp_path, problem
) -> None:
    db, _ = library
    plan_path, document = _write_plan(tmp_path, db)
    report = _backup(tmp_path, db)
    if problem == "missing":
        report = tmp_path / "no-such-report.json"
    else:
        data = json.loads(report.read_text(encoding="utf-8"))
        if problem == "unverified":
            data["verified"] = False
        else:
            data["source_database"] = str(tmp_path / "other.db")
        report.write_text(json.dumps(data), encoding="utf-8")
    before = _dump(db)
    with pytest.raises(rc.PlanRefusedError):
        _apply(tmp_path, db, plan_path, document, backup_report=report)
    assert _dump(db) == before


@pytest.mark.parametrize(
    "drift, reason",
    [
        # A renamed page: still untied, digest still reproducible by name,
        # so only the recorded names digest betrays it.
        ("UPDATE archive_pages SET entry_name = '3.png' "
         "WHERE archive_id = :a AND entry_name = '2.png'", "basis_changed"),
        # The current location's recorded metadata moved: now stale.
        ("UPDATE file_locations SET file_size = file_size + 7 "
         "WHERE archive_id = :a", "now_stale:source_metadata_differs"),
        # Signature and location moved together: still bound, but not to
        # the metadata the plan certified.
        ("UPDATE archive_content_signatures SET source_modified_time_ns = 5 "
         "WHERE archive_id = :a", None),
        # A repeat introduced after planning.
        ("UPDATE archive_pages SET entry_name = '1.png' "
         "WHERE archive_id = :a AND entry_name = '2.png'",
         "now_unresolved:repeated_entry_name"),
        # The page sha256 rows no longer reproduce the digest.
        ("UPDATE page_hashes SET digest = '00' || substr(digest, 3) "
         "WHERE algorithm = 'sha256' AND page_id IN (SELECT id FROM "
         "archive_pages WHERE archive_id = :a)",
         "now_inconsistent:digest_not_reproducible"),
        # Re-inventoried (or relabelled elsewhere) under another digest.
        ("UPDATE archive_content_signatures SET algorithm_version = '2', "
         "digest = '00' || substr(digest, 3) WHERE archive_id = :a",
         "now_inconsistent:digest_not_reproducible"),
        ("DELETE FROM archive_content_signatures WHERE archive_id = :a",
         "signature_missing"),
    ],
)
def test_a_row_that_drifted_after_planning_is_skipped_not_relabelled(
    library, tmp_path, drift, reason
) -> None:
    """Re-verified per row inside the write transaction; others proceed."""
    db, ids = library
    plan_path, document = _write_plan(tmp_path, db)
    with database_connection(db) as connection:
        connection.execute(drift, {"a": ids["stable"]})
        if reason is None:
            connection.execute(
                "UPDATE file_locations SET modified_time_ns = 5 "
                "WHERE archive_id = ?", (ids["stable"],))
            reason = "basis_changed"
    version_before = _rows(db, "SELECT algorithm_version FROM "
                               "archive_content_signatures WHERE archive_id"
                               " = ?", (ids["stable"],))
    result = _apply(tmp_path, db, plan_path, document)

    assert result["skipped"] == [{"archive_id": ids["stable"],
                                  "reason": reason}]
    assert result["relabelled"] == [ids["stable2"]]
    assert _rows(db, "SELECT algorithm_version FROM archive_content_signatures"
                     " WHERE archive_id = ?", (ids["stable"],)) == (
        version_before)


def test_a_row_at_an_unknown_version_with_the_planned_basis_is_skipped(
    library, tmp_path
) -> None:
    """Neither legacy nor current: not this tool's to relabel."""
    db, ids = library
    plan_path, document = _write_plan(tmp_path, db)
    with database_connection(db) as connection:
        connection.execute("UPDATE archive_content_signatures SET "
                           "algorithm_version = '7' WHERE archive_id = ?",
                           (ids["stable"],))
    result = _apply(tmp_path, db, plan_path, document)
    assert result["skipped"] == [{"archive_id": ids["stable"],
                                  "reason": "version_7"}]
    assert _version(db, ids["stable"]) == "7"


def test_apply_refuses_an_unreadable_plan(library, tmp_path) -> None:
    db, _ = library
    plan_path = tmp_path / "plan.json"
    plan_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(rc.PlanRefusedError, match="Unreadable plan"):
        rc.apply_plan(db, plan_path, expect_digest="0" * 64, expect_count=0,
                      backup_report=_backup(tmp_path, db))


def test_a_relabel_that_touches_no_row_rolls_the_whole_apply_back(
    library, tmp_path, monkeypatch
) -> None:
    """Fault injection: the UPDATE's own predicate is the last line.

    The re-check is made to report a row as legacy when it is already "2",
    so the UPDATE matches nothing; the apply must fail and the earlier
    relabel in the same transaction must not survive.
    """
    db, ids = library
    plan_path, document = _write_plan(tmp_path, db)
    with database_connection(db) as connection:
        connection.execute("UPDATE archive_content_signatures SET "
                           "algorithm_version = '2' WHERE archive_id = ?",
                           (ids["stable2"],))
    real = rc.classify_archive

    def lying(connection, archive_id):
        found = real(connection, archive_id)
        return rc.Classification(found.archive_id, "1", found.cls,
                                 found.reason, found.basis, found.detail)

    monkeypatch.setattr(rc, "classify_archive", lying)
    with pytest.raises(RuntimeError, match="touched 0 rows"):
        _apply(tmp_path, db, plan_path, document)
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

    result = _apply(tmp_path, db, plan_path, document)
    after = result["postflight"]
    assert after["passed"] is True
    assert after["planned_order_stable_still_legacy"] == []
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


def test_postflight_fails_while_a_skipped_planned_row_is_still_legacy(
    library, tmp_path
) -> None:
    db, ids = library
    plan_path, document = _write_plan(tmp_path, db)
    with database_connection(db) as connection:
        connection.execute("UPDATE archive_pages SET entry_name = '3.png' "
                           "WHERE archive_id = ? AND entry_name = '2.png'",
                           (ids["stable"],))
    after = _apply(tmp_path, db, plan_path, document)["postflight"]
    assert after["planned_order_stable_still_legacy"] == [ids["stable"]]
    assert after["legacy_rows_by_class"]["order_stable"] == 1
    assert after["passed"] is False


def test_postflight_fails_for_a_planned_row_that_drifted_into_another_class(
    library, tmp_path
) -> None:
    """Scoped counts alone would pass: the row is now stale, not stable.

    Only the per-plan check sees that a planned archive stayed legacy.
    """
    db, ids = library
    plan_path, document = _write_plan(tmp_path, db)
    with database_connection(db) as connection:
        connection.execute("UPDATE file_locations SET file_size = 1 "
                           "WHERE archive_id = ?", (ids["stable"],))
    after = _apply(tmp_path, db, plan_path, document)["postflight"]
    assert after["legacy_rows_by_class"]["order_stable"] == 0
    assert after["planned_order_stable_still_legacy"] == [ids["stable"]]
    assert after["passed"] is False


def test_postflight_without_a_plan_uses_the_current_location_scope(library):
    """Order-stable legacy rows still present fail it even with no plan."""
    db, _ = library
    report = rc.postflight(db)
    assert report["legacy_rows_by_class"]["order_stable"] == 2
    assert report["passed"] is False


# --- CLI ------------------------------------------------------------------


def test_cli_plan_apply_postflight_round_trip(library, tmp_path, capsys):
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
    with database_connection(db) as connection:
        connection.execute("UPDATE file_locations SET file_size = 1 "
                           "WHERE archive_id = ?", (ids["stable"],))
    assert rc.main([
        "apply", "--database", str(db), "--plan", str(plan),
        "--expect-digest", document["plan_digest"], "--expect-count", "2",
        "--backup-report", str(_backup(tmp_path, db)),
    ]) == rc.EXIT_INCOMPLETE
