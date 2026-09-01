# Slice 4 — migration 015 design

**Status: design under review. No schema, no migration, no producer change,
nothing that touches production.**

Slice 1 is `docs/revision_aware_provenance_assessment.md`; slice 2 is
`docs/page_inventory_design.md`; slice 3 is the merged planner
(`comic_automation/archive/provenance_backfill_planner.py`, PR #88, merged at
`1941cdc`).

## 0. Corrections carried by this revision

Rounds 1–4 are the prior review passes.

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
DP-17 listed in the inspection protection set   3  it passes BECAUSE
                                                   location_id is excluded
the shown rebuild's INSERT ... SELECT NULL,NULL 4  CANNOT COPY ITS FIRST ROW.
                                                   Measured. Rebuilt via a
                                                   staged plan join (§5.4)
provenance_basis NOT NULL deferred to slice 5   4  moves into slice 4 (R12)
"R6's fail-closed abort removes that writer"    4  FALSE. R6 cannot reach an
                                                   already-running process (§8.1)
executor records nothing in schema_migrations   4  it must, in the same
                                                   transaction (§12.1)
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
```

## 2. Rulings

```text
R1   page-inventory digest  4p's; row id and every written value, created_at
                            and sealed_at included; frozen values compared
                            exactly, target states as predicates.
R2   natural-key mapping    4p records archive_id -> page_inventory.id in the
                            postflight artifact, validated one-to-one inside
                            the transaction.
R3   concurrency            full writer quiescence plus §8.
R4   candidate triggers     measurement immutability in slice 4; the rest in 6.
R5   the 17 cases           read, mapped, reproduced before approval (§11).
R6   ordinary commands      FAIL CLOSED while protected 015 is pending. This
                            stops NEWLY LAUNCHED commands only -- see §8.1.
R7   measurement timestamps hashed_at, calculated_at, inspected_at immutable;
                            a byte-identical rerun PRESERVES them. created_at
                            lifecycle-immutable. Only updated_at bookkeeping.
R8   28 vs 21               21 + 4 = 25 (015 asserts 25/25), + 3 supersession
                            = 28 (slice 5 asserts 28/28).
R9   location_id            source_context, not measurement. Excluded from the
                            slice-4 guard; DP-15..DP-17 stay slice-5 cases.
R10  conflict predicate     two independent axes, payload and attribution.
                            Generated measurement timestamps and created_at are
                            protected but never comparison inputs (§7.2).
R11  trigger scope          the slice-4 guard is measurement +
                            lifecycle_immutable, and does not pull slice 5's
                            identity immutability forward. Producer versions
                            and methods are frozen across 4 -> 4p -> 5.
R12  basis nullability      MOVES INTO SLICE 4. The rebuild removes the only
                            reason it was deferred. Every provenance_basis
                            (and _a / _b) is created NOT NULL, and
                            archive_hashes.source_revision_id is NOT NULL. The
                            other revision ids stay nullable, because
                            unresolved attribution is legitimate (§6.3).
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

`180,519 + 58,437 = 238,956`.

### 3.1 Roadmap accounting after R12

```text
slice 4   ownership keys, basis columns created NOT NULL, inspector version
          pair, the results-immutability triggers, four table rebuilds
slice 4p  page_inventory + archive_pages
slice 5   uniqueness / partial indexes, UPSERT -> append, supersession columns
          and lifecycle, identity immutability, the source_context
          parent-existence guard, the remaining trigger set.
          NO LONGER slice 5's: provenance_basis nullability tightening on
          these four tables -- R12 does it here. Slice 5 still rebuilds them,
          for uniqueness, supersession and the remaining triggers.
slice 6   near_duplicate_candidates parameters, its trigger set and indexes,
          parameters_basis. Its basis nullability is also done here by R12.
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

It defines its **own** `apply_migrations` (`scripts/db.py:107`) against
`DEFAULT_MIGRATIONS_DIR = <repo root>/migrations` (`scripts/db.py:12`), which
holds only `001_initial_schema.sql`. It never imports the `comic_automation`
implementation and will never discover 015. It stays out of the protected set;
dragging its unrelated schema under the protected executor would widen the
executor's blast radius to a database 015 never touches.

The residual risk is confusion between the two roots, answered by a regression
assertion — a test, not a comment:

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
                     The abort names the migration and the executor.
read-only path       a query-only path that never calls apply_migrations();
                     the only permitted way to inspect while 015 is pending.
protected executor   the only caller permitted to apply a protected migration
                     (§12).
```

## 5. Migration 015 rebuilds all four receiving tables

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
    -- and the WRONG answer (§6.2)

sqlite 3.40.1 | python 3.11.3 | Windows-10-10.0.26200-SP0 | 2026-09-01
```

The review measured the same composite-FK result on 3.53.1 / python 3.13 /
Linux, so it is not a version artifact. R12 makes the rebuild doubly required:
`NOT NULL` on a populated table is the same refusal.

### 5.2 Two rebuild hazards, measured on the production runtime

This machine is the production runtime (the office PC is the worker). Measured
**read-only with respect to production**, against scratch in-memory databases;
`G:\ComicAutomation\` was not opened.

**Hazard 1 — `PRAGMA foreign_keys` cannot be changed inside a transaction.**

```text
PRAGMA foreign_keys=OFF issued INSIDE  BEGIN IMMEDIATE -> still 1  (NO-OP)
PRAGMA foreign_keys=OFF issued OUTSIDE a transaction   -> 0        (applies)
```

Load-bearing because the repository enables foreign keys
(`connection.py:27`, `dal.py:123`). What it costs:

```text
rebuild with foreign_keys=ON   -> referencing child rows: 1 -> 0  (cascaded)
rebuild with foreign_keys=OFF  -> referencing child rows: 1 -> 1  (preserved)
```

`DROP TABLE` fires `ON DELETE CASCADE` on children when FKs are on. Today no
table references the four receiving tables (measured via
`PRAGMA foreign_key_list` across the schema built from 001..014), so nothing
is currently at risk. Recorded anyway: 4p adds `page_inventory` children,
slice 5/6 add more, and the ordering requirement is invisible in the SQL.

**Hazard 2 — `executescript()` implicitly commits.**

```text
in_transaction after execute()        : True
in_transaction after executescript()  : False   <- the transaction was COMMITTED
COMMIT afterwards    : cannot commit - no transaction is active
ROLLBACK afterwards  : cannot rollback - no transaction is active
rows surviving the attempted rollback : both -- unrollbackable
```

§8 requires one transaction committing only after reconciliation passes. A
single `executescript()` inside it silently ends the transaction and makes
everything before it unrollbackable. `apply_migrations()` is already safe —
it splits with `iter_sql_statements()` and calls `execute()` per statement
(`migrations.py:91,102`) — and the executor must do the same, with a test
asserting the transaction is still open immediately before `COMMIT`.

### 5.3 The copy cannot pass through an invalid state — measured

The previous revision's rebuild wrote the new table with

```sql
CHECK (source_revision_id IS NOT NULL AND provenance_basis IS NOT NULL)
```

and then copied with `SELECT ..., NULL, NULL FROM archive_hashes`, intending a
per-row `UPDATE` afterwards. That cannot work, and it was not a subtle failure:

```text
INSERT ... SELECT id, archive_id, digest, NULL, NULL FROM archive_hashes
  REJECTED: CHECK constraint failed:
            source_revision_id IS NOT NULL AND provenance_basis IS NOT NULL

sqlite 3.40.1 | win32 | 2026-09-01
```

The `INSERT` fails on the first row and the later `UPDATE` is unreachable.
R12 makes this worse, not better: with the columns declared `NOT NULL`, every
one of the four tables would fail the same way.

**Every row must enter the rebuilt table already valid**, which means the
binding has to be available *during* the copy. The executor therefore loads
the verified slice-4 projection into a staging table first and populates each
rebuilt table by joining old rows to their planned binding.

### 5.4 The rebuild, per table

```sql
-- Loaded from the approved artifact, after the plan digest is revalidated
-- (§8 step 5) and before any rebuild. TEMP, so it cannot outlive the
-- connection or be mistaken for schema.
CREATE TEMP TABLE temp_slice4_plan (
    table_name          TEXT    NOT NULL,
    row_id              INTEGER NOT NULL,
    source_revision_id  INTEGER,
    provenance_basis    TEXT    NOT NULL,
    revision_b_id       INTEGER,          -- candidates only
    provenance_basis_b  TEXT,             -- candidates only
    inspector_version   TEXT,             -- inspections only
    inspector_version_basis TEXT,         -- inspections only
    PRIMARY KEY (table_name, row_id)      -- duplicate plan keys impossible
);
```

Three assertions before anything is dropped, per table. Each is a count that
must be zero, not a spot check:

```text
unmatched old rows    SELECT count(*) FROM <t> o
                      LEFT JOIN temp_slice4_plan p
                        ON p.table_name='<t>' AND p.row_id=o.id
                      WHERE p.row_id IS NULL                        -> 0
unmatched plan rows   SELECT count(*) FROM temp_slice4_plan p
                      LEFT JOIN <t> o ON o.id=p.row_id
                      WHERE p.table_name='<t>' AND o.id IS NULL     -> 0
duplicate plan keys   structurally impossible (PRIMARY KEY above), and
                      asserted anyway against the artifact row count
```

Then, shown for `archive_hashes`:

```sql
-- OUTSIDE the transaction (hazard 1): PRAGMA foreign_keys = OFF, read back 0.
BEGIN IMMEDIATE;

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
    source_revision_id INTEGER NOT NULL,                        -- R12
    provenance_basis   TEXT    NOT NULL                         -- R12
        CHECK (provenance_basis IN ('measured',
                                    'migration_014_identity_seed')),
    FOREIGN KEY (archive_id)  REFERENCES archive_files(id)  ON DELETE CASCADE,
    FOREIGN KEY (location_id) REFERENCES file_locations(id) ON DELETE SET NULL,
    FOREIGN KEY (source_revision_id, archive_id)
        REFERENCES archive_revisions(id, archive_id)            -- NO ACTION
);

INSERT INTO archive_hashes_new
    (id, archive_id, location_id, algorithm, algorithm_version, digest,
     file_size, modified_time_ns, bytes_read, hashed_at, created_at,
     updated_at, source_revision_id, provenance_basis)
SELECT
     o.id, o.archive_id, o.location_id, o.algorithm, o.algorithm_version,
     o.digest, o.file_size, o.modified_time_ns, o.bytes_read, o.hashed_at,
     o.created_at, o.updated_at, p.source_revision_id, p.provenance_basis
FROM archive_hashes AS o
JOIN temp_slice4_plan AS p
  ON p.table_name = 'archive_hashes' AND p.row_id = o.id;

DROP TABLE archive_hashes;
ALTER TABLE archive_hashes_new RENAME TO archive_hashes;
CREATE INDEX idx_archive_hashes_digest ON archive_hashes(algorithm, digest);
```

Measured: the join form is accepted and produces the bound row where the
copy-then-update form failed.

`archive_hashes` carries no paired CHECK because both its bases are bound —
slice 1 removed `unresolved_no_identity` from its vocabulary, since the hasher
computes a digest and binds in the same transaction. That is what makes
`source_revision_id NOT NULL` correct here and wrong elsewhere.

The other three differ only in their column lists, vocabularies and CHECKs:

```sql
-- archive_content_signatures: revision nullable, basis NOT NULL (R12)
    source_revision_id INTEGER,
    provenance_basis   TEXT NOT NULL
        CHECK (provenance_basis IN ('stat_matched_revision',
                                    'migration_014_field_seed',
                                    'unresolved_drift',
                                    'unresolved_no_identity')),
    CHECK ((source_revision_id IS NOT NULL
            AND provenance_basis NOT LIKE 'unresolved%')
        OR (source_revision_id IS NULL
            AND provenance_basis LIKE 'unresolved%')),
    FOREIGN KEY (source_revision_id, archive_id)
        REFERENCES archive_revisions(id, archive_id),
    ... existing archive_id / location_id FKs ...
-- index: idx_content_signatures_digest(algorithm, algorithm_version,
--                                      digest, page_count)

-- archive_inspections: adds the version pair of §6.2
    source_revision_id INTEGER,
    provenance_basis   TEXT NOT NULL
        CHECK (provenance_basis IN ('stat_matched_revision',
                                    'single_revision_inherited',
                                    'unresolved_no_identity')),
    CHECK (<the same bound/unresolved pairing>),
    inspector_version       TEXT,
    inspector_version_basis TEXT NOT NULL          -- NO DEFAULT (§6.2)
        CHECK (inspector_version_basis IN ('known', 'unknown_legacy')),
    CHECK ((inspector_version_basis = 'known'
            AND inspector_version IS NOT NULL)
        OR (inspector_version_basis = 'unknown_legacy'
            AND inspector_version IS NULL)),
-- indexes: idx_archive_inspections_status(status),
--          idx_archive_inspections_path(inspected_path)

-- near_duplicate_candidates: two keys, the pairing twice, five existing CHECKs
    revision_a_id      INTEGER,
    revision_b_id      INTEGER,
    provenance_basis_a TEXT NOT NULL CHECK (provenance_basis_a IN (...)),
    provenance_basis_b TEXT NOT NULL CHECK (provenance_basis_b IN (...)),
    CHECK (<pairing on side A>),
    CHECK (<pairing on side B>),
    FOREIGN KEY (revision_a_id, archive_a_id)
        REFERENCES archive_revisions(id, archive_id),
    FOREIGN KEY (revision_b_id, archive_b_id)
        REFERENCES archive_revisions(id, archive_id),
    -- preserved: archive_a_id < archive_b_id, two ratio ranges, the nullable
    -- dimension ratio, the review_status vocabulary
-- index: idx_near_duplicate_review(review_status, similarity_score DESC)
```

Each table's vocabulary is **derived from slice 1 §9.4.2's union** by the
migration rather than restated by hand; the lists above are what that
derivation must produce, not a second source of truth.

### 5.5 What each rebuild preserves, and how it is verified

```text
row ids            preserved exactly (measured: ids 7 and 9 survive). The
                   binding digest is keyed on row id.
column values      copied byte-identically.
current uniqueness preserved as it is -- archive_id UNIQUE stays. The partial
                   indexes of §9.3 are slice 5's and must NOT appear here.
indexes            recreated by name after the RENAME (listed above).
existing CHECKs    preserved, all five on the candidate table.
existing FKs       preserved with their actions.
```

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
                           asserted anyway, since a constraint that is never
                           tested is a constraint nobody knows is there
```

## 6. Column and constraint shapes

### 6.1 The general vocabulary

```sql
provenance_basis TEXT NOT NULL                                      -- R12
    CHECK (provenance_basis IN (
        'measured', 'stat_matched_revision',
        'migration_014_identity_seed', 'migration_014_field_seed',
        'single_revision_inherited', 'inherited_from_page_evidence',
        'unresolved_drift', 'unresolved_no_identity'))
CHECK (
    (source_revision_id IS NOT NULL AND provenance_basis IN (<the six bound>))
 OR (source_revision_id IS NULL     AND provenance_basis LIKE 'unresolved%'))
```

Per-table vocabularies (slice 1 §9.4.2):

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

### 6.2 Inspections: the version pair, with no DEFAULT

```sql
inspector_version       TEXT
inspector_version_basis TEXT NOT NULL          -- NO DEFAULT
    CHECK (inspector_version_basis IN ('known', 'unknown_legacy'))
CHECK ((inspector_version_basis = 'known'          AND inspector_version IS NOT NULL)
    OR (inspector_version_basis = 'unknown_legacy' AND inspector_version IS NULL))
```

The `NOT NULL DEFAULT 'unknown_legacy'` form `ALTER TABLE` would accept is the
wrong answer: a persistent default silently labels every future inspection
that omits the column as legacy evidence — the precise false claim §6.5
refuses. The rebuild creates the column with no default, copies historical
rows as `unknown_legacy`, and requires the producer to supply `known`.

### 6.3 R12 closes the (NULL, NULL) hole structurally — measured

The paired CHECK alone accepts an all-NULL row, because `NULL LIKE
'unresolved%'` is NULL and SQLite accepts a CHECK that is not *false*. With
the basis declared `NOT NULL`, the hole closes at the column:

```text
archive_hashes shape (revision NOT NULL, basis NOT NULL):
  pre-cutover UPSERT on an EXISTING row : REJECTED
      NOT NULL constraint failed: archive_hashes.source_revision_id
  pre-cutover UPSERT inserting a NEW row: REJECTED  (same)
  evidence row unchanged                : True

nullable-revision shape (revision NULL allowed, basis NOT NULL):
  unresolved row stores (NULL, 'unresolved_drift')  -- legitimate, still works
  pre-cutover UPSERT on an existing UNRESOLVED row : REJECTED
      NOT NULL constraint failed: sigs.provenance_basis
  pre-cutover UPSERT inserting a new row           : REJECTED  (same)
  unresolved evidence row unchanged                : True

sqlite 3.40.1 | python 3.11.3 | Windows-10-10.0.26200-SP0 | 2026-09-01
```

Reproduced on the production runtime, matching the review's 3.53.1 / Linux
result. **The rejection happens on the INSERT attempt, before the conflict
branch runs**, so a pre-cutover producer that resumes after the migration
fails without touching any evidence row — which is the third defence in §8.1.

## 7. Producer cutover — four paths, same slice and release

SQL cannot change Python producers, so the migration and the producer change
ship and deploy together; §8 step 8 restarts only the new code.

```text
archive_hashes              archive/hashing.py:153
archive_content_signatures  archive/page_hashing.py:229  (and dal.py:535)
archive_inspections         archive/repository.py:76
near_duplicate_candidates   archive/near_duplicate.py:505
```

### 7.1 What the UPSERTs do today — measured

```text
archive_hashes              ON CONFLICT(archive_id) DO UPDATE
                              ... hashed_at = CURRENT_TIMESTAMP,
                                  updated_at = CURRENT_TIMESTAMP
archive_content_signatures  ON CONFLICT(archive_id) DO UPDATE
                              ... calculated_at = CURRENT_TIMESTAMP,
                                  updated_at = CURRENT_TIMESTAMP
archive_inspections         INSERT sets inspected_at = CURRENT_TIMESTAMP, AND
                            the conflict branch sets it as well
near_duplicate_candidates   ON CONFLICT(a, b, match_method) DO UPDATE
                              ... updated_at = CURRENT_TIMESTAMP
                              WHERE review_status = 'pending_review'
```

### 7.2 Two independent axes (R10)

```text
measurement payload differs
    -> attempt the measurement update -> the immutability trigger ABORTS
only attribution differs, via a permitted producer path
    -> update attribution ONLY; preserve every measurement value and timestamp
neither differs
    -> true no-op
```

**Generated measurement timestamps and `created_at` are protected against
direct rewrites but are never comparison inputs.** `updated_at` may change on
a real permitted update, never on a no-op.

Because the two axes are different statements, each producer issues two:

**Statement 1 — the measurement path.** The existing UPSERT, gaining the basis
columns (now required) and a `WHERE` over the payload only:

```sql
INSERT INTO archive_hashes (
    archive_id, location_id, algorithm, algorithm_version, digest,
    file_size, modified_time_ns, bytes_read,
    source_revision_id, provenance_basis)          -- required by R12
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

With the payload identical the `WHERE` is false, SQLite performs no update at
all, and `hashed_at`, `updated_at` and `location_id` are preserved untouched —
which is R7 and the second half of R9 in one mechanism. With the payload
different the update is attempted and the trigger aborts it: in slice 4 a
changed re-measurement is a refusal, and slice 5 turns it into an append.

`IS NOT`, not `<>`, for the same reason as the trigger: `location_id`,
`comic_info_error`, `dimension_match_ratio` and `inspected_file_size` are
nullable, and `<>` against NULL is NULL rather than true.

**Statement 2 — the attribution path.** Touches no measurement column, so the
trigger does not fire:

```sql
UPDATE archive_hashes
   SET source_revision_id = ?, provenance_basis = ?,
       updated_at = CURRENT_TIMESTAMP
 WHERE archive_id = ?
   AND (source_revision_id IS NOT ? OR provenance_basis IS NOT ?);
```

Its `WHERE` makes an unchanged attribution a no-op too, so `updated_at` does
not move on a rerun that changes nothing at all.

**Payload and attribution columns, enumerated per producer** rather than left
to a placeholder:

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
                            never inputs calculated_at, created_at,
                                         updated_at, location_id, algorithm,
                                         algorithm_version

archive_inspections         payload      inspected_path, archive_format,
                                         status, entry_count, page_count,
                                         directory_count, encrypted,
                                         comic_info_present,
                                         comic_info_valid, comic_info_error,
                                         comic_info_json, crc_verified,
                                         inspected_file_size,
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
hold: a byte-identical rerun performs no update and therefore cannot repoint
it, and a changed rerun aborts before any repoint commits.

The candidate producer keeps its `review_status = 'pending_review'` guard and
adds the payload predicate. **A behaviour change worth stating:** a detection
rerun producing different metrics on a pending row now *fails* rather than
silently overwriting nine computed metrics. That is R4's intent and §11.4's
"refusal rather than loss" semantics, and it will surface as a failed job
rather than as silent drift.

**Timestamps differ only once the clock advances.** `CURRENT_TIMESTAMP` has
second granularity: 200 immediate reads returned **1** distinct value. A rerun
inside the same second writes an identical timestamp and would slip past a
naive value comparison. The guard is on the write, not on the observed value —
which is why §11 tests a same-second rerun explicitly.

## 8. Concurrency protocol

```text
1  verify quiescence: stop every application process and database writer, and
   VERIFY the stop
2  create the protected backup while quiescent, and verify it
3  on the dedicated executor connection, PRAGMA foreign_keys = OFF and ASSERT
   it reads back 0 (hazard 1: it is a silent no-op inside a transaction)
4  BEGIN IMMEDIATE
5  revalidate the plan digest against the approved artifact; load
   temp_slice4_plan; assert the three join counts of §5.4; rebuild, backfill,
   install triggers, record the ledger (§12.1) and reconcile -- statement by
   statement, never executescript() (hazard 2)
6  COMMIT only after reconciliation passes
7  in `finally`, PRAGMA foreign_keys = ON and ASSERT it reads back 1
8  restart only the NEW producer code
```

The backup is taken while quiescent so it is a backup of a state nothing is
still changing. The plan is revalidated **after** the lock, because a
comparison made before it can be invalidated in between. Step 3 precedes the
lock because it cannot take effect after it, and step 7 is in `finally` so the
rollback path restores the connection state too — an executor that leaves
`foreign_keys` off after a failure hands the next caller a connection with
referential integrity silently disabled.

### 8.1 Three distinct defences, and what each does *not* do

The previous revision claimed "R6's fail-closed abort removes that writer".
**That is false**, and worth correcting precisely because it made one defence
look like three. R6 is a check reached at migration discovery, inside a
process that is starting up. It cannot reach a process that is already running
old code, and it cannot reach one already blocked on the database lock.

```text
already-running writers          removed by VERIFIED QUIESCENCE (step 1) --
                                 nothing else touches them
newly launched ordinary commands prevented by R6's fail-closed abort, and only
                                 through the updated entrypoints
a writer that nevertheless
  resumes after commit           fails on the NOT NULL basis constraints (R12,
                                 §6.3), on the INSERT attempt, before its
                                 conflict branch runs -- so it changes no
                                 evidence row
```

The third is the only one that works against a writer already holding a
connection when 015 commits, and it exists only because R12 moved the
constraint into slice 4. `BEGIN IMMEDIATE` plus the 30-second `busy_timeout`
(`connection.py:39`, `dal.py:131,146`) is not one of the three: it excludes a
concurrent writer only for the transaction's duration.

```text
quiescence violated, found before commit   abort and roll back
quiescence violated, found after commit    remain offline, restore the backup
```

## 9. Disposition registries — complete, all four tables

R8's accounting:

```text
archive_inspections   current schema                    21
                      slice 4 adds attribution+version  +4
                      slice-4 shape                     25   <- 015 asserts 25/25
                      slice 5 adds supersession          +3
                      final slice-5 shape               28   <- slice 1's 28/28
```

Slice 1's "28 of 28" describes the final shape including `superseded_at`,
`superseded_by_id` and `superseded_reason`, which slice 1 §5 records as
existing on no table and assigns to slice 5. No production read is needed.

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

### 9.1 `location_id` is `source_context` (R9)

Not measurement. For slice 4: excluded from the results-immutability trigger;
a byte-identical rerun performs no update and so does not repoint it; a
changed rerun aborts before any repoint commits; `ON DELETE SET NULL`
continues to work. Slice 5 adds the parent-existence guard. **DP-15, DP-16 and
DP-17 remain slice-5 cases** — DP-17 (a genuine cascade) passes *precisely
because* `location_id` is excluded from the slice-4 set.

## 10. Trigger shape and protected sets (R11)

The slice-4 guard protects **measurement ∪ lifecycle_immutable**, nothing else.

```sql
CREATE TRIGGER trg_<table>_results_immutable
BEFORE UPDATE ON <table>
FOR EACH ROW
WHEN NEW.<col> IS NOT OLD.<col> OR ...      -- measurement + created_at
BEGIN
    SELECT RAISE(ABORT, 'measurement results are immutable; record a replacement');
END;
```

`IS NOT` rather than `<>`, matching slice 1 §9.4.2's own trigger text.

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

**The accepted risk:** between 015 and slice 5, nothing structurally prevents a
producer rewriting `algorithm_version` on an existing row. Accepted because
producer versions and methods are **frozen across the 4 → 4p → 5 interim** —
an operational assumption in the sense §14 requires, written down rather than
relied on silently.

## 11. Test plan

The 17 named cases:

```text
DP-01..DP-07, DP-10   REJECTED rewrites            archive_inspections
DP-08                 updated_at rewrite ACCEPTED  archive_inspections
DP-09                 byte-identical ACCEPTED      archive_inspections
VB-01..VB-04, VB-07   ACCEPTED basis pairings      near_duplicate_candidates
VB-05, VB-06          REJECTED basis pairings      near_duplicate_candidates
```

Two tables, no producer behaviour. Required additional coverage:

```text
immutability, hashes         digest rewrite REJECTED; hashed_at alone REJECTED
                             (R7); identical rerun performs NO update and
                             preserves hashed_at
immutability, signatures     digest / page_count rewrite REJECTED;
                             calculated_at alone REJECTED; identical rerun
                             preserves it
immutability, candidates     metric rewrite on a pending_review row REJECTED;
                             identical rerun a no-op
attribution-only transition  an otherwise identical row takes a permitted
                             attribution update, preserving every measurement
                             value and timestamp (R10 axis 2)
NULL-blindness               a protected column going NULL->value and
                             value->NULL is REJECTED (proves IS NOT)
identity NOT protected here  an algorithm_version rewrite is NOT rejected by
                             the slice-4 guard -- the negative that proves R11
                             was applied rather than described
location_id (R9)             a byte-identical rerun does not repoint it; the
                             guard does not fire on it; DP-15..DP-17 are NOT
                             asserted in slice 4
R12 basis NOT NULL           an omitted basis is rejected on INSERT, on both
                             the NOT NULL-revision and nullable-revision
                             shapes, before the conflict branch runs, leaving
                             the evidence row unchanged
unresolved still legal       a (NULL revision, 'unresolved_*') row is accepted
                             -- the negative that proves R12 did not
                             over-tighten
rebuild copy validity        the copy-then-update form FAILS on the first row;
                             the plan-join form succeeds. The three join
                             assertions -- unmatched old rows, unmatched plan
                             rows, duplicate plan keys -- are each zero, and
                             each is proven to fire by injecting one violation
same-second rerun            a rerun inside one second is still a no-op
rebuild fidelity, all four   ids preserved, counts equal, values
                             byte-identical, indexes by name,
                             foreign_key_check empty, disposition totality
                             (14 / 15 / 25 / 22), zero NULL bases
foreign_keys ordering        OFF inside the transaction is a no-op, so the
                             executor sets it before BEGIN and asserts the
                             read-back; the finally-path restores it to 1
no executescript             the transaction is still open before COMMIT
ledger (§12.1)               014 present and 015 absent before; version 15
                             present after; schema_migrations grew by exactly
                             one; an ordinary command no longer aborts
inspector default            the rebuilt column has NO default
migration-root disjointness  the two roots are disjoint (§4.1)
fail-closed (R6)             a newly launched ordinary command ABORTS while 015
                             is pending and does not run against schema 014;
                             the read-only path still works
concurrency (§8.1)           each of the four old producers is deliberately
                             queued behind the migration and proven to FAIL
                             after commit, changing no evidence row -- run per
                             producer, not once, since each has its own
                             statement and its own required columns
```

Per the injection-site gate, every guard is proven load-bearing by disabling
**it alone** and naming the tests that then fail, by name and count. Three
guards written during the #32 work failed nothing when bypassed.

## 12. Protected executor

```text
inputs      approved plan artifact (JSON envelope + CSV bindings), its snapshot
            digest, its expected per-table counts, the backup path
refuses     plan digest mismatch; expected counts not matching; backup absent
            or unverified; quiescence unverified; any §5.4 join assertion
            non-zero; any §5.5 check failing; the ledger preconditions of
            §12.1 unmet; reconciliation failing
flow        §8 steps 1-8, statement by statement via iter_sql_statements(),
            never executescript(). The transaction is asserted open
            immediately before COMMIT.
emits       a postflight artifact carrying, per table: the binding digest, the
            applied count, the §5.5 results, and the disposition totals
            (14 / 15 / 25 / 22); the ledger transition; and the deliberately
            unapplied counts (page_inventory 58,437; parameters_basis all rows)
on failure  abort and roll back before commit; after commit, remain offline and
            restore the protected backup
```

### 12.1 The executor records the migration itself

The executor bypasses ordinary `apply_migrations()`, so nothing else will
write the ledger row. **Inside the same transaction**, and in this order:

```text
assert migration 014 is present in schema_migrations, and 015 is absent
apply exactly 015 -- no other pending migration is applied by this executor
INSERT INTO schema_migrations (version, name) VALUES (15, '015_<name>.sql')
verify schema_migrations grew by exactly one row
reconcile
commit
```

`migration_version()` parses `015_*.sql` to the integer **15**
(`migrations.py:44`), and `schema_migrations.version` is an INTEGER PRIMARY
KEY (`migrations.py:12`), so the ledger row is `(15, '015_….sql',
CURRENT_TIMESTAMP)` — the same shape `apply_migrations()` would have written
(`migrations.py:108`).

Both orderings other than this one produce a split brain:

```text
never recorded         every ordinary command sees 015 as still pending and
                       aborts forever under R6 -- the database is migrated and
                       unusable
recorded after commit  a crash between the two leaves 015 applied and
                       unrecorded, which is the same state, reached less
                       obviously
```

Recording it inside the transaction makes the schema change and its ledger row
commit or roll back together — which is exactly the property
`apply_migrations()` already relies on for every other migration
(`migrations.py:105-107`).

## 13. Required before approval

```text
re-measurement   §5.1, §5.2, §5.3, §6.3 and §7.2 are measured on the production
                 runtime (sqlite 3.40.1 / python 3.11.3 / win32) and dated
                 2026-09-01. Re-run them on the runtime as it stands the day
                 015 executes: a Python upgrade moves the bundled SQLite.
```

Everything else previously listed here is resolved: the disposition registries
are complete (§9), `location_id` is settled (§9.1), the 28-vs-21 accounting is
closed (R8), `scripts/db.py` is out of scope with a regression assertion
(§4.1), the rebuild SQL is written to §5.4's standard, the producer SQL is
written to §7.2's, and the ledger is specified (§12.1).

## 14. Gates carried from the slice 3 review

**Platform-claim.** Measured, not reasoned about, and labelled. Earned twice:
three environment claims asserted from plausible reasoning on 2026-08-02 were
all wrong, and a file-id reuse claim held on Linux but not on win32 (0 of 5
cycles), which is why green Windows CI never exercised the failure. Every
measurement here carries its build; §13 requires them re-run on the day.

**Injection-site.** A mechanism change must re-point the tests that inject
failures at the replaced call site. Slice 4 rebuilds four tables and rewrites
four producer paths, so every test injecting into a producer write is
re-pointed, and each new guard bypassed alone.

**Single-writer threat model.** Recorded at `c666014`: one cooperating writer
per namespace is an operational assumption, not a property of any path. §8.1
is that gate applied here — three defences with different reach, none of which
is a substitute for another, and the one that covers an already-running writer
is verified quiescence rather than any code path. §10's
frozen-producer-version window is an assumption of the same kind, recorded the
same way.

---

Nothing in slice 4 is applied by anyone but the operator, through the protected
executor, following §8 in full: dry run first, protected backup verified,
expected count plus snapshot digest, report before act, postflight
reconciliation, and stop if code, preflight, backup and postflight disagree.
