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
quick_check     ok
data_version    2 -> 2 (unchanged across the read)
```

One guarded read. Nothing was written, and no report was placed beside the
database. Every SQLite construct below was executed in memory; production was
read and never written.

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

Three things follow, and they shape everything after.

**The batch is already the producer's unit.** An extraction is atomic: the
whole page set is replaced in one transaction, never diffed. The comment even
calls it "the page inventory". The parent table proposed here does not invent
a granularity — it *names one that already exists implicitly* and gives it a
row so that ownership, idempotency and supersession have somewhere to live.

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
archive_pages                  2,955,391
page_hashes                    8,821,073
```

Pages per archive:

```text
min      1
p50     27
p90    112
p99    336
max  6,041
avg  50.58
```

The parent is therefore ~58,432 rows against 2,955,391 children — a **50.6x**
reduction in the population that receives an ownership column.

### 4.2 Is a historical batch derivable, or must it be synthesized?

This is the question the whole design turns on, and it was measured rather
than assumed: SQLite evaluates `CURRENT_TIMESTAMP` per statement at one-second
resolution, so a large archive can straddle a second boundary even inside one
transaction.

```text
archives whose pages share ONE created_at      58,235   (99.66%)
archives whose pages span TWO created_at          197   ( 0.34%)
maximum span within one archive               1 second
                                              ------
                                               58,432
```

**The batch is derivable, but not from `created_at`.** Because the producer
replaces the entire set, every page row an archive currently has belongs to
exactly one extraction — so today the batch is simply `archive_id`, and the
backfill mints one inventory per archive that has pages. `created_at` is not
the identifier and does not need to be.

It does, however, decide the parent's own timestamp, and for 197 archives the
children disagree by a second. **Decision: `extracted_at` is `MIN(created_at)`
of the children** — the moment extraction began, which is the fact the row is
about. `MAX` would be equally defensible and must not be chosen silently; the
197 are the population that makes the two distinguishable, and any later
reconciliation should expect them.

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

Two smaller measurements that a parent must be able to express:

```text
pages with sha256 but no phash                  22,550
archives mixing hashed and unhashed pages            0
```

Perceptual hashing is **all-or-nothing per archive**: no archive has some
pages hashed and others not. The 22,550 are whole archives never perceptually
hashed, not partial coverage. That means perceptual-hash state is a property
of (archive, run) and does not need per-page bookkeeping in the parent.

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
content signatures with no page rows                      5
```

Dense indexes mean a parent's `page_count` is trustworthy and checkable
against its children. A single location per page set means `location_id`
belongs on the parent, not repeated 2,955,391 times. The 5 signature-without-
pages archives are carried forward from slice 1 §3.3 unchanged; they get no
inventory, because there is no extraction to describe.

---

## 5. The authoritative backfill unit

**Decision: `page_inventory`. One row per (archive, extraction).**

The backfill mints 58,432 inventory rows, one per archive that has page rows,
and each existing `archive_pages` row is attached to its archive's inventory.
Ownership — `source_revision_id` and `provenance_basis` — lives on the parent
and nowhere else.

The consequence for slice 1's provisional totals, which §7.2 there recorded as
two candidate figures pending exactly this decision:

```text
ownership on page rows      2,955,391  ->  total 3,135,910   NOT CHOSEN
ownership on inventory rows    58,432  ->  total   238,951   CHOSEN
```

**The backfill population is 238,951 rows.** Slice 3 emits one planned binding
per row of that population, and slice 4's gate proves each was applied once.

Run provenance is a different population again and is unchanged by this
decision: slice 1 §6.3 records 11,956,983, which counts `page_hashes` because
a run is not inherited (§7 below).

Why the parent rather than the page row:

- **Re-extraction replaces a set, not rows.** Page counts differ between
  generations, so there is no page-to-page mapping for `superseded_by_id` to
  point at. §9.5 of slice 1 made this argument; the census confirms its
  premise, since no archive has index gaps and every generation is a dense run
  of its own length.
- **An interrupted marking would look like a partial extraction.** Marking
  2,955,391 rows individually turns an all-or-nothing event into something a
  reader must reassemble. One parent row is atomic by construction.
- **The 50.6x reduction is not the reason, but it is not nothing.** A migration
  that writes 58,432 rows is a different operational risk from one that
  rewrites 2,955,391.

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
    extracted_at       TEXT NOT NULL,
    created_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    superseded_at      TEXT,
    superseded_by_id   INTEGER,
    superseded_reason  TEXT,

    UNIQUE (id, archive_id),

    CHECK ((superseded_at IS NULL) = (superseded_by_id IS NULL)),
    CHECK ((superseded_at IS NULL) = (superseded_reason IS NULL)),
    CHECK (superseded_by_id IS NULL OR superseded_by_id > id),
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

The vocabulary is the one slice 1 §9.4.2 assigns to `archive_pages / page
inventory`, unchanged. `UNIQUE (id, archive_id)` exists for the children's
composite key, the same device migration 014 added for lineage.

Active uniqueness is partial, on both branches, exactly as slice 1 §9.3
requires:

```sql
CREATE UNIQUE INDEX ui_page_inventory_bound
    ON page_inventory(source_revision_id)
 WHERE source_revision_id IS NOT NULL AND superseded_at IS NULL;

CREATE UNIQUE INDEX ui_page_inventory_unresolved
    ON page_inventory(archive_id)
 WHERE source_revision_id IS NULL AND superseded_at IS NULL;
```

One active inventory per bound revision; one per unresolved archive. The
unresolved branch is archive-keyed for the reason slice 1 gives — an
unresolved row cannot be distinguished by revision, so the conservative cap is
one per archive, which is exactly today's behaviour.

### 6.2 The children

```sql
archive_pages
    + inventory_id INTEGER NOT NULL
    - UNIQUE (archive_id, page_index)
    + UNIQUE (inventory_id, page_index)
    + FOREIGN KEY (inventory_id, archive_id)
          REFERENCES page_inventory(id, archive_id)     -- NO ACTION
    - source_revision_id / provenance_basis    -- never added; ownership is
                                                  the parent's, not the row's
```

`archive_id` **stays** on the child. It is redundant with the parent, and that
redundancy is what the composite foreign key checks: a page cannot be attached
to an inventory belonging to a different archive. Dropping it would remove the
constraint's second column and with it the guarantee.

`NO ACTION`, never `CASCADE`, on the inventory key. A superseded generation's
pages are the retention this whole design exists to create; a cascade would
delete exactly what is being kept.

`page_hashes` is **unchanged**. It keeps `UNIQUE (page_id, algorithm,
algorithm_version)` — already the shape the roadmap asks for — and keeps its
`ON DELETE CASCADE` from `archive_pages`, which is correct: if a page row is
ever genuinely deleted, hashes describing that exact page have nothing left to
describe.

---

## 7. Page-hash inheritance

**A page hash inherits its revision, and does not inherit its run.**

The revision path is unambiguous and needs no new column:

```text
page_hashes.page_id -> archive_pages.inventory_id -> page_inventory.source_revision_id
```

`page_hashes` gains no ownership column of its own, for the reason slice 1 §5
gives: 8,821,073 rows carrying a redundant key that could disagree with their
parent is a contradiction waiting to be written.

Run provenance is the opposite case, and §4.3 is why. Every `dhash` and
`phash` row was written by a later run than the extraction that produced its
page. An inventory describes **one extraction**; it cannot speak for a
perceptual-hashing run that happened weeks afterwards. So:

```text
revision   inherited through the parent                    no column needed
run        NOT inherited; belongs to page_hashes itself    deferred, slice 1 §6.3
```

This is the measured basis for slice 1's corrected run-provenance population
of 11,956,983: `page_hashes` is in it precisely because a run cannot be
inherited from a parent that was not present when the run happened.

---

## 8. Idempotency and supersession

### 8.1 Three outcomes, matching slice 1 §9.4

Let *inventory identity* be `(archive_id, source_revision_id)`. A
re-extraction produces one of three outcomes:

```text
same identity, identical page set
    -> idempotent reuse. No new inventory, nothing superseded. The parent's
       measurement columns are immutable, so a byte-identical rewrite passes
       and a differing one fails; that is the guard, not a comparison the
       producer has to remember to make.

same identity, DIFFERENT page set
    -> a contradiction: the same bytes yielded a different inventory. May
       supersede, but only as an explicit replacement carrying a reason.

different revision
    -> a new inventory, and the old one is superseded with a reason.
       Both generations' page rows are retained.
```

"Different page set" means any child differs, which the immutability triggers
detect per value rather than by column list — the slice 1 §9.4.2 lesson, and
the reason PI-26 (a byte-identical rewrite) is ACCEPTED while PI-24 is not.

### 8.2 Supersession is parent-only

Children are **never individually superseded**. They have no `superseded_at`,
no `superseded_by_id`, no reason column. A page row's currency is entirely a
property of its inventory:

```sql
-- the current page set for an archive
SELECT p.* FROM archive_pages p
  JOIN page_inventory i ON i.id = p.inventory_id
 WHERE i.archive_id = ? AND i.superseded_at IS NULL;
```

This is what §9.3 of slice 1 anticipated when it deliberately left `WHERE
active` off the `archive_pages` and `page_hashes` interim indexes: those
tables have no active/superseded state, and after this design they still do
not. The predicate lives on the parent.

The write order is slice 1 §9.4.1 unchanged — `BEGIN IMMEDIATE`, preallocate
the successor's rowid, update the predecessor, insert the successor, commit —
and it works here for the same reason: the deferred self-key tolerates the
window, the partial index does not tolerate two active rows.

### 8.3 Both complementary triggers are required, and the prototype proved it

The successor-identity check cannot be one trigger. A `BEFORE UPDATE OF
superseded_by_id` trigger cannot see a successor that does not exist yet,
which is exactly the window the deferred foreign key opens.

This was not reasoned about. The first prototype had only the `BEFORE UPDATE`
half, and **PI-22 — superseding an inventory with a successor belonging to a
different archive — was ACCEPTED.** The `AFTER INSERT` half closes it:

```sql
CREATE TRIGGER trg_page_inventory_insert_checks_waiting_predecessors
AFTER INSERT ON page_inventory FOR EACH ROW
WHEN EXISTS (SELECT 1 FROM page_inventory AS p
              WHERE p.superseded_by_id = NEW.id
                AND p.archive_id IS NOT NEW.archive_id)
BEGIN
    SELECT RAISE(ABORT, 'a predecessor already points here from another archive');
END;
```

Slice 1 §9.4.2 established this pair for `archive_inspections` and the gap was
reintroduced anyway on a new table, which is the argument for generating the
pair from one definition per table rather than writing it out each time.

Every transition trigger carries the value-change guard of slice 1 §9.4.2
(PI-16, PI-21, PI-26).

---

## 9. Executed cases

In-memory SQLite 3.40.1, same discipline as slice 1: stable identifiers,
semantic signatures, unique executions counted rather than summed.

```text
PI-01  two active inventories, same revision          REJECTED
PI-02  superseded + active for one revision           ACCEPTED
PI-03  two active unresolved for one archive          REJECTED
PI-04  bound + unresolved for one archive coexist     ACCEPTED
PI-05  inventory bound to another archive's revision  REJECTED

PI-06  same page_index twice in one inventory         REJECTED
PI-07  same page_index under two inventories          ACCEPTED
PI-08  child naming another archive's inventory       REJECTED
PI-09  pages added to a superseded inventory          REJECTED

PI-10  superseding keeps the old generation's pages   ACCEPTED
PI-11  deleting an inventory that still has pages     REJECTED
PI-12  page_hashes survive under a superseded parent  ACCEPTED
PI-13  a page delete still cascades to its hashes     ACCEPTED

PI-14  unresolved -> bound                            ACCEPTED
PI-15  rebind an already-bound inventory              REJECTED
PI-16  attribution rewrite, value-identical           ACCEPTED
PI-17  bind to a basis the table may not carry        REJECTED

PI-18  valid deferred replacement                     ACCEPTED
PI-19  INSERT already carrying a pointer              REJECTED
PI-20  un-supersede a historical inventory            REJECTED
PI-21  supersession rewrite, value-identical          ACCEPTED
PI-22  successor inventory of another archive         REJECTED
PI-23  successor id must exceed predecessor id        REJECTED

PI-24  rewrite inventory page_count                   REJECTED
PI-25  rewrite a page measurement                     REJECTED
PI-26  byte-identical inventory rewrite               ACCEPTED

registered 26   duplicate registrations 0   unique 26
all 26 produced the outcome recorded here
```

`PI-07` and `PI-10` are the two that state the point of the design: two
generations of one archive coexist, each a dense page set of its own length,
neither destroying the other.

`PI-22` is recorded as REJECTED, which is its behaviour **after** the fix of
§8.3. With the single-trigger form it was ACCEPTED; that is the finding, not a
footnote to it.

---

## 10. Migration ordering

```text
this design (slice 2)   no schema, no planner
        |
slice 3  planner        emits 238,951 planned bindings against page_inventory
        |                as the receiving table -- possible only once the
        |                unit is frozen, which is what this gate does
slice 4  migration      create page_inventory; mint 58,432 rows; add
        |                inventory_id to archive_pages and populate it;
        |                ownership + basis on the parent
        |
slice 5  uniqueness     archive_pages uniqueness moves to
                         (inventory_id, page_index); producers switch from
                         delete-then-reinsert to append-a-new-inventory
```

Two orderings are load-bearing.

**The parent must exist before the planner runs.** The planner freezes the
receiving table, the row ids, the totals and the plan digest. Planning against
`archive_pages` and then landing ownership on `page_inventory` reconciles the
wrong unit against the wrong table, and the plan is the artifact the lead
approved — it cannot be silently re-run afterwards. This is slice 1 §11.3.

**`inventory_id` must be populated in the same migration that creates the
parent.** A window in which `archive_pages` has a nullable, unpopulated
`inventory_id` is a window in which a page belongs to no extraction, and the
composite foreign key cannot be added until every row has one. Slice 4 creates,
mints, populates and constrains in one migration or does none of it.

**Uniqueness moves in slice 5, not slice 4.** `UNIQUE (archive_id,
page_index)` and `UNIQUE (inventory_id, page_index)` are identical in effect
while only one generation exists, so slice 4 can leave the old key in place
and slice 5 swaps it together with the producer change — the same
"uniqueness and producer are one change" argument slice 1 §4.3 makes.

---

## 11. Recovery path

Slice 4 touches the largest table in the database, so recovery is stated
before it is needed rather than discovered.

```text
before      protected pre-migration backup verified by size and sha256;
            expected counts recorded: 58,432 inventories to mint,
            2,955,391 pages to attach, 8,821,073 hashes untouched
during      one transaction; a partial migration is not a state the
            schema can be left in
after       reconciliation: every page row has exactly one inventory_id;
            every inventory's page_count equals its child count; every
            child's archive_id equals its parent's; hash values and row
            counts byte-identical in all three tables
failure     restore from the protected pre-migration backup
```

**There is no down-migration, and this document does not propose one.**
Migration 014 set that precedent for the same reason: a reverse migration that
has to reconstruct discarded state is a second thing to get wrong, and the
backup already exists. Recovery is restoration.

The reconciliation is checkable in full because §4.4 measured that page
indexes are dense and page counts already agree with the signature — so
"every inventory's `page_count` equals its child count" is an assertion the
current data satisfies, not an aspiration.

---

## 12. Decisions and deliberate non-decisions

### 12.1 Decisions

- **`page_inventory` is the authoritative backfill unit**, 58,432 rows, total
  backfill population 238,951 (§5). This resolves the two candidate figures
  slice 1 §7.2 left provisional.
- **`extracted_at` is `MIN(created_at)` of the children** (§4.2). 197 archives
  make `MIN` and `MAX` distinguishable; the choice is recorded rather than
  taken silently.
- **Ownership lives only on the parent.** `archive_pages` never gains
  `source_revision_id` or `provenance_basis` (§6.2).
- **Supersession is parent-only.** Children have no supersession columns
  (§8.2).
- **A page hash inherits its revision, never its run** (§7).
- **`archive_id` stays on the child**, because it is the second column of the
  composite foreign key (§6.2).

### 12.2 Deliberate non-decisions

- **The 15 `sha256` hashes written after their page row are not explained**
  (§4.3). Recorded as a gap; no reconstruction is offered.
- **The 5 signature-without-pages archives get no inventory.** There is no
  extraction to describe. Unchanged from slice 1 §3.3.
- **Perceptual-hash run provenance is not designed here.** §4.3 establishes
  that it is a separate production event; what column records it belongs to
  the slice slice 1 §6.3 defers past slice 6.
- **The 16 drift archives are not remediated.** Their 768 page rows get an
  inventory like any other, bound `unresolved_drift`, which is exactly the
  conservative treatment slice 1 §4.2 requires. Minting a revision for the
  earlier generation remains a separate, separately reviewed design.
- **No inventory-level digest is proposed.** A digest over the ordered page
  set would duplicate `archive_content_signatures`, which already exists and
  already has that meaning.
- **Retention policy for superseded inventories is not decided.** This design
  makes keeping both generations *possible*; how long they are kept is the
  retention planner's question, and slice 1 §11 sequences it separately.
