# Canonical file naming and WIP-version supersession (design)

Status: **design only**, for review. Nothing here is implemented, and no
live file is renamed, moved, or removed by this document. Tracks issue
#111. Author: claude-lead. Reviewer: codex-lead.

## Operator requirements

All from David, 2026-10-22, in the terminal and recorded on #111:

- **Every archive gets a name that identifies it at a glance.**
  - With volumes: `Series Name v01 #003 (2019).cbz`.
  - Without volumes: `Series Name ch. 1 (2019).cbz`.
- **"v" is ambiguous today.** Many WIP books use `v` for **version**, not
  volume. A later version usually contains every earlier page with new pages
  appended, so earlier versions may be overwritten.
- **The version designation is kept** in the surviving book's name.
  Because `v01` already means volume in the format above, the version marker
  must look different. This design proposes `[v3]`, for example
  `Series Name ch. 1 (2019) [v3].cbz`; David confirms or changes it at
  review.

## Non-goals

- No rename, move, removal, quarantine, ComicInfo rewrite, or database
  write is implemented or authorized here.
- No change to the V1 perceptual hashes (frozen), to routing activation, or
  to the Syncthing configuration. The Tower → X: merge (#108) stays the
  final step.
- Re-encoding or repacking archives is out of scope. A rename changes the
  path only, never the bytes.
- Canonical series records (Phase 4) are not designed here. This document
  consumes whatever series identity has been decided and refuses to guess
  when none has.

## 1. Prerequisites that must land first

Both are measured defects. The Step 8 property tests pin them as strict
xfails.

1. **`series_key` is not idempotent.** Today `Berserk_Uncensored` becomes
   `berserk uncensored` and then `berserk`. `Berserk Uncensored` becomes
   `berserk` directly. So one series splits by spelling. A renamer built on
   the current rule would write that split into filenames.
   - **The fix comes first**, in its own reviewed PR. It must make the key a
     fixpoint, so that `series_key(series_key(x)) == series_key(x)`.
   - **Then the index is rebuilt.** Because the fix changes keys, the series
     index and every key-derived record are rebuilt, with a before/after
     key-collision report reviewed by David.
2. **Page order depends on physical member order when two names sort
   equal.** Examples are `a.png` and `A.png`, or `01.png` and `1.png`. Two
   packings of the same pages can then yield different sequences, which
   would make the supersession comparison in §4 unstable.
   - **The fix** is a deterministic tie-break on the raw member name after
     the natural sort key. That is the simulated fix that made the xfail
     pass.
   - **Signature migration.** Content signatures computed under the old
     order are recomputed. The V1 perceptual hashes themselves are per page,
     so they are unaffected.

## 2. The canonical name

### 2.1 Grammar

```text
<series> [v<VV>] [#<III> | ch. <C>] [(<year>)] [[v<N>]] .cbz
```

| Part | Rule |
|---|---|
| `<series>` | **Display name.** Human-cased and readable. It is never derived from `series_key`, which is a lossy identity key for matching, never for display. Comes from the decided identity (§3). |
| `v<VV>` | **Volume**, at least two digits (`v01`). Present only when the volume is established (§3.2). |
| `#<III>` | **Issue** within a volume, at least three digits (`#003`). |
| `ch. <C>` | **Chapter**, for books without volumes. Written unpadded, in David's form `ch. 1`. |
| `(<year>)` | Four-digit year, when established. |
| `[v<N>]` | **Version**, for WIP books only (§4.5). Bracketed so it can never be read as a volume. |

### 2.2 Numbers that are not plain integers

- **Fractional numbers** keep their fraction: `#012.5` and `ch. 12.5`.
- **Ranges** stay ranges: `ch. 1-3` for a combined release, and `v01-02`.
- **Specials, extras, omake and one-shots** carry a word instead of a
  number: `Series Name Special (2019)` and `Series Name Extra 2`. The
  vocabulary is a closed list, and anything outside it goes to review.
- **Editions and language variants** go in a trailing parenthetical:
  `(Uncensored)`, `(Colored)`, `(English)`.
  - These words are exactly the marker words that `series_key` strips. That
    is correct for the key, which must still match the base series. For the
    name, the words must survive.
  - `cbz_core.preserve_decensor_markers` is the existing precedent for
    keeping them.

### 2.3 Path-safety and collisions

- **Illegal Windows characters** are replaced: `<>:"/\|?*`, trailing dots
  and spaces, and the reserved device names. Names are capped at a length
  that keeps the full path under the platform limit.
- **Collisions** are refused. If two archives would receive the same
  canonical name in the same directory, neither is renamed: the pair goes to
  review. A real duplicate is §4's business, not the renamer's.
- **Case-only renames** on NTFS (`batman.cbz` → `Batman.cbz`) go through a
  temporary name, because a direct rename is a no-op on a case-insensitive
  volume.

## 3. Where series, volume, issue and year come from

### 3.1 Precedence

Each field is resolved independently. A field with conflicting evidence is
**unresolved**, never averaged or picked.

1. **A recorded human decision.** These come from the decision manifest of
   `series_identity_decision_manifest.md` once it exists. Until then this
   source is absent.
2. **ComicInfo.xml** in the archive: `Series`, `Volume`, `Number`, `Year`.
   It counts only when it is internally consistent and not a template. The
   watcher's injected placeholder template is not evidence.
3. **Folder context.** The resolved series folder (`stable_series_lock`
   identity) and siblings that are numbered consistently.
4. **The current filename.** It is a candidate only, never decisive on its
   own for volume-versus-version (§3.2).

An archive whose series or primary number is unresolved is **not renamed**.
It is listed in the plan's review section with the evidence behind each
candidate.

### 3.2 Volume versus version

A `v<N>` token in a current name is classified as follows:

- **Volume** when ComicInfo `Volume` agrees, or when siblings form a volume
  run, and the page content of the candidates differs (they are different
  books).
- **Version** when the page evidence of §4 shows that one archive's pages
  are an ordered prefix or subsequence of another's in the same series.
- **Unresolved** otherwise. That covers two cases: no ComicInfo and no
  page-hash coverage, or evidence that conflicts.

Name evidence alone never classifies.

## 4. Superseded-version rule

### 4.1 Page sequences

The sequence compared is the archive's **current-revision page list in
fixed order**:

- the natural sort with the §1.2 tie-break,
- images only, excluding `ComicInfo.xml` and other non-image members.

Each element is the page's digest under one stated basis:

- **Exact basis:** `page_hashes.algorithm = 'sha256'`. This is byte
  equality, and it is the only basis that can prove supersession.
- **Perceptual basis:** V1 `dhash` at its frozen parameters, with the
  distance threshold stated in the plan. It may **nominate** candidates
  (re-encoded or recompressed versions); it never proves supersession.

**Provenance.** Every comparison records, for both archives:

- `archive_files.id` and `current_revision_id`,
- the page-inventory freshness (`archive_content_signatures.source_file_size`
  and `source_modified_time_ns` against the file now),
- the algorithm and version.

Page rows are keyed by `archive_id`, not by revision. So a comparison whose
page rows do not describe the archive's current bytes is **stale** and is
refused, not used.

### 4.2 Relations

For exact sequences `A` and `B` in the same decided series:

| Relation | Definition | Outcome |
|---|---|---|
| **equal** | `A == B` as sequences | an exact duplicate; the existing exact-duplicate tooling decides the keeper, not this rule |
| **strict prefix** | `len(A) < len(B)` and `B[:len(A)] == A` | A is superseded by B |
| **strict ordered subsequence** | A is a subsequence of B, preserving order **and multiplicity**, but not a prefix | A is superseded by B, flagged as an *inserted pages* case and shown in review |
| **anything else** | pages changed, removed, or reordered | **review**, never automatic |

**Multiplicity.** A page that appears twice in A must appear at least twice
in B, in order. The match is a greedy left-to-right walk over B, which
yields the exact index map from A's pages to B's. That map is written to the
plan, so the reviewer sees where every old page lives in the new book.

### 4.3 Chains and the final keeper

The relations form a graph within a series:

- **The keeper** is the **maximal** element: a node superseded by nothing.
- **One maximal element:** every other member of the chain is a removal
  candidate pointing at that keeper, never at an intermediate version.
- **Two or more maximal elements** (a fork: v3 and v3-alt both extend v2):
  the whole family goes to review.
- **Cycles cannot occur** under strict relations. If one is ever observed,
  it means the inventory was stale or corrupt, and the run stops.

### 4.4 What is never treated as a version

- **Decensor pairs.** A decensored archive and its censored original in
  `F:\ai-decensor-originals` are a pair. Their pages differ by design, they
  are never compared under this rule, and their mapping survives any rename
  (§5.4).
- **Archives in different decided series**, even with identical pages.
  That is a cross-series duplicate and goes to review.
- **Archives whose page inventory is missing, stale, or incomplete.** These
  are unresolved.

### 4.5 Naming the keeper

The surviving keeper gets its canonical name plus `[v<N>]`. N is the
highest version number observed in the family's current names. Where the
names carry no number, N is the keeper's ordinal in the chain.

## 5. Plan, approval and apply

The safety model follows the repository's guarded-operation rules: dry run
first, report first, an expected count plus a digest, quarantine rather than
delete, and stop when the evidence disagrees.

### 5.1 The plan

`plan` is read-only. It writes one JSON plan containing:

- every proposed rename (old path → new path, with the evidence per field);
- every supersession (removal candidate → keeper, relation, index map,
  basis, provenance);
- every review item with its reason;
- counts per section;
- the canonical bytes' **plan digest**.

It never renames or writes anywhere except its own output file.

### 5.2 Approval and freshness

- **Approval.** `apply` requires `--plan <file> --expect-digest <d>
  --expect-renames <n> --expect-supersessions <m>`. David's approval is of
  that exact digest.
- **Freshness.** `apply` re-proves every row before acting on it: same size,
  same mtime, and a re-hashed full SHA-256 for supersession rows.
  - **A row that drifted is skipped** and reported, never re-planned
    silently.
  - **Too much drift stops the whole run.** If skipped rows exceed a stated
    fraction, nothing further is applied.
- **Batches.** `apply` runs in bounded batches, so a long run can be stopped
  between them.

### 5.3 Journal, undo, crash safety

- **Journal first.** Each rename is journalled *before* it happens (intent)
  and confirmed after (done).
- **Database update.** The database path update (`file_locations`) happens
  in the same per-file transaction as the journal confirmation.
- **Crash recovery.** After a crash, each intent is resolved by looking at
  the disk:
  - old path present, new path absent → not done;
  - new path present, old path absent → done, so finish the database row;
  - both present or neither → stop for the operator.
- **Undo.** `undo --journal <file>` reverses confirmed renames in reverse
  order, with the same freshness checks.
- **Same volume only.** Renames never cross volumes, so a rename never
  becomes a copy plus delete.
- **Supersession removals are moves into a dated quarantine** on the same
  volume, with the same journal and undo. Nothing is deleted. The
  quarantine's retention is David's decision.

### 5.4 Interactions

- **Komga.** Komga identifies books by path and file metadata. A rename may
  reset read progress or create a new book entry.
  - **This must be measured** on a small test library before any live run.
  - **The outcome** becomes either an accepted cost, or a step that carries
    progress across through Komga's API.
- **Decensor pairs.** The pairing between a decensored file and its original
  must be keyed so a rename does not break it. Either it moves to a stable
  identifier (`archive_files.id` and content hash), or the journal rewrites
  the pairing record in the same transaction as the rename.
- **Syncthing / #108.** Renaming on Tower before the Tower → X: sync is
  compatible with Syncthing's block reuse, but it changes the paths #108
  maps. The #108 preview must be generated **after** the renames, from fresh
  path evidence, never reused from before them.
- **The watcher.** New arrivals must receive canonical names at ingest using
  the same rules. Otherwise the library drifts back.
  - **Shared code.** The watcher's naming (`_apply_fallback_naming` and the
    `cbz_core` sanitizers) moves onto the same naming function.
  - **Probed defect.** `cbz_core.sanitize` and its `clean_*` helpers were
    probed as non-idempotent (`"Batman].cC"` → `"Batman.cC"` → `""`). The
    shared function must be a fixpoint.

## 6. Order of work

Each step is its own PR with exact-head review. None runs against live data
without David's explicit go-ahead.

1. Fix `series_key` idempotence (§1.1). Also produce the key-change report
   (read-only) and the index rebuild plan.
2. Fix the page-order tie-break (§1.2) and recompute content signatures.
3. Canonical-name formatter as a pure function. It must be a fixpoint, with
   property tests over the Step 8 generators, and the watcher moves onto it.
4. Field resolution and volume/version classification, read-only, emitting
   the plan's rename and review sections.
5. Supersession evaluator, read-only, emitting the plan's supersession
   section.
6. Komga rename measurement on a test library.
7. `apply`/`undo` with the journal, against tmp fixtures only.
8. A live dry-run plan for David's review. Then, only on his approval of a
   digest, a batched apply on Tower.

## 7. Named tests (for the implementation PRs)

- `test_series_key_is_a_fixpoint_including_underscored_markers`
- `test_page_order_is_independent_of_physical_member_order`
- `test_canonical_name_is_a_fixpoint`
- `test_version_marker_can_never_parse_as_a_volume`
- `test_fractional_range_and_special_numbers_round_trip`
- `test_edition_markers_survive_in_the_name_but_not_the_key`
- `test_conflicting_comicinfo_and_folder_evidence_is_unresolved`
- `test_name_evidence_alone_never_classifies_version_or_volume`
- `test_strict_prefix_is_superseded_by_the_longer_version`
- `test_ordered_subsequence_preserves_duplicate_page_multiplicity`
- `test_equal_sequences_are_duplicates_not_supersession`
- `test_reordered_or_changed_pages_go_to_review`
- `test_chain_removal_candidates_point_at_the_final_keeper`
- `test_a_forked_version_family_goes_to_review`
- `test_perceptual_basis_nominates_but_never_proves`
- `test_stale_page_inventory_is_refused`
- `test_decensor_pairs_are_never_versions_and_survive_rename`
- `test_name_collision_renames_neither`
- `test_case_only_rename_goes_through_a_temporary_name`
- `test_apply_refuses_a_plan_whose_digest_or_counts_differ`
- `test_drifted_rows_are_skipped_and_excess_drift_stops_apply`
- `test_crash_between_rename_and_db_update_resolves_from_disk`
- `test_undo_reverses_confirmed_renames_in_reverse_order`
- `test_supersession_quarantines_on_the_same_volume_and_deletes_nothing`

## 8. Open questions

1. **Version marker.** Is `[v3]` the marker David wants (§2.1), or another
   form such as `(ver. 3)`?
2. **Inserted pages.** Should a strict ordered subsequence that is not a
   prefix (§4.2) be automatic at all, or always review? This design makes
   it a flagged supersession, shown in review.
3. **Quarantine retention** for superseded versions (§5.3).
4. **Komga.** Is a read-progress reset acceptable, once measured (§5.4)?
5. **Specials vocabulary.** The closed list of words for §2.2.
