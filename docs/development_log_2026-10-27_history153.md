# History retention verification: 2026-10-27

Codex resumed #153 after the successor-thread canary. The clean feature tree
was at `873dc8cd8eb8d0f59dc9140effe57a0c029678ab`. The five interrupted-run
commits were inspected: `5130a03` scopes store checks and serializes runner-lock
cleanup; `b6f2367` verifies partially removed sources; `d53ce93` adds ledger-first
history maintenance; `c12a115` adds lifecycle/scale witnesses; `873dc8c` documents
the contract. Each changes exactly one file. Prerequisite #154 is incorporated
through merge `8d09cf7`, with the approved helper unchanged.

## Fresh verification at 873dc8c

- Focused: Python 3.11, `pytest -q` on `test_web_job_store.py`,
  `test_web_state_dir.py`, `test_web_archives.py`, `test_web_history.py`:
  **137 passed in 100.71 seconds**, no skips.
- Full: `C:\Python311\python.exe -m pytest -n 4 --dist loadfile -q -ra
  --durations=15`: **4,632 passed / 10 skipped in 305.83 seconds**. Both real
  browser tests passed. Skips: one absent routing configuration, two deliberate
  unreadable corpus cases, seven unavailable Windows symlink-privilege cases.
- Collection delta from pushed checkpoint `be5a5e0`: the focused population
  rises from 101 to 137, exactly **36 added cases** (25 history, three archive,
  eight prerequisite helper cases). The retained earlier checkpoint log has
  4,596 passed / 10 skipped; its full run was not repeated in this successor
  turn. No new failures or removed tests are hidden in the delta.
- Fresh scale rerun: **1 passed in 44.62 seconds**. The 1,001-job fixture has
  more than 2,000 entries, restarts, creates five batches, leaves exactly 200
  hot job directories and reads an archived command. Privacy checks stay
  **19/19** before/after growth; sampled get times **70.338/130.070 ms** under
  concurrent suite load. Timings vary; bounded check count is the structural
  claim, not constant database cost or a total-history storage bound.
- Candidate diff-check passes. Changed implementation/test/doc Git blobs are
  LF; the store working copy is CRLF with an LF index, without mixed endings.
  The candidate was not mutated during these runs.

The initial default `python` selected Miniconda without pytest and failed before
collection; all actual verification used `C:\Python311\python.exe` (3.11.3).
`CBZ_TRANSLATION_ENABLED` was unset for these fresh focused/full runs; the
isolated mutation subprocesses explicitly set it to zero.

## Selected guard campaign

All **15/15 selected mutations** failed named witnesses in a separate unbound
scratch worktree pinned to 873dc8c. Three selected-job privacy cases fail for
that bypass; every other mutation below causes one test failure. There were
no collection errors. Sources were restored byte-for-byte and the clean,
inactive scratch worktree was removed. The replay script remains outside Git
at `Documents/Codex/control/web153_successor_mutants.py`.

| Guard | Witness in tests/web (test_web_ prefix omitted) |
| --- | --- |
| selected job privacy | history: test_store_checks_the_selected_job_command_privacy (3 cases) |
| closed runner lock removal | history: test_claim_close_removes_only_the_lock_not_durable_admission |
| primary error survives cleanup | history: test_cleanup_failure_preserves_the_body_error |
| nested source directory privacy | history: test_archive_checks_empty_source_directory_permissions |
| archive event transaction | history: test_archive_event_failure_leaves_orphan_batch_and_hot_evidence |
| pointer commit before deletion | history: test_archive_event_failure_leaves_orphan_batch_and_hot_evidence |
| fresh cleanup seal verification | history: test_corrupt_committed_batch_never_removes_hot_files_or_falls_back |
| cold batch privacy | history: test_cold_reads_check_the_batch_permissions |
| remaining-source member set | archives: test_cleanup_mode_still_refuses_new_hot_members |
| explicit cleanup evidence | archives: test_cleanup_mode_requires_explicit_source_and_ledger_evidence |
| batch SHA-256 | archives: test_archive_digest_failure_never_falls_back |
| hot content digest | archives: test_changed_hot_source_refuses_cleanup_verification |
| M12 stale version | job_store: test_stale_uncertain_version_is_fenced_on_an_otherwise_eligible_row |
| M11 orphan-directory reuse | job_store: test_a_complete_orphan_command_directory_cannot_be_reused |
| M14 pre-lock identity | job_store: test_root_identity_is_checked_before_a_lock_can_be_written |

This is a selected guard campaign, not a claim that every possible guard was
mutated. Earlier codec campaigns remain their own evidence records.

## Handoff boundary

Only this documentation file is added after the measured head; executable
source and tests remain byte-identical to 873dc8c. Claude owns independent
review of the final exact head and pinned merge on green exact-head CI. Codex
owns findings, then M3b bounded logs after this dependency lands. M3c provides
native terminal proof, M3d schedules retention/recovery, and M4 supplies the
trusted production submission adapter. Cold log paging is still M3b work.

This slice exposes explicit library maintenance only. Production submission,
live archival, running watcher/GUI changes, operational database work and relay
or Codex-state changes were not performed in this implementation verification.
Protocol and accepted limits remain in [web_console_history_153.md](web_console_history_153.md).
