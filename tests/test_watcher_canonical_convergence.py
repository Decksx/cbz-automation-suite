"""Watcher and import-sanitizer convergence onto the canonical formatter.

Design ``docs/canonical_naming_and_version_supersession.md`` §5.5, step 3
part B. What these tests pin:

* **The bracket split** (confirmed by David, 2026-10-22). Identity matching
  -- series-folder lookup, ``series_key`` input, ``sanitize`` and
  ``clean_directory_name`` -- still strips every bracketed block, so
  ``Berserk [Judas]`` finds the existing ``Berserk`` folder. The display name
  of an arriving archive keeps ``(YYYY)``, ``[vN]`` and the formatter's
  edition words, and still drops group and source tags.
* **Idempotence.** ``sanitize`` and its ``clean_*`` helpers, the display
  cleaner and the watcher's naming step are fixpoints. Seeded
  ``random.Random`` properties, as in ``test_step8_invariant_properties.py``
  (no ``hypothesis``: it is not a dependency), plus the minimal
  counterexamples that were probed on master.
* **Name evidence never classifies** (§3.2). A bare ``v<N>`` stays text; it
  never becomes ``v01`` or ``[v1]``, and display mode adds no field.
* **Decensor markers** keep ``preserve_decensor_markers`` behaviour on the
  identity side and render as the formatter's ``(Uncensored)`` /
  ``(Decensored)`` token on the display side.
* **Fallback naming** goes through ``format_canonical_name``, and falls back
  to the pre-§5.5 name plus tokens when the formatter refuses.

Every fixture lives under ``tmp_path``. No network: translation is stubbed
wherever the watcher pipeline runs, and the random properties exclude the
``translate`` rule.
"""

from __future__ import annotations

import random
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest

from scripts import cbz_core
from scripts import cbz_watcher as watcher
from scripts.cbz_canonical_name import (
    EDITION_WORDS,
    CanonicalFields,
    format_canonical_name,
)
from scripts.cbz_routing import series_key

SEEDS = (0, 1, 2, 3, 4, 5, 6, 7)
CASES_PER_SEED = 300
NO_TRANSLATE = cbz_core.ALL_RULES - {"translate"}

# Tokens aimed at the rules that interact: brackets that open or close
# alone, URL and scan-group shapes, entities, G-codes, separators, the
# display tokens and near misses of them, marker words, CJK, accents.
_TOKENS = [
    "a", "B", "Berserk", "Batman", "Book", " ", "  ", "_", "-", " - ", ".",
    "[", "]", "(", ")", "[Judas]", "(Digital)", "[Group]", "(2019)", "(1989)",
    "[2019]", "(201)", "[v2]", "[V02]", "(v2)", "v2", "v01", "[v0]",
    "(Uncensored)", "[Uncensored]", "(DECENSORED)", "[Group Uncensored]",
    "uncensored", "(Colored)", "(colored)", "(English)", "[English]",
    "(Special Edition)", "(Omnibus)", ".cC", ".com", "x.co", "www.", "&amp;",
    "&amp", "&#1", "#", "#3", "G1234", " G5678", "Ch.", "Ch. 5", "ch. 1",
    "Chapter", "Vol", "Vol. 2", "scans", "Scanlations", "/", ":", "?", "|",
    "ワン", "鬼", "é", "é", "0", "12", "12.5", "~",
]


def _random_name(rng: random.Random, max_tokens: int = 9) -> str:
    return "".join(rng.choice(_TOKENS) for _ in range(rng.randint(0, max_tokens)))


def _random_filename_part(rng: random.Random, max_tokens: int = 9) -> str:
    """A random name usable as one path component: no slash or backslash."""
    return _random_name(rng, max_tokens).replace("/", "").replace("\\", "")


def _random_rules(rng: random.Random) -> set[str]:
    return {rule for rule in NO_TRANSLATE if rng.random() < 0.8}


def _parse_or_none(path: Path, **kwargs):
    """parse_comic_name, or None for the one crash that predates this change.

    A chapter token whose digits run into a second fraction
    (``"Ch. 12.5012.5"``) makes ``_fmt_num_text`` raise ``ValueError`` on
    master too, with or without display tokens. It is a separate defect,
    reported rather than fixed here; such inputs are skipped, and the caller
    asserts they stay rare so the skip cannot hollow the property out.
    """
    try:
        return cbz_core.parse_comic_name(path, rules=NO_TRANSLATE, **kwargs)
    except ValueError as exc:
        assert "could not convert string to float" in str(exc)
        return None


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Batman].cC",        # stray bracket removal exposes a URL (§5.5)
        "&amp;amp;amp;#1",   # one entity level per pass
        "&amp#1",            # the probe recorded in test_step8_invariant_properties
        "Foo G1234 G5678",   # one G-code per pass
        "x [Uncensored].com",
    ],
)
def test_probed_counterexamples_are_now_fixpoints(text):
    once = cbz_core.sanitize(text)
    assert cbz_core.sanitize(once) == once
    display = cbz_core.sanitize_display(text)
    assert cbz_core.sanitize_display(display) == display


def test_the_recorded_batman_probe_cleans_in_one_call():
    """The §5.5 example: two passes used to give "Batman.cC" then "".

    Pinned to the value, not only to idempotence, so a "fix" that makes the
    first call return "Batman.cC" and stop is caught too.
    """
    assert cbz_core.sanitize("Batman].cC") == ""
    assert cbz_core.clean_filename("Batman].cC.cbz") == ".cbz"


def test_directory_stub_stripping_is_a_fixpoint():
    once = cbz_core.clean_directory_name("Foo ch v")
    assert once == "Foo"
    assert cbz_core.clean_directory_name(once) == once


@pytest.mark.parametrize("seed", SEEDS)
def test_sanitize_and_clean_helpers_are_fixpoints(seed):
    rng = random.Random(seed)
    for _ in range(CASES_PER_SEED):
        text = _random_name(rng)
        rules = _random_rules(rng)
        for clean in (
            cbz_core.sanitize,
            cbz_core.clean_xml_field,
            cbz_core.clean_directory_name,
            cbz_core.sanitize_without_markers,
            cbz_core.sanitize_display,
        ):
            once = clean(text, rules)
            assert clean(once, rules) == once, (clean.__name__, text, rules)
        name = text + rng.choice([".cbz", ".CBZ", ".zip", ""])
        once = cbz_core.clean_filename(name, rules)
        assert cbz_core.clean_filename(once, rules) == once, (name, rules)


@pytest.mark.parametrize("seed", SEEDS)
def test_watcher_naming_step_is_a_fixpoint(seed, tmp_path):
    """parse_comic_name(display_tokens=True) on its own output is a no-op.

    The output name is fed back under the same folder, exactly as the
    watcher would see it on a second pass. Translation is excluded: it calls
    a network service and is outside this claim.
    """
    rng = random.Random(seed)
    skipped = 0
    for _ in range(CASES_PER_SEED):
        folder = cbz_core.clean_directory_name(_random_filename_part(rng, 4)) or "Series"
        path = tmp_path / folder / (_random_filename_part(rng) + ".cbz")
        first = _parse_or_none(path, display_tokens=True)
        if first is None:
            skipped += 1
            continue
        again = _parse_or_none(path.with_name(first.filename), display_tokens=True)
        assert again is not None, path.name
        assert again.filename == first.filename, (path.name, first.filename)
    assert skipped <= CASES_PER_SEED // 20


@pytest.mark.parametrize("seed", SEEDS)
def test_display_mode_changes_only_the_filename(seed, tmp_path):
    """Display tokens never add a field: chapter, volume, stem and series are
    computed exactly as without them, so a [v2] cannot become a volume and a
    bare v2 is classified no differently than before (§3.2)."""
    rng = random.Random(seed)
    for _ in range(CASES_PER_SEED):
        path = tmp_path / "Series" / (_random_filename_part(rng) + ".cbz")
        legacy = _parse_or_none(path)
        display = _parse_or_none(path, display_tokens=True)
        assert (legacy is None) == (display is None), path.name
        if legacy is None:
            continue
        for field in ("stem", "series", "chapter", "volume", "season", "episode", "part"):
            assert getattr(display, field) == getattr(legacy, field), (field, path.name)


def test_fixpoint_bound_raises_instead_of_returning_a_non_fixpoint():
    """The loop bound is a guard, not a truncation: a step that never settles
    raises rather than hand back a value a second call would change."""
    flip = {"a": "b", "b": "a"}
    with pytest.raises(RuntimeError, match="fixpoint"):
        cbz_core._to_fixpoint(lambda t: flip[t], "a")


# ---------------------------------------------------------------------------
# The bracket split
# ---------------------------------------------------------------------------


def test_display_name_keeps_year_version_and_edition_tokens():
    assert (
        cbz_core.sanitize_display("[Judas] Berserk Ch. 5 (2019) [v2] (Colored)")
        == "Berserk Ch. 5 (2019) (Colored) [v2]"
    )
    parsed = cbz_core.parse_comic_name(
        Path("Berserk/[Judas] Berserk Ch. 05 (2019) [v2] (Uncensored).cbz"),
        rules=NO_TRANSLATE,
        display_tokens=True,
    )
    assert parsed.filename == "Berserk Ch. 5 (2019) (Uncensored) [v2].cbz"


def test_display_tokens_are_written_in_the_formatters_spelling():
    """Case and padding variants of a recognised token are written the way
    format_canonical_name writes them: (Colored), [v2]."""
    assert cbz_core.sanitize_display("Book (colored) [V02]") == "Book (Colored) [v2]"
    assert (
        cbz_core.sanitize_display("Book (2019) (Colored) [v2]")
        == format_canonical_name(
            CanonicalFields(series="Book", year=2019, edition="Colored", version=2)
        )[: -len(".cbz")]
    )


def test_group_and_source_tags_are_still_dropped_from_the_display_name():
    assert cbz_core.sanitize_display("[Judas] Berserk (Digital) [HQ] (f)") == "Berserk"


@pytest.mark.parametrize(
    "token",
    ["[2019]", "(201)", "(v2)", "[v0]", "[English]", "(Omnibus)", "[Colored]"],
)
def test_near_miss_tokens_are_dropped_as_before(token):
    """Only the exact canonical shapes are kept. A square-bracket year, a
    round-bracket version, square-bracket edition words (the scanlation-tag
    convention) and bracketed special words are dropped as they were before
    §5.5 -- see the section comment in cbz_core for why each is held."""
    assert cbz_core.sanitize_display(f"Book {token}") == "Book"


def test_identity_cleaning_still_strips_every_bracket():
    """§5.5: the bracket rule stays for identity. Folder names, ComicInfo
    fields and every non-watcher caller keep today's output."""
    name = "[Judas] Berserk (2019) [v2] (Colored)"
    assert cbz_core.sanitize(name) == "Berserk"
    assert cbz_core.clean_directory_name(name) == "Berserk"
    assert series_key(cbz_core.clean_directory_name(name)) == series_key("Berserk")
    legacy = cbz_core.parse_comic_name(Path(f"{name}/{name} Ch. 1.cbz"), rules=NO_TRANSLATE)
    assert legacy.filename == "Berserk Ch. 1.cbz"
    assert legacy.series == "Berserk"


def test_a_bare_v_token_is_never_a_volume_or_version_token():
    """§3.2: name evidence alone never classifies. The arrival's own "v2"
    stays exactly as it was written; it is neither padded into the volume
    token "v02" nor bracketed into the version token "[v2]"."""
    parsed = cbz_core.parse_comic_name(
        Path("Berserk/[Group] Berserk v2.cbz"), rules=NO_TRANSLATE, display_tokens=True
    )
    assert parsed.filename == "Berserk v2.cbz"
    assert cbz_core.extract_display_tokens("Berserk v2 (v3)").versions == ()


def test_an_already_canonical_arrival_name_is_kept_verbatim():
    """The stem pipeline would rewrite v01 as v1; a name the formatter
    produced is recognised and left alone instead."""
    name = "Berserk v01 #003 (2019) (Uncensored) [v2].cbz"
    parsed = cbz_core.parse_comic_name(
        Path("Berserk") / name, rules=NO_TRANSLATE, display_tokens=True
    )
    assert parsed.filename == name
    legacy = cbz_core.parse_comic_name(Path("Berserk") / name, rules=NO_TRANSLATE)
    assert legacy.filename == "Berserk v1 #003 Uncensored.cbz"


def test_a_name_made_only_of_tokens_keeps_its_legacy_name():
    """An empty core is not given a token-only name; today's result stands."""
    assert cbz_core.sanitize_display("[Uncensored]") == "Uncensored"
    assert cbz_core.sanitize_display("(2019) [Group]") == ""
    parsed = cbz_core.parse_comic_name(
        Path("Series/[Uncensored].cbz"), rules=NO_TRANSLATE, display_tokens=True
    )
    assert parsed.filename == "Uncensored.cbz"


# ---------------------------------------------------------------------------
# Decensor markers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "legacy", "display"),
    [
        ("Book [Uncensored] [CoolScans]", "Book Uncensored", "Book (Uncensored)"),
        ("Book (DECENSORED) (Group)", "Book DECENSORED", "Book (Decensored)"),
        ("Book [Group Uncensored]", "Book Uncensored", "Book (Uncensored)"),
        ("Book Uncensored [Uncensored]", "Book Uncensored", "Book Uncensored"),
    ],
)
def test_decensor_markers_render_as_the_formatters_edition_tokens(raw, legacy, display):
    """Identity keeps preserve_decensor_markers' bare word; display writes the
    formatter's edition token, and never both."""
    assert cbz_core.sanitize(raw) == legacy
    assert cbz_core.sanitize_display(raw) == display


def test_decensor_marker_display_agrees_with_format_canonical_name():
    parsed = cbz_core.parse_comic_name(
        Path("Book/Book [Uncensored].cbz"), rules=NO_TRANSLATE, display_tokens=True
    )
    assert parsed.filename == format_canonical_name(
        CanonicalFields(series="Book", edition="Uncensored")
    )


def test_both_decensor_markers_are_formatter_edition_words():
    """The display tokens are drawn from EDITION_WORDS, not a second list."""
    assert {"Uncensored", "Decensored"} <= set(EDITION_WORDS)
    assert set(cbz_core._DECENSOR_EDITION.values()) <= set(EDITION_WORDS)


def test_preserve_decensor_markers_is_unchanged():
    assert cbz_core.preserve_decensor_markers("Book [Uncensored]", "Book") == "Book Uncensored"
    assert cbz_core.preserve_decensor_markers("Book [NotUncensored]", "Book") == "Book"


# ---------------------------------------------------------------------------
# Fallback naming
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("series", "chapter", "source", "expected"),
    [
        ("Berserk", "4", "volvox Chapter", "Berserk ch. 4"),
        ("Berserk", None, "volvox Chapter", "Berserk"),
        ("Berserk", "4", "volvox Chapter (2019) (Uncensored) [v2]",
         "Berserk ch. 4 (2019) (Uncensored) [v2]"),
        ("Berserk", "12.5", "Chapter (1989)", "Berserk ch. 12.5 (1989)"),
        # A decensor word the folder already carries is not repeated.
        ("Berserk Uncensored", "4", "Chapter (Uncensored)", "Berserk Uncensored ch. 4"),
    ],
)
def test_fallback_name_comes_from_the_canonical_formatter(series, chapter, source, expected):
    assert watcher._fallback_display_stem(series, chapter, source) == expected
    # And the watcher's naming step leaves the fallback name alone, so a
    # re-processed archive is not renamed again.
    again = cbz_core.parse_comic_name(
        Path(series) / f"{expected}.cbz", rules=NO_TRANSLATE, display_tokens=True
    )
    assert again.filename == f"{expected}.cbz"


def _make_cbz(path: Path, payload_size: int, comicinfo: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("page1.jpg", b"\0" * payload_size)
        if comicinfo is not None:
            zf.writestr("ComicInfo.xml", comicinfo)


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


def test_bracketed_group_tag_still_resolves_to_the_existing_series(watch):
    """§5.5's required test. The arrival folder carries a scanlator tag; it
    must merge into the existing "Berserk" folder rather than open a
    "Berserk [Judas]" series beside it, while the archive's own display name
    keeps its year and drops the group tag.

    The folder also carries a year, a token the *display* cleaner keeps, so
    the test fails if folder identity is ever computed by the display
    cleaner instead of the identity one: "Berserk (1989)" keys differently
    from "Berserk" and would split the series.
    """
    watch_root, library = watch
    existing = library / "Berserk"
    _make_cbz(existing / "Berserk Ch. 4.cbz", 100)

    arrival = watch_root / "[Judas] Berserk (1989)"
    _make_cbz(arrival / "[Judas] Berserk Ch. 5 (2019).cbz", 200)

    watcher.process_and_move_directory(arrival)

    assert sorted(p.name for p in library.iterdir()) == ["Berserk"]
    assert sorted(p.name for p in existing.iterdir()) == [
        "Berserk Ch. 4.cbz",
        "Berserk Ch. 5 (2019).cbz",
    ]
    assert _payload_size(existing / "Berserk Ch. 5 (2019).cbz") == 200


def test_fallback_naming_end_to_end_keeps_every_display_token(watch):
    """End to end through the watcher: a [Group]-tagged per-chapter folder
    holding a placeholder-named archive with a year, a [v2] and an
    (Uncensored) edition, arriving for an existing series folder.

    Before §5.5 the (Uncensored) marker made the cleaned stem
    "volvox Chapter Uncensored", which is not a placeholder, so the archive
    kept the generic name every chapter shares -- the collision the fallback
    exists to prevent. It is now recognised on the bracket-free name and
    formatted canonically, tokens intact.
    """
    watch_root, library = watch
    existing = library / "Berserk"
    _make_cbz(existing / "Berserk ch. 3.cbz", 100)

    arrival = watch_root / "[Judas] Berserk Ch. 4"
    _make_cbz(arrival / "[Judas] volvox_Chapter (2019) [v2] (Uncensored).cbz", 400)

    watcher.process_and_move_directory(arrival)

    assert sorted(p.name for p in library.iterdir()) == ["Berserk"]
    landed = existing / "Berserk ch. 4 (2019) (Uncensored) [v2].cbz"
    assert sorted(p.name for p in existing.iterdir()) == [
        "Berserk ch. 3.cbz",
        landed.name,
    ]
    assert _payload_size(landed) == 400
    with zipfile.ZipFile(landed) as zf:
        info = ElementTree.fromstring(zf.read("ComicInfo.xml"))
    assert info.findtext("Number") == "4"
    assert not list(watch_root.rglob("*.cbz"))


def test_two_placeholder_chapters_with_tokens_do_not_collide(watch):
    """The original data-loss incident, with display tokens on both arrivals:
    both chapters survive under distinct names."""
    watch_root, library = watch
    _make_cbz(watch_root / "Arrest Thy Neighbor" / "volvox_Chapter (Uncensored).cbz", 200)
    _make_cbz(watch_root / "Arrest Thy Neighbor 2" / "volvox_Chapter (Uncensored).cbz", 400)

    watcher.process_and_move_directory(watch_root / "Arrest Thy Neighbor")
    watcher.process_and_move_directory(watch_root / "Arrest Thy Neighbor 2")

    series = library / "Arrest Thy Neighbor"
    names = sorted(p.name for p in series.iterdir())
    assert names == [
        "Arrest Thy Neighbor (Uncensored).cbz",
        "Arrest Thy Neighbor ch. 2 (Uncensored).cbz",
    ]
    assert _payload_size(series / names[0]) == 200
    assert _payload_size(series / names[1]) == 400


def test_a_volume_numbered_folder_is_not_fallback_renamed(watch):
    """What the watcher did with a bare v<N> folder before §5.5, kept: the
    folder's v3 makes parsed.volume non-empty, so the fallback never runs and
    the arrival is neither turned into "ch. 3" nor into "v03"/"[v3]"."""
    watch_root, library = watch
    arrival = watch_root / "Berserk v3"
    _make_cbz(arrival / "volvox_Chapter.cbz", 100)

    watcher.process_and_move_directory(arrival)

    assert sorted(p.name for p in (library / "Berserk").iterdir()) == ["volvox Chapter.cbz"]


def test_archive_collision_key_maps_only_decensor_tokens_to_words(tmp_path):
    """A decensored re-arrival still meets its pre-§5.5 copy; a different
    version or edition does not, so no bytes land under another version's
    label."""
    (tmp_path / "Book Uncensored.cbz").write_bytes(b"x")
    (tmp_path / "Book Ch. 1 [v2].cbz").write_bytes(b"x")
    (tmp_path / "Book Ch. 3 (Colored).cbz").write_bytes(b"x")

    found = watcher._find_archive_collision(tmp_path, "Book (Uncensored)")
    assert found is not None and found.name == "Book Uncensored.cbz"
    assert watcher._find_archive_collision(tmp_path, "Book Ch. 1 [v3]") is None
    assert watcher._find_archive_collision(tmp_path, "Book Ch. 3") is None
    found = watcher._find_archive_collision(tmp_path, "Book Ch.1 [v2]")
    assert found is not None and found.name == "Book Ch. 1 [v2].cbz"


def test_an_arriving_year_token_still_meets_the_existing_copy(tmp_path):
    """Every library name predates §5.5 and carries no year, so the year is
    not part of book identity: X Ch. 5 (2019) is the existing X Ch. 5."""
    (tmp_path / "X Ch. 5.cbz").write_bytes(b"x")
    found = watcher._find_archive_collision(tmp_path, "X Ch. 5 (2019)")
    assert found is not None and found.name == "X Ch. 5.cbz"


def test_a_different_version_still_does_not_meet(tmp_path):
    (tmp_path / "X Ch. 5 [v2].cbz").write_bytes(b"x")
    assert watcher._find_archive_collision(tmp_path, "X Ch. 5 [v3]") is None
    assert watcher._find_archive_collision(tmp_path, "X Ch. 5 (2019) [v3]") is None


def test_a_different_edition_does_not_meet(tmp_path):
    (tmp_path / "X Ch. 5.cbz").write_bytes(b"x")
    assert watcher._find_archive_collision(tmp_path, "X Ch. 5 (Colored)") is None


@pytest.mark.parametrize(
    ("incoming_size", "survivor_size"),
    [
        # Larger by more than REPLACEMENT_MIN_GAIN_BYTES: incoming bytes win.
        (watcher.REPLACEMENT_MIN_GAIN_BYTES + 1000 + 100, None),
        # Not meaningfully larger: the existing bytes are kept.
        (100 + 10, 100),
    ],
)
def test_a_year_tagged_arrival_merges_under_the_existing_name(
    tmp_path, incoming_size, survivor_size
):
    """When the year-tagged arrival meets the existing copy, the existing
    *name* always survives and only one file remains. If the incoming bytes
    win on size they move in under that name, so the (2019) token is lost
    from the name -- accepted: the existing name wins, as before §5.5."""
    src = tmp_path / "incoming"
    dest = tmp_path / "library"
    _make_cbz(dest / "X Ch. 5.cbz", 100)
    _make_cbz(src / "X Ch. 5 (2019).cbz", incoming_size)

    watcher._merge_directories(src, dest)

    assert sorted(p.name for p in dest.iterdir()) == ["X Ch. 5.cbz"]
    expected = incoming_size if survivor_size is None else survivor_size
    assert _payload_size(dest / "X Ch. 5.cbz") == expected
    assert not (src / "X Ch. 5 (2019).cbz").exists()


# ---------------------------------------------------------------------------
# Formatter refusal: no substitute rename, no unsafe merge (PR #121 review)
# ---------------------------------------------------------------------------

LONG_SERIES = "S" * 195


@pytest.mark.parametrize(
    ("series", "chapter", "source", "reason"),
    [
        ("Series Special", "4", "Chapter (2019)", "AmbiguousSeriesNameError"),
        ("Batman v1", "4", "Chapter (2019)", "AmbiguousSeriesNameError"),
        (LONG_SERIES, "4", "Chapter (2019)", "NameTooLongError"),
        ("Berserk", "4", "Chapter (Colored) (Uncensored)", "conflicting tokens"),
        ("Berserk", "4", "Chapter (2019) (2020)", "conflicting tokens"),
        ("Berserk", "4", "Chapter [v2] [v3]", "conflicting tokens"),
    ],
)
def test_a_formatter_refusal_is_returned_never_a_substitute_name(
    series, chapter, source, reason
):
    result = watcher._fallback_display_stem(series, chapter, source)
    assert isinstance(result, watcher.FallbackRefused)
    assert reason in result.reason


@pytest.mark.parametrize(
    ("series", "source"),
    [
        ("Series Special", "Chapter (2019).cbz"),
        (LONG_SERIES, "Chapter (2019).cbz"),
        ("Berserk", "Chapter (Colored) (Uncensored).cbz"),
    ],
)
def test_the_real_fallback_caller_keeps_the_name_on_refusal(tmp_path, monkeypatch, series, source):
    """Codex's probe: _apply_fallback_naming used to rename a refused
    archive to "Series Special Ch. 4 (2019).cbz" (and a 212-unit name for
    the long series). It now keeps the current name and reports the path."""
    monkeypatch.setattr(watcher, "process_comicinfo", lambda *a, **k: None)
    folder = tmp_path / "arrival"
    original = folder / source
    _make_cbz(original, 100)
    parsed = cbz_core.parse_comic_name(original, rules=NO_TRANSLATE, display_tokens=True)
    mapping = {original: parsed}

    refused = watcher._apply_fallback_naming(mapping, series, "4")

    assert refused == [original]
    assert sorted(p.name for p in folder.iterdir()) == [source]
    assert mapping == {original: parsed}


@pytest.mark.parametrize(
    ("folder", "source"),
    [
        ("Series Special Ch. 4", "volvox_Chapter (2019).cbz"),
        ("Berserk Ch. 4", "volvox_Chapter (Colored) (Uncensored).cbz"),
    ],
)
def test_a_refused_placeholder_is_not_merged_into_the_library(watch, folder, source):
    """End to end: the refused generic placeholder must not reach the series
    folder, where the next chapter sharing its name would collide with it.
    The directory stays in the import folder for review; the library is
    untouched."""
    watch_root, library = watch
    _make_cbz(library / "Berserk" / "Berserk ch. 3.cbz", 100)
    _make_cbz(library / "Series Special" / "Series Special ch. 3.cbz", 100)
    before = sorted(str(p.relative_to(library)) for p in library.rglob("*"))

    arrival = watch_root / folder
    _make_cbz(arrival / source, 400)
    watcher.process_and_move_directory(arrival)

    assert sorted(str(p.relative_to(library)) for p in library.rglob("*")) == before
    left = list(watch_root.rglob("*.cbz"))
    assert len(left) == 1
    # Still the arrival's own (display-cleaned) name, never a fallback name.
    assert "ch. 4" not in left[0].name.casefold()
    assert _payload_size(left[0]) == 400


def test_a_refused_loose_placeholder_stays_while_its_siblings_move(watch):
    """Mixed drop point: loose files beside comic subfolders are moved one by
    one. A refused placeholder stays behind; the subfolder still moves."""
    watch_root, library = watch
    drop = watch_root / "Series Special Ch. 4"
    _make_cbz(drop / "volvox_Chapter (2019).cbz", 400)
    _make_cbz(drop / "Other" / "Other Ch. 1.cbz", 100)

    watcher.process_and_move_directory(drop)

    assert (library / "Other" / "Other Ch. 1.cbz").is_file()
    assert not list((library).rglob("*volvox*"))
    assert not (library / "Series Special").exists()
    assert [p.name for p in watch_root.rglob("*.cbz")] == ["volvox Chapter (2019).cbz"]


def test_backfill_renames_nothing_when_the_formatter_refuses(watch):
    """Backfill would name the lone sibling "Series Special ch. 1", which the
    formatter refuses. It is left as it is; the numbered arrival still lands
    beside it under a different name, so nothing collides."""
    watch_root, library = watch
    _make_cbz(library / "Series Special" / "Series Special.cbz", 100)
    arrival = watch_root / "Series Special Ch. 2"
    _make_cbz(arrival / "Series Special Ch. 2.cbz", 200)

    watcher.process_and_move_directory(arrival)

    assert sorted(p.name for p in (library / "Series Special").iterdir()) == [
        "Series Special Ch. 2.cbz",
        "Series Special.cbz",
    ]


def test_backfill_control_still_renames_when_the_formatter_accepts(watch):
    watch_root, library = watch
    _make_cbz(library / "Berserk" / "Berserk.cbz", 100)
    arrival = watch_root / "Berserk Ch. 2"
    _make_cbz(arrival / "Berserk Ch. 2.cbz", 200)

    watcher.process_and_move_directory(arrival)

    assert sorted(p.name for p in (library / "Berserk").iterdir()) == [
        "Berserk Ch. 2.cbz",
        "Berserk ch. 1.cbz",
    ]


# ---------------------------------------------------------------------------
# Numeric padding in the collision key (PR #121 review)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("existing", "arriving"),
    [
        ("Berserk v1 #003", "Berserk v01 #003 (2019)"),
        ("Berserk Ch. 1", "Berserk ch. 01"),
        ("Berserk Vol. 1 #3", "Berserk Vol. 01 #003"),
        ("Berserk v1-02", "Berserk v01-02"),
    ],
)
def test_cosmetic_padding_does_not_split_one_book(tmp_path, existing, arriving):
    (tmp_path / f"{existing}.cbz").write_bytes(b"x")
    found = watcher._find_archive_collision(tmp_path, arriving)
    assert found is not None and found.stem == existing


@pytest.mark.parametrize(
    ("existing", "arriving"),
    [
        ("Berserk v1 #003", "Berserk v02 #003"),
        ("Berserk v1 #003", "Berserk v01 #030"),
        ("Berserk v01 #003 [v2]", "Berserk v01 #003 [v3]"),
        ("Berserk v01 #003", "Berserk v01 #003 (Colored)"),
        ("Berserk ch. 1", "Berserk ch. 10"),
        # Same last digit: only the padding zeros may go, never other digits.
        ("Berserk v1 #003", "Berserk v01 #013"),
        ("Berserk v1", "Berserk v11"),
    ],
)
def test_padding_normalisation_keeps_real_differences(tmp_path, existing, arriving):
    (tmp_path / f"{existing}.cbz").write_bytes(b"x")
    assert watcher._find_archive_collision(tmp_path, arriving) is None


@pytest.mark.parametrize(
    ("incoming_size", "survivor_size"),
    [
        (watcher.REPLACEMENT_MIN_GAIN_BYTES + 1000 + 100, None),
        (100 + 10, 100),
    ],
)
def test_a_canonical_arrival_meets_its_legacy_named_copy_end_to_end(
    watch, incoming_size, survivor_size
):
    """Codex's probe end to end: the arrival keeps its canonical name
    "Berserk v01 #003 (2019).cbz", the library holds the pre-§5.5 name
    "Berserk v1 #003.cbz". They are one book: one file remains, under the
    existing name, holding the incoming bytes only when meaningfully larger."""
    watch_root, library = watch
    _make_cbz(library / "Berserk" / "Berserk v1 #003.cbz", 100)
    arrival = watch_root / "Berserk"
    _make_cbz(arrival / "Berserk v01 #003 (2019).cbz", incoming_size)

    watcher.process_and_move_directory(arrival)

    assert sorted(p.name for p in (library / "Berserk").iterdir()) == ["Berserk v1 #003.cbz"]
    expected = incoming_size if survivor_size is None else survivor_size
    assert _payload_size(library / "Berserk" / "Berserk v1 #003.cbz") == expected
