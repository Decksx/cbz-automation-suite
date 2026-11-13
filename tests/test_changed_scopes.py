"""Changed-series selection from discovery batch bounds (issue #158, B2 part B).

`changed_scopes` turns the catalog events inside a completed batch's
persisted bounds into the series folders a scoped plan covers. These tests
pin: selection by event id (not clock), the two modes, every refusal of a
batch that cannot bound its own coverage, the WAL-aware read, the grouping of
what was not planned, and that the planner seals the selection into its digest.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.library import changed_scopes as cs
from comic_automation.library import canonical_plan as cp
from comic_automation.library import scan_library
from scripts.cbz_canonical_plan import main
from test_canonical_plan import _junction, _two_series, env  # noqa: F401  (env is a fixture)

MIGRATIONS = Path(__file__).resolve().parents[1] / "comic_automation" / "database" / "migrations"
COMPLETE = dict(errors=0, resumed=False, limited=False)


def record(db: Path, root: Path, paths, *, status="completed", event="discovered", **details) -> int:
    """Append ``paths`` as events and one batch bounding exactly them."""
    with database_connection(db) as connection:
        start = connection.execute("SELECT COALESCE(MAX(id), 0) FROM file_events").fetchone()[0]
        for path in paths:
            connection.execute("INSERT INTO file_events(event_type, source_path) VALUES (?, ?)", (event, str(path)))
        end = connection.execute("SELECT COALESCE(MAX(id), 0) FROM file_events").fetchone()[0]
        body = {**COMPLETE, "start_event_id": start, "end_event_id": end, **details}
        body = {k: v for k, v in body.items() if v is not None}
        return connection.execute("INSERT INTO source_batches(source_path, status, details_json) VALUES (?, ?, ?)",
                                  (str(root), status, json.dumps(body))).lastrowid


@pytest.fixture
def lib(tmp_path):
    root, db = tmp_path / "library", tmp_path / "catalog.db"
    for name in ("Alpha", "Beta", "Gamma"):
        (root / name).mkdir(parents=True)
        (root / name / f"{name} 01.cbz").write_bytes(b"x")
    with database_connection(db) as connection:
        apply_migrations(connection, MIGRATIONS)
    return root, db


def test_real_discovery_batches_select_only_their_own_folders(tmp_path):
    """End to end over scan_library: the second batch's bounds hold only what it recorded."""
    root, db = tmp_path / "library", tmp_path / "catalog.db"
    (root / "Alpha").mkdir(parents=True)
    (root / "Alpha" / "a.cbz").write_bytes(b"a")
    with database_connection(db) as connection:
        apply_migrations(connection, MIGRATIONS)
        first = scan_library(connection, root).batch_id
        (root / "Beta").mkdir()
        (root / "Beta" / "b.cbz").write_bytes(b"b")
        second = scan_library(connection, root).batch_id
    assert cs.changed_scopes(db, root, batch_id=first, since=False).planned == ["Alpha"]
    assert cs.changed_scopes(db, root, batch_id=second, since=False).planned == ["Beta"]
    assert cs.changed_scopes(db, root, batch_id=first, since=True).planned == ["Alpha", "Beta"]


def test_batch_mode_stops_at_the_end_marker_and_since_mode_reaches_the_newest_event(lib):
    root, db = lib
    batch = record(db, root, [root / "Alpha" / "Alpha 01.cbz"])
    record(db, root, [root / "Beta" / "Beta 01.cbz"])
    only = cs.changed_scopes(db, root, batch_id=batch, since=False)
    since = cs.changed_scopes(db, root, batch_id=batch, since=True)
    assert only.planned == ["Alpha"] and only.upper_event_id == 1 and only.events == 1
    assert since.planned == ["Alpha", "Beta"] and since.upper_event_id == 2 and since.mode == "since"


def test_selection_is_by_event_id_not_clock(lib):
    """A clock rollback stamps the batch's event years early; ids still bound it,
    and a same-second earlier event stays outside."""
    root, db = lib
    record(db, root, [root / "Gamma" / "Gamma 01.cbz"])
    batch = record(db, root, [root / "Alpha" / "Alpha 01.cbz"])
    with database_connection(db) as connection:
        connection.execute("UPDATE file_events SET occurred_at = '2000-01-01 00:00:00' WHERE id = 2")
        connection.execute("UPDATE file_events SET occurred_at = '2099-01-01 00:00:00' WHERE id = 1")
        connection.execute("UPDATE source_batches SET discovered_at = '2099-01-01 00:00:00'")
    assert cs.changed_scopes(db, root, batch_id=batch, since=False).planned == ["Alpha"]


def test_only_catalog_change_events_select_a_folder(lib):
    root, db = lib
    with database_connection(db) as connection:
        connection.execute("INSERT INTO file_events(event_type, source_path) VALUES ('hashed', ?)",
                           (str(root / "Gamma" / "Gamma 01.cbz"),))
    batch = record(db, root, [root / "Alpha" / "Alpha 01.cbz"], start_event_id=0)
    assert cs.changed_scopes(db, root, batch_id=batch, since=False).planned == ["Alpha"]


@pytest.mark.parametrize("change, message", [
    (dict(start_event_id=None), "no valid start_event_id"),
    (dict(end_event_id=None), "no valid end_event_id"),
    (dict(end_event_id="2"), "no valid end_event_id"),
    (dict(start_event_id=-1), "no valid start_event_id"),
    (dict(start_event_id=True), "no valid start_event_id"),
    (dict(start_event_id=1, end_event_id=0), "out of order"),
    (dict(end_event_id=99), "beyond the event log"),
    (dict(errors=2), "scan error"),
    (dict(resumed=True), "resumed"),
    (dict(limited=True), "limited"),
    (dict(errors=None), "coverage metadata"),
    (dict(resumed="no"), "coverage metadata"),
    (dict(limited=None), "coverage metadata"),
])
def test_a_batch_that_cannot_bound_its_coverage_is_refused(lib, change, message):
    root, db = lib
    batch = record(db, root, [root / "Alpha" / "Alpha 01.cbz"], **change)
    with pytest.raises(cs.ChangedScopesRefused, match=message):
        cs.changed_scopes(db, root, batch_id=batch, since=False)


@pytest.mark.parametrize("status", ["running", "failed", "partial"])
def test_only_a_completed_batch_is_accepted(lib, status):
    root, db = lib
    batch = record(db, root, [root / "Alpha" / "Alpha 01.cbz"], status=status)
    with pytest.raises(cs.ChangedScopesRefused, match="not completed"):
        cs.changed_scopes(db, root, batch_id=batch, since=True)


def test_unreadable_details_missing_batches_bad_ids_and_other_roots_are_refused(lib, tmp_path):
    root, db = lib
    batch = record(db, root, [root / "Alpha" / "Alpha 01.cbz"])
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(cs.ChangedScopesRefused, match="not"):
        cs.changed_scopes(db, other, batch_id=batch, since=False)
    for bad in (0, -1, True, "1"):
        with pytest.raises(cs.ChangedScopesRefused, match="positive integer"):
            cs.changed_scopes(db, root, batch_id=bad, since=False)
    with pytest.raises(cs.ChangedScopesRefused, match="no discovery batch"):
        cs.changed_scopes(db, root, batch_id=batch + 1, since=False)
    for body in ("not json", "[1]", None):
        with database_connection(db) as connection:
            connection.execute("UPDATE source_batches SET details_json = ? WHERE id = ?", (body, batch))
        with pytest.raises(cs.ChangedScopesRefused, match="unreadable"):
            cs.changed_scopes(db, root, batch_id=batch, since=False)


def test_the_root_is_matched_case_insensitively_like_windows(lib):
    root, db = lib
    batch = record(db, Path(str(root).upper()), [Path(str(root / "Alpha" / "Alpha 01.cbz").upper())])
    assert cs.changed_scopes(db, root, batch_id=batch, since=False).planned == ["Alpha"]


def test_everything_not_planned_is_grouped_and_visible(lib, tmp_path):
    root, db = lib
    (root / ".stversions").mkdir()
    (root / "Flat").write_bytes(b"not a folder")
    (root / "Beta" / "Beta 01.cbz").unlink()
    (root / "Beta").rmdir()
    batch = record(db, root, [
        root / "Alpha" / "Alpha 01.cbz",
        root / "Beta" / "Beta 01.cbz",            # folder since removed
        root / ".stversions" / "Old.cbz",         # never planned
        root / "Flat" / "x.cbz",                  # now a file, not a folder
        root / "Loose 01.cbz",                    # root-level book
        tmp_path / "elsewhere" / "S" / "x.cbz",   # another library root
    ], event="changed")
    chosen = cs.changed_scopes(db, root, batch_id=batch, since=False, internal=cp._INTERNAL)
    assert chosen.planned == ["Alpha"] and chosen.gone == ["Beta"]
    assert chosen.excluded == [".stversions"] and chosen.unavailable == ["Flat"]
    assert chosen.root_level == ["Loose 01.cbz"] and chosen.outside_root == 1 and chosen.events == 6
    assert chosen.omitted == ["Flat", "Loose 01.cbz"]


def test_a_case_only_rename_is_planned_under_its_disk_spelling(lib):
    """The planner needs exact spellings and the catalog still holds the old
    one; resolving the event path yields the folder's current spelling."""
    root, db = lib
    batch = record(db, root, [root / "ALPHA" / "Alpha 01.cbz", root / "alpha" / "Alpha 02.cbz"])
    assert cs.changed_scopes(db, root, batch_id=batch, since=False).planned == ["Alpha"]


def test_a_commit_during_the_read_refuses_the_selection(lib, monkeypatch):
    """Another connection commits inside the guarded read window (through the
    module's quick_check binding); data_version moves and the read is refused."""
    root, db = lib
    batch = record(db, root, [root / "Alpha" / "Alpha 01.cbz"])
    real_check = cs.quick_check

    def check_then_commit(connection):
        writer = sqlite3.connect(db)
        writer.execute("INSERT INTO file_events(event_type, source_path) VALUES ('changed', 'x')")
        writer.commit()
        writer.close()
        return real_check(connection)

    monkeypatch.setattr(cs, "quick_check", check_then_commit)
    with pytest.raises(cs.ChangedScopesRefused, match="changed while the selection was read"):
        cs.changed_scopes(db, root, batch_id=batch, since=True)


def test_the_read_never_writes_the_catalog(lib):
    root, db = lib
    batch = record(db, root, [root / "Alpha" / "Alpha 01.cbz"])
    before = db.read_bytes()
    cs.changed_scopes(db, root, batch_id=batch, since=True)
    assert db.read_bytes() == before


# --- the planner seals the selection ---------------------------------------

def test_the_plan_digest_binds_the_selection(env):
    root, db, book, plan = env
    alpha, beta = _two_series(book)
    chosen = cs.changed_scopes(db, root, batch_id=record(db, root, [alpha]), since=False).as_dict()
    sealed = plan(scopes=["Alpha"], selection=chosen)
    bare = plan(scopes=["Alpha"])
    assert sealed["planning_scope"] == {"mode": "series", "requested": ["Alpha"], "selection": chosen}
    assert sealed["plan_digest"] != bare["plan_digest"]
    assert sealed["plan_digest"] == cp.hashlib.sha256(cp.canonical_bytes(sealed)).hexdigest()


def test_a_selection_must_describe_exactly_the_planned_scopes(env):
    root, db, book, plan = env
    alpha, beta = _two_series(book)
    chosen = cs.changed_scopes(db, root, batch_id=record(db, root, [alpha]), since=False).as_dict()
    for scopes in (["Beta"], ["Alpha", "Beta"], None):
        with pytest.raises(cp.PlanningRefused, match="selection must list exactly"):
            plan(scopes=scopes, selection=chosen)


def test_cli_plans_the_changed_folders_and_seals_the_selection(env, capsys):
    root, db, book, plan = env
    alpha, beta = _two_series(book)
    batch = record(db, root, [beta])
    args = ["--library-root", str(root), "--database", str(db), "--quarantine-date", "2026-10-24",
            "--number-style", "chapter"]
    assert main([*args, "--changed-in-batch", str(batch)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert [r["source"] for r in report["renames"]] == [str(beta)]
    assert report["planning_scope"]["selection"]["planned"] == ["Beta"]
    record(db, root, [alpha])  # a later batch: only "since" reaches it
    assert main([*args, "--changed-since-batch", str(batch)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert sorted(r["source"] for r in report["renames"]) == sorted([str(alpha), str(beta)])
    assert report["planning_scope"]["selection"]["mode"] == "since"


def test_cli_with_nothing_to_plan_prints_the_selection_only(env, capsys, tmp_path):
    root, db, book, plan = env
    _two_series(book)
    args = ["--library-root", str(root), "--database", str(db), "--quarantine-date", "2026-10-24",
            "--output", str(tmp_path / "plan.json")]
    quiet = record(db, root, [])
    assert main([*args, "--changed-in-batch", str(quiet)]) == 0
    assert json.loads(capsys.readouterr().out)["plan"] is None
    loose = record(db, root, [root / "Loose 01.cbz"])
    assert main([*args, "--changed-since-batch", str(loose)]) == 1
    assert json.loads(capsys.readouterr().out)["selection"]["root_level"] == ["Loose 01.cbz"]
    assert not (tmp_path / "plan.json").exists()


def test_cli_omissions_make_a_produced_plan_exit_1(env, capsys):
    root, db, book, plan = env
    alpha, _ = _two_series(book)
    batch = record(db, root, [alpha, root / "Loose 01.cbz"])
    args = ["--library-root", str(root), "--database", str(db), "--quarantine-date", "2026-10-24",
            "--number-style", "chapter", "--changed-in-batch", str(batch)]
    assert main(args) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["counts"]["review"] == 0 and not report["skipped_scopes"]
    assert report["planning_scope"]["selection"]["root_level"] == ["Loose 01.cbz"]


def test_cli_refuses_conflicting_or_untrustworthy_selections(env, capsys):
    root, db, book, plan = env
    alpha, _ = _two_series(book)
    batch = record(db, root, [alpha], resumed=True)
    args = ["--library-root", str(root), "--database", str(db), "--quarantine-date", "2026-10-24"]
    assert main([*args, "--changed-in-batch", str(batch)]) == 2
    assert "resumed" in capsys.readouterr().err
    for extra in (["--scope", "Alpha", "--changed-in-batch", "1"],
                  ["--changed-in-batch", "1", "--changed-since-batch", "1"]):
        with pytest.raises(SystemExit) as raised:
            main([*args, *extra])
        assert raised.value.code == 2


# --- preflight and root availability (codex-lead review of #192, R1/R2) ----

def cli_args(root, db, batch, flag="--changed-in-batch"):
    return ["--library-root", str(root), "--database", str(db), "--quarantine-date", "2026-10-24",
            "--number-style", "chapter", flag, str(batch)]


def test_a_missing_library_is_refused_not_an_empty_selection(lib, capsys):
    root, db = lib
    batch = record(db, root, [root / "Alpha" / "Alpha 01.cbz"])
    root.rename(root.with_name("offline-library"))
    assert main(cli_args(root, db, batch)) == 2
    assert capsys.readouterr().out == ""


def test_a_library_disappearing_during_the_read_is_refused(lib, monkeypatch, capsys):
    """Unmounted inside the guarded window: every folder would look gone."""
    root, db = lib
    batch = record(db, root, [root / "Alpha" / "Alpha 01.cbz"])
    real_check = cs.quick_check

    def check_then_unmount(connection):
        result = real_check(connection)
        root.rename(root.with_name("offline-library"))
        return result

    monkeypatch.setattr(cs, "quick_check", check_then_unmount)
    with pytest.raises(cs.ChangedScopesRefused, match="unavailable"):
        cs.changed_scopes(db, root, batch_id=batch, since=False)


def test_a_library_replaced_during_the_read_is_refused(lib, monkeypatch):
    """Same path, different directory: the identity recheck, not existence, catches it."""
    root, db = lib
    batch = record(db, root, [root / "Alpha" / "Alpha 01.cbz"])
    real_check = cs.quick_check

    def check_then_swap(connection):
        result = real_check(connection)
        root.rename(root.with_name("old-library"))
        root.mkdir()
        return result

    monkeypatch.setattr(cs, "quick_check", check_then_swap)
    with pytest.raises(cs.ChangedScopesRefused, match="replaced"):
        cs.changed_scopes(db, root, batch_id=batch, since=False)


def test_a_removed_series_under_a_reachable_library_is_a_valid_noop(lib, capsys):
    root, db = lib
    batch = record(db, root, [root / "Alpha" / "Alpha 01.cbz"], event="missing")
    shutil.rmtree(root / "Alpha")
    assert main(cli_args(root, db, batch)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["plan"] is None and report["selection"]["gone"] == ["Alpha"]


@pytest.mark.parametrize("alias", [False, True])
@pytest.mark.parametrize("has_events", [False, True])
def test_a_catalog_inside_the_library_is_refused_before_sqlite_opens(lib, tmp_path, monkeypatch, capsys,
                                                                    alias, has_events):
    """Empty and non-empty selections, direct and through a junction alias:
    no open, so no WAL/SHM can appear in the library."""
    root, db = lib
    batch = record(db, root, [root / "Alpha" / "Alpha 01.cbz"] if has_events else [])
    inside = root / "catalog.db"
    shutil.copyfile(db, inside)
    database = inside
    if alias:
        link = tmp_path / "db-alias"
        _junction(link, root)
        database = link / inside.name
    before = set(root.iterdir())
    monkeypatch.setattr(cs, "read_consistent_snapshot", lambda *a, **k: pytest.fail("opened SQLite"))
    assert main(cli_args(root, database, batch)) == 2
    assert "outside the library" in capsys.readouterr().err
    assert set(root.iterdir()) == before


def test_an_originals_root_is_refused_even_for_an_empty_selection(lib, capsys):
    root, db = lib
    originals = root / "ai-decensor-originals"
    originals.mkdir()
    batch = record(db, originals, [])
    assert main(cli_args(originals, db, batch)) == 2
    assert "ai-decensor-originals" in capsys.readouterr().err


def test_a_later_append_stays_outside_the_recorded_cutoff(env, monkeypatch, capsys):
    """Selection and plan are two snapshots. The selection's upper_event_id is
    the cutoff; an event appended in between waits for the next selection."""
    from scripts import cbz_canonical_plan as cli
    root, db, book, plan = env
    alpha, beta = _two_series(book)
    batch = record(db, root, [alpha])
    real = cli.changed_scopes

    def select_then_append(*a, **k):
        chosen = real(*a, **k)
        record(db, root, [beta])
        return chosen

    monkeypatch.setattr(cli, "changed_scopes", select_then_append)
    assert main(cli_args(root, db, batch, "--changed-since-batch")) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["planning_scope"]["selection"]["upper_event_id"] == 1
    assert [r["source"] for r in report["renames"]] == [str(alpha)]
