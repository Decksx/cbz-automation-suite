from __future__ import annotations

import math
import sqlite3
import statistics
import time
import zipfile
import zlib
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from comic_automation.archive.candidate_selection import (
    Selection,
    revalidate_for_enqueue,
    select_candidates,
)
from comic_automation.archive.inspection import IMAGE_EXTENSIONS
from comic_automation.archive.page_hashing import _natural_key
from comic_automation.archive.repository import (
    ArchiveInspectionRepository,
)
from comic_automation.jobs import (
    CategorizedJobError,
    EnqueueOutcome,
    Job,
    JobQueue,
    PermanentJobError,
)


DHASH_ALGORITHM = "dhash"
DHASH_ALGORITHM_VERSION = "1"
PHASH_ALGORITHM = "phash"
PHASH_ALGORITHM_VERSION = "1"
DEFAULT_HASH_SIZE = 8

# Stable slugs naming each way an archive can fall outside
# `_eligible_archive_rows()`. One per clause of that predicate, so a reader
# can map a reason back to the condition that produced it. These are written
# into reports and compared in tests, so they are part of the contract rather
# than display strings.
#
# EXCLUSION_BLOCKING_JOB is the only one that carries a suffix: the blocking
# status matters, because "a worker is on it" and "it failed permanently" are
# opposite situations that the predicate treats identically.
EXCLUSION_NO_CONTENT_SIGNATURE = "no_content_signature"
EXCLUSION_SIGNATURE_PAGE_COUNT_ZERO = "signature_page_count_zero"
EXCLUSION_NO_CURRENT_LOCATION = "no_current_location"
EXCLUSION_MULTIPLE_CURRENT_LOCATIONS = "multiple_current_locations"
EXCLUSION_SIGNATURE_SIZE_MISMATCH = "signature_size_mismatch"
EXCLUSION_SIGNATURE_MTIME_MISMATCH = "signature_mtime_mismatch"
EXCLUSION_NO_OUTSTANDING_PAGES = "no_outstanding_pages"
EXCLUSION_BLOCKING_JOB = "blocking_job"

EXCLUSION_REASONS = (
    EXCLUSION_NO_CONTENT_SIGNATURE,
    EXCLUSION_SIGNATURE_PAGE_COUNT_ZERO,
    EXCLUSION_NO_CURRENT_LOCATION,
    EXCLUSION_MULTIPLE_CURRENT_LOCATIONS,
    EXCLUSION_SIGNATURE_SIZE_MISMATCH,
    EXCLUSION_SIGNATURE_MTIME_MISMATCH,
    EXCLUSION_NO_OUTSTANDING_PAGES,
    EXCLUSION_BLOCKING_JOB,
)
DEFAULT_HIGH_FREQUENCY_FACTOR = 4

# Explicit, version-pinned pixel-decode policy. See
# docs/archive_io_resource_audit.md, "Small, low-risk improvements":
# without this, the warning/rejection thresholds below depend entirely
# on whatever Image.MAX_IMAGE_PIXELS default ships with the installed
# Pillow version, and could silently shift on a Pillow upgrade.
#
# Pillow's own default at the time this was pinned is 89,478,485 pixels
# (~89.5 MP). That value is a *warning* threshold, not a hard limit --
# Pillow emits Image.DecompressionBombWarning above it but only raises
# Image.DecompressionBombError above twice that value (~179 MP). Both
# behaviors are Pillow's, not this codebase's; pinning the base value
# here only fixes where the line is drawn, not what happens at each
# side of it.
#
# The warning side is deliberately left non-terminal: an image between
# 89.5 MP and 179 MP only emits a Python warning and continues
# processing normally. The error side already terminates the page
# permanently -- Image.DecompressionBombError is caught by the
# exception tuple below and converted into a PermanentJobError
# (category="page_image_corrupt"), not a crash.
#
# This is a safety ceiling, not an operational constraint: a 600 DPI
# 8.5x11 comic page scan is roughly 34 MP, well under the warning
# threshold, so no legitimate archive page in this library is expected
# to approach either bound.
#
# This assignment is process-wide (Image.MAX_IMAGE_PIXELS is a module
# attribute on Pillow's Image module, not scoped to this file's calls),
# so importing this module changes decompression-bomb behavior for any
# other Pillow usage in the same process.
Image.MAX_IMAGE_PIXELS = 89_478_485

# Bounds the archive.read(entry) allocation below, before decoding.
# entry.file_size is the *declared* uncompressed size from the ZIP
# local file header; archive.read() allocates and reads that many bytes
# into memory in one call with no size check today (see
# docs/archive_io_resource_audit.md, "Confirmed risks in current code"
# -- unlike inspection.py's double-checked 1 MiB ComicInfo.xml cap or
# page_hashing.py's bounded chunk streaming). This cap closes that gap
# for the perceptual-hashing read path specifically.
#
# This is independent of the pixel policy above: a page can pass this
# raw-byte check and still be rejected after decoding if it exceeds the
# configured pixel budget, and vice versa (a small file can still
# decode to a large image for a sufficiently poor compression ratio,
# which the pixel policy alone catches). 200 MiB is far beyond any
# legitimate single comic page -- even large scans are typically a few
# MB -- so this exists to reject a maliciously or corruptly declared
# oversized entry before archive.read() allocates for it, not to
# constrain normal operation.
MAX_PAGE_UNCOMPRESSED_BYTES = 200 * 1024 * 1024


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
    phase_timings: PerceptualHashPhaseTimings | None = None

    @property
    def page_count(self) -> int:
        return len(self.pages)


@dataclass(frozen=True)
class PerceptualHashPhaseTimings:
    zip_open_and_inventory_seconds: float
    zip_entry_read_seconds: float
    image_open_and_decode_seconds: float
    dhash_seconds: float
    phash_seconds: float


@dataclass(frozen=True)
class PerceptualHashDatabaseTimings:
    database_lookup_seconds: float
    database_save_seconds: float


class PerceptualHashProfile:
    PHASE_NAMES = (
        "zip_open_and_inventory_seconds",
        "zip_entry_read_seconds",
        "image_open_and_decode_seconds",
        "dhash_seconds",
        "phash_seconds",
        "database_lookup_seconds",
        "database_save_seconds",
    )

    def __init__(self) -> None:
        self.profiled_archives = 0
        self.profiled_pages = 0
        self.profiled_bytes = 0
        self.phase_seconds = {
            name: 0.0 for name in self.PHASE_NAMES
        }

    def record(
        self,
        *,
        result: ArchivePerceptualHashes,
        database: PerceptualHashDatabaseTimings,
        location_lookup_seconds: float,
    ) -> None:
        phases = result.phase_timings

        if phases is None:
            raise ValueError(
                "Cannot record an unprofiled perceptual-hash result."
            )

        self.profiled_archives += 1
        self.profiled_pages += result.page_count
        self.profiled_bytes += sum(
            page.bytes_read for page in result.pages
        )

        for name in self.PHASE_NAMES[:5]:
            self.phase_seconds[name] += float(
                getattr(phases, name)
            )

        self.phase_seconds["database_lookup_seconds"] += (
            location_lookup_seconds
            + database.database_lookup_seconds
        )
        self.phase_seconds["database_save_seconds"] += (
            database.database_save_seconds
        )

    def summary(
        self,
        *,
        batch_elapsed_seconds: float,
        processed_jobs: int,
    ) -> dict:
        timed_phase_seconds = sum(self.phase_seconds.values())
        page_count = self.profiled_pages
        phase_percentages = {
            name: (
                (seconds / timed_phase_seconds) * 100
                if timed_phase_seconds > 0
                else 0.0
            )
            for name, seconds in self.phase_seconds.items()
        }

        return {
            "enabled": True,
            "profiled_archives": self.profiled_archives,
            "profiled_pages": page_count,
            "profiled_bytes": self.profiled_bytes,
            "unprofiled_jobs": max(
                processed_jobs - self.profiled_archives,
                0,
            ),
            "phase_seconds": {
                name: round(seconds, 6)
                for name, seconds in self.phase_seconds.items()
            },
            "phase_percentages": {
                name: round(percentage, 3)
                for name, percentage in phase_percentages.items()
            },
            "timed_phase_seconds": round(
                timed_phase_seconds,
                6,
            ),
            "batch_elapsed_seconds": round(
                batch_elapsed_seconds,
                6,
            ),
            "unattributed_seconds": round(
                max(
                    batch_elapsed_seconds - timed_phase_seconds,
                    0.0,
                ),
                6,
            ),
            "milliseconds_per_page": (
                round(
                    (timed_phase_seconds / page_count) * 1000,
                    6,
                )
                if page_count
                else None
            ),
            "pages_per_timed_second": (
                round(page_count / timed_phase_seconds, 6)
                if timed_phase_seconds > 0
                else None
            ),
        }


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
    profile: bool = False,
) -> ArchivePerceptualHashes:
    archive_path = Path(path)

    if archive_path.suffix.casefold() != ".cbz":
        raise ValueError(
            f"Unsupported archive format: "
            f"{archive_path.suffix or '<none>'}"
        )

    before = archive_path.stat()
    pages: list[PagePerceptualHash] = []
    zip_open_and_inventory_seconds = 0.0
    zip_entry_read_seconds = 0.0
    image_open_and_decode_seconds = 0.0
    dhash_seconds = 0.0
    phash_seconds = 0.0

    try:
        phase_started = time.perf_counter() if profile else 0.0
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
            if profile:
                zip_open_and_inventory_seconds += (
                    time.perf_counter() - phase_started
                )

            for page_index, entry in enumerate(entries):
                if entry.file_size > MAX_PAGE_UNCOMPRESSED_BYTES:
                    raise PermanentJobError(
                        "Declared page size "
                        f"{entry.file_size} bytes exceeds the "
                        f"{MAX_PAGE_UNCOMPRESSED_BYTES}-byte per-page "
                        f"cap for {entry.filename!r} in "
                        f"{archive_path}",
                        category="page_image_too_large",
                    )

                phase_started = (
                    time.perf_counter() if profile else 0.0
                )
                payload = archive.read(entry)
                if profile:
                    zip_entry_read_seconds += (
                        time.perf_counter() - phase_started
                    )

                try:
                    phase_started = (
                        time.perf_counter() if profile else 0.0
                    )
                    with Image.open(BytesIO(payload)) as image:
                        image.load()
                        width = int(image.width)
                        height = int(image.height)
                        image_format = image.format
                        if profile:
                            image_open_and_decode_seconds += (
                                time.perf_counter() - phase_started
                            )

                        phase_started = (
                            time.perf_counter()
                            if profile
                            else 0.0
                        )
                        dhash = difference_hash(
                            image,
                            hash_size=hash_size,
                        )
                        if profile:
                            dhash_seconds += (
                                time.perf_counter() - phase_started
                            )

                        phase_started = (
                            time.perf_counter()
                            if profile
                            else 0.0
                        )
                        phash = perceptual_hash(
                            image,
                            hash_size=hash_size,
                            high_frequency_factor=(
                                high_frequency_factor
                            ),
                        )
                        if profile:
                            phash_seconds += (
                                time.perf_counter() - phase_started
                            )

                        pages.append(
                            PagePerceptualHash(
                                page_index=page_index,
                                entry_name=entry.filename,
                                width=width,
                                height=height,
                                image_format=image_format,
                                dhash=dhash,
                                phash=phash,
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
        phase_timings=(
            PerceptualHashPhaseTimings(
                zip_open_and_inventory_seconds=(
                    zip_open_and_inventory_seconds
                ),
                zip_entry_read_seconds=zip_entry_read_seconds,
                image_open_and_decode_seconds=(
                    image_open_and_decode_seconds
                ),
                dhash_seconds=dhash_seconds,
                phash_seconds=phash_seconds,
            )
            if profile
            else None
        ),
    )


class ArchivePerceptualHashRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def save(
        self,
        *,
        archive_id: int,
        result: ArchivePerceptualHashes,
        profile: bool = False,
    ) -> PerceptualHashDatabaseTimings | None:
        # Perceptual hashing runs after exact page hashing, so
        # archive_pages rows already exist; load them to match each
        # freshly-computed perceptual hash back to its page_id and to
        # sanity-check the page inventory hasn't drifted underneath us.
        lookup_started = time.perf_counter() if profile else 0.0
        stored_pages = self.connection.execute(
            """
            SELECT id, page_index, entry_name
            FROM archive_pages
            WHERE archive_id = ?
            ORDER BY page_index
            """,
            (archive_id,),
        ).fetchall()
        database_lookup_seconds = (
            time.perf_counter() - lookup_started
            if profile
            else 0.0
        )

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

        save_started = time.perf_counter() if profile else 0.0

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

        if not profile:
            return None

        return PerceptualHashDatabaseTimings(
            database_lookup_seconds=database_lookup_seconds,
            database_save_seconds=(
                time.perf_counter() - save_started
            ),
        )

    def _eligible_archive_rows(
        self, *, limit: int | None = None
    ) -> list[sqlite3.Row]:
        """The literal eligibility predicate `enqueue_missing()` uses.

        Extracted to a single read-only SELECT so any caller that only
        needs to *count* or inspect eligible archives -- notably a
        strictly read-only postflight audit -- can reuse the exact
        production predicate without going anywhere near
        `enqueue_missing()`'s write path (`JobQueue.enqueue_if_absent`).
        Issuing only this SELECT is safe on a connection opened with
        SQLite's `mode=ro` URI flag plus `PRAGMA query_only = ON`.
        """
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
        #
        # The NOT EXISTS clause below is an *advisory* candidate filter,
        # not the duplicate guard: enqueue_if_absent() is the
        # authoritative, race-safe gate. Keeping the filter here still
        # matters for two reasons. It decides which rows a bounded
        # `limit` is spent on (an archive with active work is excluded
        # up front rather than consuming a slot and yielding
        # ALREADY_ACTIVE), and it carries this job type's terminal
        # policy: 'failed' IS excluded here, so a permanently-failed
        # job blocks automatic re-enqueue. enqueue_if_absent() only
        # knows about active statuses and would happily create a new
        # job after a failure, so this clause -- not the helper -- is
        # what preserves that policy.
        return self.connection.execute(
            f"""
            SELECT acs.archive_id
            FROM archive_content_signatures AS acs
            JOIN file_locations AS fl
              ON fl.archive_id = acs.archive_id
             AND fl.is_current = 1
            WHERE acs.page_count > 0
              -- Exactly one current location, not merely at least one.
              -- Without this an archive with two matching current rows joins
              -- twice and appears eligible, while select_candidates() would
              -- refuse it as ambiguous and _archive_exclusion_reasons() would
              -- report multiple_current_locations -- so the predicate and its
              -- explanation contradicted each other and the classifier had to
              -- abort. Refusing ambiguity here makes the two agree, and
              -- matches what the selection path has always done: zero current
              -- locations is unresolvable and more than one is ambiguous, and
              -- neither may be guessed at.
              AND (
                  SELECT COUNT(*)
                  FROM file_locations AS fl2
                  WHERE fl2.archive_id = acs.archive_id
                    AND fl2.is_current = 1
              ) = 1
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

    def select_enqueueable(self, *, limit: int | None = None) -> Selection:
        """Database-eligible archives, filtered by the shared selection path.

        Read-only, and the single source for "what would `enqueue_missing()`
        actually enqueue". A preflight calls this and reports
        `selection.rejected`; `enqueue_missing()` calls it and enqueues
        `selection.accepted`. Before this existed the two answered the
        question differently, and the difference was invisible until a
        worker opened a file that was not there.

        `limit` is applied to the *database* predicate, so a bounded run
        still costs a bounded amount of work. That means the accepted count
        can be smaller than `limit` when some of those candidates are
        rejected here -- which is honest: the alternative is scanning an
        unbounded number of archives to fill a quota.
        """
        eligible = [
            int(row["archive_id"])
            for row in self._eligible_archive_rows(limit=limit)
        ]
        return select_candidates(self.connection, eligible)

    def enqueue_missing(self, *, limit: int | None = None) -> int:
        selection = self.select_enqueueable(limit=limit)
        queue = JobQueue(self.connection)
        created = 0

        for candidate in selection.accepted:
            # The database can move between selection and enqueue -- an
            # archive retired by another operator, a location row rewritten
            # by repair -- and those are cheap to re-read. The filesystem is
            # deliberately not re-checked here; see revalidate_for_enqueue().
            if revalidate_for_enqueue(self.connection, candidate.archive_id):
                continue

            outcome = queue.enqueue_if_absent(
                "hash_archive_pages_perceptual",
                archive_id=candidate.archive_id,
                priority=250,
            )

            if outcome is EnqueueOutcome.CREATED:
                created += 1

        # Count rows actually inserted, not candidates considered (see
        # ArchiveHashRepository.enqueue_missing for the reasoning).
        return created

    def count_eligible(self) -> int:
        """Read-only count of archives satisfying the *database* rules alone.

        Deliberately not the count of what `enqueue_missing()` would
        enqueue: that is `len(select_enqueueable().accepted)`, and the two
        differ by every archive that is retired, has no single current
        location, or points at a path that is not an accessible regular
        file. On 2026-08-18 the difference was 226 of 12,555.

        Keeping the database-only count available matters for diagnosis --
        a gap between the two numbers is the measurement of how far the
        recorded state has drifted from the disk.

        Issues only a SELECT, so it is safe on a connection opened with
        SQLite's `mode=ro` URI flag plus `PRAGMA query_only = ON`.
        """
        return len(self._eligible_archive_rows(limit=None))

    def _archive_exclusion_reasons(self) -> dict[int, tuple[str, ...]]:
        """Why each archive is *not* in the eligible set, reason by reason.

        `_eligible_archive_rows()` answers "which archives may be enqueued"
        with a single predicate whose failures are indistinguishable from one
        another: an archive is absent from the result and the query does not
        say why. That was survivable while the only question was how much work
        remained. It stopped being survivable when 162 archives turned out to
        be missing a current location -- a fact the predicate knew and threw
        away, leaving them pooled with every other kind of ineligibility and
        visible in no report.

        This returns the same information from the other side. Membership is
        deliberately NOT recomputed here: the caller takes the eligible set
        from `_eligible_archive_rows()` and uses this only to *explain* the
        complement.

        That keeps the two from drifting on *membership* by construction, but
        it is not a proof that they agree -- and they did not, once. The
        predicate joined every current location without requiring exactly one,
        so an archive with two matching current rows was eligible while this
        function reported `multiple_current_locations`; the classifier could
        only abort. Both sides now refuse ambiguity, and a test asserts the
        partition on a deliberately varied population rather than assuming it.

        An archive may carry several reasons at once. They are all returned:
        an archive with no signature *and* no current location has two
        independent problems, and reporting one would send a reader looking
        for a single fix that does not exist.

        Issues only SELECTs, so it is safe on a connection opened with
        SQLite's `mode=ro` URI flag plus `PRAGMA query_only = ON`.
        """
        rows = self.connection.execute(
            """
            SELECT
                af.id AS archive_id,
                acs.id IS NOT NULL AS has_signature,
                COALESCE(acs.page_count, 0) AS signature_page_count,
                acs.source_file_size AS signature_file_size,
                acs.source_modified_time_ns AS signature_modified_time_ns,
                (
                    SELECT COUNT(*) FROM file_locations AS fl
                    WHERE fl.archive_id = af.id AND fl.is_current = 1
                ) AS current_location_count,
                (
                    SELECT fl.file_size FROM file_locations AS fl
                    WHERE fl.archive_id = af.id AND fl.is_current = 1
                    LIMIT 1
                ) AS location_file_size,
                (
                    SELECT fl.modified_time_ns FROM file_locations AS fl
                    WHERE fl.archive_id = af.id AND fl.is_current = 1
                    LIMIT 1
                ) AS location_modified_time_ns,
                (
                    -- Every distinct blocking status, not the first by id.
                    -- An archive can hold a failed job and a running one at
                    -- once, and LIMIT 1 hid whichever lost the race --
                    -- breaking the promise that an archive keeps every reason
                    -- that applies, and hiding the more actionable of the two
                    -- roughly half the time.
                    SELECT GROUP_CONCAT(status, ',') FROM (
                        SELECT DISTINCT j.status AS status
                        FROM jobs AS j
                        WHERE j.archive_id = af.id
                          AND j.job_type = 'hash_archive_pages_perceptual'
                          AND j.status IN
                              ('pending', 'claimed', 'running', 'failed')
                        ORDER BY j.status
                    )
                ) AS blocking_job_statuses
            FROM archive_files AS af
            LEFT JOIN archive_content_signatures AS acs
              ON acs.archive_id = af.id
            ORDER BY af.id
            """
        ).fetchall()

        outstanding = self.outstanding_page_counts()
        reasons: dict[int, tuple[str, ...]] = {}

        for row in rows:
            archive_id = int(row["archive_id"])
            found: list[str] = []

            if not row["has_signature"]:
                found.append(EXCLUSION_NO_CONTENT_SIGNATURE)
            elif int(row["signature_page_count"] or 0) <= 0:
                found.append(EXCLUSION_SIGNATURE_PAGE_COUNT_ZERO)

            locations = int(row["current_location_count"] or 0)

            if locations == 0:
                found.append(EXCLUSION_NO_CURRENT_LOCATION)
            elif locations > 1:
                found.append(EXCLUSION_MULTIPLE_CURRENT_LOCATIONS)

            # Only meaningful when both sides exist; a missing signature or
            # location is already reported above and comparing NULLs would
            # invent a second, derivative reason for the same fact.
            if row["has_signature"] and locations == 1:
                if (
                    row["signature_file_size"] != row["location_file_size"]
                ):
                    found.append(EXCLUSION_SIGNATURE_SIZE_MISMATCH)

                if (
                    row["signature_modified_time_ns"]
                    != row["location_modified_time_ns"]
                ):
                    found.append(EXCLUSION_SIGNATURE_MTIME_MISMATCH)

            if outstanding.get(archive_id, 0) == 0:
                found.append(EXCLUSION_NO_OUTSTANDING_PAGES)

            if row["blocking_job_statuses"]:
                for status in str(row["blocking_job_statuses"]).split(","):
                    found.append(
                        "%s:%s" % (EXCLUSION_BLOCKING_JOB, status)
                    )

            if found:
                reasons[archive_id] = tuple(found)

        return reasons

    def outstanding_page_counts(self) -> dict[int, int]:
        """Pages per archive still missing a Version 1 hash or dimensions.

        The same page condition `_eligible_archive_rows()` tests with EXISTS,
        counted instead of merely detected, because a report has to say how
        many pages a gap is worth and not only that one exists.

        Written as one grouped scan rather than a per-archive subquery: the
        correlated form takes minutes on a library this size, which is what
        made an earlier version of this audit unusable.
        """
        rows = self.connection.execute(
            """
            SELECT
                ap.archive_id AS archive_id,
                SUM(
                    CASE
                        WHEN dh.id IS NULL
                          OR ph.id IS NULL
                          OR ap.width IS NULL
                          OR ap.height IS NULL
                        THEN 1
                        ELSE 0
                    END
                ) AS outstanding
            FROM archive_pages AS ap
            LEFT JOIN page_hashes AS dh
              ON dh.page_id = ap.id
             AND dh.algorithm = ?
             AND dh.algorithm_version = ?
            LEFT JOIN page_hashes AS ph
              ON ph.page_id = ap.id
             AND ph.algorithm = ?
             AND ph.algorithm_version = ?
            GROUP BY ap.archive_id
            """,
            (
                DHASH_ALGORITHM,
                DHASH_ALGORITHM_VERSION,
                PHASH_ALGORITHM,
                PHASH_ALGORITHM_VERSION,
            ),
        ).fetchall()

        return {
            int(row["archive_id"]): int(row["outstanding"] or 0)
            for row in rows
        }


class HashArchivePagesPerceptualHandler:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        hash_size: int = DEFAULT_HASH_SIZE,
        high_frequency_factor: int = DEFAULT_HIGH_FREQUENCY_FACTOR,
        profile: bool = False,
    ) -> None:
        self.locations = ArchiveInspectionRepository(connection)
        self.hashes = ArchivePerceptualHashRepository(connection)
        self.hash_size = hash_size
        self.high_frequency_factor = high_frequency_factor
        self.profile = PerceptualHashProfile() if profile else None

    def __call__(self, job: Job) -> None:
        if job.archive_id is None:
            raise ValueError(f"Job {job.id} has no archive_id.")

        location_lookup_started = (
            time.perf_counter()
            if self.profile is not None
            else 0.0
        )
        location = self.locations.current_location(job.archive_id)
        location_lookup_seconds = (
            time.perf_counter() - location_lookup_started
            if self.profile is not None
            else 0.0
        )
        path = Path(str(location["path"]))

        try:
            result = calculate_perceptual_hashes(
                path,
                hash_size=self.hash_size,
                high_frequency_factor=self.high_frequency_factor,
                profile=self.profile is not None,
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

        database_timings = self.hashes.save(
            archive_id=job.archive_id,
            result=result,
            profile=self.profile is not None,
        )

        if self.profile is not None:
            if database_timings is None:
                raise RuntimeError(
                    "Profiled repository save returned no timings."
                )
            self.profile.record(
                result=result,
                database=database_timings,
                location_lookup_seconds=location_lookup_seconds,
            )
