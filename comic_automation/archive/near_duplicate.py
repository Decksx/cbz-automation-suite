from __future__ import annotations

import json
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations

from comic_automation.archive.perceptual_hashing import (
    DHASH_ALGORITHM,
    DHASH_ALGORITHM_VERSION,
    PHASH_ALGORITHM,
    PHASH_ALGORITHM_VERSION,
)


MATCH_METHOD = "ordered_perceptual_v1"
DEFAULT_MAX_HAMMING_DISTANCE = 6
DEFAULT_MIN_PAGE_MATCH_RATIO = 0.90
DEFAULT_MAX_PAGE_COUNT_DELTA_RATIO = 0.05
DEFAULT_MAX_BLOCK_BUCKET_SIZE = 200


@dataclass(frozen=True)
class PageFingerprint:
    page_index: int
    dhash: str
    phash: str
    width: int | None
    height: int | None

    @property
    def pixel_area(self) -> int | None:
        if self.width is None or self.height is None:
            return None
        return self.width * self.height


@dataclass(frozen=True)
class ArchiveFingerprint:
    archive_id: int
    content_signature: str
    pages: tuple[PageFingerprint, ...]

    @property
    def page_count(self) -> int:
        return len(self.pages)


@dataclass(frozen=True)
class NearDuplicateComparison:
    archive_a_id: int
    archive_b_id: int
    similarity_score: float
    page_match_ratio: float
    compared_page_count: int
    average_dhash_distance: float
    average_phash_distance: float
    dimension_match_ratio: float | None
    alignment_offset: int
    median_pixel_area_a: float | None
    median_pixel_area_b: float | None

    def metrics(self) -> dict:
        return {
            "alignment_offset": self.alignment_offset,
            "average_dhash_distance": self.average_dhash_distance,
            "average_phash_distance": self.average_phash_distance,
            "compared_page_count": self.compared_page_count,
            "dimension_match_ratio": self.dimension_match_ratio,
            "median_pixel_area_a": self.median_pixel_area_a,
            "median_pixel_area_b": self.median_pixel_area_b,
            "page_match_ratio": self.page_match_ratio,
        }


def hamming_distance(first: str, second: str) -> int:
    if len(first) != len(second):
        raise ValueError("Perceptual hashes must have equal lengths.")

    try:
        return (int(first, 16) ^ int(second, 16)).bit_count()
    except ValueError as exc:
        raise ValueError("Perceptual hashes must be hexadecimal.") from exc


def _dimension_matches(
    first: PageFingerprint,
    second: PageFingerprint,
) -> bool | None:
    if (
        first.width is None
        or first.height is None
        or second.width is None
        or second.height is None
        or first.height == 0
        or second.height == 0
    ):
        return None

    first_ratio = first.width / first.height
    second_ratio = second.width / second.height
    return (
        abs(first_ratio - second_ratio)
        / max(first_ratio, second_ratio)
        <= 0.02
    )


def _median_pixel_area(
    pages: tuple[PageFingerprint, ...],
) -> float | None:
    areas = [page.pixel_area for page in pages if page.pixel_area]
    return float(statistics.median(areas)) if areas else None


def _aligned_pages(
    first: ArchiveFingerprint,
    second: ArchiveFingerprint,
    offset: int,
) -> list[tuple[PageFingerprint, PageFingerprint]]:
    pairs = []

    for page in first.pages:
        other_index = page.page_index + offset
        if 0 <= other_index < second.page_count:
            pairs.append((page, second.pages[other_index]))

    return pairs


def compare_archive_fingerprints(
    first: ArchiveFingerprint,
    second: ArchiveFingerprint,
    *,
    max_hamming_distance: int = DEFAULT_MAX_HAMMING_DISTANCE,
    min_page_match_ratio: float = DEFAULT_MIN_PAGE_MATCH_RATIO,
    max_page_count_delta_ratio: float = (
        DEFAULT_MAX_PAGE_COUNT_DELTA_RATIO
    ),
) -> NearDuplicateComparison | None:
    if first.archive_id == second.archive_id:
        raise ValueError("Cannot compare an archive with itself.")
    if first.page_count == 0 or second.page_count == 0:
        return None
    if first.content_signature == second.content_signature:
        return None

    largest_page_count = max(first.page_count, second.page_count)
    count_delta = abs(first.page_count - second.page_count)
    allowed_delta = max(
        1,
        round(largest_page_count * max_page_count_delta_ratio),
    )

    if count_delta > allowed_delta:
        return None

    best: tuple[
        int,
        list[tuple[PageFingerprint, PageFingerprint]],
        list[int],
        list[int],
        list[bool],
    ] | None = None
    best_rank: float | None = None

    for offset in range(-allowed_delta, allowed_delta + 1):
        pairs = _aligned_pages(first, second, offset)
        if not pairs:
            continue

        dhash_distances = [
            hamming_distance(left.dhash, right.dhash)
            for left, right in pairs
        ]
        phash_distances = [
            hamming_distance(left.phash, right.phash)
            for left, right in pairs
        ]
        matches = [
            dhash <= max_hamming_distance
            and phash <= max_hamming_distance
            for dhash, phash in zip(dhash_distances, phash_distances)
        ]
        match_ratio = sum(matches) / largest_page_count
        rank = (
            match_ratio
            - statistics.fmean(phash_distances) / 640
            - statistics.fmean(dhash_distances) / 1280
        )
        dimensions = [
            match
            for left, right in pairs
            if (match := _dimension_matches(left, right)) is not None
        ]
        candidate = (
            offset,
            pairs,
            dhash_distances,
            phash_distances,
            dimensions,
        )

        if best_rank is None or rank > best_rank:
            best_rank = rank
            best = candidate

    if best is None:
        return None

    offset, pairs, dhash_distances, phash_distances, dimensions = best
    matched_pages = sum(
        dhash <= max_hamming_distance
        and phash <= max_hamming_distance
        for dhash, phash in zip(dhash_distances, phash_distances)
    )
    page_match_ratio = matched_pages / largest_page_count

    if page_match_ratio < min_page_match_ratio:
        return None

    average_dhash = statistics.fmean(dhash_distances)
    average_phash = statistics.fmean(phash_distances)
    dimension_ratio = (
        sum(dimensions) / len(dimensions) if dimensions else None
    )
    distance_score = 1.0 - (
        average_dhash + average_phash
    ) / 128
    similarity_score = (
        0.75 * page_match_ratio
        + 0.20 * max(0.0, distance_score)
        + 0.05 * (
            dimension_ratio if dimension_ratio is not None else 0.5
        )
    )

    if first.archive_id < second.archive_id:
        archive_a, archive_b = first, second
        normalized_offset = offset
    else:
        archive_a, archive_b = second, first
        normalized_offset = -offset

    return NearDuplicateComparison(
        archive_a_id=archive_a.archive_id,
        archive_b_id=archive_b.archive_id,
        similarity_score=min(1.0, max(0.0, similarity_score)),
        page_match_ratio=page_match_ratio,
        compared_page_count=len(pairs),
        average_dhash_distance=average_dhash,
        average_phash_distance=average_phash,
        dimension_match_ratio=dimension_ratio,
        alignment_offset=normalized_offset,
        median_pixel_area_a=_median_pixel_area(archive_a.pages),
        median_pixel_area_b=_median_pixel_area(archive_b.pages),
    )


class NearDuplicateRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def load_fingerprints(self) -> list[ArchiveFingerprint]:
        # Pull one row per (archive, page) with both perceptual hashes
        # attached, restricted to archives whose page hashing is
        # current for the live file (matching source_file_size /
        # source_modified_time_ns against file_locations). The two
        # JOINs against page_hashes (aliased dh/ph) each pick out a
        # specific algorithm/version, effectively pivoting the
        # per-algorithm hash rows onto a single page row.
        rows = self.connection.execute(
            """
            SELECT
                acs.archive_id,
                acs.digest AS content_signature,
                acs.page_count,
                ap.page_index,
                ap.width,
                ap.height,
                dh.digest AS dhash,
                ph.digest AS phash
            FROM archive_content_signatures AS acs
            JOIN archive_pages AS ap
              ON ap.archive_id = acs.archive_id
            JOIN page_hashes AS dh
              ON dh.page_id = ap.id
             AND dh.algorithm = ?
             AND dh.algorithm_version = ?
            JOIN page_hashes AS ph
              ON ph.page_id = ap.id
             AND ph.algorithm = ?
             AND ph.algorithm_version = ?
            WHERE acs.page_count > 0
              AND EXISTS (
                  SELECT 1
                  FROM file_locations AS fl
                  WHERE fl.archive_id = acs.archive_id
                    AND fl.is_current = 1
                    AND fl.file_size = acs.source_file_size
                    AND fl.modified_time_ns = (
                        acs.source_modified_time_ns
                    )
              )
            ORDER BY acs.archive_id, ap.page_index
            """,
            (
                DHASH_ALGORITHM,
                DHASH_ALGORITHM_VERSION,
                PHASH_ALGORITHM,
                PHASH_ALGORITHM_VERSION,
            ),
        ).fetchall()
        grouped: dict[int, list[sqlite3.Row]] = defaultdict(list)

        for row in rows:
            grouped[int(row["archive_id"])].append(row)

        fingerprints = []

        for archive_id, archive_rows in grouped.items():
            # Defensive check: only build a fingerprint if every page
            # for this archive came back with both hashes (an archive
            # mid-way through perceptual hashing would otherwise
            # produce a partial, misleading fingerprint).
            expected_count = int(archive_rows[0]["page_count"])
            if len(archive_rows) != expected_count:
                continue

            fingerprints.append(
                ArchiveFingerprint(
                    archive_id=archive_id,
                    content_signature=str(
                        archive_rows[0]["content_signature"]
                    ),
                    pages=tuple(
                        PageFingerprint(
                            page_index=int(row["page_index"]),
                            dhash=str(row["dhash"]),
                            phash=str(row["phash"]),
                            width=(
                                int(row["width"])
                                if row["width"] is not None
                                else None
                            ),
                            height=(
                                int(row["height"])
                                if row["height"] is not None
                                else None
                            ),
                        )
                        for row in archive_rows
                    ),
                )
            )

        return fingerprints

    def candidate_pairs(
        self,
        fingerprints: list[ArchiveFingerprint],
        *,
        max_bucket_size: int = DEFAULT_MAX_BLOCK_BUCKET_SIZE,
        max_page_count_delta_ratio: float = (
            DEFAULT_MAX_PAGE_COUNT_DELTA_RATIO
        ),
    ) -> set[tuple[int, int]]:
        # Locality-sensitive-hashing-style blocking, entirely in
        # Python/memory (no SQL here): rather than comparing every
        # archive against every other archive (O(n^2) full comparisons,
        # each itself expensive), sample a few representative pages per
        # archive and bucket archives that share a hash "band" so only
        # plausible candidate pairs go on to the expensive comparison
        # in generate_candidates().
        if max_bucket_size < 2:
            raise ValueError("max_bucket_size must be at least 2.")

        by_id = {
            fingerprint.archive_id: fingerprint
            for fingerprint in fingerprints
        }
        buckets: dict[tuple[str, int, int, str], list[int]] = (
            defaultdict(list)
        )

        for fingerprint in fingerprints:
            sample_indexes = sorted({
                0,
                fingerprint.page_count // 2,
                fingerprint.page_count - 1,
            })

            for sample_slot, page_index in enumerate(sample_indexes):
                page = fingerprint.pages[page_index]

                for algorithm, digest in (
                    (DHASH_ALGORITHM, page.dhash),
                    (PHASH_ALGORITHM, page.phash),
                ):
                    for band_index in range(4):
                        start = band_index * 4
                        key = (
                            algorithm,
                            sample_slot,
                            band_index,
                            digest[start:start + 4],
                        )
                        buckets[key].append(fingerprint.archive_id)

        pairs: set[tuple[int, int]] = set()

        for archive_ids in buckets.values():
            unique_ids = sorted(set(archive_ids))
            if len(unique_ids) > max_bucket_size:
                # Skip buckets that are too large to be useful blocking
                # (a bucket this big means the sampled hash band wasn't
                # discriminating, so pairing everything in it would
                # blow up comparison cost with little benefit).
                continue

            for archive_a_id, archive_b_id in combinations(
                unique_ids,
                2,
            ):
                first = by_id[archive_a_id]
                second = by_id[archive_b_id]
                largest = max(first.page_count, second.page_count)
                allowed = max(
                    1,
                    round(
                        largest
                        * max_page_count_delta_ratio
                    ),
                )
                if abs(first.page_count - second.page_count) <= allowed:
                    pairs.add((archive_a_id, archive_b_id))

        return pairs

    def generate_candidates(
        self,
        *,
        limit: int,
        max_hamming_distance: int = DEFAULT_MAX_HAMMING_DISTANCE,
        min_page_match_ratio: float = DEFAULT_MIN_PAGE_MATCH_RATIO,
        max_page_count_delta_ratio: float = (
            DEFAULT_MAX_PAGE_COUNT_DELTA_RATIO
        ),
        max_bucket_size: int = DEFAULT_MAX_BLOCK_BUCKET_SIZE,
    ) -> list[NearDuplicateComparison]:
        if limit < 1:
            raise ValueError("limit must be at least 1.")

        fingerprints = self.load_fingerprints()
        by_id = {
            fingerprint.archive_id: fingerprint
            for fingerprint in fingerprints
        }
        comparisons = []

        for archive_a_id, archive_b_id in sorted(
            self.candidate_pairs(
                fingerprints,
                max_bucket_size=max_bucket_size,
                max_page_count_delta_ratio=(
                    max_page_count_delta_ratio
                ),
            )
        ):
            comparison = compare_archive_fingerprints(
                by_id[archive_a_id],
                by_id[archive_b_id],
                max_hamming_distance=max_hamming_distance,
                min_page_match_ratio=min_page_match_ratio,
                max_page_count_delta_ratio=(
                    max_page_count_delta_ratio
                ),
            )
            if comparison is not None:
                comparisons.append(comparison)

        comparisons.sort(
            key=lambda item: (
                -item.similarity_score,
                item.archive_a_id,
                item.archive_b_id,
            )
        )
        selected = comparisons[:limit]

        try:
            self.connection.execute("BEGIN IMMEDIATE")

            for item in selected:
                # Upsert into the review queue, but only overwrite an
                # existing row's computed metrics while it's still
                # 'pending_review' (the WHERE clause on the UPDATE
                # branch) -- once a reviewer has confirmed, rejected, or
                # kept-both a candidate, rerunning detection must not
                # silently clobber that decision.
                self.connection.execute(
                    """
                    INSERT INTO near_duplicate_candidates (
                        archive_a_id,
                        archive_b_id,
                        match_method,
                        similarity_score,
                        page_match_ratio,
                        compared_page_count,
                        page_count_a,
                        page_count_b,
                        average_dhash_distance,
                        average_phash_distance,
                        dimension_match_ratio,
                        metrics_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(
                        archive_a_id,
                        archive_b_id,
                        match_method
                    ) DO UPDATE SET
                        similarity_score = excluded.similarity_score,
                        page_match_ratio = excluded.page_match_ratio,
                        compared_page_count = excluded.compared_page_count,
                        page_count_a = excluded.page_count_a,
                        page_count_b = excluded.page_count_b,
                        average_dhash_distance = (
                            excluded.average_dhash_distance
                        ),
                        average_phash_distance = (
                            excluded.average_phash_distance
                        ),
                        dimension_match_ratio = (
                            excluded.dimension_match_ratio
                        ),
                        metrics_json = excluded.metrics_json,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE near_duplicate_candidates.review_status = (
                        'pending_review'
                    )
                    """,
                    (
                        item.archive_a_id,
                        item.archive_b_id,
                        MATCH_METHOD,
                        item.similarity_score,
                        item.page_match_ratio,
                        item.compared_page_count,
                        by_id[item.archive_a_id].page_count,
                        by_id[item.archive_b_id].page_count,
                        item.average_dhash_distance,
                        item.average_phash_distance,
                        item.dimension_match_ratio,
                        json.dumps(
                            item.metrics(),
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                )

            self.connection.execute("COMMIT")
        except Exception:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

        return selected

    def review_summary(self) -> dict[str, int]:
        # Count of candidates in each review_status bucket, used for
        # CLI/report summaries of the review queue.
        rows = self.connection.execute(
            """
            SELECT review_status, COUNT(*) AS count
            FROM near_duplicate_candidates
            GROUP BY review_status
            """
        ).fetchall()
        return {
            str(row["review_status"]): int(row["count"])
            for row in rows
        }
