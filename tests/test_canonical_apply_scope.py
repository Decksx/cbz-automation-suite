"""The execution context of a step-7 run is bound to the approved plan.

PR #128 review (codex-lead) found four ways the run could act outside what
the operator approved; each is pinned here through the public entry points
on tmp fixtures:

1. **Root binding.** `apply`, `recover` and `undo` act on the plan's root
   only: a different ``library_root`` argument is refused before any lock,
   owner file or journal is touched under either root, and a journal
   written for another root is refused.
2. **Physical containment.** A junction anywhere between the root and a
   source, destination, quarantine directory or piece of run state is
   refused before anything moves, and again immediately before each
   ``os.rename``. Real Windows junctions are used; the UNC case is unit
   tested on the pure helper with injected filesystem calls.
3. **Database binding.** The plan carries the #117 file identity of its
   database; a SQLite backup clone, a restored file, or a database swapped
   in mid-run is refused, and so is a journal written for another database.
4. **Lock release on failure.** A failure writing the owner file releases
   the OS lock before the error escapes.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from comic_automation.library.canonical_apply import cli, containment, executor
from comic_automation.library.canonical_apply import journal as jn
from comic_automation.library.canonical_apply import scope_lock
from tests import canonical_apply_fixtures as fx
from tests.canonical_apply_fixtures import P1, P2, SimulatedCrash, book, crash_at, env  # noqa: F401

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="the step protocol is Windows-only (os.rename semantics)"
)

STATE = (scope_lock.LOCK_FILENAME, scope_lock.OWNER_FILENAME, jn.JOURNAL_DIRNAME)


def _state(root: Path) -> dict[str, bytes | None]:
    """Every canonical-naming state file under ``root``, with its bytes."""
    found: dict[str, bytes | None] = {}
    for name in STATE:
        path = root / name
        if path.is_file():
            # The lock file's byte 0 is locked while held; its existence is
            # what matters, so only the other files are read.
            found[name] = None if name == scope_lock.LOCK_FILENAME else path.read_bytes()
        elif path.is_dir():
            for child in sorted(path.rglob("*")):
                found[str(child.relative_to(root))] = child.read_bytes() if child.is_file() else None
    return found


def _rename(env, name="a.cbz", new="A ch. 1.cbz", folder=None):
    folder = folder or env.series
    source = book(folder, name, [P1])
    destination = folder / new
    return env.plan(renames=[env.rename_spec(source, destination, "ren1")]), source, destination


def _junction(link: Path, target: Path) -> None:
    import _winapi

    target.mkdir(parents=True, exist_ok=True)
    _winapi.CreateJunction(str(target), str(link))
    assert containment.is_reparse(os.lstat(link))


# --- 1. root binding ------------------------------------------------------------------


def test_apply_with_another_root_is_refused_before_any_scope_state(env, tmp_path):
    document, source, destination = _rename(env)
    plan = env.load(document)
    other = tmp_path / "unrelated_library"
    other.mkdir()

    with scope_lock.ScopeLock(env.root, "actual_holder", "test"):
        with pytest.raises(executor.Refused, match="not the plan's library root"):
            executor.apply(plan, library_root=other, database=env.database, quiescence=fx.quiet)
        holder_state = _state(env.root)

    assert source.exists() and not destination.exists()
    assert list(other.iterdir()) == []
    # Under the plan's root only the test's own lock and owner file exist.
    assert set(holder_state) == {scope_lock.LOCK_FILENAME, scope_lock.OWNER_FILENAME}
    assert env.attempts() == () and env.markers() == []


def test_recover_and_undo_with_another_root_are_refused(env, tmp_path):
    document, source, destination = _rename(env)
    plan = env.load(document)
    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at("after_done"))
    attempt = env.only_attempt()
    other = tmp_path / "unrelated_library"
    other.mkdir()
    before = (_state(env.root), env.markers(), env.tree())

    with pytest.raises(executor.Refused, match="not the plan's library root"):
        executor.recover(plan, library_root=other, database=env.database, attempt_id=attempt, quiescence=fx.quiet)
    with pytest.raises(executor.Refused, match="not the plan's library root"):
        executor.undo(plan, library_root=other, database=env.database, forward_attempt=attempt, quiescence=fx.quiet)

    assert (_state(env.root), env.markers(), env.tree()) == before
    assert list(other.iterdir()) == []
    # The same root spelt differently (letter case) is the same root, and
    # the run then uses the plan's own spelling for its state.
    report = executor.recover(
        plan, library_root=Path(str(env.root).upper()), database=env.database,
        attempt_id=attempt, quiescence=fx.quiet,
    )
    assert fx.statuses(report) == {"ren1": executor.RES_COMPLETE}
    assert report.journal == str(jn.journal_path(Path(plan.library_root), attempt))


def test_a_differently_spelt_root_runs_under_the_plans_own_root(env):
    document, source, destination = _rename(env)
    plan = env.load(document)

    report = executor.apply(
        plan, library_root=Path(str(env.root).upper()), database=env.database, quiescence=fx.quiet,
    )

    assert report.stopped is None and destination.exists()
    header = jn.read_journal(env.root, report.attempt_id)[0].data
    assert header["library_root"] == plan.library_root
    assert report.journal == str(jn.journal_path(Path(plan.library_root), report.attempt_id))


def test_a_journal_written_for_another_root_or_database_is_refused(env):
    import json

    document, source, destination = _rename(env)
    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at("after_done"))
    attempt = env.only_attempt()
    path = jn.journal_path(env.root, attempt)
    original = path.read_bytes()
    lines = original.split(b"\n")
    for key, value in (("library_root", "C:\\elsewhere"), ("database_target", {"path": "x", "st_dev": 1, "st_ino": 2})):
        header = json.loads(lines[0])
        header[key] = value
        path.write_bytes(b"\n".join([json.dumps(header, sort_keys=True).encode()] + lines[1:]))
        with pytest.raises(executor.Refused, match="was journalled for"):
            env.recover(document, attempt)
        with pytest.raises(executor.Refused, match="was journalled for"):
            env.apply(document, resume_attempt=attempt)
    path.write_bytes(original)
    assert env.markers() == []
    assert fx.statuses(env.recover(document, attempt)) == {"ren1": executor.RES_COMPLETE}


# --- 2. physical containment ----------------------------------------------------------


def test_a_junctioned_quarantine_root_is_refused_through_the_cli(env, tmp_path, capsys):
    outside = tmp_path / "outside_approved_library"
    _junction(env.root / ".cbz-canonical-quarantine", outside)
    keeper = book(env.series, "Keep.cbz", [P1, P2])
    older = book(env.series, "Old.cbz", [P1])
    document = env.plan(families=[env.family_spec("fam1", keeper, [(older, 1)])])
    path = env.write_plan(document)

    code = cli.main([
        "apply", "--library-root", str(env.root), "--database", str(env.database),
        "--plan", str(path), "--expect-digest", document["plan_digest"],
        "--expect-renames", "0", "--expect-families", "1", "--attest-quiescent",
    ])

    assert code == 1
    assert "reparse point" in capsys.readouterr().err
    assert older.exists() and keeper.exists()
    assert list(outside.iterdir()) == []
    assert env.attempts() == ()


def test_a_junctioned_series_directory_is_refused_and_an_ordinary_one_is_not(env, tmp_path):
    outside = tmp_path / "outside_series"
    book(outside, "a.cbz", [P1])
    linked = env.root / "Linked"
    _junction(linked, outside)
    source = linked / "a.cbz"
    document = env.plan(renames=[env.rename_spec(source, linked / "A ch. 1.cbz", "ren1")])

    with pytest.raises(executor.Refused, match="reparse point"):
        env.apply(document)
    assert sorted(p.name for p in outside.iterdir()) == ["a.cbz"]
    assert env.attempts() == ()

    # The control: the same shapes, no junction, complete.
    plain = book(env.root / "Plain", "b.cbz", [P2])
    keeper = book(env.series, "Keep.cbz", [P1, P2])
    older = book(env.series, "Old.cbz", [P1])
    control = env.plan(
        renames=[env.rename_spec(plain, env.root / "Plain" / "B ch. 1.cbz", "ren2")],
        families=[env.family_spec("fam1", keeper, [(older, 1)])],
    )
    report = env.apply(control)
    assert report.stopped is None
    assert [u["status"] for u in report.units] == [executor.UNIT_COMPLETED, executor.UNIT_COMPLETED]
    assert not older.exists() and not plain.exists()


def test_a_junctioned_state_directory_is_refused_before_the_lock(env, tmp_path):
    outside = tmp_path / "outside_state"
    _junction(env.root / jn.JOURNAL_DIRNAME, outside)
    document, source, destination = _rename(env)

    with pytest.raises(executor.Refused, match="canonical-naming state"):
        env.apply(document)

    assert source.exists() and list(outside.iterdir()) == []
    assert not (env.root / scope_lock.LOCK_FILENAME).exists()


def test_a_junction_created_during_a_wait_blocks_the_quarantine(env, tmp_path):
    """The quarantine directory is checked again right before it is made."""
    outside = tmp_path / "outside_late"
    keeper = book(env.series, "Keep.cbz", [P1, P2])
    older = book(env.series, "Old.cbz", [P1])
    document = env.plan(families=[env.family_spec("fam1", keeper, [(older, 1)])])

    def gate(unit_id):
        _junction(env.root / ".cbz-canonical-quarantine", outside)

    report = env.apply(document, unit_gate=gate)

    assert fx.unit_status(report, "fam1") == executor.UNIT_BLOCKED
    assert "quarantine directory" in report.unit("fam1")["reasons"][0]
    assert older.exists() and list(outside.iterdir()) == []


def test_containment_is_rechecked_immediately_before_each_rename(env, tmp_path):
    """After the intent, the series directory is swapped for a junction to
    an outside copy: the identity still matches, so only the pre-rename
    containment check stands between it and a move outside the library."""
    document, source, destination = _rename(env)
    outside = tmp_path / "outside_swap"

    def swap(point, context):
        if point == "after_intent":
            shutil.move(str(env.series), str(outside))
            _junction(env.series, outside)

    report = env.apply(document, fault=swap)

    assert report.stopped is not None and "reparse point" in report.stopped
    assert sorted(p.name for p in outside.iterdir()) == ["a.cbz"]
    assert env.step_journal(report.attempt_id) == [("intent", "ren1", 1)]


def _fake_fs(reparse=(), resolve=None):
    """Injected lstat/realpath over UNC-shaped strings; no share is touched."""
    reparse_keys = {containment._key(p) for p in reparse}

    def lstat(path):
        attributes = containment.FILE_ATTRIBUTE_REPARSE_POINT if containment._key(path) in reparse_keys else 0
        return SimpleNamespace(st_mode=0o040777, st_file_attributes=attributes)

    def realpath(path):
        return (resolve or {}).get(path, path)

    return lstat, realpath


UNC_ROOT = r"\\TOWER\media\Comics"


def test_containment_handles_unc_roots():
    lstat, realpath = _fake_fs()
    ok = r"\\TOWER\media\Comics\Series\a.cbz"
    containment.require_contained(UNC_ROOT, ok, lstat=lstat, realpath=realpath)
    containment.require_contained(UNC_ROOT, r"\\tower\MEDIA\comics\Series\a.cbz", lstat=lstat, realpath=realpath)
    assert containment.relative_parts(UNC_ROOT, ok) == ("Series", "a.cbz")
    for outside in (
        r"\\TOWER\other\Comics\a.cbz",             # another share
        r"\\NAS\media\Comics\a.cbz",               # another server
        r"\\TOWER\media\Comics\..\Elsewhere\a.cbz",  # dot-dot out
        r"\\TOWER\media\ComicsX\a.cbz",            # a sibling with a shared prefix
        UNC_ROOT,                                  # the root itself
        r"C:\Comics\a.cbz",                        # another drive
    ):
        with pytest.raises(containment.ContainmentError):
            containment.require_contained(UNC_ROOT, outside, lstat=lstat, realpath=realpath)
    lstat, realpath = _fake_fs(reparse=[r"\\TOWER\media\Comics\Series"])
    with pytest.raises(containment.ContainmentError, match="reparse point"):
        containment.require_contained(UNC_ROOT, ok, lstat=lstat, realpath=realpath)
    lstat, realpath = _fake_fs(resolve={ok: r"\\TOWER\media\Elsewhere\a.cbz"})
    with pytest.raises(containment.ContainmentError, match="resolves outside"):
        containment.require_contained(UNC_ROOT, ok, lstat=lstat, realpath=realpath)


def test_containment_stops_the_walk_at_the_first_missing_component():
    def lstat(path):
        if containment._key(path).endswith("\\new"):
            raise FileNotFoundError(path)
        return SimpleNamespace(st_mode=0o040777, st_file_attributes=0)

    containment.require_contained(UNC_ROOT, UNC_ROOT + r"\new\deeper\a.cbz", lstat=lstat, realpath=lambda p: p)


# --- 3. database binding -------------------------------------------------------------


def _backup_clone(source: Path, target: Path) -> None:
    """A SQLite backup-API clone; both connections closed afterwards."""
    original, backup = sqlite3.connect(str(source)), sqlite3.connect(str(target))
    try:
        original.backup(backup)
    finally:
        backup.close()
        original.close()


def test_a_backup_clone_of_the_database_is_refused_with_nothing_moved(env, capsys):
    document, source, destination = _rename(env)
    path = env.write_plan(document)
    clone = env.tmp / "backup_copy.db"
    _backup_clone(env.database, clone)

    code = cli.main([
        "apply", "--library-root", str(env.root), "--database", str(clone),
        "--plan", str(path), "--expect-digest", document["plan_digest"],
        "--expect-renames", "1", "--expect-families", "0", "--attest-quiescent",
    ])

    assert code == 1 and "bound to" in capsys.readouterr().err
    assert source.exists() and not destination.exists()
    copy = sqlite3.connect(str(clone))
    try:
        assert copy.execute("SELECT path FROM file_locations").fetchall() == [(str(source),)]
    finally:
        copy.close()
    assert env.row_path(document["renames"][0]["location_id"]) == str(source)
    assert env.attempts() == ()
    # Refused before the scope lock was even taken.
    assert not (env.root / scope_lock.LOCK_FILENAME).exists()

    # The control: the bound database applies, crashes and recovers.
    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at("after_done"))
    assert fx.statuses(env.recover(document, env.only_attempt())) == {"ren1": executor.RES_COMPLETE}
    assert env.row_path(document["renames"][0]["location_id"]) == str(destination)


def test_a_restored_database_needs_a_new_plan(env):
    """Restoring a copy over the same path changes the file identity."""
    document, source, destination = _rename(env)
    copy = env.tmp / "copy.db"
    shutil.copyfile(env.database, copy)
    os.replace(copy, env.database)

    with pytest.raises(executor.Refused, match="bound to"):
        env.apply(document)
    assert source.exists() and env.attempts() == ()


def test_a_database_swapped_in_mid_run_is_refused_under_the_write_lock(env):
    document, source, destination = _rename(env)
    clone = env.tmp / "clone.db"
    _backup_clone(env.database, clone)

    def swap(point, context):
        if point == "after_done":
            os.replace(clone, env.database)

    report = env.apply(document, fault=swap)

    assert report.stopped is not None and "bound to" in report.stopped
    now = sqlite3.connect(str(env.database))
    try:
        assert now.execute("SELECT COUNT(*) FROM rename_attempt_steps").fetchone() == (0,)
    finally:
        now.close()
    with pytest.raises(executor.Refused, match="bound to"):
        env.recover(document, report.attempt_id)


def test_a_database_swapped_while_waiting_is_refused_when_the_view_reopens(env):
    """The database is replaced after the entry check, while the run waits
    for quiescence under the lock: the first read connection re-verifies
    the binding and refuses, instead of reading the substitute."""
    document, source, destination = _rename(env)
    with pytest.raises(SimulatedCrash):
        env.apply(document, fault=crash_at("after_done"))
    attempt = env.only_attempt()
    clone = env.tmp / "clone.db"
    _backup_clone(env.database, clone)

    def swap_then_quiet(root):
        if clone.exists():
            os.replace(clone, env.database)
        return None

    with pytest.raises(executor.Refused, match="bound to"):
        env.recover(document, attempt, quiescence=swap_then_quiet)


# --- 4. the lock is released when owner metadata fails -------------------------------


def test_an_owner_file_failure_releases_the_lock(env):
    owner = env.root / scope_lock.OWNER_FILENAME
    owner.mkdir()
    first = scope_lock.ScopeLock(env.root, "first", "apply")
    with pytest.raises(OSError):
        first.acquire()
    assert not first.held
    owner.rmdir()

    second = scope_lock.ScopeLock(env.root, "second", "apply")
    second.acquire()
    try:
        assert second.held
    finally:
        second.release()
