-- Migration 014: immutable archive revisions.
--
-- Separates stable logical identity (archive_files) from observed byte-level
-- content state (archive_revisions), which is roadmap Step 2.
--
-- A revision is one unique byte state of one logical archive. It is NOT an
-- observation event: if previously seen bytes reappear, the existing revision
-- is reused and a new observation is recorded against it. That is the whole
-- reason observations live in their own table below -- it is what lets a
-- revision be immutable while paths, sightings and run attribution keep
-- changing underneath it.
--
-- Why this is not supersession. Migration 013 relates two *identities*: the
-- work continues under a different archive_id. A revision relates two *byte
-- states of one identity*. Archive 37704 is precisely why both exist: it
-- carries three byte generations of one work, which are three revisions of one
-- archive_id, and re-inspecting it in place would overwrite one generation
-- with another and destroy the evidence that they are distinct.
--
-- Archive 58201 is the related revision-semantics case and is deliberately NOT
-- modelled the same way: its current location metadata matches its signature,
-- so it never appears in a drift query. 37704 and 58201 share a historical
-- digest, and that shared digest is exactly the trap -- merging them on it
-- would collapse a supersession relationship and a revision relationship into
-- one wrong edge. The schema keeps them apart structurally: revisions cannot
-- span archive_ids (the lineage foreign key below is composite), and
-- supersessions cannot express a byte generation (they carry no digest at
-- all).
--
-- ---------------------------------------------------------------- identity
--
-- Revision identity is `archive_sha256`, and it is nullable. That is not
-- laxity; it is a measured fact about the population this migration has to
-- run over. Reconciled 2026-08-21 against the protected pre-revision backup:
--
--     logical archive rows                       59,688
--     archive-level SHA-256 rows (archive_hashes) 59,541
--     archive content signatures                  58,437
--     current file locations (is_current = 1)     59,377
--
-- 147 archives have never been hashed, and because 311 archives have no
-- current location at all, some of those bytes may be permanently unreachable
-- and can never acquire a digest. A NOT NULL column would therefore either
-- abort this migration forever, or silently leave 147 archives with no
-- revision and a NULL current pointer -- which breaks the Step 2 acceptance
-- criterion that *every* archive has exactly one deterministic current
-- revision.
--
-- So "identity not established" is modelled as a state rather than as a NULL
-- to be remembered. `identity_state` is 'established' (digest known) or
-- 'provisional' (digest not known), the CHECK below ties it to the presence of
-- the digest in both directions, and a partial unique index allows at most one
-- provisional revision per archive. When the bytes are finally hashed, an
-- established revision is *appended* after the provisional one and the current
-- pointer moves to it. The provisional row is kept as noncurrent history,
-- because "the identity existed and its bytes were unknown, from this date
-- until that one" is a fact worth being able to answer later -- and because
-- deleting it would cascade away every observation recorded against it.
--
-- 59,541 established + 147 provisional = 59,688, so every archive gets a
-- current revision and the gap stays queryable instead of becoming folklore.
--
-- ------------------------------------------------------------ shape choices
--
--   * `UNIQUE (archive_id, archive_sha256)` -- the same logical archive cannot
--     accumulate two rows for the same byte state. Note SQLite treats NULLs as
--     distinct in a UNIQUE constraint, so this does nothing for provisional
--     rows; the partial index below is what caps those at one.
--
--   * `archive_sha256` is indexed but deliberately NOT globally unique. Two
--     different archives holding byte-identical content is a real and expected
--     state -- 888 exact-duplicate groups were measured on 2026-08-21 -- and
--     they must stay separately addressable. Canonical-copy selection is a
--     later guarded resolution action, not a schema constraint.
--
--   * `UNIQUE (id, archive_id)` looks redundant against the primary key, and
--     as a constraint it is. It exists so the composite lineage foreign key
--     below has a unique parent key to point at, which SQLite requires.
--
--   * lineage is `(previous_revision_id, archive_id) REFERENCES
--     archive_revisions(id, archive_id)`. Carrying archive_id into the foreign
--     key structurally prevents a revision chain from wandering into another
--     archive -- the 37704/58201 failure, expressed as a constraint rather
--     than as a rule someone has to remember.
--
--     This overlaps with trg_archive_revisions_lineage_is_sequential below,
--     which also compares archive_id. Removing either one alone leaves
--     cross-archive lineage refused by the other, so neither fails a test on
--     its own; removing both opens it. That is deliberate defence in depth
--     and not an accident, but it is written down because a bypass run
--     showed each looking individually redundant, and an undocumented
--     overlap is how one of them gets deleted as dead weight later.
--
--   * `evidence` is NOT NULL and CHECKed non-blank, matching migrations 012
--     and 013. A revision asserts that specific bytes existed; that claim
--     without proof is not reviewable later. The CHECK passes an explicit
--     character set to trim(), because SQLite's one-argument trim() strips
--     spaces only and would accept a lone tab as evidence. That hole was found
--     by a test on migration 012 and the form is reused rather than re-derived.
--
-- ------------------------------------------------- the current pointer
--
-- `archive_files.current_revision_id` is the sole authoritative pointer. There
-- is deliberately no `is_current` column on archive_revisions: two sources of
-- truth for the same fact is how they drift apart.
--
-- Every archive has one, and that is enforced for the whole life of the
-- schema rather than only at backfill time. The column is nullable and always
-- will be -- an archive's first revision cannot exist before the archive row
-- it references, so there is no way to satisfy a NOT NULL column on INSERT.
-- The invariant is closed at both ends instead:
--
--   * an AFTER INSERT trigger gives every new archive an initial provisional
--     revision and points it there, so the NULL window closes inside the same
--     statement and applies to raw SQL exactly as it does to the DAL;
--   * a BEFORE UPDATE trigger refuses to clear a live archive's pointer back
--     to NULL.
--
-- Without both, an archive could be inserted and committed with no current
-- revision at all -- which the backfill alone cannot prevent, because it runs
-- once and every archive discovered afterwards arrives through an INSERT it
-- never sees.
--
-- It is added with ALTER TABLE, which in SQLite cannot express a composite
-- foreign key -- so the requirement that a pointer names a revision *of its
-- own archive* is enforced by triggers instead, which the roadmap allows as
-- "or equivalent". Rebuilding archive_files to get a real composite key was
-- considered and rejected: apply_migrations() runs each file inside
-- BEGIN IMMEDIATE, `PRAGMA foreign_keys` is a no-op inside a transaction, and
-- dropping archive_files with foreign keys enabled would fire ON DELETE
-- CASCADE across every child table. That is a data-destroying rebuild to buy a
-- constraint two triggers already provide.
--
-- The foreign key is deliberately NOT deferrable. A deferred constraint was
-- written first, on the reasoning that deleting an archive leaves the row
-- pointing at a revision the cascade is removing. Measured 2026-08-24: the
-- deletion succeeds either way, because SQLite settles the parent delete and
-- its cascade together. The deferral bought nothing and cost something --
-- violations would surface at COMMIT instead of at the offending statement --
-- so it is gone. Recorded here because the original comment claimed the
-- opposite and a bypass run is what caught it.
--
-- ---------------------------------------------------------------- backfill
--
-- Every existing archive receives exactly one initial revision at ordinal 1
-- with no predecessor, marked source = 'migration_backfill' so it is never
-- mistaken for a contemporaneous record. No archive identities are merged, no
-- existing row is altered, and the two source tables are joined one-to-one --
-- both archive_hashes.archive_id and archive_content_signatures.archive_id are
-- UNIQUE, so neither join can fan out and mint duplicate revisions.
--
-- Additive and non-destructive. apply_migrations() wraps the file in
-- BEGIN IMMEDIATE ... COMMIT, so any failure rolls back without recording
-- version 14 and without leaving an object behind.

-- --------------------------------------------------------------- revisions
CREATE TABLE IF NOT EXISTS archive_revisions (
    id INTEGER PRIMARY KEY,
    archive_id INTEGER NOT NULL,
    revision_ordinal INTEGER NOT NULL CHECK (revision_ordinal >= 1),
    identity_state TEXT NOT NULL DEFAULT 'established' CHECK (
        identity_state IN ('established', 'provisional')
    ),
    archive_sha256 TEXT,
    content_signature TEXT,
    file_size INTEGER,
    page_count INTEGER,
    previous_revision_id INTEGER,
    evidence TEXT NOT NULL CHECK (
        length(
            trim(evidence, char(32) || char(9) || char(10) || char(13))
        ) > 0
    ),
    source TEXT NOT NULL DEFAULT 'runtime' CHECK (
        source IN ('runtime', 'migration_backfill')
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Ties the state to the data in both directions, so 'established' can
    -- never mean "we lost the digest" and 'provisional' can never quietly
    -- carry one.
    CHECK (
        (identity_state = 'established' AND archive_sha256 IS NOT NULL)
        OR (identity_state = 'provisional' AND archive_sha256 IS NULL)
    ),

    -- The first revision of an archive has no predecessor and every later one
    -- does. Without this, a chain could start at ordinal 3 or a second root
    -- could appear, and "three generations" would stop meaning a sequence.
    CHECK (
        (revision_ordinal = 1 AND previous_revision_id IS NULL)
        OR (revision_ordinal > 1 AND previous_revision_id IS NOT NULL)
    ),
    CHECK (previous_revision_id IS NULL OR previous_revision_id <> id),

    UNIQUE (archive_id, archive_sha256),
    UNIQUE (archive_id, revision_ordinal),
    UNIQUE (id, archive_id),

    FOREIGN KEY (archive_id) REFERENCES archive_files(id)
        ON DELETE CASCADE,
    -- NO ACTION (the default), made DEFERRABLE INITIALLY DEFERRED. All
    -- three candidates were measured on 2026-08-24 against a three-generation
    -- archive:
    --
    --   RESTRICT   whole-archive delete FAILS -- the cascade removes the root
    --              while its successor still references it, so an archive
    --              with more than one generation could never be deleted;
    --   CASCADE    whole-archive delete succeeds, but deleting the *oldest*
    --              revision silently removes every successor -- lineage
    --              [20, 21, 22] became [] -- which would take the current
    --              revision with it;
    --   deferred   whole-archive delete succeeds, and deleting a referenced
    --   NO ACTION  revision is refused with the lineage intact.
    --
    -- The delete guard below refuses removing any revision while its archive
    -- exists, so CASCADE's hazard is latent today. It is still the wrong
    -- choice: Step 3 introduces reviewed pruning and will relax that guard,
    -- and a foreign key that quietly erases descendants is exactly the broad
    -- cascading deletion of revision evidence the roadmap forbids. Deferring
    -- the check is what lets a whole-archive delete succeed without granting
    -- an old revision the power to erase the ones after it.
    FOREIGN KEY (previous_revision_id, archive_id)
        REFERENCES archive_revisions(id, archive_id)
        DEFERRABLE INITIALLY DEFERRED
);

-- Duplicate lookup across archives. Not unique: byte-identical archives stay
-- separately addressable.
CREATE INDEX IF NOT EXISTS idx_archive_revisions_sha256
    ON archive_revisions(archive_sha256);

CREATE INDEX IF NOT EXISTS idx_archive_revisions_archive
    ON archive_revisions(archive_id, revision_ordinal);

-- UNIQUE (archive_id, archive_sha256) cannot cap provisional rows, because
-- SQLite counts each NULL as distinct. This does.
CREATE UNIQUE INDEX IF NOT EXISTS idx_archive_revisions_one_provisional
    ON archive_revisions(archive_id)
    WHERE identity_state = 'provisional';

-- ---------------------------------------------------- revision invariants
CREATE TRIGGER IF NOT EXISTS trg_archive_revisions_immutable
BEFORE UPDATE ON archive_revisions
FOR EACH ROW
BEGIN
    SELECT RAISE(
        ABORT,
        'archive_revisions rows are immutable; record a new revision'
    );
END;

-- A revision records what was known about an archive's bytes at a point in
-- time, so none of them can be cherry-picked out of a live archive's history
-- -- provisional rows included. Establishing recovered bytes appends and
-- promotes rather than replacing, so there is no legitimate reason left to
-- delete any revision while its archive exists.
--
-- The archive_files test is what keeps this from making every archive
-- undeletable. Deleting an archive cascades its revisions away, and an
-- unqualified guard would abort that cascade -- so after this migration no
-- archive could ever be deleted again, which is a far larger change than the
-- one intended and would arrive as a surprise years later.
--
-- Measured on 2026-08-24 rather than assumed, because it depends on when
-- SQLite applies foreign-key actions relative to child triggers: during a
-- cascade from archive_files the parent row is already gone when this fires
-- (visible count 0), while a direct DELETE against archive_revisions still
-- sees it (count 1). Both readings are pinned by tests, so a change in that
-- ordering fails loudly instead of silently disarming the guard.
CREATE TRIGGER IF NOT EXISTS trg_archive_revisions_not_deletable
BEFORE DELETE ON archive_revisions
FOR EACH ROW
WHEN EXISTS (SELECT 1 FROM archive_files WHERE id = OLD.archive_id)
BEGIN
    SELECT RAISE(
        ABORT,
        'a revision cannot be deleted while its archive exists; it is evidence'
    );
END;

-- Lineage is a strict chain, not merely a non-null pointer: revision N must
-- follow revision N-1 of the same archive. Checked here rather than in a CHECK
-- because it reads another row.
CREATE TRIGGER IF NOT EXISTS trg_archive_revisions_lineage_is_sequential
BEFORE INSERT ON archive_revisions
FOR EACH ROW
WHEN NEW.previous_revision_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM archive_revisions AS p
    WHERE p.id = NEW.previous_revision_id
      AND p.archive_id = NEW.archive_id
      AND p.revision_ordinal = NEW.revision_ordinal - 1
 )
BEGIN
    SELECT RAISE(
        ABORT,
        'a revision must follow the previous ordinal of the same archive'
    );
END;

-- ------------------------------------------------------- current pointer
ALTER TABLE archive_files ADD COLUMN current_revision_id INTEGER
    REFERENCES archive_revisions(id);

CREATE INDEX IF NOT EXISTS idx_archive_files_current_revision
    ON archive_files(current_revision_id);

-- The composite constraint ALTER TABLE could not express. Both directions are
-- covered: an UPDATE that repoints an existing archive, and an INSERT that
-- arrives already pointing somewhere.
CREATE TRIGGER IF NOT EXISTS trg_current_revision_owned_on_update
BEFORE UPDATE OF current_revision_id ON archive_files
FOR EACH ROW
WHEN NEW.current_revision_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM archive_revisions AS r
    WHERE r.id = NEW.current_revision_id
      AND r.archive_id = NEW.id
 )
BEGIN
    SELECT RAISE(
        ABORT,
        'current_revision_id must name a revision of this archive'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_current_revision_owned_on_insert
BEFORE INSERT ON archive_files
FOR EACH ROW
WHEN NEW.current_revision_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM archive_revisions AS r
    WHERE r.id = NEW.current_revision_id
      AND r.archive_id = NEW.id
 )
BEGIN
    SELECT RAISE(
        ABORT,
        'current_revision_id must name a revision of this archive'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_current_revision_not_cleared
BEFORE UPDATE OF current_revision_id ON archive_files
FOR EACH ROW
WHEN NEW.current_revision_id IS NULL
BEGIN
    SELECT RAISE(
        ABORT,
        'an archive must always have a current revision; it cannot be cleared'
    );
END;

-- Closes the NULL window for every archive created after this migration.
-- Fires for raw SQL exactly as for the DAL, which is the point: an invariant
-- that only the application upholds is a convention, not an invariant.
--
-- The new revision is provisional because that is the truth at this instant:
-- the identity row has just been created and nothing has hashed its bytes.
-- Hashing later appends an established revision and moves the pointer,
-- leaving this row as the record of the period when the bytes were unknown.
--
-- The pointer is read back by ordinal rather than from last_insert_rowid(),
-- so it cannot be confused by any other insert a trigger might perform.
CREATE TRIGGER IF NOT EXISTS trg_archive_files_initial_revision
AFTER INSERT ON archive_files
FOR EACH ROW
WHEN NEW.current_revision_id IS NULL
BEGIN
    INSERT INTO archive_revisions (
        archive_id, revision_ordinal, identity_state, archive_sha256,
        previous_revision_id, evidence, source
    )
    VALUES (
        NEW.id, 1, 'provisional', NULL, NULL,
        'archive identity created; bytes not yet hashed', 'runtime'
    );

    UPDATE archive_files
    SET current_revision_id = (
        SELECT id FROM archive_revisions
        WHERE archive_id = NEW.id AND revision_ordinal = 1
    )
    WHERE id = NEW.id;
END;

-- ------------------------------------------------------------ observations
-- A sighting of a revision at a location during a run. This is the table that
-- absorbs change so revisions do not have to: re-seeing known bytes appends
-- here and rewrites nothing.
CREATE TABLE IF NOT EXISTS archive_revision_observations (
    id INTEGER PRIMARY KEY,
    revision_id INTEGER NOT NULL,
    location_id INTEGER,
    run_id INTEGER,
    file_size INTEGER,
    modified_time_ns INTEGER,
    observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (revision_id) REFERENCES archive_revisions(id)
        ON DELETE CASCADE,
    -- SET NULL, matching every other location reference in this schema: a
    -- location row disappearing must not erase the fact that the bytes were
    -- seen.
    FOREIGN KEY (location_id) REFERENCES file_locations(id)
        ON DELETE SET NULL,
    FOREIGN KEY (run_id) REFERENCES processing_runs(id)
        ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_revision_observations_revision
    ON archive_revision_observations(revision_id, observed_at);

CREATE INDEX IF NOT EXISTS idx_revision_observations_location
    ON archive_revision_observations(location_id);

-- ---------------------------------------------------------------- backfill
-- One initial revision per existing archive. Ordinal 1, no predecessor.
--
-- The digest is taken from archive_hashes, not from archive_files.sha256:
-- that column is populated for 0 archives, while archive_hashes holds 59,541
-- rows. Guarded with NOT EXISTS so re-running against an already-backfilled
-- database is a no-op.
INSERT INTO archive_revisions (
    archive_id, revision_ordinal, identity_state, archive_sha256,
    content_signature, file_size, page_count, previous_revision_id,
    evidence, source, created_at
)
SELECT
    a.id,
    1,
    CASE WHEN h.digest IS NOT NULL THEN 'established' ELSE 'provisional' END,
    h.digest,
    s.digest,
    a.file_size,
    a.page_count,
    NULL,
    'migration_014 initial revision backfilled from archive_hashes'
        || CASE
               WHEN h.digest IS NOT NULL
                   THEN ' (sha256 ' || substr(h.digest, 1, 12) || ')'
               ELSE ' (no archive-level sha256 recorded; provisional)'
           END,
    'migration_backfill',
    a.created_at
FROM archive_files AS a
LEFT JOIN archive_hashes AS h ON h.archive_id = a.id
LEFT JOIN archive_content_signatures AS s ON s.archive_id = a.id
WHERE NOT EXISTS (
    SELECT 1 FROM archive_revisions AS r WHERE r.archive_id = a.id
);

-- Point every archive at the revision just created for it. The ownership
-- triggers above police this exactly as they would police application code --
-- a backfill that got it wrong would abort the migration.
UPDATE archive_files
SET current_revision_id = (
    SELECT r.id FROM archive_revisions AS r
    WHERE r.archive_id = archive_files.id
      AND r.revision_ordinal = 1
)
WHERE current_revision_id IS NULL;
