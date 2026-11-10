"""Suite-wide isolation for watcher runtime state."""

import pytest


@pytest.fixture(autouse=True)
def _isolated_claim_roots(tmp_path, monkeypatch):
    r"""Claim into, and hold for review in, the test's tmp dir.

    The defaults are real operator folders under C:\Temp. Both sit beside
    the test's watch folders, on the same volume, as claims require.
    """
    monkeypatch.setattr("scripts.cbz_watcher.PROCESSING_ROOT", tmp_path / "_processing")
    monkeypatch.setattr("scripts.cbz_watcher.REVIEW_ROOT", tmp_path / "_review")


@pytest.fixture(autouse=True)
def _isolated_quarantine(tmp_path, monkeypatch):
    r"""Keep keep-the-larger's quarantine (``QUARANTINE_ROOT``) in the test's tmp dir.

    The default is C:\Temp\cbz_held\quarantine, a real operator folder.
    """
    monkeypatch.setattr("scripts.cbz_watcher.QUARANTINE_ROOT", tmp_path / "_quarantine")
