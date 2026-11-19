# Comix oneshots: identity, location, report and relocation contract (#214)

Status: draft for review by codex-lead. Docs only; nothing here is implemented yet.
Base: comic `99a21d7`, autodecen `787463a`. Source facts below were read from those
commits, not measured on the live library, unless marked *measured*.

Revision 2 answers codex-lead's review 7024765340 of revision 1 (`7182414`). Each of the
seven findings is closed by an explicit decision, marked **[F1]** to **[F7]** where it
lands. Revision 3 answers the re-review 7025222877 of revision 2 (`f9277c0`):

- the relocation sequence and tombstones, plus fence reconciliation after a completed
  journal (F1, section 7);
- frozen provenance versus persistent preconditions in the plan (F6, section 5);
- the aggregate backup guard and backup-visible control files (F4, sections 8 and 9);
- explicit R1 precedence in the arrival classes (section 8).

## 1. Goal and operator rules

David approved (2026-11-19) moving single-book "series" in `X:\Comix` into one Komga
oneshots folder, `X:\Comix\_oneshots\`. He reported creating that folder (empty) and
setting it as the Comix library's `oneshotsDirectory` in Komga himself; neither has been
independently checked yet. *Measured* the same day: 19,237 top-level folders; 15,925
hold exactly one book and no subfolder (83%), 2,701 hold more than one, 639 have
subfolders, 4 are empty.

His rules (issue comment 7023971642), which bind every slice:

- **R1. Numbered books never go to `_oneshots`.** A book with an explicit part, chapter,
  volume, issue or episode number, including 1, stays in (or gets) a series folder,
  even when it is its series' only book.
- **R2. Promotion.** When a new, distinct book makes a oneshot part of a series, the
  oneshot moves out of `_oneshots` into a series folder in `X:\Comix\` with the new book.
- **R3. Camelia.** Its queue follows every move, and it keeps processing books in
  `_oneshots`.

Nothing moves until David has reviewed the slice-1 report. Reviewing the report is not
authorization to apply it; apply (slice 3) asks separately, citing the plan digest.

## 2. Today's behaviour this contract has to change (source facts)

- `series_resolver.build_registry` makes every top-level directory a canonical series
  (excluding `_quarantine_*`, `.stfolder`, `.stversions`, `_automation`). Identity *is*
  location: `canonical_title == relative_path == folder name`. Today `_oneshots` would be
  indexed as a series called `_oneshots`.
- `cbz_watcher._resolve_import_target` refuses any resolver result whose `relative_path`
  is not a single path component, and treats a missing folder as stale evidence.
- So moving a book out of its folder deletes that series' identity on the next registry
  rebuild. Its next chapter would create a fresh folder, splitting the series and leaving
  the first book in `_oneshots`, which breaks R2.
- The watcher imports from an intake directory that defaults to `C:\Temp\cbz_processing`,
  a different volume from `X:`. Its existing move code deliberately handles that with a
  protected partial copy, verification and commit rather than a rename into the visible
  library.
- `settle_file_conflict` recognises exact-byte duplicates; for any other collision it
  keeps the larger file and quarantines the other. It says nothing about whether two
  different files are the same book.
- Camelia (`library_backlog.py`) keys `books` by `source_path` (UNIQUE) and
  `source_key`. It sets `series_path` to the parent directory at registration, and
  `claim_job` counts related books by `(root_path, series_path)` to defer series with
  more than `DEFER_SERIES_BOOK_COUNT` books. The only path maintenance is
  `prune_missing_books` (which deletes the row and detaches its events) plus re-crawl.
  So a plain move loses state and can requeue a book that was already decensored, and
  every oneshot would share `series_path = …\_oneshots`, forming one huge pseudo-series.
- `_reuse_unchanged` reuses a crawled row only when its size and mtime match **and**
  `eligibility_version >= QUEUE_PRIORITY_VERSION`; an older row is re-inspected, which
  resets its queue state.
- The crawl recurses into `_oneshots`: it is not in `EXCLUDED_DIRECTORY_NAMES` and has no
  `_quarantine_` prefix. R3's processing half already holds and must be pinned by a test.

## 3. Identity and location

**Series identity is independent of where its books are.** A series is identified by its
canonical title, the existing folder name for folder series, plus its aliases, exactly
as the registry does today. Location is a separate attribute:

| `location.kind` | `location.relative_path` | meaning |
|---|---|---|
| `folder` | `<Series>` | today's model: a top-level series folder |
| `oneshot` | `_oneshots/<file>` | the series' single book lives in the container |

**Reserved names.** `_oneshots` is a container, never a series: `build_registry`, the
#108 observer's series inventory and the watcher's resolver all exclude it as a series
name and never use it as an alias target. `_series_locations.json` (below) is reserved
as a control file at the library root, like `_series_registry.json`.

**Durable per-book location records: `X:\Comix\_series_locations.json`.** This file
survives registry rebuilds and is the only source of oneshot identity. A file name in
`_oneshots` is not reliable evidence; for example, a measured folder holds a book named
just `.cbz`. Format:

```json
{
  "version": 1,
  "generation": 42,
  "oneshots": {
    "<canonical title>": {
      "relative_path": "_oneshots/<file>",
      "original_folder": "<folder name it came from, or null for a new import>",
      "aliases": ["…"],
      "size": 123, "mtime_ns": 456, "sha256": "…",
      "operation_id": "<relocation op that last wrote this entry>"
    }
  }
}
```

If a record's file is missing, the entry is reported stale and left unresolved (no silent
fallback), and the record is never deleted by a rebuild.

**[F1] Shared metadata writes.** `_series_locations.json` and `_series_registry.json` are
whole-file replacements shared by every series, so per-series serialization is not
enough: two series updated concurrently could each replace the file and lose the other's
change. Every read-modify-write of either file:

- holds one **global metadata lock** (`_automation/metadata.lock`, the same advisory
  lock primitive the library writer lock uses), and
- is a **compare-and-swap on `generation`**: the writer reads `generation` under the
  lock, writes the new content with `generation + 1` via temp file, fsync and replace,
  and refuses if the on-disk generation changed in between.

The registry records the locations generation it was built from. A loaded resolver
(watcher, observer) compares that against the file's current generation before using a
cached lookup and reloads when they differ, so a long-running watcher never routes from a
stale view.

**Registry v2.** `_series_registry.json` gains `version: 2`, its own `generation`, and a
`location` object per series. `resolve_from_registry` returns `location` alongside
`relative_path`; for folder series `relative_path` is unchanged, so v1 consumers keep
working for folder series. `_resolve_import_target` (slice 2) accepts `kind == "oneshot"`
and routes to promotion instead of rejecting a two-component path.

## 4. Classification (planner, slice 1)

Each top-level folder gets exactly one class:

- **`oneshot`**, which moves. All of these must hold:
  - exactly one book (`.cbz .cbr .zip .pdf .epub`) and no subdirectory
  - **no other file at all** **[F5]**. In v1 every sidecar, including `series.json`, is
    `review` with `extra_files`; no sidecar is ever moved. A later revision may define
    namespaced sidecar ownership, metadata precedence and undo, but v1 does not guess.
  - no explicit number (R1) in the file name, the folder name, or the book's
    `ComicInfo.xml` `Number`/`Volume` fields
  - not a reparse point, readable, and the target path is safe and free
  - its Camelia row, if any, is not active and not at an older eligibility version
    (see section 7, **[F7]**)
- **`numbered_single`**: one book, but numbered (R1). It stays in its folder; no move.
- **`series`**: more than one book, or subfolders. It stays.
- **`review`**, which never moves. Each item carries machine-readable `reasons`:
  - `ambiguous_number`: a bare trailing number that may be a title or year ("1984",
    "Mob Psycho 100"), or a number only in ComicInfo that disagrees with the file name
  - `extra_files`, `unreadable`, `reparse_point`, `target_collision`,
    `metadata_conflict`, `unsafe_path`, `reserved_name`, `duplicate_normalized_path`
  - `active_camelia_job`: the row is claimed or running
  - `stale_camelia_version`: the row's `eligibility_version` is below
    `QUEUE_PRIORITY_VERSION`
  - `unknown_reference`: the path is referenced by a state store the planner does not know

**Number evidence (R1):** the token families are `part`/`pt`, `chapter`/`ch`,
`volume`/`vol`/`v` followed by digits, `issue`/`#`, `episode`/`ep`, `第N話`/`第N巻`/`N巻`,
and `Act N`. A match anywhere, including `1` and `01`, means numbered. A match that the
planner can't tell from part of a title goes to `review` with `ambiguous_number`, never to
`oneshot`.

**Target name [F5]:** `_oneshots/<original file name>`. If the stem is empty or not
descriptive (`.cbz`, `1.cbz`), or the name collides with another planned or existing
file, the planner proposes `<canonical title><original suffix>`, keeping the book's own
format (`.cbr` stays `.cbr`, `.pdf` stays `.pdf`), and marks the item `renamed`. If that
name also collides, the item goes to `review` with `target_collision`. Nothing is
overwritten.

**Unsafe paths:** absolute or `..` components, reparse points anywhere on the path,
reserved Windows names, trailing dots or spaces, and two items whose normalized
(NFC, casefolded) source or target collide all go to `review`.

## 5. Versioned plan report

`comix-oneshots-plan/3`, written as JSON plus a human summary.

**[F6] What the digest binds.** `plan_digest` is the SHA-256 of the canonical JSON
(UTF-8, `sort_keys`, separators `(",", ":")`, items sorted by NFC-casefolded `source`) of
the whole **approved report**, not just the items. The report has two kinds of field
(revision 3):

- **Frozen provenance** (`generated_at`, `tool`, `diagnostics`): part of the digest, so
  the approved document can't be altered, but never re-derived. Apply reads them from
  the approved report.
- **Persistent preconditions** (`library_root`, `state_stores` identities and
  generations, `inventory_digest`, `expected_move_count`, every item's source,
  target, size, SHA-256 and expected Camelia row and references): apply validates each
  against live state.

SQLite `data_version` is connection-local, so it appears only under `diagnostics` as the
planner's own before/after read-stability check. It is never compared across processes.

```json
{
  "schema": "comix-oneshots-plan/3",
  "policy_version": "r1-r3/2026-11-19",
  "generated_at": "…", "tool": "<name> <commit>",
  "diagnostics": {"camelia_data_version_before": 0, "camelia_data_version_after": 0},
  "library_root": {"path": "X:\\Comix", "volume_serial": "…", "root_file_id": "…"},
  "state_stores": {
    "camelia": {"path": "…", "instance_uuid": "…"},
    "registry_generation": 0, "locations_generation": 0
  },
  "inventory_digest": "sha256 over sorted (relative_path, size, mtime_ns, sha256) of every candidate book",
  "expected_move_count": 0,
  "counts": {"oneshot": 0, "numbered_single": 0, "series": 0, "review": 0},
  "bytes": {"oneshot": 0},
  "review_reasons": {"ambiguous_number": 0, "...": 0},
  "items": [{
    "series": "<canonical title>", "alias_key": "…",
    "class": "oneshot", "reasons": [], "renamed": false,
    "original_folder": "<name>", "source": "<rel path>", "target": "_oneshots/<file>",
    "size": 0, "mtime_ns": 0, "sha256": "…", "number_evidence": null,
    "state_changes": {
      "camelia": [{"book_id": 1, "state": "completed", "source_key": "…",
                   "eligibility_version": 0}],
      "registry": "folder -> oneshot",
      "references": [{"store": "…", "field": "…", "count": 0}]
    }
  }],
  "plan_digest": "sha256 of the canonical JSON of every field above except plan_digest"
}
```

Changing the root, a store identity, the schema, the policy or any item changes the
digest. The planner refuses to emit duplicate normalized sources or targets.

**Read-only.** The planner opens the Camelia DB with `mode=ro` and the WAL-aware
single-read pattern from CLAUDE.md, and reads every candidate's bytes once to hash them.

**[F6] Content evidence.** Size and mtime are diagnostics, not identity: a writer can
replace a file with one of the same size and mtime. Every candidate carries its SHA-256,
and apply, undo and backup parity all compare SHA-256. A mismatch refuses the affected
item (and the plan, if the count or digest no longer holds).

**Apply** takes `--plan`, `--expected-count` and `--plan-digest`. It does not regenerate
the report. It:

1. recomputes `plan_digest` over the approved report file and refuses on any mismatch
   with `--plan-digest` (a tampered report, root, policy or item);
2. checks `expected_move_count` against `--expected-count`;
3. validates every persistent precondition against live state: same root path, volume
   serial and root file id; same Camelia `instance_uuid`; same registry and locations
   generations; per item, the same Camelia row id, state, `source_key` and eligibility
   version, the same references, the same SHA-256 at the source, and a free target.

Any failed precondition refuses the plan. Fixtures: a fresh-process preview then apply
with no changes **succeeds**; a changed affected row, root identity, generation or source
content each **refuses**.

## 6. Relocation and import primitives

Two primitives, because moving inside the library and importing from intake are
different operations **[F2]**:

- **`relocate`** moves books that are already in the library, between library paths on
  the same volume, by rename. Bytes never change.
- **`publish_import`** brings a new book from intake into the library. It keeps the
  watcher's existing cross-volume protocol unchanged (protected partial copy, verify,
  commit, then source disposal under the existing claim, hold and disposal rules); this
  contract only changes the target path and adds the location record. A new import has
  **no Camelia row** and `publish_import` never creates one: Camelia registers the book
  on its next crawl, deriving `series_key` with the shared function in section 7.

A watcher promotion uses both under one parent operation: `relocate` for the existing
oneshot, then `publish_import` for the new book.

### 6.1 Journal and identities [F1]

- Journal: `_automation/relocations.jsonl`, append-only, fsynced after each record.
- **Operation:** `operation_id` plus `payload_sha256`, the digest of the operation's full
  move list. Reusing an operation id with a different payload refuses.
- **Move:** each move has its own `move_id = <operation_id>:<index>`, with the index fixed
  by the sorted move list. Receipts and `relocate_book` key on `move_id`, so two books in
  one promotion or one bulk batch never satisfy each other's idempotency check.

### 6.2 Steps

1. **Intent.** Append the intent record (operation id, payload digest, every move with
   its `move_id`, and pre-state: size, mtime, SHA-256, Camelia row id, state and
   eligibility version) before touching anything.
2. **Lock and fence, in a fixed order.**
   1. the library writer lock;
   2. the Camelia fence (section 7): **every** normalized source and target key of the
      operation, acquired atomically in one `BEGIN IMMEDIATE` transaction that refuses if
      any key is already fenced, or any affected row is claimed or processing;
   3. the global metadata lock (section 3), taken only around each metadata write.

   Locks are always taken in this order and released in reverse, so promotion and bulk
   apply cannot deadlock each other.
3. **Filesystem move.** Rename source to target. Verify the target's size and SHA-256
   against the intent, and append a per-move receipt.
4. **Camelia relocate** (section 7), per move; append its receipt.
5. **Location and registry.** Update `_series_locations.json`, then regenerate the
   registry, each under the metadata lock with the generation CAS (section 3).
6. **Other references.**
   - Append relocation mappings to stores that hold book paths: canonical-naming and
     quarantine journals, and the inspection catalog if it holds the path.
   - Never rewrite historical receipts; append `old → new` mappings that readers apply.
   - A store that isn't enumerated in the planner's `references` list blocks apply
     (`unknown_reference`).
7. **Complete, then unfence.** Append the completion record and fsync it, commit the
   Camelia fence release in the same transaction that records the operation as
   completed, and only then release the library writer lock. A crash before this step
   leaves the fences in place.

### 6.3 Restart, reconcile and undo [F1]

- **Fences outlive crashes.** An operation without a completion record keeps its fences
  and its paths stay unclaimable, unregistrable and unprunable until it is reconciled.
- **Recovery ownership.** The tool that wrote the intent owns recovery: the watcher for
  promotions, the apply tool for bulk. On start, each owner reconciles its incomplete
  operations before doing new work. Reconcile reads the journal plus observation (file
  at source or target and its SHA-256, Camelia row path, location entry) and either
  finishes the forward path or stops with a conflict report. Nothing is guessed.
- **Conditional undo** applies only when the target still matches its receipt (SHA-256),
  the Camelia row is unchanged since its receipt, and no later operation references the
  series. Otherwise undo refuses and says why. Once new imports have landed against a
  moved series, undo for that series is reported as impossible, not attempted.

## 7. Camelia contract (autodecen repo)

- **One grouping function [F7].** `derive_series_key(root_path, source_path)` is the only
  way `series_key` is computed:
  - under `<root>/_oneshots/` it returns `oneshot:<source_key>`, so each oneshot is its
    own group;
  - otherwise it returns `<root identity>|<normalized parent directory>`, which keeps
    today's grouping.

  Registration, relocation, list/report, claim **and the migration backfill** all call
  it. The backfill does not copy `series_path`: a pre-existing `_oneshots` row would
  otherwise land in exactly the pseudo-series this function exists to prevent.
- **Older eligibility versions [F7].** `_reuse_unchanged` re-inspects rows below
  `QUEUE_PRIORITY_VERSION`, which would requeue a moved book. `relocate_book` refuses
  such rows, and the planner marks them `review` with `stale_camelia_version`. They move
  only after Camelia has re-verified them in place.
- **`relocate_book(move_id, old_path, new_path)`:**
  - keeps `books.id`, `state`, attempts, retry, hold and queue fields,
    `eligibility_version`, and `book_stages` / `book_phases` rows
  - updates `source_path`, `source_key`, `series_path`, `series_key`, and `output_path`
    when it lies under the old location
  - appends a `relocated` event; old event text is not rewritten
  - is idempotent by `move_id`, and refuses if the row is claimed or processing, if the
    path is not fenced by this operation, or if `new_path` already has a row
  - returns `no_row` (not an error) when the source has no backlog row
- **Fences [F1].** A `relocation_fences` table holds `(normalized key, operation_id)`
  with the key UNIQUE.
  - Acquisition is atomic for the whole operation (section 6.2).
  - `claim_job` checks fences **inside its write transaction**, so a claim can't slip in
    between a check and the update.
  - `record_archive` checks them in **both** of its write transactions (the
    pre-inspection lookup and the final commit, since ZIP inspection happens between
    them), so a path fenced mid-registration is not committed.
  - `prune_missing_books` refuses fenced paths, so a book mid-move is never deleted as
    missing.
- **Relocation sequence and tombstones [F1, revision 3].** Fence checks alone miss a
  registration whose ZIP inspection straddles a **whole** relocation: the fence can be
  acquired, the book moved and the fence released while the inspector is paused, so the
  final registration transaction sees no fence and would insert a fresh row at the old
  path. So:
  - a control row holds a monotonically increasing `relocation_seq`;
  - every `relocate_book` commit increments it and, in the same transaction, writes a
    durable tombstone `(old normalized key, operation_id, move_id, seq, moved_to)` for
    the old path. Tombstones are never deleted;
  - `record_archive` captures `relocation_seq` in its first transaction. Its final
    transaction refuses (and leaves the path for the next crawl) if the key is fenced
    **or** has a tombstone with `seq` greater than the captured value. A later crawl that
    finds a real file at a tombstoned path registers it as a genuinely new book: the
    tombstone stops only registrations whose evidence predates the move.
- **Crash between journal completion and the DB unfence [F1, revision 3].** Startup
  reconciliation scans **both** sides: unfinished journal operations, and every row in
  `relocation_fences`. A fence whose operation the journal already records as complete is
  checked against the move receipts (row path, tombstone, location entry) and released
  only if they match; a fence with no journal intent at all is reported as an orphan and
  left fenced for the operator.
- **Crawl after relocate** finds the row by its new `source_path`. Because the bytes,
  size and mtime are unchanged and the eligibility version is current, it reuses the row:
  a completed book stays completed.
- **Fixtures required**, each with a mutation check of its guard:
  - a completed book moved and then crawled is not requeued
  - a queued book keeps its place; a held book stays held; a running book is refused
  - a competing claim and a competing crawl during the fence, including a registration
    whose ZIP inspection straddles fence acquisition, and the stronger barrier: the
    inspector is held until the move **and the unfence** complete, then allowed to
    commit. No stale row at the old path and no overwritten state at the new path.
  - a crash after the journal completion fsync but before the DB unfence commit: startup
    reconciles the outstanding fence from the receipts
  - prune during an in-flight relocation
  - two series updating the locations file concurrently, with neither change lost
  - two books in one operation, retried after a crash: each move applies exactly once
  - a crash after the filesystem move but before the DB receipt, then restart: fences
    retained until reconcile
  - a target-row collision, a repeated operation id with the same payload (resumes) and
    with a different payload (refused)
  - conditional undo, including a same-size, same-mtime replacement detected by SHA-256
  - an upgrade with more than `DEFER_SERIES_BOOK_COUNT` existing unrelated oneshots plus
    one real multi-book series: backfill, registration, relocation, report and claim all
    agree, and grouping and priority are unchanged
  - an older-eligibility-version row is refused
  - `_oneshots` books are discovered and processed

## 8. Watcher (slice 2, codex-lead) obligations from this contract

**[F3] Identity before collision policy.** When an arrival matches a series whose
location is `oneshot`, the watcher first classifies the arrival against the existing
book:

The classes are evaluated **in this order**, and the first match wins:

| order | class | evidence | action |
|---|---|---|---|
| 1 | `numbered_duplicate` | the arrival is numbered (R1) **and** byte-identical to the oneshot | hold for review: the oneshot may itself be a numbered book under a generic name; never routed to `_oneshots` |
| 2 | `distinct` | the arrival is numbered (R1) | promotion |
| 3 | `exact_duplicate` | identical SHA-256, arrival unnumbered | existing duplicate disposal; no promotion |
| 4 | `ambiguous` | anything else: different bytes, both unnumbered | hold for review; no promotion, no overwrite |

R1 is checked first, so no overlap between the predicates can route a numbered arrival
to `_oneshots`.

v1 deliberately has no automatic "same book, better copy" replacement: two different
unnumbered files with the same generic name are not evidence of one book.
`settle_file_conflict` runs only **after** this classification, and only for a genuine
same-name collision inside a series folder. R1 takes precedence: a numbered arrival is
never treated as a replacement.

- **New series, unnumbered book:** `publish_import` to `_oneshots/<file>` and write its
  location record.
- **New series, numbered book (R1):** create a series folder, as today.
- **Promotion** (`distinct` against a `oneshot`): create `X:\Comix\<canonical>`,
  `relocate` the oneshot into it, then `publish_import` the new book, all under one
  parent operation. The location becomes `folder`.
- **Two books for a new series in one batch** go straight to a folder.
- **Concurrency:** arrivals for one series are serialized, and the locks follow
  section 6.2's order. Alias ambiguity or a target collision holds the import for
  review, and never overwrites or loses the first book.
- **[F4] Backup during normal use (revision 3).** A promotion moves exactly one existing
  book and makes no F: change, so the next "Comic Backup" mirror sees one deletion and
  one addition, plus changed `_series_registry.json` and `_series_locations.json`, which
  the script does not exclude. The old copy goes to the 30-day `_holding`.
  `Backup-Comics.ps1` decides its holding guard from the **aggregate** of every extra and
  overwritten file since the last run, so promotions add to a total that Camelia
  replacements and other imports also feed. This contract makes **no** claim that
  promotions stay below the threshold:
  - the watcher never forces or bypasses the guard, and never widens the backup's
    exclusions;
  - each promotion journals its expected backup contribution (one book plus the two
    control files), so a guard refusal can be explained;
  - a guard refusal is reported for the operator to decide, as for any other cause.

  Fixture: several promotions plus unrelated replacements between two backup runs,
  checked against the script's real listing logic, with the guard outcome reported and
  never forced.
- **Fixtures:** cross-volume intake into `_oneshots` and into a promotion; a new import
  with no Camelia row; same file name but different books (held); an exact duplicate
  (not promoted); a numbered arrival with a generic name (promoted); registry rebuild and
  restart; an interrupted promotion; preservation of aliases and state references.

## 9. Cutover (slice 3 apply and slice 4 Komga, claude-lead)

- **Preconditions:**
  - the first full F: backup has completed, and `F:\comics-backup\Comix` matches
    `X:\Comix` by SHA-256 for every planned source
  - David has reviewed the slice-1 report and separately authorized apply for a specific
    plan digest
- **Stop:** pause the watcher, Camelia, the "Comic Backup" scheduled task, any active
  Syncthing pair for Comix, and Komga scanning (record its scan interval to restore).
- **[F4] One parent journal for both roots.** The cutover is one parent operation with
  three receipted phases, each resumable and each reconciled before the next:
  1. X: moves and state stores, through `relocate`;
  2. F: phase, two kinds of step:
     - **book renames:** for each planned move, an F: intent record, a check that the F:
       source exists with the planned SHA-256 and the F: target is free, a rename, and a
       per-file receipt;
     - **backup-visible control files (revision 3):** the X: phase changes files the
       backup copies (`_series_registry.json`, `_series_locations.json`, and anything
       under `_automation` the script doesn't exclude). Each one the X: phase changed is
       copied to F: as its own journalled step, verified by SHA-256 against X:, with a
       receipt. The set is enumerated in the parent journal from the X: phase's own
       receipts, not guessed;
  3. verification: X: inventory equals the plan's targets, F: matches X: by SHA-256 for
     every moved book and every enumerated control file, and Camelia rows and paths
     reconcile.

  The parent is **not complete**, and the backup task stays disabled, until all three
  phases reconcile. Recovery resumes a partial F: phase from its receipts. A missing F:
  source or an occupied F: target stops the operation with a report; it is never
  overwritten or skipped silently. Conditional undo works per root with the same
  SHA-256 receipts.

  **The zero-change check is scoped, not assumed.** Before re-enabling the backup, run
  the script's real listing logic. The expected result is zero changes, because every
  book rename and every enumerated control file was already applied to F:. Any listed
  difference stops the resume with a report. The backup's exclusions are never widened
  to make this pass, and with the watcher and Camelia stopped no other writer should
  appear. Fixture: run that listing scope against a fixture X:/F: pair after a cutover,
  including the control files.
- **Komga:**
  - record the version, the Comix library ID, the current `oneshotsDirectory` (reported
    as `_oneshots`), `hashFiles`, and baseline book and series counts
  - rescan, then reconcile book counts and old→new paths, and check for duplicates,
    orphaned empty series from vacated folders, and representative read progress and
    metadata
  - **rollback:** restore the prior setting and undo the plan if needed (section 6.3
    limits apply)
- **Resume** in reverse order only after every inventory reconciles.

## 10. Out of scope and accepted limits

- **Manga and Graphic Novels** are not touched.
- **Multi-book series are never collapsed** into `_oneshots`, and `_oneshots` books are
  never renamed beyond the collision rule in section 4.
- **Sidecars (v1):** any folder with a sidecar is review-only and stays where it is.
- **Interim:** until slice 2 lands, a registry rebuild indexes `_oneshots` as a series
  named `_oneshots`. The folder is empty and no title normalizes to `oneshots` that we
  know of, but the reservation in section 3 is the fix.
- **Komga read progress** for moved books depends on Komga matching moved files; David
  accepted progress resets (2026-10-22). The checks in section 9 look for duplicates and
  orphans, not lost progress.
- **Watcher promotions leave one old copy per promotion in the backup's `_holding`**
  until it expires, and add to the backup's aggregate holding guard alongside every other
  change (section 8). The guard is never bypassed for them.
