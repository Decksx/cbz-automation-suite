"""Canonical Unicode composition in `series_key` (issue #44, third strand).

`series_key` now normalizes to NFC before anything else. This is an *identity*
change: it decides what "the same series" means for the router, `SeriesIndex`,
the series-operation lock registry, the reclassifier, and library maintenance.

Before this change `series_key` applied no normalization, and a combining mark
is not a word character, so the punctuation rule turned it into a space:

    NFC  "K\u00e4ntai"        ->  "k\u00e4ntai"
    NFD  "Ka" + U+0308 + "ntai"  ->  "ka ntai"     a different series

Content arriving from a macOS-side share is routinely NFD, so the split was
reachable rather than theoretical.

**The impact runs in two directions, and the second is the surprising one.**

    merges   the NFC and NFD spellings of one title, which is the point
    splits   an NFD-accented name from its unaccented ASCII spelling

The split happens because the old rule did not merely fail to compose an NFD
accent -- it *deleted* it. "Cafe" + U+0301 keyed to "cafe", colliding with the
plain ASCII "Cafe". Composing the accent instead of destroying it is correct,
but it means a name that used to share an identity with its ASCII spelling no
longer does.

Ordering matters and is asserted here: NFC must run before the punctuation
rule, because that rule is what destroys the mark. Normalizing afterwards
would compose nothing, having already lost the input.

Scope: canonical composition only. No casefolding, no NFKC/NFKD compatibility
mapping, no accent stripping, no transliteration -- those change which series
are genuinely distinct, whereas NFC only reconciles two encodings of an
identical character sequence.
"""

from __future__ import annotations

import re
import unicodedata

import pytest

from scripts.cbz_lock_order import SeriesLockRegistry, lock_key
from scripts.cbz_routing import SeriesIndex, series_key


# ── the pre-NFC rule, reconstructed ──────────────────────────────

# Same discipline as the separator corpus: historical values are *derived*
# from the rule they claim to describe rather than trusted as literals. A
# wrong recorded value survived review once already (#52).
_OLD_MARKER_RE = re.compile(r"\b(?:uncensored|decensored)\b", re.IGNORECASE)
_OLD_PUNCT_RE = re.compile(r"[^\w\s]|_")
_OLD_SPACE_RE = re.compile(r"\s+")


def _old_key(name: str) -> str:
    """What `series_key` returned before NFC was added: the same rule, unnormalized."""
    text = _OLD_MARKER_RE.sub("", name or "")
    text = _OLD_PUNCT_RE.sub(" ", text.lower())
    return _OLD_SPACE_RE.sub(" ", text).strip()


# ── the corpus ───────────────────────────────────────────────────

# (label, NFC form, NFD form, old NFC key, old NFD key, new shared key).
#
# NFD forms are written as explicit escapes, never as pasted text: an editor
# or a file round-trip can silently normalize pasted combining marks, which
# would turn a real pair into two copies of the same string and make every
# assertion below pass vacuously. `test_every_pair_is_a_genuine_nfc_nfd_pair`
# enforces that they really are pairs.
PAIRS = [
    ("middle mark, the original defect",
     "K\u00e4ntai", "Ka\u0308ntai",
     "k\u00e4ntai", "ka ntai", "k\u00e4ntai"),

    ("trailing mark",
     "Caf\u00e9", "Cafe\u0301",
     "caf\u00e9", "cafe", "caf\u00e9"),

    ("middle tilde",
     "Se\u00f1or", "Sen\u0303or",
     "se\u00f1or", "sen or", "se\u00f1or"),

    ("leading base + mark",
     "\u00dcber", "U\u0308ber",
     "\u00fcber", "u ber", "\u00fcber"),

    ("accent inside a longer word",
     "Pok\u00e9mon", "Poke\u0301mon",
     "pok\u00e9mon", "poke mon", "pok\u00e9mon"),

    ("japanese voiced kana, two marks in one title",
     "\u30ac\u30f3\u30c0\u30e0", "\u30ab\u3099\u30f3\u30bf\u3099\u30e0",
     "\u30ac\u30f3\u30c0\u30e0", "\u30ab \u30f3\u30bf \u30e0",
     "\u30ac\u30f3\u30c0\u30e0"),

    ("hangul syllable vs conjoining jamo",
     "\ud55c\uae00", "\u1112\u1161\u11ab\u1100\u1173\u11af",
     "\ud55c\uae00", "\u1112\u1161\u11ab\u1100\u1173\u11af",
     "\ud55c\uae00"),

    ("two combining marks on one base",
     "\u1ea8", "A\u0302\u0309",
     "\u1ea9", "a", "\u1ea9"),
]

# Inputs with no NFC/NFD partner, kept because the edges are where a
# normalization change tends to misbehave.
EDGES = {
    "\u0301": "",                    # a bare combining mark, no base to join
    "\u0301Test": "test",            # mark at string start, still no base
    "_\u0308_": "",                  # mark surrounded by separators
    "": "",
}

# ASCII inputs that must key exactly as they did before. NFC is a no-op on
# every one of them, so any movement here is the change reaching further
# than canonical composition.
ASCII_UNCHANGED = [
    "Berserk", "BERSERK", "Berserk!!", "Berserk (Uncensored)", "Berserk  ",
    "Attack-on-Titan", "Attack_on_Titan", "Attack on Titan", "One_Punch_Man",
    "Re:Zero", "Dr. STONE", "JoJo's", "Mob Psycho 100", "___", "---", "",
    "5 Centimeters per Second",
]


# ── the corpus is what it claims to be ───────────────────────────


@pytest.mark.parametrize("label, nfc, nfd, _old_nfc, _old_nfd, _new",
                         PAIRS, ids=[p[0] for p in PAIRS])
def test_every_pair_is_a_genuine_nfc_nfd_pair(label, nfc, nfd, _old_nfc,
                                              _old_nfd, _new):
    """Guards the corpus against silently degrading into ASCII stand-ins.

    If a combining mark were lost in transit the two forms would be equal,
    and every convergence assertion below would pass without testing
    anything. Checked in both directions so a merely *similar* pair does not
    qualify.
    """
    assert nfc != nfd, f"{label}: the two forms are the same string"
    assert unicodedata.normalize("NFC", nfd) == nfc, f"{label}: NFC(nfd) != nfc"
    assert unicodedata.normalize("NFD", nfc) == nfd, f"{label}: NFD(nfc) != nfd"
    assert any(unicodedata.combining(c) for c in nfd) or "\u1100" <= nfd[0] <= "\u11ff", (
        f"{label}: the NFD form carries no combining mark or conjoining jamo")


# ── the identity impact, stated exactly ──────────────────────────


@pytest.mark.parametrize("label, nfc, nfd, old_nfc, old_nfd, new",
                         PAIRS, ids=[p[0] for p in PAIRS])
def test_the_exact_before_and_after_for_every_pair(label, nfc, nfd, old_nfc,
                                                   old_nfd, new):
    """Both historical values and both current values, per entry.

    The recorded `old_*` values are checked against the reconstructed old
    rule rather than merely asserted to differ, so a wrong literal cannot
    survive here the way one did in the separator corpus.
    """
    assert _old_key(nfc) == old_nfc, f"{label}: recorded old NFC key is wrong"
    assert _old_key(nfd) == old_nfd, f"{label}: recorded old NFD key is wrong"
    assert series_key(nfc) == new
    assert series_key(nfd) == new


@pytest.mark.parametrize("label, nfc, nfd, _o1, _o2, _n",
                         PAIRS, ids=[p[0] for p in PAIRS])
def test_composed_and_decomposed_forms_are_one_series(label, nfc, nfd, _o1,
                                                      _o2, _n):
    """The merge direction: the whole point of the change."""
    assert series_key(nfc) == series_key(nfd)


@pytest.mark.parametrize("label, nfc, nfd, _o1, _o2, _n",
                         PAIRS, ids=[p[0] for p in PAIRS])
def test_every_pair_was_actually_split_before(label, nfc, nfd, _o1, _o2, _n):
    """Each pair must have been a real defect, not a case that already worked.

    Without this the corpus could fill up with entries that never diverged,
    and the suite would look like it covered composition while proving
    nothing about it.
    """
    assert _old_key(nfc) != _old_key(nfd), (
        f"{label}: these already agreed before the change, so this entry "
        f"demonstrates nothing")


def test_an_nfd_accent_no_longer_collides_with_its_ascii_spelling():
    """The split direction, pinned because it is the surprising one.

    The old rule deleted an NFD accent rather than failing to compose it, so
    "Cafe" + U+0301 keyed to "cafe" and shared an identity with plain ASCII
    "Cafe". Composing it is correct, but it separates two names that used to
    be one series -- which is why an already-built index has to be rebuilt
    rather than topped up.
    """
    nfd_accented, ascii_plain = "Cafe\u0301", "Cafe"

    assert _old_key(nfd_accented) == _old_key(ascii_plain) == "cafe"
    assert series_key(nfd_accented) != series_key(ascii_plain)
    assert series_key(nfd_accented) == "caf\u00e9"
    assert series_key(ascii_plain) == "cafe"


# ── ordering: NFC must run first ─────────────────────────────────


def test_normalizing_after_the_punctuation_rule_would_not_work():
    """Why NFC runs before marker removal, lowercasing, and punctuation.

    The punctuation rule replaces a combining mark with a space, so by the
    time it has run the mark is gone and there is nothing left to compose.
    Demonstrated by normalizing the *old* rule's output: it does not recover
    the composed key, while normalizing first does.
    """
    nfd = "Ka\u0308ntai"

    too_late = unicodedata.normalize("NFC", _old_key(nfd))
    assert too_late == "ka ntai", "the mark survived the punctuation rule"
    assert too_late != series_key(nfd)

    assert series_key(nfd) == "k\u00e4ntai"


# ── what must not change ─────────────────────────────────────────


@pytest.mark.parametrize("name", ASCII_UNCHANGED)
def test_ascii_and_separator_behaviour_is_untouched(name):
    """NFC is a no-op on ASCII, so these must key exactly as before.

    Compared against the reconstructed old rule rather than against recorded
    literals, so this cannot drift out of agreement with the real previous
    behaviour.
    """
    assert series_key(name) == _old_key(name)


@pytest.mark.parametrize("name, expected", sorted(EDGES.items()))
def test_the_edges_key_as_measured(name, expected):
    assert series_key(name) == expected


def test_none_still_returns_empty_string():
    """Unchanged by this PR, asserted because normalization touches the same line.

    `name or ""` moved inside the `unicodedata.normalize` call, which is
    exactly the kind of edit that silently turns None into a TypeError.
    """
    assert series_key(None) == ""
    assert lock_key(None) == ""


@pytest.mark.parametrize("a, b", [
    ("K\u00e4ntai", "Kantai"),
    ("Caf\u00e9", "Cafe"),
    ("Se\u00f1or", "Senor"),
    ("\u00dcber", "Uber"),
    ("\u30ac", "\u30ab"),
    ("Berserk", "Vinland Saga"),
])
def test_genuinely_different_names_stay_different(a, b):
    """NFC must not become "merge all accents".

    Canonical composition reconciles two encodings of the same character. It
    does not make an accented character equal to its unaccented counterpart,
    which is what NFKC-style folding or accent stripping would do -- and both
    are deliberately out of scope.
    """
    assert series_key(a) != series_key(b)


# ── the consumers, not just the helper ───────────────────────────


@pytest.mark.parametrize("label, nfc, nfd, _o1, _o2, _n",
                         PAIRS, ids=[p[0] for p in PAIRS])
def test_the_series_index_stores_one_entry_per_composition_pair(label, nfc, nfd,
                                                                _o1, _o2, _n):
    """The consumer that would make the change cosmetic if it disagreed.

    A key function that merges while `SeriesIndex` still hands out two
    entries has not merged anything.
    """
    from pathlib import Path

    index = SeriesIndex(priority=("manga", "comix"))
    index.add(nfc, "manga", Path("X:/Manga/Series"))

    assert index.lookup(nfd) is not None, f"{label}: NFD form missed the index"
    assert index.lookup(nfc) is not None

    index.add(nfd, "manga", Path("X:/Manga/Series"))
    assert len(index) == 1, f"{label}: the index stored two entries for one series"


@pytest.mark.parametrize("label, nfc, nfd, _o1, _o2, _n",
                         PAIRS, ids=[p[0] for p in PAIRS])
def test_the_lock_registry_returns_one_lock_per_composition_pair(label, nfc, nfd,
                                                                 _o1, _o2, _n):
    """Returning the same lock object is what actually serializes them.

    Asserted at the registry rather than at `lock_key` alone: a registry that
    minted a fresh lock per call would serialize nothing while appearing to.
    """
    registry = SeriesLockRegistry()
    first = registry.for_series(nfc)

    assert first is registry.for_series(nfd), f"{label}: two locks for one series"
    assert len(registry) == 1


@pytest.mark.parametrize("label, nfc, nfd, _o1, _o2, _n",
                         PAIRS, ids=[p[0] for p in PAIRS])
def test_lock_key_agrees_with_the_canonical_rule(label, nfc, nfd, _o1, _o2, _n):
    """`lock_key` no longer adds normalization of its own; it must still agree."""
    assert lock_key(nfc) == series_key(nfc)
    assert lock_key(nfd) == series_key(nfd)
    assert lock_key(nfc) == lock_key(nfd)


def test_lock_key_is_now_exactly_the_canonical_rule():
    """It used to be deliberately coarser; after #44 the two agree exactly.

    Pinned because the coarseness was a documented, intentional asymmetry
    that this change removes. If `lock_key` ever needs to diverge again, that
    has to be a decision rather than a drift.
    """
    for name in ASCII_UNCHANGED + [p[1] for p in PAIRS] + [p[2] for p in PAIRS]:
        assert lock_key(name) == series_key(name), f"diverged on {name!r}"


# ── the assumption lock_key now delegates on ─────────────────────


def test_series_key_output_is_always_nfc():
    """Pins the measurement that let `lock_key` drop its outer normalization.

    `lock_key` used to wrap `series_key` in NFC on both sides. The inner call
    is redundant by construction now that `series_key` normalizes its own
    input; the outer call is redundant only if `series_key` can never *emit*
    a non-NFC string. That was measured over 14,800,248 probes -- every
    codepoint in four embeddings, and every cased character crossed with
    every combining mark in three orders -- with zero non-NFC outputs.

    A full rescan is far too slow for the suite, so this covers the cases
    that could plausibly break it: characters whose lowercase mapping emits a
    combining sequence, crossed with marks that might then compose. If this
    ever fails, `lock_key`'s delegation is no longer safe.
    """
    probes = list(ASCII_UNCHANGED)
    probes += [p[1] for p in PAIRS] + [p[2] for p in PAIRS]
    probes += list(EDGES)
    # "\u0130" (I with dot above) lowercases to "i" + U+0307, the standard
    # example of a case mapping that produces a combining sequence.
    for base in ("\u0130", "\u1e9e", "\u01c5", "A", "a", "\u00c4"):
        for mark in ("\u0301", "\u0308", "\u0307", "\u0323", "\u3099"):
            probes += [base + mark, mark + base, "A" + base + mark + "B"]

    for probe in probes:
        out = series_key(probe)
        assert unicodedata.normalize("NFC", out) == out, (
            f"series_key({probe!r}) returned non-NFC {out!r}; lock_key's "
            f"delegation to it is no longer safe")


# ── one canonical implementation, no new copies ──────────────────


def test_the_consumers_still_share_the_canonical_helper():
    """No consumer may add normalization of its own.

    The whole value of #44 is one definition of "same series". A second
    normalization anywhere -- even a correct one -- recreates the divergence
    the consolidation removed, and nothing would detect it.
    """
    import scripts.cbz_library_maintenance as maintenance
    import scripts.cbz_lock_order as lock_order
    import scripts.cbz_watcher as watcher
    from scripts.cbz_routing import series_key as canonical

    assert watcher._series_key is canonical
    assert maintenance.normalise_series_key is canonical
    assert lock_order.series_key is canonical


def test_only_the_canonical_module_normalizes_unicode():
    """Asserted on source text, which catches a copy that is defined but unused.

    Object identity above catches a copy that is wired in; it cannot see one
    sitting in a module waiting to be called. That is the state a
    half-finished edit leaves behind.
    """
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    offenders = []
    for module in ("scripts/cbz_watcher.py",
                   "scripts/cbz_library_maintenance.py",
                   "scripts/cbz_lock_order.py",
                   "scripts/cbz_library_reclassify.py"):
        text = (repo / module).read_text(encoding="utf-8", errors="replace")
        if "unicodedata" in text:
            offenders.append(module)

    assert not offenders, (
        f"these modules reference unicodedata directly and should delegate to "
        f"cbz_routing.series_key instead: {offenders}")
