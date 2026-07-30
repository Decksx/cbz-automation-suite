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

## 6. Guarded source-drift recovery

Implemented a single-job recovery command:

```text
scripts/comic_perceptual_source_drift_recovery.py
```

Default analysis is strictly read-only and reports the exact live file
metadata that must be repeated to authorize apply mode. Apply mode:

1. revalidates the pending job, prior error, remaining attempts, live
   file, and absence of conflicting active jobs;
2. computes the current archive SHA-256, structural inspection, page
   inventory, and page SHA-256 evidence before writing;
3. verifies the CBZ retained the reviewed size and mtime throughout;
4. refreshes exact evidence and releases the existing perceptual job in
   one database transaction;
5. records a `source_drift_recovered` file event;
6. leaves perceptual calculation to the existing bounded worker.

Archive page-hash persistence now participates in a caller-owned
transaction when one exists. Its normal per-archive transaction remains
unchanged for ordinary workers. Archive-hash persistence also accepts a
recovery-only option to suppress the redundant inspection enqueue when
the same atomic operation is already refreshing inspection.

Six focused tests include an injected mid-transaction interruption and
prove that location metadata, archive hash, inspection, page inventory,
page hashes, job state, and audit events all roll back together. The
complete suite passes with 199 tests.

### Production-copy validation

Created and independently checked:

```text
G:\ComicAutomation\TestDatabase\
  inspection-working-source-drift-test-20260730-073818.db
```

Both source and copy returned `PRAGMA quick_check = ok`. Applying the
recovery to the copy refreshed 31 JPEG pages and left job `259622`
pending with its attempt count preserved. The normal bounded perceptual
worker then completed the same job on attempt 2:

```text
processed:                 1
succeeded:                 1
retry scheduled:           0
terminally failed:         0
dHash rows added:         31
pHash rows added:         31
remaining pending:         0
```

The validated copy ended with 25,623 completed jobs, 77 failed jobs,
zero active perceptual jobs, and 1,279,247 rows for each perceptual
algorithm. Its `quick_check` remained `ok`.

The production database fingerprint and state remained unchanged:

```text
completed:                    25,622
failed:                           77
pending / claimed / running:   1 / 0 / 0
dHash / pHash:        1,279,216 / 1,279,216
```

## 7. Production source-drift recovery

The recovery feature passed both GitHub test runs and merged through
pull request 5. Before applying it, created and independently verified:

```text
G:\ComicAutomation\TestDatabase\
  inspection-working-pre-source-drift-recovery-20260730-074448.db
```

The working database and protected backup both returned
`PRAGMA quick_check = ok`. The read-only analysis was repeated
immediately before apply and confirmed:

```text
job:                          259622
archive:                       23258
stored file size:          6,931,965
live file size:           18,225,427
stored pages:                     31 WebP
live pages:                       31 JPEG
conflicting active jobs:           0
recoverable:                    true
```

Apply mode refreshed archive SHA-256, structural inspection, page
inventory, and exact page SHA-256 atomically. The original perceptual
job was then processed through the normal bounded worker:

```text
processed:                 1
succeeded:                 1
retry scheduled:           0
terminally failed:         0
profiled pages:           31
timed milliseconds/page: 28.989
remaining pending:         0
```

Postflight:

```text
quick_check:                         ok
page SHA-256 rows:            2,955,304
dHash Version 1:              1,279,247
pHash Version 1:              1,279,247
completed jobs:                  25,623
failed jobs:                         77
pending / claimed / running:          0
total jobs:                      25,700
eligible archives remaining:      32,554
near-duplicate candidates:             0
recovery event recorded:              yes
protected backup unchanged:           yes
```

Eligibility remained 32,554 because the pending job had already
excluded this archive before recovery; completion replaced that
exclusion with complete Version 1 evidence.

Reports:

```text
G:\ComicAutomation\logs\perceptual-hashing\
  source-drift-analysis-job-259622-preapply-20260730.json
G:\ComicAutomation\logs\perceptual-hashing\
  source-drift-apply-job-259622-20260730.json
G:\ComicAutomation\logs\perceptual-hashing\
  source-drift-retry-job-259622-20260730.json
```

## 8. Latest guarded 5,000-archive batch

The watcher-readiness work merged through pull requests 7, 8, and 11.
After the production checkout was frozen on the tested worker revision,
a fresh protected backup and exact preflight were captured, followed by
another guarded Version 1 perceptual-hash batch.

Protected pre-batch backup:

```text
G:\ComicAutomation\TestDatabase\
  inspection-working-pre-perceptual-batch-20260730-075953.db
```

Batch report:

```text
G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-batch-5000-20260730-080325.json
```

Batch result:

```text
processed:                    5,000
succeeded:                    4,995
terminally failed:                5
retry scheduled:                  0
remaining pending:                0
elapsed seconds:             12,821.790
elapsed hours:                    3.562
throughput:              1,403.86 archives/hour
terminal-failure rate:           0.10%
```

Independent postflight reconciliation confirmed:

```text
quick_check:                         ok
page SHA-256 rows:            2,955,304
dHash Version 1:              1,506,084
pHash Version 1:              1,506,084
completed jobs:                  30,618
failed jobs:                         82
pending / claimed / running:      0 / 0 / 0
total jobs:                      30,700
eligible archives remaining:      27,554
near-duplicate candidates:             0
protected backup unchanged:           yes
```

The batch added exactly 5,000 jobs. Of those, 4,995 completed and five
became terminal failures. dHash and pHash each gained exactly 226,837
rows, page SHA-256 remained unchanged, and eligibility fell by exactly
5,000 archives.

Production phase distribution:

```text
image open and decode:       53.423%
pHash:                       27.785%
dHash:                       14.027%
ZIP entry read:               3.764%
database save:                0.527%
ZIP open and inventory:       0.441%
database lookup:              0.031%
```

The five new failures were legitimate page-image decoding defects, not
queue, database, or orchestration failures. The cumulative terminal-
failure population is now:

```text
archive_corrupt:       40
page_image_corrupt:    42
total:                 82
```

At the measured throughput, the remaining 27,554 eligible archives
represent approximately 19.63 hours of active processing.

## 9. Repository synchronization and verification

After batch postflight completed, local `master` was fast-forwarded
from `b28b0dc` to `6b1f470`. The complete test suite passed:

```text
234 passed in 19.58s
```

The production metrics and latest batch results were then updated in
`docs/implementation_roadmap.md`.

## 10. Pre-next-batch failure audit and protected backup

Generated a fresh read-only terminal-failure audit:

```text
G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-failure-audit-20260730-133444.json
G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-failure-audit-20260730-133444.csv
```

The audit classified all 82 terminal failures without any
unclassified records:

```text
corrupt archives:       40
corrupt page images:    42
missing files:           0
permissions:             0
unsupported formats:     0
unclassified:            0
```

The audit was verified read-only. The working database retained both
its exact byte length and UTC modification timestamp:

```text
length:                  1,931,128,832 bytes
LastWriteTimeUtc ticks:  639210298288983922
unchanged:               true
```

A new protected backup was then created with SQLite's backup API:

```text
G:\ComicAutomation\TestDatabase\
  inspection-working-pre-perceptual-batch-20260730-142231.db
```

Backup verification:

```text
source quick_check before:       ok
backup quick_check:              ok
source quick_check after:        ok
backup length:                   1,931,128,832 bytes
backup LastWriteTimeUtc ticks:   639210397602202172
source metadata unchanged:       true
```

## 11. Next work

Before another guarded 5,000-archive batch:

- review the complete documentation diff and run `git diff --check`;
- commit the authoritative documentation updates and return the
  production checkout to a clean state;
- capture exact read-only preflight counts against both the working
  database and the new protected backup;
- verify repository HEAD, branch, and cleanliness;
- do not enqueue if any active perceptual job, integrity mismatch,
  backup mismatch, or unexpected count is present.

The broader roadmap remains unchanged: complete and audit the Version 1
backfill before introducing immutable archive revisions, provenance
migration, and the minimum local DAL.
