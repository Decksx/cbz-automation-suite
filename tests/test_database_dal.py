"""The minimum local DAL: connection policy, transactions, repositories.

Every test here uses a temporary database. Nothing opens the production
database, applies a migration, or defines revision schema.

The claims under test are mostly *refusals*, which is the hard kind to
assert honestly: a test that expects an exception passes just as readily
when the code under test is broken in some unrelated way. So each refusal
is paired with the corresponding success -- a repository write that works
inside a transaction, a read that works on a read-only connection -- so a
blanket failure cannot masquerade as correct strictness.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from comic_automation.database import dal
from comic_automation.database.connection import connect_database
from comic_automation.database.migrations import apply_migrations


MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def database_path(tmp_path: Path) -> Path:
    """A migrated, writable temporary database."""
    path = tmp_path / "dal.db"
    connection = connect_database(path)

    try:
        apply_migrations(connection, MIGRATIONS)
        connection.commit()
    finally:
        connection.close()

    return path


@pytest.fixture()
def connection(database_path: Path):
    conn = dal.open_connection(database_path)

    try:
        yield conn
    finally:
        conn.close()


def _seed_archive(conn: sqlite3.Connection, file_size: int = 4096) -> int:
    with dal.transaction(conn):
        return dal.ArchiveRepository(conn).create(file_size=file_size)


# --- connection policy ---------------------------------------------------


def test_the_writable_policy_matches_the_existing_factory(
    tmp_path: Path,
) -> None:
    """Consolidation, not redefinition.

    `connect_database` is left in place and unchanged by this PR, so the
    only way the new policy can be a centralisation rather than a second
    dialect is if it produces the same connection state. Compared pragma by
    pragma against the live connection rather than against the source, so a
    later edit to either one fails here.
    """
    interesting = (
        "foreign_keys",
        "journal_mode",
        "synchronous",
        "busy_timeout",
    )

    existing = connect_database(tmp_path / "existing.db")
    new = dal.open_connection(tmp_path / "new.db")

    try:
        for pragma in interesting:
            old_value = existing.execute(f"PRAGMA {pragma}").fetchone()[0]
            new_value = new.execute(f"PRAGMA {pragma}").fetchone()[0]
            assert old_value == new_value, pragma

        assert existing.isolation_level == new.isolation_level is None
        assert existing.row_factory is new.row_factory is sqlite3.Row
    finally:
        existing.close()
        new.close()


def test_a_writable_connection_can_write(connection) -> None:
    """The control for every refusal below."""
    archive_id = _seed_archive(connection)

    assert dal.ArchiveRepository(connection).exists(archive_id)


def test_a_read_only_connection_reports_itself_as_read_only(
    database_path: Path,
) -> None:
    with dal.connection_scope(database_path, readonly=True) as conn:
        assert dal.is_readonly(conn) is True

    with dal.connection_scope(database_path) as conn:
        assert dal.is_readonly(conn) is False


def test_a_read_only_connection_rejects_writes(
    database_path: Path,
) -> None:
    """`query_only` is what makes this a guarantee rather than a habit.

    The `mode=ro` URI flag alone would also reject the write, but only at
    the VFS layer; `query_only` rejects it at the statement level, which is
    the layer that survives someone later changing how the file is opened.
    """
    with dal.connection_scope(database_path, readonly=True) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO archive_files (file_size) VALUES (1)"
            )


def test_a_read_only_connection_can_still_read(
    database_path: Path,
) -> None:
    with dal.connection_scope(database_path) as writer:
        archive_id = _seed_archive(writer)

    with dal.connection_scope(database_path, readonly=True) as reader:
        assert dal.ArchiveRepository(reader).get(archive_id) is not None


def test_opening_a_missing_database_read_only_creates_nothing(
    tmp_path: Path,
) -> None:
    """A plain `sqlite3.connect` would have created an empty database.

    An audit that reports zero rows against a database it silently created
    itself is worse than one that fails, because the number looks like a
    finding.
    """
    missing = tmp_path / "absent.db"

    with pytest.raises(FileNotFoundError):
        dal.open_connection(missing, readonly=True)

    assert not missing.exists()


def test_the_two_policies_differ_only_where_intended() -> None:
    """Pins the policy difference as data.

    The read-only policy exists to add `query_only`; if it ever also
    stopped setting `busy_timeout`, read-only callers would start failing
    immediately on a locked database instead of retrying -- which is
    exactly the drift found in `read_guards.py`.
    """
    writable = dict(dal.WRITABLE_POLICY.pragmas)
    readonly = dict(dal.READ_ONLY_POLICY.pragmas)

    assert readonly["query_only"] == "ON"
    assert "query_only" not in writable
    assert readonly["busy_timeout"] == writable["busy_timeout"]
    assert dal.READ_ONLY_POLICY.readonly is True
    assert dal.WRITABLE_POLICY.readonly is False


# --- transaction ownership -----------------------------------------------


def test_transaction_commits_on_success(connection) -> None:
    repository = dal.ArchiveRepository(connection)

    with dal.transaction(connection):
        repository.create(file_size=4096)

    assert connection.in_transaction is False
    assert repository.count() == 1


def test_transaction_rolls_back_on_any_exception(connection) -> None:
    """Rollback is owned here; the caller writes no ROLLBACK."""
    repository = dal.ArchiveRepository(connection)

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        with dal.transaction(connection):
            repository.create(file_size=4096)
            repository.create(file_size=8192)
            raise Boom

    assert connection.in_transaction is False
    assert repository.count() == 0


def test_transaction_rolls_back_on_base_exception(connection) -> None:
    """A KeyboardInterrupt must not leave the transaction open.

    Caught as `BaseException` rather than `Exception` on purpose: an
    interrupt landing mid-write would otherwise leave the connection inside
    a transaction, and whatever ran next on it would silently join.
    """
    repository = dal.ArchiveRepository(connection)

    with pytest.raises(KeyboardInterrupt):
        with dal.transaction(connection):
            repository.create(file_size=4096)
            raise KeyboardInterrupt

    assert connection.in_transaction is False
    assert repository.count() == 0


def test_a_partial_multi_step_change_leaves_nothing_behind(
    connection,
) -> None:
    """The reason implicit commits are refused.

    Four writes where the fourth fails. Under per-statement commits three
    would be durable with nothing to undo; under one transaction none are.
    """
    archives = dal.ArchiveRepository(connection)
    signatures = dal.ContentSignatureRepository(connection)
    archive_id = _seed_archive(connection)

    with pytest.raises(sqlite3.IntegrityError):
        with dal.transaction(connection):
            archives.set_sha256(archive_id, "a" * 64)
            signatures.save(
                archive_id=archive_id,
                digest="d" * 64,
                page_count=3,
                image_bytes=99,
                source_file_size=4096,
                source_modified_time_ns=111,
            )
            # Violates the FOREIGN KEY to archive_files.
            signatures.save(
                archive_id=999_999,
                digest="e" * 64,
                page_count=1,
                image_bytes=1,
                source_file_size=1,
                source_modified_time_ns=1,
            )

    assert archives.get(archive_id).sha256 is None
    assert signatures.for_archive(archive_id) is None


def test_nesting_a_transaction_is_refused(connection) -> None:
    """SQLite has no nested transactions, so joining silently would lie.

    An inner block that appeared to commit would be committing the outer
    block's work too, and the outer block's rollback would then have
    nothing left to undo.
    """
    with dal.transaction(connection):
        with pytest.raises(dal.DalError):
            with dal.transaction(connection):
                pass


def test_a_write_transaction_on_a_read_only_connection_is_refused(
    database_path: Path,
) -> None:
    with dal.connection_scope(database_path, readonly=True) as conn:
        with pytest.raises(dal.ReadOnlyConnectionError):
            with dal.transaction(conn):
                pass


# --- repository transaction ownership ------------------------------------


def test_repository_writes_outside_a_transaction_are_refused(
    connection,
) -> None:
    """Repositories do not own commits, and may not proceed without one."""
    archives = dal.ArchiveRepository(connection)

    with pytest.raises(dal.TransactionRequiredError):
        archives.create(file_size=4096)

    assert archives.count() == 0


def test_every_repository_write_requires_a_transaction(
    connection,
) -> None:
    """Asserted for each write method, not just the first.

    A rule enforced by one method and forgotten by its neighbour is the
    normal way this kind of guard decays.
    """
    archives = dal.ArchiveRepository(connection)
    signatures = dal.ContentSignatureRepository(connection)
    archive_id = _seed_archive(connection)

    writes = [
        lambda: archives.create(file_size=1),
        lambda: archives.set_sha256(archive_id, "a" * 64),
        lambda: signatures.save(
            archive_id=archive_id,
            digest="d" * 64,
            page_count=1,
            image_bytes=1,
            source_file_size=1,
            source_modified_time_ns=1,
        ),
    ]

    for write in writes:
        with pytest.raises(dal.TransactionRequiredError):
            write()


def test_repository_reads_do_not_require_a_transaction(
    database_path: Path,
) -> None:
    """Reads must work on a read-only connection.

    If reads required a write transaction, every audit in the codebase
    would have to open the database writable to look at it.
    """
    with dal.connection_scope(database_path) as writer:
        archive_id = _seed_archive(writer)

    with dal.connection_scope(database_path, readonly=True) as reader:
        archives = dal.ArchiveRepository(reader)

        assert archives.get(archive_id) is not None
        assert archives.exists(archive_id) is True
        assert archives.count() == 1
        assert archives.identity_ids() == [archive_id]
        assert reader.in_transaction is False


def test_a_repository_write_does_not_commit_by_itself(
    connection,
) -> None:
    """The write must still be pending when the block has not closed.

    Proves the repository defers the commit to `transaction()` rather than
    performing one of its own -- which a passing "it was written" assertion
    alone would not distinguish.
    """
    archives = dal.ArchiveRepository(connection)

    with dal.transaction(connection):
        archives.create(file_size=4096)
        assert connection.in_transaction is True

    assert connection.in_transaction is False


# --- repositories over existing entities ---------------------------------


def test_archive_round_trips(connection) -> None:
    archives = dal.ArchiveRepository(connection)
    archive_id = _seed_archive(connection, file_size=1234)

    record = archives.get(archive_id)

    assert record.archive_id == archive_id
    assert record.file_size == 1234
    assert record.sha256 is None

    with dal.transaction(connection):
        archives.set_sha256(archive_id, "b" * 64)

    assert archives.get(archive_id).sha256 == "b" * 64


def test_a_missing_archive_reads_as_none(connection) -> None:
    assert dal.ArchiveRepository(connection).get(999_999) is None
    assert dal.ArchiveRepository(connection).exists(999_999) is False


def test_content_signature_round_trips_and_upserts(connection) -> None:
    signatures = dal.ContentSignatureRepository(connection)
    archive_id = _seed_archive(connection)

    with dal.transaction(connection):
        signatures.save(
            archive_id=archive_id,
            digest="d" * 64,
            page_count=3,
            image_bytes=99,
            source_file_size=4096,
            source_modified_time_ns=111,
        )

    record = signatures.for_archive(archive_id)
    assert record.digest == "d" * 64
    assert record.page_count == 3

    # `archive_id` is UNIQUE, so a second save must update rather than
    # raise or duplicate.
    with dal.transaction(connection):
        signatures.save(
            archive_id=archive_id,
            digest="e" * 64,
            page_count=4,
            image_bytes=100,
            source_file_size=5000,
            source_modified_time_ns=222,
        )

    updated = signatures.for_archive(archive_id)
    assert updated.digest == "e" * 64
    assert updated.page_count == 4


def test_a_digest_shared_by_several_archives_returns_all_of_them(
    connection,
) -> None:
    """The column is not unique, and production has sixteen such groups.

    Returning one id would let a revision attach itself to whichever
    archive happened to sort first.
    """
    signatures = dal.ContentSignatureRepository(connection)
    shared = "f" * 64

    with dal.transaction(connection):
        archives = dal.ArchiveRepository(connection)
        first = archives.create(file_size=1)
        second = archives.create(file_size=2)

        for archive_id in (first, second):
            signatures.save(
                archive_id=archive_id,
                digest=shared,
                page_count=1,
                image_bytes=1,
                source_file_size=1,
                source_modified_time_ns=1,
            )

    assert signatures.archives_sharing_digest(shared) == [first, second]
    assert signatures.duplicate_digests() == [(shared, 2)]


# --- the connection-factory guard ----------------------------------------


def test_no_unsanctioned_module_opens_its_own_connection() -> None:
    """A new `sqlite3.connect` outside the allowlist fails here.

    The allowlist is a snapshot of the bypasses that already existed, each
    with a recorded reason, rather than a claim that they are all correct.
    Two of them are known drift -- `scripts/db.py` sets no `query_only` on
    its read-only mode and leaves the driver's default isolation level --
    and consolidating those changes behaviour, so they are recorded here
    instead of being silently tolerated.
    """
    assert dal.unsanctioned_connect_sites(REPO_ROOT) == []


def test_the_allowlist_has_no_stale_entries() -> None:
    """An allowlist that outlives its call sites stops meaning anything."""
    actual = set(dal.find_connect_sites(REPO_ROOT))
    listed = set(dal.SANCTIONED_CONNECT_SITES)

    assert listed - actual == set(), "allowlist names files that no longer connect"


def test_every_allowlist_entry_records_a_reason() -> None:
    for path, reason in dal.SANCTIONED_CONNECT_SITES.items():
        assert reason.strip(), path
        assert len(reason) > 40, f"{path}: reason is too thin to be useful"


def test_the_guard_detects_a_newly_added_bypass(tmp_path: Path) -> None:
    """Proves the guard can fail, without editing the real tree.

    A scan that returned nothing because it was looking in the wrong place,
    or matching the wrong text, would pass the assertion above forever. This
    builds a miniature tree containing a bypass and asserts it is found.
    """
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "sanctioned.py").write_text(
        "import sqlite3\nconn = sqlite3.connect('x.db')\n", encoding="utf-8"
    )
    (tmp_path / "pkg" / "rogue.py").write_text(
        "import sqlite3\nconn = sqlite3.connect('y.db')\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        "import sqlite3\nsqlite3.connect(':memory:')\n", encoding="utf-8"
    )

    found = dal.find_connect_sites(tmp_path)

    assert found == {"pkg/rogue.py": 1, "pkg/sanctioned.py": 1}, found
    # tests/ is excluded: test files legitimately open their own databases.
    assert not any(name.startswith("tests/") for name in found)

    unsanctioned = dal.unsanctioned_connect_sites(
        tmp_path, sanctioned=["pkg/sanctioned.py"]
    )
    assert unsanctioned == ["pkg/rogue.py"]


def test_commented_out_calls_are_not_counted(tmp_path: Path) -> None:
    """A mention in a comment is not a bypass.

    `read_guards.py` discusses `sqlite3.connect` in prose; counting that
    would make the guard fire on documentation.
    """
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "documented.py").write_text(
        "# a plain sqlite3.connect( would create the file\n"
        "VALUE = 1\n",
        encoding="utf-8",
    )

    assert dal.find_connect_sites(tmp_path) == {}
