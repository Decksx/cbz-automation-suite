# Production Handoff — 2026-07-30

## Purpose

This document is the authoritative continuation point for the active
Version 1 perceptual-hash backfill. It records the settled repository
state, the production schema-10 baseline, the protected backup, the
currently running guarded batch, and the checks required before any
additional production work.

Do not enqueue another perceptual-hash batch while the batch recorded
here is active. Do not apply another schema migration until this batch
has completed and its postflight has reconciled.

## Repository state

Production checkout:

```text
C:\git\ComicAutomation
```

Verified launch state:

```text
branch:                  master
HEAD:                    49f2f2d83e19f0162aa5bafc74f88885851d3f27
origin/master:           49f2f2d83e19f0162aa5bafc74f88885851d3f27
working tree:            clean
```

Commit `49f2f2d` is the merge commit for pull request 13, which records
the successful production application of migration 010. Pull request
12 merged the reviewed queue-reliability stack at `8c67cc7`.

The complete Windows suite passed with 360 tests after integration and
again after merge. GitHub Actions passed for both pull requests.

The integrated reliability work includes:

```text
7c79bd4  job-worker state hardening
0375804  unique active-job index migration
93b4b9e  atomic enqueue-if-absent helper
80bbae6  atomic enqueue caller migration
8990c12  abandoned-job read-only audit
b391f05  enqueue idempotency audit
b6936b0  duplicate-active read-only preflight
```

All temporary feature worktrees and branches used for that integration
were pruned. The production checkout was not switched or edited to
prepare this handoff.

## Production database and schema

Working database:

```text
G:\ComicAutomation\TestDatabase\inspection-working.db
```

Verified schema state:

```text
applied schema versions:        1 through 10
PRAGMA quick_check:             ok
active jobs, all job types:     0 at batch launch
duplicate active groups:        0 at batch launch
active null-archive jobs:       0 at batch launch
```

Migration 010 added this production index:

```sql
CREATE UNIQUE INDEX idx_jobs_unique_active
    ON jobs(job_type, archive_id)
    WHERE status IN ('pending', 'claimed', 'running')
```

The index is unique and partial, with columns `job_type` and
`archive_id`. Migration validation compared all 17 columns of all
272,074 pre-migration job rows and found no row changes.

Migration reports:

```text
G:\ComicAutomation\logs\perceptual-hashing\
  migration-010-preflight-20260730-194853.json
G:\ComicAutomation\logs\perceptual-hashing\
  migration-010-apply-20260730-194853.json
```

The older pre-migration backup remains protected for migration
recovery:

```text
G:\ComicAutomation\TestDatabase\
  inspection-working-pre-migration-010-20260730-194853.db
```

It intentionally remains at schema versions 1 through 9 and must not
be repurposed as the active batch backup.

## Last completed production baseline

The last fully reconciled batch report is:

```text
G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-batch-5000-20260730-152309.json
```

Completed batch result:

```text
processed:                         5,000
succeeded:                         4,972
terminally failed:                    28
retry scheduled:                       0
elapsed seconds:              12,033.728
throughput:          1,495.80 archives/hour
profiled pages:                   289,390
```

The reconciled production baseline immediately before the active batch
is:

```text
logical archive rows:              59,688
current file locations:            59,377
archive SHA-256 rows:               59,541
archive content signatures:        58,437
page SHA-256 rows:               2,955,304
perceptual job rows:                35,700
perceptual jobs completed:          35,590
perceptual jobs failed:                110
active perceptual jobs:                  0
dHash Version 1 rows:            1,795,474
pHash Version 1 rows:            1,795,474
eligible archives remaining:        22,554
near-duplicate candidates:                0
```

The latest read-only failure audit classifies all 110 terminal
perceptual failures:

```text
archive_corrupt:                    40
page_image_corrupt:                 70
missing files:                       0
permissions:                         0
unsupported formats:                0
unclassified:                        0
```

Audit artifacts:

```text
G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-failure-audit-post-batch-20260730-190646.json
G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-failure-audit-post-batch-20260730-190646.csv
```

These terminal failures are legitimate archive or image defects. They
are not evidence of queue, database, or orchestration failure.

## Protected schema-10 batch backup

The active batch is protected by this new schema-10 backup:

```text
G:\ComicAutomation\TestDatabase\
  inspection-working-pre-perceptual-batch-20260730-211752.db
```

Protected metadata:

```text
size:              2,021,249,024 bytes
modified_time_ns:  1,785,467,979,308,539,900
schema versions:   1 through 10
quick_check:       ok
```

The backup was created with SQLite's online backup API. Verification
proved:

- the working database passed `quick_check` before and after;
- the backup passed `quick_check`;
- source size and modification time did not change;
- source `PRAGMA data_version` did not change;
- the source and backup schema, index, and production counts match;
- the unique active-job index exists on both copies with the exact
  production predicate.

Backup verification report:

```text
G:\ComicAutomation\logs\perceptual-hashing\
  schema-10-backup-verification-20260730-211752.json
```

This file is the protected pre-batch baseline. Do not open it
read/write, apply migrations to it, replace it, or use it as a worker
database.

## Active guarded batch

Batch identifier:

```text
20260730-212247
```

Launch time:

```text
local:  2026-07-30 21:22:55 America/Denver
UTC:    2026-07-31 03:22:55
```

Processes at launch:

```text
supervisor PID:  66144
worker PID:      44852
```

Exact worker command:

```text
C:\Python311\python.exe
  -m comic_automation.archive.perceptual_hash_cli
  --database G:\ComicAutomation\TestDatabase\inspection-working.db
  --limit 5000
  --progress-every 250
  --enqueue-missing
  --profile
  --json-output G:\ComicAutomation\logs\perceptual-hashing\
    perceptual-batch-5000-20260730-212247.json
```

Durable batch artifacts:

```text
status:
G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-batch-5000-20260730-212247.status.json

report:
G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-batch-5000-20260730-212247.json

stdout:
G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-batch-5000-20260730-212247.stdout.log

stderr:
G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-batch-5000-20260730-212247.stderr.log

supervisor stdout:
G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-batch-5000-20260730-212247.supervisor.stdout.log

supervisor stderr:
G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-batch-5000-20260730-212247.supervisor.stderr.log
```

The background supervisor writes status atomically. Its final status
records the worker exit code, report existence, repository cleanliness,
and whether the protected backup's size and timestamp remained
unchanged.

Immediately before spawning the worker, the supervisor repeated the
launch gates:

```text
schema versions:                    1 through 10
quick_check:                                  ok
unique active-job index present:             yes
active perceptual jobs:                        0
duplicate active identity groups:              0
eligible archives:                        22,554
repository branch:                         master
repository clean and synchronized:           yes
```

Full preflight report:

```text
G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-batch-preflight-20260730-212059.json
```

An early read-only health snapshot at 2026-07-30 21:26:04
America/Denver showed:

```text
newly completed jobs:                    38
new terminal failures:                    0
pending jobs from this batch:         4,961
running jobs:                              1
total perceptual jobs:                40,700
dHash / pHash Version 1:  1,798,493 / 1,798,493
worker process:                       present
protected-backup size:         2,021,249,024 bytes
production repository:      clean and synchronized
```

This proves that all 5,000 jobs were created through the schema-10
atomic enqueue path, the worker began completing them, and the hash
algorithms remained aligned. It is an early health observation, not a
postflight result.

## Safe monitoring

Monitoring must remain read-only. The status JSON and logs are the
preferred sources. Reading them does not touch the database:

```powershell
Get-Content -LiteralPath `
  'G:\ComicAutomation\logs\perceptual-hashing\perceptual-batch-5000-20260730-212247.status.json' `
  -Raw

Get-Content -LiteralPath `
  'G:\ComicAutomation\logs\perceptual-hashing\perceptual-batch-5000-20260730-212247.stdout.log' `
  -Tail 40

Get-Content -LiteralPath `
  'G:\ComicAutomation\logs\perceptual-hashing\perceptual-batch-5000-20260730-212247.stderr.log' `
  -Tail 40
```

The worker normally emits a progress line every 250 processed
archives. Because this background launch connects standard output to a
pipe, Python may buffer those progress lines; an empty stdout log is not
by itself a failure while the process is present. A minimal read-only
database snapshot may be used when a live count is required:

```powershell
@'
import sqlite3
from pathlib import Path

database = Path(
    r"G:\ComicAutomation\TestDatabase\inspection-working.db"
).resolve()

with sqlite3.connect(
    database.as_uri() + "?mode=ro",
    uri=True,
    timeout=30.0,
) as connection:
    connection.execute("PRAGMA query_only = ON")
    for status, count in connection.execute(
        """
        SELECT status, COUNT(*)
        FROM jobs
        WHERE job_type = 'hash_archive_pages_perceptual'
        GROUP BY status
        ORDER BY status
        """
    ):
        print(f"{status}: {count}")
'@ | python -
```

Healthy progress means:

- the supervisor and worker processes remain present;
- processed count continues to increase;
- exactly one job is normally running;
- new failures remain a small minority and identify archive/image
  defects;
- stderr remains empty or contains only understood diagnostics;
- the protected backup metadata remains unchanged.

Do not infer a failure merely because a 250-archive interval takes
longer than a previous interval. Archive page counts and decode costs
vary substantially.

Do not launch a second worker, retry failed jobs, recover abandoned
jobs, apply migrations, or edit the working database while this batch
is running.

If the supervisor or worker exits unexpectedly, preserve every artifact
listed above. Do not blindly rerun the launch command. First inspect the
status, stdout, stderr, job states, database integrity, and protected
backup.

## Required postflight

When the supervisor status becomes `completed`, perform a new
read-only reconciliation before any further enqueue:

1. Read the JSON report and require `processed = 5,000`.
2. Reconcile:

   ```text
   succeeded + terminally_failed + retry_scheduled = processed
   ```

3. Require the number actually enqueued to be 5,000. The total
   perceptual-job population should then be:

   ```text
   35,700 + 5,000 = 40,700
   ```

4. Reconcile cumulative outcomes:

   ```text
   completed_after = 35,590 + succeeded
   failed_after    =    110 + terminally_failed
   ```

5. Require no unexpected `pending`, `claimed`, or `running`
   perceptual jobs. If the batch scheduled a retry, settle it through a
   separately reviewed bounded worker operation before declaring the
   batch complete.
6. Recount eligibility with the literal production predicate in
   `ArchivePerceptualHashRepository.enqueue_missing()`. With 5,000
   newly created jobs and no source drift, the expected remaining
   eligible population is:

   ```text
   22,554 - 5,000 = 17,554
   ```

7. Require Version 1 dHash and pHash row counts to remain exactly
   aligned. Reconcile their deltas with the successful profiled page
   population in the batch report.
8. Require page SHA-256 to remain exactly 2,955,304.
9. Require near-duplicate candidates to remain zero unless a separately
   reviewed comparison operation intentionally changed them.
10. Run `PRAGMA quick_check` on the working database and protected
    backup.
11. Run the active-job duplicate audit against both databases. Require
    zero duplicate active groups and verify the unique index remains
    present.
12. Verify the protected backup still has:

    ```text
    size:              2,021,249,024 bytes
    modified_time_ns:  1,785,467,979,308,539,900
    ```

13. Verify the production repository remains clean and synchronized at
    the expected revision unless an intentionally reviewed
    documentation-only merge occurred.
14. Run a fresh read-only perceptual failure audit and preserve its JSON
    and CSV reports. Every new terminal failure must be classified; no
    missing, permission, unsupported, or unclassified category should
    be accepted without investigation.
15. Update the roadmap and development log with the reconciled result,
    throughput, failure rate, phase distribution, hash counts, and
    remaining estimate.

Do not enqueue the next 5,000 archives until every applicable postflight
gate passes.

## Read-only audit commands

The production duplicate-active preflight is:

```powershell
python scripts\comic_job_active_duplicate_audit.py `
  --database G:\ComicAutomation\TestDatabase\inspection-working.db
```

The terminal perceptual-failure audit is:

```powershell
python scripts\comic_perceptual_failure_audit.py `
  --database G:\ComicAutomation\TestDatabase\inspection-working.db `
  --json-output <new-json-path> `
  --csv-output <new-csv-path>
```

Both tools open the database with SQLite `mode=ro` and
`PRAGMA query_only = ON`. Always use new output paths and keep reports
outside the repository.

`scripts\comic_batch_postflight.py` automates steps 1-14 above in one
read-only run. It is strict by default: the protected backup
(`--backup-database`) together with `--expected-backup-size-bytes` and
`--expected-backup-modified-time-ns` is required, and a repository
state git cannot determine is a gate failure. Pass `--production` to
have the command additionally refuse any relaxation flag. Omitting a
required input does not quietly downgrade the run: each gate carries an
explicit `status` of `pass`, `fail`, or `skipped`, `overall_pass` is
false whenever a required gate was skipped, and the top-level `summary`
block lists `failed_gates`, `skipped_gates`, and
`required_gates_skipped`. Exit codes are `0` pass, `1` error, `2` a
gate failed, `3` a required gate was skipped.

Development-only reconciliation against a scratch database with no
protected backup must opt out explicitly and per concern with
`--allow-missing-backup` and `--allow-undeterminable-repository`. Never
use either flag to clear a production batch.

Every path the command touches -- working database, backup, batch
report, failure-audit JSON/CSV, and its own `--json-output` -- is
cross-validated against every other before any database is opened or
any directory is created. An output that is the same file as an input,
an output that collides with another output, and an output path that
already exists are all refused outright, so a postflight run can never
overwrite the batch report it reconciles against or a previous run's
preserved evidence.

## Remaining project sequence

At the last measured throughput, the 22,554 pre-batch eligible
archives represented approximately 15.08 hours of active processing.
If this batch processes 5,000 and achieves similar throughput, about
17,554 eligible archives should remain, or roughly four more guarded
batches including a smaller final batch.

The immediate sequence remains:

1. finish and reconcile the active batch;
2. repeat the guarded backup, preflight, batch, audit, and postflight
   cycle until Version 1 eligibility reaches zero;
3. perform a full-library Version 1 coverage audit;
4. resolve or explicitly disposition all terminal failures;
5. create and independently verify a final database backup;
6. only then begin immutable archive revisions, provenance migration,
   and the minimum local DAL.

The next queue-reliability implementation candidate is safe
abandoned-job recovery, informed by the integrated read-only abandoned
job audit. Leases, heartbeats, and further idempotency protections
remain later work. None of those changes should be applied to the
production database while a perceptual batch is active.

## Operator rule

The database is the authoritative operational state, but the guarded
workflow deliberately uses four independent controls:

```text
clean reviewed code
        +
exact read-only preflight
        +
immutable protected backup
        +
bounded worker with reconciled postflight
```

If any one of those controls is missing or disagrees with the others,
stop and investigate before making another production change.
