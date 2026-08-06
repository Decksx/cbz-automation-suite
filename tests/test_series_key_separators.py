"""Underscore normalizes as hyphen does, and what that merges (issue #44 B).

This is an *identity* change, not a cleanup. `series_key` decides what "the
same series" means for the router, `SeriesIndex`, the series-operation lock
registry, the reclassifier, and library maintenance, so names that were
distinct before this change are the same series after it.

Before #44 B, `\\w` matched `_` and a plain negated class kept it, so `-` and
`_` normalized differently:

    "Attack-on-Titan"  ->  "attack on titan"
    "Attack_on_Titan"  ->  "attack_on_titan"     a different series

Scanlator releases use both interchangeably, so that split was real rather
than theoretical.

These tests pin the exact before/after corpus, name every input that newly
collides, and check the consequence at the consumers rather than only at the
helper -- a key function that agrees while `SeriesIndex` still hands out two
entries would be a change that had not actually happened.
"""

from __future__ import annotations

import pytest

from scripts.cbz_lock_order import SeriesLockRegistry, lock_key
from scripts.cbz_routing import series_key


# ── the exact corpus ─────────────────────────────────────────────

# (input, key). Every entry was measured against the implementation rather
# than predicted, and the "changed" set below is the identity impact.
EXPECTED = {
    # separators, singly and in runs
    "A_B": "a b",
    "A-B": "a b",
    "A B": "a b",
    "A__B": "a b",
    "A--B": "a b",
    "A_-B": "a b",
    "A - B": "a b",
    "A___B_C": "a b c",
    # surrounding and repeated internal whitespace
    "  A_B  ": "a b",
    "A_B   C": "a b c",
    "\tA_B\n": "a b",
    # realistic titles
    "Attack_on_Titan": "attack on titan",
    "Attack-on-Titan": "attack on titan",
    "Attack on Titan": "attack on titan",
    "One_Punch_Man": "one punch man",
    "One-Punch Man": "one punch man",
    # leading and trailing separators
    "_leading": "leading",
    "trailing_": "trailing",
    "_both_": "both",
    # separators only
    "___": "",
    "---": "",
    "_-_": "",
    # non-separator characters keep the existing contract
    "Re:Zero": "re zero",
    "Dr. STONE": "dr stone",
    "JoJo's": "jojo s",
    "Mob Psycho 100": "mob psycho 100",
    "5 Centimeters per Second": "5 centimeters per second",
    "Berserk (Uncensored)": "berserk",
    "BERSERK": "berserk",
    "": "",
}

# Inputs whose key this change moved. Listed explicitly rather than computed,
# so the identity impact is reviewable and a future change to the rule cannot
# quietly widen it.
NEWLY_CHANGED = {
    "A_B": ("a_b", "a b"),
    "A__B": ("a__b", "a b"),
    "A_-B": ("a_ b", "a b"),
    "A___B_C": ("a___b_c", "a b c"),
    "  A_B  ": ("a_b", "a b"),
    "A_B   C": ("a_b c", "a b c"),
    "\tA_B\n": ("a_b", "a b"),
    "Attack_on_Titan": ("attack_on_titan", "attack on titan"),
    "One_Punch_Man": ("one_punch_man", "one punch man"),
    "_leading": ("_leading", "leading"),
    "trailing_": ("trailing_", "trailing"),
    "_both_": ("_both_", "both"),
    "___": ("___", ""),
    "_-_": ("_ ", ""),
}


@pytest.mark.parametrize("name, expected", sorted(EXPECTED.items()))
def test_the_exact_key_for_every_corpus_entry(name, expected):
    assert series_key(name) == expected


@pytest.mark.parametrize("name, before_after", sorted(NEWLY_CHANGED.items()))
def test_every_newly_changed_input_is_accounted_for(name, before_after):
    """The identity impact, entry by entry.

    `before` is what the previous rule produced, kept here as the record of
    what moved. If a later change alters one of these again, this fails and
    the impact has to be re-stated rather than absorbed.
    """
    before, after = before_after
    assert before != after, f"{name!r} is listed as changed but did not change"
    assert series_key(name) == after


def test_the_changed_set_is_complete():
    """No corpus entry moved without being listed.

    Guards the list above from going stale: recomputing the old rule here
    means a newly-affected input cannot slip through undocumented.
    """
    import re

    old_punct = re.compile(r"[^\w\s]")
    marker = re.compile(r"\b(?:uncensored|decensored)\b", re.IGNORECASE)
    spaces = re.compile(r"\s+")

    def old_key(name):
        n = marker.sub("", name or "")
        n = old_punct.sub(" ", n.lower())
        return spaces.sub(" ", n).strip()

    moved = {n for n in EXPECTED if old_key(n) != series_key(n)}
    assert moved == set(NEWLY_CHANGED), (
        f"undocumented: {sorted(moved - set(NEWLY_CHANGED))}; "
        f"listed but unchanged: {sorted(set(NEWLY_CHANGED) - moved)}"
    )


# ── the collision proof ──────────────────────────────────────────


@pytest.mark.parametrize("a, b", [
    ("A_B", "A-B"),
    ("A_B", "A B"),
    ("Attack_on_Titan", "Attack-on-Titan"),
    ("Attack_on_Titan", "Attack on Titan"),
    ("One_Punch_Man", "One-Punch Man"),
    ("A__B", "A B"),
    ("A_-B", "A - B"),
])
def test_previously_distinct_names_now_collide(a, b):
    """The point of the change, stated as the merge it performs."""
    assert series_key(a) == series_key(b)


def test_non_separator_characters_still_separate_series():
    """The change must not merge things that are genuinely different."""
    assert series_key("Berserk") != series_key("Berserk 2")
    assert series_key("A B") != series_key("AB")
    assert series_key("One Piece") != series_key("One Punch")


# ── the consumers, not just the helper ───────────────────────────


def test_the_lock_registry_reuses_one_lock_for_equivalent_names():
    """Two arrivals spelled differently must contend for the same lock.

    Checked at the registry rather than at lock_key alone: returning one lock
    object is the property that actually serializes them.
    """
    registry = SeriesLockRegistry()
    first = registry.for_series("Attack_on_Titan")
    assert first is registry.for_series("Attack-on-Titan")
    assert first is registry.for_series("Attack on Titan")
    assert len(registry) == 1


def test_the_lock_key_agrees_with_the_canonical_rule():
    """lock_key adds NFC but must not disagree about separators."""
    assert lock_key("Attack_on_Titan") == lock_key("Attack on Titan")
    assert lock_key("A_B") == lock_key("A-B")


def test_a_separator_only_name_is_refused_by_the_lock_registry():
    """New consequence, pinned rather than discovered later.

    "___" used to key to "___" and would have received a lock. It now keys to
    "", which the registry rejects -- the same treatment "!!!" has always had.
    """
    assert series_key("___") == ""
    with pytest.raises(ValueError, match="normalizes to nothing"):
        SeriesLockRegistry().for_series("___")


def test_the_series_index_resolves_equivalent_names_through_one_key():
    """The index must not hand out two entries for one series.

    This is the consumer that would make the change cosmetic if it
    disagreed: a key function that merges while SeriesIndex still stores two
    entries has not merged anything.
    """
    from scripts.cbz_routing import SeriesIndex
    from pathlib import Path

    index = SeriesIndex(priority=("manga", "comix"))
    index.add("Attack_on_Titan", "manga", Path("X:/Manga/Attack on Titan"))

    assert index.lookup("Attack-on-Titan") is not None
    assert index.lookup("Attack on Titan") is not None
    assert index.lookup("Attack_on_Titan") is not None
    assert len(index) == 1, "the index stored more than one entry for one series"


def test_adding_both_spellings_does_not_create_a_second_entry():
    from scripts.cbz_routing import SeriesIndex
    from pathlib import Path

    index = SeriesIndex(priority=("manga", "comix"))
    index.add("One_Punch_Man", "manga", Path("X:/Manga/One-Punch Man"))
    index.add("One-Punch Man", "manga", Path("X:/Manga/One-Punch Man"))
    assert len(index) == 1
