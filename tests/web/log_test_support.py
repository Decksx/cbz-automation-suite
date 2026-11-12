"""Opt-in recovery for transient Windows index replacement in stress fixtures.

The production pump already retries storage errors and records sticky failure.
These direct-writer fixtures test retention/byte coverage, not pump recovery.
Retry only the observed index.tmp -> index.json WinError5; leave all fault
tests and production behavior alone. An exhausted retry still fails the test.
"""
from pathlib import Path
import time
import warnings

import pytest

from comic_automation.web.jobs import logs as lg


def _publish_with_retry(disk, candidate, publish):
    for attempt in range(5):
        try:
            return publish(disk, candidate)
        except PermissionError as error:
            if (getattr(error, "winerror", None) != 5
                    or error.filename is None
                    or getattr(error, "filename2", None) is None
                    or Path(error.filename) != disk.directory / "index.tmp"
                    or Path(error.filename2) != disk.index_path
                    or attempt == 4):
                raise
            warnings.warn(
                f"Transient log index WinError5; retry {attempt + 1}/4: {disk.index_path}",
                pytest.PytestWarning, stacklevel=2,
            )
            time.sleep(0.02)


@pytest.fixture
def retry_log_index(monkeypatch):
    """Opt in on direct-writer stress tests only, including initial publication."""
    publish = lg.LogDisk._publish

    def retry(disk, candidate):
        return _publish_with_retry(disk, candidate, publish)

    monkeypatch.setattr(lg.LogDisk, "_publish", retry)
