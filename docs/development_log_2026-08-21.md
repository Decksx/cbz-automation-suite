# Development log — 2026-08-21

Step 1 closed. The shared classification contract and the rebuilt coverage
audit both merged, the final pre-revision backup was taken and
independently verified, and intake resumed.

## What landed

| PR | merge commit | subject |
| --- | --- | --- |
| #76 | `04a464b239407e9249f54e450226229505fbc59d` | shared archive-classification contract |
| #77 | `5c43c4eabe229a18acb7a76705e663a8d35450ee` | three-measurement coverage accounting |

Neither carried a migration, so no production database change followed
either merge. Schema stayed at migrations 1–13 throughout. The full suite
went 1,608 → 1,637 across the two merges.

## The accounting model that replaced the populations

The old audit published one coverage number, and that number **rose when a
drive was unplugged**: an archive going missing shrank the denominator.
Pages that were hashed do not become unhashed when a volume is unmounted,
so a number that moves on that observation was not measuring coverage.

Three measurements now, with different rules about what may move them:

- **historical** — frozen `archive_pages` denominator; nothing may move it;
- **operational** — pages excluded only for recorded retirement or
  supersession; observations may never move it;
- **accountability** — every identity, including the 1,256 zero-page ones
  that neither ratio can describe.

`never_enqueued_backlog` was removed rather than renamed. It was a
*positive predicate* — "eligible, zero coverage, no job history" — so it
confidently reported archives that were fully explained by a path refusal
while staying silent on the ones that had no explanation at all. Its
replacement, `unexplained`, is residue only: no predicate produces it, and
a non-zero count fails the run.

## Guards that were not load-bearing

Thirteen sabotages were run against the audit, each applied alone with the
file restored from git in between. **Two failed nothing on the first pass.**

- disabling the operational-numerator invariant;
- rewiring `run_audit` to reconcile identities against its own rows.

Both read as correct and both were unreachable defensive code, because the
tests asserted the *measurements* directly and so never exercised the
runtime checks over a wrong measurement. This is the third time on this
project that a guard has been proven inert only by bypassing it, and the
first two were also written by someone confident they were load-bearing.

Tests were added and the sabotages re-run. All thirteen now fail named
tests.

## Two proof defects found in review

**A test that never tested what it was named.**
`test_operational_coverage_is_unmoved_by_an_unavailable_root` declared an
unrelated absent sibling directory while the archives stayed under
`tmp_path/lib`. That put them outside every declared root, which is
`undeclared_scope` reached by a different code path — so unavailability was
never exercised. The archives now stay beneath the declared root and the
root itself is removed, and the emitted availability value is asserted
*before* any coverage claim so the test cannot drift back.

**An optional argument that silently weakened an invariant.**
`check_invariants()` accepted `identities=None` and fell back to
reconciling the classification against its own ids. That makes the expected
and classified sets equal by construction, so both the missing and the
extra count are always zero — the "every archive" invariant would report
PASS while blind to the exact failure the census was added to catch. The
argument is now required and the fallback is gone.

## The archive 45217 trap

Archive 45217 is the only retirement on record, and it has **zero covered
pages**. Any test of the operational *numerator* that uses it will pass
whether or not the numerator is subtracted correctly, because there is
nothing to subtract.

This is not hypothetical: the first version of the numerator tests used a
zero-covered archive and could not distinguish a correct implementation
from one that shrank only the denominator. The tests now use a partially
covered archive — four pages, two hashed — so numerator, denominator and
outstanding count each move by a different known amount.

Recorded because production data will keep hiding this bug for as long as
45217 remains the only retirement.

## Final pre-revision checkpoint

The `2026-08-20.pre-migration-013` backup predates migration 013 and is
therefore not the pre-revision baseline. The final one is:

```text
G:\ComicAutomation\backups\inspection-working.2026-08-21.post-audit-pre-revision.db
bytes   2,378,436,608
sha256  4c0654b7b4c88cebc3cbcfda3af72e50ac85dac7179def5d7be679b83486c17a
```

Verified **independently of the backup tool's own report**, on the
principle that a tool verifying its own output is one claim rather than
two: `quick_check` and `integrity_check` both `ok`, all 71 schema objects
compared verbatim, migrations exactly 1–13, and eleven headline counts
equal between source and backup. The source was byte-unchanged by the
backup run.

## Intake resumed

Started one at a time, each verified before the next: the watcher
(routing v2 `off`, 55 rules, staging-first destinations), then the GUI.

Worth recording: the live `routing.json` is staging-first
(`X:\_staging\Comix`), while `routing.json.bak-2026-08-19` is the **older
direct-to-library** config (`X:\Comix`). Restoring the backup over the live
file would have been a regression, not a recovery. `routing.json` is
gitignored, so neither a merge nor a fresh clone can establish this — it
has to be checked on the machine.

## Metrics reconciliation

Every current-state row in the roadmap's production-metrics table was
re-measured against the immutable pre-revision backup. Four had drifted:

| row | recorded | measured |
| --- | ---: | ---: |
| Perceptual job rows | 45,700 | 58,029 |
| Page SHA-256 rows | 2,955,304 | 2,955,391 |
| Exact duplicate groups | 886 | 888 |
| Redundant copies in those groups | 1,085 | 1,090 |

The job-row figure was the one that could not be true: 45,700 rows cannot
contain 57,896 completed jobs. It was a mid-backfill snapshot that was
never updated. The reconciled population is 58,029 = 57,896 completed +
132 failed + 1 cancelled, over 58,029 distinct archives.

The table is now split by source — database facts from the backup,
filesystem observations from the scoped audit, and the 2026-08-19 batch
figures as a dated historical record — because mixing them is what let a
mid-backfill number sit in a "current" table for four days.
