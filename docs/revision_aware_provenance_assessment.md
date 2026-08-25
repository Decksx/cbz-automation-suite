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

Ownership is only part of the step. The roadmap also requires algorithm and
version separation, parameters and run provenance where applicable, data-layer
idempotency, and evidence supersession. §6 covers all of them table by table;
an earlier draft of this document covered ownership alone.

The retention planner (PR #85) is the immediate consumer, but §10 is explicit
that it unlocks less than it first appears to.

---

## 2. Provenance of every number in this document

```text
database        G:\ComicAutomation\TestDatabase\inspection-working.db
schema          migrations 1-14
measured        2026-08-25
method          comic_automation.database.read_guards.read_consistent_snapshot
                mode=ro + PRAGMA query_only, one deferred read transaction,
                PRAGMA data_version sampled outside it either side
census runs     5, all read-only
quick_check     ok (all five)
data_version    2 -> 2 (all five, unchanged)
size            2,409,934,848 bytes, unchanged across all five
mtime_ns        1787610110176798300, unchanged across all five
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
| `processing_runs` | **0** | — |
| `processing_stages` | **0** | — |

Algorithms in use: `archive_hashes` `sha256 v1` (59,541);
`archive_content_signatures` `ordered-page-sha256 v1` (58,437); `page_hashes`
`sha256 v1` 2,955,391, `dhash v1` 2,932,841, `phash v1` 2,932,841;
`near_duplicate_candidates` `match_method = 'ordered_perceptual_v1'` (3,000) —
note the version is inside the method string, not a column of its own.

Jobs: all 294,403 are terminal (294,121 completed, 281 failed, 1 cancelled),
0 active, at most 7 per archive. **0 job payloads mention a digest or sha256**,
so no job row carries any content identity.

**`processing_runs` and `processing_stages` are empty.** Run provenance has
nothing to point at today; see §6.3.

### 3.3 Coverage gaps

```text
archives with a hash but no content signature           1,104
archives with a signature but no page rows                  5
archives with page rows but no signature                    0
archives with an inspection but no hash                     0
archives with no current location                         311
provisional archives (no digest), evidence = jobs only    147
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

### 4.1 Two different relationships hide behind one apparent agreement

Read naively, the census says every piece of content evidence already agrees
with its revision:

```text
archive_hashes.digest matching some revision of its archive     59,541 / 59,541
content signature digest matching some revision of its archive  58,437 / 58,437
inspection file size agreeing with revision file size           59,541 / 59,541
inspection page count agreeing with revision page count         59,541 / 59,541
```

Comparing these tables against the revisions **as a check** proves nothing,
because migration 014 built the revision fields from those same tables. But
that is not the same as saying the rows have no provenance, and an earlier
draft of this document made exactly that overstatement.

The 014 backfill is a *causal* record:

```sql
LEFT JOIN archive_hashes AS h              ->  r.archive_sha256    = h.digest
LEFT JOIN archive_content_signatures AS s  ->  r.content_signature = s.digest
                                               r.file_size  = a.file_size
                                               r.page_count = a.page_count
```

So two distinct relationships exist, and they need different names:

**Seed rows.** Each of the 59,541 `archive_hashes` rows *is what created* its
revision's immutable SHA identity. The revision's byte identity is that digest;
it did not exist before, and it came from nowhere else. Binding the hash row to
that revision records the actual causal history of the identity, not an
inference from currency. The same holds for the 58,421 non-drift content
signatures, which seeded `content_signature`.

**Inherited rows.** `archive_inspections` and `archive_pages` were *not* joined
by the backfill — `file_size` and `page_count` came from `archive_files`, not
from the inspection. Those rows have exactly one candidate revision and nothing
contradicting them, which is weaker: unique, unchallenged, and unverified.

The rule the reviewer set still holds and still bites: currency is not
ownership, and uniqueness is not proof. What it does not license is collapsing
a documented causal act into the same bucket as an unchecked coincidence.

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

Those 768 pages carry 768 `page_hashes` rows, all `sha256 v1` and none `dhash`
or `phash` — measured, not inferred from the count. No perceptual hashing ever
ran over them, and 0 of the 3,000 `near_duplicate_candidates` rows name a drift
archive on either side, so nothing downstream depends on them.

This is the population that makes a naive backfill wrong rather than merely
unproven. Binding these signatures and page rows to the single existing
revision would record, as a durable fact, that they describe bytes they
demonstrably do not.

**This finding is reported, not acted on.** Correcting it means either editing
immutable rows or minting a revision for the earlier generation, and both
belong to a later, separately reviewed migration and remediation design.

### 4.3 One-row-per-archive uniqueness is the real obstacle, not the missing column

Every derived-evidence table is capped at one row per archive:

```text
archive_inspections            archive_id UNIQUE
archive_hashes                 archive_id UNIQUE
archive_content_signatures     archive_id UNIQUE
archive_pages                  UNIQUE (archive_id, page_index)
archive_quarantine             archive_id UNIQUE
page_hashes                    UNIQUE (page_id, algorithm, algorithm_version)
```

Re-measuring therefore *overwrites* the previous generation's evidence. Adding
`source_revision_id` to a table that can only ever hold one row per archive
buys attribution but not retention: the column records which revision the
surviving row belongs to, while the row it replaced — the only record of the
earlier generation — is already gone.

`page_hashes` is the exception. Its uniqueness is already `(page_id, algorithm,
algorithm_version)`, which is the shape the roadmap asks for, and it inherits
revision scope from its parent page row.

Sequencing consequence, and the reason §11 is ordered as it is: moving
uniqueness onto the revision and switching producers from UPSERT to append are
**the same change**, because the unique key is the conflict target the
producers write against. They cannot be landed separately without either
breaking the producers or shipping code that does nothing.

---

## 5. Evidence-ownership matrix

"Identity" means the row describes the archive as a continuing thing.
"Revision" means it describes one specific byte state.

| table | producer | describes | revision-scoped | proposed ownership column |
| --- | --- | --- | --- | --- |
| `archive_inspections` | `archive/repository.py` | structure of the bytes inspected | **yes** | `source_revision_id` |
| `archive_hashes` | `archive/hashing.py` | digest of the bytes hashed | **yes** | `source_revision_id` |
| `archive_content_signatures` | `archive/page_hashing.py`, `database/dal.py` | digest over page content | **yes** | `source_revision_id` |
| `archive_pages` | `archive/page_hashing.py` | entries of the archive as read | **yes** | `source_revision_id` |
| `near_duplicate_candidates` | `archive/near_duplicate.py` | comparison of two page sets | **yes, pairwise** | `revision_a_id` + `revision_b_id` |
| `archive_quarantine` | `archive/quarantine.py` | a specific failed read | **yes** | `source_revision_id` (NULL for all 35 today — §7) |
| `page_hashes` | `archive/page_hashing.py`, `archive/perceptual_hashing.py` | hash of one page's bytes | **inherited** | none — reaches the revision through `archive_pages.page_id` |
| `jobs` | `jobs/queue.py` | scheduled work against an identity | **no** | none — §8.2 |
| `file_locations` | `library/repository.py`, `library/relocation_repair.py` | where an identity lives | **no** | none; the revision-at-a-location fact is `archive_revision_observations` |
| `file_events` | four producers | movement of paths | **no** | none |
| `archive_retirements` | `archive/disposition.py` | a decision about an identity | **no** | none |
| `archive_supersessions` | `archive/disposition.py` | a relation between two identities | **no** | none — never a digest, by design |
| `archive_disposition_events` | trigger-maintained | history of identity decisions | **no** | none |
| `processing_items` | run infrastructure | run bookkeeping | **no** | none |
| `archive_revision_observations` | `database/dal.py` | a sighting of a revision | already revision-keyed | — |

**Six tables gain an ownership key** — `archive_inspections`,
`archive_hashes`, `archive_content_signatures`, `archive_pages`,
`archive_quarantine`, and `near_duplicate_candidates` (two keys). All six are
in slice 3; an earlier draft named six but omitted `archive_hashes` from the
slice, which is corrected here. Its possible retirement (§9.5) is a separate
question and adding the key now does not prejudge it.

Eight tables are identity-scoped and correctly key on `archive_id` today.
`page_hashes` inherits through its parent and must **not** gain its own column:
8,821,073 rows with a redundant key that could disagree with `archive_pages` is
a contradiction waiting to be written.

No generic provenance table, no polymorphic `(entity_type, entity_id)` column,
no event bus.

---

## 6. Step 4 requirement coverage, table by table

The roadmap asks for `source_revision_id`, `algorithm`, `algorithm_version`,
`parameters_json`, `processing_run_id`, `created_at`, and
`superseded_at` / `superseded_by_id`, noting "not every table needs every
field".

### 6.1 Current state

| table | ownership | algorithm | version | parameters | run | supersession | idempotency today |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `archive_inspections` | missing | n/a | **missing** | n/a | missing | **missing** | UPSERT on `archive_id` |
| `archive_hashes` | missing | present | present | n/a | missing | **missing** | UPSERT on `archive_id` |
| `archive_content_signatures` | missing | present | present | n/a | missing | **missing** | UPSERT on `archive_id` |
| `archive_pages` | missing | n/a | n/a | n/a | missing | **missing** | `UNIQUE (archive_id, page_index)` |
| `page_hashes` | inherited | present | present | deferred | missing | **missing** | `UNIQUE (page_id, algorithm, algorithm_version)` — **already the shape the roadmap asks for** |
| `near_duplicate_candidates` | missing ×2 | `match_method` only | **missing — version is embedded in the method string** | **missing — thresholds unrecorded** | missing | **missing** | `UNIQUE (archive_a_id, archive_b_id, match_method)` |
| `archive_quarantine` | missing | n/a | n/a | n/a | missing | n/a | UPSERT on `archive_id` |

"n/a" means the table records a structural read or an operator action rather
than an algorithmic result, so an algorithm column would have nothing to hold.

### 6.2 `near_duplicate_candidates` is the weakest table on every axis

It is the only one that fails algorithm separation as well as ownership. All
3,000 rows carry `match_method = 'ordered_perceptual_v1'`, so the version is a
substring of a free-text column: nothing prevents `ordered_perceptual_v2` rows
coexisting under a key that treats them as the same method, and no query can
filter by version without string surgery. The thresholds that produced a
candidate — the distances, ratios and cut-offs — are recorded nowhere;
`metrics_json` holds outputs, not the parameters that generated them.

It therefore needs, in one slice: `revision_a_id`, `revision_b_id`,
`match_algorithm` + `match_algorithm_version` split out of `match_method`,
`parameters_json`, and supersession columns — with uniqueness moving to
`(revision_a_id, revision_b_id, match_algorithm, match_algorithm_version)`.
This is a bigger change than the other five tables combined and is given its
own slice.

### 6.3 Run provenance is deferred, with a measured reason

`processing_runs` and `processing_stages` both hold **0 rows**. Adding
`processing_run_id` now would create a column that is NULL for all 3.07M
historical rows and has nothing to point at for new ones, because nothing
currently writes a run. Deferred until the job runner writes runs; recorded
here as a decision rather than an omission.

### 6.4 Acceptance criteria

| roadmap criterion | status | where addressed |
| --- | --- | --- |
| existing evidence attributable to a revision | not met | slice 3 |
| algorithm versions cannot be silently mixed | met for `page_hashes`, `archive_hashes`, `archive_content_signatures`; **not met** for `near_duplicate_candidates` | slice 5 |
| repeated work idempotent at the data layer | met today by per-archive UPSERT; must be **re-established** on the new keys when uniqueness moves | slice 4 |
| superseded evidence distinguishable from active | **not met anywhere** — no table has `superseded_at` / `superseded_by_id` | slice 4 |
| migration preserves all hash values and counts | gate | slice 3 |

Supersession is not cosmetic here: once uniqueness allows a second generation's
evidence to coexist with the first, "which of these two inspections is the
current one" becomes a question the schema must answer. That is why it lands
with the uniqueness move and not before.

---

## 7. Backfill classification

### 7.1 Bases

```text
measured                     -- the producer computed identity from bytes it
                                read, inside the writing transaction
migration_014_revision_seed  -- this row is what migration 014 used to create
                                the revision's identity field; causal history,
                                not a circular check
single_revision_inherited    -- exactly one candidate revision existed and
                                nothing contradicted it; unverified
unresolved_drift             -- the row describes bytes no revision holds
unresolved_no_identity       -- no digest was ever obtained for these bytes
```

`measured` is **0** for every historical row: nothing in this backfill
re-measures anything.

### 7.2 Counts

| table | seed | inherited | unresolved | total |
| --- | ---: | ---: | ---: | ---: |
| `archive_hashes` | 59,541 | 0 | 0 | 59,541 |
| `archive_content_signatures` | 58,421 | 0 | 16 `drift` | 58,437 |
| `archive_inspections` | 0 | 59,541 | 0 | 59,541 |
| `archive_pages` | 0 | 2,954,623 | 768 `drift` | 2,955,391 |
| `archive_quarantine` | 0 | 0 | 35 `no_identity` | 35 |
| `near_duplicate_candidates` | 0 | 3,000 per side | 0 | 3,000 |

`page_hashes` receives no column; the 768 rows under drifted pages are reached
through their parent and inherit its unresolved state.

### 7.3 Two corrections to an earlier draft

**All 35 quarantine rows are unresolved, not 34.** Every row is
`failure_category = 'corrupt_archive'`, meaning the bytes could not be read. One
of the 35 archives does carry an `archive_hashes` row, but that hash proves only
that *some* bytes were once hashed — the quarantine record carries no digest and
no stat tying its failed read to those bytes. A hash elsewhere on the archive is
not evidence about this row.

**The 147 provisional archives are a census gate, not a row classification.**
Their only evidence is job rows, and jobs are excluded from provenance (§8.2),
so no receiving table exists that could hold an `unresolved_provisional` basis.
The earlier `unresolved_provisional` value is removed from the vocabulary. The
147 remain as an archive-level gate: *any* future evidence row for an archive
whose revision has no digest must be unresolved, and the backfill planner
reports the count so the gate is visible.

### 7.4 Per-side provenance for near-duplicate candidates

A candidate row is one comparison of two page sets, and each side is bound
independently. One `provenance_basis` cannot describe a pair whose sides differ
— a measured side compared against an inherited one is an ordinary future case.
The table therefore takes `provenance_basis_a` and `provenance_basis_b`, each
paired to its own revision key by its own CHECK.

---

## 8. How future producers must capture ownership

### 8.1 The pattern already exists and should be copied, not redesigned

`archive/hashing.py` already does this correctly:

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

A producer that cannot compute a content identity for the bytes it read cannot
claim a revision, and must write NULL with a reason.

### 8.2 Why `jobs` gets no revision column

A job is enqueued against an identity *before* its bytes are read. The revision
is what the job discovers, not something known when it is created. Adding
`source_revision_id` to `jobs` would either be NULL for the whole life of every
row that matters, or would have to be written after the fact by consulting the
current pointer — precisely the anti-pattern this step exists to remove.

The evidence a job produced already carries the revision. That is the join
path; the job does not need its own. Recorded as a deliberate non-decision.

---

## 9. Proposed schema shape and invariants

Stated as requirements for review, not as a migration.

### 9.1 The foreign key

Composite, wherever the table also carries `archive_id`:

```text
FOREIGN KEY (source_revision_id, archive_id)
    REFERENCES archive_revisions(id, archive_id)
```

This structurally prevents an inspection of archive A being bound to a revision
of archive B. `archive_revisions` already carries the `UNIQUE (id, archive_id)`
such a key needs, added by 014 for exactly this purpose.

`near_duplicate_candidates` takes two: `(revision_a_id, archive_a_id)` and
`(revision_b_id, archive_b_id)`.

### 9.2 Delete semantics

`NO ACTION`, never `CASCADE`. Deleting a revision must not silently remove the
evidence that describes it — the same argument 014 made for lineage.

### 9.3 Nullability and basis

Nullable, and never nullable *silently*:

```text
source_revision_id INTEGER          -- NULL = ownership not established
provenance_basis   TEXT NOT NULL
    CHECK (provenance_basis IN (
        'measured',
        'migration_014_revision_seed',
        'single_revision_inherited',
        'unresolved_drift',
        'unresolved_no_identity'
    ))
CHECK (
    (source_revision_id IS NOT NULL
     AND provenance_basis IN ('measured',
                              'migration_014_revision_seed',
                              'single_revision_inherited'))
 OR (source_revision_id IS NULL
     AND provenance_basis LIKE 'unresolved%')
)
```

The paired CHECK is load-bearing: a row cannot carry a revision without saying
how it got one, or be NULL without saying why. `near_duplicate_candidates`
carries this pair twice, once per side.

### 9.4 Supersession

`superseded_at` / `superseded_by_id`, added with the uniqueness move (§6.4).
A row is active when `superseded_at IS NULL`; the successor is named so a
reader can follow the chain rather than infer it from timestamps.

### 9.5 `archive_hashes` after Step 4

`archive_hashes` holds one mutable row per archive whose digest is, by
construction, the revision's digest, plus the stat of the read that produced
it — and that stat belongs in `archive_revision_observations`, which currently
holds zero rows.

Retiring it is **flagged, not proposed**, and is not folded into any slice
below. It does gain an ownership key in slice 3 like the other five: attributing
it now costs one column and does not prejudge the retirement question either
way.

### 9.6 Reconciliation invariants a backfill must satisfy

```text
every evidence row has a provenance_basis                   (NOT NULL CHECK)
bound + unresolved = total rows, per table                  (reconciliation)
no bound row names a revision of a different archive        (composite FK)
seed bindings match 014's own join, row for row             (recomputable)
pre-existing row counts unchanged in every touched table
all current hash and signature values byte-identical after
schema_migrations grows by exactly one
the protected pre-migration backup is verified before and after
```

---

## 10. Consumers affected — and what the planner actually gains

The retention planner's four `archive_proxy` rules do **not** all become
revision-granular. An earlier draft implied they would.

| planner rule | backing evidence | after Step 4 |
| --- | --- | --- |
| `active_or_recoverable_job` | `jobs` | **permanently proxy** — jobs are identity-scoped by design (§8.2) and gain no key |
| `unresolved_failure` | `jobs` | **permanently proxy**, same reason |
| `open_review_work` | `near_duplicate_candidates` | **can become revision-granular**, per side, once slice 5 lands and only for bound sides |
| `quarantine_or_resolution` | `archive_quarantine` **and** `archive_disposition_events` | **must be split** — see below |

`quarantine_or_resolution` folds two different things into one reason: a
quarantine row, which is revision-scoped and could be bound, and a disposition
event, which is a decision about an identity and never will be. It should be
split into `quarantine` and `disposition_history` so each rule has one meaning
again. Modelling a hybrid rule is the wrong fix.

Even after splitting, granularity is not a property of the rule alone. A
`quarantine` reason is revision-granular when the row that fired it is bound and
proxy when it is unresolved — and today all 35 are unresolved. The planner's
`RULE_GRANULARITY` constant cannot express that, so it should become
`RULE_MAX_GRANULARITY` — what the rule *could* achieve — with the actual
granularity resolved per evidence row from that row's `provenance_basis`. The
row-level "weakest wins" rule in `_evidence_granularity` is unchanged and
already correct.

Also affected: `perceptual_coverage_audit` (per-archive denominators; per-revision
changes what "covered" means and needs its own decision), `content_duplicate_audit`
and `near_duplicate` (comparisons become revision-pair-scoped),
`source_drift_recovery` (the producer that would *resolve* the 16, and whose
behaviour under revision-awareness must be specified before they can move out of
`unresolved_drift`). `classification.py`, disposition, retirement and
supersession key on identity and are unaffected — and must stay that way.

---

## 11. Recommended PR slices

Reordered from an earlier draft: producer append cannot precede the
append-capable schema, and the two are the same change (§4.3).

| # | slice | gate |
| --- | --- | --- |
| **1** | *this document* | lead accepts the matrix, the census, the requirement coverage and the invariants |
| **2** | read-only **backfill planner**: classify every evidence row into the five bases of §7.1, with a snapshot digest, deterministic JSON/CSV, and totals reconciling per table. No schema. | counts reproduce §7.2 exactly; `measured` is 0; the 16 and their 768 pages are `unresolved_drift`; all 35 quarantine rows are `unresolved_no_identity`; the 147 provisional archives are reported as an archive-level gate |
| **3** | migration: ownership keys + `provenance_basis` on all six tables, backfilling exactly what slice 2 planned. **Uniqueness unchanged.** Producers updated only to write a basis on the path they already take, plus an interim guard that **refuses** to overwrite a row bound to a different revision. | protected backup verified first; row counts unchanged; all hash and signature values byte-identical; the planned-vs-applied reconciliation of §11.1 passes; recovery is restore-from-backup, no down-migration |
| **4** | uniqueness moves to revision scope, producers switch UPSERT → append, `superseded_at` / `superseded_by_id` added, `provenance_basis` becomes NOT NULL, interim guard removed | idempotency re-established on the new keys and proven by bypass; a second generation's evidence demonstrably coexists with the first; supersession distinguishes active from superseded |
| **5** | `near_duplicate_candidates`: split `match_method` into algorithm + version, add `parameters_json`, move uniqueness to include both revisions and the version | no v1 row is reinterpreted; a v2 row can coexist; parameters recorded for new rows, NULL and explained for the 3,000 historical ones |
| **6** | planner: split `quarantine_or_resolution`, introduce `RULE_MAX_GRANULARITY`, resolve granularity per evidence row | planner reconciles unchanged on production; no rule silently upgrades from proxy on an unresolved or inherited row |

Interim state after slice 3, stated plainly: attribution improves, retention
does not. The interim guard is what keeps that window safe — an overwrite that
would have silently discarded another revision's evidence becomes a failed job
instead. Slice 4 removes the guard by removing the need for it.

Slices 3, 4 and 5 each touch production and each need the guarded-operation
sequence: dry run, protected backup, expected count plus snapshot digest, report
before act, postflight reconciliation.

### 11.1 The migration gate, respecified

An earlier draft required the post-migration planner output to reproduce the
pre-migration snapshot digest. That is not achievable and should not be asked
for: `source_revision_id` and `provenance_basis` are decision-bearing inputs, so
adding them *must* change any honest snapshot digest. A gate that demands
otherwise can only be satisfied by a digest that ignores the change.

Two digests instead:

```text
plan digest          computed by slice 2 over pre-migration inputs.
                     This is the artifact the lead approves, and it is
                     recomputed and compared immediately before the migration
                     acts. A change means the database moved under the review.

binding digest       computed after the migration over (table, row id,
                     source_revision_id, provenance_basis) for every row the
                     backfill touched.
```

The gate is a reconciliation between them, not equality of either:

```text
every binding in the plan was applied exactly once
no binding exists that the plan did not contain
per-table totals match the plan's totals
rows the plan marked unresolved carry NULL and the planned reason
```

---

## 12. Deliberate non-decisions, recorded

- **`archive_hashes` retirement** (§9.5) is flagged, not proposed. It still
  gains an ownership key in slice 3.
- **The 16 drift archives are not remediated here.** Resolving them needs a
  revision for the earlier generation, which belongs to a later, separately
  reviewed migration and remediation design. Not pre-decided here.
- **`jobs` deliberately gets no revision column** (§8.2).
- **`page_hashes` deliberately gets no column of its own** (§5).
- **`processing_run_id` is deferred** (§6.3) because `processing_runs` is
  empty — measured, not assumed.
- **`parameters_json` for `page_hashes` is deferred.** Version 1 perceptual
  hashing is frozen and its parameters are pinned by regression vectors, so
  there is nothing a parameters column would disambiguate until a v2 exists.
  It becomes required at that point.
- **No time-based or event-sourced provenance layer** is proposed. Direct
  foreign keys only.
- **The 439 mtime-only drift archives are not treated as changed.** Size agrees
  for all of them; mtime alone is moved by a copy or restore. If a later
  decision treats them as suspect, that is a new decision and this paragraph is
  the record that it was considered and declined here.
