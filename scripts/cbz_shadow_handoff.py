"""Staging shadow-plan: what a review handoff *would* do. Moves nothing.

Phase A3.1 of docs/staging_integration_and_rollback_design.md. The watcher's
v2 shadow already logs how v2 would classify each arrival; this module adds
the next observation: given that classification, would the arrival be handed
to classification staging for review, moved by the legacy path, or deferred,
and -- for a would-be handoff -- which staging case it would become.

Everything here is an observation. The watcher's existing move runs exactly
as before whatever the decision says (`_move_loose_files` for a mixed parent,
the whole-directory move otherwise). "Defer" and "review handoff" describe a
future, separately approved activation; a change that let them suppress any
movement would be a behaviour change needing its own gate, not this module.

The staging planner is called read-only (`plan_case`): it hashes the payload
and reads the review root, but creates no `.partial`, no payload and no
manifest. It runs only when the review root is configured and exists; the
full configuration gate (same volume as the library, not inside the watch
folder or a library root) is phase A3.2 and is not implemented here.
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from scripts.cbz_classification_staging import plan_case
from scripts.cbz_routing import series_key

log = logging.getLogger(__name__)

OUTCOME_LEGACY_MOVE = "legacy_move"
OUTCOME_REVIEW_HANDOFF = "review_handoff"
OUTCOME_DEFER = "defer"

# Why an arrival is deferred. Only these two exist today; a new reason is a
# policy change and gets its own review, not a new string at a call site.
DEFER_CLASSIFICATION_FAILED = "classification_failed"
DEFER_MIXED_PARENT = "mixed_parent"

# Why an arrival would be handed to review (design A1).
REVIEW_AMBIGUOUS_SERIES = "ambiguous_series"
REVIEW_UNRESOLVED = "unresolved"

# Whether the read-only plan ran for a would-be handoff, and if not, why.
PLAN_NOT_APPLICABLE = "not_applicable"
PLAN_REVIEW_ROOT_UNSET = "review_root_unset"
PLAN_REVIEW_ROOT_UNAVAILABLE = "review_root_unavailable"
PLAN_PLANNED = "planned"
PLAN_FAILED = "plan_failed"

# No default, deliberately: a review root is a placement decision (design
# A3.2), and a guessed one would put staged payloads somewhere nobody chose.
REVIEW_ROOT_ENV = "CBZ_CLASSIFICATION_REVIEW_ROOT"


@dataclass(frozen=True)
class HandoffDecision:
    """What the watcher would do with one arrival under a staging handoff.

    `payload` is what the handoff would own. For a whole-directory arrival it
    is the directory itself, because `plan_case` inventories the whole tree
    and the inventory is the authority on its members. For a mixed parent it
    is the loose archives only -- recorded so the deferral is explainable,
    since no contract yet exists to stage a selective payload (design A2).
    """

    outcome: str
    reason: str
    decision: object | None
    payload: tuple[str, ...]
    error: str = ""


def decide_handoff(comic_dir: Path, decision, *, mixed_parent: bool,
                   archives: list[Path], error: str = "") -> HandoffDecision:
    """Pure policy: classify one arrival as legacy_move, review_handoff or defer.

    Order matters. A classification error is checked first, so a failed
    classification is never reported as a mixed-parent deferral that merely
    happened to fail too: the error is the thing an operator must see. A mixed
    parent is checked before the review policy, because no review handoff can
    stage its payload correctly however the classification came out.

    `authoritative == False` alone is not a review trigger: it also covers
    resolved metadata classifications, and treating it as one would send
    ordinary arrivals to review.
    """
    if mixed_parent:
        payload = tuple(str(a) for a in archives)
    else:
        payload = (str(comic_dir),)

    if error or decision is None:
        return HandoffDecision(OUTCOME_DEFER, DEFER_CLASSIFICATION_FAILED,
                               decision, payload,
                               error or "no classification produced")
    if mixed_parent:
        return HandoffDecision(OUTCOME_DEFER, DEFER_MIXED_PARENT, decision,
                               payload)
    if decision.ambiguous_series:
        return HandoffDecision(OUTCOME_REVIEW_HANDOFF, REVIEW_AMBIGUOUS_SERIES,
                               decision, payload)
    if decision.confidence == "unresolved":
        return HandoffDecision(OUTCOME_REVIEW_HANDOFF, REVIEW_UNRESOLVED,
                               decision, payload)
    return HandoffDecision(OUTCOME_LEGACY_MOVE, "", decision, payload)


@dataclass(frozen=True)
class HandoffObservation:
    """One arrival's handoff decision and, for a would-be handoff, its plan."""

    series: str
    handoff: HandoffDecision
    plan_status: str
    case_id: str = ""
    inventory_digest: str = ""
    file_count: int = 0
    method: str = ""
    detail: str = ""

    def describe(self) -> str:
        h = self.handoff
        parts = [f"[handoff:{h.outcome}]", repr(self.series)]
        if h.reason:
            parts.append(f"reason={h.reason}")
        if h.outcome == OUTCOME_REVIEW_HANDOFF:
            parts.append(f"plan={self.plan_status}")
        if self.plan_status == PLAN_PLANNED:
            parts.append(f"case={self.case_id[:16]} "
                         f"inventory={self.inventory_digest[:16]} "
                         f"files={self.file_count} method={self.method}")
        if h.error:
            parts.append(f"error={h.error}")
        if self.detail:
            parts.append(f"detail={self.detail}")
        return " ".join(parts)


def configured_review_root() -> Path | None:
    """The review root from the environment, or None when unset."""
    value = os.environ.get(REVIEW_ROOT_ENV, "").strip()
    return Path(value) if value else None


def observe(comic_dir: Path, series_name: str, source_name: str, decision, *,
            mixed_parent: bool, archives: list[Path], error: str = "",
            review_root: Path | None = None) -> HandoffObservation:
    """Decide, and for a would-be review handoff plan the case read-only.

    Never raises for a planning failure: shadow must not be able to stop the
    watcher, and a plan that cannot be computed is itself the observation.
    The payload is hashed in full, so this costs one read of the arrival --
    only for would-be handoffs, and only when a review root is configured.
    """
    handoff = decide_handoff(comic_dir, decision, mixed_parent=mixed_parent,
                             archives=archives, error=error)
    if handoff.outcome != OUTCOME_REVIEW_HANDOFF:
        return HandoffObservation(series_name, handoff, PLAN_NOT_APPLICABLE)

    root = review_root if review_root is not None else configured_review_root()
    if root is None:
        return HandoffObservation(series_name, handoff, PLAN_REVIEW_ROOT_UNSET)
    # Checked here rather than left to plan_case, because plan_case would
    # still hash the whole payload before discovering the root is missing.
    if not root.is_dir():
        return HandoffObservation(series_name, handoff,
                                  PLAN_REVIEW_ROOT_UNAVAILABLE, detail=str(root))
    try:
        plan = plan_case(root, comic_dir, comic_dir, series_name,
                         series_key(series_name), source_name, decision)
    except Exception as exc:                     # noqa: BLE001 — never fatal
        return HandoffObservation(series_name, handoff, PLAN_FAILED,
                                  detail=f"{type(exc).__name__}: {exc}")
    m = plan.manifest
    return HandoffObservation(
        series_name, handoff, PLAN_PLANNED, case_id=m.case_id,
        inventory_digest=m.payload_inventory_digest, file_count=m.file_count,
        method=m.transfer_method,
    )


def summarise(observations: list[HandoffObservation]) -> str:
    """One line for the end of one pass, counting `defer` by reason."""
    if not observations:
        return "  Staging shadow-plan: no arrivals observed."
    outcomes = Counter(o.handoff.outcome for o in observations)
    defers = Counter(o.handoff.reason for o in observations
                     if o.handoff.outcome == OUTCOME_DEFER)
    planned = sum(1 for o in observations if o.plan_status == PLAN_PLANNED)
    defer_detail = ", ".join(f"{r} {n}" for r, n in sorted(defers.items()))
    return (
        f"  Staging shadow-plan: {len(observations)} observed, "
        f"{outcomes[OUTCOME_LEGACY_MOVE]} legacy_move, "
        f"{outcomes[OUTCOME_REVIEW_HANDOFF]} review_handoff "
        f"({planned} planned), {outcomes[OUTCOME_DEFER]} defer"
        + (f" ({defer_detail})" if defer_detail else "") + "."
    )
