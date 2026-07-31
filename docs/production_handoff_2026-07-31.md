# Production Handoff — 2026-07-31

## Purpose

This is the authoritative continuation point for the Version 1
perceptual-hash backfill after reconciliation of the guarded batch
launched on 2026-07-30 and merger of the guarded operations and
WAL-aware audit hardening.

The production database is healthy and idle. No perceptual jobs are
pending, claimed, or running. Do not enqueue the next batch until a fresh
protected backup and exact preflight have been captured from this settled
state.

## Repository state

Production checkout:

```text
C:\git\ComicAutomation
```

Reviewed operational code baseline:

```text
branch:            master
HEAD:              d8f0c565df0a0f3b56ff16b7e22fb05356d547f5
origin/master:     d8f0c565df0a0f3b56ff16b7e22fb05356d547f5
working tree:      clean before this documentation update
```

Pull request 15 merged the guarded operations tooling at `4b3c957`.
Pull request 16 merged the coverage-language correction, removal of
unattended startup recovery, shared WAL-aware read guards, and the strict
final coverage gate at `d8f0c56`.

Validation for pull request 16:

```text
focused integration tests:       62 passed
complete Windows suite:         537 passed
GitHub Actions pytest runs:        2 passed
git diff --check:                    clean
```

Any later merge containing only this handoff, the 2026-07-31 development
log, and reconciled roadmap figures is a documentation-only successor to
the operational code baseline above.

## Working database

```text
G:\ComicAutomation\TestDatabase\inspection-working.db
```

Settled metadata after the completed batch:

```text
size:              2,108,760,064 bytes
modified_time_ns:  1,785,482,789,644,110,600
schema versions:                   1 through 10
quick_check:                                ok
unique active index:  idx_jobs_unique_active
```

The merged all-job-type preflight verified `quick_check = ok`, an
unchanged `PRAGMA data_version` pair, zero active jobs, zero duplicate
active identity groups, and zero active null-archive jobs.

## Latest protected backup

The protected backup used for the completed batch is:

```text
G:\ComicAutomation\TestDatabase\
  inspection-working-pre-perceptual-batch-20260730-211752.db
```

Protected metadata:

```text
size:              2,021,249,024 bytes
modified_time_ns:  1,785,467,979,308,539,900
schema versions:                   1 through 10
quick_check:                                ok
```

It remained unchanged throughout the batch and postflight. The merged
WAL-aware duplicate-active audit also passed against it with zero active
or duplicate jobs.

This backup predates the now-completed batch. Preserve it, but do not
reuse it as the protected backup for the next batch. Create a fresh
schema-10 backup from the settled working database first.

## Completed guarded batch

Batch identifier:

```text
20260730-212247
```

Primary artifacts:

```text
G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-batch-5000-20260730-212247.json
G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-batch-5000-20260730-212247.stdout.log
G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-batch-5000-20260730-212247.stderr.log
G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-batch-5000-20260730-212247.status.json
```

Result:

```text
processed:                              5,000
enqueued:                               5,000
succeeded:                              4,991
terminally failed:                          9
retry scheduled / pending:                  0
elapsed seconds:                       14,728.333
elapsed hours:                              4.091
throughput:                    1,222.13 archives/hour
terminal-failure rate:                    0.18%
profiled archives / pages:        4,991 / 280,518
```

The report reconciled exactly. Both Version 1 hash populations increased
by 280,518 rows, matching the profiled page total. Page SHA-256 remained
unchanged. The protected backup retained its counts and metadata.

Unified postflight:

```text
G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-batch-unified-postflight-20260731-014132.json
```

All 16 postflight gates passed.

## Current production counts

```text
logical archive rows:                   59,688
current file locations:                 59,377
archive SHA-256 rows:                   59,541
archive content signatures:             58,437
page SHA-256 rows:                    2,955,304
perceptual job rows:                     40,700
perceptual jobs completed:               40,581
perceptual jobs failed:                     119
pending / claimed / running:            0 / 0 / 0
all active jobs:                              0
duplicate active identity groups:            0
active null-archive jobs:                     0
dHash Version 1 rows:                 2,075,992
pHash Version 1 rows:                 2,075,992
eligible archives remaining:             17,554
near-duplicate candidates:                    0
```

At the last measured throughput, the remaining eligible population
represents approximately 14.36 hours of active processing. This is not a
calendar completion promise.

## Terminal failures

The cumulative 119 terminal failures are fully classified:

```text
archive_corrupt:         40
page_image_corrupt:      79
missing files:            0
permissions:              0
unsupported formats:      0
unclassified:             0
```

The nine failures added by the latest batch are legitimate page-image
decoding defects. No queue, database, or orchestration failure occurred.

## Merged read-only validation

Abandoned-job audit:

```text
G:\ComicAutomation\logs\perceptual-hashing\
  abandoned-job-audit-merged-20260731-060401.json
```

It found zero stale claimed/running jobs. Startup no longer automatically
recovers an age-stale job; it only warns. Without leases or heartbeats,
age cannot distinguish a dead worker from legitimate long-running work.
Recovery must use the guarded report-first CLI with stopped workers,
expected count, and exact snapshot digest.

Coverage audit:

```text
G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-coverage-audit-merged-20260731-060414.json
G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-coverage-audit-merged-20260731-060414.csv
```

Populations:

```text
complete:        40,581
incomplete:      17,554
failed:             119
stale:                 0
ineligible:         1,434
partition:        59,688 == 59,688
```

Default mode correctly calls all 17,554 remaining eligible archives the
never-enqueued backlog. The console prints only a 20-ID sample and an
explicit omitted count; JSON and CSV retain the complete list.

Strict `--expect-backfill-complete` mode was also exercised against the
same production state. It exited 2 with `blocking_incomplete_count =
17,554`, `blocking_stale_count = 0`, and a false final gate. This proves
the final audit cannot pass while work remains, even if every remaining
archive eventually has job history.

## WAL-aware audit rule

Every migrated multi-query read-only audit now uses this authoritative
sequence:

```text
PRAGMA data_version before
BEGIN deferred read transaction
PRAGMA quick_check plus every report query
END
PRAGMA data_version after
```

A changed data-version pair rejects the report. Main database file size
and mtime, and WAL/SHM sidecar presence, remain diagnostic evidence only:
a WAL-only commit can leave the main file unchanged, while read-only
connection activity itself can create or touch sidecars.

## Next guarded batch prerequisites

The system is idle. Before enqueueing another 5,000 archives:

1. require clean synchronized `master` containing the documentation-only
   successor to `d8f0c56`;
2. confirm no worker or old batch process is running;
3. create a fresh protected schema-10 backup with the guarded backup CLI;
4. independently verify source and backup integrity, schema, table counts,
   and duplicate-active state;
5. capture exact working-database and backup metadata;
6. require these launch counts:

```text
perceptual jobs:              40,700
completed / failed:   40,581 / 119
pending / claimed / running: 0 / 0 / 0
dHash / pHash V1: 2,075,992 / 2,075,992
page SHA-256:              2,955,304
eligible archives:            17,554
near-duplicate candidates:         0
```

7. launch one bounded 5,000-archive batch with profiling and unique JSON,
   stdout, stderr, and status paths;
8. preserve the new backup and run strict postflight, failure audit,
   default coverage audit, and repository checks before another batch.

Do not use `--expect-backfill-complete` as the normal mid-backfill gate.
Use it only after ordinary eligibility reaches zero; at that point it
must return exit code 0 before the backfill is signed off.

## Remaining project sequence

1. repeat the guarded backup, preflight, batch, and postflight cycle until
   Version 1 eligibility reaches zero;
2. run strict full-library coverage audit and disposition every terminal
   failure;
3. create and independently verify a final database backup;
4. only then begin immutable archive revisions, provenance migration, and
   the minimum local DAL;
5. design leases and heartbeats before restoring any unattended abandoned-
   job recovery behavior.

## Operator rule

```text
clean reviewed code
        +
WAL-aware read-only preflight
        +
fresh immutable protected backup
        +
bounded worker with reconciled postflight
```

If any control is missing, skipped, or disagrees with the others, stop
before making another production change.
