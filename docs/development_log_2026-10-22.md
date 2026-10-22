# Development log 2026-10-22 -- Wired ingest evidence (#32/#35)

Implementation lead: Codex. Independent reviewer: Claude. Branch
`test/32-wired-ingest-evidence`, based on remote master
`a5dc3bc20a4fba052918cf4f2a86d1dc1180192d`. The read-only reconciliation and
accepted test slice are on [issue #35](https://github.com/Decksx/cbz-automation-suite/issues/35#issuecomment-6345673435)
and hive messages #1937/#1938. Test implementation commit:
`7e414e33b66e64a2fa008834164e1a4391cc1680`.

## Reconciliation

The former staging PR #33 head
`0ba5eec52849ef32e81ef30851f74d08de3b90ab` is an ancestor of current master:
zero unique commits, 458 commits behind. The remote no longer advertises that
branch. #30/#33/#38/#40/#41/#42/#43/#46 already supplied shadow routing,
staging planning, source revalidation, transfer, persisted-state recovery,
operator operations, ordered locks and watcher transaction wiring. The final
August comments on #32/#35 say their acceptance criteria were implemented;
their open issue states were stale evidence of remaining engineering work.

Later #44 normalized series identities consistently, #56 completed the former
#31 provenance blocker, #94 added rename retries with fresh checks after waits,
and #96 supplied complete index diagnostics. This session did not reimplement
those changes or treat the old staging branch as a new candidate.

## Evidence gap and change

The existing test named "across arrivals" created a second arrival but only
processed the first and probed its lock from another thread. It is retained
with a more accurate name and without the unused second arrival.

Three additional tests use real watcher ingests, the shadow router,
classification, index updates and movement on temporary paths. They isolate
identity parsing and per-file normalization, which are outside the boundary
being checked.

- Same series: A pauses immediately before index publication while holding its
  operation lock. B reaches that lock and observes contention. After A publishes
  and releases, B classifies from A's actual index entry: its decision changes
  from the configured fallback to the indexed library, with the indexed path
  and authority flag. Both archives reach the same temporary directory.
- Different series: B acquires its own lock and completes classification,
  movement and index publication while A is still paused before publication.
- Failed move: A returns no landed path, makes no publication attempt and adds
  no index entry. B subsequently ingests the same identity, acquires the released
  lock and publishes its real move.

Events and actual acquisition results establish the ordering; there are no
sleep-based negative assertions. Five-second waits/joins bound a broken test
rather than decide whether an operation was serialized. Worker exceptions are
retained, workers are joined, and teardown releases the publication barrier.

## Verification

Windows / Python 3.11.3, isolated checkout
`C:\git\cbz-review-32-35-20261022`.

Before editing, the staging/operator/lock focused selection passed
**283 tests in 19.25 s** on the exact master above. After the changes:
`python -m pytest -q tests/test_watcher_series_lock.py` passed **11 tests in
9.58 s**. Full suite, `python -m pytest -n 4 --dist loadfile -q`:
**2412 passed, 8 skipped in 50.63 s**. This adds three collected passes to the
2409-pass / 8-skip record at that base; no full-suite baseline was rerun before
editing. Skips are reported rather than counted as passing.

Four selected mutations were independently rejected, one failing case each:

| Mutation | Witness |
| --- | --- |
| omit the transaction lock | waiting ingest never contends |
| omit the router index update | B still chooses fallback instead of the indexed library |
| remove the failed-move guard | an index publication is attempted with no landed path |
| use one global operation lock | an unrelated series contends instead of proceeding |

All production-source bytes were restored after each mutation. Raw logs and
the harness are outside Git under
`C:\Users\david\Documents\Codex\control\comic32-20261022` and
`comic32-mutations-20261022.py` in its parent. Git diff confirms no production
source changes. The test file remains index LF / working-tree CRLF;
`git diff --check` passes.

## Boundary and next action

Only tests and this log changed. No live library or database was opened, no
backfill batch launched, no production service changed and no routing activated.
Accepted watcher modes remain off/shadow. Cross-volume rollback, watcher staging
wiring, a fresh target ambiguity observation and the reviewed-exception manifest
design remain separate capabilities/gates.

Next owner: Claude for independent review of the final exact PR head and CI.
Codex handles merge after approval and green exact-head CI, recording the merge
receipt on the PR and in the hive. This is a test/documentation slice and needs
no production rollout. These local checks do not claim CI or review has already
completed.

