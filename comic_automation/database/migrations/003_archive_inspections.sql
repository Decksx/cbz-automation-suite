-- Migration 003: archive inspections.
--
-- Stores the read-only structural inspection result for each archive:
-- format, page/entry/directory counts, ComicInfo.xml presence and
-- validity, and whether the archive was encrypted or CRC-verified.
-- One row per archive_id (see archive_id UNIQUE below); re-inspecting an
-- archive overwrites the row via the ON CONFLICT upsert in
-- ArchiveInspectionRepository.save (comic_automation/archive/repository.py)
-- rather than accumulating history.
CREATE TABLE IF NOT EXISTS archive_inspections (
    id INTEGER PRIMARY KEY,
    archive_id INTEGER NOT NULL UNIQUE,
    location_id INTEGER,
    inspected_path TEXT NOT NULL,
    archive_format TEXT NOT NULL,
    status TEXT NOT NULL,
    entry_count INTEGER NOT NULL,
    page_count INTEGER NOT NULL,
    directory_count INTEGER NOT NULL,
    encrypted INTEGER NOT NULL DEFAULT 0,
    comic_info_present INTEGER NOT NULL DEFAULT 0,
    comic_info_valid INTEGER NOT NULL DEFAULT 0,
    comic_info_error TEXT,
    comic_info_json TEXT,
    crc_verified INTEGER NOT NULL DEFAULT 0,
    inspected_file_size INTEGER,
    inspected_modified_time_ns INTEGER,
    result_json TEXT NOT NULL,
    inspected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (archive_id) REFERENCES archive_files(id)
        ON DELETE CASCADE,
    FOREIGN KEY (location_id) REFERENCES file_locations(id)
        ON DELETE SET NULL
);

-- Supports the status-breakdown query used by the CLI/report tooling
-- (for example counting how many archives are "ok" vs "corrupt").
CREATE INDEX IF NOT EXISTS idx_archive_inspections_status
    ON archive_inspections(status);

-- Supports looking up an inspection result by the path it was inspected
-- at, independent of archive_id.
CREATE INDEX IF NOT EXISTS idx_archive_inspections_path
    ON archive_inspections(inspected_path);
