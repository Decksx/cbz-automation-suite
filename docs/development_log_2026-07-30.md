# Development Log — 2026-07-30

This log records the first guarded 5,000-archive production batch after
the Phase 5 performance work, its postflight reconciliation, and the
read-only terminal-failure audit.

## 1. Optimized production batch

The batch used the production perceptual-hashing worker with:

- immutable process-local pHash constant caching;
- exact Version 1 regression coverage;
- optional in-worker and repository phase timing;
- the existing bounded persistent-job workflow;
- a fresh protected SQLite backup and exact preflight assertions.

Preflight:

```text
completed jobs:                  20,631
failed jobs:                         69
pending / claimed / running:          0
total jobs:                      20,700
dHash Version 1:              1,028,793
pHash Version 1:              1,028,793
eligible archives remaining:      37,554
near-duplicate candidates:             0
quick_check:                         ok
```

Protected backup:

```text
G:\ComicAutomation\TestDatabase\
  inspection-working-pre-optimized-batch-20260729-214419.db
```

Batch outcome:

```text
processed:                              5,000
enqueued:                               5,000
succeeded:                              4,991
terminally failed:                          8
retry scheduled:                            1
remaining pending:                          1
elapsed seconds:                       14,509.332
elapsed hours:                              4.030
throughput:                    1,240.58 archives/hour
terminal-failure rate:                    0.16%
retry rate:                               0.02%
```

The outcome reconciled exactly:

```text
4,991 succeeded + 8 terminal failures + 1 retry = 5,000 processed
```

Primary report:

```text
G:\ComicAutomation\logs\perceptual-hashing\
  optimized-batch-5000-20260729-214419.json
```

## 2. Production phase timing

The worker successfully profiled 4,991 archives, 250,423 pages, and
243,327,647,254 bytes. The nine unsuccessful jobs are explicitly
reported as unprofiled.

```text
image open and decode:       51.257%
pHash:                       29.799%
dHash:                       13.972%
ZIP entry read:               3.966%
database save:                0.543%
ZIP open and inventory:       0.436%
database lookup:              0.027%
```

Timed phases accounted for 14,380.368 seconds, with 128.964 seconds
unattributed; together they match the 14,509.332-second batch elapsed
time. The run averaged 57.424 timed milliseconds per successfully
profiled page.

The larger sample confirms the earlier 100-archive production result:
image decoding remains the largest measured cost. pHash and dHash are
material CPU costs, while ZIP reads and SQLite lookup/save remain small.

## 3. Postflight reconciliation

The independent read-only postflight produced:

```text
quick_check:                         ok
page SHA-256 rows:            2,955,304
dHash Version 1:              1,279,216
pHash Version 1:              1,279,216
completed jobs:                  25,622
failed jobs:                         77
pending / claimed / running:      1 / 0 / 0
total jobs:                      25,700
eligible archives remaining:      32,554
near-duplicate candidates:             0
```

Every expected count matched. The eligible count fell by exactly 5,000,
dHash and pHash counts remained aligned, page SHA-256 did not change,
and the protected backup retained its complete pre-batch state and
passed its own `quick_check`. The repository remained clean throughout
the run.

At the measured throughput, the remaining eligible population
represents approximately 26.2 hours of active processing time. This is
not a calendar completion estimate.

## 4. Read-only terminal-failure audit

The new terminal-failure audit was run against the production database
with SQLite `mode=ro` and `PRAGMA query_only`. It verified database size
and modification time before and after the audit.

```text
terminal failures:             77
corrupt archives:              40
corrupt images:                37
missing files:                  0
permissions:                    0
unsupported formats:            0
unclassified:                   0
database changed:              no
```

All eight new terminal failures are `page_image_corrupt`: truncated or
unidentifiable JPEGs plus two WebP decoder failures. None indicate queue
or database orchestration defects.

Reports:

```text
G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-failure-audit-20260730-020114.json
G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-failure-audit-20260730-020114.csv
```

## 5. Pending retry requires reinspection

Job `259622`, archive `23258`, is pending after one attempt:

```text
Stored page inventory does not match the current archive
for archive_id=23258.
```

The mismatch is legitimate source drift, not a transient worker error:

```text
stored file size:       6,931,965 bytes
current file size:     18,225,427 bytes
stored page inventory: 31 WebP pages
current page inventory: 31 JPEG pages
```

The file changed after its last database observation. Retrying the same
stale inventory unchanged is not expected to succeed. The correct
recovery path is:

1. rediscover the current file metadata;
2. rerun exact page inspection and hashing for the new content;
3. supersede or safely resolve the stale pending perceptual job;
4. enqueue perceptual hashing against the refreshed inventory.

No retry or database mutation was performed during this audit.

## 6. Next work

Before starting another guarded 5,000-archive batch:

- finish and merge the terminal-failure audit tooling;
- add a guarded source-drift recovery path for archive `23258`;
- verify the refreshed archive is eligible under the production query;
- capture a fresh protected backup and updated preflight counts.

The broader roadmap remains unchanged: complete and audit the Version 1
backfill before introducing immutable archive revisions, provenance
migration, and the minimum local DAL.
