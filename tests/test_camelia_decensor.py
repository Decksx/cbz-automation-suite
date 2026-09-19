"""Safety and batch behavior for the standalone Camelia GUI command."""

from __future__ import annotations

import argparse
import io
import json
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from scripts import camelia_decensor


def _book(path: Path, *, tagged: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.png", b"fixture")
        tag = "<Tags>uncensored</Tags>" if tagged else ""
        archive.writestr("ComicInfo.xml", f"<ComicInfo><Title>Book</Title>{tag}</ComicInfo>")
    return path


def _valid_book(
    path: Path, *, tagged: bool = False, title: str = "Book",
    methods: tuple[str, ...] = ("black_bars",),
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    page = io.BytesIO()
    Image.new("RGB", (8, 8), "green" if tagged else "red").save(page, format="PNG")
    method_tags = ", ".join(f"camelia:{method}" for method in methods)
    tags = f"<Tags>uncensored, {method_tags}</Tags>" if tagged else ""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.png", page.getvalue())
        archive.writestr("ComicInfo.xml", f"<ComicInfo><Title>{title}</Title>{tags}</ComicInfo>")
    return path


def _metadata_inspector(path: str) -> dict:
    with zipfile.ZipFile(path) as archive:
        metadata = archive.read("ComicInfo.xml").decode("utf-8")
    marked = "uncensored" in metadata
    return {
        "eligible": not marked,
        "already_processed": marked,
        "applied_methods": [
            method for method in camelia_decensor.MODEL_TYPES
            if f"camelia:{method}" in metadata
        ],
        "reason": "method tagged" if marked else "Unmarked",
        "parse_error": None,
    }


def _inspector(path: str) -> dict:
    marked = "uncensored" in Path(path).stem.casefold()
    return {
        "eligible": not marked,
        "already_processed": marked,
        "reason": "Already marked" if marked else "Unmarked",
        "parse_error": None,
    }


def _args(source: Path, output: Path, tmp_path: Path, *, dry_run: bool) -> argparse.Namespace:
    return argparse.Namespace(
        source=source,
        output_dir=output,
        model_type=None,
        dry_run=dry_run,
        camelia_root=tmp_path / "camelia",
        camelia_python=tmp_path / "python.exe",
    )


def test_dry_run_recurses_and_skips_already_marked_without_creating_output(tmp_path, capsys):
    source = tmp_path / "input"
    eligible = _book(source / "series" / "Book.cbz")
    _book(source / "Already Uncensored.cbz")
    output = tmp_path / "Comix"

    assert camelia_decensor.run_batch(_args(source, output, tmp_path, dry_run=True), _inspector) == 0

    printed = capsys.readouterr().out
    assert "Books to process: 1; already marked: 1" in printed
    assert f"DRY RUN {eligible} -> {output / 'series' / 'Book.cbz'}" in printed
    assert not output.exists()


def test_verified_existing_output_is_skipped_and_scan_continues(tmp_path, capsys):
    source_root = tmp_path / "input"
    source = _valid_book(source_root / "A.cbz", title="A")
    later = _valid_book(source_root / "B.cbz", title="B")
    output = tmp_path / "Comix"
    existing = _valid_book(output / "A.cbz", tagged=True, title="A")
    existing_bytes = existing.read_bytes()
    args = _args(source_root, output, tmp_path, dry_run=True)
    args.model_type = ["black_bars"]

    assert camelia_decensor.run_batch(args, _metadata_inspector) == 0

    printed = capsys.readouterr().out
    assert f"SKIP verified existing output: {existing}" in printed
    assert f"DRY RUN {later} -> {output / 'B.cbz'}" in printed
    assert existing.read_bytes() == existing_bytes
    assert source.is_file()


def test_conflicting_existing_output_still_fails_closed_by_default(tmp_path):
    source = _valid_book(tmp_path / "input" / "Book.cbz", title="Book")
    output = tmp_path / "Comix"
    _valid_book(output / "Book.cbz", tagged=True, title="Different")
    args = _args(source, output, tmp_path, dry_run=True)
    args.model_type = ["black_bars"]

    with pytest.raises(FileExistsError, match="Refusing to resume existing output"):
        camelia_decensor.run_batch(args, _metadata_inspector)


def test_existing_additional_method_output_is_skipped_and_scan_continues(tmp_path, capsys):
    source_root = tmp_path / "input"
    processed = _valid_book(source_root / "A.cbz", tagged=True, title="A")
    later = _valid_book(source_root / "B.cbz", title="B")
    output = tmp_path / "Comix"
    _valid_book(output / "A.cbz", tagged=True, title="A")
    existing = _valid_book(
        output / "_additional_methods" / "white_bars" / "A.cbz",
        tagged=True,
        title="A",
        methods=("black_bars", "white_bars"),
    )
    args = _args(source_root, output, tmp_path, dry_run=True)
    args.model_type = ["black_bars", "white_bars"]

    assert camelia_decensor.run_batch(args, _metadata_inspector) == 0

    printed = capsys.readouterr().out
    assert f"SKIP verified existing output: {existing}" in printed
    assert f"DRY RUN {later} -> {output / 'B.cbz'}" in printed
    assert processed.is_file()


def test_processed_book_runs_only_missing_method_into_separate_output(tmp_path, capsys):
    source = _book(tmp_path / "input" / "Book.cbz")
    output = tmp_path / "Comix"
    _book(output / "Book.cbz")

    def inspect(_path):
        return {"eligible": False, "already_processed": True,
                "applied_methods": ["black_bars"], "reason": "method tagged", "parse_error": None}

    args = _args(source, output, tmp_path, dry_run=True)
    args.model_type = ["black_bars", "mosaic"]
    assert camelia_decensor.run_batch(args, inspect) == 0
    assert str(output / "_additional_methods" / "mosaic" / "Book.cbz") in capsys.readouterr().out
    assert camelia_decensor.methods_to_run(inspect(str(source)), args.model_type) == ["mosaic"]


def test_reprocess_override_selects_applied_method(tmp_path):
    source = _book(tmp_path / "input" / "Book.cbz")
    output = tmp_path / "Comix"

    def inspect(_path):
        return {"eligible": False, "already_processed": True,
                "applied_methods": ["black_bars"], "reason": "method tagged", "parse_error": None}

    assert camelia_decensor.plan_batch(source, output, inspect, ["black_bars"])[1] == 1
    planned, skipped, resumed = camelia_decensor.plan_batch(source, output, inspect, ["black_bars"], True)
    assert planned == [(source, output / "Book.cbz")]
    assert skipped == 0
    assert resumed == 0
    command = camelia_decensor.build_camelia_command(
        source, output / "Book.cbz", ["black_bars"], tmp_path, tmp_path / "python.exe", True
    )
    assert command[-1] == "--reprocess"


@pytest.mark.parametrize("output_name", ["ordinary", "input/Comix"])
def test_unsafe_output_root_is_refused(tmp_path, output_name):
    source = tmp_path / "input"
    _book(source / "Book.cbz")

    with pytest.raises(ValueError, match="Output must be|cannot be inside"):
        camelia_decensor.run_batch(
            _args(source, tmp_path / output_name, tmp_path, dry_run=True), _inspector
        )


def test_copy_command_preserves_selected_stage_order(tmp_path):
    command = camelia_decensor.build_camelia_command(
        tmp_path / "input" / "Book.cbz", tmp_path / "Comix" / "Book.cbz",
        ["transparent_black", "mosaic"], tmp_path / "camelia", tmp_path / "python.exe",
    )

    assert command[3:] == [
        "--copy", "--output-dir", str(tmp_path / "Comix"),
        "--model-type", "transparent_black", "--model-type", "mosaic",
    ]
    assert "--replace" not in command


def test_resume_skips_only_verified_matching_output(tmp_path, capsys):
    source = _valid_book(tmp_path / "input" / "Book.cbz")
    output = _valid_book(tmp_path / "Comix" / "Book.cbz", tagged=True)
    output_bytes = output.read_bytes()
    args = _args(source, output.parent, tmp_path, dry_run=True)
    args.model_type = ["black_bars"]
    args.resume = True

    assert camelia_decensor.run_batch(args, _metadata_inspector) == 0
    assert "SKIP verified existing output" in capsys.readouterr().out
    assert output.read_bytes() == output_bytes


def test_resume_accepts_generated_comicinfo_when_source_had_none(tmp_path):
    source = tmp_path / "input" / "Book.cbz"
    source.parent.mkdir(parents=True)
    page = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(page, format="PNG")
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("001.png", page.getvalue())
    output = _valid_book(tmp_path / "Comix" / "Book.cbz", tagged=True)

    camelia_decensor.verify_completed_output(source, output, ["black_bars"], _metadata_inspector)


@pytest.mark.parametrize("conflict", ["wrong_title", "wrong_member", "missing_tag"])
def test_resume_refuses_conflicting_output(tmp_path, conflict):
    source = _valid_book(tmp_path / "input" / "Book.cbz")
    output = tmp_path / "Comix" / "Book.cbz"
    if conflict == "wrong_title":
        _valid_book(output, tagged=True, title="Other book")
    elif conflict == "wrong_member":
        _valid_book(output, tagged=True)
        with zipfile.ZipFile(output, "a") as archive:
            archive.writestr("unexpected.txt", "wrong output")
    else:
        _valid_book(output)
    args = _args(source, output.parent, tmp_path, dry_run=True)
    args.model_type = ["black_bars"]
    args.resume = True

    with pytest.raises(FileExistsError, match="Refusing to resume existing output"):
        camelia_decensor.run_batch(args, _metadata_inspector)


def test_resume_and_reprocess_are_incompatible(tmp_path):
    source = _valid_book(tmp_path / "input" / "Book.cbz")
    args = _args(source, tmp_path / "Comix", tmp_path, dry_run=True)
    args.resume = True
    args.reprocess = True

    with pytest.raises(ValueError, match="cannot be used together"):
        camelia_decensor.run_batch(args, _metadata_inspector)


def test_normal_batch_invokes_each_book_sequentially_and_rechecks_target(tmp_path, monkeypatch, capsys):
    source = tmp_path / "input"
    first = _book(source / "A.cbz")
    second = _book(source / "B.cbz")
    output = tmp_path / "Comix"
    camelia_root = tmp_path / "camelia"
    (camelia_root / "scripts").mkdir(parents=True)
    (camelia_root / "scripts" / "process_cbz.py").touch()
    python = tmp_path / "python.exe"
    python.touch()
    args = _args(source, output, tmp_path, dry_run=False)
    calls = []

    def fake_process(archive, destination, sequence, *_rest):
        calls.append((archive, destination, sequence))
        _book(destination)

    monkeypatch.setattr(camelia_decensor, "run_camelia_book", fake_process)
    assert camelia_decensor.run_batch(args, _inspector) == 0

    assert [entry[0] for entry in calls] == [first, second]
    assert all(entry[2] == list(camelia_decensor.MODEL_TYPES) for entry in calls)
    assert first.is_file() and second.is_file()
    assert "CBZ_PROGRESS 2/2 100% books" in capsys.readouterr().out


def test_output_created_during_batch_is_verified_skipped_and_later_books_continue(
    tmp_path, monkeypatch, capsys
):
    source = tmp_path / "input"
    first = _valid_book(source / "A.cbz", title="A")
    second = _valid_book(source / "B.cbz", title="B")
    third = _valid_book(source / "C.cbz", title="C")
    output = tmp_path / "Comix"
    camelia_root = tmp_path / "camelia"
    (camelia_root / "scripts").mkdir(parents=True)
    (camelia_root / "scripts" / "process_cbz.py").touch()
    python = tmp_path / "python.exe"
    python.touch()
    args = _args(source, output, tmp_path, dry_run=False)
    args.model_type = ["black_bars"]
    calls = []

    def fake_process(archive, destination, sequence, *_rest):
        calls.append(archive)
        _valid_book(destination, tagged=True, title=archive.stem)
        if archive == first:
            _valid_book(output / "B.cbz", tagged=True, title="B")

    monkeypatch.setattr(camelia_decensor, "run_camelia_book", fake_process)

    assert camelia_decensor.run_batch(args, _metadata_inspector) == 0

    assert calls == [first, third]
    printed = capsys.readouterr().out
    assert f"SKIP verified output created during batch: {output / 'B.cbz'}" in printed
    assert "CBZ_PROGRESS 3/3 100% books" in printed


def test_source_symlink_is_refused(tmp_path):
    source = _book(tmp_path / "real.cbz")
    alias = tmp_path / "alias.cbz"
    try:
        alias.symlink_to(source)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks unavailable in this environment")

    with pytest.raises(ValueError, match="Symbolic-link"):
        camelia_decensor.run_batch(
            _args(alias, tmp_path / "Comix", tmp_path, dry_run=True), _inspector
        )


@pytest.mark.parametrize("tagged", [False, True])
def test_child_result_must_be_tagged_before_batch_accepts_it(tmp_path, monkeypatch, tagged):
    source = _book(tmp_path / "input" / "Book.cbz")
    destination = tmp_path / "Comix" / "Book.cbz"

    class FakeChild:
        def __enter__(self):
            _book(destination, tagged=tagged)
            result = {
                "path": str(destination), "original_deleted": False, "backup_path": None,
            }
            self.stdout = iter([f"CAMELIA_RESULT={json.dumps(result)}\n"])
            return self

        def __exit__(self, *_args):
            return False

        def wait(self):
            return 0

    def inspect(path):
        with zipfile.ZipFile(path) as archive:
            marked = b"uncensored" in archive.read("ComicInfo.xml").lower()
        return {"already_processed": marked}

    launches = []

    def fake_popen(*_args, **kwargs):
        launches.append(kwargs)
        return FakeChild()

    monkeypatch.setattr(camelia_decensor.subprocess, "Popen", fake_popen)
    if tagged:
        camelia_decensor.run_camelia_book(
            source, destination, ["mosaic"], tmp_path, tmp_path / "python.exe", inspect
        )
    else:
        with pytest.raises(RuntimeError, match="missing its uncensored marker"):
            camelia_decensor.run_camelia_book(
                source, destination, ["mosaic"], tmp_path, tmp_path / "python.exe", inspect
            )
    assert launches[0]["encoding"] == "utf-8"
    assert launches[0]["env"]["PYTHONIOENCODING"] == "UTF-8"
