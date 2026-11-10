"""Suite-wide isolation for watcher runtime state."""

import pytest


@pytest.fixture(autouse=True)
def _isolated_held_state(tmp_path, monkeypatch):
    """Keep the watcher's held-directory record out of the real Logs folder.

    Any test that runs a watcher pass may record a held directory
    (``scripts.cbz_watcher.HELD_STATE_FILE``). Without this, the record
    would land in the checkout's ``Logs`` and leak between tests, so a
    later test's directory could be skipped as "unchanged".
    """
    monkeypatch.setattr("scripts.cbz_watcher.HELD_STATE_FILE",
                        tmp_path / "_watcher_state" / "cbz_watcher_held.json")


@pytest.fixture(autouse=True)
def _isolated_quarantine(tmp_path, monkeypatch):
    r"""Keep keep-the-larger's quarantine (``QUARANTINE_ROOT``) in the test's tmp dir.

    The default is C:\Temp\cbz_held\quarantine, a real operator folder.
    """
    monkeypatch.setattr("scripts.cbz_watcher.QUARANTINE_ROOT", tmp_path / "_quarantine")
