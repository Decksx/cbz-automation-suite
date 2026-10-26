"""David's #2991 decisions: selected series identity, reboot years and Full."""

import pytest

from comic_automation.archive import field_resolution as fr
from scripts.cbz_canonical_name import CanonicalFields, format_canonical_name, parse_canonical_name


@pytest.mark.parametrize("series_year,issue_year,suffix", [
    (2016, None, "Batman (2016) #003.cbz"),
    (2016, 2016, "Batman (2016) #003.cbz"),
    (2016, 2020, "Batman (2016) #003.cbz"),
    (2026, 2026, "Batman (2026) #003.cbz"),
])
def test_series_and_issue_years_are_unambiguous_fixpoint_fields(series_year, issue_year, suffix):
    fields = CanonicalFields("Batman", issue=3, series_year=series_year, year=issue_year)
    assert format_canonical_name(fields) == suffix
    assert parse_canonical_name(suffix) == fields
    assert fr.parse_name_tokens(suffix).series == f"Batman ({series_year})"


def test_reboots_keep_distinct_identity_keys():
    assert fr.series_key("Batman (2016)") != fr.series_key("Batman (2026)")


def test_confirmed_folder_authority_changes_only_series_and_retains_metadata_evidence():
    ci = fr.comicinfo_facts((b"<ComicInfo><Series>English Alias</Series><Number>3</Number><Volume>2</Volume></ComicInfo>",))
    fact = fr.ArchiveFacts("a", "old #3.cbz", ci)
    result = fr.resolve_folder(fr.FolderFacts("Kaguya-sama", (fact,), series_folder_wins=True)).archives[0]
    assert result.fields["series"].value == "Kaguya-sama"
    assert any(e.source == fr.SOURCE_COMICINFO and e.value == "English Alias"
               for e in result.fields["series"].evidence)
    assert result.fields["number"].value == "3" and result.fields["volume"].value == "2"
