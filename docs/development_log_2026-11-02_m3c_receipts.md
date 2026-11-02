# 2026-11-02: M3c slice 2 checkpoint A, claims and durable receipts

Owner: Codex; independent exact-head reviewer/merge owner: Claude.
Relay #3495 acknowledged as confirmed hive **#3497** at 21:58UTC.
Base master **080e39d5a644b67c9df1b2d55d8e222b21995935**, whose tree
**ba9d1a2394231ffc25682e580fccb840dc5675bd** matches the already-tested
PR159 synthetic integration. Swarm187 also independently confirmed merged
as **289c334b0f708f520775c06c26837cd43c7d0753**.

Code/test/protocol checkpoint **7c853b713aa4e42ecea9dcb475450c719ad155d1**
is six one-file commits. Store transitions authenticate live matching
claim/nonce/runner identity/state/version/physical lock, read immutable commands,
persist running or a positively pre-birth refusal, and finalize only after
a durable receipt plus fresh owned native/log proof. Publication and finalization
both require a trusted outcome mapper; the receipt cannot choose success.
Terminal rows/events/clock high-water updates share a transaction. No native
wait, log flush or receipt fsync is inside the write transaction.

Receipt reads are private, identity-pinned, regular-file and bounded. Publication
uses an exclusive sibling temp, checked write, flush/fsync, atomic replace and
readback. Existing receipts are never replaced. Actual root exit, empty job,
started/settled log threads, actual EOF, durable complete index and command/seal
consistency are required again at finalization. Unsupported stop intent refuses.
Durable uncertain cannot auto-clear. Native environment sorting uses uppercase
names and has a CreateProcessW argument witness. Dedicated-host inheritance
and collided-job last-handle caveats are documented in the protocol.

## Checks and qualified scope

OfficePC, Python3.11, disposable state homes/contained Python children only.
Final focused command:

```
C:\Python311\python.exe -m pytest -q tests/web/test_web_runner_receipts.py tests/web/test_web_job_store.py tests/web/test_web_win32.py
```

**136 passed in40.75s**: 53 new receipt/transition cases, 50 existing store
cases and33 native cases (one new environment case). Earlier partial focused
runs are superseded by that measurement. An initial oversized-byte pytest
parameter generated an overlong test-directory name; explicit case IDs fixed
that fixture problem. No source/platform workaround was applied.

The full `-n4 --dist loadfile -q` suite started at exact7c853b7 at22:13UTC;
its result is pending at this evidence checkpoint. Later results and final
candidate CI receipts belong on the PR and in an additive update here.
No clean full-suite or CI result is claimed yet.

## Guard bypasses

Replay outside Git: `C:\Users\david\Documents\Codex\control\m3c_receipt_mutants.py`;
final output `m3c_receipt_mutants_final.log`. **19 selected mutants killed**,
with31 failing test executions (overlapping populations; not31 unique tests).
Every source file was restored byte-equal before the focused/full runs.

| Removed or bypassed boundary | Named witness / failed executions |
| --- | --- |
| closed claim | live_exact_claim_fences_running_transition[closed],1 |
| state version | same[version],1 |
| nonce | same[nonce],1 |
| runner PID | same[pid],1 |
| exact runner FILETIME | same[creation],1 |
| runner session | same[session],1 |
| required running state | late_valid_receipt_does_not_clear_uncertain,1 |
| physical lock identity | claim_lock_must_still_name_its_open_physical_file,1 |
| root exit proof | publication_itself_requires_fresh_native_and_thread_proof[root],1 |
| verified empty job | persisted_booleans_never_replace_fresh_owned_proof[descendants],1 |
| writer settlement | publication_itself_requires_fresh_native_and_thread_proof[writer],1 |
| reader settlement | same[reader],1 |
| exact receipt binding/shape | receipt_shape_and_binding_are_not_authority,13 |
| receipt fsync | publication_fsync_failure_never_publishes_or_finalizes,1 |
| immutable command digest | claimed_command_is_bound_to_every_immutable_ledger_field,1 |
| fresh mapped outcome | receipt_shape_and_binding_are_not_authority[state],1 |
| future receipt clock guard | same[future],1 |
| unsupported stop evidence | missing_receipt_and_pending_stop_cannot_release_admission,1 |
| uppercase environment order | environment_block_is_ordered_by_uppercase_names,1 |

These selected bypasses do not claim exhaustive mutation coverage. Bounded
reads/canonical shape and immutable digests provide some redundant refusal
boundaries. The real native happy paths cover successful and failed children;
individual failure predicates use deterministic injected observations.
The terminal-event failure is injected inside the real transaction, which
rolls back; the durable receipt then retries once under the same live claim.
This is not yet a dead-runner recovery/crash import witness.

## Stopping point and next action

Checkpoint A is deliberately the store/receipt foundation of slice2, **not
completed runner supervision**. Its synchronous receipt fsync can stall; the
forthcoming supervisor must remain responsive independently and retain the
claim/handles for unsettled work. There is no runner entry point/registry
adapter, LaunchSpec reconstruction, scheduling or HTTP production path here.

After exact-head review/green CI/pinned merge, checkpoint B implements the
trusted claimed runner, post-birth commit failure, descendant grace independent
of EOF, responsive receipt publication, stop gates and bounded cleanup.
The writer-fails/child-writes-beyond-pipe-buffer witness remains specifically
owed, as does drain/discard through final cleanup. Slice3 recovery evidence,
M3d orchestration/routes and M4 production adapters remain separate. No rollout,
production library/DB action, relay change or Codex-state change.
