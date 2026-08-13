"""Routing configuration and rule evaluation for the watcher.

Replaces the flat glob list that routing.json v1 used. That format could
express exactly one idea -- "this source folder name means manga" -- and the
live file said it 55 times. The three-way split (explicitly adult -> Comix,
otherwise Asian origin -> Manga, otherwise Graphic Novels) is a predicate
over content attributes, which v1 had no vocabulary for.

The v2 shape is:

    lists    -- plain data, e.g. every Asian-origin source folder name
    signals  -- named predicate trees over fields, referencing lists
    rules    -- ordered signal -> destination, first match wins

so adding a scanlation site is appending one string rather than a four-line
rule object, and the whole precedence order is two rules and a default.

Three behaviours are deliberate:

* A missing ComicInfo field makes a matcher false, never an error. Archives
  with no metadata still route, they just fall through to source/title.
* An invalid config is fatal at load. v1 set its default destination to ""
  on any parse error, and `Path("") / "Batman"` is the *relative* path
  "Batman" -- a typo silently moved comics into the watcher's working
  directory instead of the library. Refusing to start is the correct
  failure.
* Every decision carries the rule and the matcher that produced it, so a
  misroute is diagnosable without reading the whole file by eye.

v1 files keep working: load() converts them in memory and logs a
deprecation notice.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

VALID_OPERATORS = frozenset(
    {"equals", "in", "in_list", "glob", "glob_in_list", "glob_tokens_in_list",
     "contains_any"}
)
# Fields that carry a comma-joined list rather than one value split on this.
# Only the comma: a census of the live library found it to be the sole
# separator present in Publisher, and no occurrences at all of ';', '|', '/'
# or '&'. Adding speculative separators would widen matching with nothing
# measured to justify it.
TOKEN_SEPARATOR = ","
COMBINATORS = frozenset({"any", "all", "not"})
COMICINFO_PREFIX = "comicinfo."

# How much a decision's evidence is worth, as structured data rather than as a
# substring of the rule's display name. A caller that samples several archives
# of one series has to rank their decisions, and inferring that ranking from
# `"weak" in rule_name` coupled it to config naming: renaming a rule silently
# changed which sample won, and nothing detected it.
EvidenceStrength = Literal["none", "weak", "strong"]

STRENGTH_ORDER: dict[str, int] = {"none": 0, "weak": 1, "strong": 2}

# A rule that matches always constitutes some evidence, so a rule may declare
# only "weak" or "strong". "none" is what a decision carries when no rule
# matched at all, or when the decision did not come from evidence.
RULE_STRENGTHS = frozenset({"weak", "strong"})

# An undeclared rule is strong. Every rule in every config in this repository
# was treated as strong before strength became explicit, except the one now
# annotated in routing.v2.json, so this keeps existing files behaving
# identically rather than silently demoting them.
DEFAULT_RULE_STRENGTH: EvidenceStrength = "strong"

# Whether classification actually established a destination. Orthogonal to
# both strength and authority: a decision can be resolved with no evidence
# (an override), and an unresolved one still has a dest_key, because the
# archive has to go somewhere. Confidence says how that destination was
# arrived at, not where it is.
Confidence = Literal["resolved", "unresolved"]

# Series-name normalisation, kept byte-for-byte compatible with
# cbz_watcher._series_key so the index agrees with the watcher's own
# existing-folder matching rather than being a second, subtly different
# notion of "same series".
_MARKER_WORDS_RE = re.compile(r"\b(?:uncensored|decensored)\b", re.IGNORECASE)
# Non-word non-space characters, plus underscore. The underscore needs its own
# alternative because `\w` includes it, so it cannot simply be added to the
# negated class. Without it `-` and `_` normalize differently and
# "Attack_on_Titan" is a different series from "Attack on Titan" (#44).
_SERIES_KEY_PUNCT_RE = re.compile(r"[^\w\s]|_")
_SERIES_KEY_SPACE_RE = re.compile(r"\s+")


def series_key(name: str) -> str:
    """The canonical series identity. One definition, four consumers.

    This is the single rule for "same series" across the watcher, the
    reclassifier, the series-operation lock registry, and SeriesIndex. It was
    three separate implementations until #44; a divergence between them would
    have meant the watcher comparing series by one rule while the lock and the
    index used another, with nothing detecting it.

    The normalization, in order:

        1. `uncensored` / `decensored` removed as whole words, case-insensitive
        2. lowercased
        3. every non-word, non-space character *and every underscore*
           replaced with a space
        4. runs of whitespace collapsed, ends stripped

    So "BERSERK", "Berserk!!", "Berserk (Uncensored)" and "Berserk " all key
    alike, and "Attack-on-Titan", "Attack_on_Titan" and "Attack on Titan" are
    one series.

    Underscore is called out in step 3 because it is a word character: `\\w`
    matches it, so a plain negated class silently keeps it. Until #44 it did
    survive, which made `-` and `_` normalize differently and split every
    series whose releases used the two interchangeably -- a real division,
    since scanlator naming uses both.

    A name consisting only of separators now keys to "" where it previously
    kept its underscores. That matches what "!!!" has always done, and callers
    that cannot act on an empty identity already reject it: see
    `cbz_lock_order.SeriesLockRegistry.for_series`.

    No Unicode normalization here. `cbz_lock_order.lock_key` adds NFC around
    this deliberately, because the lock domain must over-serialize rather than
    under-serialize; SeriesIndex does not, so composed and decomposed forms
    remain distinct series to routing.

    Accepts None and returns "". No caller passes it -- every call site
    supplies a str by contract -- so this is a defensive floor, not a
    behaviour to depend on.
    """
    name = _MARKER_WORDS_RE.sub("", name or "")
    name = _SERIES_KEY_PUNCT_RE.sub(" ", name.lower())
    return _SERIES_KEY_SPACE_RE.sub(" ", name).strip()


class RoutingConfigError(ValueError):
    """Raised for any structurally invalid routing configuration."""


@dataclass(frozen=True)
class ReviewHint:
    """Advisory evidence attached to a decision, never a routing reason.

    A hint explains why an unresolved archive was surfaced for review and
    what a reviewer might conclude. It must not influence dest_key: title
    words in particular are far weaker evidence than the provenance signals
    the rules use, and letting them route would degrade a classifier that
    currently misfiles two series in 646.
    """

    kind: str
    value: str


@dataclass(frozen=True)
class RoutingDecision:
    dest_key: str
    dest_path: str
    rule_name: str | None       # None means nothing matched; the default won
    reason: str
    # Set when an override renamed the series; callers should use this as the
    # destination folder name so aliases merge instead of forking.
    canonical_series: str | None = None
    # Set when an existing series folder was found, so the caller can move
    # into that exact directory rather than re-deriving its name.
    series_dir: Path | None = None
    ambiguous_series: bool = False
    # What the metadata was worth. "none" means no rule matched, or the
    # decision did not come from evidence at all.
    evidence_strength: EvidenceStrength = "none"
    # A human decision or an existing placement, not a reading of metadata.
    # Kept separate from evidence_strength so the two are never compared as if
    # they were the same kind of thing: an override outranks all evidence
    # regardless of how strong that evidence is, and saying so with a flag is
    # clearer than inventing a fourth strength above "strong".
    authoritative: bool = False
    # Whether classification established this destination at all. Stays
    # "unresolved" for a no-match even when compatibility behaviour sends it
    # to the ordinary default -- where the archive went is a policy question,
    # whether it was classified is not.
    confidence: Confidence = "resolved"
    # Advisory only, and empty until hint producers exist. Never consulted
    # when choosing dest_key.
    review_hints: tuple[ReviewHint, ...] = ()

    @property
    def matched(self) -> bool:
        return self.rule_name is not None


@dataclass(frozen=True)
class SeriesOverride:
    """A human decision that outranks both the index and the rules.

    Different scanlation groups romanise and translate the same series
    differently ("Kanojo, Okarishimasu" / "Kanojo Okarishimasu" /
    "Rent-a-Girlfriend"), and normalisation cannot unify genuinely different
    words. An override maps any number of aliases onto one canonical folder
    name, optionally pinning the destination too.
    """

    canonical: str
    aliases: tuple[str, ...]
    dest_key: str | None = None

    def matches(self, key: str) -> bool:
        return key in {series_key(a) for a in (self.canonical, *self.aliases)}


@dataclass
class RoutingConfig:
    destinations: dict[str, str]
    default_key: str
    lists: dict[str, list[str]] = field(default_factory=dict)
    signals: dict[str, dict] = field(default_factory=dict)
    rules: list[dict] = field(default_factory=list)
    source_version: int = 2
    series_overrides: tuple[SeriesOverride, ...] = ()
    # Enabling the index makes a series' current location sticky. Where that
    # location was decided by a person, stickiness is the point: Comix
    # membership is a deliberate adult determination, and seeding the index
    # from it preserves that judgement instead of re-deriving it from
    # metadata that cannot express it. Where a library is being retired or is
    # known-misclassified, list it in `destinations` only after its contents
    # have been migrated, or the index will pin them where they sit.
    series_index_enabled: bool = False
    series_index_destinations: tuple[str, ...] = ()
    # Where an archive goes when nothing classified it. None keeps the
    # pre-existing behaviour of falling through to `default`, which is what
    # every config in this repository still does.
    unresolved_destination: str | None = None

    @property
    def default_path(self) -> str:
        return self.destinations[self.default_key]

    def override_for(self, series_name: str) -> SeriesOverride | None:
        key = series_key(series_name)
        if not key:
            return None
        for override in self.series_overrides:
            if override.matches(key):
                return override
        return None


class SeriesIndex:
    """`series_key -> destination` for series that already exist on disk.

    Built once per scan pass rather than per directory: the destinations hold
    ~18k series folders between them and enumerating those costs ~400 ms,
    against microseconds for rule evaluation. Per-directory lookups would
    make routing dramatically slower, not faster -- the reason to do this is
    that a series must not split across libraries when a chapter arrives
    without usable metadata, not speed.
    """

    def __init__(self, priority: tuple[str, ...] = ()) -> None:
        # Destination precedence for a series that exists in more than one
        # library. Order comes from series_index.destinations, so a library
        # whose membership encodes a human decision -- Comix membership is an
        # adult determination, made deliberately, not derived from metadata --
        # is listed first and wins. Deferring to the rules instead would let a
        # metadata signal quietly overturn that decision.
        self._priority = priority
        self._entries: dict[str, tuple[str, Path, int]] = {}
        self._ambiguous: set[str] = set()

    @property
    def priority(self) -> tuple[str, ...]:
        """The destination precedence this index resolves ambiguity with.

        Public because a caller handed an index has no other way to check it
        agrees with the configuration. An index built with no priority ranks
        every destination equally, so a series present in two libraries is
        decided by whichever write happened first -- silently, and differently
        run to run.
        """
        return self._priority

    def _rank(self, dest_key: str) -> int:
        try:
            return self._priority.index(dest_key)
        except ValueError:
            return len(self._priority)

    @classmethod
    def build(cls, cfg: RoutingConfig,
              lister=None) -> "SeriesIndex":
        keys = cfg.series_index_destinations or tuple(cfg.destinations)
        index = cls(priority=keys)
        if not cfg.series_index_enabled:
            return index
        listdir = lister or _default_lister
        for dest_key in keys:
            root = cfg.destinations.get(dest_key)
            if not root:
                continue
            for path in listdir(Path(root)):
                index.add(path.name, dest_key, path)
        return index

    def add(self, series_name: str, dest_key: str, path: Path) -> None:
        key = series_key(series_name)
        if not key:
            return
        rank = self._rank(dest_key)
        existing = self._entries.get(key)
        if existing is None:
            self._entries[key] = (dest_key, path, rank)
            return
        if existing[0] == dest_key:
            return
        # Present in two libraries. Resolve by priority rather than refusing,
        # but keep it flagged so a genuine split is visible rather than
        # silently papered over.
        self._ambiguous.add(key)
        if rank < existing[2]:
            self._entries[key] = (dest_key, path, rank)

    def lookup(self, series_name: str) -> tuple[str, Path] | None:
        key = series_key(series_name)
        if not key:
            return None
        entry = self._entries.get(key)
        return (entry[0], entry[1]) if entry else None

    def is_ambiguous(self, series_name: str) -> bool:
        return series_key(series_name) in self._ambiguous

    def __len__(self) -> int:
        return len(self._entries)


def sample_rank(decision: RoutingDecision) -> tuple[int, int]:
    """Order one sample's decision against another's.

    Origin is a property of a series, not of a chapter, so a caller that reads
    several archives of one series has to rank their decisions. Authoritative
    decisions -- a manual override, or an existing series folder -- outrank
    every reading of metadata, however strong. Within evidence, strong beats
    weak beats none.

    Lives here rather than in a caller because every consumer must rank
    identically. Two callers with their own copies is how the migration tool
    and the watcher would drift into disagreeing about the same series.
    """
    return (1 if decision.authoritative else 0,
            STRENGTH_ORDER[decision.evidence_strength])


def is_terminal_sample(decision: RoutingDecision) -> bool:
    """True when no further sample can improve on this one.

    The other half of the ranking contract: nothing outranks an authoritative
    decision or a strong signal, so reading more archives cannot change the
    answer and the caller should stop.
    """
    return decision.authoritative or decision.evidence_strength == "strong"


def _default_lister(root: Path):
    if not root.is_dir():
        return []
    return [p for p in root.iterdir() if p.is_dir()]


# ---------------------------------------------------------------- context

def build_context(
    source: str,
    title: str,
    comic_info: dict[str, str] | None = None,
) -> dict[str, str]:
    """Flatten the routable facts into `field name -> value`.

    ComicInfo element names are matched case-insensitively, since the field
    references in a hand-edited config should not have to reproduce the XML's
    exact casing to work.
    """
    context = {"source": source or "", "title": title or ""}
    for key, value in (comic_info or {}).items():
        if value is None:
            continue
        context[f"{COMICINFO_PREFIX}{key.casefold()}"] = str(value)
    return context


def _lookup(context: dict[str, str], field_name: str) -> str | None:
    name = field_name.strip()
    if name.casefold().startswith(COMICINFO_PREFIX):
        key = COMICINFO_PREFIX + name[len(COMICINFO_PREFIX):].casefold()
    else:
        key = name.casefold()
    value = context.get(key)
    return value if value not in (None, "") else None


# ------------------------------------------------------------- evaluation

def _evaluate(node: Any, context: dict[str, str],
              cfg: RoutingConfig) -> tuple[bool, str]:
    """Evaluate a predicate node. Returns (result, human-readable reason)."""
    if isinstance(node, str):
        # A bare string references a named signal.
        signal = cfg.signals.get(node)
        if signal is None:
            raise RoutingConfigError(f"unknown signal: {node!r}")
        ok, why = _evaluate(signal, context, cfg)
        return ok, f"{node}({why})" if ok else why

    if not isinstance(node, dict):
        raise RoutingConfigError(f"predicate must be an object or signal name: {node!r}")

    for combinator in ("any", "all", "not"):
        if combinator in node:
            children = node[combinator]
            if combinator == "not":
                ok, why = _evaluate(children, context, cfg)
                return (not ok), f"not({why})"
            if not isinstance(children, list) or not children:
                raise RoutingConfigError(f"'{combinator}' needs a non-empty list")
            reasons = []
            for child in children:
                ok, why = _evaluate(child, context, cfg)
                if combinator == "any" and ok:
                    return True, why
                if combinator == "all" and not ok:
                    return False, f"all failed at {why}"
                reasons.append(why)
            return (combinator == "all"), (
                "; ".join(reasons) if combinator == "all" else "no match"
            )

    return _evaluate_matcher(node, context, cfg)


def _evaluate_matcher(node: dict, context: dict[str, str],
                      cfg: RoutingConfig) -> tuple[bool, str]:
    field_name = node.get("field")
    if not field_name:
        raise RoutingConfigError(f"matcher missing 'field': {node!r}")

    operators = [k for k in node if k != "field"]
    if len(operators) != 1:
        raise RoutingConfigError(
            f"matcher on {field_name!r} needs exactly one operator, got {operators}"
        )
    op = operators[0]
    if op not in VALID_OPERATORS:
        raise RoutingConfigError(f"unknown operator {op!r} on field {field_name!r}")

    value = _lookup(context, field_name)
    if value is None:
        # Absent metadata is a false matcher, never an error.
        return False, f"{field_name} absent"

    operand = node[op]
    folded = value.casefold()

    def named_list(name: str) -> list[str]:
        items = cfg.lists.get(name)
        if items is None:
            raise RoutingConfigError(f"unknown list: {name!r}")
        return items

    if op == "equals":
        ok = folded == str(operand).casefold()
    elif op == "in":
        ok = folded in {str(x).casefold() for x in operand}
    elif op == "in_list":
        ok = folded in {str(x).casefold() for x in named_list(str(operand))}
    elif op == "glob":
        ok = fnmatch.fnmatch(folded, str(operand).casefold())
    elif op == "glob_in_list":
        ok = any(fnmatch.fnmatch(folded, str(p).casefold())
                 for p in named_list(str(operand)))
    elif op == "glob_tokens_in_list":
        # Publisher and Imprint carry comma-joined lists, e.g. "Gangan
        # Wing,Yen Press". glob_in_list applies the pattern to the whole
        # field, so an anchored pattern like "yen press*" cannot match unless
        # that publisher happens to be listed first. Match each token
        # separately instead.
        #
        # Deliberately not folded into glob_in_list: Web values are single
        # URLs, not lists, and a URL legitimately contains commas. Splitting
        # them would change domain matching for no measured benefit.
        patterns = [str(p).casefold() for p in named_list(str(operand))]
        tokens = [t.strip() for t in folded.split(TOKEN_SEPARATOR)]
        ok = any(fnmatch.fnmatch(token, pattern)
                 for token in tokens if token
                 for pattern in patterns)
    else:  # contains_any -- for comma-joined free text like Genre/Tags
        ok = any(str(x).casefold() in folded for x in operand)

    return ok, f"{field_name}={value!r} {op} {operand!r}" if ok else (
        f"{field_name}={value!r} !{op}"
    )


def resolve(
    cfg: RoutingConfig,
    context: dict[str, str],
    *,
    series_name: str | None = None,
    index: SeriesIndex | None = None,
    route_unresolved: bool = True,
) -> RoutingDecision:
    """Decide a destination.

    Precedence, highest first:

      1. a manual series override -- an explicit human decision, and the
         only way to unify titles that differ by translation rather than by
         punctuation ("Kanojo Okarishimasu" vs "Rent-a-Girlfriend");
      2. an existing series folder, so a series never splits across
         libraries because one chapter arrived without usable metadata;
      3. the rules, top to bottom, first match wins;
      4. the default.

    A series found in two libraries at once is treated as no match: the
    library disagrees with itself, so the rules stay authoritative rather
    than the engine picking one arbitrarily.

    `route_unresolved` is the caller's handling policy, not a statement about
    the decision. A no-match is always confidence="unresolved"; the flag only
    decides whether it goes to the configured review destination or falls
    through to `default`. A migration tool reclassifying an existing library
    passes False -- those series already have a home, so "unresolved" has no
    useful destination to offer them.
    """
    canonical: str | None = None
    effective = series_name

    if series_name:
        override = cfg.override_for(series_name)
        if override is not None:
            canonical = override.canonical
            effective = override.canonical
            if override.dest_key:
                return RoutingDecision(
                    override.dest_key, cfg.destinations[override.dest_key],
                    "series override",
                    f"series {series_name!r} pinned to {override.dest_key} "
                    f"as {override.canonical!r}",
                    canonical_series=canonical,
                    authoritative=True,
                )

    ambiguous = False
    if effective and index is not None:
        ambiguous = index.is_ambiguous(effective)
        hit = index.lookup(effective)
        if hit is not None:
            dest_key, path = hit
            note = " (also present elsewhere; resolved by priority)" if ambiguous else ""
            return RoutingDecision(
                dest_key, cfg.destinations[dest_key], "existing series",
                f"series {effective!r} already exists in {dest_key}{note}",
                canonical_series=canonical, series_dir=path,
                ambiguous_series=ambiguous, authoritative=True,
            )

    for rule in cfg.rules:
        ok, why = _evaluate(rule["when"], context, cfg)
        if ok:
            key = rule["dest"]
            return RoutingDecision(key, cfg.destinations[key],
                                   rule.get("name", key), why,
                                   canonical_series=canonical,
                                   ambiguous_series=ambiguous,
                                   evidence_strength=rule.get(
                                       "strength", DEFAULT_RULE_STRENGTH))

    # Nothing classified this. The destination is a policy question; the fact
    # that classification failed is not, so confidence is unresolved either
    # way. The reason text for the compatibility path is left exactly as it
    # was, so a caller that records it keeps producing identical output.
    if route_unresolved and cfg.unresolved_destination:
        key = cfg.unresolved_destination
        return RoutingDecision(key, cfg.destinations[key], None,
                               f"no rule matched; unresolved -> {key}",
                               canonical_series=canonical,
                               ambiguous_series=ambiguous,
                               confidence="unresolved")
    return RoutingDecision(cfg.default_key, cfg.default_path, None,
                           "no rule matched; default",
                           canonical_series=canonical,
                           ambiguous_series=ambiguous,
                           confidence="unresolved")


def explain(
    cfg: RoutingConfig,
    context: dict[str, str],
    series_name: str | None = None,
    index: SeriesIndex | None = None,
    *,
    route_unresolved: bool = True,
) -> list[str]:
    """Full trace: overrides, the series index, then every rule."""
    lines = [f"context: {context}"]
    if series_name:
        lines.append(f"series: {series_name!r} (key={series_key(series_name)!r})")
        override = cfg.override_for(series_name)
        if override is not None:
            lines.append(
                f"MATCH override -> canonical {override.canonical!r}"
                + (f", pinned to {override.dest_key}" if override.dest_key else "")
            )
        if index is not None:
            name = override.canonical if override else series_name
            if index.is_ambiguous(name):
                lines.append("  --  series index: ambiguous (in >1 library)")
            elif index.lookup(name):
                dest_key, path = index.lookup(name)
                lines.append(f"MATCH series index -> {dest_key} ({path})")
            else:
                lines.append("  --  series index: no existing folder")

    decision = resolve(cfg, context, series_name=series_name,
                       index=index, route_unresolved=route_unresolved)
    for rule in cfg.rules:
        ok, why = _evaluate(rule["when"], context, cfg)
        lines.append(f"{'MATCH ' if ok else '  --  '}"
                     f"{rule.get('name', rule['dest'])}: {why}")
        if ok:
            break
    else:
        # Describe the policy that actually applied. Printing "(default)"
        # while the archive goes to a review destination is a trace that
        # contradicts its own conclusion.
        if decision.confidence == "unresolved":
            if route_unresolved and cfg.unresolved_destination:
                lines.append(
                    f"  --  (unresolved handling) -> {decision.dest_key}")
            else:
                lines.append(f"  --  (unresolved; compatibility default) "
                             f"-> {cfg.default_key}")
        else:
            lines.append(f"  --  (default) -> {cfg.default_key}")

    # Say plainly that nothing classified this, rather than letting the
    # trailing summary read as though the default were a matched rule.
    if decision.confidence == "unresolved":
        lines.append("Unresolved: no override, existing-series match, or "
                     "routing rule matched.")

    # Never label a missing rule_name "default": with review routing enabled
    # the destination is not the default, and the label would name a rule
    # that never fired.
    label = decision.rule_name or decision.confidence
    lines.append(f"       => {decision.dest_key} = {decision.dest_path} "
                 f"[{label}]")
    return lines


# ------------------------------------------------------------- loading

def _convert_v1(raw: dict) -> dict:
    """Fold a v1 file into v2 without changing which files go where.

    v1 rules are first-match-wins globs, so a *consecutive* run sharing the
    same (match, dest) can be collapsed into one list plus one rule with no
    semantic change. The live file is 55 consecutive source->manga rules, so
    this yields exactly one list and one rule.
    """
    lists: dict[str, list[str]] = {}
    signals: dict[str, dict] = {}
    rules: list[dict] = []

    runs: list[tuple[str, str, list[str]]] = []
    for rule in raw.get("rules", []):
        if "pattern" not in rule or "dest" not in rule:
            continue                       # v1 allowed bare _comment entries
        match_on = rule.get("match", "source")
        dest = rule["dest"]
        if runs and runs[-1][0] == match_on and runs[-1][1] == dest:
            runs[-1][2].append(rule["pattern"])
        else:
            runs.append((match_on, dest, [rule["pattern"]]))

    for index, (match_on, dest, patterns) in enumerate(runs, start=1):
        suffix = "" if len(runs) == 1 else f"_{index}"
        list_name = f"v1_{dest}_{match_on}_patterns{suffix}"
        signal_name = f"v1_{dest}_by_{match_on}{suffix}"
        lists[list_name] = patterns
        signals[signal_name] = {
            "any": [{"field": match_on, "glob_in_list": list_name}]
        }
        rules.append({
            "name": f"migrated v1 {match_on} rules -> {dest}",
            "when": signal_name,
            "dest": dest,
        })

    return {
        "version": 2,
        "destinations": dict(raw.get("destinations", {})),
        "default": raw.get("default", ""),
        "lists": lists,
        "signals": signals,
        "rules": rules,
    }


def _strip_comments(mapping: Any, label: str) -> dict:
    """Drop documentation keys from one mapping, rejecting a non-mapping.

    Every shipped config uses `_comment*` keys, but the parser had no notion
    of them, so what happened depended on which block they landed in: ignored
    at the top level and inside a rule object, fatal inside `signals`, and --
    worst -- silently accepted inside `lists`, where `list("Japanese...")`
    produced a 116-entry list of single characters. A comment name colliding
    with a referenced list would have resolved, matched nothing meaningful,
    and raised nothing.

    Underscore-prefixed keys are documentation everywhere, uniformly. Anything
    present that is not a mapping raises: turning malformed configuration into
    an empty block is the same absent-versus-malformed conflation, and for
    `series_index` it would silently disable index authority.
    """
    if not isinstance(mapping, dict):
        raise RoutingConfigError(
            f"{label} must be an object, got {type(mapping).__name__}"
        )
    return {k: v for k, v in mapping.items() if not str(k).startswith("_")}


def _parse_mapping_block(raw: dict, name: str) -> dict:
    """Read one optional mapping block. Only a genuinely absent key yields {}.

    A present null, scalar, or array is malformed and raises. `"lists": null`
    is not the same statement as omitting `lists`, and a parser whose contract
    is to fail closed must not treat them alike.
    """
    if name not in raw:
        return {}
    return _strip_comments(raw[name], repr(name))


def _parse_sequence_block(raw: dict, name: str) -> list:
    """Read one optional array block, with the same absence rule."""
    if name not in raw:
        return []
    value = raw[name]
    if not isinstance(value, list):
        raise RoutingConfigError(
            f"{name!r} must be an array, got {type(value).__name__}"
        )
    return value


def _parse_lists(raw: dict) -> dict[str, list[str]]:
    """Validate `name -> [pattern, ...]` strictly.

    Filtering comment keys alone would leave the real defect open: any
    string value is iterable, so a hand-edited `"asian_publishers":
    "yen press*"` became a list of nine characters that matched nothing and
    reported nothing. A list must be an array of strings or the config is
    malformed.
    """
    out: dict[str, list[str]] = {}
    for name, items in _parse_mapping_block(raw, "lists").items():
        if not isinstance(items, list):
            raise RoutingConfigError(
                f"list {name!r} must be an array of strings, got "
                f"{type(items).__name__}"
            )
        for position, item in enumerate(items):
            if not isinstance(item, str):
                raise RoutingConfigError(
                    f"list {name!r} entry {position} must be a string, got "
                    f"{type(item).__name__}"
                )
        out[name] = list(items)
    return out


def parse(raw: dict) -> RoutingConfig:
    source_version = int(raw.get("version", 1))
    if source_version == 1:
        raw = _convert_v1(raw)
    elif source_version != 2:
        raise RoutingConfigError(f"unsupported routing config version: {source_version}")

    destinations = _parse_mapping_block(raw, "destinations")
    if not destinations:
        raise RoutingConfigError("routing config defines no destinations")
    for key, value in destinations.items():
        if not value or not Path(value).is_absolute():
            raise RoutingConfigError(
                f"destination {key!r} must be an absolute path, got {value!r}"
            )

    default_key = raw.get("default") or ""
    if default_key not in destinations:
        raise RoutingConfigError(
            f"default {default_key!r} is not one of {sorted(destinations)}"
        )

    overrides: list[SeriesOverride] = []
    for index_, raw_entry in enumerate(_parse_sequence_block(raw, "series_overrides")):
        entry = _strip_comments(raw_entry, f"series_overrides[{index_}]")
        canonical = (entry.get("canonical") or "").strip()
        if not canonical:
            raise RoutingConfigError(
                f"series_overrides[{index_}] has no 'canonical' name"
            )
        dest = entry.get("dest")
        if dest is not None and dest not in destinations:
            raise RoutingConfigError(
                f"series_overrides[{index_}] destination {dest!r} is not defined"
            )
        aliases = tuple(entry.get("aliases") or ())
        if not aliases and dest is None:
            raise RoutingConfigError(
                f"series_overrides[{index_}] ({canonical!r}) does nothing: "
                "give it aliases, a dest, or both"
            )
        overrides.append(SeriesOverride(canonical, aliases, dest))

    # An alias claimed by two overrides has no defined winner; reject rather
    # than let evaluation order decide it silently.
    seen_keys: dict[str, str] = {}
    for override in overrides:
        for name in (override.canonical, *override.aliases):
            key = series_key(name)
            if not key:
                continue
            if key in seen_keys and seen_keys[key] != override.canonical:
                raise RoutingConfigError(
                    f"alias {name!r} is claimed by both "
                    f"{seen_keys[key]!r} and {override.canonical!r}"
                )
            seen_keys[key] = override.canonical

    index_cfg = _parse_mapping_block(raw, "series_index")

    # A real Boolean, not a truthy value. bool("false") and bool("no") are
    # both True, so coercion turned two ordinary ways of writing "off" into
    # index authority switched on -- and the index makes placement sticky
    # with Comix-first priority, which encodes an adult determination.
    # Silently enabling that is the wrong way to fail. isinstance rejects 0
    # and 1 for free: they are int, not bool.
    if "enabled" not in index_cfg:
        index_enabled = False
    elif not isinstance(index_cfg["enabled"], bool):
        raise RoutingConfigError(
            f"series_index.enabled must be true or false, got "
            f"{index_cfg['enabled']!r}"
        )
    else:
        index_enabled = index_cfg["enabled"]

    # Absent, null, and [] all mean "index every configured destination" --
    # SeriesIndex.build expands the empty tuple -- so the permissiveness here
    # is deliberate and the three forms are genuinely equivalent. Anything
    # else must be an array of strings, validated on its own contract before
    # the reference check below. A scalar "comix" would otherwise iterate
    # into characters and fail with "destination 'c' is not defined", which
    # points nowhere near the real mistake.
    raw_dests = index_cfg.get("destinations")
    if raw_dests is None:
        index_dests: tuple[str, ...] = ()
    elif not isinstance(raw_dests, list):
        raise RoutingConfigError(
            f"series_index.destinations must be an array of strings, got "
            f"{type(raw_dests).__name__}"
        )
    else:
        for position, dest in enumerate(raw_dests):
            if not isinstance(dest, str):
                raise RoutingConfigError(
                    f"series_index.destinations entry {position} must be a "
                    f"string, got {type(dest).__name__}"
                )
        index_dests = tuple(raw_dests)

    for dest in index_dests:
        if dest not in destinations:
            raise RoutingConfigError(
                f"series_index destination {dest!r} is not defined"
            )

    # Fail closed: a malformed unresolved block must raise rather than
    # quietly disable itself, or an operator who typoed the key would believe
    # unclassified archives were being held back when they were not.
    unresolved_destination = None
    if "unresolved" in raw:
        # Present-but-null is malformed, not absent. Treating it as absent
        # would let an operator write `"unresolved": null`, believe malformed
        # configuration is rejected, and get a silent fallthrough to default
        # instead -- the exact failure this block is meant to make impossible.
        block = raw["unresolved"]
        if not isinstance(block, dict):
            raise RoutingConfigError(
                f"'unresolved' must be an object, got {type(block).__name__}"
            )
        dest = _strip_comments(block, "'unresolved'").get("destination")
        if not isinstance(dest, str) or not dest.strip():
            raise RoutingConfigError(
                f"unresolved.destination must be a non-empty string, got {dest!r}"
            )
        if dest not in destinations:
            raise RoutingConfigError(
                f"unresolved destination {dest!r} is not one of {sorted(destinations)}"
            )
        unresolved_destination = dest

    cfg = RoutingConfig(
        destinations=destinations,
        default_key=default_key,
        lists=_parse_lists(raw),
        signals=_parse_mapping_block(raw, "signals"),
        # Comments are stripped from each rule too, so the runtime model and
        # to_v2_dict() carry only semantic configuration. Comments live in the
        # file a person edits, not in the canonical serialisation.
        rules=[_strip_comments(r, f"rules[{i}]")
               for i, r in enumerate(_parse_sequence_block(raw, "rules"))],
        source_version=source_version,
        series_overrides=tuple(overrides),
        series_index_enabled=index_enabled,
        series_index_destinations=index_dests,
        unresolved_destination=unresolved_destination,
    )

    for index, rule in enumerate(cfg.rules):
        if "when" not in rule:
            raise RoutingConfigError(f"rule {index} has no 'when'")
        if rule.get("dest") not in destinations:
            raise RoutingConfigError(
                f"rule {index} destination {rule.get('dest')!r} is not defined"
            )
        strength = rule.get("strength", DEFAULT_RULE_STRENGTH)
        if strength not in RULE_STRENGTHS:
            raise RoutingConfigError(
                f"rule {index} strength {strength!r} must be one of "
                f"{sorted(RULE_STRENGTHS)}"
            )
        # Validate the predicate now rather than on the first archive that
        # happens to reach it.
        _validate_predicate(rule["when"], cfg)

    for name, signal in cfg.signals.items():
        _validate_predicate(signal, cfg, _seen={name})

    return cfg


def _validate_predicate(node: Any, cfg: RoutingConfig,
                        _seen: set[str] | None = None) -> None:
    seen = set(_seen or ())
    if isinstance(node, str):
        if node not in cfg.signals:
            raise RoutingConfigError(f"unknown signal: {node!r}")
        if node in seen:
            raise RoutingConfigError(f"signal {node!r} references itself")
        _validate_predicate(cfg.signals[node], cfg, seen | {node})
        return

    if not isinstance(node, dict):
        raise RoutingConfigError(f"predicate must be an object or signal name: {node!r}")

    for combinator in COMBINATORS:
        if combinator in node:
            children = node[combinator]
            if combinator == "not":
                _validate_predicate(children, cfg, seen)
                return
            if not isinstance(children, list) or not children:
                raise RoutingConfigError(f"'{combinator}' needs a non-empty list")
            for child in children:
                _validate_predicate(child, cfg, seen)
            return

    if "field" not in node:
        raise RoutingConfigError(f"matcher missing 'field': {node!r}")
    operators = [k for k in node if k != "field"]
    if len(operators) != 1:
        raise RoutingConfigError(
            f"matcher on {node['field']!r} needs exactly one operator, got {operators}"
        )
    op = operators[0]
    if op not in VALID_OPERATORS:
        raise RoutingConfigError(f"unknown operator {op!r} on field {node['field']!r}")
    if op in ("in_list", "glob_in_list", "glob_tokens_in_list") \
            and str(node[op]) not in cfg.lists:
        raise RoutingConfigError(f"unknown list: {node[op]!r}")


def load(path: Path) -> RoutingConfig:
    """Read and validate a routing config. Raises rather than degrading."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError as exc:
        raise RoutingConfigError(f"routing config not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RoutingConfigError(f"routing config is not valid JSON: {exc}") from exc
    return parse(raw)


def to_v2_dict(cfg: RoutingConfig) -> dict:
    """Serialise back to the v2 on-disk shape, for --migrate-routing."""
    return {
        "version": 2,
        "destinations": cfg.destinations,
        "default": cfg.default_key,
        "lists": cfg.lists,
        "signals": cfg.signals,
        "rules": cfg.rules,
        "series_overrides": [
            {
                "canonical": o.canonical,
                "aliases": list(o.aliases),
                **({"dest": o.dest_key} if o.dest_key else {}),
            }
            for o in cfg.series_overrides
        ],
        "series_index": {
            "enabled": cfg.series_index_enabled,
            "destinations": list(cfg.series_index_destinations),
        },
        # Omitted when unset, so a config that never enabled it round-trips
        # to a file that still has not enabled it.
        **({"unresolved": {"destination": cfg.unresolved_destination}}
           if cfg.unresolved_destination else {}),
    }
