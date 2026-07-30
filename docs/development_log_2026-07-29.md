# Development Log — 2026-07-29

## 1. Starting point

Work resumed on the production Version 1 perceptual-hash backfill after
the archive inspection, quarantine, archive SHA-256, exact page
SHA-256, and initial perceptual-hashing work documented in
`docs/development_log_2026-07-28.md`.

The repository began this session on:

```text
branch: feature/archive-inspection
HEAD:   5ae7d18 docs: document archive review UI design
```

No executable code changed during the work recorded here. The
production database was updated only through the existing
`hash_archive_pages_perceptual` queue and worker.

## 2. Stale-job recovery validation

Compared the recovered working database with the pre-recovery backup:

```text
working:
  G:\ComicAutomation\TestDatabase\inspection-working.db

backup:
  G:\ComicAutomation\TestDatabase\
    inspection-working-pre-stale-recovery-20260729-031750.db
```

Both databases passed `PRAGMA quick_check`.

The backup retained one running job:

```text
job_id:    238655
archive_id: 2282
status:    running
```

The working database showed that job recovered and completed:

```text
working completed jobs: 10,540
backup completed jobs:   10,539

working running jobs: 0
backup running jobs:  1

working dHash/pHash: 536,073 / 536,073
backup dHash/pHash:  536,051 / 536,051
```

The exact eligibility query used by
`ArchivePerceptualHashRepository.enqueue_missing()` returned identical
eligible archive-ID sets:

```text
working eligible:   47,654
backup eligible:    47,654
working-only IDs:        0
backup-only IDs:         0
```

Determination:

```text
READY_CURRENT_ELIGIBILITY_CONFIRMED
```

This established that stale-job recovery completed safely and did not
change the next eligible archive set.

## 3. First guarded 5,000-archive perceptual batch

Preflight state:

```text
page SHA-256:          2,955,304
dHash Version 1:         536,073
pHash Version 1:         536,073
completed jobs:           10,540
failed jobs:                  60
pending/claimed/running:       0
total jobs:               10,600
eligible archives:        47,654
near-duplicate candidates:     0
quick_check:                  ok
```

The guarded runner:

- verified exact expected database counts;
- verified the stale-recovery backup state;
- enqueued exactly 5,000 eligible archives;
- processed through the production perceptual worker;
- reconciled every outcome;
- ran a post-batch read-only audit;
- verified the protected backup remained unchanged.

Result:

```text
processed:             5,000
succeeded:             4,993
terminally failed:         7
retry scheduled:           0
remaining pending:         0
elapsed seconds:  15,985.194541
throughput:       ~1,126 archives/hour
failure rate:          0.14%
```

The seven terminal failures were legitimate malformed/unreadable image
pages:

| Job | Page | Result |
| ---: | --- | --- |
| 248111 | `168.jpg` | truncated image, 1 byte not processed |
| 248330 | `001.jpg` | Pillow could not identify the image |
| 248979 | `9Cloud.us_0222-P109.jpg` | truncated image, 37 bytes not processed |
| 248981 | `008.jpg` | truncated image, 16 bytes not processed |
| 249945 | `Kui_Communication_0060.jpg` | truncated image, 6 bytes not processed |
| 251510 | `001.jpg` | Pillow could not identify the image |
| 251511 | `001.jpg` | Pillow could not identify the image |

Pillow also reported nonfatal palette-transparency and large-image
warnings. Those archives completed normally.

Post-batch state:

```text
page SHA-256:          2,955,304
dHash Version 1:         776,996
pHash Version 1:         776,996
completed jobs:           15,533
failed jobs:                  67
pending/claimed/running:       0
total jobs:               15,600
eligible archives:        42,654
near-duplicate candidates:     0
quick_check:                  ok
```

Reconciliation:

```text
4,993 succeeded + 7 terminal failures + 0 retries = 5,000 processed
47,654 pre-eligible - 5,000 processed = 42,654 post-eligible
```

Each perceptual algorithm gained 240,923 rows.

Reports:

```text
G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-batch-5000-20260729-092253.json

G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-batch-5000-20260729-092253-full.json
```

## 4. Repository-state investigation

The guarded run began with a clean working tree. During the long batch,
two documentation files changed:

```text
M docs/implementation_roadmap.md
M docs/overview.md
```

The full diff showed coherent documentation-only updates describing
the SQLite operational core and production hashing status. No code,
SQL, migrations, configuration, queue logic, or hashing implementation
changed.

Filesystem timestamps placed both edits at approximately 10:02 AM
during the batch:

```text
docs/implementation_roadmap.md  2026-07-29 10:02:13
docs/overview.md                2026-07-29 10:02:25
```

The edits contained the pre-batch metrics and were therefore recognized
as intentional concurrent documentation work, not hashing side
effects. Subsequent repository checks showed only these two files.

## 5. Post-crash recovery check

The interactive session later crashed, but no worker was active at the
time.

A fresh read-only database audit reproduced the first batch's exact
post-state:

```text
quick_check:                  ok
page SHA-256:          2,955,304
dHash Version 1:         776,996
pHash Version 1:         776,996
completed jobs:           15,533
failed jobs:                  67
pending/claimed/running:       0
eligible archives:        42,654
near-duplicate candidates:     0
active jobs:                   []
```

No stale or interrupted job required recovery.

## 6. Fresh pre-batch backup

Created a new SQLite online backup:

```text
G:\ComicAutomation\TestDatabase\
  inspection-working-pre-perceptual-batch-20260729-143552.db
```

Validation:

```text
source quick_check:  ok
backup quick_check:  ok
backup size:         1,704,202,240 bytes
```

The backup matched the exact verified post-crash state and was used as
the protected baseline for the next guarded batch.

## 7. Second guarded 5,000-archive perceptual batch

The second guarded runner additionally required the repository to
contain exactly the two known documentation modifications and required
the fresh backup to match the verified working database.

Result:

```text
processed:             5,000
succeeded:             4,998
terminally failed:         2
retry scheduled:           0
remaining pending:         0
elapsed seconds:  16,013.667346
throughput:       ~1,124 archives/hour
failure rate:          0.04%
```

Terminal failures:

| Job | Archive/page | Result |
| ---: | --- | --- |
| 254659 | `Naughty or Nice Charmed by the Seductive Pink Santa Ch.16.cbz` / `010.webp` | Pillow could not identify the image |
| 256848 | `Pizza Boy Vs Milfs\5.cbz` / `001.jpg` | Pillow could not identify the image |

Large-image decompression-bomb warnings and a palette-transparency
warning were nonfatal.

Post-batch state:

```text
page SHA-256:          2,955,304
dHash Version 1:       1,025,682
pHash Version 1:       1,025,682
completed jobs:           20,531
failed jobs:                  69
pending/claimed/running:       0
total jobs:               20,600
distinct job archives:    20,600
eligible archives:        37,654
near-duplicate candidates:     0
quick_check:                  ok
```

Reconciliation:

```text
4,998 succeeded + 2 terminal failures + 0 retries = 5,000 processed
42,654 pre-eligible - 5,000 processed = 37,654 post-eligible
```

Each perceptual algorithm gained 248,686 rows. Combined stored
Version 1 perceptual evidence is now:

```text
1,025,682 dHash + 1,025,682 pHash = 2,051,364 rows
```

All runner validations passed:

```text
reconciled:          true
validation_passed:   true
validation_failures: []
Python exit code:    0
```

Protected-backup verification:

```text
before size:       1,704,202,240
after size:        1,704,202,240
before/after mtime: identical
unchanged:         true
```

Repository verification:

```text
before:
  M docs/implementation_roadmap.md
  M docs/overview.md

after:
  M docs/implementation_roadmap.md
  M docs/overview.md

unchanged: true
```

Reports:

```text
G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-batch-5000-20260729-143926.json

G:\ComicAutomation\logs\perceptual-hashing\
  perceptual-batch-5000-20260729-143926-full.json
```

## 8. Combined progress

Across the two guarded batches:

```text
processed:             10,000
succeeded:              9,991
terminally failed:          9
retries:                     0
dHash rows added:      489,609
pHash rows added:      489,609
eligible reduction:     10,000
```

At the latest measured rate, the 37,654 remaining eligible archives
represent approximately 33.5 active processing hours, or seven full
5,000-archive batches plus one final partial batch.

The eligible count intentionally excludes archives with terminal
failed jobs. Final coverage auditing must reconcile both the remaining
eligible population and all 69 failed archives.

## 9. Architecture and optimization decisions

The implementation roadmap was expanded and made concrete around the
following decisions:

- complete and audit the Version 1 perceptual backfill before changing
  archive identity/foreign-key structure;
- model immutable archive revisions as unique byte states, separate
  from filesystem observations;
- use one authoritative current-revision pointer with structural
  ownership enforcement;
- preserve distinct archive IDs for byte-identical archives during the
  revision migration;
- pair immutable revisions with guarded retention/pruning;
- add revision-aware provenance without prematurely building a general
  invalidation engine;
- converge database access on a local DAL before applying revision
  migrations;
- add job leases, deterministic idempotency keys, and explicit failure
  classification;
- add targeted archive resource limits and streaming/local-stage
  mutation workflows;
- add a golden corpus, focused property tests, and crash-recovery
  tests;
- keep a lightweight decisions log rather than introducing formal ADR
  ceremony.

### Agreed Phase 5 performance sequence

Before resuming production backfill:

1. Run a read-only exact-SHA reuse opportunity analysis.
2. Freeze exact Version 1 dHash/pHash regression vectors.
3. Cache immutable pHash cosine/normalization constants without
   changing arithmetic or output.
4. Add optional worker/repository phase timing.
5. Implement bulk version-aware exact-hash reuse if material.
6. Evaluate selective missing-page hashing if partial reuse is common.
7. Resume guarded 5,000-archive batches after regression and
   database-copy validation.

The reuse analysis must report:

```text
reusable_pages
fully_satisfied_archives
partially_satisfied_archives
pages_still_requiring_decode
archives_still_requiring_processing
```

Partial reuse is not credited as avoided work under the current
archive-level worker because an archive with any missing evidence still
causes every image page to be decoded.

Version 1 regression uses exact digest equality:

```text
cached_digest == uncached_digest
```

Static analysis identifies pure-Python pHash as the strongest current
CPU-bottleneck hypothesis, but optional phase timings will establish
the real wall-clock breakdown before broader optimization.

## 10. Next action

Do not enqueue another production batch yet.

The next action is the read-only exact-SHA reuse opportunity analysis
against the current verified state. It must distinguish fully
satisfied archives from partial reuse and make no database changes.

After the analysis, use the measured opportunity to decide whether
bulk reuse and selective missing-page hashing are justified before the
next guarded backfill batch.
