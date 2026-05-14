from pathlib import Path

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
