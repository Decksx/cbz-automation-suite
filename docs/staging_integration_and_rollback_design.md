# Watcher-to-staging integration (inert) and verified cross-volume rollback (design)

Status: **design only**, for review. Nothing here is wired, implemented, or
activated; accepted watcher modes remain `off` and `shadow`. Roadmap "Next
work" item 3 (`docs/implementation_roadmap.md`). Author: claude-lead;
reviewer: codex-lead. First draft `002270d` (2026-10-22, master `cd2b774`);
this revision answers codex-lead's review of that head (PR #103): a typed
handoff decision and excluded mixed parents, a journalled retained source,
ownership receipts instead of content equality, and versioned journals with
a complete crash matrix.

## 0. Two different "stagings" — kept apart

| | Classification staging (this design) | Routine staging |
|---|---|---|
| Module | `scripts/cbz_classification_staging.py` (#33, #38, #40–#42; retries #94) | `scripts/cbz_promote_staging.py` (#73) |
| Purpose | hold an arrival whose classification needs **human review** | land routine arrivals in a staging destination, then merge into the library |
| Safety | manifest first (`classification-case-v1`, strict parser); full-SHA inventory; source revalidation; recovery matrix; digest-checked operator commands | plan-then-`--apply`; no manifest, hash, or rollback |
| Wired into the watcher | **no** (no production import) | yes, via the live routing destinations |

"Staging" below always means classification staging. The routine path is
not changed by this design.

## 1. Topology is an input, verified at runtime — never assumed

The 2026-08-24 handoff and the code defaults describe the watch folder on C:
and the library on X:, which would make a handoff a cross-volume
`copy_verify`, a promote a same-volume rename, and a return to the source a
cross-volume move. **Those are historical and default examples, not a current
measurement**, and different drive letters alone do not prove different
volumes. The design is therefore conditional on topology, and the eventual
operator preflight must verify, at runtime and for each configured root:
volume identity (`st_dev` / volume serial), access path (local, not SMB), and
filesystem (NTFS; exFAT and SMB refused — engineering decision "Library
volume filesystem and access path are architectural"). The staging module
already chooses `rename` vs `copy_verify` by `st_dev` and refuses a plan
whose method contradicts the recorded volumes; this design keeps that rule
and adds nothing that infers volumes from letters.

What is source-backed at `cd2b774`:

- the watcher moves with `shutil.move` (`_move_cbz_dir`, `_merge_directories`
  / `_move_loose_files`): across volumes that is copy-then-delete without
  manifest, hash, or verification;
- `execute_transfer` supports cross-volume transfer in (copy into `.partial`,
  verify the full-SHA inventory, revalidate the source, publish);
- `_same_volume_move` refuses cross-volume promote, and `rollback_case`
  refuses cross-volume return ("no verified cross-volume rollback protocol
  exists", `cbz_classification_staging.py:2054-2069`) and an occupied return
  target.

## Part A — Inert watcher-to-staging integration plan

### A1. A typed handoff decision, not the shadow log

`_shadow_route()` today is a logging adapter: it returns `None` and catches
classification errors. A handoff needs a typed result, produced at the same
point (under the wired `stable_series_lock`, after identity is settled, before
any move):

```text
HandoffDecision
  outcome      legacy_move | review_handoff | defer
  reason       classification_failed | mixed_parent | ...  (for defer)
  classification   the v2 decision when one was produced
  payload      the exact set of member paths the handoff would own
```

- A classification **error** yields `defer` with the error recorded: the
  arrival is left in place and reported, never moved by either path.
- **Review policy is proposed here, not existing API.** The router has no
  "review-required" call. This design proposes: `review_handoff` when the
  key is ambiguous or unresolved under #99's gate. `authoritative == False`
  alone is **not** the trigger, because it also covers otherwise resolved
  metadata classifications. The policy itself needs review before
  implementation.

### A2. Mixed parents are excluded until a selective-payload contract exists

The watcher has two move branches. A mixed drop point (loose archives
alongside nested series directories) moves **only** the loose archives in
`parsed_by_path` (`_move_loose_files`). `plan_case(staging_source_path=...)`
inventories the **whole tree** (`build_inventory`), and transfer copies or
moves that whole tree. Reusing it on a mixed parent would stage unrelated
nested series.

So: **a mixed parent always yields `defer` (reason `mixed_parent`)**, and the
handoff covers only the whole-directory branch, where the payload is the
whole tree and equals what `plan_case` inventories. A selective-payload
contract (an explicit member list, inventoried and verified as such) is a
separate, later design.

### A3. Phases (each its own PR and review; none activates routing)

1. **Shadow-plan (inert, first).** In `shadow`, compute the
   `HandoffDecision`; for `review_handoff`, call the staging planner only
   (`plan_case`, read-only) and log the would-be case id, inventory digest,
   file count and method. No copy, `.partial`, or manifest. Count outcomes in
   the shadow summary, including `defer` by reason. **In shadow the decision
   is an observation only: the existing move path runs exactly as today**
   (`_move_loose_files` for a mixed parent, the whole-directory move
   otherwise), whatever the decision says. `defer` and `review_handoff`
   change no movement until a separately approved activation (A3.3); the
   "moves nothing" and "no legacy fallback" rules of A1, A2 and A5 describe
   that future activated behaviour, not shadow. A first PR that suppressed
   any movement would be a behaviour change needing its own gate.

   *Implemented (2026-10-22)* in `scripts/cbz_shadow_handoff.py`, wired
   through `_shadow_route` in the watcher. The review root is read from
   `CBZ_CLASSIFICATION_REVIEW_ROOT`; unset or not an existing directory, the
   shadow logs `review_root_unset` / `review_root_unavailable` and plans
   nothing. Only that minimal check exists: the full gate of A3.2 (same volume
   as the library, not inside the watch folder or a library root) is not
   implemented, which is safe only because nothing here writes. The trigger
   used is the one proposed in A1 and still open in D.1: `ambiguous_series`,
   or `confidence == "unresolved"`. Because a v2 no-match is always
   `unresolved`, every arrival that no rule classifies counts as a would-be
   handoff; the shadow counts are the evidence D.1 should be decided on, not
   a decision. The plan hashes the whole payload under the series lock, once
   per would-be handoff, and only when a review root is configured.
2. **Configuration gate.** The review root is an explicit, validated setting
   (no default): it exists; it passes the runtime topology checks (section
   1); it is on the same volume as the library roots, so promote stays a
   rename; and it is not inside the watch folder, a library root, or a
   routine staging destination. Invalid or unset: phase 1 logs that, nothing
   else.
3. **Activation is not designed here.** It requires its own mode value and a
   separate decision, and depends on part B and on #99's authority work.

### A4. Retained source and the handoff journal

`execute_transfer(delete_copied_source=False)` stays the default on handoff:
the original arrival is retained. To keep the watcher from re-detecting it,
it is moved **within its own volume** to a case-owned holding area. That
move creates a new authoritative path, so it is journalled, not implied.

A **handoff journal** per case, in its own file with its own contract
(`watcher-handoff-v1`, strict parser in the same style as the case
manifest), records:

```text
case_id
original_identity   the arrival's original path and the inventory digest
                    planned for it — immutable for the life of the case
source_location     where the retained source currently is: original path
                    or holding path — the only mutable field, changed only
                    by journalled steps
state               planned → transferred → source_held → (released |
                    restored)
```

| Crash point | What disk + journal show | Recovery |
|---|---|---|
| after `transferred`, before the holding move | journal `transferred`, source at original path | move to holding (re-verifying the digest first), then `source_held` |
| during the holding move (same-volume rename) | the source at exactly one of the two paths | record whichever path holds the digest-matching tree; both or neither: `operator_required` |
| source digest changed while held | mismatch at check time | `operator_required`; touch nothing |

The watcher excludes the holding root by configuration (it is not under the
watch folder), and recovery, not the watcher, owns anything in it.

**Two different ways back, and only one needs part B.**
- **Restore the retained source**: the held source is a verified, case-owned
  copy on the original volume. Returning it is a **same-volume** journalled
  rename from holding back to the original path (refused if that path is
  occupied), plus discarding the staged copy through quarantine. This needs
  no cross-volume protocol, so a handoff with a retained source is **not**
  irreversible without part B.
- **Return the staged payload across volumes** (part B) is needed only when
  the source was released, or when the operator wants the staged bytes
  rather than the retained ones.

### A5. Rules the eventual handoff must keep

- An arrival is either handed off or moved by the legacy path, never both;
  the two are exclusive branches of the typed decision (guard-removal test).
- `note_move` is not called for a handed-off arrival: it did not land.
- The copy runs under the series lock (same-series arrivals wait, the #32
  serialization) but **never** under the router index lock (lock_topology.md:
  series → index, never the reverse).
- If staging refuses (collision, partial recovery required, source changed),
  the decision becomes `defer` and the arrival is reported; there is no
  silent fallback to the legacy move.

## Part B — Verified cross-volume rollback

### B1. Scope

Return a `pending_review` case's **staged payload** to a target on another
volume, when the retained source is released or not wanted (section A4).
Promoted cases remain terminal (an "un-promote" is a different plan/apply).
The existing same-volume `rollback_case` is unchanged.

### B2. Case ownership: one contract every mutator obeys

A separate journal keeps the v1 parser intact but would not, by itself, stop
the existing operator commands: `_ready_for_operator` accepts the same
`pending_review` manifest and digest throughout an attempt, and
`promote_case`, `reject_case`, `rollback_case`, and `recover_case` know
nothing of it. So ownership is part of the case record, under one lock:

- **One per-case OS lock**, `<review-root>\locks\<case>.lock` (non-blocking
  exclusive lock, released by the OS if the holder dies), held by **every**
  mutator for the whole of its mutation: promote, reject, same-volume
  rollback, recovery, and every step of a cross-volume attempt. A mutator
  that cannot take it refuses; it never waits indefinitely.
- **Ownership is claimed by bumping the reader contract.** Under the lock, an
  attempt rewrites the case manifest (atomic replace) from
  `classification-case-v1` to **`classification-case-v2`**, which adds one
  field: `owner = {journal: cross-volume-rollback-v1, attempt_id}`. Today's
  strict parser refuses any contract that is not `classification-case-v1`,
  so an older binary **that reads the case after the claim** refuses it.
  That fences fresh reads only; in-flight legacy operations are handled by
  the rollout rule below. New code reads v1 and v2.
- **Identity hashes do not move with the reader contract.** Today
  `CASE_CONTRACT` feeds `case_identity()` and the inventory digest as well as
  the parser check. The implementation splits them: a frozen
  `CASE_IDENTITY_NAMESPACE = "classification-case-v1"` for case identity and
  inventory hashing, and `contract` for the record's reader schema only, so
  a v2 record keeps every existing case id and inventory digest unchanged.
- **The approved digest and the owned record are bound, not equal.**
  `record_digest` hashes the whole serialized record (contract, state,
  timestamps), so the claim necessarily changes it. The operator approves
  the **pre-claim v1 digest D** (from `show_case`); the journal freezes D. The
  only allowed transition is: v1 record with digest D → v2 record whose
  **projection** (the v2 record with `owner` removed and `contract` set back
  to v1, re-serialized) has digest exactly D. Any other difference between
  the owned record and that preimage is tampering, and recovery refuses.
  Terminal completion is the same projection rule plus `state: rolled_back`
  and its timestamp.
- **Rollout precondition (activation, not now).** Strict parsing cannot fence
  a legacy promote, reject, or rollback that already read v1 and is about to
  move: old code takes no case lock, and `_replace`'s first attempt does not
  re-check. So claims may be enabled only after every legacy staging mutator
  is stopped and drained, the new code is the only version installed, and no
  mixed-version writer is running (verified at the operator preflight).
  While any legacy process might still be running, claims stay disabled.
- **Every new mutator checks `owner`** after taking the lock and refuses
  when a journal owns the case, except that journal's own recovery for the
  same `attempt_id`.
- **Two simultaneous claims** serialize on the lock; the second sees `owner`
  already set and is refused. A crash leaves the lock released and `owner`
  set, so only the owning journal's recovery can continue: the claim
  survives the crash.
- The attempt's terminal step writes `rolled_back` and keeps `owner` (the
  record of who finished it).
- **Abandonment is journalled and conservative.** The operator writes
  `abandoned` (reason, operator receipt) to the attempt journal under the
  case lock. It is **refused** if anything exists at the target path, with
  or without a receipt: a target whose publish cannot be attributed is
  ambiguous and must first be dispositioned by the operator outside this
  protocol, then recorded. Only after that is `owner` cleared, by restoring
  the exact v1 preimage with digest D, and only if the staged root still
  verifies against the frozen inventory.

### B2a. The rollback journal

Each attempt has its own journal file with its own contract
(`cross-volume-rollback-v1`, strict, append-only progress). It also freezes
the case identity it acted on (case id, the manifest's `record_digest`, the
frozen inventory) and recovery checks that the manifest still matches.

```text
attempt_id          unique per attempt; the ownership token
case_id, record_digest   the case and the show_case digest the operator approved
target              the return path; must be absent at start
frozen_inventory    the case's recorded inventory digest
staged_root         the case's actual staged payload root,
                    pending\<case>\payload\<source-name> (`_staged_payload_root`)
layout              <target-volume>\.rollback-partial\<case>\<attempt>\<source-name>
progress            planned → copied → verified → published → quarantined →
                    case_recorded
published_receipt   target path, attempt_id, digest — written immediately
                    AFTER the publish rename (two filesystem writes, not atomic)
quarantine_path     <review-root>\rolled_back\<case>\<attempt>\
```

**The copy is bound to the real staged root**, not its parent: step 2 copies
`pending\<case>\payload\<source-name>` to
`.rollback-partial\<case>\<attempt>\<source-name>`, and publication renames
exactly that directory to the target. The inventory root is the same
`<source-name>` tree that transfer inventoried, so the frozen inventory
applies unchanged and the returned tree has the original shape; no
flattening contract is introduced.

### B3. Steps

```text
precondition  case is pending_review; the operator supplies record_digest
i intent      journal written FIRST with the complete immutable intent:
              attempt_id, case_id, approved digest D, target, staged_root,
              frozen_inventory, layout; target must not exist
0 claimed     under the case lock: compare-and-set the manifest — still v1
              with digest D and no owner — to v2 with owner = this attempt
1 planned     journal progress → planned
2 copied      copytree staged_root → layout path (target volume)
3 verified    full-SHA inventory of the copy == frozen_inventory; and, freshly,
              staged_root exists, belongs to this case, and still verifies
4 published   same-volume rename of the layout tree → target, refused if the
              target now exists; then published_receipt (separate write)
5 quarantined freshly verify the TARGET (exists, full-SHA == frozen_inventory)
              and the staged root; then move pending\<case> → quarantine_path
6 case_recorded  freshly verify the target again and that quarantine_path
              holds exactly this case's tree (full-SHA == frozen_inventory);
              then case manifest → rolled_back (terminal), last
```

Nothing is removed before step 5, and step 5 moves rather than deletes.

### B4. Ownership, not equality

A target that exists with bytes **equal** to the frozen inventory is not
proof that this attempt published it: an earlier attempt, an operator, or a
coincidence could have put it there. So:

- Recovery treats the target as published by this attempt **only** if the
  journal holds this attempt's `published_receipt`. Without it, an existing
  target is **ambiguous** and reported, whether its bytes match or not;
  nothing is recorded or quarantined automatically.
- An incomplete or mismatched `.rollback-partial` may be discarded **only
  after freshly re-proving** that the pending payload still exists, belongs
  to this case (case id and path from the manifest), and verifies against
  the frozen inventory. If pending is missing or changed, the partial may be
  the only surviving copy: it is kept, and the attempt is `operator_required`.
- The same fresh proof is repeated after any Windows-lock wait (`_replace`
  retries, #94) before acting on the result.
- **A receipt proves a past publish, not the present.** Before quarantine
  (step 5) and before the terminal record (step 6), the target is freshly
  re-verified against the frozen inventory; a missing, replaced, or changed
  target stops the attempt as `operator_required`, keeping the pending (or
  quarantined) copy. Likewise, a directory at `quarantine_path` is accepted
  as this attempt's move only if it holds exactly this case's tree and
  verifies against the frozen inventory.
- **The rename-then-receipt gap** (a crash after the publish rename, before
  the receipt write) leaves a target without a receipt; it is treated
  conservatively as ambiguous (`operator_required`), never inferred.

### B5. Complete crash matrix

| Last durable progress | Disk | Recovery |
|---|---|---|
| `intent`, manifest still v1 with digest D, no owner | orphan intent | only an operator-initiated recovery may claim (step 0) or mark it `abandoned`; nothing automatic |
| `intent`, manifest v1 but digest ≠ D | case changed since approval | intent `abandoned` (stale); nothing touched |
| `intent`, manifest v2 owned by this attempt | claimed without progress | verify the projection equals D, then progress → `planned` |
| manifest v2 owned by an attempt with **no journal** | should be impossible with intent-first | `operator_required`; never reconstruct a target or digest from absent metadata |
| `planned` | no partial | start step 2 |
| `planned` | partial present (copy interrupted) | fresh pending proof → discard the partial and recopy; proof fails → keep the partial, `operator_required` |
| `copied` | partial complete, unverified | verify (step 3) |
| `verified` | target absent | publish (step 4) |
| `verified` | target present, **no receipt** | ambiguous target → `operator_required` |
| `published` (receipt present) | target present and freshly verifies | quarantine (step 5) |
| `published` | target missing, replaced, or not verifying | `operator_required`; keep pending |
| `published` | pending already at quarantine_path, journal not advanced | accept only if quarantine_path verifies as exactly this case's tree, then record `quarantined`; otherwise `operator_required` |
| `quarantined` | target and quarantine both freshly verify, manifest still owned v2 `pending_review` | write `rolled_back` (step 6) |
| `quarantined` | target or quarantine no longer verifies | `operator_required`; keep the quarantined copy |
| `case_recorded` | — | terminal; nothing to do |
| any | pending missing or changed before step 5 | keep everything, `operator_required` |

Every action is idempotent and decided from journal plus disk alone, under
the case lock (B2). One attempt per case: the v2 `owner` field, set under the
lock, refuses any second attempt and every other mutator until the owning
attempt reaches `case_recorded` or the operator abandons it.

### B6. Limits, stated

- Integrity rests on the full-SHA inventory; the known window for a
  same-size rewrite inside the mtime quantum (`_hash_stable_file`) applies to
  the pending re-checks, as on transfer.
- Local NTFS only (section 1). Between steps 4 and 5 the bytes exist on both
  volumes, by design; the operation is not atomic across volumes.

## C. Named tests (for the implementation PRs)

Part A:
- `test_shadow_plan_logs_would_be_case_and_moves_nothing`
- `test_classification_error_defers_and_moves_nothing`
- `test_mixed_parent_with_loose_files_and_unrelated_nested_series_defers`
- `test_authoritative_false_alone_is_not_a_review_handoff`
- `test_missing_or_invalid_review_root_logs_and_does_nothing`
- `test_review_root_on_another_volume_than_the_library_is_refused`
- `test_handoff_and_legacy_move_are_exclusive`
- `test_handed_off_arrival_does_not_update_the_series_index`
- `test_staging_refusal_defers_without_legacy_fallback`
- `test_copy_never_holds_the_router_index_lock`
- `test_holding_move_crash_resolves_to_exactly_one_location`
- `test_retained_source_restoration_is_a_same_volume_journalled_rename`
- `test_changed_held_source_is_operator_required`

Part B:
- `test_cross_volume_rollback_returns_verified_bytes`
- `test_verify_compares_to_the_frozen_inventory_not_the_copy`
- `test_foreign_identical_target_without_receipt_is_ambiguous`
- `test_partial_is_kept_when_pending_is_missing`
- `test_partial_is_kept_when_pending_changed`
- `test_pending_is_re_proved_after_a_windows_lock_wait`
- `test_case_manifest_becomes_rolled_back_only_last`
- `test_v1_parser_refuses_a_journal_owned_v2_case`
- `test_promote_reject_and_rollback_refuse_a_journal_owned_case`
- `test_recover_case_refuses_a_journal_owned_case_except_its_own_attempt`
- `test_paused_rollback_racing_promote_is_refused_by_the_case_lock`
- `test_two_simultaneous_attempt_claims_admit_exactly_one`
- `test_ownership_survives_a_crash_and_only_the_owner_recovers`
- `test_target_drift_after_receipt_stops_before_quarantine`
- `test_target_replaced_after_quarantine_stops_before_terminal`
- `test_foreign_tree_at_quarantine_path_is_not_accepted`
- `test_copy_is_rooted_at_the_staged_payload_root`
- `test_crash_between_publish_and_receipt_is_ambiguous`
- `test_shadow_decision_never_changes_the_existing_move`
- `test_owned_v2_projection_must_equal_the_approved_v1_digest`
- `test_tampered_owned_record_is_refused`
- `test_case_identity_and_inventory_digest_are_stable_across_v1_to_v2`
- `test_crash_after_claim_before_planned_resumes_from_intent`
- `test_orphan_intent_without_claim_is_not_auto_claimed`
- `test_stale_intent_after_case_change_is_abandoned_without_mutation`
- `test_owned_case_without_journal_is_operator_required`
- `test_abandon_is_refused_while_a_target_exists`
- `test_abandon_restores_the_exact_v1_preimage`
- `test_claims_stay_disabled_while_a_legacy_mutator_may_run`
- each crash-matrix row (B5) as its own test
- `test_unsupported_topology_is_refused` (SMB, exFAT)

Every cross-volume test today fakes volumes by monkeypatching
`volume_identity` / `same_volume`. That stays for unit tests, but part B also
needs **one opt-in integration test on two real local volumes** (skipped when
the host has only one), because a faked `st_dev` cannot prove
copy-then-rename across real filesystems. It is operator-run and never
touches the library.

## D. Open questions

1. **Review policy** (A1): is "ambiguous or unresolved key under #99" the
   right handoff trigger, and does any `authoritative == False` case also
   belong?
2. **Holding root** (A4): its location and retention, and who approves
   releasing a held source after promote.
3. **Order of work**: shadow-plan (A3.1) first; part B only when a released
   source or an explicit staged-bytes return is wanted, since retained-source
   restoration (A4) needs no cross-volume protocol.
4. **Selective payloads** (A2): whether mixed parents ever get handed off, or
   stay on the legacy path permanently.
