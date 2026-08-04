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
import threading
from dataclasses import dataclass
from pathlib import Path

from scripts.cbz_library_reclassify import (
    read_comic_info,
    sample_archives,
    spread_sample,
)
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


def _normalise(path) -> Path:
    try:
        return Path(path).resolve()
    except OSError:                     # unresolvable path: compare textually
        return Path(path)


def _same_dir(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return _normalise(a) == _normalise(b)


def summarise(results: list[ShadowComparison]) -> str:
    """One line for the end of one processing pass.

    Takes the results rather than reading router state: a long-running daemon
    must not accumulate a comparison per directory for its whole lifetime, and
    per-call state also keeps overlapping passes from mixing.
    """
    if not results:
        return "  Routing v2 shadow: no directories classified."
    differ = [c for c in results if not c.agrees]
    unresolved = [c for c in results if c.decision.confidence == "unresolved"]
    return (
        f"  Routing v2 shadow: {len(results)} classified, {len(differ)} would "
        f"differ from legacy, {len(unresolved)} unresolved."
    )


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
        # Destination path -> key. The legacy resolver yields a path and the
        # v2 model a key, so recording a completed move needs the reverse map.
        # Immutable after construction, so it needs no lock.
        self._key_by_path = {
            _normalise(path): key for key, path in cfg.destinations.items()
        }
        # The watcher processes unrelated arrival directories concurrently on
        # Timer threads -- its own guard only prevents two threads working the
        # same or a nested path -- and they share this router and its index.
        #
        # SeriesIndex.add is a compound read-modify-write: it reads _entries,
        # branches on whether the key was present, and only then writes and
        # marks ambiguity. Two threads adding the same series can both see it
        # absent, both write, and lose the ambiguity marker along with the
        # configured destination priority. The GIL makes each dict operation
        # atomic; it does not make that sequence atomic.
        self._index_lock = threading.RLock()

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
                 series_name: str | None = None,
                 archives: list[Path] | None = None) -> RoutingDecision:
        """Decide where this batch belongs, from a spread sample of its CBZs.

        *series_name* must be the identity the caller will actually file under.
        The watcher resolves "Berserk Ch. 4" to "Berserk" late in processing,
        and classifying before that misses the existing-series index hit that
        should have decided it.

        *archives* is the exact collection being moved. Without it a mixed
        drop point -- loose archives beside nested series directories -- would
        be sampled by walking the parent, classifying the loose files from an
        unrelated series' metadata.

        `route_unresolved=False` for now: this branch never lets v2 choose a
        destination, and the review path it would name does not exist yet.
        The decision still reports confidence="unresolved", which is the
        number worth observing in shadow mode.
        """
        series = series_name or comic_dir.name
        sampled = (spread_sample(archives, self.sample_limit)
                   if archives is not None
                   else sample_archives(comic_dir, self.sample_limit))

        # Read every sampled archive before taking the lock. Opening ZIPs and
        # parsing XML must not block another thread's index update, and the
        # whole ranking has to see one coherent index state rather than an
        # index that changed underneath it mid-loop.
        #
        # This costs the early exit: previously a strong first sample stopped
        # the reads. Up to sample_limit archives are now always opened, which
        # is the price of keeping I/O outside the lock.
        contexts = [
            build_context(source_name, series, read_comic_info(archive))
            for archive in sampled
        ]

        decision: RoutingDecision | None = None
        with self._index_lock:
            for context in contexts:
                candidate = resolve(
                    self.cfg, context, series_name=series,
                    index=self.index, route_unresolved=False,
                )
                if decision is None or sample_rank(candidate) > sample_rank(decision):
                    decision = candidate
                if is_terminal_sample(candidate):
                    break

            if decision is None:                # no archives, or none readable
                decision = resolve(
                    self.cfg,
                    build_context(source_name, series, {}),
                    series_name=series,
                    index=self.index,
                    route_unresolved=False,
                )
        return decision

    def shadow(self, comic_dir: Path, source_name: str, legacy_dest: str,
               series_name: str | None = None,
               archives: list[Path] | None = None) -> ShadowComparison | None:
        """Classify and log, without influencing anything.

        Returns the comparison so the caller can collect it for one pass.
        Deliberately not accumulated on the router: this runs in a daemon, and
        a per-directory record kept for the process lifetime is an unbounded
        list and a cross-pass tally nobody asked for.

        Returns None when v2 is off, so the caller stays a single line and
        the watcher pays nothing for a mode it is not running.
        """
        if not self.enabled:
            return None
        series = series_name or comic_dir.name
        comparison = ShadowComparison(
            series=series, source=source_name, legacy_dest=legacy_dest,
            decision=self.classify(comic_dir, source_name, series, archives),
        )
        log.info("  %s", comparison.describe())
        return comparison

    # ------------------------------------------------------- index upkeep

    def dest_key_for_path(self, dest_folder: str) -> str | None:
        """Map a legacy destination path back to its v2 destination key."""
        return self._key_by_path.get(_normalise(dest_folder))

    def note_move(self, series_name: str, dest_folder: str,
                  series_dir: Path) -> None:
        """Record a completed move so the series is sticky within this session.

        The index is built once at startup for cost reasons, so a series
        created during a long watcher session is absent from it. Without this
        a second chapter arriving an hour later would be classified from
        metadata again and could land somewhere else -- the exact split the
        index exists to prevent.

        *series_dir* must be the directory the files actually landed in, not a
        re-derived guess: the mover merges into a pre-existing folder when one
        matches, and that folder's name is what a later lookup has to find.

        Does nothing when the index is disabled. resolve() consults whatever
        index it is handed without re-checking the flag, so populating a
        disabled index would manufacture authority the configuration
        explicitly withheld.
        """
        if not self.cfg.series_index_enabled:
            return
        dest_key = self.dest_key_for_path(dest_folder)
        if dest_key is None:
            log.warning("  index not updated: %r is not a v2 destination",
                        dest_folder)
            return
        with self._index_lock:
            self.index.add(series_name, dest_key, series_dir)
