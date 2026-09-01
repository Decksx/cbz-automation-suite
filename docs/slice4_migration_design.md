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
                            predicate carries this until slice 5's transition
                            triggers land, because those triggers reference
                            supersession columns slice 4 does not have.
R14  candidate 4->4p window a candidate created between 015 and 4p writes
                            unresolved_no_identity on BOTH sides, and is bound
                            after 4p by an explicit binding pass (§7.5).
                            Joining the content signature instead would be a
                            new ruling and is NOT taken here.
R15  ledger precondition    refuse unless the pending protected set is exactly
                            {15} and every discovered version below 15 is
                            recorded (§12.1).
R16  applied binding digest reuses the planner's canonical binding rendering;
                            reconciliation is field-for-field over reconstructed
                            bindings, not a count plus a digest (§12.2).
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
slice 4p  page_inventory + archive_pages, then the candidate binding pass (R14)
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

The reorder is safe, and that is a property of the code rather than an
assumption:

```text
save() already runs entirely inside one transaction (require_transaction at
the top), so moving statements within it changes no durability boundary.

record_or_reuse() touches ONLY archive_revisions -- revision_with_digest(),
_append() and current_for() all read or write that table (dal.py:752-790).
It does not read archive_hashes, file_locations or archive_files, so moving
it earlier breaks no read-after-write dependency.

The one ordering constraint that must be preserved: `metadata_changed` reads
file_locations BEFORE the UPDATE that refreshes it. That read is already the
first statement and stays there.
```

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
attribution-only update          -> applied; measurements + hashed_at preserved
attribution rerun                -> rows changed: 0
```

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

### 7.5 Candidate attribution in the 4 → 4p window (R14)

Candidates must write non-NULL bases from 015 (R12), but their authoritative
source — page evidence — does not gain ownership until 4p. A candidate created
in that window has nothing to inherit from.

```text
during 4 -> 4p   a newly created candidate writes unresolved_no_identity on
                 BOTH sides, with both revision ids NULL. That is honest: the
                 evidence it would inherit from carries no ownership yet.
after 4p         an explicit binding pass performs the per-side transition
                 unresolved_no_identity -> inherited_from_page_evidence for
                 every candidate side whose page evidence has since bound.
                 It is 4p's responsibility and named in 4p's gate, not left to
                 be noticed.
```

**Deliberately not taken:** binding the candidate by joining
`archive_content_signatures` instead of page evidence. That would give the side
a different provenance than slice 1 §8.2 assigns it — signatures are
stat-matched, page evidence is what the comparison actually read — and it would
need `stat_matched_revision` in a vocabulary that does not contain it. It is a
**new ruling**, not an implementation detail, and this document does not take
it.

The interim cost, stated: candidates created between 015 and 4p are unresolved
on both sides until the pass runs. §10 resolves an unresolved side
conservatively, so this understates attribution rather than overstating it.

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
R14 window                   a candidate created before 4p carries
                             unresolved_no_identity on both sides; after the 4p
                             pass its bound sides carry
                             inherited_from_page_evidence
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
reconciliation (R16)         planned and reconstructed applied bindings agree
                             FIELD-FOR-FIELD, including per-side mapping;
                             a single altered basis, revision, or side label is
                             detected, each injected separately
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
3  REFUSE unless {v in discovered : v < 15} is a SUBSET of recorded
      -- "014 present" is not enough: a ledger holding only 14 passes that
         test while 003 was never applied
4  REFUSE unless the pending protected set is exactly {15}
      -- pending = discovered - recorded. If it holds another protected id,
         or 015 is absent from it, the executor is being asked for something
         other than what was approved
5  apply exactly 015 -- no other pending migration is applied here
6  INSERT INTO schema_migrations (version, name) VALUES (15, '015_<name>.sql')
7  verify schema_migrations grew by exactly one row
8  reconcile (§12.2), then commit
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

### 12.2 The applied binding digest is the planner's own form (R16)

A count plus an implementation-chosen digest is not the §11.1 reconciliation
gate. The applied side reuses the planner's canonical rendering rather than
inventing a second one:

```text
representation   PlannedBinding.canonical_line(), which renders through
                 _canonical_json -- injective, delimiters escaped inside
                 strings, sides nested in their own scope rather than
                 flattened, a missing value rendered null rather than "".
                 (planner lines 361-392.)
version marker   PLAN_DIGEST_VERSION = "provenance-backfill-plan/2" and
                 PLANNER_VERSION = "provenance-backfill-planner/2" travel with
                 it, so a rendering change cannot silently compare equal.
fields           table, key_kind, key, archive_id, bound, and per side:
                 label, archive_id, source_revision_id, provenance_basis;
                 plus the table-specific frozen `values`.
side mapping     the candidate table's two sides are the planner's
                 sides[label='a'] and sides[label='b'], mapped to
                 (revision_a_id, provenance_basis_a) and
                 (revision_b_id, provenance_basis_b). The mapping is by LABEL,
                 never by tuple position.
```

Reconciliation, after the rebuilds and inside the transaction:

```text
1  reconstruct a PlannedBinding for every slice-4 row from the REBUILT tables,
   using the same key_kind ('row_id') the planner used
2  render both sides through canonical_line()
3  compare FIELD-FOR-FIELD, not digest-to-digest: the digests are compared too,
   but a mismatch must name the table, row id, field and both values, because
   a digest that differs tells an operator nothing about what moved
4  assert set equality both ways -- every planned binding applied exactly once,
   and no applied binding absent from the plan
5  the per-table applied digest is SHA-256 over the sorted canonical lines for
   that table, and goes into the postflight artifact
```

Sorting is by `(table, key)`, which is total because the staging table's
primary key made `(table_name, row_id)` unique.

`page_inventory` bindings are excluded from step 1 by construction — their
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
for another. §10's frozen-producer-version window and §7.5's interim unresolved
candidates are assumptions of the same kind, recorded the same way.

---

Nothing in slice 4 is applied by anyone but the operator, through the protected
executor, following §8 in full: dry run first, protected backup verified,
expected count plus snapshot digest, report before act, postflight
reconciliation, and stop if code, preflight, backup and postflight disagree.
