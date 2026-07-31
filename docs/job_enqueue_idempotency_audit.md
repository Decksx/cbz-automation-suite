# Job Enqueue & Idempotency Audit

**Status:** evidence-only audit. No production code, tests, schemas, or
migrations were changed as part of this document. Every claim below
references function names and stable code locations (module + function/class
name), not line numbers, and was verified by reading the actual source. No
production or backup database was queried and no production CLI was run;
every behavioral claim is derived from static reading of the code in this
worktree (`origin/master` @ the commit this branch was cut from) plus the
existing test suite's intent, not from executing anything against real data.

**Related prior audits** (read for context, not duplicated here):
`docs/jobs_worker_retry_audit.md` (queue/worker state machine, atomicity of
`claim_next`/`mark_failed`/`recover_abandoned`, and the lease/fencing gap)
and `docs/archive_io_resource_audit.md` (handler I/O and resource-limit
behavior). This audit is scoped specifically to **enqueue-time duplicate
prevention** — whether a second, independent `jobs` row for the same logical
work can be created — which is a different question from "can two workers
process the same row concurrently" (the lease/fencing gap those audits
already documented). See "Handler-side idempotency limitations" below for
where the two concerns intersect and where they don't.

---

## 1. Executive summary

The `jobs` table has no database-level uniqueness guarantee for active work.
Five production code paths call `JobQueue.enqueue()`, directly or via a
repository helper. All five are visible to a `grep` for `JobQueue(`,
`.enqueue(`, and `enqueue_missing` across `comic_automation/`. Three of the
five (`ArchiveHashRepository.enqueue_missing`,
`ArchivePageHashRepository.enqueue_missing`,
`ArchivePerceptualHashRepository.enqueue_missing`) and one of the remaining
two (`ArchiveHashRepository._enqueue_reinspection_if_absent`) defend against
duplicate enqueueing with an application-level `NOT EXISTS` / prior-`SELECT`
check, but none of those four wrap the check-then-insert in a transaction
that holds SQLite's write lock across both statements — so none of the four
are provably safe against **concurrent** duplication, only against
**sequential** duplication (repeated calls with no overlap).

The fifth path (`LibraryRepository._enqueue_inspection_if_absent`, the
discovery path) wraps its check-then-insert in a caller-held `BEGIN
IMMEDIATE` transaction, but that only protects it against **another
transaction that also acquires the write lock before checking** — i.e.
another `BEGIN IMMEDIATE`-guarded writer, such as a second, concurrent
discovery scan. It provides no protection against
`_enqueue_reinspection_if_absent()` (path #2), whose `SELECT` is a bare,
unguarded statement that runs to completion, releases any lock, and only
*afterwards* issues its `INSERT`. The write lock discovery holds delays
that later `INSERT` from proceeding; it does not force
`_enqueue_reinspection_if_absent()` to repeat its already-completed check
once the lock is released. Concretely:

1. Hash-triggered reinspection (path #2) runs its standalone `SELECT` and
   sees no active `inspect_archive` job for the archive.
2. Discovery (path #1) acquires `BEGIN IMMEDIATE`'s write lock, runs its own
   check, inserts, and commits — for the *same* archive.
3. Hash-triggered reinspection, still holding the result of its earlier
   `SELECT`, now runs the `INSERT` it had already decided on in step 1.
4. Two active `inspect_archive` rows now exist for the same archive.

So path #1 is conditionally safe — safe against another instance of itself
(two concurrent discovery scans), because both sides of that particular race
hold the write lock across their full check-then-insert — but it is not
globally safe: it does not protect against, and is not protected by, path
#2's unguarded check. **No path among the five is provably safe against
every other current concurrent enqueue path for the same job type; the
"provably safe concurrently" count is 0 of 5.**

One further, concrete inconsistency was found rather than inferred:
`ArchiveHashRepository.enqueue_missing()`'s duplicate-check excludes only
`pending`/`claimed`/`running` jobs, while the page-hash and perceptual-hash
`enqueue_missing()` methods also exclude `failed`. This means repeated
`--enqueue-missing` CLI runs will keep creating new `calculate_archive_hash`
jobs for an archive whose hash job permanently failed, but will not do the
same for `hash_archive_pages` or `hash_archive_pages_perceptual`. Nothing in
the code or tests documents whether this divergence is intentional
retry-via-re-enqueue behavior or a copy-paste gap between the three near-
identical methods; it is flagged as an open question, not resolved here.

No job type observed in production code enqueues with `archive_id = NULL`;
every one of the five call sites supplies a concrete `archive_id`, even
though the column itself is nullable at the schema level. `(job_type,
archive_id)` is therefore sufficient to describe logical *identity* — "which
piece of work is this" — for every job type currently enqueued in
production, unconditionally: the terminal-failure inconsistency above does
not change what identifies a job, only whether a caller is willing to
create a *new* one once the previous one for that same identity has already
failed. Those are separate questions; see sections 6-7 for why the
inconsistency affects caller-side re-enqueue policy but not the identity
tuple or the uniqueness index built on it.

The smallest safe improvement that does not require lease/fencing or
handler-level changes is a **partial unique index** on `jobs(job_type,
archive_id)` restricted to non-terminal (`pending`/`claimed`/`running`)
statuses. This index can be added regardless of the `calculate_archive_hash`
`failed`-status inconsistency described above: the inconsistency is about
whether a caller should be *allowed* to re-enqueue after a terminal
`failed` outcome, which is a policy question about the non-unique,
already-terminal rows the index never touches. The index itself only
ever needs to define "active," and `pending`/`claimed`/`running` is exactly
that definition, uniformly, for every job type today — no job type treats
any of those three statuses as anything other than active work in flight.
Paired with a small centralized `enqueue_if_absent()`-style helper (using an
`INSERT ... ON CONFLICT DO NOTHING` matching the index, not error-message
matching — see section 6), this closes the check-then-insert race at the
database level for every caller,
including any future direct `JobQueue.enqueue()` call site that never
adopts a `NOT EXISTS` convention, without touching `claim_next`,
`mark_failed`, `recover_abandoned`, leases, or heartbeats. It does **not**
address the separate, already-documented risk (see
`docs/jobs_worker_retry_audit.md`, "Missing lease/idempotency protections")
that `recover_abandoned()` can hand a still-owned job's row to a second
worker — that is a single-row double-processing problem, not a duplicate-
row problem, and needs its own fencing-token design.

---

## 2. Current schema and index inventory

### `jobs` table (`comic_automation/database/migrations/001_operational_foundation.sql`)

```
id               INTEGER PRIMARY KEY
job_type         TEXT NOT NULL
status           TEXT NOT NULL DEFAULT 'pending'
priority         INTEGER NOT NULL DEFAULT 100
archive_id       INTEGER                          -- nullable; FK -> archive_files(id) ON DELETE CASCADE
payload_json     TEXT
attempts         INTEGER NOT NULL DEFAULT 0
max_attempts     INTEGER NOT NULL DEFAULT 3
available_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
claimed_at       TEXT
started_at       TEXT
completed_at     TEXT
worker_id        TEXT
error_message    TEXT
failure_category TEXT                              -- added by migration 005
created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
updated_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
```

No `UNIQUE`, `CHECK`, or other constraint restricts `(job_type, archive_id)`
or `(job_type, archive_id, status)` combinations. Any number of rows may
share the same `job_type` and `archive_id` regardless of status.

**Indexes on `jobs`** (all three currently defined, across two migrations):

| Index | Columns | Migration | Purpose (per migration comment) |
|---|---|---|---|
| `idx_jobs_status_available` | `(status, available_at, priority)` | 001 | Supports `JobQueue.claim_next()`'s candidate SELECT. |
| `idx_jobs_archive` | `(archive_id)` | 001 | "Supports 'does this archive already have a job of a given type' existence checks used before enqueueing duplicate work" — i.e. this index exists *specifically* to make the `NOT EXISTS`/prior-`SELECT` convention fast, but it is a plain (non-unique) index and enforces nothing. |
| `idx_jobs_failure_category` | `(job_type, status, failure_category)` | 005 | Supports the failure-review/triage query, unrelated to enqueue dedup but notable because it's the closest existing index to `(job_type, ..., status)` and could inform a future partial index's leading columns. |

No migration after 001 has ever touched the `jobs` table's own constraints
(005 only added the `failure_category` column and its index).

### Other tables referencing or bordering job identity

- `near_duplicate_candidates` (migration 008) has
  `UNIQUE (archive_a_id, archive_b_id, match_method)` — a **full** (not
  partial) unique constraint: re-running the same detector against the same
  pair always upserts/conflicts regardless of `review_status`. This is a
  useful contrast case in section 6: `jobs` cannot use a similarly simple
  full unique constraint, because a `jobs` row's terminal history
  (`completed`/`failed`/`cancelled`) is explicitly meant to be preserved as
  distinct rows across retries/re-runs, not merged.
- `archive_quarantine` (migration 009) has `UNIQUE (archive_id)`, a
  full uniqueness constraint over a table that is not itself
  append-only (`status` transitions `pending_redownload` → `resolved`/
  `abandoned` in place); not directly relevant to `jobs` dedup design but
  confirms this codebase already uses both full and (via `idx_jobs_...`
  naming intent, if not enforcement) partial-existence-check patterns
  elsewhere.

---

## 3. Complete enqueue call-site matrix

Five call sites found across two repository-wide search patterns
(`.enqueue(` and reading every file `enqueue_missing` appears in that is not
a test). Every result below appears exactly once; none were found only by
one search pattern and missed by the other (cross-checked in section 9).

| # | Source module.function | Job type | `archive_id` | Default priority / `max_attempts` | Direct `enqueue()` vs helper | Statuses treated as duplicate | Terminal history re-enqueues? |
|---|---|---|---|---|---|---|---|
| 1 | `library/repository.py`::`LibraryRepository._enqueue_inspection_if_absent` (called from `record_archive`, itself called from `scan_library`'s `flush()`) | `inspect_archive` | required, always supplied | 100 / 3 (default) | Direct `self.queue.enqueue()` | `pending`, `claimed`, `running` | Yes — `completed`/`failed`/`cancelled`/`blocked` history does not block a fresh enqueue. |
| 2 | `archive/hashing.py`::`ArchiveHashRepository._enqueue_reinspection_if_absent` (called from `ArchiveHashRepository.save()` when `metadata_changed and enqueue_reinspection`) | `inspect_archive` | required, always supplied | 100 / 3 (default) | Direct `JobQueue(self.connection).enqueue()` | `pending`, `claimed`, `running` | Yes, same as #1. |
| 3 | `archive/hashing.py`::`ArchiveHashRepository.enqueue_missing` | `calculate_archive_hash` | required, always supplied | 200 / 3 (default) | Helper (`NOT EXISTS` guarded bulk loop) | `pending`, `claimed`, `running` **only** — `failed` is *not* excluded | **Yes for `failed`** — a permanently-failed hash job does not block re-enqueue; every subsequent `enqueue_missing()` call creates another job for that archive as long as it still lacks a hash row. See section 1 and section 5 for why this is flagged as an open question. |
| 4 | `archive/page_hashing.py`::`ArchivePageHashRepository.enqueue_missing` | `hash_archive_pages` | required, always supplied | 300 / 3 (default) | Helper (`NOT EXISTS` guarded bulk loop) | `pending`, `claimed`, `running`, `failed` | No — a permanently-failed job blocks re-enqueue (by design, per in-code comment: "so a job that previously failed permanently isn't silently retried here"). |
| 5 | `archive/perceptual_hashing.py`::`ArchivePerceptualHashRepository.enqueue_missing` | `hash_archive_pages_perceptual` | required, always supplied | 250 / 3 (default) | Helper (`NOT EXISTS` guarded bulk loop, explicitly commented as "mirroring ... page_hashing.py") | `pending`, `claimed`, `running`, `failed` | No, same as #4. |

`JobQueue.enqueue()` itself (`comic_automation/jobs/queue.py`) performs no
duplicate checking of any kind — it is a single unconditional `INSERT`. Every
guard listed above is caller-side, not queue-side.

### Paths explicitly examined and confirmed to **not** enqueue

- **`comic_automation/archive/source_drift_recovery.py`** (source-drift
  recovery): calls `ArchiveHashRepository.save(..., enqueue_reinspection=
  False)` explicitly, opting out of path #2 above. It additionally reads
  `jobs` via a private `_active_conflicts()` helper (`status IN ('pending',
  'claimed', 'running')` for the same `archive_id`, excluding the job being
  reviewed) inside its own `BEGIN IMMEDIATE` block, and raises
  `RecoveryPreconditionError` if any conflict is found — i.e. this path
  actively *refuses to proceed* if another active job already targets the
  archive, rather than creating one. It never calls `JobQueue.enqueue()`.
- **`comic_automation/service.py`** (long-running service
  initialization/`ComicAutomationService.initialize()`): calls
  `JobQueue.recover_abandoned()` once at startup, which only transitions
  existing `claimed`/`running` rows to `pending`/`failed` — it inserts no
  new rows and calls `enqueue()` nowhere in the file.
- **`comic_automation/archive/cli.py`** (bounded `inspect_archive` CLI):
  constructs a `JobWorker` and drives `worker.run_once()` in a loop to
  *process* already-enqueued jobs; it never calls `.enqueue()` or
  `enqueue_missing()` anywhere in the file.
- **`comic_automation/archive/near_duplicate.py` /
  `near_duplicate_cli.py`** (near-duplicate candidate generation): neither
  file references `JobQueue`, `enqueue`, or any `job_type` string. Candidate
  generation writes directly to `near_duplicate_candidates` and is not
  wired into the job queue at all — there is no `job_type` for it to
  enqueue as.
- **`comic_automation/archive/quarantine.py`,
  `comic_automation/archive/perceptual_reuse_analysis.py`,
  `comic_automation/archive/duplicate_resolution.py`**: each reads `jobs`
  (`FROM jobs` appears in all three) for reporting/lookup purposes only;
  none contains `enqueue` or `INSERT INTO jobs`.
- **`hash_cli.py`, `page_hash_cli.py`, `perceptual_hash_cli.py`**: each is a
  thin wrapper that optionally calls its repository's `enqueue_missing()`
  (paths #3/#4/#5 above) and then drives a bounded `JobWorker` loop; none
  calls `JobQueue.enqueue()` directly or adds any additional duplicate
  logic of its own.

---

## 4. Sequential versus concurrent behavior

"Sequential" below means repeated, non-overlapping invocations (no two
enqueue attempts for the same `archive_id`/`job_type` are ever in flight at
the same time). "Concurrent" means two or more connections/threads/processes
can genuinely interleave their statements.

| # | Path | Sequentially safe? | Concurrently safe? | Basis for the concurrency verdict |
|---|---|---|---|---|
| 1 | Discovery (`_enqueue_inspection_if_absent`) | Yes | **Conditional — safe only against another path #1 invocation** | The entire `SELECT ... WHERE archive_id = ? AND job_type = 'inspect_archive' AND status IN (...)` and the subsequent `queue.enqueue()` INSERT execute inside one `BEGIN IMMEDIATE ... COMMIT` block opened by the caller, `scan_library()`'s `flush()`. `BEGIN IMMEDIATE` acquires SQLite's write lock (the RESERVED lock) *before* the SELECT runs, so a second connection racing with **another `BEGIN IMMEDIATE`-guarded writer** (e.g. a second, concurrent discovery scan) cannot insert a competing row in the gap, because there is no gap during which the lock is released. This does **not** generalize to every concurrent writer: the lock only delays a competing write, it does not force an already-unguarded caller to repeat its own check. Path #2 (`_enqueue_reinspection_if_absent`) is exactly such a caller — its `SELECT` is a bare, unwrapped statement that can complete, release any lock, and be followed by its own `INSERT` *after* discovery's transaction has already committed a row discovery's check didn't need to see and path #2's check didn't see either: (1) path #2's `SELECT` finds no active `inspect_archive` job; (2) discovery's `BEGIN IMMEDIATE` acquires the lock, checks, inserts, commits; (3) path #2, still acting on its stale result from step 1, issues the `INSERT` it had already decided on; (4) two active rows now exist for the same archive. So path #1 is safe against itself, not safe against path #2, and path #2 is unaffected by path #1's locking discipline either way. |
| 2 | Hash-triggered reinspection (`_enqueue_reinspection_if_absent`) | Yes, in isolation | **No** | Called from inside `ArchiveHashRepository.save()`, which is called from `CalculateArchiveHashHandler.__call__()` — i.e. from inside a `JobWorker` handler invocation, which `JobWorker.run_once()` does not wrap in any transaction. The SELECT and the INSERT are two independent, unwrapped statements (the connection uses `isolation_level=None`, so each bare `execute()` is its own auto-committing unit outside an explicit `BEGIN`). Between them, any other connection can commit a competing `inspect_archive` insert for the same `archive_id` (from path #1, or from a second, concurrently-running `calculate_archive_hash` handler hitting this same method for the same archive) and this path's already-completed SELECT would not see it. |
| 3 | `calculate_archive_hash` bulk (`ArchiveHashRepository.enqueue_missing`) | **Conditional** — safe against re-duplicating a `pending`/`claimed`/`running` job, but *not* safe against re-duplicating after a `failed` terminal outcome (see section 1/3) | **No** | The candidate `SELECT ... NOT EXISTS (...)` and the per-row `queue.enqueue()` loop are not wrapped in any `BEGIN IMMEDIATE`. Two concurrent invocations of `enqueue_missing()` (e.g. an operator running `hash_cli.py --enqueue-missing` twice, or a scheduler misfire) can both execute the SELECT before either commits its INSERTs, so both will see the same "missing" archive and both will enqueue a job for it. |
| 4 | `hash_archive_pages` bulk (`ArchivePageHashRepository.enqueue_missing`) | Yes (including the `failed` case, since `failed` is excluded from re-enqueue) | **No** | Same structural gap as #3 — no `BEGIN IMMEDIATE` around the SELECT/INSERT-loop. |
| 5 | `hash_archive_pages_perceptual` bulk (`ArchivePerceptualHashRepository.enqueue_missing`) | Yes, same as #4 | **No** | Same structural gap as #3/#4. |

**Totals against the five production paths:**

- Safe against sequential duplication (no caveats): **4 of 5** (#1, #2, #4, #5).
- Conditionally/inconsistently safe sequentially: **1 of 5** (#3, only for
  non-terminal-failed archives).
- Demonstrably/provably safe against **every** currently-existing concurrent
  enqueue path for the same job type: **0 of 5**. Path #1 is safe against a
  second instance of itself (two concurrent discovery scans), but that is a
  narrower claim than "safe against concurrent duplication" — it is not safe
  against path #2, which targets the same job type (`inspect_archive`) and
  is not lock-guarded at all. No path is globally protected.
- Rely only on an application-level convention with no lock held across the
  check-then-insert gap against at least one other concurrent path targeting
  the same job type (i.e. exposed to concurrent duplication today by some
  realistic race): **5 of 5** — #2 through #5 unconditionally, and #1
  conditionally (exposed specifically to #2, though not to another instance
  of itself).

None of the five paths can create a duplicate *within a single call* to
themselves for a single archive (each is either a single-archive check, or a
bulk `SELECT` whose `WHERE` clause structurally returns at most one row per
`archive_id`, verified by reading each query's join shape: every join key
back to `archive_id`-unique or `is_current = 1`-filtered tables). The
concurrency risk is strictly cross-call / cross-connection, not
within-call.

---

## 5. Handler-side idempotency limitations

This section answers "if a duplicate active job for the same archive did
get created and both were processed, would that be harmless?" — a distinct
question from "can a duplicate be created" (sections 3-4).

- **`ArchiveInspectionRepository.save()`** (backing `inspect_archive`,
  paths #1/#2's target job type): `archive_inspections` has `archive_id
  INTEGER NOT NULL UNIQUE` (migration 003), and the migration's own comment
  states re-inspecting "overwrites the row via the `ON CONFLICT` upsert ...
  rather than accumulating history." Two concurrent `inspect_archive`
  handlers for the same archive would each independently read the file,
  compute a result, and upsert — the *last write wins*, deterministically
  at the database level (SQLite serializes the two `UPDATE`/upsert
  statements), but the two handlers' in-memory computation (file read,
  format detection) still both ran, doubling I/O. Whether the two results
  actually agree depends on the file not changing between the two reads;
  this audit does not attempt to prove that generally.
- **`ArchiveHashRepository.save()`** (`calculate_archive_hash`):
  `archive_hashes` has `archive_id INTEGER NOT NULL UNIQUE` with an
  `ON CONFLICT(archive_id) DO UPDATE` upsert — same last-write-wins
  property as above.
- **`ArchivePageHashRepository.save()`** (`hash_archive_pages`): deletes
  all existing `archive_pages` rows for the `archive_id` and reinserts,
  wrapped in its own `BEGIN IMMEDIATE` (`owns_transaction` guard — it
  reuses an already-open transaction if the caller has one, otherwise opens
  its own). `page_hashes` and `archive_content_signatures` are similarly
  delete/reinsert or upsert. Two concurrent executions would each acquire
  the write lock in turn (SQLite serializes them) and each fully replace
  the page inventory — the final state reflects whichever save ran last,
  not a merge or corruption, **but** this is a claim about final-state
  determinism, not about wasted work: both handlers still fully re-read and
  re-hash the archive's pages.
- **`ArchivePerceptualHashRepository.save()`** (`hash_archive_pages_perceptual`):
  upserts into `page_hashes` via `ON CONFLICT(page_id, algorithm,
  algorithm_version) DO UPDATE`, also inside its own `BEGIN IMMEDIATE`.
  Same last-write-wins property as the others.

**What this means for a broad uniqueness rule:** every handler's *database
write* is idempotent in the sense that re-running it converges on the same
final row values (upsert or delete+reinsert, never append-only
accumulation) — so if the enqueue-time race in section 4 does let two
duplicate active jobs exist and both get processed, the **data does not
get corrupted or duplicated**. What is **not** demonstrated (and would
require the dedicated concurrency tests in section 9, not code reading) is
whether the two concurrent handler executions interleave safely at the
statement level for `hash_archive_pages`'s delete-then-reinsert sequence
specifically — a delete from one execution interleaving with an insert from
the other, both inside their own `BEGIN IMMEDIATE`, is serialized by SQLite
(one full transaction completes before the other starts) — the same
"guarded against another instance of itself" reasoning section 4 applied to
path #1's own enqueue check, which held for path #1 only when *both* sides
of the race were `BEGIN IMMEDIATE`-guarded. Whether every real-world writer
that could touch `archive_pages` for the same archive concurrently is
guarded that consistently (as opposed to path #1 vs. path #2's mismatch) is
not verified here and is called out as believed-safe-by-analogy rather than
proven to the same standard as this audit applied elsewhere.

**This is explicitly not the same problem as the enqueue race.** A
duplicate *row* (two `jobs` rows for the same archive/type) is a queue-
layer problem, fixable at the database level (section 6) without touching
any handler. A single row being processed twice by two workers because
`recover_abandoned()` reassigned it while the original worker was still
alive is a *fencing* problem, already documented in
`docs/jobs_worker_retry_audit.md`'s "Missing lease/idempotency protections"
section, and is out of scope for a duplicate-enqueue fix — closing the
enqueue race does not close that gap, and vice versa.

---

## 6. Candidate design options (evaluated, not implemented)

### Option A — Partial unique index on active statuses

```sql
CREATE UNIQUE INDEX idx_jobs_unique_active
    ON jobs(job_type, archive_id)
    WHERE status IN ('pending', 'claimed', 'running');
```

- SQLite has supported partial indexes since 3.8.0 (well within this
  project's requirements); a `UNIQUE` partial index enforces uniqueness
  only among rows matching the `WHERE` predicate, so `completed`/`failed`/
  `cancelled`/`blocked` rows for the same `(job_type, archive_id)` are
  never compared against each other or against active rows outside the
  predicate — terminal history is fully preserved, which a plain (non-
  partial) `UNIQUE(job_type, archive_id)` constraint could not do (it would
  reject a legitimate retry-after-completion `inspect_archive` enqueue,
  since `completed` rows would still collide).
- **NULL behavior**: standard SQL (and SQLite) treats every `NULL` as
  distinct from every other `NULL` for uniqueness purposes, including
  inside a partial unique index. If a future job type ever enqueues with
  `archive_id = NULL`, this index would **not** prevent duplicates among
  those `NULL`-archive rows — two simultaneously-`pending` jobs of the same
  `job_type` with `archive_id = NULL` would both be allowed. Since section
  3 confirms no current production job type does this, Option A is
  sufficient today, but this NULL caveat should be written into the
  migration's own comment so a future job type with an optional
  `archive_id` doesn't silently regain the duplicate-row problem. If that
  need ever arises, the standard SQLite workaround is a functional/
  expression index, e.g. `UNIQUE(job_type, COALESCE(archive_id, -1))
  WHERE status IN (...)` (safe here because `archive_id` is a positive
  autoincrement key, so `-1` can never collide with a real one) — noted as
  a future extension, not proposed for adoption now.
- **Whether it changes behavior other than rejecting duplicates**: yes —
  every existing `enqueue()` call site that currently relies on its own
  `NOT EXISTS`/`SELECT` convention would, after this migration, also be
  protected against the concurrent race those checks can't currently rule
  out, *for free*, without code changes to the check logic itself (though
  see Option B for why a code change is still recommended). Any call site
  that does **not** currently check for an existing job (there are none in
  production today per section 3, but this protects future ones too) would
  start raising `sqlite3.IntegrityError` on a duplicate attempt instead of
  silently succeeding.

### Option B — Centralized atomic `enqueue_if_absent()` operation

A single `JobQueue.enqueue_if_absent(job_type, *, archive_id, ...)` method,
built on top of Option A's constraint, that inserts without a preceding
`SELECT` and relies on the database, not caller-matched error text, to
detect a collision. Two ways to implement the detection correctly, either
acceptable, both avoiding the mistake of matching `sqlite3.IntegrityError`
against a specific index name or column list — SQLite's uniqueness error
commonly identifies the *conflicting columns*, not the partial index's
name, so matching on the index name is not reliable:

- **Preferred:** `INSERT ... ON CONFLICT(job_type, archive_id) WHERE status
  IN ('pending', 'claimed', 'running') DO NOTHING`, mirroring Option A's own
  partial-index predicate in the `ON CONFLICT` target so SQLite resolves the
  conflict against exactly that index. The statement then either inserts a
  new row (rowcount 1) or silently no-ops (rowcount 0); the caller checks
  the row count/`lastrowid` rather than catching an exception at all.
  **The return value should be a plain `created` / `already_active` outcome,
  not a promise to also hand back the conflicting row.** A follow-up
  `SELECT` for that row, run as a separate statement after the `INSERT`
  commits, is not guaranteed to still find it active: the row can complete,
  fail, or otherwise leave the active-status set in the gap between the
  `INSERT ... DO NOTHING` and the follow-up query, and a lookup keyed only
  on `(job_type, archive_id)` with no status filter could then return an
  unrelated later row instead. If a caller genuinely needs the existing
  job back (not just the outcome), that requires either running the
  `INSERT ... DO NOTHING` and the confirming `SELECT` inside one
  transaction that holds the write lock across both (so nothing else can
  transition the row in between), or a bounded retry loop that re-attempts
  the `SELECT` (and tolerates it legitimately finding nothing, in which
  case the caller should treat the job as no longer active rather than
  retrying indefinitely) — not documented here as a blocking requirement,
  since no current call site (section 3) needs the existing job back, only
  the outcome.
- **Alternative:** catch `sqlite3.IntegrityError`, narrow it by SQLite's
  structured error code (`sqlite3.Error.sqlite_errorcode` /
  `errorcode == SQLITE_CONSTRAINT_UNIQUE`, not string-matching the message),
  and then run a confirming `SELECT` for an active row matching `(job_type,
  archive_id)`. If that row is found, report `already_active`. If it is
  **not** found, the violation is unconfirmed — it may have come from this
  constraint with the conflicting row having since left the active-status
  set, or from an entirely unrelated uniqueness constraint — and the two
  must not be conflated. Resolve it by **retrying the intended insert, a
  bounded number of times**:
  - if a retry succeeds, report `created`;
  - if a retry conflicts again and the confirming `SELECT` now finds a
    matching active row, report `already_active`;
  - if the retry budget is exhausted with the violation still unconfirmed,
    **propagate the original `IntegrityError`**.

  An unconfirmed constraint error must never be classified as
  `already_active` — doing so would silently swallow an unrelated
  uniqueness failure (for example one introduced by a future constraint on
  this table) and report it as ordinary, expected deduplication. This is
  the reason the `ON CONFLICT ... DO NOTHING` form above is preferred: it
  targets exactly one index by name-free column/predicate match, so it
  never produces an ambiguous error to disambiguate in the first place.

Either approach would:

- give every current and future call site one call to make instead of a
  bespoke `SELECT ... NOT EXISTS` (or `SELECT`-inside-`BEGIN IMMEDIATE`)
  followed by `enqueue()`, removing the possibility of a future call site
  forgetting the check entirely — `JobQueue.enqueue()` itself has no such
  guard, so every guard today is caller-supplied by convention. All five
  current call sites do perform a check; the issue is not that any of them
  skips one, but that four are unguarded (no lock held across
  check-then-insert) and the fifth, discovery, is transactionally guarded
  yet still not globally sufficient (section 4);
- still require Option A underneath it — without the database constraint,
  neither approach has anything to detect; `enqueue_if_absent()` would just
  be the same unprotected `SELECT`-then-`INSERT` race, renamed.

**This does not require resolving the `calculate_archive_hash` `failed`-
status inconsistency first.** The partial index's own `WHERE` clause — and
therefore what `enqueue_if_absent()`'s conflict detection targets — only
ever needs to define "active" (`pending`/`claimed`/`running`), which is
already uniform across every job type today (see Option A). The
inconsistency is a separate, caller-side policy question: *given that no
active row exists, should this call site still refuse to enqueue because
the most recent row for this `(job_type, archive_id)` is `failed`?* That
policy lives in each repository's own pre-check (as it already does today
for `ArchivePageHashRepository`/`ArchivePerceptualHashRepository`, and does
not for `ArchiveHashRepository`) — it is layered *on top of*
`enqueue_if_absent()`, not encoded into the database constraint or into
`enqueue_if_absent()` itself. See section 7 for how this splits into
separate, independently-shippable steps.

### Option C — Per-job-type idempotency keys

Instead of `(job_type, archive_id)`, add an explicit `idempotency_key TEXT`
column populated by the caller (e.g. a hash of the logical work), with a
partial unique index on `(job_type, idempotency_key)` for active statuses.
This generalizes beyond archive-scoped work (useful if a future job type
isn't naturally keyed by a single `archive_id` — for example a batch job
covering many archives at once, which `(job_type, archive_id)` cannot
express). Not needed for any of the five current job types, all of which
are cleanly single-archive-scoped; would add a column and caller-side
key-construction responsibility for no current benefit. Worth deferring
until a job type actually needs it.

### Option D — Allow terminal history while preventing duplicate active work

This is not a fourth alternative so much as the *requirement* Options A-C
must all satisfy, restated explicitly: any schema-level fix must scope
uniqueness to the active-status subset and must never restrict or delete
rows outside that subset. Unlike what an earlier draft of this audit
claimed, this requirement is **already satisfied** by Option A as written:
`pending`/`claimed`/`running` is a uniform, job-type-independent definition
of "active" today, so the partial index does not need to wait on the
`calculate_archive_hash` `failed`-status inconsistency (section 1) to be
resolved. That inconsistency is about caller-side re-enqueue *policy* for
already-terminal rows — which the index never constrains either way — not
about the index's own predicate. See section 7 for the resulting,
un-blocked sequence.

---

## 7. Recommended implementation sequence

This audit recommends, but does not implement, the following order. The
three concerns below are deliberately kept as separate, independently
reviewable/shippable steps rather than one bundled change, because they
answer different questions: whether the database enforces active-row
uniqueness at all (step 1), whether a given caller is *currently allowed*
to re-enqueue after its own prior job failed (step 2, unchanged from
today's behavior), and whether that caller-specific policy should be made
consistent across job types (step 3, a later product decision, not a
prerequisite for steps 1-2).

1. **Add the partial unique index** (Option A) —
   `UNIQUE(job_type, archive_id) WHERE status IN ('pending', 'claimed',
   'running')` — in its own reviewed migration. This does not require
   resolving the `calculate_archive_hash` `failed`-status question first
   (see Option D): the index only defines "active," which is already
   uniform, and it never restricts or touches terminal (`completed`/
   `failed`/`cancelled`/`blocked`) rows for any job type, so today's
   retry-via-re-enqueue behavior for `calculate_archive_hash` keeps working
   exactly as it does now.
2. **Add `JobQueue.enqueue_if_absent()`** (Option B, using the `ON
   CONFLICT ... DO NOTHING` form or the narrow-error-code-plus-confirming-
   query form — not index-name matching) and migrate all five existing
   call sites to use it in place of their hand-rolled `SELECT`/`NOT
   EXISTS` (or, for path #1, `SELECT`-inside-`BEGIN IMMEDIATE`) checks
   against active statuses: the four unguarded, convention-only call sites
   (#2 directly, #3-#5 inside their bulk loops), **and** path #1
   (discovery), even though #1's own check is already conditionally safe
   (section 4) — #1 must move too, because `enqueue_if_absent()`'s
   database-enforced guarantee is what closes #1's actual gap, its lack of
   protection against #2, not a rewrite of #1 in isolation. This step is
   behavior-preserving
   with respect to each call site's **existing** terminal-status policy:
   `ArchivePageHashRepository`/`ArchivePerceptualHashRepository` still need
   to keep their own additional `... OR status = 'failed'` exclusion
   *layered on top of* `enqueue_if_absent()` (e.g. a preceding check, or a
   caller-supplied flag), since `enqueue_if_absent()` itself only ever
   enforces the active-only definition from step 1;
   `ArchiveHashRepository.enqueue_missing()` keeps its current behavior
   unchanged (no additional `failed` exclusion) unless step 3 changes it.
   Nothing here changes *whether* a permanently-failed `calculate_archive_hash`
   job currently blocks re-enqueue — it doesn't, before or after this step.
3. **Separately, resolve the `calculate_archive_hash` terminal-failure
   inconsistency** (section 1/3) as its own product decision, on its own
   timeline, not gating steps 1-2: either (a) add the same `failed`-
   exclusion policy `ArchivePageHashRepository`/`ArchivePerceptualHashRepository`
   already use to `ArchiveHashRepository.enqueue_missing()`'s caller-side
   check, harmonizing all three, or (b) keep the current retry-via-
   re-enqueue behavior and document it as intentional. Either choice is a
   change to that one repository's own pre-check logic, not to the partial
   index or to `enqueue_if_absent()`.
4. **Only after 1-3 are live and validated**, revisit the separate
   lease/fencing-token problem from `docs/jobs_worker_retry_audit.md` — it
   is unrelated to duplicate-row prevention and should not be bundled into
   the same migration or PR, so each can be reviewed and rolled back
   independently.

Option C (idempotency keys) is explicitly deferred until a job type
actually needs it (see section 6).

---

## 8. Required migrations and validation gates

**Migration precondition — detecting existing duplicate active rows.**
Before Option A's constraint can be added, any database that already has
duplicate active rows for the same `(job_type, archive_id)` would fail the
migration (SQLite refuses to create a `UNIQUE` index over data that
violates it). The detection query (read-only, safe to run against a live
or backup database ahead of time, structurally identical to the read-only
audits in `comic_automation/archive/perceptual_failure_audit.py` and
`comic_automation/jobs/abandoned_job_audit.py` — i.e. `mode=ro` +
`PRAGMA query_only`, no writes):

```sql
SELECT job_type, archive_id, COUNT(*) AS duplicate_count
FROM jobs
WHERE status IN ('pending', 'claimed', 'running')
  AND archive_id IS NOT NULL
GROUP BY job_type, archive_id
HAVING COUNT(*) > 1;
```

This predicate matches section 7 step 1's index exactly (`pending`/
`claimed`/`running`) and does not depend on the separate, later step 3
decision about `calculate_archive_hash`'s `failed`-status policy — that
decision affects only whether a *new* job can be created after a *failed*
row, not whether two *active* rows can coexist, so it has no bearing on
this precondition query or on the index it validates.

This audit does not run this query against any database (production,
backup, or otherwise) — it is documented here as the precondition check a
future migration PR must run and report on, not as a finding about whether
such duplicates currently exist. **This document explicitly does not
propose automatically deleting or rewriting any row this query would find**
— per the task constraints, any pre-existing duplicates must be resolved by
an explicit, reviewed, human-directed decision (which row is canonical,
whether the other is cancelled/reassigned/investigated), not by an
automatic migration step.

**Validation gates before the migration ships:**

- The precondition query above returns zero rows against a current
  production snapshot (or every returned row has been manually resolved).
- The deterministic concurrency tests in section 9 exist and pass against
  the new constraint in a disposable test database.
- `JobQueue.enqueue()`'s existing callers (all five, per section 3) are
  confirmed to still succeed for their normal, non-duplicate case after the
  index is added (a regression here would mean the `WHERE` predicate or
  column list doesn't match what section 7 decided).
- The migration is additive only (`CREATE UNIQUE INDEX IF NOT EXISTS`,
  consistent with every existing index statement's style in this codebase)
  and does not alter or drop any existing column, index, or row.

---

## 9. Required deterministic tests

None of these exist today (confirmed by reading `tests/test_job_queue.py`,
`tests/test_archive_hashing.py`, `tests/test_archive_page_hashing.py`,
`tests/test_archive_perceptual_hashing.py`, and `tests/test_service.py` in
full) and all should exist, against a disposable `tmp_path` database, before
Options A/B ship:

1. **Two connections racing `enqueue_missing()` for the same archive**: open
   two `sqlite3.Connection`s to the same file, seed one "missing" candidate
   archive, call `enqueue_missing()` from both without committing/yielding
   in between (e.g. via a monkeypatched hook between the SELECT and the
   INSERT, mirroring `test_only_one_connection_can_claim_job`'s and
   `test_run_audit_raises_if_database_mutated_mid_run`'s existing
   patterns of interrupting a call mid-flight), and assert only one `jobs`
   row is created after both complete. This test should **fail** against
   today's code (proving the race genuinely exists) and pass once Option A
   is in place.
2. **The partial index rejects a duplicate active insert directly**: two
   `INSERT`s for the same `(job_type, archive_id)` while the first is
   `pending`, expect `sqlite3.IntegrityError` on the second.
3. **The partial index allows a new active insert after the prior job
   reached a terminal state**: insert, transition to `completed` (and,
   separately, to `failed`), then insert again for the same `(job_type,
   archive_id)` and expect success in both cases — this is the index's own
   behavior (step 1) and holds regardless of whatever caller-side `failed`-
   status policy section 7 step 3 eventually settles on for
   `calculate_archive_hash` specifically.
4. **`archive_id IS NULL` is not protected by a plain partial index** (documents
   the NULL caveat from section 6 as an explicit regression test, currently
   moot since no job type uses it, but should exist so the gap is visible
   if that ever changes): two `NULL`-archive_id jobs of the same type both
   insert successfully even with the index in place, demonstrating the
   caveat rather than asserting it's fixed.
5. **`enqueue_if_absent()` (Option B) reports `already_active` rather than
   raising**, exercised against a pre-existing active row, and **creates a
   new job and reports `created`** when none exists — asserted as the
   outcome/rowcount signal described in Option B, not as a guarantee that
   the returned value includes the pre-existing row (Option B documents why
   that promise doesn't hold). If implemented via `ON CONFLICT ... DO
   NOTHING`, assert the no-op case produces rowcount 0 and the `created`
   case produces rowcount 1 with the new row's id; if implemented via the
   narrow-error-code-plus-bounded-retry form, assert all three of its
   documented outcomes separately: (a) confirming `SELECT` finds an active
   row → `already_active`; (b) confirming `SELECT` finds nothing and a
   bounded retry then succeeds → `created`; (c) the violation stays
   unconfirmed until the retry budget is exhausted → the original
   `IntegrityError` propagates. Case (c) must include an unrelated
   uniqueness violation (not a `FOREIGN KEY` error, which is a different
   error code and would be rejected by the code-narrowing step before the
   retry logic is reached) to prove an unconfirmed constraint failure is
   never reported as `already_active`. If a variant of
   `enqueue_if_absent()` that also returns the conflicting row is
   implemented, add a separate test proving it holds the write lock across
   the insert-attempt-and-lookup (or correctly bounds/labels a retry loop),
   per Option B's caveat.
6. **Path #1 vs. path #2 concurrent duplication is fixed**: a test
   reproducing this document's section 1/4 race directly — an interrupted
   `_enqueue_reinspection_if_absent()` call (paused after its `SELECT`,
   mirroring the interruption pattern used elsewhere in this codebase, e.g.
   `test_run_audit_raises_if_database_mutated_mid_run`) racing a full
   `_enqueue_inspection_if_absent()` transaction for the same archive.
   Before Option A, both succeed and two rows exist (proving the race from
   this audit); after step 1 (the partial index) and step 2
   (`enqueue_if_absent()`), one of the two must either no-op or block/retry
   rather than create a second active row. This also serves as the
   regression test that discovery's own existing `BEGIN IMMEDIATE`-enclosed
   behavior (safe against another instance of itself, per section 4) is not
   accidentally narrowed by the step 2 refactor.

---

## 10. Explicit deferred work

- **Resolving the `calculate_archive_hash` `failed`-status inconsistency**
  is a product decision (section 7, step 3), not something this audit
  decides, and does not block the partial index or `enqueue_if_absent()`
  (section 7, steps 1-2). Both directions are defensible; neither is
  implemented here.
- **Lease/fencing tokens** for preventing the *same row* from being
  processed twice by two workers (as opposed to two *rows* existing for the
  same logical work) are out of scope — already tracked in
  `docs/jobs_worker_retry_audit.md` and explicitly not conflated with this
  audit's findings (section 5).
- **Idempotency keys (Option C)** are deferred until a job type needs
  identity broader than a single `archive_id`.
- **Whether the bounded CLIs should ever run concurrently by design** (the
  realistic trigger for the concurrent-duplication risk in section 4,
  paths #3-#5) was not determined here — this audit establishes that they
  *can* race if they are, not what an operator's actual deployment/
  scheduling practice is.
- **Whether any external tool or script writes to `jobs` directly**,
  bypassing `JobQueue` entirely, was not re-investigated here; the prior
  audit (`docs/jobs_worker_retry_audit.md`, "Deferred work") already flagged
  this as unresolved and it remains so.
- **This audit did not query any production or backup database, run any
  production CLI, or apply any migration.** Every finding above is derived
  from reading `origin/master`'s source in an isolated worktree
  (`audit/job-enqueue-idempotency`) and the existing test suite's stated
  intent.
