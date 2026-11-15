"""Canonical apply/recover/undo under the library writer gate (#158 B3, slice 2).

Design: ``docs/library_writer_coordination.md`` ("Maintenance runs and
quiescence during rollout"). A run holds the registered root's gate
exclusively after the canonical scope lock, so a participating writer
defers for the whole run, and a busy gate refuses before anything is
journalled or moved. The gate never replaces attestation on a root that is
not ``enforced``: an old watcher that never takes the gate is invisible to it.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

from comic_automation.library import canonical_plan as cp
from comic_automation.library import writer_gate as wg
from comic_automation.library.canonical_apply import cli, executor
from tests.canonical_apply_fixtures import env  # noqa: F401  (fixture)
from tests.test_canonical_apply_components import _rename_doc

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the step protocol and gate are Windows-only")


def registry(path: Path, root: Path, *, enforced: bool = False) -> Path:
    path.write_text(json.dumps({"contract": wg.REGISTRY_CONTRACT, "roots": [
        {"path": str(root), "series_depth": 1, "enforced": enforced}]}), encoding="utf-8")
    return path


def gate_for(env, *, enforced: bool = False) -> wg.CoordinationRoot:
    return wg.load_registry(registry(env.tmp / "roots.json", env.root, enforced=enforced))[0]


def contend(gate: wg.CoordinationRoot, key: str = "series") -> str:
    """A participating writer on another thread: granted or busy, never waiting."""
    result = []

    def writer():
        try:
            with wg.ordinary_unit(gate, key):
                result.append("granted")
        except wg.GateBusy:
            result.append("busy")

    thread = threading.Thread(target=writer)
    thread.start()
    thread.join(timeout=10)
    return result[0]


def test_an_apply_holds_the_gate_exclusively_for_the_whole_run(env):
    gate = gate_for(env)
    document, source, destination = _rename_doc(env)
    seen = []
    report = env.apply(document, writer_gate=gate, unit_gate=lambda unit: seen.append(contend(gate)))
    assert seen == ["busy"]                                  # a writer mid-run defers
    assert report.units[0]["status"] == executor.UNIT_COMPLETED and destination.exists()
    assert contend(gate) == "granted"                        # released afterwards
    owner = json.loads((env.root / wg.GATE_OWNER).read_text())
    assert owner["tool"] == "canonical_apply" and owner["mode"] == "apply"


@pytest.mark.parametrize("mode", ["recover", "undo"])
def test_recover_and_undo_hold_the_gate_too(env, mode):
    gate = gate_for(env)
    document, source, destination = _rename_doc(env)
    attempt = env.apply(document).attempt_id
    seen = []
    real = executor._check_quiescent

    def check_then_contend(quiescence, root):
        seen.append(contend(gate))
        return real(quiescence, root)

    executor._check_quiescent, saved = check_then_contend, real
    try:
        if mode == "recover":
            env.recover(document, attempt, writer_gate=gate)
        else:
            env.undo(document, attempt, writer_gate=gate)
    finally:
        executor._check_quiescent = saved
    assert seen == ["busy"]


def test_a_writer_mid_transaction_refuses_the_apply_before_anything_moves(env):
    gate = gate_for(env)
    document, source, destination = _rename_doc(env)
    with wg.ordinary_unit(gate, "series"):
        held = threading.Event()
        errors = []

        def run():
            try:
                env.apply(document, writer_gate=gate)
            except wg.GateBusy as exc:
                errors.append(exc)
            held.set()

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(timeout=30)
    assert errors and "writers active" in str(errors[0])
    assert source.exists() and not destination.exists() and env.attempts() == ()


def test_a_gate_that_does_not_enclose_the_library_root_is_refused(env, tmp_path):
    other = tmp_path / "other-library"
    other.mkdir()
    gate = wg.load_registry(registry(tmp_path / "other.json", other))[0]
    document, source, destination = _rename_doc(env)
    with pytest.raises(wg.GateRefused, match="no registered library root"):
        env.apply(document, writer_gate=gate)
    assert source.exists() and env.attempts() == ()


# --- CLI rollout rules -------------------------------------------------------------

def _argv(env, document, path, *extra):
    return ["apply", "--library-root", str(env.root), "--database", str(env.database), "--plan", str(path),
            "--expect-digest", document["plan_digest"], "--expect-renames", "1", "--expect-families", "0", *extra]


def test_a_root_not_yet_enforced_still_requires_operator_quiescence(env, capsys):
    """Mixed-version witness: the gate is free (an old watcher never takes it), yet
    the run refuses without --attest-quiescent; the external attestation is not enough."""
    document, source, destination = _rename_doc(env)
    path = env.write_plan(document)
    roots = registry(env.tmp / "roots.json", env.root, enforced=False)
    assert cli.main(_argv(env, document, path, "--writer-roots", str(roots), "--attest-no-external-writers")) == 1
    assert "not quiesced" in capsys.readouterr().err and source.exists()
    assert cli.main(_argv(env, document, path, "--writer-roots", str(roots), "--attest-quiescent")) == 0
    assert destination.exists()


def test_an_enforced_root_needs_only_the_non_participant_attestation(env, capsys):
    document, source, destination = _rename_doc(env)
    path = env.write_plan(document)
    roots = registry(env.tmp / "roots.json", env.root, enforced=True)
    assert cli.main(_argv(env, document, path, "--writer-roots", str(roots))) == 1
    assert "--attest-no-external-writers" in capsys.readouterr().err and source.exists()
    assert cli.main(_argv(env, document, path, "--writer-roots", str(roots), "--attest-no-external-writers")) == 0
    assert destination.exists()


def test_without_a_registry_nothing_changes(env, capsys):
    document, source, destination = _rename_doc(env)
    path = env.write_plan(document)
    assert cli.main(_argv(env, document, path, "--attest-no-external-writers")) == 1
    capsys.readouterr()
    assert cli.main(_argv(env, document, path, "--attest-quiescent")) == 0
    assert not (env.root / wg.GATE_LOCK).exists()


def test_a_library_under_no_registered_root_and_a_busy_gate_refuse_via_the_cli(env, tmp_path, capsys):
    document, source, destination = _rename_doc(env)
    path = env.write_plan(document)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert cli.main(_argv(env, document, path, "--writer-roots", str(registry(tmp_path / "x.json", elsewhere)),
                          "--attest-quiescent")) == 1
    assert "no registered library root" in capsys.readouterr().err
    roots = registry(env.tmp / "roots.json", env.root)
    with wg.ordinary_unit(wg.load_registry(roots)[0], "series"):
        result = []
        thread = threading.Thread(target=lambda: result.append(
            cli.main(_argv(env, document, path, "--writer-roots", str(roots), "--attest-quiescent"))))
        thread.start()
        thread.join(timeout=30)
    assert result == [1] and "GateBusy" in capsys.readouterr().err and source.exists()


# --- the planner ignores coordination state ---------------------------------------

def test_the_planner_never_inventories_control_files(tmp_path):
    root = tmp_path / "library"
    (root / "Series").mkdir(parents=True)
    for name in wg.CONTROL_FILENAMES:
        (root / name).write_bytes(b"")
    rows, books, excluded = cp._inventory(root)
    assert not any(Path(row["path"]).name in wg.CONTROL_FILENAMES for row in rows)
    assert {row["path"] for row in excluded} == set(wg.CONTROL_FILENAMES)
