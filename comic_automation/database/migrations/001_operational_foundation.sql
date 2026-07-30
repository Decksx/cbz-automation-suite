-- Migration 001: operational foundation.
--
-- Establishes the core tables the SQLite-backed service uses to track
-- processing runs, archive identity, filesystem locations, the
-- append-only event log, and the persistent job queue. This is the
-- foundation every later migration builds on.

-- Generic key/value store for service-wide settings that need to be
-- readable/writable at runtime, as opposed to static TOML config
-- (see comic_automation/config.py).
CREATE TABLE IF NOT EXISTS application_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- A processing_run is one top-level invocation of a maintenance or
-- discovery workflow (for example "scan the library" or "sanitize
-- a batch"). Stages and items below hang off of a run.
CREATE TABLE IF NOT EXISTS processing_runs (
    id INTEGER PRIMARY KEY,
    run_type TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    host_name TEXT,
    details_json TEXT
);

-- A processing_stage is one named phase within a run (for example
-- "sanitize" or "organize"). Deleting the parent run cascades to its
-- stages.
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

-- A processing_item tracks the status of a single archive as it moves
-- through the stages of a run.
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

-- A source_batch is one directory-tree scan of an incoming or library
-- root. discovery_checkpoints (added in migration 002) track resumable
-- progress within a batch.
CREATE TABLE IF NOT EXISTS source_batches (
    id INTEGER PRIMARY KEY,
    source_path TEXT NOT NULL,
    discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status TEXT NOT NULL,
    details_json TEXT
);

-- archive_files is the canonical identity for a single comic archive,
-- independent of where it currently lives on disk. sha256 and
-- content_signature are populated later by the hashing stages, not at
-- discovery time.
CREATE TABLE IF NOT EXISTS archive_files (
    id INTEGER PRIMARY KEY,
    sha256 TEXT UNIQUE,
    content_signature TEXT,
    file_size INTEGER NOT NULL,
    page_count INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- file_locations records every filesystem path an archive has been seen
-- at. is_current=1 marks the path that currently exists; history rows
-- are kept rather than overwritten so moves and renames stay auditable.
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

-- file_events is an append-only audit log of what happened to an
-- archive (discovered, changed, restored, missing, moved, and so on).
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

-- jobs is the persistent work queue that every background stage
-- (inspection, hashing, perceptual hashing, near-duplicate detection,
-- ...) enqueues into and claims from. See
-- comic_automation/jobs/queue.py for the claim/complete/fail state
-- machine that operates on this table.
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

-- Supports listing all stages that belong to a run.
CREATE INDEX IF NOT EXISTS idx_processing_stages_run
    ON processing_stages(run_id);

-- Supports listing all items that belong to a run.
CREATE INDEX IF NOT EXISTS idx_processing_items_run
    ON processing_items(run_id);

-- Supports finding all recorded locations for a given archive.
CREATE INDEX IF NOT EXISTS idx_file_locations_archive
    ON file_locations(archive_id);

-- Supports the audit-log lookup "what has happened to this archive".
CREATE INDEX IF NOT EXISTS idx_file_events_archive
    ON file_events(archive_id);

-- Supports the job-queue claim query: find the highest-priority
-- pending job whose available_at has passed
-- (see JobQueue.claim_next in comic_automation/jobs/queue.py).
CREATE INDEX IF NOT EXISTS idx_jobs_status_available
    ON jobs(status, available_at, priority);

-- Supports "does this archive already have a job of a given type"
-- existence checks used before enqueueing duplicate work.
CREATE INDEX IF NOT EXISTS idx_jobs_archive
    ON jobs(archive_id);
