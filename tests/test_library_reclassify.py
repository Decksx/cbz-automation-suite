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
    cmd_move,
    plan_series,
    sample_archives,
    tree_digest,
)
from scripts.cbz_routing import parse


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


# ── sampled decision precedence ──────────────────────────────────
#
# Origin is a property of a series, not of a chapter, so the sample ranks
# strong > weak > no match and a chapter that arrived untagged must not
# dilute the evidence of one that carries it. The regression these guard
# is that an unmatched RoutingDecision is truthy, which once let the first
# sample's no-match suppress every later weak match.


def _cbz_meta(path: Path, **fields: str) -> None:
    """A CBZ carrying exactly *fields*; no fields means no ComicInfo at all."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"<{k}>{v}</{k}>" for k, v in fields.items())
    with zipfile.ZipFile(path, "w") as zf:
        if body:
            zf.writestr("ComicInfo.xml", f"<ComicInfo>{body}</ComicInfo>")
        zf.writestr("001.jpg", b"page")


NO_MATCH: dict[str, str] = {}
WEAK = {"Genre": "seinen"}
STRONG = {"LanguageISO": "ja"}


def _origin_cfg(tmp_path: Path, strong_name="Asian origin (strong)",
                weak_name="Asian origin (weak)"):
    """Two rules declaring the strength plan_series ranks on.

    The names are parameters so a test can prove that ranking follows the
    declared strength and not the display name.
    """
    return parse({
        "version": 2,
        "destinations": {"manga": str(tmp_path / "manga"),
                         "graphic_novels": str(tmp_path / "gn")},
        "default": "graphic_novels",
        "lists": {"asian_languages": ["ja"]},
        "signals": {
            "asian_origin_strong": {
                "any": [{"field": "comicinfo.LanguageISO",
                         "in_list": "asian_languages"}]},
            "asian_origin_weak": {
                "any": [{"field": "comicinfo.Genre",
                         "contains_any": ["seinen"]}]},
        },
        "rules": [
            {"name": strong_name, "when": "asian_origin_strong",
             "dest": "manga", "strength": "strong"},
            {"name": weak_name, "when": "asian_origin_weak",
             "dest": "manga", "strength": "weak"},
        ],
    })


def _plan_one(tmp_path: Path, samples: list[dict], cfg=None):
    """Plan one series whose chapters carry *samples* in that order.

    The sample limit exceeds the chapter count, so sample_archives returns
    every chapter in name order and the list reads as the evaluation order.
    """
    series_dir = tmp_path / "src" / "Series"
    for index, fields in enumerate(samples):
        _cbz_meta(series_dir / f"ch{index:02d}.cbz", **fields)
    roots = {"manga": tmp_path / "manga", "graphic_novels": tmp_path / "gn"}
    for root in roots.values():
        root.mkdir(parents=True, exist_ok=True)
    return plan_series(series_dir, cfg or _origin_cfg(tmp_path), "src", 5, roots)


def test_later_weak_match_beats_an_earlier_no_match(tmp_path: Path):
    # The regression: `decision = decision or candidate` held the no-match.
    row = _plan_one(tmp_path, [NO_MATCH, WEAK])
    assert row.dest_key == "manga"
    assert "weak" in row.reason_rule


def test_later_strong_match_beats_an_earlier_weak_match(tmp_path: Path):
    row = _plan_one(tmp_path, [WEAK, STRONG])
    assert row.dest_key == "manga"
    assert "strong" in row.reason_rule


def test_no_match_anywhere_falls_through_to_the_default(tmp_path: Path):
    row = _plan_one(tmp_path, [NO_MATCH, NO_MATCH, NO_MATCH])
    assert row.dest_key == "graphic_novels"
    assert row.reason_rule == "default"


def test_repeated_weak_matches_do_not_fall_back_to_the_default(tmp_path: Path):
    row = _plan_one(tmp_path, [WEAK, WEAK])
    assert row.dest_key == "manga"
    assert "weak" in row.reason_rule

    # ... and still not once an untagged chapter is sampled first.
    other = _plan_one(tmp_path / "second", [NO_MATCH, WEAK, WEAK])
    assert other.dest_key == "manga"
    assert "weak" in other.reason_rule


def test_later_no_match_cannot_displace_an_earlier_weak_match(tmp_path: Path):
    # Guards the opposite error from the regression: ranking must not be
    # rewritten as "last sample wins".
    row = _plan_one(tmp_path, [WEAK, NO_MATCH])
    assert row.dest_key == "manga"
    assert row.reason_rule == "Asian origin (weak)"


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


# -- single-series move ------------------------------------------

def _move_args(tmp_path: Path, series: str, src: Path, dst: Path,
               confirm: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        series=series, from_root=str(src), to_root=str(dst),
        quarantine=str(tmp_path / "q"), confirm=confirm,
    )


def test_move_relocates_a_series_between_libraries(tmp_path: Path):
    src = _library(tmp_path / "manga", {"Ice Cream Man": 2})
    dst = tmp_path / "gn"
    dst.mkdir()

    cmd_move(_move_args(tmp_path, "Ice Cream Man", src, dst, confirm=True))

    assert not (src / "Ice Cream Man").exists()
    assert len(list((dst / "Ice Cream Man").glob("*.cbz"))) == 2


def test_move_matches_the_series_name_after_normalisation(tmp_path: Path):
    # Punctuation and casing differences must not stop a manual fix.
    src = _library(tmp_path / "manga", {"Ice Cream Man": 1})
    dst = tmp_path / "gn"
    dst.mkdir()

    cmd_move(_move_args(tmp_path, "ice-cream man!", src, dst, confirm=True))

    assert (dst / "Ice Cream Man").is_dir()


def test_move_merges_without_overwriting(tmp_path: Path):
    src = _library(tmp_path / "manga", {"A": 0})
    _cbz(src / "A" / "vol1.cbz")
    dst = tmp_path / "gn"
    _cbz(dst / "A" / "ch01.cbz", page=b"already here")
    keep = (dst / "A" / "ch01.cbz").read_bytes()

    cmd_move(_move_args(tmp_path, "A", src, dst, confirm=True))

    assert (dst / "A" / "vol1.cbz").exists()
    assert (dst / "A" / "ch01.cbz").read_bytes() == keep


def test_move_dry_run_changes_nothing(tmp_path: Path):
    src = _library(tmp_path / "manga", {"A": 1})
    dst = tmp_path / "gn"
    dst.mkdir()

    cmd_move(_move_args(tmp_path, "A", src, dst, confirm=False))

    assert (src / "A").is_dir()
    assert not (dst / "A").exists()


def test_move_refuses_when_the_series_is_absent(tmp_path: Path):
    src = _library(tmp_path / "manga", {"A": 1})
    dst = tmp_path / "gn"
    dst.mkdir()
    with pytest.raises(SystemExit, match="no series matching"):
        cmd_move(_move_args(tmp_path, "Nonexistent", src, dst, confirm=True))
