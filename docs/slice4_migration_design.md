# Slice 4 — migration 015 design

**Status: design under review. No schema, no migration, no producer change,
nothing that touches production.**

Slice 1 is `docs/revision_aware_provenance_assessment.md`; slice 2 is
`docs/page_inventory_design.md`; slice 3 is the merged planner
(`comic_automation/archive/provenance_backfill_planner.py`, PR #88, merged at
`1941cdc`).

## 0. Corrections carried by this revision

Rounds 1–5 are the prior review passes.

```text
withdrawn claim                             round  disposition
slice 4 creates page_inventory                  1  4p owns the page tables
~58,432 inventory parents                       1  58,437 (slice 2 §4.5)
"producer requirement in the same migration"    1  same slice and release
candidate triggers deferred to slice 6          1  immutability lands here
twelve apply_migrations call sites              2  eleven; scripts/db.py separate
composite FK added by ALTER TABLE               2  syntax error; rebuild
inspector_version_basis added NOT NULL          2  refused; rebuild
"inspections insert inspected_at"               2  set on the conflict branch too
a moved hashed_at / calculated_at is accepted   2  reversed by R7
28-vs-21 is an unresolved production mystery    3  21 -> 25 -> 28 (R8)
location_id disposition is unresolved           3  source_context (R9)
"WHERE any protected column differs"            3  two axes (R10)
algorithm / algorithm_version protected in s4   3  identity, slice 5 (R11)
DP-17 in the inspection protection set          3  it passes BECAUSE
                                                   location_id is excluded
INSERT ... SELECT NULL,NULL                     4  cannot copy row one; staged
                                                   plan join (§5.4)
provenance_basis NOT NULL deferred to slice 5   4  moves into slice 4 (R12)
"R6's fail-closed abort removes that writer"    4  false; three defences (§8.1)
executor records nothing in schema_migrations   4  same-transaction ledger
three rebuild blocks left as placeholders       5  written in full (§5.5-5.7)
producer cutover shown for hashes only          5  all four, with data flow (§7)
attribution update permits ANY transition       5  REPRODUCED: a seed row was
                                                   silently relabelled. Exact
                                                   pairs only (R13, §7.4)
candidate attribution in the 4->4p window       5  undefined; specified (R14)
ledger precondition "014 present, 015 absent"   5  admits holes; exact pending
                                                   set {15} (R15, §12.1)
"a per-table binding digest"                    5  no serialization named;
                                                   reuses the planner's
                                                   canonical form (R16, §12.2)
§13 remeasurement blocks design approval        5  it blocks EXECUTION, not
                                                   approval (§13)
```

## 1. Starting state, verified in the tree

Checked at `1941cdc`.

```text
migrations present   001..014 (.sql), comic_automation/database/migrations/
migration 015        absent
provenance_basis / source_revision_id / inspector_version   absent
page_inventory       no table
no ALTER TABLE in 001..014 targets any of the four receiving tables
no table in 001..014 REFERENCES any of the four receiving tables
schema_migrations    version INTEGER PRIMARY KEY, name TEXT NOT NULL,
                     applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                     (migrations.py:12); migration_version("015_*.sql") -> 15
planner canonical    PlannedBinding.canonical_line() via _canonical_json,
                     PLAN_DIGEST_VERSION = "provenance-backfill-plan/2",
                     PLANNER_VERSION = "provenance-backfill-planner/2"
```

## 2. Rulings

```text
R1   page-inventory digest  4p's; row id and every written value.
R2   natural-key mapping    4p records archive_id -> page_inventory.id in the
                            postflight artifact, validated inside the txn.
R3   concurrency            full writer quiescence plus §8.
R4   candidate triggers     measurement immutability in slice 4; rest in 6.
R5   the 17 cases           read, mapped, reproduced before approval (§11).
R6   ordinary commands      FAIL CLOSED while 015 is pending. NEWLY LAUNCHED
                            commands only -- see §8.1.
R7   measurement timestamps hashed_at, calculated_at, inspected_at immutable;
                            a byte-identical rerun PRESERVES them.
R8   28 vs 21               21 + 4 = 25 (015 asserts 25/25); + 3 = 28 (slice 5).
R9   location_id            source_context, not measurement.
R10  conflict predicate     two axes, payload and attribution.
R11  trigger scope          slice-4 guard = measurement + lifecycle_immutable.
R12  basis nullability      moves into slice 4; every basis NOT NULL, and
                            archive_hashes.source_revision_id NOT NULL.
R13  attribution transitions  EXACT PAIRS ONLY, per table (§7.4). The producer
                            predicate carries this until each table's
                            transition trigger lands: slice 5 for signatures
                            and inspections, SLICE 6 for candidates
                            (ND-01..ND-16). The candidate predicate becomes
                            ACTIVE AT 4p, not during 4 -> 4p: before 4p no
                            code path performs that transition, because there
                            is no explicitly-loaded inventory to inherit from.
                            From 4p to slice 6 it is load-bearing.
R14  candidate attribution  no RETROSPECTIVE binding, ever: a candidate created
                            between 015 and 4p stays unresolved permanently,
                            because nothing records which page generation it
                            compared. CONTEMPORANEOUS binding from 4p onward:
                            the loader is given an explicit revision/inventory
                            (slice 2 PI-08), so each side binds at INSERT with
                            inherited_from_page_evidence. A pending unresolved
                            row binds only on a fresh comparison whose full
                            payload matches the stored one (§7.6).
R15  ledger precondition    refuse unless the pending protected set is exactly
                            {15} and every discovered version below 15 is
                            recorded (§12.1).
R16  applied projection     a SEPARATE slice-4 projection, not the planner's
                            rendering -- PlannedBinding requires
                            parameters_basis, which slice 4 does not write, so
                            its own invariant refuses a candidate reconstructed
                            from post-015 state. Reconciliation is
                            field-for-field over projections, not a count plus
                            a digest (§12.2).
```

## 3. Scope: four tables, 180,519 field projections

Page tables are **slice 4p**, ordered 4 → 4p → 5 (slice 2 §10.3).

```text
archive_hashes                              59,541
archive_content_signatures                  58,437
archive_inspections                         59,541
near_duplicate_candidates                    3,000
                                           -------
slice-4 total                              180,519
planned total (slice 3)                    238,956
page_inventory bindings, deferred to 4p     58,437
```

### 3.1 Roadmap accounting after R12

```text
slice 4   ownership keys, basis columns created NOT NULL, inspector version
          pair, results-immutability triggers, four table rebuilds
slice 4p  page_inventory + archive_pages. No retrospective candidate pass;
          contemporaneous candidate binding becomes possible here (R14).
slice 5   uniqueness / partial indexes, UPSERT -> append, supersession columns
          and lifecycle, identity immutability, the source_context guard, the
          attribution TRANSITION TRIGGERS (which need supersession columns),
          the remaining trigger set.
          NOT slice 5's any more: basis nullability on these four tables.
slice 6   candidate parameters, its trigger set and indexes, parameters_basis
slice 7   per-row granularity resolution
```

## 4. Migration 015 must be unreachable by an ordinary command

**Measured at `1941cdc`.** `apply_migrations()` (`migrations.py:76`) discovers
every numbered `.sql` file and applies each unapplied one inside
`BEGIN IMMEDIATE`, with no notion of approval, backup or postflight. **Eleven
call sites** invoke it:

```text
archive/cli.py:291                      archive/hash_cli.py:61
archive/duplicate_resolution_cli.py:202 archive/near_duplicate_cli.py:93
archive/page_hash_cli.py:64             archive/perceptual_hash_cli.py:69
archive/quarantine_cli.py:198           jobs/enqueue_missing_stages.py:176
library/cli.py:237                      service.py:159
scripts/benchmark_perceptual_hash_profiling.py:178
```

### 4.1 `scripts/db.py` is out of scope — settled

It defines its own `apply_migrations` (`scripts/db.py:107`) against
`DEFAULT_MIGRATIONS_DIR = <repo root>/migrations` (`scripts/db.py:12`), which
holds only `001_initial_schema.sql`. It never imports the `comic_automation`
implementation and will never discover 015. Regression assertion required — a
test, not a comment:

```text
the two roots are disjoint; neither contains the other's files
scripts/db.py's DEFAULT_MIGRATIONS_DIR != comic_automation's MIGRATIONS
no protected migration id appears under the root migrations/ directory
comic_automation's migrations directory contains every protected id
```

### 4.2 Required shape, under R6

```text
protected set        015 declared protected IN CODE, not by filename.
ordinary path        apply_migrations() ABORTS when a protected migration is
                     pending; it does not skip and continue against schema 014.
read-only path       a query-only path that never calls apply_migrations().
protected executor   the only caller permitted to apply it (§12).
```

## 5. The rebuilds

### 5.1 Why ALTER TABLE cannot do it — measured

```text
ALTER TABLE ev ADD FOREIGN KEY (rev_id, archive_id) REFERENCES ...
    OperationalError: near "FOREIGN": syntax error
ALTER TABLE ev ADD COLUMN rev_id INTEGER, FOREIGN KEY (rev_id, archive_id) ...
    OperationalError: near ",": syntax error
ALTER TABLE ev ADD COLUMN rev2 INTEGER REFERENCES parent(id)      ACCEPTED
ALTER TABLE t  ADD COLUMN b TEXT NOT NULL
    REFUSED: Cannot add a NOT NULL column with default value NULL
ALTER TABLE t  ADD COLUMN c TEXT NOT NULL DEFAULT 'unknown_legacy'  ACCEPTED
    -- and the WRONG answer (§5.6)

sqlite 3.40.1 | python 3.11.3 | Windows-10-10.0.26200-SP0 | 2026-09-01
```

### 5.2 Two rebuild hazards, measured on the production runtime

Measured **read-only with respect to production**, against scratch in-memory
databases on the production runtime (the office PC is the worker);
`G:\ComicAutomation\` was not opened.

```text
PRAGMA foreign_keys=OFF INSIDE  BEGIN IMMEDIATE -> still 1  (SILENT NO-OP)
PRAGMA foreign_keys=OFF OUTSIDE a transaction   -> 0        (applies)

rebuild with foreign_keys=ON   -> referencing child rows: 1 -> 0  (cascaded)
rebuild with foreign_keys=OFF  -> referencing child rows: 1 -> 1  (preserved)

in_transaction after execute()        : True
in_transaction after executescript()  : False  <- transaction COMMITTED
COMMIT / ROLLBACK afterwards          : "no transaction is active"
rows surviving the attempted rollback : both -- unrollbackable
```

The repository enables foreign keys (`connection.py:27`, `dal.py:123`), so the
`PRAGMA` must precede `BEGIN`. No table references the four receiving tables
today, so the cascade costs nothing yet; 4p adds `page_inventory` children.
`apply_migrations()` is already safe on the second hazard — it splits with
`iter_sql_statements()` and calls `execute()` per statement
(`migrations.py:91,102`) — and the executor must do the same.

### 5.3 The copy must not pass through an invalid state — measured

```text
INSERT ... SELECT id, archive_id, digest, NULL, NULL FROM archive_hashes
  REJECTED: CHECK constraint failed:
            source_revision_id IS NOT NULL AND provenance_basis IS NOT NULL
```

The `INSERT` fails on row one; the per-row `UPDATE` that was meant to follow is
unreachable. Under R12 all four tables fail the same way. Every row must enter
the rebuilt table already valid, so the binding is supplied *during* the copy
from a staged plan.

```sql
CREATE TEMP TABLE temp_slice4_plan (
    table_name              TEXT    NOT NULL,
    row_id                  INTEGER NOT NULL,
    archive_id              INTEGER NOT NULL,
    source_revision_id      INTEGER,
    provenance_basis        TEXT    NOT NULL,
    revision_b_id           INTEGER,          -- candidates only
    provenance_basis_b      TEXT,             -- candidates only
    inspector_version       TEXT,             -- inspections only
    inspector_version_basis TEXT,             -- inspections only
    PRIMARY KEY (table_name, row_id)          -- duplicate plan keys impossible
);
```

Three counts, each zero, per table, before anything is dropped:

```text
unmatched old rows    LEFT JOIN plan ON table_name/row_id, WHERE plan IS NULL
unmatched plan rows   LEFT JOIN table ON id, WHERE table IS NULL
duplicate plan keys   structurally impossible (PRIMARY KEY), asserted anyway
                      against the artifact's row count
```

### 5.4 `archive_hashes`

```sql
-- OUTSIDE the transaction: PRAGMA foreign_keys = OFF, assert it reads back 0.
CREATE TABLE archive_hashes_new (
    id                 INTEGER PRIMARY KEY,
    archive_id         INTEGER NOT NULL UNIQUE,
    location_id        INTEGER,
    algorithm          TEXT NOT NULL,
    algorithm_version  TEXT NOT NULL,
    digest             TEXT NOT NULL,
    file_size          INTEGER NOT NULL,
    modified_time_ns   INTEGER NOT NULL,
    bytes_read         INTEGER NOT NULL,
    hashed_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_revision_id INTEGER NOT NULL,                          -- R12
    provenance_basis   TEXT    NOT NULL                           -- R12
        CHECK (provenance_basis IN ('measured',
                                    'migration_014_identity_seed')),
    FOREIGN KEY (archive_id)  REFERENCES archive_files(id)  ON DELETE CASCADE,
    FOREIGN KEY (location_id) REFERENCES file_locations(id) ON DELETE SET NULL,
    FOREIGN KEY (source_revision_id, archive_id)
        REFERENCES archive_revisions(id, archive_id)
);

INSERT INTO archive_hashes_new
    (id, archive_id, location_id, algorithm, algorithm_version, digest,
     file_size, modified_time_ns, bytes_read, hashed_at, created_at,
     updated_at, source_revision_id, provenance_basis)
SELECT o.id, o.archive_id, o.location_id, o.algorithm, o.algorithm_version,
       o.digest, o.file_size, o.modified_time_ns, o.bytes_read, o.hashed_at,
       o.created_at, o.updated_at, p.source_revision_id, p.provenance_basis
FROM archive_hashes AS o
JOIN temp_slice4_plan AS p
  ON p.table_name = 'archive_hashes' AND p.row_id = o.id;

DROP TABLE archive_hashes;
ALTER TABLE archive_hashes_new RENAME TO archive_hashes;
CREATE INDEX idx_archive_hashes_digest ON archive_hashes(algorithm, digest);
```

No paired CHECK: both bases are bound, because slice 1 removed
`unresolved_no_identity` from this table's vocabulary — the hasher computes a
digest and binds in the same transaction. That is what makes
`source_revision_id NOT NULL` correct here and wrong elsewhere.

### 5.5 `archive_content_signatures`

```sql
CREATE TABLE archive_content_signatures_new (
    id                      INTEGER PRIMARY KEY,
    archive_id              INTEGER NOT NULL UNIQUE,
    location_id             INTEGER,
    algorithm               TEXT NOT NULL,
    algorithm_version       TEXT NOT NULL,
    digest                  TEXT NOT NULL,
    page_count              INTEGER NOT NULL,
    image_bytes             INTEGER NOT NULL,
    source_file_size        INTEGER NOT NULL,
    source_modified_time_ns INTEGER NOT NULL,
    calculated_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_revision_id      INTEGER,
    provenance_basis        TEXT NOT NULL
        CHECK (provenance_basis IN ('stat_matched_revision',
                                    'migration_014_field_seed',
                                    'unresolved_drift',
                                    'unresolved_no_identity')),
    CHECK ((source_revision_id IS NOT NULL
            AND provenance_basis IN ('stat_matched_revision',
                                     'migration_014_field_seed'))
        OR (source_revision_id IS NULL
            AND provenance_basis IN ('unresolved_drift',
                                     'unresolved_no_identity'))),
    FOREIGN KEY (archive_id)  REFERENCES archive_files(id)  ON DELETE CASCADE,
    FOREIGN KEY (location_id) REFERENCES file_locations(id) ON DELETE SET NULL,
    FOREIGN KEY (source_revision_id, archive_id)
        REFERENCES archive_revisions(id, archive_id)
);

INSERT INTO archive_content_signatures_new
    (id, archive_id, location_id, algorithm, algorithm_version, digest,
     page_count, image_bytes, source_file_size, source_modified_time_ns,
     calculated_at, created_at, updated_at, source_revision_id,
     provenance_basis)
SELECT o.id, o.archive_id, o.location_id, o.algorithm, o.algorithm_version,
       o.digest, o.page_count, o.image_bytes, o.source_file_size,
       o.source_modified_time_ns, o.calculated_at, o.created_at, o.updated_at,
       p.source_revision_id, p.provenance_basis
FROM archive_content_signatures AS o
JOIN temp_slice4_plan AS p
  ON p.table_name = 'archive_content_signatures' AND p.row_id = o.id;

DROP TABLE archive_content_signatures;
ALTER TABLE archive_content_signatures_new
    RENAME TO archive_content_signatures;
CREATE INDEX idx_content_signatures_digest
    ON archive_content_signatures(algorithm, algorithm_version, digest,
                                  page_count);
```

The paired CHECK is written as two explicit `IN` lists rather than
`LIKE 'unresolved%'`, because the `LIKE` form is what makes an all-NULL row
pass (§6.3); with `provenance_basis NOT NULL` the hole is already closed, and
the explicit lists make the pairing checkable by reading rather than by
knowing SQL's three-valued logic.

### 5.6 `archive_inspections`

```sql
CREATE TABLE archive_inspections_new (
    id                         INTEGER PRIMARY KEY,
    archive_id                 INTEGER NOT NULL UNIQUE,
    location_id                INTEGER,
    inspected_path             TEXT NOT NULL,
    archive_format             TEXT NOT NULL,
    status                     TEXT NOT NULL,
    entry_count                INTEGER NOT NULL,
    page_count                 INTEGER NOT NULL,
    directory_count            INTEGER NOT NULL,
    encrypted                  INTEGER NOT NULL DEFAULT 0,
    comic_info_present         INTEGER NOT NULL DEFAULT 0,
    comic_info_valid           INTEGER NOT NULL DEFAULT 0,
    comic_info_error           TEXT,
    comic_info_json            TEXT,
    crc_verified               INTEGER NOT NULL DEFAULT 0,
    inspected_file_size        INTEGER,
    inspected_modified_time_ns INTEGER,
    result_json                TEXT NOT NULL,
    inspected_at               TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at                 TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at                 TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_revision_id         INTEGER,
    provenance_basis           TEXT NOT NULL
        CHECK (provenance_basis IN ('stat_matched_revision',
                                    'single_revision_inherited',
                                    'unresolved_no_identity')),
    inspector_version          TEXT,
    inspector_version_basis    TEXT NOT NULL          -- NO DEFAULT
        CHECK (inspector_version_basis IN ('known', 'unknown_legacy')),
    -- Every column definition must precede every table constraint: SQLite
    -- rejects a column that follows one. An earlier draft interleaved them
    -- and failed to parse, which only executing the block revealed.
    CHECK ((source_revision_id IS NOT NULL
            AND provenance_basis IN ('stat_matched_revision',
                                     'single_revision_inherited'))
        OR (source_revision_id IS NULL
            AND provenance_basis = 'unresolved_no_identity')),
    CHECK ((inspector_version_basis = 'known'
            AND inspector_version IS NOT NULL)
        OR (inspector_version_basis = 'unknown_legacy'
            AND inspector_version IS NULL)),
    FOREIGN KEY (archive_id)  REFERENCES archive_files(id)  ON DELETE CASCADE,
    FOREIGN KEY (location_id) REFERENCES file_locations(id) ON DELETE SET NULL,
    FOREIGN KEY (source_revision_id, archive_id)
        REFERENCES archive_revisions(id, archive_id)
);

INSERT INTO archive_inspections_new
    (id, archive_id, location_id, inspected_path, archive_format, status,
     entry_count, page_count, directory_count, encrypted, comic_info_present,
     comic_info_valid, comic_info_error, comic_info_json, crc_verified,
     inspected_file_size, inspected_modified_time_ns, result_json,
     inspected_at, created_at, updated_at, source_revision_id,
     provenance_basis, inspector_version, inspector_version_basis)
SELECT o.id, o.archive_id, o.location_id, o.inspected_path, o.archive_format,
       o.status, o.entry_count, o.page_count, o.directory_count, o.encrypted,
       o.comic_info_present, o.comic_info_valid, o.comic_info_error,
       o.comic_info_json, o.crc_verified, o.inspected_file_size,
       o.inspected_modified_time_ns, o.result_json, o.inspected_at,
       o.created_at, o.updated_at, p.source_revision_id, p.provenance_basis,
       p.inspector_version, p.inspector_version_basis
FROM archive_inspections AS o
JOIN temp_slice4_plan AS p
  ON p.table_name = 'archive_inspections' AND p.row_id = o.id;

DROP TABLE archive_inspections;
ALTER TABLE archive_inspections_new RENAME TO archive_inspections;
CREATE INDEX idx_archive_inspections_status ON archive_inspections(status);
CREATE INDEX idx_archive_inspections_path
    ON archive_inspections(inspected_path);
```

`inspector_version_basis` has **no default**. The `NOT NULL DEFAULT
'unknown_legacy'` form that `ALTER TABLE` would accept silently labels every
future inspection omitting the column as legacy evidence — the precise false
claim §6.5 refuses. All 59,541 historical rows are copied `unknown_legacy`
with a NULL version, from the plan.

### 5.7 `near_duplicate_candidates`

```sql
CREATE TABLE near_duplicate_candidates_new (
    id                     INTEGER PRIMARY KEY,
    archive_a_id           INTEGER NOT NULL,
    archive_b_id           INTEGER NOT NULL,
    match_method           TEXT NOT NULL,
    similarity_score       REAL NOT NULL,
    page_match_ratio       REAL NOT NULL,
    compared_page_count    INTEGER NOT NULL,
    page_count_a           INTEGER NOT NULL,
    page_count_b           INTEGER NOT NULL,
    average_dhash_distance REAL NOT NULL,
    average_phash_distance REAL NOT NULL,
    dimension_match_ratio  REAL,
    metrics_json           TEXT NOT NULL,
    review_status          TEXT NOT NULL DEFAULT 'pending_review',
    reviewed_by            TEXT,
    reviewed_at            TEXT,
    created_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revision_a_id          INTEGER,
    revision_b_id          INTEGER,
    provenance_basis_a     TEXT NOT NULL
        CHECK (provenance_basis_a IN ('inherited_from_page_evidence',
                                      'single_revision_inherited',
                                      'unresolved_no_identity')),
    provenance_basis_b     TEXT NOT NULL
        CHECK (provenance_basis_b IN ('inherited_from_page_evidence',
                                      'single_revision_inherited',
                                      'unresolved_no_identity')),
    CHECK ((revision_a_id IS NOT NULL
            AND provenance_basis_a IN ('inherited_from_page_evidence',
                                       'single_revision_inherited'))
        OR (revision_a_id IS NULL
            AND provenance_basis_a = 'unresolved_no_identity')),
    CHECK ((revision_b_id IS NOT NULL
            AND provenance_basis_b IN ('inherited_from_page_evidence',
                                       'single_revision_inherited'))
        OR (revision_b_id IS NULL
            AND provenance_basis_b = 'unresolved_no_identity')),
    FOREIGN KEY (archive_a_id) REFERENCES archive_files(id) ON DELETE CASCADE,
    FOREIGN KEY (archive_b_id) REFERENCES archive_files(id) ON DELETE CASCADE,
    FOREIGN KEY (revision_a_id, archive_a_id)
        REFERENCES archive_revisions(id, archive_id),
    FOREIGN KEY (revision_b_id, archive_b_id)
        REFERENCES archive_revisions(id, archive_id),
    -- the five existing CHECKs, preserved verbatim
    CHECK (archive_a_id < archive_b_id),
    CHECK (similarity_score BETWEEN 0.0 AND 1.0),
    CHECK (page_match_ratio BETWEEN 0.0 AND 1.0),
    CHECK (dimension_match_ratio IS NULL
           OR dimension_match_ratio BETWEEN 0.0 AND 1.0),
    CHECK (review_status IN ('pending_review', 'confirmed_duplicate',
                             'keep_both', 'rejected'))
);

INSERT INTO near_duplicate_candidates_new
    (id, archive_a_id, archive_b_id, match_method, similarity_score,
     page_match_ratio, compared_page_count, page_count_a, page_count_b,
     average_dhash_distance, average_phash_distance, dimension_match_ratio,
     metrics_json, review_status, reviewed_by, reviewed_at, created_at,
     updated_at, revision_a_id, revision_b_id, provenance_basis_a,
     provenance_basis_b)
SELECT o.id, o.archive_a_id, o.archive_b_id, o.match_method,
       o.similarity_score, o.page_match_ratio, o.compared_page_count,
       o.page_count_a, o.page_count_b, o.average_dhash_distance,
       o.average_phash_distance, o.dimension_match_ratio, o.metrics_json,
       o.review_status, o.reviewed_by, o.reviewed_at, o.created_at,
       o.updated_at, p.source_revision_id, p.revision_b_id,
       p.provenance_basis, p.provenance_basis_b
FROM near_duplicate_candidates AS o
JOIN temp_slice4_plan AS p
  ON p.table_name = 'near_duplicate_candidates' AND p.row_id = o.id;

DROP TABLE near_duplicate_candidates;
ALTER TABLE near_duplicate_candidates_new
    RENAME TO near_duplicate_candidates;
CREATE INDEX idx_near_duplicate_review
    ON near_duplicate_candidates(review_status, similarity_score DESC);
```

Side A uses the staging table's `source_revision_id` / `provenance_basis`
columns and side B its `revision_b_id` / `provenance_basis_b`; the loader maps
the planner's `sides` tuple onto that pair by `label` (§12.2), so the
correspondence is explicit rather than positional.

### 5.8 What each rebuild preserves, and how it is verified

Verified inside the transaction, per table:

```text
PRAGMA foreign_key_check   no rows
row count                  new == old, against the recorded expected count
id set                     identical, not merely equal in cardinality
measurement values         byte-identical old vs new, including the three
                           measurement timestamps of R7
index set                  every index present by name
disposition totality       PRAGMA table_info(t) == the union of that table's
                           disposition lists (§9): 14 / 15 / 25 / 22
basis totality             zero NULL bases -- structurally guaranteed by R12,
                           asserted anyway
```

## 6. Vocabulary and the structural refusal

### 6.1 Per-table vocabularies (slice 1 §9.4.2)

```text
archive_hashes              measured, migration_014_identity_seed
archive_content_signatures  stat_matched_revision, migration_014_field_seed,
                            unresolved_drift, unresolved_no_identity
archive_inspections         stat_matched_revision, single_revision_inherited,
                            unresolved_no_identity
near_duplicate_candidates   inherited_from_page_evidence,
                            single_revision_inherited,
                            unresolved_no_identity          (per side)
```

The migration derives each table's CHECK from slice 1's union rather than
restating it; §5.4–5.7 show what that derivation must produce, not a second
source of truth.

### 6.2 R12 closes the (NULL, NULL) hole structurally — measured

```text
revision NOT NULL shape (archive_hashes):
  pre-cutover UPSERT on an EXISTING row : REJECTED
      NOT NULL constraint failed: archive_hashes.source_revision_id
  pre-cutover UPSERT inserting a NEW row: REJECTED  (same)
  evidence row unchanged                : True

nullable-revision shape:
  unresolved row stores (NULL, 'unresolved_drift')  -- still legitimate
  pre-cutover UPSERT, existing UNRESOLVED row : REJECTED
      NOT NULL constraint failed: sigs.provenance_basis
  pre-cutover UPSERT, new row                 : REJECTED  (same)
  unresolved evidence row unchanged           : True

sqlite 3.40.1 | python 3.11.3 | Windows-10-10.0.26200-SP0 | 2026-09-01
```

The rejection lands on the INSERT attempt, before the conflict branch runs, so
a pre-cutover producer resuming after the migration fails without touching any
evidence row — the third defence in §8.1.

## 7. Producer cutover — four paths, same slice and release

```text
archive_hashes              archive/hashing.py:151   (save)
archive_content_signatures  archive/page_hashing.py:229  (and dal.py:535)
archive_inspections         archive/repository.py:76
near_duplicate_candidates   archive/near_duplicate.py:505
```

### 7.1 The hasher must be reordered — verified in the tree

`ArchiveHashRepository.save()` writes `archive_hashes` at **line 151** but does
not obtain a revision until `record_or_reuse()` at **line 233**. Under R12
`source_revision_id` is `NOT NULL`, so the write cannot proceed without an ID
that does not yet exist. The cutover is therefore a **reordering**, not an
added column.

**The previous revision's safety proof was false.** It claimed
`record_or_reuse()` touches only `archive_revisions`. It does not:

```text
current_for()  READS archive_files.current_revision_id   (dal.py:652-661)
set_current()  WRITES archive_files.current_revision_id  (dal.py:830-833),
               and record_or_reuse calls it on the provisional path
```

`save()` also writes `archive_files.file_size`, so the two overlap on that
table. The reorder still holds, but for a reason that has to be stated against
the real read/write set rather than a wrong one:

```text
save() runs entirely inside one transaction (require_transaction at the top),
so moving statements within it changes no durability boundary.

The overlap on archive_files is COLUMN-DISJOINT: record_or_reuse touches only
current_revision_id, and save() touches only file_size. Neither reads what the
other writes, so their relative order does not change either result.

record_or_reuse does not read archive_hashes at all, which is the dependency
that would actually forbid moving it before the archive_hashes write.

The one ordering constraint that must be preserved: `metadata_changed` reads
file_locations BEFORE the UPDATE that refreshes it. That read is already the
first statement and stays there.
```

Column-disjointness is an argument about the code as it stands today, not an
invariant the database enforces, so §11 tests it: the reorder is asserted to
leave `current_revision_id`, `file_size` and the revision lineage identical to
what the unreordered path produced for the same input.

Required order:

```text
1  require_transaction
2  read previous file_locations row  -> metadata_changed        (unchanged)
3  record_or_reuse(...)              -> revision_id             (MOVED UP)
4  set_current(archive_id, revision_id)                         (MOVED UP)
5  INSERT/UPSERT archive_hashes, binding source_revision_id = revision_id
   and provenance_basis = 'measured'                            (was 3)
6  UPDATE file_locations, UPDATE archive_files                  (unchanged)
7  revisions.observe(...)                                       (unchanged)
8  enqueue reinspection if metadata_changed                     (unchanged)
```

Steps 3–4 before 5 is what makes `measured` truthful: the row binds to the
revision its own digest just established, which is slice 1 §8.2's "direct"
path.

### 7.2 The two axes need two statements (R10)

```text
measurement payload differs -> attempt the update -> the trigger ABORTS
only attribution differs    -> update attribution only, preserving every
                               measurement value and timestamp
neither differs             -> true no-op
```

**Statement 1 — the measurement path**, shown for `archive_hashes`:

```sql
INSERT INTO archive_hashes (
    archive_id, location_id, algorithm, algorithm_version, digest,
    file_size, modified_time_ns, bytes_read,
    source_revision_id, provenance_basis)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'measured')
ON CONFLICT(archive_id) DO UPDATE SET
    location_id      = excluded.location_id,
    digest           = excluded.digest,
    file_size        = excluded.file_size,
    modified_time_ns = excluded.modified_time_ns,
    bytes_read       = excluded.bytes_read,
    hashed_at        = CURRENT_TIMESTAMP,
    updated_at       = CURRENT_TIMESTAMP
WHERE archive_hashes.digest           IS NOT excluded.digest
   OR archive_hashes.file_size        IS NOT excluded.file_size
   OR archive_hashes.modified_time_ns IS NOT excluded.modified_time_ns
   OR archive_hashes.bytes_read       IS NOT excluded.bytes_read;
```

With the payload identical the `WHERE` is false, SQLite performs no update, and
`hashed_at`, `updated_at` and `location_id` are preserved — R7 and the second
half of R9 in one mechanism. With it different the update is attempted and the
trigger aborts: in slice 4 a changed re-measurement is a refusal, and slice 5
turns it into an append.

`IS NOT`, not `<>`: `location_id`, `comic_info_error`, `dimension_match_ratio`
and `inspected_file_size` are nullable, and `<>` against NULL is NULL.

**Verified end-to-end** with the trigger installed and the clock advanced past
a second boundary:

```text
identical rerun (clock advanced) -> unchanged: True | location repointed: False
changed payload                  -> REJECTED: measurement results are immutable
```

The same harness also verified the attribution mechanism -- an attribution-only
update applies while preserving every measurement and `hashed_at`, and a rerun
of it changes 0 rows. **That was measured on the generic shape, not on
`archive_hashes`**, which issues no attribution statement at all (§7.4). The
previous revision listed those two lines inside this table's transcript, which
read as though the hasher performed an attribution update it must never
perform. They belong to §7.5's signature and inspection paths, and are shown
there.

### 7.3 Payload and attribution columns, per producer

```text
archive_hashes              payload      digest, file_size, modified_time_ns,
                                         bytes_read
                            attribution  source_revision_id, provenance_basis
                            never inputs hashed_at, created_at, updated_at,
                                         location_id, algorithm,
                                         algorithm_version

archive_content_signatures  payload      digest, page_count, image_bytes,
                                         source_file_size,
                                         source_modified_time_ns
                            attribution  source_revision_id, provenance_basis
                            never inputs calculated_at, created_at, updated_at,
                                         location_id, algorithm,
                                         algorithm_version

archive_inspections         payload      inspected_path, archive_format,
                                         status, entry_count, page_count,
                                         directory_count, encrypted,
                                         comic_info_present, comic_info_valid,
                                         comic_info_error, comic_info_json,
                                         crc_verified, inspected_file_size,
                                         inspected_modified_time_ns,
                                         result_json
                            attribution  source_revision_id, provenance_basis
                            never inputs inspected_at, created_at, updated_at,
                                         location_id, inspector_version,
                                         inspector_version_basis

near_duplicate_candidates   payload      similarity_score, page_match_ratio,
                                         compared_page_count, page_count_a,
                                         page_count_b, average_dhash_distance,
                                         average_phash_distance,
                                         dimension_match_ratio, metrics_json
                            attribution  revision_a_id, revision_b_id,
                                         provenance_basis_a,
                                         provenance_basis_b
                            never inputs created_at, updated_at, match_method,
                                         review_status, reviewed_by,
                                         reviewed_at
```

`location_id` is a comparison input on none of them, which is what makes R9
hold.

### 7.4 The attribution statement permits EXACT PAIRS only (R13)

The previous revision's statement updated any differing revision or basis.
**Reproduced**, on the shape the migration will actually build:

```text
row before : (source_revision_id 10, 'migration_014_identity_seed')
statement  : UPDATE ... SET source_revision_id=11, provenance_basis='measured'
             WHERE archive_id=1 AND (revision differs OR basis differs)
row after  : (11, 'measured')
```

A migration-seed row was silently relabelled as a measured binding to a
different revision, and the results trigger permitted it because no measurement
column changed. Slice 1 §9.4.2's transition trigger would refuse it — but that
trigger's `WHEN` references `superseded_at` and `superseded_by_id`, which are
**slice 5 columns**. Slice 4 does not have them, so **the producer predicate
carries the rule in the interim**, and it is load-bearing rather than
defensive.

Slice 1's exact pairs:

```text
archive_hashes              NONE. The hasher binds at INSERT and has no
                            unresolved state to leave, so it issues NO
                            attribution statement at all.
archive_content_signatures  unresolved_no_identity -> stat_matched_revision
archive_inspections         unresolved_no_identity -> stat_matched_revision
near_duplicate_candidates   unresolved_no_identity -> inherited_from_page_evidence
                            (per side)
```

`unresolved_drift` has no outbound transition anywhere: a drift row binds only
once a revision exists for the generation it describes, and minting that
revision is deferred. `migration_014_*` seeds and `single_revision_inherited`
are backfill states, unreachable by any transition.

So the attribution statement, for signatures and inspections:

```sql
UPDATE archive_content_signatures
   SET source_revision_id = ?,                 -- the matched revision
       provenance_basis   = 'stat_matched_revision',
       updated_at         = CURRENT_TIMESTAMP
 WHERE archive_id         = ?
   AND source_revision_id IS NULL                       -- was unbound
   AND provenance_basis   = 'unresolved_no_identity'    -- exact source state
   AND ? IS NOT NULL;                                   -- exact target is bound
```

and per side for candidates, with `revision_a_id` / `provenance_basis_a` (and
the `_b` pair) in place of the single columns. The four `AND` clauses are the
predicate: an already-bound row, a drift row, a seed row and a
`single_revision_inherited` row all fail to match and are left untouched
rather than relabelled.

`archive_hashes` issues no attribution statement, which is why the reproduction
above describes a statement that will not exist after this correction.

### 7.5 The other three producers, concretely

Slice 1 §8.2 gives the abstract ownership paths; these are the changes to the
producers as they stand.

**Signatures — `page_hashing.py:229`.** The stat match is the join the file
already uses for freshness (`page_hashing.py:329`:
`acs.source_file_size = fl.file_size AND acs.source_modified_time_ns =
fl.modified_time_ns`), pointed at `archive_hashes` instead:

```sql
-- The lookup. Binds only on EXACTLY ONE revision-bound match (slice 1 §8.3):
-- zero leaves it unresolved, and two or more also leaves it unresolved,
-- because picking either would be a coin flip recorded as a fact.
SELECT ah.source_revision_id
  FROM archive_hashes AS ah
 WHERE ah.archive_id         = :archive_id
   AND ah.file_size          = :source_file_size
   AND ah.modified_time_ns   = :source_modified_time_ns
   AND ah.source_revision_id IS NOT NULL
 LIMIT 2;                       -- LIMIT 2, so "several" is distinguishable
                                -- from "one"; LIMIT 1 would hide the case
                                -- the rule exists to refuse
```

```text
exactly one row   -> source_revision_id = it, basis 'stat_matched_revision'
zero rows         -> source_revision_id NULL, basis 'unresolved_no_identity'
two or more       -> source_revision_id NULL, basis 'unresolved_no_identity'
```

**Pre-commit revalidation — of the FILE, not the row.** The previous revision
re-read `file_locations` here. That is nearly worthless: `BEGIN IMMEDIATE`
already excludes every other database writer for the transaction's duration, so
the row cannot have moved — and a filesystem replacement does not update that
row at all. The check would have passed over exactly the event it was meant to
catch.

The archive hasher already does this correctly, and is the model: it stats the
path before and after reading and refuses on a difference
(`hashing.py:85-93`, `raise OSError("Archive changed while hashing")`).

```text
1  carry the PATH into the transactional save flow. It is not there today --
   the signature producer receives ids and a result, not the path it read --
   so this is a signature change, not just an added statement.
2  immediately before COMMIT, re-stat that path:
       after = Path(path).stat()
3  ABORT unless ALL of:
       after.st_size     == the captured source_file_size
       after.st_mtime_ns == the captured source_modified_time_ns
       the location row still names this archive_id and this path
4  the database check remains, but as the identity half only: the location
   must still be the same row for the same archive at the same path. It is
   not evidence about the file's bytes, and is no longer described as such.
```

A missing file raises rather than compares: `stat()` on a deleted path throws,
and that is the correct outcome — abort, do not commit evidence about bytes
that are gone.

This closes the window slice 1 §8.3 names, and it is the only form that does:
the stat that matters is the filesystem's, and only re-reading it can detect a
replacement.

The write itself is §7.2's two statements over the signature payload of §7.3,
with `provenance_basis` supplied by the lookup above.

**Inspections — `repository.py:76`.** Three separate defects, each verified
in the tree, and none of them fixable by adding columns to the existing
statement.

**(a) The stat is not measured.** `InspectArchiveHandler` passes
`file_size=int(location["file_size"])` from the `file_locations` row it read
earlier (`handlers.py:85-86`), and `inspect_archive()` never stats the file at
all — `inspection.py` contains no `.stat()`, `st_size` or `st_mtime_ns`. So
`inspected_file_size` and `inspected_modified_time_ns` today record what the
database believed, not what was inspected. Binding on them would stat-match
against a remembered value.

```text
required   inspect_archive() captures the path's stat BEFORE and AFTER reading
           it and refuses on a difference, exactly as the hasher does
           (hashing.py:85-93), and returns the after-stat as part of its
           result. The repository stores THAT, not the caller's parameter.
           The file_size / modified_time_ns parameters of save() go away:
           keeping them beside a measured value would leave two answers to
           one question.
```

**That is necessary and not sufficient.** A before/after stat inside
`inspect_archive()` proves only that the file held still *during the read*. It
can be replaced after the function returns and before the commit:

```text
inspect V1 -> returns stat S
file replaced with V2
BEGIN IMMEDIATE
lookup the hash row matching S
UPSERT the inspection, bound to that revision
COMMIT                      <- evidence about V1's bytes, attributed under a
                               lock that never saw V2 arrive
```

The database lock cannot detect it: the archive is outside SQLite's
transaction boundary. The hasher already carries the full discipline, and the
inspection flow mirrors it statement for statement:

```text
1  carry the inspected PATH and the captured stat into the handler-owned
   transaction (the handler has the path already -- handlers.py:43 --
   but does not pass it onward today)
2  BEGIN IMMEDIATE (handler)
3  revalidate the location: same location id, same archive_id, same path
   -- the hasher's _assert_still_current (hashing.py:468)
4  re-stat the path and compare to the captured stat -- fail-fast, and
   deliberately redundant: the hasher records that removing this one alone
   fails no test, because step 6 subsumes it, and keeps it so a replacement
   noticed early does not first write rows and enqueue work it must undo
   (hashing.py:443-452). The same reasoning applies here, and is recorded
   for the same reason: an overlap nobody wrote down gets deleted later as
   dead weight.
5  the ownership lookup and the measurement UPSERT
6  RE-STAT THE PATH AFTER ALL WRITES AND IMMEDIATELY BEFORE COMMIT --
   the hasher's second _assert_file_matches (hashing.py:460-464). This is
   the check that closes the window above.
7  ABORT and roll back on disappearance or any stat disagreement. A
   deleted path raises from stat() rather than comparing, which is the
   correct outcome.
8  COMMIT
```

**(b) The write is not atomic.** `ArchiveInspectionRepository.save()` has no
`require_transaction` and runs in autocommit, so the ownership lookup, the
measurement UPSERT and any attribution update are three separate transactions.
A crash between them leaves an inspection whose basis does not describe its
own row.

The previous revision said `save()` "takes BEGIN IMMEDIATE ownership... with
the caller owning the transaction". Those cannot both be true, and the hasher
shows which one is right — the split is unambiguous there:

```text
handler      hashing.py:437   with transaction(self.connection):   <- OWNS it
repository   hashing.py:126   require_transaction(self.connection) <- REFUSES
                                                                      outside
```

```text
required   InspectArchiveHandler opens the transaction and owns commit and
           rollback. ArchiveInspectionRepository.save() only calls
           require_transaction() and refuses outside one; it opens nothing.

           This is a concrete interface change, not wording.
           InspectArchiveHandler.__init__ receives `connection`
           (handlers.py:29) but stores only
           self.repository = ArchiveInspectionRepository(connection)
           (handlers.py:33) -- it keeps no connection of its own and so
           cannot open a transaction today. It must hold one, as the hash
           handler does.
```

**(c) The late-binding predicate is too weak.** §7.4's four clauses match on
`archive_id` and the unresolved basis. That binds *any* unresolved inspection
of that archive to the revision the hasher just established — including one
taken from older bytes, which is precisely the wrong-generation error §7.6
withdrew the candidate pass over.

```sql
-- Run inside the hasher's transaction, after record_or_reuse, and ONLY for an
-- inspection whose own recorded stat equals the stat the hash just measured.
UPDATE archive_inspections
   SET source_revision_id = :revision_id,
       provenance_basis   = 'stat_matched_revision',
       updated_at         = CURRENT_TIMESTAMP
 WHERE archive_id                 = :archive_id
   AND source_revision_id         IS NULL
   AND provenance_basis           = 'unresolved_no_identity'
   AND inspected_file_size        = :hash_result_file_size
   AND inspected_modified_time_ns = :hash_result_modified_time_ns;
```

The last two clauses are the correction. Without them the statement says "this
archive has a revision now"; with them it says "this inspection read the same
bytes that revision describes", which is the only claim
`stat_matched_revision` is entitled to make. An inspection of older bytes fails
to match and stays unresolved — the same conservative outcome as §7.6.

This is the one place a slice-4 producer performs an attribution update on a
row it did not just write, which is why §7.4's predicate is a set of `AND`
clauses rather than a revision comparison.

**Candidates — `near_duplicate.py:505`.** Detection already opens its own
`BEGIN IMMEDIATE` and upserts each selected comparison.

```text
INSERT branch  BEFORE 4p: both sides unresolved_no_identity with NULL
               revisions, permanently (§7.6).
               FROM 4p: each side binds to the revision of the inventory the
               detector was explicitly given, basis
               inherited_from_page_evidence. The detector must therefore carry
               that revision/inventory id from the loader call into the write
               -- it has the value already (slice 2 PI-08) but does not thread
               it to the insert today, so this is a data-flow change.
UPDATE branch  keeps its existing WHERE review_status = 'pending_review'
               guard, AND gains §7.2's payload predicate. Both must hold: a
               reviewed row is still never touched, and a pending row whose
               metrics are unchanged is now a no-op rather than a rewrite.
review status  unchanged in every respect. review_status, reviewed_by and
               reviewed_at are `review` disposition (§9), mutable by the
               reviewer workflow, and no slice-4 guard touches them.
```

**The behaviour change worth stating plainly:** a detection rerun that produces
*different* metrics on a `pending_review` row now fails the job rather than
silently overwriting nine computed metrics. That is R4's intent and §11.4's
"refusal rather than loss" semantics, and it will surface operationally as a
failed near-duplicate run rather than as silent drift.

### 7.6 Unresolved candidate sides stay unresolved (R14, revised)

Candidates must carry a non-NULL basis from 015 (R12), but their authoritative
source — page evidence — does not gain ownership until 4p. The previous
revision answered that with a binding pass in 4p. **That pass is withdrawn: it
could bind a candidate to page evidence it never compared.**

The sequence, which the previous design accepted:

```text
candidate compares page set V1
page hashing replaces it with V2
4p mints the inventory for V2
the pass assigns V2's revision to a candidate that compared V1
```

Nothing in the row can distinguish the two. Measured — `metrics()`
(`near_duplicate.py:65-75`) returns `alignment_offset`,
`average_dhash_distance`, `average_phash_distance`, `compared_page_count`,
`dimension_match_ratio`, `median_pixel_area_a`, `median_pixel_area_b` and
`page_match_ratio`. **No inventory id, no content signature, no page digest.**
The table stores archive ids and metrics; it does not record which generation
of page evidence the comparison read. So the pass could not prove the binding
it was making, and the test the previous revision proposed — "a candidate
created during the window is bound" — would have certified exactly that unsafe
behaviour.

The flaw is not confined to the window, either. `_resolve` may return
`unresolved_no_identity` for a side of a *pre-015* candidate whose archive has
no single revision (planner lines 1009-1019), and the 3,000 backfilled rows
were bound "by the one-revision-per-archive census rather than from page
evidence" (slice 1 §7.4). Any later pass binding those from page evidence would
assign a provenance the comparison never used.

**Ruling: no RETROSPECTIVE binding, ever. Contemporaneous binding from 4p
onward.** The distinction is between repairing a row whose evidence is
unrecorded and attributing one whose evidence is in hand.

An earlier draft of this section over-corrected into "every candidate insert is
unresolved". That is wrong from 4p onward, because 4p changes what the detector
knows. Slice 2 §8.6.2 gives the page loader an `explicit_revision/inventory`
access path: "the caller must be *told* which generation, because there is no
defensible default... the loader takes the revision or inventory id as an
argument rather than inferring one" (`PI-08`). After 4p a comparison is
therefore between two *named* inventories, and slice 1 §8.2 assigns exactly
that case `inherited_from_page_evidence` — "each side takes the revision of the
page evidence it compared".

```text
created 015 -> 4p    unresolved_no_identity on both sides, NULL revisions.
                     Never retrospectively bound: the row does not record
                     which generation it compared, and nothing later can
                     supply that.
created after 4p     each side binds AT INSERT to the revision of the
                     inventory the detector was explicitly given, basis
                     inherited_from_page_evidence. The evidence is in hand at
                     write time, which is the whole difference.
pending unresolved   may bind ONLY during a fresh comparison of those same
  historical row      explicit inventories, and only when the complete
                     recomputed payload matches the stored payload. Then the
                     binding is contemporaneous with a comparison that
                     actually happened, not a guess about an old one.
reviewed, or         stay unresolved. A reviewed row is never rewritten
  payload mismatch   (its UPDATE guard), and a payload that no longer matches
                     is evidence the comparison changed.
```

**A slice-6 anchor does not change this.** An anchor written from slice 6
onward helps future auditability; it cannot retroactively prove which evidence
a row created before it compared. So it is not a deferred fix for the
pre-4p rows — those stay unresolved permanently — and the previous revision was
wrong to present it as one.

**The options, for the pre-4p rows specifically:**

```text
retrospective binding        REJECTED. Nothing records which generation was
                             compared, so any pass would be a guess presented
                             as a fact.
enforce quiescence           REJECTED. It would require no page hashing between
                             015 and 4p, across an interval whose length is an
                             operator's scheduling decision, with no way to
                             verify afterwards that it held. An assumption that
                             cannot be checked is not a defence.
leave them unresolved        TAKEN. Costs attribution on candidates created in
                             one bounded window; costs no correctness. §10
                             resolves an unresolved side conservatively, so
                             this understates rather than overstates.
```

An evidence anchor written at detection time remains worth having for
auditability, and slice 6 already reopens this table — but as future-facing
provenance, not as a repair for these rows.

## 8. Concurrency protocol

```text
1  verify quiescence: stop every application process and database writer, and
   VERIFY the stop
2  create the protected backup while quiescent, and verify it
3  on the dedicated executor connection, PRAGMA foreign_keys = OFF and ASSERT
   it reads back 0
4  BEGIN IMMEDIATE
5  revalidate the plan digest; load temp_slice4_plan; assert the three join
   counts; rebuild, backfill, install triggers, record the ledger (§12.1),
   reconcile (§12.2) -- statement by statement, never executescript()
6  COMMIT only after reconciliation passes
7  in `finally`, PRAGMA foreign_keys = ON and ASSERT it reads back 1
8  restart only the NEW producer code
```

Step 3 precedes the lock because it cannot take effect after it; step 7 is in
`finally` so the rollback path restores connection state too — an executor
leaving `foreign_keys` off after a failure hands the next caller a connection
with referential integrity silently disabled.

### 8.1 Three distinct defences, and what each does *not* do

```text
already-running writers          removed by VERIFIED QUIESCENCE (step 1)
newly launched ordinary commands prevented by R6's fail-closed abort, and only
                                 through the updated entrypoints
a writer that resumes after
  commit                         fails on the NOT NULL basis constraints (R12,
                                 §6.2), on the INSERT attempt, before its
                                 conflict branch runs -- changing no evidence
```

R6 is reached at migration discovery inside a starting process; it cannot
reach a process already running old code or already blocked on the lock. Only
the third defence covers a writer already holding a connection when 015
commits, and it exists only because R12 moved the constraint into slice 4.
`BEGIN IMMEDIATE` plus the 30-second `busy_timeout` is not one of the three.

```text
quiescence violated, found before commit   abort and roll back
quiescence violated, found after commit    remain offline, restore the backup
```

## 9. Disposition registries

R8's accounting: `archive_inspections` is 21 current + 4 slice-4 = **25**
(015 asserts 25/25), + 3 supersession = 28 (slice 5 asserts 28/28). Slice 1's
"28 of 28" describes the final shape including `superseded_at`,
`superseded_by_id` and `superseded_reason`, which slice 1 §5 records as
existing on no table.

```text
archive_hashes                                            14 of 14 after 015
  identity             id, archive_id, algorithm, algorithm_version
  attribution          source_revision_id, provenance_basis
  source_context       location_id
  measurement          digest, file_size, modified_time_ns, bytes_read,
                       hashed_at
  lifecycle_immutable  created_at
  lifecycle_mutable    updated_at

archive_content_signatures                                15 of 15 after 015
  identity             id, archive_id, algorithm, algorithm_version
  attribution          source_revision_id, provenance_basis
  source_context       location_id
  measurement          digest, page_count, image_bytes, source_file_size,
                       source_modified_time_ns, calculated_at
  lifecycle_immutable  created_at
  lifecycle_mutable    updated_at

archive_inspections                                       25 of 25 after 015
  identity             id, archive_id, inspector_version,
                       inspector_version_basis
  attribution          source_revision_id, provenance_basis
  source_context       location_id
  measurement          inspected_path, archive_format, status, entry_count,
                       page_count, directory_count, encrypted,
                       comic_info_present, comic_info_valid, comic_info_error,
                       comic_info_json, crc_verified, inspected_file_size,
                       inspected_modified_time_ns, result_json, inspected_at
  lifecycle_immutable  created_at
  lifecycle_mutable    updated_at
  (supersession        superseded_at, superseded_by_id, superseded_reason
                       -- slice 5, taking this table to 28 of 28)

near_duplicate_candidates                                 22 of 22 after 015
  identity             id, archive_a_id, archive_b_id, match_method
  attribution          revision_a_id, revision_b_id, provenance_basis_a,
                       provenance_basis_b
  source_context       (none -- this table has no location_id)
  measurement          similarity_score, page_match_ratio,
                       compared_page_count, page_count_a, page_count_b,
                       average_dhash_distance, average_phash_distance,
                       dimension_match_ratio, metrics_json
  review               review_status, reviewed_by, reviewed_at
  lifecycle_immutable  created_at
  lifecycle_mutable    updated_at
```

Verified against the schema built from 001..014: each union equals the actual
column set plus slice 4's additions, with no column assigned twice.

`location_id` is `source_context` (R9), excluded from the slice-4 guard.
DP-15..DP-17 remain slice-5 cases; DP-17 (a genuine `ON DELETE SET NULL`
cascade) passes *precisely because* `location_id` is excluded.

## 10. Trigger shape and protected sets (R11)

```sql
CREATE TRIGGER trg_<table>_results_immutable
BEFORE UPDATE ON <table>
FOR EACH ROW
WHEN NEW.<col> IS NOT OLD.<col> OR ...      -- measurement + created_at
BEGIN
    SELECT RAISE(ABORT, 'measurement results are immutable; record a replacement');
END;
```

```text
archive_hashes              digest, file_size, modified_time_ns, bytes_read,
                            hashed_at, created_at                        (6)
archive_content_signatures  digest, page_count, image_bytes,
                            source_file_size, source_modified_time_ns,
                            calculated_at, created_at                    (7)
archive_inspections         the 16 measurement columns + created_at     (17)
near_duplicate_candidates   the 9 measurement columns + created_at      (10)
```

**Deliberately excluded:** `algorithm`, `algorithm_version`,
`inspector_version`, `inspector_version_basis`, `match_method` — all
`identity`, whose mechanism is slice 5's (candidates', slice 6's);
`updated_at` (DP-08); `location_id` (R9).

**Accepted risk:** between 015 and slice 5 nothing structurally prevents a
producer rewriting `algorithm_version`. Accepted because producer versions and
methods are **frozen across the 4 → 4p → 5 interim** — an operational
assumption in §14's sense, written down rather than relied on silently.

## 11. Test plan

The 17 named cases cover two tables and no producer behaviour:

```text
DP-01..DP-07, DP-10   REJECTED rewrites            archive_inspections
DP-08, DP-09          ACCEPTED                     archive_inspections
VB-01..VB-04, VB-07   ACCEPTED basis pairings      near_duplicate_candidates
VB-05, VB-06          REJECTED basis pairings      near_duplicate_candidates
```

Required additional coverage:

```text
immutability, per table      digest / metric rewrite REJECTED; the measurement
                             timestamp rewritten alone REJECTED (R7); identical
                             rerun performs NO update and preserves it
attribution-only transition  an otherwise identical row takes a permitted
                             attribution update, preserving every measurement
NULL-blindness               a protected column going NULL->value and
                             value->NULL is REJECTED (proves IS NOT)
identity NOT protected here  an algorithm_version rewrite is NOT rejected by
                             the slice-4 guard -- the negative proving R11
R13 exact pairs              a seed row (migration_014_identity_seed) and a
                             drift row are NOT relabelled by the attribution
                             statement; only unresolved_no_identity ->
                             stat_matched_revision applies. This is the
                             reproduction of §7.4, asserted as a regression
R13 hashes                   the hasher issues NO attribution statement
hasher reordering (§7.1)     the revision exists before archive_hashes is
                             written; save() remains one transaction;
                             metadata_changed still reads file_locations before
                             the refresh
R14 pre-4p stays unresolved  a candidate created before 4p carries
                             unresolved_no_identity on both sides, and STILL
                             does after 4p completes -- the negative that
                             proves no retrospective pass exists
R14 post-4p binds at INSERT  a candidate created after 4p carries
                             inherited_from_page_evidence and the revision of
                             the inventory the detector was explicitly given,
                             per side, without any later pass
R14 fresh-comparison bind    a pending unresolved historical row binds only
                             when a fresh comparison of the same explicit
                             inventories reproduces the stored payload
                             completely; a payload mismatch leaves it
                             unresolved, and a REVIEWED row is untouched
                             either way
inspection ownership         save() refuses outside a transaction; the HANDLER
                             opens and owns it, and a handler-level rollback
                             discards lookup, UPSERT and attribution together
inspection post-read race    the file is replaced AFTER inspect_archive()
                             returns but BEFORE commit: the transaction aborts
                             and the stored inspection is unchanged. Proven by
                             replacing the file between the two, which is the
                             window the in-read before/after stat cannot see
R12 basis NOT NULL           an omitted basis is rejected on INSERT, on both
                             shapes, before the conflict branch, leaving the
                             evidence row unchanged
unresolved still legal       a (NULL revision, 'unresolved_*') row is accepted
rebuild copy validity        copy-then-update FAILS on row one; the plan-join
                             form succeeds; each of the three join assertions
                             is proven to fire by injecting one violation
same-second rerun            a rerun inside one second is still a no-op
rebuild fidelity, all four   ids preserved, counts equal, values
                             byte-identical, indexes by name,
                             foreign_key_check empty, disposition totality
                             (14 / 15 / 25 / 22), zero NULL bases
foreign_keys ordering        OFF inside the transaction is a no-op, so it is
                             set before BEGIN and the read-back asserted; the
                             finally-path restores it to 1
no executescript             the transaction is still open before COMMIT
ledger (R15)                 the executor refuses a ledger with a hole below
                             15, refuses when another protected migration is
                             also pending, applies exactly 015, and
                             schema_migrations grows by exactly one
reconciliation (R16)         planned and applied PROJECTIONS agree
                             field-for-field, including per-side mapping by
                             label; a single altered basis, revision, or side
                             label is detected, each injected separately.
                             The projection's field set is asserted to be
                             exactly §12.2's, so parameters_basis cannot enter
                             it -- the field that makes PlannedBinding itself
                             unusable here
hasher read/write set        the reorder leaves current_revision_id, file_size
                             and the revision lineage identical to the
                             unreordered path for the same input -- the
                             column-disjointness argument of §7.1 is a claim
                             about today's code, not an enforced invariant
signature stat match         exactly one revision-bound match binds
                             stat_matched_revision; zero and TWO OR MORE both
                             leave unresolved_no_identity, the second proven
                             with two rows sharing a stat
pre-commit revalidation      a location whose stat changed between the match
                             and COMMIT aborts rather than committing a
                             binding to bytes that are gone
inspection stat measured     inspect_archive captures the path's real stat
                             before and after, refuses on a difference, and the
                             stored inspected_file_size /
                             inspected_modified_time_ns come from THAT, not
                             from the caller's file_locations parameter
inspection first write       inspector_version_basis = 'known' with a non-NULL
                             version, never 'unknown_legacy'
late binding is stat-gated   an unresolved inspection whose recorded stat
                             EQUALS the hash result's stat is bound; one whose
                             stat DIFFERS stays unresolved even though the same
                             archive now has a revision -- the negative that
                             proves the two added clauses are load-bearing
signature re-stat            a file replaced on disk between the match and
                             COMMIT ABORTS; re-reading file_locations alone
                             does not detect it, which is why the check is a
                             filesystem stat
candidate UPDATE branch      a reviewed row is still never touched; a pending
                             row with unchanged metrics is a no-op; a pending
                             row with changed metrics FAILS
R15 ledger set equality      recorded {1..14,16} with discovered {1..15} is
                             REFUSED -- the reproduction of §12.1, asserted as
                             a regression; an unknown recorded version is
                             refused; 015 must be the next applicable
inspector default            the rebuilt column has NO default
migration-root disjointness  the two roots are disjoint (§4.1)
fail-closed (R6)             a newly launched ordinary command ABORTS while 015
                             is pending; the read-only path still works
concurrency (§8.1)           each of the four old producers is queued behind
                             the migration and proven to FAIL after commit,
                             changing no evidence row -- per producer
```

Per the injection-site gate, every guard is proven load-bearing by disabling
**it alone** and naming the tests that then fail, by name and count.

## 12. Protected executor

```text
inputs      approved plan artifact (JSON envelope + CSV bindings), its snapshot
            digest, its expected per-table counts, the backup path
refuses     plan digest mismatch; expected counts not matching; backup absent
            or unverified; quiescence unverified; any join assertion non-zero;
            any §5.8 check failing; the ledger preconditions of §12.1 unmet;
            reconciliation (§12.2) failing
flow        §8 steps 1-8, statement by statement via iter_sql_statements(),
            never executescript(); the transaction asserted open immediately
            before COMMIT
emits       a postflight artifact carrying, per table: the applied binding
            digest, the applied count, the §5.8 results, the disposition
            totals; the ledger transition; and the deliberately unapplied
            counts (page_inventory 58,437; parameters_basis all rows)
on failure  abort and roll back before commit; after commit, remain offline and
            restore the protected backup
```

### 12.1 The ledger, with no holes (R15)

The executor bypasses ordinary `apply_migrations()`, so nothing else writes the
ledger row. Inside the same transaction, in this order:

```text
1  discovered = discover_migrations(MIGRATIONS)       -- every numbered file
2  recorded   = applied_versions(connection)
3  REFUSE unless recorded == {v in discovered : v < 15}, as SET EQUALITY.
      Three holes close at once, and the previous revision's subset test
      closed only the first:
        a ledger holding only 14 fails, because 001..013 are missing
        an UNKNOWN recorded version fails: recorded - discovered must be
          empty, so a row for a migration file this tree does not have is
          refused rather than ignored
        an ALREADY-APPLIED LATER version fails: 16 in recorded is not in
          {v < 15}, so it is refused
4  REFUSE unless 015 is the NEXT APPLICABLE migration: min(discovered -
      recorded) == 15. Not merely a member of the pending set -- the next one.
5  REFUSE unless the pending protected set is exactly {15}
6  apply exactly 015 -- no other pending migration is applied here
7  INSERT INTO schema_migrations (version, name) VALUES (15, '015_<name>.sql')
8  verify schema_migrations grew by exactly one row
9  reconcile (§12.2), then commit

Step 3's set equality is what the subset test missed. Measured against the
previous revision's two checks:

```text
discovered = {1..15}, recorded = {1..14, 16}
  "all below 15 recorded"     -> True   (subset test passes)
  "pending protected == {15}" -> True   (pending == {15})
  => 015 would be applied AFTER 016 was already applied
```

Both documented checks passed on a ledger that had already run a later
migration. Set equality plus the next-applicable test refuses it twice
over.
```

`migration_version()` parses `015_*.sql` to **15** (`migrations.py:44`) and
`version` is an INTEGER PRIMARY KEY (`migrations.py:12`), so the row is the
same shape `apply_migrations()` would have written (`migrations.py:108`).

Both other orderings produce a split brain: never recorded means every ordinary
command sees 015 as pending and aborts forever under R6 — migrated and
unusable; recorded after commit reaches the same state via a crash between the
two. Inside the transaction, schema change and ledger row commit or roll back
together, which is the property `apply_migrations()` already relies on
(`migrations.py:105-107`).

### 12.2 The slice-4 applied projection (R16)

A count plus an implementation-chosen digest is not the §11.1 reconciliation
gate. But **`PlannedBinding.canonical_line()` cannot be reused directly**, and
the previous revision was wrong to say it could. Measured:

```text
PlannedBinding(table="near_duplicate_candidates", ..., values={"parameters_basis": ...})
    PlannerInvariantError: planned values do not match the table's artifact
    columns (missing ['archive_b_id'])
PlannedBinding(..., values={})
    PlannerInvariantError: ... (missing ['archive_b_id', 'parameters_basis'])

ARTIFACT_COLUMNS["near_duplicate_candidates"] == ('archive_b_id',
                                                  'parameters_basis')
```

`parameters_basis` is required by the planner's own invariant and is
**deliberately not written by slice 4** — it lands in slice 6 with the fields
that give it meaning. So a candidate binding reconstructed from post-015 state
cannot be a `PlannedBinding` at all: the column does not exist to read.

The answer is a projection, defined once and applied to both sides, rather
than two formats compared:

```text
name     slice-4 applied projection
version  "provenance-backfill-applied/1"
         Its own marker, NOT the planner's. A projection that borrowed
         PLAN_DIGEST_VERSION would compare equal across a change to either
         definition, which is the failure the planner's own marker exists to
         prevent.
```

**Fields, per binding.** Exactly what 015 writes, and nothing else:

```text
table         one of the four; page_inventory is excluded by construction
key_kind      always "row_id" in slice 4 -- the natural-key form is 4p's
key           the row id
archive_id    the row's archive_id
sides         ordered by label; for the three single-sided tables one side
              with label ""; for candidates two, labels "a" and "b"
  label
  archive_id
  source_revision_id
  provenance_basis
values        table-specific, restricted to columns 015 writes:
                archive_hashes              {}
                archive_content_signatures  {}
                archive_inspections         inspector_version,
                                            inspector_version_basis
                near_duplicate_candidates   archive_b_id
              parameters_basis is ABSENT by design, and its absence is
              asserted rather than assumed: a projection that grew it would
              mean slice 4 had started writing a slice-6 column.
```

**Framing, stated exactly**, because "canonical" without framing is not a
format:

```text
serialization  the planner's _canonical_json semantics: sorted keys, no
               inserted whitespace, delimiters escaped inside strings, nesting
               preserved so sides and values occupy their own scopes rather
               than flattening into one stream, and a missing value rendered
               null rather than "". Injective, which is the property that
               makes two distinct bindings unable to render identically.
line           one binding per line; the line IS the JSON object, with no
               surrounding delimiters to escape.
order          sorted by (table, key). Total, because the staging table's
               PRIMARY KEY (table_name, row_id) makes the pair unique.
document       version marker line, then "bindings|count=<n>", then the
               sorted lines.
join           one LF byte (0x0A) between lines, and one trailing LF byte,
               so no rendering can be a prefix of another. No CR: the
               document is joined as bytes, not written through a
               text-mode writer that might translate them. Specified as a
               byte value rather than as a backslash escape because the
               escape did not survive this document's own authoring -- it
               was emitted as a real line break, splitting the
               specification of the separator across the lines it was
               specifying.
digest         SHA-256 over the UTF-8 encoding of that document, lowercase hex.
per-table      the same framing restricted to one table's lines, so the
               postflight artifact carries a digest per table as well as one
               over all four.
```

**Both sides use this one function** -- the planned side projects the approved
artifact's bindings, the applied side projects rows read back from the rebuilt
tables. That gives a comparison of like with like, and it is explicitly **not**
a protection against a shared omission: one projector applied twice will drop
the same field from both sides and compare equal. What protects against that is
the independently asserted field registry of step 5 -- written out here, not
derived from the projector -- and the fault-injection tests of §11, which alter
one field at a time and require the mismatch to be reported.

Reconciliation, after the rebuilds and inside the transaction:

```text
1  project every planned slice-4 binding from the artifact
2  project every slice-4 row read back from the REBUILT tables
3  compare FIELD-FOR-FIELD, not digest-to-digest. The digests are compared
   too, but a mismatch must name the table, row id, field and both values --
   a digest that differs tells an operator nothing about what moved.
4  assert set equality both ways: every planned binding applied exactly once,
   and no applied binding absent from the plan
5  assert the projections' field sets are identical to the definition above,
   so a column added later cannot silently enter or leave the comparison
6  the per-table and whole-run digests go into the postflight artifact
```

Candidate sides are mapped **by label**: `sides[label='a']` to
`(revision_a_id, provenance_basis_a)` and `sides[label='b']` to
`(revision_b_id, provenance_basis_b)`, never by tuple position.

`page_inventory` bindings are excluded at step 1 by construction — their
`key_kind` is `archive_id` and their table is not among the four — and are
counted as deliberately unapplied rather than silently missing.

## 13. Required before production execution

**Not before design approval.** These are execution-time obligations; listing
them as approval blockers would make approval impossible by definition.

```text
re-measurement   §5.1, §5.2, §5.3, §6.2 and §7.2 are measured on the production
                 runtime (sqlite 3.40.1 / python 3.11.3 / win32) and dated
                 2026-09-01. Re-run them on the runtime as it stands the day
                 015 executes: a Python upgrade moves the bundled SQLite.
dry run          against a restored copy of the production database, producing
                 the full postflight artifact, before the real run.
```

## 14. Gates carried from the slice 3 review

**Platform-claim.** Measured, not reasoned about, and labelled. Earned twice:
three environment claims asserted from plausible reasoning on 2026-08-02 were
all wrong, and a file-id reuse claim held on Linux but not on win32 (0 of 5
cycles), which is why green Windows CI never exercised the failure.

**Injection-site.** A mechanism change must re-point the tests that inject
failures at the replaced call site. Slice 4 rebuilds four tables and rewrites
four producer paths — including a **statement reordering** inside
`ArchiveHashRepository.save()` (§7.1), which is exactly the case this gate was
written for: a test injecting at the old position of the `archive_hashes`
write would still pass while guarding nothing.

**Single-writer threat model.** Recorded at `c666014`: one cooperating writer
per namespace is an operational assumption, not a property of any path. §8.1 is
that gate applied here — three defences with different reach, none a substitute
for another. §10's frozen-producer-version window and §7.6's permanently unresolved
candidates are assumptions of the same kind, recorded the same way.

---

Nothing in slice 4 is applied by anyone but the operator, through the protected
executor, following §8 in full: dry run first, protected backup verified,
expected count plus snapshot digest, report before act, postflight
reconciliation, and stop if code, preflight, backup and postflight disagree.
