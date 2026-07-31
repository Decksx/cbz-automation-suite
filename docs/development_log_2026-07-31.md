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

## 6. Post-handoff code hardening

This work is independent of the perceptual-hash backfill. No production
database, backup, archive, or queue row was touched. The production
system remained idle throughout.

### 6.1 Verification of already-shipped enqueue work

Before writing anything new, the state of the job-enqueue duplicate-row
work was verified directly against `master` @ `fe8897b` rather than
taken from the audit's recommendations. All four steps of
`docs/job_enqueue_idempotency_audit.md` section 7 were already
implemented:

```text
migration 010:            idx_jobs_unique_active, exact recommended predicate
enqueue_if_absent():      ON CONFLICT ... DO NOTHING form, EnqueueOutcome enum
call sites migrated:      5 of 5 (no direct JobQueue.enqueue() outside the
                          queue module)
failed-status policy:     resolved as "keep and document as intentional"
required tests:           72 passing across the three test files
```

The audit document has been updated to record this rather than
continuing to read as though the work were outstanding. No new code was
needed.

The same check against `docs/jobs_worker_retry_audit.md` found two of
its three low-risk items already implemented (`JobWorkerStateError`
wrapping the nested `mark_failed()` failure; `permanent=True` with a
dedicated category for the missing-handler path). The third — an inline
comment recording that `claim_next()` counts a claim as an attempt even
if the worker dies before the handler runs — was missing and has been
added.

### 6.2 Archive I/O resource limits

`comic_automation/archive/perceptual_hashing.py`:

- `Image.MAX_IMAGE_PIXELS` is now pinned explicitly at `89_478_485`
  with a documented rationale, instead of inheriting whatever default
  the installed Pillow ships. The comment records that the warning band
  (to roughly 179 MP) is deliberately non-terminal, that the hard error
  above it was already handled, and that the assignment is process-wide
  rather than scoped to this module.
- A new `MAX_PAGE_UNCOMPRESSED_BYTES` (200 MiB) bounds
  `archive.read(entry)` before it allocates, raising
  `PermanentJobError(category="page_image_too_large")`. This closes the
  gap the audit flagged against `inspection.py`'s double-checked 1 MiB
  `ComicInfo.xml` cap and `page_hashing.py`'s bounded chunk streaming.

Both limits are safety ceilings far above any legitimate library page —
a 600 DPI letter-size scan is roughly 34 MP — not operational tuning.
The Version 1 hash implementation, decode path, resize behavior, and
digest semantics are entirely unchanged; nothing here can alter a
stored digest.

Writing the size-cap test surfaced a detail worth recording: `zipfile`
recomputes `ZipInfo.file_size` from the actual written payload at write
time, so an archive whose header lies about a member's size cannot be
constructed with `writestr()` alone. The test injects the mismatch at
read time through `infolist()` instead — the same call
`calculate_perceptual_hashes()` uses to build its entry list.

### 6.3 Rewrite-path staleness guards

All three archive-replacement paths now snapshot the target's
size/mtime and re-verify immediately before the destructive step,
reusing the before/after `stat()` pattern already proven in the
read-only hashing path:

```text
cbz_sanitizer._write_cbz_with_comicinfo   -> raises into existing retry loop
cbz_library_maintenance.write_comicinfo   -> abandons, original intact
cbz_library_maintenance.pack_image_folder -> abandons, counts stats.errors
```

Detection is now consistent; recovery deliberately is not. The
sanitizer has a retry loop and uses it. `cbz_library_maintenance.py`
has no retry logic anywhere in the file, and none was added — drift
there abandons the operation rather than silently retrying under a
different policy than the rest of the module.

`pack_image_folder`'s window is genuinely narrower than the others (its
size comparison and unlink are adjacent, with no read-rebuild phase
between them), but it is the same class of unchecked-staleness bug and
is closed the same way.

None of this makes replacement atomic. The multi-step
backup/rename/unlink sequences are unchanged, and a comment in
`pack_image_folder` now records that the remaining non-atomicity is a
deliberate deferral pending direct SMB rename-semantics validation, not
an oversight.

### 6.4 Retry-policy normalization

`scripts/cbz_sanitizer.py` now defines `FILE_LOCK_RETRY_ATTEMPTS = 5`
and `FILE_LOCK_RETRY_DELAY_SECONDS = 5.0`, used by both of its retry
loops. Previously `_write_cbz_with_comicinfo()` waited 0.5s while
`process_comicinfo()` waited 5s for the same transient locked-file
condition, so identical failures behaved differently depending on which
function encountered them. The longer interval was kept: a lock held by
another process is likelier to clear after seconds than milliseconds,
and repeatedly hammering an SMB share is worse than waiting.

### 6.5 Entry-name pass-through documented

In-code comments were added at both sites that round-trip
`ZipInfo.filename` values unvalidated into rewritten archives. The
behavior is unchanged by design — neither function extracts to a real
filesystem path, so an unsafe member name is not exploitable there —
but a future contributor adding an extraction feature must not assume
names have already been sanitized.

### 6.6 Validation

```text
py_compile across comic_automation/ and scripts/:   clean
full suite:                          542 passed, 2 failed
new tests added:                                      12
```

The 2 failures are pre-existing and unrelated:
`tests/test_series_detection.py` asserts on `\\tower\media\comics\...`
UNC paths, which do not parse equivalently under Linux `pathlib`. They
fail identically on unmodified `fe8897b` and are expected to pass on
Windows. Test count rose from 530 to 542, matching the 12 added:

```text
tests/test_archive_perceptual_hashing.py       +3
tests/test_sanitizer_comicinfo_rewrite.py      +3 (new file)
tests/test_library_maintenance_rewrite.py      +6 (new file)
```

One test flaw was caught and fixed during development: the
persistent-drift test originally used `id(object())` to generate a
distinct mtime per call, but short-lived objects reuse memory
addresses, so two consecutive calls could coincidentally agree and let
the test pass for the wrong reason. It now uses a monotonic counter.

### 6.7 Next work

Unchanged and still the priority: the guarded Version 1 perceptual-hash
backfill, per the launch checklist in
`docs/production_handoff_2026-07-31.md`. 17,554 eligible archives
remain. Nothing in this session's work touches the backfill's
preflight counts, protected backup, or launch prerequisites.

Remaining hardening candidates, in no committed order:

- `scripts/cbz_compilation_resolver.py` has still had no I/O and
  resource-limit audit; it is where true archive-content merging
  happens and is the likeliest remaining concentration of memory and
  zip-slip risk;
- encrypted-archive handling is still untraced end-to-end beyond
  `inspection.py`'s detection flag;
- the golden corpus and property tests from implementation roadmap
  Step 8;
- lease/fencing tokens for the job queue, which remain correctly
  deferred and must not be bundled with duplicate-row work.
