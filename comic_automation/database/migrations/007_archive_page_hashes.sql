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

CREATE INDEX IF NOT EXISTS idx_archive_pages_archive
    ON archive_pages(archive_id, page_index);

CREATE INDEX IF NOT EXISTS idx_page_hashes_digest
    ON page_hashes(algorithm, algorithm_version, digest);

CREATE INDEX IF NOT EXISTS idx_content_signatures_digest
    ON archive_content_signatures(
        algorithm,
        algorithm_version,
        digest,
        page_count
    );
