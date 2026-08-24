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
    """The ordinary case: a write on a read-only connection is refused.

    Both layers reject it, so this test alone cannot say which one did.
    `test_query_only_cannot_be_turned_off_on_a_read_only_connection`
    separates them, and the measurement there runs the other way from the
    intuitive reading: `query_only` is a pragma and can simply be turned
    back off, while `mode=ro` is the layer that survives that.
    """
    with dal.connection_scope(database_path, readonly=True) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO archive_files (file_size) VALUES (1)"
            )


def test_query_only_cannot_be_turned_off_on_a_read_only_connection(
    database_path: Path,
) -> None:
    """Why `mode=ro` and `query_only` are both set rather than either.

    `query_only` is a pragma, and a pragma can be turned back off -- by a
    later refactor, or by any code that runs on the connection. Measured:
    without the `mode=ro` URI flag, setting `query_only = OFF` makes the
    connection writable again and the INSERT succeeds. With it, the write
    is still refused at the VFS layer.

    Removing the flag therefore fails no test that only tries to write, so
    this asserts the specific thing the flag adds.
    """
    with dal.connection_scope(database_path, readonly=True) as conn:
        conn.execute("PRAGMA query_only = OFF")

        assert dal.is_readonly(conn) is False  # the pragma really is off

        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute(
                "INSERT INTO archive_files (file_size) VALUES (1)"
            )


def test_a_missing_read_only_database_names_itself_in_the_error(
    tmp_path: Path,
) -> None:
    """The explicit check exists for the message, and is tested for it.

    `Path.resolve(strict=True)` would also raise `FileNotFoundError`, but
    with the OS's text -- "[WinError 2] The system cannot find the file
    specified" -- which does not say *which* database an operator pointed a
    tool at. Dropping the explicit check therefore breaks no exception type
    and no control flow, only the diagnostic, so the message is what this
    asserts.
    """
    missing = tmp_path / "absent.db"

    with pytest.raises(FileNotFoundError, match="Database does not exist"):
        dal.open_connection(missing, readonly=True)


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


def test_a_readonly_request_with_a_writable_policy_is_refused(
    database_path: Path,
) -> None:
    """A safety request must never silently become its opposite.

    `readonly=True` with `WRITABLE_POLICY` used to hand back a fully
    writable connection, because the supplied policy won and the flag was
    dropped. The caller asked for a connection that cannot write and got
    one that can, with nothing raised and nothing logged.
    """
    with pytest.raises(dal.DalError) as caught:
        dal.open_connection(
            database_path, readonly=True, policy=dal.WRITABLE_POLICY
        )

    message = str(caught.value)
    assert "readonly=True" in message
    assert "writable" in message


def test_a_writable_request_with_a_readonly_policy_is_refused(
    database_path: Path,
) -> None:
    """The mirror image, refused for the same reason.

    Resolving it the other way is no better: the caller supplied a policy
    deliberately and would have had it silently discarded.
    """
    with pytest.raises(dal.DalError) as caught:
        dal.open_connection(
            database_path, readonly=False, policy=dal.READ_ONLY_POLICY
        )

    assert "read_only" in str(caught.value)


def test_a_policy_agreeing_with_the_flag_is_accepted(
    database_path: Path,
) -> None:
    """The refusal is about disagreement, not about passing a policy.

    Without this, a blanket refusal of the `policy` argument would pass
    both tests above while making the parameter useless.
    """
    with dal.connection_scope(database_path) as _:
        pass

    explicit_write = dal.open_connection(
        database_path, readonly=False, policy=dal.WRITABLE_POLICY
    )
    try:
        assert dal.is_readonly(explicit_write) is False
    finally:
        explicit_write.close()

    explicit_read = dal.open_connection(
        database_path, readonly=True, policy=dal.READ_ONLY_POLICY
    )
    try:
        assert dal.is_readonly(explicit_read) is True
    finally:
        explicit_read.close()


def test_a_custom_policy_is_still_honoured(database_path: Path) -> None:
    """A caller-supplied policy that agrees with the flag still applies.

    Proves the conflict check did not turn into "ignore the policy".
    """
    custom = dal.ConnectionPolicy(
        name="custom_writable",
        readonly=False,
        timeout_seconds=5.0,
        pragmas=(("busy_timeout", "7000"),),
    )

    conn = dal.open_connection(database_path, policy=custom)

    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 7000
    finally:
        conn.close()

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
    signatures = dal.ContentSignatureRepository(connection)
    archive_id = _seed_archive(connection)
    second_id = _seed_archive(connection, file_size=8192)

    with pytest.raises(sqlite3.IntegrityError):
        with dal.transaction(connection):
            signatures.save(
                archive_id=second_id,
                digest="c" * 64,
                page_count=1,
                image_bytes=1,
                source_file_size=1,
                source_modified_time_ns=1,
            )
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

    assert signatures.for_archive(second_id) is None
    assert signatures.for_archive(archive_id) is None


def _arm_deferred_constraint(conn: sqlite3.Connection) -> None:
    """Scaffold a constraint that fails at COMMIT rather than at INSERT.

    Every FOREIGN KEY in the production schema is immediate, so it raises
    at statement time and never reaches the commit boundary. A DEFERRABLE
    INITIALLY DEFERRED key is checked only when COMMIT runs, which is the
    one point `transaction()` cannot delegate to the caller. Created in the
    temporary database only -- this is test scaffolding, not schema.
    """
    conn.executescript(
        """
        CREATE TABLE commit_time_parent(id INTEGER PRIMARY KEY);
        CREATE TABLE commit_time_child(
            id INTEGER PRIMARY KEY,
            parent_id INTEGER REFERENCES commit_time_parent(id)
                DEFERRABLE INITIALLY DEFERRED
        );
        """
    )


def _fail_at_commit(conn: sqlite3.Connection) -> None:
    """Run one transaction whose COMMIT is the statement that fails."""
    with pytest.raises(sqlite3.IntegrityError):
        with dal.transaction(conn):
            conn.execute("INSERT INTO commit_time_child VALUES (1, 999)")


def test_a_failing_commit_rolls_back_and_leaves_nothing_durable(
    connection, database_path: Path
) -> None:
    """COMMIT is itself a statement that can fail.

    Measured before this was guarded: the block exited with the
    transaction still open, the failed row still visible on the
    connection, and that row became durable the moment anything else on
    the connection committed. The last assertion is that strongest form --
    an unrelated later commit must not carry the failed work with it.
    """
    _arm_deferred_constraint(connection)
    _fail_at_commit(connection)

    assert connection.in_transaction is False
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM commit_time_child"
        ).fetchone()[0]
        == 0
    )

    connection.execute("INSERT INTO commit_time_parent VALUES (999)")
    connection.commit()

    with dal.connection_scope(database_path, readonly=True) as reader:
        assert (
            reader.execute(
                "SELECT COUNT(*) FROM commit_time_child"
            ).fetchone()[0]
            == 0
        )


def test_a_failing_commit_releases_the_write_lock(
    connection, database_path: Path
) -> None:
    """A transaction stranded by a failed COMMIT holds the write lock.

    Measured before this was guarded: a second writer got "database is
    locked" and stayed locked out until the first connection was closed.
    """
    _arm_deferred_constraint(connection)
    _fail_at_commit(connection)

    second = dal.open_connection(database_path)

    try:
        # Far below the policy's 30s default, so a still-held lock fails
        # this test in half a second instead of stalling the suite.
        second.execute("PRAGMA busy_timeout = 500")
        second.execute("BEGIN IMMEDIATE")
        second.execute("ROLLBACK")
    finally:
        second.close()


def test_a_failing_commit_leaves_the_connection_usable(connection) -> None:
    """The stranded transaction also poisoned the connection.

    `require_transaction()` saw `in_transaction` and let repository writes
    join a transaction nobody owned, while `transaction()` refused every
    later block as a nested one. Both follow from the rollback, so both
    are asserted here rather than assumed.
    """
    _arm_deferred_constraint(connection)
    _fail_at_commit(connection)

    with pytest.raises(dal.TransactionRequiredError):
        dal.ArchiveRepository(connection).create(file_size=1)

    with dal.transaction(connection):
        archive_id = dal.ArchiveRepository(connection).create(file_size=7)

    assert dal.ArchiveRepository(connection).get(archive_id).file_size == 7


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
        lambda: archives.create(file_size=1),
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


def test_a_raw_begin_does_not_satisfy_the_transaction_requirement(
    connection,
) -> None:
    """`in_transaction` is not ownership.

    A caller that issues its own ``BEGIN`` has an open transaction, so a
    guard checking only `connection.in_transaction` let repository writes
    straight through -- while nothing in this module owned the commit or
    the rollback the repository was relying on. The write is refused, and
    the caller's transaction is left exactly as it was found.
    """
    archives = dal.ArchiveRepository(connection)
    connection.execute("BEGIN IMMEDIATE")

    try:
        assert connection.in_transaction is True

        with pytest.raises(dal.TransactionRequiredError) as caught:
            archives.create(file_size=1)

        # The message has to distinguish this from "no transaction at all",
        # or the reader fixes the wrong problem.
        assert "did not start it" in str(caught.value)
        assert connection.in_transaction is True
    finally:
        connection.execute("ROLLBACK")

    assert archives.count() == 0


def test_an_inherited_transaction_does_not_satisfy_it_either(
    connection,
) -> None:
    """The same hole, arrived at without anyone writing ``BEGIN``.

    An implicit transaction opened by unrelated code on the same
    connection is indistinguishable from a raw one as far as
    `in_transaction` is concerned.
    """
    archives = dal.ArchiveRepository(connection)
    connection.execute("BEGIN")

    try:
        with pytest.raises(dal.TransactionRequiredError):
            archives.create(file_size=1)
    finally:
        connection.execute("ROLLBACK")


def test_ownership_is_released_when_the_block_ends(connection) -> None:
    """Ownership is scoped to the block, on every exit path.

    Asserted through the public refusal rather than by reading the private
    set, so the test still means something if the bookkeeping changes.
    """
    archives = dal.ArchiveRepository(connection)

    with dal.transaction(connection):
        archives.create(file_size=1)

    # Committed and released.
    with pytest.raises(dal.TransactionRequiredError):
        archives.create(file_size=2)

    # Released after a rollback too.
    with pytest.raises(ValueError):
        with dal.transaction(connection):
            archives.create(file_size=3)
            raise ValueError("boom")

    with pytest.raises(dal.TransactionRequiredError):
        archives.create(file_size=4)

    assert archives.count() == 1


def test_ownership_is_released_when_the_commit_fails(connection) -> None:
    """The stranded-transaction path must not strand ownership either."""
    _arm_deferred_constraint(connection)
    _fail_at_commit(connection)

    with pytest.raises(dal.TransactionRequiredError):
        dal.ArchiveRepository(connection).create(file_size=1)


def test_ownership_does_not_leak_between_connections(
    database_path: Path,
) -> None:
    """One connection's transaction does not license another's writes."""
    with dal.connection_scope(database_path) as first:
        with dal.connection_scope(database_path) as second:
            with dal.transaction(first):
                dal.ArchiveRepository(first).create(file_size=1)

                with pytest.raises(dal.TransactionRequiredError):
                    dal.ArchiveRepository(second).create(file_size=2)

# --- repositories over existing entities ---------------------------------


def test_archive_round_trips(connection) -> None:
    archives = dal.ArchiveRepository(connection)
    archive_id = _seed_archive(connection, file_size=1234)

    record = archives.get(archive_id)

    assert record.archive_id == archive_id
    assert record.file_size == 1234
    # The legacy digest column stays empty: since migration 014 byte identity
    # lives in the archive's current revision, and the DAL deliberately offers
    # no way to write this column without one.
    assert record.sha256 is None
    assert not hasattr(archives, "set_sha256")


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


def test_the_allowlist_counts_match_the_tree() -> None:
    """The recorded count is the live one, not a number that drifted.

    An entry recording more calls than the file actually makes would leave
    room for a bypass to be added later without the guard noticing, which
    is the whole failure this count exists to close.
    """
    actual = dal.find_connect_sites(REPO_ROOT)

    for path, entry in dal.SANCTIONED_CONNECT_SITES.items():
        assert actual[path] == entry.calls, (
            f"{path}: allowlist records {entry.calls}, tree has {actual[path]}"
        )


def test_every_allowlist_entry_records_a_reason() -> None:
    for path, entry in dal.SANCTIONED_CONNECT_SITES.items():
        assert entry.reason.strip(), path
        assert len(entry.reason) > 40, f"{path}: reason is too thin to be useful"
        assert entry.calls >= 1, f"{path}: an entry allowing no calls is dead"


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
        tmp_path, sanctioned={"pkg/sanctioned.py": 1}
    )
    assert len(unsanctioned) == 1
    assert unsanctioned[0].startswith("pkg/rogue.py")


def test_a_second_call_inside_a_sanctioned_file_is_caught(
    tmp_path: Path,
) -> None:
    """The hole a filename-level allowlist leaves open.

    Subtracting whole filenames cleared every call in an allowlisted file,
    so a new `sqlite3.connect` added to one of the seven files that are
    already listed -- which is where database code actually gets written --
    passed the guard silently. The count is what closes it.
    """
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "sanctioned.py").write_text(
        "import sqlite3\n"
        "first = sqlite3.connect('a.db')\n"
        "second = sqlite3.connect('b.db')\n",
        encoding="utf-8",
    )

    assert dal.find_connect_sites(tmp_path) == {"pkg/sanctioned.py": 2}

    # One call is sanctioned; the second is not.
    violations = dal.unsanctioned_connect_sites(
        tmp_path, sanctioned={"pkg/sanctioned.py": 1}
    )
    assert len(violations) == 1
    assert "allowlist records 1" in violations[0]

    # Recording both makes it clean again -- the guard tracks the count,
    # it does not simply forbid a second call.
    assert (
        dal.unsanctioned_connect_sites(
            tmp_path, sanctioned={"pkg/sanctioned.py": 2}
        )
        == []
    )


def test_aliased_and_split_calls_are_counted(tmp_path: Path) -> None:
    """Four spellings the textual scan missed entirely.

    `"sqlite3.connect(" in line` matched one shape. An aliased module, a
    directly imported `connect`, an aliased `connect`, and a call split
    across lines are all the same bypass and none of them contain that
    substring.
    """
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "aliased_module.py").write_text(
        "import sqlite3 as sq\nconn = sq.connect('a.db')\n", encoding="utf-8"
    )
    (tmp_path / "pkg" / "imported_func.py").write_text(
        "from sqlite3 import connect\nconn = connect('b.db')\n",
        encoding="utf-8",
    )
    (tmp_path / "pkg" / "aliased_func.py").write_text(
        "from sqlite3 import connect as c\nconn = c('c.db')\n",
        encoding="utf-8",
    )
    (tmp_path / "pkg" / "split_call.py").write_text(
        "import sqlite3\nconn = sqlite3 . connect (\n    'd.db',\n)\n",
        encoding="utf-8",
    )

    found = dal.find_connect_sites(tmp_path)

    assert found == {
        "pkg/aliased_func.py": 1,
        "pkg/aliased_module.py": 1,
        "pkg/imported_func.py": 1,
        "pkg/split_call.py": 1,
    }, found

    # None of them are on an allowlist, so all four are violations.
    assert len(dal.unsanctioned_connect_sites(tmp_path, sanctioned={})) == 4


def test_an_unrelated_connect_is_not_counted(tmp_path: Path) -> None:
    """`connect` is a common method name; only sqlite3's is a bypass.

    Without this the guard would fire on any `something.connect(...)` --
    a socket, an SSH client, a signal -- and a guard that cries wolf gets
    an allowlist entry added to shut it up.
    """
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "unrelated.py").write_text(
        "import socket\n"
        "import sqlite3\n"
        "s = socket.socket()\n"
        "s.connect(('localhost', 1))\n",
        encoding="utf-8",
    )

    assert dal.find_connect_sites(tmp_path) == {}


def test_commented_out_calls_are_not_counted(tmp_path: Path) -> None:
    """A mention in a comment or a string is not a bypass.

    `read_guards.py` discusses `sqlite3.connect` in prose; counting that
    would make the guard fire on documentation. The parser drops comments
    outright, and a docstring is a string rather than a call.
    """
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "documented.py").write_text(
        '"""A plain sqlite3.connect( would create the file."""\n'
        "# a plain sqlite3.connect( would create the file\n"
        "VALUE = 1\n",
        encoding="utf-8",
    )

    assert dal.find_connect_sites(tmp_path) == {}


def test_a_module_that_cannot_be_parsed_is_reported_not_skipped(
    tmp_path: Path,
) -> None:
    """A file the scanner cannot read is a file it cannot clear.

    Swallowing the SyntaxError would silently drop the module from the
    scan, which is indistinguishable from the module being clean.
    """
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "broken.py").write_text(
        "def oops(:\n    pass\n", encoding="utf-8"
    )

    with pytest.raises(dal.UnparseableModuleError) as caught:
        dal.find_connect_sites(tmp_path)

    assert "pkg/broken.py" in str(caught.value)
