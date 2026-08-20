"""Tests for promoting staged series directories into the library.

The watcher now routes arrivals into a staging root instead of straight into
the library, which closed the drift source and left nothing to move them
onward. These pin the tool that does.

The behaviour that matters most is the dry run being *honest*: it predicts
using the same collision lookup and the same gain rule the merge itself uses,
so an operator who accepts a plan gets what the plan said. A report that could
disagree with the action it describes would be worse than no report.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.cbz_promote_staging import (
    _remove_if_empty,
    plan_series,
    promote,
    render,
    staged_series,
)
from scripts.cbz_watcher import REPLACEMENT_MIN_GAIN_BYTES


def write(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


@pytest.fixture()
def roots(tmp_path: Path):
    staging = tmp_path / "_staging" / "Comix"
    library = tmp_path / "Comix"
    staging.mkdir(parents=True)
    library.mkdir(parents=True)
    return staging, library


# --- discovery -----------------------------------------------------------


def test_a_missing_staging_root_is_not_an_error(tmp_path: Path) -> None:
    """Promotion runs on a schedule; an absent root means nothing arrived."""
    assert staged_series(tmp_path / "never-created") == []


def test_only_immediate_child_directories_are_series(roots) -> None:
    staging, _ = roots
    write(staging / "Series A" / "Ch 1.cbz", 100)
    write(staging / "Series B" / "Ch 1.cbz", 100)
    write(staging / "loose.cbz", 100)

    assert [p.name for p in staged_series(staging)] == ["Series A", "Series B"]


# --- the dry run predicts what apply does -------------------------------


def test_a_new_series_is_all_new_files(roots) -> None:
    staging, library = roots
    write(staging / "Series A" / "Ch 1.cbz", 5_000)
    write(staging / "Series A" / "Ch 2.cbz", 5_000)

    plan = plan_series(staging / "Series A", library / "Series A")

    assert len(plan.new_files) == 2
    assert plan.replacing == []


def test_a_bigger_incoming_archive_is_reported_as_replacing(roots) -> None:
    """The count an operator needs before accepting: recorded evidence lost."""
    staging, library = roots
    write(staging / "Series A" / "Ch 1.cbz", 5_000_000)
    write(library / "Series A" / "Ch 1.cbz", 1_000_000)

    plan = plan_series(staging / "Series A", library / "Series A")

    assert len(plan.replacing) == 1
    target, existing, incoming = plan.replacing[0]
    assert (existing, incoming) == (1_000_000, 5_000_000)
    assert plan.new_files == []


def test_a_marginal_gain_is_reported_as_kept(roots) -> None:
    """Promotion inherits the watcher's 10 KB rule rather than restating it."""
    staging, library = roots
    write(staging / "Series A" / "Ch 1.cbz", 1_000_250)
    write(library / "Series A" / "Ch 1.cbz", 1_000_000)

    plan = plan_series(staging / "Series A", library / "Series A")

    assert len(plan.kept_below_threshold) == 1
    assert plan.replacing == []


def test_a_smaller_incoming_archive_is_reported_as_not_larger(roots) -> None:
    staging, library = roots
    write(staging / "Series A" / "Ch 1.cbz", 500)
    write(library / "Series A" / "Ch 1.cbz", 1_000_000)

    plan = plan_series(staging / "Series A", library / "Series A")

    assert len(plan.kept_not_larger) == 1
    assert plan.replacing == []


def test_the_plan_matches_what_apply_actually_does(roots) -> None:
    """The report and the action must not be able to disagree.

    Predicted counts are compared against the library's real state after the
    move, not against a second prediction.
    """
    staging, library = roots
    write(staging / "Series A" / "Ch 1.cbz", 5_000_000)   # replaces
    write(staging / "Series A" / "Ch 2.cbz", 1_000_250)   # below threshold
    write(staging / "Series A" / "Ch 3.cbz", 400)         # smaller
    write(staging / "Series A" / "Ch 4.cbz", 7_000)       # new
    write(library / "Series A" / "Ch 1.cbz", 1_000_000)
    write(library / "Series A" / "Ch 2.cbz", 1_000_000)
    write(library / "Series A" / "Ch 3.cbz", 1_000_000)

    predicted = plan_series(staging / "Series A", library / "Series A")
    assert len(predicted.replacing) == 1
    assert len(predicted.kept_below_threshold) == 1
    assert len(predicted.kept_not_larger) == 1
    assert len(predicted.new_files) == 1

    promote(staging, library, apply=True)

    assert (library / "Series A" / "Ch 1.cbz").stat().st_size == 5_000_000
    assert (library / "Series A" / "Ch 2.cbz").stat().st_size == 1_000_000
    assert (library / "Series A" / "Ch 3.cbz").stat().st_size == 1_000_000
    assert (library / "Series A" / "Ch 4.cbz").stat().st_size == 7_000


# --- read-only by default ------------------------------------------------


def test_without_apply_nothing_moves(roots) -> None:
    staging, library = roots
    write(staging / "Series A" / "Ch 1.cbz", 5_000)

    promote(staging, library)

    assert (staging / "Series A" / "Ch 1.cbz").exists()
    assert not (library / "Series A").exists()


# --- merging, not refusing ----------------------------------------------


def test_promotion_merges_into_an_existing_series(roots) -> None:
    """The reason promote_case() could not be reused.

    Routine promotion is mostly new chapters joining a series that already
    exists; a tool that refused an occupied destination would refuse nearly
    every time.
    """
    staging, library = roots
    write(staging / "Series A" / "Ch 2.cbz", 5_000)
    write(library / "Series A" / "Ch 1.cbz", 5_000)

    promote(staging, library, apply=True)

    assert (library / "Series A" / "Ch 1.cbz").exists()
    assert (library / "Series A" / "Ch 2.cbz").exists()


# --- source cleanup ------------------------------------------------------


def test_an_emptied_series_directory_is_removed(roots) -> None:
    staging, library = roots
    write(staging / "Series A" / "Ch 1.cbz", 5_000)

    promote(staging, library, apply=True)

    assert not (staging / "Series A").exists()


def test_a_directory_still_holding_a_file_is_kept(tmp_path: Path) -> None:
    """Anything the merge declined to move is a file without a decision.

    Removing its directory would discard it silently, so cleanup only ever
    removes a tree that is genuinely empty.
    """
    kept = tmp_path / "kept"
    write(kept / "nested" / "left-behind.cbz", 10)

    assert _remove_if_empty(kept) is False
    assert (kept / "nested" / "left-behind.cbz").exists()


def test_an_empty_tree_is_removed_including_nested_directories(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty"
    (empty / "nested" / "deeper").mkdir(parents=True)

    assert _remove_if_empty(empty) is True
    assert not empty.exists()


def test_limit_bounds_how_many_series_are_promoted(roots) -> None:
    staging, library = roots
    for index in range(4):
        write(staging / ("Series %d" % index) / "Ch 1.cbz", 5_000)

    plans = promote(staging, library, apply=True, limit=2)

    assert len(plans) == 2
    assert sum(1 for p in staged_series(staging)) == 2


# --- reporting -----------------------------------------------------------


def test_the_summary_names_the_cost_of_replacing(roots) -> None:
    staging, library = roots
    write(staging / "Series A" / "Ch 1.cbz", 5_000_000)
    write(library / "Series A" / "Ch 1.cbz", 1_000_000)

    plans = promote(staging, library)
    output = render(plans, applied=False)

    assert "REPLACES" in output
    assert "invalidates recorded evidence" in output
    assert format(REPLACEMENT_MIN_GAIN_BYTES, ",") in output
