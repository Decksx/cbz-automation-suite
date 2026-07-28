-- Migration 009: archive quarantine.
--
-- Tracks permanently-broken archives (corrupt_archive, and any future
-- non-retryable category) that have been explicitly approved for
-- remediation and physically moved out of the live library into a
-- designated quarantine folder, pending manual re-download.
--
-- This is deliberately a separate table rather than overloading
-- file_locations: file_locations models "where does this archive live
-- in the library right now", and a quarantined archive is no longer
-- part of the live library at all. The move itself is additionally
-- logged as a 'quarantined' row in file_events (source_path /
-- destination_path), consistent with how every other archive
-- relocation is audited.
CREATE TABLE IF NOT EXISTS archive_quarantine (
    id INTEGER PRIMARY KEY,
    archive_id INTEGER NOT NULL UNIQUE,
    source_path TEXT NOT NULL,
    quarantine_path TEXT NOT NULL,
    failure_category TEXT NOT NULL,
    job_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending_redownload',
    quarantined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT,
    notes TEXT,
    FOREIGN KEY (archive_id) REFERENCES archive_files(id)
        ON DELETE CASCADE,
    -- pending_redownload: sitting in the quarantine folder, waiting on
    --   the user to supply a working replacement.
    -- resolved: the user has dealt with it (redownloaded, or decided
    --   not to) and it can be dropped from active tracking views.
    -- abandoned: explicitly given up on; kept for history.
    CHECK (
        status IN ('pending_redownload', 'resolved', 'abandoned')
    )
);

-- Supports "what's still waiting on a redownload" and prevents an
-- already-quarantined archive from being selected as a candidate
-- again.
CREATE INDEX IF NOT EXISTS idx_archive_quarantine_status
    ON archive_quarantine(status);
