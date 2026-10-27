"""Runner inspection shares guards with the server but cannot initialize state."""
from pathlib import Path

import pytest

from comic_automation.database import dal
from comic_automation.web import acl, state_dir as sd
from web_support import private_dir


@pytest.fixture
def opened(tmp_path):
    state = sd.open_state_dir(private_dir(tmp_path / "state"), environ={})
    try:
        yield state
    finally:
        state.close()


def inspect(state):
    return sd.inspect_state_dir(state.path, expected_identity=state.identity, environ={})


def test_runner_can_inspect_while_server_owns_console_lock(opened):
    view = inspect(opened)
    assert view.identity == opened.identity and view.database == opened.database
    assert view._lock is None
    view.close()
    with pytest.raises(sd.StateDirRefused, match="already running"):
        sd.open_state_dir(opened.path, environ={})


def test_inspection_never_creates_a_missing_home(tmp_path):
    missing = tmp_path / "absent"
    with pytest.raises(sd.StateDirRefused, match="does not exist"):
        sd.inspect_state_dir(missing, expected_identity=(1, 1), environ={})
    assert not missing.exists()


def test_inspection_never_creates_a_missing_ledger(tmp_path):
    root = private_dir(tmp_path / "state")
    st = root.stat()
    with pytest.raises(sd.StateDirRefused, match="existing console ledger"):
        sd.inspect_state_dir(root, expected_identity=(st.st_dev, st.st_ino), environ={})
    assert not (root / "console.sqlite3").exists()
    assert not (root / "locks").exists()


def test_inspection_does_not_copy_missing_configuration(opened):
    (opened.path / "console.toml").unlink()
    with pytest.raises(sd.StateDirRefused, match="console.toml is invalid"):
        inspect(opened)
    assert not (opened.path / "console.toml").exists()


def test_an_expected_identity_mismatch_refuses(opened):
    wrong = (opened.identity[0], opened.identity[1] + 1)
    with pytest.raises(sd.StateDirRefused, match="identity changed"):
        sd.inspect_state_dir(opened.path, expected_identity=wrong, environ={})


@pytest.mark.parametrize("replacement", [None, "1:2"])
def test_missing_or_wrong_pin_is_never_initialized_or_rewritten(opened, replacement):
    with dal.connection_scope(opened.database) as db, dal.transaction(db):
        db.execute("DELETE FROM console_meta WHERE key = 'state_dir_identity'")
        if replacement is not None:
            db.execute("INSERT INTO console_meta VALUES ('state_dir_identity', ?)", (replacement,))
    with pytest.raises(sd.StateDirRefused, match="pin is missing or changed"):
        inspect(opened)
    with dal.connection_scope(opened.database, readonly=True) as db:
        row = db.execute("SELECT value FROM console_meta WHERE key='state_dir_identity'").fetchone()
    assert (row[0] if row else None) == replacement


def test_runner_inspection_rechecks_containment(opened):
    parent = str(opened.path.parent)
    (opened.path / "console.toml").write_text(f"[paths]\nlibrary = ['{parent}']\n")
    with pytest.raises(sd.StateDirRefused, match="inside paths.library"):
        inspect(opened)


@pytest.mark.skipif(sd.os.name != "nt", reason="native Windows permissions")
def test_runner_inspection_rechecks_child_permissions(opened):
    acl._icacls(str(opened.database), "/grant", "*S-1-5-11:R")
    with pytest.raises(sd.StateDirRefused, match="not private"):
        inspect(opened)


@pytest.mark.skipif(sd.os.name != "nt", reason="native Windows permissions")
def test_runner_inspection_directly_refuses_a_broad_root_grant(opened):
    acl._icacls(str(opened.path), "/grant", "*S-1-5-11:R")
    with pytest.raises(sd.StateDirRefused, match="not private"):
        inspect(opened)


def test_runner_inspection_directly_refuses_a_network_location(opened, monkeypatch):
    real = sd._is_network
    monkeypatch.setattr(sd, "_is_network", lambda p: str(p) == str(opened.path) or real(p))
    with pytest.raises(sd.StateDirRefused, match="network location"):
        inspect(opened)


def test_runner_inspection_directly_refuses_zero_identity(opened, monkeypatch):
    real = sd.os.stat
    def zero_identity(path, *args, **kwargs):
        value = real(path, *args, **kwargs)
        if str(path) == str(opened.path):
            fields = list(value)
            fields[1] = fields[2] = 0
            return sd.os.stat_result(fields)
        return value
    monkeypatch.setattr(sd.os, "stat", zero_identity)
    with pytest.raises(sd.StateDirRefused, match="identity changed or is unusable"):
        inspect(opened)


def test_runner_inspection_sees_a_committed_wal_pin(opened):
    # Keep a writer open so the committed pin is genuinely in WAL, not merely
    # a checkpointed database file that an immutable reader could also see.
    with dal.connection_scope(opened.database) as db:
        db.execute("PRAGMA wal_autocheckpoint = 0")
        with dal.transaction(db):
            db.execute("UPDATE console_meta SET value='0:0' WHERE key='state_dir_identity'")
        assert Path(str(opened.database) + "-wal").exists()
        with pytest.raises(sd.StateDirRefused, match="pin is missing or changed"):
            inspect(opened)
