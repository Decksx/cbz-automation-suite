from __future__ import annotations

import errno
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from comic_automation.archive.perceptual_reuse_analysis import (
    analyze_reuse_opportunity,
    connect_read_only,
)
from comic_automation.archive.perceptual_reuse_cli import main
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations


MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


def _insert_archive(
    connection,
    *,
    archive_id: int,
    page_digests: list[str],
    page_states: list[str],
) -> None:
    location = connection.execute(
        """
        INSERT INTO archive_files (id, file_size, page_count)
        VALUES (?, 1000, ?)
        """,
        (archive_id, len(page_digests)),
    )
    assert location.lastrowid is not None
    stored_location = connection.execute(
        """
        INSERT INTO file_locations (
            archive_id,
            path,
            file_size,
            modified_time_ns
        )
        VALUES (?, ?, 1000, 1)
        """,
        (archive_id, f"/library/{archive_id}.cbz"),
    )
    connection.execute(
        """
        INSERT INTO archive_content_signatures (
            archive_id,
            location_id,
            algorithm,
            algorithm_version,
            digest,
            page_count,
            image_bytes,
            source_file_size,
            source_modified_time_ns
        )
        VALUES (?, ?, 'sha256-page-sequence', '1', ?, ?, 900, 1000, 1)
        """,
        (
            archive_id,
            int(stored_location.lastrowid),
            f"signature-{archive_id}",
            len(page_digests),
        ),
    )

    for page_index, (digest, state) in enumerate(
        zip(page_digests, page_states)
    ):
        dimensions = (
            (1200, 1800, "PNG")
            if state in {"complete", "missing_phash"}
            else (None, None, None)
        )
        page = connection.execute(
            """
            INSERT INTO archive_pages (
                archive_id,
                location_id,
                page_index,
                entry_name,
                entry_size,
                compressed_size,
                crc32,
                width,
                height,
                image_format
            )
            VALUES (?, ?, ?, ?, 100, 80, ?, ?, ?, ?)
            """,
            (
                archive_id,
                int(stored_location.lastrowid),
                page_index,
                f"{page_index:03}.png",
                page_index,
                *dimensions,
            ),
        )
        page_id = int(page.lastrowid)
        connection.execute(
            """
            INSERT INTO page_hashes (
                page_id,
                algorithm,
                algorithm_version,
                digest,
                bytes_read
            )
            VALUES (?, 'sha256', '1', ?, 100)
            """,
            (page_id, digest),
        )

        if state in {"complete", "missing_phash"}:
            connection.execute(
                """
                INSERT INTO page_hashes (
                    page_id,
                    algorithm,
                    algorithm_version,
                    digest,
                    bytes_read
                )
                VALUES (?, 'dhash', '1', ?, 100)
                """,
                (page_id, f"dhash-{digest}"),
            )

        if state == "complete":
            connection.execute(
                """
                INSERT INTO page_hashes (
                    page_id,
                    algorithm,
                    algorithm_version,
                    digest,
                    bytes_read
                )
                VALUES (?, 'phash', '1', ?, 100)
                """,
                (page_id, f"phash-{digest}"),
            )


def _database_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def seed_reuse_scenario(database: Path) -> None:
    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATIONS)

        _insert_archive(
            connection,
            archive_id=1,
            page_digests=["source-a", "source-b", "source-c"],
            page_states=["complete", "complete", "complete"],
        )
        _insert_archive(
            connection,
            archive_id=2,
            page_digests=["source-a", "source-b"],
            page_states=["missing_all", "missing_all"],
        )
        _insert_archive(
            connection,
            archive_id=3,
            page_digests=["source-c", "unique-b"],
            page_states=["missing_phash", "missing_all"],
        )
        _insert_archive(
            connection,
            archive_id=4,
            page_digests=["unique-c"],
            page_states=["missing_all"],
        )


def test_analysis_distinguishes_full_and_partial_reuse(
    tmp_path: Path,
) -> None:
    database = tmp_path / "reuse.db"
    seed_reuse_scenario(database)
    before_digest = _database_digest(database)
    before_stat = database.stat()

    result = analyze_reuse_opportunity(database)

    after_stat = database.stat()
    assert result["read_only"] is True
    assert result["quick_check"] == "ok"
    assert result["metrics"] == {
        "eligible_archives": 3,
        "eligible_pages": 5,
        "incomplete_pages": 5,
        "reusable_pages": 3,
        "fully_satisfied_archives": 1,
        "partially_satisfied_archives": 1,
        "pages_still_requiring_decode": 2,
        "archives_still_requiring_processing": 2,
        "archives_without_reuse": 1,
        "pages_avoided_by_full_archive_reuse": 2,
        "pages_decoded_by_current_worker_after_reuse": 3,
        "pages_avoided_with_selective_worker": 3,
        "needed_sha256_digests": 5,
        "sha256_digests_with_complete_source": 3,
        "unambiguous_source_sha256_digests": 3,
        "ambiguous_source_sha256_digests": 0,
        "incomplete_pages_without_sha256": 0,
    }
    assert result["database_snapshot"]["metadata_unchanged"] is True
    assert before_stat.st_size == after_stat.st_size
    assert before_stat.st_mtime_ns == after_stat.st_mtime_ns
    assert _database_digest(database) == before_digest


def test_failed_jobs_are_excluded_from_eligibility(
    tmp_path: Path,
) -> None:
    database = tmp_path / "failed.db"
    seed_reuse_scenario(database)

    with database_connection(database) as connection:
        connection.execute(
            """
            INSERT INTO jobs (
                job_type,
                archive_id,
                status
            )
            VALUES ('hash_archive_pages_perceptual', 4, 'failed')
            """
        )

    result = analyze_reuse_opportunity(database)

    assert result["metrics"]["eligible_archives"] == 2
    assert result["metrics"]["archives_without_reuse"] == 0


def test_cli_writes_json_report_without_modifying_database(
    tmp_path: Path,
    capsys,
) -> None:
    database = tmp_path / "cli.db"
    output = tmp_path / "report.json"
    seed_reuse_scenario(database)
    before_digest = _database_digest(database)

    exit_code = main([
        "--database",
        str(database),
        "--json-output",
        str(output),
    ])
    captured = capsys.readouterr()
    report = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert json.loads(captured.out)["metrics"]["reusable_pages"] == 3
    assert report["metrics"]["fully_satisfied_archives"] == 1
    assert report["read_only"] is True
    assert report["query"]["parameters"]["sha256_algorithm"] == "sha256"
    assert "WITH eligible_archives AS" in report["query"]["sql"]
    assert report["json_output"] == str(output.resolve())
    assert _database_digest(database) == before_digest


# --- the connection comes from the DAL (Step 5) ---------------------------
#
# `connect_read_only` used to call `sqlite3.connect` itself and now opens
# through `dal.open_connection` under a caller-side policy. The tests above
# the "migration-only" marker pin the connection to the state the private
# copy produced -- measured on that copy at master `6410880` before it was
# replaced, and passing against it when it is swapped back in -- so the
# migration is shown to be behaviour-preserving rather than assumed to be.


def _plain_database(path: Path) -> None:
    """A one-row WAL database whose row records the path it was made at."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("CREATE TABLE marker (path TEXT)")
        connection.execute("INSERT INTO marker VALUES (?)", (str(path),))
        connection.commit()
    finally:
        connection.close()


def _spy_on_connect(monkeypatch: pytest.MonkeyPatch) -> list:
    """Record every `sqlite3.connect` call, wherever it is made from.

    Patched on the `sqlite3` module itself, so it sees the private copy's
    call and the DAL's alike: both look the function up on the module at
    call time.
    """
    calls: list = []
    real_connect = sqlite3.connect

    def spy(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        calls.append((args, kwargs, connection))
        return connection

    monkeypatch.setattr(sqlite3, "connect", spy)
    return calls


def test_reuse_connection_state_matches_the_pre_dal_factory(
    tmp_path: Path,
) -> None:
    """Every connection property the analysis can observe, pinned at once.

    Measured on the private factory: query_only 1, busy_timeout 60000,
    isolation_level None, sqlite3.Row rows, no open transaction, nothing
    changed. foreign_keys and synchronous are SQLite's defaults and
    journal_mode is the file's own: a read-only analysis must not adopt
    the writable factory's settings.
    """
    database = tmp_path / "state.db"
    seed_reuse_scenario(database)

    connection = connect_read_only(database)
    try:
        def pragma(name: str):
            return connection.execute(f"PRAGMA {name}").fetchone()[0]

        assert pragma("query_only") == 1
        assert pragma("busy_timeout") == 60000
        assert pragma("foreign_keys") == 0
        assert pragma("journal_mode") == "wal"
        assert pragma("synchronous") == 2  # FULL: SQLite's default
        assert connection.isolation_level is None
        assert connection.row_factory is sqlite3.Row
        assert connection.in_transaction is False
        assert connection.total_changes == 0

        row = connection.execute(
            "SELECT id FROM archive_files ORDER BY id"
        ).fetchone()
        assert row["id"] == 1
    finally:
        connection.close()


def test_reuse_connection_is_opened_with_the_pre_dal_connect_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One `mode=ro` URI connect, 60 s timeout, autocommit isolation.

    The timeout is only observable here: the later ``busy_timeout``
    pragma overwrites the handler ``timeout=`` installs, so a changed
    timeout leaves every PRAGMA reading the same.
    """
    database = tmp_path / "arguments.db"
    _plain_database(database)
    calls = _spy_on_connect(monkeypatch)

    connect_read_only(database).close()

    assert len(calls) == 1
    args, kwargs, _ = calls[0]
    assert kwargs == {"uri": True, "timeout": 60.0, "isolation_level": None}
    assert len(args) == 1
    assert args[0].startswith("file:")
    assert args[0].endswith("?mode=ro")


def test_reuse_connection_refuses_writes_with_query_only_turned_off(
    tmp_path: Path,
) -> None:
    """`mode=ro` is the layer that survives the pragma being cleared."""
    database = tmp_path / "readonly.db"
    _plain_database(database)
    before = database.read_bytes()

    connection = connect_read_only(database)
    try:
        connection.execute("PRAGMA query_only = OFF")

        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM marker")
    finally:
        connection.close()

    assert database.read_bytes() == before


@pytest.mark.parametrize(
    "relative",
    [
        "plain.db",
        "with space.db",
        "hash#1.db",
        "percent%41.db",
        "query&x=1.db",
        "semi;colon.db",
        "naïve_日本.db",
        "dir with space#%/inner.db",
    ],
)
def test_reuse_connection_opens_exactly_the_named_file(
    tmp_path: Path, relative: str
) -> None:
    """URI-significant characters reach SQLite escaped, not interpreted.

    The private copy built ``file:`` + `quote(...)`; the DAL builds
    `Path.as_uri()`. An unescaped ``#`` or ``?`` would end the path early
    and ``%41`` would be decoded to ``A``, so SQLite would open -- or, if
    not read-only, create -- a different file. Each case checks the file
    SQLite reports, the row only the intended file holds, and that nothing
    new appeared beside it except the database's own WAL sidecars.
    """
    database = tmp_path / relative
    _plain_database(database)
    before = sorted(p.name for p in database.parent.iterdir())

    connection = connect_read_only(database)
    try:
        listed = connection.execute("PRAGMA database_list").fetchone()
        marker = connection.execute("SELECT path FROM marker").fetchone()
    finally:
        connection.close()

    assert Path(listed["file"]) == database.resolve()
    assert marker["path"] == str(database)
    after = sorted(p.name for p in database.parent.iterdir())
    sidecars = {f"{database.name}-wal", f"{database.name}-shm"}
    assert {name for name in after if name not in before} <= sidecars


def test_a_missing_database_raises_file_not_found_and_creates_nothing(
    tmp_path: Path,
) -> None:
    """Unchanged by the migration: `Path.resolve(strict=True)` refuses it.

    The errno tells the two refusals apart. The OS error from `resolve`
    carries ENOENT; the DAL's own ``FileNotFoundError("Database does not
    exist: ...")`` carries none, so it reaching a missing path first would
    mean the strict resolve had been dropped.
    """
    missing = tmp_path / "absent.db"

    with pytest.raises(FileNotFoundError) as caught:
        connect_read_only(missing)

    assert caught.value.errno == errno.ENOENT

    with pytest.raises(FileNotFoundError):
        analyze_reuse_opportunity(missing)

    assert not missing.exists()
    assert list(tmp_path.iterdir()) == []


def test_analysis_closes_its_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The analysis owns the close of the connection it opened."""
    database = tmp_path / "close.db"
    seed_reuse_scenario(database)
    calls = _spy_on_connect(monkeypatch)

    analyze_reuse_opportunity(database)

    assert len(calls) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        calls[0][2].execute("SELECT 1")


# --- migration-only: these fail against the private copy, by design -------


def test_reuse_connection_opens_through_the_dal_with_its_own_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The factory is the DAL's, asked for read-only, with the 60 s policy.

    A spy rather than a source grep: it proves the call happens on the
    path the analysis takes, with the arguments that matter.
    """
    from comic_automation.archive import perceptual_reuse_analysis as module
    from comic_automation.database import dal

    calls: list = []
    real_open = dal.open_connection

    def spy(database_path, **kwargs):
        calls.append((database_path, kwargs))
        return real_open(database_path, **kwargs)

    monkeypatch.setattr(module, "open_connection", spy)

    database = tmp_path / "spy.db"
    seed_reuse_scenario(database)

    analyze_reuse_opportunity(database)

    assert calls == [
        (
            database.resolve(),
            {
                "readonly": True,
                "policy": module.REUSE_ANALYSIS_READ_ONLY_POLICY,
            },
        )
    ]


def test_the_reuse_policy_is_the_read_only_policy_with_a_60s_wait() -> None:
    """Pinned as data, so a change to it is a visible diff and a red test."""
    from comic_automation.archive import perceptual_reuse_analysis as module
    from comic_automation.database import dal

    policy = module.REUSE_ANALYSIS_READ_ONLY_POLICY

    assert policy.readonly is True
    assert policy.timeout_seconds == 60.0
    assert policy.pragmas == (
        ("query_only", "ON"),
        ("busy_timeout", "60000"),
    )
    assert [name for name, _ in policy.pragmas] == [
        name for name, _ in dal.READ_ONLY_POLICY.pragmas
    ]


def test_a_directory_is_refused_before_sqlite_is_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The one deliberate behaviour change of the migration.

    The private copy passed a directory to SQLite, which raised
    `sqlite3.OperationalError("unable to open database file")` -- not an
    `OSError`, so the CLI's handler missed it and it escaped as a
    traceback. The DAL refuses it first with `FileNotFoundError`, which
    the CLI reports as an ordinary failure.
    """
    directory = tmp_path / "not-a-database"
    directory.mkdir()
    calls = _spy_on_connect(monkeypatch)

    with pytest.raises(FileNotFoundError, match="Database does not exist"):
        connect_read_only(directory)

    assert main(["--database", str(directory)]) == 1
    assert "Database does not exist" in capsys.readouterr().err
    assert calls == []
    assert list(directory.iterdir()) == []


def test_a_policy_failure_closes_the_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-configured connection is closed, not left to the GC.

    The private copy applied its pragmas with no cleanup, so a failing
    ``busy_timeout`` left open a connection that already had
    ``query_only`` set and could still read. The connection class here
    fails only that pragma; everything else is a real connection.
    """

    class FailingBusyTimeout(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.startswith("PRAGMA busy_timeout"):
                raise sqlite3.OperationalError("injected busy_timeout failure")
            return super().execute(sql, *args)

    calls: list = []
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        connection = real_connect(*args, factory=FailingBusyTimeout, **kwargs)
        calls.append(connection)
        return connection

    database = tmp_path / "failing.db"
    _plain_database(database)
    monkeypatch.setattr(sqlite3, "connect", connect)

    with pytest.raises(sqlite3.OperationalError, match="injected"):
        connect_read_only(database)

    assert len(calls) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        calls[0].execute("SELECT 1")


def test_perceptual_reuse_analysis_is_no_longer_a_sanctioned_connect_site() -> None:
    """A raw connect returning to the module must trip the DAL guard."""
    from comic_automation.database import dal

    repo_root = Path(__file__).resolve().parent.parent
    name = "comic_automation/archive/perceptual_reuse_analysis.py"

    assert name not in dal.SANCTIONED_CONNECT_SITES
    assert name not in dal.find_connect_sites(repo_root)
