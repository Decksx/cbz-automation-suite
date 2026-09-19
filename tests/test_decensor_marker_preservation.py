"""Processed-status words must survive both import and sanitizer cleanup."""

from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree
import zipfile

from scripts import cbz_core, cbz_sanitizer, cbz_watcher


def test_shared_cleaner_keeps_bracketed_markers_but_drops_other_groups():
    assert cbz_core.sanitize("Book [Uncensored] [CoolScans]") == "Book Uncensored"
    assert cbz_core.clean_filename("Book (DECENSORED) (Group).cbz") == "Book DECENSORED.cbz"
    assert cbz_core.clean_directory_name("Series [Uncensored]") == "Series Uncensored"
    assert cbz_core.clean_xml_field("Volume 2 (Decensored)") == "Volume 2 Decensored"
    assert cbz_core.sanitize("Book [NotUncensored] [uncensoredness]") == "Book"


def test_import_parser_keeps_marker_after_chapter_and_mixed_title_normalization():
    parsed = cbz_core.parse_comic_name(Path("Series (Decensored)/Ch. 1 [Uncensored].cbz"))
    assert "uncensored" in parsed.filename.casefold()
    assert "decensored" in parsed.series.casefold()
    assert "uncensored" in cbz_core.sanitize("One Piece / ワンピース [Uncensored] Ch.005").casefold()


def test_comicinfo_replacement_keeps_existing_title_and_series_markers():
    parsed = cbz_core.parse_comic_name(Path("Series/Book Ch.1.cbz"))
    xml = (
        "<ComicInfo><Title>Manga Chapter [Uncensored]</Title>"
        "<Series>Series (Decensored)</Series></ComicInfo>"
    )
    with patch.object(cbz_core, "translate_metadata_text", side_effect=lambda text: (text, None)):
        updated, changed = cbz_core.update_comicinfo_xml(xml, parsed)
    root = ElementTree.fromstring(updated)
    assert changed
    assert "uncensored" in root.findtext("Title").casefold()
    assert "decensored" in root.findtext("Series").casefold()


def test_standalone_sanitizer_keeps_same_markers_in_names_and_xml_fields():
    assert cbz_sanitizer.clean_filename("Book [Uncensored] [Group].cbz") == "Book Uncensored.cbz"
    assert cbz_sanitizer.clean_directory_name("Series (Decensored)") == "Series Decensored"
    assert cbz_sanitizer.clean_xml_field("Book [Uncensored]") == "Book Uncensored"


def _marked_book(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.jpg", b"fixture")
        archive.writestr(
            "ComicInfo.xml",
            "<ComicInfo><Title>Manga Chapter [Uncensored]</Title>"
            "<Series>Series (Decensored)</Series></ComicInfo>",
        )


def _assert_markers_survived(path: Path) -> None:
    assert "uncensored" in path.name.casefold()
    with zipfile.ZipFile(path) as archive:
        info = ElementTree.fromstring(archive.read("ComicInfo.xml"))
    assert "uncensored" in info.findtext("Title").casefold()
    assert "decensored" in info.findtext("Series").casefold()


def test_watcher_import_keeps_markers_in_cbz_name_and_comicinfo(tmp_path):
    source = tmp_path / "Series (Decensored)" / "Book [Uncensored].cbz"
    _marked_book(source)
    with patch.object(cbz_watcher, "wait_for_file_stable", return_value=True), \
            patch.object(cbz_core, "translate_metadata_text", side_effect=lambda text: (text, None)):
        result, parsed = cbz_watcher.process_cbz_file(source)
    assert parsed is not None
    _assert_markers_survived(result)


def test_sanitizer_keeps_markers_in_cbz_name_and_comicinfo(tmp_path):
    source = tmp_path / "Series (Decensored)" / "Book [Uncensored].cbz"
    _marked_book(source)
    with patch.object(cbz_core, "translate_metadata_text", side_effect=lambda text: (text, None)):
        result = cbz_sanitizer.process_cbz_file(source)
    _assert_markers_survived(result)


def test_sanitizer_fallback_keeps_markers_without_shared_parser(tmp_path, monkeypatch):
    source = tmp_path / "Series (Decensored)" / "Book [Uncensored].cbz"
    _marked_book(source)
    monkeypatch.setattr(cbz_sanitizer, "_parse_comic_name", None)
    monkeypatch.setattr(cbz_sanitizer, "_update_comicinfo_xml", None)
    result = cbz_sanitizer.process_cbz_file(source)
    _assert_markers_survived(result)
