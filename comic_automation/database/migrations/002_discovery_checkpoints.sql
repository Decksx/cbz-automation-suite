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

CREATE INDEX IF NOT EXISTS idx_discovery_checkpoints_source
    ON discovery_checkpoints(source_path, updated_at);
