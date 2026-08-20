-- Migration 013: explicit supersession, durable disposition history.
--
-- Records that one archive identity has been replaced by another, as a
-- decision about the archives rather than an observation about the disk.
--
-- Why this is not retirement. Migration 012 stores "this archive is out of
-- scope". Supersession stores something different: "the work continues under
-- another identity". Both remove an archive from operational scope, but only
-- one of them names a successor, and conflating them would make it impossible
-- to answer "where did this go?" later. Measured on 2026-08-20: 12 archives
-- hold a page inventory for bytes that now live under a different archive_id,
-- created when a reclassification moved the file and discovery -- which keys
-- identity on path -- minted a second identity for the same bytes. Those 12
-- are superseded, not retired.
--
-- Why this is not a revision. The roadmap's Step 2 introduces
-- archive_revisions for byte-level content states *within* one logical
-- archive. Supersession relates two *identities*. Archive 37704 needs both:
-- three byte-generations of one work (revisions) recorded across two archive
-- identities (supersession). They are not substitutes and this table does not
-- pre-empt Step 2.
--
-- Shape and the reasoning behind each choice:
--
--   * predecessor_archive_id is the PRIMARY KEY, so an archive is superseded
--     or it is not. A second supersession is a rejected INSERT rather than a
--     silent overwrite of the first reason, matching archive_retirements.
--     Out-degree is therefore capped at one.
--
--   * successor_archive_id is deliberately NOT unique -- only indexed. One
--     successor absorbing several predecessors is the natural shape of a
--     reclassification that folds several chapters into one re-discovered
--     identity, and a model that forbade it would be wrong. In-degree is
--     unbounded; out-degree is one. The asymmetry is intentional.
--
--   * Both foreign keys are ON DELETE RESTRICT, not CASCADE. Every other
--     archive-scoped table here cascades, because it holds *derived* data that
--     is meaningless without its archive. A disposition is not derived data --
--     it is the record of a decision, and the evidence for it must not vanish
--     because someone deleted a row. RESTRICT means an archive that
--     participates in a supersession cannot be deleted at all until the
--     supersession is explicitly reversed, which is the correct order of
--     operations.
--
--   * reason and evidence are both NOT NULL and both CHECKed non-blank.
--     Migration 012 left evidence optional; supersession is a claim that
--     specific bytes live somewhere else, and a claim of that kind without
--     proof is not reviewable later. The CHECK passes an explicit character
--     set to trim(), because SQLite's one-argument trim() strips spaces only
--     and would accept a lone tab or newline as a reason. That exact hole was
--     found by a test on migration 012; the form is reused rather than
--     re-derived.
--
-- Cycle prevention is enforced here, in the database, not only by the writer.
-- Because the primary key caps out-degree at one, the supersession graph is a
-- functional graph and a cycle is reachable by walking successors. A CHECK
-- cannot traverse a table, but a recursive CTE in a trigger's WHEN clause can,
-- and UNION (not UNION ALL) makes the walk terminate even if a cycle already
-- existed. The trigger below rejects self-links, two-node cycles, and longer
-- cycles identically, and it fires for raw SQL exactly as it does for the
-- application writer. The redundant CHECK on self-supersession is kept as the
-- cheaper first line of defence.
--
-- Retirement interaction is closed in both directions and both orders. Four
-- conflicts are rejected:
--     superseding a retired predecessor;
--     superseding into a retired successor;
--     retiring an archive that is already a superseded predecessor;
--     retiring an archive that is still the successor of live predecessors.
-- The last one matters most: retiring a successor would point live
-- supersessions at an identity declared out of scope, which is the failure
-- migration 012 was written to prevent, one level up.
--
-- Relationship rows are immutable. A BEFORE UPDATE trigger aborts every UPDATE
-- on both disposition tables, so there is no path by which an UPDATE can
-- bypass an INSERT-time check. Changing a successor is delete-then-insert:
-- two visible acts, each with its own reason, rather than one invisible one.
--
-- History is written by trigger, never by application code, so it cannot be
-- skipped and is atomic with the row it describes by construction -- the same
-- statement writes both. Exactly one history row is produced per action.
--
-- Reversal requires its own reason, enforced by the database. A DELETE trigger
-- cannot be handed an argument, so the reason arrives through a single-row
-- context table that the reversing transaction populates first; the BEFORE
-- DELETE trigger refuses any deletion whose context does not match the exact
-- row being removed, which also prevents a stale context from silently
-- labelling the next reversal.
--
-- archive_disposition_events carries NO foreign key, deliberately. Requirement:
-- disposition evidence must survive the deletion of the archive it describes.
-- A foreign key would either cascade the history away or restrict the delete
-- forever. file_events was considered and rejected for this: its archive_id is
-- ON DELETE SET NULL, so it cannot be the durable home for decision history.
--
-- Backfill: exactly one row, the retirement of archive 45217 on 2026-08-19,
-- reconstructed from the archive_retirements row that already exists and
-- marked source = 'migration_backfill' so it is never mistaken for a
-- contemporaneous record. Nothing else is written, no existing row is altered,
-- and no archive is retired or superseded by this migration.
--
-- Additive and non-destructive. apply_migrations() wraps the file in
-- BEGIN IMMEDIATE ... COMMIT, so a failure rolls back without recording
-- version 13 and without leaving any object behind.

-- ---------------------------------------------------------------- history
CREATE TABLE IF NOT EXISTS archive_disposition_events (
    id INTEGER PRIMARY KEY,
    archive_id INTEGER NOT NULL,
    counterpart_archive_id INTEGER,
    disposition TEXT NOT NULL CHECK (
        disposition IN ('retired', 'superseded')
    ),
    action TEXT NOT NULL CHECK (action IN ('recorded', 'reversed')),
    reason TEXT NOT NULL,
    evidence TEXT,
    source TEXT NOT NULL DEFAULT 'application' CHECK (
        source IN ('application', 'migration_backfill')
    ),
    occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_archive_disposition_events_archive
    ON archive_disposition_events(archive_id, id);

CREATE INDEX IF NOT EXISTS idx_archive_disposition_events_occurred_at
    ON archive_disposition_events(occurred_at);

-- -------------------------------------------------------- reversal context
CREATE TABLE IF NOT EXISTS disposition_reversal_context (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    archive_id INTEGER NOT NULL,
    disposition TEXT NOT NULL CHECK (
        disposition IN ('retired', 'superseded')
    ),
    reason TEXT NOT NULL CHECK (
        length(
            trim(reason, char(32) || char(9) || char(10) || char(13))
        ) > 0
    )
);

-- ----------------------------------------------------------- supersessions
CREATE TABLE IF NOT EXISTS archive_supersessions (
    predecessor_archive_id INTEGER PRIMARY KEY,
    successor_archive_id INTEGER NOT NULL,
    superseded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reason TEXT NOT NULL CHECK (
        length(
            trim(reason, char(32) || char(9) || char(10) || char(13))
        ) > 0
    ),
    evidence TEXT NOT NULL CHECK (
        length(
            trim(evidence, char(32) || char(9) || char(10) || char(13))
        ) > 0
    ),
    CHECK (successor_archive_id <> predecessor_archive_id),
    FOREIGN KEY (predecessor_archive_id) REFERENCES archive_files(id)
        ON DELETE RESTRICT,
    FOREIGN KEY (successor_archive_id) REFERENCES archive_files(id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_archive_supersessions_successor
    ON archive_supersessions(successor_archive_id);

CREATE INDEX IF NOT EXISTS idx_archive_supersessions_superseded_at
    ON archive_supersessions(superseded_at);

-- ------------------------------------------------- supersession invariants
CREATE TRIGGER IF NOT EXISTS trg_supersession_no_cycle
BEFORE INSERT ON archive_supersessions
FOR EACH ROW
WHEN EXISTS (
    WITH RECURSIVE chain(node) AS (
        SELECT NEW.successor_archive_id
        UNION
        SELECT s.successor_archive_id
          FROM archive_supersessions AS s
          JOIN chain AS c ON s.predecessor_archive_id = c.node
    )
    SELECT 1 FROM chain WHERE node = NEW.predecessor_archive_id
)
BEGIN
    SELECT RAISE(
        ABORT,
        'supersession would create a cycle in the successor chain'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_supersession_predecessor_not_retired
BEFORE INSERT ON archive_supersessions
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM archive_retirements AS r
    WHERE r.archive_id = NEW.predecessor_archive_id
)
BEGIN
    SELECT RAISE(ABORT, 'cannot supersede a retired archive');
END;

CREATE TRIGGER IF NOT EXISTS trg_supersession_successor_not_retired
BEFORE INSERT ON archive_supersessions
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM archive_retirements AS r
    WHERE r.archive_id = NEW.successor_archive_id
)
BEGIN
    SELECT RAISE(ABORT, 'cannot supersede into a retired successor');
END;

CREATE TRIGGER IF NOT EXISTS trg_supersession_immutable
BEFORE UPDATE ON archive_supersessions
FOR EACH ROW
BEGIN
    SELECT RAISE(
        ABORT,
        'archive_supersessions rows are immutable; reverse and re-record'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_supersession_reversal_needs_reason
BEFORE DELETE ON archive_supersessions
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM disposition_reversal_context AS ctx
    WHERE ctx.id = 1
      AND ctx.disposition = 'superseded'
      AND ctx.archive_id = OLD.predecessor_archive_id
)
BEGIN
    SELECT RAISE(
        ABORT,
        'reversing a supersession requires a matching reversal reason'
    );
END;

-- --------------------------------------------------- retirement invariants
CREATE TRIGGER IF NOT EXISTS trg_retirement_requires_evidence
BEFORE INSERT ON archive_retirements
FOR EACH ROW
WHEN NEW.evidence IS NULL
  OR length(
        trim(NEW.evidence, char(32) || char(9) || char(10) || char(13))
     ) = 0
BEGIN
    SELECT RAISE(ABORT, 'retirement requires non-blank evidence');
END;

CREATE TRIGGER IF NOT EXISTS trg_retirement_not_superseded_predecessor
BEFORE INSERT ON archive_retirements
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM archive_supersessions AS s
    WHERE s.predecessor_archive_id = NEW.archive_id
)
BEGIN
    SELECT RAISE(ABORT, 'cannot retire an archive that is superseded');
END;

CREATE TRIGGER IF NOT EXISTS trg_retirement_not_live_successor
BEFORE INSERT ON archive_retirements
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM archive_supersessions AS s
    WHERE s.successor_archive_id = NEW.archive_id
)
BEGIN
    SELECT RAISE(
        ABORT,
        'cannot retire an archive that other archives are superseded into'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_retirement_immutable
BEFORE UPDATE ON archive_retirements
FOR EACH ROW
BEGIN
    SELECT RAISE(
        ABORT,
        'archive_retirements rows are immutable; reverse and re-record'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_retirement_reversal_needs_reason
BEFORE DELETE ON archive_retirements
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM disposition_reversal_context AS ctx
    WHERE ctx.id = 1
      AND ctx.disposition = 'retired'
      AND ctx.archive_id = OLD.archive_id
)
BEGIN
    SELECT RAISE(
        ABORT,
        'reversing a retirement requires a matching reversal reason'
    );
END;

-- ------------------------------------------------------------ history taps
CREATE TRIGGER IF NOT EXISTS trg_supersession_recorded_history
AFTER INSERT ON archive_supersessions
FOR EACH ROW
BEGIN
    INSERT INTO archive_disposition_events (
        archive_id, counterpart_archive_id, disposition, action,
        reason, evidence, source
    )
    VALUES (
        NEW.predecessor_archive_id, NEW.successor_archive_id,
        'superseded', 'recorded', NEW.reason, NEW.evidence, 'application'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_supersession_reversed_history
AFTER DELETE ON archive_supersessions
FOR EACH ROW
BEGIN
    INSERT INTO archive_disposition_events (
        archive_id, counterpart_archive_id, disposition, action,
        reason, evidence, source
    )
    VALUES (
        OLD.predecessor_archive_id, OLD.successor_archive_id,
        'superseded', 'reversed',
        (SELECT reason FROM disposition_reversal_context WHERE id = 1),
        OLD.evidence, 'application'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_retirement_recorded_history
AFTER INSERT ON archive_retirements
FOR EACH ROW
BEGIN
    INSERT INTO archive_disposition_events (
        archive_id, counterpart_archive_id, disposition, action,
        reason, evidence, source
    )
    VALUES (
        NEW.archive_id, NULL, 'retired', 'recorded',
        NEW.reason, NEW.evidence, 'application'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_retirement_reversed_history
AFTER DELETE ON archive_retirements
FOR EACH ROW
BEGIN
    INSERT INTO archive_disposition_events (
        archive_id, counterpart_archive_id, disposition, action,
        reason, evidence, source
    )
    VALUES (
        OLD.archive_id, NULL, 'retired', 'reversed',
        (SELECT reason FROM disposition_reversal_context WHERE id = 1),
        OLD.evidence, 'application'
    );
END;

-- ---------------------------------------------------------------- backfill
-- The one retirement this database already holds was recorded before any
-- history mechanism existed, so it has no event. Reconstruct it from the
-- retirement row itself -- never from an assumption -- and mark it as
-- backfilled so a reader can tell it apart from a contemporaneous record.
-- Guarded so re-running against a database that already has the event, or one
-- with no retirements at all, is a no-op.
INSERT INTO archive_disposition_events (
    archive_id, counterpart_archive_id, disposition, action,
    reason, evidence, source, occurred_at
)
SELECT
    r.archive_id, NULL, 'retired', 'recorded',
    r.reason, r.evidence, 'migration_backfill', r.retired_at
FROM archive_retirements AS r
WHERE NOT EXISTS (
    SELECT 1 FROM archive_disposition_events AS e
    WHERE e.archive_id = r.archive_id
      AND e.disposition = 'retired'
      AND e.action = 'recorded'
);
