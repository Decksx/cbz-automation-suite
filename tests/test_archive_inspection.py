"""Tests for comic_automation.archive.inspect_archive: the low-level CBZ
inspection pipeline that opens an archive, counts pages/entries, and
parses ComicInfo.xml if present.

Covers: page/entry counting distinguishing images from non-image entries,
successful ComicInfo.xml parsing, malformed ComicInfo.xml being treated as
absent/invalid metadata WITHOUT failing the archive as a whole, XXE/DTD
rejection for untrusted ComicInfo.xml, classification of image-less and
truly-empty archives, opt-in CRC verification, and error handling for
corrupt archives, unsupported formats, and missing files.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from comic_automation.archive import (
    ArchiveInspectionError,
    UnsupportedArchiveFormatError,
    inspect_archive,
)


def create_cbz(
    path: Path,
    entries: dict[str, bytes],
) -> Path:
    """Build a .cbz at `path` containing the given {name: content} entries."""
    path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(
        path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)

    return path


def test_inspects_cbz_pages_and_entries(
    tmp_path: Path,
) -> None:
    """Basic happy path: an archive with two images (one .jpg, one
    uppercase .PNG) and one non-image file should report status "ok",
    entry_count 3, but page_count only 2 -- image entries are counted
    case-insensitively by extension, non-image entries are not pages.
    """
    archive = create_cbz(
        tmp_path / "issue.cbz",
        {
            "001.jpg": b"image-one",
            "002.PNG": b"image-two",
            "notes.txt": b"notes",
        },
    )

    result = inspect_archive(archive)

    assert result.archive_format == "cbz"
    assert result.status == "ok"
    assert result.entry_count == 3
    assert result.page_count == 2
    assert result.encrypted is False
    assert result.comic_info_present is False
    assert result.crc_verified is False


def test_reads_comic_info_metadata(
    tmp_path: Path,
) -> None:
    """A well-formed ComicInfo.xml should be parsed into structured fields
    (title, series, number, volume, year, writer, language), with numeric
    fields (Number, Volume, Year) converted to their proper types.
    """
    archive = create_cbz(
        tmp_path / "metadata.cbz",
        {
            "cover.webp": b"cover",
            "ComicInfo.xml": b"""
                <ComicInfo>
                    <Title>Issue Title</Title>
                    <Series>Example Series</Series>
                    <Number>7.5</Number>
                    <Volume>2</Volume>
                    <Year>2026</Year>
                    <Writer>Example Writer</Writer>
                    <LanguageISO>en</LanguageISO>
                </ComicInfo>
            """,
        },
    )

    result = inspect_archive(archive)

    assert result.comic_info_present is True
    assert result.comic_info_valid is True
    assert result.comic_info_error is None
    assert result.comic_info is not None
    assert result.comic_info.title == "Issue Title"
    assert result.comic_info.series == "Example Series"
    assert result.comic_info.number == "7.5"
    assert result.comic_info.volume == 2
    assert result.comic_info.year == 2026
    assert result.comic_info.writer == "Example Writer"
    assert result.comic_info.language_iso == "en"


def test_invalid_comic_info_does_not_invalidate_archive(
    tmp_path: Path,
) -> None:
    """Malformed ComicInfo.xml (truncated here) should be reported as
    present-but-invalid, with a comic_info_error explaining why -- but the
    archive as a whole must still inspect successfully (status "ok") since
    broken metadata shouldn't block processing of the actual pages.
    """
    archive = create_cbz(
        tmp_path / "invalid-metadata.cbz",
        {
            "001.jpg": b"image",
            "ComicInfo.xml": b"<ComicInfo><Title>",
        },
    )

    result = inspect_archive(archive)

    assert result.status == "ok"
    assert result.page_count == 1
    assert result.comic_info_present is True
    assert result.comic_info_valid is False
    assert result.comic_info is None
    assert result.comic_info_error is not None


def test_rejects_comic_info_dtd(
    tmp_path: Path,
) -> None:
    """Security guard: a ComicInfo.xml containing a DOCTYPE/ENTITY
    declaration (an XXE-style attack vector) must be rejected as invalid
    with an error mentioning it's prohibited, rather than being parsed --
    ComicInfo.xml comes from untrusted archive files, not a trusted source.
    """
    archive = create_cbz(
        tmp_path / "unsafe-metadata.cbz",
        {
            "001.jpg": b"image",
            "ComicInfo.xml": b"""
                <!DOCTYPE ComicInfo [
                    <!ENTITY example "unsafe">
                ]>
                <ComicInfo>
                    <Title>&example;</Title>
                </ComicInfo>
            """,
        },
    )

    result = inspect_archive(archive)

    assert result.status == "ok"
    assert result.comic_info_present is True
    assert result.comic_info_valid is False
    assert "prohibited" in result.comic_info_error.lower()


def test_cbz_without_images_is_classified(
    tmp_path: Path,
) -> None:
    """An archive with entries but no recognized image files should be
    classified with status "no_images" rather than "ok", since there's
    nothing to actually read as comic pages.
    """
    archive = create_cbz(
        tmp_path / "empty.cbz",
        {"notes.txt": b"no images"},
    )

    result = inspect_archive(archive)

    assert result.status == "no_images"
    assert result.page_count == 0



def test_truly_empty_cbz_is_classified(
    tmp_path: Path,
) -> None:
    """A zip file with zero entries at all (distinct from "has entries but
    no images") should be classified with its own status, "empty_archive".
    """
    archive = tmp_path / "truly-empty.cbz"

    with zipfile.ZipFile(archive, mode="w"):
        pass

    result = inspect_archive(archive)

    assert result.status == "empty_archive"
    assert result.entry_count == 0
    assert result.page_count == 0
    assert result.comic_info_present is False

def test_crc_verification_can_be_enabled(
    tmp_path: Path,
) -> None:
    """CRC verification is opt-in via verify_crc=True (off by default,
    presumably for performance); when enabled on a valid archive, it
    should report crc_verified=True.
    """
    archive = create_cbz(
        tmp_path / "verified.cbz",
        {"001.jpg": b"image"},
    )

    result = inspect_archive(archive, verify_crc=True)

    assert result.status == "ok"
    assert result.crc_verified is True


def test_corrupt_cbz_raises_inspection_error(
    tmp_path: Path,
) -> None:
    """A file with a .cbz extension whose content isn't actually a valid
    zip archive should raise ArchiveInspectionError with a message
    identifying it as invalid/corrupt.
    """
    archive = tmp_path / "corrupt.cbz"
    archive.write_bytes(b"this is not a zip archive")

    with pytest.raises(
        ArchiveInspectionError,
        match="Invalid or corrupt CBZ",
    ):
        inspect_archive(archive)


def test_unsupported_archive_is_rejected(
    tmp_path: Path,
) -> None:
    """A .cbr (RAR-based) archive is not a supported format for this
    inspection pipeline and should raise UnsupportedArchiveFormatError.
    """
    archive = tmp_path / "issue.cbr"
    archive.write_bytes(b"rar")

    with pytest.raises(
        UnsupportedArchiveFormatError,
        match="Unsupported archive format",
    ):
        inspect_archive(archive)


def test_missing_archive_raises_file_not_found(
    tmp_path: Path,
) -> None:
    """Inspecting a path that doesn't exist on disk should raise the
    standard FileNotFoundError rather than a custom archive error.
    """
    with pytest.raises(FileNotFoundError):
        inspect_archive(tmp_path / "missing.cbz")
