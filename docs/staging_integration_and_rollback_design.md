# Watcher-to-staging integration (inert) and verified cross-volume rollback (design)

Status: **design only**, for review. Nothing here is wired, implemented, or
activated; accepted watcher modes remain `off` and `shadow`. Roadmap "Next
work" item 3 (`docs/implementation_roadmap.md`): "the inert watcher-to-staging
integration plan and a verified cross-volume rollback design". Author:
claude-lead; reviewer: codex-lead. Written 2026-10-22 at master `cd2b774`.

## 0. Two different "stagings" — kept apart

| | Classification staging (this design) | Routine staging |
|---|---|---|
| Module | `scripts/cbz_classification_staging.py` (#33, #38, #40–#42; retries #94) | `scripts/cbz_promote_staging.py` (#73) |
| Purpose | hold an arrival whose classification needs **human review** | land routine arrivals in `X:\_staging\*`, then merge into the library |
| Safety | manifest first; full-SHA inventory; source revalidation; recovery matrix; digest-checked operator commands | plan-then-`--apply`; no manifest, hash, or rollback |
| Wired into the watcher | **no** (no production import) | the live `routing.json` points the watcher at `X:\_staging\*` |

"Staging" below always means the classification-staging review machinery.
The routine path is not changed by this design.

## 1. The constraint that shapes both halves

Measured from code and records at `cd2b774` (sources in each line):

- The watch folder is on **C:** (`cbz_watcher.py:68`, `WATCH_FOLDER`); the
  library and `X:\_staging\*` are on **X:** (`development_log_2026-08-24.md`).
- Today's watcher move is `shutil.move` (`_move_cbz_dir`, `_merge_directories`):
  across C:→X: that is a silent copy-then-delete, with no manifest, hash, or
  post-copy verification.
- Classification staging already **supports** cross-volume transfer in
  (`execute_transfer`: copy into `.partial`, verify full-SHA inventory,
  revalidate the source, publish `.partial → pending`; method chosen by
  `st_dev`, `validate_execution_plan` refuses a contradicting method).
- It **refuses** cross-volume promote (`_same_volume_move`) and cross-volume
  rollback (`rollback_case`: "no verified cross-volume rollback protocol
  exists", `cbz_classification_staging.py:2054-2069`).

So the natural review root (`X:\_classification_review`, documented but not
configured anywhere) makes **every** watcher handoff cross-volume in (C:→X:),
promote same-volume (X:→X:, supported), and **rollback cross-volume back to
C:, which is refused**. Integration without part B would create review cases
that can never be returned. That is why the two are one design.

## Part A — Inert watcher-to-staging integration plan

### A1. Where the handoff would sit

Inside `_process_and_move_directory_inner`, under the existing
`stable_series_lock` (#43, already wired at `cbz_watcher.py:1756`), after
`_shadow_route` has classified the arrival and **before** `_move_cbz_dir` /
`_move_loose_files`. That is the only point where identity is settled (#32),
the v2 classification exists, and nothing has moved.

The decision it would make, later and only under a separately approved
activation: **a review-required classification is handed to staging instead
of the legacy move; everything else keeps the legacy path unchanged.**
"Review-required" is defined by the v2 router (`authoritative == False`, an
ambiguous key, or an unresolved identity under #99's gate), never by
destination priority.

### A2. Phases (each its own PR and review; none activates routing)

1. **Shadow-plan (inert, first).** At the handoff point, when the router is in
   `shadow` and the classification is review-required, call the staging
   *planner* only (`plan_case` / inventory of the arrival, read-only) and log
   what would be staged: would-be case id, inventory digest and file count,
   chosen method (`copy_verify` C:→X:), and the configured review root. No
   copy, no `.partial`, no manifest. Count these in the shadow pass summary.
   This measures how often review would trigger before anything depends on
   it.
2. **Configuration gate.** The review root becomes an explicit, validated
   setting (no default): it must exist, be on the library volume (so promote
   stays a same-volume rename), and not be inside the watch folder, the
   library roots, or `X:\_staging`. A missing or invalid setting keeps phase 1
   logging "review root unset" and nothing else.
3. **Activation (not designed here).** Any real handoff is a separate,
   separately reviewed decision, gated on part B existing and on the #99
   authority work. It needs its own mode value; the current
   `MODE_OFF`/`MODE_SHADOW` set does not grow in this design.

### A3. Rules the eventual handoff must keep

- **Source release.** `execute_transfer(delete_copied_source=False)` is the
  default and must stay so on handoff: the C: arrival is the only rollback
  target until part B can return bytes from X:. To stop the watcher
  re-detecting it, the arrival is moved **within C:** (same-volume rename)
  into a `handed-off/<case-id>` holding area recorded in the manifest. Only a
  successful promote may release it, and only by quarantine-then-delete,
  never by `rmtree` (CLAUDE.md "Quarantine, don't delete").
- **Lock scope.** The copy runs under the series lock (same-series arrivals
  wait, which is the #32 serialization) but **never** under the router index
  lock (`_index_lock`, a plain `RLock`; lock_topology.md rule: series → index,
  never the reverse). `note_move` is **not** called for a handed-off arrival:
  it did not land, so the index must not learn a home for it.
- **One owner per arrival.** An arrival handed to staging is never also given
  to `_move_cbz_dir`; the handoff and the legacy move are exclusive branches,
  with a test that removing the branch guard fails.
- **No silent fallback.** If staging refuses (collision, partial recovery
  required, source changed), the arrival stays where it is and is reported;
  the watcher does not fall back to the legacy move for a review-required
  classification.

## Part B — Verified cross-volume rollback

### B1. What exists

`rollback_case` returns a `pending_review` payload to `staging_source_path`
by atomic rename, and refuses if that path is occupied or on another volume
(tested: `test_rollback_reports_cross_volume_unavailability_explicitly`). A
promoted case is terminal and cannot be rolled back (out of scope here; an
"un-promote" would be its own plan/apply).

### B2. The protocol: a reverse transfer, verified before anything is released

Cross-volume rollback is the transfer protocol run backwards, with the same
guarantees and no new kind of trust:

```text
precondition   case is pending_review; operator supplies record_digest
               (show_case) — same gate as every operator command
1 plan         manifest records state rollback_planned: target = original
               staging_source_path (or the A3 holding area), method
               copy_verify, the payload's recorded inventory digest
2 copy         copytree pending/<case>/payload → <target-volume>
               .rollback-partial/<case>/ (same volume as the target)
3 verify       full-SHA inventory of .rollback-partial == the manifest's
               recorded inventory digest (not merely == the pending copy)
4 revalidate   re-hash pending/<case>/payload: unchanged since promotion
               to pending (PayloadChangedError retryable, a different
               digest is operator_required — never guessed)
5 publish      same-volume rename .rollback-partial/<case>/payload →
               target; refuse if target occupied (no merge, no overwrite)
6 record       manifest: rolled_back, with both digests and paths
7 release      move pending/<case> to rolled_back/<case> on X: (quarantine,
               not delete); its eventual deletion is a separate,
               operator-approved retention step
```

Nothing is removed from either volume until the returned copy is verified and
published (steps 1–6). After step 7 there are still two verified copies: the
returned arrival and the quarantined staging copy.

### B3. Crash and retry semantics

States extend the existing recovery matrix: `rollback_planned`,
`rollback_copied_verified`, `rolled_back` (terminal). Recovery acts only on
what disk and manifest together prove:

| Found | Action |
|---|---|
| `rollback_planned`, no `.rollback-partial` | restart from step 2 |
| `.rollback-partial` incomplete or digest mismatch | discard **only** `.rollback-partial` (it is a copy; pending is intact), restart step 2 |
| `.rollback-partial` verified, target absent | resume at step 4 |
| target present and equal to the recorded digest, manifest not yet `rolled_back` | record (step 6), then release (step 7) — idempotent |
| target present with **different** content | `operator_required`; touch nothing |
| pending payload changed | `operator_required`; touch nothing |

Renames use the existing `_replace` retry (WinError 5/32 only, bounded,
re-checking after each wait, #94). Every step is idempotent, and recovery is
rebuildable from manifest plus disk alone, like `recover_case` today.

### B4. Limits, stated

- The integrity check is the full-SHA inventory digest; the known
  `(st_size, st_mtime_ns)` window for a same-size rewrite inside the mtime
  quantum (`_hash_stable_file`) applies to the source re-check exactly as it
  does on transfer.
- Both volumes must be local NTFS. SMB targets stay refused (engineering
  decision "Library volume filesystem and access path are architectural":
  0/6 detection over `\\tower`); exFAT widens the quantum and is refused for
  the same reason.
- Not an atomic cross-volume operation: between steps 5 and 7 the bytes exist
  on both volumes, by design.

## C. What implementation must prove (named tests, for later PRs)

Part A:
- `test_shadow_plan_logs_would_be_case_and_moves_nothing`
- `test_shadow_plan_is_skipped_for_authoritative_classifications`
- `test_missing_or_invalid_review_root_logs_and_does_nothing`
- `test_review_root_inside_watch_or_library_roots_is_refused`
- `test_handoff_and_legacy_move_are_exclusive` (guard removal fails it)
- `test_handed_off_arrival_does_not_update_the_series_index`
- `test_staging_refusal_never_falls_back_to_legacy_move`
- `test_copy_never_holds_the_router_index_lock`

Part B:
- `test_cross_volume_rollback_returns_verified_bytes`
- `test_rollback_verify_compares_to_the_recorded_digest_not_the_copy`
- `test_rollback_refuses_an_occupied_target_without_merging`
- `test_nothing_is_released_before_publish_and_record`
- `test_released_payload_is_quarantined_not_deleted`
- each recovery-table row as its own test
- `test_changed_pending_payload_is_operator_required`
- `test_smb_or_exfat_target_is_refused`

Every cross-volume test in the suite today fakes volumes by monkeypatching
`volume_identity` / `same_volume`. That stays for unit tests, but part B also
needs **one opt-in integration test on two real local volumes** (for example
a temporary directory on C: and one on a second local volume, skipped when
none exists), because a faked `st_dev` cannot prove copy-then-rename
behaviour across real filesystems. It is operator-run, never against the
library.

## D. Open questions

1. **Handoff holding area** (A3): rename within C: to `handed-off/`, or keep
   the arrival in place and teach the watcher to skip paths with an open
   case? The first is simpler to reason about; the second avoids a move.
2. **Review-root location**: `X:\_classification_review` (library volume, so
   promote stays same-volume) is assumed; confirm, and confirm it must not
   be under `X:\_staging`.
3. **Release of the handed-off source after promote**: quarantine retention
   period and who approves deletion.
4. **Order of work**: part B (rollback) before any part A phase beyond
   shadow-plan, since without it every review case is irreversible.
