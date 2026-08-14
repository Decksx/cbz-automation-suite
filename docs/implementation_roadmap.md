# Implementation Roadmap

## Status as of 2026-07-31

The project has moved beyond a collection of standalone scripts into a
SQLite-backed automation platform for discovering, inspecting, hashing,
comparing, reviewing, quarantining, and eventually publishing comic
archives.

The current active workstream is production-scale per-page perceptual
hashing. Structural changes to archive identity, revision ownership, and
hash foreign keys are intentionally deferred until the Version 1
perceptual-hash backfill and its final coverage audit are complete.

### Current production metrics

These values are the last fully reconciled production baseline. Update
this table after each major guarded run and after the final coverage
audit.

| Metric | Verified value |
| --- | ---: |
| Logical archive rows | 59,688 |
| Current file locations/archives | 59,377 |
| Archive SHA-256 rows | 59,541 |
| Archive content signatures | 58,437 |
| Exact duplicate groups | 2 |
| Page SHA-256 rows | 2,955,304 |
| Perceptual job rows | 40,700 |
| Perceptual jobs completed | 40,581 |
| Perceptual jobs failed | 119 |
| Active perceptual jobs | 0 |
| Production schema migrations | 1–10 |
| dHash rows, Version 1 | 2,075,992 |
| pHash rows, Version 1 | 2,075,992 |
| Eligible archives remaining | 17,554 |
| Near-duplicate candidates | 0 |
| Last guarded batch | 5,000 processed |
| Last-batch outcomes | 4,991 succeeded, 9 terminal failures, 0 retries |
| Last-batch terminal-failure rate | 0.18% |
| Last-batch throughput | 1,222.13 archives/hour |
| Estimated active processing time remaining | approximately 14.36 hours |

Throughput is calculated from the guarded batch result:

```text
archives_per_hour = processed / (elapsed_seconds / 3600)
```

Estimated active processing time remaining is:

```text
eligible_archives_remaining / archives_per_hour
```

This is an estimate of active processing time, not a predicted calendar
completion date. It varies with archive size, page count, image
complexity, storage performance, cache opportunity, and terminal
failures.

The source-drift recovery for archive `23258` is complete. Its stale
6.9 MB WebP-based exact inventory was atomically replaced with the
current 18.2 MB, 31-page JPEG inventory. The original perceptual job
then completed successfully on attempt 2, leaving no active perceptual
jobs.

The guarded runner already reports `processed` and `elapsed_seconds`.
Preserve those fields in future runner revisions. Before adding a
perceptual-hashing-specific run table, determine whether the existing
`processing_runs` model can store or reference batch limits, outcomes,
timestamps, elapsed time, and phase timing.

## Architectural principles

- SQLite remains the operational database while the project is
  single-host and single-operator.
- New database access goes through a shared local data-access layer
  (DAL); local high-throughput work is not routed through HTTP.
- SQLite writes occur locally on the database host, not directly over
  SMB.
- Archive identity is separate from filesystem location.
- Revision content identity is immutable.
- Observations, retention state, and operator-control metadata may
  change without changing revision content identity.
- Derived evidence is versioned and attributable to a source revision
  and processing run.
- Evidence is preserved separately from later conclusions or
  resolution actions.
- Heavy work uses the persistent job queue.
- Destructive actions remain previewable, guarded, auditable, and
  quarantine-first.
- Optimizations are measured before deployment and must preserve
  Version 1 hash semantics unless a new algorithm version is declared.
- Infrastructure is introduced in response to demonstrated need, not
  anticipated scale.

## Deliberate deferrals

Do not introduce these until a concrete operational need justifies
them:

- a network-facing FastAPI control plane;
- remote or GPU workers;
- OpenTelemetry or distributed tracing;
- message brokers or a distributed job framework;
- microservices;
- PostgreSQL;
- a full single-page frontend framework;
- a generic lifecycle-event framework;
- a generalized invalidation engine;
- a speculative multi-table review schema;
- workflow-orchestration platforms such as Airflow, Dagster, or Prefect.

Use structured JSON logs, `processing_runs`, guarded reports, and a
lightweight `docs/engineering_decisions.md` log in the current
environment.

The target architecture for the GUI is recorded separately in
`docs/gui_architecture_implementation_roadmap.md`. **That document does not
reverse the two deferrals above** — it neither introduces a network-facing
control plane nor adopts a frontend framework. It is written now so that the
safety invariants and sequencing are fixed before any frontend work begins,
rather than being settled later under delivery pressure. Activation remains
tied to backend readiness rather than a date.

## Near-term implementation sequence

### Step 1 — Finish and audit the Version 1 perceptual-hash backfill

After each active guarded batch completes:

- reconcile `succeeded + terminally_failed + retry_scheduled` against
  `processed`;
- require no unexpected pending, claimed, or running work;
- run SQLite `quick_check`;
- verify the eligible-archive reduction;
- verify dHash and pHash Version 1 counts remain aligned;
- verify the protected backup remains unchanged;
- verify repository status does not change unexpectedly;
- record throughput, terminal-failure rate, and the updated production
  metrics.

Before the next structural database migration:

- complete the remaining Version 1 backfill;
- audit full-library coverage;
- classify legitimate terminal failures;
- capture a final database backup;
- verify the backup independently;
- update the production metrics and development log.

### Step 1A — Phase 5 performance optimization

Perform these optimizations only after the currently active guarded
batch finishes and reconciles. They must not change JPEG decoding,
resize behavior, DCT arithmetic, floating-point accumulation order, or
stored Version 1 hash semantics.

#### A. Read-only exact-SHA reuse analysis

**Completed 2026-07-29.**

Run a read-only opportunity query limited to currently eligible
archives. A destination page is reusable only when:

- it has matching versioned page SHA-256 evidence;
- a source page with the same SHA-256 has complete dHash and pHash
  evidence for the required algorithm versions;
- the source also has complete width and height values;
- source evidence is internally consistent and unambiguous.

The report must include:

```text
reusable_pages
fully_satisfied_archives
partially_satisfied_archives
pages_still_requiring_decode
archives_still_requiring_processing
```

The opportunity report must distinguish archives that can be fully
satisfied by reuse from archives with only partial page reuse. Under
the current archive-level worker, partial reuse does not avoid decoding
unless selective missing-page processing is implemented.

Also:

- inspect the query plan;
- add an index only if the measured plan requires one;
- record the exact SQL and algorithm-version assumptions in the report;
- make no database changes during this analysis;
- use the number of fully satisfied archives and avoidable decodes—not
  only the raw reusable-page count—to decide whether reuse is material.

Acceptance criteria:

- both page-level and archive-level reuse opportunities are reported;
- full and partial reuse are never conflated;
- only currently eligible archives are included;
- no database rows or files are changed;
- the report is reproducible against the same database snapshot.

Measured result:

```text
eligible_archives:                         37,654
eligible_pages:                         1,917,928
reusable_pages:                            16,163
fully_satisfied_archives:                     333
partially_satisfied_archives:                 407
pages_still_requiring_decode:           1,901,765
archives_still_requiring_processing:       37,321
pages_avoided_by_full_archive_reuse:        11,539
pages_avoided_with_selective_worker:        16,163
ambiguous_source_sha256_digests:                 0
```

The analysis completed in approximately 87 seconds, used the existing
versioned-digest and page-ownership indexes, passed `quick_check`, and
left database size and modification time unchanged.

Decision:

- full-archive reuse would avoid only 333 archives (0.88% of the
  eligible population) and 11,539 page decodes (0.60% of eligible
  pages);
- selective missing-page processing would avoid 16,163 page decodes
  in total (0.84%), only 4,624 more than full-archive reuse;
- defer both production bulk reuse and selective missing-page hashing
  because the measured savings do not justify their write/recovery and
  worker-complexity cost during the Version 1 backfill;
- retain the read-only command and rerun it if library composition or
  the reuse population changes materially.

#### B. Freeze Version 1 regression vectors

**Completed 2026-07-29.**

Create a small frozen regression set before changing the pHash
implementation. Record the expected exact dHash and pHash digest
strings produced by the current Version 1 implementation.

Include, where supported:

- JPEG, PNG, WebP, GIF, and TIFF;
- grayscale, RGB, RGBA, and palette images;
- very small and very large dimensions;
- unusual aspect ratios;
- representative known production pages;
- non-default `hash_size` and `high_frequency_factor` values.

The equality gate is exact:

```text
cached_digest == uncached_digest
```

Zero Hamming distance is not used as a substitute for exact string
equality.

Acceptance criteria:

- expected digest strings are frozen before implementation changes;
- existing tests pass against the unchanged implementation;
- cached and uncached dHash/pHash output is byte-for-byte identical for
  every regression vector;
- any change in a stored digest blocks deployment as Version 1.

The frozen suite contains eight deterministic vectors covering JPEG,
PNG, WebP, GIF, and TIFF; grayscale, RGB, RGBA, and palette images;
3x5 through 1536x1024 dimensions; unusual aspect ratios; and default,
4x2, and 12x3 hash configurations. The unchanged implementation passed
all exact expected-digest assertions before the cache change was made.
Real collection pages are not committed as fixtures.

#### C. Implement immutable pHash constant caching

**Completed 2026-07-29.**

Cache the pHash cosine and normalization constants by:

```text
(hash_size, high_frequency_factor)
```

Implementation requirements:

- use immutable nested tuples;
- use a bounded or parameter-keyed process-local cache such as
  `functools.lru_cache`;
- preserve the existing coefficient loop order;
- preserve floating-point accumulation order;
- preserve current resize and grayscale conversion behavior;
- do not alter the public digest format or algorithm-version label.

This optimization removes repeated constant construction but does not
remove the pure-Python DCT accumulation loops. Its actual throughput
impact must be measured rather than assumed.

Acceptance criteria:

- all Version 1 regression vectors match exactly;
- existing perceptual-hash tests pass;
- non-default supported parameters are covered;
- before/after benchmark results are recorded;
- the cached objects cannot be mutated by callers.

Implementation result:

- constants are stored as nested tuples in a process-local
  `lru_cache(maxsize=32)`;
- the existing coefficient and floating-point accumulation order is
  unchanged;
- all eight frozen Version 1 vectors retain exact digest equality;
- a checked-in paired benchmark preserves the pre-cache implementation
  as its reference path;
- on Python 3.11.3 and Pillow 12.3.0, seven alternating rounds of 250
  hashes measured median throughput increasing from 122.00 to 123.37
  hashes/second (approximately 1.13%), confirming a modest gain rather
  than a transformative one.

#### D. Add optional phase timing

**Completed 2026-07-29.**

Timing must be captured inside `calculate_perceptual_hashes()` and the
repository lookup/save path. The batch runner may aggregate the
results, but it cannot infer internal phases accurately.

Use `time.perf_counter()` and aggregate these values per archive and
per batch:

```text
zip_open_and_inventory_seconds
zip_entry_read_seconds
image_open_and_decode_seconds
dhash_seconds
phash_seconds
database_lookup_seconds
database_save_seconds
```

Also report page count and, where cheaply available, bytes read so the
results can be normalized.

Requirements:

- profiling is optional;
- normal processing behavior is unchanged when profiling is disabled;
- no telemetry row is written per page;
- timing overhead is measured and kept small;
- batch output reports phase totals and normalized values such as
  milliseconds per page and phase percentage.

Benchmark a representative sample of 50–200 archives before and after
cosine caching. Include a mixture of common image formats, archive
sizes, page counts, and storage conditions.

Acceptance criteria:

- phase totals reconcile plausibly with total elapsed time;
- results identify measured bottlenecks rather than asserting them from
  static operation counts alone;
- benchmark inputs and configuration are recorded;
- profiling does not change any stored hash digest.

Implementation result:

- `--profile` enables in-memory per-archive accumulation and one batch
  summary; disabled runs do not call the timing clock inside page
  phases;
- no telemetry schema or per-page timing writes were added;
- successful profiled work reports archive count, page count, bytes
  read, phase seconds, phase percentages, milliseconds per page, and
  unattributed batch time;
- failed or retried jobs are counted explicitly as unprofiled jobs
  rather than silently included in successful phase totals;
- profiled and unprofiled calculations produce identical page results
  and stored hash digests.

A reproducible local benchmark processed 50 synthetic archives with
four pages each across PNG, JPEG, GIF, TIFF, and WebP. Three alternating
profiled/unprofiled rounds showed no measurable overhead
(`-0.24%`, within timing noise). Across 600 profiled pages, the timed
phase distribution was:

```text
phash:                       88.51%
image open and decode:        5.35%
dhash:                        3.07%
ZIP entry read:               1.30%
database save:                0.93%
ZIP open and inventory:       0.63%
database lookup:              0.22%
```

The subsequent guarded production sample processed 100 archives and
3,111 pages from the SMB library. All 100 jobs succeeded, database
integrity and eligibility reconciled, and the protected backup remained
unchanged. The production phase distribution was:

```text
image open and decode:       64.22%
phash:                       21.15%
dhash:                       10.41%
ZIP entry read:               3.09%
ZIP open and inventory:       0.78%
database save:                0.34%
database lookup:              0.01%
```

The run processed 3.83 GB of image payload in 276.26 seconds at 79.94
timed milliseconds per page. Unlike the local synthetic benchmark, the
production workload is dominated by Pillow image decoding rather than
pHash. ZIP entry reads and SQLite together remain a small share, so
neither WAL tuning nor database write batching is a current priority.

#### E. Implement bulk exact-hash reuse if material

**Measured and deferred 2026-07-29.** The opportunity analysis found
only 333 fully satisfiable archives and 11,539 page decodes avoidable
with the current archive-level worker. Keep this design for future use,
but do not implement production writes during the current backfill.

If the read-only analysis shows meaningful savings, implement a
version-aware bulk reuse operation on a database copy first.

Requirements:

- match exact page SHA-256 algorithm and version;
- require complete source dHash and pHash evidence for the requested
  versions;
- require complete width and height;
- insert distinct evidence rows for each destination `page_id`;
- never reuse a source row ID as destination evidence;
- copy or safely derive image format only when current schema semantics
  support it;
- preserve destination-page ownership;
- remain idempotent under repeated execution;
- perform writes in explicit, bounded transactions without weakening
  archive-level consistency;
- record reused counts and version assumptions in the run report.

After the operation:

- reconcile inserted dHash and pHash rows;
- reconcile filled width and height values;
- recount eligible archives;
- distinguish fully satisfied from partially satisfied archives;
- run SQLite integrity checks;
- verify no source evidence changed;
- retain the pre-operation database backup.

Acceptance criteria:

- only complete, version-compatible evidence is reused;
- repeated execution creates no duplicate evidence;
- destination rows reference the correct destination pages;
- dHash and pHash counts remain aligned;
- the eligible-archive change matches the fully satisfied archive
  count;
- database-copy validation succeeds before production use.

#### F. Evaluate selective missing-page hashing

**Measured and deferred 2026-07-29.** Selective processing would avoid
only 4,624 additional page decodes beyond full-archive reuse, so the
incremental savings do not justify a second worker path today.

Implement selective missing-page hashing only if partial reuse leaves a
material number of avoidable page decodes.

The selective worker must:

- validate the archive's current content signature before processing;
- read and validate the complete archive inventory;
- confirm page ordering and page-to-entry mapping;
- skip only pages that already have complete width, height, dHash, and
  pHash evidence for the requested versions;
- decode every page whose required evidence is missing or incomplete;
- preserve archive-level transactional saves;
- abort safely on inventory or source-revision mismatch;
- avoid mixing algorithm versions;
- remain correct when rerun after a crash.

Acceptance criteria:

- selective and full processing produce identical complete Version 1
  evidence on the same test archives;
- already complete pages are not decoded;
- missing or incomplete pages are processed;
- inventory validation still covers the whole archive;
- a failure cannot leave an archive falsely marked complete;
- archive-level reconciliation and idempotency remain intact;
- measured partial-reuse savings justify the added code path.

#### G. Resume guarded 5,000-archive backfill

**Guarded optimized production backfill resumed 2026-07-30.**

Resume production batches only after the selected optimizations pass
their regression, database-copy, and benchmark gates.

For each optimized batch:

- capture a fresh verified backup;
- record exact preflight counts;
- process no more than 5,000 archives;
- retain the existing production worker and queue reconciliation;
- compare throughput and terminal-failure rate with the prior baseline;
- report phase timing when profiling is enabled;
- confirm exact Version 1 digest semantics;
- verify no pending, claimed, or running jobs remain unexpectedly;
- run `quick_check`;
- verify the protected backup and repository state.

Do not treat a throughput improvement alone as success. Integrity,
idempotency, Version 1 equality, and reconciliation remain mandatory.

Latest production result:

```text
processed:                              5,000
succeeded:                              4,991
terminally failed:                          9
retry scheduled / pending:                  0
elapsed seconds:                       14,728.333
elapsed hours:                              4.091
throughput:                    1,222.13 archives/hour
profiled archives / pages:        4,991 / 280,518
dHash Version 1 rows:                 2,075,992
pHash Version 1 rows:                 2,075,992
eligible archives remaining:             17,554
```

The report, job outcomes, phase totals, database counts, and
eligible-archive reduction reconciled exactly. Both the working
database and protected backup passed `PRAGMA quick_check`; page
SHA-256 remained unchanged; the backup retained its pre-batch counts
and metadata; near-duplicate candidates remained zero; and the
repository stayed clean.

Latest production phase distribution:

```text
image open and decode:       54.827%
pHash:                       28.211%
dHash:                       11.535%
ZIP entry read:               4.204%
database save:                0.666%
ZIP open and inventory:       0.506%
database lookup:              0.051%
```

The 9 new terminal failures are legitimate page-image decoding
defects classified as `page_image_corrupt`; no queue, database, or
orchestration failure occurred. The cumulative perceptual terminal-
failure population is now 119: 40 `archive_corrupt` and 79
`page_image_corrupt`. A fresh read-only audit captured these
classifications and verified that the database remained unchanged.

The post-batch all-job-type migration preflight found no pending,
claimed, or running jobs, no duplicate active `(job_type, archive_id)`
groups, and no active jobs with a null archive ID. After the reviewed
implementation merged, a fresh schema-9 backup was created and verified
independently. Migration 010 then advanced the working database to
schema version 10 and created the exact reviewed partial unique index.
All 272,074 job rows remained column-for-column unchanged, all
production counts reconciled, `PRAGMA quick_check` remained `ok`, and
the protected pre-migration backup remained unchanged at schema
version 9.

The guarded operations tooling and the subsequent reliability hardening
merged through pull requests 15 and 16. Read-only audits now obtain all
report inputs from one deferred transaction bracketed by
`PRAGMA data_version`; main-file and WAL-sidecar fingerprints are
diagnostic evidence, not the concurrency gate. Service startup no longer
recovers age-stale jobs automatically, because the queue has no leases or
heartbeats with which to distinguish a dead worker from legitimate
long-running work. Recovery is an explicit guarded operator action.

The full-library coverage audit now reports the 17,554 eligible archives
with no job history as the expected never-enqueued backlog during the
bounded backfill. Its strict `--expect-backfill-complete` mode verifies
that both `incomplete` and `stale` populations are zero and exits 2 while
work remains; it cannot pass merely because all remaining archives have
job history.

#### H. Recover in-place source drift

**Implementation, database-copy validation, and production recovery
completed 2026-07-30.**

Job `259622` detected that archive `23258` changed after its exact page
inventory was stored. Add a guarded, single-job recovery path rather
than retrying stale evidence or issuing ad hoc SQL.

Requirements:

- default to strictly read-only analysis;
- require a pending perceptual job with the expected inventory-mismatch
  error and remaining attempts;
- report stored and live file metadata plus page-inventory differences;
- reject recovery when another active job targets the archive;
- require the operator to repeat the exact reviewed live file size and
  mtime before apply;
- recompute archive SHA-256, structural inspection, page inventory, and
  exact page SHA-256 with production implementations;
- refresh all exact evidence atomically;
- preserve the original job and its attempt history;
- clear its stale error and make it available only after commit;
- record a `source_drift_recovered` file event;
- roll back on any file change, precondition change, or write failure;
- leave perceptual hashing to the normal bounded worker.

Validation result:

- six focused recovery tests cover read-only analysis, apply guards,
  atomic rollback, unrelated failures, and the full retry path;
- the complete suite passes with 199 tests;
- a fresh production database copy passed `quick_check` before and
  after recovery;
- the stale 31-page WebP inventory was replaced with the current
  31-page JPEG inventory;
- the normal perceptual worker completed job `259622` on attempt 2 and
  stored 31 dHash plus 31 pHash values;
- the real production database remained unchanged during copy
  validation.

Production result:

- a fresh protected backup passed `quick_check`;
- the read-only analysis repeated every recovery gate immediately
  before apply;
- exact evidence refreshed atomically for 31 JPEG pages;
- the normal bounded worker completed job `259622` on attempt 2;
- dHash and pHash Version 1 counts increased to 1,279,247 each;
- completed perceptual jobs increased to 25,623;
- pending, claimed, and running perceptual jobs all returned to zero;
- eligible archives remained 32,554 because the pending job had already
  excluded this archive before its now-complete evidence was stored;
- the working database and protected backup both passed postflight
  `quick_check`, and the backup remained unchanged.

#### Deferred Version 2 research

Research these only after the Version 1 backfill:

- Pillow JPEG `draft()` decoding;
- vectorized or third-party DCT implementations;
- different resize filters or grayscale conversions;
- fused inspection/exact-hash/perceptual-hash execution.

Any change that alters stored digest bits requires a new algorithm
version. Version 1 and Version 2 evidence must not be compared as if
they were produced by the same algorithm.

### Step 2 — Design immutable archive revisions

Separate stable logical identity from observed byte-level content:

```text
archive_files
    stable logical identity

archive_revisions
    unique byte-level content states for one archive

file_locations
    current and historical paths

archive_observations
    sightings of a revision at a location during a run
```

An archive revision represents one unique byte-level content state for
one logical archive, not an individual observation event. If previously
seen bytes reappear, reuse the existing revision and record a new
observation.

Revision content identity is immutable: `archive_id`,
`archive_sha256`, content-derived metadata, and source relationships are
never rewritten to represent different bytes. Observation, retention,
and operator-control metadata may be updated without changing revision
identity.

Use `archive_files.current_revision_id` as the sole authoritative
pointer to the current revision. Do not persist a second `is_current`
source of truth.

Schema requirements:

- index `archive_sha256` for duplicate lookup;
- do not make `archive_sha256` globally unique;
- use `UNIQUE (archive_id, archive_sha256)` so the same logical archive
  cannot accumulate duplicate rows for the same byte state;
- structurally prevent an archive from pointing to a revision owned by
  another archive, using a composite foreign key or equivalent;
- retain direct foreign keys rather than a generic polymorphic
  provenance relationship.

Migration requirements:

- every existing `archive_id` receives exactly one initial revision;
- no archive identities are merged as a migration side effect;
- the two known exact-duplicate groups remain distinct `archive_files`
  rows whose initial revisions share the same `archive_sha256`;
- canonical-copy selection and duplicate cleanup remain later guarded
  resolution actions;
- all migration steps run in a transaction where feasible;
- pre/post counts, foreign keys, current-revision pointers, and hashes
  are reconciled;
- rollback and restore procedures are tested against a database copy.

Acceptance criteria:

- every archive has exactly one deterministic current revision;
- every current-revision pointer belongs to its parent archive;
- every pre-migration archive maps one-to-one to its initial revision;
- byte-identical archives remain separately addressable;
- paths and observations can change without rewriting revision identity;
- derived data can be migrated to the correct source revision.

### Step 3 — Define revision retention and guarded pruning

Immutable revisions do not imply unbounded retention.

Keep:

- the current revision;
- at least the immediately previous revision for a defined retention
  window;
- revisions referenced by active or recoverable jobs;
- revisions referenced by open review work;
- revisions referenced by quarantine or resolution history;
- revisions associated with unresolved failures;
- operator-pinned revisions.

Use a two-stage process:

1. Classify and report revisions as prunable.
2. Remove them only through a separate guarded plan/apply operation.

Useful administrative fields may include:

```text
prunable_at
prune_reason
pinned_at
pinned_reason
```

Avoid broad cascading deletes. Prefer restrictive foreign keys and an
explicit purge plan that enumerates every derived row to be removed.

Acceptance criteria:

- a dry-run plan identifies all protected and prunable revisions;
- applying a plan requires the exact reviewed plan;
- current or referenced revisions cannot be pruned;
- prune operations reconcile all affected row counts;
- interrupted pruning is recoverable or safely repeatable.

### Step 4 — Add revision-aware provenance

Add the schema support needed by existing and near-term derived
artifacts without building a generalized invalidation engine.

Use applicable fields such as:

```text
source_revision_id
algorithm
algorithm_version
parameters_json
processing_run_id
created_at
superseded_at
superseded_by_id
```

Not every table needs every field. Prefer direct foreign keys and
table-specific uniqueness constraints.

For page evidence, uniqueness should encode page ownership and
algorithm version so rerunning a job cannot create duplicate results.
Future candidate scores and embeddings must likewise retain their
method, version, parameters, source revisions, and processing run.

Defer automatic invalidation rules until the corresponding producers
and consumers exist.

Acceptance criteria:

- existing evidence is attributable to a revision;
- algorithm versions cannot be silently mixed;
- repeated work is idempotent at the data layer;
- superseded evidence remains distinguishable from active evidence;
- migration preserves all current hash values and counts.

### Step 5 — Establish the minimum local DAL before migrations

Steps 2 through 4 define the revision, retention, and provenance model.
Before applying their migrations or implementing their repositories,
establish the minimum DAL foundation.

Recommended package shape:

```text
comic_automation/
    db/
        connection.py
        transactions.py
        migrations.py
        backups.py
        repositories/
            archives.py
            revisions.py
            observations.py
            pages.py
            hashes.py
            jobs.py
            runs.py
```

The DAL owns:

- connection construction;
- SQLite pragmas and busy timeout;
- read-only connections;
- transaction boundaries;
- migration checks;
- backup operations;
- repository queries;
- transient lock handling;
- schema invariants.

It does not own:

- image processing;
- archive parsing;
- similarity algorithms;
- filesystem moves;
- CLI formatting;
- business-policy decisions.

Migration strategy:

1. All new database code uses the DAL.
2. Existing `comic_automation` access moves first.
3. Standalone scripts migrate when touched for functional work.
4. Raw `sqlite3.connect()` calls outside approved modules are
   deprecated.
5. A test or lint rule prevents new unauthorized direct connections.

Acceptance criteria:

- revision migrations use DAL-managed transactions and backups;
- production connection settings are defined once;
- read-only and writable connections are explicit;
- current workflows remain functional during incremental migration;
- the project is not paused for an all-at-once rewrite.

### Step 6 — Harden the persistent job queue

Add directly relevant single-host recovery controls:

- `claimed_by`;
- `claimed_at`;
- `lease_expires_at`;
- `heartbeat_at` or lease renewal;
- `attempt_count`;
- deterministic `idempotency_key`;
- failure class and failure code;
- explicit retryability;
- terminal-failure reporting.

Job acquisition must be atomic. Abandoned ownership must expire
predictably. Enqueue must be idempotent for the same revision,
algorithm version, and parameters.

A separate dead-letter status is optional. The existing failed status
may remain terminal if failure classification and retryability are
unambiguous.

Acceptance criteria:

- two workers cannot acquire the same job;
- a crashed worker's lease can be recovered safely;
- repeated enqueue attempts create no duplicate job;
- malformed content is not blindly retried;
- transient failures follow an explicit bounded retry policy;
- recovery tests prove that no completed evidence is duplicated.

### Step 7 — Harden archive resource handling

Audit:

```text
archive/inspection.py
archive/handlers.py
archive/page_hashing.py
archive/perceptual_hashing.py
```

Verify and enforce:

- maximum entry count;
- maximum individual compressed and uncompressed entry size;
- maximum total declared uncompressed size;
- maximum decoded pixels;
- absolute-path and path-traversal rejection;
- duplicate member-name handling;
- encrypted entry handling;
- nested archive policy;
- processing timeout and temporary-disk budget;
- prompt closing of archive and member streams.

For mutating workflows such as sanitization:

- copy the source to local scratch;
- rewrite locally using streaming member-to-member copies;
- validate the completed ZIP;
- copy the result to a unique sibling temporary file on the destination
  share;
- validate the destination temporary file;
- use a guarded destination-side replacement with backup retention;
- remove scratch files only after post-replacement validation.

Benchmark local stage-and-swap against direct SMB rewriting. Do not
claim a fixed performance multiplier without project-specific results.

Acceptance criteria:

- whole archives are not accumulated in memory;
- malformed or dangerous members fail with explicit reason codes;
- large legitimate scans follow a documented allow/warn/review/reject
  policy;
- an interrupted rewrite cannot silently destroy the only good copy;
- source and replacement archives are independently verifiable.

### Step 8 — Add a golden corpus and focused property tests

Create a small routine regression corpus covering:

- a valid ordinary CBZ;
- corrupt or truncated ZIP structures;
- truncated and unidentified images;
- exact duplicate pages;
- reordered pages;
- a one-page difference;
- ComicInfo-only changes;
- Unicode and punctuation in paths;
- an archive replaced in place;
- duplicate content at multiple locations;
- unsafe member paths;
- excessive entries or decoded pixels.

Keep large or sensitive real-world fixtures outside Git and reference
them through an optional integration-test manifest.

Use property-based tests for inexpensive invariants:

- normalization is idempotent;
- page ordering is deterministic;
- idempotency keys are stable;
- repeated completed jobs do not create duplicate rows;
- moving a file does not change archive identity;
- changed bytes create or select the correct revision;
- a current-revision pointer always belongs to its archive;
- batch outcome accounting reconciles;
- dry-run and plan generation do not mutate sources;
- invalid job-state transitions are rejected.

Add crash and fault-injection tests around job acquisition, hash saves,
database commits, archive replacement, quarantine moves, and recovery.

### Step 9 — Maintain a lightweight decisions log

Continue using the existing file:

```text
docs/engineering_decisions.md
```

Record dated entries with:

- context;
- decision;
- alternatives considered;
- consequences.

Initial decisions should cover:

- SQLite remains the operational database;
- database writes occur on the database host;
- archive revisions represent immutable byte states;
- observations are separate from revisions;
- duplicate identities are not merged during revision migration;
- evidence is versioned;
- quarantine precedes deletion;
- heavy work uses the persistent queue;
- Version 1 hash semantics are frozen during the active backfill.

## Phase 1 — Baseline and tests

- [x] keep documentation aligned with code;
- [x] establish repeatable unit and integration tests;
- [x] preserve dry-run behavior for mutating workflows;
- [ ] complete the focused golden corpus;
- [ ] add property and crash-recovery tests from Step 8.

## Phase 2 — SQLite operational core

- [x] schema migrations;
- [x] persistent jobs;
- [x] processing-run records;
- [x] archive identity separate from file location;
- [x] archive inspection foundation;
- [ ] consolidate new and touched database access through the local DAL;
- [ ] add job leases, idempotency, recovery, and failure
      classification.

## Phase 3 — Archive audit

- [x] archive inventory;
- [x] archive-level SHA-256 coverage for 59,541 archives;
- [x] identify 2 exact-duplicate groups;
- [x] ComicInfo metadata inventory through the inspection pipeline;
- [x] page inventory;
- [x] 2,955,304 per-page SHA-256 rows recorded;
- [x] resumable database jobs;
- [ ] separately audit final full-library page-hash coverage;
- [x] implement guarded exact-duplicate resolution tooling;
- [ ] execute reviewed resolution plans for the 2 known
      exact-duplicate groups;
- [ ] introduce immutable archive revisions after the Version 1
      perceptual backfill.

Exact-duplicate resolution remains separate from the revision migration.
The migration preserves distinct archive identities and shared
byte-level hashes.

## Phase 4 — Series identity

- [ ] canonical series records;
- [ ] title and alias normalization;
- [ ] provider and filesystem observations;
- [ ] confidence-scored identity proposals;
- [ ] operator-confirmed merges and splits;
- [ ] provenance for identity evidence and decisions;
- [ ] provider-neutral candidate model and durable human-decision
      manifest;
- [ ] read-only external candidate search and cover retrieval;
- [ ] archive-level assignment and splitting for mixed folders;
- [ ] guarded metadata/move plan generation with separately approved
      apply;
- [ ] ambiguity re-measured under the canonical `series_key`.

Part of the normalization item landed in #44: `series_key()` in
`scripts/cbz_routing.py` is now the single definition of "same series"
for the watcher, the reclassifier, the series-operation lock registry,
and `SeriesIndex`, NFC-normalized at both ends. The item stays open
because canonical series records do not exist yet, and alias handling
today is a hand-maintained `series_overrides` list in
`config/routing.v2.json` rather than a modelled one.

Do not begin structural Phase 4 work until archive revisions and the
minimum DAL are stable.

### Human-reviewed identity resolution (#57)

Binding. See "A human decision is the only resolution of an ambiguous
identity" in `docs/engineering_decisions.md` for the reasoning; these are
the rules the work is built against.

1. Programmatic matching may retrieve, score, rank, and explain
   candidates, and may resolve an identity automatically only when an
   approved, deterministic, tested rule finds exactly one unambiguous
   result with no material contradictory evidence.
2. A confidence score or threshold alone does not make an ambiguous
   identity authoritative. A materially ambiguous identity is never
   resolved automatically by score, provider order, popularity, current
   placement, or index priority.
3. Multiple plausible candidates, conflicting identity evidence, mixed
   folders, and merge or split decisions whose identity remains
   uncertain require explicit human review.
4. A reviewed human decision overrides conflicting programmatic
   proposals until another reviewed decision supersedes it.
5. Mixed folders support archive-by-archive assignment and splitting.
6. External metadata and covers are advisory until the operator selects
   them.
7. Candidate search is read-only. ComicInfo changes require a separate
   content-addressed plan/apply operation with source-revision
   revalidation, backup, and audit history.
8. Before the v2 index becomes authoritative, ambiguity must equal zero,
   or every remaining ambiguous key must carry a reviewed
   exception/identity manifest.
9. Logging `ambiguous_series=True` while continuing to route does not
   satisfy rule 8.

Sequence, in order:

1. finish #31 without activation — **done**, PR #56;
2. measure current ambiguity using the canonical `series_key`;
3. define a provider-neutral candidate model and durable human-decision
   manifest;
4. add read-only MangaDex search and cover retrieval first;
5. add MyAnimeList or Jikan only behind the same adapter interface;
6. display local cover/ComicInfo evidence beside bounded external
   candidates;
7. allow assign, alternate edition, split, none-of-these, and unresolved
   decisions;
8. add guarded metadata/move plan generation and separately approved
   apply;
9. re-measure ambiguity before any v2-authority decision.

One provider comes before two so the adapter boundary is proven by use
rather than asserted, and the candidate model precedes both so it is
provider-neutral rather than shaped by whichever API arrived first.

Step 2 is a measurement, not a formality. The last count — **25 keys**,
22 Comix↔Manga/GN plus 3 Manga↔GN — was taken on 2026-08-03 and recorded
in `duplicates.json` in the retained census. It predates the `series_key`
consolidation of #44/#51/#54, and normalization can only merge more
names, so it is a **lower bound rather than a current figure**. It cannot
be recomputed from retained evidence, which kept the overlapping keys but
not the directory listings they were derived from, so step 2 requires
reading the live destination roots.

## Phase 5 — Perceptual deduplication

- [x] per-page dHash and pHash Version 1 implementation;
- [x] sampled hash blocking and decoded dimension summaries;
- [x] conservative ordered page comparison;
- [x] persistent review-only Tier C candidates;
- [x] persistent `hash_archive_pages_perceptual` jobs;
- [x] guarded production batches with preflight, reconciliation,
      backups, and postflight integrity checks;
- [ ] complete the Version 1 full-library perceptual-hash backfill;
- [ ] execute the remaining Phase 5 performance optimization sequence
      in Step 1A;
- [x] measure exact-SHA reuse opportunity and decide against bulk reuse
      and selective missing-page hashing for the current backfill;
- [x] freeze exact Version 1 regression vectors and implement
      output-preserving immutable pHash constant caching;
- [x] capture optional phase timing from both a local 50-archive
      benchmark, a guarded 100-archive SMB production sample, and the
      first optimized 5,000-archive production batch;
- [x] resume the guarded 5,000-archive backfill with immutable pHash
      constant caching and phase timing;
- [x] add and run a read-only terminal-failure audit that verifies the
      database remains unchanged;
- [ ] perform a final coverage and terminal-failure audit;
- [ ] generate near-duplicate candidates at production scale;
- [ ] add richer aggregate archive signatures;
- [ ] support partial-overlap and compilation detection;
- [ ] design evidence-first review cases and decisions when candidate
      generation is real;
- [ ] build review functionality after the evidence schema stabilizes.

Candidate generation and later review must preserve measured evidence
separately from conclusions. Do not commit prematurely to a large
generic table topology before real candidate queries and operator
workflows define the required relationships.

## Phase 6 — Quality scoring

- [ ] define versioned, explainable quality features;
- [ ] score resolution, compression, completeness, metadata, and other
      relevant signals;
- [ ] retain component scores and parameters, not only a total;
- [ ] compare quality only between appropriate candidate revisions;
- [ ] use scores as review evidence, not automatic deletion authority.

## Phase 7 — OpenCLIP and semantic evidence

- [ ] define the use cases that exact and perceptual hashes do not
      solve;
- [ ] benchmark representative local GPU workloads;
- [ ] version model, preprocessing, parameters, and embeddings;
- [ ] store embeddings against immutable source revisions;
- [ ] keep semantic evidence review-oriented until accuracy is measured.

Do not build remote GPU orchestration before a local workload
demonstrates the need.

## Phase 8 — Managed resolution and publication

- [ ] produce reviewable action plans;
- [ ] require explicit confirmation for destructive or publishing
      actions;
- [ ] quarantine before deletion;
- [ ] retain source, destination, revision, reason, run, and operator
      history;
- [ ] make filesystem and database changes recoverable and
      reconcilable;
- [ ] stage content before publication to Komga.

## Phase 9 — Dashboard and operator control plane

- [ ] define query/read models after evidence and review schemas
      stabilize;
- [ ] expose queue health, coverage, failures, review backlog, and
      guarded actions;
- [ ] use a local API only when a real remote client exists;
- [ ] keep heavy work in the persistent job queue;
- [ ] avoid direct remote SQLite access.

An eventual small FastAPI and server-rendered/HTMX interface may fit
the project, but it is not a current foundation requirement.

What that interface should look like when it is built is specified in
`docs/gui_architecture_implementation_roadmap.md` — the plan lifecycle,
version-specific approval, execute-time revalidation, narrow entity-level
locking, and the phased escalation from a read-only control plane to guarded
execution. It is a **future workstream**, and stating the architecture does
not make building it a current requirement: the deferral recorded under
*Deliberate deferrals* still stands, and the GUI must never become a route
around the routing-v2 readiness gates.

## Definition of roadmap completion

The roadmap is complete when:

- archive and revision identity are structurally sound;
- all derived evidence is versioned and attributable;
- background work is idempotent and recoverable;
- resource limits and archive mutation are guarded;
- exact, perceptual, quality, and semantic evidence can be reproduced;
- candidate conclusions remain reviewable;
- resolution actions are auditable and reversible;
- publication is staged and controlled;
- the operator can understand system state without reading raw database
  tables or reconstructing prior runs.
