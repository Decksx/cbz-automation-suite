# Slice 4 — migration 015 design

**Status: design under review. No schema, no migration, no producer change,
nothing that touches production.**

Slice 1 is `docs/revision_aware_provenance_assessment.md`; slice 2 is
`docs/page_inventory_design.md`; slice 3 is the merged planner
(`comic_automation/archive/provenance_backfill_planner.py`, PR #88, merged at
`1941cdc`).

## 0. What the first draft got wrong

Recorded rather than silently replaced, because the corrections change the
shape of the slice and a reader comparing revisions is entitled to know which
claims were withdrawn.

```text
claim in the first draft                    disposition
slice 4 creates page_inventory              WRONG. Slice 2 §10.3 ordered
                                            4 -> 4p -> 5; 4p owns the page
                                            tables. Withdrawn (§2, §3).
~58,432 inventory parents                   WRONG figure, taken from slice 1
                                            line 577. Slice 2 §4.5 froze
                                            58,437: the five zero-page
                                            archives are extractions, not
                                            absences. Withdrawn.
the digest asymmetry is slice 4's first     WRONG. It is 4p's, and the lead
question                                    has since ruled on it (§11).
"the producer requirement, in the same      INCOMPLETE and misleading. SQL
migration"                                  cannot change Python producers,
                                            and the requirement covers four
                                            producers, not one (§6).
candidate triggers all deferred to slice 6  WRONG. Measurement immutability
                                            lands here (§7); the lead has
                                            ruled.
```

The first draft also treated "what runs while 015 runs" as a question to ask.
It is a protocol to specify, and §8 specifies it.

## 1. Starting state, verified in the tree

Every claim checked at `1941cdc` on 2026-08-28 rather than taken from a
document.

```text
migrations present        001..014, as .sql under
                          comic_automation/database/migrations/
migration 015             absent
provenance_basis          absent from every migration
source_revision_id        absent from every migration
inspector_version         absent from every migration
page_inventory            no table
slice 3 planner           merged, basis vocabulary and natural-key handling
                          for page_inventory present
```

Slice 4 is genuinely unbuilt.

## 2. Scope: four tables, 180,519 field projections

Slice 4 covers the four receiving tables that already hold their rows. The
page tables are **slice 4p**, a separate migration and PR, ordered
4 → 4p → 5 by the lead (slice 2 §10.3): 4p creates `page_inventory`, mints
58,437 rows, rebuilds `archive_pages`, and cuts over producers and consumers
atomically.

```text
table                          rows slice 4 applies
archive_hashes                              59,541
archive_content_signatures                  58,437
archive_inspections                         59,541
near_duplicate_candidates                    3,000
                                           -------
slice-4 total                              180,519

planned total (slice 3)                    238,956
page_inventory bindings, deferred to 4p     58,437
```

`180,519 + 58,437 = 238,956`, which is the arithmetic the gate has to
reconcile against.

**Not in slice 4:**

```text
page_inventory / archive_pages    slice 4p
parameters_basis                  slice 6, with the fields that give it meaning
uniqueness / partial indexes      slice 5
UPSERT -> append                  slice 5
provenance_basis NOT NULL         slice 5, via the table rebuild
candidate attribution/supersession/
  identity triggers and indexes   slice 6 (measurement immutability is here)
per-row granularity resolution    slice 7, deferred (§12.2)
```

## 3. The gate

Because slice 4 deliberately leaves 58,437 planned bindings unapplied, the
gate cannot say "every binding in the plan was applied". It says:

```text
every slice-4 FIELD PROJECTION was applied exactly once
no applied binding exists that the plan did not contain
per-table totals match the plan's slice-4 totals (the four rows above)
rows the plan marked unresolved carry NULL and the planned reason
page_inventory bindings are deliberately unapplied and counted as such
parameters_basis is deliberately unwritten and counted as such
```

Two digests, per slice 1 §11.1:

```text
plan digest      slice 3's, over pre-migration inputs. Recomputed and compared
                 immediately before 015 acts; a change means the database moved
                 under the review.
binding digest   computed after, per table, over (table, row id, every column
                 015 wrote):

  archive_hashes              source_revision_id, provenance_basis
  archive_content_signatures  source_revision_id, provenance_basis
  archive_inspections         source_revision_id, provenance_basis,
                              inspector_version, inspector_version_basis
  near_duplicate_candidates   revision_a_id, revision_b_id,
                              provenance_basis_a, provenance_basis_b
```

Plus slice 1 §11's list: protected backup verified first; row counts
unchanged; all hash and signature values byte-identical; every row carries a
non-NULL basis at the gate even though the column permits NULL; a rerun that
would change any measurement value fails on bound and unresolved rows alike
while a byte-identical rerun passes; the 17 named cases reproduce (§10);
recovery is restore-from-backup.

**The snapshot digest is not a substitute for a `data_version` pair.**
`PRAGMA data_version` before, a single deferred read transaction,
`data_version` after; a changed pair rejects the report. File size, mtime and
WAL/SHM presence are diagnostics, never concurrency proof.

## 4. Migration 015 must not be applicable by an ordinary command

**Measured at `1941cdc`.** `apply_migrations()`
(`comic_automation/database/migrations.py:76`) discovers every numbered `.sql`
file in the directory and applies each unapplied one inside `BEGIN IMMEDIATE`.
It has no notion of approval, backup, or postflight. **Twelve call sites**
reach it, counted at `1941cdc` -- every one of them a command an operator or a
service starts:

```text
comic_automation/archive/cli.py:291                    comic_automation/archive/hash_cli.py:61
comic_automation/archive/duplicate_resolution_cli.py:202  comic_automation/archive/near_duplicate_cli.py:93
comic_automation/archive/page_hash_cli.py:64           comic_automation/archive/perceptual_hash_cli.py:69
comic_automation/archive/quarantine_cli.py:198         comic_automation/jobs/enqueue_missing_stages.py:176
comic_automation/library/cli.py:237                    comic_automation/service.py:159
scripts/benchmark_perceptual_hash_profiling.py:178     scripts/db.py:141
```

So **merely committing `015_*.sql` would arm every one of those commands to
apply the backfill**, with no backup, no approved plan, no reconciliation, and
only a 30-second `busy_timeout` (`database/connection.py:39`,
`database/dal.py:131,146`) between it and a concurrent writer. That is not a
hypothetical: it is what the current code does with any file that lands in
that directory.

### 4.1 Required shape

```text
protected set        015 is declared protected. The declaration lives in code,
                     not in the filename, so a rename cannot unprotect it.
ordinary path        apply_migrations() REFUSES a protected migration. It does
                     not skip it silently -- silence would let a command run
                     against a half-migrated schema believing it was current.
                     It raises, naming the migration and the executor.
protected executor   a separate operator CLI, the only caller permitted to
                     apply a protected migration, which performs §8's sequence
                     and refuses to act if any step is unsatisfied.
artifact validation  the executor takes the approved plan artifact, its
                     expected counts, and its snapshot digest, and refuses a
                     state that has moved.
```

The refusal, not the executor, is the load-bearing half. An executor that
exists beside an auto-apply path that still works has changed nothing.

**Open for review:** whether ordinary commands should refuse to *run at all*
while a protected migration is pending, or run against the old schema and
refuse only the migration. The first is safer and noisier; the second keeps
read-only tooling usable during the window. This document does not choose.

## 5. Column and constraint shapes

From slice 1 §9.1 and §9.2, quoted rather than restated, so the migration and
the document cannot drift apart.

```sql
-- every receiving table that carries archive_id
source_revision_id INTEGER
FOREIGN KEY (source_revision_id, archive_id)
    REFERENCES archive_revisions(id, archive_id)
-- NO ACTION, never CASCADE: deleting a revision must not silently remove the
-- evidence describing it. 014 already carries the UNIQUE (id, archive_id)
-- this key needs.

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

`near_duplicate_candidates` takes two keys, `(revision_a_id, archive_a_id)`
and `(revision_b_id, archive_b_id)`, and the paired CHECK twice, once per side
(§7.4).

Each table narrows the vocabulary further (§9.4.2): `measured` is legal only
on `archive_hashes`, because that is the only producer that computes a digest.
**The migration derives each table's CHECK from the union above rather than
restating it by hand** — restating it by hand is how the same omission was
committed twice in slice 1's own drafts.

`archive_inspections` additionally takes §6.5's pair:

```sql
inspector_version       TEXT
inspector_version_basis TEXT NOT NULL
    CHECK (inspector_version_basis IN ('known', 'unknown_legacy'))
CHECK ((inspector_version_basis = 'known'          AND inspector_version IS NOT NULL)
    OR (inspector_version_basis = 'unknown_legacy' AND inspector_version IS NULL))
```

All 59,541 historical rows are written `unknown_legacy` with a NULL version.
Historical rows must **not** be given the current version: a value in
`inspector_version` *is* the assertion that the row came from that code.

### 5.1 The paired CHECK does not stop an unattributed row — measured

```text
case                                 result
unchanged producer (NULL, NULL)      ACCEPTED
bound + measured                     ACCEPTED
unbound + unresolved_drift           ACCEPTED
bound + unresolved       (VB-05)     REJECTED
unbound + measured       (VB-06)     REJECTED

sqlite 3.40.1 | python 3.11.3 | win32 | measured 2026-08-28
```

An all-NULL row passes because SQL is three-valued: `source_revision_id IS
NULL` is true, `NULL LIKE 'unresolved%'` is NULL, `true AND NULL` is NULL,
`false OR NULL` is NULL — and SQLite accepts a CHECK that is not *false*.

This is why §6 is mandatory rather than tidy-up. **The constraint rejects a
lying row and accepts a silent one.** An unchanged producer writing its
existing column list creates rows with no revision and no basis, immediately
after the migration, passing every constraint the migration adds.

Per the platform-claim gate, that is a measurement on win32 with the SQLite
version named, not a deduction. It should be re-measured on the production
SQLite build before the executor runs.

## 6. Producer cutover — four paths, in the same release

"In the same migration" is not achievable and the first draft should not have
written it: SQL cannot update Python producers. The requirement is **in the
same slice and the same release** — the migration and the producer change ship
and deploy together, and §8.7 restarts only the new code.

All four write paths, located in the tree:

```text
archive_hashes              archive/hashing.py:153
archive_content_signatures  archive/page_hashing.py:229  (and dal.py:535)
archive_inspections         archive/repository.py:76
near_duplicate_candidates   archive/near_duplicate.py:505
```

Required behaviour:

```text
archive hashes        bind directly as 'measured' -- this producer computes
                      the digest, so it is the one table where 'measured' is
                      legal.
content signatures    stat-match to a revision, or write
                      'unresolved_no_identity' honestly. Never (NULL, NULL).
inspections           'known' plus a non-NULL version; initial attribution at
                      write; later binding per §8.4.
candidates            both sides attributed honestly, per-side, using the
                      paired CHECK of §7.4.
```

### 6.1 The UPSERTs rewrite timestamps, and the guards must not trip on that

**Measured at `1941cdc`**, from the four statements above:

```text
archive_hashes              ON CONFLICT(archive_id) DO UPDATE
                            ... hashed_at = CURRENT_TIMESTAMP,
                                updated_at = CURRENT_TIMESTAMP
archive_content_signatures  ON CONFLICT(archive_id) DO UPDATE
                            ... calculated_at = CURRENT_TIMESTAMP,
                                updated_at = CURRENT_TIMESTAMP
archive_inspections         inserted with inspected_at = CURRENT_TIMESTAMP
near_duplicate_candidates   ON CONFLICT(a, b, match_method) DO UPDATE
                            ... updated_at = CURRENT_TIMESTAMP
                            WHERE review_status = 'pending_review'
```

So a rerun that recomputes the *same* digest still writes a different
`hashed_at` / `calculated_at` / `inspected_at`. A naive immutability trigger
comparing every column would abort exactly the byte-identical rerun that
slice 1 §11.4 requires to *succeed*.

The disposition set already rules on the analogous case for
`archive_inspections`: DP-08 accepts an `updated_at` rewrite as legitimately
mutable, and DP-09 accepts a byte-identical rewrite of the whole result. The
immutability trigger therefore protects **measurement columns**, and the
recording timestamps are explicitly outside that set.

**Open for review:** `hashed_at`, `calculated_at` and `inspected_at` record
*when* a measurement was taken, which is arguably part of the measurement
rather than bookkeeping like `updated_at`. DP-01..DP-17 assign every one of
`archive_inspections`' 28 columns, so `inspected_at` has a ruling there; the
other three tables have no equivalent disposition set yet. §12 lists producing
one as required work.

## 7. Candidate measurement immutability lands in slice 4

The first draft flagged §11.4 and §11's slice-6 row as inconsistent. The lead
has ruled: **measurement immutability for `near_duplicate_candidates` is in
slice 4**; the attribution, supersession, identity and index set remains in
slice 6, which may replace or extend the guard.

The evidence for the ruling is in the tree. The current UPSERT
(`near_duplicate.py:505`) carries
`WHERE near_duplicate_candidates.review_status = 'pending_review'`, and on
those rows it overwrites `similarity_score`, `page_match_ratio`,
`compared_page_count`, `page_count_a`, `page_count_b`,
`average_dhash_distance`, `average_phash_distance`, `dimension_match_ratio`
and `metrics_json`. A rerun between slices 4 and 6 would therefore destroy the
historical measurement of every pending candidate — which is precisely what
"no measurement can be lost" forbids. Deferring the guard to slice 6 would
have left that hole open across two slices.

## 8. Concurrency protocol

Not an assumption to write down — a sequence the executor performs and can
fail.

```text
1  stop every application process and database writer, and VERIFY the stop
2  create the protected backup while quiescent, and verify it
3  acquire a fail-fast write lock
4  recompute the approved plan and compare it, AFTER the lock is held
5  apply schema, backfill and reconciliation in ONE transaction
6  commit only after reconciliation passes
7  restart only the NEW producer code, after postflight succeeds
```

Order matters at two points. The backup is taken while quiescent, so it is a
backup of a state nothing is still changing. The plan is recompared **after**
the lock, because a comparison made before it can be invalidated between the
comparison and the lock.

**`BEGIN IMMEDIATE` plus the 30-second busy timeout is not sufficient on its
own.** It excludes a concurrent writer only for the duration of the
transaction: an old writer that begins waiting during the migration can
acquire the lock and resume the instant 015 commits, writing rows through the
pre-migration code path against the post-migration schema — the (NULL, NULL)
row of §5.1, with the backfill already reconciled and signed off.

```text
quiescence violated, found before commit   abort and roll back
quiescence violated, found after commit    remain offline, restore the
                                           protected backup
```

Recovery is restore-from-backup, not repair-in-place.

## 9. Column dispositions

Slice 1 §9.4.2 carries a complete 28-column disposition for
`archive_inspections` — "28 columns, 28 assigned, 0 missing, 0 unassigned" —
and DP-01..DP-17 execute it.

**No equivalent exists for `archive_hashes`, `archive_content_signatures` or
`near_duplicate_candidates`.** Producing one for each, to the same
completeness standard, is required work and is listed in §12. It is not
optional: §6.1 shows that the disposition is what decides whether the
immutability trigger aborts a legitimate rerun, and three of the four tables
currently have no ruling.

## 10. The 17 named cases are a floor

Mapped from slice 1 §9.4.2 and §9.2:

```text
DP-01  status rewrite                                REJECTED  archive_inspections
DP-02  inspected_path rewrite                        REJECTED  archive_inspections
DP-03  archive_format rewrite                        REJECTED  archive_inspections
DP-04  encrypted rewrite                             REJECTED  archive_inspections
DP-05  comic_info_present rewrite                    REJECTED  archive_inspections
DP-06  comic_info_error rewrite                      REJECTED  archive_inspections
DP-07  created_at rewrite                            REJECTED  archive_inspections
DP-08  updated_at rewrite (legitimately mutable)     ACCEPTED  archive_inspections
DP-09  byte-identical rewrite of the whole result    ACCEPTED  archive_inspections
DP-10  one differing column among identical ones     REJECTED  archive_inspections

VB-01  backfilled: single_revision_inherited both    ACCEPTED  candidates
VB-02  producer: inherited_from_page_evidence both   ACCEPTED  candidates
VB-03  mixed: bound side inherited, other unresolved ACCEPTED  candidates
VB-04  bound inherited, other single_revision_inh.   ACCEPTED  candidates
VB-05  bound side carrying an unresolved basis       REJECTED  candidates
VB-06  unbound side carrying a bound basis           REJECTED  candidates
VB-07  side A binds: unresolved -> inherited         ACCEPTED  candidates
```

**What that covers: two tables.** DP exercises immutability on
`archive_inspections`; VB exercises the paired basis CHECK on
`near_duplicate_candidates`. The 17 prove neither mechanism on
`archive_hashes` or `archive_content_signatures`, and prove no producer
behaviour at all.

Required additional coverage, by the mechanism it exercises:

```text
immutability, archive_hashes              digest rewrite rejected; byte-identical
                                          rerun with a moved hashed_at accepted
immutability, archive_content_signatures  digest / page_count rewrite rejected;
                                          byte-identical rerun with a moved
                                          calculated_at accepted
immutability, candidates (§7)             metric rewrite on a pending_review row
                                          rejected; identical rerun accepted
paired CHECK, the three single-sided      bound+unresolved and unbound+bound
  tables                                  rejected on each
the (NULL, NULL) hole (§5.1)              an unchanged producer's row is
                                          rejected by the post-cutover producer
                                          path, since the CHECK will not do it
producer cutover, all four                each writes a legal basis on the path
                                          it already takes
protected executor (§4)                   apply_migrations REFUSES 015; the
                                          executor applies it; the refusal is
                                          proven by attempting an ordinary CLI
concurrency (§8)                          a writer that begins waiting during
                                          the migration cannot commit a
                                          pre-cutover row afterwards
```

Per the injection-site gate, every new guard is proven load-bearing by
disabling **it alone** and naming the tests that then fail, by name and count.
A guard that fails nothing when bypassed is unreachable defensive code — three
such guards were found that way during the #32 work.

## 11. Rulings recorded

```text
1  page-inventory digest    belongs to 4p. Its applied binding digest includes
                            the actual row id and every written value,
                            created_at and sealed_at included. Reconciliation
                            compares frozen values exactly and target states as
                            predicates; a clock range is supplementary, never a
                            substitute.
2  natural-key mapping      4p records archive_id -> page_inventory.id in the
                            postflight artifact and validates the one-to-one
                            mapping INSIDE the transaction. Not reconstructed
                            later.
3  concurrency              full writer quiescence plus §8's sequence.
4  candidate triggers       measurement immutability in slice 4; the remainder
                            in slice 6.
5  the 17 cases             read, mapped and reproduced before design approval,
                            with further producer and table tests where they do
                            not reach (§10).
```

Rulings 1 and 2 govern slice 4p and are recorded here so they are not
rediscovered when 4p opens.

## 12. Required before this design is approvable

Honest list of what this document still does not contain.

```text
trigger SQL                 exact CREATE TRIGGER text per table, with the
                            protected column set each one covers
executor design             the protected-migration CLI's own flow, its refusal
                            paths, and its artifact validation
dispositions                complete column coverage for archive_hashes,
                            archive_content_signatures and
                            near_duplicate_candidates (§9)
producer diffs              before/after SQL for each of the four write paths,
                            including what each writes on the unresolved branch
test plan                   the additional cases of §10 written out, each with
                            the guard it would catch and the bypass that proves
                            it load-bearing
re-measurement              §5.1 re-measured on the production SQLite build,
                            labelled with that build
```

Three of those depend on the two questions left open in §4.1 and §6.1, which
is why they are listed rather than guessed.

## 13. Gates carried from the slice 3 review

**Platform-claim.** Every platform-specific measurement is measured, not
reasoned about, and labelled with its platform. Earned twice: three
environment claims asserted from plausible reasoning on 2026-08-02 were all
wrong, and a file-id reuse claim held on Linux but not on win32 (0 of 5
unlink/recreate cycles), which is why green Windows CI never exercised the
failure. §5.1 is written to this standard; §12 requires it re-measured on the
production build.

**Injection-site.** A mechanism change must re-point the tests that inject
failures at the replaced call site. A test injecting at `os.write` passes
while guarding nothing once the code no longer calls it. Slice 4 replaces four
producer write paths and adds triggers that abort statements, so every test
injecting into a producer write is re-pointed, and each new guard is bypassed
alone.

**Single-writer threat model.** Recorded in `docs/engineering_decisions.md` at
`c666014`: one cooperating writer per namespace is an operational assumption,
not a property of any path. Slice 4's exposure is larger than slice 3's,
because a migration writes to the production database rather than to two
files — which is why §8 makes it a verified protocol rather than an assumption.

---

Nothing in slice 4 is applied by anyone but the operator, through the
protected executor of §4, following §8 in full: dry run first, protected
backup verified, expected count plus snapshot digest, report before act,
postflight reconciliation, and stop if code, preflight, backup and postflight
disagree.
