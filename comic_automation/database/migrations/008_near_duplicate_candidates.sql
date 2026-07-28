ALTER TABLE archive_pages ADD COLUMN width INTEGER;
ALTER TABLE archive_pages ADD COLUMN height INTEGER;
ALTER TABLE archive_pages ADD COLUMN image_format TEXT;

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
    CHECK (archive_a_id < archive_b_id),
    CHECK (similarity_score BETWEEN 0.0 AND 1.0),
    CHECK (page_match_ratio BETWEEN 0.0 AND 1.0),
    CHECK (
        dimension_match_ratio IS NULL
        OR dimension_match_ratio BETWEEN 0.0 AND 1.0
    ),
    CHECK (
        review_status IN (
            'pending_review',
            'confirmed_duplicate',
            'keep_both',
            'rejected'
        )
    ),
    UNIQUE (archive_a_id, archive_b_id, match_method)
);

CREATE INDEX IF NOT EXISTS idx_near_duplicate_review
    ON near_duplicate_candidates(
        review_status,
        similarity_score DESC
    );
