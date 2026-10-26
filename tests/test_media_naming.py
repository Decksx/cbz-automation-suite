"""Media semantics and title display use only their intended authority."""

import pytest

from comic_automation.archive import field_resolution as fr
from comic_automation.library.media_naming import media_fields, series_display
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
    result = media_fields(series="Series", number=None, volume=None, style="issue", tokens=tokens)
    assert result.volume == "1" and result.collection == "TPB"
    fields = media_fields(series="Series Full", number="1", volume=None, style="issue", tokens=tokens)
    assert fields.series == "Series Full"


@pytest.mark.parametrize("number", range(1, 7))
def test_each_numbered_tpb_maps_its_own_volume_without_a_default(number):
    result = resolve(f"Series TPB {number}.cbz", (
        f"<ComicInfo><Series>Series</Series><Number>{number}</Number></ComicInfo>".encode()))
    assert result.fields["volume"].value == str(number)
    fields = media_fields(series="Series", number=str(number), volume=str(number), style="issue",
                          tokens=fr.parse_name_tokens(result.filename))
    assert format_canonical_name(fields) == f"Series v{number:02} (TPB).cbz"


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
