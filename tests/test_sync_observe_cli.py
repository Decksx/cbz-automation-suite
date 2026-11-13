"""Estimate/progress and the real read-only observation CLI (#108)."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from comic_automation.library import sync_inventory as sync
from comic_automation.library import sync_observe_cli as cli


@pytest.fixture
def declared(tmp_path):
    tower, target = tmp_path / "tower", tmp_path / "target"
    tower.mkdir()
    target.mkdir()
    for root in (tower, target):
        (root / "book.cbz").write_bytes(b"same")
    path = tmp_path / "pairs.json"
    path.write_text(json.dumps(dict(contract="sync-root-pairs-v1", pairs=[
        dict(folder_id="manga", tower="tower", target="target")
    ])), encoding="utf-8")
    return path, sync.RootPair("manga", tower, target)


def test_estimate_counts_each_side_without_payload_reads(declared, monkeypatch):
    _, pair = declared
    (pair.tower / "unmatched.cbz").write_bytes(b"unique size")
    monkeypatch.setattr(sync, "_hash", lambda *args, **kwargs: pytest.fail("estimated payload was read"))
    report = sync.observe_pairs((pair,), hash_content=False)
    assert report["complete"] and report["hash_mode"] == "estimate"
    assert report["hash_plan"] == dict(candidate_files=2, candidate_bytes=8, inventory_complete=True,
                                       by_side=dict(tower=dict(files=1, bytes=4), target=dict(files=1, bytes=4)))
    assert all(row["sha256"] is None and row["bytes_read"] == 0 for row in report["files"])


def test_progress_precedes_payload_and_reports_within_large_files(declared, monkeypatch):
    _, pair = declared
    data = b"x" * (2 * sync._BLOCK + 3)
    for root in (pair.tower, pair.target):
        (root / "book.cbz").write_bytes(data)
    events = []
    original = Path.open
    def checked(path, mode="r", *args, **kwargs):
        if mode == "rb":
            assert any(event["phase"] == "hash_plan" for event in events)
        return original(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", checked)
    report = sync.observe_pairs((pair,), progress=events.append)
    reads = [event for event in events if event["phase"] == "hash_read"]
    assert len(reads) >= 6 and reads[-1]["bytes_read"] == 2 * len(data)
    assert [event["bytes_read"] for event in reads] == sorted(event["bytes_read"] for event in reads)
    assert any(event["phase"] == "hash_read" and event["files_hashed"] == 0 for event in events)
    assert events[-1] == dict(phase="complete", complete=True, files_hashed=2)
    assert report == sync.observe_pairs((pair,))  # progress timing never changes report evidence


def test_a_progress_consumer_cannot_modify_report_scope_or_estimates(declared):
    _, pair = declared
    def mutate(event):
        if event["phase"] == "hash_plan":
            event["by_side"]["tower"]["bytes"] = 0
    report = sync.observe_pairs((pair,), progress=mutate)
    assert report["hash_plan"]["by_side"]["tower"]["bytes"] == 4


def test_metadata_estimation_reports_inventory_and_recheck_counts(declared):
    events = []
    report = sync.observe_pairs((declared[1],), hash_content=False, progress=events.append)
    for phase in ("inventory_read", "recheck_read"):
        measured = [event for event in events if event["phase"] == phase]
        assert len(measured) == 2 and all(event["files"] == 1 for event in measured)
    assert report["complete"] and not any(event["phase"] == "hash_read" for event in events)


@pytest.mark.parametrize("failure", [OSError, ValueError, RuntimeError])
def test_failed_progress_consumers_stop_after_first_read(declared, monkeypatch, failure):
    _, pair = declared
    original = Path.open
    opened = []
    def track(path, mode="r", *args, **kwargs):
        if mode == "rb":
            opened.append(path)
        return original(path, mode, *args, **kwargs)
    def failed(event):
        if event["phase"] == "hash_read":
            raise failure("controlled consumer failure")
    monkeypatch.setattr(Path, "open", track)
    with pytest.raises(sync.ProgressError) as raised:
        sync.observe_pairs((pair,), progress=failed)
    assert isinstance(raised.value.__cause__, failure) and len(opened) == 1


def test_incomplete_inventory_stops_before_expensive_hashing(declared, monkeypatch):
    _, pair = declared
    original = sync._walk
    def incomplete(root, prefixes):
        rows, excluded, problems = original(root, prefixes)
        if root == pair.tower:
            problems.append(dict(path="unreadable", reason="controlled coverage gap"))
        return rows, excluded, problems
    monkeypatch.setattr(sync, "_walk", incomplete)
    monkeypatch.setattr(sync, "_hash", lambda *args, **kwargs: pytest.fail("incomplete inventory payload read"))
    report = sync.observe_pairs((pair,))
    assert not report["complete"] and not report["hash_plan"]["inventory_complete"]
    assert report["hash_plan"]["candidate_bytes"] == 8
    assert all(row["sha256"] is None for row in report["files"])


@pytest.mark.parametrize("value", [None, 0, "False"])
def test_hash_content_requires_a_boolean(declared, value):
    with pytest.raises(ValueError):
        sync.observe_pairs((declared[1],), hash_content=value)


def test_estimate_and_content_modes_are_explicitly_bound(declared):
    _, pair = declared
    estimate = sync.observe_pairs((pair,), hash_content=False)
    content = sync.observe_pairs((pair,))
    assert estimate["hash_mode"] == "estimate" and content["hash_mode"] == "content"
    assert estimate["observation_digest"] != content["observation_digest"]
    assert all(row["sha256"] for row in content["files"])


@pytest.mark.parametrize("mode", ["estimate", "observe"])
def test_cli_json_and_progress_have_separate_streams_and_preserve_payloads(declared, capsys, mode):
    manifest, pair = declared
    before = {root: (root / "book.cbz").read_bytes() for root in (pair.tower, pair.target)}
    assert cli.main([mode, "--pairs", str(manifest)]) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    events = [json.loads(line) for line in captured.err.splitlines()]
    assert report["complete"] and report["hash_plan"]["candidate_bytes"] == 8
    assert any(event["phase"] == "hash_plan" for event in events) and events[-1]["phase"] == "complete"
    assert report["hash_mode"] == ("content" if mode == "observe" else "estimate")
    assert all((root / "book.cbz").read_bytes() == payload for root, payload in before.items())


def test_cli_quiet_and_real_module_entrypoint(declared):
    manifest, _ = declared
    run = subprocess.run([sys.executable, "-m", "comic_automation.library.sync_observe_cli", "estimate",
                          "--pairs", str(manifest), "--quiet"], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0 and run.stderr == ""
    assert json.loads(run.stdout)["hash_mode"] == "estimate"


@pytest.mark.parametrize("change", ["secret", "contract", "pairs", "id", "path", "drive_relative", "prefixes", "empty"])
def test_invalid_manifests_fail_without_echoing_unknown_values(declared, capsys, change):
    manifest, _ = declared
    document = json.loads(manifest.read_text())
    row = document["pairs"][0]
    if change == "secret":
        document["api_key"] = "NEVER_ECHO_THIS"
    elif change == "contract":
        document["contract"] = "unknown"
    elif change == "pairs":
        document["pairs"] = "invalid"
    elif change == "id":
        row["folder_id"] = ""
    elif change == "path":
        row["target"] = 7
    elif change == "drive_relative":
        row["target"] = "X:relative"
    elif change == "prefixes":
        row["exclude_prefixes"] = "private"
    else:
        document["pairs"] = []
    manifest.write_text(json.dumps(document))
    assert cli.main(["estimate", "--pairs", str(manifest), "--quiet"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "NEVER_ECHO_THIS" not in captured.err
    assert "observation refused" in captured.err


def test_missing_root_outputs_an_incomplete_report(declared, capsys):
    manifest, pair = declared
    (pair.target / "book.cbz").unlink()
    pair.target.rmdir()
    assert cli.main(["estimate", "--pairs", str(manifest), "--quiet"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert not report["complete"] and report["problems"]


class BrokenWriter:
    def write(self, value):
        raise BrokenPipeError("controlled output failure")
    def flush(self):
        pass


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_broken_output_consumers_return_failure(declared, monkeypatch, stream):
    manifest, _ = declared
    with monkeypatch.context() as patch:
        patch.setattr(sys, stream, BrokenWriter())
        assert cli.main(["observe", "--pairs", str(manifest)]) == 1


def test_cli_has_no_apply_or_sync_control_command(declared):
    with pytest.raises(SystemExit) as raised:
        cli.main(["apply", "--pairs", str(declared[0])])
    assert raised.value.code == 2
