# M3d D1 supervisor protocol and current evidence

Codex implementation from master55e0cd8, 2026-11-03. Contract#167 agreed at
5517c79; load/lifetime additions atbc9a7e7 await exact-head verification.
This slice has no production registry, HTTP submission/stop routes or rollout.

## Ownership and reconciliation

`Supervisor` requires an opened StateDir with a live console lock and uses its
already-pinned home identity. `start()` runs recovery before readiness; the
server binds first but publishes its new session only after successful startup.
Startup and the30s scheduler share one nonblocking pass guard. A concurrent
pass skips instead of nesting admission locks. Each pass expires unclaimed
admissions, snapshots a maximum admission sequence, and visits claimed/running
rows in keyset pages of100. A page transaction closes before each recovery
call; recovery takes its own fresh admission exclusion/native evidence and
row/nonce/version fences. Later admissions wait for a later pass. Durable
uncertain rows remain sticky and are never automatically rechecked/cleared.

Only RECOVERY_FENCED and RECOVERY_STATE_INELIGIBLE are tolerated live-owner
races. Other failures disable service admission and remain private diagnostics;
a later actually successful pass restores readiness. No fallback database,
replacement launch, native adoption or termination happens during recovery.

`submit()` is an internal trusted seam. It requires readiness, live console
ownership, a verifier and measured absence of parent Job Object membership.
The store validates raw-command rejection/classification and commits admission
before exactly one fixed runner spawn attempt. The command is this Python
interpreter, `-E -s -m comic_automation.web.jobs.runner`, job ID and nonce, with
trusted repository cwd, an allowlisted system/temp environment, original pinned
CBZ_CONSOLE_STATE_IDENTITY and CBZ_WEB_CONSOLE_HOME. No shell, inherited pipe,
registry import selector or browser-selected executable/environment is accepted.
The default verifier is absent and production reconstruction remains M4; tests
alone substitute a disposable runner entry point. Existing runner main refuses
before native tool birth when that production registry is absent.

A spawn error raises SpawnUnconfirmed carrying job_id. Its committed admitted
row is retained for claim/deadline recovery; no retry or outcome is guessed,
including errors after an unconfirmed native birth. Spawn does not mutate the
admitted lifecycle generation, preserving Runner.start's version2 assumption.

## Parent containment and shutdown

DETACHED_PROCESS/CREATE_NEW_PROCESS_GROUP/CREATE_NO_WINDOW are console/window
flags, not proof of independence from an inherited Job Object. The parent is
measured via IsProcessInJob immediately before admission; unknown/non-Windows
refuses. Any membership refuses conservatively, including a seemingly benign
inner job, because an unqueried outer ancestor may have kill-on-close. Production
never requests BREAKAWAY_FROM_JOB and never launches an uncontained tool fallback.
There remains the ordinary externally induced check-to-birth window if another
process assigns this server to a job after the membership query; this slice
provides no guarantee against such outside reassignment.

`close(timeout=2)` closes admission and signals the scheduler, then waits only
the bounded budget for actual service operations/scheduler exit. It never waits
for/stops a detached runner. A false result requires the caller to retain the
console guard and call wait_closed() before releasing it. Server shutdown logs
that unsettled state and retains its process/guard until real work exits. An
already-entered submission may finish while shutdown waits; it is never cancelled
mid-storage. Startup is counted through scheduler publication so close cannot
release ownership in that gap. Session-publication failure closes the service
and listener; startup failure publishes no new readiness capability.

## Native measurement and outstanding acceptance

On this OfficePC execution host, IsProcessInJob=true and querying the current
job reports limit_flags0x2800: kill-on-close plus explicit BREAKAWAY_OK. A native
disposable contained-parent fixture measures membership=true, refuses submission
before any jobs directory/admission, then exits cleanly. A test-only attempt to
create an uncontained fixture server using the measured BREAKAWAY_OK still
measures membership=true in the child: an outer job remains. The production
refusal is correct in that measured context. No parent policy was changed.

The two positive lifetime witnesses (clean/abrupt server death, exact runner and
child surviving, new server observing the same live owner, natural completion)
are implemented but skip when no genuinely uncontained fixture context can be
measured. They must run successfully from a supported uncontained desktop or CI
context before D1 is considered complete. This is an acceptance gap, not a
passing native lifetime result. The test-only breakaway flag is never a
production fallback and cannot bypass the child's membership assertion.

D2 protected DTO/routes/operator intent, D3 shared-reader SSE/progress and D4
browser/restart acceptance remain separate. Exact focused/full/mutation/CI
results and the next owner are recorded on the PR; this document does not claim
unrun checks or a deployment. One file per commit, bounded relay checkpoints.
