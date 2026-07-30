-- Migration 006: archive hashes.
--
-- Stores the full-file SHA-256 digest for each archive (phase 3 of the
-- dedupe pipeline: exact archive hash, before per-page hashing). One
-- row per archive_id; file_size/modified_time_ns are duplicated here
-- from file_locations so a hash can be judged stale (the source file
-- changed since it was hashed) without a join. See
-- ArchiveHashRepository in comic_automation/archive/hashing.py.
CREATE TABLE IF NOT EXISTS archive_hashes (
    id INTEGER PRIMARY KEY,
    archive_id INTEGER NOT NULL UNIQUE,
    location_id INTEGER,
    algorithm TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    digest TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    modified_time_ns INTEGER NOT NULL,
    bytes_read INTEGER NOT NULL,
    hashed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (archive_id) REFERENCES archive_files(id)
        ON DELETE CASCADE,
    FOREIGN KEY (location_id) REFERENCES file_locations(id)
        ON DELETE SET NULL
);

-- Supports the exact-duplicate query: group archives sharing the same
-- algorithm + digest (see ArchiveHashRepository.duplicate_groups).
CREATE INDEX IF NOT EXISTS idx_archive_hashes_digest
    ON archive_hashes(algorithm, digest);
