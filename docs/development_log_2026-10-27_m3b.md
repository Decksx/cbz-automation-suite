# M3b bounded-log implementation and verification (2026-10-27)

Codex owns implementation; Claude owns independent exact-head review and pinned
merge on green CI. Baseline is merged #155,
`20ebcc26989ebabd8f65b6fe6363edabeec64470`. Its tree
`d8b2fd4693cfa48653d8b24228a98cf7a9dff25a` independently equals approved
`eeaeacf`; source/tests also equal the earlier measured 873dc8c baseline.
The retained baseline verification is 4,632 passed / 10 skipped. No new baseline
full rerun is claimed in this turn.

## What changed

The M3b library supplies native shared file reads, best-effort synchronous I/O
cancellation, a bounded nonblocking raw-byte queue, independent reader/writer
threads, contiguous segments and compact gaps, durable coverage/EOF metadata,
strict cursors, bounded UTF-8 paging, progress parsing, matching-claim writers,
and private hot/cold readers. Archive pointers additionally pin member digests
in an additive immutable table, committed with the archive event.

Two protocol refinements need explicit review: compact atomic `index.json`
instead of unbounded `gaps.jsonl`, and cold member authentication against
independent ledger pins instead of rehashing every unrelated byte in a batch.
The original command and cleanup readers still verify the whole seal. Old
batches without pins refuse the new fast paging path; no ledger is backfilled.
See [the M3b protocol](web_console_m3b_logs.md) for APIs, ownership and limits.

An earlier complete run passed **4,695 / 10 skipped in 261.90s** before the
adjacent-cache correction. Inspection found that one cached member could thrash
while a line read looked across rotation. The fixed two-member cache and its
direct witness replace that candidate. That head passed 4,696 / 10 skipped in
291.22s. A further member-count packing fix prevents tiny segments from making
every default-size archive batch exceed the member cap. The final run below
includes that source fix and its direct regression.

## Fresh final verification

Measured implementation head:
`bfa57ef428740c3e2a5acd9610f6ebf531c73bc3`. The final verification-log commit
changes only this document; source and tests stay byte-identical.

- New log files at b51d552: **64 passed in 29.08s**, no skips. A prior broader focus of
  logs/store/archives/history passed 154 before the last additional witnesses;
  it is not relabelled as a final full focused selection.
- After the packing fix: its new regression plus all 15 log-service tests,
  **16 passed in 24.70s**. These overlap the 64 above and are not added as a
  separate population. Final collection adds 65 cases to the 4,632 baseline.
- Final full: **4,697 passed / 10 skipped in 282.62s**, using
  `C:\Python311\python.exe -m pytest -n 4 --dist loadfile -q -ra --durations=15`.
  Exactly 65 cases are added to the retained baseline. Both real-browser tests
  passed. Skips remain one absent routing configuration, two deliberately
  unreadable corpus cases and seven unavailable Windows symlink privileges.
- All **21/21 selected mutations** were caught against bfa57ef in a separate
  scratch worktree, 23 named failures total. Every source byte was restored.
  The clean inactive scratch worktree was then removed.
  The replay/log are outside Git in Documents/Codex/control/
  `m3b_guard_replay.py` and `m3b_guard_replay_final.log`.
- Diff-check passes; each implementation/test/protocol commit changes one file.
  Python is C:\Python311 (3.11.3), Windows. Full suites use four workers and
  loadfile distribution; mutation subprocesses use isolated bytecode caches
  and explicitly disable translation. Ordinary suite runs leave the translation
  environment unset.

The fresh tests include a deliberately paced 50MiB producer with no server/app,
exact persisted-byte hash and restart paging; a 1MiB line; malformed UTF-8;
UTF-8 across rotation; escaped JSON bounds; loss cursors before/inside/after
gaps; stalled and failed writers; short/zero writes; interrupted index publish
and safe replay; retention-before-delete; bounded alternating-loss metadata;
claim closure; unknown tails; native shared append/delete/noninheritance;
hot/cold corruption without fallback; immutable member pins and legacy refusal;
and progress when small files exhaust the archive member-count budget.

The first integration run had one fixture failure: its deliberately unsafe
junction was still present when claim cleanup revalidated it. The fixture now
removes its own junction before closing the claim. Product refusal was correct.

## Selected mutation witnesses

All nodes are under tests/web; test_web_ prefix is omitted in the table. Every
mutation has one failing case, except the page-limit bypass (three). The cursor
bypass also runs 12 unaffected controls. No collection errors count as kills.

| Guard | Witness |
| --- | --- |
| archive member-count packing | history: test_batches_pack_member_count_instead_of_refusing_segmented_history |
| adjacent-member cache | log_service: test_cold_page_lookahead_does_not_thrash_adjacent_segments |
| queue cap | logs: test_queue_advances_raw_offsets_without_pending_gap_objects |
| dropped-byte offset advancement | logs: test_queue_advances_raw_offsets_without_pending_gap_objects |
| reader performs no disk I/O | logs: test_stalled_writer_does_not_stop_reader_and_publishes_exact_loss |
| durable retention gap before unlink | logs: test_retention_gap_is_durable_before_closed_file_deletion |
| truncate uncommitted retry suffix | logs: test_index_failure_retry_truncates_uncommitted_suffix |
| compact old gaps | logs: test_alternating_overflow_compacts_map_and_physical_files |
| unknown tail is not EOF | logs: test_unreadable_pipe_retains_unknown_tail |
| malformed UTF-8 visible | logs: test_json_budget_counts_replacement_characters_and_escapes |
| escaped JSON byte budget | logs: test_json_budget_counts_replacement_characters_and_escapes |
| cursor integer bound | logs: test_cursor_refuses_noncanonical_or_unbounded_values |
| exact index watermark | logs: test_index_corruption_is_refused[uncovered] |
| direct page limits | logs: test_page_limits_are_checked_at_the_direct_service_boundary |
| finite progress counts | logs: test_progress_has_strict_finite_counts_and_never_changes_lifecycle |
| native delete sharing | logs: test_native_shared_reader_allows_write_delete_and_noninheritance |
| pinned cold member digest | log_service: test_tampered_cold_member_refuses_even_with_hot_copy[seg-0000000000000000.log] |
| hot/cold digest agreement | log_service: test_hot_corruption_outside_requested_slice_is_not_hidden |
| closed claim cannot write | log_service: test_matching_claim_is_required_and_close_does_not_grant_new_writer |
| A8: ledger versus manifest digest | log_service: test_codec_verification_compares_supplied_ledger_command_digest |
| A15: claim-lock serialization | log_service: test_claim_runner_lock_creation_is_inside_admission_exclusion |

A15 uses a deterministic hook immediately before opening the runner lock and
a competing real admission byte-lock attempt. Moving that creation outside
`_home` removes exclusion and fails the witness. A8 gives the public codec a
ledger mapping with a different command digest; bypassing only that comparison
fails its witness. Both prior #155 review gaps are directly covered.

## Remaining seams

M3c owns native launch/containment, cancellation/handle ownership and terminal
proof. It must retain the claim until the pump settles, and cannot clear an
uncertain result just because storage later recovers. M3d owns lifecycle hooks,
protected log/SSE routes, durable progress cursor/events, eight-stream limits,
and a byte/time budget for idle archive maintenance. M5 renders continuation
indicators. These are explicit later-slice responsibilities, not enabled
features of this library. Byte bounds do not guarantee arbitrary disk drivers
return within a wall-clock deadline. A missing mapped artifact refuses rather
than inventing a retention gap or successful EOF.

All operations used temporary fixtures. No native tool launch, live state-home
maintenance, operational database change, watcher/GUI rollout, production-submit
route, relay configuration or Codex-state change was performed.
