"""§7 "Apply, drift and recovery" named tests for design step 7.

`comic_automation.library.canonical_apply` applies an approved plan with
attempt ownership: the OS scope lock, the fsynced journal, the
``rename_attempt_steps`` marker committed with the conditional path
updates, family-unit phased revalidation and frontier recovery.

Every test runs against a ``tmp_path`` library and a real migrated tmp
SQLite database (`tests.canonical_apply_fixtures`). Crashes are simulated
by raising `SimulatedCrash` (a BaseException) from the executor's injected
``fault`` hook at a named boundary: the run unwinds without writing
anything more, as a dying process would, and the OS-equivalent lock
release happens in the context manager.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from comic_automation.library.canonical_apply import executor
from comic_automation.library.canonical_apply import journal as jn
from comic_automation.library.canonical_apply import scope_lock
from comic_automation.library.canonical_apply.markers import (
    PairingRef,
    StepRefused,
    commit_step,
)
from comic_automation.library.canonical_apply.plan import PlanError, load_plan
from comic_automation.library.canonical_apply import cli
from tests import canonical_apply_fixtures as fx
from tests.canonical_apply_fixtures import (
    P1, P2, P3, P4, P5,
    SimulatedCrash,
    book,
    crash_at,
    env,  # noqa: F401  (pytest fixture)
    statuses,
    unit_status,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="the step protocol is Windows-only (os.rename semantics)"
)


# --- builders ---------------------------------------------------------------


def _family(env, *, rename=False, dependants=2):
    """A keeper with ``dependants`` strict-prefix versions below it."""
    pages = [P1, P2, P3, P4, P5][: dependants + 1]
    keeper = book(env.series, f"Series ch. 1 v{dependants + 1}.cbz", pages)
    deps = [
        (book(env.series, f"Series ch. 1 v{n}.cbz", pages[:n]), n)
        for n in range(1, dependants + 1)
    ]
    rename_to = env.series / "Series ch. 1 (2019) [v3].cbz" if rename else None
    spec = env.family_spec("fam1", keeper, deps, rename_to=rename_to)
    return spec, keeper, [path for path, _ in deps], rename_to


def _case_only(env):
    source = book(env.series, "series ch. 1.cbz", [P1, P2])
    destination = env.series / "Series ch. 1.cbz"
    spec = env.rename_spec(source, destination, "ren1")
    return spec, source, destination


def _rewrite(path: Path, pages) -> None:
    """Replace ``path``'s bytes with a different archive at the same name."""
    path.unlink()
    book(path.parent, path.name, pages)


# --- approval -----------------------------------------------------------------


def test_apply_refuses_a_plan_whose_digest_or_counts_differ(env, capsys):
    source = book(env.series, "a.cbz", [P1])
    document = env.plan(renames=[env.rename_spec(source, env.series / "A ch. 1.cbz", "ren1")])
    path = env.write_plan(document)
    good = dict(
        library_root=env.root,
        expect_digest=document["plan_digest"],
        expect_renames=1,
        expect_families=0,
    )
    for override in (
        {"expect_digest": "0" * 64},
        {"expect_renames": 2},
        {"expect_families": 1},
        {"library_root": env.root / "Series"},
    ):
        with pytest.raises(PlanError):
            load_plan(path, **{**good, **override})

    # A body edited after sealing no longer matches its own digest.
    tampered = dict(document, renames=[dict(document["renames"][0], destination=str(env.series / "B.cbz"))])
    with pytest.raises(PlanError, match="canonical bytes"):
        load_plan(env.write_plan(tampered), **good)
    # Counts that disagree with the contents are refused even when resealed.
    from comic_automation.library.canonical_apply.plan import compute_digest

    recounted = dict(document, counts=dict(document["counts"], renames=5))
    recounted["plan_digest"] = compute_digest(recounted)
    with pytest.raises(PlanError, match="counts"):
        load_plan(env.write_plan(recounted), **{**good, "expect_digest": recounted["plan_digest"], "expect_renames": 5})

    # The CLI refuses before the lock: exit 1, nothing moved, no journal.
    code = cli.main([
        "apply", "--library-root", str(env.root), "--database", str(env.database),
        "--plan", str(path), "--expect-digest", "f" * 64, "--expect-renames", "1",
        "--expect-families", "0", "--attest-quiescent",
    ])
    assert code == 1
    assert "does not match --expect-digest" in capsys.readouterr().err
    assert source.exists() and env.attempts() == ()
    assert not (env.root / scope_lock.LOCK_FILENAME).exists()


# --- family drift -------------------------------------------------------------


def test_keeper_drift_blocks_every_dependant_in_its_family(env):
    """The keeper changes after approval: no dependant is quarantined.

    A drift fraction of 1.0 shows the block does not come from the run-wide
    stop; an unrelated rename in the same plan still completes.
    """
    spec, keeper, deps, _ = _family(env)
    other = book(env.root / "Other", "o.cbz", [P5])
    rename = env.rename_spec(other, env.root / "Other" / "Other ch. 1.cbz", "ren1")
    document = env.plan(renames=[rename], families=[spec])
    _rewrite(keeper, [P1, P2, P4])  # same name, different third page

    report = env.apply(document, max_drift_fraction=1.0, batch_size=1)

    assert unit_status(report, "fam1") == executor.UNIT_BLOCKED
    assert "keeper drifted" in report.unit("fam1")["reasons"][0]
    assert all(path.exists() for path in deps)
    assert unit_status(report, "ren1") == executor.UNIT_COMPLETED
    attempt = report.attempt_id
    assert [a for _, a, _ in env.step_journal(attempt)] == ["ren1"] * 3
    assert {m[1] for m in env.markers()} == {"ren1"}


def test_a_vanished_keeper_blocks_its_family(env):
    spec, keeper, deps, _ = _family(env)
    document = env.plan(families=[spec])
    shutil.move(str(keeper), str(env.tmp / keeper.name))

    report = env.apply(document)

    assert unit_status(report, "fam1") == executor.UNIT_BLOCKED
    assert "keeper vanished" in report.unit("fam1")["reasons"][0]
    assert all(path.exists() for path in deps)
    assert env.step_journal(report.attempt_id) == []
    assert env.markers() == []


def test_a_keeper_scheduled_for_quarantine_blocks_its_family(env):
    """Family 1's keeper is family 2's quarantine candidate: both blocked."""
    from comic_automation.library.canonical_apply.plan import DependantSpec, FamilySpec

    k2 = book(env.series, "Series ch. 1 v4.cbz", [P1, P2, P3, P4])
    spec1, keeper, deps, _ = _family(env)
    # The same archive (same ids and path) is fam1's keeper and fam2's
    # quarantine candidate.
    second = FamilySpec(
        family_id="fam2",
        keeper=env.member(k2),
        dependants=(DependantSpec(
            member=spec1.keeper,
            action_id="fam2:quarantine:1",
            quarantine_to=str(Path(env.quarantine_root) / "fam2" / keeper.name),
            index_map=(0, 1, 2),
        ),),
    )
    document = env.plan(families=[spec1, second])
    before = env.tree()

    report = env.apply(document)

    assert unit_status(report, "fam1") == executor.UNIT_BLOCKED
    assert unit_status(report, "fam2") == executor.UNIT_BLOCKED
    assert any("scheduled for quarantine" in r for r in report.unit("fam1")["reasons"])
    assert env.tree() == before
    assert env.markers() == []


# --- phases and resume -------------------------------------------------------------


def test_interruption_between_keeper_rename_and_dependant_quarantine_resumes(env):
    spec, keeper, deps, renamed = _family(env, rename=True)
    document = env.plan(families=[spec])

    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at("after_keeper_rename"))
    attempt = env.only_attempt()
    assert renamed.exists() and not keeper.exists()
    assert all(path.exists() for path in deps)

    recovered = env.recover(document, attempt)
    assert statuses(recovered) == {
        "fam1:keeper": executor.RES_COMPLETE,
        "fam1:quarantine:1": executor.RES_NOTHING,
        "fam1:quarantine:2": executor.RES_NOTHING,
    }

    report = env.apply(document, resume_attempt=attempt)

    assert unit_status(report, "fam1") == executor.UNIT_COMPLETED
    assert not any(path.exists() for path in deps)
    quarantine = Path(env.quarantine_root) / "fam1"
    assert sorted(p.name for p in quarantine.iterdir()) == sorted(p.name for p in deps)
    assert renamed.exists()


def test_resume_revalidates_the_renamed_keeper_before_any_quarantine(env):
    """Companion: the keeper drifts at its new path while the run is down."""
    spec, keeper, deps, renamed = _family(env, rename=True)
    document = env.plan(families=[spec])
    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at("after_keeper_rename"))
    attempt = env.only_attempt()
    _rewrite(renamed, [P1, P2, P4])

    # The keeper rename is complete, so its proven location is history the
    # resume's recovery pass checks first; the drift refuses the resume.
    with pytest.raises(executor.Refused, match="proven location drifted"):
        env.apply(document, resume_attempt=attempt)
    assert all(path.exists() for path in deps)
    assert [a for k, a, _ in env.step_journal(attempt) if k == "intent"] == ["fam1:keeper"]


def test_revalidation_happens_after_a_lock_wait(env):
    """A wait at the unit gate, then at the database write lock.

    The gate stands for a series-lock / operator-mode wait: the keeper is
    replaced while the run waits, and the family is blocked because
    validation runs after the wait. The second half replaces the renamed
    file while ``BEGIN IMMEDIATE`` holds the write lock: the transaction
    re-verifies it and rolls back.
    """
    spec, keeper, deps, _ = _family(env)
    document = env.plan(families=[spec])

    def gate(unit_id: str) -> None:
        _rewrite(keeper, [P1, P2, P4])

    report = env.apply(document, unit_gate=gate)
    assert unit_status(report, "fam1") == executor.UNIT_BLOCKED
    assert all(path.exists() for path in deps)

    source = book(env.root / "R", "r.cbz", [P1])
    destination = env.root / "R" / "R ch. 1.cbz"
    rename_doc = env.plan(renames=[env.rename_spec(source, destination, "ren1")])

    def swap(point, context):
        if point == "after_db_lock":
            _rewrite(destination, [P2])

    report = env.apply(rename_doc, fault=swap)
    assert report.stopped is not None and "drifted under the write lock" in report.stopped
    location_id = rename_doc["renames"][0]["location_id"]
    assert env.row_path(location_id) == str(source)
    assert [m for m in env.markers() if m[1] == "ren1"] == []


# --- occupancy and ownership ---------------------------------------------------------


def test_rename_refuses_an_occupied_destination_at_execution_time(env):
    source = book(env.series, "a.cbz", [P1])
    destination = env.series / "A ch. 1.cbz"
    document = env.plan(renames=[env.rename_spec(source, destination, "ren1")])

    def occupy(point, context):
        if point == "after_intent":
            destination.write_bytes(b"foreign occupant")

    report = env.apply(document, fault=occupy)

    assert report.stopped is not None and "os.rename" in report.stopped
    assert source.exists() and destination.read_bytes() == b"foreign occupant"
    assert env.step_journal(report.attempt_id) == [("intent", "ren1", 1)]
    recovered = env.recover(document, report.attempt_id)
    assert statuses(recovered) == {"ren1": executor.RES_REFUSED}


def test_an_occupied_destination_blocks_the_unit_before_any_intent(env):
    source = book(env.series, "a.cbz", [P1])
    destination = env.series / "A ch. 1.cbz"
    document = env.plan(renames=[env.rename_spec(source, destination, "ren1")])
    destination.write_bytes(b"already here")

    report = env.apply(document)

    assert unit_status(report, "ren1") == executor.UNIT_BLOCKED
    assert env.step_journal(report.attempt_id) == []


def test_recovery_refuses_a_foreign_occupant_at_the_destination(env):
    source = book(env.series, "a.cbz", [P1])
    destination = env.series / "A ch. 1.cbz"
    document = env.plan(renames=[env.rename_spec(source, destination, "ren1")])
    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at("after_intent"))
    attempt = env.only_attempt()
    book(env.series, destination.name, [P2])  # a foreign archive appears

    report = env.recover(document, attempt)
    assert statuses(report) == {"ren1": executor.RES_REFUSED}
    assert "operator" in report.resolutions[0]["detail"]

    source.unlink()  # and now only the foreign file remains
    report = env.recover(document, attempt)
    assert statuses(report) == {"ren1": executor.RES_REFUSED}
    assert "ambiguous" in report.resolutions[0]["detail"]
    location_id = document["renames"][0]["location_id"]
    assert env.row_path(location_id) == str(source)
    assert env.markers() == []


def test_recovery_finishes_the_db_update_only_for_the_owned_file(env):
    source = book(env.series, "a.cbz", [P1])
    destination = env.series / "A ch. 1.cbz"
    document = env.plan(renames=[env.rename_spec(source, destination, "ren1")])
    location_id = document["renames"][0]["location_id"]
    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at("after_done"))
    attempt = env.only_attempt()
    assert env.row_path(location_id) == str(source)

    report = env.recover(document, attempt)

    assert statuses(report) == {"ren1": executor.RES_COMPLETE}
    assert env.row_path(location_id) == str(destination)
    assert env.markers(attempt) == [(attempt, "ren1", 1, "rename", str(source), str(destination))]
    assert env.step_journal(attempt) == [
        ("intent", "ren1", 1), ("done", "ren1", 1), ("confirmed", "ren1", 1)
    ]


def test_recovery_refuses_an_owned_destination_whose_bytes_changed(env):
    source = book(env.series, "a.cbz", [P1])
    destination = env.series / "A ch. 1.cbz"
    document = env.plan(renames=[env.rename_spec(source, destination, "ren1")])
    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at("after_done"))
    attempt = env.only_attempt()
    _rewrite(destination, [P2])

    report = env.recover(document, attempt)

    assert statuses(report) == {"ren1": executor.RES_REFUSED}
    assert env.row_path(document["renames"][0]["location_id"]) == str(source)
    assert env.markers() == []


def test_foreign_identical_target_without_receipt_is_ambiguous(env):
    """Identical bytes at the destination, and no done receipt: not owned."""
    source = book(env.series, "a.cbz", [P1])
    destination = env.series / "A ch. 1.cbz"
    document = env.plan(renames=[env.rename_spec(source, destination, "ren1")])
    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at("after_intent"))
    attempt = env.only_attempt()
    shutil.copyfile(source, destination)
    source.unlink()
    assert fx.sha(destination) == document["renames"][0]["sha256"]

    report = env.recover(document, attempt)

    assert statuses(report) == {"ren1": executor.RES_REFUSED}
    assert "ambiguous" in report.resolutions[0]["detail"]
    assert env.markers() == []
    with pytest.raises(executor.Refused):
        env.apply(document, resume_attempt=attempt)


def test_crash_after_rename_before_done_receipt_is_ambiguous(env):
    source = book(env.series, "a.cbz", [P1])
    destination = env.series / "A ch. 1.cbz"
    document = env.plan(renames=[env.rename_spec(source, destination, "ren1")])
    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at("after_rename"))
    attempt = env.only_attempt()
    assert destination.exists() and not source.exists()

    report = env.recover(document, attempt)

    assert statuses(report) == {"ren1": executor.RES_REFUSED}
    assert "no done receipt" in report.resolutions[0]["detail"]
    assert env.row_path(document["renames"][0]["location_id"]) == str(source)
    assert env.step_journal(attempt) == [("intent", "ren1", 1)]


def test_a_changed_source_is_refused_before_retry(env):
    source = book(env.series, "a.cbz", [P1])
    destination = env.series / "A ch. 1.cbz"
    document = env.plan(renames=[env.rename_spec(source, destination, "ren1")])
    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at("after_intent"))
    attempt = env.only_attempt()
    assert statuses(env.recover(document, attempt)) == {"ren1": executor.RES_NOT_DONE}
    _rewrite(source, [P2])

    report = env.recover(document, attempt)
    assert statuses(report) == {"ren1": executor.RES_REFUSED}
    assert "source changed" in report.resolutions[0]["detail"]
    with pytest.raises(executor.Refused):
        env.apply(document, resume_attempt=attempt)
    assert source.exists() and not destination.exists()


def test_an_intact_not_done_step_is_retried_by_resume(env):
    """Companion: the intent stands and resume completes the step."""
    source = book(env.series, "a.cbz", [P1])
    destination = env.series / "A ch. 1.cbz"
    document = env.plan(renames=[env.rename_spec(source, destination, "ren1")])
    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at("after_intent"))
    attempt = env.only_attempt()

    report = env.apply(document, resume_attempt=attempt)

    assert unit_status(report, "ren1") == executor.UNIT_COMPLETED
    assert destination.exists() and not source.exists()
    assert env.step_journal(attempt) == [
        ("intent", "ren1", 1), ("done", "ren1", 1), ("confirmed", "ren1", 1)
    ]


def test_crash_after_db_commit_before_confirmation_recovers_from_the_marker(env):
    source = book(env.series, "a.cbz", [P1])
    destination = env.series / "A ch. 1.cbz"
    document = env.plan(renames=[env.rename_spec(source, destination, "ren1")])
    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at("after_db_commit"))
    attempt = env.only_attempt()
    markers_before = env.markers()
    assert len(markers_before) == 1

    report = env.recover(document, attempt, single_round=True)

    assert statuses(report) == {"ren1": executor.RES_CONFIRMED}
    assert env.markers() == markers_before
    assert env.step_journal(attempt)[-1] == ("confirmed", "ren1", 1)


def test_repeated_recovery_is_idempotent(env):
    source = book(env.series, "a.cbz", [P1])
    destination = env.series / "A ch. 1.cbz"
    document = env.plan(renames=[env.rename_spec(source, destination, "ren1")])
    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at("after_done"))
    attempt = env.only_attempt()

    first = env.recover(document, attempt)
    journal = env.journal(attempt)
    markers = env.markers()
    tree = env.tree()
    for _ in range(3):
        again = env.recover(document, attempt)
        assert statuses(again) == statuses(first) == {"ren1": executor.RES_COMPLETE}
        assert env.journal(attempt) == journal
        assert env.markers() == markers
        assert env.tree() == tree


def test_recovery_advances_the_frontier_by_at_most_one_record_per_round(env):
    source = book(env.series, "a.cbz", [P1])
    destination = env.series / "A ch. 1.cbz"
    document = env.plan(renames=[env.rename_spec(source, destination, "ren1")])
    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at("after_done"))
    attempt = env.only_attempt()

    rounds = [statuses(env.recover(document, attempt, single_round=True))["ren1"] for _ in range(3)]

    assert rounds == [executor.RES_DB_COMMITTED, executor.RES_CONFIRMED, executor.RES_COMPLETE]


# --- the database step ---------------------------------------------------------------


def test_db_update_is_conditional_and_stops_unless_one_row(env):
    source = book(env.series, "a.cbz", [P1])
    destination = env.series / "A ch. 1.cbz"
    document = env.plan(renames=[env.rename_spec(source, destination, "ren1")])
    location_id = document["renames"][0]["location_id"]
    elsewhere = str(env.series / "moved-by-someone-else.cbz")

    def drift(point, context):
        if point == "after_done":
            env.execute("UPDATE file_locations SET path = ? WHERE id = ?", (elsewhere, location_id))

    report = env.apply(document, fault=drift)

    assert report.stopped is not None and "exactly one" in report.stopped
    assert env.row_path(location_id) == elsewhere
    assert env.markers() == []
    recovered = env.recover(document, report.attempt_id)
    assert statuses(recovered) == {"ren1": executor.RES_REFUSED}
    assert "not at the preimage" in recovered.resolutions[0]["detail"]

    # The same CAS directly: wrong preimage, or a row that is not current.
    archive_id = document["renames"][0]["archive_id"]
    kwargs = dict(
        attempt_id="x", action_id="a", step=1, kind="rename", archive_id=archive_id,
        location_id=location_id, postimage=str(destination), pairings=(), registry={},
        revalidate=lambda: None,
    )
    with pytest.raises(StepRefused, match="0 rows matched"):
        commit_step(env.database, preimage=str(source), **kwargs)
    env.execute("UPDATE file_locations SET is_current = 0 WHERE id = ?", (location_id,))
    with pytest.raises(StepRefused):
        commit_step(env.database, preimage=elsewhere, **kwargs)
    assert env.markers() == []


def test_location_and_pairing_updates_commit_atomically(env):
    fx.create_pairing_table(env)
    source = book(env.series, "a.cbz", [P1])
    destination = env.series / "A ch. 1.cbz"
    env.execute("INSERT INTO decensor_pairs (id, decensored_path) VALUES (7, ?)", (str(source),))
    registry = {"decensor_pairs": fx.PAIRS}
    document = env.plan(renames=[env.rename_spec(source, destination, "ren1", (PairingRef("decensor_pairs", 7),))])
    location_id = document["renames"][0]["location_id"]

    with pytest.raises(executor.Refused, match="unregistered"):
        env.apply(document)

    def drift(point, context):
        if point == "after_done":
            env.execute("UPDATE decensor_pairs SET decensored_path = 'elsewhere' WHERE id = 7")

    report = env.apply(document, pairing_columns=registry, fault=drift)
    assert report.stopped is not None and "pairing decensor_pairs/7" in report.stopped
    # The location update rolled back with the failed pairing update.
    assert env.row_path(location_id) == str(source)
    assert env.markers() == []

    env.execute("UPDATE decensor_pairs SET decensored_path = ? WHERE id = 7", (str(source),))
    report = env.recover(document, report.attempt_id, pairing_columns=registry)
    assert statuses(report) == {"ren1": executor.RES_COMPLETE}
    assert env.row_path(location_id) == str(destination)
    assert env.query("SELECT decensored_path FROM decensor_pairs WHERE id = 7") == [(str(destination),)]


# --- case-only renames ---------------------------------------------------------------


CRASH_POINTS = ("after_intent", "after_rename", "after_done", "after_db_commit", "after_confirmed")


@pytest.mark.parametrize("step", (1, 2))
@pytest.mark.parametrize("point", CRASH_POINTS)
def test_case_only_rename_crash_at_each_boundary_resolves(env, step, point):
    """Every boundary of both steps resolves: to completion, or to operator.

    Only a crash between ``os.rename`` and the ``done`` receipt is left for
    the operator (ambiguous by design); every other boundary recovers and
    resumes to the new name, with the row and both markers in place.
    """
    spec, source, destination = _case_only(env)
    document = env.plan(renames=[spec])
    location_id = spec.location_id
    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at(point, step=step))
    attempt = env.only_attempt()
    temp = f"{destination}.{attempt}.tmp"

    report = env.recover(document, attempt)
    status = statuses(report)["ren1"]
    if point == "after_rename":
        assert status == executor.RES_REFUSED
        assert env.row_path(location_id) == (str(source) if step == 1 else temp)
        return
    expected = {
        "after_intent": executor.RES_NOT_DONE,
        "after_done": executor.RES_COMPLETE if step == 2 else executor.RES_BOUNDARY,
        "after_db_commit": executor.RES_COMPLETE if step == 2 else executor.RES_BOUNDARY,
        "after_confirmed": executor.RES_COMPLETE if step == 2 else executor.RES_BOUNDARY,
    }[point]
    assert status == expected

    env.apply(document, resume_attempt=attempt)

    assert fx.exact_names(env.series) == ["Series ch. 1.cbz"]
    assert env.row_path(location_id) == str(destination)
    assert [m[2:] for m in env.markers(attempt)] == [
        (1, "temp", str(source), temp),
        (2, "rename", temp, str(destination)),
    ]


def test_case_only_crash_after_second_move_recovers_from_the_frontier(env):
    """Step 1 complete, step 2 done without its marker; temp is long gone."""
    spec, source, destination = _case_only(env)
    document = env.plan(renames=[spec])
    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at("after_done", step=2))
    attempt = env.only_attempt()
    temp = Path(f"{destination}.{attempt}.tmp")
    assert not temp.exists()
    assert env.row_path(spec.location_id) == str(temp)

    report = env.recover(document, attempt, single_round=True)

    assert statuses(report) == {"ren1": executor.RES_DB_COMMITTED}
    assert env.row_path(spec.location_id) == str(destination)
    assert statuses(env.recover(document, attempt)) == {"ren1": executor.RES_COMPLETE}
    assert fx.exact_names(env.series) == ["Series ch. 1.cbz"]


def test_case_only_crash_after_second_db_commit_before_confirmation(env):
    spec, source, destination = _case_only(env)
    document = env.plan(renames=[spec])
    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at("after_db_commit", step=2))
    attempt = env.only_attempt()
    assert env.row_path(spec.location_id) == str(destination)

    report = env.recover(document, attempt, single_round=True)

    assert statuses(report) == {"ren1": executor.RES_CONFIRMED}
    assert env.step_journal(attempt)[-1] == ("confirmed", "ren1", 2)


@pytest.mark.parametrize("point", ("after_done", "after_db_commit"))
def test_repeated_recovery_from_either_case_only_crash_is_idempotent(env, point):
    spec, source, destination = _case_only(env)
    document = env.plan(renames=[spec])
    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at(point, step=2))
    attempt = env.only_attempt()

    env.recover(document, attempt)
    state = (env.journal(attempt), env.markers(), env.tree(), env.row_path(spec.location_id))
    for _ in range(3):
        assert statuses(env.recover(document, attempt)) == {"ren1": executor.RES_COMPLETE}
        assert (env.journal(attempt), env.markers(), env.tree(), env.row_path(spec.location_id)) == state


def test_case_only_temp_step_has_its_own_db_postimage_and_undo(env):
    spec, source, destination = _case_only(env)
    document = env.plan(renames=[spec])
    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at("after_confirmed", step=1))
    attempt = env.only_attempt()
    temp = f"{destination}.{attempt}.tmp"
    # Step 1 is a complete step of its own: row and marker at the temp name.
    assert env.row_path(spec.location_id) == temp
    assert [m[2:] for m in env.markers(attempt)] == [(1, "temp", str(source), temp)]

    env.apply(document, resume_attempt=attempt)
    undone = env.undo(document, attempt)

    assert unit_status(undone, "ren1") == executor.UNIT_COMPLETED
    assert fx.exact_names(env.series) == ["series ch. 1.cbz"]
    assert env.row_path(spec.location_id) == str(source)
    assert [m[2:] for m in env.markers(undone.attempt_id)] == [
        (1, "temp", str(destination), temp),
        (2, "rename", temp, str(source)),
    ]


# --- quarantine moves ---------------------------------------------------------------


@pytest.mark.parametrize("point", CRASH_POINTS)
def test_quarantine_move_crash_at_each_boundary_resolves(env, point):
    spec, keeper, deps, _ = _family(env, dependants=1)
    document = env.plan(families=[spec])
    (dependant,) = deps
    target = Path(env.quarantine_root) / "fam1" / dependant.name
    location_id = spec.dependants[0].member.location_id
    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at(point, action_id="fam1:quarantine:1"))
    attempt = env.only_attempt()

    report = env.recover(document, attempt)
    status = statuses(report)["fam1:quarantine:1"]
    if point == "after_rename":
        assert status == executor.RES_REFUSED
        assert target.exists() and env.row_path(location_id) == str(dependant)
        return
    assert status == (executor.RES_NOT_DONE if point == "after_intent" else executor.RES_COMPLETE)

    env.apply(document, resume_attempt=attempt)

    assert target.exists() and not dependant.exists() and keeper.exists()
    assert fx.sha(target) == spec.dependants[0].member.sha256
    assert env.row_path(location_id) == str(target)
    assert [m[2:] for m in env.markers(attempt)] == [(1, "quarantine", str(dependant), str(target))]


def test_crash_after_the_first_of_several_dependant_quarantines_resumes(env):
    spec, keeper, deps, _ = _family(env, dependants=3)
    document = env.plan(families=[spec])
    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at("after_dependant"))
    attempt = env.only_attempt()
    quarantine = Path(env.quarantine_root) / "fam1"
    assert [p.exists() for p in deps] == [False, True, True]

    report = env.apply(document, resume_attempt=attempt)

    assert unit_status(report, "fam1") == executor.UNIT_COMPLETED
    assert not any(p.exists() for p in deps)
    assert sorted(p.name for p in quarantine.iterdir()) == sorted(p.name for p in deps)
    # The first dependant's move happened once: three receipts, one marker.
    assert [e for e in env.step_journal(attempt) if e[1] == "fam1:quarantine:1"] == [
        ("intent", "fam1:quarantine:1", 1), ("done", "fam1:quarantine:1", 1),
        ("confirmed", "fam1:quarantine:1", 1),
    ]


def test_resume_after_a_dependant_requires_the_keeper_to_revalidate(env):
    spec, keeper, deps, _ = _family(env, dependants=3)
    document = env.plan(families=[spec])
    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at("after_dependant"))
    attempt = env.only_attempt()
    _rewrite(keeper, [P1, P2, P3, P5])

    report = env.apply(document, resume_attempt=attempt)

    assert unit_status(report, "fam1") == executor.UNIT_BLOCKED
    assert [p.exists() for p in deps] == [False, True, True]


def _evaluated_family(env):
    """A plan family built from the real step-5 evaluator, keeper renamed."""
    from comic_automation.archive.page_hashing import ArchivePageHashRepository, calculate_page_hashes
    from comic_automation.archive.supersession import evaluate_folder
    from comic_automation.database.connection import database_connection
    from comic_automation.library.canonical_apply.plan import family_spec_from_evaluation

    v1 = book(env.series, "Series ch. 1 v1.cbz", [P1])
    v2 = book(env.series, "Series ch. 1 v2.cbz", [P1, P2])
    ids, locations = {}, {}
    for path in (v1, v2):
        archive_id, location_id = env.register(path)
        with database_connection(env.database) as connection:
            ArchivePageHashRepository(connection).save(
                archive_id=archive_id, location_id=location_id, result=calculate_page_hashes(path)
            )
        ids[path.name] = archive_id
        locations[str(path)] = (archive_id, location_id)
    folder = evaluate_folder(env.series, database=env.database, archive_ids=ids, series_folder="Series")
    (family,) = [f for f in folder.families if f.status == "supersession"]
    renamed = env.series / "Series ch. 1 [v2].cbz"
    spec = family_spec_from_evaluation(
        family, folder, family_id="fam1", locations=locations,
        quarantine_root=env.quarantine_root, keeper_rename_to=str(renamed),
    )
    return env.plan(families=[spec]), v1, v2, renamed


def test_a_new_family_whose_keeper_needs_renaming_validates_in_phases(env):
    """Built from the real step-5 evaluator: the keeper rename is confirmed
    before the first quarantine intent, and the quarantine follows."""
    document, v1, v2, renamed = _evaluated_family(env)

    report = env.apply(document)

    assert unit_status(report, "fam1") == executor.UNIT_COMPLETED
    order = [(kind, action) for kind, action, _ in env.step_journal(report.attempt_id)]
    assert order == [
        ("intent", "fam1:keeper"), ("done", "fam1:keeper"), ("confirmed", "fam1:keeper"),
        ("intent", "fam1:quarantine:1"), ("done", "fam1:quarantine:1"),
        ("confirmed", "fam1:quarantine:1"),
    ]
    assert renamed.exists() and not v1.exists() and not v2.exists()
    assert (Path(env.quarantine_root) / "fam1" / v1.name).exists()


def test_phase_two_freshly_validates_the_renamed_keeper(env):
    """The keeper changes right after its rename is confirmed: phase 2's
    fresh validation at the resulting path blocks every quarantine."""
    document, v1, v2, renamed = _evaluated_family(env)

    def tamper(point, context):
        if point == "after_confirmed" and context["action_id"] == "fam1:keeper":
            _rewrite(renamed, [P1, P3])

    report = env.apply(document, fault=tamper)

    assert unit_status(report, "fam1") == executor.UNIT_BLOCKED
    assert "renamed keeper" in report.unit("fam1")["reasons"][0]
    assert v1.exists()
    assert [a for k, a, _ in env.step_journal(report.attempt_id) if k == "intent"] == ["fam1:keeper"]


# --- undo -----------------------------------------------------------------------


def test_undo_reverses_confirmed_actions_and_refuses_drifted_files(env):
    spec, keeper, deps, renamed = _family(env, rename=True)
    other = book(env.root / "Other", "o.cbz", [P5])
    other_new = env.root / "Other" / "Other ch. 1.cbz"
    rename = env.rename_spec(other, other_new, "ren1")
    document = env.plan(renames=[rename], families=[spec])
    before = env.tree()
    forward = env.apply(document)
    assert forward.stopped is None

    # A drifted file refuses the whole undo before anything moves.
    original = other_new.read_bytes()
    _rewrite(other_new, [P4])
    after_apply = env.tree()
    with pytest.raises(executor.Refused, match="nothing moved"):
        env.undo(document, forward.attempt_id)
    assert env.tree() == after_apply
    other_new.write_bytes(original)

    undone = env.undo(document, forward.attempt_id)

    assert undone.stopped is None
    assert env.tree() == before
    assert env.row_path(rename.location_id) == str(other)
    assert env.row_path(spec.keeper.location_id) == str(keeper)
    for dep in spec.dependants:
        assert env.row_path(dep.member.location_id) == dep.member.path
    # Reversed latest first: the rename unit ran last, so it is undone first.
    assert [a for k, a, _ in env.step_journal(undone.attempt_id) if k == "intent"] == [
        "ren1", "fam1:quarantine:2", "fam1:quarantine:1", "fam1:keeper",
    ]
    # An undo is never undone, and the forward chain cannot be undone twice.
    with pytest.raises(executor.Refused):
        env.undo(document, undone.attempt_id)
    with pytest.raises(executor.Refused):
        env.undo(document, forward.attempt_id)


def test_undo_requires_a_fully_confirmed_forward_chain(env):
    spec, source, destination = _case_only(env)
    document = env.plan(renames=[spec])
    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at("after_confirmed", step=1))
    attempt = env.only_attempt()

    with pytest.raises(executor.Refused):
        env.undo(document, attempt)
    assert env.attempts() == (attempt,)


def test_interrupted_undo_chain_recovers_from_its_frontier(env):
    spec, source, destination = _case_only(env)
    document = env.plan(renames=[spec])
    forward = env.apply(document)
    with pytest.raises(SimulatedCrash):
        env.undo(document, forward.attempt_id, fault=crash_at("after_done", step=1))
    (undo_attempt,) = [a for a in env.attempts() if a != forward.attempt_id]

    report = env.recover(document, undo_attempt)
    assert statuses(report) == {"ren1": executor.RES_BOUNDARY}
    temp = f"{destination}.{forward.attempt_id}.tmp"
    assert env.row_path(spec.location_id) == temp

    resumed = env.undo(document, forward.attempt_id, resume_attempt=undo_attempt)

    assert unit_status(resumed, "ren1") == executor.UNIT_COMPLETED
    assert fx.exact_names(env.series) == ["series ch. 1.cbz"]
    assert env.row_path(spec.location_id) == str(source)


# --- the chain grammar ---------------------------------------------------------------


def test_a_gap_or_receipt_beyond_the_frontier_refuses(env):
    spec, source, destination = _case_only(env)
    document = env.plan(renames=[spec])
    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at("before_intent"))
    attempt = env.only_attempt()
    plan = env.load(document)
    action = plan.actions["ren1"]
    steps = action.steps(attempt)
    path = jn.journal_path(env.root, attempt)
    header = jn.read_journal(env.root, attempt)[0].data

    def journal_with(entries, **override):
        path.unlink()
        writer = jn.JournalWriter.create(env.root, attempt, {k: header[k] for k in ("mode", "plan_digest", "library_root")})
        for kind, number in entries:
            step = steps[number - 1]
            writer.append(kind, {
                "action_id": "ren1", "steps_total": 2, "archive_id": action.archive_id,
                "location_id": action.location_id, "sha256": action.sha256, **step.as_dict(),
                **override,
            })
        writer.close()

    # Each forged chain is refused for its own reason, not by some later
    # filesystem check that happens to fail too.
    for entries, reason in (
        ([("intent", 1), ("confirmed", 1)], "a gap, reordering"),          # no done
        ([("done", 1)], "a gap, reordering"),                               # done without intent
        ([("intent", 1), ("done", 1), ("confirmed", 1), ("done", 2)], "a gap, reordering"),
        ([("intent", 1), ("intent", 2)], "a gap, reordering"),              # beyond the frontier
        ([("intent", 1), ("done", 1), ("confirmed", 1)], "no database marker"),
    ):
        journal_with(entries)
        report = env.recover(document, attempt)
        assert statuses(report) == {"ren1": executor.RES_REFUSED}, entries
        assert reason in report.resolutions[0]["detail"], (entries, report.resolutions)

    # An intent that does not describe the plan's step.
    journal_with([("intent", 1)], destination=str(env.series / "Somewhere else.cbz"))
    report = env.recover(document, attempt)
    assert statuses(report) == {"ren1": executor.RES_REFUSED}
    assert "does not describe plan step" in report.resolutions[0]["detail"]

    # A marker beyond the frontier.
    journal_with([])
    env.execute(
        "INSERT INTO rename_attempt_steps (attempt_id, action_id, step, kind, location_id, "
        "preimage_path, postimage_path) VALUES (?, 'ren1', 2, 'rename', ?, ?, ?)",
        (attempt, action.location_id, steps[1].source, steps[1].destination),
    )
    report = env.recover(document, attempt)
    assert statuses(report) == {"ren1": executor.RES_REFUSED}
    assert "beyond the frontier" in report.resolutions[0]["detail"]

    # A marker for a step that has only its intent (checked before the
    # step-2 marker above is even considered).
    journal_with([("intent", 1)])
    env.execute(
        "INSERT INTO rename_attempt_steps (attempt_id, action_id, step, kind, location_id, "
        "preimage_path, postimage_path) VALUES (?, 'ren1', 1, 'temp', ?, ?, ?)",
        (attempt, action.location_id, steps[0].source, steps[0].destination),
    )
    report = env.recover(document, attempt)
    assert statuses(report) == {"ren1": executor.RES_REFUSED}
    assert "no done receipt" in report.resolutions[0]["detail"]

    # A torn record and a sequence gap refuse the whole journal.
    path.write_bytes(path.read_bytes() + b'{"torn":')
    with pytest.raises(jn.JournalCorruptError):
        env.recover(document, attempt)
    lines = path.read_bytes().split(b"\n")
    path.write_bytes(lines[0].replace(b'"seq": 0', b'"seq": 1') + b"\n")
    with pytest.raises(jn.JournalCorruptError):
        env.recover(document, attempt)
    assert source.exists()


# --- the scope lock ---------------------------------------------------------------


def test_recovery_takes_the_scope_lock_and_refuses_while_it_is_held(env):
    source = book(env.series, "a.cbz", [P1])
    destination = env.series / "A ch. 1.cbz"
    document = env.plan(renames=[env.rename_spec(source, destination, "ren1")])
    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at("after_done"))
    attempt = env.only_attempt()
    journal = env.journal(attempt)

    with scope_lock.ScopeLock(env.root, "holder", "test"):
        with pytest.raises(scope_lock.ScopeLockHeld):
            env.recover(document, attempt)
        with pytest.raises(scope_lock.ScopeLockHeld):
            env.apply(document, resume_attempt=attempt)
        with pytest.raises(scope_lock.ScopeLockHeld):
            env.undo(document, attempt)
    assert env.journal(attempt) == journal
    assert env.markers() == []

    # Recovery also re-verifies quiescence under the lock before acting.
    with pytest.raises(executor.Refused, match="not quiesced"):
        env.recover(document, attempt, quiescence=lambda root: "watcher running")
    assert env.markers() == []
    assert statuses(env.recover(document, attempt)) == {"ren1": executor.RES_COMPLETE}


_HOLDER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from comic_automation.library.canonical_apply.scope_lock import ScopeLock
    lock = ScopeLock(Path(sys.argv[1]), "child", "test").acquire()
    print("held", flush=True)
    if sys.argv[2] == "wait":
        sys.stdin.readline()
    import os
    os._exit(0)  # no release: the OS must drop the lock
    """
)


def _child(root: Path, mode: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(root), mode],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )


def test_two_recoverers_cannot_both_hold_the_scope_lock(env):
    child = _child(env.root, "wait")
    try:
        assert child.stdout.readline().strip() == "held"
        with pytest.raises(scope_lock.ScopeLockHeld):
            scope_lock.ScopeLock(env.root, "second", "recover").acquire()
    finally:
        child.communicate("\n", timeout=30)
    # In one process, a second handle is refused just the same.
    with scope_lock.ScopeLock(env.root, "first", "recover"):
        with pytest.raises(scope_lock.ScopeLockHeld):
            scope_lock.ScopeLock(env.root, "second", "recover").acquire()


def test_the_lock_is_released_by_process_exit_without_any_unlink(env, monkeypatch):
    lock_file = env.root / scope_lock.LOCK_FILENAME
    child = _child(env.root, "exit")
    assert child.stdout.readline().strip() == "held"
    child.wait(timeout=30)
    identity = os.stat(lock_file).st_ino

    removed = []
    real_unlink, real_remove = os.unlink, os.remove

    def guard(function):
        def wrapper(path, *args, **kwargs):
            if Path(path).name == scope_lock.LOCK_FILENAME:
                removed.append(path)
            return function(path, *args, **kwargs)
        return wrapper

    monkeypatch.setattr(os, "unlink", guard(real_unlink))
    monkeypatch.setattr(os, "remove", guard(real_remove))
    with scope_lock.ScopeLock(env.root, "successor", "recover") as held:
        assert held.held
    assert removed == []
    assert lock_file.exists() and os.stat(lock_file).st_ino == identity
    # Released again by close: a third holder takes it at once.
    scope_lock.ScopeLock(env.root, "third", "recover").acquire().release()


# --- guards proven by targeted mutants -------------------------------------------
#
# Each test below was added because a specific mutant of the executor
# survived the named tests above; the mutant is named in the docstring.


def test_keeper_drift_blocks_a_keeper_rename_before_it_happens(env):
    """Phase 1 checks the keeper before phase 2 renames it (mutant: phase 1
    keeper identity skipped). Without phase 1 the step protocol's own
    pre-intent source check still refuses the rename, so nothing moves
    either way; the reason shows which guard fired, and phase 1 must be it,
    because it runs before any action of the family is attempted."""
    spec, keeper, deps, renamed = _family(env, rename=True)
    document = env.plan(families=[spec])
    _rewrite(keeper, [P1, P2, P4])

    report = env.apply(document)

    assert unit_status(report, "fam1") == executor.UNIT_BLOCKED
    assert report.unit("fam1")["reasons"][0].startswith("keeper drifted")
    assert keeper.exists() and not renamed.exists()
    assert env.step_journal(report.attempt_id) == []


def test_the_keeper_is_revalidated_before_every_quarantine(env):
    """The keeper changes after the first quarantine: the second dependant
    stays (mutant: phase 3 keeper revalidation removed)."""
    spec, keeper, deps, _ = _family(env, dependants=2)
    document = env.plan(families=[spec])

    def tamper(point, context):
        if point == "after_dependant":
            _rewrite(keeper, [P1, P2, P4])

    report = env.apply(document, fault=tamper)

    assert unit_status(report, "fam1") == executor.UNIT_BLOCKED
    assert "keeper drifted" in report.unit("fam1")["reasons"][0]
    assert [p.exists() for p in deps] == [False, True]


def test_an_approved_relation_that_does_not_hold_blocks_the_family(env):
    """Identities match, but the bytes do not bear out the approved
    relation: a wrong index map, or a dependant that is not a prefix
    (mutant: relation never re-derived)."""
    from comic_automation.library.canonical_apply.plan import DependantSpec, FamilySpec

    keeper = book(env.series, "K v2.cbz", [P1, P2, P3])
    prefix = book(env.series, "K v1.cbz", [P1, P2])
    stranger = book(env.root / "Other", "O v1.cbz", [P4])
    okeeper = book(env.root / "Other", "O v2.cbz", [P1, P2])

    def family(family_id, keeper_path, dependant_path, index_map):
        return FamilySpec(
            family_id=family_id,
            keeper=env.member(keeper_path),
            dependants=(DependantSpec(
                member=env.member(dependant_path),
                action_id=f"{family_id}:quarantine:1",
                quarantine_to=str(Path(env.quarantine_root) / family_id / dependant_path.name),
                index_map=index_map,
            ),),
        )

    document = env.plan(families=[
        family("wrongmap", keeper, prefix, (0, 2)),
        family("notprefix", okeeper, stranger, (0,)),
    ])
    report = env.apply(document, max_drift_fraction=1.0)

    for unit_id in ("wrongmap", "notprefix"):
        assert unit_status(report, unit_id) == executor.UNIT_BLOCKED
        assert "approved relation" in report.unit(unit_id)["reasons"][0]
    assert prefix.exists() and stranger.exists()


def test_a_keeper_row_that_moved_blocks_its_family(env):
    """The keeper's file_locations row no longer names it (mutant: phase 1
    keeper row check removed -- nothing else looks at an unrenamed keeper's
    row)."""
    spec, keeper, deps, _ = _family(env)
    document = env.plan(families=[spec])
    env.execute("UPDATE file_locations SET path = 'elsewhere' WHERE id = ?", (spec.keeper.location_id,))

    report = env.apply(document)

    assert unit_status(report, "fam1") == executor.UNIT_BLOCKED
    assert all(p.exists() for p in deps)


def test_undo_refuses_an_action_that_never_ran(env):
    """A forward unit that was blocked has no chain to reverse (mutant:
    complete-chain precondition removed)."""
    source = book(env.series, "a.cbz", [P1])
    destination = env.series / "A ch. 1.cbz"
    document = env.plan(renames=[env.rename_spec(source, destination, "ren1")])
    destination.write_bytes(b"occupied")
    forward = env.apply(document)
    assert unit_status(forward, "ren1") == executor.UNIT_BLOCKED

    with pytest.raises(executor.Refused, match="forward chain is not complete"):
        env.undo(document, forward.attempt_id, action_ids=["ren1"])
    assert env.attempts() == (forward.attempt_id,)


def test_confirmed_is_not_written_when_the_postimage_rows_moved(env):
    """Marker present but the row has since moved (mutant: confirmed written
    without checking the postimage rows)."""
    source = book(env.series, "a.cbz", [P1])
    destination = env.series / "A ch. 1.cbz"
    document = env.plan(renames=[env.rename_spec(source, destination, "ren1")])
    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at("after_db_commit"))
    attempt = env.only_attempt()
    env.execute("UPDATE file_locations SET path = 'elsewhere' WHERE id = ?", (document["renames"][0]["location_id"],))

    report = env.recover(document, attempt)

    assert statuses(report) == {"ren1": executor.RES_REFUSED}
    assert env.step_journal(attempt)[-1] == ("done", "ren1", 1)


def test_a_case_only_name_restored_outside_the_protocol_is_refused(env):
    """After a confirmed case-only rename the file is put back in its old
    letter case by hand. ``Path.exists`` would still find it; the exact-name
    check does not (mutant: exact-name check made case-insensitive)."""
    spec, source, destination = _case_only(env)
    document = env.plan(renames=[spec])
    forward = env.apply(document)
    os.rename(destination, env.series / "tmp-name.cbz")
    os.rename(env.series / "tmp-name.cbz", source)

    report = env.recover(document, forward.attempt_id)
    assert statuses(report) == {"ren1": executor.RES_REFUSED}
    with pytest.raises(executor.Refused):
        env.undo(document, forward.attempt_id)
    assert fx.exact_names(env.series) == ["series ch. 1.cbz"]


def test_an_owned_destination_is_refused_when_its_source_reappears(env):
    """Done receipt, but something now sits at the source again (mutant:
    source reoccupation ignored)."""
    source = book(env.series, "a.cbz", [P1])
    destination = env.series / "A ch. 1.cbz"
    document = env.plan(renames=[env.rename_spec(source, destination, "ren1")])
    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at("after_done"))
    attempt = env.only_attempt()
    source.write_bytes(b"newcomer")

    report = env.recover(document, attempt)

    assert statuses(report) == {"ren1": executor.RES_REFUSED}
    assert "occupied again" in report.resolutions[0]["detail"]
    assert env.markers() == []


def test_a_source_changed_after_the_intent_is_never_moved(env):
    """The source is replaced between the intent and the rename (mutant:
    no re-verification after the intent -- the changed bytes would be moved
    and only caught later, under the write lock)."""
    source = book(env.series, "a.cbz", [P1])
    destination = env.series / "A ch. 1.cbz"
    document = env.plan(renames=[env.rename_spec(source, destination, "ren1")])

    def swap(point, context):
        if point == "after_intent":
            _rewrite(source, [P2])

    report = env.apply(document, fault=swap)

    assert "source changed after intent" in report.stopped
    assert source.exists() and not destination.exists()
    assert env.step_journal(report.attempt_id) == [("intent", "ren1", 1)]


# --- guards proven by the lead's independent mutants (PR #128 review) -----------


def test_phase_two_compares_the_renamed_keepers_re_derived_sequence(env, monkeypatch):
    """Phase 2 re-derives the keeper's sequence at its resulting path.

    Equal sha256 means equal pages for a deterministic derivation, so this
    guard is reachable only through an injected derivation: here the
    renamed keeper's derivation loses its last page while its sha256 is
    unchanged. Without the guard, phase 3's relation check would still block
    the family, so the reason shows which guard fired (mutant:
    phase2_sequence_not_compared)."""
    from dataclasses import replace

    spec, keeper, deps, renamed = _family(env, rename=True)
    document = env.plan(families=[spec])
    real = executor.derive_archive

    def derive(path, *args, **kwargs):
        derived = real(path, *args, **kwargs)
        if Path(path) == renamed:
            return replace(derived, pages=derived.pages[:-1])
        return derived

    monkeypatch.setattr(executor, "derive_archive", derive)
    report = env.apply(document)

    assert unit_status(report, "fam1") == executor.UNIT_BLOCKED
    assert report.unit("fam1")["reasons"] == ["renamed keeper's re-derived sequence differs"]
    assert all(p.exists() for p in deps)


def test_a_dependant_row_that_moved_blocks_the_family_before_anything_moves(env):
    """Phase 1 checks every dependant's row (mutant:
    phase1_dependant_rows_skipped -- the quarantine CAS would then refuse,
    but only after the file had already been moved)."""
    spec, keeper, deps, _ = _family(env, dependants=1)
    document = env.plan(families=[spec])
    env.execute(
        "UPDATE file_locations SET path = 'elsewhere' WHERE id = ?",
        (spec.dependants[0].member.location_id,),
    )

    report = env.apply(document)

    assert unit_status(report, "fam1") == executor.UNIT_BLOCKED
    assert "dependant fam1:quarantine:1: file_locations row" in report.unit("fam1")["reasons"][0]
    assert report.stopped is None
    assert deps[0].exists() and env.step_journal(report.attempt_id) == []


def test_the_keeper_row_is_revalidated_before_every_quarantine(env):
    """§5.3 phase 3: the keeper is re-validated at its current path, rows
    included. Its row moves after the first quarantine; the second
    dependant stays."""
    spec, keeper, deps, _ = _family(env, dependants=2)
    document = env.plan(families=[spec])

    def move_row(point, context):
        if point == "after_dependant":
            env.execute(
                "UPDATE file_locations SET path = 'elsewhere' WHERE id = ?",
                (spec.keeper.location_id,),
            )

    report = env.apply(document, fault=move_row)

    assert unit_status(report, "fam1") == executor.UNIT_BLOCKED
    assert report.unit("fam1")["reasons"][0].startswith("keeper: file_locations row")
    assert [p.exists() for p in deps] == [False, True]


def test_recovery_refuses_a_complete_action_whose_rows_moved(env):
    """Bytes still at the proven location, row moved since (mutant:
    recover_complete_rows_unchecked)."""
    source = book(env.series, "a.cbz", [P1])
    destination = env.series / "A ch. 1.cbz"
    document = env.plan(renames=[env.rename_spec(source, destination, "ren1")])
    forward = env.apply(document)
    env.execute("UPDATE file_locations SET path = 'elsewhere' WHERE id = ?", (document["renames"][0]["location_id"],))

    report = env.recover(document, forward.attempt_id)

    assert statuses(report) == {"ren1": executor.RES_REFUSED}
    assert "database rows are not at the proven location" in report.resolutions[0]["detail"]


def test_recovery_refuses_an_intent_only_step_whose_rows_moved(env):
    """Intent only, source intact, but the row is no longer at the step's
    preimage: not retryable (mutant: intent_only_rows_unchecked)."""
    source = book(env.series, "a.cbz", [P1])
    destination = env.series / "A ch. 1.cbz"
    document = env.plan(renames=[env.rename_spec(source, destination, "ren1")])
    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at("after_intent"))
    attempt = env.only_attempt()
    env.execute("UPDATE file_locations SET path = 'elsewhere' WHERE id = ?", (document["renames"][0]["location_id"],))

    report = env.recover(document, attempt)

    assert statuses(report) == {"ren1": executor.RES_REFUSED}
    assert "not at the step's preimage" in report.resolutions[0]["detail"]
    with pytest.raises(executor.Refused):
        env.apply(document, resume_attempt=attempt)
    assert source.exists() and not destination.exists()


def test_a_held_scope_lock_is_refused_at_once_never_waited_for(env):
    """The second acquirer refuses promptly with the non-blocking refusal
    (mutant: blocking_lock -- LK_LOCK retries for about ten seconds before
    it raises, so only the timing tells it apart)."""
    import time

    with scope_lock.ScopeLock(env.root, "first", "apply"):
        started = time.monotonic()
        with pytest.raises(scope_lock.ScopeLockHeld, match="refusing rather than waiting"):
            scope_lock.ScopeLock(env.root, "second", "recover").acquire()
        assert time.monotonic() - started < 2.0
