from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from comic_automation.archive.page_hashing import (
    PAGE_HASH_ALGORITHM,
    PAGE_HASH_ALGORITHM_VERSION,
)
from comic_automation.archive.perceptual_hashing import (
    DHASH_ALGORITHM,
    DHASH_ALGORITHM_VERSION,
    PHASH_ALGORITHM,
    PHASH_ALGORITHM_VERSION,
)


JOB_TYPE = "hash_archive_pages_perceptual"


ANALYSIS_SQL = """
WITH eligible_archives AS (
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
           AND dh.algorithm = :dhash_algorithm
           AND dh.algorithm_version = :dhash_version
          LEFT JOIN page_hashes AS ph
            ON ph.page_id = ap.id
           AND ph.algorithm = :phash_algorithm
           AND ph.algorithm_version = :phash_version
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
            AND j.job_type = :job_type
            AND j.status IN (
                'pending',
                'claimed',
                'running',
                'failed'
            )
      )
),
eligible_page_state AS (
    SELECT
        ap.id AS page_id,
        ap.archive_id,
        ap.width,
        ap.height,
        sha.digest AS sha256_digest,
        CASE
            WHEN dh.id IS NULL
              OR ph.id IS NULL
              OR ap.width IS NULL
              OR ap.height IS NULL
            THEN 1
            ELSE 0
        END AS is_incomplete
    FROM eligible_archives AS ea
    JOIN archive_pages AS ap
      ON ap.archive_id = ea.archive_id
    LEFT JOIN page_hashes AS sha
      ON sha.page_id = ap.id
     AND sha.algorithm = :sha256_algorithm
     AND sha.algorithm_version = :sha256_version
    LEFT JOIN page_hashes AS dh
      ON dh.page_id = ap.id
     AND dh.algorithm = :dhash_algorithm
     AND dh.algorithm_version = :dhash_version
    LEFT JOIN page_hashes AS ph
      ON ph.page_id = ap.id
     AND ph.algorithm = :phash_algorithm
     AND ph.algorithm_version = :phash_version
),
incomplete_destinations AS (
    SELECT *
    FROM eligible_page_state
    WHERE is_incomplete = 1
),
needed_sha256 AS (
    SELECT DISTINCT sha256_digest
    FROM incomplete_destinations
    WHERE sha256_digest IS NOT NULL
),
complete_source_rows AS (
    SELECT
        sha.digest AS sha256_digest,
        dh.digest AS dhash_digest,
        ph.digest AS phash_digest,
        ap.width,
        ap.height
    FROM needed_sha256 AS needed
    CROSS JOIN page_hashes AS sha
        INDEXED BY idx_page_hashes_digest
    JOIN archive_pages AS ap
      ON ap.id = sha.page_id
    JOIN page_hashes AS dh
      ON dh.page_id = ap.id
     AND dh.algorithm = :dhash_algorithm
     AND dh.algorithm_version = :dhash_version
    JOIN page_hashes AS ph
      ON ph.page_id = ap.id
     AND ph.algorithm = :phash_algorithm
     AND ph.algorithm_version = :phash_version
    WHERE ap.width IS NOT NULL
      AND ap.height IS NOT NULL
      AND sha.algorithm = :sha256_algorithm
      AND sha.algorithm_version = :sha256_version
      AND sha.digest = needed.sha256_digest
),
source_evidence_rollup AS (
    SELECT
        sha256_digest,
        COUNT(*) AS complete_source_rows,
        MIN(dhash_digest) AS minimum_dhash,
        MAX(dhash_digest) AS maximum_dhash,
        MIN(phash_digest) AS minimum_phash,
        MAX(phash_digest) AS maximum_phash,
        MIN(width) AS minimum_width,
        MAX(width) AS maximum_width,
        MIN(height) AS minimum_height,
        MAX(height) AS maximum_height
    FROM complete_source_rows
    GROUP BY sha256_digest
),
unambiguous_source AS (
    SELECT sha256_digest
    FROM source_evidence_rollup
    WHERE minimum_dhash = maximum_dhash
      AND minimum_phash = maximum_phash
      AND minimum_width = maximum_width
      AND minimum_height = maximum_height
),
destination_classification AS (
    SELECT
        state.*,
        CASE
            WHEN state.is_incomplete = 1
             AND state.sha256_digest IN (
                 SELECT sha256_digest
                 FROM unambiguous_source
             )
            THEN 1
            ELSE 0
        END AS is_reusable,
        CASE
            WHEN state.is_incomplete = 1
             AND (
                 state.sha256_digest IS NULL
                 OR state.sha256_digest NOT IN (
                     SELECT sha256_digest
                     FROM unambiguous_source
                 )
             )
            THEN 1
            ELSE 0
        END AS still_requires_decode
    FROM eligible_page_state AS state
),
archive_rollup AS (
    SELECT
        archive_id,
        COUNT(*) AS page_count,
        SUM(is_incomplete) AS incomplete_pages,
        SUM(is_reusable) AS reusable_pages,
        SUM(still_requires_decode) AS pages_still_requiring_decode
    FROM destination_classification
    GROUP BY archive_id
)
SELECT
    (SELECT COUNT(*) FROM eligible_archives)
        AS eligible_archives,
    COALESCE((SELECT SUM(page_count) FROM archive_rollup), 0)
        AS eligible_pages,
    COALESCE((SELECT SUM(incomplete_pages) FROM archive_rollup), 0)
        AS incomplete_pages,
    COALESCE((SELECT SUM(reusable_pages) FROM archive_rollup), 0)
        AS reusable_pages,
    COALESCE((
        SELECT COUNT(*)
        FROM archive_rollup
        WHERE pages_still_requiring_decode = 0
    ), 0) AS fully_satisfied_archives,
    COALESCE((
        SELECT COUNT(*)
        FROM archive_rollup
        WHERE reusable_pages > 0
          AND pages_still_requiring_decode > 0
    ), 0) AS partially_satisfied_archives,
    COALESCE((
        SELECT SUM(pages_still_requiring_decode)
        FROM archive_rollup
    ), 0) AS pages_still_requiring_decode,
    COALESCE((
        SELECT COUNT(*)
        FROM archive_rollup
        WHERE pages_still_requiring_decode > 0
    ), 0) AS archives_still_requiring_processing,
    COALESCE((
        SELECT COUNT(*)
        FROM archive_rollup
        WHERE reusable_pages = 0
    ), 0) AS archives_without_reuse,
    COALESCE((
        SELECT SUM(page_count)
        FROM archive_rollup
        WHERE pages_still_requiring_decode = 0
    ), 0) AS pages_avoided_by_full_archive_reuse,
    COALESCE((
        SELECT SUM(page_count)
        FROM archive_rollup
        WHERE pages_still_requiring_decode > 0
    ), 0) AS pages_decoded_by_current_worker_after_reuse,
    COALESCE((
        SELECT SUM(page_count - pages_still_requiring_decode)
        FROM archive_rollup
    ), 0) AS pages_avoided_with_selective_worker,
    (SELECT COUNT(*) FROM needed_sha256)
        AS needed_sha256_digests,
    (SELECT COUNT(*) FROM source_evidence_rollup)
        AS sha256_digests_with_complete_source,
    (SELECT COUNT(*) FROM unambiguous_source)
        AS unambiguous_source_sha256_digests,
    (
        SELECT COUNT(*)
        FROM source_evidence_rollup
        WHERE minimum_dhash != maximum_dhash
           OR minimum_phash != maximum_phash
           OR minimum_width != maximum_width
           OR minimum_height != maximum_height
    ) AS ambiguous_source_sha256_digests,
    (
        SELECT COUNT(*)
        FROM incomplete_destinations
        WHERE sha256_digest IS NULL
    ) AS incomplete_pages_without_sha256
"""


def _parameters() -> dict[str, str]:
    return {
        "sha256_algorithm": PAGE_HASH_ALGORITHM,
        "sha256_version": PAGE_HASH_ALGORITHM_VERSION,
        "dhash_algorithm": DHASH_ALGORITHM,
        "dhash_version": DHASH_ALGORITHM_VERSION,
        "phash_algorithm": PHASH_ALGORITHM,
        "phash_version": PHASH_ALGORITHM_VERSION,
        "job_type": JOB_TYPE,
    }


def connect_read_only(database: str | Path) -> sqlite3.Connection:
    path = Path(database).resolve(strict=True)
    uri_path = quote(path.as_posix(), safe="/:")
    connection = sqlite3.connect(
        f"file:{uri_path}?mode=ro",
        uri=True,
        timeout=60.0,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 60000")
    return connection


def analyze_reuse_opportunity(database: str | Path) -> dict:
    path = Path(database).resolve(strict=True)
    before = path.stat()
    total_started = time.perf_counter()

    with closing(connect_read_only(path)) as connection:
        connection.execute("BEGIN")
        try:
            quick_check_started = time.perf_counter()
            quick_check = connection.execute(
                "PRAGMA quick_check"
            ).fetchone()[0]
            quick_check_seconds = (
                time.perf_counter() - quick_check_started
            )

            if quick_check != "ok":
                raise RuntimeError(
                    f"Database quick_check failed: {quick_check}"
                )

            plan_rows = connection.execute(
                f"EXPLAIN QUERY PLAN {ANALYSIS_SQL}",
                _parameters(),
            ).fetchall()
            analysis_started = time.perf_counter()
            metrics_row = connection.execute(
                ANALYSIS_SQL,
                _parameters(),
            ).fetchone()
            analysis_seconds = time.perf_counter() - analysis_started
            index_rows = connection.execute(
                """
                SELECT name, tbl_name, sql
                FROM sqlite_master
                WHERE type = 'index'
                  AND tbl_name IN (
                      'archive_pages',
                      'page_hashes',
                      'jobs',
                      'file_locations'
                  )
                ORDER BY tbl_name, name
                """
            ).fetchall()
        finally:
            connection.execute("ROLLBACK")

    after = path.stat()
    metrics = {
        key: int(metrics_row[key])
        for key in metrics_row.keys()
    }
    elapsed_seconds = time.perf_counter() - total_started

    return {
        "analysis": "perceptual_hash_exact_sha_reuse_opportunity",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "database": str(path),
        "read_only": True,
        "quick_check": quick_check,
        "algorithm_versions": {
            PAGE_HASH_ALGORITHM: PAGE_HASH_ALGORITHM_VERSION,
            DHASH_ALGORITHM: DHASH_ALGORITHM_VERSION,
            PHASH_ALGORITHM: PHASH_ALGORITHM_VERSION,
        },
        "query": {
            "sql": ANALYSIS_SQL.strip(),
            "parameters": _parameters(),
        },
        "database_snapshot": {
            "size_bytes_before": int(before.st_size),
            "size_bytes_after": int(after.st_size),
            "modified_time_ns_before": int(before.st_mtime_ns),
            "modified_time_ns_after": int(after.st_mtime_ns),
            "metadata_unchanged": (
                before.st_size == after.st_size
                and before.st_mtime_ns == after.st_mtime_ns
            ),
        },
        "metrics": metrics,
        "timing": {
            "quick_check_seconds": quick_check_seconds,
            "analysis_seconds": analysis_seconds,
            "total_seconds": elapsed_seconds,
        },
        "query_plan": [
            {
                "id": int(row["id"]),
                "parent": int(row["parent"]),
                "detail": str(row["detail"]),
            }
            for row in plan_rows
        ],
        "available_indexes": [
            {
                "name": str(row["name"]),
                "table": str(row["tbl_name"]),
                "sql": (
                    str(row["sql"])
                    if row["sql"] is not None
                    else None
                ),
            }
            for row in index_rows
        ],
    }
