# Revision-aware provenance — assessment and census

Roadmap Step 4, design only. This document proposes nothing that has been
built. It contains no migration, no schema change, and no implementation, and
the branch it lands on contains no implementation code.

Out of scope by instruction, and not pre-decided anywhere below: revision
pruning, migration 015, production remediation, issue #82, and unsafe
archive-member handling.

---

## 0. Resolution log

This document is an evidence record. Findings below are preserved as written
and marked inline where a later slice superseded them; they are not rewritten
to describe current plans.

### 2026-09-01 — basis nullability moves from slices 5/6 into slice 4

**What changed.** Slice 4's design review ruled (its R12) that migration 015
creates every `provenance_basis`, `provenance_basis_a` and
`provenance_basis_b` column as `NOT NULL`, and
`archive_hashes.source_revision_id` with them. Slices 5 and 6 no longer
establish basis nullability.

**Why the original ruling no longer applies.** §9.2 reasoned that "SQLite
cannot add a NOT NULL column without a default, and a default would let an
unattributed row pass as attributed". Both halves are still true — measured on
sqlite 3.40.1 / python 3.11.3 / win32: `ADD COLUMN ... TEXT NOT NULL` is
refused on a populated table, and the `DEFAULT 'unknown_legacy'` form that is
accepted would mislabel future rows. What changed is the premise that slice 4
would use `ALTER TABLE` at all. §9.1's composite ownership key **cannot** be
added by `ALTER TABLE` in any form — both
`ADD FOREIGN KEY (a, b)` and `ADD COLUMN ..., FOREIGN KEY (a, b)` are syntax
errors, measured on the same runtime and independently on 3.53.1 / Linux — so
migration 015 rebuilds all four receiving tables regardless. A rebuild creates
the column `NOT NULL` from the start, with no default and no window, so the
constraint the original ruling worked around does not arise.

**What this buys, beyond tidiness.** The nullable window was not merely
untidy. The paired CHECK of §9.2 accepts an all-NULL row — measured: SQL
three-valued logic makes `NULL LIKE 'unresolved%'` evaluate to NULL, and
SQLite accepts a CHECK that is not *false* — so an un-cut-over producer could
write `(NULL, NULL)` rows that pass every constraint the migration adds.
`NOT NULL` closes that at the column, and is the only defence that reaches a
writer already holding a connection when 015 commits.

**What did NOT change.** `source_revision_id` stays nullable on
`archive_content_signatures`, `archive_inspections`, `page_inventory` and both
candidate sides, because unresolved attribution is legitimate on all of them.
Slices 5 and 6 still rebuild these tables — for uniqueness, supersession,
parameters and their trigger sets. Only the nullability of the basis columns
moved.

**Authority.** Slice 4 design review, recorded in
`docs/slice4_migration_design.md` R12 and §5. Superseded passages below are
marked `[SUPERSEDED 2026-09-01 — see §0]` at the site.

**Open.** Nothing arising from this change. The slice-5 and slice-6 rebuilds
retain every other obligation this document assigns them.

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

### 4.1 Three different relationships hide behind one apparent agreement

Read naively, the census says every piece of content evidence already agrees
with its revision:

```text
archive_hashes.digest matching some revision of its archive     59,541 / 59,541
content signature digest matching some revision of its archive  58,437 / 58,437
inspection file size agreeing with revision file size           59,541 / 59,541
inspection page count agreeing with revision page count         59,541 / 59,541
```

Comparing these tables against the revisions **as a check** proves nothing,
because migration 014 built the revision fields from those same tables. But the
backfill is also a causal record, and it did not do the same thing to every
table. Three relationships exist, and conflating any two of them overstates the
result.

**Identity seed — `archive_hashes` only.** Each of the 59,541 hash rows *is
what created* its revision's `archive_sha256`, which is the revision's
immutable byte identity: `identity_state` is `established` exactly when that
column is non-NULL, and the CHECK ties the two in both directions. The digest
did not exist on the revision before the backfill and came from nowhere else.
Binding the hash row to that revision records the actual causal history of the
identity.

**Field seed — `archive_content_signatures`.** The backfill copied
`s.digest` into `r.content_signature`, which is an *ancillary nullable column*.
No CHECK ties it to `archive_sha256`, nothing about the revision's identity
depends on it, and §4.2 measures 16 rows where the two describe different byte
generations of the same archive.

That last point is why excluding the 16 does **not** promote the remaining
58,421 to proven ownership. The 16 were detectable only because their sizes
differ from the current location's. A rewrite that preserved file size would
produce exactly the same contradiction and leave no size signal at all, so what
the exclusion establishes is narrower than it looks: that no *further
size-detectable* case exists. It says nothing about undetectable ones.

A field seed is therefore a causal fact worth recording — 014 did copy from
this row — while remaining a **conservative, proxy-grade** basis that must not
be consumed as revision-granular evidence. An earlier draft of this document
placed hashes and signatures in one basis and was wrong to.

**Inherited — `archive_inspections`, `archive_pages`.** Never joined by the
backfill at all; `file_size` and `page_count` came from `archive_files`. Exactly
one candidate revision, nothing contradicting it: unique, unchallenged, and
unverified.

The rule the reviewer set still holds and still bites: currency is not
ownership, and uniqueness is not proof. What it does not license is collapsing
a documented causal act into the same bucket as an unchecked coincidence — nor
collapsing the creation of an identity into the population of a side field.

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
| `archive_quarantine` | `archive/quarantine.py` | a specific failed read | **no, by decision** | none — §5.1 |
| `page_hashes` | `archive/page_hashing.py`, `archive/perceptual_hashing.py` | hash of one page's bytes | **inherited** | none — reaches the revision through `archive_pages.page_id` |
| `jobs` | `jobs/queue.py` | scheduled work against an identity | **no** | none — §8.6 |
| `file_locations` | `library/repository.py`, `library/relocation_repair.py` | where an identity lives | **no** | none; the revision-at-a-location fact is `archive_revision_observations` |
| `file_events` | four producers | movement of paths | **no** | none |
| `archive_retirements` | `archive/disposition.py` | a decision about an identity | **no** | none |
| `archive_supersessions` | `archive/disposition.py` | a relation between two identities | **no** | none — never a digest, by design |
| `archive_disposition_events` | trigger-maintained | history of identity decisions | **no** | none |
| `processing_items` | run infrastructure | run bookkeeping | **no** | none |
| `archive_revision_observations` | `database/dal.py` | a sighting of a revision | already revision-keyed | — |

**Five tables gain an ownership key** — `archive_inspections`,
`archive_hashes`, `archive_content_signatures`, `archive_pages`, and
`near_duplicate_candidates` (two keys). `archive_hashes` is included in the
schema slice, not merely named; §9.7 records the decision to retain it
throughout Step 4.

### 5.1 Quarantine stays identity-scoped

An earlier draft made `archive_quarantine` a sixth receiving table with a
column that would have been NULL for every row that exists and every row any
current producer could write. A quarantine records that bytes **could not be
read**; no digest is obtained, so nothing can bind it, and a guaranteed-NULL
ownership column adds no provenance while implying one is available.

It therefore keeps `archive_id` alone. What would reverse this: a producer that
hashes the quarantined file itself and mints a revision for the corrupt byte
state. No such producer exists or is planned, and inventing the column ahead of
it would be building for an anticipated need the roadmap explicitly defers.

Nine tables are identity-scoped and correctly key on `archive_id` today.
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

| table | ownership | algorithm | version | parameters | run | `created_at` | supersession | idempotency today |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `archive_inspections` | missing | n/a | **missing (slice 4, §6.5)** | n/a | missing | present | **missing** | UPSERT on `archive_id` |
| `archive_hashes` | missing | present | present | n/a | missing | present | **missing** | UPSERT on `archive_id` |
| `archive_content_signatures` | missing | present | present | n/a | missing | present | **missing** | UPSERT on `archive_id` |
| `archive_pages` | missing | n/a | n/a | n/a | missing | present | **missing** | `UNIQUE (archive_id, page_index)` |
| `page_hashes` | inherited | present | present | deferred | missing | present | **missing** | `UNIQUE (page_id, algorithm, algorithm_version)` — **already the shape the roadmap asks for** |
| `near_duplicate_candidates` | missing ×2 | `match_method` only | **missing — version is embedded in the method string** | **missing — thresholds unrecorded** | missing | present | **missing** | `UNIQUE (archive_a_id, archive_b_id, match_method)` |
| `archive_quarantine` | missing | n/a | n/a | n/a | missing | `quarantined_at` | n/a | UPSERT on `archive_id` |

"n/a" means the table records a structural read or an operator action rather
than an algorithmic result, so an algorithm column would have nothing to hold.

`created_at` is the one roadmap field already satisfied everywhere: every
receiving table carries it (`archive_quarantine` names it `quarantined_at`), so
it needs no slice. An earlier draft omitted the column from this matrix
entirely, which read as an unexamined gap rather than as the settled matter it
is.

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
`parameters_json` + `parameters_digest` + `parameters_basis`, and supersession
columns — with uniqueness moving to `(revision_a_id, revision_b_id,
match_algorithm, match_algorithm_version, parameters_digest)`, split into the
four branches of §9.6 because the digest is NULL for every historical row.
This is a bigger change than the other five tables combined and is given its
own slice.

### 6.3 Run provenance: historical backfill deferred, future rows are not

`processing_runs` and `processing_stages` both hold **0 rows**. That is a
sufficient reason why the historical rows cannot be given a
`processing_run_id`: there is no run to point at, and inventing one would
manufacture provenance rather than record it.

**Run provenance has a different population from ownership, and an earlier
draft used the ownership figure for both.** It cited 3,135,910 here, which is
the count of rows *receiving an ownership column* (§7.2). `page_hashes`
receives no ownership column — it reaches its revision through
`archive_pages.page_id` (§5) — but that is an argument about ownership only.
Run provenance is not inherited: a page hash is computed by a run of its own,
`dhash v1` and `phash v1` over one page were not necessarily produced by the
same run as its `sha256 v1`, and §6.1 marks the column missing for that table
while §12.2 counts it among the five non-candidate tables whose run provenance
is deferred. So its 8,821,073 rows belong in this population:

```text
ownership-receiving rows (§7.2)                     3,135,910
page_hashes                                         8,821,073
                                                   ----------
rows with no run provenance and nothing to point at 11,956,983
```

The ownership total stays 3,135,910 and is still provisional on slice 2
(§7.2); this figure is the run-provenance population and is provisional in the
same way, since the page-inventory design may change where a page hash's run
would be recorded. Two populations, two numbers, and the smaller one is not a
subset boundary anyone should read as the whole.

It is **not** a reason to keep creating new evidence without run provenance,
and an earlier draft of this document elided the two. The roadmap requires
future candidate scores to retain their processing run, so slice 6 must either
begin writing `processing_runs` rows -- the table exists and was built for
this -- or define an equally durable run identity carried on the row. The
recommendation is the former: a table that exists and is unused is a cheaper
answer than a second mechanism.

For the other tables the requirement is **deferred past slice 5**, with a
reason rather than by omission. Their producers change in slices 4 and 5 —
slice 4 for the basis and inspector-version rules of §6.5, slice 5 for the
append switch — but neither slice adds a `processing_run_id` because nothing writes a `processing_runs` row
until slice 6 introduces run-writing for the near-duplicate detector. Adding
the column in slice 5 would create a foreign key to an empty table that every
producer would populate with NULL — the same objection that defers the
historical backfill, applied to new rows.

It therefore follows slice 6, once a run-writing mechanism exists to point at.
That slice is not sequenced in this document; it is recorded in §12.2 as
explicitly deferred rather than left to be discovered missing.

### 6.4 Acceptance criteria

| roadmap criterion | status | where addressed |
| --- | --- | --- |
| existing evidence attributable to a revision | not met | slice 4 |
| algorithm versions cannot be silently mixed | met for `page_hashes`, `archive_hashes`, `archive_content_signatures`; **not met** for `near_duplicate_candidates` (slice 6) or `archive_inspections` (column in slice 4, enforced in slice 5, §6.5) | slices 4-6 |
| repeated work idempotent at the data layer | met today by per-archive UPSERT; must be **re-established** on the new keys when uniqueness moves | slice 5 |
| superseded evidence distinguishable from active | **not met anywhere** — no table has `superseded_at` / `superseded_by_id` | slice 5 |
| migration preserves all hash values and counts | gate | slice 4 |

Supersession is not cosmetic here: once uniqueness allows a second generation's
evidence to coexist with the first, "which of these two inspections is the
current one" becomes a question the schema must answer. That is why it lands
with the uniqueness move and not before.

### 6.5 Inspections need a version, and it is applicable

An earlier draft left `archive_inspections` marked "version: missing" with no
slice to fix it, which is the shape of an omission rather than a decision.

It is genuinely applicable. An inspection is a procedure with results that
depend on how it was performed: `entry_count`, `page_count`,
`directory_count`, `comic_info_valid` and `crc_verified` all follow from
decisions the inspector makes about what counts as a page, how strictly
ComicInfo is validated, and whether CRCs are checked. Change any of those and
the same bytes produce a different row. That is exactly the condition the
roadmap's "algorithm versions cannot be silently mixed" criterion is aimed at,
and the two inspections would be indistinguishable in the table.

So an `inspector_version` column is introduced in slice 4, with the rest of
the backfill, so that its value and its basis enter the same binding digest as
every other column that migration writes (§11.1). **The producer requirement
lands in slice 4 too**, in the same migration that adds the column: an
inspection written after it must carry `known` and a version. Slice 5 adds
only the uniqueness branches and makes the requirement structural.

**Historical rows must not be given the current version.** An earlier draft
proposed backfilling all 59,541 rows with whatever the present inspector
reports, "recorded as the value it is rather than as a claim". That distinction
does not survive contact with the column: a version written into
`inspector_version` *is* the assertion that this row came from that code, and a
sentence elsewhere saying otherwise does not unwrite it. The 59,541 rows were
produced over months by code that has changed since; nobody knows which
versions, and inventing one uniform answer is exactly the kind of plausible
reconstruction that cannot afterwards be told from a fact.

It mirrors the near-duplicate parameters solution (§9.6):

```text
inspector_version       TEXT
inspector_version_basis TEXT NOT NULL
    CHECK (inspector_version_basis IN ('known', 'unknown_legacy'))
CHECK (
    (inspector_version_basis = 'known'          AND inspector_version IS NOT NULL)
 OR (inspector_version_basis = 'unknown_legacy' AND inspector_version IS NULL)
)
```

All 59,541 historical rows are `unknown_legacy` with a NULL version, written
by slice 4. Every row a producer writes **from slice 4 onward** requires
`known` and a non-NULL version. Both the status and the version are
decision-bearing, so both go into the slice-3 plan digest and the slice-4
binding digest — which is why the columns must exist by slice 4 rather than 5.

An earlier draft deferred the producer requirement to slice 5 while adding the
column in slice 4, which opens a window where a brand-new inspection is
labelled `unknown_legacy`. That is the same false claim this section refuses
to make about the historical 59,541, made about rows whose version is known
perfectly well. `unknown_legacy` describes evidence produced before the column
existed; it must never describe evidence produced after it. Where the column
and the producer rule cannot land together — `parameters_basis`, whose paired
CHECK needs fields that arrive later — the *column* moves to meet the
producer instead (§9.6), rather than the label being stretched to cover rows
it does not describe.

Because the version may be NULL, the active uniqueness needs the same
known/unknown split the near-duplicate table uses (§9.3).

`algorithm` stays "n/a" for this table: there is one inspection procedure, not
a family of interchangeable ones, so a version without an algorithm name is the
honest shape.

---

## 7. Backfill classification

### 7.1 Bases

```text
measured                     -- the producer computed a content DIGEST for the
                                bytes it read and bound the row on that digest,
                                in the transaction that recorded it. Only the
                                archive hasher can reach this today.
stat_matched_revision        -- the producer measured bytes but computed no
                                archive digest, and bound through the unique
                                revision-bound hash row whose size and mtime
                                equal the stat it captured. A stat match is not
                                digest equality, so this basis is CONSERVATIVE
                                and is consumed as proxy (§8.3).
migration_014_identity_seed  -- this row created the revision's archive_sha256,
                                which IS its immutable byte identity.
                                archive_hashes only.
migration_014_field_seed     -- this row populated an ancillary revision field
                                (content_signature). Causal history, but the
                                field is not the identity and can disagree with
                                it, so this basis is CONSERVATIVE and is
                                consumed as proxy, never as revision-granular.
single_revision_inherited    -- exactly one candidate revision existed and
                                nothing contradicted it; unverified
inherited_from_page_evidence -- a downstream row took its revision from the
                                page evidence it read, having read no archive
                                bytes of its own. near_duplicate_candidates
                                only, per side (§7.4). Always CONSERVATIVE and
                                consumed as proxy, whatever the upstream was.
unresolved_drift             -- the row describes bytes no revision holds
unresolved_no_identity       -- no digest was ever obtained for these bytes
```

`measured` and `stat_matched_revision` are both **0** for every historical
row: nothing in this backfill measures or re-reads anything. They exist for
producers, not for the backfill.

The split between the two seed bases is the subject of §4.1 and is not
cosmetic. An identity seed may be consumed as revision-granular evidence; a
field seed may not.

### 7.2 Counts

| table | identity seed | field seed | inherited | unresolved | total |
| --- | ---: | ---: | ---: | ---: | ---: |
| `archive_hashes` | 59,541 | 0 | 0 | 0 | 59,541 |
| `archive_content_signatures` | 0 | 58,421 | 0 | 16 `drift` | 58,437 |
| `archive_inspections` | 0 | 0 | 59,541 | 0 | 59,541 |
| `archive_pages` | 0 | 0 | 2,954,623 | 768 `drift` | 2,955,391 |
| `near_duplicate_candidates` | 0 | 0 | 3,000 per side | 0 | 3,000 |
| **total rows receiving a basis** | | | | | **3,135,910** |

`archive_quarantine`'s 35 rows are no longer in this table: it keeps
`archive_id` alone (§5.1), which removes 35 from the earlier total of
3,135,945.

**This total is provisional until slice 2 lands.** If page ownership moves from
`archive_pages` to inventory parents (§9.5), the page contribution changes and
so does everything derived from it:

```text
ownership on page rows        2,955,391 page rows   -> total 3,135,910
ownership on inventory rows      58,432 parent rows -> total   238,951
```

The planner (slice 3) freezes whichever unit slice 2 chooses, and §11.3
explains why it cannot run first.

`page_hashes` receives no column; the 768 rows under drifted pages are reached
through their parent and inherit its unresolved state.

### 7.3 Corrections to earlier drafts

**Quarantine receives no ownership column at all.** Every row is
`failure_category = 'corrupt_archive'`, meaning the bytes could not be read, so
no digest was ever obtained. One of the 35 archives does carry an
`archive_hashes` row, but that hash proves only that *some* bytes were once
hashed; the quarantine record carries no digest and no stat tying its failed
read to those bytes. Since every row would be permanently NULL, the column is
not added — see §5.1.

**The 147 provisional archives are a census gate, not a row classification.**
Their only evidence is job rows, and jobs are excluded from provenance (§8.6),
so no receiving table exists that could hold an `unresolved_provisional` basis.
That value is removed from the vocabulary. The 147 remain as an archive-level
gate: *any* future evidence row for an archive whose revision has no digest must
be unresolved, and the backfill planner reports the count so the gate is visible.

**Content signatures are field seeds, not identity seeds** (§4.1).

### 7.4 Per-side provenance for near-duplicate candidates

A candidate row is one comparison of two page sets, and each side is bound
independently. One `provenance_basis` cannot describe a pair whose sides
differ — a bound side compared against an unresolved one is an ordinary
case, and §9.6.2 measures what the index set does with it. The table
therefore takes `provenance_basis_a` and `provenance_basis_b`, each paired to
its own revision key by its own CHECK.

Both sides draw on a vocabulary of the table's own (§9.4.2):
`inherited_from_page_evidence` for a side whose page evidence has bound,
`single_revision_inherited` for the 3,000 backfilled rows, and
`unresolved_no_identity` otherwise. A side never copies its page evidence's
basis: the candidate read no bytes, so labelling a side `stat_matched_revision`
would claim a measurement it never made. Every value here resolves to **proxy**
granularity, which is why an earlier draft's phrase "a measured side" was
wrong — no side of a candidate is ever `measured`.

---

## 8. Per-producer ownership paths

There is no single rule that every producer can follow. An earlier draft stated
one, and it is not implementable: only the archive hasher computes the raw
archive digest, and it does not compute it inside the transaction either.

### 8.1 What the archive hasher actually does

```text
outside the transaction   calculate_archive_hash(path) -> digest, size, mtime
inside the transaction    _assert_still_current(archive_id, location_id, path)
                          _assert_file_matches(path, result)      [fail fast]
                          hashes.save(...)
                          record_or_reuse(archive_sha256=result.digest)
                          set_current(...) ; observe(...)
                          re-stat after every write, before COMMIT
```

The measurement is outside; what the transaction guarantees is that the bytes
measured are still the archive's current bytes at the moment they are recorded,
re-checked immediately before COMMIT. `SourceChangedError` is categorised as
retryable `filesystem_io` after rollback.

The essential property is not "measure inside a transaction" but: **the revision
is keyed on a content identity the producer established for the bytes it read,
and is never read from `archive_files.current_revision_id`** — a mutable pointer
another writer may have moved.

### 8.2 The five paths

| producer | writes | ownership path | first-write state |
| --- | --- | --- | --- |
| archive hashing | `archive_hashes` | **direct**: its own digest keys `record_or_reuse`; bind to the returned revision | `measured` |
| page hashing | `archive_pages`, `page_hashes`, `archive_content_signatures` | **stat-matched**: it captures `source_file_size` / `source_modified_time_ns` for the file it read, and binds through the **unique** revision-bound `archive_hashes` row whose `(file_size, modified_time_ns)` equal them (§8.3) | `stat_matched_revision` on a single match, else `unresolved_no_identity` |
| inspection | `archive_inspections` | **initially unresolved, bound later**: an inspection has `inspected_file_size` / `inspected_modified_time_ns` but no digest, and normally runs *before* hashing, so at first write no revision may exist to bind to | `unresolved_no_identity`, then `stat_matched_revision` once a single revision-bound hash matches |
| near-duplicate | `near_duplicate_candidates` | **inherited per side**: each side takes the revision of the page evidence it compared | per side: inherited from the page rows, or unresolved if that side is |
| quarantine | `archive_quarantine` | **none** — identity-scoped by decision (§5.1); no ownership column exists to write | n/a |

Only the first path establishes ownership from a digest it computed itself.
The next two derive it from a stat, the fourth inherits it, and the fifth has
no column at all — which is why the basis vocabulary distinguishes them
rather than calling all of them `measured`. An earlier draft did call the
stat-matched paths `measured` while stating two paragraphs later that a stat
match is not digest equality; the two cannot both be true.

### 8.3 Why the stat match is sound, and where it stops

`archive_hashes` records the size and mtime of the file it hashed, and page
hashing records the size and mtime of the file it read. When both match, the two
producers read a file in the same state, so the page evidence describes the same
bytes the hash does — and the hash's revision is that byte state's identity.

This is the same join the codebase already uses to decide signature freshness
(`page_hashing.py` joins `acs.source_file_size = fl.file_size AND
acs.source_modified_time_ns = fl.modified_time_ns`), so the technique is
established here rather than introduced.

It stops being sound in one direction that must be stated: a size-and-mtime
match is not a proof of byte equality, only of an unchanged stat. Two byte
states of one archive can share a size and an mtime — a size-preserving
rewrite followed by a restored mtime is the obvious construction, and §4.2
already measures 16 archives whose bytes changed under a stat the rest of the
system treated as stable.

Three consequences, all of them requirements rather than caveats:

**The match must be unique.** The lookup binds only when **exactly one**
revision-bound `archive_hashes` row for that archive has the captured size and
mtime. Zero matches leaves the evidence `unresolved_no_identity`. *Two or more*
matches also leaves it unresolved — once an archive has several revisions,
two different digests sharing a stat is no longer hypothetical, and picking
either one would be a coin flip recorded as a fact.

**It gets its own basis.** `stat_matched_revision`, not `measured`. Its planner
granularity is conservative (§10): a stat-matched row is proxy evidence, not
revision-granular, until a producer carries a digest.

**It is revalidated before COMMIT.** The location identity and the captured
stat are re-checked immediately before COMMIT, exactly as the archive hasher
does, so a file replaced between the match and the write cannot leave evidence
bound to a revision it never described.

The way to promote this basis later is to have page hashing carry the archive
digest forward from the job that computed it, making the binding an identity
match rather than a stat match. That is not proposed here.

### 8.4 The later-binding step for inspections

Binding an initially-unresolved inspection is itself a producer action and needs
the same discipline: it must match on the recorded stat against a revision-bound
hash row, inside a transaction, and must **not** bind by reading the current
pointer. An inspection whose stat matches no revision-bound hash stays
unresolved — including permanently, which is a legitimate outcome for an archive
that was inspected and then never successfully hashed.

### 8.5 Supersession is not implied by any of these

None of these paths supersede an existing row merely by producing a new one. See
§9.4: supersession is caused by re-running the same method against the same
revision, and by nothing else.

### 8.6 Why `jobs` gets no revision column

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

### 9.1 The ownership key

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

`NO ACTION`, never `CASCADE`. Deleting a revision must not silently remove the
evidence that describes it — the same argument 014 made for lineage.

### 9.2 Nullability and basis

Nullable, and never nullable *silently*:

```text
source_revision_id INTEGER          -- NULL = ownership not established
provenance_basis   TEXT
    CHECK (provenance_basis IN (
        'measured',
        'stat_matched_revision',
        'migration_014_identity_seed',
        'migration_014_field_seed',
        'single_revision_inherited',
        'inherited_from_page_evidence',
        'unresolved_drift',
        'unresolved_no_identity'
    ))
CHECK (
    (source_revision_id IS NOT NULL
     AND provenance_basis IN ('measured',
                              'stat_matched_revision',
                              'migration_014_identity_seed',
                              'migration_014_field_seed',
                              'single_revision_inherited',
                              'inherited_from_page_evidence'))
 OR (source_revision_id IS NULL
     AND provenance_basis LIKE 'unresolved%')
)
```

`stat_matched_revision` is in the **bound** half: a stat-matched row does carry
a revision. Its conservatism lives in how it is *consumed* (§10 resolves it to
proxy granularity), not in whether it may be bound. An earlier draft introduced
the basis in §7 and the producer paths but left it out of this CHECK, which
would have made every stat-matched binding fail the constraint the moment a
producer wrote one.

`inherited_from_page_evidence` is in the **bound** half for the same reason,
and was omitted from this CHECK in exactly the same way. The basis was
introduced in §7.4 and §9.4.2 without being added here or to §7.1, and the
omission reproduces: with the earlier list, a candidate side carrying it was
rejected on insert, as was the documented
`unresolved_no_identity -> inherited_from_page_evidence` transition. That is
the identical defect recorded in the paragraph above, committed a second time
against a second basis — which is the argument for deriving each table's
vocabulary from this union in the migration rather than restating it by hand.

Executed against the corrected pair, applied twice to
`near_duplicate_candidates` as §7.4 requires. The harness parses the
vocabulary out of this section rather than restating it, so the test cannot
pass against a list the document no longer contains:

```text
VB-01  backfilled row: single_revision_inherited both sides      ACCEPTED  s4
VB-02  producer row: inherited_from_page_evidence both sides     ACCEPTED  s4
VB-03  mixed row: bound side inherited, other unresolved         ACCEPTED  s4
VB-04  bound side inherited, other single_revision_inherited     ACCEPTED  s4
VB-05  a bound side carrying an unresolved basis                 REJECTED  s4
VB-06  an unbound side carrying a bound basis                    REJECTED  s4
VB-07  side A binds: unresolved -> inherited_from_page_evidence  ACCEPTED  s4
```

The last two matter as much as the first five: widening the vocabulary must
not weaken the pairing, and they confirm a side still cannot carry a revision
without saying how it got one, or omit one without saying why.

**This is the union, not any single table's vocabulary.** Each receiving table
carries a narrower CHECK of its own (§9.4.2): `measured` is legal only on
`archive_hashes`, because that is the only producer that computes a digest.
Relying on this global list alone let an inspection bind straight to
`measured` — which §10 then consumes as revision-granular evidence.

The paired CHECK is load-bearing: a row cannot carry a revision without saying
how it got one, or be NULL without saying why.
`near_duplicate_candidates` carries this pair twice, once per side.

**[SUPERSEDED 2026-09-01 — see §0.** Slice 4 rebuilds all four receiving
tables for §9.1's composite key, which `ALTER TABLE` cannot add, so every basis
column is created `NOT NULL` there and no nullable window exists. The reasoning
below is preserved as the record of why the window was accepted when slice 4
was expected to use `ALTER TABLE`; its premise, not its logic, is what
changed.**]

**`provenance_basis` is nullable during slice 4 and becomes `NOT NULL` in
slice 5 — except on `near_duplicate_candidates`, where it becomes `NOT NULL`
in slice 6.** SQLite cannot add a NOT NULL column without a default, and a
default would let an unattributed row pass as attributed. So slice 4 adds it
nullable, its gate proves every existing row is populated, and a later table
rebuild — happening anyway for the uniqueness change — makes it structurally
NOT NULL.

Which rebuild, and therefore how long the window lasts, differs by table.
There are five receiving tables in all (§5). The four non-candidate ones are
rebuilt in slice 5, one slice after the window opens. The fifth is
`near_duplicate_candidates`, which slice 5 excludes: its two per-side bases
cannot be rebuilt then, and they become structurally non-null in the slice-6
rebuild that also lands its indexes and triggers (§9.6.1) — two slices after
the window opens. An earlier draft said "one slice long" without qualification,
which was true of four tables out of five. Both windows are closed by a gate
rather than by convention.

### 9.3 Uniqueness must be partial, and must cover unresolved rows

"Move uniqueness to revision scope" is insufficient, because **SQLite treats
NULLs as distinct in a UNIQUE constraint**. Migration 014 hit this exact problem
and solved it with a partial unique index for provisional revisions, so the
technique is established here rather than invented.

A plain `UNIQUE (source_revision_id)` would permit unlimited unresolved
signatures or quarantine rows to accumulate, `UNIQUE (source_revision_id,
page_index)` would permit duplicate unresolved page indexes, and nullable
revision pairs would leave near-duplicate idempotency weaker than it is today.

Every table therefore needs **two** partial unique indexes — one for bound
active rows, one for unresolved active rows keyed conservatively by archive:

```text
archive_inspections    -- four branches: inspector_version may be NULL (§6.5)
                       -- executable form and results below
    bound,      version known    (source_revision_id, inspector_version)
    bound,      version unknown  (source_revision_id)
    unresolved, version known    (archive_id, inspector_version)
    unresolved, version unknown  (archive_id)

archive_hashes         -- ONE branch: this table has no unresolved state
    bound       (source_revision_id, algorithm, algorithm_version)

archive_content_signatures
    bound       (source_revision_id, algorithm, algorithm_version)
    unresolved  (archive_id, algorithm, algorithm_version)

archive_pages          -- NO active predicate; see below
    bound       (source_revision_id, page_index)  WHERE bound
    unresolved  (archive_id, page_index)          WHERE unresolved

page_hashes            -- NO active predicate; see below
    (page_id, algorithm, algorithm_version)       -- unchanged shape
```

where *bound* is `source_revision_id IS NOT NULL`, *unresolved* is
`source_revision_id IS NULL`, and *active* is `superseded_at IS NULL`.

`archive_quarantine` has no entry: it keeps `archive_id UNIQUE` unchanged,
because it gains no ownership column (§5.1).

**`archive_hashes` gets one branch, not two.** §9.4.2 removes
`unresolved_no_identity` from its vocabulary — the hasher computes a digest
and binds inside the same transaction, so an unresolved hash row is
unreachable — which is what makes `source_revision_id` NOT NULL at the slice-5
rebuild. An unresolved partial index on a NOT NULL column is dead on arrival:
its `WHERE source_revision_id IS NULL` can never match a row. An earlier draft
listed the pair here anyway, having been written before the vocabulary was
tightened. Nothing would have failed — which is the problem, since a dead
index is indistinguishable from a working one until someone relies on it.
Measured against the post-rebuild shape:

```text
AH-01  an unresolved archive_hashes row is unconstructible  REJECTED  s5
       rows such an index could ever hold (observation)            0
```

**`archive_pages` and `page_hashes` deliberately carry no `WHERE active`
predicate.** Page supersession is excluded from the schema slice pending the
inventory-parent design (§9.5), so those two tables have no active/superseded
state for a predicate to test — an earlier draft wrote `WHERE active` for
them anyway, against columns that will not exist. Their interim indexes omit
it, and the predicate is added by the inventory design, not before it.

The unresolved keys are deliberately archive-scoped: an unresolved row cannot be
distinguished by revision, so the conservative cap is one per archive per
method, exactly as today. That preserves current idempotency rather than
loosening it while the bound population grows.

#### 9.3.1 The executable form, and what it was measured to do

Prose is not enough here: every one of these predicates turns on SQLite's
treatment of NULL, and a partial index whose predicate is subtly wrong fails
open rather than loudly. Written out for `archive_inspections` and executed:

```sql
CREATE UNIQUE INDEX ui_insp_bound_known
    ON archive_inspections(source_revision_id, inspector_version)
 WHERE source_revision_id IS NOT NULL
   AND inspector_version  IS NOT NULL
   AND superseded_at      IS NULL;

CREATE UNIQUE INDEX ui_insp_bound_unknown
    ON archive_inspections(source_revision_id)
 WHERE source_revision_id IS NOT NULL
   AND inspector_version  IS NULL
   AND superseded_at      IS NULL;

CREATE UNIQUE INDEX ui_insp_unresolved_known
    ON archive_inspections(archive_id, inspector_version)
 WHERE source_revision_id IS NULL
   AND inspector_version  IS NOT NULL
   AND superseded_at      IS NULL;

CREATE UNIQUE INDEX ui_insp_unresolved_unknown
    ON archive_inspections(archive_id)
 WHERE source_revision_id IS NULL
   AND inspector_version  IS NULL
   AND superseded_at      IS NULL;
```

```text
II-01  two active bound rows, same revision + version            REJECTED  s5
II-02  two active bound rows, same revision, versions NULL       REJECTED  s5
II-03  bound + unresolved for the same archive coexist           ACCEPTED  s5
II-04  two active unresolved rows, same archive, versions NULL   REJECTED  s5
II-05  two active unresolved rows, same archive, same version    REJECTED  s5
II-06  same identity, one superseded -> coexist                  ACCEPTED  s5
II-07  different version, both active -> coexist                 ACCEPTED  s5
II-08  known-version and unknown-version bound rows coexist      ACCEPTED  s5
II-09  unresolved -> bound colliding with an existing bound row  REJECTED  s5
II-10  unresolved -> bound with no collision                     ACCEPTED  s5
```

Two results are worth drawing out because they are the ones prose would have
got wrong.

**The unresolved-to-bound transition is policed by the index, not only by the
trigger.** A row completing its attribution moves from the unresolved branch
into the bound branch, and if a bound row for that revision and version already
exists the UPDATE fails. That is the correct outcome — two rows claiming to
be the current evidence for one revision — but nothing in §9.4.2 says
so; it falls out of the index set, and a producer must be ready for the
constraint failure rather than assuming its bind will succeed.

**A known-version and an unknown-version row for the same revision coexist.**
That is deliberate: the legacy row's version is unknown, so it cannot be shown
to be the same evidence as the versioned one. It also means slice 5 does not
silently deduplicate history, and that any later reconciliation of legacy rows
is an explicit operation rather than a side effect.

`near_duplicate_candidates` uniqueness is defined in §9.6 and changes in
**slice 6, not slice 5** — an earlier draft listed it in both.

### 9.4 Supersession: a rerun is not automatically a replacement

Two earlier drafts were wrong here in opposite directions. The first let a new
revision supersede older evidence; the second said any rerun supersedes. Both
are too loose, and the missing distinction is what the *result* was.

**A newer revision never supersedes older evidence.** An inspection of revision
1 remains a true and current statement about revision 1 for as long as revision
1 exists. Revision 2 appearing says nothing about it. Marking it superseded
would destroy the per-revision history the whole step exists to create.

**A rerun has three outcomes, and only one of them supersedes.** Let *evidence
identity* be `(source_revision_id, algorithm, algorithm_version, parameters)`:

```text
same identity, byte-identical result
    -> idempotent reuse. No new row, nothing superseded. The rerun may record
       a processing run or an observation, which is where "it ran again" is
       written down.

same identity, DIFFERENT result
    -> a contradiction: the same method over the same bytes produced two
       answers. Either the method is nondeterministic, or something is wrong.
       This may supersede, but only as an EXPLICIT replacement carrying a
       reason -- it is not a routine outcome and must not be recorded as one.

different identity (different revision, algorithm, version or parameters)
    -> independent active evidence. dhash v1 and phash v1 over revision 7 are
       both active, and so are sha256 v1 over revision 7 and over revision 8.
```

That is why supersession needs a reason column of its own:

```text
superseded_at      TEXT
superseded_by_id   INTEGER
superseded_reason  TEXT      -- non-blank, same trim() form as migration 012/013
```

An automatic supersession with no stated reason is indistinguishable from a
bug that wrote the same evidence twice.

#### 9.4.1 The write order is not obvious, and prose will not fix it

The active partial unique key and the paired `superseded_by_id` conflict with
each other under a naive write order:

```text
insert the successor first    -> it is active, and so is the predecessor, so
                                 both sit in the active partial unique index:
                                 constraint violation.
supersede the predecessor
first                         -> superseded_by_id must name a row that has not
                                 been inserted yet: foreign-key violation.
```

The concrete pattern, which works because SQLite checks a **deferred** foreign
key at COMMIT and an index immediately:

```text
0. BEGIN IMMEDIATE -- the write lock is taken BEFORE the id is chosen, so two
                      writers cannot preallocate the same rowid. This is also
                      what makes the id-ordering CHECK in 9.4.2 sound.
1. preallocate the successor's rowid
       SELECT IFNULL(MAX(id), 0) + 1 FROM <table>
2. UPDATE the predecessor
       SET superseded_at = <now>,
           superseded_by_id = <preallocated>,
           superseded_reason = <reason>
   -- it leaves the active partial index at this point, and the dangling
   -- superseded_by_id is legal because the self-FK is deferred
3. INSERT the successor with that explicit id
       -- the active index now has room; the deferred FK resolves
4. COMMIT -- the deferred FK is checked here and succeeds
```

```text
FOREIGN KEY (superseded_by_id) REFERENCES <same table>(id)
    DEFERRABLE INITIALLY DEFERRED
```

Migration 014 already uses `DEFERRABLE INITIALLY DEFERRED` for its lineage key,
so this is an established pattern in this schema rather than a new one. The
whole sequence runs in one DAL-owned transaction; a rollback leaves neither row
changed.

#### 9.4.2 Cycles and dead ends need an enforcer, not a sentence

An earlier draft listed "no chains into a dead end" as prose, which enforces
nothing. Two mechanisms, both concrete:

```text
CHECK ((superseded_at IS NULL) = (superseded_by_id IS NULL))
CHECK ((superseded_at IS NULL) = (superseded_reason IS NULL))
CHECK (superseded_by_id IS NULL OR superseded_by_id > id)
```

The third does the real work. Rowids are allocated monotonically and the
preallocation above preserves that, so requiring the successor's id to be
**greater** makes a cycle impossible by construction and makes every chain
finite and forward-ordered — no trigger walk, no recursive CTE, one CHECK.
A chain must therefore terminate, and it can only terminate at a row with a
NULL `superseded_by_id`, which is an active row. Dead ends are excluded by the
same constraint that excludes cycles.

What a CHECK cannot express, because it reads another row, is that the
successor describes the *same* revision and the *same* evidence identity.

##### The evidence-identity tuple is defined once, per table

An earlier draft spelled the identity out inline in each trigger and got a
different subset every time — one omitted parameters, another omitted
`inspector_version`, and none of them handled the near-duplicate table's
pairwise columns. A successor could satisfy both triggers and then have an
omitted component changed, which is the same hole the immutability trigger was
supposed to close.

So the tuple is named per table and every mechanism below is generated from
that one definition:

```text
archive_hashes              (archive_id, source_revision_id,
                             algorithm, algorithm_version)

archive_content_signatures  (archive_id, source_revision_id,
                             algorithm, algorithm_version)

archive_inspections         (archive_id, source_revision_id,
                             inspector_version)

near_duplicate_candidates   (archive_a_id, archive_b_id,
                             revision_a_id, revision_b_id,
                             match_algorithm, match_algorithm_version,
                             parameters_digest)

archive_pages / page inventory
                            deferred to slice 2 (§9.5); whatever that design
                            settles on becomes this table's tuple
```

Note `near_duplicate_candidates` has no single `archive_id` or
`source_revision_id`: its identity is pairwise, which is precisely why a
generic four-column template could not express it.

**The same tuple is used in all four places**, and they must not drift apart:

```text
1. the active partial unique index          (§9.3, §9.6)
2. the idempotent-result lookup             (§9.4, "same identity")
3. trg_<table>_supersede_checks_existing_successor
4. trg_<table>_insert_checks_waiting_predecessors
```

**The identity-immutability trigger is not one of them.** An earlier draft
appended it to this list as "the identity-immutability trigger, which lists
exactly these columns", and taken literally that breaks the design: the
evidence-identity tuple contains `source_revision_id`, which the disposition
list below assigns to `attribution` — the one column with a guarded
transition. A trigger generated from the tuple freezes it, and the documented
`unresolved_no_identity -> stat_matched_revision` bind is then rejected.
Measured both ways on the real column set:

```text
IT-01  trigger from the evidence-identity tuple:
       the documented binding transition                 REJECTED  s5  WRONG
IT-02  trigger from the 'identity' disposition:
       the documented binding transition                 ACCEPTED  s5
IT-03  the same trigger still freezes inspector_version  REJECTED  s5
```

So the immutability trigger is generated from the **`identity` disposition**
(`id`, `archive_id`, `inspector_version`, `inspector_version_basis` for this
table), and the four mechanisms above are generated from the **evidence-identity
tuple**. The two sets overlap without being the same set, and the difference
is exactly `source_revision_id`: it participates in what makes two rows the
same evidence, and it is still permitted to change once. Naming them
separately is what keeps the attribution transition reachable.

**Every comparison must be NULL-safe.** `source_revision_id`,
`inspector_version`, `parameters_digest` and the near-duplicate revision
columns are all nullable, and `a <> b` is NULL — neither true nor false — when
either side is NULL, so a mismatch involving a NULL would pass a trigger
silently. SQLite's `IS NOT` is the NULL-safe form and is what these triggers
use throughout.

##### The two complementary triggers

A single `BEFORE UPDATE` trigger asserting `NOT EXISTS (matching successor)`
**cannot work**, and an earlier draft proposed exactly that. Step 2 of the write
order updates the predecessor *before* the successor exists, so such a trigger
sees no successor and aborts every valid replacement. The deferred foreign key
exists precisely to permit that window; a trigger that refuses it cancels the
mechanism it was paired with.

Two triggers instead, each covering what the other cannot. Shown for
`archive_inspections`; the others differ only in which columns the tuple names.

```sql
CREATE TRIGGER trg_archive_inspections_supersede_checks_existing_successor
BEFORE UPDATE OF superseded_by_id ON archive_inspections
FOR EACH ROW
WHEN NEW.superseded_by_id IS NOT NULL
 AND EXISTS (SELECT 1 FROM archive_inspections
              WHERE id = NEW.superseded_by_id)
 AND NOT EXISTS (
        SELECT 1 FROM archive_inspections AS succ
         WHERE succ.id                 =      NEW.superseded_by_id
           AND succ.archive_id         IS     NEW.archive_id
           AND succ.source_revision_id IS     NEW.source_revision_id
           AND succ.inspector_version  IS     NEW.inspector_version)
BEGIN
    SELECT RAISE(ABORT,
        'successor does not describe the same evidence identity');
END;
```

The `EXISTS` guard is what makes the deferral usable: a successor that is not
there yet is the deliberately deferred case and passes; a successor that *is*
there and does not match is refused at the offending statement.

```sql
CREATE TRIGGER trg_archive_inspections_insert_checks_waiting_predecessors
AFTER INSERT ON archive_inspections
FOR EACH ROW
WHEN EXISTS (
        SELECT 1 FROM archive_inspections AS pred
         WHERE pred.superseded_by_id = NEW.id
           AND (pred.archive_id         IS NOT NEW.archive_id
             OR pred.source_revision_id IS NOT NEW.source_revision_id
             OR pred.inspector_version  IS NOT NEW.inspector_version))
BEGIN
    SELECT RAISE(ABORT,
        'a predecessor already points here with another identity');
END;
```

This is the half that closes the window the first trigger leaves open: every
predecessor already pointing at the row being inserted must match it. Together
with the deferred FK, which guarantees the successor exists by COMMIT, the pair
is complete — the FK proves it exists, this proves it matches.

##### Every column gets a disposition, and the triggers are generated from it

An earlier draft declared three field sets in prose and then wrote a trigger
listing ten columns. Executed against the real `archive_inspections` shape, the
gap between the declaration and the SQL was wide:

```text
status rewrite                ACCEPTED     archive_format rewrite   ACCEPTED
inspected_path rewrite        ACCEPTED     encrypted rewrite        ACCEPTED
comic_info_present rewrite    ACCEPTED     comic_info_error rewrite ACCEPTED
created_at rewrite            ACCEPTED     updated_at rewrite       ACCEPTED
```

Every one of those is a measurement or a lifetime fact that the replacement
path exists to protect, and a prose sentence saying "measurement fields are
immutable" protects none of them. The fix is not a longer column list — a
longer list drifts the same way the moment a migration adds a column. **Every
column of every receiving table is assigned exactly one disposition, and the
triggers are generated from that assignment.**

Seven dispositions, plus one that exists on a single table:

```text
identity              immutable, unconditionally
attribution           exactly one guarded transition (below)
measurement           immutable when the value would CHANGE
source_context        may only be cleared, and only by its ON DELETE SET NULL
lifecycle_immutable   created_at
lifecycle_mutable     updated_at
supersession          the one-way lifecycle of the next subsection

review                near_duplicate_candidates only: review_status,
                      reviewed_by, reviewed_at. Mutable, because that is the
                      reviewer workflow, and freezing it would break the
                      feature this table exists for.
```

`archive_inspections`, in full, as the worked example. The other tables get the
same treatment.

```text
identity             id, archive_id, inspector_version, inspector_version_basis
attribution          source_revision_id, provenance_basis
source_context       location_id
measurement          inspected_path, archive_format, status, entry_count,
                     page_count, directory_count, encrypted,
                     comic_info_present, comic_info_valid, comic_info_error,
                     comic_info_json, crc_verified, inspected_file_size,
                     inspected_modified_time_ns, result_json, inspected_at
lifecycle_immutable  created_at
lifecycle_mutable    updated_at
supersession         superseded_at, superseded_by_id, superseded_reason
                                                          -- 28 of 28 columns
```

**The migration must assert that assignment is total**, or this drifts again:

```text
for every receiving table:
    the set of column names in PRAGMA table_info(t)
      == the union of that table's disposition lists
```

A column added later with no disposition fails the assertion instead of
silently becoming mutable. That check is the durable fix; the corrected trigger
below is only this round's instance of it.

##### Measurement immutability compares values, not column lists

```sql
CREATE TRIGGER trg_archive_inspections_results_immutable
BEFORE UPDATE OF inspected_path, archive_format, status, entry_count,
                 page_count, directory_count, encrypted, comic_info_present,
                 comic_info_valid, comic_info_error, comic_info_json,
                 crc_verified, inspected_file_size, inspected_modified_time_ns,
                 result_json, inspected_at, created_at
    ON archive_inspections
FOR EACH ROW
WHEN NEW.inspected_path             IS NOT OLD.inspected_path
  OR NEW.archive_format             IS NOT OLD.archive_format
  OR NEW.status                     IS NOT OLD.status
  OR NEW.entry_count                IS NOT OLD.entry_count
  OR NEW.page_count                 IS NOT OLD.page_count
  OR NEW.directory_count            IS NOT OLD.directory_count
  OR NEW.encrypted                  IS NOT OLD.encrypted
  OR NEW.comic_info_present         IS NOT OLD.comic_info_present
  OR NEW.comic_info_valid           IS NOT OLD.comic_info_valid
  OR NEW.comic_info_error           IS NOT OLD.comic_info_error
  OR NEW.comic_info_json            IS NOT OLD.comic_info_json
  OR NEW.crc_verified               IS NOT OLD.crc_verified
  OR NEW.inspected_file_size        IS NOT OLD.inspected_file_size
  OR NEW.inspected_modified_time_ns IS NOT OLD.inspected_modified_time_ns
  OR NEW.result_json                IS NOT OLD.result_json
  OR NEW.inspected_at               IS NOT OLD.inspected_at
  OR NEW.created_at                 IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'measurement results are immutable; record a replacement');
END;
```

The value comparison is load-bearing twice over, and the earlier
column-list-only form failed both.

**It makes idempotent reuse actually work.** §9.4 says a rerun producing a
byte-identical result is a no-op. A trigger keyed on the column appearing in the
`SET` list fires whether or not the value changed, so the earlier form
**rejected** the very case the model calls idempotent — measured, not reasoned
about. `IS NOT` is used rather than `<>` because these columns are nullable and
`<>` against NULL is NULL rather than true.

**It is also what makes slice 4's interim window safe** (§11.4).

`location_id` cannot be frozen outright: its foreign key is `ON DELETE SET
NULL`, and an unconditional trigger would abort that cascade — the same trap
migration 014 documented for its own delete guard.

An earlier draft only refused a non-NULL new value, which let *any*
`UPDATE ... SET location_id = NULL` through, not only the cascade. The
discriminator is migration 014's parent-existence test, and it works here for
the same measured reason:

```text
during ON DELETE SET NULL   OLD.location_id = 7, parent_visible = 0
during a direct clear       OLD.location_id = 9, parent_visible = 1
```

Measured on SQLite 3.40.1 rather than inferred from the cascade order.

```sql
CREATE TRIGGER trg_archive_inspections_source_context_only_clears
BEFORE UPDATE OF location_id ON archive_inspections
FOR EACH ROW
WHEN NEW.location_id IS NOT OLD.location_id
 AND (NEW.location_id IS NOT NULL
   OR EXISTS (SELECT 1 FROM file_locations WHERE id = OLD.location_id))
BEGIN
    SELECT RAISE(ABORT, 'location_id may only be cleared by ON DELETE SET NULL');
END;
```

So: repointing is refused, a direct clear while the location still exists is
refused, and the cascade — where the parent is already gone — passes.

##### The attribution transition is table-specific, and so is the vocabulary

The global basis CHECK permitted every bound basis on every table, and the
binding trigger accepted any `unresolved%` to non-`unresolved%` move. Together
they let an inspection bind straight to `measured`:

```text
unresolved -> measured                       ACCEPTED   WRONG
unresolved -> migration_014_identity_seed    ACCEPTED   WRONG
```

That is not cosmetic. §10 resolves `measured` to **revision granularity** and
`stat_matched_revision` to **proxy**, so a row that manufactured the stronger
basis would be consumed as revision-granular evidence on the strength of a stat
match. The composite foreign key does not prevent it either: a same-archive
revision exists, so the reference is valid.

The vocabulary becomes table-specific, in the table's own CHECK:

```text
archive_hashes              measured, migration_014_identity_seed
archive_content_signatures  stat_matched_revision, migration_014_field_seed,
                            unresolved_drift, unresolved_no_identity
archive_inspections         stat_matched_revision, single_revision_inherited,
                            unresolved_no_identity
archive_pages / inventory   stat_matched_revision, single_revision_inherited,
                            unresolved_drift, unresolved_no_identity
near_duplicate_candidates   inherited_from_page_evidence,
                            single_revision_inherited,
                            unresolved_no_identity          (per side)
```

Three corrections an earlier draft needed, each found by checking a vocabulary
against the producer that has to write into it:

**`archive_pages` needs `unresolved_no_identity`.** Page hashing writes it
whenever the stat match finds zero or several candidate hash rows (§8.3),
and the transition table below names it as the *starting* value for that
table — so a vocabulary omitting it rejected its own documented producer
path.

**`archive_hashes` loses `unresolved_no_identity`.** It had no producer path
that could write it: the hasher computes a digest and binds inside the same
transaction, so an unresolved hash row is unreachable. Removing it means both
remaining bases are bound, so `archive_hashes.source_revision_id` becomes
**NOT NULL** at the slice-5 rebuild — a tightening the vocabulary makes
available rather than a separate decision.
[**SUPERSEDED 2026-09-01 — see §0:** the tightening is unchanged and still
follows from this vocabulary; it lands at the **slice-4** rebuild rather than
slice 5's, because slice 4 now rebuilds this table.]

**Near-duplicate candidates get their own downstream basis.**
§7.4 permits the two sides of a candidate to carry different bases, which
the earlier two-value vocabulary could not express. The question it raises is
whether a candidate *copies* its page evidence's basis or gets one of its own,
and copying is wrong: a candidate never read any bytes, so labelling a side
`stat_matched_revision` would claim a measurement it did not make.
`inherited_from_page_evidence` says what actually happened, and §10 resolves
it to **proxy** unconditionally — never stronger than the upstream it
inherits from, whatever that upstream was. `single_revision_inherited` remains
for the 3,000 backfilled rows, which were bound by the one-revision-per-archive
census rather than from page evidence.

`measured` now appears only where a producer computes a digest, which is
`archive_hashes` alone (§8.2). `single_revision_inherited` is written by the
backfill and is unreachable by any transition.

And the transition is an exact pair, not a direction:

```text
archive_hashes              none - the hasher binds at INSERT, and has no
                            unresolved state to leave
archive_content_signatures  unresolved_no_identity -> stat_matched_revision
archive_inspections         unresolved_no_identity -> stat_matched_revision
archive_pages / inventory   unresolved_no_identity -> stat_matched_revision
near_duplicate_candidates   unresolved_no_identity -> inherited_from_page_evidence
                            (per side, once that side's page evidence binds)
```

`unresolved_drift` has no outbound transition anywhere: a drift row can only
bind once a revision exists for the generation it describes, and minting that
revision is deferred to a later remediation design.

```sql
CREATE TRIGGER trg_archive_inspections_attribution_binds_once
BEFORE UPDATE OF source_revision_id, provenance_basis ON archive_inspections
FOR EACH ROW
WHEN (NEW.source_revision_id IS NOT OLD.source_revision_id
   OR NEW.provenance_basis   IS NOT OLD.provenance_basis)
 AND NOT (
        OLD.source_revision_id IS NULL
    AND NEW.source_revision_id IS NOT NULL
    AND OLD.provenance_basis = 'unresolved_no_identity'
    AND NEW.provenance_basis = 'stat_matched_revision'
    AND OLD.superseded_at IS NULL
    AND NOT EXISTS (SELECT 1 FROM archive_inspections AS p
                     WHERE p.superseded_by_id = OLD.id)
)
BEGIN
    SELECT RAISE(ABORT,
        'attribution binds once: unresolved_no_identity -> stat_matched_revision');
END;
```

##### Every transition trigger needs a value-change guard first

The measurement trigger compares values because a column-list trigger fires
whether or not the value changed (above). The same argument applies to every
trigger whose `WHEN` tests a *transition*, and an earlier draft applied it to
none of them. `BEFORE UPDATE OF <cols> WHEN NOT (<valid transition>)` asks
"is this a valid transition" without first asking "is this a transition at
all", so a rewrite that changes nothing is judged as though it were a move:

```text
VI-01  attribution rewrite, bound row        as documented: REJECTED  WRONG
VI-02  attribution rewrite, unresolved row   as documented: REJECTED  WRONG
VI-03  supersession rewrite, active row      as documented: REJECTED  WRONG
VI-04  supersession rewrite, historical row  as documented: REJECTED  WRONG
VI-05  source-context rewrite                as documented: REJECTED  WRONG

       the statements, in the same order:
         SET source_revision_id = source_revision_id,
             provenance_basis   = provenance_basis           (VI-01, VI-02)
         SET superseded_at      = superseded_at,
             superseded_by_id   = superseded_by_id,
             superseded_reason  = superseded_reason          (VI-03, VI-04)
         SET location_id        = location_id                (VI-05)

       all five ACCEPTED once guarded; all owned by slice 5
```

None of those is a transition and all three should be no-ops. This is the
same defect the measurement trigger was corrected for, in three more places,
and it defeats idempotent reuse the same way: a producer that rewrites a row
byte-identically -- exactly what §9.4 calls a no-op -- writes every column,
not only the ones it changed, so it trips a trigger that never looked at the
values. The source-context case was not among the two the review named; it was
found by testing the whole class rather than the reported instances.

The fix is one NULL-safe clause in front of each `WHEN`, and it is `IS NOT`
rather than `<>` for the reason given above -- every column involved is
nullable:

```sql
WHEN (NEW.source_revision_id IS NOT OLD.source_revision_id
   OR NEW.provenance_basis   IS NOT OLD.provenance_basis)
 AND NOT ( ... the transition ... )
```

The guard must not weaken any rejection the trigger already made, which is
the half worth measuring rather than assuming. All eleven real rejections and
the `ON DELETE SET NULL` cascade behave exactly as before (VI-01..VI-05 record
the no-op cases; the rejections they must not weaken are SA-03, SA-04, SA-12,
SA-13, SA-14, DP-12, DP-13, DP-15, DP-16, SA-11 and the cascade DP-17).

##### The supersession fields have a lifecycle, and it is one-way

The complementary triggers of the previous section check a predecessor *being*
superseded and predecessors *waiting for* a successor. Neither looks at a row
inserted already carrying `superseded_by_id`, and nothing prevented an existing
chain being rewritten. Reproduced against the documented SQL:

```text
INSERT a row already pointing at an existing successor,
       with a different archive and revision            ACCEPTED   WRONG
redirect an existing successor 2 -> 3                   ACCEPTED   WRONG
un-supersede a historical row                           ACCEPTED   WRONG
```

Two more triggers close the lifecycle:

```sql
CREATE TRIGGER trg_archive_inspections_inserts_start_active
BEFORE INSERT ON archive_inspections
FOR EACH ROW
WHEN NEW.superseded_at IS NOT NULL
  OR NEW.superseded_by_id IS NOT NULL
  OR NEW.superseded_reason IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'evidence rows are inserted active');
END;

CREATE TRIGGER trg_archive_inspections_supersession_is_terminal
BEFORE UPDATE OF superseded_at, superseded_by_id, superseded_reason
    ON archive_inspections
FOR EACH ROW
WHEN (NEW.superseded_at     IS NOT OLD.superseded_at
   OR NEW.superseded_by_id  IS NOT OLD.superseded_by_id
   OR NEW.superseded_reason IS NOT OLD.superseded_reason)
 AND NOT (OLD.superseded_at IS NULL
      AND NEW.superseded_at IS NOT NULL
      AND NEW.superseded_by_id IS NOT NULL
      AND NEW.superseded_reason IS NOT NULL)
BEGIN
    SELECT RAISE(ABORT, 'supersession is a single terminal transition; it cannot be cleared, redirected or re-explained');
END;
```

Together: a row is born active, becomes superseded exactly once in a single
atomic transition that sets all three fields, and is then historical and
frozen. A partial transition is refused too — setting `superseded_at` and
`superseded_by_id` without a reason does not pass.

Note the message is one string literal. SQLite has no implicit string
concatenation, so a `RAISE(ABORT, 'a' 'b')` split across lines is a syntax
error at `CREATE TRIGGER` time — found by running this schema rather than by
reading it.

##### Verified, not asserted

The corrected set was executed in memory against every case above:

```text
SA-01  valid deferred replacement             ACCEPTED  s5
SA-02  unresolved -> bound inspection         ACCEPTED  s5
SA-03  rebind an already-bound row            REJECTED  s5
SA-04  unbind a bound row                     REJECTED  s5
SA-05  bind a superseded row                  REJECTED  s5
SA-06  bind a row a predecessor points at     REJECTED  s5
SA-07  result payload UPDATE                  REJECTED  s5
SA-08  page_count UPDATE                      REJECTED  s5
SA-09  identity UPDATE (inspector_version)    REJECTED  s5
SA-10  INSERT already superseded, mismatched  REJECTED  s5
SA-11  redirect an existing successor         REJECTED  s5
SA-12  un-supersede a historical row          REJECTED  s5
SA-13  re-explain a historical row            REJECTED  s5
SA-14  partial supersession (no reason)       REJECTED  s5

14 / 14 behaved as specified (SA-01 .. SA-14)
```

And the disposition set, against the real 28-column `archive_inspections`
shape. An earlier draft cited this table from the slice gates without ever
including it:

```text
completeness         28 columns, 28 assigned, 0 missing, 0 unassigned

DP-01  status rewrite                                   REJECTED  s4
DP-02  inspected_path rewrite                           REJECTED  s4
DP-03  archive_format rewrite                           REJECTED  s4
DP-04  encrypted rewrite                                REJECTED  s4
DP-05  comic_info_present rewrite                       REJECTED  s4
DP-06  comic_info_error rewrite                         REJECTED  s4
DP-07  created_at rewrite                               REJECTED  s4
DP-08  updated_at rewrite (legitimately mutable)        ACCEPTED  s4
DP-09  byte-identical rewrite of the whole result       ACCEPTED  s4
DP-10  one differing column among identical ones        REJECTED  s4
DP-11  INSERT with basis 'measured'                     REJECTED  s5
DP-12  unresolved -> measured                           REJECTED  s5
DP-13  unresolved -> single_revision_inherited          REJECTED  s5
DP-14  unresolved_no_identity -> stat_matched_revision  ACCEPTED  s5
DP-15  repoint location_id                              REJECTED  s5
DP-16  direct clear while the location exists           REJECTED  s5
DP-17  genuine ON DELETE SET NULL cascade               ACCEPTED  s5

17 / 17 cases behaved as specified (DP-01 .. DP-17), plus the
completeness assertion above, which is a check rather than a case
```

Together with §9.2, §9.3, §9.3.1, §9.6.1 and §9.6.2, every piece of SQL this
document specifies has now been executed. Two earlier totals here -- 54, then
63 -- were both reached by summing section subtotals, which cannot detect a
case counted in two of them, so cases carry stable identifiers and are counted
by identity.

```text
group                            ids                        registered
§9.4.2  supersession/attribution  SA-01 .. SA-14                    14
§9.4.2  column dispositions       DP-01 .. DP-17                    17
§9.4.2  immutability source       IT-01 .. IT-03                     3
§9.4.2  value-identical rewrites  VI-01 .. VI-05                     5
§9.3.1  inspection indexes        II-01 .. II-10                    10
§9.6.1  candidate triggers        ND-01 .. ND-16                    16
§9.6.2  candidate indexes         CI-01 .. CI-09                     9
§9.2    basis vocabulary          VB-01 .. VB-07                     7
§9.3    archive_hashes            AH-01                              1
                                                                  ----
registered executions                                               82
duplicate registrations (below)                                     -3
                                                                  ----
UNIQUE EXECUTIONS                                                   79
```

Three registered cases assert a behaviour another case already asserts. Two
cases are the same execution when the initial state, the mutating operation
and the expected outcome all match; which trigger subset the harness had
installed is a property of the proof, not of the behaviour, so it does not
make a second case:

```text
bind-unresolved-inspection-correct-basis   SA-02  DP-14  IT-02
change-inspector_version                   SA-09  IT-03
```

The three questions an earlier review posed, answered explicitly rather than
left to the arithmetic:

**The three source-context cases stay inside the disposition block.** They are
DP-15, DP-16 and DP-17, counted once, there. The "22 index and source-context
cases" phrasing that added them a second time is retired.

**The two unresolved-to-bound inspection cases are the same execution.** SA-02
and DP-14 set up an unresolved row, bind it with the correct basis, and expect
ACCEPTED. So does IT-02. All three are one behaviour, counted once.

**`VB-07` belongs to slice 4, not slice 6.** An earlier draft assigned it to
slice 6 as "the per-side attribution transition". It is not one: §9.2's harness
installs the paired CHECK and no triggers, so `VB-07` proves the CHECK admits
the *state* a bound side reaches, not that any trigger permits the move. The
transition itself is `ND-01` and `ND-02`, which are slice 6. All seven `VB`
cases test the CHECK and land with it in slice 4. The misreading was possible
because the displayed block carried no identifiers and its fifth line was the
one labelled `VB-07`; every block now names its case on the line, which is
what makes the identifiers reproducible rather than merely stable.

**A twelfth "new" case did not exist.** The round that added VB and AH
registered 11, not 12: the twelfth was a derived observation -- that an
unresolved `archive_hashes` partial index can hold zero rows -- which follows
from AH-01 rather than being executed, and is recorded in §9.3 as an
observation.

Two executed assertions are counted in neither total: the
disposition-completeness check in the block above (28 columns, 28 assigned)
and the zero-row observation in §9.3.

##### Cases belong to the slice that builds what they test

An earlier draft made slice 5's gate "all cases reproduce". That gate was
impossible: it included CI-01..CI-09, which exercise the candidate partial
indexes, and slice 5 explicitly excludes `near_duplicate_candidates` — those
indexes do not exist until slice 6. A gate cannot require proof of a mechanism
its own slice does not build.

Each case is therefore owned by the slice that introduces the mechanism it
exercises, and each slice's gate requires its own cases plus everything
already proven:

```text
slice  own  cumulative  cases
    4   17          17  DP-01..DP-10, the measurement-immutability set,
                        which §11 lands in slice 4; VB-01..VB-07, the
                        paired basis CHECK, which arrives with the column
    5   37          54  SA-01..SA-14, VI-01..VI-05, DP-11..DP-17, IT-01,
                        II-01..II-10, AH-01 -- the inspections trigger
                        set, the inspection indexes, the NOT NULL rebuild
                        [SUPERSEDED 2026-09-01 -- see §0: slice 5 still
                        rebuilds for uniqueness and supersession, but the
                        basis NOT NULL is established in slice 4. The case
                        allocation itself is unchanged: these 37 cases
                        exercise triggers and indexes, not nullability]
    6   25          79  ND-01..ND-16 and CI-01..CI-09 -- the candidate
                        trigger set and the candidate indexes, neither of
                        which exists before this slice
```

"All 79 by the end of slice 6" survives as an aggregate gate. What does not
survive is asking any earlier slice to prove a mechanism it has not built.

The "same revision, same identity" rule is what makes the model coherent: a
superseding row is a *replacement measurement of the same bytes by the same
method*, not a later measurement of anything else. Anything else is independent
active evidence.

### 9.5 Page inventories take a batch parent, and its design gates the schema

Per-page supersession is very likely the wrong granularity for `archive_pages`.
A re-extraction replaces an entire inventory, not individual pages; page counts
can differ between generations, so there is no page-by-page mapping to point
`superseded_by_id` at; and marking 2,955,391 rows individually makes an
all-or-nothing event look partial if it is interrupted.

The shape that follows is a `page_inventory` parent — one row per (archive,
revision, extraction), carrying the supersession state, with `archive_pages` as
its children. That is a larger structural change than the rest of slice 5 and
touches the biggest table in the database.

**Decided by the lead: this direction is accepted, and its design is a
required pre-schema gate.** It is not a conditional slice after the migration.

The reason is the cost of getting the order wrong. The inventory parent
determines where page ownership, idempotency and supersession live — if it
lands after a migration has already added `source_revision_id` to
`archive_pages`, the 2,955,391-row table is rebuilt twice. Deciding first costs
a design round; deciding second costs the largest table in the database twice
over.

It therefore becomes **slice 2**, ahead of the backfill planner as well as
the schema, and everything renumbers behind it. §11.3 explains why the planner
cannot precede it: the planner freezes the backfill unit, and this decision is
what the unit *is*.

### 9.6 `near_duplicate_candidates`: parameters must be in the key

Splitting `match_method` into `match_algorithm` + `match_algorithm_version` is
necessary but not sufficient. Two runs of `ordered_perceptual v1` with different
distance thresholds produce different candidate sets, and a unique key over
algorithm and version alone would collide them — the second run would overwrite
or conflict with the first, and neither row would record which thresholds
produced it.

Two ways to close it, and the recommendation is the first:

1. **Canonical parameters identity.** Store `parameters_json` plus a
   `parameters_digest` over its canonical rendering, and include the digest in
   the active unique key. Parameters then participate in identity without
   anyone having to remember to bump a version.
2. **Version-bump discipline.** Require every parameter change to bump
   `match_algorithm_version`. Cheaper, but it relies on discipline, and the
   failure mode is silent collision — exactly the class of error the roadmap's
   "algorithm versions cannot be silently mixed" criterion is aimed at.

Putting `parameters_digest` in the key reopens the NULL hole it was added to
close: the 3,000 historical rows have no recorded parameters, so a NULL digest
would make every one of them distinct from every other and permit duplicates
under the very key meant to prevent them. **Four** partial indexes, split on
whether parameters are known:

```text
bound, parameters known
    (revision_a_id, revision_b_id, match_algorithm,
     match_algorithm_version, parameters_digest)
    WHERE bound AND active AND parameters_digest IS NOT NULL

bound, parameters unknown
    (revision_a_id, revision_b_id, match_algorithm, match_algorithm_version)
    WHERE bound AND active AND parameters_digest IS NULL

unresolved, parameters known
    (archive_a_id, archive_b_id, match_algorithm,
     match_algorithm_version, parameters_digest)
    WHERE unresolved AND active AND parameters_digest IS NOT NULL

unresolved, parameters unknown
    (archive_a_id, archive_b_id, match_algorithm, match_algorithm_version)
    WHERE unresolved AND active AND parameters_digest IS NULL
```

The unknown-parameter keys are deliberately narrower, which makes them
*stricter*: two legacy rows for the same pair and version collide and one must
be resolved, rather than both surviving because their NULLs differ.

**`bound` and `unresolved` need defining for a two-sided row**, because a
candidate can be bound on one side and not the other:

```text
bound       := revision_a_id IS NOT NULL AND revision_b_id IS NOT NULL
unresolved  := revision_a_id IS NULL  OR  revision_b_id IS NULL
```

A mixed row is therefore *unresolved* and falls under the archive-keyed index,
which is the conservative reading: a comparison is only as well-attributed as
its weaker side, and keying a half-bound row by one revision would imply an
attribution the row does not have. The two definitions are exhaustive and
disjoint, so the four indexes cover every row exactly once.

**A NULL `parameters_json` needs its own status.** The per-side ownership bases
say nothing about parameters — a row can be fully bound on both sides and still
have no recorded thresholds, which is precisely the state of all 3,000
historical rows. So parameters carry a basis of their own:

```text
parameters_basis TEXT NOT NULL
    CHECK (parameters_basis IN ('known', 'unknown_legacy'))
CHECK (
    (parameters_basis = 'known'          AND parameters_json IS NOT NULL
                                         AND parameters_digest IS NOT NULL)
 OR (parameters_basis = 'unknown_legacy' AND parameters_json IS NULL
                                         AND parameters_digest IS NULL)
)
```

The paired CHECK does the same job as the ownership one: it makes "we know the
parameters" and "we recorded them" the same statement, so a NULL cannot pass as
an unstated known value. All 3,000 existing rows are `unknown_legacy`; every
row a producer writes after slice 6 is `known`.

**`parameters_basis` cannot arrive before the fields it describes.** An
earlier draft added and backfilled it in slice 4 while `parameters_json`,
`parameters_digest` and the producer requirement waited for slice 6. That
leaves an interval in which a candidate written by the detector has nowhere
honest to go: labelled `unknown_legacy` it is a fresh row calling itself
historical; labelled `known` it asserts parameters the schema has no column to
hold; and refusing the insert stops candidate production for two slices. The
paired CHECK above makes the second impossible, so the interval could only
resolve as the first or the third -- a false label or an outage.

The column therefore lands in **slice 6**, with `parameters_json` and
`parameters_digest`, and the same migration that backfills the 3,000 rows as
`unknown_legacy` requires `known` from every new row. Basis and fields arrive
together, which is what makes the pairing enforceable from its first moment.

The same reasoning fixes the analogous gap for inspections, in the other
direction. `inspector_version` and `inspector_version_basis` do land in slice
4, because §11.1 needs them in that slice's binding digest, so the producer
requirement moves *forward* to meet them: inspection producers write
`known` from slice 4, in the same migration that adds the column. Slice 5 then
only makes structural what producers already do. Without that, every
inspection written between slices 4 and 5 would be stamped `unknown_legacy` --
a brand-new row asserting nobody knows which code produced it, which is
exactly the false claim §6.5 refuses to make about the historical 59,541.

The considered alternative was a non-NULL sentinel digest for legacy rows
(`'unknown-legacy'` under a CHECK). It needs one index instead of four, but it
asserts that all 3,000 rows shared one parameter set, which is not known to be
true. The partial-index form declines to assert it.

#### 9.6.1 The trigger set, which slice 5 cannot carry

Slice 5 excludes `near_duplicate_candidates`, so every trigger the other
receiving tables gain in that slice arrives here instead. An earlier draft
named only this table's columns, run provenance and indexes for slice 6 and
left the trigger set unstated, which would have shipped a table with partial
uniqueness and no attribution, supersession or successor-identity enforcement
at all. `CI-01..CI-09` prove the indexes; they prove nothing about triggers.

The set mirrors §9.4.2 with one structural difference: identity is pairwise,
so the attribution transition is **per side** — two triggers, not one — and
the successor-identity comparison spans both revisions, both archives, the
algorithm, the version and the parameters digest (the tuple §9.4.2 names for
this table). Every comparison is `IS NOT`, and every transition trigger opens
with the value-change guard of §9.4.2, because a pairwise row is exactly as
liable to a byte-identical rewrite as a single-sided one.

```text
ND-01  side A binds unresolved -> inherited            ACCEPTED  s6
ND-02  side B binds unresolved -> inherited            ACCEPTED  s6
ND-03  rebind an already-bound side A                  REJECTED  s6
ND-04  unbind a bound side A                           REJECTED  s6
ND-05  side A binds to a basis it may not carry        REJECTED  s6
ND-06  INSERT already carrying a supersession pointer  REJECTED  s6
ND-07  valid deferred pairwise replacement             ACCEPTED  s6
ND-08  un-supersede a historical candidate             REJECTED  s6
ND-09  redirect an existing successor                  REJECTED  s6
ND-10  partial supersession (no reason)                REJECTED  s6
ND-11  successor naming a different revision_b         REJECTED  s6
ND-12  successor with a different parameters_digest    REJECTED  s6
ND-13  successor with a different algorithm version    REJECTED  s6
ND-14  attribution rewrite, value-identical            ACCEPTED  s6
ND-15  supersession rewrite, value-identical           ACCEPTED  s6
ND-16  both sides bind in one statement                ACCEPTED  s6
```

`ND-05` is the pairwise analogue of `DP-13`: a side may not transition to
`single_revision_inherited`, which the backfill writes and no producer may.
`ND-16` binds both sides in **one** `UPDATE`. That is not `ND-01` and
`ND-02` run in sequence: it fires both `UPDATE OF` triggers within a single
SQLite statement, each seeing the same `OLD` row while `NEW` already carries
the other side's change. Whether that combination is accepted is a property of
statement-level trigger evaluation rather than of either trigger alone, so it
is measured rather than deduced from the two single-side cases. It is what
makes the two-trigger design usable by a producer that learns both revisions
at once.

`ND-11..ND-13` are the cases a single-sided successor check would miss — a
successor agreeing on side A and differing on side B, on the parameters
digest, or on the version — and they are why the tuple is spelled out per
table rather than templated.

#### 9.6.2 The executable index form, and what it was measured to do

```sql
CREATE UNIQUE INDEX ui_nd_bound_known
    ON near_duplicate_candidates(revision_a_id, revision_b_id,
                                 match_algorithm, match_algorithm_version,
                                 parameters_digest)
 WHERE revision_a_id IS NOT NULL AND revision_b_id IS NOT NULL
   AND parameters_digest IS NOT NULL AND superseded_at IS NULL;

CREATE UNIQUE INDEX ui_nd_bound_unknown
    ON near_duplicate_candidates(revision_a_id, revision_b_id,
                                 match_algorithm, match_algorithm_version)
 WHERE revision_a_id IS NOT NULL AND revision_b_id IS NOT NULL
   AND parameters_digest IS NULL AND superseded_at IS NULL;

CREATE UNIQUE INDEX ui_nd_unresolved_known
    ON near_duplicate_candidates(archive_a_id, archive_b_id,
                                 match_algorithm, match_algorithm_version,
                                 parameters_digest)
 WHERE (revision_a_id IS NULL OR revision_b_id IS NULL)
   AND parameters_digest IS NOT NULL AND superseded_at IS NULL;

CREATE UNIQUE INDEX ui_nd_unresolved_unknown
    ON near_duplicate_candidates(archive_a_id, archive_b_id,
                                 match_algorithm, match_algorithm_version)
 WHERE (revision_a_id IS NULL OR revision_b_id IS NULL)
   AND parameters_digest IS NULL AND superseded_at IS NULL;
```

The `OR` in the unresolved predicates is what implements §9.6's definition
of `unresolved` as *either* side unbound. SQLite accepts a disjunction in a
partial-index predicate; measured, because the documentation permits only
deterministic expressions over the table's own columns and it was not obvious
that this qualifies.

```text
CI-01  two bound rows, same revisions + same parameters       REJECTED  s6
CI-02  two bound rows, same revisions, different parameters   ACCEPTED  s6
CI-03  two legacy rows, same pair, both parameters NULL       REJECTED  s6
CI-04  legacy (NULL params) and new (known params) coexist    ACCEPTED  s6
CI-05  two mixed-side rows for the same archive pair collide  REJECTED  s6
CI-06  mixed-side and fully bound coexist                     ACCEPTED  s6
CI-07  two fully unresolved rows, same pair, same parameters  REJECTED  s6
CI-08  same identity, one superseded -> coexist               ACCEPTED  s6
CI-09  different algorithm version -> coexist                 ACCEPTED  s6
```

The mixed-side results are the ones that justify §9.6's definition. Two
rows for one archive pair, one bound on side A and the other on side B, collide
under the archive-keyed index — which is right, because neither is
better-attributed than the other and keeping both would leave two current
answers for the same comparison. A mixed row and a fully bound row coexist,
because the fully bound one is genuinely more specific.

The existing 3,000 rows are backfilled with the algorithm and version split out
of the method string and a NULL `parameters_json` explained by a basis, not
invented.

### 9.7 `archive_hashes` after Step 4

`archive_hashes` holds one mutable row per archive whose digest is, by
construction, the revision's digest, plus the stat of the read that produced
it — and that stat belongs in `archive_revision_observations`, which currently
holds zero rows.

**Decided by the lead: `archive_hashes` is retained throughout Step 4.** It is
now the binding anchor other producers reach through — §8.2 has page
hashing and inspection resolve their revision via its rows — so retiring it
would remove the only immutable identity source those consumers have.

Retirement is not to be reconsidered until those consumers have another such
source, which in practice means the digest-carrying producer change described
at the end of §8.3. It gains an ownership key with the other four tables and is
otherwise left alone.

### 9.8 Reconciliation invariants a backfill must satisfy

```text
every evidence row has a provenance_basis        (gate in slice 4, CHECK in 5)
bound + unresolved = total rows, per table                  (reconciliation)
no bound row names a revision of a different archive        (composite FK)
identity-seed bindings match 014's own join, row for row    (recomputable)
pre-existing row counts unchanged in every touched table
all current hash and signature values byte-identical after
schema_migrations grows by exactly one per migration
the protected pre-migration backup is verified before and after
```

---

## 10. Consumers affected — and what the planner actually gains

The retention planner's four `archive_proxy` rules do **not** all become
revision-granular.

| planner rule | backing evidence | after Step 4 |
| --- | --- | --- |
| `active_or_recoverable_job` | `jobs` | **permanently proxy** — jobs are identity-scoped by design (§8.6) and gain no key |
| `unresolved_failure` | `jobs` | **permanently proxy**, same reason |
| `open_review_work` | `near_duplicate_candidates` | **proxy throughout Step 4** — binding a side improves attribution but not granularity, because every basis a candidate side may carry resolves to proxy (below) |
| `quarantine_or_resolution` | `archive_quarantine` **and** `archive_disposition_events` | **must be split**, and both halves stay proxy — §5.1 leaves quarantine identity-scoped, so neither half can ever be revision-granular |

`quarantine_or_resolution` folds two different things into one reason: a
quarantine row and a disposition event. They should still be split into
`quarantine` and `disposition_history` so each rule has one meaning — but note
that after §5.1 *neither* becomes revision-granular, because quarantine keeps
`archive_id` alone. The split is for clarity, not for capability.

An earlier draft said `open_review_work` becomes revision-granular for bound
sides once slice 6 lands. It does not, and the contradiction was internal to
this document: §9.4.2 permits a candidate side only
`inherited_from_page_evidence`, `single_revision_inherited` or
`unresolved_no_identity`, and none of the three is on the revision-granular
list below. A bound side is therefore better *attributed* — it names a
revision — without being better *evidence*, which is exactly the distinction
§7.4 draws when it refuses to let a candidate copy its upstream's basis.

Three bases need naming in that resolution, and all three resolve to **proxy**:

- **`migration_014_field_seed`** (§4.1) is a causal record of what 014 copied,
  not a statement that the row describes the revision's bytes.
- **`stat_matched_revision`** (§8.3) is a size-and-mtime agreement, not a
  digest match. It becomes eligible for revision granularity only when a
  producer carries the archive digest into the binding.
- **`inherited_from_page_evidence`** (§7.4) is a revision taken from page
  evidence the candidate producer read, having read no archive bytes itself.
  It is proxy unconditionally, and never stronger than the upstream it
  inherits from — including after that upstream is promoted, since the
  candidate still measured nothing.

Only `measured`, `migration_014_identity_seed` and a future digest-carrying
binding may resolve to revision granularity.

**Consequence for the planner slice, ruled on by the lead.** With
`open_review_work` proxy as well, all four `archive_proxy` rules are proxy for
the whole of Step 4, so no rule's actual granularity can differ from its
maximum and per-row resolution cannot move a single row. An earlier draft kept
both the `RULE_MAX_GRANULARITY` rename and the per-row resolution as a
standalone planner slice on that basis, which would have been a roadmap gate
that changes no output. The lead's ruling splits them:

- **The rename rides the reason split.** `RULE_GRANULARITY` is keyed by rule
  name, so splitting `quarantine_or_resolution` into `quarantine` and
  `disposition_history` already rewrites its keys. Renaming the constant in
  that same edit costs nothing extra and removes the misreading the name
  invites — that granularity is a property of the rule. It is terminology
  carried by a change that has its own justification, not a gate of its own.
- **Per-row resolution is deferred** until a basis exists that resolves to
  revision granularity (§12.2). Building the machinery now would be building
  for an anticipated need, and it cannot be tested against a row that
  exercises it.

The row-level "weakest wins" rule in `_evidence_granularity` is unchanged and
already correct either way.

Also affected: `perceptual_coverage_audit` (per-archive denominators;
per-revision changes what "covered" means and needs its own decision),
`content_duplicate_audit` and `near_duplicate` (comparisons become
revision-pair-scoped), `source_drift_recovery` (the producer that would
*resolve* the 16, and whose behaviour under revision-awareness must be specified
before they can move out of `unresolved_drift`). `classification.py`,
disposition, retirement and supersession key on identity and are unaffected —
and must stay that way.

---

## 11. Recommended PR slices

| # | slice | gate |
| --- | --- | --- |
| **1** | *this document* | lead accepts the matrix, the census, the requirement coverage, the producer paths and the invariants |
| **2** | **page-inventory design** (§9.5). Design only, no schema, no planner. Decides whether page ownership lives on 2,955,391 `archive_pages` rows or on ~58,432 inventory parents, and where page idempotency and supersession live | lead accepts the parent's shape, the **authoritative backfill unit**, and the migration path. **This gates the planner as well as the schema** — see §11.3 |
| **3** | read-only **backfill planner**: classify every evidence row into the bases of §7.1, with a snapshot digest, deterministic JSON/CSV, and totals reconciling per table | counts reproduce §7.2 for whatever unit slice 2 settled; `measured` and `stat_matched_revision` are both 0; identity seed 59,541, field seed 58,421; the 16 drift signatures and their page evidence are `unresolved_drift`; the 147 provisional archives reported as an archive-level gate; quarantine appears in no table; every inspection is planned as `inspector_version_basis = 'unknown_legacy'` with a NULL version, with the near-duplicate parameter classification planned but **not** applied until slice 6, so it contributes to the plan digest without implying a slice-4 write |
| **4** | migration: ownership keys + `provenance_basis` **NOT NULL** on the receiving tables (**[SUPERSEDED 2026-09-01 — see §0]** this row read "**nullable** `provenance_basis`"; slice 4 rebuilds all four tables for the composite key, so the column is created NOT NULL there, and `archive_hashes.source_revision_id` with it), plus `inspector_version` / `inspector_version_basis` (§6.5), backfilling exactly what slice 3 planned. **`parameters_basis` is not in this slice** — it lands in slice 6 with the parameter fields that give it meaning (§9.6). **Inspection producers begin writing `inspector_version_basis = 'known'` here, in the same slice that adds the column**, so no row written after the migration is labelled legacy. **Uniqueness unchanged.** Producers write a basis on the path they already take. **The measurement-immutability triggers of §9.4.2 land here, not in slice 5** — they are what makes the interim window safe (§11.4) | protected backup verified first; row counts unchanged; all hash and signature values byte-identical; every row has a non-NULL basis at the gate (**[SUPERSEDED 2026-09-01 — see §0]** this read "even though the column permits NULL"; it no longer does, so the gate now confirms a structural guarantee rather than compensating for its absence — which is why it is kept rather than dropped: a constraint nobody tests is a constraint nobody knows is there); planned-vs-applied reconciliation of §11.1 passes; **a rerun that would change any measurement value fails, on bound and unresolved rows alike, while a byte-identical rerun passes**; slice 4's own 17 cases reproduce (DP-01..DP-10, VB-01..VB-07); recovery is restore-from-backup |
| **5** | uniqueness → the partial indexes of §9.3 (bound **and** unresolved), producers switch UPSERT → append, the remainder of §9.4.2's trigger set — the attribution transition, the supersession lifecycle, and both complementary identity triggers — ~~`provenance_basis` becomes `NOT NULL` via the table rebuild, interim guard removed~~ (**[SUPERSEDED 2026-09-01 — see §0]** established in slice 4; slice 5 still rebuilds for uniqueness and supersession, and there is no interim guard to remove because there is no nullable window), and the `inspector_version_basis = 'known'` requirement producers already follow since slice 4 becomes structurally enforced. Page tables adopt whatever slice 2 decided. **Excludes `near_duplicate_candidates`** | idempotency re-established on the new keys and proven by bypass, for bound *and* unresolved rows on every table that has both — `archive_hashes` is bound-only (§9.3) and is proven on its single branch; a second generation's evidence demonstrably coexists with the first; a byte-identical rerun is a no-op, a differing rerun requires a reason, a new revision supersedes nothing; the id-ordering CHECK makes a cycle unconstructible; slice 5's own 37 cases reproduce and the 17 from slice 4 still do, 54 cumulative, by the identifiers and the ownership table in §9.4.2 -- every candidate case, index and trigger alike, belongs to slice 6 and is not required here, each guard proven by disabling it alone — including the three the earlier design let through: an INSERT already carrying a supersession pointer, un-supersession, and successor rewiring; and the one it wrongly refused, an unresolved inspection binding to a revision |
| **6** | `near_duplicate_candidates`: split `match_method` into algorithm + version, add `parameters_json` + `parameters_digest`, add `parameters_basis` and backfill all 3,000 historical rows as `unknown_legacy`, **requiring `known` for every new row from the same migration**, **begin writing `processing_runs`** and carry `processing_run_id`, the four partial indexes of §9.6.2, **and the full candidate trigger set of §9.6.1** — per-side attribution transitions, the supersession lifecycle, and the pairwise successor-identity pair, all carrying the value-change guard — ~~plus the `NOT NULL` rebuild of both per-side bases, which slice 5 could not perform on this table~~ (**[SUPERSEDED 2026-09-01 — see §0]** both per-side bases are created NOT NULL by slice 4's rebuild of this table; slice 6 still rebuilds it for parameters, indexes and triggers) | no v1 row reinterpreted; a v2 row can coexist; two parameter sets at one version cannot collide; two legacy rows with unknown parameters cannot both survive; new rows carry a run; slice 6's own 25 cases reproduce (ND-01..ND-16, CI-01..CI-09) and the full 79 hold cumulatively, which is the aggregate gate §9.4.2 defines |
| **7** | planner: split `quarantine_or_resolution` into `quarantine` and `disposition_history`, renaming `RULE_GRANULARITY` to `RULE_MAX_GRANULARITY` in the same edit because the split rewrites its keys anyway (§10). **Per-row granularity resolution is not in this slice** — it is deferred until a revision-granular basis exists (§12.2), so this slice changes the reason census and nothing else | see §11.2 |

### 11.1 The migration gate, respecified

An earlier draft required the post-migration planner output to reproduce the
pre-migration snapshot digest. That is not achievable and should not be asked
for: `source_revision_id` and `provenance_basis` are decision-bearing inputs, so
adding them *must* change any honest snapshot digest. A gate that demands
otherwise can only be satisfied by a digest that ignores the change.

Two digests instead:

```text
plan digest      computed by slice 3 over pre-migration inputs. This is the
                 artifact the lead approves, and it is recomputed and compared
                 immediately before the migration acts. A change means the
                 database moved under the review.

binding digest   computed after the migration over (table, row id, and
                 EVERY column that migration wrote) for every row touched.
                 Named per table rather than fixed, because slice 4 writes
                 more than two columns:

                     archive_hashes
                         source_revision_id, provenance_basis

                     archive_content_signatures
                         source_revision_id, provenance_basis

                     archive_pages / page inventory
                         source_revision_id, provenance_basis

                     archive_inspections
                         source_revision_id, provenance_basis,
                         inspector_version, inspector_version_basis

                     near_duplicate_candidates
                         revision_a_id, revision_b_id,
                         provenance_basis_a, provenance_basis_b

                 parameters_basis is absent because slice 4 no longer
                 writes it; it enters slice 6's own binding digest with
                 parameters_json and parameters_digest (§9.6).
```

An earlier draft fixed this tuple at `(table, row id, source_revision_id,
provenance_basis)` while also claiming the inspector fields entered it. They
could not: the columns were introduced in slice 5, and slice 4's digest had no
place for them. Resolved by moving the inspector columns into **slice 4** with
the rest of the backfill and widening the digest per table, rather than
inventing a second applied-state digest for slice 5 to carry.

The gate is a reconciliation between them, not equality of either:

```text
every binding in the plan was applied exactly once
no binding exists that the plan did not contain
per-table totals match the plan's totals
rows the plan marked unresolved carry NULL and the planned reason
```

### 11.2 The planner gate for slice 7, respecified

Slice 7 renames reasons — `quarantine_or_resolution` becomes `quarantine` and
`disposition_history` — so it **cannot** reconcile byte-for-byte against slice
3's output, and the plan digest is expected to change. Requiring otherwise would
be requiring the split not to have happened.

What must be unchanged:

```text
every revision's policy_classification          identical
the set of protected revisions                  identical
the set of candidates                           identical (0 on production)
unexplained                                     identical (0 on production)
gate_failures                                   identical (none on production)
evidence_granularity per row                    identical -- every rule is
                                                proxy for all of Step 4 (§10),
                                                and per-row resolution is not
                                                in this slice at all
```

What is expected to change, and must be reported rather than hidden:

```text
the reason census                 quarantine_or_resolution splits into two
the plan digest                   reason names are decision-bearing inputs
```

The verdict must not move. The explanation may, and the diff of explanations is
the reviewable artifact.

---

### 11.3 Why the page-inventory design gates the planner, not just the schema

An earlier draft placed the inventory design after the planner, on the reasoning
that only a migration can rebuild a table. That was wrong, and the reason is
worth stating because it is easy to make again.

The planner does not merely count rows; it **freezes the backfill unit**. It
emits one planned binding per receiving row, digests them, and slice 4's gate
proves every planned binding was applied exactly once. If slice 2 decides
ownership lives on inventory parents rather than page rows, then:

```text
receiving rows for page evidence   2,955,391  ->  ~58,432
receiving table                    archive_pages -> page_inventory
row ids in the plan                every one of them different
per-table totals                   different
plan digest                        different
planned-to-applied reconciliation  reconciles against the wrong unit
```

None of that is repairable by re-running the planner after the fact, because
the plan is the artifact the lead approved. **Every total in this document that
includes page evidence is therefore provisional until slice 2 lands**, including
the 3,135,910 in §7.2 — see the two candidate figures recorded there.

### 11.4 What the interim window after slice 4 actually guarantees

An earlier draft said the window was kept safe by a guard that "refuses to
overwrite a row bound to a different revision". That guard has nothing to
compare on the population it most needs to protect: the 16 drift signatures and
their page evidence are deliberately **unresolved**, so there is no revision to
differ from, and a producer rerun between slices 4 and 5 could still overwrite
that historical measurement under the old per-archive UPSERT.

The measurement-immutability triggers replace it, and they are stronger in
exactly the right way because they compare *values* rather than revisions:

```text
rerun produces byte-identical results   -> UPDATE succeeds, nothing changes
                                           (this is §9.4's idempotent reuse)
rerun produces ANY different value      -> ABORT, whatever the row's binding
row is unresolved                       -> same rule; no revision needed
```

So the honest statement of the interim is: **attribution improves, retention
does not, and no measurement can be lost** — a rerun that would change one
fails the job instead. What is still missing until slice 5 is the ability to
*keep both* generations; until then a changed re-measurement cannot be
recorded at all, which is a refusal rather than a loss.

Slice 5 turns that refusal into an append.

Slices 4, 5 and 6 each touch production and each need the guarded-operation
sequence: dry run, protected backup, expected count plus snapshot digest, report
before act, postflight reconciliation.

## 12. Decisions and deliberate non-decisions

### 12.1 Decisions taken by the lead

- **`archive_hashes` is retained throughout Step 4** (§9.7). It is the binding
  anchor other producers reach through, and retirement is not reconsidered
  until those consumers have another immutable identity source.
- **The `page_inventory` parent direction is accepted, and its design is a
  required gate ahead of both the planner and the schema** (§9.5, §11.3), now
  slice 2. Deciding it after the planner would freeze the wrong backfill unit;
  deciding it after a migration would rebuild the 2,955,391-row page table
  twice.
- **Quarantine stays identity-scoped** (§5.1), reducing the receiving tables
  from six to five and the backfill population from 3,135,945 to 3,135,910. A
  guaranteed-NULL ownership column adds no provenance.

### 12.2 Deliberate non-decisions
- **The 16 drift archives are not remediated here.** Resolving them needs a
  revision for the earlier generation, which belongs to a later, separately
  reviewed migration and remediation design.
- **`jobs` deliberately gets no revision column** (§8.6).
- **`page_hashes` deliberately gets no column of its own** (§5).
- **`processing_run_id` is not backfilled** (§6.3) because `processing_runs` is
  empty — but slice 6 must start writing runs rather than inheriting that gap.
- **Run provenance for the five non-near-duplicate tables is deferred past
  slice 5** (§6.3), because nothing writes a run until slice 6 does. It follows
  slice 6 in a slice this document does not sequence; recorded here so the gap
  is a decision rather than an oversight.
- **`parameters_json` for `page_hashes` is deferred.** Version 1 perceptual
  hashing is frozen and its parameters are pinned by regression vectors, so
  there is nothing a parameters column would disambiguate until a v2 exists.
- **Stat-matched binding is not digest equality** (§8.3), which is why it has
  its own basis and resolves to proxy granularity. Hardening it by carrying the
  archive digest into page hashing is possible and is not proposed here; it is
  also the change that would let `archive_hashes` retirement be reconsidered.
- **Per-row granularity resolution in the planner is deferred** (§10). Every
  `archive_proxy` rule is proxy for the whole of Step 4, so resolving
  granularity per evidence row cannot move a row and could not be tested
  against one that exercises it. It becomes worth building when a basis
  resolves to revision granularity — which today means the digest-carrying
  producer change of §8.3, and nothing sooner. The `RULE_MAX_GRANULARITY`
  rename is *not* deferred with it: it rides slice 7's reason split, which
  rewrites the same constant's keys regardless.
- **No time-based or event-sourced provenance layer** is proposed. Direct
  foreign keys only.
- **The 439 mtime-only drift archives are not treated as changed.** Size agrees
  for all of them; mtime alone is moved by a copy or restore. If a later
  decision treats them as suspect, that is a new decision and this paragraph is
  the record that it was considered and declined here.
