"""Windows no-replace case renames retain bytes across final/rollback errors."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from comic_automation.library import arrival_rename as ar


def test_case_change_uses_two_owned_steps_and_preserves_bytes(tmp_path, monkeypatch):
    source, target = tmp_path / "Series Ch. 3.cbz", tmp_path / "Series ch. 3.cbz"
    source.write_bytes(b"original archive")
    steps = []
    original = Path.rename

    def observe(path, destination):
        steps.append((path, Path(destination)))
        return original(path, destination)

    monkeypatch.setattr(Path, "rename", observe)
    assert ar.rename_arrival(source, target) == target
    assert target.read_bytes() == b"original archive"
    assert [p.name for p in tmp_path.iterdir()] == [target.name]
    assert len(steps) == 2 and steps[0][1] == steps[1][0]


def test_occupied_destination_is_never_replaced(tmp_path):
    source, target = tmp_path / "old.cbz", tmp_path / "new.cbz"
    source.write_bytes(b"source")
    target.write_bytes(b"foreign")
    with pytest.raises(FileExistsError):
        ar.rename_arrival(source, target)
    assert source.read_bytes() == b"source" and target.read_bytes() == b"foreign"


def test_case_alias_requires_same_file_identity(tmp_path, monkeypatch):
    source, target = tmp_path / "Series.cbz", tmp_path / "series.cbz"
    source.write_bytes(b"source")
    if not target.exists():
        target.write_bytes(b"other")
    monkeypatch.setattr(Path, "samefile", lambda *a: False)
    with pytest.raises(FileExistsError):
        ar.rename_arrival(source, target)
    assert source.read_bytes() == b"source"


def test_final_step_failure_restores_original_spelling_and_bytes(tmp_path, monkeypatch):
    source, target = tmp_path / "Series.cbz", tmp_path / "series.cbz"
    source.write_bytes(b"source")
    original = Path.rename

    def deny_final(path, destination):
        if path.name.startswith(".media-case-") and Path(destination).name == target.name:
            raise PermissionError("final step locked")
        return original(path, destination)

    monkeypatch.setattr(Path, "rename", deny_final)
    with pytest.raises(PermissionError, match="final step"):
        ar.rename_arrival(source, target)
    assert [p.name for p in tmp_path.iterdir()] == [source.name]
    assert source.read_bytes() == b"source"


@pytest.mark.skipif(os.name != "nt", reason="Windows rename refuses occupied names atomically")
def test_occupied_temporary_name_never_replaces_foreign_bytes(tmp_path, monkeypatch):
    source, target = tmp_path / "Series.cbz", tmp_path / "series.cbz"
    source.write_bytes(b"source")
    temporary = tmp_path / ".media-case-fixed.tmp"
    temporary.write_bytes(b"foreign")
    monkeypatch.setattr(ar, "uuid4", lambda: SimpleNamespace(hex="fixed"))
    with pytest.raises(FileExistsError):
        ar.rename_arrival(source, target)
    assert temporary.read_bytes() == b"foreign" and source.read_bytes() == b"source"


@pytest.mark.skipif(os.name != "nt", reason="Windows no-replace rollback protocol")
def test_rollback_collision_exposes_retained_archive_without_overwriting_interloper(tmp_path, monkeypatch):
    source, target = tmp_path / "Series.cbz", tmp_path / "series.cbz"
    source.write_bytes(b"source")
    original = Path.rename

    def block_after_first_step(path, destination):
        if path.name.startswith(".media-case-") and Path(destination).name == target.name:
            source.write_bytes(b"interloper")
            raise PermissionError("final step blocked")
        return original(path, destination)

    monkeypatch.setattr(Path, "rename", block_after_first_step)
    with pytest.raises(ar.CaseRenameRecoveryError) as error:
        ar.rename_arrival(source, target)
    assert source.read_bytes() == b"interloper"
    assert error.value.retained_path.read_bytes() == b"source"
