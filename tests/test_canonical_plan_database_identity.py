"""DB containment witnesses using temporary local, UNC and mapped paths.

Only localhost admin shares are used; no existing drive mapping is changed.
Host-dependent cases skip explicitly when that access or privilege is absent.
"""

from __future__ import annotations

import ctypes
import os
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.library import canonical_plan as cp

MIGRATIONS = Path(__file__).resolve().parents[1] / "comic_automation/database/migrations"


@pytest.fixture
def catalog_paths(tmp_path):
    """A nested in-library catalog and an independent, valid external one."""
    root = tmp_path / "library"
    inside = root / "metadata" / "catalog.db"
    inside.parent.mkdir(parents=True)
    outside = tmp_path / "outside.db"
    with database_connection(outside) as connection:
        apply_migrations(connection, MIGRATIONS)
    inside.write_bytes(outside.read_bytes())
    return root, inside, outside


def _unc(path: Path) -> Path:
    """Expose only this fixture via the host's existing localhost admin share."""
    if os.name != "nt" or len(path.drive) != 2 or path.drive[1] != ":":
        pytest.skip("localhost drive-share witness requires a Windows drive")
    result = Path(f"//localhost/{path.drive[0]}$") / path.relative_to(path.anchor)
    try:
        result.stat()
    except OSError as exc:
        pytest.skip(f"localhost admin share unavailable: {exc}")
    # Establish actual host alias semantics, rather than assuming them.
    assert os.path.samestat(path.stat(), result.stat())
    return result


@pytest.fixture
def mapped_parent(tmp_path):
    """Temporarily map one unused drive to this fixture, then remove only it."""
    target = _unc(tmp_path)
    kernel = ctypes.windll.kernel32
    used = kernel.GetLogicalDrives()
    for letter in "PQRSTUVWDEHIJKLMNO":
        drive = letter + ":"
        buffer = ctypes.create_unicode_buffer(32768)
        if used & (1 << (ord(letter) - ord("A"))):
            continue
        if kernel.QueryDosDeviceW(drive, buffer, len(buffer)):
            continue
        result = subprocess.run(
            ["net", "use", drive, str(target), "/persistent:no"],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode:
            pytest.skip(f"temporary drive mapping unavailable: {result.stderr.strip()}")
        try:
            mapped = Path(drive + "\\")
            assert os.path.samestat(tmp_path.stat(), mapped.stat())
            yield mapped
        finally:
            subprocess.run(["net", "use", drive, "/delete", "/y"],
                           check=True, capture_output=True, timeout=20)
        return
    pytest.skip("no unused drive letter for the temporary mapping")


def _contents(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() if p.is_file() else None
            for p in root.rglob("*")}


def _refuses_before_open(root: Path, database: Path, local_root: Path, monkeypatch):
    """Assert refusal precedes SQLite and leaves fixture bytes/names intact."""
    before = _contents(local_root)

    def forbidden(*args, **kwargs):
        pytest.fail("containment must refuse before opening SQLite")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    with pytest.raises(cp.PlanningRefused, match="database.*outside the library"):
        cp.build_plan(root, database=database, quarantine_date="2026-10-26")
    assert _contents(local_root) == before


@pytest.mark.parametrize("direction", ["unc-root", "unc-database"])
def test_mixed_unc_local_catalog_refuses_before_sqlite(catalog_paths, monkeypatch, direction):
    """Different resolved strings still name the same nested library catalog."""
    root, inside, _ = catalog_paths
    alias_root, database = (_unc(root), inside) if direction == "unc-root" else (root, _unc(inside))
    assert root.resolve() != _unc(root).resolve()
    _refuses_before_open(alias_root, database, root, monkeypatch)


@pytest.mark.parametrize("direction", ["mapped-root", "mapped-database"])
def test_mapped_local_catalog_refuses_before_sqlite(catalog_paths, mapped_parent, monkeypatch, direction):
    """A real SMB drive mapping must not put WAL/SHM inside the library."""
    root, inside, _ = catalog_paths
    mapped_root = mapped_parent / root.name
    mapped_database = mapped_root / inside.relative_to(root)
    alias_root, database = (mapped_root, inside) if direction == "mapped-root" else (root, mapped_database)
    _refuses_before_open(alias_root, database, root, monkeypatch)


@pytest.mark.parametrize("direction", ["unc-root", "unc-database"])
def test_outward_junction_keeps_the_catalog_access_path_guarded(catalog_paths, tmp_path, monkeypatch, direction):
    """Resolving away an in-library access directory must not erase it.

    The mixed alias also bypasses lexical containment, forcing inspection of
    the supplied path's ancestors as well as the resolved target's ancestors.
    """
    root, _, outside = catalog_paths
    _unc(root)  # Establish availability before creating a junction.
    home = tmp_path / "external-home"
    home.mkdir()
    (home / outside.name).write_bytes(outside.read_bytes())
    link = root / "database-access"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(home)],
                   check=True, capture_output=True)
    try:
        database = link / outside.name
        alias_root, chosen = (_unc(root), database) if direction == "unc-root" else (root, _unc(database))
        _refuses_before_open(alias_root, chosen, root, monkeypatch)
    finally:
        link.rmdir()  # Remove the junction only, preserving the target fixture.


@pytest.mark.parametrize("alias", ["local", "unc", "mapped"])
def test_external_catalog_still_plans_with_a_library_alias(catalog_paths, request, alias):
    """A common parent or device does not make an external catalog internal."""
    root, _, outside = catalog_paths
    chosen = root
    if alias == "unc":
        chosen = _unc(root)
    elif alias == "mapped":
        chosen = request.getfixturevalue("mapped_parent") / root.name
    before = _contents(root)
    report = cp.build_plan(chosen, database=outside, quarantine_date="2026-10-26")
    assert not report["review"] and not report["renames"] and not report["families"]
    assert report["database_target"] == cp.file_identity(outside)
    assert _contents(root) == before


def test_identity_alias_refusal_does_not_depend_on_windows_shares(catalog_paths, tmp_path, monkeypatch):
    """A synthetic directory identity alias keeps the guard exercised on CI.

    Real UNC/mapped witnesses above establish the actual Windows semantics;
    this stat seam covers the same refusal when admin shares are unavailable.
    """
    root, _, outside = catalog_paths
    original = Path.stat
    root_stat = root.stat()

    def alias_stat(path, *args, **kwargs):
        if path == outside.parent:
            return root_stat
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", alias_stat)
    _refuses_before_open(root, outside, root, monkeypatch)


def test_a_file_id_on_a_different_device_does_not_mean_containment(catalog_paths, monkeypatch):
    """File numbers alone are not global directory identities."""
    root, _, outside = catalog_paths
    original = Path.stat
    root_stat = root.stat()

    def other_device(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path != outside.parent:
            return result
        fields = {name: getattr(result, name) for name in dir(result) if name.startswith("st_")}
        fields.update(st_dev=root_stat.st_dev + 1, st_ino=root_stat.st_ino)
        return SimpleNamespace(**fields)

    monkeypatch.setattr(Path, "stat", other_device)
    report = cp.build_plan(root, database=outside, quarantine_date="2026-10-26")
    assert not report["review"] and not report["renames"] and not report["families"]


@pytest.mark.parametrize("where", ["root", "ancestor"])
@pytest.mark.parametrize("failure", ["stat-error", "no-file-id"])
def test_uncertain_directory_identity_refuses_before_sqlite(catalog_paths, monkeypatch, where, failure):
    """A failed query or zero file id cannot prove that a DB is outside."""
    root, _, outside = catalog_paths
    original = Path.stat
    uncertain = root if where == "root" else outside.parent
    calls = 0

    def failing_stat(path, *args, **kwargs):
        nonlocal calls
        result = original(path, *args, **kwargs)
        if path != uncertain:
            return result
        calls += 1
        if failure == "stat-error":
            # root.is_dir() must still work; exercise the identity query itself.
            if where == "root" and calls == 1:
                return result
            raise PermissionError("fixture identity query denied")
        fields = {name: getattr(result, name) for name in dir(result) if name.startswith("st_")}
        fields["st_ino"] = 0
        return SimpleNamespace(**fields)

    monkeypatch.setattr(Path, "stat", failing_stat)

    def forbidden(*args, **kwargs):
        pytest.fail("identity uncertainty must refuse before opening SQLite")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    with pytest.raises(cp.PlanningRefused, match="directory identity"):
        cp.build_plan(root, database=outside, quarantine_date="2026-10-26")


@pytest.mark.parametrize("direction", ["inside-link", "outside-link"])
def test_file_symlink_catalog_refuses_before_sqlite(catalog_paths, tmp_path, monkeypatch, direction):
    """Both the supplied access path and its resolved target must be outside."""
    root, inside, outside = catalog_paths
    link, target = (root / "linked.db", outside) if direction == "inside-link" else (tmp_path / "linked.db", inside)
    try:
        link.symlink_to(target)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("file symlink privilege unavailable on this Windows host")
        raise
    _refuses_before_open(root, link, root, monkeypatch)
