"""Tests for the canonical file-name formatter (design §6 step 3 part A).

The design (`docs/canonical_naming_and_version_supersession.md`) requires the
formatter to be a fixpoint (§5.5) and to refuse rather than drop (§2.2,
§2.3). The four §7 named tests that belong to the formatter are here under
their design names; the rest pin each grammar part, the path-safety rules,
and every refusal, so that removing a guard fails a test by name.

The round-trip property runs over a seeded `random.Random` generator rather
than hypothesis, which is not a dependency of this repository.
"""

from __future__ import annotations

import random

import pytest

from scripts.cbz_canonical_name import (
    DEFAULT_MAX_NAME_LENGTH,
    EDITION_WORDS,
    SPECIAL_WORDS,
    AmbiguousSeriesNameError,
    CanonicalFields,
    CanonicalNameError,
    EmptySeriesNameError,
    IssueAndChapterError,
    MalformedNumberError,
    MalformedVersionError,
    MalformedYearError,
    NameTooLongError,
    NotCanonicalNameError,
    ReservedNameError,
    UnknownEditionError,
    UnknownSpecialWordError,
    find_canonical_collisions,
    format_canonical_name,
    parse_canonical_name,
    sanitize_series_name,
)

F = CanonicalFields
fmt = format_canonical_name
parse = parse_canonical_name


# ---------------------------------------------------------------------------
# §7 named tests
# ---------------------------------------------------------------------------


def test_canonical_name_is_a_fixpoint():
    """format(parse(format(f))) == format(f), and parse(format(f)) == f."""
    cases = [
        F("Series Name", volume=1, issue=3, year=2019),
        F("Series Name", chapter=1, year=2019),
        F("Series Name", chapter=1, year=2019, version=3),
        F("Berserk", volume=41, edition="Uncensored"),
        F("One Piece", special="Omnibus", volume="01-03"),
        F("Batman].cC"),
    ]
    for f in cases:
        name = fmt(f)
        assert parse(name) == f
        assert fmt(parse(name)) == name
        # And a second lap changes nothing.
        assert fmt(parse(fmt(parse(name)))) == name


def test_version_marker_can_never_parse_as_a_volume():
    name = fmt(F("Series Name", chapter=1, year=2019, version=3))
    assert name == "Series Name ch. 1 (2019) [v3].cbz"
    parsed = parse(name)
    assert parsed.volume is None
    assert parsed.version == 3
    # Volume and version together stay in their own slots.
    both = parse(fmt(F("Series Name", volume=3, issue=1, version=3)))
    assert (both.volume, both.version) == ("3", 3)
    # A bare "[v3]" in the series display name is refused, never re-read.
    with pytest.raises(AmbiguousSeriesNameError):
        F("Series Name [v3]")


def test_fractional_range_and_special_numbers_round_trip():
    expected = {
        F("S", issue="12.5"): "S #012.5.cbz",
        F("S", chapter="12.5"): "S ch. 12.5.cbz",
        F("S", chapter="1-3"): "S ch. 1-3.cbz",
        F("S", volume="01-02"): "S v01-02.cbz",
        F("S", volume=1, issue="1-3"): "S v01 #001-003.cbz",
        F("S", issue="0"): "S #000.cbz",
        F("S", chapter="1.5-2"): "S ch. 1.5-2.cbz",
        F("Series Name", special="Special", year=2019): "Series Name Special (2019).cbz",
        F("S", special="One-shot"): "S One-shot.cbz",
    }
    for f, name in expected.items():
        assert fmt(f) == name
        assert parse(name) == f


def test_display_name_keeps_year_version_and_edition_tokens():
    name = fmt(F("Series Name", chapter=1, year=2019, edition="Colored", version=2))
    assert name == "Series Name ch. 1 (2019) (Colored) [v2].cbz"
    for token in ("(2019)", "(Colored)", "[v2]"):
        assert token in name


# ---------------------------------------------------------------------------
# Grammar, part by part
# ---------------------------------------------------------------------------


def test_operator_examples_format_exactly():
    assert fmt(F("Series Name", volume=1, issue=3, year=2019)) == "Series Name v01 #003 (2019).cbz"
    assert fmt(F("Series Name", chapter=1, year=2019)) == "Series Name ch. 1 (2019).cbz"
    assert fmt(F("Series Name", chapter=1, year=2019, version=3)) == "Series Name ch. 1 (2019) [v3].cbz"


def test_series_only_name():
    assert fmt(F("Series Name")) == "Series Name.cbz"


@pytest.mark.parametrize(
    ("volume", "expected"),
    [(1, "v01"), ("1", "v01"), ("01", "v01"), ("001", "v01"), (12, "v12"), (123, "v123"), ("1.5", "v01.5")],
)
def test_volume_is_padded_to_at_least_two_digits(volume, expected):
    assert fmt(F("S", volume=volume)) == f"S {expected}.cbz"


@pytest.mark.parametrize(
    ("issue", "expected"),
    [(1, "#001"), (12, "#012"), (123, "#123"), (1234, "#1234"), ("0003", "#003"), ("12.5", "#012.5")],
)
def test_issue_is_padded_to_at_least_three_digits(issue, expected):
    assert fmt(F("S", issue=issue)) == f"S {expected}.cbz"


@pytest.mark.parametrize(("chapter", "expected"), [(1, "ch. 1"), ("001", "ch. 1"), ("12.5", "ch. 12.5"), (100, "ch. 100")])
def test_chapter_is_unpadded(chapter, expected):
    assert fmt(F("S", chapter=chapter)) == f"S {expected}.cbz"


def test_fraction_digits_are_kept_verbatim():
    # "12.50" is not silently shortened to "12.5": whether the zero matters
    # is a resolution question.
    assert fmt(F("S", chapter="12.50")) == "S ch. 12.50.cbz"
    assert F("S", chapter="12.50") != F("S", chapter="12.5")


def test_slot_order_is_fixed():
    name = fmt(
        F("S", special="Omnibus", volume=2, issue=5, year=2001, edition="English", version=4)
    )
    assert name == "S Omnibus v02 #005 (2001) (English) [v4].cbz"


def test_volume_combines_with_chapter():
    # The §2.1 grammar admits it; see the module docstring.
    assert fmt(F("S", volume=3, chapter=20)) == "S v03 ch. 20.cbz"


def test_equivalent_inputs_normalise_to_equal_fields():
    assert F("S", volume="01", issue="003", year="2019", version="3") == F(
        "S", volume=1, issue=3, year=2019, version=3
    )


# ---------------------------------------------------------------------------
# Closed lists
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("edition", EDITION_WORDS)
def test_every_listed_edition_round_trips(edition):
    f = F("S", chapter=1, edition=edition)
    assert parse(fmt(f)) == f


@pytest.mark.parametrize("special", SPECIAL_WORDS)
def test_every_listed_special_word_round_trips(special):
    f = F("S", special=special, year=2019)
    assert parse(fmt(f)) == f


@pytest.mark.parametrize("edition", ["Digital", "uncensored", "Colour", "", "Uncensored English"])
def test_edition_outside_the_closed_list_is_refused(edition):
    with pytest.raises(UnknownEditionError):
        F("S", edition=edition)


@pytest.mark.parametrize("special", ["Annual", "special", "Oneshot", ""])
def test_special_word_outside_the_closed_list_is_refused(special):
    with pytest.raises(UnknownSpecialWordError):
        F("S", special=special)


def test_lists_name_the_operator_decisions():
    for word in ("Uncensored", "Decensored", "Colored", "Colorized", "English",
                 "Special Edition", "Limited Edition", "Alternate Cover"):
        assert word in EDITION_WORDS
    for word in ("Omnibus", "Compilation", "Complete", "Special", "Extra",
                 "Bonus", "Omake", "One-shot"):
        assert word in SPECIAL_WORDS


# ---------------------------------------------------------------------------
# Refusals: invalid fields raise, never silently drop
# ---------------------------------------------------------------------------


def test_issue_and_chapter_are_mutually_exclusive():
    with pytest.raises(IssueAndChapterError):
        F("S", issue=1, chapter=1)


@pytest.mark.parametrize(
    "number",
    ["", "1.", ".5", "-1", "1-", "3-1", "2-2", "1-2-3", "1e3", " 1", "1 ", "one", "１", 1.5, -1, True, [1]],
)
def test_malformed_numbers_are_refused(number):
    for slot in ("volume", "issue", "chapter"):
        with pytest.raises(MalformedNumberError):
            F("S", **{slot: number})


@pytest.mark.parametrize("year", [19, 999, 10000, "19", "20190", "２０１９", 2019.0, True, "abcd"])
def test_malformed_years_are_refused(year):
    with pytest.raises(MalformedYearError):
        F("S", year=year)


@pytest.mark.parametrize("version", [0, -1, "0", "03", "1.5", 1.0, True, "v3", ""])
def test_malformed_versions_are_refused(version):
    with pytest.raises(MalformedVersionError):
        F("S", version=version)


@pytest.mark.parametrize("series", ["", "   ", "...", " . . ", "???", "<>:\"/\\|?*", "\x00\x1f"])
def test_series_empty_after_sanitising_is_refused(series):
    with pytest.raises(EmptySeriesNameError):
        F(series)


@pytest.mark.parametrize(
    "series",
    ["Batman v01", "Batman v1", "Batman V2", "Batman #3", "Batman ch. 1", "Batman Ch.12",
     "Batman (1989)", "Batman [v2]", "Batman (Uncensored)", "Batman (uncensored)",
     "Batman Special", "Batman omnibus", "v2", "Omake"],
)
def test_series_ending_in_a_name_token_is_refused(series):
    """A trailing token belongs in its field; left in the series it would be
    read back as that field (or misread by a human)."""
    with pytest.raises(AmbiguousSeriesNameError):
        F(series)


@pytest.mark.parametrize(
    "series",
    ["Batman v01 Returns", "Area 51", "Specialists", "The Complete Works of X", "Bach 3",
     "Mr. Smith", "Re:Zero", "Batman [Judas]"],
)
def test_series_with_tokens_only_inside_is_accepted(series):
    f = F(series)
    assert parse(fmt(f)) == f


def test_round_trip_self_check_backstops_the_trailing_token_guard(monkeypatch):
    """With the trailing-token guard disabled, the formatter's own round-trip
    check must still refuse a series that would read back as a volume. In
    normal operation the guard fires first, so only this test reaches the
    backstop."""
    import re

    import scripts.cbz_canonical_name as mod

    monkeypatch.setattr(mod, "_TRAILING_TOKEN_RE", re.compile(r"(?!)"))
    f = mod.CanonicalFields("Batman v01")
    with pytest.raises(AmbiguousSeriesNameError):
        mod.format_canonical_name(f)


def test_refusals_share_one_base_class():
    for exc in (EmptySeriesNameError, AmbiguousSeriesNameError, IssueAndChapterError,
                MalformedNumberError, MalformedYearError, MalformedVersionError,
                UnknownEditionError, UnknownSpecialWordError, ReservedNameError,
                NameTooLongError, NotCanonicalNameError):
        assert issubclass(exc, CanonicalNameError)


# ---------------------------------------------------------------------------
# Windows path safety
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("char", list('<>:"/\\|?*') + [chr(c) for c in range(32)] + ["\x7f"])
def test_illegal_characters_are_replaced(char):
    f = F(f"Bat{char}man")
    name = fmt(f)
    assert char not in name
    assert name == "Bat man.cbz"


def test_trailing_dots_and_spaces_are_stripped():
    assert F("Batman. . .").series == "Batman"
    assert F("Batman ?.").series == "Batman"
    assert F("  Batman  ").series == "Batman"


def test_internal_dots_and_whitespace_runs():
    assert F("Mr.  Smith\t\nGoes").series == "Mr. Smith Goes"


@pytest.mark.parametrize(
    "series", ["CON", "con", "Prn", "AUX", "NUL", "COM1", "com9", "LPT1", "lpt9", "COM0", "COM¹", "Con. Smith", "NUL.x"]
)
def test_reserved_device_names_are_refused(series):
    """Device names are reserved with or without an extension and in any case;
    'Con. Smith.cbz' opens CON because Windows matches up to the first dot."""
    with pytest.raises(ReservedNameError):
        fmt(F(series))


@pytest.mark.parametrize("series", ["CONAN", "Console", "COM10", "LPT", "AUX Files"])
def test_names_that_merely_contain_a_device_name_are_accepted(series):
    assert parse(fmt(F(series))) == F(series)


def test_device_name_with_further_tokens_is_not_reserved():
    assert fmt(F("CON", volume=1)) == "CON v01.cbz"


def test_length_cap_refuses_rather_than_truncates():
    limit_series = "x" * (DEFAULT_MAX_NAME_LENGTH - len(".cbz"))
    assert len(fmt(F(limit_series))) == DEFAULT_MAX_NAME_LENGTH
    with pytest.raises(NameTooLongError):
        fmt(F(limit_series + "x"))
    with pytest.raises(NameTooLongError):
        fmt(F("abc", volume=1), max_length=10)
    assert fmt(F("abc"), max_length=7) == "abc.cbz"


def test_length_cap_counts_utf16_units():
    # One astral character is two UTF-16 units on NTFS.
    series = "\U0001f600" * 98  # 196 units
    assert fmt(F(series)) == series + ".cbz"
    with pytest.raises(NameTooLongError):
        fmt(F(series + "\U0001f600"))


def test_unicode_is_nfc_normalised():
    assert F("Pokémon") == F("Pokémon")


# ---------------------------------------------------------------------------
# Idempotent sanitising (§5.5)
# ---------------------------------------------------------------------------


def test_batman_example_is_stable_under_repeated_sanitising():
    """cbz_core.sanitize takes "Batman].cC" -> "Batman.cC" -> "" (§5.5). This
    sanitiser must neither lose text on a second pass nor reach empty."""
    once = sanitize_series_name("Batman].cC")
    assert once == "Batman].cC"
    assert sanitize_series_name(once) == once
    name = fmt(F("Batman].cC"))
    assert name == "Batman].cC.cbz"
    assert parse(name).series == "Batman].cC"


@pytest.mark.parametrize(
    "raw", ["Foo . .", "Foo ?.", " . Foo", "á?b", "x\t.\n.", "Foo:Bar", "..", "a. b.  c . "]
)
def test_sanitise_is_idempotent_on_edge_cases(raw):
    once = sanitize_series_name(raw)
    assert sanitize_series_name(once) == once


_NASTY = list("abcXYZ019 .-_[](){}#!&',") + list('<>:"/\\|?*') + [
    "\t", "\n", "\x00", "\x1f", "\x7f", " ", "　", "é", "é", "́",
    "ß", "İ", "日本", "\U0001f600", "v", "ch.", "vol",
]


def _random_text(rng: random.Random, max_parts: int = 12) -> str:
    return "".join(rng.choice(_NASTY) for _ in range(rng.randint(0, max_parts)))


def test_sanitise_is_idempotent_over_random_strings():
    rng = random.Random(20261023)
    for _ in range(5000):
        raw = _random_text(rng)
        once = sanitize_series_name(raw)
        assert sanitize_series_name(once) == once, raw


# ---------------------------------------------------------------------------
# Round-trip fixpoint over a seeded generator
# ---------------------------------------------------------------------------


def _random_number(rng: random.Random) -> str | int:
    def endpoint() -> str:
        whole = str(rng.randint(0, 1200)).zfill(rng.choice([1, 1, 2, 3, 4]))
        if rng.random() < 0.3:
            whole += "." + str(rng.randint(0, 99)).zfill(rng.choice([1, 2]))
        return whole

    if rng.random() < 0.3:
        return rng.randint(0, 1500)
    low = endpoint()
    if rng.random() < 0.2:
        return f"{low}-{endpoint()}"  # may not ascend; that must be refused
    return low


def _random_fields(rng: random.Random) -> dict:
    kwargs: dict = {"series": _random_text(rng, 16)}
    if rng.random() < 0.5:
        kwargs["volume"] = _random_number(rng)
    roll = rng.random()
    if roll < 0.4:
        kwargs["issue"] = _random_number(rng)
    elif roll < 0.8:
        kwargs["chapter"] = _random_number(rng)
    elif roll < 0.85:
        kwargs["issue"] = _random_number(rng)
        kwargs["chapter"] = _random_number(rng)
    if rng.random() < 0.5:
        kwargs["year"] = rng.randint(1000, 9999)
    if rng.random() < 0.3:
        kwargs["edition"] = rng.choice(EDITION_WORDS + ("Digital",))
    if rng.random() < 0.2:
        kwargs["special"] = rng.choice(SPECIAL_WORDS + ("Annual",))
    if rng.random() < 0.3:
        kwargs["version"] = rng.randint(0, 20)
    return kwargs


def test_round_trip_fixpoint_over_seeded_random_fields():
    """For every generated field set that constructs and formats, parse is the
    exact inverse of format and a second lap is stable. Refusals are allowed
    (the generator deliberately produces invalid input) but must be
    CanonicalNameError, and enough cases must be valid to mean something."""
    rng = random.Random(1123)
    valid = 0
    for _ in range(6000):
        kwargs = _random_fields(rng)
        try:
            f = F(**kwargs)
            name = fmt(f)
        except CanonicalNameError:
            continue
        valid += 1
        parsed = parse(name)
        assert parsed == f, (kwargs, name)
        assert fmt(parsed) == name
        assert parse(fmt(parsed)) == parsed
    assert valid >= 2000, valid


def test_parse_accepts_only_the_formatters_image():
    for name in [
        "Batman v1.cbz",          # unpadded volume
        "Batman #3.cbz",          # unpadded issue
        "Batman #0003.cbz",       # over-padded issue
        "Batman ch. 01.cbz",      # padded chapter
        "Batman ch.1.cbz",        # missing space
        "Batman.CBZ",             # extension case
        "Batman.cbr",
        "Batman (2019) v01.cbz",  # slots out of order
        "Batman #001 ch. 1.cbz",  # issue and chapter
        "Batman [v0].cbz",
        "CON.cbz",                # reserved
        " Batman.cbz",            # unsanitised series
        "Batman .cbz",
        ".cbz",
    ]:
        with pytest.raises(NotCanonicalNameError):
            parse(name)


def test_a_parenthesised_word_outside_the_edition_list_is_series_text():
    # "(Digital)" is not an edition; dropping source tags is part B's job, so
    # here it is simply part of the display name and must not become a field.
    parsed = parse("Batman (Digital).cbz")
    assert parsed == F("Batman (Digital)")
    assert parsed.edition is None


def test_parse_never_reads_a_bare_v_token_as_a_volume_or_version():
    # Classifying v-tokens in non-canonical names is resolution (§3.2, step 4);
    # the canonical parser refuses rather than guesses.
    for name in ["Series v3.cbz", "Series v03 [v3] v2.cbz", "Series V03.cbz"]:
        with pytest.raises(NotCanonicalNameError):
            parse(name)


# ---------------------------------------------------------------------------
# Collisions (§2.3)
# ---------------------------------------------------------------------------


def test_identical_canonical_names_collide_and_both_are_returned():
    items = [
        ("a.cbz", F("Batman", issue=1)),
        ("b.cbz", F("Batman", issue="001")),
        ("c.cbz", F("Batman", issue=2)),
    ]
    collisions = find_canonical_collisions(items)
    assert len(collisions) == 1
    assert collisions[0].entries == (("a.cbz", "Batman #001.cbz"), ("b.cbz", "Batman #001.cbz"))


def test_case_only_collisions_are_reported():
    items = [("x", F("Batman", issue=1)), ("y", F("BATMAN", issue=1)), ("z", F("batman", issue=1))]
    (collision,) = find_canonical_collisions(items)
    assert [src for src, _ in collision.entries] == ["x", "y", "z"]
    assert collision.key == "batman #001.cbz"


def test_no_collision_without_duplicates():
    assert find_canonical_collisions([("x", F("A")), ("y", F("B"))]) == []
    assert find_canonical_collisions([]) == []


def test_collision_helper_propagates_unformattable_items():
    with pytest.raises(ReservedNameError):
        find_canonical_collisions([("x", F("CON"))])


def test_collisions_are_reported_in_first_appearance_order():
    items = [("1", F("B")), ("2", F("A")), ("3", F("b")), ("4", F("a"))]
    keys = [c.key for c in find_canonical_collisions(items)]
    assert keys == ["b.cbz", "a.cbz"]
