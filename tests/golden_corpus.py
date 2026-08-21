"""A deterministic synthetic corpus of CBZ archives.

Every fixture here is *byte-reproducible*: building the same case twice, on
any machine, in any order, produces the same file bytes and therefore the
same SHA-256. That is the whole point of a golden corpus. A fixture whose
bytes drift cannot anchor a regression test, because a later failure is
then indistinguishable from the fixture having changed underneath it.

Two things had to be pinned to get there.

**ZIP member timestamps.** `ZipFile.writestr(name, data)` stamps each member
with *the current local time*, so two archives built a second apart differ
in bytes while being logically identical. Every member here is written
through an explicit `ZipInfo` carrying `FIXED_DATE_TIME`.

**Image encoding.** Page images are generated from a seed with Pillow and
saved as PNG, which Pillow writes deterministically -- no timestamp chunk,
no encoder nondeterminism. This is asserted by a test rather than assumed,
because it is a property of the library rather than of this module.

Nothing here touches the production database, the live library, or the
network. Every builder writes inside a caller-supplied `tmp_path`.

Scope note
----------

These are *synthetic* fixtures, deliberately small. The roadmap's
"large or sensitive real-world fixtures outside Git, referenced through an
optional integration-test manifest" is a separate concern and is not
implemented here.
"""

from __future__ import annotations

import hashlib
import zipfile
import zlib
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw


# Pinned so archive bytes do not depend on when the test ran. The value is
# arbitrary; only its constancy matters.
FIXED_DATE_TIME = (2026, 1, 1, 0, 0, 0)

# Stored rather than deflated: zlib's output is stable for a given input and
# level, but the level is a global default that a dependency could change.
# Storing removes the compressor from the reproducibility argument entirely.
FIXED_COMPRESSION = zipfile.ZIP_STORED

COMIC_INFO = "ComicInfo.xml"


def page_image(
    seed: int,
    *,
    size: tuple[int, int] = (64, 64),
    image_format: str = "PNG",
) -> bytes:
    """A deterministic, decodable page image derived from `seed`.

    Two different seeds always produce different bytes, and the same seed
    always produces identical bytes. Both directions are asserted by the
    corpus tests -- a "different" page that happened to encode identically
    would silently turn a difference test into a no-op.
    """
    image = Image.new("RGB", size, "white")
    drawing = ImageDraw.Draw(image)

    # Geometry driven entirely by the seed, so distinct seeds cannot
    # collide into the same rendering.
    offset = (seed * 7) % 24
    drawing.rectangle(
        (2 + offset, 4, 20 + offset, 40 + (seed % 17)),
        fill=(seed % 256, (seed * 3) % 256, (seed * 11) % 256),
    )
    drawing.ellipse(
        (26, 8 + (seed % 13), 58, 40 + (seed % 9)),
        fill=((seed * 5) % 256, (seed * 13) % 256, (seed * 2) % 256),
    )

    buffer = BytesIO()
    image.save(buffer, format=image_format)
    return buffer.getvalue()


def comic_info_xml(
    title: str = "Golden", number: str = "1", series: str = "Corpus"
) -> bytes:
    """Minimal, well-formed ComicInfo.xml."""
    return (
        "<?xml version='1.0' encoding='utf-8'?>"
        "<ComicInfo>"
        f"<Series>{series}</Series>"
        f"<Title>{title}</Title>"
        f"<Number>{number}</Number>"
        "</ComicInfo>"
    ).encode("utf-8")


def build_cbz(
    path: Path,
    entries: list[tuple[str, bytes]],
    *,
    date_time: tuple[int, int, int, int, int, int] = FIXED_DATE_TIME,
    compression: int = FIXED_COMPRESSION,
) -> Path:
    """Write a byte-reproducible .cbz containing `entries` in order.

    `entries` is a list rather than a dict so member *order* is part of the
    fixture. Page ordering is one of the properties under test, and a dict
    would make the archive's physical order an artefact of insertion.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(path, mode="w", compression=compression) as archive:
        for name, payload in entries:
            info = zipfile.ZipInfo(filename=name, date_time=date_time)
            info.compress_type = compression
            # Every field below is otherwise derived from the host, which
            # would make the same corpus differ between a developer machine
            # and CI while every logical property held.
            #
            # `create_system` is the one that actually bites: ZipInfo sets
            # it to 0 on Windows and 3 on everything else, so the bytes --
            # and therefore every frozen digest here -- would differ purely
            # by operating system. Pinned to 0 rather than to the host's
            # value, because "reproducible on this machine" is not the
            # claim being made.
            info.create_system = 0
            info.create_version = 20
            info.extract_version = 20
            info.internal_attr = 0
            info.external_attr = 0o600 << 16
            archive.writestr(info, payload)

    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 16), b""):
            digest.update(chunk)

    return digest.hexdigest()


def member_names(path: Path) -> list[str]:
    """Member names in physical archive order."""
    with zipfile.ZipFile(path) as archive:
        return [info.filename for info in archive.infolist()]


def member_payloads(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {
            info.filename: archive.read(info.filename)
            for info in archive.infolist()
            if not info.is_dir()
        }


# --- the cases -----------------------------------------------------------


@dataclass(frozen=True)
class Case:
    """One named corpus fixture.

    `build` takes a destination path and returns it. `expects_valid_zip`
    records whether the archive is structurally readable at all, so a test
    can iterate the whole corpus and still know which cases are supposed to
    raise.
    """

    name: str
    description: str
    build: Callable[[Path], Path]
    expects_valid_zip: bool = True
    expects_images: bool = True
    tags: tuple[str, ...] = field(default_factory=tuple)


def _ordinary(path: Path) -> Path:
    return build_cbz(
        path,
        [
            (COMIC_INFO, comic_info_xml()),
            ("001.png", page_image(1)),
            ("002.png", page_image(2)),
            ("003.png", page_image(3)),
        ],
    )


def _comicinfo_only_change(path: Path) -> Path:
    """Identical pages, different metadata.

    The pair (`ordinary`, this) is what distinguishes a content-addressed
    page inventory from a whole-file hash: every page digest must match
    across the two while the archive digests differ.
    """
    return build_cbz(
        path,
        [
            (COMIC_INFO, comic_info_xml(title="Changed", number="2")),
            ("001.png", page_image(1)),
            ("002.png", page_image(2)),
            ("003.png", page_image(3)),
        ],
    )


def _reordered_pages(path: Path) -> Path:
    """The same three page payloads, in reverse physical order.

    Names travel with their payloads, so this is a reordering of the
    *archive*, not a renaming of pages.
    """
    return build_cbz(
        path,
        [
            (COMIC_INFO, comic_info_xml()),
            ("003.png", page_image(3)),
            ("002.png", page_image(2)),
            ("001.png", page_image(1)),
        ],
    )


def _one_page_difference(path: Path) -> Path:
    """`ordinary` with its final page replaced by a different image."""
    return build_cbz(
        path,
        [
            (COMIC_INFO, comic_info_xml()),
            ("001.png", page_image(1)),
            ("002.png", page_image(2)),
            ("003.png", page_image(99)),
        ],
    )


def _one_page_fewer(path: Path) -> Path:
    return build_cbz(
        path,
        [
            (COMIC_INFO, comic_info_xml()),
            ("001.png", page_image(1)),
            ("002.png", page_image(2)),
        ],
    )


def _duplicate_pages(path: Path) -> Path:
    """Two members with byte-identical payloads under different names."""
    duplicated = page_image(1)
    return build_cbz(
        path,
        [
            (COMIC_INFO, comic_info_xml()),
            ("001.png", duplicated),
            ("002.png", duplicated),
            ("003.png", page_image(3)),
        ],
    )


def _unicode_paths(path: Path) -> Path:
    """Non-ASCII and punctuation in member names.

    Includes a combining sequence and a full-width colon, both of which have
    bitten path handling on this project before.
    """
    return build_cbz(
        path,
        [
            (COMIC_INFO, comic_info_xml(series="Série", title="Nüll")),
            ("00１－é.png", page_image(1)),
            ("002 — side：a.png", page_image(2)),
            ("日本語/003.png", page_image(3)),
        ],
    )


def _unsafe_members(path: Path) -> Path:
    """Member names that would escape the destination on naive extraction.

    Nothing in this project extracts archives to disk, so these are not an
    active vulnerability -- see `docs/archive_io_resource_audit.md`,
    "Confirmed risks in current code". They are in the corpus because the
    rewrite paths *preserve* such names verbatim rather than rejecting them,
    and that behaviour should be pinned rather than left to be rediscovered.
    """
    return build_cbz(
        path,
        [
            (COMIC_INFO, comic_info_xml()),
            ("001.png", page_image(1)),
            ("../escape.png", page_image(2)),
            ("nested/../../escape2.png", page_image(3)),
            ("/absolute.png", page_image(4)),
        ],
    )


def _no_images(path: Path) -> Path:
    return build_cbz(
        path,
        [
            (COMIC_INFO, comic_info_xml()),
            ("readme.txt", b"no pages here"),
        ],
    )


def _empty_archive(path: Path) -> Path:
    return build_cbz(path, [])


def _truncated_image(path: Path) -> Path:
    """Valid ZIP, structurally intact, containing an unreadable image.

    The archive opens and lists cleanly; only decoding fails. This is the
    shape that separates "corrupt archive" from "corrupt page", which the
    terminal-failure classification depends on.
    """
    full = page_image(1)
    return build_cbz(
        path,
        [
            (COMIC_INFO, comic_info_xml()),
            ("001.png", full[: len(full) // 3]),
            ("002.png", page_image(2)),
        ],
    )


def _unidentified_image(path: Path) -> Path:
    """An image extension over bytes that are not an image at all."""
    return build_cbz(
        path,
        [
            (COMIC_INFO, comic_info_xml()),
            ("001.png", b"this is definitely not a PNG"),
            ("002.png", page_image(2)),
        ],
    )


def _corrupt_zip(path: Path) -> Path:
    """A truncated ZIP: the central directory is gone."""
    _ordinary(path)
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])
    return path


def _malformed_comicinfo(path: Path) -> Path:
    """Unparseable metadata in an otherwise sound archive.

    Must not fail the archive as a whole -- `inspection.py` treats invalid
    ComicInfo as absent, and that contract is worth pinning.
    """
    return build_cbz(
        path,
        [
            (COMIC_INFO, b"<ComicInfo><Title>unclosed"),
            ("001.png", page_image(1)),
            ("002.png", page_image(2)),
        ],
    )


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        len(data).to_bytes(4, "big")
        + tag
        + data
        + (zlib.crc32(tag + data) & 0xFFFFFFFF).to_bytes(4, "big")
    )


def declared_size_png(width: int, height: int) -> bytes:
    """A tiny PNG whose header *declares* an enormous image.

    Decoders check the declared dimensions in the IHDR before allocating,
    which is exactly how a decompression bomb is caught -- so a 68-byte file
    exercises the decoded-pixel limit without a fixture that costs hundreds
    of megabytes to build or store.

    The pixel data is deliberately not real. This fixture is for the
    *dimension* guard; anything that gets past that guard fails on the
    truncated image data instead, which is a different case the corpus
    already covers.
    """
    signature = b"\x89PNG\r\n\x1a\n"
    header = _png_chunk(
        b"IHDR",
        width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + bytes([8, 2, 0, 0, 0]),
    )
    data = _png_chunk(b"IDAT", zlib.compress(b"\x00" * 16))
    end = _png_chunk(b"IEND", b"")
    return signature + header + data + end


# Pillow warns above `Image.MAX_IMAGE_PIXELS` and only raises
# `DecompressionBombError` above *twice* that value, which
# `perceptual_hashing.py` documents and relies on. Both bands are
# represented so a change to either threshold is visible.
PIXEL_WARNING_DIMENSIONS = (12_000, 9_000)   # 108 MP, warning band
PIXEL_BOMB_DIMENSIONS = (20_000, 9_000)      # 180 MP, error band


def _decoded_pixel_warning(path: Path) -> Path:
    return build_cbz(
        path,
        [
            (COMIC_INFO, comic_info_xml()),
            ("001.png", declared_size_png(*PIXEL_WARNING_DIMENSIONS)),
        ],
    )


def _decoded_pixel_bomb(path: Path) -> Path:
    return build_cbz(
        path,
        [
            (COMIC_INFO, comic_info_xml()),
            ("001.png", declared_size_png(*PIXEL_BOMB_DIMENSIONS)),
        ],
    )


def _many_entries(path: Path) -> Path:
    """A legitimate high-entry-count archive, used as a control.

    120 pages is more than a typical chapter and less than anything
    pathological. This is **not** the roadmap's "excessive entries" case:
    there is no entry-count limit in the codebase to exercise, so this
    fixture pins that a large-but-ordinary archive is handled normally
    rather than proving a guard. See `CORPUS_GAPS`.
    """
    entries: list[tuple[str, bytes]] = [(COMIC_INFO, comic_info_xml())]
    entries.extend(
        (f"{index:04d}.png", page_image(index)) for index in range(1, 121)
    )
    return build_cbz(path, entries)


CASES: tuple[Case, ...] = (
    Case("ordinary", "A valid three-page CBZ with metadata.", _ordinary,
         tags=("baseline",)),
    Case("comicinfo_only_change",
         "Identical pages, different ComicInfo.", _comicinfo_only_change,
         tags=("metadata", "pairs-with-ordinary")),
    Case("reordered_pages",
         "Identical page payloads in reverse archive order.",
         _reordered_pages, tags=("ordering", "pairs-with-ordinary")),
    Case("one_page_difference",
         "One page replaced with different content.", _one_page_difference,
         tags=("content", "pairs-with-ordinary")),
    Case("one_page_fewer", "A page removed.", _one_page_fewer,
         tags=("content", "pairs-with-ordinary")),
    Case("duplicate_pages",
         "Two byte-identical pages under different names.",
         _duplicate_pages, tags=("duplicates",)),
    Case("unicode_paths",
         "Non-ASCII, combining marks and punctuation in member names.",
         _unicode_paths, tags=("unicode",)),
    Case("unsafe_members",
         "Traversing and absolute member names.", _unsafe_members,
         tags=("safety",)),
    Case("no_images", "Structurally valid, contains no image entries.",
         _no_images, expects_images=False, tags=("classification",)),
    Case("empty_archive", "A valid but completely empty ZIP.",
         _empty_archive, expects_images=False, tags=("classification",)),
    Case("truncated_image", "Valid ZIP, one undecodable page.",
         _truncated_image, tags=("corruption",)),
    Case("unidentified_image", "Image extension over non-image bytes.",
         _unidentified_image, tags=("corruption",)),
    Case("malformed_comicinfo", "Unparseable ComicInfo, sound pages.",
         _malformed_comicinfo, tags=("metadata", "corruption")),
    Case("corrupt_zip", "Truncated ZIP with no central directory.",
         _corrupt_zip, expects_valid_zip=False, expects_images=False,
         tags=("corruption",)),
    Case("many_entries",
         "120 pages: a legitimate high-count control, not a limit test.",
         _many_entries, tags=("control",)),
    Case("decoded_pixel_warning",
         "Header declares 108 MP: above the pixel limit, below the "
         "raise threshold.",
         _decoded_pixel_warning, tags=("limits", "resource")),
    Case("decoded_pixel_bomb",
         "Header declares 180 MP: above twice the limit, where Pillow "
         "raises.",
         _decoded_pixel_bomb, tags=("limits", "resource")),
)


# Roadmap shapes this corpus deliberately does not cover, and why. Recorded
# here rather than left as an absence, because a missing fixture is
# indistinguishable from an overlooked one.
CORPUS_GAPS = {
    "excessive_entries": (
        "No entry-count limit exists anywhere in the codebase, so there is "
        "no guard to exercise. `many_entries` is a legitimate high-count "
        "control instead. Deferred to resource hardening, which is where "
        "such a limit would be introduced."
    ),
    "real_pixel_payload": (
        "`decoded_pixel_*` declare their dimensions in the PNG header "
        "rather than carrying real pixel data. The dimension check is the "
        "guard under test; a fixture with genuine payload would cost "
        "hundreds of megabytes to gain nothing."
    ),
}

EXPECTED_SHA256: dict[str, str] = {
    "ordinary":
        "32a91dcb9897101e5556fbf5d9248c0e89a4a28d1449707563359dd8099661c3",
    "comicinfo_only_change":
        "5cc738208f8e1cf0fc048ba12404847e78946bcf3a728c8527b57c0fa9eac1e4",
    "reordered_pages":
        "61e301f9d69b2a506b83fa7f558ecaaed3300e7ea51158dcc5374cd1e5e4071b",
    "one_page_difference":
        "2246d0dd84143f492c6a4f0af6a8f2cebd044bd58b43b24c9fb735a5d623c1f4",
    "one_page_fewer":
        "95e8ce9e4936840913d0c74859abf1089fe45edf4d416e7a385ea18ccf10b537",
    "duplicate_pages":
        "7bcb3c80bc7b97431862d249f907af980837e202eb9ca3ceace027a85d151448",
    "unicode_paths":
        "8726b50876cbdfbf2804f342b951cc20df6038c8b0b3293f5747bca40c53e338",
    "unsafe_members":
        "9fd556d7aad1a2c0f119fd06437e5d9c2e0691d605b46410bc8db1dc3f8e10c9",
    "no_images":
        "5da8b6b41db18dafa24dc9efc95b7e19477c386bacd5ad4284dac21e8ac1853d",
    "empty_archive":
        "8739c76e681f900923b900c9df0ef75cf421d39cabb54650c4b9ad19b6a76d85",
    "truncated_image":
        "0c8748c4e01bd6c18b37a7668613f1493a451dd6f0847019767cb5d45ebb183e",
    "unidentified_image":
        "ea157eba11a1b1f4059d0f9042b9030a30f186e9045afd2a888e2e39af1d75d9",
    "malformed_comicinfo":
        "e5d3a5d93f5440aa4dce14b05423905394619de4a821edfe721c0651a9926b46",
    "corrupt_zip":
        "ec385de8d2849a3b9a4b6d214bc32af957f08174e1c0c40a115451436ed7475d",
    "many_entries":
        "4c6ee6313f6e1edf4a7c975a0abe6a4f89cee2d738dd7d039ac23e777328d561",
    "decoded_pixel_warning":
        "3a6afb0cc39d806942d7979156e8426ac739adbfebec7cb7d776ef9551bc68f5",
    "decoded_pixel_bomb":
        "7ee2050a7ad11932d1e252032eb8bab707d5b674147765e72f2f9d878ab58019",
}
"""Frozen digest of every case, asserted by the corpus tests.

Building each case twice inside one process proves the builder is not
reading the clock; it proves nothing about drift *across* runs, machines
or dependency versions, because both builds share the same Pillow and the
same platform. These digests are the actual anchor.

They are expected to break, and the break is the signal. `Pillow>=10.0.0`
is unpinned, so an encoder change silently alters every page image and
therefore every fixture; `create_system` differs by operating system.
Either would leave every logical property passing while the corpus quietly
became a different corpus. When one of these fails, work out *why* the
bytes moved before updating the constant -- regenerating it to make the
suite green discards the only warning this file exists to give.
"""


CASES_BY_NAME = {case.name: case for case in CASES}


def build_case(name: str, directory: Path) -> Path:
    """Build one named case inside `directory`, returning its path."""
    case = CASES_BY_NAME[name]
    return case.build(directory / f"{name}.cbz")


def build_all(directory: Path) -> dict[str, Path]:
    """Build every case inside `directory`."""
    return {case.name: build_case(case.name, directory) for case in CASES}


# --- in-place replacement ------------------------------------------------


def replace_in_place(path: Path, replacement_case: str, tmp: Path) -> Path:
    """Overwrite `path`'s bytes with another case, keeping the same path.

    Archive identity in this project is keyed on path, so "the file changed
    underneath us" and "a new archive appeared" are indistinguishable by
    name alone. This is the operation that produces the first of those.
    """
    source = build_case(replacement_case, tmp)
    path.write_bytes(source.read_bytes())
    return path


def same_size_replacement(path: Path) -> bytes:
    """Different content, byte-identical length, as a valid archive.

    The size/mtime staleness guard is structurally blind to this case when
    the mtime lands in the same filesystem timestamp bucket. Returned as
    bytes so a caller can decide when to write them.
    """
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        payloads = {
            info.filename: archive.read(info.filename) for info in infos
        }

    target = next(
        name for name in payloads if name.casefold().endswith(".png")
    )
    payloads[target] = b"\xff" * len(payloads[target])

    buffer = BytesIO()

    with zipfile.ZipFile(buffer, "w") as out:
        for info in infos:
            out.writestr(info, payloads[info.filename],
                         compress_type=info.compress_type)

    data = buffer.getvalue()

    if len(data) != path.stat().st_size:
        raise AssertionError(
            "same-size replacement is not the same size: "
            f"{len(data)} vs {path.stat().st_size}"
        )

    return data
