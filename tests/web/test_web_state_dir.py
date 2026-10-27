"""The G: state directory guards (design §4.2): every refusal, privacy, the
single-console lock and the identity pin. All tests use temporary directories,
never G:."""

from __future__ import annotations

import os
import sqlite3
import subprocess

import pytest

from comic_automation.web import acl
from comic_automation.web import state_dir as sd
from comic_automation.web.state_dir import StateDirRefused, init_state_dir, open_state_dir, state_dir_from_env
from web_support import private_dir

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows ACLs and junctions")


@pytest.fixture
def state(tmp_path):
    """A state directory prepared by the operator's --init-state-dir step."""
    return private_dir(tmp_path / "web-console")


def _toml(path, text):
    # The tests write paths as TOML literal strings ('...'), which keep
    # backslashes exactly as written, so no escaping is needed.
    (path / "console.toml").write_text(text, encoding="utf-8")


def test_production_default_is_g_and_the_override_is_the_test_variable():
    assert str(state_dir_from_env({})) == r"G:\ComicAutomation\web-console"
    assert str(state_dir_from_env({"CBZ_WEB_CONSOLE_HOME": r"C:\t\s"})) == r"C:\t\s"


def test_a_missing_directory_is_refused_not_created(tmp_path):
    missing = tmp_path / "web-console"
    with pytest.raises(StateDirRefused, match="does not exist.*--init-state-dir"):
        open_state_dir(missing, environ={})
    assert not missing.exists()  # a remounted drive must not get a fresh, second ledger


def test_init_creates_a_private_directory_but_never_its_parent(tmp_path):
    made = init_state_dir(tmp_path / "web-console")
    assert made.is_dir() and acl.privacy_problems(made) == []
    with pytest.raises(StateDirRefused, match="parent .* does not exist"):
        init_state_dir(tmp_path / "unmounted-drive" / "web-console")
    assert not (tmp_path / "unmounted-drive").exists()


def test_a_file_in_place_of_the_directory_is_refused(tmp_path):
    f = tmp_path / "web-console"
    f.write_text("")
    with pytest.raises(StateDirRefused, match="not a directory"):
        open_state_dir(f, environ={})


@windows_only
def test_a_directory_that_inherits_shared_access_is_refused(tmp_path):
    """The real case: a folder made by hand under G:\\ComicAutomation inherits
    Authenticated Users: Modify. It must not start until made private."""
    plain = tmp_path / "web-console"
    plain.mkdir()
    with pytest.raises(StateDirRefused, match="not private to you.*--init-state-dir"):
        open_state_dir(plain, environ={})
    assert not (plain / "locks").exists()  # refused before writing anything
    init_state_dir(plain)                  # the documented repair
    open_state_dir(plain, environ={}).close()


@windows_only
def test_sddl_aliases_resolve_to_full_sids():
    """The CI runner is the built-in Administrator, which SDDL writes as LA; a
    hand-kept alias table that missed it refused the operator's own folder."""
    assert acl.canonical_sid("SY") == acl.SYSTEM_SID
    assert acl.canonical_sid("BA") == acl.ADMINISTRATORS_SID
    la = acl.canonical_sid("LA")
    assert la.startswith("S-1-5-21-") and la.endswith("-500")
    me = acl.operator_sid()
    assert acl.canonical_sid(me) == me


@windows_only
def test_inheriting_is_refused_even_when_the_parent_is_private(tmp_path):
    """Isolates the protected-DACL check: every inherited entry is an allowed
    account, so 'inherits access' is the only problem. Inheritance means a later
    change to the parent's access would silently reach the secret files."""
    parent = tmp_path / "private-parent"
    parent.mkdir()
    acl.make_private(parent, directory=True)
    child = parent / "web-console"
    child.mkdir()
    assert acl.privacy_problems(child) == ["inherits access from its parent folder"]
    with pytest.raises(StateDirRefused, match="inherits access from its parent folder"):
        open_state_dir(child, environ={})


@windows_only
def test_an_extra_grant_on_a_private_directory_is_refused(state):
    subprocess.run(["icacls", str(state), "/grant", "*S-1-5-11:(OI)(CI)R"], check=True, capture_output=True)
    with pytest.raises(StateDirRefused, match="grants access to"):
        open_state_dir(state, environ={})


def test_first_open_copies_the_example_config_and_pins_identity(state):
    opened = open_state_dir(state, environ={})
    try:
        assert (state / "console.toml").read_bytes() == sd.EXAMPLE_CONFIG.read_bytes()
        db = sqlite3.connect(state / "console.sqlite3")
        pinned = db.execute("SELECT value FROM console_meta WHERE key='state_dir_identity'").fetchone()[0]
        db.close()
        assert pinned == f"{opened.identity[0]}:{opened.identity[1]}"
        assert all(opened.identity)
    finally:
        opened.close()


def test_only_one_console_can_hold_the_directory(state):
    first = open_state_dir(state, environ={})
    try:
        with pytest.raises(StateDirRefused, match="already running"):
            open_state_dir(state, environ={})
    finally:
        first.close()
    open_state_dir(state, environ={}).close()  # released on close


def test_a_changed_identity_is_refused(state):
    open_state_dir(state, environ={}).close()
    db = sqlite3.connect(state / "console.sqlite3")
    with db:
        db.execute("UPDATE console_meta SET value='1:2' WHERE key='state_dir_identity'")
    db.close()
    with pytest.raises(StateDirRefused, match="identity changed"):
        open_state_dir(state, environ={})
    # The refusal closed its connection, so Windows lets the file go at once.
    (state / "console.sqlite3").unlink()


def test_a_refusal_after_locking_releases_the_lock(state):
    (state / "console.toml").write_text("[server]\nport = 1\n")
    with pytest.raises(StateDirRefused, match="console.toml is invalid"):
        open_state_dir(state, environ={})
    (state / "console.toml").write_text("")
    open_state_dir(state, environ={}).close()  # would say "already running" if the lock leaked


def test_zero_identity_is_refused(state, monkeypatch):
    real_stat = sd.os.stat

    class NoInode:
        """A real stat result with st_ino zeroed, as some filesystems report."""

        def __init__(self, st):
            self._st = st

        def __getattr__(self, name):
            return 0 if name == "st_ino" else getattr(self._st, name)

    def fake_stat(path, *a, **k):
        st = real_stat(path, *a, **k)
        return NoInode(st) if str(path) == str(state) else st

    monkeypatch.setattr(sd.os, "stat", fake_stat)
    with pytest.raises(StateDirRefused, match="no usable file identity"):
        open_state_dir(state, environ={})


def test_an_unwritable_directory_is_refused(state, monkeypatch):
    real_write = sd.Path.write_bytes

    def deny(self, data):
        if self.name.startswith(".write-probe-"):
            raise PermissionError("denied")
        return real_write(self, data)

    monkeypatch.setattr(sd.Path, "write_bytes", deny)
    with pytest.raises(StateDirRefused, match="not writable"):
        open_state_dir(state, environ={})


def test_a_lock_that_cannot_be_created_is_a_refusal_not_a_traceback(state, monkeypatch):
    def deny(self, *a, **k):
        raise PermissionError("read-only")

    monkeypatch.setattr(sd.Path, "mkdir", deny)
    with pytest.raises(StateDirRefused, match="Cannot create the console lock"):
        open_state_dir(state, environ={})


@pytest.mark.parametrize("role", ["library", "quarantine", "decensor_archive", "readiness_hold"])
def test_the_state_directory_may_not_sit_inside_a_configured_root(tmp_path, role):
    root = tmp_path / "comics"
    root.mkdir()
    state = private_dir(root / "web-console")
    _toml(state, f"[paths]\n{role} = ['{str(root).upper()}']\n")
    with pytest.raises(StateDirRefused, match=f"inside paths.{role}"):
        open_state_dir(state, environ={})


@windows_only
def test_a_junction_into_a_root_is_caught_on_the_resolved_path(tmp_path):
    import _winapi

    root = tmp_path / "library"
    root.mkdir()
    private_dir(root / "web-console")
    link = tmp_path / "elsewhere"
    _winapi.CreateJunction(str(root), str(link))  # no privilege needed, as in the planner tests
    _toml(root / "web-console", f"[paths]\nlibrary = ['{root}']\n")
    with pytest.raises(StateDirRefused, match="inside paths.library"):
        open_state_dir(link / "web-console", environ={})


def test_network_roots_are_never_resolved(state, monkeypatch):
    """Resolving a UNC root blocks for 20 s+ when the server is down; a network
    root is compared lexically only."""
    _toml(state, "[paths]\nlibrary = ['\\\\tower-is-offline\\media\\comics']\n")
    assert "\\\\tower-is-offline\\media" in (state / "console.toml").read_text()  # one UNC prefix, as written
    real = sd.os.path.realpath

    def guarded(p, *a, **k):
        assert not str(p).startswith("\\\\tower-is-offline"), "network root was resolved"
        return real(p, *a, **k)

    monkeypatch.setattr(sd.os.path, "realpath", guarded)
    open_state_dir(state, environ={}).close()


def test_a_state_directory_under_a_unc_root_is_still_caught_lexically():
    unc_root = "\\\\server\\share\\comics"
    hit = sd._containing_root(sd.Path(unc_root + "\\web-console"), [("paths.library", unc_root)])
    assert hit == ("paths.library", unc_root)


def test_the_state_directory_may_not_sit_inside_the_repository(monkeypatch, tmp_path):
    monkeypatch.setattr(sd, "REPO_ROOT", tmp_path)
    inner = private_dir(tmp_path / "state")
    with pytest.raises(StateDirRefused, match="inside the repository"):
        open_state_dir(inner, environ={})


def test_the_state_directory_may_not_sit_inside_the_watch_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(sd, "watcher_watch_folder", lambda: str(tmp_path))
    inner = private_dir(tmp_path / "state")
    with pytest.raises(StateDirRefused, match="inside the watcher's watch folder"):
        open_state_dir(inner, environ={})


def test_database_and_plan_roots_may_contain_it(tmp_path):
    state = private_dir(tmp_path / "web-console")
    _toml(state, f"[paths]\ndatabase = ['{tmp_path}']\nplan_import = ['{tmp_path}']\n")
    open_state_dir(state, environ={}).close()
