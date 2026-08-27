# Page inventory — design gate

Roadmap Step 4, slice 2. Design only. This document proposes nothing that has
been built: no migration, no schema change, no implementation, and no producer
change. The branch it lands on contains no implementation code.

It exists because slice 1 (`docs/revision_aware_provenance_assessment.md`,
merged at `8e21bbb`) made this a required gate ahead of both the backfill
planner and the schema — §9.5 and §11.3 there — on the grounds that the
planner freezes the backfill unit and this decision *is* what the unit is.

Out of scope by instruction: migration 015, the planner itself, pruning,
production remediation, and the 16 drift archives.

---

## 1. What this gate has to decide

```text
the authoritative backfill unit     what slice 3 emits one planned row per
parent / child schema               where ownership, idempotency, supersession live
idempotency                         what a re-extraction producing the same bytes does
supersession                        what a re-extraction producing different bytes does
page-hash inheritance               how page_hashes reach a revision
migration ordering                  what must land before what, and why
recovery path                       how a failed migration is undone
```

Everything below is measured or executed. Where a claim is neither, it is
marked as a decision with its reasoning, or as a deliberate non-decision.

---

## 2. Provenance

```text
database        G:\ComicAutomation\TestDatabase\inspection-working.db
schema          migrations 1-14
measured        2026-08-26
method          comic_automation.database.read_guards.read_consistent_snapshot
                mode=ro + PRAGMA query_only, one deferred read transaction,
                PRAGMA data_version sampled outside it either side
census runs     2, both read-only
quick_check     ok (both)
data_version    2 -> 2 (both, unchanged)
```

Nothing was written, and no report was placed beside the database. Every
SQLite construct below was executed in memory; production was read, never
written.

---

## 3. The producer already has an inventory — it just has no name

`ArchivePageHashRepository.save` opens `BEGIN IMMEDIATE` and does this before
inserting anything:

```python
# Rehashing replaces the entire page inventory rather than
# trying to diff it: delete-then-reinsert is simpler and
# correct even if pages were added, removed, or reordered.
self.connection.execute(
    "DELETE FROM archive_pages WHERE archive_id = ?", (archive_id,))
```

**The batch is already the producer's unit.** An extraction is atomic in the
code: `save` runs inside one transaction and replaces the whole page set,
never diffing it. The comment even calls it "the page inventory". The parent
table proposed here does not invent a granularity — it names one that already
exists implicitly and gives it a row so ownership, idempotency and
supersession have somewhere to live.

That claim rests on **reading the producer, not on the stored timestamps.** An
earlier draft of this document also offered the created_at measurement of §4.2
as evidence of batch atomicity, which it is not: equal one-second timestamps
are consistent with one transaction but cannot prove it, and unequal ones (197
archives) do not disprove it. The code is the evidence; the timestamps are
only evidence about timestamps.

**Re-extraction currently destroys the prior generation, hashes included.**
`page_hashes` is `ON DELETE CASCADE` from `archive_pages`, so the `DELETE`
above removes the previous generation's page rows *and* every hash computed
over them. Today that is 8,821,073 rows one re-extraction away from deletion,
per archive, with no record that they existed. This is the retention problem
in its sharpest form and it is why the parent is worth the migration.

**The delete-then-reinsert is not a defect.** It is correct for a schema whose
uniqueness is `(archive_id, page_index)` and which can therefore only hold one
generation. The change proposed here is what makes appending possible; until
then, replacing is the honest thing for the producer to do.

---

## 4. Census

### 4.1 Volumes

```text
archives                          59,688
archives with page rows           58,432
archives with a zero-page
    extraction result (§4.5)           5
archive_pages                  2,955,391
page_hashes                    8,821,073
```

Pages per archive, over the 58,432 with page rows:

```text
min      1
p50     27
p90    112
p99    336
max  6,041
avg  50.58
```

The parent is therefore ~58,437 rows against 2,955,391 children — a **50.6x**
reduction in the population that receives an ownership column.

### 4.2 The extraction timestamp, and what it is not

SQLite evaluates `CURRENT_TIMESTAMP` per statement at one-second resolution,
so a large archive can straddle a boundary even inside one transaction:

```text
archives whose pages share ONE created_at      58,235   (99.66%)
archives whose pages span TWO created_at          197   ( 0.34%)
maximum span within one archive               1 second
                                              ------
                                               58,432
```

The batch does not need this to be identified. Because the producer replaces
the whole set, every page row an archive currently has belongs to exactly one
extraction, so for the backfill the batch is simply `archive_id`.

It does decide the parent's own timestamp, and for 197 archives the children
disagree. **Decision: `extracted_at` is `MIN(created_at)` of the children.**

Stated honestly, that is **the first persistence timestamp after extraction
completed** — not "the moment extraction began", which an earlier draft
claimed and no stored value supports. Extraction happens before any row is
written; the earliest child row marks when the results started being written
down, and the true start is not recorded anywhere. `MAX` would be equally
available and means the last such write; the 197 are the population that makes
the two distinguishable, and the choice is recorded rather than taken
silently.

### 4.3 Page hashes are two production events, not one

```text
page_hashes written WITH their page row      2,955,376
page_hashes written AFTER their page row     5,865,697
                                            ----------
                                             8,821,073

by algorithm, written later:
    sha256                                          15
    dhash                                    2,932,841   (all of them)
    phash                                    2,932,841   (all of them)
```

`sha256 v1` is computed during extraction and written in the same transaction
as its page — 2,955,376 of 2,955,391. **Every** `dhash v1` and `phash v1` row
was written later, by the separate perceptual-hashing run.

This is decisive for §7. The inventory parent describes **an extraction**. It
owns the page rows and the sha256 hashes produced with them. It does *not* own
the perceptual hashes, which describe the same bytes but were produced by a
different run at a different time.

Two smaller measurements a parent must be able to express:

```text
pages with sha256 but no phash                  22,550
archives mixing hashed and unhashed pages            0
```

Perceptual hashing is **all-or-nothing per archive**. The 22,550 are whole
archives never perceptually hashed, not partial coverage, so perceptual-hash
state is a property of (archive, run) and needs no per-page bookkeeping in the
parent.

**The 15 `sha256` rows written after their page row are not explained.** They
are 0.0005% of the sha256 population and could be a retry, a resumed batch, or
something else. No artifact establishes which, and this document does not
invent one — recorded as a gap, because a plausible reconstruction could not
afterwards be told from a fact.

### 4.4 Structural facts the design may rely on

```text
archives whose page_index is not a dense 0..n-1 run       0
archives whose pages span more than one location_id       0
archives where the signature's page_count disagrees
    with the stored page rows                             0
```

Dense indexes make a parent's `page_count` checkable against its children,
which §8.3 turns into an enforced invariant. A single location per page set is
why `location_id` moves to the parent and is **removed** from the child
(§6.2).

### 4.5 The five zero-page archives are extractions, not absences

An earlier draft excluded the five archives that have a content signature but
no page rows, on the reasoning that there was "no extraction to describe".
That was wrong, and the producer says so: it digests the page count *before*
the pages, so the empty tuple has a well-defined value.

```python
signature.update(len(pages).to_bytes(8, "big"))   # then each page's digest
```

Computed and compared against what is stored:

```text
canonical empty-tuple digest
    af5570f5a1810b7af78caf4bc70a660f0df51e42baf91d4de5b2328de0e83dfc

archives      909, 10046, 13976, 40535, 55853
digest        all five equal the canonical empty-tuple digest
page_count    0 for all five
image_bytes   0 for all five
inspection    status 'no_images', page_count 0, for all five
revision      page_count 0, identity_state 'established', for all five
archive_hashes present for all five
page_hashes  0 under all five
signatures with page_count = 0 anywhere in the database: exactly these 5
```

Three independent sources agree — the inspector recorded `no_images`, the
revision recorded zero pages, and the signature is the canonical digest of the
empty set. These are **successful extractions that found no pages**, not
extractions that went missing. "No pages" does not prove "no extraction", and
treating it as though it did would have discarded five real results.

**They receive inventories like any other archive**, with `page_count = 0`,
the empty-tuple `content_digest`, and no children. §5's totals are revised
accordingly, and `PI-15` proves a zero-page inventory seals.

---

## 5. The authoritative backfill unit

**Decision: `page_inventory`. One row per (archive, extraction).**

The backfill mints one inventory per archive that has an extraction result —
58,432 with pages plus the 5 zero-page results of §4.5 — and attaches each
existing `archive_pages` row to its archive's inventory. Ownership,
`source_revision_id` and `provenance_basis`, lives on the parent and nowhere
else.

```text
archives with page rows                58,432
zero-page extraction results (§4.5)         5
                                       ------
inventories minted                     58,437

slice 1 §7.2 population, page rows  3,135,910
    less archive_pages             -2,955,391
    plus inventories                  +58,437
                                   ----------
backfill population                   238,956
```

An earlier draft of this document said 58,432 and 238,951, having excluded the
five. **The frozen totals are 58,437 inventories and 238,956 planned
bindings.** This resolves the two candidate figures slice 1 §7.2 left
provisional, at a value neither of them named.

Run provenance is a different population and is unchanged: slice 1 §6.3
records 11,956,983, which counts `page_hashes` because a run is not inherited
(§7).

Why the parent rather than the page row:

- **Re-extraction replaces a set, not rows.** Page counts differ between
  generations, so there is no page-to-page mapping for `superseded_by_id` to
  point at.
- **An interrupted marking would look like a partial extraction.** Marking
  2,955,391 rows individually turns an all-or-nothing event into something a
  reader must reassemble. One parent row is atomic by construction.
- **The 50.6x reduction is not the reason, but it is not nothing.** A
  migration that writes 58,437 rows is a different operational risk from one
  that rewrites 2,955,391.

---

## 6. Schema

Stated as requirements for review, not as a migration.

### 6.1 The parent

```sql
CREATE TABLE page_inventory (
    id                 INTEGER PRIMARY KEY,
    archive_id         INTEGER NOT NULL,
    source_revision_id INTEGER,
    provenance_basis   TEXT NOT NULL
        CHECK (provenance_basis IN ('stat_matched_revision',
                                    'single_revision_inherited',
                                    'unresolved_drift',
                                    'unresolved_no_identity')),
    location_id        INTEGER,
    page_count         INTEGER NOT NULL CHECK (page_count >= 0),
    content_digest     TEXT NOT NULL,
    extracted_at       TEXT NOT NULL,
    sealed_at          TEXT,
    created_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    superseded_at      TEXT,
    superseded_by_id   INTEGER,
    superseded_reason  TEXT,

    UNIQUE (id, archive_id),

    CHECK ((superseded_at IS NULL) = (superseded_by_id IS NULL)),
    CHECK ((superseded_at IS NULL) = (superseded_reason IS NULL)),
    CHECK (superseded_by_id IS NULL OR superseded_by_id > id),
    -- an inventory still being built is not yet evidence of anything, so it
    -- cannot be superseded; supersession relates two completed extractions
    CHECK (superseded_at IS NULL OR sealed_at IS NOT NULL),
    CHECK ((source_revision_id IS NOT NULL
            AND provenance_basis IN ('stat_matched_revision',
                                     'single_revision_inherited'))
        OR (source_revision_id IS NULL
            AND provenance_basis LIKE 'unresolved%')),

    FOREIGN KEY (source_revision_id, archive_id)
        REFERENCES archive_revisions(id, archive_id),
    FOREIGN KEY (archive_id) REFERENCES archive_files(id) ON DELETE CASCADE,
    FOREIGN KEY (location_id) REFERENCES file_locations(id) ON DELETE SET NULL,
    FOREIGN KEY (superseded_by_id) REFERENCES page_inventory(id)
        DEFERRABLE INITIALLY DEFERRED
);
```

`content_digest` is the ordered-page digest over the sealed set, in the
producer's existing construction. §12.1 records why it is here after an
earlier draft declined it.

The vocabulary is the one slice 1 §9.4.2 assigns to `archive_pages / page
inventory`, unchanged. `UNIQUE (id, archive_id)` exists for the children's
composite key, the same device migration 014 added for lineage.

Three partial unique indexes:

```sql
CREATE UNIQUE INDEX ui_page_inventory_bound
    ON page_inventory(source_revision_id)
 WHERE source_revision_id IS NOT NULL AND superseded_at IS NULL;

CREATE UNIQUE INDEX ui_page_inventory_unresolved
    ON page_inventory(archive_id)
 WHERE source_revision_id IS NULL AND superseded_at IS NULL;

CREATE UNIQUE INDEX ui_page_inventory_building
    ON page_inventory(archive_id)
 WHERE sealed_at IS NULL;
```

The first two are slice 1 §9.3's bound/unresolved pair: one active inventory
per bound revision, one per unresolved archive. The third is new and belongs
to the lifecycle of §8.3 — at most one inventory may be under construction for
an archive at a time, so two concurrent extractions cannot interleave children
into each other's set (`PI-06`).

### 6.2 The children

```sql
archive_pages
    + inventory_id INTEGER NOT NULL
    - location_id                              -- moves to the parent
    - UNIQUE (archive_id, page_index)
    + UNIQUE (inventory_id, page_index)
    + FOREIGN KEY (inventory_id, archive_id)
          REFERENCES page_inventory(id, archive_id)     -- NO ACTION
    - source_revision_id / provenance_basis    -- never added; ownership is
                                                  the parent's, not the row's
```

**`location_id` is removed from the child.** §4.4 measured that no archive's
pages span more than one location, so the column is the same value repeated
2,955,391 times, and two copies of one fact can disagree. The alternative —
keeping it and enforcing equality with the parent by trigger — buys nothing
the parent does not already record. Removing it is why the child rebuild of
§10 has to happen anyway, and it costs nothing extra once it does.

**`archive_id` stays** on the child. It is also redundant with the parent, and
that redundancy is deliberate: it is the second column of the composite
foreign key, which is what prevents a page being attached to an inventory
belonging to a different archive (`PI-24`). Removing it would remove the
constraint. This is the difference between a redundant column that *checks*
something and one that merely repeats it.

`NO ACTION`, never `CASCADE`, on the inventory key. A superseded generation's
pages are the retention this whole design exists to create; a cascade would
delete exactly what is being kept (`PI-27`).

`page_hashes` is **unchanged**. It keeps `UNIQUE (page_id, algorithm,
algorithm_version)` — already the shape the roadmap asks for — and keeps its
`ON DELETE CASCADE` from `archive_pages`.

---

## 7. Page-hash inheritance

**A page hash inherits its revision, and does not inherit its run.**

```text
page_hashes.page_id -> archive_pages.inventory_id -> page_inventory.source_revision_id
```

`page_hashes` gains no ownership column of its own, for the reason slice 1 §5
gives: 8,821,073 rows carrying a redundant key that could disagree with their
parent is a contradiction waiting to be written.

Run provenance is the opposite case, and §4.3 is why. Every `dhash` and
`phash` row was written by a later run than the extraction that produced its
page. An inventory describes **one extraction**; it cannot speak for a
perceptual-hashing run that happened weeks afterwards.

```text
revision   inherited through the parent                    no column needed
run        NOT inherited; belongs to page_hashes itself    deferred, slice 1 §6.3
```

This is the measured basis for slice 1's corrected run-provenance population
of 11,956,983.

---

## 8. Idempotency and supersession

### 8.1 Three outcomes, matching slice 1 §9.4

Let *inventory identity* be `(archive_id, source_revision_id)`. A
re-extraction produces one of three outcomes:

```text
same identity, same content_digest
    -> idempotent reuse. No new inventory, nothing superseded, nothing
       written. PI-29.

same identity, DIFFERENT content_digest
    -> a contradiction: the same bytes yielded a different page set. An
       explicit replacement carrying a reason. The predecessor is superseded
       and both generations' pages are retained. PI-30.

different revision
    -> INDEPENDENT ACTIVE EVIDENCE. A new inventory is created and NOTHING is
       superseded. Both remain active, each the current inventory for its own
       revision. PI-31, PI-10.
```

**The third line is a correction.** An earlier draft of this document said a
different revision supersedes the old inventory. That contradicts slice 1
§9.4 — "a newer revision never supersedes older evidence... an inspection of
revision 1 remains a true and current statement about revision 1 for as long
as revision 1 exists" — and it would destroy the per-revision history the
whole step exists to create.

The partial index of §6.1 was already right: it caps one active inventory per
*revision*, not per archive, so two revisions' inventories coexist by
construction. It was the prose that disagreed with the schema.

### 8.2 "Current" is a question about a revision

The correction above breaks a query the earlier draft published. With two
active inventories for one archive, this returns both generations
interleaved:

```sql
-- WRONG: returns every active generation, not "the current page set"
SELECT p.* FROM archive_pages p
  JOIN page_inventory i ON i.id = p.inventory_id
 WHERE i.archive_id = ? AND i.superseded_at IS NULL;
```

Measured on two revisions with 3 and 4 pages: **5 rows across 2 inventories.**
"The current page set for an archive" is not well defined without naming which
revision is current. Two queries are, and both are proved:

```sql
-- the page set for a given revision (PI-08)
SELECT p.* FROM archive_pages p
  JOIN page_inventory i ON i.id = p.inventory_id
 WHERE i.source_revision_id = ? AND i.superseded_at IS NULL;

-- the page set for the archive's current revision (PI-09)
SELECT p.* FROM archive_pages p
  JOIN page_inventory i ON i.id = p.inventory_id
  JOIN archive_files  a ON a.id = i.archive_id
 WHERE i.archive_id = ?
   AND i.source_revision_id = a.current_revision_id
   AND i.superseded_at IS NULL;
```

The second reads `archive_files.current_revision_id`, which migration 014
calls "the sole authoritative pointer". Slice 1 §8.1 forbids reading that
pointer **when binding evidence**, because it is mutable and another writer
may have moved it. Reading it to answer "what is current *now*" is the exact
thing it exists for. The two uses are different and the distinction is worth
keeping explicit: never for attribution, always for currency.

### 8.3 The sealing invariant: building, sealed, superseded

A parent `UPDATE` trigger cannot enforce anything about a child set — it never
sees one. An earlier draft relied on parent measurement-immutability for
idempotency, and that does not hold. Reproduced against the earlier design:

```text
delete a child of an ACTIVE inventory              ACCEPTED   WRONG
  declared page_count                                     3
  actual child count                                      2
  page_hashes remaining                     2 (cascade took one)
add an unrelated child to an ACTIVE inventory      ACCEPTED   WRONG
```

An active inventory could gain or lose children, diverge from its declared
`page_count`, and cascade-delete hashes, with nothing to stop it. Immutable
parent columns say nothing about that.

The fix is a lifecycle with one moment where the set stops changing:

```text
building    sealed_at IS NULL
            children may be inserted and deleted; the inventory is not yet
            evidence and may not be superseded (CHECK in §6.1, PI-18)

sealed      sealed_at IS NOT NULL
            the child set is frozen: no insert, no delete, no reparenting
            (PI-19, PI-20, PI-25). Sealing is one-way (PI-16, PI-17)

superseded  superseded_at IS NOT NULL
            historical, and only reachable from sealed
```

Sealing is the set-level check, and it is the only place the parent's declared
values are compared against the children that actually exist:

```sql
CREATE TRIGGER trg_page_inventory_seal_checks_children
BEFORE UPDATE OF sealed_at ON page_inventory FOR EACH ROW
WHEN NEW.sealed_at IS NOT OLD.sealed_at
 AND (OLD.sealed_at IS NOT NULL
   OR NEW.sealed_at IS NULL
   OR NEW.page_count <> (SELECT count(*) FROM archive_pages
                          WHERE inventory_id = OLD.id)
   OR NEW.page_count <> (SELECT ifnull(max(page_index) + 1, 0)
                           FROM archive_pages WHERE inventory_id = OLD.id))
BEGIN
    SELECT RAISE(ABORT,
      'seal requires a dense child set matching the declared page_count');
END;
```

The two comparisons are not redundant. The first catches a wrong count; the
second catches a *right* count over a sparse set — three children at indexes
0, 1, 5 satisfy `count(*) = 3` and are not a page inventory (`PI-14`). §4.4
measured that every existing archive is dense, so this is an invariant the
current data already satisfies rather than one it would have to be forced
into.

`content_digest` is immutable from insert (`PI-44`), so the digest a producer
declares before filling is the one it is held to at seal.

### 8.4 Both complementary triggers are required, and the prototype proved it

The successor-identity check cannot be one trigger. A `BEFORE UPDATE OF
superseded_by_id` trigger cannot see a successor that does not exist yet,
which is exactly the window the deferred foreign key opens.

This was not reasoned about. The first prototype had only the `BEFORE UPDATE`
half, and **superseding an inventory with a successor belonging to a different
archive was ACCEPTED.** The `AFTER INSERT` half closes it, and both now also
compare `source_revision_id`, so a successor for a *different revision* is
refused too (`PI-40`, `PI-41`) — which is what §8.1's correction requires,
since a different revision is not a replacement at all.

Slice 1 §9.4.2 established this pair for `archive_inspections` and the gap was
reintroduced anyway on a new table, which is the argument for generating the
pair from one definition per table rather than writing it out each time.

Every transition trigger carries the value-change guard of slice 1 §9.4.2
(`PI-34`, `PI-39`, `PI-46`).

The write order is slice 1 §9.4.1 unchanged — `BEGIN IMMEDIATE`, preallocate
the successor's rowid, update the predecessor, insert the successor, commit.

---

## 9. Executed cases

In-memory SQLite 3.40.1. Stable identifiers, semantic signatures, unique
executions counted rather than summed.

```text
parent uniqueness
PI-01  two active inventories, same revision                  REJECTED
PI-02  superseded + active for one revision                   ACCEPTED
PI-03  two active unresolved for one archive                  REJECTED
PI-04  bound + unresolved for one archive coexist             ACCEPTED
PI-05  inventory bound to another archive's revision          REJECTED
PI-06  two inventories building for one archive               REJECTED

independent generations
PI-07  two revisions, both sealed and active                  ACCEPTED
PI-08  per-revision query returns exactly one generation      ACCEPTED
PI-09  current-revision query returns exactly one generation  ACCEPTED
PI-10  a new revision supersedes nothing                      ACCEPTED

sealing
PI-11  seal with fewer children than page_count               REJECTED
PI-12  seal with more children than page_count                REJECTED
PI-13  seal a dense matching child set                        ACCEPTED
PI-14  seal a sparse child set of the right size              REJECTED
PI-15  seal a zero-page inventory                             ACCEPTED
PI-16  re-seal an already sealed inventory                    REJECTED
PI-17  un-seal an inventory                                   REJECTED
PI-18  supersede an unsealed inventory                        REJECTED

child confinement
PI-19  add a page to a sealed inventory                       REJECTED
PI-20  delete a page from a sealed inventory                  REJECTED
PI-21  delete a page while still building                     ACCEPTED
PI-22  same page_index twice in one inventory                 REJECTED
PI-23  same page_index under two inventories                  ACCEPTED
PI-24  child naming another archive's inventory               REJECTED
PI-25  reparent a page to another inventory                   REJECTED

retention
PI-26  superseding keeps the old generation's pages           ACCEPTED
PI-27  deleting an inventory that still has pages             REJECTED
PI-28  page_hashes survive under a superseded parent          ACCEPTED

end-to-end rerun
PI-29  identical rerun writes nothing                         ACCEPTED
PI-30  differing rerun replaces, retaining both               ACCEPTED
PI-31  a different revision leaves both active                ACCEPTED

attribution
PI-32  unresolved -> bound                                    ACCEPTED
PI-33  rebind an already-bound inventory                      REJECTED
PI-34  attribution rewrite, value-identical                   ACCEPTED
PI-35  bind to a basis the table may not carry                REJECTED

supersession
PI-36  INSERT already carrying a pointer                      REJECTED
PI-37  INSERT already sealed                                  REJECTED
PI-38  un-supersede a historical inventory                    REJECTED
PI-39  supersession rewrite, value-identical                  ACCEPTED
PI-40  successor of another archive                           REJECTED
PI-41  successor naming a different revision                  REJECTED
PI-42  successor id must exceed predecessor id                REJECTED

immutability
PI-43  rewrite inventory page_count                           REJECTED
PI-44  rewrite inventory content_digest                       REJECTED
PI-45  rewrite a page measurement                             REJECTED
PI-46  byte-identical inventory rewrite                       ACCEPTED

registered 46   duplicate registrations 0   unique 46
all 46 produced the outcome recorded here
```

`PI-29`, `PI-30` and `PI-31` are end-to-end: each drives a producer-shaped
sequence — build, fill, seal, then decide — rather than asserting a single
statement, which is what makes them evidence about idempotency rather than
about one trigger.

`PI-07` and `PI-31` state the point of the design: two revisions of one
archive coexist, both active, neither superseding the other.

---

## 10. Migration ordering

### 10.1 The planner cannot key on rows that do not exist

Slice 3 plans bindings for a table slice 4 creates. An earlier draft said the
planner "freezes the row IDs", which for this table is impossible: no
`page_inventory` row exists when the planner runs.

**The plan's key for this table is `archive_id`, not `page_inventory.id`.**
That is a real key for the planned population, because §3 establishes that
every archive currently has exactly one extraction, so one inventory is minted
per archive and `archive_id` identifies it uniquely. The planner emits:

```text
archive_id, provenance_basis, source_revision_id, page_count, content_digest
```

and slice 4's binding digest records, per row, the `archive_id` it planned and
the `page_inventory.id` it actually minted. The reconciliation is
**planned-key to applied-row**, not id to id:

```text
every planned archive_id was minted exactly once
no inventory exists whose archive_id the plan did not contain
per-table totals match the plan's totals            58,437
rows the plan marked unresolved carry NULL and the planned reason
```

The other four receiving tables keep slice 1 §11.1's `(table, row id, ...)`
digest unchanged, because their rows already exist when the planner runs.
Only this table needs the natural key, and only because it is created by the
migration it is being planned for.

### 10.2 The child table must be rebuilt exactly once

Two SQLite constraints force this, both measured on 3.40.1:

```text
ALTER TABLE ... ADD COLUMN inventory_id INTEGER NOT NULL
    on an empty table                                 ACCEPTED
    on a populated table                              REFUSED
      "Cannot add a NOT NULL column with default value NULL"

DROP INDEX sqlite_autoindex_archive_pages_1           REFUSED
      "index associated with UNIQUE or PRIMARY KEY constraint
       cannot be dropped"
```

`UNIQUE (archive_id, page_index)` is a table-level constraint, so its index is
an implicit autoindex (`origin = 'u'`) that no `DROP INDEX` can remove. Adding
a populated `NOT NULL inventory_id` needs a rebuild, and removing the old
unique key needs a rebuild. An earlier draft split those across slices 4 and
5, which would have rebuilt a 2,955,391-row table **twice**.

**One rebuild, landing the final child shape**: `inventory_id NOT NULL` with
its composite foreign key, `location_id` dropped, `UNIQUE (inventory_id,
page_index)` in place of `UNIQUE (archive_id, page_index)`.

### 10.3 The producer must cut over in the same slice

The current producer writes no `inventory_id` and deletes by `archive_id`:

```sql
DELETE FROM archive_pages WHERE archive_id = ?
INSERT INTO archive_pages (archive_id, location_id, page_index, ...)
```

Against the final child shape, both statements are wrong: the insert omits a
`NOT NULL` column and names a dropped one, and the delete would remove a
sealed generation's children (`PI-20` refuses it). There is no interim shape
in which the old producer and the new schema are both correct, so **the
rebuild and the producer cutover are one change** and cannot be separated.

This is a deviation from slice 1's shape, and the lead should rule on it.
Slice 1 §11 splits every other receiving table across slice 4 (ownership,
uniqueness unchanged) and slice 5 (uniqueness moves, producers switch to
append). The page tables cannot follow that split, because their "uniqueness
unchanged" interim does not exist — the child rebuild that adds `inventory_id`
is the same rebuild that changes the unique key.

```text
slice 3   planner            plans 238,956 bindings; page evidence keyed by
                             archive_id (§10.1); no schema
slice 4   other four tables  exactly as slice 1 §11 specifies
slice 4p  page tables        ONE migration: create page_inventory, mint
                             58,437 rows, rebuild archive_pages into its
                             final shape, populate inventory_id, and cut the
                             producer over to build/fill/seal
slice 5   other four tables  exactly as slice 1 §11 specifies; page tables
                             have nothing left to do here
```

`4p` is drawn as a sibling of slice 4 rather than a renumbering, so slice 1's
sequence for the other tables is untouched. Whether it runs before, after or
in the same operator window as slice 4 is a sequencing question this document
does not decide.

---

## 11. Recovery path

```text
before      protected pre-migration backup verified by size and sha256;
            expected counts recorded: 58,437 inventories to mint,
            2,955,391 pages to attach, 8,821,073 hashes untouched
during      one transaction; a partial rebuild is not a state the schema
            can be left in
after       reconciliation: every page row has exactly one inventory_id;
            every inventory is sealed; every inventory's page_count equals
            its child count and its max(page_index)+1; every child's
            archive_id equals its parent's; hash values and row counts
            byte-identical in all three tables
failure     restore from the protected pre-migration backup
```

**There is no down-migration, and this document does not propose one.**
Migration 014 set that precedent for the same reason: a reverse migration that
has to reconstruct discarded state is a second thing to get wrong, and the
backup already exists. Recovery is restoration.

The backfill mints every historical inventory **already sealed**, because the
extraction it describes completed months ago. `PI-37` refuses an insert that
arrives sealed, so the migration seals in a second statement inside the same
transaction — which is also what runs the §8.3 child check over all 58,437,
making the reconciliation above an enforced invariant rather than a
post-hoc audit.

---

## 12. Decisions and deliberate non-decisions

### 12.1 Decisions

- **`page_inventory` is the authoritative backfill unit**, 58,437 rows, total
  backfill population 238,956 (§5).
- **The five zero-page archives receive inventories** (§4.5). Corroborated by
  the inspector, the revision and the canonical empty-tuple digest.
- **`extracted_at` is `MIN(created_at)` of the children**, understood as the
  first persistence timestamp after extraction completed (§4.2).
- **The inventory carries its own `content_digest`.** An earlier draft
  declined this as duplicating `archive_content_signatures`. That was wrong
  for a reason the earlier draft did not consider: `archive_content_signatures`
  is `UNIQUE (archive_id)` and can hold exactly one row per archive, so it
  cannot carry a per-generation identity once two generations exist. Without a
  digest on the parent there is nothing to compare an idempotent rerun
  against (§8.3).
- **Ownership lives only on the parent.** `archive_pages` never gains
  `source_revision_id` or `provenance_basis` (§6.2).
- **Supersession is parent-only**, and a different revision supersedes nothing
  (§8.1).
- **`location_id` is removed from the child**; `archive_id` stays, because it
  is the composite foreign key's second column (§6.2).
- **A page hash inherits its revision, never its run** (§7).
- **The child table is rebuilt once, with the producer cutover in the same
  slice** (§10.2, §10.3).

### 12.2 Deliberate non-decisions

- **The 15 `sha256` hashes written after their page row are not explained**
  (§4.3). Recorded as a gap; no reconstruction is offered.
- **Where slice 4p sits relative to slice 4 is not decided** (§10.3). It is a
  sequencing question for the operator and the lead.
- **Perceptual-hash run provenance is not designed here.** §4.3 establishes
  that it is a separate production event; what column records it belongs to
  the slice slice 1 §6.3 defers past slice 6.
- **The 16 drift archives are not remediated.** Their 768 page rows get an
  inventory like any other, bound `unresolved_drift`, which is the
  conservative treatment slice 1 §4.2 requires.
- **Retention policy for superseded inventories is not decided.** This design
  makes keeping both generations *possible*; how long they are kept is the
  retention planner's question.
- **Concurrent extraction of one archive is refused, not queued** (`PI-06`).
  The building index permits one in-flight inventory per archive; what a
  second worker should do about that is a job-queue question, and slice 1
  defers queue leases deliberately.
