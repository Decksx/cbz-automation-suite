ALTER TABLE jobs ADD COLUMN failure_category TEXT;

UPDATE jobs
SET failure_category = CASE
    WHEN error_message LIKE 'Invalid or corrupt CBZ archive:%'
        THEN 'corrupt_archive'
    WHEN error_message LIKE 'Unsupported archive format:%'
        THEN 'unsupported_archive_format'
    WHEN error_message GLOB '[A-Za-z]:\*'
        THEN 'filesystem_not_found'
    ELSE 'legacy_unclassified'
END
WHERE job_type = 'inspect_archive'
  AND status = 'failed'
  AND failure_category IS NULL;

CREATE INDEX IF NOT EXISTS idx_jobs_failure_category
    ON jobs(job_type, status, failure_category);
