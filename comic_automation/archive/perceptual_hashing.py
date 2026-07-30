from __future__ import annotations

import math
import sqlite3
import statistics
import zipfile
import zlib
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from comic_automation.archive.inspection import IMAGE_EXTENSIONS
from comic_automation.archive.page_hashing import _natural_key
from comic_automation.archive.repository import (
    ArchiveInspectionRepository,
)
from comic_automation.jobs import (
    CategorizedJobError,
    Job,
    JobQueue,
    PermanentJobError,
)


DHASH_ALGORITHM = "dhash"
DHASH_ALGORITHM_VERSION = "1"
PHASH_ALGORITHM = "phash"
PHASH_ALGORITHM_VERSION = "1"
DEFAULT_HASH_SIZE = 8
DEFAULT_HIGH_FREQUENCY_FACTOR = 4


@dataclass(frozen=True)
class PagePerceptualHash:
    page_index: int
    entry_name: str
    width: int
    height: int
    image_format: str | None
    dhash: str
    phash: str
    bytes_read: int


@dataclass(frozen=True)
class ArchivePerceptualHashes:
    pages: tuple[PagePerceptualHash, ...]
    source_file_size: int
    source_modified_time_ns: int

    @property
    def page_count(self) -> int:
        return len(self.pages)


def _bits_to_hex(bits: list[bool]) -> str:
    value = 0

    for bit in bits:
        value = (value << 1) | int(bit)

    width = (len(bits) + 3) // 4
    return f"{value:0{width}x}"


def difference_hash(
    image: Image.Image,
    *,
    hash_size: int = DEFAULT_HASH_SIZE,
) -> str:
    if hash_size < 2:
        raise ValueError("hash_size must be at least 2.")

    grayscale = image.convert("L").resize(
        (hash_size + 1, hash_size),
        Image.Resampling.LANCZOS,
    )
    pixels = grayscale.tobytes()
    bits = [
        pixels[row * (hash_size + 1) + column]
        > pixels[row * (hash_size + 1) + column + 1]
        for row in range(hash_size)
        for column in range(hash_size)
    ]
    return _bits_to_hex(bits)


@lru_cache(maxsize=32)
def _perceptual_hash_constants(
    hash_size: int,
    high_frequency_factor: int,
) -> tuple[tuple[tuple[float, ...], ...], tuple[float, ...]]:
    sample_size = hash_size * high_frequency_factor
    scale = math.pi / (2 * sample_size)
    cosine = tuple(
        tuple(
            math.cos((2 * position + 1) * frequency * scale)
            for position in range(sample_size)
        )
        for frequency in range(hash_size)
    )
    normalization = tuple(
        (
            math.sqrt(1 / sample_size)
            if frequency == 0
            else math.sqrt(2 / sample_size)
        )
        for frequency in range(hash_size)
    )
    return cosine, normalization


def perceptual_hash(
    image: Image.Image,
    *,
    hash_size: int = DEFAULT_HASH_SIZE,
    high_frequency_factor: int = DEFAULT_HIGH_FREQUENCY_FACTOR,
) -> str:
    if hash_size < 2:
        raise ValueError("hash_size must be at least 2.")
    if high_frequency_factor < 1:
        raise ValueError(
            "high_frequency_factor must be at least 1."
        )

    sample_size = hash_size * high_frequency_factor
    grayscale = image.convert("L").resize(
        (sample_size, sample_size),
        Image.Resampling.LANCZOS,
    )
    pixels = grayscale.tobytes()
    cosine, normalization = _perceptual_hash_constants(
        hash_size,
        high_frequency_factor,
    )
    coefficients: list[float] = []

    for vertical_frequency in range(hash_size):
        for horizontal_frequency in range(hash_size):
            coefficient = 0.0

            for row in range(sample_size):
                row_factor = cosine[vertical_frequency][row]
                offset = row * sample_size

                for column in range(sample_size):
                    coefficient += (
                        pixels[offset + column]
                        * row_factor
                        * cosine[horizontal_frequency][column]
                    )

            coefficients.append(
                coefficient
                * normalization[vertical_frequency]
                * normalization[horizontal_frequency]
            )

    threshold = statistics.median(coefficients[1:])
    return _bits_to_hex(
        [coefficient > threshold for coefficient in coefficients]
    )


def calculate_perceptual_hashes(
    path: str | Path,
    *,
    hash_size: int = DEFAULT_HASH_SIZE,
    high_frequency_factor: int = DEFAULT_HIGH_FREQUENCY_FACTOR,
) -> ArchivePerceptualHashes:
    archive_path = Path(path)

    if archive_path.suffix.casefold() != ".cbz":
        raise ValueError(
            f"Unsupported archive format: "
            f"{archive_path.suffix or '<none>'}"
        )

    before = archive_path.stat()
    pages: list[PagePerceptualHash] = []

    try:
        with zipfile.ZipFile(archive_path, mode="r") as archive:
            entries = sorted(
                (
                    entry
                    for entry in archive.infolist()
                    if not entry.is_dir()
                    and Path(entry.filename).suffix.casefold()
                    in IMAGE_EXTENSIONS
                ),
                key=lambda entry: _natural_key(entry.filename),
            )

            for page_index, entry in enumerate(entries):
                payload = archive.read(entry)

                try:
                    with Image.open(BytesIO(payload)) as image:
                        image.load()
                        pages.append(
                            PagePerceptualHash(
                                page_index=page_index,
                                entry_name=entry.filename,
                                width=int(image.width),
                                height=int(image.height),
                                image_format=image.format,
                                dhash=difference_hash(
                                    image,
                                    hash_size=hash_size,
                                ),
                                phash=perceptual_hash(
                                    image,
                                    hash_size=hash_size,
                                    high_frequency_factor=(
                                        high_frequency_factor
                                    ),
                                ),
                                bytes_read=len(payload),
                            )
                        )
                except (
                    Image.DecompressionBombError,
                    UnidentifiedImageError,
                    OSError,
                    SyntaxError,
                    ValueError,
                ) as exc:
                    raise PermanentJobError(
                        "Invalid or unsupported image page "
                        f"{entry.filename!r} in {archive_path}: {exc}",
                        category="page_image_corrupt",
                    ) from exc
    except PermanentJobError:
        raise
    except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
        raise PermanentJobError(
            f"Invalid or corrupt CBZ archive: {archive_path}",
            category="archive_corrupt",
        ) from exc
    except RuntimeError as exc:
        raise PermanentJobError(
            f"Unreadable CBZ entry in {archive_path}: {exc}",
            category="archive_unreadable",
        ) from exc

    after = archive_path.stat()

    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise OSError(
            f"Archive changed while perceptually hashing pages: "
            f"{archive_path}"
        )

    return ArchivePerceptualHashes(
        pages=tuple(pages),
        source_file_size=int(after.st_size),
        source_modified_time_ns=int(after.st_mtime_ns),
    )


class ArchivePerceptualHashRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def save(
        self,
        *,
        archive_id: int,
        result: ArchivePerceptualHashes,
    ) -> None:
        # Perceptual hashing runs after exact page hashing, so
        # archive_pages rows already exist; load them to match each
        # freshly-computed perceptual hash back to its page_id and to
        # sanity-check the page inventory hasn't drifted underneath us.
        stored_pages = self.connection.execute(
            """
            SELECT id, page_index, entry_name
            FROM archive_pages
            WHERE archive_id = ?
            ORDER BY page_index
            """,
            (archive_id,),
        ).fetchall()

        expected = [
            (int(row["page_index"]), str(row["entry_name"]))
            for row in stored_pages
        ]
        actual = [
            (page.page_index, page.entry_name)
            for page in result.pages
        ]

        if expected != actual:
            raise OSError(
                "Stored page inventory does not match the current "
                f"archive for archive_id={archive_id}."
            )

        try:
            self.connection.execute("BEGIN IMMEDIATE")

            for row, page in zip(stored_pages, result.pages):
                page_id = int(row["id"])

                # Backfill the width/height/image_format columns added
                # by migration 008, now that they're known.
                self.connection.execute(
                    """
                    UPDATE archive_pages
                    SET width = ?,
                        height = ?,
                        image_format = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        page.width,
                        page.height,
                        page.image_format,
                        page_id,
                    ),
                )

                # Store both the dhash and phash as separate
                # page_hashes rows (same table used for the exact
                # sha256 content hash), keyed by algorithm so all three
                # hash kinds can coexist per page; upsert so
                # recomputing perceptual hashes overwrites in place.
                for algorithm, version, digest in (
                    (
                        DHASH_ALGORITHM,
                        DHASH_ALGORITHM_VERSION,
                        page.dhash,
                    ),
                    (
                        PHASH_ALGORITHM,
                        PHASH_ALGORITHM_VERSION,
                        page.phash,
                    ),
                ):
                    self.connection.execute(
                        """
                        INSERT INTO page_hashes (
                            page_id,
                            algorithm,
                            algorithm_version,
                            digest,
                            bytes_read
                        )
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(
                            page_id,
                            algorithm,
                            algorithm_version
                        ) DO UPDATE SET
                            digest = excluded.digest,
                            bytes_read = excluded.bytes_read,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (
                            page_id,
                            algorithm,
                            version,
                            digest,
                            page.bytes_read,
                        ),
                    )

            self.connection.execute("COMMIT")
        except Exception:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def enqueue_missing(self, *, limit: int | None = None) -> int:
        limit_clause = ""
        parameters: list[int] = []

        if limit is not None:
            if limit < 1:
                raise ValueError("limit must be at least 1.")
            limit_clause = " LIMIT ?"
            parameters.append(limit)

        # Candidates for perceptual hashing: archives with a current
        # content signature (i.e. exact page hashing already ran) whose
        # recorded source_file_size/source_modified_time_ns still match
        # the live file_locations row, that have at least one page
        # still missing a dhash, phash, or decoded width/height, and
        # that don't already have a perceptual-hash job in flight
        # (including 'failed', mirroring enqueue_missing() in
        # page_hashing.py).
        rows = self.connection.execute(
            f"""
            SELECT acs.archive_id
            FROM archive_content_signatures AS acs
            JOIN file_locations AS fl
              ON fl.archive_id = acs.archive_id
             AND fl.is_current = 1
            WHERE acs.page_count > 0
              AND acs.source_file_size = fl.file_size
              AND acs.source_modified_time_ns = fl.modified_time_ns
              AND EXISTS (
                  SELECT 1
                  FROM archive_pages AS ap
                  LEFT JOIN page_hashes AS dh
                    ON dh.page_id = ap.id
                   AND dh.algorithm = ?
                   AND dh.algorithm_version = ?
                  LEFT JOIN page_hashes AS ph
                    ON ph.page_id = ap.id
                   AND ph.algorithm = ?
                   AND ph.algorithm_version = ?
                  WHERE ap.archive_id = acs.archive_id
                    AND (
                        dh.id IS NULL
                        OR ph.id IS NULL
                        OR ap.width IS NULL
                        OR ap.height IS NULL
                    )
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM jobs AS j
                  WHERE j.archive_id = acs.archive_id
                    AND j.job_type = 'hash_archive_pages_perceptual'
                    AND j.status IN (
                        'pending',
                        'claimed',
                        'running',
                        'failed'
                    )
              )
            ORDER BY acs.archive_id
            {limit_clause}
            """,
            [
                DHASH_ALGORITHM,
                DHASH_ALGORITHM_VERSION,
                PHASH_ALGORITHM,
                PHASH_ALGORITHM_VERSION,
                *parameters,
            ],
        ).fetchall()
        queue = JobQueue(self.connection)

        for row in rows:
            queue.enqueue(
                "hash_archive_pages_perceptual",
                archive_id=int(row["archive_id"]),
                priority=250,
            )

        return len(rows)


class HashArchivePagesPerceptualHandler:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        hash_size: int = DEFAULT_HASH_SIZE,
        high_frequency_factor: int = DEFAULT_HIGH_FREQUENCY_FACTOR,
    ) -> None:
        self.locations = ArchiveInspectionRepository(connection)
        self.hashes = ArchivePerceptualHashRepository(connection)
        self.hash_size = hash_size
        self.high_frequency_factor = high_frequency_factor

    def __call__(self, job: Job) -> None:
        if job.archive_id is None:
            raise ValueError(f"Job {job.id} has no archive_id.")

        location = self.locations.current_location(job.archive_id)
        path = Path(str(location["path"]))

        try:
            result = calculate_perceptual_hashes(
                path,
                hash_size=self.hash_size,
                high_frequency_factor=self.high_frequency_factor,
            )
        except PermanentJobError:
            raise
        except FileNotFoundError as exc:
            raise CategorizedJobError(
                str(exc),
                category="filesystem_not_found",
            ) from exc
        except PermissionError as exc:
            raise CategorizedJobError(
                str(exc),
                category="filesystem_permission",
            ) from exc
        except OSError as exc:
            raise CategorizedJobError(
                str(exc),
                category="filesystem_io",
            ) from exc

        self.hashes.save(archive_id=job.archive_id, result=result)
