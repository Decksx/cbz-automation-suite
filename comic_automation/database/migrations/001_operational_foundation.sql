CREATE TABLE IF NOT EXISTS application_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS processing_runs (
    id INTEGER PRIMARY KEY,
    run_type TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    host_name TEXT,
    details_json TEXT
);

CREATE TABLE IF NOT EXISTS processing_stages (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    stage_name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    details_json TEXT,
    FOREIGN KEY (run_id) REFERENCES processing_runs(id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS processing_items (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    archive_id INTEGER,
    status TEXT NOT NULL,
    current_stage TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES processing_runs(id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS source_batches (
    id INTEGER PRIMARY KEY,
    source_path TEXT NOT NULL,
    discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status TEXT NOT NULL,
    details_json TEXT
);

CREATE TABLE IF NOT EXISTS archive_files (
    id INTEGER PRIMARY KEY,
    sha256 TEXT UNIQUE,
    content_signature TEXT,
    file_size INTEGER NOT NULL,
    page_count INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS file_locations (
    id INTEGER PRIMARY KEY,
    archive_id INTEGER NOT NULL,
    path TEXT NOT NULL UNIQUE,
    is_current INTEGER NOT NULL DEFAULT 1,
    file_size INTEGER,
    modified_time_ns INTEGER,
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (archive_id) REFERENCES archive_files(id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS file_events (
    id INTEGER PRIMARY KEY,
    archive_id INTEGER,
    event_type TEXT NOT NULL,
    source_path TEXT,
    destination_path TEXT,
    occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    details_json TEXT,
    FOREIGN KEY (archive_id) REFERENCES archive_files(id)
        ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    priority INTEGER NOT NULL DEFAULT 100,
    archive_id INTEGER,
    payload_json TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    available_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    claimed_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    worker_id TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (archive_id) REFERENCES archive_files(id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_processing_stages_run
    ON processing_stages(run_id);

CREATE INDEX IF NOT EXISTS idx_processing_items_run
    ON processing_items(run_id);

CREATE INDEX IF NOT EXISTS idx_file_locations_archive
    ON file_locations(archive_id);

CREATE INDEX IF NOT EXISTS idx_file_events_archive
    ON file_events(archive_id);

CREATE INDEX IF NOT EXISTS idx_jobs_status_available
    ON jobs(status, available_at, priority);

CREATE INDEX IF NOT EXISTS idx_jobs_archive
    ON jobs(archive_id);
