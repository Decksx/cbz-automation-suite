"""Tests for the retiring-library reclassification tool.

The apply step moves live library content, so the tests that matter most are
the refusals. Each guard exists because acting on a stale plan is how a
migration silently does the wrong thing to 50 GB of comics:

* the digest catches a source tree that changed after the plan was reviewed,
  including changes that leave the series count identical;
* --expect-series makes the operator state what they reviewed, so a plan
  cannot be applied to a tree that grew or shrank;
* a collision is quarantined, never overwritten, so no file is lost to a
  merge.
"""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path

import pytest

from scripts.cbz_library_reclassify import (
    ApplyStats,
    apply_series,
    cmd_apply,
    sample_archives,
    tree_digest,
)


def _cbz(path: Path, page: bytes = b"page") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("ComicInfo.xml", "<ComicInfo><Series>S</Series></ComicInfo>")
        zf.writestr("001.jpg", page)


def _library(root: Path, layout: dict[str, int]) -> Path:
    for series, count in layout.items():
        for i in range(count):
            _cbz(root / series / f"ch{i:02d}.cbz")
    return root


# ── digest ───────────────────────────────────────────────────────


def test_digest_is_stable_when_nothing_changes(tmp_path: Path):
    source = _library(tmp_path / "src", {"A": 2, "B": 1})
    assert tree_digest(source) == tree_digest(source)


def test_digest_reports_series_and_file_counts(tmp_path: Path):
    source = _library(tmp_path / "src", {"A": 2, "B": 3})
    _, series, files = tree_digest(source)
    assert (series, files) == (2, 5)


def test_digest_changes_when_a_file_is_added(tmp_path: Path):
    source = _library(tmp_path / "src", {"A": 2})
    before = tree_digest(source)[0]
    _cbz(source / "A" / "ch99.cbz")
    assert tree_digest(source)[0] != before


def test_digest_changes_when_a_file_is_resized(tmp_path: Path):
    # Same series count, same file count -- only the bytes differ. This is
    # the case --expect-series cannot catch on its own.
    source = _library(tmp_path / "src", {"A": 2})
    before = tree_digest(source)[0]
    _cbz(source / "A" / "ch00.cbz", page=b"a much larger page payload" * 50)
    digest, series, files = tree_digest(source)
    assert (series, files) == (1, 2)
    assert digest != before


def test_digest_changes_when_a_series_is_renamed(tmp_path: Path):
    source = _library(tmp_path / "src", {"A": 1, "B": 1})
    before = tree_digest(source)[0]
    (source / "B").rename(source / "C")
    assert tree_digest(source)[0] != before


# ── sampling ─────────────────────────────────────────────────────


def test_sampling_spreads_across_the_series(tmp_path: Path):
    # Reading only the first N misses metadata that exists on later
    # chapters, which is common when a back catalogue was added untagged.
    source = _library(tmp_path / "src", {"A": 20})
    picked = sample_archives(source / "A", 4)
    names = [p.name for p in picked]
    assert len(picked) == 4
    assert names[0] == "ch00.cbz"
    assert names[-1] != "ch03.cbz"


def test_sampling_returns_everything_when_under_the_limit(tmp_path: Path):
    source = _library(tmp_path / "src", {"A": 3})
    assert len(sample_archives(source / "A", 10)) == 3


# ── apply guards ─────────────────────────────────────────────────


def _plan_file(tmp_path: Path, source: Path, dest: Path,
               rows: list[dict]) -> Path:
    digest, series, files = tree_digest(source)
    plan = {
        "source": str(source),
        "digest": digest,
        "series_count": series,
        "file_count": files,
        "destinations": {"graphic_novels": str(dest)},
        "series": rows,
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path


def _row(series: str, dest_dir: Path, files: int) -> dict:
    return {
        "series": series, "dest_key": "graphic_novels", "reason": "",
        "reason_rule": "default", "file_count": files, "total_bytes": 0,
        "sampled": 1, "with_comicinfo": 1, "target_exists": dest_dir.exists(),
        "target_dir": str(dest_dir), "canonical": series,
        "needs_review": False, "evidence": {},
    }


def _args(plan: Path, tmp_path: Path, **kw) -> argparse.Namespace:
    base = dict(plan=str(plan), expect_series=1,
                quarantine=str(tmp_path / "q"), skip_review=False,
                limit=None, confirm=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_apply_refuses_without_expected_count(tmp_path: Path):
    source = _library(tmp_path / "src", {"A": 1})
    dest = tmp_path / "dst"
    dest.mkdir()
    plan = _plan_file(tmp_path, source, dest, [_row("A", dest / "A", 1)])
    with pytest.raises(SystemExit, match="expect-series is required"):
        cmd_apply(_args(plan, tmp_path, expect_series=None))


def test_apply_refuses_on_count_mismatch(tmp_path: Path):
    source = _library(tmp_path / "src", {"A": 1})
    dest = tmp_path / "dst"
    dest.mkdir()
    plan = _plan_file(tmp_path, source, dest, [_row("A", dest / "A", 1)])
    with pytest.raises(SystemExit, match="but found"):
        cmd_apply(_args(plan, tmp_path, expect_series=7))


def test_apply_refuses_when_the_tree_changed_after_planning(tmp_path: Path):
    source = _library(tmp_path / "src", {"A": 1})
    dest = tmp_path / "dst"
    dest.mkdir()
    plan = _plan_file(tmp_path, source, dest, [_row("A", dest / "A", 1)])
    _cbz(source / "A" / "sneaky.cbz")        # changed after review
    with pytest.raises(SystemExit, match="has changed since this plan"):
        cmd_apply(_args(plan, tmp_path, expect_series=1))


def test_apply_refuses_when_a_destination_is_missing(tmp_path: Path):
    source = _library(tmp_path / "src", {"A": 1})
    dest = tmp_path / "dst"
    dest.mkdir()
    plan = _plan_file(tmp_path, source, dest, [_row("A", dest / "A", 1)])
    dest.rmdir()
    with pytest.raises(SystemExit, match="destination"):
        cmd_apply(_args(plan, tmp_path, expect_series=1))


def test_dry_run_moves_nothing(tmp_path: Path, capsys):
    source = _library(tmp_path / "src", {"A": 2})
    dest = tmp_path / "dst"
    dest.mkdir()
    plan = _plan_file(tmp_path, source, dest, [_row("A", dest / "A", 2)])

    cmd_apply(_args(plan, tmp_path, expect_series=1, confirm=False))

    assert (source / "A").is_dir()
    assert not (dest / "A").exists()
    assert "Nothing was changed" in capsys.readouterr().out


def test_confirm_moves_the_series(tmp_path: Path):
    source = _library(tmp_path / "src", {"A": 2})
    dest = tmp_path / "dst"
    dest.mkdir()
    plan = _plan_file(tmp_path, source, dest, [_row("A", dest / "A", 2)])

    cmd_apply(_args(plan, tmp_path, expect_series=1, confirm=True))

    assert not (source / "A").exists()
    assert len(list((dest / "A").glob("*.cbz"))) == 2


# ── merging never destroys ───────────────────────────────────────


def test_merge_quarantines_a_collision_instead_of_overwriting(tmp_path: Path):
    source = _library(tmp_path / "src", {"A": 1})
    dest_series = tmp_path / "dst" / "A"
    _cbz(dest_series / "ch00.cbz", page=b"the copy already in the library")
    original = (dest_series / "ch00.cbz").read_bytes()
    quarantine = tmp_path / "q"

    stats = ApplyStats()
    apply_series(_row("A", dest_series, 1), source, quarantine, stats,
                 dry_run=False)

    # The existing file is untouched and the incoming one is preserved.
    assert (dest_series / "ch00.cbz").read_bytes() == original
    assert (quarantine / "A" / "ch00.cbz").exists()
    assert stats.files_quarantined == 1
    assert stats.files_moved == 0


def test_merge_moves_non_colliding_files_through(tmp_path: Path):
    source = _library(tmp_path / "src", {"A": 0})
    _cbz(source / "A" / "new.cbz")
    dest_series = tmp_path / "dst" / "A"
    _cbz(dest_series / "existing.cbz")

    stats = ApplyStats()
    apply_series(_row("A", dest_series, 1), source, tmp_path / "q", stats,
                 dry_run=False)

    assert (dest_series / "new.cbz").exists()
    assert (dest_series / "existing.cbz").exists()
    assert stats.files_moved == 1
    assert stats.files_quarantined == 0


def test_missing_source_directory_is_an_error_not_a_crash(tmp_path: Path):
    stats = ApplyStats()
    apply_series(_row("Gone", tmp_path / "dst" / "Gone", 1),
                 tmp_path / "src", tmp_path / "q", stats, dry_run=False)
    assert stats.errors == 1
    assert stats.files_moved == 0


def test_skip_review_holds_back_flagged_rows(tmp_path: Path):
    source = _library(tmp_path / "src", {"A": 1, "B": 1})
    dest = tmp_path / "dst"
    dest.mkdir()
    flagged = _row("B", dest / "B", 1)
    flagged["needs_review"] = True
    plan = _plan_file(tmp_path, source, dest,
                      [_row("A", dest / "A", 1), flagged])

    cmd_apply(_args(plan, tmp_path, expect_series=2, skip_review=True,
                    confirm=True))

    assert (dest / "A").exists()
    assert not (dest / "B").exists()
    assert (source / "B").is_dir()
