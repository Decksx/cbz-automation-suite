"""Media semantics and title display use only their intended authority."""

import pytest

from comic_automation.archive import field_resolution as fr
from comic_automation.library.media_naming import KeepArrivalName, media_fields, series_display
from scripts.cbz_canonical_name import CanonicalNameError, format_canonical_name


def resolve(name, xml=b"<ComicInfo><Series>Series</Series><Number>1</Number></ComicInfo>"):
    fact = fr.ArchiveFacts("book", name, fr.comicinfo_facts((xml,)) if xml else fr.ComicInfoFacts(fr.COMICINFO_ABSENT))
    return fr.resolve_folder(fr.FolderFacts(None, (fact,))).archives[0]


@pytest.mark.parametrize("label", ["TPB 1", "Vol. 1", "Full 1", "v01 (TPB)"])
def test_collection_volume_requires_corroboration_and_survives_canonical_reparse(label):
    result = resolve(f"Series {label}.cbz")
    assert result.renamable and result.fields["volume"].value == "1"
    assert not resolve(f"Series {label}.cbz", xml=b"").renamable
    tokens = fr.parse_name_tokens(result.filename)
    fields = media_fields(series="Series", number="1", volume="1", style="issue", tokens=tokens)
    assert format_canonical_name(fields) == "Series v01 (TPB).cbz"
    again = resolve(format_canonical_name(fields))
    assert again.renamable and again.fields["volume"].value == "1"


@pytest.mark.parametrize("label", ["TPB 1", "Vol. 1", "Full 1"])
def test_a_conflicting_number_cannot_supply_collection_volume_authority(label):
    result = resolve(f"Series {label}.cbz", b"<ComicInfo><Series>Series</Series><Number>2</Number></ComicInfo>")
    assert not result.fields["volume"].resolved


@pytest.mark.parametrize("filename", ["Series Full.cbz", "Series (Full).cbz",
    "Series Full (2019).cbz", "Series Full (Colored).cbz"])
def test_unnumbered_full_defaults_to_one_while_literal_titles_keep_their_words(filename):
    tokens = fr.parse_name_tokens(filename)
    year = "2019" if "(2019)" in filename else None  # Callers pass the filename year; it is never dropped.
    result = media_fields(series="Series", number=None, volume=None, style="issue", tokens=tokens, year=year)
    assert result.volume == "1" and result.collection == "TPB"
    fields = media_fields(series="Series Full", number="1", volume=None, style="issue", tokens=tokens, year=year)
    assert fields.series == "Series Full"
    if year:
        with pytest.raises(KeepArrivalName):
            media_fields(series="Series", number=None, volume=None, style="issue", tokens=tokens)


@pytest.mark.parametrize("number", range(1, 7))
def test_each_numbered_tpb_maps_its_own_volume_without_a_default(number):
    result = resolve(f"Series TPB {number}.cbz", (
        f"<ComicInfo><Series>Series</Series><Number>{number}</Number></ComicInfo>".encode()))
    assert result.fields["volume"].value == str(number)
    fields = media_fields(series="Series", number=str(number), volume=str(number), style="issue",
                          tokens=fr.parse_name_tokens(result.filename))
    assert format_canonical_name(fields) == f"Series v{number:02} (TPB).cbz"


@pytest.mark.parametrize("filename,title", [("'68 Scars TPB.cbz", "'68 Scars"), ("Series TPB.cbz", "Series"),
                                            ("Series tpb (2019).cbz", "Series"), ("TPB.cbz", "Blessed Thistle"),
                                            ("TPB (2019).cbz", "Series"), ("TPB [Scan].cbz", "Series"),
                                            ("TPB.cbz", "Series (2019)")])
def test_a_bare_tpb_after_the_decided_title_is_volume_not_issue(filename, title):
    # "'68 Scars TPB" with ComicInfo Number 1 used to be named issue #001.
    year = "2019" if "(2019)" in filename else None
    fields = media_fields(series=title, number="1", volume=None, style="issue",
                          tokens=fr.parse_name_tokens(filename), year=year)
    assert (fields.volume, fields.issue, fields.collection) == ("1", None, "TPB")


@pytest.mark.parametrize("filename,title", [("TPB Tales.cbz", "TPB Tales"), ("Series TPB.cbz", "Series TPB"),
    ("TPB Tales #003.cbz", "TPB Tales"), ("\u00e9TPB Series.cbz", "\u00e9TPB Series"),
    ("Series TPB\u00e9.cbz", "Series TPB\u00e9"), ("Series TPBX.cbz", "Series"),
    ("Caf\u00e9TPB.cbz", "Caf\u00e9"), ("Series TPB Extra.cbz", "Series"), ("TPB.cbz", "TPB"),
    ("TPB #003.cbz", "TPB"), ("TPB [Scan].cbz", "TPB"), ("TPB.cbz", "TPB (2019)")])
def test_tpb_inside_the_decided_title_or_a_longer_word_stays_an_issue(filename, title):
    """Codex's #157 R1: a title word is not a collection label.

    "TPB Tales", a series literally ending in TPB, and letters such as
    \u00e9TPB or TPBX keep their words, so Number 3 is still issue #003.
    """
    fields = media_fields(series=title, number="3", volume=None, style="issue",
                          tokens=fr.parse_name_tokens(filename))
    assert (fields.issue, fields.volume, fields.collection) == ("3", None, None)


def test_a_series_literally_named_tpb_keeps_a_year_tagged_issue():
    """Codex's #169 R1: tags or an issue year never turn the title into a label."""
    fields = media_fields(series="TPB", number="1", volume=None, style="issue",
                          tokens=fr.parse_name_tokens("TPB (2019).cbz"), year="2019")
    assert (fields.issue, fields.volume, fields.collection) == ("1", None, None)


def test_a_bare_tpb_takes_its_volume_from_number_or_volume_and_holds_a_conflict():
    tokens = fr.parse_name_tokens("Series TPB.cbz")
    assert media_fields(series="Series", number="3", volume=None, style="issue", tokens=tokens).volume == "3"
    assert media_fields(series="Series", number=None, volume="3", style="issue", tokens=tokens).volume == "3"
    with pytest.raises(CanonicalNameError, match="conflicts"):
        media_fields(series="Series", number="2", volume="3", style="issue", tokens=tokens)


def test_a_bare_tpb_without_a_number_is_held_rather_than_defaulted():
    # Full defaults to v01 by operator policy; nothing approves that for TPB.
    with pytest.raises(CanonicalNameError, match="no resolved volume"):
        media_fields(series="Series", number=None, volume=None, style="issue",
                     tokens=fr.parse_name_tokens("Series TPB.cbz"))


@pytest.mark.parametrize("filename", ["Series TPB #001.cbz", "Series TPB ch. 1.cbz"])
def test_a_tpb_label_plus_an_issue_or_chapter_number_is_held(filename):
    # Collection and primary number disagree about what the book is.
    with pytest.raises(CanonicalNameError, match="also claims"):
        media_fields(series="Series", number="1", volume=None, style="issue",
                     tokens=fr.parse_name_tokens(filename))


def test_a_bare_tpb_label_conflicts_with_chapter_style():
    with pytest.raises(CanonicalNameError, match="chapter style"):
        media_fields(series="Series", number="1", volume=None, style="chapter",
                     tokens=fr.parse_name_tokens("Series TPB.cbz"))


@pytest.mark.parametrize("resolved,folder", [("ICECREAMMAN", "Ice Cream Man"),
    ("68", "'68"), ("Series Name", "Series! Name"), ("Straße", "STRASSE")])
def test_normalized_equal_folder_spelling_wins_without_deleting_legal_punctuation(resolved, folder):
    assert series_display(resolved, folder) == folder


@pytest.mark.parametrize("resolved,folder", [("Tales for a HalloweeNight", "Tales for a Halloween Night"),
    ("Series Uncensored", "Series"), ("Series Season 2", "Series"), ("Other", "Series")])
def test_operator_chosen_series_folder_wins_substantive_alias_display(resolved, folder):
    assert series_display(resolved, folder) == folder


def test_manga_volumes_keep_volume_semantics_and_no_tpb_marker():
    fields = media_fields(series="Series", number="1", volume="1", style="chapter",
                          tokens=fr.parse_name_tokens("Series Vol. 1.cbz"))
    assert format_canonical_name(fields) == "Series v01.cbz"


def test_issue_and_collection_cannot_become_one_version_family():
    facts = tuple(fr.ArchiveFacts(str(i), name, fr.comicinfo_facts((
        b"<ComicInfo><Series>Series</Series><Number>1</Number></ComicInfo>",)))
        for i, name in enumerate(["Series #001 [v1].cbz", "Series TPB 1 [v2].cbz"]))
    family = fr.resolve_folder(fr.FolderFacts(None, facts)).families[0]
    assert not family.established
    assert any("collected-edition" in reason for reason in family.reasons)


def _watcher_name(tmp_path, filename, series="Usogui"):
    """Name an arrival the way the watcher does: real archive, real parser."""
    import zipfile
    from comic_automation.library.media_naming import watcher_fields
    from scripts.cbz_core import ALL_RULES, parse_comic_name

    path = tmp_path / series / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("001.png", b"page")
    parsed = parse_comic_name(path, rules=ALL_RULES - {"translate"}, display_tokens=True)
    fields = watcher_fields(parsed, fr.archive_facts_from_file(path), series=series, style="chapter")
    return format_canonical_name(fields)


@pytest.mark.parametrize("filename, expected", [
    # Taken from the 2026-11-09 Usogui arrival, where all 54 such side
    # stories were refused and held the whole 710-file directory back.
    ("Inglorious Vol.44 Ch.483.5 - Volume 44 Side Story.cbz", "Usogui v44 ch. 483.5.cbz"),
    ("Team Duwang Vol.5 Ch.53.5 - Volume 5 Side Story.cbz", "Usogui v05 ch. 53.5.cbz"),
])
def test_a_volume_stated_twice_with_one_value_is_one_claim(tmp_path, filename, expected):
    assert _watcher_name(tmp_path, filename) == expected


@pytest.mark.parametrize("filename", [
    # A trailing v2 may be a revision, not volume 2: two different values.
    "Team Duwang Vol.12 Ch.127 - Superhuman v2.cbz",
    "Series Vol.3 Ch.10 - Volume 4 Side Story.cbz",
    # Two different chapter numbers stay ambiguous too.
    "Team Duwang Vol.30 Ch.323 - Chapter 1, Fin.cbz",
])
def test_different_repeated_number_values_are_still_refused(tmp_path, filename):
    with pytest.raises(CanonicalNameError, match="conflicting arrival number tokens"):
        _watcher_name(tmp_path, filename)
