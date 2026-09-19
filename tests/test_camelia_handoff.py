from __future__ import annotations

import os
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.library.camelia_handoff import HandoffRefused, prepare_source, reconcile_completed
from comic_automation.library.repository import scan_library


MIGRATIONS = Path(__file__).resolve().parents[1] / "comic_automation" / "database" / "migrations"


def setup_databases(tmp_path: Path) -> tuple[Path, Path, Path, Path, int]:
    root = tmp_path / "comix"
    root.mkdir()
    archive = root / "Book.cbz"
    archive.write_bytes(b"old archive content")
    comic_db = tmp_path / "comic.db"
    with database_connection(comic_db) as comic:
        apply_migrations(comic, MIGRATIONS)
        scan_library(comic, root)
        row = comic.execute("SELECT archive_id FROM file_locations WHERE path=?", (str(archive),)).fetchone()
        archive_id = int(row["archive_id"])
    camelia_db = tmp_path / "camelia.db"
    with sqlite3.connect(camelia_db) as camelia:
        camelia.executescript("""
            CREATE TABLE books(id INTEGER PRIMARY KEY, source_path TEXT, root_path TEXT,
                               file_size INTEGER, mtime_ns INTEGER, state TEXT);
            CREATE TABLE events(id INTEGER PRIMARY KEY, book_id INTEGER, event_type TEXT);
            CREATE TABLE controls(name TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO controls VALUES('instance_uuid','test-camelia-instance');
        """)
    return root, archive, comic_db, camelia_db, archive_id


def complete_camelia_job(camelia_db: Path, root: Path, archive: Path, book_id=1, event_id=1) -> None:
    stat = archive.stat()
    with sqlite3.connect(camelia_db) as camelia:
        camelia.execute(
            "INSERT OR REPLACE INTO books VALUES(?,?,?,?,?,?)",
            (book_id, str(archive), str(root), stat.st_size, stat.st_mtime_ns, "completed"),
        )
        camelia.execute("INSERT INTO events VALUES(?,?,'job_complete')", (event_id, book_id))


def replace_archive(archive: Path) -> None:
    before = archive.stat()
    archive.write_bytes(b"new decensored archive bytes")
    os.utime(archive, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))


def test_handoff_preserves_archive_id_and_is_idempotent(tmp_path: Path) -> None:
    root, archive, comic_db, camelia_db, archive_id = setup_databases(tmp_path)
    replace_archive(archive)
    complete_camelia_job(camelia_db, root, archive)

    plan = reconcile_completed(
        camelia_database=camelia_db, comic_database=comic_db, root=root, apply=False,
    )
    assert len(plan) == 1
    assert plan[0].action == "would_change"
    with database_connection(comic_db) as comic:
        assert comic.execute("SELECT COUNT(*) FROM file_events WHERE event_type='changed'").fetchone()[0] == 0

    applied = reconcile_completed(
        camelia_database=camelia_db, comic_database=comic_db, root=root, apply=True,
    )
    assert len(applied) == 1
    assert applied[0].archive_id == archive_id
    assert applied[0].action == "changed"
    assert applied[0].page_hash_queued is True
    assert reconcile_completed(
        camelia_database=camelia_db, comic_database=comic_db, root=root, apply=True,
    ) == []

    with database_connection(comic_db) as comic:
        location = comic.execute(
            "SELECT archive_id,file_size,modified_time_ns FROM file_locations WHERE path=?",
            (str(archive),),
        ).fetchone()
        assert location["archive_id"] == archive_id
        assert location["file_size"] == archive.stat().st_size
        assert location["modified_time_ns"] == archive.stat().st_mtime_ns
        assert comic.execute("SELECT COUNT(*) FROM archive_files").fetchone()[0] == 1
        assert comic.execute("SELECT COUNT(*) FROM file_events WHERE event_type='changed'").fetchone()[0] == 1
        assert comic.execute(
            "SELECT COUNT(*) FROM jobs WHERE archive_id=? AND job_type='hash_archive_pages' AND status='pending'",
            (archive_id,),
        ).fetchone()[0] == 1
        assert comic.execute(
            "SELECT digest FROM archive_hashes WHERE archive_id=?", (archive_id,)
        ).fetchone()[0] == applied[0].sha256
        assert comic.execute(
            """SELECT ar.archive_sha256 FROM archive_files AS af
               JOIN archive_revisions AS ar ON ar.id=af.current_revision_id
               WHERE af.id=?""", (archive_id,)
        ).fetchone()[0] == applied[0].sha256


def test_handoff_refuses_untracked_book(tmp_path: Path) -> None:
    root, _archive, comic_db, camelia_db, _archive_id = setup_databases(tmp_path)
    untracked = root / "Untracked.cbz"
    untracked.write_bytes(b"processed")
    complete_camelia_job(camelia_db, root, untracked)
    with pytest.raises(HandoffRefused, match="untracked"):
        reconcile_completed(camelia_database=camelia_db, comic_database=comic_db, root=root, apply=True)
    with database_connection(comic_db) as comic:
        assert comic.execute("SELECT COUNT(*) FROM archive_files").fetchone()[0] == 1


def test_preparation_registers_untracked_source_before_replacement(tmp_path: Path) -> None:
    root, _archive, comic_db, camelia_db, _archive_id = setup_databases(tmp_path)
    untracked = root / "New Series" / "Untracked.cbz"
    untracked.parent.mkdir()
    untracked.write_bytes(b"original unprocessed archive")
    preview = prepare_source(comic_database=comic_db, root=root, source=untracked)
    assert preview.action == "would_register"
    assert preview.archive_id is None
    with database_connection(comic_db) as comic:
        assert comic.execute("SELECT COUNT(*) FROM archive_files").fetchone()[0] == 1

    prepared = prepare_source(comic_database=comic_db, root=root, source=untracked, apply=True)
    assert prepared.action == "registered"
    assert prepared.archive_id is not None
    assert prepare_source(comic_database=comic_db, root=root, source=untracked, apply=True).archive_id == prepared.archive_id
    with database_connection(comic_db) as comic:
        assert comic.execute("SELECT COUNT(*) FROM archive_files").fetchone()[0] == 2
        assert comic.execute(
            "SELECT digest FROM archive_hashes WHERE archive_id=?", (prepared.archive_id,)
        ).fetchone()[0] == prepared.sha256
        assert comic.execute(
            """SELECT r.archive_sha256 FROM archive_files AS a
               JOIN archive_revisions AS r ON r.id=a.current_revision_id WHERE a.id=?""",
            (prepared.archive_id,),
        ).fetchone()[0] == prepared.sha256

    replace_archive(untracked)
    complete_camelia_job(camelia_db, root, untracked)
    reconciled = reconcile_completed(
        camelia_database=camelia_db, comic_database=comic_db, root=root, apply=True,
    )
    assert reconciled[0].archive_id == prepared.archive_id
    assert reconciled[0].sha256 != prepared.sha256


def test_preparation_refuses_version_history_and_ambiguous_bytes(tmp_path: Path) -> None:
    root, archive, comic_db, _camelia_db, _archive_id = setup_databases(tmp_path)
    history = root / ".stversions" / "History.cbz"
    history.parent.mkdir()
    history.write_bytes(b"historical copy")
    with pytest.raises(HandoffRefused, match="not an active CBZ"):
        prepare_source(comic_database=comic_db, root=root, source=history, apply=True)
    with database_connection(comic_db) as comic:
        assert comic.execute("SELECT COUNT(*) FROM archive_files").fetchone()[0] == 1

    prepare_source(comic_database=comic_db, root=root, source=archive, apply=True)
    before = archive.stat()
    archive.write_bytes(b"new archive content")  # same length, original hash exists
    os.utime(archive, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(HandoffRefused, match="disagree"):
        prepare_source(comic_database=comic_db, root=root, source=archive, apply=True)


def test_handoff_refuses_file_changed_after_camelia_completion(tmp_path: Path) -> None:
    root, archive, comic_db, camelia_db, _archive_id = setup_databases(tmp_path)
    replace_archive(archive)
    complete_camelia_job(camelia_db, root, archive)
    archive.write_bytes(b"changed again")
    with pytest.raises(HandoffRefused, match="fingerprint"):
        reconcile_completed(camelia_database=camelia_db, comic_database=comic_db, root=root, apply=True)


def test_handoff_refuses_ambiguous_same_stat_change(tmp_path: Path) -> None:
    root, archive, comic_db, camelia_db, _archive_id = setup_databases(tmp_path)
    before = archive.stat()
    archive.write_bytes(b"new archive content")  # same byte length as the original
    assert archive.stat().st_size == before.st_size
    os.utime(archive, ns=(before.st_atime_ns, before.st_mtime_ns))
    complete_camelia_job(camelia_db, root, archive)
    with pytest.raises(HandoffRefused, match="ambiguous"):
        reconcile_completed(camelia_database=camelia_db, comic_database=comic_db, root=root, apply=True)


def test_handoff_does_not_migrate_an_old_live_database(tmp_path: Path) -> None:
    root, archive, _comic_db, camelia_db, _archive_id = setup_databases(tmp_path)
    replace_archive(archive)
    complete_camelia_job(camelia_db, root, archive)
    legacy_db = tmp_path / "legacy.db"
    with sqlite3.connect(legacy_db) as connection:
        for version in ("001_operational_foundation.sql", "002_discovery_checkpoints.sql"):
            connection.executescript((MIGRATIONS / version).read_text(encoding="utf-8"))
    with pytest.raises(HandoffRefused, match="not ready"):
        reconcile_completed(camelia_database=camelia_db, comic_database=legacy_db, root=root, apply=True)
    with sqlite3.connect(legacy_db) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='archive_revisions'"
        ).fetchone()[0] == 0


def test_cli_preview_then_apply_uses_the_same_safe_handoff(tmp_path: Path) -> None:
    root, archive, comic_db, camelia_db, archive_id = setup_databases(tmp_path)
    replace_archive(archive)
    complete_camelia_job(camelia_db, root, archive)
    command = [
        sys.executable, "-m", "comic_automation.library.camelia_handoff",
        "--camelia-database", str(camelia_db), "--database", str(comic_db),
        "--root", str(root),
    ]
    preview = subprocess.run(command, cwd=MIGRATIONS.parents[2], capture_output=True, text=True, check=True)
    assert json.loads(preview.stdout)["results"][0]["action"] == "would_change"
    applied = subprocess.run([*command, "--apply"], cwd=MIGRATIONS.parents[2],
                             capture_output=True, text=True, check=True)
    assert json.loads(applied.stdout)["results"][0]["archive_id"] == archive_id
