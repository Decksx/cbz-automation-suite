"""The producer path: hashing an archive must move its revision.

Migration 014 makes `archive_files.current_revision_id` the authoritative
statement of an archive's byte identity, while `archive_hashes` remains the
mutable record of the most recent hash. If only one of them were written,
the database would hold two answers to "what bytes is this archive?" -- and
that is not hypothetical: before this integration, new intake received a
provisional current revision, hashing recorded a real digest in
`archive_hashes`, and the current revision stayed provisional forever while
rehashing overwrote the digest in place with no generation appended.

These tests drive the real `ArchiveHashRepository.save()` over real files,
across the three cases that path actually sees.
"""

from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path

import pytest

from comic_automation.archive.hashing import (
    ArchiveHashRepository,
    calculate_archive_hash,
)
from comic_automation.database import dal
from comic_automation.database.connection import connect_database
from comic_automation.database.migrations import apply_migrations

MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


def _make_cbz(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.jpg", payload)

    return path


@pytest.fixture()
def database(tmp_path: Path) -> Path:
    path = tmp_path / "hashing.db"
    connection = connect_database(path)

    try:
        apply_migrations(connection, MIGRATIONS)
        connection.commit()
    finally:
        connection.close()

    return path


@pytest.fixture()
def connection(database: Path):
    conn = dal.open_connection(database)

    try:
        yield conn
    finally:
        conn.close()


def _seed(conn: sqlite3.Connection, archive: Path) -> tuple[int, int]:
    """An archive identity and its current location, as discovery makes them."""
    stat = archive.stat()

    with dal.transaction(conn):
        archive_id = dal.ArchiveRepository(conn).create(
            file_size=int(stat.st_size)
        )
        cursor = conn.execute(
            """
            INSERT INTO file_locations (
                archive_id, path, file_size, modified_time_ns
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                archive_id,
                str(archive.resolve()),
                int(stat.st_size),
                int(stat.st_mtime_ns),
            ),
        )

    return archive_id, int(cursor.lastrowid)


def _hash(conn: sqlite3.Connection, archive_id: int, location_id: int,
          archive: Path) -> None:
    with dal.transaction(conn):
        ArchiveHashRepository(conn).save(
            archive_id=archive_id,
            location_id=location_id,
            result=calculate_archive_hash(archive),
            enqueue_reinspection=False,
        )


def _stored_digest(conn: sqlite3.Connection, archive_id: int) -> str:
    return conn.execute(
        "SELECT digest FROM archive_hashes WHERE archive_id = ?",
        (archive_id,),
    ).fetchone()["digest"]


def test_the_first_hash_establishes_the_identity(
    connection, tmp_path: Path
) -> None:
    """A newly discovered archive is provisional until it is hashed.

    Before this integration the pointer stayed provisional indefinitely,
    while `archive_hashes` held the real digest.
    """
    archive = _make_cbz(tmp_path / "library" / "issue.cbz", b"first bytes")
    archive_id, location_id = _seed(connection, archive)
    revisions = dal.RevisionRepository(connection)

    origin = revisions.current_for(archive_id)
    assert origin.identity_state == "provisional"

    _hash(connection, archive_id, location_id, archive)

    current = revisions.current_for(archive_id)
    lineage = revisions.lineage_for(archive_id)

    assert current.identity_state == "established"
    assert current.archive_sha256 == _stored_digest(connection, archive_id)
    assert current.revision_ordinal == 2

    # The provisional origin survives as the record of the period before the
    # bytes were known.
    assert len(lineage) == 2
    assert lineage[0].revision_id == origin.revision_id
    assert lineage[1].previous_revision_id == origin.revision_id

    # And the sighting was recorded against the revision.
    assert revisions.observations_for(current.revision_id) != []


def test_an_unchanged_rehash_adds_an_observation_not_a_generation(
    connection, tmp_path: Path
) -> None:
    """A file rediscovered unchanged must not look like a file that changed."""
    archive = _make_cbz(tmp_path / "library" / "issue.cbz", b"stable bytes")
    archive_id, location_id = _seed(connection, archive)
    revisions = dal.RevisionRepository(connection)

    _hash(connection, archive_id, location_id, archive)
    established = revisions.current_for(archive_id)

    _hash(connection, archive_id, location_id, archive)
    _hash(connection, archive_id, location_id, archive)

    current = revisions.current_for(archive_id)

    assert current.revision_id == established.revision_id
    assert len(revisions.lineage_for(archive_id)) == 2
    # One observation per hash, including the first.
    assert len(revisions.observations_for(current.revision_id)) == 3
    # The evidence recorded at first hash was not rewritten.
    assert current.evidence == established.evidence


def test_a_changed_rehash_appends_a_generation_and_moves_the_pointer(
    connection, tmp_path: Path
) -> None:
    """The case `archive_hashes` alone destroyed.

    Its upsert overwrote `digest` in place, so the previous byte state left
    no trace. The revision is appended beside the old one instead, and the
    pointer moves because a digest measured from the file on disk *is* the
    archive's current byte state.
    """
    archive = _make_cbz(tmp_path / "library" / "issue.cbz", b"first bytes")
    archive_id, location_id = _seed(connection, archive)
    revisions = dal.RevisionRepository(connection)

    _hash(connection, archive_id, location_id, archive)
    first = revisions.current_for(archive_id)

    _make_cbz(archive, b"different bytes entirely")
    _hash(connection, archive_id, location_id, archive)

    second = revisions.current_for(archive_id)
    lineage = revisions.lineage_for(archive_id)

    assert second.revision_id != first.revision_id
    assert second.archive_sha256 != first.archive_sha256
    assert second.revision_ordinal == first.revision_ordinal + 1
    assert second.previous_revision_id == first.revision_id

    # Provisional origin, generation one, generation two.
    assert len(lineage) == 3

    # The earlier generation is still readable -- this is the evidence the
    # in-place upsert used to destroy.
    assert revisions.get(first.revision_id).archive_sha256 == (
        first.archive_sha256
    )

    # And the two records agree about the present.
    assert second.archive_sha256 == _stored_digest(connection, archive_id)


def test_reverting_to_earlier_bytes_reuses_that_generation(
    connection, tmp_path: Path
) -> None:
    """A -> B -> A records two generations, not three.

    The third hash sees bytes the archive already has a revision for, so it
    reuses it and points back at it rather than minting a duplicate that
    `UNIQUE (archive_id, archive_sha256)` would refuse anyway.
    """
    archive = _make_cbz(tmp_path / "library" / "issue.cbz", b"state A")
    archive_id, location_id = _seed(connection, archive)
    revisions = dal.RevisionRepository(connection)

    _hash(connection, archive_id, location_id, archive)
    state_a = revisions.current_for(archive_id)

    _make_cbz(archive, b"state B is longer")
    _hash(connection, archive_id, location_id, archive)
    state_b = revisions.current_for(archive_id)

    _make_cbz(archive, b"state A")
    _hash(connection, archive_id, location_id, archive)

    current = revisions.current_for(archive_id)

    assert current.revision_id == state_a.revision_id
    assert current.revision_id != state_b.revision_id
    assert len(revisions.lineage_for(archive_id)) == 3
    assert len(revisions.observations_for(state_a.revision_id)) == 2


def test_the_hash_and_its_revision_are_written_atomically(
    connection, tmp_path: Path
) -> None:
    """A failure anywhere in save() must leave both records untouched.

    Before this change the hash upsert, the location update and the
    archive_files update were three separate autocommits; now that the
    current revision is derived from the same digest, a partial write would
    mean `archive_hashes.digest` and `current_revision_id` naming different
    bytes.
    """
    archive = _make_cbz(tmp_path / "library" / "issue.cbz", b"first bytes")
    archive_id, location_id = _seed(connection, archive)
    revisions = dal.RevisionRepository(connection)

    _hash(connection, archive_id, location_id, archive)
    before_digest = _stored_digest(connection, archive_id)
    before_current = revisions.current_for(archive_id)
    before_lineage = revisions.lineage_for(archive_id)

    _make_cbz(archive, b"different bytes entirely")

    with pytest.raises(RuntimeError):
        with dal.transaction(connection):
            ArchiveHashRepository(connection).save(
                archive_id=archive_id,
                location_id=location_id,
                result=calculate_archive_hash(archive),
                enqueue_reinspection=False,
            )
            raise RuntimeError("failure after the hash and the revision")

    assert _stored_digest(connection, archive_id) == before_digest
    assert revisions.current_for(archive_id).revision_id == (
        before_current.revision_id
    )
    assert revisions.lineage_for(archive_id) == before_lineage


def test_saving_outside_a_transaction_is_refused(
    connection, tmp_path: Path
) -> None:
    """The producer does not own a commit any more than a repository does.

    Paired with the successes above so a blanket breakage could not pass as
    correct strictness.
    """
    archive = _make_cbz(tmp_path / "library" / "issue.cbz", b"bytes")
    archive_id, location_id = _seed(connection, archive)

    with pytest.raises(dal.TransactionRequiredError):
        ArchiveHashRepository(connection).save(
            archive_id=archive_id,
            location_id=location_id,
            result=calculate_archive_hash(archive),
            enqueue_reinspection=False,
        )

    assert (
        connection.execute(
            "SELECT COUNT(*) FROM archive_hashes WHERE archive_id = ?",
            (archive_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        dal.RevisionRepository(connection)
        .current_for(archive_id)
        .identity_state
        == "provisional"
    )
