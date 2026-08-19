-- Migration 012: durable archive retirement.
--
-- Records that an archive is permanently out of scope for automated work,
-- as a fact about the archive rather than a fact about one of its jobs.
--
-- Why this is not the same as cancelling a job. Migration 011 added
-- JobQueue.cancel(), which retired archive 45217's blocked perceptual
-- job. That closed the job correctly, but retirement did not survive it:
-- the eligible-archive predicate excludes archives holding an *active*
-- job, and 'cancelled' is terminal, so cancelling the job returned the
-- archive to the eligible set. Measured on 2026-08-18, eligibility rose
-- from 12,554 to 12,555 for exactly that reason.
--
-- A live-path existence check would hide the problem rather than fix it.
-- It keeps 45217 out today because its file is absent, but retirement
-- would then be an accident of the filesystem: restore the file, re-sync
-- it, or rename something back, and a deliberately retired archive
-- silently returns to the queue. Retirement has to be storable, so this
-- table stores it.
--
-- Shape:
--
--   * archive_id is the PRIMARY KEY, so an archive is retired or it is
--     not -- there is no second, contradictory retirement to reconcile.
--     Re-retiring is therefore a rejected INSERT rather than a silent
--     overwrite of the first reason, matching how JobQueue.cancel()
--     refuses a second cancellation.
--   * reason is NOT NULL and CHECKed non-blank at the database level, not
--     only in application code. A retirement with no reason is
--     indistinguishable later from a mistake, and this table exists
--     precisely for records a future reader will question.
--
--     The CHECK passes an explicit character set to trim(). SQLite's
--     one-argument trim() strips spaces and nothing else, so
--     `length(trim(reason)) > 0` accepts a lone tab or newline as a
--     reason -- which is blank to every reader but not to the database.
--     A test caught that; the character set closes it.
--   * evidence is free-form and optional, for the digests, paths, and
--     surviving-copy identifiers that justify the decision. It is
--     deliberately not parsed by anything: it is there to be read by a
--     person deciding whether to reverse the retirement.
--   * ON DELETE CASCADE matches every other archive-scoped table here, so
--     a retirement cannot outlive the archive it describes.
--
-- Un-retiring is a DELETE, performed deliberately by an operator. There is
-- no application path to it, because nothing so far needs one and a
-- reversal deserves the same scrutiny the retirement got.
--
-- Additive and non-destructive: one new table, one index, no existing row
-- touched and no backfill. Every archive starts un-retired, which is
-- correct -- nothing has ever been retired at archive level.
-- apply_migrations() wraps each file in BEGIN IMMEDIATE ... COMMIT, so a
-- failure rolls back without recording version 12.
CREATE TABLE IF NOT EXISTS archive_retirements (
    archive_id INTEGER PRIMARY KEY,
    retired_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reason TEXT NOT NULL CHECK (
        length(
            trim(reason, char(32) || char(9) || char(10) || char(13))
        ) > 0
    ),
    evidence TEXT,
    FOREIGN KEY (archive_id) REFERENCES archive_files(id)
        ON DELETE CASCADE
);

-- Supports "when was this retired" and chronological review of
-- retirements; the archive_id lookup is already served by the primary key.
CREATE INDEX IF NOT EXISTS idx_archive_retirements_retired_at
    ON archive_retirements(retired_at);
