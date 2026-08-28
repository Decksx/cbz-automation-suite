# Slice 4 — migration 015 design

**Status: design under review. No schema, no migration, no producer change,
nothing that touches production.**

Slice 1 is `docs/revision_aware_provenance_assessment.md`; slice 2 is
`docs/page_inventory_design.md`; slice 3 is the merged planner
(`comic_automation/archive/provenance_backfill_planner.py`, PR #88, merged at
`1941cdc`).

## 0. Corrections carried by this revision

Recorded rather than silently replaced. Rounds 1 and 2 are the two prior
review passes.

```text
withdrawn claim                             round  disposition
slice 4 creates page_inventory                  1  4p owns the page tables
~58,432 inventory parents                       1  58,437 (slice 2 §4.5)
"producer requirement in the same migration"    1  same slice and release
candidate triggers deferred to slice 6          1  immutability lands here
twelve apply_migrations call sites              2  ELEVEN; scripts/db.py is a
                                                   separate implementation (§4)
composite FK added by ALTER TABLE               2  syntax error; rebuild (§5)
inspector_version_basis added NOT NULL          2  refused on a populated
                                                   table; rebuild (§5.3)
"inspections insert inspected_at"               2  the UPSERT also sets it on
                                                   conflict (§7.1)
"the timestamps invariably differ"              2  they differ only once the
                                                   clock advances (§7.2)
a moved hashed_at / calculated_at is accepted   2  REVERSED by ruling R7 (§2)
```

## 1. Starting state, verified in the tree

Checked at `1941cdc` on 2026-08-28.

```text
migrations present        001..014 (.sql), comic_automation/database/migrations/
migration 015             absent
provenance_basis          absent from every migration
source_revision_id        absent from every migration
inspector_version         absent from every migration
page_inventory            no table
no ALTER TABLE targets any of the four receiving tables in 001..014
```

## 2. Rulings

```text
R1  page-inventory digest   4p's. Its applied binding digest includes the row
                            id and every written value, created_at and
                            sealed_at included. Frozen values compared exactly,
                            target states as predicates; a clock range is
                            supplementary, never a substitute.
R2  natural-key mapping     4p records archive_id -> page_inventory.id in the
                            postflight artifact, validating the one-to-one
                            mapping INSIDE the transaction.
R3  concurrency             full writer quiescence plus §8's sequence.
R4  candidate triggers      measurement immutability in slice 4; the remainder
                            in slice 6.
R5  the 17 cases            read, mapped and reproduced before approval, with
                            further tests where they do not reach (§11).
R6  ordinary commands       FAIL CLOSED. If protected 015 is pending, every
                            ordinary auto-migrating command aborts; it may not
                            continue against schema 014. Explicit read-only
                            diagnostics may use a separate query-only path that
                            never calls apply_migrations(). During the window,
                            only the protected executor runs.
R7  measurement timestamps  hashed_at, calculated_at and inspected_at are
                            immutable measurement facts. A byte-identical rerun
                            PRESERVES them -- it does not move them, because it
                            performs no measurement update at all. created_at
                            is lifecycle-immutable. Only updated_at is
                            bookkeeping and mutable.
```

R7 matters beyond tidiness: slice 4p uses `calculated_at` as the five
zero-page inventories' `extracted_at`, so a moved `calculated_at` would
corrupt a value 4p depends on.

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
partial indexes, UPSERT→append, `provenance_basis NOT NULL` (5); candidate
attribution, supersession, identity triggers and indexes (6); per-row
granularity resolution (7).

## 4. Migration 015 must be unreachable by an ordinary command

**Measured at `1941cdc`.** `apply_migrations()`
(`comic_automation/database/migrations.py:76`) discovers every numbered `.sql`
file and applies each unapplied one inside `BEGIN IMMEDIATE`, with no notion
of approval, backup or postflight. **Eleven call sites** invoke it:

```text
comic_automation/archive/cli.py:291                      comic_automation/archive/hash_cli.py:61
comic_automation/archive/duplicate_resolution_cli.py:202 comic_automation/archive/near_duplicate_cli.py:93
comic_automation/archive/page_hash_cli.py:64             comic_automation/archive/perceptual_hash_cli.py:69
comic_automation/archive/quarantine_cli.py:198           comic_automation/jobs/enqueue_missing_stages.py:176
comic_automation/library/cli.py:237                      comic_automation/service.py:159
scripts/benchmark_perceptual_hash_profiling.py:178
```

**`scripts/db.py` is not among them and must be recorded separately.** It
defines its *own* `apply_migrations` (`scripts/db.py:107`) against
`DEFAULT_MIGRATIONS_DIR = <repo root>/migrations` (`scripts/db.py:12`), which
contains only `001_initial_schema.sql`. It never imports the
`comic_automation` implementation and will never discover
`comic_automation/database/migrations/015_*.sql`.

That distinction is load-bearing for the protected set: **a protected-set
implementation living in `comic_automation.database.migrations` does not
govern `scripts/db.py`**, and must not be described as if it did. Counting it
as a twelfth caller, as the previous revision did, would have made the
protection look complete while leaving a second implementation outside it.
`scripts/db.py` needs its own decision — most simply, that it is confirmed to
operate only on the separate root-`migrations/` schema and is documented as
out of scope for the protected set.

### 4.1 Required shape, under R6

```text
protected set        015 declared protected IN CODE, not by filename, so a
                     rename cannot unprotect it.
ordinary path        apply_migrations() ABORTS when a protected migration is
                     pending. It does not skip it and continue against schema
                     014: a command running on the old schema while the
                     backfill is pending is exactly the writer §8 exists to
                     exclude. The abort names the migration and the executor.
read-only diagnostics  may use a separate query-only path that never calls
                     apply_migrations(). This is the only permitted way to
                     inspect the database while 015 is pending.
protected executor   a dedicated operator CLI, the only caller permitted to
                     apply a protected migration, performing §8 and refusing
                     if any step is unsatisfied.
artifact validation  the executor takes the approved plan artifact, its
                     expected counts and its snapshot digest, and refuses a
                     state that has moved.
```

The abort, not the executor, is the load-bearing half. An executor beside an
auto-apply path that still works has changed nothing.

## 5. Migration 015 rebuilds all four receiving tables

### 5.1 Why ALTER TABLE cannot do it — measured

```text
ALTER TABLE ev ADD FOREIGN KEY (rev_id, archive_id) REFERENCES ...
    OperationalError: near "FOREIGN": syntax error
ALTER TABLE ev ADD COLUMN rev_id INTEGER, FOREIGN KEY (rev_id, archive_id) ...
    OperationalError: near ",": syntax error
ALTER TABLE ev ADD COLUMN rev2 INTEGER REFERENCES parent(id)
    ACCEPTED   (single-column reference only)

sqlite 3.40.1 | python 3.11.3 | win32 | measured 2026-08-28
```

Independently measured on sqlite 3.53.1 / python 3.13 / Linux with the same
result, so this is not a version artifact. The accepted slice-4 contract
requires the **composite** ownership keys of §9.1 immediately, and no `ALTER
TABLE` form produces one.

**Ruling: 015 rebuilds all four tables.** No interim ownership triggers are
substituted — a trigger enforcing what a foreign key should enforce is a
different mechanism with different failure modes, and the contract asked for
the key. These are 180,519 rows; the 2.95-million-row page rebuild stays
isolated in 4p.

### 5.2 What each rebuild preserves

The classic twelve-step rebuild, with the preservation obligations named
because a rebuild that silently drops one is the failure mode:

```text
row ids            preserved exactly. INSERT INTO new SELECT id, ... FROM old
                   -- never a fresh rowid. The binding digest is keyed on row
                   id, so a re-numbered table cannot reconcile.
column values      copied byte-identically. Verified after (§5.4).
current uniqueness  preserved AS IT IS. archive_id UNIQUE stays; the partial
                   indexes of §9.3 are slice 5's and must NOT appear here.
indexes            recreated by name:
                     idx_archive_hashes_digest(algorithm, digest)
                     idx_content_signatures_digest(algorithm,
                       algorithm_version, digest, page_count)
                     idx_archive_inspections_status(status)
                     idx_archive_inspections_path(inspected_path)
                     idx_near_duplicate_review(review_status,
                       similarity_score DESC)
existing CHECKs    preserved. near_duplicate_candidates carries five --
                   archive_a_id < archive_b_id, two ratio ranges, the nullable
                   dimension ratio, and the review_status vocabulary.
existing FKs       preserved with their existing actions: archive_id ->
                   archive_files ON DELETE CASCADE, location_id ->
                   file_locations ON DELETE SET NULL.
new constraints    the composite ownership FK (NO ACTION), the table-specific
                   basis CHECKs, and for inspections the §6.5 pair.
```

`PRAGMA legacy_alter_table` and `PRAGMA foreign_keys` handling around the
rebuild is platform- and version-sensitive and is named in §13 as requiring
measurement on the production build rather than assumption.

### 5.3 `inspector_version_basis NOT NULL` needs the rebuild too — measured

```text
ALTER TABLE t ADD COLUMN b TEXT NOT NULL
    REFUSED: Cannot add a NOT NULL column with default value NULL
ALTER TABLE t ADD COLUMN c TEXT NOT NULL DEFAULT 'unknown_legacy'
    ACCEPTED
ALTER TABLE t ADD COLUMN d TEXT
    ACCEPTED

sqlite 3.40.1 | python 3.11.3 | win32 | measured 2026-08-28 (populated table)
```

The `DEFAULT 'unknown_legacy'` form is accepted and is **the wrong answer**: a
persistent default silently labels every future inspection that omits the
column as legacy evidence. That is the precise false claim §6.5 refuses — a
label describing evidence produced before the column existed, applied to
evidence produced after it.

The inspection rebuild therefore creates the final `NOT NULL` column **with no
default**, copies historical rows as `unknown_legacy`, and requires the new
producer to supply `known` explicitly.

### 5.4 Verification after each rebuilt shape

Per table, inside the same transaction:

```text
PRAGMA foreign_key_check       must return no rows
row count                      new == old, per table, against the recorded
                               expected count
measurement values             byte-identical old vs new: digests, counts,
                               sizes, metrics_json, result_json, and the three
                               measurement timestamps of R7
id set                         identical, not merely the same cardinality
index set                      every index above present by name
```

A rebuild is reconciled before the transaction commits, not after.

## 6. Column and constraint shapes

From slice 1 §9.1 and §9.2, quoted so the migration and the document cannot
drift apart.

```sql
source_revision_id INTEGER
FOREIGN KEY (source_revision_id, archive_id)
    REFERENCES archive_revisions(id, archive_id)      -- NO ACTION, never CASCADE

provenance_basis TEXT
    CHECK (provenance_basis IN (
        'measured', 'stat_matched_revision',
        'migration_014_identity_seed', 'migration_014_field_seed',
        'single_revision_inherited', 'inherited_from_page_evidence',
        'unresolved_drift', 'unresolved_no_identity'))
CHECK (
    (source_revision_id IS NOT NULL
     AND provenance_basis IN ('measured', 'stat_matched_revision',
                              'migration_014_identity_seed',
                              'migration_014_field_seed',
                              'single_revision_inherited',
                              'inherited_from_page_evidence'))
 OR (source_revision_id IS NULL
     AND provenance_basis LIKE 'unresolved%'))
```

`near_duplicate_candidates` takes two keys — `(revision_a_id, archive_a_id)`
and `(revision_b_id, archive_b_id)` — and the paired CHECK once per side.
Each table narrows the vocabulary (§9.4.2): `measured` is legal only on
`archive_hashes`. **The migration derives each table's CHECK from the union
rather than restating it by hand**, which is how the same omission was
committed twice in slice 1's own drafts.

`archive_inspections` additionally takes §6.5's pair:

```sql
inspector_version       TEXT
inspector_version_basis TEXT NOT NULL          -- no DEFAULT (§5.3)
    CHECK (inspector_version_basis IN ('known', 'unknown_legacy'))
CHECK ((inspector_version_basis = 'known'          AND inspector_version IS NOT NULL)
    OR (inspector_version_basis = 'unknown_legacy' AND inspector_version IS NULL))
```

All 59,541 historical rows are `unknown_legacy` with a NULL version.

### 6.1 The paired CHECK accepts an unattributed row — measured

```text
unchanged producer (NULL, NULL)      ACCEPTED
bound + measured                     ACCEPTED
unbound + unresolved_drift           ACCEPTED
bound + unresolved       (VB-05)     REJECTED
unbound + measured       (VB-06)     REJECTED

sqlite 3.40.1 | python 3.11.3 | win32 | measured 2026-08-28
```

`NULL LIKE 'unresolved%'` is NULL, `true AND NULL` is NULL, `false OR NULL` is
NULL, and SQLite accepts a CHECK that is not *false*. **The constraint rejects
a lying row and accepts a silent one**, which is why §7 is mandatory.

## 7. Producer cutover — four paths, same slice and release

SQL cannot change Python producers, so the requirement is that the migration
and the producer change ship and deploy together; §8.7 restarts only the new
code.

```text
archive_hashes              archive/hashing.py:153
archive_content_signatures  archive/page_hashing.py:229  (and dal.py:535)
archive_inspections         archive/repository.py:76
near_duplicate_candidates   archive/near_duplicate.py:505
```

```text
archive hashes        bind directly as 'measured' -- the only table where
                      'measured' is legal, being the only producer that
                      computes a digest.
content signatures    stat-match to a revision, or write
                      'unresolved_no_identity' honestly. Never (NULL, NULL).
inspections           'known' plus a non-NULL version, explicitly (§5.3);
                      initial attribution at write; later binding per §8.4.
candidates            both sides attributed honestly, per side.
```

### 7.1 What the UPSERTs do today — measured

```text
archive_hashes              ON CONFLICT(archive_id) DO UPDATE
                              ... hashed_at = CURRENT_TIMESTAMP,
                                  updated_at = CURRENT_TIMESTAMP
archive_content_signatures  ON CONFLICT(archive_id) DO UPDATE
                              ... calculated_at = CURRENT_TIMESTAMP,
                                  updated_at = CURRENT_TIMESTAMP
archive_inspections         INSERT sets inspected_at = CURRENT_TIMESTAMP,
                            AND the ON CONFLICT(archive_id) DO UPDATE branch
                            sets inspected_at = CURRENT_TIMESTAMP as well
near_duplicate_candidates   ON CONFLICT(a, b, match_method) DO UPDATE
                              ... updated_at = CURRENT_TIMESTAMP
                              WHERE review_status = 'pending_review'
```

The previous revision described the inspection producer as setting
`inspected_at` on insert only. It sets it on both branches, which makes it the
same defect as the other two rather than a lesser one.

### 7.2 The required producer change, under R7

Under R7 a byte-identical rerun must **preserve** all three measurement
timestamps. So the producer's conflict branch must perform **no measurement
update at all** when nothing measured has changed:

```text
DO UPDATE SET ... WHERE <any protected measurement column differs>
```

With that predicate false, SQLite performs no update, so `hashed_at`,
`calculated_at`, `inspected_at` and every measurement value are preserved
untouched — which is what R7 requires and what the immutability trigger will
then never see. When the predicate is true the rerun is a *changed*
re-measurement, and the trigger aborts it; slice 5 turns that refusal into an
append.

`updated_at` is bookkeeping and mutable (DP-08), so it may move — but a no-op
rerun does not move it either, and DP-08 accepts a rewrite rather than
requiring one.

**Timestamps differ only once the clock advances.** `CURRENT_TIMESTAMP` has
second granularity: 200 immediate reads returned **1** distinct value (sqlite
3.40.1 / python 3.11.3 / win32, 2026-08-28). So a fast rerun inside the same
second writes an identical timestamp and would slip past a naive
value-comparison guard, while the same rerun a second later would not. Saying
these rewrites *invariably* differ, as the previous revision did, would have
made the guard look testable by timing alone. It is not: the guard must be on
the write, not on the observed value.

### 7.3 An unresolved disposition: `location_id`

`location_id` can change without any measurement changing — a relocated
archive is the same bytes at a new path, and this repository has run
relocation repairs. If `location_id` is protected as a measurement, the guard
aborts a legitimate relocation update; if it is not, a rerun may silently
repoint evidence. DP-15 and DP-16 rule on repointing for
`archive_inspections` and assign both to slice 5. **The other three tables
have no ruling, and this design does not invent one** — §9 lists it.

## 8. Concurrency protocol

```text
1  stop every application process and database writer, and VERIFY the stop
2  create the protected backup while quiescent, and verify it
3  acquire a fail-fast write lock
4  recompute the approved plan and compare it, AFTER the lock is held
5  apply schema rebuilds, backfill and reconciliation in ONE transaction
6  commit only after reconciliation passes
7  restart only the NEW producer code, after postflight succeeds
```

The backup is taken while quiescent so it is a backup of a state nothing is
still changing. The plan is recompared **after** the lock, because a
comparison made before it can be invalidated between comparison and lock.

**`BEGIN IMMEDIATE` plus the 30-second `busy_timeout` is not sufficient alone**
(`database/connection.py:39`, `database/dal.py:131,146`). It excludes a
concurrent writer only for the transaction's duration: an old writer that
begins waiting during the migration can acquire the lock and resume the
instant 015 commits, writing the (NULL, NULL) row of §6.1 through
pre-cutover code against the post-migration schema, with the backfill already
reconciled and signed off. R6's fail-closed abort is what removes that writer;
the lock alone does not.

```text
quiescence violated, found before commit   abort and roll back
quiescence violated, found after commit    remain offline, restore the
                                           protected backup
```

## 9. Column dispositions

Required, and incomplete. Slice 1 §9.4.2 carries a disposition set for
`archive_inspections` and DP-01..DP-17 execute it.

**A discrepancy to resolve before the DP completeness assertion can be
trusted.** Slice 1 asserts "28 columns, 28 assigned, 0 missing, 0 unassigned"
for `archive_inspections`. Built from migrations 001..014 and measured with
`PRAGMA table_info`:

```text
archive_hashes                12 columns
archive_content_signatures    13 columns
archive_inspections           21 columns
near_duplicate_candidates     18 columns
```

No `ALTER TABLE` in 001..014 targets any of the four. 21 plus slice 4's four
new inspection columns is 25, still not 28. Either slice 1 counted a different
shape, or production has drifted from the migration files. **Production is
out of bounds to this design**, so this is reported, not resolved: the DP
completeness assertion must be re-established against whichever shape is
authoritative before it can be cited as a gate.

Dispositions for `archive_hashes`, `archive_content_signatures` and
`near_duplicate_candidates` do not exist at all and are required work (§13).
§7.2 and §7.3 show why: the disposition decides whether the guard aborts a
legitimate rerun or a relocation, and three of four tables have no ruling.

## 10. Trigger shape

One measurement-immutability trigger per table, `BEFORE UPDATE`, aborting when
a protected column would change value. The protected set per table follows the
dispositions of §9 and is therefore **not final** for three of them; the shape
is:

```sql
CREATE TRIGGER <table>_measurement_immutable
BEFORE UPDATE ON <table>
FOR EACH ROW
WHEN (<any protected column: NEW.col IS NOT OLD.col>)
BEGIN
    SELECT RAISE(ABORT, '<table>: measurement values are immutable; '
                        'a changed re-measurement must be an append (slice 5)');
END;
```

`IS NOT` rather than `<>`, because `<>` is NULL-blind and a column going
NULL→value or value→NULL would slip past it. Nullable columns exist on every
one of the four tables (`location_id`, `comic_info_error`,
`dimension_match_ratio`), so this is load-bearing rather than stylistic.

Protected sets, subject to §9:

```text
archive_hashes              digest, algorithm, algorithm_version, file_size,
                            modified_time_ns, bytes_read, hashed_at,
                            created_at
archive_content_signatures  digest, algorithm, algorithm_version, page_count,
                            image_bytes, source_file_size,
                            source_modified_time_ns, calculated_at, created_at
archive_inspections         per DP-01..DP-10 and DP-17; inspected_at and
                            created_at protected, updated_at excluded
near_duplicate_candidates   similarity_score, page_match_ratio,
                            compared_page_count, page_count_a, page_count_b,
                            average_dhash_distance, average_phash_distance,
                            dimension_match_ratio, metrics_json, created_at
```

`updated_at` is excluded from every set (DP-08). `location_id` is in none of
them pending §7.3.

Candidate immutability is in slice 4 (R4) because the current UPSERT carries
`WHERE review_status = 'pending_review'` and overwrites nine computed metrics
on exactly those rows; deferring the guard to slice 6 would leave that open
across two slices.

## 11. Test plan

The 17 named cases, mapped:

```text
DP-01..DP-07, DP-10   REJECTED rewrites            archive_inspections
DP-08                 updated_at rewrite ACCEPTED  archive_inspections
DP-09                 byte-identical ACCEPTED      archive_inspections
VB-01..VB-04, VB-07   ACCEPTED basis pairings      near_duplicate_candidates
VB-05, VB-06          REJECTED basis pairings      near_duplicate_candidates
```

They cover **two** tables and no producer behaviour. Required additional
coverage:

```text
immutability, archive_hashes         digest rewrite REJECTED; hashed_at
                                     rewritten alone REJECTED (R7); identical
                                     rerun performs NO update and preserves
                                     hashed_at
immutability, signatures             digest / page_count rewrite REJECTED;
                                     calculated_at rewritten alone REJECTED
                                     (R7); identical rerun preserves it
immutability, candidates             metric rewrite on a pending_review row
                                     REJECTED; identical rerun a no-op
NULL-blindness                       a protected column going NULL->value and
                                     value->NULL is REJECTED (proves IS NOT)
paired CHECK, three single-sided     bound+unresolved and unbound+bound
  tables                             REJECTED on each
the (NULL, NULL) hole                the post-cutover producer never writes it;
                                     the CHECK will not catch it (§6.1)
producer cutover, all four           each writes a legal basis on the path it
                                     already takes
same-second rerun                    a rerun inside one second is still a
                                     no-op, so the guard does not depend on
                                     the clock having advanced (§7.2)
rebuild fidelity, all four           ids preserved, counts equal, values
                                     byte-identical, indexes present by name,
                                     foreign_key_check empty (§5.4)
inspector default                    the rebuilt column has NO default, so an
                                     omitted value fails rather than silently
                                     becoming unknown_legacy (§5.3)
fail-closed (R6)                     an ordinary auto-migrating command ABORTS
                                     while 015 is pending, and does not run
                                     against schema 014; the read-only path
                                     still works
protected executor                   apply_migrations refuses 015; the executor
                                     applies it
concurrency (§8)                     a writer that begins waiting during the
                                     migration cannot commit a pre-cutover row
                                     afterwards
```

Per the injection-site gate, every guard is proven load-bearing by disabling
**it alone** and naming the tests that then fail, by name and count. Three
guards written during the #32 work failed nothing when bypassed — they were
unreachable defensive code, and only the bypass showed it.

## 12. Executor design

```text
inputs        approved plan artifact (JSON envelope + CSV bindings), its
              snapshot digest, its expected per-table counts, backup path
refuses       plan digest mismatch; expected counts not matching; backup
              absent or unverified; quiescence unverified; any §5.4 check
              failing; reconciliation failing
sequence      §8, steps 1-7
emits         a postflight artifact carrying the binding digest per table, the
              per-table applied counts, the deliberately-unapplied counts
              (page_inventory 58,437; parameters_basis all rows), and the
              §5.4 results per rebuilt table
on failure    abort and roll back before commit; after commit, remain offline
              and restore the protected backup
```

The executor is the only caller permitted to apply a protected migration, and
`apply_migrations()` aborts for everyone else (R6).

## 13. Required before approval

```text
dispositions          complete column coverage for archive_hashes,
                      archive_content_signatures, near_duplicate_candidates,
                      including the location_id ruling of §7.3
28-vs-21              resolve the archive_inspections column-count discrepancy
                      (§9) against whichever shape is authoritative
producer diffs        before/after SQL for each of the four paths, including
                      the §7.2 predicate and the unresolved branch
rebuild SQL           the full twelve-step text per table
re-measurement        §5.1, §5.3, §6.1 and §7.2 re-measured on the production
                      SQLite build and labelled with it; PRAGMA
                      legacy_alter_table / foreign_keys behaviour around the
                      rebuild measured rather than assumed
scripts/db.py         confirm it is out of scope for the protected set, or
                      bring it in (§4)
```

## 14. Gates carried from the slice 3 review

**Platform-claim.** Measured, not reasoned about, and labelled. Earned twice:
three environment claims asserted from plausible reasoning on 2026-08-02 were
all wrong, and a file-id reuse claim held on Linux but not on win32 (0 of 5
cycles), which is why green Windows CI never exercised the failure. Every
measurement in this document carries its build; §13 requires them repeated on
production's.

**Injection-site.** A mechanism change must re-point the tests that inject
failures at the replaced call site. Slice 4 rebuilds four tables and rewrites
four producer paths, so every test injecting into a producer write is
re-pointed, and each new guard bypassed alone.

**Single-writer threat model.** Recorded at `c666014`: one cooperating writer
per namespace is an operational assumption, not a property of any path. Slice
4's exposure is larger, which is why §8 is a verified protocol and R6 makes
the ordinary path fail closed rather than trusting operator discipline.

---

Nothing in slice 4 is applied by anyone but the operator, through the
protected executor, following §8 in full: dry run first, protected backup
verified, expected count plus snapshot digest, report before act, postflight
reconciliation, and stop if code, preflight, backup and postflight disagree.
