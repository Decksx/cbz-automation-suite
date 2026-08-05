"""Tests for ComicInfo.xml metadata generation/merging in scripts.cbz_core.

update_comicinfo_xml() takes an existing ComicInfo.xml string (possibly empty)
plus a ParsedComicName describing what the filename parser extracted, and
returns (new_xml, changed). These tests cover:
  - inserting missing tags into a bare <ComicInfo></ComicInfo> document
  - respecting/overwriting "generic" titles (e.g. "Chapter 5") vs. custom ones
  - season/episode/part handling, which is recorded in <Notes> rather than
    dedicated ComicInfo tags since the schema has no native support for them
  - idempotency: re-running the update on its own output should be a no-op
  - translation of native-language (e.g. Japanese) titles/summaries via the
    googletrans-backed helper, with the original text preserved as "Original
    Title"/"Original Summary" notes for reference
"""

from pathlib import Path
from unittest.mock import patch

from scripts.cbz_core import ParsedComicName, update_comicinfo_xml


def parsed(stem="Batman Ch.5", series="Batman", chapter="5", volume="1"):
    """Build a minimal ParsedComicName for the common chapter/volume case.

    Defaults describe a single-file chapter book ("Batman Ch.5") so most
    tests only need to override the fields relevant to what they're testing.
    """
    return ParsedComicName(
        original_path=Path("Batman/Batman Ch.5.cbz"),
        filename=f"{stem}.cbz",
        stem=stem,
        series=series,
        chapter=chapter,
        volume=volume,
    )


def test_update_comicinfo_adds_missing_tags():
    """An empty ComicInfo document should get Title/Series/Number/Volume added."""
    xml, changed = update_comicinfo_xml("<ComicInfo></ComicInfo>", parsed())

    assert changed is True
    assert "<Title>Batman Ch.5</Title>" in xml
    assert "<Series>Batman</Series>" in xml
    assert "<Number>5</Number>" in xml
    assert "<Volume>1</Volume>" in xml


def test_update_comicinfo_replaces_generic_title():
    """A title/series that looks auto-generated ("Chapter 5", "Bad Name")
    should be overwritten with the values derived from the parsed filename.
    """
    source = "<ComicInfo><Title>Chapter 5</Title><Series>Bad Name</Series></ComicInfo>"

    xml, changed = update_comicinfo_xml(source, parsed())

    assert changed is True
    assert "<Title>Batman Ch.5</Title>" in xml
    assert "<Series>Batman</Series>" in xml


def test_update_comicinfo_preserves_custom_title():
    """If the existing Title/Series/Number/Volume already match what we'd
    write (i.e. nothing is stale or generic), the XML must be left untouched
    byte-for-byte and changed must report False.
    """
    source = "<ComicInfo><Title>Batman: Year One</Title><Series>Batman</Series><Number>5</Number><Volume>1</Volume></ComicInfo>"

    xml, changed = update_comicinfo_xml(source, parsed())

    assert changed is False
    assert xml == source


def test_overwrite_generic_false_preserves_generic_title():
    """With overwrite_generic=False, even a generic-looking Title like
    "Chapter 5" must be left alone -- this flag lets callers opt out of the
    generic-title-replacement behavior entirely.
    """
    source = "<ComicInfo><Title>Chapter 5</Title><Series>Batman</Series><Number>5</Number><Volume>1</Volume></ComicInfo>"

    xml, changed = update_comicinfo_xml(source, parsed(), overwrite_generic=False)

    assert changed is False
    assert xml == source


def parsed_sep(stem, series="Show", chapter=None, volume=None, season=None, episode=None, part=None):
    """Build a ParsedComicName for season/episode/part (SEP) scenarios,
    e.g. TV-style releases ("Show S1 E5 Part 2") that don't map cleanly onto
    the chapter/volume model used by parsed() above.
    """
    from pathlib import Path
    return ParsedComicName(
        original_path=Path(f"{series}/{stem}.cbz"),
        filename=f"{stem}.cbz",
        stem=stem,
        series=series,
        chapter=chapter,
        volume=volume,
        season=season,
        episode=episode,
        part=part,
    )


def test_chapter_owns_number_when_episode_present():
    """When both a chapter number and an episode number are present, Number
    should reflect the chapter (not the episode), and the episode should
    only show up in the free-text notes rather than a dedicated field -- and
    Season should not appear at all if no season was parsed.
    """
    xml, changed = update_comicinfo_xml(
        "<ComicInfo></ComicInfo>",
        parsed_sep("Show Chapter 88 Ep. 1", chapter="88", episode="1"),
    )
    assert changed is True
    assert "<Number>88</Number>" in xml
    assert "Episode: 1" in xml
    assert "Season" not in xml


def test_season_episode_part_recorded_in_notes():
    """Season/Episode/Part all get folded into a single pipe-delimited line
    inside <Notes>, since ComicInfo.xml has no native tags for them.
    """
    xml, _ = update_comicinfo_xml(
        "<ComicInfo></ComicInfo>",
        parsed_sep("Show S1 E5 Part 2", season="1", episode="5", part="2"),
    )
    assert "<Notes>Season: 1 | Episode: 5 | Part: 2</Notes>" in xml


def test_sep_notes_idempotent():
    """Running the update twice with the same parsed data should not
    duplicate the Season/Episode notes line on the second pass.
    """
    p = parsed_sep("Show S2 E7", season="2", episode="7")
    xml1, _ = update_comicinfo_xml("<ComicInfo></ComicInfo>", p)
    xml2, changed2 = update_comicinfo_xml(xml1, p)
    assert changed2 is False
    assert xml2.count("Season:") == 1


def test_sep_notes_replaces_stale_line_and_keeps_other_notes():
    """If the Season/Episode line in Notes is out of date (e.g. episode
    number changed), it should be replaced in place -- while any unrelated
    note text ("Scanned by Group") already present is preserved.
    """
    p = parsed_sep("Show S2 E8", season="2", episode="8")
    xml, _ = update_comicinfo_xml(
        "<ComicInfo><Notes>Scanned by Group | Season: 2 | Episode: 7</Notes></ComicInfo>", p
    )
    assert "Scanned by Group" in xml
    assert "Episode: 8" in xml
    assert "Episode: 7" not in xml
    assert xml.count("Season:") == 1


def test_existing_native_metadata_fields_are_translated_and_preserved_in_notes():
    """When the source ComicInfo already has native-language Title/Series/
    Summary (e.g. Japanese), the translation helper should be used to
    produce English values for the standard tags, while the original
    native-language text is preserved as "Original Title"/"Original
    Summary" lines in Notes so nothing is lost.

    _run_googletrans is patched so the test doesn't depend on network
    access or the real translation service.
    """
    source = (
        "<ComicInfo>"
        "<Title>鬼滅の刃</Title>"
        "<Series>鬼滅の刃</Series>"
        "<Summary>鬼を倒す少年の物語</Summary>"
        "</ComicInfo>"
    )

    translations = {
        "鬼滅の刃": ("Demon Slayer", "ja"),
        "鬼を倒す少年の物語": ("The story of a boy who slays demons", "ja"),
        "Batman": ("Batman", "en"),
    }

    with patch(
        "scripts.cbz_core._run_googletrans",
        side_effect=lambda text: translations.get(text, (text, "en")),
    ):
        xml, changed = update_comicinfo_xml(
            source,
            parsed(stem="Demon Slayer Ch.5", series="Demon Slayer"),
        )

    assert changed is True
    assert "<Title>Demon Slayer Ch.5</Title>" in xml
    assert "<Series>Demon Slayer</Series>" in xml
    assert "<Summary>The story of a boy who slays demons</Summary>" in xml
    assert "Original Title: 鬼滅の刃" in xml
    assert "Original Summary: 鬼を倒す少年の物語" in xml


def test_translated_parsed_title_writes_english_title_and_original_alternate():
    """When the *parsed filename* itself already carries both an
    original_title and a translated_title (i.e. the filename parser did the
    translation, not update_comicinfo_xml), the English translated_title
    should be written as Title/Series, and the original native-language
    series name should be preserved via <AlternateSeries> rather than Notes.
    """
    translated = ParsedComicName(
        original_path=Path("鬼滅の刃/鬼滅の刃 第5話.cbz"),
        filename="Demon Slayer Ch. 5.cbz",
        stem="Demon Slayer Ch. 5",
        series="鬼滅の刃",
        chapter="5",
        volume=None,
        original_title="鬼滅の刃",
        translated_title="Demon Slayer",
    )

    with patch(
        "scripts.cbz_core._run_googletrans",
        return_value=("Demon Slayer", "ja"),
    ):
        xml, changed = update_comicinfo_xml("<ComicInfo></ComicInfo>", translated)

    assert changed is True
    assert "<Title>Demon Slayer Ch. 5</Title>" in xml
    assert "<Series>Demon Slayer</Series>" in xml
    assert "<AlternateSeries>鬼滅の刃</AlternateSeries>" in xml
