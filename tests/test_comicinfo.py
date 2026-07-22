from pathlib import Path
from unittest.mock import patch

from scripts.cbz_core import ParsedComicName, update_comicinfo_xml


def parsed(stem="Batman Ch.5", series="Batman", chapter="5", volume="1"):
    return ParsedComicName(
        original_path=Path("Batman/Batman Ch.5.cbz"),
        filename=f"{stem}.cbz",
        stem=stem,
        series=series,
        chapter=chapter,
        volume=volume,
    )


def test_update_comicinfo_adds_missing_tags():
    xml, changed = update_comicinfo_xml("<ComicInfo></ComicInfo>", parsed())

    assert changed is True
    assert "<Title>Batman Ch.5</Title>" in xml
    assert "<Series>Batman</Series>" in xml
    assert "<Number>5</Number>" in xml
    assert "<Volume>1</Volume>" in xml


def test_update_comicinfo_replaces_generic_title():
    source = "<ComicInfo><Title>Chapter 5</Title><Series>Bad Name</Series></ComicInfo>"

    xml, changed = update_comicinfo_xml(source, parsed())

    assert changed is True
    assert "<Title>Batman Ch.5</Title>" in xml
    assert "<Series>Batman</Series>" in xml


def test_update_comicinfo_preserves_custom_title():
    source = "<ComicInfo><Title>Batman: Year One</Title><Series>Batman</Series><Number>5</Number><Volume>1</Volume></ComicInfo>"

    xml, changed = update_comicinfo_xml(source, parsed())

    assert changed is False
    assert xml == source


def test_overwrite_generic_false_preserves_generic_title():
    source = "<ComicInfo><Title>Chapter 5</Title><Series>Batman</Series><Number>5</Number><Volume>1</Volume></ComicInfo>"

    xml, changed = update_comicinfo_xml(source, parsed(), overwrite_generic=False)

    assert changed is False
    assert xml == source


def parsed_sep(stem, series="Show", chapter=None, volume=None, season=None, episode=None, part=None):
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
    xml, changed = update_comicinfo_xml(
        "<ComicInfo></ComicInfo>",
        parsed_sep("Show Chapter 88 Ep. 1", chapter="88", episode="1"),
    )
    assert changed is True
    assert "<Number>88</Number>" in xml
    assert "Episode: 1" in xml
    assert "Season" not in xml


def test_season_episode_part_recorded_in_notes():
    xml, _ = update_comicinfo_xml(
        "<ComicInfo></ComicInfo>",
        parsed_sep("Show S1 E5 Part 2", season="1", episode="5", part="2"),
    )
    assert "<Notes>Season: 1 | Episode: 5 | Part: 2</Notes>" in xml


def test_sep_notes_idempotent():
    p = parsed_sep("Show S2 E7", season="2", episode="7")
    xml1, _ = update_comicinfo_xml("<ComicInfo></ComicInfo>", p)
    xml2, changed2 = update_comicinfo_xml(xml1, p)
    assert changed2 is False
    assert xml2.count("Season:") == 1


def test_sep_notes_replaces_stale_line_and_keeps_other_notes():
    p = parsed_sep("Show S2 E8", season="2", episode="8")
    xml, _ = update_comicinfo_xml(
        "<ComicInfo><Notes>Scanned by Group | Season: 2 | Episode: 7</Notes></ComicInfo>", p
    )
    assert "Scanned by Group" in xml
    assert "Episode: 8" in xml
    assert "Episode: 7" not in xml
    assert xml.count("Season:") == 1


def test_existing_native_metadata_fields_are_translated_and_preserved_in_notes():
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
