"""Tests for content-keyed location repair.

Archive identity in this codebase is established by path, which is right for
discovery and wrong for repair: a moved file rescanned becomes a second
archive identity, stranding the original's page inventory, hashes and job
history. These tests pin the repair that fixes it, and -- more importantly --
pin the conditions under which it refuses to act.
"""

from __future__ import annotations

import hashlib
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
) -> int:
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
        (archive_id, "sig-" + digest[:8], size, mtime_ns),
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
