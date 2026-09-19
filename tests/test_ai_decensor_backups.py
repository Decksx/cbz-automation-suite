"""Series-organized, verified migration of Camelia rollback originals."""

from __future__ import annotations

import shutil
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path

import pytest

from scripts import ai_decensor_backups as backups
from scripts import cbz_watcher as watcher


def _book(path: Path, series: str, page: bytes = b"original") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ComicInfo.xml", f"<ComicInfo><Series>{series}</Series></ComicInfo>")
        archive.writestr("001.jpg", page)
    return path


def _legacy(root: Path, filename: str, series: str, page: bytes = b"original") -> Path:
    return _book(root / str(uuid.uuid4()) / filename, series, page)


def test_archive_moves_verified_original_to_series_folder(tmp_path):
    original = _legacy(tmp_path / "local", "Book.cbz", "My Series")
    expected_bytes = original.read_bytes()

    target = backups.archive_backup(original, tmp_path / "archive", "My Series")

    assert target == tmp_path / "archive" / "My Series" / "Book.cbz"
    assert target.read_bytes() == expected_bytes
    assert not original.exists()
    assert not original.parent.exists()


def test_collisions_keep_distinct_backups_without_guid_folders(tmp_path):
    local = tmp_path / "local"
    first = _legacy(local, "Book.cbz", "Series", b"first")
    second = _legacy(local, "Book.cbz", "Series", b"second")
    same_as_first = _legacy(local, "Book.cbz", "Series", b"first")
    archive = tmp_path / "archive"

    first_target = backups.archive_backup(first, archive, "Series")
    second_target = backups.archive_backup(second, archive, "Series")
    deduplicated_target = backups.archive_backup(same_as_first, archive, "Series")

    assert first_target.name == "Book.cbz"
    assert second_target != first_target
    assert "backup" in second_target.name
    assert second_target.read_bytes() != first_target.read_bytes()
    assert deduplicated_target == first_target
    assert len(list((archive / "Series").glob("*.cbz"))) == 2


def test_failed_install_leaves_local_original_and_cleans_partial(tmp_path, monkeypatch):
    original = _legacy(tmp_path / "local", "Book.cbz", "Series")
    original_bytes = original.read_bytes()

    def fail_link(*_args):
        raise OSError("archive drive unavailable")

    monkeypatch.setattr(backups.os, "link", fail_link)
    with pytest.raises(OSError, match="archive drive unavailable"):
        backups.archive_backup(original, tmp_path / "archive", "Series")

    assert original.read_bytes() == original_bytes
    assert not list((tmp_path / "archive" / "Series").iterdir())


def test_legacy_migration_preview_then_apply(tmp_path):
    local = tmp_path / "local"
    first = _legacy(local, "One.cbz", "First Series")
    second = _legacy(local, "Two.cbz", "Second Series")
    archive = tmp_path / "archive"

    assert backups.migrate_existing(local, archive) == (2, 0)
    assert first.exists() and second.exists()
    assert not archive.exists()

    assert backups.migrate_existing(local, archive, apply=True) == (2, 0)
    assert not first.exists() and not second.exists()
    assert (archive / "First Series" / "One.cbz").is_file()
    assert (archive / "Second Series" / "Two.cbz").is_file()
    assert backups.migrate_existing(local, archive, apply=True) == (0, 0)


def test_legacy_backup_without_series_stays_on_local_drive(tmp_path):
    source = _legacy(tmp_path / "local", "Book.cbz", "Series")
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("ComicInfo.xml", "<ComicInfo/>")

    assert backups.migrate_existing(tmp_path / "local", tmp_path / "archive", apply=True) == (0, 1)
    assert source.is_file()


def test_single_backlog_backup_offload_and_scope_guard(tmp_path):
    local = tmp_path / "local"
    source = _legacy(local, "Book.cbz", "My Series")
    original = source.read_bytes()
    archive = tmp_path / "archive"

    target = backups.offload_one(source, local, archive)

    assert target == archive / "My Series" / "Book.cbz"
    assert target.read_bytes() == original
    assert not source.exists()
    outside = _book(tmp_path / "outside" / "Book.cbz", "My Series")
    with pytest.raises(ValueError, match="not directly inside"):
        backups.offload_one(outside, local, archive)
    assert outside.is_file()


def test_single_backup_cli_matches_camelia_callback(tmp_path):
    local = tmp_path / "local"
    source = _legacy(local, "Book.cbz", "My Series")
    archive = tmp_path / "archive"
    run = subprocess.run(
        [sys.executable, "-m", "scripts.ai_decensor_backups",
         "--source-root", str(local), "--destination-root", str(archive),
         "--source-file", str(source), "--apply"],
        cwd=backups.REPO_ROOT, capture_output=True, text=True, check=True,
    )
    assert "MOVED" in run.stdout
    assert not source.exists()
    assert (archive / "My Series" / "Book.cbz").is_file()


def test_watcher_archives_only_after_successful_route(tmp_path, monkeypatch):
    incoming_root = tmp_path / "incoming"
    comic_dir = incoming_root / "Series"
    original = _book(comic_dir / "Book.cbz", "Series")
    original_bytes = original.read_bytes()
    quarantine = tmp_path / "quarantine"
    archive_root = tmp_path / "archive"
    destination = tmp_path / "library" / "Comix"

    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(incoming_root))
    monkeypatch.setattr(watcher, "AI_DECENSOR_ENABLED", True)
    monkeypatch.setattr(watcher, "AI_DECENSOR_ARCHIVE_DIR", archive_root)
    monkeypatch.setattr(watcher, "_routing_rules", [])
    monkeypatch.setattr(watcher, "_routing_default", str(destination))
    monkeypatch.setattr(
        watcher, "process_cbz_file", lambda path, override_name=None: (
            path,
            watcher.ParsedComicName(
                original_path=path, filename=path.name, stem=path.stem,
                series="Series", chapter=None, volume=None,
            ),
        ),
    )

    def fake_decensor(path, _destination):
        backup = quarantine / str(uuid.uuid4()) / path.name
        backup.parent.mkdir(parents=True)
        shutil.copy2(path, backup)
        _book(path, "Series", b"processed")
        return backup

    monkeypatch.setattr(watcher, "run_ai_decensor", fake_decensor)

    watcher.process_and_move_directory(comic_dir)

    archived = archive_root / "Series" / "Book.cbz"
    assert archived.read_bytes() == original_bytes
    assert not list(quarantine.rglob("*.cbz"))
    assert (destination / "Series" / "Book.cbz").is_file()


def test_failed_offload_is_logged_and_keeps_c_backup(tmp_path, monkeypatch, caplog):
    source = _legacy(tmp_path / "quarantine", "Book.cbz", "Series")
    monkeypatch.setattr(watcher, "AI_DECENSOR_ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(
        watcher, "archive_backup", lambda *_args: (_ for _ in ()).throw(OSError("F: unavailable"))
    )

    watcher.archive_completed_ai_backups([(tmp_path / "processed.cbz", source)], tmp_path / "Series")

    assert source.is_file()
    assert "remains on C:" in caplog.text
