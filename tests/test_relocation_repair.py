"""Tests for content-keyed location repair.

Archive identity in this codebase is established by path, which is right for
discovery and wrong for repair: a moved file rescanned becomes a second
archive identity, stranding the original's page inventory, hashes and job
history. These tests pin the repair that fixes it, and -- more importantly --
pin the conditions under which it refuses to act.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

import pytest

from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.library.relocation_repair import (
    apply_repairs,
    current_paths,
    find_broken_locations,
    index_roots,
    plan_repairs,
    sha256_file,
)


MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


def _write_archive(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def _seed(
    connection: sqlite3.Connection,
    *,
    path: Path,
    digest: str,
    size: int,
    mtime_ns: int,
    signature_size: int | None = None,
    signature_mtime_ns: int | None = None,
) -> int:
    """Seed one archive.

    ``signature_*`` default to the hash row's values, which is the coherent
    case. Passing different values models the incoherent one: an archive hash
    recomputed after a content change while the page signature stayed stale.
    """
    archive_id = int(
        connection.execute(
            "INSERT INTO archive_files (file_size) VALUES (?)", (size,)
        ).lastrowid
    )
    connection.execute(
        """
        INSERT INTO file_locations (
            archive_id, path, is_current, file_size, modified_time_ns
        )
        VALUES (?, ?, 1, ?, ?)
        """,
        (archive_id, str(path), size, mtime_ns),
    )
    connection.execute(
        """
        INSERT INTO archive_hashes (
            archive_id, algorithm, algorithm_version, digest, file_size,
            modified_time_ns, bytes_read
        )
        VALUES (?, 'sha256', '1', ?, ?, ?, ?)
        """,
        (archive_id, digest, size, mtime_ns, size),
    )
    connection.execute(
        """
        INSERT INTO archive_content_signatures (
            archive_id, algorithm, algorithm_version, digest, page_count,
            image_bytes, source_file_size, source_modified_time_ns
        )
        VALUES (?, 'ordered-page-sha256', '1', ?, 10, 1024, ?, ?)
        """,
        (
            archive_id,
            "sig-" + digest[:8],
            size if signature_size is None else signature_size,
            mtime_ns if signature_mtime_ns is None else signature_mtime_ns,
        ),
    )
    return archive_id


@pytest.fixture()
def database(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    with database_connection(path) as connection:
        apply_migrations(connection, MIGRATIONS)
    return path


def test_moved_file_is_found_by_content_and_repointed(database: Path, tmp_path: Path):
    """The 2026-08-17 relocation case: same bytes, different folder.

    Repair must preserve the archive id -- that identity carries the page
    inventory, hashes and job history a rescan would strand.
    """
    library = tmp_path / "lib"
    original = library / "Manga" / "Series" / "Ch1.cbz"
    payload = b"archive payload one"
    digest = _write_archive(original, payload)
    stat = original.stat()

    with database_connection(database) as connection:
        archive_id = _seed(
            connection,
            path=original,
            digest=digest,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
        )
        connection.commit()

    # Reorganise: same bytes, new home.
    moved = library / "Graphic Novels" / "Series" / "Ch1.cbz"
    moved.parent.mkdir(parents=True, exist_ok=True)
    original.rename(moved)

    with database_connection(database) as connection:
        connection.row_factory = sqlite3.Row
        broken = find_broken_locations(connection)
        assert [b.state for b in broken] == ["missing"]

        plan = plan_repairs(broken, index_roots([library]), known_paths=current_paths(connection))
        assert len(plan.repairs) == 1
        assert plan.repairs[0].kind == "moved"
        assert plan.repairs[0].new_path == str(moved)

        apply_repairs(connection, plan.repairs)
        connection.commit()

        rows = connection.execute(
            "SELECT path, is_current FROM file_locations WHERE archive_id = ? "
            "ORDER BY is_current",
            (archive_id,),
        ).fetchall()

    # Old row retired, not deleted: history survives for a later move.
    assert [(r["path"], r["is_current"]) for r in rows] == [
        (str(original), 0),
        (str(moved), 1),
    ]


def test_restored_file_with_new_mtime_is_repaired_in_place(
    database: Path, tmp_path: Path
):
    """The restore case: right path, identical bytes, new mtime.

    A restored copy is a new file object, so the eligibility predicate's
    source_modified_time_ns comparison fails until the recorded metadata is
    re-established against proven-identical content.
    """
    archive = tmp_path / "lib" / "a.cbz"
    payload = b"restored payload"
    digest = _write_archive(archive, payload)

    with database_connection(database) as connection:
        archive_id = _seed(
            connection,
            path=archive,
            digest=digest,
            size=len(payload),
            mtime_ns=111_111,  # deliberately not the real mtime
        )
        connection.commit()

    with database_connection(database) as connection:
        connection.row_factory = sqlite3.Row
        broken = find_broken_locations(connection)
        assert [b.state for b in broken] == ["metadata_drift"]

        plan = plan_repairs(broken, {}, known_paths=current_paths(connection))
        assert len(plan.repairs) == 1
        assert plan.repairs[0].kind == "metadata_drift"

        apply_repairs(connection, plan.repairs)
        connection.commit()

        location = connection.execute(
            "SELECT file_size, modified_time_ns FROM file_locations "
            "WHERE archive_id = ? AND is_current = 1",
            (archive_id,),
        ).fetchone()
        signature = connection.execute(
            "SELECT source_file_size, source_modified_time_ns "
            "FROM archive_content_signatures WHERE archive_id = ?",
            (archive_id,),
        ).fetchone()

    # Location and signature agree again, which is what eligibility requires.
    assert location["modified_time_ns"] == archive.stat().st_mtime_ns
    assert signature["source_modified_time_ns"] == location["modified_time_ns"]
    assert signature["source_file_size"] == location["file_size"]


def test_content_mismatch_is_never_repaired(database: Path, tmp_path: Path):
    """A file whose bytes changed is not the archive we recorded.

    Repairing it would fit fresh metadata around stale evidence -- exactly the
    failure this module's docstring warns about.
    """
    archive = tmp_path / "lib" / "a.cbz"
    _write_archive(archive, b"original bytes")

    with database_connection(database) as connection:
        _seed(
            connection,
            path=archive,
            digest=hashlib.sha256(b"original bytes").hexdigest(),
            size=len(b"original bytes"),
            mtime_ns=111_111,
        )
        connection.commit()

    archive.write_bytes(b"completely different content now")

    with database_connection(database) as connection:
        connection.row_factory = sqlite3.Row
        plan = plan_repairs(
            find_broken_locations(connection), {}, known_paths=current_paths(connection)
        )

    assert plan.repairs == []
    assert len(plan.unresolved) == 1
    assert "sha256 differs" in plan.unresolved[0]["reason"]


def test_multiple_matching_copies_are_ambiguous_not_guessed(
    database: Path, tmp_path: Path
):
    """Identical content at two paths is a duplicate question, not a move.

    Choosing one would silently decide which copy the library treats as
    canonical.
    """
    library = tmp_path / "lib"
    original = library / "orig" / "a.cbz"
    payload = b"duplicated payload"
    digest = _write_archive(original, payload)
    stat = original.stat()

    with database_connection(database) as connection:
        _seed(
            connection,
            path=original,
            digest=digest,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
        )
        connection.commit()

    original.unlink()
    _write_archive(library / "copyA" / "a.cbz", payload)
    _write_archive(library / "copyB" / "b.cbz", payload)

    with database_connection(database) as connection:
        connection.row_factory = sqlite3.Row
        plan = plan_repairs(
            find_broken_locations(connection),
            index_roots([library]),
            known_paths=current_paths(connection),
        )

    assert plan.repairs == []
    assert len(plan.ambiguous) == 1
    assert len(plan.ambiguous[0]["candidates"]) == 2


def test_a_file_already_claimed_by_another_archive_is_not_stolen(
    database: Path, tmp_path: Path
):
    """Repair must never point two archives at one file."""
    library = tmp_path / "lib"
    payload = b"shared payload"
    kept = library / "kept" / "a.cbz"
    digest = _write_archive(kept, payload)
    stat = kept.stat()

    gone = library / "gone" / "b.cbz"
    _write_archive(gone, payload)
    gone_stat = gone.stat()

    with database_connection(database) as connection:
        _seed(connection, path=kept, digest=digest, size=stat.st_size,
              mtime_ns=stat.st_mtime_ns)
        _seed(connection, path=gone, digest=digest, size=gone_stat.st_size,
              mtime_ns=gone_stat.st_mtime_ns)
        connection.commit()

    gone.unlink()  # its content still exists, but as another archive's file

    with database_connection(database) as connection:
        connection.row_factory = sqlite3.Row
        plan = plan_repairs(
            find_broken_locations(connection),
            index_roots([library]),
            known_paths=current_paths(connection),
        )

    assert plan.repairs == []
    assert len(plan.unresolved) == 1


def test_same_size_different_content_is_not_accepted_as_the_move_target(
    database: Path, tmp_path: Path
):
    """Size narrows the search; it must never establish identity.

    Comic archives of the same chapter count cluster tightly in size, so a
    same-size-different-content collision is ordinary rather than exotic. If
    the search-time hash check were dropped, this file would be adopted as the
    archive's new location and its whole evidence chain would be re-pointed at
    the wrong bytes.
    """
    library = tmp_path / "lib"
    original = library / "orig" / "a.cbz"
    payload = b"the genuine archive payload"
    digest = _write_archive(original, payload)
    stat = original.stat()

    with database_connection(database) as connection:
        _seed(
            connection,
            path=original,
            digest=digest,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
        )
        connection.commit()

    original.unlink()
    # Byte-for-byte the same length, entirely different content.
    impostor = library / "elsewhere" / "b.cbz"
    _write_archive(impostor, b"X" * len(payload))
    assert impostor.stat().st_size == len(payload)

    with database_connection(database) as connection:
        connection.row_factory = sqlite3.Row
        plan = plan_repairs(
            find_broken_locations(connection),
            index_roots([library]),
            known_paths=current_paths(connection),
        )

    assert plan.repairs == []
    assert len(plan.unresolved) == 1
    assert "no file under the searched roots matches" in plan.unresolved[0]["reason"]


def test_archive_without_stored_hash_cannot_be_verified(database: Path, tmp_path: Path):
    """No stored digest means no proof, so no repair."""
    archive = tmp_path / "lib" / "a.cbz"
    _write_archive(archive, b"payload")

    with database_connection(database) as connection:
        archive_id = int(
            connection.execute(
                "INSERT INTO archive_files (file_size) VALUES (7)"
            ).lastrowid
        )
        connection.execute(
            """
            INSERT INTO file_locations (
                archive_id, path, is_current, file_size, modified_time_ns
            )
            VALUES (?, ?, 1, 999, 999)
            """,
            (archive_id, str(archive)),
        )
        connection.commit()

    with database_connection(database) as connection:
        connection.row_factory = sqlite3.Row
        plan = plan_repairs(
            find_broken_locations(connection), {}, known_paths=current_paths(connection)
        )

    assert plan.repairs == []
    assert plan.unresolved[0]["reason"] == "no stored sha256 to verify against"


def test_apply_skips_a_file_that_changed_after_planning(database: Path, tmp_path: Path):
    """Re-verification at apply time is load-bearing, not ceremonial."""
    archive = tmp_path / "lib" / "a.cbz"
    payload = b"planned payload"
    digest = _write_archive(archive, payload)

    with database_connection(database) as connection:
        archive_id = _seed(
            connection, path=archive, digest=digest, size=len(payload), mtime_ns=1
        )
        connection.commit()

    with database_connection(database) as connection:
        connection.row_factory = sqlite3.Row
        plan = plan_repairs(
            find_broken_locations(connection), {}, known_paths=current_paths(connection)
        )
        assert len(plan.repairs) == 1

        # The world moves between plan and apply.
        archive.write_bytes(b"changed after the plan was made")

        outcome = apply_repairs(connection, plan.repairs)
        connection.commit()

        still_stale = connection.execute(
            "SELECT modified_time_ns FROM file_locations "
            "WHERE archive_id = ? AND is_current = 1",
            (archive_id,),
        ).fetchone()

    assert outcome["applied"] == []
    assert len(outcome["skipped"]) == 1
    assert "changed between plan and apply" in outcome["skipped"][0]["reason"]
    assert still_stale["modified_time_ns"] == 1  # untouched


def test_plan_digest_changes_when_the_plan_changes(database: Path, tmp_path: Path):
    """The digest must actually discriminate, or --plan-digest guards nothing."""
    library = tmp_path / "lib"
    first = library / "a.cbz"
    payload = b"payload one"
    digest = _write_archive(first, payload)

    with database_connection(database) as connection:
        _seed(connection, path=first, digest=digest, size=len(payload), mtime_ns=5)
        connection.commit()

    with database_connection(database) as connection:
        connection.row_factory = sqlite3.Row
        plan_a = plan_repairs(
            find_broken_locations(connection), {}, known_paths=current_paths(connection)
        )
        digest_a = plan_a.digest()

    first.write_bytes(payload + b" extended")

    with database_connection(database) as connection:
        connection.row_factory = sqlite3.Row
        plan_b = plan_repairs(
            find_broken_locations(connection), {}, known_paths=current_paths(connection)
        )

    assert digest_a != plan_b.digest()


def test_two_archives_cannot_both_claim_one_file(database: Path, tmp_path: Path):
    """Duplicate archives sharing a digest must not both select the same file.

    Two archives with the same stored SHA-256 is exactly what a duplicate pair
    is, and production holds 886 such groups. Planning previously computed the
    claimed-path set once and never reserved targets as it went, so both
    archives selected the surviving file and the apply handed it to whichever
    ran last.
    """
    library = tmp_path / "lib"
    payload = b"shared duplicate payload"
    digest = hashlib.sha256(payload).hexdigest()

    first = library / "one" / "a.cbz"
    second = library / "two" / "b.cbz"
    _write_archive(first, payload)
    _write_archive(second, payload)
    size = first.stat().st_size

    with database_connection(database) as connection:
        _seed(connection, path=first, digest=digest, size=size, mtime_ns=1)
        _seed(connection, path=second, digest=digest, size=size, mtime_ns=2)
        connection.commit()

    # Both originals vanish; one identical copy survives elsewhere.
    first.unlink()
    second.unlink()
    survivor = library / "survivor" / "c.cbz"
    _write_archive(survivor, payload)

    with database_connection(database) as connection:
        connection.row_factory = sqlite3.Row
        plan = plan_repairs(
            find_broken_locations(connection),
            index_roots([library]),
            known_paths=current_paths(connection),
        )

    # Exactly one archive may claim the survivor; the other is left unresolved.
    targets = [r.new_path for r in plan.repairs]
    assert len(targets) == len(set(targets))
    assert len(plan.repairs) <= 1


def test_apply_refuses_a_path_claimed_by_another_archive(
    database: Path, tmp_path: Path
):
    """A path claimed after planning must not be stolen at apply time.

    Silently re-pointing a live location at a different archive merges two
    identities and strands one archive's accumulated evidence.
    """
    library = tmp_path / "lib"
    payload = b"contested payload"
    digest = hashlib.sha256(payload).hexdigest()
    original = library / "gone" / "a.cbz"
    _write_archive(original, payload)
    size = original.stat().st_size

    with database_connection(database) as connection:
        moving_id = _seed(
            connection, path=original, digest=digest, size=size, mtime_ns=1
        )
        connection.commit()

    original.unlink()
    target = library / "elsewhere" / "b.cbz"
    _write_archive(target, payload)

    with database_connection(database) as connection:
        connection.row_factory = sqlite3.Row
        plan = plan_repairs(
            find_broken_locations(connection),
            index_roots([library]),
            known_paths=current_paths(connection),
        )
        assert len(plan.repairs) == 1

        # A scan running after the plan claims that path for a new archive.
        other_id = int(
            connection.execute(
                "INSERT INTO archive_files (file_size) VALUES (?)", (size,)
            ).lastrowid
        )
        connection.execute(
            """
            INSERT INTO file_locations (
                archive_id, path, is_current, file_size, modified_time_ns
            )
            VALUES (?, ?, 1, ?, 9)
            """,
            (other_id, str(target), size),
        )
        connection.commit()

        outcome = apply_repairs(connection, plan.repairs)
        connection.commit()

        owner = connection.execute(
            "SELECT archive_id FROM file_locations WHERE path = ? AND is_current = 1",
            (str(target),),
        ).fetchone()

    assert outcome["applied"] == []
    assert "current location of archive" in outcome["skipped"][0]["reason"]
    assert int(owner["archive_id"]) == other_id  # ownership untouched
    assert other_id != moving_id


def test_incoherent_evidence_is_never_repaired(database: Path, tmp_path: Path):
    """Matching the archive hash does not license refreshing the page signature.

    If the archive hash was recomputed after a content change while the page
    signature stayed stale, the two rows describe different file states.
    Refreshing source_* would launder the stale page evidence into looking
    current, so repair must refuse and ask for reinspection instead.
    """
    archive = tmp_path / "lib" / "a.cbz"
    payload = b"payload whose page evidence is stale"
    digest = _write_archive(archive, payload)

    with database_connection(database) as connection:
        _seed(
            connection,
            path=archive,
            digest=digest,
            size=len(payload),
            mtime_ns=111,
            # Page signature was computed from a DIFFERENT observed state.
            signature_size=len(payload) - 5,
            signature_mtime_ns=42,
        )
        connection.commit()

    with database_connection(database) as connection:
        connection.row_factory = sqlite3.Row
        plan = plan_repairs(
            find_broken_locations(connection), {}, known_paths=current_paths(connection)
        )

    assert plan.repairs == []
    assert len(plan.unresolved) == 1
    assert "different file states" in plan.unresolved[0]["reason"]


def test_coherent_evidence_is_repaired(database: Path, tmp_path: Path):
    """The provenance gate must not block the ordinary case."""
    archive = tmp_path / "lib" / "a.cbz"
    payload = b"coherent payload"
    digest = _write_archive(archive, payload)

    with database_connection(database) as connection:
        _seed(
            connection, path=archive, digest=digest, size=len(payload), mtime_ns=111
        )
        connection.commit()

    with database_connection(database) as connection:
        connection.row_factory = sqlite3.Row
        plan = plan_repairs(
            find_broken_locations(connection), {}, known_paths=current_paths(connection)
        )

    assert len(plan.repairs) == 1
    assert plan.repairs[0].kind == "metadata_drift"


def test_permission_error_is_not_treated_as_absence(
    database: Path, tmp_path: Path, monkeypatch
):
    """An unreadable path says nothing about whether the file is there.

    Classifying it as "missing" would send repair hunting for a replacement for
    a file that exists, and could re-point the archive at a different copy.
    """
    archive = tmp_path / "lib" / "a.cbz"
    payload = b"present but unreadable"
    digest = _write_archive(archive, payload)

    with database_connection(database) as connection:
        _seed(connection, path=archive, digest=digest, size=len(payload), mtime_ns=1)
        connection.commit()

    real_stat = os.stat

    def denying_stat(path, *args, **kwargs):
        if str(path) == str(archive):
            raise PermissionError(13, "Access is denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(
        "comic_automation.library.relocation_repair.os.stat", denying_stat
    )

    with database_connection(database) as connection:
        connection.row_factory = sqlite3.Row
        broken = find_broken_locations(connection)

    assert [b.state for b in broken] == ["unreadable"]

    plan = plan_repairs(broken, {}, known_paths=set())
    assert plan.repairs == []
    assert "not evidence of absence" in plan.unresolved[0]["reason"]


def test_healthy_locations_are_not_reported_as_broken(database: Path, tmp_path: Path):
    """A location that matches disk exactly must produce no work."""
    archive = tmp_path / "lib" / "a.cbz"
    payload = b"healthy"
    digest = _write_archive(archive, payload)
    stat = archive.stat()

    with database_connection(database) as connection:
        _seed(
            connection,
            path=archive,
            digest=digest,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
        )
        connection.commit()

    with database_connection(database) as connection:
        connection.row_factory = sqlite3.Row
        assert find_broken_locations(connection) == []
