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

CREATE INDEX IF NOT EXISTS idx_archive_hashes_digest
    ON archive_hashes(algorithm, digest);
