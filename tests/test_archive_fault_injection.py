"""Fault injection over the golden corpus.

Each test fails one thing, deterministically, at a chosen call, and asserts
what survives. The alternative -- racing two threads and hoping -- produces
a test that reproduces sometimes, which is worse than no test: it will be
marked flaky and deleted by someone who could not reproduce it.

Three families here:

*Transaction rollback* -- a failure inside a migration must leave no partial
schema and no recorded version.

*Dry-run non-mutation* -- a dry run must reach the point of deciding to
write and then write nothing. "The file is unchanged" alone is too weak: a
run that silently did nothing at all also leaves it unchanged.

*Interrupted archive rewrite* -- the read-rebuild paths write a temp file and
rename it into place. Failing at each rename leaves a different state, and
those states are what a crash actually looks like.

No production database, no live library. Every fixture is synthetic.
"""

from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path

import pytest

from comic_automation.database.connection import connect_database
from comic_automation.database.migrations import (
    applied_versions,
    apply_migrations,
    discover_migrations,
    migration_version,
)
from comic_automation.database.protected_migrations import is_protected
from scripts import cbz_library_maintenance
from scripts.cbz_library_maintenance import write_comicinfo
from tests import fault_injection as fi
from tests import golden_corpus as gc


MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)

NEW_XML = "<ComicInfo><Title>Rewritten</Title></ComicInfo>"


@pytest.fixture()
def database(tmp_path: Path):
    connection = connect_database(tmp_path / "faults.db")
    apply_migrations(connection, MIGRATIONS)

    try:
        yield connection
    finally:
        connection.close()


# --- the harness restores its own patches --------------------------------


def test_frozen_stat_restores_path_stat_on_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leaving the block must put the real `Path.stat` back.

    The first version of this harness patched before `yield` and never
    undid it, so the patch survived until pytest tore the fixture down at
    the end of the test. Anything after the `with` block ran against a
    frozen stat while looking like ordinary code -- the failure mode being
    that a later assertion in the same test silently observes stale
    metadata.
    """
    path = gc.build_case("ordinary", tmp_path)
    real_size = path.stat().st_size

    with fi.frozen_stat(monkeypatch, path):
        path.write_bytes(path.read_bytes() + b"XXXX")
        assert path.stat().st_size == real_size  # frozen inside

    assert path.stat().st_size == real_size + 4  # live again outside


def test_failing_path_rename_restores_path_rename_on_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = gc.build_case("ordinary", tmp_path)

    with fi.failing_path_rename(monkeypatch, after=0):
        with pytest.raises(fi.InjectedFailure):
            path.rename(tmp_path / "never.cbz")

    # The real rename works again immediately after the block.
    destination = tmp_path / "renamed.cbz"
    path.rename(destination)
    assert destination.exists()


def test_the_harness_patches_nothing_after_its_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Identity check, not a behavioural one.

    Comparing the bound attribute before and after catches a helper that
    restored *something* -- a differently-wrapped function, or a previous
    layer of patching -- rather than the original.
    """
    path = gc.build_case("ordinary", tmp_path)
    original_stat = Path.stat
    original_rename = Path.rename

    with fi.frozen_stat(monkeypatch, path):
        assert Path.stat is not original_stat

    assert Path.stat is original_stat

    with fi.failing_path_rename(monkeypatch):
        assert Path.rename is not original_rename

    assert Path.rename is original_rename


def test_nested_harness_blocks_unwind_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two faults active at once must each undo only their own patch."""
    path = gc.build_case("ordinary", tmp_path)
    original_stat = Path.stat
    original_rename = Path.rename

    with fi.frozen_stat(monkeypatch, path):
        with fi.failing_path_rename(monkeypatch):
            assert Path.stat is not original_stat
            assert Path.rename is not original_rename

        assert Path.rename is original_rename
        assert Path.stat is not original_stat

    assert Path.stat is original_stat


# --- transaction rollback ------------------------------------------------


def _write_migration(directory: Path, version: int, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{version:03d}_injected.sql"
    path.write_text(body, encoding="utf-8")
    return path


def test_a_failing_migration_leaves_no_partial_schema(
    tmp_path: Path,
) -> None:
    """Half a migration must not survive.

    The migration creates one table, then executes a statement that cannot
    succeed. Both are inside one BEGIN IMMEDIATE, so neither the table nor
    the version row may exist afterwards. Without the transaction the first
    statement would persist and the schema would be silently half-applied --
    the state that is hardest to detect later, because the version row is
    absent and a re-run would try to create the table again.
    """
    directory = tmp_path / "migrations"
    _write_migration(
        directory,
        1,
        "CREATE TABLE injected_first (id INTEGER PRIMARY KEY);\n"
        "INSERT INTO table_that_does_not_exist (id) VALUES (1);\n",
    )

    connection = connect_database(tmp_path / "rollback.db")

    try:
        with pytest.raises(sqlite3.Error):
            apply_migrations(connection, directory)

        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "injected_first" not in tables

        versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations"
            )
        ]
        assert versions == []
        assert connection.in_transaction is False
    finally:
        connection.close()


def test_a_failure_recording_the_version_rolls_back_the_schema(
    tmp_path: Path,
) -> None:
    """The version row and the schema change share one transaction.

    Injected at the `INSERT INTO schema_migrations` statement, which is the
    last thing before COMMIT. If the two were in separate transactions the
    table would exist with no version recorded, and the next run would fail
    trying to create it again.
    """
    directory = tmp_path / "migrations"
    _write_migration(
        directory,
        1,
        "CREATE TABLE injected_second (id INTEGER PRIMARY KEY);\n",
    )

    real = connect_database(tmp_path / "rollback2.db")

    try:
        guarded = fi.failing_execute(
            real, fail_on="insert into schema_migrations"
        )

        with pytest.raises(fi.InjectedFailure):
            apply_migrations(guarded, directory)

        assert guarded.matches == 1

        tables = {
            row[0]
            for row in real.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "injected_second" not in tables
    finally:
        real.close()


def test_a_failure_at_commit_discards_the_whole_migration(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "migrations"
    _write_migration(
        directory,
        1,
        "CREATE TABLE injected_third (id INTEGER PRIMARY KEY);\n",
    )

    real = connect_database(tmp_path / "rollback3.db")

    try:
        guarded = fi.failing_execute(real, fail_on="commit")

        with pytest.raises(fi.InjectedFailure):
            apply_migrations(guarded, directory)

        tables = {
            row[0]
            for row in real.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "injected_third" not in tables
    finally:
        real.close()


def test_a_successful_migration_is_the_control(tmp_path: Path) -> None:
    """Without this, the rollback tests could pass for the wrong reason.

    If the migration never applied at all -- a bad filename, an undiscovered
    directory -- every assertion above would still hold while proving
    nothing about transactions.
    """
    directory = tmp_path / "migrations"
    _write_migration(
        directory,
        1,
        "CREATE TABLE injected_control (id INTEGER PRIMARY KEY);\n",
    )

    connection = connect_database(tmp_path / "control.db")

    try:
        applied = apply_migrations(connection, directory)

        assert applied == [1]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "injected_control" in tables
    finally:
        connection.close()


# --- dry-run non-mutation ------------------------------------------------


def test_dry_run_rewrite_changes_nothing_on_disk(tmp_path: Path) -> None:
    """A dry run must not write, and must still report success."""
    path = gc.build_case("ordinary", tmp_path)
    before = fi.file_snapshot([path])

    result = write_comicinfo(path, gc.COMIC_INFO, NEW_XML, dry_run=True)

    assert result is True
    assert fi.file_snapshot([path]) == before
    assert not list(tmp_path.glob("*.tmp.cbz"))
    assert not list(tmp_path.glob("*.bak.cbz"))


def test_dry_run_does_not_even_open_the_archive_for_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stronger than "the bytes are unchanged".

    A dry run that opened the archive, rebuilt it and wrote identical bytes
    would pass a bytes comparison while doing all the dangerous work. This
    asserts no ZipFile is opened in a writing mode at all.
    """
    path = gc.build_case("ordinary", tmp_path)
    modes: list[str] = []
    real_zipfile = zipfile.ZipFile

    def recording_zipfile(file, mode="r", *args, **kwargs):
        modes.append(mode)
        return real_zipfile(file, mode, *args, **kwargs)

    monkeypatch.setattr(
        cbz_library_maintenance.zipfile, "ZipFile", recording_zipfile
    )

    write_comicinfo(path, gc.COMIC_INFO, NEW_XML, dry_run=True)

    assert [mode for mode in modes if mode != "r"] == []


def test_the_real_rewrite_is_the_control_for_the_dry_run(
    tmp_path: Path,
) -> None:
    """Proves the dry-run tests are not passing because nothing works.

    The same call with `dry_run=False` must actually change the file. If it
    did not, the dry-run assertions above would be vacuous.
    """
    path = gc.build_case("ordinary", tmp_path)
    before = gc.sha256_file(path)

    assert write_comicinfo(path, gc.COMIC_INFO, NEW_XML, dry_run=False)
    assert gc.sha256_file(path) != before

    payloads = gc.member_payloads(path)
    assert b"Rewritten" in payloads[gc.COMIC_INFO]


def test_a_rewrite_preserves_every_page_byte_for_byte(
    tmp_path: Path,
) -> None:
    """A metadata edit must not disturb page content.

    This is the write-side counterpart of the corpus's ComicInfo-only case:
    there the two archives were built independently, here one is produced
    from the other by the production code path.
    """
    path = gc.build_case("ordinary", tmp_path)
    pages_before = {
        name: payload
        for name, payload in gc.member_payloads(path).items()
        if name.endswith(".png")
    }

    assert write_comicinfo(path, gc.COMIC_INFO, NEW_XML, dry_run=False)

    pages_after = {
        name: payload
        for name, payload in gc.member_payloads(path).items()
        if name.endswith(".png")
    }
    assert pages_after == pages_before


def test_a_rewrite_preserves_unsafe_member_names_verbatim(
    tmp_path: Path,
) -> None:
    """Pins the accepted limitation on the write path.

    `docs/archive_io_resource_audit.md` records that the rewrite paths carry
    traversing member names through rather than rejecting them. Asserted
    here so the behaviour is visible and deliberate. If member validation is
    added later this test should fail and be replaced -- that is the signal,
    not a regression.
    """
    path = gc.build_case("unsafe_members", tmp_path)

    assert write_comicinfo(path, gc.COMIC_INFO, NEW_XML, dry_run=False)

    names = gc.member_names(path)
    assert "../escape.png" in names
    assert "/absolute.png" in names


# --- interrupted rewrite -------------------------------------------------


def test_a_failure_before_the_first_rename_leaves_the_original_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The safe failure: nothing has been moved yet.

    The original must be byte-identical and the temp file must be gone --
    a surviving `.tmp.cbz` would be picked up as a stale artefact by a later
    run.
    """
    path = gc.build_case("ordinary", tmp_path)
    before = fi.file_snapshot([path])

    with fi.failing_path_rename(monkeypatch, after=0) as state:
        result = write_comicinfo(path, gc.COMIC_INFO, NEW_XML, dry_run=False)

    assert result is False
    assert state["calls"] == 1
    assert fi.file_snapshot([path]) == before
    assert not list(tmp_path.glob("*.tmp.cbz"))


def test_a_failure_between_the_two_renames_leaves_the_data_in_the_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[RESOLVED 2026-08-24] The dangerous window, now with a restore attempt.

    Originally pinned as characterisation, not correctness: `write_comicinfo`
    renamed the original to `.bak.cbz`, then the temp file to the original
    name, and a failure between the two left **no file at the archive's own
    path** while the original bytes survived under `.bak.cbz`. The handler
    unlinked the temp file and did not move the backup back, so the archive
    stayed absent from its recorded location until somebody restored it by
    hand. That was recorded here as belonging in a separate PR.

    That PR landed. The second rename is now followed by a restore, so this
    injection no longer describes the common case -- with `after=1` *every*
    subsequent rename fails, including the restore, which is the one case
    the function still cannot repair in-process. What it must do there is
    keep both copies rather than delete either, and that is what is asserted
    now. The ordinary case, where only the swap fails and the restore
    succeeds, is covered by
    `tests/test_bak_cbz_recovery.py::test_maintenance_restores_the_original_when_the_swap_fails`.
    """
    path = gc.build_case("ordinary", tmp_path)
    original_bytes = path.read_bytes()

    with fi.failing_path_rename(monkeypatch, after=1) as state:
        result = write_comicinfo(path, gc.COMIC_INFO, NEW_XML, dry_run=False)

    assert result is False
    # Three now, not two: the swap, the failed rebuild rename, and the
    # restore attempt that the injection also fails.
    assert state["calls"] == 3

    backup = path.with_suffix(".bak.cbz")
    assert backup.exists()
    assert backup.read_bytes() == original_bytes
    assert not path.exists()

    # The change that matters: the rebuild is no longer deleted while the
    # archive is missing from its recorded path, so no byte of this archive
    # exists in only one place.
    assert path.with_suffix(".tmp.cbz").exists()


def test_an_interrupted_rewrite_never_leaves_a_truncated_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever survives must still be a readable archive.

    The failure modes above differ in *where* the bytes are, but in neither
    case may a partially written file be left behind under a name something
    else will later open.
    """
    for after in (0, 1):
        path = gc.build_case("ordinary", tmp_path / f"run{after}")

        with monkeypatch.context() as patch:
            with fi.failing_path_rename(patch, after=after):
                write_comicinfo(
                    path, gc.COMIC_INFO, NEW_XML, dry_run=False
                )

        survivors = [
            candidate
            for candidate in (tmp_path / f"run{after}").glob("*.cbz")
        ]
        assert survivors

        for candidate in survivors:
            assert zipfile.is_zipfile(candidate), candidate


# --- concurrent replacement during the rewrite window --------------------


def test_a_same_size_replacement_is_caught_by_the_content_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case size and mtime structurally cannot see.

    `frozen_stat` makes the file's size and mtime appear unchanged while its
    contents really are replaced mid-rewrite -- exactly what a coarse
    filesystem timestamp does in the wild. Only the central-directory CRC
    comparison can detect it, so this fails if that check is removed.
    """
    path = gc.build_case("ordinary", tmp_path)
    replacement = gc.same_size_replacement(path)

    real_zipfile = zipfile.ZipFile
    state = {"swapped": False}

    def replacing_zipfile(file, mode="r", *args, **kwargs):
        # Swapped the moment the rewrite opens its temp file for writing:
        # that instant is after the source has been read and its
        # fingerprint captured, and before the pre-rename re-check. It is
        # precisely the window a concurrent writer occupies.
        if mode == "w" and not state["swapped"]:
            state["swapped"] = True
            path.write_bytes(replacement)

        return real_zipfile(file, mode, *args, **kwargs)

    with fi.frozen_stat(monkeypatch, path):
        monkeypatch.setattr(
            cbz_library_maintenance.zipfile, "ZipFile", replacing_zipfile
        )
        result = write_comicinfo(
            path, gc.COMIC_INFO, NEW_XML, dry_run=False
        )

    assert result is False
    assert path.read_bytes() == replacement


def test_appended_bytes_are_caught_only_by_the_size_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case the content fingerprint is blind to.

    `zipfile` locates the end-of-central-directory record by searching
    backwards from the end of the file, so bytes appended after it leave the
    archive readable and its central directory byte-identical. The CRC
    comparison therefore sees nothing, and only the size/mtime check
    notices the file changed.

    This exists because removing the size/mtime check failed no test while
    the content check was present: every replacement that could be
    constructed also changed a CRC. The two guards are not redundant, and
    this is the case that shows it.
    """
    path = gc.build_case("ordinary", tmp_path)
    fingerprint_before = cbz_library_maintenance._read_central_directory_fingerprint(
        path
    )

    real_zipfile = zipfile.ZipFile
    state = {"appended": False}

    def appending_zipfile(file, mode="r", *args, **kwargs):
        if mode == "w" and not state["appended"]:
            state["appended"] = True

            with path.open("ab") as stream:
                stream.write(b"X" * 128)

        return real_zipfile(file, mode, *args, **kwargs)

    monkeypatch.setattr(
        cbz_library_maintenance.zipfile, "ZipFile", appending_zipfile
    )

    result = write_comicinfo(path, gc.COMIC_INFO, NEW_XML, dry_run=False)

    assert state["appended"] is True
    assert result is False

    # The blindness is the point: assert the content check genuinely could
    # not have fired, so this test cannot be satisfied by the CRC guard.
    assert zipfile.is_zipfile(path)
    assert (
        cbz_library_maintenance._read_central_directory_fingerprint(path)
        == fingerprint_before
    )


# --- database non-mutation under failure ---------------------------------


def test_a_commit_failure_inside_a_migration_restores_the_tables(
    tmp_path: Path
) -> None:
    """Production owns the rollback, and is the thing under test.

    The previous version issued its own ROLLBACK after the injected COMMIT
    failure, so it proved SQLite honours an explicit rollback -- not that
    any code recovers on its own. Nothing in the test exercised a
    production transaction boundary.

    Here the boundary belongs to `apply_migrations`, which opens
    BEGIN IMMEDIATE, and whose own `except` clause issues the ROLLBACK. The
    test injects a failure at COMMIT and then asserts, without touching the
    connection itself, that both the migration's table and its row are
    absent and that every pre-existing table is byte-identical to before.
    """
    tables = ("archive_files", "file_locations", "jobs")
    directory = tmp_path / "migrations"
    # One past the real migration set, derived rather than written down.
    # This migration is applied on top of MIGRATIONS, so a hard-coded
    # version silently becomes "already applied" the moment a real
    # migration claims that number -- which is exactly what happened when
    # 014 landed: the injected file was skipped, nothing raised, and the
    # test reported that no InjectedFailure occurred.
    #
    # Protected versions are then skipped over. The fixture below is a
    # rollback probe that has to travel through apply_migrations() to
    # reach the COMMIT this test injects at; landing on a protected
    # version would make the protected-migration guard refuse the call
    # before BEGIN IMMEDIATE, so the injected failure would never fire
    # and the test would fail for a reason unrelated to rollback. That
    # is not hypothetical -- deriving max+1 produced exactly 15, the
    # first declared protected version, the moment the guard landed.
    injected_version = (
        max(
            migration_version(path)
            for path in discover_migrations(MIGRATIONS)
        )
        + 1
    )

    while is_protected(injected_version):
        injected_version += 1
    _write_migration(
        directory,
        injected_version,
        "CREATE TABLE injected_rollback (id INTEGER PRIMARY KEY);\n"
        "INSERT INTO archive_files (file_size) VALUES (4096);\n",
    )

    real = connect_database(tmp_path / "injected.db")

    try:
        apply_migrations(real, MIGRATIONS)
        before = fi.table_snapshot(real, tables)
        versions_before = applied_versions(real)

        guarded = fi.failing_execute(real, fail_on="commit")

        with pytest.raises(fi.InjectedFailure):
            apply_migrations(guarded, directory)

        assert guarded.matches == 1

        # No manual recovery happens here: apply_migrations already did it.
        assert real.in_transaction is False

        names = {
            row[0]
            for row in real.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "injected_rollback" not in names

        # The migration's INSERT targeted a real table, so a partial commit
        # would show up as a changed digest rather than only a missing
        # table -- which is the case a schema-only assertion would miss.
        assert fi.table_snapshot(real, tables) == before
        assert applied_versions(real) == versions_before
    finally:
        real.close()


def test_sqlite_rolls_back_when_told_to_is_only_a_control(
    database,
) -> None:
    """Named for exactly what it proves, which is not much.

    Kept as the baseline the test above is measured against: if SQLite did
    not honour an explicit ROLLBACK, that test could pass for a reason
    having nothing to do with `apply_migrations`. It asserts a property of
    the database engine, and its name now says so rather than implying
    production recovery.
    """
    tables = ("archive_files",)
    before = fi.table_snapshot(database, tables)

    database.execute("BEGIN IMMEDIATE")
    database.execute(
        "INSERT INTO archive_files (file_size) VALUES (?)", (4096,)
    )
    database.execute("ROLLBACK")

    assert fi.table_snapshot(database, tables) == before


def test_the_snapshot_helper_notices_an_in_place_update(
    database, tmp_path: Path
) -> None:
    """The control, chosen to exercise the digest rather than the count.

    An INSERT would move the row count, so a snapshot that compared counts
    alone would pass this while being blind to a modified row. Updating a
    row in place leaves the count identical, so only the content digest can
    see it -- which is the part that needed proving.
    """
    tables = ("archive_files",)
    database.execute(
        "INSERT INTO archive_files (file_size) VALUES (?)", (4096,)
    )
    database.commit()

    before = fi.table_snapshot(database, tables)
    assert before[tables[0]][0] == 1

    database.execute("UPDATE archive_files SET file_size = 8192")
    database.commit()

    after = fi.table_snapshot(database, tables)
    assert after[tables[0]][0] == 1          # count unchanged...
    assert after != before                   # ...digest is not


def test_data_version_only_moves_for_another_connections_commit(
    tmp_path: Path
) -> None:
    """Why `table_snapshot` no longer reports `data_version`.

    `PRAGMA data_version` is defined to change when a *different* connection
    commits. A write committed by the connection doing the sampling leaves
    it untouched -- so reporting it beside same-connection row counts
    implied a mutation check it cannot provide.

    Both halves are asserted here so the removal is justified by measurement
    rather than by assertion in a docstring.
    """
    database = tmp_path / "observer.db"
    writer = connect_database(database)

    try:
        apply_migrations(writer, MIGRATIONS)

        # Same connection: its own commit does not move its own reading.
        own_before = writer.execute("PRAGMA data_version").fetchone()[0]
        writer.execute(
            "INSERT INTO archive_files (file_size) VALUES (?)", (4096,)
        )
        writer.commit()
        own_after = writer.execute("PRAGMA data_version").fetchone()[0]

        assert own_after == own_before

        # A separate observer, held open across the write, does see it.
        with fi.observer_data_version(database) as sample:
            observed_before = sample()
            writer.execute(
                "INSERT INTO archive_files (file_size) VALUES (?)", (8192,)
            )
            writer.commit()
            observed_after = sample()

        assert observed_after != observed_before
    finally:
        writer.close()
