"""Collected-edition grammar keeps its marker independent from variants."""

import pytest

from scripts.cbz_canonical_name import (
    CanonicalFields, InvalidCollectionError, AmbiguousSeriesNameError,
    format_canonical_name, parse_canonical_name,
)


@pytest.mark.parametrize("volume", range(1, 7))
def test_numbered_tpb_round_trips_with_two_digit_volume(volume):
    fields = CanonicalFields("Series", volume=volume, collection="TPB")
    name = f"Series v{volume:02} (TPB).cbz"
    assert format_canonical_name(fields) == name
    assert parse_canonical_name(name) == fields


def test_collection_does_not_consume_edition_year_or_verified_version():
    fields = CanonicalFields("'68", volume=1, collection="TPB", edition="Colored",
                             year=2019, version=2)
    name = "'68 v01 (2019) (TPB) (Colored) [v2].cbz"
    assert format_canonical_name(fields) == name
    assert parse_canonical_name(name) == fields


@pytest.mark.parametrize("kwargs", [dict(collection="Full", volume=1),
    dict(collection="TPB"), dict(collection="TPB", volume=1, issue=3),
    dict(collection="TPB", volume=1, chapter=3)])
def test_collection_fields_refuse_ambiguous_or_incomplete_semantics(kwargs):
    with pytest.raises(InvalidCollectionError):
        CanonicalFields("Series", **kwargs)


def test_a_collection_marker_cannot_hide_in_series_display():
    with pytest.raises(AmbiguousSeriesNameError):
        CanonicalFields("Series (TPB)", volume=1)


def test_existing_issue_and_manga_volume_names_keep_their_distinct_facts():
    for fields, name in [
        (CanonicalFields("Series", issue=3, year=2019), "Series #003 (2019).cbz"),
        (CanonicalFields("Series", volume=1), "Series v01.cbz"),
    ]:
        assert format_canonical_name(fields) == name
        assert parse_canonical_name(name) == fields
