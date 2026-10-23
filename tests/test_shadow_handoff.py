"""The staging shadow-plan (design A3.1): observed, never acted on.

Two layers. The pure policy (`decide_handoff`) is pinned case by case, because
its order is the policy: a classification error outranks a mixed parent, and
both outrank the review trigger. The watcher wiring is then driven through
real processing, and every wiring test asserts the same thing from a
different angle: whatever the handoff decision says, the legacy move runs
exactly as it would with shadow off, and the review root stays empty.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts import cbz_shadow_handoff as handoff
from scripts import cbz_watcher as watcher
from scripts.cbz_classification_staging import plan_case
from scripts.cbz_routing import RoutingDecision, series_key
from tests.test_watcher_shadow_integration import _cbz, _wire


def _decision(**overrides) -> RoutingDecision:
    fields = dict(dest_key="manga", dest_path="M", rule_name="origin",
                  reason="test", confidence="resolved")
    fields.update(overrides)
    return RoutingDecision(**fields)


def _tree(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


def _handoff_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if "[handoff:" in r.getMessage()]


@pytest.fixture(autouse=True)
def _no_ambient_review_root(monkeypatch):
    # The operator's environment must not decide what these tests exercise.
    monkeypatch.delenv(handoff.REVIEW_ROOT_ENV, raising=False)


# ── the pure policy ──────────────────────────────────────────────


def test_classification_error_defers():
    h = handoff.decide_handoff(Path("A"), None, mixed_parent=False,
                               archives=[], error="RuntimeError: boom")
    assert (h.outcome, h.reason) == ("defer", "classification_failed")
    assert h.error == "RuntimeError: boom"


def test_a_missing_decision_without_an_error_still_defers():
    # The router returning nothing is not evidence the arrival is routine.
    h = handoff.decide_handoff(Path("A"), None, mixed_parent=False, archives=[])
    assert (h.outcome, h.reason) == ("defer", "classification_failed")


def test_classification_error_outranks_mixed_parent():
    h = handoff.decide_handoff(Path("A"), None, mixed_parent=True,
                               archives=[Path("A/x.cbz")], error="boom")
    assert h.reason == "classification_failed"


def test_mixed_parent_defers_even_when_the_series_is_ambiguous():
    # No review handoff can stage a selective payload yet (design A2).
    h = handoff.decide_handoff(Path("A"), _decision(ambiguous_series=True),
                               mixed_parent=True, archives=[Path("A/x.cbz")])
    assert (h.outcome, h.reason) == ("defer", "mixed_parent")
    assert h.payload == (str(Path("A/x.cbz")),)


def test_ambiguous_series_is_a_review_handoff():
    h = handoff.decide_handoff(Path("A"), _decision(ambiguous_series=True),
                               mixed_parent=False, archives=[])
    assert (h.outcome, h.reason) == ("review_handoff", "ambiguous_series")
    assert h.payload == (str(Path("A")),)


def test_unresolved_classification_is_a_review_handoff():
    h = handoff.decide_handoff(Path("A"), _decision(confidence="unresolved"),
                               mixed_parent=False, archives=[])
    assert (h.outcome, h.reason) == ("review_handoff", "unresolved")


def test_authoritative_false_alone_is_not_a_review_handoff():
    h = handoff.decide_handoff(Path("A"), _decision(authoritative=False),
                               mixed_parent=False, archives=[])
    assert h.outcome == "legacy_move"


# ── planning, read-only ──────────────────────────────────────────


def test_missing_or_invalid_review_root_logs_and_does_nothing(
        tmp_path, monkeypatch):
    def must_not_plan(*a, **k):
        raise AssertionError("planned without a usable review root")

    monkeypatch.setattr(handoff, "plan_case", must_not_plan)
    arrival = tmp_path / "Arrival"
    _cbz(arrival / "a.cbz")
    unresolved = _decision(confidence="unresolved")

    unset = handoff.observe(arrival, "Arrival", "Arrival", unresolved,
                            mixed_parent=False, archives=[])
    assert unset.plan_status == "review_root_unset"

    missing = tmp_path / "no-such-root"
    unavailable = handoff.observe(arrival, "Arrival", "Arrival", unresolved,
                                  mixed_parent=False, archives=[],
                                  review_root=missing)
    assert unavailable.plan_status == "review_root_unavailable"
    assert not missing.exists(), "a missing review root was created"


def test_a_plan_failure_is_recorded_not_raised(tmp_path, monkeypatch):
    def refuse(*a, **k):
        raise OSError("disk unhappy")

    monkeypatch.setattr(handoff, "plan_case", refuse)
    root = tmp_path / "review"
    root.mkdir()
    arrival = tmp_path / "Arrival"
    _cbz(arrival / "a.cbz")
    obs = handoff.observe(arrival, "Arrival", "Arrival",
                          _decision(confidence="unresolved"),
                          mixed_parent=False, archives=[], review_root=root)
    assert obs.plan_status == "plan_failed"
    assert "OSError: disk unhappy" in obs.detail


def test_summary_counts_defer_by_reason():
    def obs(outcome, reason, status="not_applicable"):
        return handoff.HandoffObservation(
            "S", handoff.HandoffDecision(outcome, reason, None, ()), status)

    line = handoff.summarise([
        obs("legacy_move", ""),
        obs("review_handoff", "unresolved", "planned"),
        obs("review_handoff", "ambiguous_series", "review_root_unset"),
        obs("defer", "mixed_parent"),
        obs("defer", "classification_failed"),
        obs("defer", "mixed_parent"),
    ])
    assert "6 observed" in line
    assert "1 legacy_move" in line
    assert "2 review_handoff (1 planned)" in line
    assert "3 defer (classification_failed 1, mixed_parent 2)" in line


# ── wired into the watcher ───────────────────────────────────────


def test_shadow_plan_logs_would_be_case_and_moves_nothing_extra(
        tmp_path, monkeypatch, caplog):
    # "Moves nothing" in shadow means: nothing beyond the legacy move. The
    # arrival is untagged, so v2 leaves it unresolved -- a would-be handoff.
    watch_root, dest_root, _, _ = _wire(monkeypatch, tmp_path)
    review = tmp_path / "review"
    review.mkdir()
    monkeypatch.setenv(handoff.REVIEW_ROOT_ENV, str(review))
    arrival = watch_root / "Untagged Series"
    _cbz(arrival / "Untagged Series.cbz")
    # The real planner, observed. The expected case cannot be computed before
    # processing (the watcher injects ComicInfo first, changing the payload)
    # or after it (the arrival has moved, and the path is part of the case).
    plans = []

    def spy(*args, **kwargs):
        plans.append((args, plan_case(*args, **kwargs)))
        return plans[-1][1]

    monkeypatch.setattr(handoff, "plan_case", spy)

    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(arrival)

    assert len(plans) == 1
    args, expected = plans[0]
    assert args[:6] == (review, arrival, arrival, "Untagged Series",
                        series_key("Untagged Series"), "Untagged Series")
    lines = _handoff_lines(caplog)
    assert len(lines) == 1
    assert "[handoff:review_handoff]" in lines[0]
    assert "plan=planned" in lines[0]
    assert f"case={expected.case_id[:16]}" in lines[0]
    assert f"inventory={expected.inventory.digest[:16]}" in lines[0]
    assert "files=1" in lines[0]
    assert _tree(review) == [], "the shadow plan wrote into the review root"
    assert (dest_root / "Untagged Series" / "Untagged Series.cbz").exists()
    summaries = [r.getMessage() for r in caplog.records
                 if "Staging shadow-plan:" in r.getMessage()]
    assert summaries and "1 review_handoff (1 planned)" in summaries[-1]


def test_classification_error_defers_and_the_legacy_move_still_runs(
        tmp_path, monkeypatch, caplog):
    watch_root, dest_root, _, router = _wire(monkeypatch, tmp_path)

    def boom(*a, **k):
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr(router, "shadow", boom)
    arrival = watch_root / "Doomed"
    _cbz(arrival / "Doomed.cbz")

    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(arrival)

    lines = _handoff_lines(caplog)
    assert len(lines) == 1 and "reason=classification_failed" in lines[0]
    assert "classifier exploded" in lines[0]
    assert (dest_root / "Doomed" / "Doomed.cbz").exists()


def test_mixed_parent_with_loose_files_and_unrelated_nested_series_defers(
        tmp_path, monkeypatch, caplog):
    watch_root, _, _, _ = _wire(monkeypatch, tmp_path)
    review = tmp_path / "review"
    review.mkdir()
    monkeypatch.setenv(handoff.REVIEW_ROOT_ENV, str(review))
    drop = watch_root / "SomeSource"
    _cbz(drop / "Loose Title.cbz")
    _cbz(drop / "Nested Series" / "Nested Series.cbz", LanguageISO="ja")

    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(drop)

    mixed = [l for l in _handoff_lines(caplog) if "reason=mixed_parent" in l]
    assert len(mixed) == 1, _handoff_lines(caplog)
    assert "Nested" not in mixed[0]
    assert _tree(review) == []


def test_shadow_handoff_failure_never_stops_processing(
        tmp_path, monkeypatch, caplog):
    watch_root, dest_root, _, _ = _wire(monkeypatch, tmp_path)

    def boom(*a, **k):
        raise RuntimeError("observer exploded")

    monkeypatch.setattr(handoff, "observe", boom)
    arrival = watch_root / "Resilient"
    _cbz(arrival / "Resilient.cbz")

    with caplog.at_level("INFO"):
        watcher.process_and_move_directory(arrival)

    assert (dest_root / "Resilient" / "Resilient.cbz").exists()
    assert any("Staging shadow-plan failed" in r.getMessage()
               for r in caplog.records)
    summaries = [r.getMessage() for r in caplog.records
                 if "Staging shadow-plan:" in r.getMessage()]
    assert summaries and "no arrivals observed" in summaries[-1]


def _populate(scenario: str, watch_root: Path) -> Path:
    if scenario == "mixed_parent":
        drop = watch_root / "SomeSource"
        _cbz(drop / "Loose Title.cbz")
        _cbz(drop / "Nested Series" / "Nested Series.cbz", LanguageISO="ja")
        return drop
    if scenario == "legacy_move":
        arrival = watch_root / "Tagged"
        _cbz(arrival / "Tagged.cbz", LanguageISO="ja")
        return arrival
    arrival = watch_root / "Untagged"                # review_handoff / error
    _cbz(arrival / "Untagged.cbz")
    return arrival


@pytest.mark.parametrize("scenario", [
    "legacy_move", "review_handoff", "classification_failed", "mixed_parent"])
def test_shadow_decision_never_changes_the_existing_move(
        tmp_path, monkeypatch, caplog, scenario):
    """Every outcome leaves the filesystem exactly as shadow-off would.

    Run once with v2 off, once with shadow on and a review root configured,
    and compare the whole resulting tree. A handoff that suppressed, redirected
    or duplicated any movement shows up as a difference here.
    """
    trees = []
    for run in ("off", "shadow"):
        base = tmp_path / run
        base.mkdir()
        watch_root, _, _, router = _wire(monkeypatch, base)
        review = base / "review"
        review.mkdir()
        if run == "off":
            monkeypatch.setattr(watcher, "_router", None)
        else:
            monkeypatch.setenv(handoff.REVIEW_ROOT_ENV, str(review))
            if scenario == "classification_failed":
                def boom(*a, **k):
                    raise RuntimeError("classifier exploded")
                monkeypatch.setattr(router, "shadow", boom)
        with caplog.at_level("INFO"):
            watcher.process_and_move_directory(_populate(scenario, watch_root))
        trees.append(_tree(base))
        monkeypatch.delenv(handoff.REVIEW_ROOT_ENV, raising=False)

    assert trees[0] == trees[1]
    assert trees[1], "nothing was processed; the comparison is vacuous"
    # The scenario really produced the outcome it is named for; otherwise
    # this would compare four runs of the same path.
    outcome = {"legacy_move": "[handoff:legacy_move]",
               "review_handoff": "plan=planned",
               "classification_failed": "reason=classification_failed",
               "mixed_parent": "reason=mixed_parent"}[scenario]
    assert any(outcome in line for line in _handoff_lines(caplog))
