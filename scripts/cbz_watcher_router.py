"""v2 classification for the watcher, and the shadow comparison ahead of it.

The watcher's own routing is v1: it reads the repository-root routing.json,
matches flat `match`/`pattern` globs against a source folder or title, and
returns a destination path. It cannot express the v2 signal model, produce a
RoutingDecision, consult a series index, or report that nothing classified an
archive. Pointing its ROUTING_FILE at config/routing.v2.json would not
activate v2 -- it would match nothing and send the entire library to the
default.

So the replacement arrives in two steps, and this module is the first: an
object that classifies exactly as the watcher eventually will, while the
legacy resolver stays authoritative over every move. Running it in shadow
mode produces a production-observable comparison with no writes at all.

Three things are deliberate:

* Classification reuses cbz_library_reclassify's sampling and ComicInfo
  reading, and the engine's ranking contract, so the watcher and the
  migration tool cannot disagree about the same series. A second, subtly
  different notion of "what this series is" is how a library drifts out of
  agreement with its own router.
* The index is built once at startup -- the destinations hold ~18k series
  folders and enumerating them costs ~400 ms -- and updated in memory after
  each successful move. Without that update a series first seen during a long
  watcher session would not become sticky until the process restarted.
* A review destination must never be indexed. See _reject_indexed_review.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from scripts.cbz_library_reclassify import read_comic_info, sample_archives
from scripts.cbz_routing import (
    RoutingConfig,
    RoutingDecision,
    RoutingConfigError,
    SeriesIndex,
    build_context,
    is_terminal_sample,
    load,
    resolve,
    sample_rank,
)

log = logging.getLogger(__name__)

# off    -- v2 is not consulted at all; the watcher behaves exactly as before.
# shadow -- v2 classifies and logs, the legacy destination still wins.
#
# There is deliberately no "active" mode here. Letting v2 control a move is
# the staging branch's job, and it needs the review path and the guarded
# transfer protocol that do not exist yet.
MODE_OFF = "off"
MODE_SHADOW = "shadow"
VALID_MODES = frozenset({MODE_OFF, MODE_SHADOW})

DEFAULT_SAMPLE_LIMIT = 5


def _reject_indexed_review(cfg: RoutingConfig) -> None:
    """A review destination in the series index would erase its own semantics.

    An unresolved archive staged for review sits in a directory. If that
    directory is indexed, the next scan finds the series "already exists in
    review" and returns an authoritative index hit -- confidence resolved,
    authoritative true. The archive would be promoted from "nothing
    classified this" to "a prior decision put it here" by nothing more than a
    restart, and the review queue would become self-confirming.

    Checked at startup rather than trusted to configuration discipline: the
    staged config happens to list its index destinations explicitly today,
    but that is a property of the current file, not a guarantee.
    """
    if not cfg.unresolved_destination:
        return
    indexed = cfg.series_index_destinations or tuple(cfg.destinations)
    if cfg.series_index_enabled and cfg.unresolved_destination in indexed:
        raise RoutingConfigError(
            f"unresolved destination {cfg.unresolved_destination!r} is also a "
            f"series_index destination {list(indexed)}; a staged review case "
            "would become an authoritative index hit after a restart"
        )


@dataclass(frozen=True)
class ShadowComparison:
    """One legacy-versus-v2 result, for logging and later tallying."""

    series: str
    source: str
    legacy_dest: str
    decision: RoutingDecision

    @property
    def agrees(self) -> bool:
        """Whether v2 would have sent this to the same directory as v1.

        Compared as paths, because the two models do not share a vocabulary:
        v1 yields a destination path, v2 a destination key. Path is the only
        thing they both mean.
        """
        return _same_dir(self.legacy_dest, self.decision.dest_path)

    def describe(self) -> str:
        verdict = "agree" if self.agrees else "DIFFER"
        return (
            f"[shadow:{verdict}] {self.series!r} "
            f"legacy={self.legacy_dest} v2={self.decision.dest_path} "
            f"key={self.decision.dest_key} rule={self.decision.rule_name or '-'} "
            f"confidence={self.decision.confidence} "
            f"strength={self.decision.evidence_strength} "
            f"authoritative={self.decision.authoritative} "
            f"why={self.decision.reason}"
        )


def _same_dir(a: str, b: str) -> bool:
    if not a or not b:
        return False
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:                     # unresolvable path: compare textually
        return Path(a) == Path(b)


class WatcherRouter:
    """Classifies a comic directory the way the watcher eventually will."""

    def __init__(self, cfg: RoutingConfig, index: SeriesIndex, *,
                 mode: str = MODE_OFF,
                 sample_limit: int = DEFAULT_SAMPLE_LIMIT) -> None:
        if mode not in VALID_MODES:
            raise RoutingConfigError(
                f"routing mode {mode!r} must be one of {sorted(VALID_MODES)}"
            )
        _reject_indexed_review(cfg)
        self.cfg = cfg
        self.index = index
        self.mode = mode
        self.sample_limit = sample_limit
        self.comparisons: list[ShadowComparison] = []

    @classmethod
    def load(cls, config_path: Path, *, mode: str = MODE_OFF,
             sample_limit: int = DEFAULT_SAMPLE_LIMIT,
             lister=None) -> "WatcherRouter":
        """Read the config and build the index once, at startup."""
        cfg = load(config_path)
        _reject_indexed_review(cfg)
        index = SeriesIndex.build(cfg, lister=lister)
        log.info(
            "  Routing v2: mode=%s, %d rule(s), %d override(s), "
            "index=%s (%d series)",
            mode, len(cfg.rules), len(cfg.series_overrides),
            "on" if cfg.series_index_enabled else "off", len(index),
        )
        return cls(cfg, index, mode=mode, sample_limit=sample_limit)

    @property
    def enabled(self) -> bool:
        return self.mode != MODE_OFF

    # ------------------------------------------------------- classification

    def classify(self, comic_dir: Path, source_name: str,
                 series_name: str | None = None) -> RoutingDecision:
        """Decide where *comic_dir* belongs, from a spread sample of its CBZs.

        `route_unresolved=False` for now: this branch never lets v2 choose a
        destination, and the review path it would name does not exist yet.
        The decision still reports confidence="unresolved", which is the
        number worth observing in shadow mode.
        """
        series = series_name or comic_dir.name
        decision: RoutingDecision | None = None

        for archive in sample_archives(comic_dir, self.sample_limit):
            info = read_comic_info(archive)
            candidate = resolve(
                self.cfg,
                build_context(source_name, series, info),
                series_name=series,
                index=self.index,
                route_unresolved=False,
            )
            if decision is None or sample_rank(candidate) > sample_rank(decision):
                decision = candidate
            if is_terminal_sample(candidate):
                break

        if decision is None:                    # no archives, or none readable
            decision = resolve(
                self.cfg,
                build_context(source_name, series, {}),
                series_name=series,
                index=self.index,
                route_unresolved=False,
            )
        return decision

    def shadow(self, comic_dir: Path, source_name: str, legacy_dest: str,
               series_name: str | None = None) -> ShadowComparison | None:
        """Classify and record, without influencing anything.

        Returns None when v2 is off, so the caller stays a single line and
        the watcher pays nothing for a mode it is not running.
        """
        if not self.enabled:
            return None
        series = series_name or comic_dir.name
        comparison = ShadowComparison(
            series=series, source=source_name, legacy_dest=legacy_dest,
            decision=self.classify(comic_dir, source_name, series),
        )
        self.comparisons.append(comparison)
        log.info("  %s", comparison.describe())
        return comparison

    # ------------------------------------------------------- index upkeep

    def note_move(self, series_name: str, dest_key: str, path: Path) -> None:
        """Record a completed move so the series is sticky within this session.

        The index is built once at startup for cost reasons, so a series
        created during a long watcher session is absent from it. Without this
        a second chapter arriving an hour later would be classified from
        metadata again and could land somewhere else -- the exact split the
        index exists to prevent.
        """
        if dest_key not in self.cfg.destinations:
            log.warning("  index not updated: unknown destination %r", dest_key)
            return
        self.index.add(series_name, dest_key, path)

    def summarise(self) -> str:
        """One line for the end of a scan pass."""
        total = len(self.comparisons)
        if not total:
            return "  Routing v2 shadow: no directories classified."
        differ = [c for c in self.comparisons if not c.agrees]
        unresolved = [c for c in self.comparisons
                      if c.decision.confidence == "unresolved"]
        return (
            f"  Routing v2 shadow: {total} classified, {len(differ)} would "
            f"differ from legacy, {len(unresolved)} unresolved."
        )
