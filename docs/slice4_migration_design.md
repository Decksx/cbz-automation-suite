# Slice 4 — migration 015 design

**Status: design in progress. No schema, no migration, no producer change.**
This document exists to be reviewed before anything is built. Where it does
not know something it says so rather than proposing an answer.

Slice 1 is `docs/revision_aware_provenance_assessment.md`; slice 2 is
`docs/page_inventory_design.md`; slice 3 is the merged planner
(`comic_automation/archive/provenance_backfill_planner.py`, PR #88, merged at
`1941cdc`).

## 1. Starting state, verified in the tree

Documents lag code, so every claim below was checked against the working tree
on 2026-08-28 at `1941cdc` rather than taken from slice 1.

```text
migrations present        001..014, as .sql under
                          comic_automation/database/migrations/
migration 015             does not exist
provenance_basis          not present in any migration
source_revision_id        not present in any migration
inspector_version         not present in any migration
page_inventory            no table; slice 3 plans its rows by natural key
                          because they do not exist yet
slice 3 planner           merged, with the basis vocabulary of §7.1 and the
                          two target-state facts of slice 2 §10.1
```

The absence of all four is the thing worth stating: slice 1 §11 was written
before slice 3 shipped, and the standing rule is that an entire planned work
item was once found already implemented while three audits still called it
outstanding. That is not the case here — slice 4 is genuinely unbuilt.

## 2. What slice 4 is

From slice 1 §11, restated as the work rather than as a table row:

1. **Ownership keys and a nullable `provenance_basis`** on the receiving
   tables, backfilling exactly what slice 3 planned.
2. **`inspector_version` and `inspector_version_basis`** on
   `archive_inspections`, with the paired CHECK of §6.5, all 59,541 historical
   rows written `unknown_legacy` with a NULL version.
3. **The producer requirement, in the same migration**: an inspection written
   after 015 carries `known` and a non-NULL version. Slice 5 makes it
   structural; slice 4 makes it true.
4. **The measurement-immutability triggers** of §9.4.2. Slice 1 §11.4 moved
   these out of slice 5 deliberately: they are what makes the interim window
   safe, and they compare *values* rather than revisions, so they also protect
   the 16 deliberately-unresolved drift signatures that have no revision to
   differ from.
5. **Creating `page_inventory`** and populating it. See §4 — this is the part
   slice 1 §11's one-line summary does not convey.

And what it is not:

```text
parameters_basis                  slice 6, with the fields that give it meaning
uniqueness / partial indexes      slice 5
UPSERT -> append                  slice 5
provenance_basis NOT NULL         slice 5, via the table rebuild
near_duplicate_candidates work    slice 6 (the columns are written here; the
                                  trigger set and indexes are not)
per-row granularity resolution    slice 7, deferred (§12.2)
```

## 3. What the migration writes, per table

This is the binding-digest population of §11.1. The digest is named per table
because slice 4 writes more than two columns, and an earlier draft's fixed
four-tuple could not carry the inspector fields.

```text
archive_hashes                source_revision_id, provenance_basis
archive_content_signatures    source_revision_id, provenance_basis
archive_inspections           source_revision_id, provenance_basis,
                              inspector_version, inspector_version_basis
near_duplicate_candidates     revision_a_id, revision_b_id,
                              provenance_basis_a, provenance_basis_b
page_inventory                see §4 — open
```

## 4. `page_inventory` is a creation, not a backfill — and it breaks the digest symmetry

Every other receiving table already holds its rows, and slice 3 planned them
by row id. `page_inventory` holds none, so slice 3 planned it by **natural
key** (`NATURAL_KEY_TABLES` in the merged planner, slice 2 §10.1). Slice 4
therefore creates the table and materialises ~58,432 parents rather than
adding columns to rows that exist.

That produces a reconciliation problem §11.1 does not resolve, and it is the
first thing the pre-build review should settle.

**The problem.** §11.1 defines the binding digest over "EVERY column that
migration wrote". Two of `page_inventory`'s columns are `sealed_at` and
`created_at`, which slice 2 §10.1 classifies as *target states*: a plan
computed before the migration runs cannot name them without predicting a
clock. So:

```text
digest includes sealed_at/created_at  -> cannot reconcile against the plan,
                                         because the plan could not contain
                                         them
digest excludes them                  -> columns the migration wrote are
                                         unattested by the gate
```

Both readings are defensible and they are not the same gate. This needs the
lead's ruling, not a guess. The shape of an answer might be that the binding
digest covers *frozen values* only and the clock-valued columns are attested
by a separate bounded-range check (they fall inside the migration's own
run window), but that is a proposal to be accepted or rejected, not a
decision this document takes.

**A second, smaller one.** The planner plans `page_inventory` by natural key;
the gate in §11.1 says "every binding in the plan was applied exactly once".
Reconciling a natural-key binding against a created row means resolving the
key to the id the migration assigned. Whether that mapping is recorded in the
postflight artifact or recomputed from the key is an open choice with a
different failure mode each way.

## 5. The gate

From §11.1, and stated as the reconciliation it is rather than as equality of
either digest:

```text
plan digest      recomputed immediately before the migration acts and compared
                 to the approved artifact. A change means the database moved
                 under the review.
binding digest   computed after, per table, over every column 015 wrote.

reconciliation   every binding in the plan was applied exactly once
                 no binding exists that the plan did not contain
                 per-table totals match the plan's totals
                 rows the plan marked unresolved carry NULL and the planned
                 reason
```

Plus slice 1 §11's own gate list: protected backup verified first; row counts
unchanged; all hash and signature values byte-identical; every row carries a
non-NULL basis at the gate even though the column permits NULL; a rerun that
would change any measurement value fails on bound and unresolved rows alike
while a byte-identical rerun passes; slice 4's 17 cases reproduce
(DP-01..DP-10, VB-01..VB-07); recovery is restore-from-backup.

**The snapshot digest is not a substitute for a data_version pair.** The
read-only audit rule stands: `PRAGMA data_version` before, a single deferred
read transaction, `data_version` after, and a changed pair rejects the report.
File size, mtime and WAL/SHM presence are diagnostics, never concurrency
proof.

## 6. Gates carried into the pre-build review

Three gates were established during the slice 3 review and are carried
forward. They are listed here so the slice 4 review starts with them rather
than rediscovering them.

### 6.1 Platform-claim gate

**Every platform-specific measurement is labelled with the platform it was
measured on, and is measured rather than reasoned about.**

Earned twice. On 2026-08-02 three environment claims were asserted from
plausible reasoning and all three were wrong. During slice 3, a file-id reuse
claim held on Linux and not on win32 (0 of 5 unlink/recreate cycles reused the
id), which is exactly why green Windows CI never exercised the failure.

Slice 4 exposure: SQLite trigger behaviour, `PRAGMA` semantics under WAL, and
whatever the migration does about table rebuilds are all platform- and
version-sensitive. Anything asserted about them carries the platform and the
SQLite version, or it is a hypothesis worth testing rather than a finding.

### 6.2 Injection-site gate

**Any mechanism change must audit the tests that inject failures at the
replaced call site.**

A test that injects at `os.write` proves nothing once the code no longer calls
`os.write`; it passes while guarding nothing. Slice 4 replaces producer write
paths with basis-writing ones and introduces triggers that abort statements,
so every existing test that injects a failure into a producer's write must be
re-pointed at the path the producer actually takes afterwards, and each new
guard must be proven load-bearing by disabling it *alone* and naming the tests
that then fail, by name and count.

### 6.3 Single-writer threat-model gate

**The one-cooperating-writer assumption is an operational assumption, not a
property of any path, and anything relying on it says so.**

Recorded in `docs/engineering_decisions.md` at `c666014`: artifact generation
requires one cooperating writer per requested final and staging namespace.
`O_EXCL` refuses concurrent *creation* and does nothing further; the staging
names are deterministic siblings, so the second writer loses the create rather
than the name being unguessable.

Slice 4 exposure is larger than slice 3's, because a migration is a writer
against the production database rather than against two files. The review
needs an explicit answer to: what else may be running while 015 runs, what
enforces that, and what happens if it is violated. "The operator will not run
a worker at the same time" is an acceptable answer *if it is written down as
an assumption*; it is not acceptable as an unstated premise.

## 7. Open questions for the lead

Ordered by how much of the build they block.

1. **`page_inventory`'s binding digest** (§4). Frozen values only, or every
   written column including the clock-valued ones? This changes what the gate
   can assert and therefore what the postflight artifact must record.
2. **Natural-key reconciliation** (§4). Is the key→id mapping recorded in the
   postflight artifact, or recomputed from the key at reconciliation time?
3. **Concurrency premise** (§6.3). What is asserted to be quiescent while 015
   runs, and what enforces it?
4. **Trigger scope.** §9.4.2's measurement-immutability set lands here, but
   `near_duplicate_candidates` gets its columns in slice 4 and its trigger set
   in slice 6. Do the immutability triggers cover the candidate table's
   measurement columns in the interim, or is that table unprotected until
   slice 6? §11.4's "no measurement can be lost" reads as covering every
   receiving table; §11's slice-6 row reads as deferring the candidate
   triggers entirely. These are not obviously consistent.
5. **DP-01..DP-10 and VB-01..VB-07.** Slice 1 assigns 17 cases to slice 4. The
   case bodies live in §9.4.2 and §9.2 and have not yet been read line by line
   against what 015 will actually build; that reading is the next step of this
   design, not a gap in it.

## 8. Not yet done

This document covers scope, the verified starting state, the gate, and the
carried review gates. It does **not** yet contain the migration's SQL shape,
the trigger definitions, the producer diffs, or the case-by-case walk of the
17. Those follow once the questions in §7 are answered, because three of them
change what gets built.

Nothing in slice 4 touches production until the operator runs it. The
guarded-operation sequence applies in full: dry run first, protected backup
verified, expected count plus snapshot digest, report before act, postflight
reconciliation, and stop if code, preflight, backup and postflight disagree.
