"""Component contracts behind design step 7 (lock, journal, markers, plan, CLI).

The §7 named tests live in `tests/test_canonical_apply.py`; these pin the
individual guarantees they rest on, each of which a mutant was written
against: the owner file follows the lock, every record is fsynced, the
marker table is append-only and required, ``os.replace`` is never used,
quarantine is a same-volume move that deletes nothing, plan structure is
refused before anything is locked, the CLI has no default paths and no
fault injection, and a run stops at a batch boundary.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.library.canonical_apply import cli, executor
from comic_automation.library.canonical_apply import journal as jn
from comic_automation.library.canonical_apply import markers, scope_lock
from comic_automation.library.canonical_apply.plan import (
    DependantSpec,
    FamilySpec,
    PlanError,
    build_plan,
    compute_digest,
    load_plan,
    parse_plan,
)
from tests import canonical_apply_fixtures as fx
from tests.canonical_apply_fixtures import P1, P2, P3, book, env  # noqa: F401

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="the step protocol is Windows-only (os.rename semantics)"
)


def _rename_doc(env, name="a.cbz", new="A ch. 1.cbz", action_id="ren1", folder=None):
    folder = folder or env.series
    source = book(folder, name, [P1])
    return env.plan(renames=[env.rename_spec(source, folder / new, action_id)]), source, folder / new


# --- scope lock -----------------------------------------------------------------


def test_owner_file_is_written_only_by_the_holder(env):
    owner = env.root / scope_lock.OWNER_FILENAME
    with scope_lock.ScopeLock(env.root, "first", "apply"):
        assert json.loads(owner.read_text())["attempt_id"] == "first"
        with pytest.raises(scope_lock.ScopeLockHeld):
            scope_lock.ScopeLock(env.root, "second", "apply").acquire()
        # The refused acquirer never wrote the owner file.
        assert json.loads(owner.read_text())["attempt_id"] == "first"


def test_the_lock_refuses_off_windows(env, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(scope_lock.UnsupportedPlatformError):
        scope_lock.ScopeLock(env.root, "x", "apply").acquire()
    document, source, _ = _rename_doc(env)
    with pytest.raises(scope_lock.UnsupportedPlatformError):
        env.apply(document)
    assert source.exists()


# --- journal ----------------------------------------------------------------------


def test_every_journal_record_is_fsynced_before_append_returns(env, monkeypatch):
    synced = []
    real = os.fsync

    def spy(fd):
        synced.append(fd)
        return real(fd)

    monkeypatch.setattr(os, "fsync", spy)
    writer = jn.JournalWriter.create(env.root, "a1", {"mode": "apply"})
    for n in range(3):
        before = len(synced)
        writer.append(jn.KIND_NOTE, {"note": f"n{n}"})
        assert len(synced) == before + 1
        # Durable and readable as soon as append returns.
        assert len(jn.read_journal(env.root, "a1")) == n + 2
    writer.close()


def test_a_journal_is_created_exclusively(env):
    jn.JournalWriter.create(env.root, "a1", {"mode": "apply"}).close()
    with pytest.raises(FileExistsError):
        jn.JournalWriter.create(env.root, "a1", {"mode": "apply"})
    assert len(jn.read_journal(env.root, "a1")) == 1


def test_a_journal_record_from_another_attempt_is_refused(env):
    jn.JournalWriter.create(env.root, "a1", {"mode": "apply"}).close()
    path = jn.journal_path(env.root, "a1")
    path.write_bytes(path.read_bytes().replace(b'"a1"', b'"a2"'))
    with pytest.raises(jn.JournalCorruptError, match="belongs to attempt"):
        jn.read_journal(env.root, "a1")


# --- marker table ---------------------------------------------------------------


def test_the_marker_table_is_append_only(env):
    env.execute(
        "INSERT INTO rename_attempt_steps (attempt_id, action_id, step, kind, location_id, "
        "preimage_path, postimage_path) VALUES ('a', 'b', 1, 'rename', 1, 'x', 'y')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        env.execute("UPDATE rename_attempt_steps SET postimage_path = 'z'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        env.execute("DELETE FROM rename_attempt_steps")
    with pytest.raises(sqlite3.IntegrityError):
        env.execute(
            "INSERT INTO rename_attempt_steps (attempt_id, action_id, step, kind, location_id, "
            "preimage_path, postimage_path) VALUES ('a', 'b', 2, 'rename', 1, 'x', 'x')"
        )


def test_apply_refuses_a_database_without_the_marker_table(env, tmp_path):
    bare = tmp_path / "bare.db"
    with database_connection(bare) as connection:
        apply_migrations(connection, fx.MIGRATIONS)
    document, source, _ = _rename_doc(env)
    with pytest.raises(markers.SchemaMissingError):
        executor.apply(env.load(document), library_root=env.root, database=bare, quiescence=fx.quiet)
    assert source.exists() and env.attempts() == ()
    # install-schema through the CLI, twice: idempotent.
    assert cli.main(["install-schema", "--database", str(bare)]) == 0
    assert cli.main(["install-schema", "--database", str(bare)]) == 0
    with database_connection(bare) as connection:
        markers.require_schema(connection)
        assert "rename_attempt_steps" not in {
            row[1] for row in connection.execute("SELECT version, name FROM schema_migrations")
        }


def test_the_apply_connection_is_synchronous_full(env):
    connection = markers.open_durable(env.database)
    try:
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    finally:
        connection.close()


# --- the filesystem step ---------------------------------------------------------


def test_apply_moves_with_os_rename_and_never_os_replace(env, monkeypatch):
    spec, keeper, deps = _family_spec(env)
    document = env.plan(families=[spec])
    renamed = []
    real = os.rename

    def no_replace(*args, **kwargs):
        raise AssertionError("os.replace must never be used")

    def spy(source, destination):
        renamed.append((source, destination))
        return real(source, destination)

    monkeypatch.setattr(os, "replace", no_replace)
    monkeypatch.setattr(os, "rename", spy)
    report = env.apply(document)
    assert report.stopped is None
    assert renamed == [(str(deps[0]), str(Path(env.quarantine_root) / "fam1" / deps[0].name))]


def _family_spec(env):
    keeper = book(env.series, "Series ch. 1 v2.cbz", [P1, P2])
    dep = book(env.series, "Series ch. 1 v1.cbz", [P1])
    return env.family_spec("fam1", keeper, [(dep, 1)]), keeper, [dep]


def test_quarantine_is_a_same_volume_owned_move_that_deletes_nothing(env, monkeypatch):
    spec, keeper, deps = _family_spec(env)
    document = env.plan(families=[spec])
    before = sorted(fx.sha(p) for p in env.series.iterdir())

    report = env.apply(document)

    target = Path(env.quarantine_root) / "fam1" / deps[0].name
    assert report.stopped is None and target.exists()
    assert target.parent.parent.name == fx.DATE
    assert target.parent.parent.parent == env.root / ".cbz-canonical-quarantine"
    after = sorted(fx.sha(p) for p in list(env.series.iterdir()) + [target])
    assert after == before
    assert os.stat(target).st_dev == os.stat(keeper).st_dev

    # A destination on another volume is refused before any intent.
    other, _, _ = _family_spec_in(env, "Other")
    monkeypatch.setattr(executor, "_same_volume", lambda source, destination: False)
    report = env.apply(env.plan(families=[other]))
    assert fx.unit_status(report, "fam2") == executor.UNIT_BLOCKED
    assert "another volume" in report.unit("fam2")["reasons"][0]
    assert env.step_journal(report.attempt_id) == []


def _family_spec_in(env, folder_name):
    folder = env.root / folder_name
    keeper = book(folder, "Other ch. 1 v2.cbz", [P1, P3])
    dep = book(folder, "Other ch. 1 v1.cbz", [P1])
    return env.family_spec("fam2", keeper, [(dep, 1)]), keeper, [dep]


# --- the plan ----------------------------------------------------------------------


def test_plan_structure_is_refused_before_anything_is_locked(env, tmp_path):
    source = book(env.series, "a.cbz", [P1])
    other = book(env.series, "b.cbz", [P2])
    spec = env.rename_spec(source, env.series / "A ch. 1.cbz", "ren1")

    def refused(**overrides):
        fields = dict(spec.__dict__, **overrides)
        with pytest.raises(PlanError):
            env.plan(renames=[spec.__class__(**fields)])

    refused(destination=str(tmp_path / "outside.cbz"))                 # outside the root
    refused(destination=str(env.root / "Elsewhere" / "A.cbz"))         # changes directory
    refused(destination=str(env.series / "A ch. 1.txt"))               # not a .cbz
    refused(destination=str(env.series / ".." / "Series" / "x.cbz"))   # not normalised
    refused(sha256="abc")
    refused(action_id="bad id with spaces")
    refused(destination=str(env.root / ".cbz-canonical-naming" / "x.cbz"))

    second = env.rename_spec(other, env.series / "B ch. 1.cbz", "ren2")
    with pytest.raises(PlanError, match="another action's source"):
        env.plan(renames=[spec.__class__(**dict(spec.__dict__, destination=str(other))), second])

    keeper = book(env.series, "K v2.cbz", [P1, P2])
    dep = book(env.series, "K v1.cbz", [P1])
    family = env.family_spec("fam1", keeper, [(dep, 1)])
    bad_relation = FamilySpec(
        family_id="fam1", keeper=family.keeper,
        dependants=(DependantSpec(**dict(family.dependants[0].__dict__, outcome="inserted_pages_review")),),
    )
    with pytest.raises(PlanError, match="strict-prefix"):
        env.plan(families=[bad_relation])
    outside_quarantine = FamilySpec(
        family_id="fam1", keeper=family.keeper,
        dependants=(DependantSpec(**dict(family.dependants[0].__dict__, quarantine_to=str(env.series / "q.cbz"))),),
    )
    with pytest.raises(PlanError, match="quarantine_root"):
        env.plan(families=[outside_quarantine])
    with pytest.raises(PlanError, match="quarantine_root"):
        build_plan(library_root=str(env.root), quarantine_root=str(env.root / "q" / fx.DATE))


def test_the_digest_is_over_canonical_bytes_not_file_formatting(env):
    document, source, _ = _rename_doc(env)
    path = env.tmp / "reformatted.json"
    path.write_text(json.dumps(dict(reversed(list(document.items())))), encoding="utf-8")
    plan = load_plan(
        path, library_root=env.root, expect_digest=document["plan_digest"],
        expect_renames=1, expect_families=0,
    )
    assert plan.digest == compute_digest(document) == document["plan_digest"]
    with pytest.raises(PlanError):
        parse_plan(dict(document, contract="canonical-naming-plan-v0"))


def test_family_spec_from_evaluation_refuses_a_review_verdict():
    from types import SimpleNamespace

    from comic_automation.library.canonical_apply.plan import family_spec_from_evaluation

    with pytest.raises(PlanError, match="actionable"):
        family_spec_from_evaluation(
            SimpleNamespace(status="review", keeper="k"), SimpleNamespace(archives=()),
            family_id="f", locations={}, quarantine_root="q",
        )


# --- attempts --------------------------------------------------------------------


def test_a_new_attempt_is_refused_while_another_is_unfinished(env):
    document, source, destination = _rename_doc(env)
    with pytest.raises(fx.SimulatedCrash):
        env.apply(document, fault=fx.crash_at("after_done"))
    second, _, _ = _rename_doc(env, "c.cbz", "C ch. 1.cbz", "ren9")
    with pytest.raises(executor.Refused, match="unfinished"):
        env.apply(second)
    assert len(env.attempts()) == 1


def test_the_same_plan_is_not_applied_twice_without_resume(env):
    document, source, destination = _rename_doc(env)
    first = env.apply(document)
    with pytest.raises(executor.Refused, match="already applied"):
        env.apply(document)
    assert env.attempts() == (first.attempt_id,)
    resumed = env.apply(document, resume_attempt=first.attempt_id)
    assert fx.unit_status(resumed, "ren1") == executor.UNIT_ALREADY_COMPLETE


def test_a_resume_must_name_an_apply_of_the_same_plan(env):
    document, _, _ = _rename_doc(env)
    first = env.apply(document)
    other, _, _ = _rename_doc(env, "c.cbz", "C ch. 1.cbz", "ren9")
    with pytest.raises(executor.Refused, match="not an apply of plan"):
        env.apply(other, resume_attempt=first.attempt_id)


def test_a_batch_boundary_rechecks_quiescence_and_the_drift_fraction(env):
    specs = []
    for n in range(3):
        source = book(env.series, f"s{n}.cbz", [bytes([n])])
        specs.append(env.rename_spec(source, env.series / f"S ch. {n}.cbz", f"ren{n}"))
    document = env.plan(renames=specs)
    calls = []

    def probe(root):
        calls.append(root)
        return None if len(calls) < 3 else "watcher resumed"

    report = env.apply(document, quiescence=probe, batch_size=1)
    assert [u["status"] for u in report.units] == [
        executor.UNIT_COMPLETED, executor.UNIT_COMPLETED, executor.UNIT_NOT_REACHED,
    ]
    assert "quiescence lost" in report.stopped

    specs = []
    for n in range(3):
        source = book(env.root / "D", f"d{n}.cbz", [bytes([10 + n])])
        specs.append(env.rename_spec(source, env.root / "D" / f"D ch. {n}.cbz", f"drift{n}"))
    document = env.plan(renames=specs)
    (env.root / "D" / "d0.cbz").write_bytes(b"drifted")
    report = env.apply(document, batch_size=1, max_drift_fraction=0.0)
    assert [u["status"] for u in report.units] == [
        executor.UNIT_BLOCKED, executor.UNIT_NOT_REACHED, executor.UNIT_NOT_REACHED,
    ]
    assert "run-wide drift" in report.stopped


# --- CLI ---------------------------------------------------------------------------


def test_the_cli_has_no_default_paths_and_no_fault_injection():
    parser = cli.build_parser()
    options = set()
    for action in parser._subparsers._group_actions[0].choices.values():
        for option in action._actions:
            options.update(option.option_strings)
            if option.dest in ("library_root", "database", "plan"):
                assert option.required and option.default is None
    assert not any("fault" in o or "crash" in o for o in options)
    with pytest.raises(SystemExit):
        cli.main(["apply", "--plan", "p.json"])


def test_the_cli_requires_attested_quiescence_and_then_applies(env, capsys):
    document, source, destination = _rename_doc(env)
    path = env.write_plan(document)
    argv = [
        "apply", "--library-root", str(env.root), "--database", str(env.database),
        "--plan", str(path), "--expect-digest", document["plan_digest"],
        "--expect-renames", "1", "--expect-families", "0",
    ]
    assert cli.main(argv) == 1
    assert "not quiesced" in capsys.readouterr().err
    assert source.exists() and env.attempts() == ()

    assert cli.main(argv + ["--attest-quiescent"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["units"][0]["status"] == executor.UNIT_COMPLETED
    assert destination.exists() and not source.exists()

    common = argv[1:] + ["--attest-quiescent"]
    assert cli.main(["recover", *common, "--attempt", report["attempt_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["resolutions"][0]["status"] == executor.RES_COMPLETE
    assert cli.main(["undo", *common, "--forward-attempt", report["attempt_id"]]) == 0
    assert source.exists() and not destination.exists()
