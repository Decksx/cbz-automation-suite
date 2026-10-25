"""Round-bracket group, status and language tags in display names.

David's decision of 2026-10-24 (hive #2646), design
``docs/canonical_naming_and_version_supersession.md`` §2.1 and §5.5: the
display name an arriving archive receives drops round-bracket scanlator,
source, status and language tags -- ``(ManhwaClub)``, ``(Incognitymous)``,
``(Ongoing)``, ``(Digital)``, ``(English)`` -- and keeps only the canonical
round tokens: ``(YYYY)``, ``(Colored)`` (also promoted from ``[Colored]``)
and the edition words other than the language words.

The stripped tokens below are the round-bracket tokens of a read-only scan of
73,564 archives on the Tower library (2026-10-24, every token in its
``file_round_top`` and ``folder_round_top`` lists), written with the casing a
real name carries them in. The scan's own file is not a fixture: its
contents are restated here so the test does not depend on it.

Everything runs through entry points -- ``parse_comic_name`` with
``display_tokens=True`` (the watcher's naming step), ``sanitize_display``,
the watcher's fallback formatter and ``process_and_move_directory`` on
tmp-path CBZs. No network: translation is excluded or stubbed.
"""

from __future__ import annotations

import random
import zipfile
from pathlib import Path

import pytest

from scripts import cbz_core
from scripts import cbz_watcher as watcher
from scripts.cbz_canonical_name import (
    EDITION_WORDS,
    CanonicalFields,
    format_canonical_name,
)
from scripts.cbz_routing import series_key

NO_TRANSLATE = cbz_core.ALL_RULES - {"translate"}

# Every round token of the 2026-10-24 Tower scan, files and folders. Each
# count is the number of archive (or folder) names carrying it.
SCANNED_ROUND_TAGS = [
    "(ManhwaClub)",                         # 52 files
    "(Incognitymous)",                      # 29
    "(Ongoing)",                            # 21
    "(Inuyasha)",                           # 17 (doujin parody tag)
    "(OC by Riz)",                          # 15
    "(Danny Phantom)",                      # 10 (parody)
    "(English)",                            #  7
    "(Animated WebP)",                      #  5
    "(1)",                                  #  4 files, 3 folders
    "(Updated)",                            #  4
    "(Original)",                           #  3
    "(ft. Reina)",                          #  3
    "(OCs by Riz)",                         #  3
    "(THE IDOLM@STER CINDERELLA GIRLS)",    #  2 (parody)
    "(S2)",                                 #  1
    "(3)",                                  #  1
    "(Completed)",                          #  1
    "(+Extras)",                            #  1
    "(WebP)",                               #  1
    "(Gravity Falls)",                      #  1 (parody)
    "(Complete)",                           #  1
    "(Gotoubun no Hanayome)",               #  1 (parody)
    "(C95)",                                #  1 (Comiket event)
    "(Kantai Collection -KanColle-)",       #  1 (parody)
    "(Dick)",                               #  1
    "(Fellatrix)",                          #  1 folder
    # Named in the decision but absent from the scan's top lists.
    "(Digital)",
]

# Round tokens the display name keeps, in the spelling it writes them.
KEPT_EDITIONS = [word for word in EDITION_WORDS if word != "English"]


def _display(name: str, folder: str = "Solo Leveling") -> str:
    return cbz_core.parse_comic_name(
        Path(folder) / name, rules=NO_TRANSLATE, display_tokens=True
    ).filename


def _assert_fixpoint(filename: str, folder: str = "Solo Leveling") -> None:
    assert _display(filename, folder) == filename


# ---------------------------------------------------------------------------
# Stripped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tag", SCANNED_ROUND_TAGS)
@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("Solo Leveling ch. 5 {tag}.cbz", "Solo Leveling ch. 5.cbz"),
        ("Solo Leveling {tag} ch. 5.cbz", "Solo Leveling ch. 5.cbz"),
        ("{tag} Solo Leveling ch. 5.cbz", "Solo Leveling ch. 5.cbz"),
    ],
    ids=["trailing", "after-series", "leading"],
)
def test_scanned_round_tags_are_dropped_from_the_display_name(tag, template, expected):
    """At every position a tag sits in a real name, the display name drops it
    and the result is a fixpoint of the watcher's naming step."""
    filename = _display(template.format(tag=tag))
    assert filename == expected
    _assert_fixpoint(filename)


@pytest.mark.parametrize("spelling", ["(English)", "(english)", "(ENGLISH)", "( English )"])
def test_english_is_dropped_in_any_case(spelling):
    """(English) was kept before hive #2646 because it is an EDITION_WORDS
    entry; it is now dropped in any case and spacing the edition match
    accepts."""
    assert cbz_core.sanitize_display(f"Book ch. 3 {spelling}") == "Book ch. 3"
    assert _display(f"Book ch. 3 {spelling}.cbz", "Book") == "Book ch. 3.cbz"
    assert cbz_core.extract_display_tokens(f"Book {spelling}").editions == ()


def test_several_tags_together_leave_only_the_name():
    name = "Solo Leveling ch. 5 (ManhwaClub) (Ongoing) (English) (Incognitymous).cbz"
    assert _display(name) == "Solo Leveling ch. 5.cbz"


# ---------------------------------------------------------------------------
# Kept
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("edition", KEPT_EDITIONS)
def test_non_language_edition_words_are_kept_among_stripped_tags(edition):
    name = f"Book ch. 3 (ManhwaClub) ({edition.lower()}) (English) (Ongoing).cbz"
    filename = _display(name, "Book")
    assert filename == f"Book ch. 3 ({edition}).cbz"
    _assert_fixpoint(filename, "Book")


def test_year_colored_and_version_survive_the_tags_around_them():
    name = "[Group] Solo Leveling ch. 5 (2019) (ManhwaClub) [Colored] (English) [v2] (Ongoing).cbz"
    filename = _display(name)
    assert filename == "Solo Leveling ch. 5 (2019) (Colored) [v2].cbz"
    _assert_fixpoint(filename)


def test_round_edition_keep_list_is_the_edition_list_less_english():
    """The keep-list is explicit: EDITION_WORDS less the language words, so
    a word added to EDITION_WORDS is kept unless it is also named a
    language word."""
    assert set(cbz_core._ROUND_EDITION_WORDS.values()) == set(KEPT_EDITIONS)
    assert cbz_core._DISPLAY_LANGUAGE_WORDS == frozenset({"english"})


# ---------------------------------------------------------------------------
# Canonical names that carry (English)
# ---------------------------------------------------------------------------


def test_a_canonical_english_name_loses_only_the_english_token():
    """A formatter-produced name is otherwise kept verbatim (its v01 padding
    survives); one carrying (English) loses that token and nothing else,
    and the result is kept verbatim from then on."""
    name = format_canonical_name(
        CanonicalFields("Berserk", volume=1, issue=3, year=2019, edition="English", version=2)
    )
    assert name == "Berserk v01 #003 (2019) (English) [v2].cbz"
    filename = _display(name, "Berserk")
    assert filename == "Berserk v01 #003 (2019) [v2].cbz"
    _assert_fixpoint(filename, "Berserk")


def test_a_canonical_kept_edition_name_is_still_kept_verbatim():
    name = "Berserk v01 #003 (2019) (Colored) [v2].cbz"
    assert _display(name, "Berserk") == name


@pytest.mark.parametrize(
    ("bare", "tagged"),
    [
        ("Book (English).cbz", "Book (English) (ManhwaClub).cbz"),
        ("Book (2019) (English).cbz", "Book (2019) (English) (Ongoing).cbz"),
    ],
)
def test_canonical_and_tagged_spellings_name_the_same_file(bare, tagged):
    assert _display(bare, "Book") == _display(tagged, "Book")


# ---------------------------------------------------------------------------
# Fallback naming
# ---------------------------------------------------------------------------


def test_fallback_name_drops_english_and_keeps_colored():
    """Before hive #2646 (English) and (Colored) were two editions and the
    formatter refused the pair as conflicting; English is now no edition."""
    stem = watcher._fallback_display_stem("Berserk", "4", "Chapter (English) (Colored)")
    assert stem == "Berserk ch. 4 (Colored)"
    stem = watcher._fallback_display_stem("Berserk", "4", "Chapter (ManhwaClub) (Ongoing)")
    assert stem == "Berserk ch. 4"


# ---------------------------------------------------------------------------
# Identity is unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tag", SCANNED_ROUND_TAGS)
def test_identity_cleaning_strips_the_tag_as_before(tag):
    name = f"Solo Leveling {tag}"
    assert cbz_core.sanitize(name) == "Solo Leveling"
    assert cbz_core.clean_directory_name(name) == "Solo Leveling"
    assert series_key(cbz_core.clean_directory_name(name)) == series_key("Solo Leveling")
    legacy = cbz_core.parse_comic_name(
        Path(name) / f"{name} ch. 5.cbz", rules=NO_TRANSLATE
    )
    assert legacy.series == "Solo Leveling"
    assert legacy.stem == "Solo Leveling ch. 5"


def test_display_mode_still_changes_only_the_filename():
    path = Path("Solo Leveling (ManhwaClub)/Solo Leveling ch. 5 (English) (Colored).cbz")
    legacy = cbz_core.parse_comic_name(path, rules=NO_TRANSLATE)
    display = cbz_core.parse_comic_name(path, rules=NO_TRANSLATE, display_tokens=True)
    assert display.filename == "Solo Leveling ch. 5 (Colored).cbz"
    assert (display.stem, display.series, display.chapter) == (
        legacy.stem, legacy.series, legacy.chapter,
    )


# ---------------------------------------------------------------------------
# Fixpoint property
# ---------------------------------------------------------------------------

_TOKENS = [
    "Book", "Solo", " ", "  ", "_", "-", " - ", ".", "(", ")", "[", "]",
    "ch. 5", "v01", "#003", "(2019)", "[v2]", "[Colored]", "(Colored)",
    "(Uncensored)", "(Special Edition)", "[English]", *SCANNED_ROUND_TAGS,
    "(english)", "(ENGLISH)", "English",
]


@pytest.mark.parametrize("seed", range(6))
def test_display_naming_is_a_fixpoint_with_round_tags(seed):
    rng = random.Random(seed)
    for _ in range(300):
        stem = "".join(rng.choice(_TOKENS) for _ in range(rng.randint(1, 8)))
        stem = stem.replace("/", "").replace("\\", "")
        once = _display(f"{stem}.cbz", "Book")
        assert _display(once, "Book") == once, (stem, once)
        assert "(english)" not in once.casefold(), (stem, once)


# ---------------------------------------------------------------------------
# End to end through the watcher
# ---------------------------------------------------------------------------


def _make_cbz(path: Path, payload_size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("page1.jpg", b"\0" * payload_size)


def _payload_size(cbz_path: Path) -> int:
    with zipfile.ZipFile(cbz_path) as zf:
        return len(zf.read("page1.jpg"))


@pytest.fixture
def watch(tmp_path, monkeypatch):
    """A tmp watch folder and library wired into the watcher, offline."""
    watch_root = tmp_path / "incoming"
    library = tmp_path / "library"
    watch_root.mkdir()
    library.mkdir()
    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(watch_root))
    monkeypatch.setattr(watcher, "_routing_rules", [])
    monkeypatch.setattr(watcher, "_routing_default", str(library))
    monkeypatch.setattr(watcher, "POLL_INTERVAL", 0)
    monkeypatch.setattr(watcher, "wait_for_file_stable", lambda *a, **k: True)
    monkeypatch.setattr(cbz_core, "translate_metadata_text", lambda text: (text, None))
    return watch_root, library


def test_a_tagged_arrival_resolves_to_the_existing_series(watch):
    """An arrival folder ``Series (ManhwaClub)`` merges into the existing
    ``Series`` folder, and its archive lands with every round tag dropped and
    its year and (Colored) kept."""
    watch_root, library = watch
    existing = library / "Solo Leveling"
    _make_cbz(existing / "Solo Leveling ch. 4.cbz", 100)

    arrival = watch_root / "Solo Leveling (ManhwaClub)"
    _make_cbz(
        arrival / "Solo Leveling ch. 5 (2019) (ManhwaClub) (Ongoing) (English) [Colored].cbz",
        200,
    )

    watcher.process_and_move_directory(arrival)

    assert sorted(p.name for p in library.iterdir()) == ["Solo Leveling"]
    landed = existing / "Solo Leveling ch. 5 (2019) (Colored).cbz"
    assert sorted(p.name for p in existing.iterdir()) == [
        "Solo Leveling ch. 4.cbz",
        landed.name,
    ]
    assert _payload_size(landed) == 200
    assert not list(watch_root.rglob("*.cbz"))


def test_an_english_tagged_arrival_meets_the_existing_copy(watch):
    """Before hive #2646 the display name kept (English), an edition, so
    ``ch. 4 (English)`` was a different book from the existing ``ch. 4``
    and landed beside it. It is now the same book: one file remains, under
    the existing name, holding the larger incoming bytes."""
    watch_root, library = watch
    existing = library / "Solo Leveling"
    _make_cbz(existing / "Solo Leveling ch. 4.cbz", 100)

    arrival = watch_root / "Solo Leveling"
    incoming = watcher.REPLACEMENT_MIN_GAIN_BYTES + 1000 + 100
    _make_cbz(arrival / "Solo Leveling ch. 4 (English).cbz", incoming)

    watcher.process_and_move_directory(arrival)

    assert sorted(p.name for p in existing.iterdir()) == ["Solo Leveling ch. 4.cbz"]
    assert _payload_size(existing / "Solo Leveling ch. 4.cbz") == incoming
