-- Migration 002: discovery checkpoints.
--
-- Adds resumable-scan bookkeeping for library discovery. A single
-- discovery_checkpoints row belongs to exactly one source_batches row
-- (see migration 001) and is updated incrementally as the scan walks
-- the source tree in path order, so an interrupted scan can resume from
-- last_path instead of restarting. See LibraryRepository / scan_library
-- in comic_automation/library/repository.py for the code that reads and
-- writes this table.
CREATE TABLE IF NOT EXISTS discovery_checkpoints (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL UNIQUE,
    source_path TEXT NOT NULL,
    last_path TEXT,
    scanned INTEGER NOT NULL DEFAULT 0,
    new_count INTEGER NOT NULL DEFAULT 0,
    changed_count INTEGER NOT NULL DEFAULT 0,
    unchanged_count INTEGER NOT NULL DEFAULT 0,
    jobs_queued INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (batch_id) REFERENCES source_batches(id)
        ON DELETE CASCADE
);

-- Supports finding the most recent (resumable) checkpoint for a given
-- source path, ordered by recency.
CREATE INDEX IF NOT EXISTS idx_discovery_checkpoints_source
    ON discovery_checkpoints(source_path, updated_at);
