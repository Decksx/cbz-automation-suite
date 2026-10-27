"""Temporary sealed archive batches, independent from the store/helper split."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import zipfile

import pytest

from comic_automation.web.jobs import archives as ar

JOB_ID = "00000000000000000000000001"
BATCH_ID = "00000000000000000000000099"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "hot" / JOB_ID
    root.mkdir(parents=True)
    command = root / "command.json"
    command.write_text('{"fixture":true}')
    logs = root / "log"
    logs.mkdir()
    (logs / "seg-0.log").write_bytes(b"fixture log\n")
    archive = tmp_path / "archive"
    archive.mkdir()
    return archive, ar.ArchiveJob(JOB_ID, "admission_expired", digest(command), root)


def test_publish_reopen_verify_and_same_handle_command_read(source):
    directory, job = source
    receipt = ar.write_batch(directory, BATCH_ID, [job], 1000)
    target = directory / receipt.file_name
    assert receipt.sha256 == digest(target)
    manifest = ar.verify_batch(target, receipt.sha256, jobs={JOB_ID: job}, check_sources=True)
    assert manifest["jobs"][0]["state"] == "admission_expired"
    assert ar.read_command(target, receipt.sha256, JOB_ID, job.command_digest) == (job.root / "command.json").read_bytes()
    assert (job.root / "log" / "seg-0.log").read_bytes() == b"fixture log\n"


def test_existing_batch_is_never_overwritten(source):
    directory, job = source
    receipt = ar.write_batch(directory, BATCH_ID, [job], 1000)
    before = (directory / receipt.file_name).read_bytes()
    with pytest.raises(ar.ArchiveRefused, match="already exists"):
        ar.write_batch(directory, BATCH_ID, [job], 1001)
    assert (directory / receipt.file_name).read_bytes() == before


def test_archive_digest_failure_never_falls_back(source):
    directory, job = source
    receipt = ar.write_batch(directory, BATCH_ID, [job], 1000)
    target = directory / receipt.file_name
    with target.open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(ar.ArchiveRefused, match="batch digest mismatch"):
        ar.read_command(target, receipt.sha256, JOB_ID, job.command_digest)
    assert job.root.is_dir()


def test_changed_hot_source_refuses_cleanup_verification(source):
    directory, job = source
    receipt = ar.write_batch(directory, BATCH_ID, [job], 1000)
    (job.root / "log" / "seg-0.log").write_bytes(b"changed")
    with pytest.raises(ar.ArchiveRefused, match="byte limit|Source changed"):
        ar.verify_batch(directory / receipt.file_name, receipt.sha256, jobs={JOB_ID: job}, check_sources=True)
    assert job.root.is_dir()


def test_command_digest_must_match_the_ledger_on_write_and_read(source):
    directory, job = source
    bad = ar.ArchiveJob(job.job_id, job.state, "0" * 64, job.root)
    with pytest.raises(ar.ArchiveRefused, match="Command digest"):
        ar.write_batch(directory, BATCH_ID, [bad], 1000)
    assert (job.root / "command.json").exists()
    receipt = ar.write_batch(directory, "00000000000000000000000098", [job], 1000)
    with pytest.raises(ar.ArchiveRefused, match="requested ledger row"):
        ar.read_command(directory / receipt.file_name, receipt.sha256, JOB_ID, "0" * 64)


def test_active_job_is_not_an_archive_source(source):
    directory, job = source
    active = ar.ArchiveJob(job.job_id, "running", job.command_digest, job.root)
    with pytest.raises(ar.ArchiveRefused, match="terminal ledger"):
        ar.write_batch(directory, BATCH_ID, [active], 1000)
    assert (job.root / "command.json").exists()


def test_failed_fsync_cannot_publish_a_batch(source, monkeypatch):
    directory, job = source
    def fail(fd):
        raise OSError("injected archive sync failure")
    monkeypatch.setattr(ar.os, "fsync", fail)
    with pytest.raises(OSError, match="injected"):
        ar.write_batch(directory, BATCH_ID, [job], 1000)
    assert not (directory / f"jobs-{BATCH_ID}.zip").exists()
    assert (job.root / "command.json").exists()


def test_bounded_byte_and_entry_budgets(source, monkeypatch):
    directory, job = source
    monkeypatch.setattr(ar, "MAX_BATCH_BYTES", 4)
    with pytest.raises(ar.ArchiveRefused, match="byte limit"):
        ar.write_batch(directory, BATCH_ID, [job], 1000)
    monkeypatch.setattr(ar, "MAX_BATCH_BYTES", 1024)
    monkeypatch.setattr(ar, "MAX_JOB_ENTRIES", 1)
    with pytest.raises(ar.ArchiveRefused, match="entry limit"):
        ar.write_batch(directory, "00000000000000000000000098", [job], 1000)


def rewrite_batch(path, change):
    with zipfile.ZipFile(path) as batch:
        members = {name: batch.read(name) for name in batch.namelist()}
    manifest = json.loads(members["manifest.json"])
    change(manifest, members)
    members["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as batch:
        for name, data in members.items():
            batch.writestr(name, data)


@pytest.mark.parametrize("fault", ["member_digest", "member_size", "extra_member", "path_escape"])
def test_manifest_and_member_evidence_are_verified(source, fault):
    directory, job = source
    receipt = ar.write_batch(directory, BATCH_ID, [job], 1000)
    target = directory / receipt.file_name
    def change(manifest, members):
        entry = manifest["jobs"][0]["members"][0]
        if fault == "member_digest":
            entry["sha256"] = "0" * 64
        elif fault == "member_size":
            entry["size"] += 1
        elif fault == "extra_member":
            members[JOB_ID + "/extra"] = b"unlisted"
        else:
            entry["path"] = JOB_ID + "/../escape"
    rewrite_batch(target, change)
    with pytest.raises(ar.ArchiveRefused):
        ar.verify_batch(target, digest(target))
    assert (job.root / "command.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="privilege-free Windows junction fixture")
def test_junction_source_is_rejected_before_traversal(source, tmp_path):
    directory, job = source
    junction = tmp_path / "source-junction"
    assert job.root.resolve().is_relative_to(tmp_path.resolve())
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(job.root)], capture_output=True)
    assert result.returncode == 0
    redirected = ar.ArchiveJob(JOB_ID, job.state, job.command_digest, junction)
    with pytest.raises(ar.ArchiveRefused, match="Reparse"):
        ar.write_batch(directory, BATCH_ID, [redirected], 1000)
    assert (job.root / "command.json").exists()
