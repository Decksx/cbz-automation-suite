# Comix oneshots: identity, location, report and relocation contract (#214)

Status: draft for review by codex-lead. Docs only; nothing here is implemented yet.
Base: comic `99a21d7`, autodecen `787463a`. Source facts below were read from those
commits, not measured on the live library, unless marked *measured*.

## 1. Goal and operator rules

David approved (2026-11-19) moving single-book "series" in `X:\Comix` into one Komga
oneshots folder, `X:\Comix\_oneshots\`. He created that folder (empty) and set it as the
Comix library's `oneshotsDirectory` in Komga himself. *Measured* the same day: 19,237
top-level folders; 15,925 hold exactly one book and no subfolder (83%), 2,701 hold more
than one, 639 have subfolders, 4 are empty.

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
- Camelia (`library_backlog.py`) keys `books` by `source_path` (UNIQUE) and
  `source_key`. It sets `series_path` to the parent directory at registration, and
  `claim_job` counts related books by `(root_path, series_path)` to defer series with
  more than `DEFER_SERIES_BOOK_COUNT` books. The only path maintenance is
  `prune_missing_books` (which deletes the row and detaches its events) plus re-crawl.
  So a plain move loses state and can requeue a book that was already decensored, and
  every oneshot would share `series_path = …\_oneshots`, forming one huge pseudo-series.
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
  "oneshots": {
    "<canonical title>": {
      "relative_path": "_oneshots/<file>",
      "original_folder": "<folder name it came from, or null for a new import>",
      "aliases": ["…"],
      "size": 123, "mtime_ns": 456,
      "operation_id": "<relocation op that last wrote this entry>"
    }
  }
}
```

It is written atomically (temp file plus replace, as `_atomic_json` does), and only by the
relocation primitive (section 6). `build_registry` reads it and emits one series entry per
record with `location.kind = "oneshot"`. If a record's file is missing, the entry is
reported stale and left unresolved (no silent fallback), and the record is never deleted
by a rebuild.

**Registry v2.** `_series_registry.json` gains `version: 2` and a `location` object per
series. `resolve_from_registry` returns `location` alongside `relative_path`; for folder
series `relative_path` is unchanged, so v1 consumers keep working for folder series.
`_resolve_import_target` (slice 2) accepts `kind == "oneshot"` and routes to promotion
instead of rejecting a two-component path.

## 4. Classification (planner, slice 1)

Each top-level folder gets exactly one class:

- **`oneshot`**, which moves. All of these must hold:
  - exactly one book (`.cbz .cbr .zip .pdf .epub`) and no subdirectory
  - no other files except sidecars the plan explicitly carries (`series.json`; anything
    else means `review`)
  - no explicit number (R1) in the file name, the folder name, or the book's
    `ComicInfo.xml` `Number`/`Volume` fields
  - not a reparse point, readable, and the target path is safe and free
- **`numbered_single`**: one book, but numbered (R1). It stays in its folder; no move.
- **`series`**: more than one book, or subfolders. It stays.
- **`review`**, which never moves. Each item carries machine-readable `reasons`:
  - `ambiguous_number`: a bare trailing number that may be a title or year ("1984",
    "Mob Psycho 100"), or a number only in ComicInfo that disagrees with the file name
  - `extra_files`, `unreadable`, `reparse_point`, `target_collision`,
    `metadata_conflict`, `unsafe_path`, `reserved_name`
  - `active_camelia_job`: the row is claimed or running
  - `unknown_reference`: the path is referenced by a state store the planner does not know

**Number evidence (R1):** the token families are `part`/`pt`, `chapter`/`ch`,
`volume`/`vol`/`v` followed by digits, `issue`/`#`, `episode`/`ep`, `第N話`/`第N巻`/`N巻`,
and `Act N`. A match anywhere, including `1` and `01`, means numbered. A match that the
planner can't tell from part of a title goes to `review` with `ambiguous_number`, never to
`oneshot`.

**Target name:** `_oneshots/<original file name>`. If the stem is empty or not
descriptive (`.cbz`, `1.cbz`), or the name collides with another planned or existing
file, the planner proposes `<canonical title>.cbz` and marks the item `renamed`. If that
name also collides, the item goes to `review` with `target_collision`. Nothing is
overwritten.

## 5. Versioned plan report

`comix-oneshots-plan/1`, written as JSON plus a human summary:

```json
{
  "schema": "comix-oneshots-plan/1",
  "generated_at": "…", "tool": "<name> <commit>",
  "library_root": "X:\\Comix",
  "inventory_digest": "sha256 over sorted (relative_path, size, mtime_ns) of every file",
  "counts": {"oneshot": 0, "numbered_single": 0, "series": 0, "review": 0},
  "bytes": {"oneshot": 0},
  "review_reasons": {"ambiguous_number": 0, "...": 0},
  "items": [{
    "series": "<canonical title>", "alias_key": "…",
    "class": "oneshot", "reasons": [], "renamed": false,
    "original_folder": "<name>", "source": "<rel path>", "target": "_oneshots/<file>",
    "size": 0, "mtime_ns": 0, "number_evidence": null,
    "state_changes": {
      "camelia": [{"book_id": 1, "state": "completed", "source_path": "…"}],
      "registry": "folder -> oneshot",
      "references": [{"store": "…", "field": "…", "count": 0}]
    }
  }],
  "plan_digest": "sha256 of the canonical JSON of items"
}
```

The planner is read-only, including the Camelia DB, which it opens with `mode=ro` and
the WAL-aware single-read pattern from CLAUDE.md. Apply takes `--plan`,
`--expected-count` and `--plan-digest`. It refuses if any of the three differ, or if the
live inventory no longer matches the plan's items (size, mtime, presence, empty targets).

## 6. Shared relocation primitive (used by slice 2 promotion and slice 3 apply)

`relocate(operation_id, moves[])`, where each move is `(source, target, series, new_location)`.
This is a recoverable multi-resource operation, not one transaction: X:, F:, JSON and
SQLite cannot commit together.

1. **Intent.** Append a journal record (operation id, moves, pre-state evidence: size,
   mtime and Camelia row id/state) to `_automation/relocations.jsonl` before touching
   anything. A repeated operation id resumes instead of re-running.
2. **Fence.** Take the existing library writer lock. Ask Camelia to fence the source and
   target paths (section 7): no claim, no registration, and refusal if a fenced book is
   running.
3. **Filesystem move.** Use a same-volume rename, so the bytes are unchanged. Verify the
   target's size and mtime against the plan, and record a per-move receipt.
4. **Camelia relocate** (section 7); record its receipt.
5. **Location and registry.** Update `_series_locations.json` (add, remove or change the
   entry), then regenerate the registry.
6. **Other references.**
   - Append relocation mappings to stores that hold book paths: canonical-naming and
     quarantine journals, and the inspection catalog if it holds the path.
   - Never rewrite historical receipts; append `old → new` mappings that readers apply.
   - A store that isn't enumerated in the planner's `references` list blocks apply
     (`unknown_reference`).
7. **Release** the fence and append the completion receipt.

**Restart or reconcile.** From the journal plus observation (file at source or target,
Camelia row path, location entry), finish the forward path or report a conflict.
Nothing is guessed. **Conditional undo** applies only when the target still matches its
receipt (size, mtime), the Camelia row is unchanged since its receipt, and no later
operation references the series. Otherwise undo refuses and says why. Once new imports
have landed against a moved series, undo for that series is reported as impossible, not
attempted.

## 7. Camelia contract (autodecen repo)

- **`series_key`, separate from `series_path`.** `series_key` is the logical grouping
  used by `claim_job`'s related-book count. For books under `_oneshots`, `series_key` is
  `oneshot:<source_key>`, so each oneshot is its own group. Otherwise it is the parent
  directory, which keeps today's behaviour. The migration backfills
  `series_key = series_path`. Registration, relocation, list/report and claim all use the
  same function to derive it.
- **`relocate_book(operation_id, old_path, new_path)`:**
  - keeps `books.id`, `state`, attempts, retry, hold and queue fields, and `book_stages`
    / `book_phases` rows
  - updates `source_path`, `source_key`, `series_path`, `series_key`, and `output_path`
    when it lies under the old location
  - appends a `relocated` event; old event text is not rewritten
  - is idempotent by `operation_id`, and refuses if the row is claimed or running, or if
    `new_path` already has a row
- **Fencing.** A `relocation_fences` table (path, operation id) is checked by
  `claim_job` and by `record_archive`. A fenced path is not claimed, and is not
  registered as new, until the fence is released.
- **Crawl after relocate** finds the row by its new `source_path`. Because size and
  mtime are unchanged, it doesn't re-inspect or requeue; a completed book stays
  completed.
- **Fixtures required**, each with a mutation check of its guard:
  - a completed book moved and then crawled is not requeued
  - a queued book keeps its place
  - a held book stays held
  - a running book is refused
  - a competing claim and a competing crawl during the fence
  - a fault between the filesystem move and the DB receipt
  - a target-row collision, and a repeated operation id
  - conditional undo
  - more than `DEFER_SERIES_BOOK_COUNT` unrelated oneshots alongside one real multi-book
    series, so that grouping and priority are unchanged
  - `_oneshots` books are discovered and processed

## 8. Watcher (slice 2, codex-lead) obligations from this contract

- **New series, unnumbered book:** route to `_oneshots/<file>` and write its location
  record through `relocate` (an import is a move from intake).
- **New series, numbered book (R1):** create a series folder, as today.
- **Match to a `oneshot` location, distinct new book:** promote. Create
  `X:\Comix\<canonical>`, relocate the oneshot into it, then import the new book, all
  under one operation id. The location becomes `folder`.
- **Duplicate or replacement** of the oneshot (the same book per `settle_file_conflict`
  evidence) is not a promotion.
- **Two books for a new series in one batch** go straight to a folder.
- **Concurrency:** arrivals for one series are serialized. Alias ambiguity or a target
  collision holds the import for review, and never overwrites or loses the first book.
- **Fixtures:** registry rebuild and restart, an interrupted promotion, and preservation
  of aliases and state references.

## 9. Cutover (slice 3 apply and slice 4 Komga, claude-lead)

- **Preconditions:**
  - the first full F: backup has completed and `F:\comics-backup\Comix` matches `X:\Comix`
    (size and mtime parity)
  - David has reviewed the slice-1 report and separately authorized apply for a specific
    plan digest
- **Stop:** pause the watcher, Camelia, the "Comic Backup" scheduled task, any active
  Syncthing pair for Comix, and Komga scanning (record its scan interval to restore).
- **Apply** the plan through `relocate`. Then apply the identical relative renames to
  `F:\comics-backup\Comix`, so that the next mirror is a no-op and the backup's
  10%-to-holding guard is never bypassed.
- **Verify** before resuming anything: the source inventory equals the plan's target
  inventory, F: matches X:, and Camelia rows and paths reconcile. The backup script's dry
  listing must show zero changes.
- **Komga:**
  - record the version, the Comix library ID, the current `oneshotsDirectory` (already
    `_oneshots`, set by David), `hashFiles`, and baseline book and series counts
  - rescan, then reconcile book counts and old→new paths, and check for duplicates,
    orphaned empty series from vacated folders, and representative read progress and
    metadata
  - **rollback:** restore the prior setting and undo the plan if needed (section 6
    limits apply)
- **Resume** in reverse order only after every inventory reconciles.

## 10. Out of scope and accepted limits

- **Manga and Graphic Novels** are not touched.
- **Multi-book series are never collapsed** into `_oneshots`, and `_oneshots` books are
  never renamed beyond the collision rule in section 4.
- **Interim:** until slice 2 lands, a registry rebuild indexes `_oneshots` as a series
  named `_oneshots`. The folder is empty and no title normalizes to `oneshots` that we
  know of, but the reservation in section 3 is the fix.
- **Komga read progress** for moved books depends on Komga matching moved files; David
  accepted progress resets (2026-10-22). The checks in section 9 look for duplicates and
  orphans, not lost progress.
