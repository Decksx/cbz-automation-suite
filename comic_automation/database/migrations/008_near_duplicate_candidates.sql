-- Migration 008: near-duplicate candidates.
--
-- Adds page dimensions (used for aspect-ratio comparison) and the
-- review table for perceptual near-duplicate matches (phase 5:
-- pHash/dHash comparison, ahead of quality scoring / OpenCLIP). See
-- comic_automation/archive/near_duplicate.py for the comparison logic
-- that populates near_duplicate_candidates.

ALTER TABLE archive_pages ADD COLUMN width INTEGER;
ALTER TABLE archive_pages ADD COLUMN height INTEGER;
ALTER TABLE archive_pages ADD COLUMN image_format TEXT;

-- near_duplicate_candidates is a review queue, not a final verdict:
-- every row starts 'pending_review' and is only ever a candidate match
-- between two archives (archive_a_id < archive_b_id enforces a single
-- canonical row per unordered pair) produced by a specific match_method.
CREATE TABLE IF NOT EXISTS near_duplicate_candidates (
    id INTEGER PRIMARY KEY,
    archive_a_id INTEGER NOT NULL,
    archive_b_id INTEGER NOT NULL,
    match_method TEXT NOT NULL,
    similarity_score REAL NOT NULL,
    page_match_ratio REAL NOT NULL,
    compared_page_count INTEGER NOT NULL,
    page_count_a INTEGER NOT NULL,
    page_count_b INTEGER NOT NULL,
    average_dhash_distance REAL NOT NULL,
    average_phash_distance REAL NOT NULL,
    dimension_match_ratio REAL,
    metrics_json TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'pending_review',
    reviewed_by TEXT,
    reviewed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (archive_a_id) REFERENCES archive_files(id)
        ON DELETE CASCADE,
    FOREIGN KEY (archive_b_id) REFERENCES archive_files(id)
        ON DELETE CASCADE,
    -- Enforces a single canonical ordering per pair so the same match
    -- can't be stored twice as (A, B) and (B, A).
    CHECK (archive_a_id < archive_b_id),
    CHECK (similarity_score BETWEEN 0.0 AND 1.0),
    CHECK (page_match_ratio BETWEEN 0.0 AND 1.0),
    CHECK (
        dimension_match_ratio IS NULL
        OR dimension_match_ratio BETWEEN 0.0 AND 1.0
    ),
    -- Reviewer decision states: a match is either still awaiting
    -- review, confirmed as a true duplicate, explicitly kept as two
    -- distinct archives, or rejected as a false positive.
    CHECK (
        review_status IN (
            'pending_review',
            'confirmed_duplicate',
            'keep_both',
            'rejected'
        )
    ),
    -- One row per (pair, match_method): re-running the same detector
    -- updates the existing candidate instead of duplicating it.
    UNIQUE (archive_a_id, archive_b_id, match_method)
);

-- Supports the review-queue query: pull pending candidates ranked by
-- similarity, highest first.
CREATE INDEX IF NOT EXISTS idx_near_duplicate_review
    ON near_duplicate_candidates(
        review_status,
        similarity_score DESC
    );
