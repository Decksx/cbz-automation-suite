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


## Ambiguity-report workstream -- issue #98

David authorized the combined next-work plan in hive #1972. Codex owns the
report command; Claude owns independent exact-head review. Claude's parallel
manifest-design and roadmap work uses different files/worktrees.

The earlier #97 handoff above completed: approved head
`a7c0151753e5acfd1975996e553600dbbd033a84` merged as
`f88d1da68a4213b1f3eb0030d5a851d3940e511e` at 11:31:58 UTC-6. Exact-head CI
and independent review were green; #32/#35 closed completed with pointers.
That merge is the base of this work, branch `feat/series-ambiguity-report`.

### Report contract and changes

Source commit `a0d578d0c6f57637bf933730807b5ec0e68563fb` adds
`scripts/cbz_series_ambiguity_report.py`. Required `--routing` and `--scope`
confirm the complete configured index population before enumeration. Partial,
extra or duplicate scope and duplicate configured index labels refuse.
There is no default config, output file or library mutation.

The command supplies observed directory lists to the existing SeriesIndex,
retains those exact lists in deterministic JSON, and fingerprints the full
report plus exact config bytes. Non-colliding names are bound too. Failed
roots remain explicit scan problems; disabled indexing scans nothing and
cannot be clean. Exit 0 is a clear observation, 1 a produced blocked report,
2 refusal without JSON. It does not implement reviewed exceptions or activate
the watcher. Counts distinguish label-level directory observations from the
index's deduplicated paths/keys.

Tests are in commit `55c75e625bb3c79eddfb26eb81a62dd2c0b6b3d4`; the usage and
fingerprint contract are in `docs/series_ambiguity_report.md`, commit
`851d675bf70c6bdba9fd5bbb1e77841ee235eab3`.

### Verification

Windows / Python 3.11.3, isolated
`C:\git\cbz-ambiguity-report-20261022`. Initial existing diagnostics baseline:
**36 passed in 0.62 s**. New report plus existing diagnostics:
**62 passed in 23.77 s**. Full suite:
`python -m pytest -n 4 --dist loadfile -q`,
**2438 passed, 8 skipped in 55.58 s**. The change adds 26 collected passes to
the earlier 2412-pass / 8-skip base record; no fresh full-suite baseline was
run before editing. Skips remain skips.

Nine selected mutations independently rejected:

| Mutation | Failing cases |
| --- | ---: |
| omit scope membership check | 3 |
| omit scope duplicate check | 1 |
| admit duplicate configured labels | 1 |
| omit failed-root scan problems | 3 |
| enumerate with indexing disabled | 1 |
| fingerprint diagnostics only | 3 |
| omit exact config-byte digest | 1 |
| return success for blocked reports | 4 |
| exclude all dot-prefixed directories | 1 |

Each mutation restored exact original source bytes. Harness and raw logs stay
outside Git at
`C:\Users\david\Documents\Codex\control\comic98-mutations-20261022.py`
and sibling `comic98-20261022\`. Fixture tests cover real CLI invocation from
another directory, Unicode, input preservation/no archive content reads,
all diagnostics populations, alias labels, absent/denied roots and digest
reproducibility/drift. No live library or database path was opened.

### Limits and next owner

Directory metadata observations are not atomic or guaranteed fresh. Physical
junction/SMB/symlink identity is not resolved. The fingerprint identifies report
contents, not approval or archive contents. A clear result is not a complete
watcher validation or permission to activate routing.

Next owner: Claude independently reviews the final exact PR head and CI;
Codex resolves findings and owns merge after approval and green exact-head CI.
No runtime rollout or production census belongs to this feature slice.


## Protected-executor contract preparation -- issue #102

David authorized the next comic tracks in hive #2035. Ownership settled in
#2042/#2043: Claude leads staging/rollback design; Codex leads the protected
executor contracts and reviews Claude's design. This first bounded B slice was
proposed in #2044 and accepted in #2045; it is not the whole executor.

Branch `test/slice4b2-executor-contracts`, base
`cd2b774ed2e2f83dd5e14a9b27dd01faab4b72ca`. Production was not opened.

### The admission gap and bounded change

The generic `resolve_protected_execution()` seam intentionally compares only
the pending protected set with explicit authorization. Both discovered
`{1..15}` / recorded `{1..14,16}` and the same ledger with 016 also discovered
pass that seam. This is the exact R15 counterexample in
`docs/slice4_migration_design.md` section 12.1.

Source commit `f30eeae59d2226313617ebe1de674b57c4adeb1d` adds the pure
`require_slice4_ledger(snapshot)` predicate for section 12.1 **step 3 only**:
recorded versions must equal all discovered versions below 15, as set equality.
Missing and unexpected versions are reported separately. The seam's docstring
now distinguishes this implemented predicate from obligations it still does
not enforce. Its call graph/behavior is unchanged: it does not call the new
predicate or apply anything.

Test commit `b8729197172c31ac65522ad825d3fde5168cda47` adds 19 fixture cases.
They include both counterexamples, partial ledgers, a removed migration file,
unknown/later/already-applied rows, the absent-ledger refusal without creation,
composition with explicit protected authorization, and input preservation.
A later unprotected file remains pending and is not selected. Passing ledger
equality does not prove a 015 file exists. The comparison follows the specified
discovered-file equation, not a synthesized contiguous range.

A stale snapshot deliberately remains stale: the pure predicate neither opens
a connection nor re-reads a directory/ledger. The test shows fresh state can
refuse while the old snapshot still passes. The future executor must take its
snapshot under its write lock and check the approved release-file set; this
helper supplies no freshness proof.

### Fresh verification

Windows / Python 3.11.3, isolated
`C:\git\cbz-executor-contracts-20261022`. Full-suite baseline on the exact base:
`python -m pytest -n 4 --dist loadfile -q`:
**2438 passed, 8 skipped in 69.72 s**. New contracts plus existing guard tests:
**73 passed in 3.57 s**. After a test connection-close correction, all
**19 new cases passed in 5.83 s**. Final full suite:
**2457 passed, 8 skipped in 59.20 s**, exactly +19 passes and unchanged skips.
These timings are individual runs, not a performance claim.

Six independent selected mutations were rejected:

| Mutation | Failing cases |
| --- | ---: |
| check missing predecessors only (subset) | 8 |
| check unexpected rows only (reverse subset) | 5 |
| silently filter out later recorded rows | 6 |
| exclude predecessor 014 | 17 |
| include pending 015 as a predecessor | 14 |
| omit the refusal | 14 |

Original source bytes were restored after each run. Raw harness and logs are
outside Git at
`C:\Users\david\Documents\Codex\control\comic102-mutations-20261022.py`
and sibling `comic102-20261022\`. Source remains index LF / working-tree CRLF;
`git diff --check` passes. The SQL statements in tests create only synthetic
ledger fixtures; no test applies even a synthetic 015 payload.

### Remaining executor obligations / next owner

This delivers one reusable precondition and its contracts. The next-applicable
test (section 12.1 step 4, `min(discovered-recorded)==15`) remains explicitly
open, along with verified quiescence/backup, locked plan revalidation,
statement-by-statement SQL, same-transaction ledger insertion, applied
projection reconciliation, commit/postflight/restore, producer cutover and
production execution. No migration015 file, CLI or SQL-application path was
added, and no database/library runtime operation was performed.

Claude independently reviews the exact candidate head and CI; Codex handles
findings and merge after approval and green matching CI. No production rollout
belongs to this preparation slice.

