-- 001_initial_schema.sql

CREATE TABLE series (
    id INTEGER PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL UNIQUE,
    preferred_language TEXT,
    publisher TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','review','merged','archived')),
    routing_rule_ref TEXT,
    komga_series_id TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE series_aliases (
    id INTEGER PRIMARY KEY,
    series_id INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    alias_type TEXT NOT NULL DEFAULT 'observed'
        CHECK (alias_type IN ('observed','english','romaji','native','metadata','manual','source')),
    language TEXT,
    source TEXT,
    confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0.0 AND 1.0),
    is_preferred INTEGER NOT NULL DEFAULT 0 CHECK (is_preferred IN (0,1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(series_id, normalized_alias)
);

CREATE INDEX idx_series_aliases_normalized ON series_aliases(normalized_alias);

CREATE TABLE archives (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    series_id INTEGER REFERENCES series(id) ON DELETE SET NULL,
    normalized_series TEXT,
    number TEXT,
    volume TEXT,
    archive_hash TEXT,
    file_size INTEGER NOT NULL,
    file_mtime REAL NOT NULL,
    page_count INTEGER,
    comicinfo_status TEXT NOT NULL DEFAULT 'unknown',
    source_name TEXT,
    source_path TEXT,
    import_status TEXT NOT NULL DEFAULT 'discovered'
        CHECK (import_status IN ('discovered','staged','normalized','review','approved','imported','rejected','missing','error')),
    date_added TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_processed_at TEXT,
    deleted_at TEXT
);

CREATE INDEX idx_archives_series_number ON archives(series_id, number);
CREATE INDEX idx_archives_normalized_series ON archives(normalized_series, number);
CREATE INDEX idx_archives_hash ON archives(archive_hash);
CREATE INDEX idx_archives_import_status ON archives(import_status);

CREATE TABLE pages (
    id INTEGER PRIMARY KEY,
    archive_id INTEGER NOT NULL REFERENCES archives(id) ON DELETE CASCADE,
    page_index INTEGER NOT NULL,
    entry_name TEXT NOT NULL,
    sha256 TEXT,
    phash TEXT,
    dhash TEXT,
    width INTEGER,
    height INTEGER,
    file_size INTEGER,
    image_format TEXT,
    quality_score REAL,
    quality_method TEXT,
    clip_embedding BLOB,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(archive_id, page_index),
    UNIQUE(archive_id, entry_name)
);

CREATE INDEX idx_pages_archive ON pages(archive_id);
CREATE INDEX idx_pages_sha256 ON pages(sha256);
CREATE INDEX idx_pages_phash ON pages(phash);

CREATE TABLE processing_runs (
    id INTEGER PRIMARY KEY,
    tool_name TEXT NOT NULL,
    command_name TEXT,
    trigger_type TEXT NOT NULL DEFAULT 'manual'
        CHECK (trigger_type IN ('manual','watcher','scheduled','api','gui','test')),
    source_path TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0 CHECK (dry_run IN (0,1)),
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running','success','partial','failed','cancelled')),
    files_scanned INTEGER NOT NULL DEFAULT 0,
    files_touched INTEGER NOT NULL DEFAULT 0,
    files_failed INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT,
    error_message TEXT
);

CREATE TABLE archive_series_matches (
    id INTEGER PRIMARY KEY,
    archive_id INTEGER NOT NULL REFERENCES archives(id) ON DELETE CASCADE,
    candidate_series_id INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    match_method TEXT NOT NULL,
    score REAL NOT NULL CHECK (score BETWEEN 0.0 AND 1.0),
    reasons_json TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','accepted','rejected','superseded')),
    reviewed_by TEXT,
    reviewed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(archive_id, candidate_series_id, match_method)
);

CREATE INDEX idx_archive_series_matches_status
    ON archive_series_matches(status, score DESC);

CREATE TABLE dedupe_candidates (
    id INTEGER PRIMARY KEY,
    archive_a_id INTEGER NOT NULL REFERENCES archives(id) ON DELETE CASCADE,
    archive_b_id INTEGER NOT NULL REFERENCES archives(id) ON DELETE CASCADE,
    similarity_score REAL NOT NULL CHECK (similarity_score BETWEEN 0.0 AND 1.0),
    match_method TEXT NOT NULL,
    reasons_json TEXT,
    resolution_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (resolution_status IN ('pending','keep_a','keep_b','keep_both','merged','ignored','error')),
    resolved_by TEXT,
    resolved_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (archive_a_id < archive_b_id),
    UNIQUE(archive_a_id, archive_b_id, match_method)
);

CREATE TABLE quality_scores (
    id INTEGER PRIMARY KEY,
    archive_id INTEGER REFERENCES archives(id) ON DELETE CASCADE,
    page_id INTEGER REFERENCES pages(id) ON DELETE CASCADE,
    method TEXT NOT NULL,
    score REAL NOT NULL,
    details_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (archive_id IS NOT NULL AND page_id IS NULL)
        OR (archive_id IS NULL AND page_id IS NOT NULL)
    )
);

CREATE TABLE file_events (
    id INTEGER PRIMARY KEY,
    run_id INTEGER REFERENCES processing_runs(id) ON DELETE SET NULL,
    archive_id INTEGER REFERENCES archives(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    old_path TEXT,
    new_path TEXT,
    details_json TEXT,
    occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_file_events_archive ON file_events(archive_id);
CREATE INDEX idx_file_events_run ON file_events(run_id);

CREATE TABLE routing_log (
    id INTEGER PRIMARY KEY,
    run_id INTEGER REFERENCES processing_runs(id) ON DELETE SET NULL,
    archive_id INTEGER REFERENCES archives(id) ON DELETE SET NULL,
    rule_ref TEXT,
    rule_type TEXT,
    source_value TEXT,
    destination_path TEXT NOT NULL,
    matched INTEGER NOT NULL DEFAULT 1 CHECK (matched IN (0,1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE repair_log (
    id INTEGER PRIMARY KEY,
    run_id INTEGER REFERENCES processing_runs(id) ON DELETE SET NULL,
    archive_id INTEGER REFERENCES archives(id) ON DELETE SET NULL,
    tool_name TEXT NOT NULL,
    repair_type TEXT NOT NULL,
    field_name TEXT,
    old_value TEXT,
    new_value TEXT,
    outcome TEXT NOT NULL
        CHECK (outcome IN ('planned','applied','skipped','failed','reverted')),
    details_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE review_queue (
    id INTEGER PRIMARY KEY,
    review_type TEXT NOT NULL
        CHECK (review_type IN ('series_identity','possible_same_series','duplicate_archive','title_cleanup','metadata','quality','routing')),
    archive_id INTEGER REFERENCES archives(id) ON DELETE CASCADE,
    series_id INTEGER REFERENCES series(id) ON DELETE CASCADE,
    candidate_ref TEXT,
    suggested_action TEXT,
    confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0.0 AND 1.0),
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','approved','rejected','deferred','resolved','error')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TEXT,
    reviewed_by TEXT
);

CREATE INDEX idx_review_queue_status ON review_queue(status, review_type);

CREATE TABLE application_settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TRIGGER trg_series_updated_at
AFTER UPDATE ON series
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE series SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;
