# Job Queue & Worker Retry-Lifecycle Audit

**Status:** originally an evidence-only audit; its Section 3 "small, low-risk
improvements" are now all implemented. Findings reference function names, not
line numbers; every claim below was verified by reading the actual source
referenced.

The findings text below is preserved unedited as the original evidence record
and describes the code as it was at audit time. Resolution status:

| Section 3 item | Status |
| --- | --- |
| Guard the nested `mark_failed()` call inside `run_once()`'s `except Exception` | **Resolved.** `JobWorkerStateError` now carries both the original `processing_exception` and the `transition_exception`, logs both, and raises rather than returning a `WorkerResult` that would imply a known outcome. |
| Mark the "no handler registered" failure as `permanent=True` | **Resolved.** Uses `permanent=True` with a dedicated `MISSING_HANDLER_CATEGORY` rather than defaulting to a retryable `unclassified_error`. |
| Comment `claim_next()`'s attempt-counting timing | **Resolved 2026-07-31.** `queue.py` now documents inline that a claim counts as an attempt even if the worker dies before `mark_running()` or the handler runs. |

The Section 4 items (lease/fencing tokens, heartbeats, per-category or
exponential backoff) and the Section 5 deferred work remain open and unchanged.
The one Section 4 item that *was* implemented separately — a partial unique
index preventing duplicate *active* job rows — closes a different problem
(duplicate rows) than the fencing gap described here (one row processed twice);
see `docs/job_enqueue_idempotency_audit.md` Section 5 for why the two are not
interchangeable.

**Scope:**

- `comic_automation/jobs/models.py`
- `comic_automation/jobs/queue.py` (`JobQueue`)
- `comic_automation/jobs/worker.py` (`JobWorker`)
- `comic_automation/service.py`, only for how it wires `JobQueue`/`JobWorker`
  together (startup recovery timing, per-worker connections, thread model)

**Out of scope:** the individual job handlers (`InspectArchiveHandler`,
`HashArchivePagesHandler`, `HashArchivePagesPerceptualHandler`, etc. — see
`docs/archive_io_resource_audit.md` for those), and the bounded batch CLIs
(`archive/cli.py`, `hash_cli.py`, `perceptual_hash_cli.py`), except to note
which of the queue's safety mechanisms those CLIs do or don't invoke.

---

## State machine

`JobStatus` (`models.py`): `PENDING`, `CLAIMED`, `RUNNING`, `COMPLETED`,
`FAILED`, `CANCELLED`, `BLOCKED`. Only five of these seven values are ever
assigned by `queue.py`/`worker.py`: `CANCELLED` and `BLOCKED` are declared in
the enum but no code path in `JobQueue` or `JobWorker` sets a job to either
status — they exist in the schema/enum but are currently unused by this
lifecycle (see Deferred work).

Transitions actually implemented, each as its own `JobQueue` method:

- `enqueue()`: (new row) → `PENDING`
- `claim_next()`: `PENDING` → `CLAIMED` (sets `claimed_at`, `worker_id`,
  increments `attempts`, clears `started_at`/`completed_at`/`error_message`)
- `mark_running()`: `CLAIMED` → `RUNNING` (sets `started_at`)
- `mark_completed()`: `CLAIMED` **or** `RUNNING` → `COMPLETED` (accepts
  either source status, since a caller may skip `mark_running()` and
  complete directly after claiming)
- `mark_failed()`: `CLAIMED` **or** `RUNNING` → either back to `PENDING`
  (retry path) or to `FAILED` (terminal path), depending on attempts
  remaining and the `permanent` flag
- `recover_abandoned()`: `CLAIMED`/`RUNNING` (stale) → `PENDING` (if attempts
  remain) or `FAILED` (if exhausted)

Every transition method issues an `UPDATE ... WHERE id = ? AND status = ?`
(or `status IN (...)`) and checks `cursor.rowcount`, raising
`InvalidJobTransitionError` via `_raise_transition_error()` if the row didn't
match — so an invalid transition (wrong source status, or a `worker_id`
ownership mismatch) fails loudly rather than silently corrupting state.

---

## Attempt limits and retry timing

`attempts` is incremented in `claim_next()`'s `UPDATE`, not in
`mark_failed()` — meaning **a claim counts as an attempt even if the job
never actually starts running** (e.g. a crash between `claim_next()` and
`mark_running()`, or a handler lookup failure). `max_attempts` defaults to 3
(`enqueue()`'s default parameter), validated to be `>= 1`.

`mark_failed()`'s retry-vs-terminal decision:

```python
if not permanent and job.attempts < job.max_attempts:
    # retry: back to PENDING with available_at = now + retry_delay_seconds
else:
    # terminal: FAILED
```

`retry_delay_seconds` is supplied by the caller of `mark_failed()` — in
practice, `JobWorker.run_once()` passes `self.retry_delay_seconds` (a
constructor parameter, default `30`) uniformly for every failure, regardless
of failure category. There is no per-category backoff (e.g. a
`filesystem_permission` failure and a generic `unclassified_error` both wait
the same fixed delay), and no exponential backoff across successive
attempts of the *same* job — attempt 1's retry delay and attempt 2's retry
delay (if `max_attempts > 2`) are identical, both equal to the worker's
fixed `retry_delay_seconds`.

`permanent=True` (set by `JobWorker.run_once()` when the caught exception is
a `PermanentJobError`) skips the attempts check entirely and goes straight
to `FAILED` regardless of `attempts`/`max_attempts` — so a `PermanentJobError`
on attempt 1 of a job with `max_attempts=3` still terminates immediately,
by design.

---

## Crash windows and stale jobs

**Detection is a one-shot startup check, not continuous.** `recover_abandoned()`
is only ever invoked from `ComicAutomationService.initialize()`, which itself
only runs once, at the start of `ComicAutomationService.run()`, before any
worker threads are started:

```python
def initialize(self) -> list[int]:
    ...
    recovered = JobQueue(connection).recover_abandoned(
        older_than_seconds=self.abandoned_after_seconds
    )
```

There is no periodic re-invocation of `recover_abandoned()` anywhere in
`service.py` while workers are running — a job abandoned by a worker thread
crash *during* a long-running service session (as opposed to abandoned by an
interrupted previous process, caught at the *next* startup) will sit in
`claimed`/`running` indefinitely until the service is restarted. Confirmed
by full-file review of `service.py`: no scheduled-task/timer/loop calls
`recover_abandoned()` outside `initialize()`.

**The bounded batch CLIs never call `recover_abandoned()` at all.** Grep
across the repository shows `recover_abandoned` is referenced in exactly
three files: `queue.py` (definition), `tests/test_job_queue.py`, and
`service.py` (the one call site above). None of `archive/cli.py`,
`hash_cli.py`, `page_hash_cli.py`, or `perceptual_hash_cli.py` call it. Since
this project's actual production workflow runs bounded CLI batches (not the
long-running `ComicAutomationService`), **a job abandoned by a crashed or
killed CLI process has no automatic recovery path at all** in that workflow
— it remains `claimed`/`running` until either the long-running service is
started at least once, or an operator manually invokes
`JobQueue.recover_abandoned()`.

**Staleness cutoff, not a lease/heartbeat.** `recover_abandoned()`'s
"abandoned" test is a single timestamp comparison against whichever service
call happens to invoke it:

```python
WHERE status IN (?, ?)
  AND COALESCE(started_at, claimed_at) <= ?
```

There is no heartbeat/lease-renewal mechanism updating `started_at` (or any
other column) while a handler is genuinely still running — a legitimately
long-running job (longer than `abandoned_after_seconds`, default `300`) looks
identical, from `recover_abandoned()`'s perspective, to a job whose worker
actually crashed. This matters specifically because recovery only runs at
service startup: if a second service process is started while a worker from
the first process is still genuinely in progress and older than the cutoff,
`recover_abandoned()` will reset its job to `PENDING`, making it eligible for
a *second* worker to claim and process concurrently with the first (see
Missing lease/idempotency protections below). An ordinary restart that has
already stopped and joined every original worker does not create this
overlap; the risk requires overlapping processes or an otherwise still-live
original worker.

---

## Atomicity

**Claim race.** `claim_next()` wraps its candidate-selection `SELECT` and
claiming `UPDATE` in `BEGIN IMMEDIATE ... COMMIT`, taking SQLite's write
lock before the `SELECT`, so no other connection can claim the same
candidate between the `SELECT` and the `UPDATE`. The `UPDATE`'s own
`WHERE id = ? AND status = ?` plus a `cursor.rowcount != 1` check
(`ROLLBACK` + return `None` if not exactly one row changed) is defense in
depth on top of the transaction isolation — a "belt and suspenders" pattern
that doesn't rely on `BEGIN IMMEDIATE` alone.

**`mark_failed()` and `recover_abandoned()`** are each similarly wrapped in
their own `BEGIN IMMEDIATE ... COMMIT`/`ROLLBACK` block.

**`mark_running()` and `mark_completed()` are intentionally single-statement
compare-and-swap updates.** Each uses a bare `UPDATE` with the expected
source status and, when supplied, worker ownership in its `WHERE` clause,
then requires `cursor.rowcount == 1`. The connection uses
`isolation_level=None`, so each statement is its own atomic autocommit unit.
No read-before-write transaction is needed for these transitions: the state
check and mutation occur in the same SQLite statement. Wrapping either in
`BEGIN IMMEDIATE` would not strengthen this invariant and would hold the
database write lock longer.

**Nested-failure gap in `JobWorker.run_once()`.** The `except Exception`
block that calls `self.queue.mark_failed(...)` after a handler failure is
itself unguarded:

```python
except Exception as exc:
    ...
    failed_job = self.queue.mark_failed(
        job.id, str(exc), ...
    )
```

`mark_failed()` itself raises `InvalidJobTransitionError` if the job is not
currently `CLAIMED`/`RUNNING` (e.g. if `mark_running()` had already failed
due to a status/ownership mismatch caused by a concurrent
`recover_abandoned()` reassigning the job elsewhere). If that happens, the
`InvalidJobTransitionError` from *within* the exception handler is not
caught by anything in `run_once()` and propagates up to the caller
uncaught — a double-fault scenario with no fallback path, distinct from the
normal single-exception handling `run_once()` is otherwise built around.

**Per-worker connections.** `ComicAutomationService._run_worker()` opens a
separate `database_connection` per worker thread (each `JobWorker` gets its
own `sqlite3.Connection`), so all of the above atomicity guarantees are
cross-connection (relying on SQLite's file-level locking under WAL), not
just cross-thread-safe within a single connection.

---

## Missing lease/idempotency protections

**No fencing token.** A job's "ownership" is represented only by the
`worker_id` column plus `status`, checked at each transition. There is no
monotonically increasing epoch/fencing token that a handler's *side effects*
(e.g. `ArchivePageHashRepository.save`, `ArchivePerceptualHashRepository.save`)
could check before committing their own database writes. Concretely: if
`recover_abandoned()` resets a job to `PENDING` because it looked stale
(cutoff exceeded), but the original worker was in fact still alive and
genuinely still running the handler, both the original ("zombie") worker and
a new worker that claims the reset job can execute the same handler
concurrently against the same `archive_id`. `JobQueue`'s transition guards
(`WHERE status = ...`, `rowcount` check) will correctly prevent *both*
workers from successfully calling `mark_completed()` on the same job row
(only one will match and succeed; the other raises
`InvalidJobTransitionError`), but nothing prevents both workers from having
already executed the handler's actual archive I/O and database writes before
either one reaches `mark_completed()`.

**Partial mitigation at the data layer, not the queue layer.** The audited
handlers' repository `save()` methods use deterministic replacement/upsert
patterns — `ArchivePageHashRepository.save()` deletes and reinserts
`archive_pages`, while hashing signature/page tables use `INSERT ... ON
CONFLICT DO UPDATE`. Sequential reruns therefore tend to converge on the
same stored values. That does **not** prove concurrent duplicate executions
safe: interleaved delete/reinsert and upsert transactions still need a
dedicated concurrency test. In all cases this behavior belongs to the
handler/repository layer (see `docs/archive_io_resource_audit.md`), not to
`JobQueue`/`JobWorker`; the queue has no idempotency key or schema-level
dedupe mechanism of its own.

**No unique constraint preventing duplicate in-flight jobs.** The `jobs`
table (`database/migrations/001_operational_foundation.sql`) has no `UNIQUE`
constraint over `(job_type, archive_id)` or similar. The existing
`enqueue_missing()` methods on `ArchivePageHashRepository` and
`ArchivePerceptualHashRepository` each defend against duplicate enqueueing
with an application-level `NOT EXISTS (... WHERE archive_id = ? AND
job_type = ? AND status IN ('pending','claimed','running','failed'))`
check — but this is a convention followed by those two call sites, not a
database-level guarantee. Any other code path that calls
`JobQueue.enqueue()` directly (bypassing `enqueue_missing()`) can create a
second, fully independent job for the same `archive_id`/`job_type`, which
`claim_next()` would then happily hand to a second worker to run
concurrently with the first.

---

## Error classification and retry decision-making (`worker.py`)

`JobWorker.run_once()` classifies every handler exception via
`getattr(exc, "category", "unclassified_error")` and treats
`isinstance(exc, PermanentJobError)` as the sole signal for
`permanent=True`. This means:

- Any handler exception that is a plain `CategorizedJobError` (not the
  `PermanentJobError` subclass) is always retryable (subject to
  `attempts < max_attempts`), regardless of its `category` string — the
  category is purely informational/diagnostic at this layer, not itself a
  retry/no-retry signal. (The categories that end up meaning "permanent" —
  e.g. `corrupt_archive`, `unsupported_archive_format` — are only permanent
  because the *handler* chose to raise `PermanentJobError` rather than
  `CategorizedJobError` for them, not because `worker.py` inspects the
  category string.)
- An exception that is neither `CategorizedJobError` nor `PermanentJobError`
  (e.g. an unexpected `KeyError` or `AttributeError` bug in a handler) still
  gets caught by the broad `except Exception`, defaults to
  `category="unclassified_error"`, and is retried like any other transient
  failure — a genuine programming bug in a handler will be retried up to
  `max_attempts` times rather than surfaced as an immediately-terminal
  condition distinct from an expected transient I/O error.

`run_once()`'s no-handler-registered branch (`handler is None`) calls
`mark_failed()` with no `failure_category` or `permanent=True` — this always
takes the retry path (subject to `attempts < max_attempts`) even though "no
handler is registered for this job_type" can never resolve itself on retry;
it will retry up to `max_attempts` times, then land in `FAILED` with
`error_message` describing the missing handler, `failure_category` defaulted
to `mark_failed()`'s own default (`"unclassified_error"`). Comment in the
code (`# This should be unreachable because claim_next() is filtered by the
registered handler names`) suggests this is believed impossible in practice,
but the fallback path that exists for it is not itself marked permanent.

---

## Prioritized Findings

### 1. Confirmed safeguards already present

- `claim_next()` uses `BEGIN IMMEDIATE` plus a `WHERE status = ?` /
  `rowcount` check as defense-in-depth against concurrent claim races —
  correct under SQLite's locking model and doesn't rely on the transaction
  boundary alone.
- `mark_running()` and `mark_completed()` use atomic conditional updates,
  require `cursor.rowcount == 1`, and raise `InvalidJobTransitionError` on
  unexpected source status or worker ownership. `mark_failed()` instead
  acquires `BEGIN IMMEDIATE`, reads and validates status/ownership while
  holding the write lock, and only then updates the row.
- `mark_failed()` and `recover_abandoned()` correctly give abandoned/failed
  jobs the same "retry if attempts remain, else terminal" treatment,
  keeping the two failure paths consistent with each other.
- `PermanentJobError` gives handlers a clean way to skip the retry loop
  entirely for errors known to be non-transient (e.g. corrupt archives),
  independent of `attempts`/`max_attempts`.
- `recover_abandoned()` runs automatically at every service startup via
  `ComicAutomationService.initialize()`, so a crashed long-running-service
  session is self-healing on the next restart without operator
  intervention.

### 2. Confirmed risks in current code

- **No continuous stale-job recovery.** `recover_abandoned()` only runs once
  at service startup; a worker-thread crash during a live session leaves its
  job stuck in `claimed`/`running` until the next restart.
- **No recovery path at all in the bounded-CLI workflow.** None of the
  batch CLIs call `recover_abandoned()`; a crashed/killed CLI process leaves
  its claimed job stuck indefinitely with no automatic remediation.
- **No lease/fencing mechanism.** Staleness is a fixed time cutoff, not a
  heartbeat; a legitimately slow job and a genuinely crashed job look
  identical to `recover_abandoned()`. Combined with the one-shot recovery
  timing above, a false-positive "abandoned" classification can let two
  workers execute the same handler concurrently against the same archive,
  with no fencing token to detect or prevent it at the queue layer.
- **No database-level duplicate-job guard.** No `UNIQUE` constraint exists
  over `(job_type, archive_id)`; duplicate-prevention is an
  application-level convention followed only by `enqueue_missing()`, not
  enforced for arbitrary `JobQueue.enqueue()` callers.
- **Uncaught nested failure in `run_once()`'s exception handler.** If
  `mark_failed()` itself raises `InvalidJobTransitionError` while already
  handling a prior exception (e.g. the job's status/ownership changed
  concurrently), that second exception propagates uncaught out of
  `run_once()`, unlike every other failure mode in this function.
- **Retry timing is uniform, not category-aware.** Every retryable failure
  waits the same fixed `retry_delay_seconds` regardless of failure category
  or attempt number — no exponential backoff, no per-category tuning (e.g.
  a permission error and a generic unclassified error retry on the same
  schedule).
- **A claim always counts as an attempt**, even if the job crashes before
  `mark_running()`/the handler ever executes — a worker that dies
  immediately after claiming (before doing any real work) still consumes
  one of the job's limited `max_attempts`.

### 3. Small, low-risk improvements

- Wrap the `mark_failed()` call inside `run_once()`'s `except Exception`
  block in its own `try`/`except InvalidJobTransitionError`. Log both the
  original handler/transition exception and the failure to persist its
  outcome, then raise a dedicated worker-state error with exception chaining.
  Do not return a normal `WorkerResult`, because the queue outcome is unknown
  and allowing the caller to continue would conceal inconsistent state.
- Mark the "no handler registered" `mark_failed()` call in `run_once()` as
  `permanent=True` (or give it a dedicated non-retryable failure category),
  since retrying it can never succeed.
- Add an explicit code comment on `claim_next()`'s `attempts + 1` increment
  noting that attempts are counted at claim time, not at handler-start time,
  so future readers don't assume the two are equivalent.

### 4. Changes requiring schema design or concurrency validation

- Introducing a lease/fencing-token mechanism (e.g. a monotonic `lease_epoch`
  column checked by both `JobQueue` transitions and handler-level repository
  writes) is a schema and cross-cutting behavior change touching every
  handler in `docs/archive_io_resource_audit.md`'s scope; it needs its own
  design review and migration, not a drop-in fix, and should be validated
  against concurrent-worker test scenarios before adoption.
- Adding a heartbeat/lease-renewal update during long-running handlers (so
  `recover_abandoned()` can distinguish "still working" from "crashed")
  changes the write frequency to the `jobs` table during normal operation
  and should be evaluated for contention impact under the existing
  `BEGIN IMMEDIATE` locking model before rollout.
- Adding a `UNIQUE(job_type, archive_id)`-style constraint (or a partial
  unique index restricted to non-terminal statuses) to prevent duplicate
  in-flight jobs at the schema level is a migration that could reject
  inserts current code doesn't expect to fail; it needs review of every
  existing `enqueue()` call site (not just `enqueue_missing()`) before
  being added.
- Any change to retry timing (per-category backoff, exponential backoff)
  changes observable job-processing behavior under load and should be
  evaluated against the existing bounded-batch CLI workflows (which
  currently assume a fixed, predictable retry delay) before adoption.

### 5. Deferred work

- `JobStatus.CANCELLED` and `JobStatus.BLOCKED` are declared but never set
  by any code path in `queue.py`/`worker.py` — whether these are planned,
  vestigial, or intended for a caller outside this audit's scope wasn't
  determined here and would need its own investigation.
- This audit did not trace whether any external tool or script directly
  manipulates the `jobs` table (bypassing `JobQueue` entirely) in a way that
  could violate the transition guarantees documented here; only
  `JobQueue`'s own methods were reviewed.
- A dedicated concurrency test (two `JobWorker` instances against the same
  database, deliberately racing a `recover_abandoned()` call against a
  slow-but-alive handler) would be needed to empirically confirm the
  double-processing scenario described under "Missing lease/idempotency
  protections" rather than relying on code-reading alone.
- The interaction between `recover_abandoned()`'s startup-only timing and
  the bounded-CLI workflow's complete lack of any recovery call was
  identified here as a gap, but no recommendation is made in this audit
  about whether the CLIs *should* call `recover_abandoned()` (and with what
  cutoff) — that is a design decision for a follow-up, not a documentation
  finding.
