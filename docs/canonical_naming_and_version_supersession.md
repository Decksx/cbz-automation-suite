# Canonical file naming and WIP-version supersession (design)

Status: **design only**, for review. Nothing here is implemented, and no
live file is renamed, moved, or removed by this document. Tracks issue
#111. Author: claude-lead. Reviewer: codex-lead.

- **First draft:** `de4c757`.
- **This revision** answers codex-lead's review of that head. It covers
  five contracts: version-family evidence, current-byte provenance,
  dependency-aware drift, owned durable recovery, and verified version
  labels. It also makes two source corrections.

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
- **Bracket stripping and canonical tokens.** David asked for the import
  sanitizer to stop stripping brackets. Because the same cleaned name drives
  series matching on import, this design keeps bracket stripping for
  **identity matching** but preserves canonical tokens in the **display**
  name. The details are in §5.5 and are pending David's confirmation on #112.

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

### 5.3 Dependency-aware revalidation

Per-row skipping is unsafe for supersession. If keeper B drifts or vanishes
while A passes its own check, quarantining A would lose pages. So:

- **The unit of action is a family**, not a row. Before any removal in a
  family, `apply` revalidates, in one pass:
  - the keeper's strong identity and presence at its expected
    post-rename path;
  - every candidate's strong identity;
  - each approved relation, by re-deriving the sequences from the bytes;
  - that the keeper is not itself scheduled for quarantine by another unit.
- **Any failure blocks the whole family.** A drifted, missing or
  unverifiable keeper blocks all its dependants, whatever the run-wide drift
  fraction.
- **When to revalidate:** immediately before the family's first action,
  after every lock wait, and again at each batch boundary.
- **An ordinary rename** is its own unit, revalidated against its source
  strong identity.
- **Ordering inside a family:** the keeper's rename (if any) is applied and
  confirmed before any dependant is quarantined. An interruption between
  them leaves the family resumable, never half-quarantined against a keeper
  that never landed.
- **A run-wide drift fraction** still stops the run early. It is an
  additional stop, never a substitute for per-family revalidation.

### 5.4 Owned, durable recovery

**Ownership.** A run is an **attempt** with a unique id. Mutations are
exclusive while it runs: there is one attempt lock, and the watcher and the
other mutators are quiesced for the target library roots, using the
existing series-lock and operator-mode mechanisms.

**Journal.** The journal is an append-only file, one per attempt, written
with `fsync` per record. A file journal and a SQLite row cannot share a
transaction, so the protocol is ordered and idempotent instead. For each
action:

1. **Intent record:** the attempt id, the action id, the source strong
   identity, and the old, temp (if any), new or quarantine paths, with
   expected bytes. Fsynced.
2. **Filesystem step** with `os.rename`, which on Windows **refuses an
   existing destination**. That refusal is the execution-time occupancy
   check, never `os.replace`. A case-only rename is two steps: old → owned
   temp `<new>.<attempt>.tmp`, then temp → new. Each step has its own intent
   and done records.
3. **Done record** for the filesystem step. Fsynced.
4. **Database update** (`file_locations` and any pairing record) as a
   **conditional** update, `UPDATE … WHERE path = <old> AND <identity
   matches>`. It must affect exactly one row, otherwise it stops.
5. **Confirmed record.** Fsynced.

**Recovery** walks each incomplete action and **verifies ownership by
content**: the full-file hash at the destination must equal the intent's
source strong identity.

| State found | Resolution |
|---|---|
| source present, destination absent | not done; may be retried |
| destination present **with the intended identity**, source absent | filesystem step done; finish the database update and confirm |
| destination present with **any other identity** | a foreign occupant: stop, never touch it |
| both present, or neither | stop for the operator |

The same matrix covers the temp step of a case-only rename and quarantine
moves.

**Undo** replays confirmed actions in reverse, with the same ownership
verification. It refuses foreign occupants and drifted files.

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
- **Brackets, per David's request.** The `"brackets"` rule
  (`_BRACKET_RE = \[[^\]]*\]|\([^)]*\)`) removes `(2019)`, `(Uncensored)`
  and `[v3]`. Proposed (pending David's confirmation):
  - Bracket stripping stays for **identity matching**. Otherwise
    `Berserk [Judas]` would miss the existing `Berserk` folder and split
    the series.
  - The **display** name preserves canonical tokens: `(YYYY)`, `[vN]`, and
    the §2.1 edition list.
  - Scanlator and source tags (`[Group]`, `(Digital)`) are still dropped.
  - Tested by an arrival tagged `[Group]` still resolving to the existing
    series.

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
  Syncthing's ability to reuse blocks for renamed files is to be
  **confirmed** by #108's own preview and verifier, not assumed here.

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

## 8. Open questions

1. **Inserted pages.** Should an ordered-subsequence supersession (§4.4) be
   allowed at all, or always stay a review item?
2. **Quarantine retention** for superseded versions (§5.4).
3. **Komga.** Is a read-progress reset acceptable, once measured (§5.6)?
4. **Closed lists.** The specials and edition vocabularies (§2.1, §2.2).
5. **Brackets.** Does David confirm the identity/display split of §5.5, or
   want bracket stripping fully off despite the series-split risk?
6. **Dropping ComicInfo.** May a superseded member's ComicInfo.xml be
   dropped when the keeper carries its own (§4.4)?
