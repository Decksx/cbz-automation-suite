# Development Log — 2026-07-31

This log records reconciliation of the guarded 5,000-archive production
batch launched on 2026-07-30, integration of the guarded operations
tooling, and the follow-up coverage, startup-recovery, and WAL-read
hardening.

## 1. Guarded production batch reconciled

Primary batch report:

```text
G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-batch-5000-20260730-212247.json
```

Result:

```text
processed:                              5,000
enqueued:                               5,000
succeeded:                              4,991
terminally failed:                          9
retry scheduled:                            0
elapsed seconds:                       14,728.333
elapsed hours:                              4.091
throughput:                    1,222.13 archives/hour
terminal-failure rate:                    0.18%
profiled archives / pages:        4,991 / 280,518
```

The outcome reconciled exactly:

```text
4,991 succeeded + 9 terminal failures + 0 retries = 5,000 processed
```

Both Version 1 hash populations increased by exactly the 280,518
profiled pages. Page SHA-256 rows did not change.

Post-batch production state:

```text
logical archive rows:                   59,688
current file locations:                 59,377
archive SHA-256 rows:                   59,541
archive content signatures:             58,437
page SHA-256 rows:                    2,955,304
perceptual jobs:                         40,700
completed perceptual jobs:               40,581
failed perceptual jobs:                     119
pending / claimed / running:            0 / 0 / 0
dHash Version 1 rows:                 2,075,992
pHash Version 1 rows:                 2,075,992
eligible archives remaining:             17,554
near-duplicate candidates:                    0
```

Phase distribution:

```text
image open and decode:       54.827%
pHash:                       28.211%
dHash:                       11.535%
ZIP entry read:               4.204%
database save:                0.666%
ZIP open and inventory:       0.506%
database lookup:              0.051%
```

The nine new terminal failures were legitimate page-image decoding
defects. The cumulative failure audit now classifies all 119 terminal
failures as 40 corrupt archives and 79 corrupt images, with no missing,
permission, unsupported-format, or unclassified failures.

The unified postflight passed all 16 gates:

```text
G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-batch-unified-postflight-20260731-014132.json
```

## 2. Guarded operations tooling merged

Pull request 15 merged at `4b3c957`. It added:

- guarded, report-first abandoned-job recovery;
- one-command strict batch postflight;
- full-library Version 1 coverage audit;
- guarded SQLite online backup creation.

The combined pre-merge validation passed 488 tests on Windows. Both
GitHub Actions test runs passed.

## 3. Follow-up reliability hardening merged

Pull request 16 merged at `d8f0c56`. It integrated three independently
reviewed branches plus a final completion-gate correction:

- never-enqueued eligible archives are neutral backlog during the active
  bounded backfill, not an orchestration defect;
- strict final coverage mode verifies that both `incomplete` and `stale`
  populations are zero;
- service startup observes and warns about age-stale jobs but never
  rewrites them unattended;
- read-only audits share one WAL-aware consistent-snapshot helper using
  `PRAGMA data_version` around a single deferred read transaction;
- database file and sidecar fingerprints remain clearly labelled
  diagnostics rather than concurrency proof.

Validation:

```text
focused integration tests:       62 passed
complete Windows suite:         537 passed
GitHub Actions pytest runs:        2 passed
git diff --check:                    clean
```

## 4. Merged production validation

The merged read-only tools were run against the settled working database
and the protected schema-10 backup. Both duplicate-active preflights
reported:

```text
quick_check:                         ok
data_version before / after:      2 / 2
active jobs:                          0
duplicate active groups:              0
active null-archive jobs:              0
unique active index present:        true
main database file unchanged:       true (diagnostic)
```

The abandoned-job audit found zero stale claimed/running jobs:

```text
G:\ComicAutomation\logs\perceptual-hashing\
  abandoned-job-audit-merged-20260731-060401.json
```

The corrected default coverage audit reported the remaining 17,554
eligible archives as expected never-enqueued backlog and printed only a
20-ID sample while retaining every ID in JSON/CSV:

```text
G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-coverage-audit-merged-20260731-060414.json
G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-coverage-audit-merged-20260731-060414.csv
```

Coverage populations reconciled exactly:

```text
complete:        40,581
incomplete:      17,554
failed:             119
stale:                 0
ineligible:         1,434
partition:        59,688 == 59,688
```

The same production database was then checked in strict
`--expect-backfill-complete` mode. It exited 2 and reported:

```text
final backfill gate: false
blocking incomplete: 17,554
blocking stale:           0
```

This is the required behavior while the backfill remains incomplete.
No production database row, protected backup, archive, or queue state
was changed by these validations.

## 5. Next guarded work

No perceptual jobs are active. Before another bounded batch:

1. create and independently verify a fresh protected backup of the
   settled schema-10 working database;
2. capture an exact preflight baseline with 17,554 eligible archives;
3. require clean synchronized `master` at `d8f0c56` or its reviewed
   documentation successor;
4. run one bounded 5,000-archive batch with profiling and preserved
   stdout/stderr/report artifacts;
5. run the merged strict postflight and repeat the failure and coverage
   audits.

At the last measured throughput, the remaining active processing time is
approximately 14.36 hours. This is a workload estimate, not a calendar
completion promise.
