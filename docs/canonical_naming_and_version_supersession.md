# Canonical file naming and WIP-version supersession (design)

Status: **design only**, for review. Nothing here is implemented, and no
live file is renamed, moved, or removed by this document. Tracks issue
#111. Author: claude-lead. Reviewer: codex-lead.

- **First draft:** `de4c757`.
- **Second draft:** `826c827`, which answered the first review (version
  family, current-byte provenance, dependency-aware drift, recovery,
  version labels).
- **Third draft:** `a3a54b9`. It answered the re-review: receipt-backed
  ownership, recovery at every durable boundary, and phased family
  validation.
- **This revision** answers the third review. Locking is now an OS-held
  shared-scope lock instead of an O_EXCL file with takeover. Recovery now
  works on an action-level frontier, so a completed step's destination is
  history, not a current-state check.

## Operator requirements

All from David, 2026-10-22, in the terminal and recorded on #111 and #112:

- **Every archive gets a name that identifies it at a glance.**
  - With volumes: `Series Name v01 #003 (2019).cbz`.
  - Without volumes: `Series Name ch. 1 (2019).cbz`.
- **"v" is ambiguous today.** Many WIP books use `v` for **version**, not
  volume. A later version usually contains every earlier page with new pages
  appended, so earlier versions may be overwritten.
- **The version designation is kept**, written **`[vN]`** (approved). For
  example `Series Name ch. 1 (2019) [v3].cbz`. It can never be read as the
  `v01` volume token.
- **Brackets.** Bracket stripping stays for identity matching, while the
  display name preserves the canonical tokens (§5.5). Confirmed by David on
  2026-10-22.

## Non-goals

- No rename, move, removal, quarantine, ComicInfo rewrite, or database
  write is implemented or authorized here.
- No change to the V1 perceptual hashes (frozen), to routing activation, or
  to the Syncthing configuration. The Tower → X: merge (#108) stays the
  final step.
- Re-encoding or repacking archives is out of scope. A rename changes the
  path only, never the bytes.
- Canonical series records (Phase 4) are not designed here. This document
  consumes whatever identity has been decided and refuses to guess when none
  has.

## 1. Prerequisites that must land first

Both are measured defects. The Step 8 property tests (#113) pin them as
strict xfails.

1. **`series_key` is not idempotent.** Today `Berserk_Uncensored` becomes
   `berserk uncensored` and then `berserk`. `Berserk Uncensored` becomes
   `berserk` directly.
   - **The fix comes first**, in its own reviewed PR. It must make the key a
     fixpoint, so that `series_key(series_key(x)) == series_key(x)`.
   - **Then the index is rebuilt.** Because the fix changes keys, the index
     and every key-derived record are rebuilt. A before/after key-collision
     report is produced first and reviewed by David.
   - **Scope of the change.** The fix changes only the treatment of the
     marker words `series_key` already strips. Those are
     `uncensored`/`decensored` (`scripts/cbz_routing.py`). Language and
     edition words keep whatever identity treatment they have today.
2. **Page order depends on physical member order when two names sort
   equal.** The fix is a deterministic tie-break on the raw member name
   after the natural sort key.
   - **Identical raw names are not ordered.** Two members with the same
     name, whether or not their bytes differ, cannot be ordered by name. An
     archive containing them is **unresolved** for comparison (§4.1), never
     ordered by physical position. This is the narrower claim: order becomes
     independent of physical order *for archives whose member names are
     unique*.
   - **The reorder is recorded, not just recomputed.** Page rows are keyed
     `(archive_id, page_index)` and their hashes hang off the page row. A
     reorder therefore changes which index a member occupies. The fix PR
     re-inventories affected archives and re-associates each hash with its
     member by `entry_name`, never by index. Content signatures are
     recomputed. The V1 perceptual digests are per page and unchanged, but
     they are re-associated the same way.

## 2. The canonical name

### 2.1 Grammar

```text
<series> [v<VV>] [#<III> | ch. <C>] [(<year>)] [(<edition>)] [[v<N>]] .cbz
```

| Part | Rule |
|---|---|
| `<series>` | **Display name.** Human-cased and readable. It is never derived from `series_key`, which is a lossy identity key for matching, never for display. Comes from the decided identity (§3). |
| `v<VV>` | **Volume**, at least two digits (`v01`). Present only when the volume is established (§3.2). |
| `#<III>` | **Issue** within a volume, at least three digits (`#003`). |
| `ch. <C>` | **Chapter**, for books without volumes. Written unpadded, in David's form `ch. 1`. |
| `(<year>)` | Four-digit year, when established. |
| `(<edition>)` | An edition or language variant from a closed list, for example `(Uncensored)`, `(Colored)` or `(English)`. |
| `[v<N>]` | **Version**, for WIP books only, and only when it is verified (§4.6). |

### 2.2 Numbers that are not plain integers

- **Fractional numbers** keep their fraction: `#012.5` and `ch. 12.5`.
- **Ranges** stay ranges: `ch. 1-3` and `v01-02`.
- **Specials, extras, omake and one-shots** carry a word from a closed
  list, for example `Series Name Special (2019)`. Anything outside the list
  goes to review.
- **Edition markers in the name versus the key.** Of the example edition
  words, `series_key` strips only `uncensored`/`decensored` (by design: a
  decensored copy is the same series). Language and other edition words are
  **not** stripped by the key.
  - **Display:** the name keeps whichever of these words applies.
  - **Identity:** this design does not change how any of them affects
    identity.
  - **Precedent:** `cbz_core.preserve_decensor_markers` already keeps the
    censorship words in display text.

### 2.3 Path-safety and collisions

- **Illegal Windows characters** are replaced: `<>:"/\|?*`, trailing dots
  and spaces, and the reserved device names. Names are capped at a length
  that keeps the full path under the platform limit.
- **Collisions are refused.** If two archives would receive the same
  canonical name in the same directory, neither is renamed: the pair goes to
  review.
- **Case-only renames** go through an owned temporary name (§5.4).

## 3. Where series, volume, issue, year and version come from

### 3.1 Precedence

Each field is resolved independently. A field with conflicting evidence is
**unresolved**, never averaged or picked.

1. **A recorded human decision.** These come from the decision manifest of
   `series_identity_decision_manifest.md` once it exists.
2. **ComicInfo.xml**: `Series`, `Volume`, `Number`, `Year`. It counts only
   when internally consistent and not the watcher's injected template.
3. **Folder context.** The resolved series folder and siblings that are
   numbered consistently.
4. **The current filename.** It is a candidate only.

An archive whose series or primary number is unresolved is **not renamed**.
It is listed in the plan's review section with the evidence behind each
candidate.

### 3.2 Volume versus version

A `v<N>` token in a current name is classified as follows:

- **Volume** when ComicInfo `Volume` agrees, or when siblings form a volume
  run, and their page content shows different books.
- **Version** only through an established version family (§4.2).
- **Unresolved** otherwise.

Name evidence alone never classifies.

## 4. Superseded-version rule

### 4.1 Comparable page sequences

A sequence is **comparable** only if all of the following hold. Otherwise
the archive is unresolved for supersession and nothing is inferred from it.

1. **Bound to current bytes (§4.3).**
2. **Complete:** every image member of the current bytes has a row and a
   digest under the stated basis, with no missing, extra or failed pages.
3. **Nonempty:** at least one image page. An empty sequence is a strict
   prefix of everything, so it is excluded outright.
4. **Ordered under §1.2,** with unique member names.

The **sequence** is the image members in that order. Each element is the
member's digest under one stated basis:

- **Exact:** `sha256`. Only this basis can prove supersession.
- **Perceptual:** V1 `dhash` at its frozen parameters and a stated
  threshold. It may **nominate** candidates and never proves supersession.

### 4.2 The version family must be established first

Page overlap alone is **not** family evidence. An omnibus contains every
page of a separate issue without superseding it, and a colored edition is
not a later version of the monochrome one. Two archives are compared for
supersession only when **affirmative family evidence** puts them in the same
**version family**, independent of any overlap:

- **Same work.** The same decided series, and the same issue, chapter or
  volume number, or an explicitly identical "work" identifier (ComicInfo
  `Number`/`Volume`, or a human decision).
- **Same edition and language.** These come from §2.1's edition list, and
  absent counts as a value. `Uncensored` versus censored is a decensor pair
  (§4.5), never a version.
- **Version-marked names.** At least one member carries a version marker,
  or a human decision declares the family.

Families are **refused** when the evidence conflicts (different numbers,
editions or languages) or is absent. The archive then stays unresolved, and
it is **never** placed in a family on page overlap alone.

### 4.3 Current-byte provenance

Today's page rows do not prove they describe the current file:

- `ArchivePageHashRepository.save` stores rows and signatures by
  `archive_id`.
- `ArchivePerceptualHashRepository.save` checks page index and name, not
  content or revision identity.
- Equal size and mtime can survive a replacement.

So no comparison may rest on the stored rows alone.

**Contract.** For every archive the plan acts on (each supersession
candidate, each keeper, **and every ordinary rename source**), the plan
records a **strong identity**: the full-file `sha256` of the bytes it
evaluated, computed at plan time from the file itself. Additionally, for
supersession:

- the planner **re-derives the exact page sequence from those bytes**: it
  reads each image member, hashes it, and orders it under §1.2;
- it **compares that sequence with the stored rows**;
- if the two disagree, the stored rows are stale and the archive is
  unresolved.

The stored rows are a **nomination index** for finding candidates cheaply.
Authority comes only from the re-derived sequence bound to the full-file
hash.

**Legacy rows.** Rows that are not revision-bound stay usable for
nomination only. Authoritative comparison from stored rows alone waits for
the revision-bound, filesystem-validated provenance of Step 4. Until then
the planner always re-derives.

**Database snapshot.** All database reads use the repository's WAL-aware
read-audit contract: `data_version` before, one deferred read transaction,
`data_version` after. A changed pair rejects the plan.

### 4.4 Relations within an established family

For comparable exact sequences `A` and `B` in the same family:

| Relation | Definition | Outcome |
|---|---|---|
| **equal pages** | `A == B` | **not** a supersession, and not proof of an exact duplicate either. Non-image members (ComicInfo, credits, text) are outside the sequence. So: a **whole-archive** duplicate only if the full-file hashes are equal; otherwise **review**, showing the differing non-image members |
| **strict prefix** | `0 < len(A) < len(B)` and `B[:len(A)] == A` | A is superseded by B, *subject to the non-image check below* |
| **strict ordered subsequence** | A is a subsequence of B preserving order and multiplicity, not a prefix | flagged *inserted pages* supersession, always shown in review |
| **anything else** | changed, removed or reordered pages | **review** |

**Non-image payload.** Supersession discards A, so it must not discard
something unique to A. Every non-image member of A must exist in B with
identical bytes, or be a ComicInfo.xml the operator has accepted dropping.
Otherwise the relation is downgraded to **review**, listing A's unique
members.

**Index map.** A greedy left-to-right walk over B yields the exact index
map from A's pages to B's, and it is written to the plan.

### 4.5 Chains and the final keeper

- **The keeper** of a family is its unique **maximal** element. Every
  superseded member points at that keeper, never at an intermediate.
- **Forks** (two or more maximal elements) send the whole family to review.
- **Cycles** mean stale or corrupt inventory, and they stop the run.
- **Graph rank** (position in the chain) is internal ordering only. It is
  never a release version and never written into a name.

**What is never a version:**

- **Decensor pairs.** The censored original in `F:\ai-decensor-originals`
  and its decensored file. Their pairing is keyed by stable identity, not by
  path (§5.6).
- **Cross-family overlap.** For example an omnibus and its issues, or two
  editions.
- **Incomparable inventories** (§4.1).

### 4.6 The keeper's version label

The keeper keeps the version designation **established for the keeper
itself**:

- **From the keeper's own name**, when that name carries an unambiguous
  version marker and nothing contradicts it.
- **Or from its own ComicInfo** (a version field or note), when present and
  consistent.
- **Or from a recorded human decision.**

Otherwise the label is **unresolved**. The keeper is renamed without
`[vN]` only if David accepts that in the plan; otherwise its rename is held
for a decision. The design never chooses a number from other members'
names: a verified v2 must not become `[v8]` because an obsolete member was
called v8. It never uses the chain rank either.

## 5. Plan, approval and apply

The repository's guarded-operation rules apply: dry run first, report
first, an expected count plus a digest, quarantine rather than delete, and
stop when the evidence disagrees. The accepted staging design (#103) is the
precedent for ownership and journalling.

### 5.1 The plan

`plan` is read-only. Its single JSON output contains:

- every rename (source strong identity, old path, new path, evidence per
  field);
- every supersession, grouped by **family unit**: the members, the keeper,
  each relation and index map, the basis, every member's strong identity,
  and the WAL snapshot;
- every review item;
- counts;
- the **plan digest** over the canonical bytes.

### 5.2 Approval

`apply` requires `--plan`, `--expect-digest`, `--expect-renames` and
`--expect-families`. David approves that exact digest.

### 5.3 Dependency-aware revalidation, in phases

Per-row skipping is unsafe for supersession. If keeper B drifts or vanishes
while A passes its own check, quarantining A would lose pages. **The unit of
action is a family**, and any failure blocks the whole family, whatever the
run-wide drift fraction. A family runs in three phases:

1. **Initial validation.** This happens before any action in the family.
   Every member, keeper included, is checked at its **approved current
   location** against its strong identity. Each approved relation is
   re-derived from the bytes. The check also confirms that no other unit
   schedules the keeper for quarantine.
2. **Keeper rename.** This applies only if the plan renames the keeper. It
   is performed and **confirmed** (§5.4) before anything else. Then the
   keeper is **freshly validated at its resulting path**: its strong
   identity, and its sequence re-derived.
3. **Dependant quarantines.** Before each quarantine, the keeper is
   re-validated at its current path and the candidate at its current
   location.

**Revalidation also happens** after every lock wait and at each batch
boundary.

**On resume** (§5.4), each member is validated at its **journal-proven
current location**:
- the approved path when no owned action has moved it;
- the confirmed destination when one has, including an already-owned
  quarantine location.

Members are not required to still sit at their original paths. A family
whose journal shows dependants already quarantined continues with the
remaining ones only after re-validating the keeper.

**Ordinary renames** are single-member units, validated against the source
strong identity. A run-wide drift fraction still stops the run early, as an
**additional** stop and never a substitute.

### 5.4 Owned, durable recovery

This follows the accepted staging design #103 §B4: **content equality is
not ownership**. A destination is this attempt's only when the attempt holds
a **durable receipt** for that step **and** fresh content verification
agrees. Without the receipt, an existing destination is **ambiguous even if
byte-identical** to the source.

**Ownership and quiescence.** A run is an **attempt** with a unique id.

- **The scope lock.** This follows the #103 precedent. There is one
  **OS-held exclusive lock** on a single stable path per target scope,
  `<library-root>\.cbz-canonical-naming.lock`. It is shared by **every**
  rename attempt, recovery and undo on that root.
  - **Taking it:** the lock is a non-blocking exclusive byte-range lock
    (`LockFileEx`, `msvcrt.locking`), held for the whole run. A process that
    cannot take it refuses; it never waits indefinitely.
  - **The OS releases it** when the holder exits or crashes. So there is no
    stale-lock takeover, nothing ever unlinks the lock file, and a successor
    can never have its lock removed by another recoverer's earlier
    liveness check.
  - **Owner metadata** (attempt id, pid, start time) lives in a separate
    owner file, written only **after** the lock is held. It is informational
    and never consulted for exclusion.
- **Quiescence.** While holding the scope lock, mutators and the watcher
  are quiesced for the target library roots, using the existing series-lock
  and operator-mode mechanisms. Recovery and undo take the same scope lock
  and re-verify quiescence before touching anything.

**Journal.** The journal is an append-only file per attempt. Each record
carries the attempt id, the action id and a sequence number, and is fsynced.
A file journal and a SQLite transaction cannot be one transaction, so the
database carries its own durable marker.

**The step protocol.** It runs per filesystem step. A case-only rename has
two steps, old → `<new>.<attempt>.tmp` → new, and each step runs the whole
protocol.

1. **`intent`** (journal). Contents: the source path and strong identity;
   the destination path; the action kind (rename, temp, or quarantine).
   Fsynced.
2. **Filesystem step:** `os.rename(src, dst)`. On Windows this **refuses an
   existing destination**, which is the execution-time occupancy check;
   `os.replace` is never used.
3. **`done` receipt** (journal). Fsynced. This is the ownership receipt for
   the destination.
4. **Database transaction.** In **one** SQLite transaction:
   - a conditional update of the `file_locations` row and of any pairing
     record (§5.6), each matched on its stable id **and** its preimage path
     (`WHERE id = ? AND path = <old>`), each required to affect exactly one
     row;
   - an insert of a `rename_attempt_steps` row keyed by `(attempt_id,
     action_id, step)`, carrying the preimage and postimage paths.

   The marker row commits atomically with the path updates. So the database
   itself records whether this step's update has landed.
5. **`confirmed`** (journal). Fsynced.

**Recovery works on the action's frontier.** Recovery never validates a
completed step's destination as if it were current. A later step may
legitimately have moved the file and the row on. So recovery runs per
**action**:

1. **Validate the chain.** Read the action's ordered steps from the plan.
   For each step, read its journal receipts and its DB marker. The chain
   must be a **prefix-closed** sequence:
   - every step before the frontier has `done`, a DB marker and `confirmed`,
     in order;
   - no step after the frontier has any receipt or marker.

   Any gap, reordering, or receipt beyond the frontier **refuses**.
2. **Treat completed predecessors as history.** They are proven by their
   receipts and markers. Their destinations and postimages are **not**
   required to still exist, because the successor step consumed them.
3. **Find the latest proven location.**
   - **Normally** it is the last completed step's destination.
   - **For an action with no completed step**, it is the source.
   - **If the frontier step itself has `done`**, it is the frontier step's
     destination.
4. **Validate that location** with fresh content, matching the action's
   source strong identity (renames never change bytes), plus the DB rows.
5. **Recover only the frontier step**, using the matrix below. The step's
   source is the previous location, and its destination is its own target.
   Contradictory or unlisted states **refuse**.

| Journal | DB marker | Filesystem (fresh content check) | Resolution |
|---|---|---|---|
| none | — | — | nothing to do |
| `intent` only | absent | source present with the intended identity, destination absent | not done. **Re-verify the source** identity; retry, or abandon |
| `intent` only | absent | source changed | refuse; operator |
| `intent` only | absent | destination present (any content), source absent | **ambiguous**: no receipt, so even identical bytes are not owned. Operator |
| `intent` only | absent | both present, or neither | operator |
| `done` | absent | destination present, intended identity; source absent | owned. Run step 4 (the preimage CAS still matches) |
| `done` | absent | destination missing or changed | refuse; operator |
| `done` | present | destination present, intended identity | DB already committed. Verify the postimage rows; write `confirmed` |
| `done` / `confirmed` | present | path rows match neither preimage nor postimage, or are partial | refuse; operator |
| `confirmed` | present | destination intended identity | complete; nothing to do |

**Repeated recovery** is idempotent. Each resolution advances the
frontier by at most one record and re-derives it from scratch on the next
run, so re-running recovery on a resolved attempt does nothing.

**Case-only renames** are a two-step action, old → temp → new.

- **Crash after the second move.** Suppose step 1 is complete and step 2
  has `done` but no marker. The frontier is step 2 and the latest proven
  location is `new`. Recovery verifies the bytes at `new` and that the DB
  row is still at **temp**, which is step 2's preimage. It then runs step 2's
  DB transaction.
- **Crash after step 2's DB commit**, before its `confirmed` record. The
  bytes are at `new`, the row is at `new`, and the marker is present.
  Recovery writes `confirmed`.
- **In neither case** is step 1's temp destination required to still
  exist.

**Quarantine moves** use the same protocol, with the quarantine path as the
destination.

**Undo** is a new attempt, under the same scope lock, with its own journal.
It plans the **reverse action chain** of a fully confirmed forward action,
for example new → temp → old for a case-only rename, and runs it through the
same step protocol and frontier recovery.

- **Its precondition** is the forward action's complete confirmed chain,
  plus fresh content and DB rows at the forward action's final location.
- **An interrupted undo** is recovered from its own frontier, exactly like a
  forward action.
- **Anything else** is refused.

**Volumes and quarantine.** Renames never cross volumes. Supersession
removals are owned moves into a dated quarantine on the same volume.
Nothing is deleted, and retention is David's decision.

### 5.5 The watcher and import sanitizer

- **New arrivals get canonical names** through the same formatter (§6
  step 3). Otherwise the library drifts back. The watcher's
  `_apply_fallback_naming` and the `cbz_core` sanitizers move onto it.
- **The formatter must be a fixpoint.** `cbz_core.sanitize` and its
  `clean_*` helpers were probed as non-idempotent
  (`"Batman].cC"` → `"Batman.cC"` → `""`).
- **Brackets, confirmed by David on 2026-10-22** (PR #112 comment, hive
  #2175). The `"brackets"` rule (`_BRACKET_RE = \[[^\]]*\]|\([^)]*\)`):
  - **Stays for identity matching.** Otherwise `Berserk [Judas]` would miss
    the existing `Berserk` folder and split the series.
  - **The display name preserves canonical tokens:** `(YYYY)`, `[vN]`, and
    the §2.1 edition list.
  - **Scanlator and source tags** (`[Group]`, `(Digital)`) are still
    dropped.
  - **Tested** by an arrival tagged `[Group]` still resolving to the
    existing series.

### 5.6 Other interactions

- **Komga.** Whether a rename resets read progress or creates a new book is
  measured on a test library before any live run. The outcome becomes either
  an accepted cost or a progress-carrying step.
- **Decensor pairs.** The pairing between a decensored file and its
  original is keyed by stable identity (`archive_files.id` plus content
  hash). Alternatively the pairing record is updated by the same conditional
  step 4 of §5.4. A rename must never orphan it.
- **Syncthing / #108.** Renames change the paths #108 maps, so the #108
  preview is generated **after** renames from fresh evidence, never reused.
  - **Block reuse is unmeasured.** Whether Syncthing reuses blocks for
    renamed files, and how much it writes, is a **performance expectation
    only**.
  - **#108 cannot measure it.** #108's read-only filesystem contract
    verifies logical paths and bytes, not block reuse or disk-write volume.
  - **Who measures it.** If the claim ever matters for a decision, it needs
    a separate measurement with its own owner. Nothing here relies on it.

## 6. Order of work

Each step is its own PR with exact-head review. None runs against live data
without David's explicit go-ahead.

1. `series_key` fixpoint (§1.1), with the read-only key-change report and
   the index rebuild plan.
2. Page-order tie-break, the duplicate-name refusal, and hash
   re-association by entry name (§1.2).
3. Canonical formatter as a pure fixpoint function, plus the watcher and
   sanitizer convergence (§5.5).
4. Field resolution and family evidence, read-only (§3, §4.2).
5. Re-derivation and supersession evaluator, read-only, bound to strong
   identities (§4.1, §4.3–4.6).
6. Komga rename measurement on a test library.
7. `apply`/`undo`/`recover` with attempt ownership and the journal, against
   tmp fixtures only (§5.3–5.4).
8. A live dry-run plan for David's review. Then, only on his approval of a
   digest, a batched apply on Tower.

### Step 5 implementation notes

Step 5 is `comic_automation/archive/supersession.py`, which is read-only.
The design was silent on the points below, so each records the reading the
code takes. They are open to review.

1. **Stored rows are required.** §4.3 says the planner compares the
   re-derived sequence with the stored rows, but not what happens when there
   are none. The evaluator takes `archive_ids` and a database. A file with
   no id, or an id with no rows, is unresolved, so a new arrival blocks its
   family until it has been inventoried.
2. **Rows that are not comparable evidence are stale**, even when their
   digests match the bytes. This covers legacy ordering contract `"1"`, an
   unresolved stored order, and an incomplete inventory. The refusal
   reason says the rows fail the required contract. It does not claim
   their bytes differ, which is a separate reason.
3. **Dropping ComicInfo (§8 question 6, conservative reading).** With
   `accept_comicinfo_drop` (default off), A's ComicInfo.xml may be dropped
   only when B carries its own **usable** ComicInfo.xml. Usable means
   `field_resolution.comicinfo_facts` reads it as `usable` or
   `edited_template`. A missing, malformed or contradictory document (for
   example two differing ComicInfo members) does not qualify, and nor does
   the bare injected template. Otherwise the relation goes to review as
   before. An accepted drop is listed in the relation.

   **Field coverage.** David's permission is recorded on #111: "more
   accurate or more comprehensive metadata may replace old metadata". The
   same record keeps that permission separate from a *proposed mechanical
   rule*, and says accuracy is not judged automatically. Step 5 implements
   that rule as the reviewed mechanism behind the explicit drop flag. It is
   not itself David's decision, and presence alone does not establish
   accuracy.

   The mechanism is deliberately narrow; it is not an XML merger. Only
   these shapes in A's document can certify coverage, and values may
   differ in all of them:
   - **Simple top-level element:** no attributes, no children, and text
     that is non-blank after strip. It is covered when B has the same
     element with non-blank text. Free text such as Notes and Summary
     counts the same way.
   - **`Pages`**, in this shape only:
     - `Pages` has no attributes and no non-blank text;
     - its children are only `Page` elements;
     - each `Page` has no children, no text and no tail, and an `Image`
       attribute of one to six decimal digits, unique by value within
       `Pages`. The length is checked before conversion, so an oversized
       index is a refusal rather than an interpreter error.

     It is covered when every A `Page` attribute other than `Image` with a
     non-blank value is also non-blank on B's `Page` with the same `Image`
     index. B's `Pages` must be in the same supported shape.
   - **An element whose content is blank** (blank text, no attributes, no
     children) needs no cover. Neither does a blank `Page` attribute.

   **Everything else in A refuses the drop** with "unsupported ComicInfo
   shape: <path>" and goes to review:
   - any other element with attributes or children;
   - non-blank tail text anywhere;
   - text or attributes on `Pages`, or on the root;
   - a root tag that is not exactly the unqualified `ComicInfo`, in A's or
     B's document. That includes a prefixed `x:ComicInfo`, and a default
     namespace whose children reset it with `xmlns=""`. A keeper with such
     a root covers nothing;
   - a missing, blank, invalid (including more than six digits) or
     duplicate `Image` index;
   - any namespaced (`{uri}`-qualified) element or `Page` attribute. Names
     are compared raw, so `x:Bookmark` is never `Bookmark`. A namespaced
     element in the keeper's document never covers;
   - a repeated `Pages`;
   - nesting deeper than `MAX_COMICINFO_DEPTH` (8).

   The scan is iterative and stops at the depth bound, so a deeply nested
   older document is a family-local refusal that cannot stop the folder.
   An empty `<ComicInfo/>` never replaces a filled one.

   **When A's ComicInfo is inconsistent but well-formed**, it is still
   inventoried from its XML, including a field that holds contradictory
   values: inconsistency is not emptiness. **When A's ComicInfo does not
   parse**, it cannot be inventoried, so the drop goes to review.
4. **Payload identity.** The non-image payload is compared by exact raw
   member name and sha256, as a multiset. A credits file moved into a
   subfolder therefore blocks supersession.
5. **Duplicates at the top of a family.** Byte-identical duplicates there
   are two maximal members, so there is no keeper and the family goes to
   review. Duplicates below the keeper both point at it.
6. **Inserted pages.** The proposed keeper, pointers and index maps are
   reported, but the family status is `review`. Only `supersession` is
   actionable by a later step.
7. **Direction.** For unequal lengths, the shorter sequence is the older
   one. A shorter archive that is a subsequence of a longer one is reported
   as superseded with inserted pages, whatever its name claims.
8. **No ComicInfo version label yet.** §4.6 allows a label from the keeper's
   own ComicInfo. ComicInfo has no standard version element and step 4 reads
   none, so that route never fires and such a label is held for a decision.
   A held label must stay a separate rename gate in the later planner.
9. **A bare `v<N>` is the keeper's own marker** once step 4 has classified
   it as a version through the established family and the version field is
   resolved, that is, uncontradicted.
10. **Higher numbers on other members are not contradictions.** Another
    member's higher number is a note for review, never a contradiction and
    never the label (§4.6, the v2-versus-v8 example).
11. **Unreachable guards.** Relations derived from bytes cannot cycle,
    because every edge strictly lengthens the sequence, and containment is
    transitive. So the cycle guard, the missing-direct-relation guard and
    the two-established-families guard raise
    `InconsistentRelationsError`, and are proven only by tests that inject
    relations. They rest on three assumptions, which must hold for the
    guards to stay unreachable:
    - page counts strictly increase along every edge;
    - containment and the payload rule are transitive;
    - step 4's grouping gives no archive two established families.
12. **Memory bound and binding.** The full-file hash, the pages, the
    non-image members and the ComicInfo always come from one byte source
    that the evaluation owns, never from separate reads of the library
    file.
    - **At most `max_held_bytes`** (512 MiB by default), that source is one
      in-memory buffer.
    - **Larger archives** are copied into a private temp file, and the hash
      is taken over the copy stream. Everything is then derived from the
      copy alone. Afterwards the copy and its fresh `mkdtemp` directory are
      removed, best-effort. A leftover is logged at warning level with its
      path; removal is not guaranteed.
    - **Writes.** The copy path writes temp files under `temp_dir` (default
      the system temp directory), never a library file.
    - **Failure.** If the copy cannot be made (no space, an I/O error, or
      larger than an optional `max_copy_bytes`), the archive is unresolved,
      "too large to bind", and is never comparable.
    - **After-check.** Either way the library file is re-hashed at the end.
      A difference makes the archive unresolved. Equality means the
      evaluated bytes were the file's bytes when evaluation ended, so step
      7's recheck of the recorded hash rechecks the evaluated content.

    A change made and reverted during derivation therefore cannot pair one
    revision's hash with another revision's pages. That was possible in the
    earlier streamed design, refused in the #123 review. The copy is
    protected by ownership (an `O_EXCL` file in a private directory), not
    by an OS lock.
13. **Scope of failure.** One unreadable, corrupt or refused archive makes
    its own family unresolved. It does not stop the folder. Only an
    inconsistent relation set (item 11) or a concurrent database commit
    stops the run.

Perceptual hashes are not consulted at all in step 5.

### Step 7 implementation notes

Step 7 is the package `comic_automation/library/canonical_apply/`: `plan`,
`scope_lock`, `journal`, `markers`, `executor` and `cli`. It runs against
tmp fixtures only. The design was silent on the points below. Each one
records the conservative reading the code takes, and each is open to
review.

1. **The marker table is not a numbered migration.** Migration 015 is
   declared protected. The Slice 4 executor requires the ledger to equal
   exactly `{v < 15}` before it applies 015 (`slice4_migration_design.md`
   §12.1 step 3), and `tests/test_migrations.py` requires contiguous
   version numbers. So an ordinary `016` would either fail the contiguity
   sentinel, or later make every database that ran it unable to apply 015.
   - `rename_attempt_steps` and its two append-only triggers are created
     by `markers.install_schema`, or by `cli install-schema`. That is
     `CREATE ... IF NOT EXISTS` in one `BEGIN IMMEDIATE`, outside the
     ledger.
   - `apply`, `recover` and `undo` refuse a database whose table is
     missing or differs from the expected schema.
   - Bringing the table into the ledger waits until the Slice 4 numbering
     is settled.
2. **The plan format** is defined here (`canonical-naming-plan-v1`). No
   live `plan` command is built: that is step 8.
   - `plan.build_plan` and `plan.family_spec_from_evaluation` are the
     pure builders. The second takes a step-5 verdict, and only a
     `supersession` verdict is accepted.
   - `--expect-renames` counts ordinary renames only. Keeper renames are
     part of their family and are counted separately as `keeper_renames`.
   - The digest is the sha256 of the sorted, compact JSON of every key
     except `plan_digest`.
3. **Plan structure is refused before the lock.** All of these are
   refused:
   - a path that is not absolute and normalised, not under the root, or
     not a `.cbz`;
   - a path inside the canonical-naming state (the lock, the owner file,
     or the journal directory);
   - a rename that changes directory;
   - a destination that is any source, except a case-only rename's own;
   - a dependant whose relation is not a strict-prefix supersession;
   - a quarantine path outside
     `<root>\.cbz-canonical-quarantine\<YYYY-MM-DD>`.

   Quarantine paths are fixed at plan time, so the date is the plan's
   date.
4. **An archive named by two units** blocks every unit that names it.
   The plan is not refused. This is how §5.3's "no other unit schedules
   the keeper for quarantine" is enforced.
5. **Windows only.** POSIX `rename` replaces an existing destination, so
   every entry point raises `UnsupportedPlatformError` elsewhere.
6. **Quiescence has no cross-process mechanism yet.** The series locks are
   in-process watcher locks, and no operator mode exists.
   - The library API takes a required `quiescence` probe. The CLI accepts
     only the operator's `--attest-quiescent` and refuses without it.
   - The probe is checked right after the lock is taken, and again at
     every batch boundary.
   - A `unit_gate` seam is where a future series-lock wait goes.
7. **Lock waits.** Validation of a unit always runs after its gate
   returns. The SQLite write lock is the other wait: inside
   `BEGIN IMMEDIATE`, the destination is re-hashed before the conditional
   updates.
8. **Journal.** Each record is one JSON line, `fsync`ed before the caller
   goes on, and the file is created exclusively.
   - The reader refuses the whole journal on a torn final line, a parse
     error, a foreign contract or attempt id, or a gap in the sequence.
     No prefix is trusted.
   - The parent directory is not fsynced. Python on Windows cannot do it.
     This is a known gap.
9. **What recovery writes.** Each round re-derives everything from
   scratch and writes at most one record. A run repeats rounds on one
   action until its frontier step is confirmed or refused, so a single
   run settles a `done` step. `single_round` exposes one round.
   - Recovery never begins a step that has no `intent`.
   - An `intent`-only step whose source is intact is reported
     `not_done_retryable`. `apply --resume` (or `undo --resume`) retries
     it under full unit validation, reusing the durable intent, which must
     match the plan step.
   - **Abandon is not implemented.** Such an attempt stays unfinished.
10. **Completed history is still checked.** For an action that is complete
    or sits at a step boundary, recovery re-checks the content and rows at
    the proven location. Drift there is a refusal, so it also refuses a
    resume. It does not merely block the family.
11. **One unfinished attempt at a time.** A new apply or undo is refused
    while any attempt under the root is unfinished.
    - "Unfinished" is decided from journals alone: every receipt carries
      `steps_total`.
    - A journal that cannot be read counts as unfinished.
    - Re-applying a plan digest that an earlier attempt already started
      needs `--resume`.
12. **Unit or run.** A failure before an `intent` is durable blocks only
    its unit. A failure after it stops the whole run for recovery: either
    `os.rename` refusing, or the database step refusing and rolling back.
13. **Database rows.**
    - The `file_locations` row is updated in place (`path`,
      `last_seen_at`), matched on `id`, `archive_id`, the preimage path and
      `is_current = 1`.
    - A quarantined file's row stays current, at its quarantine path.
    - No `file_events`, `archive_quarantine` or migration-013 disposition
      rows are written. That is left for review.
14. **Pairing records.** No pairing table exists yet.
    - The caller registers `(table, path column)` pairs, and the plan
      names only a registered table and a row id. Identifiers never come
      from the plan.
    - The CLI registers none, so a plan carrying pairings is refused there.
15. **Durability of the database step.** The apply connection uses
    `synchronous = FULL`. Under WAL, `NORMAL` may lose the last commit,
    and the `confirmed` record follows that commit.
16. **Run-wide stop.** The defaults are a drift fraction of 0.05 and a
    batch size of 25 units. Both are conservative and uncalibrated. The
    fraction is checked at batch boundaries only, in addition to the
    per-unit checks.
17. **Undo.**
    - Every requested action is checked before anything moves. One
      failure refuses the whole undo.
    - It refuses while any forward action is part-way along its chain.
    - Actions are reversed latest first, by first intent.
    - An undo attempt cannot itself be undone.
    - A case-only undo reuses the forward attempt's temporary name.
18. **Strong identity** is always step 5's `derive_archive` sha256. That
    includes ordinary rename sources, so a rename parses the archive.
    Family members must also be comparable.
19. **Exact names.**
    - A file is "present" only when its parent directory lists exactly
      that name, because `Path.exists` ignores case on Windows.
    - A destination counts as "occupied" when anything exists at it in any
      case (`os.path.lexists`).
20. **An injection-only guard.** Phase 2 compares the renamed keeper's
    re-derived pages with phase 1's. Equal sha256 implies equal pages for
    a deterministic derivation, so the comparison is reachable only
    through an injected derivation, and it is tested that way. Phase 3
    re-validates the keeper's `file_locations` row as well as its bytes
    before every quarantine.
21. **Physical containment (PR #128 review).** The plan's path checks are
    lexical, and a junction can defeat them while every string still
    looks contained. So the executor checks physical containment
    (`containment.require_contained`) for:
    - every source, destination and temporary name;
    - the quarantine root;
    - the run state: the lock, the owner file and the journal directory.

    The check refuses a reparse point (junction, symlink or any
    `FILE_ATTRIBUTE_REPARSE_POINT`) on any existing component below the
    root, and refuses a target whose `realpath` is outside the root's.
    - The root itself may be a mapping or a share. Only what lies below it
      is constrained.
    - **When it runs:** for the whole run before the lock, again before
      each step's intent, again immediately before each `os.rename`, and
      before the quarantine directory is created.
    - **The remaining window** is between that last check and the rename.
      Closing it would need handle-relative renames, which this prototype
      does not do.
    - **UNC roots** are handled by the pure helper: shares are drives, and
      case is ignored. That is unit-tested on UNC-shaped strings with
      injected filesystem calls. **No SMB target has been measured.**
22. **Root binding.** `apply`, `recover` and `undo` act on the plan's root
    only. A `library_root` argument that does not resolve to the same
    physical directory is refused before any lock, owner file or journal is
    touched under either root. A journal header written for another root
    is refused too.
23. **Database binding.** This reuses #117's
    `content_signature_recertify.file_identity` (real path, `st_dev`,
    `st_ino`).
    - The plan records the identity as `database_target`, and every
      attempt header records it too.
    - It is verified at entry before the lock, every time a read
      connection is opened, and under the write lock before the
      conditional updates.
    - A backup clone, a copy, a hard link or a restored file is a
      different database, and needs a new plan.
24. **Attempt ids and the journal leaf (PR #128 re-review).**
    - **One grammar for every attempt id:**
      `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`, which matches what
      `executor._new_attempt_id` generates.
    - **Where it applies:** ids supplied to `apply`, `recover` and
      `undo` (including the CLI's `--resume`, `--attempt` and
      `--forward-attempt`), and ids read from a header, such as an
      undo's forward attempt.
    - **When it is checked:** before any filesystem access, and again
      inside `journal.journal_path`, so no caller can build an
      unvalidated journal path.
    - **Header action ids** must be actions of the plan.
    - **Unknown files block new attempts.** `list_attempts` returns only
      names in the grammar. Any other file in the attempts directory
      counts as unfinished and blocks new attempts.
    - **Run state is re-checked after every wait.** That covers the
      journal leaf path itself, the journal directory, the owner file,
      the lock and the quarantine root. The re-check runs:
      - after the quiescence wait;
      - immediately before a journal is created or opened, and before
        every journal read;
      - after each unit gate, where a failure stops the run;
      - under the database write lock.

    **Audit of other externally supplied paths:**
    - **Plan paths** are lexically and physically contained (notes 3
      and 21).
    - **Action and family ids** never name a path in the executor.
    - **Temp names** are built only from a validated destination and a
      validated attempt id.
    - **Header fields other than ids** (root, database target, digest,
      mode) are compared and never used as paths.
    - **Journal record fields** are compared with the plan's steps and
      never used as paths.
    - **CLI arguments:** the root is bound to the plan, the database to
      its identity, `--plan` is only read, and `--action` ids are looked
      up in the forward chain.
    - **Pairing identifiers** come from the caller's registry, never from
      the plan.

## 7. Named tests (for the implementation PRs)

Naming and resolution:
- `test_series_key_is_a_fixpoint_including_underscored_markers`
- `test_series_key_fix_changes_only_the_existing_marker_words`
- `test_page_order_is_independent_of_physical_member_order_for_unique_names`
- `test_duplicate_member_names_make_the_archive_unresolved`
- `test_reorder_reassociates_hashes_by_entry_name_not_index`
- `test_canonical_name_is_a_fixpoint`
- `test_version_marker_can_never_parse_as_a_volume`
- `test_fractional_range_and_special_numbers_round_trip`
- `test_conflicting_comicinfo_and_folder_evidence_is_unresolved`
- `test_name_evidence_alone_never_classifies_version_or_volume`
- `test_bracketed_group_tag_still_resolves_to_the_existing_series`
- `test_display_name_keeps_year_version_and_edition_tokens`

Family and comparison:
- `test_an_omnibus_containing_an_issue_is_not_its_version`
- `test_different_editions_or_languages_are_not_one_family`
- `test_overlap_without_family_evidence_is_never_compared`
- `test_an_empty_inventory_is_never_a_prefix`
- `test_an_incomplete_inventory_is_unresolved`
- `test_equal_pages_with_different_non_image_members_go_to_review`
- `test_a_unique_non_image_member_blocks_supersession`
- `test_strict_prefix_is_superseded_by_the_longer_version`
- `test_ordered_subsequence_preserves_duplicate_page_multiplicity`
- `test_chain_removal_candidates_point_at_the_final_keeper`
- `test_a_forked_version_family_goes_to_review`
- `test_perceptual_basis_nominates_but_never_proves`
- `test_decensor_pairs_are_never_versions_and_survive_rename`

Provenance:
- `test_same_size_same_mtime_replacement_is_detected_by_strong_identity`
- `test_rows_from_an_older_revision_are_stale_and_refused`
- `test_mixed_or_stale_perceptual_rows_only_nominate`
- `test_a_concurrent_database_change_rejects_the_plan`

Version labels:
- `test_keeper_keeps_its_own_verified_version_label`
- `test_other_members_version_numbers_never_relabel_the_keeper`
- `test_an_unnumbered_chain_leaves_the_label_unresolved`

Apply, drift and recovery:
- `test_apply_refuses_a_plan_whose_digest_or_counts_differ`
- `test_keeper_drift_blocks_every_dependant_in_its_family`
- `test_a_vanished_keeper_blocks_its_family`
- `test_a_keeper_scheduled_for_quarantine_blocks_its_family`
- `test_interruption_between_keeper_rename_and_dependant_quarantine_resumes`
- `test_revalidation_happens_after_a_lock_wait`
- `test_rename_refuses_an_occupied_destination_at_execution_time`
- `test_recovery_refuses_a_foreign_occupant_at_the_destination`
- `test_recovery_finishes_the_db_update_only_for_the_owned_file`
- `test_case_only_rename_crash_at_each_boundary_resolves`
- `test_quarantine_move_crash_at_each_boundary_resolves`
- `test_db_update_is_conditional_and_stops_unless_one_row`
- `test_undo_reverses_confirmed_actions_and_refuses_drifted_files`
- `test_foreign_identical_target_without_receipt_is_ambiguous`
- `test_crash_after_rename_before_done_receipt_is_ambiguous`
- `test_a_changed_source_is_refused_before_retry`
- `test_crash_after_db_commit_before_confirmation_recovers_from_the_marker`
- `test_repeated_recovery_is_idempotent`
- `test_location_and_pairing_updates_commit_atomically`
- `test_case_only_temp_step_has_its_own_db_postimage_and_undo`
- `test_recovery_takes_the_scope_lock_and_refuses_while_it_is_held`
- `test_two_recoverers_cannot_both_hold_the_scope_lock`
- `test_the_lock_is_released_by_process_exit_without_any_unlink`
- `test_case_only_crash_after_second_move_recovers_from_the_frontier`
- `test_case_only_crash_after_second_db_commit_before_confirmation`
- `test_repeated_recovery_from_either_case_only_crash_is_idempotent`
- `test_a_gap_or_receipt_beyond_the_frontier_refuses`
- `test_interrupted_undo_chain_recovers_from_its_frontier`
- `test_a_new_family_whose_keeper_needs_renaming_validates_in_phases`
- `test_crash_after_the_first_of_several_dependant_quarantines_resumes`

## 8. Open questions

1. **Inserted pages.** Should an ordered-subsequence supersession (§4.4) be
   allowed at all, or always stay a review item?
2. **Quarantine retention** for superseded versions (§5.4).
3. **Komga.** Is a read-progress reset acceptable, once measured (§5.6)?
4. **Closed lists.** The specials and edition vocabularies (§2.1, §2.2).
5. ~~Brackets~~: **closed** on 2026-10-22. David confirmed the §5.5 split.
6. **Dropping ComicInfo.** May a superseded member's ComicInfo.xml be
   dropped when the keeper carries its own (§4.4)?
