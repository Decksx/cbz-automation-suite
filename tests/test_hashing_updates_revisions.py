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

from comic_automation.archive import hashing as hashing_module
from comic_automation.archive.hashing import (
    ArchiveHashRepository,
    CalculateArchiveHashHandler,
    calculate_archive_hash,
)
from comic_automation.jobs import CategorizedJobError
from comic_automation.database import dal
from comic_automation.database.connection import connect_database
from comic_automation.database.migrations import apply_migrations
from tests import golden_corpus as gc

MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


def _make_cbz(path: Path, payload: bytes) -> Path:
    """A byte-reproducible archive: identical payload, identical digest.

    Built through the golden-corpus helper rather than a bare
    `writestr`, which stamps each member with the current time at
    2-second DOS granularity. Writing the same payload twice then yields
    two different archive digests whenever the writes straddle an
    interval boundary -- measured: two identical-payload archives three
    seconds apart hashed differently.

    That matters most for the A -> B -> A test below, whose whole premise
    is that restoring the earlier bytes reproduces the earlier digest. It
    would have passed only when the test happened to finish inside one
    interval, which is the same timestamp trap found during PR #79.
    """
    return gc.build_cbz(path, [("001.jpg", payload)])


def test_the_fixture_writer_is_byte_reproducible(tmp_path: Path) -> None:
    """The A -> B -> A proof rests on this, so it is asserted directly.

    Comparing two writes for equal digests is not enough: a bare `writestr`
    produces equal digests too, whenever both writes land inside the same
    2-second DOS timestamp interval, which in a fast test is almost always.
    Restoring the unpinned writer therefore failed nothing downstream --
    measured -- and the flakiness would only appear later, on a slower
    machine or at an unlucky moment.

    So the stored metadata is inspected instead. A writer that stamps the
    current time cannot produce FIXED_DATE_TIME, whatever the clock is doing
    when the test runs.
    """
    first = _make_cbz(tmp_path / "one.cbz", b"same payload")
    second = _make_cbz(tmp_path / "two.cbz", b"same payload")

    with zipfile.ZipFile(first) as archive:
        info = archive.getinfo("001.jpg")

    assert info.date_time == gc.FIXED_DATE_TIME
    assert info.create_system == 0
    assert first.read_bytes() == second.read_bytes()
    assert (
        calculate_archive_hash(first).digest
        == calculate_archive_hash(second).digest
    )


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
    original_digest = calculate_archive_hash(archive).digest
    archive_id, location_id = _seed(connection, archive)
    revisions = dal.RevisionRepository(connection)

    _hash(connection, archive_id, location_id, archive)
    state_a = revisions.current_for(archive_id)

    _make_cbz(archive, b"state B is longer")
    _hash(connection, archive_id, location_id, archive)
    state_b = revisions.current_for(archive_id)

    _make_cbz(archive, b"state A")

    # The premise, asserted rather than assumed: restoring the payload has
    # actually restored the digest. Without a byte-reproducible writer this
    # is false whenever the writes straddle a DOS timestamp interval, and
    # the reuse assertion below would fail for a reason that has nothing to
    # do with revisions.
    assert calculate_archive_hash(archive).digest == original_digest

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


# --- the window between hashing and promotion ----------------------------
#
# `calculate_archive_hash()` proves the file held still only while it was
# being read. The handler then opens a transaction and promotes, and between
# those two moments the file -- or the database's idea of where the file is
# -- can change. Without the checks below the hash row and the revision
# would agree with each other while both described bytes that were no longer
# current, which is harder to notice than a plain disagreement.


def _snapshot(conn: sqlite3.Connection, archive_id: int) -> dict:
    """Everything the handler could damage, in one comparable value."""
    revisions = dal.RevisionRepository(conn)
    lineage = revisions.lineage_for(archive_id)
    hash_row = conn.execute(
        "SELECT digest, file_size, modified_time_ns FROM archive_hashes "
        "WHERE archive_id = ?",
        (archive_id,),
    ).fetchone()
    locations = conn.execute(
        "SELECT id, path, file_size, modified_time_ns, is_current "
        "FROM file_locations WHERE archive_id = ? ORDER BY id",
        (archive_id,),
    ).fetchall()

    return {
        "hash": tuple(hash_row) if hash_row is not None else None,
        "current": revisions.current_for(archive_id),
        "lineage": lineage,
        "observations": [
            tuple(revisions.observations_for(record.revision_id))
            for record in lineage
        ],
        "locations": [tuple(row) for row in locations],
    }


def _run_handler(conn: sqlite3.Connection, archive_id: int):
    from comic_automation.jobs import JobQueue

    handler = CalculateArchiveHashHandler(conn)
    job = JobQueue(conn).enqueue(
        "calculate_archive_hash", archive_id=archive_id
    )

    return handler(job)


def test_a_replacement_before_the_transaction_is_refused(
    connection, tmp_path: Path, monkeypatch
) -> None:
    """The file is swapped after hashing, before the write transaction opens.

    Injected by replacing the archive the instant `calculate_archive_hash`
    returns, which is exactly the window the handler cannot see from inside
    its own transaction.
    """
    archive = _make_cbz(tmp_path / "library" / "issue.cbz", b"original bytes")
    archive_id, location_id = _seed(connection, archive)
    _hash(connection, archive_id, location_id, archive)

    before = _snapshot(connection, archive_id)

    real_hash = hashing_module.calculate_archive_hash

    def hash_then_replace(path, **kwargs):
        result = real_hash(path, **kwargs)
        # The swap the handler must notice.
        _make_cbz(Path(path), b"replaced after hashing, before the write")
        return result

    monkeypatch.setattr(
        hashing_module, "calculate_archive_hash", hash_then_replace
    )

    with pytest.raises(CategorizedJobError) as caught:
        _run_handler(connection, archive_id)

    assert caught.value.category == "filesystem_io"
    assert _snapshot(connection, archive_id) == before


def test_a_replacement_during_the_write_is_refused(
    connection, tmp_path: Path, monkeypatch
) -> None:
    """The file is swapped after save() has written, before COMMIT.

    This is the case the pre-write check alone cannot catch: everything
    looked correct when the writes began. Only the re-stat immediately
    before commit sees it, and the whole transaction has to roll back --
    including the revision and its observation.
    """
    archive = _make_cbz(tmp_path / "library" / "issue.cbz", b"original bytes")
    archive_id, location_id = _seed(connection, archive)
    _hash(connection, archive_id, location_id, archive)

    before = _snapshot(connection, archive_id)

    real_save = ArchiveHashRepository.save

    def save_then_replace(self, **kwargs):
        real_save(self, **kwargs)
        # Every write has landed; COMMIT has not.
        _make_cbz(archive, b"replaced mid-transaction, before commit")

    monkeypatch.setattr(ArchiveHashRepository, "save", save_then_replace)

    with pytest.raises(CategorizedJobError) as caught:
        _run_handler(connection, archive_id)

    assert caught.value.category == "filesystem_io"
    # The rollback took the hash row, the pointer, the lineage, the
    # observations and the location metadata with it.
    assert _snapshot(connection, archive_id) == before


def test_a_relocation_after_hashing_is_refused(
    connection, tmp_path: Path, monkeypatch
) -> None:
    """The archive's current location is reassigned while it is hashed.

    The handler reads the location before hashing and promotes after, so a
    relocation in between would attach a digest measured at the old path to
    whatever the archive points at now.
    """
    archive = _make_cbz(tmp_path / "library" / "issue.cbz", b"original bytes")
    archive_id, location_id = _seed(connection, archive)
    _hash(connection, archive_id, location_id, archive)

    moved = tmp_path / "library" / "moved.cbz"
    _make_cbz(moved, b"original bytes")

    before = _snapshot(connection, archive_id)

    real_hash = hashing_module.calculate_archive_hash

    def hash_then_relocate(path, **kwargs):
        result = real_hash(path, **kwargs)
        stat = moved.stat()
        # A new current location, exactly as a relocation repair would make.
        connection.execute(
            "UPDATE file_locations SET is_current = 0 WHERE archive_id = ?",
            (archive_id,),
        )
        connection.execute(
            """
            INSERT INTO file_locations (
                archive_id, path, file_size, modified_time_ns, is_current
            )
            VALUES (?, ?, ?, ?, 1)
            """,
            (
                archive_id,
                str(moved.resolve()),
                int(stat.st_size),
                int(stat.st_mtime_ns),
            ),
        )
        return result

    monkeypatch.setattr(
        hashing_module, "calculate_archive_hash", hash_then_relocate
    )

    with pytest.raises(CategorizedJobError) as caught:
        _run_handler(connection, archive_id)

    assert caught.value.category == "filesystem_io"

    after = _snapshot(connection, archive_id)

    # The relocation itself is not this handler's to undo -- it was committed
    # outside the transaction -- but nothing derived from the stale hash was
    # written: the digest, pointer, lineage and observations are untouched.
    assert after["hash"] == before["hash"]
    assert after["current"] == before["current"]
    assert after["lineage"] == before["lineage"]
    assert after["observations"] == before["observations"]


def test_the_handler_still_succeeds_when_nothing_moves(
    connection, tmp_path: Path
) -> None:
    """The guards did not break the path they guard.

    Three refusals above are worth nothing without this: a handler that
    always raised would pass every one of them.
    """
    archive = _make_cbz(tmp_path / "library" / "issue.cbz", b"original bytes")
    archive_id, location_id = _seed(connection, archive)
    revisions = dal.RevisionRepository(connection)

    _run_handler(connection, archive_id)

    current = revisions.current_for(archive_id)

    assert current.identity_state == "established"
    assert current.archive_sha256 == _stored_digest(connection, archive_id)
    assert revisions.observations_for(current.revision_id) != []
