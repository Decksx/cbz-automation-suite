-- Migration 005: job failure categories.
--
-- Adds a failure_category column so failed jobs can be triaged without
-- parsing error_message text, and backfills it for jobs that failed
-- before this column existed.
ALTER TABLE jobs ADD COLUMN failure_category TEXT;

-- Backfill: classify already-failed inspect_archive jobs by matching
-- known error_message patterns. Anything that doesn't match a known
-- pattern falls back to 'legacy_unclassified' so it's still queryable
-- and visibly distinct from newly-categorized failures.
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

-- Supports the failure-review query (grouping/filtering failed jobs by
-- type and category) used by the CLI reports.
CREATE INDEX IF NOT EXISTS idx_jobs_failure_category
    ON jobs(job_type, status, failure_category);
