# Slice 4 — migration 015 design

**Status: design under review. No schema, no migration, no producer change,
nothing that touches production.**

Slice 1 is `docs/revision_aware_provenance_assessment.md`; slice 2 is
`docs/page_inventory_design.md`; slice 3 is the merged planner
(`comic_automation/archive/provenance_backfill_planner.py`, PR #88, merged at
`1941cdc`).

## 0. Corrections carried by this revision

Rounds 1–3 are the prior review passes.

```text
withdrawn claim                             round  disposition
slice 4 creates page_inventory                  1  4p owns the page tables
~58,432 inventory parents                       1  58,437 (slice 2 §4.5)
"producer requirement in the same migration"    1  same slice and release
candidate triggers deferred to slice 6          1  immutability lands here
twelve apply_migrations call sites              2  eleven; scripts/db.py is a
                                                   separate implementation
composite FK added by ALTER TABLE               2  syntax error; rebuild
inspector_version_basis added NOT NULL          2  refused; rebuild
"inspections insert inspected_at"               2  the UPSERT sets it on the
                                                   conflict branch too
a moved hashed_at / calculated_at is accepted   2  reversed by R7
28-vs-21 is an unresolved production mystery    3  NOT a mystery. 21 -> 25 ->
                                                   28 (R8, §9)
location_id disposition is unresolved           3  it is source_context, and
                                                   already decided (R9, §9.1)
"WHERE any protected column differs"            3  wrong on two counts; the
                                                   predicate has two axes (R10)
algorithm / algorithm_version in the slice-4    3  they are identity, which is
  protected set                                    slice 5's mechanism (R11)
DP-17 listed in the inspection protection set   3  it passes BECAUSE
                                                   location_id is excluded
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
```

## 2. Rulings

```text
R1   page-inventory digest  4p's. Its applied binding digest includes the row
                            id and every written value, created_at and
                            sealed_at included; frozen values compared exactly,
                            target states as predicates, a clock range
                            supplementary and never a substitute.
R2   natural-key mapping    4p records archive_id -> page_inventory.id in the
                            postflight artifact, validated one-to-one INSIDE
                            the transaction.
R3   concurrency            full writer quiescence plus §8.
R4   candidate triggers     measurement immutability in slice 4; the remainder
                            in slice 6.
R5   the 17 cases           read, mapped, reproduced before approval (§11).
R6   ordinary commands      FAIL CLOSED while protected 015 is pending; no
                            continuing against schema 014. Read-only
                            diagnostics use a query-only path that never calls
                            apply_migrations().
R7   measurement timestamps hashed_at, calculated_at, inspected_at are
                            immutable measurement facts; a byte-identical rerun
                            PRESERVES them. created_at is lifecycle-immutable.
                            Only updated_at is bookkeeping.
R8   28 vs 21               not production drift: 21 current + 4 slice-4 = 25,
                            + 3 supersession = 28. 015 asserts 25 of 25 for
                            inspections; slice 5 asserts 28 of 28 (§9).
R9   location_id            source_context on hashes, signatures and
                            inspections. NOT measurement. Excluded from the
                            slice-4 guard; DP-15..DP-17 stay slice-5 cases.
R10  conflict predicate     two independent axes -- measurement payload and
                            attribution. Generated measurement timestamps and
                            created_at are protected against direct rewrites
                            but are NEVER comparison inputs (§7.2).
R11  trigger scope          the slice-4 results guard protects
                            measurement + lifecycle_immutable, and does not
                            pull slice 5's identity-immutability forward.
                            Producer versions and methods are frozen across the
                            4 -> 4p -> 5 interim (§10).
```

R7 matters beyond tidiness: 4p uses `calculated_at` as the five zero-page
inventories' `extracted_at`, so a moved `calculated_at` corrupts a value 4p
depends on.

## 3. Scope: four tables, 180,519 field projections

Page tables are **slice 4p**, a separate migration and PR, ordered
4 → 4p → 5 (slice 2 §10.3).

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

Not in slice 4: page tables (4p); `parameters_basis` (6); uniqueness and
partial indexes, UPSERT→append, `provenance_basis NOT NULL`, identity
immutability, the `source_context` parent-existence guard (5); candidate
attribution, supersession, identity triggers and indexes (6); per-row
granularity resolution (7).

## 4. Migration 015 must be unreachable by an ordinary command

**Measured at `1941cdc`.** `apply_migrations()`
(`comic_automation/database/migrations.py:76`) discovers every numbered `.sql`
file and applies each unapplied one inside `BEGIN IMMEDIATE`, with no notion of
approval, backup or postflight. **Eleven call sites** invoke it:

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
implementation and will never discover
`comic_automation/database/migrations/015_*.sql`.

**Decision: it stays out of the protected set.** It operates on a separate
root migration directory and an unrelated schema, and dragging that schema
under the protected executor would widen the executor's blast radius to reach
a database 015 never touches.

The risk that decision leaves is *confusion between the two roots* — someone
placing 015 in the wrong directory, or repointing one implementation at the
other's directory, would produce either an unprotected apply or a silent
no-op. So a regression assertion is required:

```text
the two migration roots are disjoint and neither contains the other's files
scripts/db.py's DEFAULT_MIGRATIONS_DIR != comic_automation's MIGRATIONS
no protected migration id appears under the root migrations/ directory
comic_automation's migrations directory contains every protected id
```

This is a test, not a comment. A comment saying "these are different
directories" is exactly what stops holding the moment someone changes one.

### 4.2 Required shape, under R6

```text
protected set        015 declared protected IN CODE, not by filename, so a
                     rename cannot unprotect it.
ordinary path        apply_migrations() ABORTS when a protected migration is
                     pending -- it does not skip and continue against schema
                     014, because a command on the old schema during the window
                     is exactly the writer §8 exists to exclude. The abort names
                     the migration and the executor.
read-only path       a separate query-only path that never calls
                     apply_migrations(); the only permitted way to inspect the
                     database while 015 is pending.
protected executor   a dedicated operator CLI, the only caller permitted to
                     apply a protected migration, performing §8 and refusing if
                     any step is unsatisfied.
```

The abort, not the executor, is the load-bearing half.

## 5. Migration 015 rebuilds all four receiving tables

### 5.1 Why ALTER TABLE cannot do it — measured

```text
ALTER TABLE ev ADD FOREIGN KEY (rev_id, archive_id) REFERENCES ...
    OperationalError: near "FOREIGN": syntax error
ALTER TABLE ev ADD COLUMN rev_id INTEGER, FOREIGN KEY (rev_id, archive_id) ...
    OperationalError: near ",": syntax error
ALTER TABLE ev ADD COLUMN rev2 INTEGER REFERENCES parent(id)
    ACCEPTED   (single-column reference only)

ALTER TABLE t ADD COLUMN b TEXT NOT NULL
    REFUSED: Cannot add a NOT NULL column with default value NULL
ALTER TABLE t ADD COLUMN c TEXT NOT NULL DEFAULT 'unknown_legacy'
    ACCEPTED  -- and is the WRONG answer (§6.1)

sqlite 3.40.1 | python 3.11.3 | Windows-10-10.0.26200-SP0 | 2026-09-01
```

The review measured the same composite-FK result on sqlite 3.53.1 / python
3.13 / Linux, so it is not a version artifact.

**015 rebuilds all four tables.** No interim ownership triggers are
substituted: a trigger enforcing what a foreign key should enforce is a
different mechanism with different failure modes, and the contract asked for
the key. 180,519 rows; the 2.95-million-row page rebuild stays in 4p.

### 5.2 Two rebuild hazards, measured on the production runtime

This machine is the production runtime — the office PC is the worker
(`docs/engineering_decisions.md`). The measurements below were taken
**read-only with respect to production**: against scratch in-memory databases
on the production runtime. The production database at `G:\ComicAutomation\`
was not opened.

**Hazard 1 — `PRAGMA foreign_keys` cannot be changed inside a transaction.**

```text
PRAGMA foreign_keys=OFF issued INSIDE  BEGIN IMMEDIATE -> still 1  (NO-OP)
PRAGMA foreign_keys=OFF issued OUTSIDE a transaction   -> 0        (applies)

sqlite 3.40.1 | python 3.11.3 | win32 | 2026-09-01
```

This is load-bearing here because **the repository enables foreign keys**:
`PRAGMA foreign_keys = ON` at `database/connection.py:27` and
`database/dal.py:123`. So an executor that opens its transaction first and
then disables foreign keys gets a silent no-op and rebuilds with them on.

What that costs, measured:

```text
rebuild (CREATE new / INSERT SELECT / DROP old / RENAME) with
  foreign_keys=ON   -> referencing child rows: 1 -> 0   (silently cascaded)
  foreign_keys=OFF  -> referencing child rows: 1 -> 1   (preserved)
```

`DROP TABLE` fires `ON DELETE CASCADE` on children when foreign keys are on.
**Today no table references any of the four receiving tables** (measured via
`PRAGMA foreign_key_list` across the schema built from 001..014), so the
hazard does not currently destroy data. It is recorded anyway because 4p adds
`page_inventory` children and slice 5/6 add more, and because the ordering
requirement is invisible in the SQL: the `PRAGMA` line looks executed either
way.

**Hazard 2 — `executescript()` implicitly commits.**

```text
in_transaction after execute()        : True
in_transaction after executescript()  : False   <- the open transaction was COMMITTED
explicit COMMIT afterwards            : cannot commit - no transaction is active
ROLLBACK afterwards                   : cannot rollback - no transaction is active
rows surviving the attempted rollback : both -- unrollbackable

sqlite 3.40.1 | python 3.11.3 | win32 | 2026-09-01
```

§8 requires schema, backfill and reconciliation in **one** transaction that
commits only after reconciliation passes. A single `executescript()` anywhere
inside that transaction silently ends it and makes everything before it
unrollbackable — the failure would be discovered at the point where rollback
was supposed to save the database.

The existing `apply_migrations()` is already safe here: it splits with
`iter_sql_statements()` and calls `connection.execute()` per statement
(`migrations.py:91,102`). **The protected executor must do the same**, and a
test must prove the transaction is still open after the rebuild step.

### 5.3 What each rebuild preserves

```text
row ids            preserved exactly: INSERT INTO new SELECT id, ... FROM old.
                   The binding digest is keyed on row id, so a renumbered table
                   cannot reconcile. Measured: ids 7 and 9 survive a rebuild.
column values      copied byte-identically; verified in §5.5.
current uniqueness preserved AS IT IS -- archive_id UNIQUE stays. The partial
                   indexes of §9.3 are slice 5's and must NOT appear here.
indexes            recreated by name after the RENAME:
                     idx_archive_hashes_digest(algorithm, digest)
                     idx_content_signatures_digest(algorithm,
                       algorithm_version, digest, page_count)
                     idx_archive_inspections_status(status)
                     idx_archive_inspections_path(inspected_path)
                     idx_near_duplicate_review(review_status,
                       similarity_score DESC)
existing CHECKs    preserved. near_duplicate_candidates carries five:
                   archive_a_id < archive_b_id, two ratio ranges, the nullable
                   dimension ratio, and the review_status vocabulary.
existing FKs       preserved with their actions: archive_id -> archive_files
                   ON DELETE CASCADE, location_id -> file_locations
                   ON DELETE SET NULL.
new constraints    the composite ownership FK (NO ACTION), the table-specific
                   basis CHECK, and for inspections the §6.1 pair.
```

### 5.4 The rebuild, per table

Shown for `archive_hashes`; the other three follow the same shape with their
own columns, CHECKs and indexes.

```sql
-- OUTSIDE the transaction (hazard 1):
PRAGMA foreign_keys = OFF;

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
    source_revision_id INTEGER,
    provenance_basis   TEXT,
    FOREIGN KEY (archive_id)  REFERENCES archive_files(id)   ON DELETE CASCADE,
    FOREIGN KEY (location_id) REFERENCES file_locations(id)  ON DELETE SET NULL,
    FOREIGN KEY (source_revision_id, archive_id)
        REFERENCES archive_revisions(id, archive_id),          -- NO ACTION
    -- table-specific vocabulary (slice 1 §9.4.2): archive_hashes is the only
    -- table where 'measured' is legal, and it has no unresolved branch.
    CHECK (provenance_basis IN ('measured', 'migration_014_identity_seed')),
    CHECK (source_revision_id IS NOT NULL AND provenance_basis IS NOT NULL)
);

INSERT INTO archive_hashes_new
    (id, archive_id, location_id, algorithm, algorithm_version, digest,
     file_size, modified_time_ns, bytes_read, hashed_at, created_at,
     updated_at, source_revision_id, provenance_basis)
SELECT
     id, archive_id, location_id, algorithm, algorithm_version, digest,
     file_size, modified_time_ns, bytes_read, hashed_at, created_at,
     updated_at, NULL, NULL
FROM archive_hashes;

-- backfill exactly what slice 3 planned, from the approved artifact
UPDATE archive_hashes_new SET source_revision_id = ?, provenance_basis = ?
WHERE id = ?;   -- one per planned binding

DROP TABLE archive_hashes;
ALTER TABLE archive_hashes_new RENAME TO archive_hashes;
CREATE INDEX idx_archive_hashes_digest ON archive_hashes(algorithm, digest);

-- §5.5 verification, then the trigger of §10, then COMMIT.
```

`archive_hashes` takes the stricter paired CHECK above rather than the general
one, because slice 1 removed `unresolved_no_identity` from its vocabulary: the
hasher computes a digest and binds in the same transaction, so an unresolved
hash row is unreachable. That makes both remaining bases bound. The
`NOT NULL` tightening of `source_revision_id` this makes available is
**slice 5's**, not slice 4's — the CHECK above expresses it without changing
the column's declared nullability.

### 5.5 Verification after each rebuilt shape, inside the transaction

```text
PRAGMA foreign_key_check   must return no rows
row count                  new == old, against the recorded expected count
id set                     identical, not merely equal in cardinality
measurement values         byte-identical old vs new -- digests, counts, sizes,
                           metrics_json, result_json, and the three measurement
                           timestamps of R7
index set                  every index above present by name
disposition totality       PRAGMA table_info(t) column set == the union of that
                           table's disposition lists (§9)
```

A rebuild is reconciled before the transaction commits, never after.

## 6. Column and constraint shapes

The general vocabulary (slice 1 §9.2), from which each table's narrower CHECK
is **derived rather than restated by hand** — restating it by hand is how the
same omission was committed twice in slice 1's own drafts:

```sql
provenance_basis TEXT
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
                            unresolved_no_identity        (per side)
```

### 6.1 Inspections: the version pair, with no DEFAULT

```sql
inspector_version       TEXT
inspector_version_basis TEXT NOT NULL          -- NO DEFAULT
    CHECK (inspector_version_basis IN ('known', 'unknown_legacy'))
CHECK ((inspector_version_basis = 'known'          AND inspector_version IS NOT NULL)
    OR (inspector_version_basis = 'unknown_legacy' AND inspector_version IS NULL))
```

The `NOT NULL DEFAULT 'unknown_legacy'` form that `ALTER TABLE` would accept
is the wrong answer: a persistent default silently labels every future
inspection that omits the column as legacy evidence — the precise false claim
§6.5 refuses. The rebuild creates the final column with no default, copies
historical rows as `unknown_legacy`, and requires the producer to supply
`known` explicitly.

### 6.2 The paired CHECK accepts an unattributed row — measured

```text
unchanged producer (NULL, NULL)   ACCEPTED
bound + unresolved     (VB-05)    REJECTED
unbound + measured     (VB-06)    REJECTED

sqlite 3.40.1 | python 3.11.3 | win32
```

`NULL LIKE 'unresolved%'` is NULL, `true AND NULL` is NULL, `false OR NULL` is
NULL, and SQLite accepts a CHECK that is not *false*. **The constraint rejects
a lying row and accepts a silent one**, which is why §7 is mandatory. (On
`archive_hashes` the stricter CHECK of §5.4 does reject it, because both its
bases are bound; the other three tables need the producer.)

## 7. Producer cutover — four paths, same slice and release

SQL cannot change Python producers, so the migration and the producer change
ship and deploy together; §8.7 restarts only the new code.

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
                            the ON CONFLICT(archive_id) DO UPDATE branch sets
                            inspected_at = CURRENT_TIMESTAMP as well
near_duplicate_candidates   ON CONFLICT(a, b, match_method) DO UPDATE
                              ... updated_at = CURRENT_TIMESTAMP
                              WHERE review_status = 'pending_review'
```

### 7.2 The conflict predicate has two independent axes (R10)

The previous revision proposed one predicate over "any protected measurement
column", which is wrong twice over. Including the generated timestamps makes
every rerun after the clock advances attempt an update that the trigger then
rejects — even for identical results. Excluding them but keying only on
measurements makes a legal attribution-only transition impossible on an
otherwise identical row.

The executable rule:

```text
measurement payload differs
    -> attempt the measurement update -> the immutability trigger ABORTS
only attribution differs, via a permitted producer path
    -> update attribution ONLY; preserve every measurement value and timestamp
neither differs
    -> true no-op
```

**Generated measurement timestamps and `created_at` are protected against
direct rewrites but are never comparison inputs for conflict detection.**
`updated_at` may change on a real permitted update, never on a no-op.

The payload predicates, enumerated per producer rather than by placeholder:

```text
archive_hashes              payload:  digest, file_size, modified_time_ns,
                                      bytes_read
                            attribution: source_revision_id, provenance_basis
                            NOT inputs: hashed_at, created_at, updated_at,
                                        location_id, algorithm,
                                        algorithm_version

archive_content_signatures  payload:  digest, page_count, image_bytes,
                                      source_file_size,
                                      source_modified_time_ns
                            attribution: source_revision_id, provenance_basis
                            NOT inputs: calculated_at, created_at, updated_at,
                                        location_id, algorithm,
                                        algorithm_version

archive_inspections         payload:  inspected_path, archive_format, status,
                                      entry_count, page_count,
                                      directory_count, encrypted,
                                      comic_info_present, comic_info_valid,
                                      comic_info_error, comic_info_json,
                                      crc_verified, inspected_file_size,
                                      inspected_modified_time_ns, result_json
                            attribution: source_revision_id, provenance_basis
                            NOT inputs: inspected_at, created_at, updated_at,
                                        location_id, inspector_version,
                                        inspector_version_basis

near_duplicate_candidates   payload:  similarity_score, page_match_ratio,
                                      compared_page_count, page_count_a,
                                      page_count_b, average_dhash_distance,
                                      average_phash_distance,
                                      dimension_match_ratio, metrics_json
                            attribution: revision_a_id, revision_b_id,
                                         provenance_basis_a, provenance_basis_b
                            NOT inputs: created_at, updated_at, match_method,
                                        review_status, reviewed_by, reviewed_at
```

`location_id` is not a comparison input on any of them, which is what makes
R9 hold: a byte-identical rerun performs no update and therefore does not
repoint it.

**Timestamps differ only once the clock advances**, and this is why the guard
cannot be tested by timing. `CURRENT_TIMESTAMP` has second granularity: 200
immediate reads returned **1** distinct value (sqlite 3.40.1 / python 3.11.3 /
win32). A rerun inside the same second writes an identical timestamp and would
slip past a naive value comparison; a second later it would not. The guard is
on the write, not on the observed value.

## 8. Concurrency protocol

```text
0  PRAGMA foreign_keys = OFF   -- OUTSIDE the transaction (§5.2 hazard 1)
1  stop every application process and database writer, and VERIFY the stop
2  create the protected backup while quiescent, and verify it
3  acquire a fail-fast write lock (BEGIN IMMEDIATE)
4  recompute the approved plan and compare it, AFTER the lock is held
5  rebuild, backfill, install triggers and reconcile in ONE transaction,
   statement by statement -- never executescript() (§5.2 hazard 2)
6  commit only after reconciliation passes
7  PRAGMA foreign_keys = ON; restart only the NEW producer code
```

The backup is taken while quiescent so it is a backup of a state nothing is
still changing. The plan is recompared **after** the lock, because a
comparison made before it can be invalidated in between. Step 0 precedes the
lock because it cannot take effect after it.

**`BEGIN IMMEDIATE` plus the 30-second `busy_timeout` is not sufficient alone**
(`connection.py:39`, `dal.py:131,146`). It excludes a concurrent writer only
for the transaction's duration: an old writer that begins waiting during the
migration can acquire the lock and resume the instant 015 commits, writing the
(NULL, NULL) row of §6.2 through pre-cutover code against the post-migration
schema, with the backfill already reconciled and signed off. R6's fail-closed
abort removes that writer; the lock alone does not.

```text
quiescence violated, found before commit   abort and roll back
quiescence violated, found after commit    remain offline, restore the backup
```

## 9. Disposition registries — complete, all four tables

R8 resolves the count. There is no production-drift mystery:

```text
archive_inspections   current schema                 21
                      slice 4 adds attribution+version  +4
                      slice-4 shape                  25   <- 015 asserts 25/25
                      slice 5 adds supersession          +3
                      final slice-5 shape            28   <- slice 1's 28/28
```

Slice 1's "28 of 28" describes the final shape, including `superseded_at`,
`superseded_by_id` and `superseded_reason` — which slice 1 §5 confirms no
table has yet and assigns to slice 5. No production read is needed.

Dispositions per slice 1 §9.4.2: `identity`, `attribution`, `measurement`,
`source_context`, `lifecycle_immutable`, `lifecycle_mutable`, `supersession`,
plus `review` on the candidate table only.

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

Each total is asserted by §5.5's totality check, so a column added later with
no disposition fails the assertion instead of silently becoming mutable.

### 9.1 `location_id` is `source_context`, and already decided (R9)

It is not measurement. For slice 4:

```text
excluded from the results-immutability trigger
a byte-identical producer rerun performs no update, so it does not repoint it
a changed rerun aborts before any repoint commits
ON DELETE SET NULL continues to work
```

Slice 5 adds the parent-existence guard proving repoint rejected, direct clear
rejected, genuine cascade accepted. **DP-15, DP-16 and DP-17 remain slice-5
cases.** The previous revision listed DP-17 inside the inspection
measurement-protection set; that was wrong — DP-17 (a genuine
`ON DELETE SET NULL` cascade) passes *precisely because* `location_id` is
excluded from that set.

## 10. Trigger shape and protected sets (R11)

The slice-4 guard protects **measurement ∪ lifecycle_immutable**, and nothing
else. It does not pull slice 5's identity-immutability mechanism forward.

```sql
CREATE TRIGGER trg_<table>_results_immutable
BEFORE UPDATE ON <table>
FOR EACH ROW
WHEN NEW.<col> IS NOT OLD.<col> OR ...      -- measurement + created_at
BEGIN
    SELECT RAISE(ABORT, 'measurement results are immutable; record a replacement');
END;
```

`IS NOT` rather than `<>`, because these columns are nullable and `<>` against
NULL is NULL rather than true — `comic_info_error`, `dimension_match_ratio`
and `inspected_file_size` are all nullable, so a value→NULL rewrite would slip
past a NULL-blind comparison. This matches slice 1 §9.4.2's own trigger text.

```text
archive_hashes              digest, file_size, modified_time_ns, bytes_read,
                            hashed_at, created_at                       (6)
archive_content_signatures  digest, page_count, image_bytes,
                            source_file_size, source_modified_time_ns,
                            calculated_at, created_at                   (7)
archive_inspections         the 16 measurement columns of §9 + created_at (17)
near_duplicate_candidates   the 9 measurement columns of §9 + created_at (10)
```

**Removed from these sets, deliberately:** `algorithm` and
`algorithm_version` on hashes and signatures, `inspector_version` and
`inspector_version_basis` on inspections, and `match_method` on candidates.
All are `identity`, whose general immutability mechanism is slice 5's
(candidates', slice 6's). The previous revision had them in the slice-4 sets,
which mixed a final disposition with a slice-4 mechanism and is why those sets
could not yet be generated from the registry as claimed. They can now: the
slice-4 set is exactly `measurement ∪ lifecycle_immutable` from §9.

**The accepted risk this leaves, stated rather than left implicit:** between
015 and slice 5, nothing structurally prevents a producer from rewriting
`algorithm_version` on an existing row. It is accepted because producer
versions and methods are **frozen across the 4 → 4p → 5 interim** — no
algorithm, inspector or match-method version change ships in that window. That
is an operational assumption in the sense §14 requires, and it is written down
rather than relied on silently.

`updated_at` is excluded from every set (DP-08). `location_id` is in none
(R9).

Candidate immutability is in slice 4 (R4) because the current UPSERT carries
`WHERE review_status = 'pending_review'` and overwrites nine computed metrics
on exactly those rows; deferring to slice 6 would leave that open across two
slices.

## 11. Test plan

The 17 named cases:

```text
DP-01..DP-07, DP-10   REJECTED rewrites            archive_inspections
DP-08                 updated_at rewrite ACCEPTED  archive_inspections
DP-09                 byte-identical ACCEPTED      archive_inspections
VB-01..VB-04, VB-07   ACCEPTED basis pairings      near_duplicate_candidates
VB-05, VB-06          REJECTED basis pairings      near_duplicate_candidates
```

They cover two tables and no producer behaviour. Required additional coverage:

```text
immutability, hashes         digest rewrite REJECTED; hashed_at rewritten alone
                             REJECTED (R7); identical rerun performs NO update
                             and preserves hashed_at
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
                             slice-4 guard does not fire on it; DP-15..DP-17
                             are NOT asserted in slice 4
paired CHECK                 bound+unresolved and unbound+bound REJECTED per
                             table; (NULL, NULL) rejected on archive_hashes by
                             its stricter CHECK and NOT on the other three
same-second rerun            a rerun inside one second is still a no-op, so the
                             guard does not depend on the clock advancing
rebuild fidelity, all four   ids preserved, counts equal, values byte-identical,
                             indexes present by name, foreign_key_check empty,
                             disposition totality asserted (25/25 for
                             inspections, not 28/28)
foreign_keys ordering        setting it OFF inside the transaction is a no-op,
                             so the executor sets it before BEGIN -- asserted,
                             since the SQL looks identical either way
no executescript             the transaction is still open after the rebuild
                             step (proves hazard 2 avoided)
inspector default            the rebuilt column has NO default, so an omitted
                             value fails rather than silently becoming
                             unknown_legacy
migration-root disjointness  the two roots are disjoint; no protected id under
                             the root migrations/ directory (§4.1)
fail-closed (R6)             an ordinary auto-migrating command ABORTS while
                             015 is pending and does not run against schema
                             014; the read-only path still works
protected executor           apply_migrations refuses 015; the executor applies
concurrency (§8)             a writer that begins waiting during the migration
                             cannot commit a pre-cutover row afterwards
```

Per the injection-site gate, every guard is proven load-bearing by disabling
**it alone** and naming the tests that then fail, by name and count. Three
guards written during the #32 work failed nothing when bypassed.

## 12. Protected executor

```text
inputs      approved plan artifact (JSON envelope + CSV bindings), its snapshot
            digest, its expected per-table counts, the backup path
refuses     plan digest mismatch; expected counts not matching; backup absent
            or unverified; quiescence unverified; any §5.5 check failing;
            reconciliation failing
flow        §8 steps 0-7. Statement-by-statement execution via
            iter_sql_statements(), never executescript() (§5.2). The
            transaction is asserted open immediately before COMMIT.
emits       a postflight artifact carrying, per table: the binding digest, the
            applied count, the §5.5 results, and the disposition totals
            (14 / 15 / 25 / 22); plus the deliberately-unapplied counts
            (page_inventory 58,437; parameters_basis all rows)
on failure  abort and roll back before commit; after commit, remain offline and
            restore the protected backup
```

## 13. Required before approval

```text
producer diffs   before/after SQL for each of the four paths, expressing the
                 §7.2 two-axis predicate and the unresolved branch
rebuild SQL      the remaining three tables written out to §5.4's standard
re-measurement   §5.1, §5.2, §6.2 and §7.2 are measured on the production
                 runtime (sqlite 3.40.1 / python 3.11.3 / win32) and dated.
                 They should be re-run on the runtime as it stands on the day
                 015 executes, since a Python upgrade moves the bundled SQLite.
```

Everything else previously listed here is now resolved: the disposition
registries are complete (§9), `location_id` is settled (§9.1), the 28-vs-21
accounting is closed (R8), and `scripts/db.py` is out of scope with a
regression assertion specified (§4.1).

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
per namespace is an operational assumption, not a property of any path. Slice
4's exposure is larger, which is why §8 is a verified protocol and R6 makes the
ordinary path fail closed rather than trusting operator discipline. §10's
frozen-producer-version window is an assumption of the same kind, recorded the
same way.

---

Nothing in slice 4 is applied by anyone but the operator, through the protected
executor, following §8 in full: dry run first, protected backup verified,
expected count plus snapshot digest, report before act, postflight
reconciliation, and stop if code, preflight, backup and postflight disagree.
