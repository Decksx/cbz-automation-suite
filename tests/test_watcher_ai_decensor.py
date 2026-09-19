import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from scripts import cbz_watcher as watcher


def test_run_ai_decensor_accepts_only_verified_in_place_result(
    tmp_path, monkeypatch, caplog
):
    """The watcher must validate both paths returned across the process boundary."""
    camelia_root = tmp_path / "camelia"
    cli = camelia_root / "scripts" / "process_cbz.py"
    cli.parent.mkdir(parents=True)
    cli.write_text("# fixture", encoding="utf-8")
    python = tmp_path / "python.exe"
    python.write_bytes(b"fixture")
    incoming = tmp_path / "incoming" / "Book.cbz"
    incoming.parent.mkdir()
    incoming.write_bytes(b"processed")
    backup = tmp_path / "quarantine" / "job" / "Book.cbz"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"original")

    monkeypatch.setattr(watcher, "CAMELIA_ROOT", camelia_root)
    monkeypatch.setattr(watcher, "CAMELIA_PYTHON", python)
    monkeypatch.setattr(watcher, "AI_DECENSOR_BACKUP_DIR", tmp_path / "quarantine")
    monkeypatch.setattr(
        watcher,
        "AI_DECENSOR_MODELS",
        ["transparent_black", "white_bars"],
    )
    caplog.set_level("INFO", logger=watcher.log.name)

    def fake_run(command, **kwargs):
        assert command[2] == str(incoming)
        assert command[command.index("--destination-dir") + 1] == str(
            tmp_path / "library" / "Comix"
        )
        model_values = [
            command[index + 1]
            for index, value in enumerate(command)
            if value == "--model-type"
        ]
        assert model_values == ["transparent_black", "white_bars"]
        payload = {"path": str(incoming), "backup_path": str(backup)}
        stdout = (
            "[PASS] Starting transparent_black (1/2)\n"
            "[PASS] Completed transparent_black: 1 output image(s)\n"
            "[PASS] Starting white_bars (2/2)\n"
            "[PASS] Completed white_bars: 1 output image(s)\n"
            f"CAMELIA_RESULT={json.dumps(payload)}\n"
        )
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(watcher.subprocess, "run", fake_run)

    assert watcher.run_ai_decensor(
        incoming,
        tmp_path / "library" / "Comix",
    ) == backup
    assert "AI decensor stages for Book.cbz: transparent_black -> white_bars" in caplog.text
    assert "Starting transparent_black (1/2)" in caplog.text
    assert "Completed white_bars: 1 output image(s)" in caplog.text


def test_watcher_parser_keeps_single_stage_default_and_accepts_ordered_stages():
    default_args = watcher.build_argument_parser().parse_args(["--ai-decensor"])
    ordered_args = watcher.build_argument_parser().parse_args([
        "--ai-decensor",
        "--ai-decensor-model", "white_bars",
        "--ai-decensor-model", "mosaic",
    ])

    assert default_args.ai_decensor_models is None
    assert ordered_args.ai_decensor_models == ["white_bars", "mosaic"]
    assert ordered_args.ai_decensor_archive_dir == watcher.AI_DECENSOR_ARCHIVE_DIR


def test_watcher_skip_result_needs_no_backup_and_override_reaches_camelia(tmp_path, monkeypatch):
    camelia_root = tmp_path / "camelia"
    cli = camelia_root / "scripts" / "process_cbz.py"
    cli.parent.mkdir(parents=True)
    cli.write_text("# fixture", encoding="utf-8")
    python = tmp_path / "python.exe"
    python.write_bytes(b"fixture")
    incoming = tmp_path / "Book.cbz"
    incoming.write_bytes(b"fixture")
    monkeypatch.setattr(watcher, "CAMELIA_ROOT", camelia_root)
    monkeypatch.setattr(watcher, "CAMELIA_PYTHON", python)
    monkeypatch.setattr(watcher, "AI_DECENSOR_REPROCESS", True)

    def fake_run(command, **_kwargs):
        assert "--reprocess" in command
        payload = {"status": "skipped", "path": str(incoming)}
        return subprocess.CompletedProcess(command, 0, f"CAMELIA_RESULT={json.dumps(payload)}\n", "")

    monkeypatch.setattr(watcher.subprocess, "run", fake_run)
    assert watcher.run_ai_decensor(incoming, tmp_path / "Comix") is None
    assert watcher.build_argument_parser().parse_args(["--ai-decensor-reprocess"]).ai_decensor_reprocess


def test_watcher_passes_mosaic_stage_to_camelia_cli(tmp_path, monkeypatch):
    camelia_root = tmp_path / "camelia"
    cli = camelia_root / "scripts" / "process_cbz.py"
    cli.parent.mkdir(parents=True)
    cli.write_text("# fixture", encoding="utf-8")
    python = tmp_path / "python.exe"
    python.write_bytes(b"fixture")
    incoming = tmp_path / "incoming" / "Book.cbz"
    incoming.parent.mkdir()
    incoming.write_bytes(b"processed")
    backup = tmp_path / "quarantine" / "job" / "Book.cbz"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"original")

    monkeypatch.setattr(watcher, "CAMELIA_ROOT", camelia_root)
    monkeypatch.setattr(watcher, "CAMELIA_PYTHON", python)
    monkeypatch.setattr(watcher, "AI_DECENSOR_BACKUP_DIR", tmp_path / "quarantine")
    monkeypatch.setattr(watcher, "AI_DECENSOR_MODELS", ["black_bars", "mosaic"])

    def fake_run(command, **_kwargs):
        model_values = [command[index + 1] for index, value in enumerate(command)
                        if value == "--model-type"]
        assert model_values == ["black_bars", "mosaic"]
        payload = {"path": str(incoming), "backup_path": str(backup)}
        return subprocess.CompletedProcess(command, 0, f"CAMELIA_RESULT={json.dumps(payload)}\n", "")

    monkeypatch.setattr(watcher.subprocess, "run", fake_run)

    assert watcher.run_ai_decensor(incoming, tmp_path / "Comix") == backup


def test_ai_failure_restores_prior_books_and_stops_directory_import(
    tmp_path, monkeypatch, caplog
):
    """A multi-book directory cannot be routed with only a partial AI pass."""
    incoming_root = tmp_path / "incoming"
    comic_dir = incoming_root / "Series"
    comic_dir.mkdir(parents=True)
    first = comic_dir / "01.cbz"
    second = comic_dir / "02.cbz"
    with zipfile.ZipFile(first, "w") as archive:
        archive.writestr("001.jpg", b"first original")
    with zipfile.ZipFile(second, "w") as archive:
        archive.writestr("001.jpg", b"second original")
    first_original = first.read_bytes()
    second_original = second.read_bytes()
    quarantine = tmp_path / "quarantine"
    destination = tmp_path / "Comix"

    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(incoming_root))
    monkeypatch.setattr(watcher, "AI_DECENSOR_ENABLED", True)
    monkeypatch.setattr(watcher, "_routing_rules", [])
    monkeypatch.setattr(watcher, "_routing_default", str(destination))
    monkeypatch.setattr(watcher, "process_cbz_file", lambda path, override_name=None: (path, object()))

    calls = []

    def fake_decensor(path, destination_dir):
        assert destination_dir == str(destination)
        calls.append(path.name)
        if path == second:
            raise watcher.AiDecensorError("injected failure")
        backup = quarantine / path.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        path.write_bytes(b"AI output")
        return backup

    monkeypatch.setattr(watcher, "run_ai_decensor", fake_decensor)

    watcher.process_and_move_directory(comic_dir)

    assert calls == ["01.cbz", "02.cbz"]
    assert first.read_bytes() == first_original
    assert second.read_bytes() == second_original
    assert comic_dir.is_dir()
    assert not destination.exists()
    assert "AI decensor failed for 02.cbz: injected failure" in caplog.text


def test_non_comix_destination_skips_ai_without_stopping_import(
    tmp_path, monkeypatch, caplog
):
    incoming_root = tmp_path / "incoming"
    comic_dir = incoming_root / "Series"
    comic_dir.mkdir(parents=True)
    source = comic_dir / "01.cbz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("001.jpg", b"page")
    destination = tmp_path / "Manga"

    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(incoming_root))
    monkeypatch.setattr(watcher, "AI_DECENSOR_ENABLED", True)
    monkeypatch.setattr(watcher, "_routing_rules", [])
    monkeypatch.setattr(watcher, "_routing_default", str(destination))
    monkeypatch.setattr(
        watcher,
        "process_cbz_file",
        lambda path, override_name=None: (
            path,
            watcher.ParsedComicName(
                original_path=path,
                filename=path.name,
                stem=path.stem,
                series="Series",
                chapter="1",
                volume=None,
            ),
        ),
    )
    monkeypatch.setattr(
        watcher,
        "run_ai_decensor",
        lambda *args: pytest.fail("Manga destinations must not invoke Camelia"),
    )
    caplog.set_level("INFO", logger=watcher.log.name)

    watcher.process_and_move_directory(comic_dir)

    assert "AI decensor skipped: destination is not under a Comix directory" in caplog.text
    assert not comic_dir.exists()


def test_run_ai_decensor_rejects_backup_outside_quarantine(tmp_path, monkeypatch):
    """A child-process result cannot redirect the trusted rollback source."""
    camelia_root = tmp_path / "camelia"
    cli = camelia_root / "scripts" / "process_cbz.py"
    cli.parent.mkdir(parents=True)
    cli.write_text("# fixture", encoding="utf-8")
    python = tmp_path / "python.exe"
    python.write_bytes(b"fixture")
    incoming = tmp_path / "incoming.cbz"
    incoming.write_bytes(b"processed")
    outside = tmp_path / "outside.cbz"
    outside.write_bytes(b"not trusted")
    quarantine = tmp_path / "quarantine"

    monkeypatch.setattr(watcher, "CAMELIA_ROOT", camelia_root)
    monkeypatch.setattr(watcher, "CAMELIA_PYTHON", python)
    monkeypatch.setattr(watcher, "AI_DECENSOR_BACKUP_DIR", quarantine)
    payload = {"path": str(incoming), "backup_path": str(outside)}
    monkeypatch.setattr(
        watcher.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, f"CAMELIA_RESULT={json.dumps(payload)}\n", ""
        ),
    )

    with pytest.raises(watcher.AiDecensorError, match="outside quarantine"):
        watcher.run_ai_decensor(incoming, tmp_path / "Comix")
