# M3a implementation: 2026-10-27

Codex implemented the first slice of approved plan #150 after M2 merged as
8f1f0ffe3443e38e4224964b2a1c5e8564e08973. Claude independently reviews the
exact final head and owns pinned merge on green continuous integration (CI).
Board93 tracks this slice. No rollout or live-data/watcher change.

## What is implemented

Lock-free runner state inspection shares M2's guards and reads the committed
identity pin without initializing state. The separate console job store owns
schema version1, immutable command artifacts, admission compatibility, byte
locks, durable claims, deadline expiry, uncertain holds and append-only events.
Production submission requires M4's trusted verifier. M3a does not spawn a
runner or tool. Native evidence collection and terminal proof are later work;
these tests inject exact process-identity fields.

One ordering refinement needs review: the small command artifact is prepared
under the admission OS lock, before the database transaction, so temporary
files cannot race the shared privacy scan. Artifact-before-row ordering stays
intact and fsync occurs outside BEGIN IMMEDIATE. Protocol and limits are in
[web_console_m3a.md](web_console_m3a.md).

## Measurements

- Baseline at8f1f0ff: **4,519 passed /10 skipped in228.13s**.
- Full at9bd3e7f: **4,576 passed /10 skipped in220.04s**, exactly57 new cases.
- Final focused inspection/store files: **57 passed in15.02s**.
- Earlier broader focus including the shared database layer: **93 passed**;
  the later12 additions are in the final57 and full4576 above.
- Windows, Python3.11.3, translation disabled; full suites use four workers
  with loadfile distribution. Fixture processes use temporary private roots.
- After the full run, only documentation changed, including a store validation
  docstring and a corrected existing containment comment. Executable code and
  tests were checked equal to the measured candidate before final handoff.

The57 cases include a20-process admission barrier (exactly one admitted,
19 refused), two competing runner processes with one marker, claim/expiry
process racing, deadline equality, wrong nonce, duplicate claim, rollback of
a failed event insert, immutable event SQL, changed/zero/missing state,
committed WAL pin visibility, raw-command refusal, unknown watcher, schema
refusal, clock rollback, exact integer fields and bounded paging/lock waits.

## Selected guard bypasses

All **19/19** selected mutations were caught by the witnesses below. Each
witness passes on the unmutated source in the57-case run. Mutations run in
a separate scratch copy with isolated bytecode caches; every source byte was
restored. The measured failures are ordinary named-test failures, not collection
errors. Raw logs remain outside Git in Documents/Codex/control/m3a_guards.log.

| Guard | Failing witness (test_web_ prefix omitted from filename) | Failures |
|---|---|---|
| expected identity | state_inspection: test_an_expected_identity_mismatch_refuses | 1 |
| existing pin | state_inspection: test_missing_or_wrong_pin_is_never_initialized_or_rewritten | 2 |
| inspection privacy | state_inspection: test_runner_inspection_rechecks_child_permissions | 1 |
| inspection containment | state_inspection: test_runner_inspection_rechecks_containment | 1 |
| task exclusion | job_store: test_only_one_task_and_no_artifact_for_a_known_conflict | 1 |
| unknown watcher | job_store: test_writers_refuse_a_present_or_unknown_watcher | 1 |
| uncertain hold | job_store: test_uncertain_rows_block_every_class_and_cannot_expire | 1 |
| claim nonce | job_store: test_wrong_nonce_and_late_claim_never_enter_the_tool_side | 1 |
| strict claim deadline | job_store: test_wrong_nonce_and_late_claim_never_enter_the_tool_side | 1 |
| claim source state | job_store: test_claim_is_committed_before_return_and_keeps_admission_after_close | 1 |
| clock high water | job_store: test_clock_rollback_cannot_expire_or_claim | 1 |
| preparation clock | job_store: test_clock_rollback_between_preparation_and_commit_refuses | 1 |
| event update trigger | job_store: test_events_are_append_only_even_through_direct_sql | 1 |
| event delete trigger | job_store: test_events_are_append_only_even_through_direct_sql | 1 |
| transaction atomicity | job_store: test_failed_event_rolls_back_claim_and_releases_its_runner_lock | 1 |
| admission OS lock | job_store: test_admission_os_lock_is_required_even_with_an_idle_database | 1 |
| command readback | job_store: test_readback_failure_cannot_create_an_admitted_row | 1 |
| command fsync | job_store: test_failed_fsync_leaves_no_runnable_row_and_orphans_are_not_reused | 1 |
| schema version | job_store: test_future_schema_refuses_existing_and_new_store | 1 |

## Next owner and limits

Claude reviews the exact final commit and the ordering refinement. Merge needs
green exact-head CI; no approval or merge is inferred from local passes.
M3b logs follow this slice's review and merge. M3c supplies native containment
and actual process evidence, M3d reconciliation/routes, M4 the production
schema/seal adapter, M6 the writing-tool interruption gate. M2's2,000-entry
privacy ceiling and growing job artifacts need a retention policy before
production submission is enabled. The operational database remains separate.
