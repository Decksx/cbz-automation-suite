-- Migration 007: archive page hashes.
--
-- Adds the per-page inventory and exact page-content hashing tables
-- (phase 3: ordered exact page hashes, one level more granular than the
-- whole-archive hash in migration 006). See
-- comic_automation/archive/page_hashing.py for the code that populates
-- these tables.

-- archive_pages inventories every image entry inside an archive, in
-- natural reading order (page_index), independent of hashing.
CREATE TABLE IF NOT EXISTS archive_pages (
    id INTEGER PRIMARY KEY,
    archive_id INTEGER NOT NULL,
    location_id INTEGER,
    page_index INTEGER NOT NULL,
    entry_name TEXT NOT NULL,
    entry_size INTEGER NOT NULL,
    compressed_size INTEGER NOT NULL,
    crc32 INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (archive_id) REFERENCES archive_files(id)
        ON DELETE CASCADE,
    FOREIGN KEY (location_id) REFERENCES file_locations(id)
        ON DELETE SET NULL,
    UNIQUE (archive_id, page_index)
);

-- page_hashes stores one digest per (page, algorithm, algorithm_version)
-- triple. Reused later by perceptual hashing (migration 008 / dhash and
-- phash rows), not just the exact sha256 content hash.
CREATE TABLE IF NOT EXISTS page_hashes (
    id INTEGER PRIMARY KEY,
    page_id INTEGER NOT NULL,
    algorithm TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    digest TEXT NOT NULL,
    bytes_read INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (page_id) REFERENCES archive_pages(id)
        ON DELETE CASCADE,
    UNIQUE (page_id, algorithm, algorithm_version)
);

-- archive_content_signatures is a single digest over the ordered
-- sequence of page hashes for an archive (see calculate_page_hashes),
-- letting two archives be compared for exact content equality without
-- comparing every page individually.
CREATE TABLE IF NOT EXISTS archive_content_signatures (
    id INTEGER PRIMARY KEY,
    archive_id INTEGER NOT NULL UNIQUE,
    location_id INTEGER,
    algorithm TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    digest TEXT NOT NULL,
    page_count INTEGER NOT NULL,
    image_bytes INTEGER NOT NULL,
    source_file_size INTEGER NOT NULL,
    source_modified_time_ns INTEGER NOT NULL,
    calculated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (archive_id) REFERENCES archive_files(id)
        ON DELETE CASCADE,
    FOREIGN KEY (location_id) REFERENCES file_locations(id)
        ON DELETE SET NULL
);

-- Supports iterating an archive's pages in reading order.
CREATE INDEX IF NOT EXISTS idx_archive_pages_archive
    ON archive_pages(archive_id, page_index);

-- Supports the per-page exact-duplicate lookup: find other pages
-- sharing the same algorithm/version/digest.
CREATE INDEX IF NOT EXISTS idx_page_hashes_digest
    ON page_hashes(algorithm, algorithm_version, digest);

-- Supports the whole-archive exact-duplicate lookup
-- (see ArchivePageHashRepository.duplicate_content_groups), including
-- page_count so archives are only grouped when both digest and length
-- agree.
CREATE INDEX IF NOT EXISTS idx_content_signatures_digest
    ON archive_content_signatures(
        algorithm,
        algorithm_version,
        digest,
        page_count
    );
