# Revision-aware provenance — assessment and census

Roadmap Step 4, design only. This document proposes nothing that has been
built. It contains no migration, no schema change, and no implementation, and
the branch it lands on contains no implementation code.

Out of scope by instruction, and not pre-decided anywhere below: revision
pruning, migration 015, production remediation, issue #82, and unsafe
archive-member handling.

---

## 1. What Step 4 has to answer

Migration 014 made byte identity immutable. It did not make *derived evidence*
say which byte state it was derived from. Every table below still keys on
`archive_id`, so an inspection, a hash, a page row or a page hash asserts
something about "this archive" without recording which generation of its bytes
it measured.

The retention planner (PR #85) is the immediate consumer: four of its nine
protection rules can only be evaluated at archive granularity, and they are
labelled `archive_proxy` for exactly this reason. Step 4 is what would let
them become revision-granular.

---

## 2. Provenance of every number in this document

```text
database        G:\ComicAutomation\TestDatabase\inspection-working.db
schema          migrations 1-14
measured        2026-08-25
method          comic_automation.database.read_guards.read_consistent_snapshot
                mode=ro + PRAGMA query_only, one deferred read transaction,
                PRAGMA data_version sampled outside it either side
census runs     3, all read-only
quick_check     ok (all three)
data_version    2 -> 2 (all three, unchanged)
size            2,409,934,848 bytes, unchanged across all three
mtime_ns        1787610110176798300, unchanged across all three
elapsed         8.4s, 7.6s, and a targeted third run
```

Nothing was written to production, and no plan or report was written beside
the database.

Two figures corroborate independently recorded history: the drift population
below measures 16 archives and 768 pages, against the handoff's separately
recorded "15 ordinary signature-drift archives / 727 pages" plus "archive
37704 / 41 pages". 15 + 1 = 16 and 727 + 41 = 768. These were arrived at by
different queries on different days and agree exactly.

---

## 3. Measured census

### 3.1 Revisions

```text
archives                          59,688
revisions                         59,688
revisions per archive             1 for all 59,688 (no archive has 2+)
established (digest known)        59,541
provisional (digest unknown)         147
archive_revision_observations          0
```

`archive_revision_observations` is **empty**. Migration 014 created none, and
nothing has run since that writes one. There is therefore no sighting evidence
available to corroborate any binding proposed below.

### 3.2 Evidence volumes

| table | rows | distinct archives |
| --- | ---: | ---: |
| `archive_inspections` | 59,541 | 59,541 |
| `archive_hashes` | 59,541 | 59,541 |
| `archive_content_signatures` | 58,437 | 58,437 |
| `archive_pages` | 2,955,391 | 58,432 |
| `page_hashes` | 8,821,073 | — (keyed on `page_id`) |
| `jobs` (with an archive) | 294,403 | 59,688 |
| `file_locations` | 61,859 | 59,688 |
| `file_events` | 60,190 | 59,688 |
| `near_duplicate_candidates` | 3,000 | 4,772 (either side) |
| `archive_quarantine` | 35 | 35 |
| `archive_disposition_events` | 1 | 1 |
| `archive_retirements` | 1 | 1 |
| `archive_supersessions` | 0 | 0 |
| `processing_items` | 0 | 0 |

`page_hashes` by algorithm: `sha256 v1` 2,955,391; `dhash v1` 2,932,841;
`phash v1` 2,932,841.

Jobs: all 294,403 are terminal (294,121 completed, 281 failed, 1 cancelled),
0 active, at most 7 per archive. **0 job payloads mention a digest or sha256**,
so no job row carries any content identity.

### 3.3 Coverage gaps

```text
archives with a hash but no content signature           1,104
archives with a signature but no page rows                  5
archives with page rows but no signature                    0
archives with an inspection but no hash                     0
archives with no current location                         311
provisional archives carrying some evidence               147  (jobs only)
```

### 3.4 Drift — where recorded evidence and the file on disk disagree

| comparison | compared | size differs | mtime only | agree |
| --- | ---: | ---: | ---: | ---: |
| `archive_hashes` stat vs current location | 59,376 | **0** | 439 | 58,937 |
| `archive_content_signatures` stat vs current location | 58,275 | **16** | 0 | 58,259 |
| `archive_inspections` stat vs `archive_hashes` stat | 59,541 | 0 | 0 | 59,541 |
| revision `file_size` vs current location | 59,377 | 0 | — | 59,377 |

Archives where **both** the hash and the signature disagree on size with the
current location: **0**.

The 439 are mtime-only, with sizes agreeing. A copy, a restore or a touch
moves mtime without changing content, so these are weak signals and are *not*
treated below as evidence that bytes changed. The 16 differ in size, which is
strong.

---

## 4. Three findings that constrain the whole design

### 4.1 The apparent agreement between evidence and revisions is tautological

Read naively, the census says every piece of content evidence already agrees
with its revision:

```text
archive_hashes.digest matching some revision of its archive     59,541 / 59,541
content signature digest matching some revision of its archive  58,437 / 58,437
inspection file size agreeing with revision file size           59,541 / 59,541
inspection page count agreeing with revision page count         59,541 / 59,541
```

**None of that is evidence.** Migration 014's backfill built the revision rows
*from those exact tables*:

```sql
LEFT JOIN archive_hashes AS h              ->  r.archive_sha256    = h.digest
LEFT JOIN archive_content_signatures AS s  ->  r.content_signature = s.digest
                                               r.file_size  = a.file_size
                                               r.page_count = a.page_count
```

So each of those four lines is one fact read twice. They establish that a
mechanical backfill would be *internally consistent and unambiguous* — a
useful thing to know, since a 1:1 join with zero contradictions will not
strand rows — but they establish nothing whatsoever about whether the evidence
actually describes the revision's bytes.

This is the "do not infer ownership because a revision is current today" trap
in its subtler form. A one-revision-per-archive database makes every candidate
unique; uniqueness is not proof.

### 4.2 Sixteen revision rows already describe two byte generations at once

The 16 signature-drift archives are not merely stale. For every one of them:

```text
hash file size            == current location file size      16 / 16
signature source size     != current location file size      16 / 16
revision.archive_sha256   == archive_hashes.digest           16 / 16
revision.content_signature == content signature digest       16 / 16
```

Read together: the archive was re-hashed after its bytes changed, but its
pages were never re-extracted and its signature never recomputed. The 014
backfill then joined both tables into one revision row — so **the revision's
`archive_sha256` describes today's bytes while its `content_signature`
describes an earlier generation of the same archive.**

```text
archive   location size    hash size    signature size   pages
 10999      34,694,326   34,694,326      12,905,443       26
 18348      13,283,174   13,283,174       9,922,810       17
 27218     112,388,584  112,388,584     112,387,135       84
 27219      71,441,232   71,441,232      71,440,273       61
 27220      78,058,892   78,058,892      78,057,955       60
 27221      91,370,548   91,370,548      91,369,457       67
 27222      65,997,463   65,997,463      65,996,658       54
 27223      73,040,901   73,040,901      73,040,052       56
 27224      82,609,739   82,609,739      82,608,912       55
 27225      86,680,068   86,680,068      86,679,219       56
 27226      86,695,675   86,695,675      86,694,870       54
 27227      77,896,974   77,896,974      77,896,125       56
 27228      78,123,620   78,123,620      78,122,727       58
 28440      11,741,381   11,741,381      10,239,999        9
 28441      16,316,817   16,316,817      15,716,108       14
 37704       7,065,799    7,065,799       5,980,623       41
                                                       -----
                                                         768 pages
```

Those 768 pages carry 768 `page_hashes` rows, all of them `sha256 v1` and
none `dhash` or `phash` — measured, not inferred from the count. No perceptual
hashing ever ran over them, which is consistent with their having been held
back from the backfill, and it means the perceptual-coverage figures are not
affected by what follows. It also means no `near_duplicate_candidates` row can
depend on them, which the census confirms directly: 0 of the 3,000 rows name a
drift archive on either side.

This is the population that makes a naive backfill wrong rather than merely
unproven. Binding `archive_content_signatures` and the 768 page rows to the
single existing revision would record, as a durable fact, that they describe
bytes they demonstrably do not.

**This finding is reported, not acted on.** Correcting the revision rows means
either editing immutable rows or minting a revision for the earlier
generation, and both are migration-015 and remediation territory.

### 4.3 One-row-per-archive uniqueness is the real obstacle, not the missing column

Every derived-evidence table is capped at one row per archive:

```text
archive_inspections            archive_id UNIQUE
archive_hashes                 archive_id UNIQUE
archive_content_signatures     archive_id UNIQUE
archive_pages                  UNIQUE (archive_id, page_index)
archive_quarantine             archive_id UNIQUE
```

Re-measuring therefore *overwrites* the previous generation's evidence. Adding
`source_revision_id` to a table that can only ever hold one row per archive
buys almost nothing: the column would record which revision the surviving row
belongs to, while the row it replaced — the only record of the earlier
generation — is already gone.

Step 4 is therefore two changes, and the second is the substantive one:

1. add a direct `source_revision_id` foreign key;
2. move uniqueness from `(archive_id)` to `(source_revision_id)` so a second
   generation's evidence can coexist with the first.

Sequencing note: (2) is what actually retains history and is also what changes
producer behaviour from UPSERT to append. It should not be bundled with (1).

---

## 5. Evidence-ownership matrix

"Identity" means the row describes the archive as a continuing thing.
"Revision" means it describes one specific byte state.

| table | producer | describes | revision-scoped | proposed column |
| --- | --- | --- | --- | --- |
| `archive_inspections` | `archive/repository.py` | structure of the bytes inspected | **yes** | `source_revision_id` NOT NULL (new rows) |
| `archive_hashes` | `archive/hashing.py` | digest of the bytes hashed | **yes** | `source_revision_id`; largely redundant with the revision itself — see §8.4 |
| `archive_content_signatures` | `archive/page_hashing.py`, `database/dal.py` | digest over page content | **yes** | `source_revision_id` |
| `archive_pages` | `archive/page_hashing.py` | entries of the archive as read | **yes** | `source_revision_id` |
| `page_hashes` | `archive/page_hashing.py`, `archive/perceptual_hashing.py` | hash of one page's bytes | **inherited** | none — reaches the revision through `archive_pages.page_id` |
| `near_duplicate_candidates` | `archive/near_duplicate.py` | comparison of two page sets | **yes, pairwise** | `revision_a_id`, `revision_b_id` |
| `archive_quarantine` | `archive/quarantine.py` | a specific failed read | **yes** | `source_revision_id` NULLABLE — see §6.3 |
| `jobs` | `jobs/queue.py` | scheduled work against an identity | **no** | none — see §7.2 |
| `file_locations` | `library/repository.py`, `library/relocation_repair.py` | where an identity lives | **no** | none; the revision-at-a-location fact is `archive_revision_observations` |
| `file_events` | four producers | movement of paths | **no** | none |
| `archive_retirements` | `archive/disposition.py` | a decision about an identity | **no** | none |
| `archive_supersessions` | `archive/disposition.py` | a relation between two identities | **no** | none — never a digest, by design |
| `archive_disposition_events` | trigger-maintained | history of identity decisions | **no** | none |
| `processing_items` | run infrastructure | run bookkeeping | **no** | none |
| `archive_revision_observations` | `database/dal.py` | a sighting of a revision | already revision-keyed | — |

Six tables gain a direct foreign key. Seven are identity-scoped and correctly
key on `archive_id` today. One (`page_hashes`) inherits ownership through its
parent and must **not** gain its own column — 8,821,073 rows with a redundant
key that could disagree with `archive_pages` is a contradiction waiting to be
written.

No generic provenance table, no polymorphic `(entity_type, entity_id)` column,
no event bus. Each relationship is a named foreign key on the table that owns
the fact, which is also what lets the retention planner join it directly.

---

## 6. Backfill classification

### 6.1 Provable

**None.** No evidence row in this database can be bound to a revision with
independent proof, because §4.1 applies to every candidate: the revision's
content fields were copied from the very tables that would be checked against
them.

Recording this as a nil result matters. A backfill that reports "59,541 rows
bound with proof" would be describing a circular check.

### 6.2 Unambiguous but inherited

| table | rows | basis |
| --- | ---: | --- |
| `archive_inspections` | 59,541 | one revision per archive; stat agrees with the hash for all 59,541 |
| `archive_hashes` | 59,541 | one revision per archive; digest identical by construction |
| `archive_content_signatures` | 58,421 | one revision per archive, **excluding the 16** |
| `archive_pages` | 2,954,623 | one revision per archive, **excluding 768** |
| `near_duplicate_candidates` | 3,000 | both sides resolve to a single revision each; **0** of the 3,000 touch a drift archive on either side, checked rather than assumed |

"Unambiguous" here means exactly one candidate exists and nothing contradicts
it. It does not mean verified. Any backfill writing these must record the
basis on the row — a `provenance_basis` of `single_revision_inherited`, not
`verified` — so a later reader cannot mistake the two.

### 6.3 Must remain explicitly unresolved

| population | rows | why |
| --- | ---: | --- |
| the 16 drift signatures | 16 | describe bytes no revision holds (§4.2) |
| their page rows | 768 | same, inherited from the signature's generation |
| their page hashes | 768 | reached through those page rows |
| quarantine rows | 34 of 35 | all 35 are `corrupt_archive`, and only **1** of the 35 archives has ever been hashed. A corrupt archive is one whose bytes could not be read, so no digest was obtained and there is nothing to match against. The 1 hashed archive is unambiguous-inherited rather than unresolvable |
| evidence on provisional archives | 147 archives | their revision has no digest at all, so there is nothing to match against |

For these, `source_revision_id` must be NULL with a recorded reason, never a
best guess. A NULL that means "we could not establish this" is only
distinguishable from a NULL that means "nobody has looked" if the reason is
written down, so the column needs a companion `provenance_basis` rather than
standing alone.

---

## 7. How future producers must capture ownership

### 7.1 The pattern already exists and should be copied, not redesigned

`archive/hashing.py` already does this correctly, and it is the template:

```text
measure the bytes            -> result.digest, result.file_size
resolve or append a revision -> RevisionRepository.record_or_reuse(
                                    archive_id, archive_sha256=result.digest)
move the pointer             -> set_current(...)
record the sighting          -> observe(revision_id, location_id, stat)
```

all inside one DAL-owned transaction, with the source path, location identity
and `stat()` revalidated inside that transaction including immediately before
COMMIT, and `SourceChangedError` categorised as retryable `filesystem_io`
after rollback.

The essential property is that the revision is derived **from the bytes the
producer just measured**, keyed on their digest. It is never read from
`archive_files.current_revision_id`, which is a mutable pointer another writer
may have moved between the measurement and the write.

Every producer in §5 that gains a `source_revision_id` must obtain it this
way. A producer that cannot compute a content identity for the bytes it read
cannot claim a revision, and must write NULL with a reason.

### 7.2 Why `jobs` gets no revision column

A job is enqueued against an identity *before* its bytes are read. The
revision is what the job discovers, not something known when it is created.
Adding `source_revision_id` to `jobs` would either be NULL for the whole life
of every row that matters, or would have to be written after the fact by
consulting the current pointer — precisely the anti-pattern this step exists
to remove.

The evidence a job produced already carries the revision. That is the join
path; the job does not need its own.

Recorded as a deliberate non-decision: this is a design choice with a reason,
not an omission.

---

## 8. Proposed schema shape and invariants

Stated as requirements for review, not as a migration.

### 8.1 The foreign key

```text
source_revision_id INTEGER
    REFERENCES archive_revisions(id)
```

Composite, as migration 014 did for lineage, wherever the table also carries
`archive_id`:

```text
FOREIGN KEY (source_revision_id, archive_id)
    REFERENCES archive_revisions(id, archive_id)
```

This is what structurally prevents an inspection of archive A being bound to a
revision of archive B. `archive_revisions` already carries the
`UNIQUE (id, archive_id)` that such a key needs, added by 014 for exactly this
purpose, so no new constraint is required on the parent.

### 8.2 Delete semantics

`NO ACTION`, never `CASCADE`. Deleting a revision must not silently remove the
evidence that describes it — that is the same argument 014 made for lineage,
and the same reason the planner reports pruning as infeasible today.

### 8.3 Nullability and basis

The column must be nullable — 6.3 exists — and must never be nullable
*silently*:

```text
source_revision_id INTEGER          -- NULL = ownership not established
provenance_basis   TEXT NOT NULL    -- how it was established, or why not
    CHECK (provenance_basis IN (
        'measured',                  -- producer computed identity from the
                                     -- bytes it read, in the same transaction
        'single_revision_inherited', -- one candidate, nothing contradicted it
        'unresolved_drift',          -- evidence describes bytes no revision holds
        'unresolved_no_identity',    -- no digest was ever obtained
        'unresolved_provisional'     -- the archive's revision has no digest
    ))
CHECK (
    (source_revision_id IS NOT NULL
     AND provenance_basis IN ('measured', 'single_revision_inherited'))
 OR (source_revision_id IS NULL
     AND provenance_basis LIKE 'unresolved%')
)
```

The paired CHECK is the load-bearing part: it makes "bound" and "explained"
the same statement, so a row cannot carry a revision without saying how it got
one, and cannot be NULL without saying why.

### 8.4 `archive_hashes` is now largely redundant

`archive_hashes` holds one mutable row per archive whose digest is, by
construction, the current revision's digest. Once evidence is revision-aware
it is a cache of `archive_revisions.archive_sha256` plus the stat of the read
that produced it — and the stat belongs in `archive_revision_observations`,
which is the table built for it and currently holds zero rows.

Retiring it is out of scope here and is flagged rather than proposed. It
should not be folded into any of the slices below.

### 8.5 Reconciliation invariants a backfill must satisfy

```text
every evidence row has a provenance_basis                   (NOT NULL CHECK)
bound + unresolved = total rows, per table                  (reconciliation)
no bound row names a revision of a different archive        (composite FK)
no row is bound to a revision whose digest contradicts it   (0 expected;
                                                             16 known
                                                             exclusions)
pre-existing row counts unchanged in every touched table
schema_migrations grows by exactly one
the protected pre-migration backup is verified before and after
```

---

## 9. Consumers affected

| consumer | effect |
| --- | --- |
| **retention planner** (PR #85) | the four `archive_proxy` rules can become revision-granular *only* for tables that gain the column and only for `measured` rows. `single_revision_inherited` must stay proxy — it is the same conservative claim wearing a new label |
| `perceptual_coverage_audit` | denominators are per-archive today; per-revision changes what "covered" means and needs its own decision |
| `content_duplicate_audit`, `near_duplicate` | comparisons become revision-pair-scoped; historical rows carry inherited basis |
| `source_drift_recovery` | this is the producer that would *resolve* the 16, by re-extracting pages and recording a new revision. Its behaviour under revision-awareness needs specifying before the 16 can move out of `unresolved_drift` |
| `classification.py` contract | keys on identity, unaffected |
| disposition / retirement / supersession | identity-scoped, unaffected — and must stay that way |

The planner's `RULE_GRANULARITY` table is the place a reader will look to see
whether Step 4 landed, so it should be updated in the same slice that makes a
rule revision-granular, never earlier.

---

## 10. Recommended PR slices

Each slice ends at a reviewable gate. No slice both adds a column and changes
how a producer writes.

| # | slice | gate |
| --- | --- | --- |
| **1** | *this document* | lead accepts the matrix, the census and the invariants |
| **2** | read-only backfill **planner**: classify every evidence row into `measured` / `single_revision_inherited` / one of the `unresolved` reasons, with a snapshot digest, deterministic JSON/CSV, and totals that reconcile per table. No schema. | the 16 and their 768 pages appear as `unresolved_drift`; the 147 provisional archives as `unresolved_provisional`; `measured` is 0; every table reconciles |
| **3** | migration: add `source_revision_id` + `provenance_basis` to `archive_inspections`, `archive_content_signatures`, `archive_pages`, `archive_quarantine`, and `revision_a_id` / `revision_b_id` to `near_duplicate_candidates`. Composite FKs, `NO ACTION`, paired CHECK. Backfill exactly what slice 2 planned. | protected backup taken and verified first; row counts unchanged in all touched tables; planner output from slice 2 reproduced post-migration digest-for-digest; recovery is restore-from-backup, no down-migration |
| **4** | producers write `provenance_basis = 'measured'` atomically, following the `hashing.py` pattern, with in-transaction stat revalidation | bypass proofs that a producer cannot write a revision it did not measure; existing rows untouched |
| **5** | uniqueness moves from `(archive_id)` to `(source_revision_id)`, producers change from UPSERT to append | this is the slice that retains history and the one most likely to need its own design round — do not start it before 4 is merged |
| **6** | retention planner rules become revision-granular where `measured` allows, `RULE_GRANULARITY` updated | planner reconciles unchanged on production; no rule silently upgrades from proxy on inherited rows |

Slices 3 and 5 each touch production and each need the guarded-operation
sequence: dry run, protected backup, expected count plus snapshot digest,
report before act, and postflight reconciliation.

---

## 11. Deliberate non-decisions, recorded

- **`archive_hashes` retirement** (§8.4) is flagged and not proposed.
- **The 16 drift archives are not remediated here.** Resolving them needs a
  new revision for the earlier generation, which is migration-015 and
  production-remediation work.
- **`jobs` deliberately gets no revision column** (§7.2), with the reason
  stated so a later reader does not read it as an oversight.
- **`page_hashes` deliberately gets no column of its own** (§5).
- **No time-based or event-sourced provenance layer** is proposed. Direct
  foreign keys only, per instruction and per the roadmap's standing deferral
  of generic lifecycle frameworks.
- **The 439 mtime-only drift archives are not treated as changed.** Size
  agrees for all of them; mtime alone is moved by a copy or restore. If a
  later decision treats them as suspect, that is a new decision and this
  paragraph is the record that it was considered and declined here.
