from __future__ import annotations

import hashlib
import re
import sqlite3
import zipfile
import zlib
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from comic_automation.archive.archive_limits import (
    UnsafeArchiveError,
    validate_archive_members,
)
from comic_automation.archive.inspection import IMAGE_EXTENSIONS
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


PAGE_HASH_ALGORITHM = "sha256"
PAGE_HASH_ALGORITHM_VERSION = "1"
CONTENT_SIGNATURE_ALGORITHM = "ordered-page-sha256"
# The version names the *ordering contract* the signature was computed
# under, because the digest is over pages in order and two orderings of the
# same members give different digests -- or, worse, the same digest for
# different archives. "1" ordered by the natural key alone and left ties to
# physical member order; "2" breaks ties on the raw name (`page_order_key`).
# A "1" row and a "2" row are not comparable: archive A packed a.png=X,
# A.png=Y stores X,Y under "1", archive B packed a.png=Y, A.png=X stores X,Y
# under "2", and the digests collide although the archives differ. Readers
# that group or align on ordered content accept only the current version
# (`comparable_ordered_signature_sql`); older rows are legacy diagnostics
# until re-certified or re-inventoried (docs/engineering_decisions.md).
# The per-page sha256 (PAGE_HASH_ALGORITHM_VERSION) is unchanged.
CONTENT_SIGNATURE_VERSION = "2"
DEFAULT_CHUNK_SIZE = 1024 * 1024
_NATURAL_PART_RE = re.compile(r"(\d+)")


@dataclass(frozen=True)
class PageContentHash:
    page_index: int
    entry_name: str
    entry_size: int
    compressed_size: int
    crc32: int
    digest: str
    bytes_read: int


@dataclass(frozen=True)
class ArchivePageHashes:
    """Exact page inventory and ordered content signature of one archive.

    ``duplicate_entry_names`` lists the raw image member names that occur
    more than once (see `order_page_entries`). When it is non-empty the page
    order is *unresolved*: every page is still inventoried, but the order
    among same-named members, and so ``content_digest``, depends on physical
    member order and proves nothing when compared with another archive.
    Comparison consumers must check ``order_resolved`` before trusting
    ``content_digest``. It defaults to empty, so a result built by hand for
    an archive with unique names means what it always meant.
    """

    pages: tuple[PageContentHash, ...]
    content_digest: str
    image_bytes: int
    source_file_size: int
    source_modified_time_ns: int
    duplicate_entry_names: tuple[str, ...] = ()

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def order_resolved(self) -> bool:
        """False when duplicate image member names leave the order open."""
        return not self.duplicate_entry_names


def _natural_key(value: str) -> tuple[tuple[int, object], ...]:
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part.casefold())
        for part in _NATURAL_PART_RE.split(value.replace("\\", "/"))
        if part
    )


def page_order_key(
    name: str,
) -> tuple[tuple[tuple[int, object], ...], str]:
    """Sort key for one image member: the natural key, then the raw name.

    The natural key alone ties on names that differ only in case or in zero
    padding (``a.png``/``A.png``, ``01.png``/``1.png``, ``p1.png``/
    ``p01.png``). ``sorted`` is stable, so before this tie-break the
    physically earlier member won, and re-packing the same pages in another
    order changed the content signature. The raw name breaks every tie
    between *distinct* names, so for an archive whose image names are unique
    the order is a function of the names alone (design
    ``docs/canonical_naming_and_version_supersession.md`` §1.2).

    The raw comparison is by code point: locale-free and fixed, which is all
    a tie-break needs. Which of two tied names comes first is arbitrary but
    never varies (``"A.png" < "a.png"``, ``"01.png" < "1.png"``).

    Changing this key changes the content signature of every archive that
    holds tied names. Signatures stored under the old key are replaced only
    when that archive is re-saved; see `ArchivePageHashRepository.save`.
    """
    return (_natural_key(name), name)


def is_page_member(entry: zipfile.ZipInfo) -> bool:
    """True for a non-directory member with an image extension."""
    return (
        not entry.is_dir()
        and Path(entry.filename).suffix.casefold() in IMAGE_EXTENSIONS
    )


@dataclass(frozen=True)
class PageOrder:
    """An archive's image members in page order, and whether that is an answer.

    ``entries`` are the image members sorted by `page_order_key`.
    ``duplicate_names`` are the raw names held by more than one image
    member, sorted by code point. Identical names cannot be ordered by name,
    so when it is non-empty the order is **unresolved**: same-named members
    keep their physical order only because an index has to be assigned, and
    that placement carries no authority. ``resolved`` is the flag to test.
    """

    entries: tuple[zipfile.ZipInfo, ...]
    duplicate_names: tuple[str, ...]

    @property
    def resolved(self) -> bool:
        return not self.duplicate_names


def order_page_entries(members: Iterable[zipfile.ZipInfo]) -> PageOrder:
    """The one definition of page order, shared by every reader of it.

    Exact page hashing, perceptual hashing and the source-drift inventory
    all order pages through this function. They must agree exactly: the
    perceptual save refuses unless its pages match the stored exact
    inventory index for index, so a reader with its own sort would report
    drift on every archive where the two sorts differ.

    Only image members are ordered, so only their names are checked for
    duplicates; a duplicated ``notes.txt`` does not make the *page order*
    unresolved. Names are compared raw, so ``a.png`` and ``A.png`` are
    distinct here even though they would collide when extracted to a
    case-insensitive filesystem; that is an extraction question, not an
    ordering one.
    """
    entries = tuple(
        sorted(
            (entry for entry in members if is_page_member(entry)),
            key=lambda entry: page_order_key(entry.filename),
        )
    )
    counts = Counter(entry.filename for entry in entries)

    return PageOrder(
        entries=entries,
        duplicate_names=tuple(
            sorted(name for name, count in counts.items() if count > 1)
        ),
    )


def hash_ordered_pages(
    archive: zipfile.ZipFile,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> tuple[tuple[PageContentHash, ...], PageOrder]:
    """Admit ``archive`` and hash its image members in page order.

    The one exact page-hashing pass, shared by `calculate_page_hashes` (which
    opens a path) and the supersession evaluator (which opens the held bytes
    it has already bound to a full-file sha256), so the two can never hash or
    order pages differently. ``archive`` may be backed by a path or by an
    in-memory buffer; nothing here looks at the file system.

    Raises `UnsafeArchiveError` from the admission check and whatever
    ``zipfile`` raises for a corrupt member; the caller maps those.
    """
    if chunk_size < 1:
        raise ValueError("chunk_size must be at least 1.")

    members = archive.infolist()
    # Archive-wide limits, member names and encryption, checked on
    # the central directory before the first member is opened. An
    # encrypted member would otherwise surface from archive.open()
    # as a bare RuntimeError and be filed as archive_unreadable.
    validate_archive_members(members)
    order = order_page_entries(members)
    pages: list[PageContentHash] = []

    # Each digest is computed from, and recorded with, the entry
    # whose name it is stored under: the index positions a page but
    # never selects which bytes its digest describes.
    for page_index, entry in enumerate(order.entries):
        digest = hashlib.sha256()
        bytes_read = 0

        with archive.open(entry, mode="r") as stream:
            while chunk := stream.read(chunk_size):
                digest.update(chunk)
                bytes_read += len(chunk)

        pages.append(
            PageContentHash(
                page_index=page_index,
                entry_name=entry.filename,
                entry_size=int(entry.file_size),
                compressed_size=int(entry.compress_size),
                crc32=int(entry.CRC),
                digest=digest.hexdigest(),
                bytes_read=bytes_read,
            )
        )

    return tuple(pages), order


def ordered_content_digest(pages: Iterable[PageContentHash]) -> str:
    """The ordered content signature over ``pages`` (already in page order).

    The 8-byte big-endian page count, then each page sha256's raw bytes.
    """
    page_list = list(pages)
    signature = hashlib.sha256()
    signature.update(len(page_list).to_bytes(8, "big"))

    for page in page_list:
        signature.update(bytes.fromhex(page.digest))

    return signature.hexdigest()


def calculate_page_hashes(
    path: str | Path,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> ArchivePageHashes:
    if chunk_size < 1:
        raise ValueError("chunk_size must be at least 1.")

    archive_path = Path(path)

    if archive_path.suffix.casefold() != ".cbz":
        raise ValueError(
            f"Unsupported archive format: "
            f"{archive_path.suffix or '<none>'}"
        )

    before = archive_path.stat()

    try:
        with zipfile.ZipFile(archive_path, mode="r") as archive:
            pages, order = hash_ordered_pages(archive, chunk_size=chunk_size)
    except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
        raise PermanentJobError(
            f"Invalid or corrupt CBZ archive: {archive_path}",
            category="archive_corrupt",
        ) from exc
    except UnsafeArchiveError as exc:
        # Permanent: the same central directory fails the same way on
        # every retry. The reason code is the category.
        raise PermanentJobError(
            f"Refused CBZ archive {archive_path}: {exc}",
            category=exc.reason,
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
            f"Archive changed while hashing pages: {archive_path}"
        )

    return ArchivePageHashes(
        pages=pages,
        content_digest=ordered_content_digest(pages),
        image_bytes=sum(page.bytes_read for page in pages),
        source_file_size=int(after.st_size),
        source_modified_time_ns=int(after.st_mtime_ns),
        duplicate_entry_names=order.duplicate_names,
    )


# Correlated predicate: true when the archive whose id is the formatted
# column expression has two stored page rows with the same raw entry_name.
# That is the stored form of an unresolved page order (`PageOrder`), and it
# needs no schema change because `archive_pages` already records every
# image member's raw name. SQLite compares TEXT with BINARY collation unless
# told otherwise, which is the raw-name equality `order_page_entries` uses.
_STORED_DUPLICATE_ENTRY_NAMES_SQL = """
    SELECT 1
    FROM archive_pages AS dup_ap
    WHERE dup_ap.archive_id = {archive_id}
    GROUP BY dup_ap.entry_name
    HAVING COUNT(*) > 1
"""


def comparable_ordered_signature_sql(alias: str) -> str:
    """SQL condition: signature row ``alias`` is canonical ordered evidence.

    True only when the row was computed by this algorithm under the current
    ordering contract (``CONTENT_SIGNATURE_VERSION``) *and* its archive's
    stored page order is resolved (no duplicated raw ``entry_name``). Every
    reader that groups archives by ``archive_content_signatures.digest`` or
    aligns their pages by ``page_index`` must apply it, so a legacy-order
    row, a row of unknown provenance, or an unresolved order can never be
    presented as evidence. Those rows are diagnostics; see
    `signature_evidence_counts`.

    The values are this module's own constants, inlined as literals, and
    ``alias`` must be a plain identifier, so nothing caller-supplied reaches
    the SQL text.
    """
    if not alias.isidentifier():
        raise ValueError(f"Not an SQL alias: {alias!r}")

    duplicates = _STORED_DUPLICATE_ENTRY_NAMES_SQL.format(
        archive_id=f"{alias}.archive_id"
    )
    return (
        f"{alias}.algorithm = '{CONTENT_SIGNATURE_ALGORITHM}' "
        f"AND {alias}.algorithm_version = '{CONTENT_SIGNATURE_VERSION}' "
        f"AND NOT EXISTS ({duplicates})"
    )


def signature_evidence_counts(connection: sqlite3.Connection) -> dict:
    """How many stored signatures are, and are not, comparable evidence.

    Read-only diagnostics for the readers that exclude non-comparable rows,
    so an exclusion is counted rather than silent:

    * ``comparable`` -- current algorithm and version, resolved order;
    * ``legacy_ordering_version`` -- this algorithm under an older ordering
      contract, awaiting re-certification or re-inventory;
    * ``unknown_algorithm`` -- any other algorithm name;
    * ``unresolved_page_order`` -- current version, but duplicated raw
      image names, so the order is not an answer.

    Counted over every signature row, whatever its location state; the
    readers apply their own location and staleness rules on top.
    """
    duplicates = _STORED_DUPLICATE_ENTRY_NAMES_SQL.format(
        archive_id="acs.archive_id"
    )
    row = connection.execute(
        f"""
        SELECT
            SUM(CASE WHEN acs.algorithm = ? AND acs.algorithm_version = ?
                      AND NOT EXISTS ({duplicates}) THEN 1 ELSE 0 END),
            SUM(CASE WHEN acs.algorithm = ? AND acs.algorithm_version != ?
                     THEN 1 ELSE 0 END),
            SUM(CASE WHEN acs.algorithm != ? THEN 1 ELSE 0 END),
            SUM(CASE WHEN acs.algorithm = ? AND acs.algorithm_version = ?
                      AND EXISTS ({duplicates}) THEN 1 ELSE 0 END)
        FROM archive_content_signatures AS acs
        """,
        (
            CONTENT_SIGNATURE_ALGORITHM,
            CONTENT_SIGNATURE_VERSION,
            CONTENT_SIGNATURE_ALGORITHM,
            CONTENT_SIGNATURE_VERSION,
            CONTENT_SIGNATURE_ALGORITHM,
            CONTENT_SIGNATURE_ALGORITHM,
            CONTENT_SIGNATURE_VERSION,
        ),
    ).fetchone()
    return {
        "comparable": int(row[0] or 0),
        "legacy_ordering_version": int(row[1] or 0),
        "unknown_algorithm": int(row[2] or 0),
        "unresolved_page_order": int(row[3] or 0),
    }


class ArchivePageHashRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def save(
        self,
        *,
        archive_id: int,
        location_id: int,
        result: ArchivePageHashes,
    ) -> None:
        owns_transaction = not self.connection.in_transaction

        try:
            if owns_transaction:
                self.connection.execute("BEGIN IMMEDIATE")

            # Rehashing replaces the entire page inventory rather than
            # trying to diff it: delete-then-reinsert is simpler and
            # correct even if pages were added, removed, or reordered.
            #
            # It is also what keeps a reorder honest. When the page-order
            # key changes (the raw-name tie-break, design §1.2), a member's
            # page_index changes, and an upsert keyed on
            # (archive_id, page_index) would leave the old member's digest
            # on a row that now names another member. Here each row and its
            # digest are written together from one PageContentHash, so a
            # digest is bound to its entry_name by construction.
            #
            # The delete cascades to every page_hashes row, perceptual ones
            # included: re-saving an archive discards its dhash/phash rows
            # and the perceptual worker recomputes them. Carrying them
            # across by entry_name is not implemented. A content signature
            # stored under the old order is replaced only when its archive
            # is re-saved; nothing here recomputes signatures in bulk. Until
            # then its algorithm_version ("1") marks it as legacy, and the
            # comparison readers refuse it as evidence.
            self.connection.execute(
                "DELETE FROM archive_pages WHERE archive_id = ?",
                (archive_id,),
            )

            for page in result.pages:
                cursor = self.connection.execute(
                    """
                    INSERT INTO archive_pages (
                        archive_id,
                        location_id,
                        page_index,
                        entry_name,
                        entry_size,
                        compressed_size,
                        crc32
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        archive_id,
                        location_id,
                        page.page_index,
                        page.entry_name,
                        page.entry_size,
                        page.compressed_size,
                        page.crc32,
                    ),
                )
                # page_hashes rows are keyed off the archive_pages row
                # just inserted (lastrowid), storing the exact
                # sha256 content digest for this page.
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
                    """,
                    (
                        int(cursor.lastrowid),
                        PAGE_HASH_ALGORITHM,
                        PAGE_HASH_ALGORITHM_VERSION,
                        page.digest,
                        page.bytes_read,
                    ),
                )

            # One archive_content_signatures row per archive_id,
            # summarizing the ordered page digests into a single
            # comparable value; upsert on rehash.
            self.connection.execute(
                """
                INSERT INTO archive_content_signatures (
                    archive_id,
                    location_id,
                    algorithm,
                    algorithm_version,
                    digest,
                    page_count,
                    image_bytes,
                    source_file_size,
                    source_modified_time_ns
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(archive_id) DO UPDATE SET
                    location_id = excluded.location_id,
                    algorithm = excluded.algorithm,
                    algorithm_version = excluded.algorithm_version,
                    digest = excluded.digest,
                    page_count = excluded.page_count,
                    image_bytes = excluded.image_bytes,
                    source_file_size = excluded.source_file_size,
                    source_modified_time_ns =
                        excluded.source_modified_time_ns,
                    calculated_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    archive_id,
                    location_id,
                    CONTENT_SIGNATURE_ALGORITHM,
                    CONTENT_SIGNATURE_VERSION,
                    result.content_digest,
                    result.page_count,
                    result.image_bytes,
                    result.source_file_size,
                    result.source_modified_time_ns,
                ),
            )
            # Keep the denormalized content_signature/page_count on
            # archive_files current for quick lookups without a join.
            self.connection.execute(
                """
                UPDATE archive_files
                SET
                    content_signature = ?,
                    page_count = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    result.content_digest,
                    result.page_count,
                    archive_id,
                ),
            )
            if owns_transaction:
                self.connection.execute("COMMIT")
        except Exception:
            if owns_transaction and self.connection.in_transaction:
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

        # Candidates for page hashing: archives that already have an
        # exact archive hash whose recorded file_size/modified_time_ns
        # still match the current file_locations row, and that either
        # lack a content signature for that exact size/mtime or don't
        # already have a hash_archive_pages job in flight (including
        # 'failed', so a job that previously failed permanently isn't
        # silently retried here -- see the module's job handler).
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
        rows = self.connection.execute(
            f"""
            SELECT ah.archive_id
            FROM archive_hashes AS ah
            JOIN file_locations AS fl
              ON fl.archive_id = ah.archive_id
             AND fl.is_current = 1
            LEFT JOIN archive_content_signatures AS acs
              ON acs.archive_id = ah.archive_id
             AND acs.source_file_size = fl.file_size
             AND acs.source_modified_time_ns = fl.modified_time_ns
            WHERE ah.file_size = fl.file_size
              AND ah.modified_time_ns = fl.modified_time_ns
              AND acs.id IS NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM jobs AS j
                  WHERE j.archive_id = ah.archive_id
                    AND j.job_type = 'hash_archive_pages'
                    AND j.status IN (
                        'pending',
                        'claimed',
                        'running',
                        'failed'
                    )
              )
            ORDER BY ah.archive_id
            {limit_clause}
            """,
            parameters,
        ).fetchall()
        queue = JobQueue(self.connection)
        created = 0

        for row in rows:
            outcome = queue.enqueue_if_absent(
                "hash_archive_pages",
                archive_id=int(row["archive_id"]),
                priority=300,
            )

            if outcome is EnqueueOutcome.CREATED:
                created += 1

        # Count rows actually inserted, not candidates considered (see
        # ArchiveHashRepository.enqueue_missing for the reasoning).
        return created

    def unresolved_page_order_archive_ids(self) -> list[int]:
        """Archives whose stored page order is unresolved, ascending.

        An archive is unresolved when two of its stored page rows carry the
        same raw ``entry_name`` (see `order_page_entries`). Read-only.
        """
        predicate = _STORED_DUPLICATE_ENTRY_NAMES_SQL.format(
            archive_id="af.id"
        )
        return [
            int(row[0])
            for row in self.connection.execute(
                f"""
                SELECT af.id
                FROM archive_files AS af
                WHERE EXISTS ({predicate})
                ORDER BY af.id
                """
            )
        ]

    def duplicate_content_groups(self) -> list[dict]:
        # Group archives (current location only) by content signature
        # algorithm/version/digest/page_count; more than one member
        # means byte-for-byte identical page sequences, i.e. the same
        # comic content even if the container files differ.
        #
        # Only canonical evidence is grouped: the current ordering version
        # (a legacy-order row can collide with a current one for different
        # archives) and a resolved page order (an unresolved signature
        # depends on physical member order, so two of them sharing one is
        # not evidence of the same sequence). Grouping by algorithm and
        # version alone would not be enough: it keeps versions apart but
        # still reports legacy-vs-legacy groups as duplicates.
        comparable = comparable_ordered_signature_sql("acs")
        rows = self.connection.execute(
            f"""
            SELECT
                acs.algorithm,
                acs.algorithm_version,
                acs.digest,
                acs.page_count,
                COUNT(*) AS archive_count,
                GROUP_CONCAT(fl.path, char(10)) AS paths
            FROM archive_content_signatures AS acs
            JOIN file_locations AS fl
              ON fl.archive_id = acs.archive_id
             AND fl.is_current = 1
            WHERE {comparable}
            GROUP BY
                acs.algorithm,
                acs.algorithm_version,
                acs.digest,
                acs.page_count
            HAVING COUNT(*) > 1
               AND acs.page_count > 0
            ORDER BY archive_count DESC, acs.page_count DESC, acs.digest
            """
        ).fetchall()

        return [
            {
                "algorithm": row["algorithm"],
                "algorithm_version": row["algorithm_version"],
                "digest": row["digest"],
                "page_count": int(row["page_count"]),
                "archive_count": int(row["archive_count"]),
                "paths": str(row["paths"]).splitlines(),
            }
            for row in rows
        ]


class HashArchivePagesHandler:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        self.locations = ArchiveInspectionRepository(connection)
        self.pages = ArchivePageHashRepository(connection)
        self.chunk_size = chunk_size

    def __call__(self, job: Job) -> None:
        if job.archive_id is None:
            raise ValueError(f"Job {job.id} has no archive_id.")

        location = self.locations.current_location(job.archive_id)
        path = Path(str(location["path"]))

        try:
            result = calculate_page_hashes(
                path,
                chunk_size=self.chunk_size,
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

        self.pages.save(
            archive_id=job.archive_id,
            location_id=int(location["id"]),
            result=result,
        )
