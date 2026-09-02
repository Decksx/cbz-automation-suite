"""Migration 014 and the immutable revision model.

A revision is one unique byte state of one logical archive. Three properties
have to hold together, and each is asserted here against a real migrated
database rather than described:

* a revision is never rewritten -- re-seeing bytes appends an observation, and
  a new byte state appends a generation beside the old one, so evidence that
  two generations were distinct cannot be destroyed by later processing;
* a revision belongs to exactly one archive, structurally -- lineage carries
  `archive_id` into its foreign key, so a chain cannot wander into another
  identity;
* which revision is *current* is a pointer on `archive_files`, not a flag on
  the revision, so there is only one thing to read and only one thing to move.

Archive 37704 is the case that motivated all of this: three byte generations
of one work. Archive 58201 shares a historical digest with it but is a
*supersession* case -- a different identity -- and the tests below pin that
the shared digest does not merge them, because merging them is the specific
mistake the model exists to prevent.

Everything runs against a temporary database. Nothing here opens production.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from comic_automation.database import dal
from comic_automation.database import migrations as migration_module
from comic_automation.database import (
    protected_migrations as protected_module,
)
from comic_automation.database.connection import connect_database
from comic_automation.database.migrations import apply_migrations

MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHARED = "5" * 64


def _apply_through(connection: sqlite3.Connection, version: int) -> None:
    """Apply migrations up to and including *version*.

    Used to build a schema-13 database so the 13 -> 14 upgrade can be tested
    as an upgrade. Applying every migration to an empty file exercises table
    creation but never the backfill, which is the part that touches existing
    rows and therefore the part that can lose them.

    The patch targets `protected_module.discover_migrations`, not
    `migration_module`'s. Discovery moved when the protected-migration
    guard was made coherent: `apply_migrations` no longer scans the
    directory itself, it asks `take_migration_snapshot()` for one reading,
    and that function binds `discover_migrations` into
    `protected_migrations`' namespace at import time. Patching the module
    that *defines* the name therefore no longer reaches the call, and the
    silent result was that this helper applied every migration instead of
    stopping at `version`.

    Which is why the outcome is asserted rather than assumed. The next time
    that call site moves, this fails here with a message naming the helper
    instead of surfacing as six unrelated backfill failures downstream.
    """
    real = protected_module.discover_migrations
    try:
        protected_module.discover_migrations = lambda directory: [
            path
            for path in real(directory)
            if migration_module.migration_version(path) <= version
        ]
        apply_migrations(connection, MIGRATIONS)
    finally:
        protected_module.discover_migrations = real

    highest = connection.execute(
        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
    ).fetchone()[0]

    assert highest == version, (
        f"_apply_through({version}) left the schema at {highest}; the patch "
        "no longer reaches the discovery call apply_migrations() uses"
    )


def _seed_schema_13(connection: sqlite3.Connection) -> None:
    """A population with every shape the backfill has to handle.

    Five archives: two byte-identical to each other (a duplicate group that
    must stay two identities), one with its own digest, and two that were
    never hashed -- the shape of the 147 archives that have no archive-level
    SHA-256 in production.
    """
    connection.execute("BEGIN IMMEDIATE")

    for archive_id in range(1, 6):
        connection.execute(
            "INSERT INTO archive_files (id, file_size, page_count) "
            "VALUES (?, ?, ?)",
            (archive_id, 1000 + archive_id, 10 + archive_id),
        )

    for archive_id, digest in ((1, SHARED), (2, SHARED), (3, SHA_C)):
        connection.execute(
            """
            INSERT INTO archive_hashes (
                archive_id, algorithm, algorithm_version, digest,
                file_size, modified_time_ns, bytes_read
            )
            VALUES (?, 'sha256', '1', ?, ?, ?, ?)
            """,
            (archive_id, digest, 1000 + archive_id, 111, 1000 + archive_id),
        )

    for archive_id in (1, 2, 3, 4):
        connection.execute(
            """
            INSERT INTO archive_content_signatures (
                archive_id, algorithm, algorithm_version, digest, page_count,
                image_bytes, source_file_size, source_modified_time_ns
            )
            VALUES (?, 'ordered-page', '1', ?, ?, ?, ?, ?)
            """,
            (
                archive_id,
                "s" * 63 + str(archive_id),
                10 + archive_id,
                99,
                1000 + archive_id,
                111,
            ),
        )

    connection.execute("COMMIT")


@pytest.fixture()
def legacy_database(tmp_path: Path) -> Path:
    """A populated schema-13 database, ready to be upgraded."""
    path = tmp_path / "legacy.db"
    connection = connect_database(path)

    try:
        _apply_through(connection, 13)
        _seed_schema_13(connection)
    finally:
        connection.close()

    return path


@pytest.fixture()
def database_path(tmp_path: Path) -> Path:
    """An empty database at the current schema."""
    path = tmp_path / "revisions.db"
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


def _new_archive(conn: sqlite3.Connection, file_size: int = 4096) -> int:
    """A new archive, which the schema immediately gives a provisional origin.

    Every archive created after migration 014 starts with one provisional
    revision at ordinal 1, because that is the truth at that instant: the
    identity row exists and nothing has hashed its bytes. Established
    generations are therefore appended from ordinal 2 onward, and tests that
    care about byte generations filter for them rather than counting rows.
    """
    with dal.transaction(conn):
        return dal.ArchiveRepository(conn).create(file_size=file_size)


def _established(
    revisions: "dal.RevisionRepository", archive_id: int
) -> list:
    """Only the generations whose bytes are known."""
    return [
        record
        for record in revisions.lineage_for(archive_id)
        if record.is_established
    ]


def _provisional_origin(
    revisions: "dal.RevisionRepository", archive_id: int
):
    """The ordinal-1 placeholder every archive is created with."""
    origin = revisions.lineage_for(archive_id)[0]
    assert origin.revision_ordinal == 1 and not origin.is_established
    return origin


# --- the forward migration -----------------------------------------------


def test_the_upgrade_gives_every_archive_exactly_one_initial_revision(
    legacy_database: Path,
) -> None:
    """The Step 2 acceptance criterion, asserted on an upgrade.

    Pre/post counts are compared rather than only post counts: a backfill that
    dropped or merged identities would still leave a self-consistent database.
    """
    connection = connect_database(legacy_database)

    try:
        before = connection.execute(
            "SELECT COUNT(*) FROM archive_files"
        ).fetchone()[0]

        assert apply_migrations(connection, MIGRATIONS) == [14]
        connection.commit()

        after = connection.execute(
            "SELECT COUNT(*) FROM archive_files"
        ).fetchone()[0]
        revisions = connection.execute(
            "SELECT COUNT(*) FROM archive_revisions"
        ).fetchone()[0]

        assert after == before == 5, "no archive identity may be added or lost"
        assert revisions == 5, "exactly one initial revision per archive"

        # Every archive points at a revision, and at one of its own.
        orphans = connection.execute(
            """
            SELECT COUNT(*) FROM archive_files AS a
            WHERE a.current_revision_id IS NULL
               OR NOT EXISTS (
                   SELECT 1 FROM archive_revisions AS r
                   WHERE r.id = a.current_revision_id AND r.archive_id = a.id
               )
            """
        ).fetchone()[0]
        assert orphans == 0

        ordinals = connection.execute(
            "SELECT DISTINCT revision_ordinal FROM archive_revisions"
        ).fetchall()
        assert [row[0] for row in ordinals] == [1]
    finally:
        connection.close()


def test_the_upgrade_splits_established_from_provisional(
    legacy_database: Path,
) -> None:
    """Un-hashed archives get a modelled state, not a silent NULL.

    Three of the five seeded archives have an `archive_hashes` row; two never
    did. In production that split was 59,541 / 147.
    """
    connection = connect_database(legacy_database)

    try:
        apply_migrations(connection, MIGRATIONS)
        connection.commit()

        counts = dict(
            connection.execute(
                "SELECT identity_state, COUNT(*) FROM archive_revisions "
                "GROUP BY identity_state"
            ).fetchall()
        )
        assert counts == {"established": 3, "provisional": 2}

        # The tie between state and data holds in both directions.
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM archive_revisions "
                "WHERE (identity_state = 'established') "
                "   != (archive_sha256 IS NOT NULL)"
            ).fetchone()[0]
            == 0
        )

        # Every backfilled row is labelled as such and carries evidence.
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM archive_revisions "
                "WHERE source != 'migration_backfill' OR trim(evidence) = ''"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_byte_identical_archives_stay_two_identities(
    legacy_database: Path,
) -> None:
    """A duplicate group must not be merged as a migration side effect.

    Archives 1 and 2 hold the same bytes. They keep separate `archive_files`
    rows and separate revisions that happen to share a digest -- which is why
    `archive_sha256` is indexed but not globally unique.
    """
    connection = connect_database(legacy_database)

    try:
        apply_migrations(connection, MIGRATIONS)
        connection.commit()

        sharing = connection.execute(
            "SELECT archive_id FROM archive_revisions "
            "WHERE archive_sha256 = ? ORDER BY archive_id",
            (SHARED,),
        ).fetchall()

        assert [row[0] for row in sharing] == [1, 2]
        assert (
            connection.execute(
                "SELECT COUNT(DISTINCT id) FROM archive_files"
            ).fetchone()[0]
            == 5
        )
    finally:
        connection.close()


def test_the_upgrade_leaves_the_database_consistent(
    legacy_database: Path,
) -> None:
    """Foreign keys and page structure both check out afterwards."""
    connection = connect_database(legacy_database)

    try:
        apply_migrations(connection, MIGRATIONS)
        connection.commit()

        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        )
    finally:
        connection.close()


def test_the_upgrade_is_idempotent(legacy_database: Path) -> None:
    """Re-running must not mint a second generation for every archive.

    `apply_migrations` already skips recorded versions, so this drives the
    backfill's own NOT EXISTS guard directly -- the protection that matters if
    the statement is ever replayed by hand.
    """
    connection = connect_database(legacy_database)

    try:
        apply_migrations(connection, MIGRATIONS)
        connection.commit()
        first = connection.execute(
            "SELECT COUNT(*) FROM archive_revisions"
        ).fetchone()[0]

        statements = migration_module.iter_sql_statements(
            (MIGRATIONS / "014_archive_revisions.sql").read_text(
                encoding="utf-8-sig"
            )
        )
        # Matched on content, not on a `startswith`: iter_sql_statements keeps
        # each statement's leading comment block, so these do not start with
        # the keyword.
        # Narrowed to the backfill specifically: the auto-provision trigger
        # also contains "INSERT INTO archive_revisions", and matching both
        # would have this replay a trigger body as a bare statement.
        backfill = [
            s
            for s in statements
            if "INSERT INTO archive_revisions" in s
            and "FROM archive_files AS a" in s
        ]
        assert len(backfill) == 1, "expected exactly one backfill INSERT"

        connection.execute("BEGIN IMMEDIATE")
        connection.execute(backfill[0])
        connection.execute("COMMIT")

        assert (
            connection.execute(
                "SELECT COUNT(*) FROM archive_revisions"
            ).fetchone()[0]
            == first
        )
    finally:
        connection.close()


# --- rollback and recovery -----------------------------------------------


def test_a_failing_migration_leaves_the_database_at_thirteen(
    legacy_database: Path, tmp_path: Path
) -> None:
    """The rollback path, driven by a real failure inside 014.

    `apply_migrations` wraps each file in BEGIN IMMEDIATE ... COMMIT. This
    injects a failing statement into the middle of 014 and asserts the
    database is left exactly at 13 with none of 014's objects -- so a failed
    upgrade is recoverable by fixing the cause and re-running, with no
    half-applied state to clean up first.
    """
    broken = tmp_path / "broken_migrations"
    shutil.copytree(MIGRATIONS, broken)
    target = broken / "014_archive_revisions.sql"
    target.write_text(
        target.read_text(encoding="utf-8")
        + "\nINSERT INTO archive_revisions (archive_id, revision_ordinal, "
        "evidence) VALUES (999999, 1, 'no such archive');\n",
        encoding="utf-8",
    )

    connection = connect_database(legacy_database)

    try:
        with pytest.raises(sqlite3.Error):
            apply_migrations(connection, broken)

        assert (
            connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
            == 13
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE name = 'archive_revisions'"
            ).fetchone()[0]
            == 0
        )
        assert "current_revision_id" not in {
            row[1]
            for row in connection.execute("PRAGMA table_info(archive_files)")
        }
        # The pre-existing data is untouched, which is what makes re-running
        # after a fix safe rather than merely possible.
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM archive_files"
            ).fetchone()[0]
            == 5
        )
    finally:
        connection.close()


def test_a_copy_taken_before_the_upgrade_restores_the_old_schema(
    legacy_database: Path, tmp_path: Path
) -> None:
    """The documented recovery: restore the copy, and 14 is simply gone.

    This is the rollback procedure for a migration that succeeded but is
    afterwards judged wrong. 014 defines no down-migration -- dropping a table
    that already holds revision evidence would destroy it -- so restoring a
    pre-upgrade copy is the supported path, and it is tested rather than
    assumed.
    """
    backup = tmp_path / "pre-upgrade.db"
    shutil.copy2(legacy_database, backup)

    connection = connect_database(legacy_database)

    try:
        apply_migrations(connection, MIGRATIONS)
        connection.commit()
        assert (
            connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
            == 14
        )
    finally:
        connection.close()

    shutil.copy2(backup, legacy_database)

    restored = connect_database(legacy_database)

    try:
        assert (
            restored.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
            == 13
        )
        assert (
            restored.execute(
                "SELECT COUNT(*) FROM archive_files"
            ).fetchone()[0]
            == 5
        )
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

        # And the upgrade can simply be run again on the restored copy.
        assert apply_migrations(restored, MIGRATIONS) == [14]
        restored.commit()
        assert (
            restored.execute(
                "SELECT COUNT(*) FROM archive_revisions"
            ).fetchone()[0]
            == 5
        )
    finally:
        restored.close()


# --- schema invariants ---------------------------------------------------


def test_a_revision_cannot_be_rewritten(connection) -> None:
    """Immutability is the whole model; it is enforced in the database.

    Re-inspecting archive 37704 in place would overwrite one byte generation
    with another and destroy the evidence that they were distinct. An UPDATE
    is refused so no code path -- application, CLI, or an operator's sqlite3
    prompt -- can do it by accident.
    """
    archive_id = _new_archive(connection)
    revisions = dal.RevisionRepository(connection)

    with dal.transaction(connection):
        revision_id, _ = revisions.record_or_reuse(
            archive_id=archive_id, archive_sha256=SHA_A, evidence="first"
        )

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with dal.transaction(connection):
            connection.execute(
                "UPDATE archive_revisions SET archive_sha256 = ? WHERE id = ?",
                (SHA_B, revision_id),
            )

    assert revisions.get(revision_id).archive_sha256 == SHA_A


def test_an_established_revision_cannot_be_deleted(connection) -> None:
    """Deleting it would erase the evidence that those bytes existed."""
    archive_id = _new_archive(connection)
    revisions = dal.RevisionRepository(connection)

    with dal.transaction(connection):
        revision_id, _ = revisions.record_or_reuse(
            archive_id=archive_id, archive_sha256=SHA_A, evidence="first"
        )

    with pytest.raises(sqlite3.IntegrityError, match="evidence"):
        with dal.transaction(connection):
            connection.execute(
                "DELETE FROM archive_revisions WHERE id = ?", (revision_id,)
            )

    assert revisions.get(revision_id) is not None


def test_a_provisional_revision_cannot_be_deleted_either(
    connection,
) -> None:
    """A provisional row records a real historical state, so it is evidence.

    It says the identity existed and its bytes were unknown between two
    dates, and every observation made during that period hangs off it.
    Deleting it when a digest finally arrives would erase that uncertainty
    and cascade the observations away with it.
    """
    archive_id = _new_archive(connection)
    revisions = dal.RevisionRepository(connection)
    origin = _provisional_origin(revisions, archive_id)

    with pytest.raises(sqlite3.IntegrityError, match="evidence"):
        with dal.transaction(connection):
            connection.execute(
                "DELETE FROM archive_revisions WHERE id = ?",
                (origin.revision_id,),
            )

    assert revisions.get(origin.revision_id) is not None


def test_identity_state_and_the_digest_cannot_disagree(connection) -> None:
    """'established' can never mean "we lost the digest", and vice versa."""
    archive_id = _new_archive(connection)

    for state, digest, label in (
        ("established", None, "established without a digest"),
        ("provisional", SHA_A, "provisional carrying a digest"),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            with dal.transaction(connection):
                connection.execute(
                    "INSERT INTO archive_revisions (archive_id, "
                    "revision_ordinal, identity_state, archive_sha256, "
                    "evidence) VALUES (?, 1, ?, ?, ?)",
                    (archive_id, state, digest, label),
                )


def test_an_archive_may_hold_only_one_provisional_revision(
    connection,
) -> None:
    """UNIQUE(archive_id, archive_sha256) cannot express this.

    SQLite counts every NULL as distinct, so that constraint permits any
    number of provisional rows. The partial index is what caps them, and
    without the cap an archive could accumulate placeholders for the same
    unknown bytes.
    """
    archive_id = _new_archive(connection)
    revisions = dal.RevisionRepository(connection)
    origin = _provisional_origin(revisions, archive_id)

    # A second placeholder at a valid ordinal behind a valid predecessor, with
    # a valid state/digest pairing: the partial unique index is the only
    # constraint left that can refuse it. Inserting it at ordinal 2 with a
    # NULL predecessor would be caught by the ordinal CHECK instead, and the
    # test would pass while the index did nothing -- which is exactly what it
    # did until a bypass run removed the index and nothing failed.
    #
    # The index reports as a UNIQUE failure on archive_id, the column it
    # indexes, matched explicitly so this cannot start passing on some other
    # constraint's error.
    with pytest.raises(
        sqlite3.IntegrityError, match=r"archive_revisions\.archive_id"
    ):
        with dal.transaction(connection):
            connection.execute(
                "INSERT INTO archive_revisions (archive_id, "
                "revision_ordinal, identity_state, archive_sha256, "
                "previous_revision_id, evidence) "
                "VALUES (?, 2, 'provisional', NULL, ?, 'second placeholder')",
                (archive_id, origin.revision_id),
            )

    assert len(revisions.lineage_for(archive_id)) == 1


def test_one_archive_cannot_hold_the_same_byte_state_twice(
    connection,
) -> None:
    archive_id = _new_archive(connection)
    revisions = dal.RevisionRepository(connection)

    with dal.transaction(connection):
        first_id, _ = revisions.record_or_reuse(
            archive_id=archive_id, archive_sha256=SHA_A, evidence="first"
        )

    with pytest.raises(sqlite3.IntegrityError):
        with dal.transaction(connection):
            connection.execute(
                "INSERT INTO archive_revisions (archive_id, "
                "revision_ordinal, archive_sha256, previous_revision_id, "
                "evidence) VALUES (?, 3, ?, ?, 'duplicate bytes')",
                (archive_id, SHA_A, first_id),
            )


def test_lineage_cannot_cross_into_another_archive(connection) -> None:
    """The 37704/58201 failure, refused by a composite foreign key.

    `previous_revision_id` is paired with `archive_id` in the key, so a
    predecessor belonging to a different identity is not a valid parent row.
    """
    first = _new_archive(connection)
    second = _new_archive(connection)
    revisions = dal.RevisionRepository(connection)

    with dal.transaction(connection):
        foreign_id, _ = revisions.record_or_reuse(
            archive_id=first, archive_sha256=SHA_A, evidence="belongs to first"
        )

    with pytest.raises(sqlite3.IntegrityError):
        with dal.transaction(connection):
            connection.execute(
                "INSERT INTO archive_revisions (archive_id, "
                "revision_ordinal, archive_sha256, previous_revision_id, "
                "evidence) VALUES (?, 2, ?, ?, 'stolen parent')",
                (second, SHA_B, foreign_id),
            )


def test_lineage_must_be_a_sequential_chain(connection) -> None:
    """A predecessor must be the immediately preceding ordinal.

    Without this a chain could skip a generation, and "three generations"
    would stop describing a sequence -- which is exactly what the 37704
    evidence has to preserve.
    """
    archive_id = _new_archive(connection)
    revisions = dal.RevisionRepository(connection)

    with dal.transaction(connection):
        first_id, _ = revisions.record_or_reuse(
            archive_id=archive_id, archive_sha256=SHA_A, evidence="gen 1"
        )

    with pytest.raises(sqlite3.IntegrityError, match="previous ordinal"):
        with dal.transaction(connection):
            connection.execute(
                "INSERT INTO archive_revisions (archive_id, "
                "revision_ordinal, archive_sha256, previous_revision_id, "
                "evidence) VALUES (?, 4, ?, ?, 'skips an ordinal')",
                (archive_id, SHA_C, first_id),
            )


def test_the_first_revision_has_no_predecessor_and_later_ones_do(
    connection,
) -> None:
    archive_id = _new_archive(connection)
    revisions = dal.RevisionRepository(connection)

    with dal.transaction(connection):
        first_id, _ = revisions.record_or_reuse(
            archive_id=archive_id, archive_sha256=SHA_A, evidence="gen 1"
        )

    # A later row with no predecessor.
    with pytest.raises(sqlite3.IntegrityError):
        with dal.transaction(connection):
            connection.execute(
                "INSERT INTO archive_revisions (archive_id, "
                "revision_ordinal, archive_sha256, evidence) "
                "VALUES (?, 3, ?, 'rootless')",
                (archive_id, SHA_C),
            )

    # A second root.
    with pytest.raises(sqlite3.IntegrityError):
        with dal.transaction(connection):
            connection.execute(
                "INSERT INTO archive_revisions (archive_id, "
                "revision_ordinal, archive_sha256, evidence) "
                "VALUES (?, 1, ?, 'second root')",
                (archive_id, SHA_C),
            )

    # The archive's only root is the provisional origin it was created with.
    assert _provisional_origin(revisions, archive_id).previous_revision_id is None
    assert revisions.get(first_id).previous_revision_id is not None


def test_a_revision_requires_non_blank_evidence(connection) -> None:
    """Whitespace is not evidence.

    SQLite's one-argument trim() strips spaces only, so a lone tab would pass
    a naive check. The schema passes an explicit character set, the same form
    migration 012's test found was needed.
    """
    archive_id = _new_archive(connection)

    for blank in ("", "   ", "\t", "\n", " \r\n\t "):
        with pytest.raises(sqlite3.IntegrityError):
            with dal.transaction(connection):
                connection.execute(
                    "INSERT INTO archive_revisions (archive_id, "
                    "revision_ordinal, archive_sha256, evidence) "
                    "VALUES (?, 1, ?, ?)",
                    (archive_id, SHA_A, blank),
                )


def test_the_current_pointer_must_name_a_revision_of_its_own_archive(
    connection,
) -> None:
    """ALTER TABLE cannot express a composite foreign key, so triggers do.

    Both directions are covered because there are two ways in: repointing an
    existing archive, and inserting one that already carries a pointer.
    """
    first = _new_archive(connection)
    second = _new_archive(connection)
    revisions = dal.RevisionRepository(connection)

    with dal.transaction(connection):
        foreign_id, _ = revisions.record_or_reuse(
            archive_id=first, archive_sha256=SHA_A, evidence="belongs to first"
        )

    with pytest.raises(sqlite3.IntegrityError, match="must name a revision"):
        with dal.transaction(connection):
            revisions.set_current(second, foreign_id)

    with pytest.raises(sqlite3.IntegrityError, match="must name a revision"):
        with dal.transaction(connection):
            connection.execute(
                "INSERT INTO archive_files (id, file_size, "
                "current_revision_id) VALUES (?, 1, ?)",
                (9999, foreign_id),
            )

    # The same pointer is accepted for the archive that owns it.
    with dal.transaction(connection):
        revisions.set_current(first, foreign_id)

    assert revisions.current_for(first).revision_id == foreign_id


def test_an_established_revision_cannot_be_cherry_picked_from_a_live_archive(
    connection,
) -> None:
    """The other half of the delete guard, and the half that must still bite.

    Paired with `test_an_archive_can_still_be_deleted`: together they pin both
    readings of the measured SQLite behaviour the guard depends on. If cascade
    ordering ever changed so the parent stayed visible, the deletion test
    fails; if it changed so the parent was never visible, this one fails and
    the guard is shown to have been silently disarmed.
    """
    archive_id = _new_archive(connection)
    revisions = dal.RevisionRepository(connection)

    with dal.transaction(connection):
        gen1, _ = revisions.record_or_reuse(
            archive_id=archive_id, archive_sha256=SHA_A, evidence="gen 1"
        )
        gen2, _ = revisions.record_or_reuse(
            archive_id=archive_id, archive_sha256=SHA_B, evidence="gen 2"
        )
        revisions.set_current(archive_id, gen2)

    # The tip, with the archive still present.
    with pytest.raises(sqlite3.IntegrityError, match="evidence"):
        with dal.transaction(connection):
            connection.execute(
                "DELETE FROM archive_revisions WHERE id = ?", (gen2,)
            )

    # Provisional origin plus the two established generations.
    assert len(revisions.lineage_for(archive_id)) == 3
    assert len(_established(revisions, archive_id)) == 2


def test_an_archive_can_still_be_deleted(connection) -> None:
    """The deferred foreign key and the scoped delete guard, together.

    Deleting an archive cascades its revisions away while the archive row
    still points at one. Two things have to be right for this to work: the
    pointer's foreign key is deferred, so the intermediate state is allowed;
    and the established-revision delete guard is scoped to live archives, so
    the cascade is not aborted. An unqualified guard would make every archive
    permanently undeletable after this migration.
    """
    archive_id = _new_archive(connection)
    revisions = dal.RevisionRepository(connection)

    with dal.transaction(connection):
        revision_id, _ = revisions.record_or_reuse(
            archive_id=archive_id, archive_sha256=SHA_A, evidence="only"
        )
        revisions.set_current(archive_id, revision_id)

    with dal.transaction(connection):
        connection.execute(
            "DELETE FROM archive_files WHERE id = ?", (archive_id,)
        )

    assert revisions.lineage_for(archive_id) == []
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


# --- every archive has a current revision, for the life of the schema ----
#
# The backfill only covers archives that existed when 014 ran. Everything
# discovered afterwards arrives through an INSERT the backfill never sees, so
# the invariant has to be enforced by the schema or it is not enforced at all.


def test_a_repository_created_archive_has_a_current_revision(
    connection,
) -> None:
    """`ArchiveRepository.create()` inserts no revision of its own.

    Before the trigger existed, this transaction committed an archive with
    `current_revision_id` NULL, and nothing ever revisited it.
    """
    with dal.transaction(connection):
        archive_id = dal.ArchiveRepository(connection).create(file_size=4096)

        # Already true inside the transaction, not repaired at commit.
        assert (
            connection.execute(
                "SELECT current_revision_id FROM archive_files WHERE id = ?",
                (archive_id,),
            ).fetchone()[0]
            is not None
        )

    current = dal.RevisionRepository(connection).current_for(archive_id)

    assert current is not None
    assert current.archive_id == archive_id
    assert current.revision_ordinal == 1
    assert current.identity_state == "provisional"


def test_a_raw_sql_created_archive_has_a_current_revision(
    connection,
) -> None:
    """The invariant is not a DAL convention.

    An INSERT issued from a CLI, a migration, or an operator's sqlite3
    prompt gets the same treatment, which is the difference between an
    invariant and a house rule.
    """
    with dal.transaction(connection):
        connection.execute(
            "INSERT INTO archive_files (id, file_size) VALUES (4242, 99)"
        )

    row = connection.execute(
        "SELECT current_revision_id FROM archive_files WHERE id = 4242"
    ).fetchone()

    assert row[0] is not None

    current = dal.RevisionRepository(connection).current_for(4242)
    assert current.archive_id == 4242
    assert current.identity_state == "provisional"

    # And no archive anywhere is left without one.
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM archive_files WHERE current_revision_id IS NULL"
        ).fetchone()[0]
        == 0
    )


def test_the_current_pointer_cannot_be_cleared(connection) -> None:
    """The other end of the invariant.

    Auto-provisioning closes the window at INSERT; without this, any later
    UPDATE could reopen it and leave a live archive pointing at nothing.
    """
    archive_id = _new_archive(connection)

    with pytest.raises(sqlite3.IntegrityError, match="cannot be cleared"):
        with dal.transaction(connection):
            connection.execute(
                "UPDATE archive_files SET current_revision_id = NULL "
                "WHERE id = ?",
                (archive_id,),
            )

    assert (
        dal.RevisionRepository(connection).current_for(archive_id) is not None
    )


def test_clearing_every_pointer_at_once_is_refused(connection) -> None:
    """The shape a careless repair actually takes.

    A blanket UPDATE with no WHERE clause is how this would really happen,
    and it must fail on the first row rather than partway through.
    """
    first = _new_archive(connection)
    second = _new_archive(connection)

    with pytest.raises(sqlite3.IntegrityError, match="cannot be cleared"):
        with dal.transaction(connection):
            connection.execute(
                "UPDATE archive_files SET current_revision_id = NULL"
            )

    revisions = dal.RevisionRepository(connection)
    assert revisions.current_for(first) is not None
    assert revisions.current_for(second) is not None


def test_the_migration_leaves_no_archive_without_a_current_revision(
    legacy_database: Path,
) -> None:
    """Backfilled archives and newly inserted ones both satisfy it.

    The upgrade covers the five that existed; the trigger covers the sixth,
    inserted afterwards. Asserted together because the invariant is only
    useful if it holds across that boundary.
    """
    connection = connect_database(legacy_database)

    try:
        apply_migrations(connection, MIGRATIONS)
        connection.commit()

        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO archive_files (id, file_size) VALUES (99, 1)"
        )
        connection.execute("COMMIT")

        assert (
            connection.execute(
                "SELECT COUNT(*) FROM archive_files "
                "WHERE current_revision_id IS NULL"
            ).fetchone()[0]
            == 0
        )

        # The five backfilled archives keep established identities; only the
        # one created afterwards is provisional.
        states = dict(
            connection.execute(
                """
                SELECT r.identity_state, COUNT(*)
                FROM archive_files AS a
                JOIN archive_revisions AS r ON r.id = a.current_revision_id
                GROUP BY r.identity_state
                """
            ).fetchall()
        )
        assert states == {"established": 3, "provisional": 3}
    finally:
        connection.close()


def test_the_lineage_key_protects_successors_without_the_delete_guard(
    connection,
) -> None:
    """Defence in depth for Step 3's reviewed pruning.

    The delete guard refuses removing any revision while its archive exists,
    so today nothing reaches the lineage foreign key. Step 3 introduces
    reviewed pruning and will relax that guard, and at that moment the key
    becomes the only thing between "prune an old revision" and "erase every
    generation after it".

    So the guard is dropped here -- in this temporary database only -- and
    the key is asserted alone.

    The current pointer is deliberately parked on the provisional origin,
    outside the chain being pruned. An earlier version of this test left it
    on the newest generation, and it passed under `ON DELETE CASCADE` for
    the wrong reason: the cascade reached the current revision and
    `archive_files.current_revision_id` aborted the statement, so the
    refusal came from a different constraint entirely. With the pointer out
    of the way, only the lineage key can refuse this.
    """
    archive_id = _new_archive(connection)
    revisions = dal.RevisionRepository(connection)
    origin = _provisional_origin(revisions, archive_id)

    with dal.transaction(connection):
        gen1, _ = revisions.record_or_reuse(
            archive_id=archive_id, archive_sha256=SHA_A, evidence="gen 1"
        )
        gen2, _ = revisions.record_or_reuse(
            archive_id=archive_id, archive_sha256=SHA_B, evidence="gen 2"
        )
        gen3, _ = revisions.record_or_reuse(
            archive_id=archive_id, archive_sha256=SHA_C, evidence="gen 3"
        )
        revisions.set_current(archive_id, origin.revision_id)

    before = revisions.lineage_for(archive_id)
    assert len(before) == 4  # provisional origin plus three generations

    connection.execute("DROP TRIGGER trg_archive_revisions_not_deletable")

    try:
        with pytest.raises(sqlite3.IntegrityError):
            with dal.transaction(connection):
                connection.execute(
                    "DELETE FROM archive_revisions WHERE id = ?", (gen1,)
                )

        # What survived is the assertion that matters. A cascading key would
        # have taken gen2 and gen3 with gen1 and reported success.
        assert revisions.lineage_for(archive_id) == before
        assert revisions.get(gen2) is not None
        assert revisions.get(gen3) is not None
    finally:
        # Leave the fixture's database as the migration built it.
        connection.executescript(
            """
            CREATE TRIGGER trg_archive_revisions_not_deletable
            BEFORE DELETE ON archive_revisions
            FOR EACH ROW
            WHEN EXISTS (SELECT 1 FROM archive_files WHERE id = OLD.archive_id)
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'a revision cannot be deleted while its archive exists; it is evidence'
                );
            END;
            """
        )


# --- archive 37704: three generations, and 58201 kept apart --------------


def test_archive_37704_carries_three_distinct_generations(
    connection,
) -> None:
    """The case that motivates the whole model.

    Three byte generations of one work, recorded as three revisions of one
    archive_id. Each generation keeps its own digest and its own evidence, and
    the earlier two survive the arrival of the third -- which is precisely
    what re-inspecting in place would have destroyed.
    """
    archive_37704 = _new_archive(connection, file_size=41 * 1024)
    revisions = dal.RevisionRepository(connection)
    generations = (
        (SHA_A, "generation 1: as first discovered"),
        (SHA_B, "generation 2: after re-encode"),
        (SHA_C, "generation 3: after metadata rewrite"),
    )

    recorded = []

    for digest, evidence in generations:
        with dal.transaction(connection):
            revision_id, created = revisions.record_or_reuse(
                archive_id=archive_37704,
                archive_sha256=digest,
                evidence=evidence,
            )
            revisions.set_current(archive_37704, revision_id)
        assert created is True
        recorded.append(revision_id)

    origin = _provisional_origin(revisions, archive_37704)
    established = _established(revisions, archive_37704)

    # Three byte generations, appended after the provisional origin the
    # archive was created with -- so ordinals 2, 3, 4.
    assert [r.revision_ordinal for r in established] == [2, 3, 4]
    assert [r.archive_sha256 for r in established] == [SHA_A, SHA_B, SHA_C]
    assert [r.previous_revision_id for r in established] == [
        origin.revision_id,
        recorded[0],
        recorded[1],
    ]
    assert [r.evidence for r in established] == [e for _, e in generations]
    assert revisions.current_for(archive_37704).revision_id == recorded[2]

    # The earlier generations survived the arrival of the third, which is
    # precisely what re-inspecting in place would have destroyed.
    assert revisions.get(recorded[0]).archive_sha256 == SHA_A
    assert revisions.get(recorded[1]).archive_sha256 == SHA_B


def test_37704_and_58201_are_not_merged_by_their_shared_digest(
    connection,
) -> None:
    """The specific trap the roadmap names.

    The two archives share a historical digest. They are nevertheless separate
    identities related by *supersession*, not by revision, and a query that
    grouped on the shared digest would collapse them into one. The digest is
    therefore indexed but not unique, and lookups return every archive holding
    it rather than a single winner.
    """
    archive_37704 = _new_archive(connection, file_size=41 * 1024)
    archive_58201 = _new_archive(connection, file_size=41 * 1024)
    revisions = dal.RevisionRepository(connection)

    with dal.transaction(connection):
        # 37704's first generation is the shared byte state.
        gen1, _ = revisions.record_or_reuse(
            archive_id=archive_37704,
            archive_sha256=SHARED,
            evidence="37704 generation 1",
        )
        revisions.set_current(archive_37704, gen1)

        # 58201 holds the same bytes, as its own identity.
        other, _ = revisions.record_or_reuse(
            archive_id=archive_58201,
            archive_sha256=SHARED,
            evidence="58201, same bytes, different identity",
        )
        revisions.set_current(archive_58201, other)

    with dal.transaction(connection):
        gen2, _ = revisions.record_or_reuse(
            archive_id=archive_37704,
            archive_sha256=SHA_B,
            evidence="37704 generation 2",
        )
        revisions.set_current(archive_37704, gen2)

    # Both archives are returned; neither is chosen for the caller.
    assert revisions.archives_sharing_digest(SHARED) == sorted(
        [archive_37704, archive_58201]
    )

    # The shared digest did not pull 58201 into 37704's lineage.
    assert len(_established(revisions, archive_37704)) == 2
    assert len(_established(revisions, archive_58201)) == 1
    assert revisions.current_for(archive_58201).revision_id == other
    assert revisions.current_for(archive_37704).revision_id == gen2


def test_supersession_and_revision_describe_different_things(
    connection,
) -> None:
    """Recording a supersession changes no revision, and the reverse.

    The two relationships are kept in separate tables on purpose. This asserts
    they do not interfere: superseding 58201 into 37704 leaves both lineages
    exactly as they were, and 37704 keeps its own generations.
    """
    archive_37704 = _new_archive(connection)
    archive_58201 = _new_archive(connection)
    revisions = dal.RevisionRepository(connection)

    with dal.transaction(connection):
        gen1, _ = revisions.record_or_reuse(
            archive_id=archive_37704, archive_sha256=SHARED, evidence="gen 1"
        )
        revisions.set_current(archive_37704, gen1)
        other, _ = revisions.record_or_reuse(
            archive_id=archive_58201, archive_sha256=SHA_C, evidence="58201"
        )
        revisions.set_current(archive_58201, other)

    before = (
        revisions.lineage_for(archive_37704),
        revisions.lineage_for(archive_58201),
    )

    with dal.transaction(connection):
        connection.execute(
            """
            INSERT INTO archive_supersessions (
                predecessor_archive_id, successor_archive_id, reason, evidence
            )
            VALUES (?, ?, 'reclassification minted a second identity',
                    'measured 2026-08-20')
            """,
            (archive_58201, archive_37704),
        )

    assert (
        revisions.lineage_for(archive_37704),
        revisions.lineage_for(archive_58201),
    ) == before

    # The supersession is recorded, and it carries no byte state at all --
    # which is why it cannot express a generation.
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(archive_supersessions)")
    }
    assert not columns & {"archive_sha256", "digest", "revision_id"}


# --- repository behaviour through the DAL --------------------------------


def test_re_seeing_known_bytes_reuses_the_revision(connection) -> None:
    """A revision is a content state, not a sighting.

    A file that keeps being rediscovered unchanged must not look like a file
    that keeps changing, so the same digest returns the existing revision and
    the caller records an observation instead.
    """
    archive_id = _new_archive(connection)
    revisions = dal.RevisionRepository(connection)

    with dal.transaction(connection):
        first_id, created = revisions.record_or_reuse(
            archive_id=archive_id, archive_sha256=SHA_A, evidence="first sight"
        )
    assert created is True

    with dal.transaction(connection):
        again_id, created_again = revisions.record_or_reuse(
            archive_id=archive_id,
            archive_sha256=SHA_A,
            evidence="seen again on a later run",
        )
        revisions.observe(revision_id=again_id, file_size=4096)

    assert again_id == first_id
    assert created_again is False
    assert len(_established(revisions, archive_id)) == 1
    assert len(revisions.observations_for(first_id)) == 1
    # The original evidence is untouched -- re-seeing did not rewrite it.
    assert revisions.get(first_id).evidence == "first sight"


def test_hashing_a_provisional_archive_appends_and_promotes(
    connection,
) -> None:
    """The placeholder is kept, not replaced.

    Deleting it would erase the record that this identity existed with
    unknown bytes for a period, and would cascade away every observation
    made during that period. So the established revision is appended after
    it and the pointer moves, both inside the caller's transaction -- the
    promotion is not a second call anyone has to remember.
    """
    archive_id = _new_archive(connection)
    revisions = dal.RevisionRepository(connection)
    origin = _provisional_origin(revisions, archive_id)

    # An observation recorded while the bytes were still unknown.
    with dal.transaction(connection):
        revisions.observe(revision_id=origin.revision_id, file_size=4096)

    with dal.transaction(connection):
        established_id, created = revisions.record_or_reuse(
            archive_id=archive_id,
            archive_sha256=SHA_A,
            evidence="file recovered and hashed",
        )

    lineage = revisions.lineage_for(archive_id)

    assert created is True
    assert len(lineage) == 2, "the provisional origin must be retained"

    assert lineage[0].revision_id == origin.revision_id
    assert lineage[0].identity_state == "provisional"
    assert lineage[0].archive_sha256 is None
    assert lineage[0].evidence == origin.evidence

    assert lineage[1].revision_id == established_id
    assert lineage[1].revision_ordinal == 2
    assert lineage[1].identity_state == "established"
    assert lineage[1].archive_sha256 == SHA_A
    assert lineage[1].previous_revision_id == origin.revision_id

    # Promoted without a separate set_current() call.
    assert revisions.current_for(archive_id).revision_id == established_id

    # The observation made while the identity was unknown is still there.
    assert revisions.observations_for(origin.revision_id) != []


def test_establishing_identity_is_atomic(connection) -> None:
    """Append and promote land together or not at all.

    If the promotion were a separate step the caller had to remember, a
    failure between the two would leave an archive holding established bytes
    while still reporting a provisional identity -- permanently, since
    nothing would ever revisit it.
    """
    archive_id = _new_archive(connection)
    revisions = dal.RevisionRepository(connection)
    origin = _provisional_origin(revisions, archive_id)

    with pytest.raises(RuntimeError):
        with dal.transaction(connection):
            revisions.record_or_reuse(
                archive_id=archive_id,
                archive_sha256=SHA_A,
                evidence="hashed",
            )
            raise RuntimeError("failure after the append and the promotion")

    assert revisions.lineage_for(archive_id) == [origin]
    assert revisions.current_for(archive_id).revision_id == origin.revision_id


def test_a_later_generation_does_not_move_the_pointer_by_itself(
    connection,
) -> None:
    """Only an unestablished identity is promoted automatically.

    Once an archive's identity is established, which generation is current is
    an operator decision -- rolling back to an earlier one is legitimate --
    so appending must not silently override it.
    """
    archive_id = _new_archive(connection)
    revisions = dal.RevisionRepository(connection)

    with dal.transaction(connection):
        first_id, _ = revisions.record_or_reuse(
            archive_id=archive_id, archive_sha256=SHA_A, evidence="gen 1"
        )

    assert revisions.current_for(archive_id).revision_id == first_id

    with dal.transaction(connection):
        second_id, _ = revisions.record_or_reuse(
            archive_id=archive_id, archive_sha256=SHA_B, evidence="gen 2"
        )

    assert revisions.current_for(archive_id).revision_id == first_id
    assert revisions.get(second_id) is not None


def test_current_is_read_from_the_pointer_not_the_highest_ordinal(
    connection,
) -> None:
    """Rolling back to an earlier generation is a legitimate operator act.

    If `current_for` took the newest revision it would silently disagree with
    the pointer the moment anyone did that, and there would be two answers to
    a question the schema deliberately gives one answer to.
    """
    archive_id = _new_archive(connection)
    revisions = dal.RevisionRepository(connection)

    with dal.transaction(connection):
        gen1, _ = revisions.record_or_reuse(
            archive_id=archive_id, archive_sha256=SHA_A, evidence="gen 1"
        )
        gen2, _ = revisions.record_or_reuse(
            archive_id=archive_id, archive_sha256=SHA_B, evidence="gen 2"
        )
        revisions.set_current(archive_id, gen2)

    assert revisions.current_for(archive_id).revision_id == gen2

    with dal.transaction(connection):
        revisions.set_current(archive_id, gen1)

    current = revisions.current_for(archive_id)

    assert current.revision_id == gen1
    assert current.revision_ordinal == 2
    # Generation 2 still exists; rolling back is not deleting.
    assert len(_established(revisions, archive_id)) == 2


def test_every_revision_write_requires_the_dal_transaction(
    connection,
) -> None:
    """These repositories do not own commits either.

    Paired with the successes above rather than asserted alone, so a blanket
    breakage could not masquerade as correct strictness.
    """
    archive_id = _new_archive(connection)
    revisions = dal.RevisionRepository(connection)

    with dal.transaction(connection):
        revision_id, _ = revisions.record_or_reuse(
            archive_id=archive_id, archive_sha256=SHA_A, evidence="seed"
        )

    writes = (
        lambda: revisions.record_or_reuse(
            archive_id=archive_id, archive_sha256=SHA_B, evidence="x"
        ),
        lambda: revisions.set_current(archive_id, revision_id),
        lambda: revisions.observe(revision_id=revision_id),
    )

    for write in writes:
        with pytest.raises(dal.TransactionRequiredError):
            write()

    # Reads are deliberately not gated.
    assert revisions.get(revision_id) is not None
    assert revisions.current_for(archive_id) is not None
    assert len(_established(revisions, archive_id)) == 1


def test_a_failed_revision_transaction_leaves_no_generation_behind(
    connection,
) -> None:
    """Multi-step revision work is all-or-nothing."""
    archive_id = _new_archive(connection)
    revisions = dal.RevisionRepository(connection)

    with dal.transaction(connection):
        gen1, _ = revisions.record_or_reuse(
            archive_id=archive_id, archive_sha256=SHA_A, evidence="gen 1"
        )
        revisions.set_current(archive_id, gen1)

    with pytest.raises(RuntimeError):
        with dal.transaction(connection):
            gen2, _ = revisions.record_or_reuse(
                archive_id=archive_id, archive_sha256=SHA_B, evidence="gen 2"
            )
            revisions.set_current(archive_id, gen2)
            raise RuntimeError("something failed after the write")

    assert len(_established(revisions, archive_id)) == 1
    assert revisions.current_for(archive_id).revision_id == gen1
