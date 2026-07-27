from __future__ import annotations

import json
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


IMAGE_EXTENSIONS = frozenset({
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".jfif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
})

COMIC_INFO_FILENAME = "comicinfo.xml"
MAX_COMIC_INFO_BYTES = 1_048_576


class ArchiveInspectionError(RuntimeError):
    """Base exception for archive inspection failures."""


class UnsupportedArchiveFormatError(ArchiveInspectionError):
    """Raised when no safe inspector exists for an archive format."""


class UnsafeComicInfoError(ArchiveInspectionError):
    """Raised when ComicInfo.xml exceeds safety limits."""


@dataclass(frozen=True)
class ComicInfoMetadata:
    title: str | None = None
    series: str | None = None
    number: str | None = None
    volume: int | None = None
    summary: str | None = None
    year: int | None = None
    month: int | None = None
    day: int | None = None
    writer: str | None = None
    penciller: str | None = None
    inker: str | None = None
    colorist: str | None = None
    letterer: str | None = None
    cover_artist: str | None = None
    editor: str | None = None
    publisher: str | None = None
    imprint: str | None = None
    genre: str | None = None
    tags: str | None = None
    language_iso: str | None = None
    format: str | None = None
    web: str | None = None


@dataclass(frozen=True)
class ArchiveInspection:
    path: str
    archive_format: str
    status: str
    entry_count: int
    page_count: int
    directory_count: int
    encrypted: bool
    comic_info_present: bool
    comic_info_valid: bool
    comic_info_error: str | None
    comic_info: ComicInfoMetadata | None
    crc_verified: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def inspect_archive(
    path: str | Path,
    *,
    verify_crc: bool = False,
) -> ArchiveInspection:
    archive_path = Path(path).resolve(strict=False)
    suffix = archive_path.suffix.casefold()

    if suffix != ".cbz":
        raise UnsupportedArchiveFormatError(
            f"Unsupported archive format: {suffix or '<none>'}"
        )

    return inspect_cbz(archive_path, verify_crc=verify_crc)


def inspect_cbz(
    path: str | Path,
    *,
    verify_crc: bool = False,
) -> ArchiveInspection:
    archive_path = Path(path).resolve(strict=False)

    if not archive_path.is_file():
        raise FileNotFoundError(str(archive_path))

    try:
        with zipfile.ZipFile(archive_path, mode="r") as archive:
            entries = archive.infolist()
            files = [entry for entry in entries if not entry.is_dir()]
            directories = [
                entry for entry in entries if entry.is_dir()
            ]

            image_entries = [
                entry
                for entry in files
                if Path(entry.filename).suffix.casefold()
                in IMAGE_EXTENSIONS
            ]

            encrypted = any(
                bool(entry.flag_bits & 0x1)
                for entry in files
            )

            comic_info_entry = next(
                (
                    entry
                    for entry in files
                    if Path(entry.filename).name.casefold()
                    == COMIC_INFO_FILENAME
                ),
                None,
            )

            metadata: ComicInfoMetadata | None = None
            comic_info_valid = False
            comic_info_error: str | None = None

            if comic_info_entry is not None:
                try:
                    metadata = _read_comic_info(
                        archive,
                        comic_info_entry,
                    )
                    comic_info_valid = True
                except Exception as exc:
                    comic_info_error = (
                        f"{type(exc).__name__}: {exc}"
                    )

            crc_verified = False

            if verify_crc:
                bad_entry = archive.testzip()

                if bad_entry is not None:
                    raise zipfile.BadZipFile(
                        f"CRC validation failed for {bad_entry!r}."
                    )

                crc_verified = True

    except zipfile.BadZipFile as exc:
        raise ArchiveInspectionError(
            f"Invalid or corrupt CBZ archive: {archive_path}"
        ) from exc

    status = "ok"

    if encrypted:
        status = "encrypted"
    elif not files:
        status = "empty_archive"
    elif not image_entries:
        status = "no_images"

    return ArchiveInspection(
        path=str(archive_path),
        archive_format="cbz",
        status=status,
        entry_count=len(files),
        page_count=len(image_entries),
        directory_count=len(directories),
        encrypted=encrypted,
        comic_info_present=comic_info_entry is not None,
        comic_info_valid=comic_info_valid,
        comic_info_error=comic_info_error,
        comic_info=metadata,
        crc_verified=crc_verified,
    )


def _read_comic_info(
    archive: zipfile.ZipFile,
    entry: zipfile.ZipInfo,
) -> ComicInfoMetadata:
    if entry.file_size > MAX_COMIC_INFO_BYTES:
        raise UnsafeComicInfoError(
            "ComicInfo.xml exceeds the 1 MiB safety limit."
        )

    payload = archive.read(entry)

    if len(payload) > MAX_COMIC_INFO_BYTES:
        raise UnsafeComicInfoError(
            "ComicInfo.xml exceeds the 1 MiB safety limit."
        )

    lowered = payload.lower()

    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise UnsafeComicInfoError(
            "ComicInfo.xml contains a prohibited DTD or entity."
        )

    root = ElementTree.fromstring(payload)

    return ComicInfoMetadata(
        title=_text(root, "Title"),
        series=_text(root, "Series"),
        number=_text(root, "Number"),
        volume=_integer(root, "Volume"),
        summary=_text(root, "Summary"),
        year=_integer(root, "Year"),
        month=_integer(root, "Month"),
        day=_integer(root, "Day"),
        writer=_text(root, "Writer"),
        penciller=_text(root, "Penciller"),
        inker=_text(root, "Inker"),
        colorist=_text(root, "Colorist"),
        letterer=_text(root, "Letterer"),
        cover_artist=_text(root, "CoverArtist"),
        editor=_text(root, "Editor"),
        publisher=_text(root, "Publisher"),
        imprint=_text(root, "Imprint"),
        genre=_text(root, "Genre"),
        tags=_text(root, "Tags"),
        language_iso=_text(root, "LanguageISO"),
        format=_text(root, "Format"),
        web=_text(root, "Web"),
    )


def _text(
    root: ElementTree.Element,
    element_name: str,
) -> str | None:
    element = root.find(element_name)

    if element is None or element.text is None:
        return None

    value = element.text.strip()
    return value or None


def _integer(
    root: ElementTree.Element,
    element_name: str,
) -> int | None:
    value = _text(root, element_name)

    if value is None:
        return None

    try:
        return int(value)
    except ValueError:
        return None
