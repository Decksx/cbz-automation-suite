# M3c slice 3: recovery evidence and conditional reconciliation

Base: master4f9893a (slice2B merged91cae2c, TPB correction merged4f9893a).
Codex implements; Claude reviews exact heads and pinned-merges after CI.
This supplies the accepted #160 contract's recovery primitives. M3d still owns
server scheduling, authenticated operator routes, admission-context propagation
and lifecycle wiring. M4 supplies production registry/outcome/stop adapters.
There is no new launch route or production rollout.

## Interfaces and authority

`recovery.observe(row)` samples exact runner/recorded-child process identities
and the deterministic Local job in its recorded Windows session. Reports bind
job, nonce, row version, expected identities/name/session, native observations
and error codes. Reused PID means only the old exact identity is gone. A denied
query or another session's namespace is unknown; a present empty job remains
present. Query handles close immediately. Reports are diagnostics, not reusable
proof, and are never accepted as input to a state-changing API.

`recovery.reconcile(store, job_id, nonce, version, outcome_mapper=...)` owns the
admission byte lock, reads the row, samples fresh evidence and rechecks the full
row inside a short transaction before any transition/event. Native queries,
command/receipt/index/segment reads occur outside the write transaction. No
native waits, log flush or fsync occurs there. It never kills/adopts a job or
launches another tool. An exact live runner retains ownership. Unclaimed expiry
remains the existing store API. Scheduling and sequence-order iteration are M3d.

A dead-owner claimed row with no recorded child can become lost only after the
exact runner is gone and the deterministic same-session job is absent. This
covers death before the child identity commit, relying on measured containment
and kill-on-close. Running rows additionally require the recorded child gone
and the stored name equal to the deterministic name. Absence without a valid
published receipt means lost with unknown outcome, never invented success.

A running row can import a receipt only after fresh runner+child gone and job
absent. The receipt must be bounded, canonical, private, identity-pinned, match
the immutable command/seal and exact identities, have complete matching log
index/coverage and existing correctly sized segments, and agree with a trusted
internal outcome mapper. Canonical bytes reject duplicate/extra keys and
Boolean/integer aliases. The end time cannot be in the future. Fresh native
absence is sampled again after all file reads. The index binds logical coverage
and its byte hash; segment payloads have no independent content hash in M3b.

The terminal receipt import and attributed event commit atomically. Repeating
the same successful import returns the prior row without a duplicate event.
A bad/stale receipt, missing durable bytes, contradictory native observation or
foreign named job records uncertainty and retains admission. Unavailable state
or DB raises; the existing row still holds admission, with no alternate ledger.

## Uncertainty, acknowledgement and orphan files

Automatic reconciliation never clears durable uncertain, even with a valid late
receipt or recovered disk. `recovery.recheck(..., actor=..., note=...)` is the
internal primitive for M3d's explicit authenticated operator action. It requires
expected nonce/version, a nonempty attributed acknowledgement, fresh exact
runner/recorded-child gone and same-session job absent. Complete proof changes
uncertain only to lost/unknown, never success. An inconclusive attempt retains
uncertainty and records its evidence/note with a new version to fence replay.
Log-only uncertainty has the same explicit acknowledgement requirement.

An uncertain row with no recorded child can be explicitly rechecked after the
exact runner is gone and the deterministic named job is absent in that runner's
Windows session. JOB_LIST at birth, no breakaway and kill-on-close mean there
cannot be a surviving contained member, even if death preceded child-identity
persistence. This is the same absence proof accepted for a claimed pre-child
crash; it changes only the operator-attributed recheck-to-lost path. Automatic
reconciliation still leaves uncertain untouched and receipt import still needs
a recorded child. Both child fields must be absent: a partial identity, present
job (even empty), wrong/unknown session, denied query or foreign stored name
retains uncertainty. There is no desired-outcome or broad proof override.

An existing exit.json.tmp produces ORPHAN_EXIT_RECEIPT_TEMP during publication
or dead-owner recovery. The file is preserved, never promoted, overwritten,
erased or used as relaunch authority. Even a complete fsynced temp is not the
published receipt. A final receipt plus an orphan temp remains ambiguous.

## Runner follow-ups

Log-pump startup failure after native birth has the specific reason
LOG_PUMP_START_FAILED_AFTER_BIRTH:<exception>. It retains supervision and still
attempts to persist the exact child identity before recording uncertainty.
Failure to start a reader is not a new writing-tool termination permission.

Native polling stays at250ms by default; ledger refresh is independently1s
(internal maximum2s). Refresh latency does not age-release admission. Actual
errors retain sticky uncertainty. M3d must account for this stop-intent sampling
latency; delivery/interruption gates remain M4.

The runner CLI still accepts only job ID and nonce. It now requires the trusted
parent's CBZ_CONSOLE_STATE_IDENTITY environment value as canonical decimal
device:file-id and passes that expected identity to inspection. It never derives
expected identity by statting the current path. Missing/malformed context refuses
before any ledger inspection; M3d must propagate the server's admitted identity.
This context is an identity transport, not an initializer or new identity pin.

## Verification

Tests combine typed injected native outcomes for ledger/receipt guards with
actual Windows fixture-owner death at explicit claim, native-birth,
before-receipt, fsynced-temp and durable-receipt handshakes. Only known fixture
Popen owners are killed. The receipt crash imports once, earlier crashes do not
guess success, and temp-only publication retains uncertainty/evidence.
The dated development log records exact checks and selected mutation results.
