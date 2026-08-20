"""Tests for the minimum size gain before an archive may be replaced.

The watcher resolves a filename collision by keeping the larger file and
deleting the other. That is the intended policy -- for the same chapter name, a
bigger archive is normally the better scan -- but before this threshold it
applied to *any* gain at all.

Measured across 1,261 conflict replacements in the watcher logs from 2026-03 to
2026-08, 15.1% gained under 1 KB and the smallest were +1, +2 and +6 bytes. A
gain that size is a repack, a rewritten ComicInfo.xml, or different zip
metadata around identical pages. Acting on it destroyed a working archive and
invalidated the recorded page inventory, archive hash and content signature of
a file the database had already inspected -- for nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.cbz_watcher import (
    REPLACEMENT_MIN_GAIN_BYTES,
    _merge_directories,
    _replacement_gain_is_meaningful,
)


def write(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


# --- the decision --------------------------------------------------------


def test_the_threshold_is_ten_kilobytes() -> None:
    """Pinned because the value came from a measurement, not a preference.

    10 KB is where the observed distribution turns: it blocks 19.1% of past
    replacements, 20 KB blocks only 0.9% more, and 100 KB starts refusing gains
    large enough to be genuine.
    """
    assert REPLACEMENT_MIN_GAIN_BYTES == 10 * 1024


@pytest.mark.parametrize("gain", [1, 2, 6, 23, 1024, 10 * 1024 - 1])
def test_a_gain_below_the_threshold_is_not_meaningful(gain: int) -> None:
    """+1 and +6 bytes are real values from the log, not invented ones."""
    assert _replacement_gain_is_meaningful(1_000_000 + gain, 1_000_000) is False


def test_a_gain_exactly_at_the_threshold_is_meaningful() -> None:
    """The boundary is inclusive; pinned so a later >/>= slip is caught."""
    assert _replacement_gain_is_meaningful(
        1_000_000 + REPLACEMENT_MIN_GAIN_BYTES, 1_000_000
    ) is True


@pytest.mark.parametrize("gain", [10 * 1024 + 1, 5_000_000, 9_000_000_000])
def test_a_gain_above_the_threshold_is_meaningful(gain: int) -> None:
    assert _replacement_gain_is_meaningful(1_000_000 + gain, 1_000_000) is True


@pytest.mark.parametrize("delta", [0, -1, -5_000_000])
def test_an_equal_or_smaller_incoming_file_is_never_meaningful(
    delta: int,
) -> None:
    """Unchanged behaviour: the existing file already wins these."""
    assert _replacement_gain_is_meaningful(1_000_000 + delta, 1_000_000) is False


# --- the behaviour it controls ------------------------------------------


def test_a_marginal_gain_leaves_the_existing_archive_in_place(
    tmp_path: Path,
) -> None:
    """The case this exists for: +250 B must not destroy a working archive.

    Sub-kilobyte replacements are why five archives ended up with a page
    inventory describing bytes that no longer existed.
    """
    source = tmp_path / "incoming"
    destination = tmp_path / "library"
    write(source / "Ch 1.cbz", 40_000_250)
    existing = write(destination / "Ch 1.cbz", 40_000_000)
    existing_bytes = existing.stat().st_size

    _merge_directories(source, destination)

    assert existing.stat().st_size == existing_bytes
    assert not (source / "Ch 1.cbz").exists()


def test_a_real_gain_still_replaces(tmp_path: Path) -> None:
    """The policy itself is unchanged: a genuinely bigger scan still wins."""
    source = tmp_path / "incoming"
    destination = tmp_path / "library"
    write(source / "Ch 1.cbz", 40_000_000 + 5 * 1024 * 1024)
    write(destination / "Ch 1.cbz", 40_000_000)

    _merge_directories(source, destination)

    assert (destination / "Ch 1.cbz").stat().st_size == (
        40_000_000 + 5 * 1024 * 1024
    )
    assert not (source / "Ch 1.cbz").exists()


def test_a_smaller_incoming_archive_is_still_discarded(tmp_path: Path) -> None:
    source = tmp_path / "incoming"
    destination = tmp_path / "library"
    write(source / "Ch 1.cbz", 1_000)
    write(destination / "Ch 1.cbz", 40_000_000)

    _merge_directories(source, destination)

    assert (destination / "Ch 1.cbz").stat().st_size == 40_000_000
    assert not (source / "Ch 1.cbz").exists()


def test_a_new_archive_without_a_collision_is_moved(tmp_path: Path) -> None:
    """The threshold must not affect files that collide with nothing."""
    source = tmp_path / "incoming"
    destination = tmp_path / "library"
    write(source / "Ch 2.cbz", 500)
    write(destination / "Ch 1.cbz", 40_000_000)

    _merge_directories(source, destination)

    assert (destination / "Ch 2.cbz").exists()
    assert (destination / "Ch 1.cbz").stat().st_size == 40_000_000
