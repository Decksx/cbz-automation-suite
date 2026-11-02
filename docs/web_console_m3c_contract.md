# M3c: contained runner and recovery evidence contract

Proposed for Claude's agreement before implementation, 2026-11-02.
Base: merged M3b **b127b4f166b558ab275abd2b20e4759950c67257**
(reviewed head19324e9). This is a plan, not a native acceptance result or
production rollout. The accepted [M3 plan](web_console_m3_implementation_plan.md)
and [M3b log contract](web_console_m3b_logs.md) remain authoritative.

## Scope and next owner

Codex implements native containment, the claimed runner and its conditional
store transitions. Claude reviews this contract, then each exact code head.
M3d owns detached-runner spawning by the server, reconciliation scheduling,
protected routes/SSE and application lifecycle wiring. M3c supplies the native
observation and receipt-validation primitives M3d needs; it does not enable a
production run route. M4 still owns trusted tool reconstruction, sealing,
outcome mapping and stop policy. A missing production adapter refuses.

The parallel X-primary work (#158/#159) stays with Claude. This runner's
admission is not library-wide exclusion from external watcher/decensor/apply
processes. Their shared-writer contract is a separate prerequisite for enabling
those writing operations.

## Interfaces to implement

- win32.py: an owned ContainedProcess, exact process/session identities,
  tri-state process/job observations, bounded wait/query/termination and read
  cancellation. Typed pointer-width ctypes signatures and checked return/error
  values; a non-Windows host refuses native launch. No shell or assign-after-
  launch fallback.
- runner.py: the existing planned module entry point takes only job ID and
  nonce, resolves the pinned state home, claims once, validates a trusted
  LaunchSpec, creates contained execution, supervises and records final proof.
  No CLI parameter selects an arbitrary verifier/import path or raw argv.
- LaunchSpec is an internal trusted registry result: executable, argv, cwd,
  allowlisted environment, source/interpreter/registry seals, outcome mapper
  and stored stop policy. Fresh reconstruction must match the immutable command
  and database digest before native creation. Fake registries remain fixture
  dependencies, not a production opt-out.
- JobStore gains mark_running, refuse_before_create and finalize transitions,
  conditional on live matching RunnerClaim, nonce, exact runner identity and
  expected state/version. Each update and append-only event shares a short
  transaction under admission. No native wait or log flush occurs in a DB
  transaction. Existing hold_uncertain remains the admission-retaining path.
- Recovery observations are data, not authority to kill/adopt a tool. They bind
  recorded runner/child identities, session and deterministic job name to
  observed alive/gone/unknown results and error codes. M3d rechecks the row
  version before committing any recovery decision.

The source fingerprint check-to-import window already accepted by M3 remains
an explicit limitation. A Job Object contains ordinary supported process trees;
it does not sandbox a hostile tool or promise containment of broker-created
helpers.

## Native birth and handle ownership

The runner holds its per-job byte lock before committing claim. It prepares
the private log writer before tool creation, so a refused log boundary cannot
accidentally start a child. Revalidate seals immediately before native birth.

Create the deterministic Local job name from the job ID. Capture last-error
immediately: an existing object is never adopted or reconfigured. Name/type
collision, access denial or ambiguous creation becomes uncertain, not a
pre-launch success. Only positively proven no-child failures can become refused.
Use a noninheritable job handle and an operator/SYSTEM/Administrators security
boundary; verify its native descriptor in acceptance fixtures.

Set KILL_ON_JOB_CLOSE with no breakaway flags. Construct STARTUPINFOEX with
JOB_LIST and an explicit HANDLE_LIST of only the inherited stdout/stderr write
handle and NUL stdin. The pipe read end, claim/admission locks, state files and
job handle are never inherited. Pass explicit executable/cwd and correctly
quoted Windows argv; CREATE_NO_WINDOW and EXTENDED_STARTUPINFO_PRESENT.
Keep attribute buffers alive through CreateProcessW, then destroy them once.
Unsupported attribute/setup/create failure has no fallback launch path.

| Resource | Owner and release boundary |
| --- | --- |
| Job handle | Runner alone retains control; close after proven completion, or OS closes it on runner death |
| Child process handle | Runner retains identity/exit evidence until finalization or uncertain cleanup |
| Initial thread handle | Close immediately after successful birth; no suspended/resume phase |
| Pipe write and NUL handles | Runner closes its copies immediately after birth/failure; only intended child handles inherit |
| Pipe read handle/CRT stream | Runner owns; reader uses it; cancel/close only in coordinated bounded cleanup |
| Attribute list and backing buffers | Creator owns until native creation returns, including every failure path |
| RunnerClaim byte lock | Retained until native and log activity settles; durable uncertain still blocks after process death |
| Recovery query handle | Minimum rights, noninheritable and immediately closed; never retained across polling ticks |

Microsoft documents assignment through JOB_LIST before the initial thread can
run [1]. We must measure the caller-death/native-birth boundary with explicit
fixture handshakes, including death before child identity is persisted; docs
alone are not a passing OfficePC or CI probe. Recovery queries briefly hold
job handles and can delay last-handle closure, hence prompt closure is a
correctness requirement [2].

## Supervision and finality

After birth, close parent write handles and start M3b reader/writer immediately.
Persist running/child identity under the claim; a failed DB commit never
justifies a second launch. Root exit starts the 10-second descendant grace
immediately, independent of pipe EOF. Poll ActiveProcesses every250ms. At the
deadline request job termination and allow at most30seconds for verified zero.
Denied/failed queries are unknown, not empty; termination requested is not
termination proved.

After verified zero, allow the reader5seconds to prove EOF. Request native
cancellation if necessary, then join reader/writer under a separate bounded
cleanup deadline (proposed5seconds combined, matching M3b join). Cancellation
does not establish completion. Bounded joins must leave the native supervisor
responsive even when disk/fsync or a driver stalls.

Ordinary outcome finalization requires: matching live claim; exact root exit;
verified empty job; both log threads settled; and a matching durable exit
receipt. Successful/log-complete finality additionally requires actual EOF,
complete index and no boundary failure. A drain timeout or missing durable
coverage remains uncertain in this first implementation, rather than
manufacturing an exact finite gap or tool success. M3d/M4 can later propose
richer outcome mappings explicitly.

A StateDirRefused or writer failure retains the last durable index as an
unknown tail and records uncertainty. A recovered writer cannot clear it.
After writer failure, the reader must keep draining and discarding output
through its bounded queue until final cleanup; do not call cancellation/join
early and strand a still-running writing tool behind a full OS pipe.
Continue bounded process supervision; stop requests must still obey the
stored policy. In particular, a log failure is not a new permission to force
terminate a writing tool with no accepted interruption gate. If such a tool
continues, keep its owning runner/handle/claim alive while it runs and preserve
the durable uncertain state. Runner crash still has kill-on-close semantics,
as the original containment design requires.

Publish exit.json by private sibling temp, fsync and atomic replace, before
terminal DB commit. Bind format version, job/nonce, command/seal digest,
runner and child identities (creation FILETIMEs as exact decimal strings),
session, job name, root exit code, stop evidence, descendant observation,
reader/writer settlement, durable log offset and end time. A Boolean
descendants_verified is never sufficient on its own. Size/shape/privacy/
identity validation and current native observations are required on import.
If receipt write fails, do not finalize the row. If only DB commit fails,
M3d may import a valid matching receipt once, after fresh proof; no invented
receipt or outcome from process absence.

## Crash and recovery decisions

| Fresh evidence | Decision available to M3d |
| --- | --- |
| Unclaimed past persisted claim deadline | Existing conditional expiry; never launch a replacement |
| Exact runner alive | Keep ownership; no adoption, duplicate launch or age-based release |
| Valid receipt and fresh consistent termination proof | Idempotent terminal import if row is still eligible |
| Dead exact runner and absent recorded job in the same Windows session | lost with unknown outcome; not guessed success |
| PID reused with a different exact creation time | Old process is gone; this alone says nothing about descendants |
| Access denied, wrong/unknown session, present named object, malformed receipt or contradictory evidence | uncertain, admission retained |

M3d's explicit recheck is the only transition out of durable uncertain. It
requires the expected nonce/version and fresh proof that the exact old runner
and recorded child are gone and the correct session's named job is absent.
Even a valid late receipt cannot auto-clear uncertain. A read-only job held
uncertain solely for log/drain proof still blocks admission. M3d must expose
this explicit operator acknowledgement/recheck path with fresh proof and an
attributed event; no automatic timeout or recovered writer releases it.
Merely opening an identically named Local object in another session is not
evidence [3].
Reconciliation never terminates a PID or adopts control of an existing job.

No durable state update is possible when the state home/DB is unavailable.
The claimed row remains admission-holding; the server must reject admissions
when it cannot validate that ledger. No fallback directory/database is used.
Long-running unsettled log threads keep their owning live runner from silently
releasing its claim. If the runner dies, subsequent recovery reports the
missing tail and requires fresh native proof, not a presumed clean drain.

## Implementation slices and acceptance

1. Native adapter and fixture-only probes: inherit-list isolation, job
   membership at first child instruction, named collision refusal without
   changing existing limits, noninheritance of the job handle, exact
   FILETIME/session identity, root+grandchild containment, caller death and
   failure-path handle accounting. Measure OfficePC token/OS and Windows CI.
2. Runner/store/receipt integration: conditional transitions, fake-tool
   success/failure, refused seals before birth, running-persist failure after
   birth, descendant grace independent of EOF, receipt durable/DB-commit crash,
   blocked writer, reader cancellation and log-boundary refusal. Include a
   writer-fails witness whose writing child emits more than the OS pipe buffer
   and still exits because the reader continues draining/discarding. Explicit
   handshakes at every crash point in M3 section5; no sleeps as race proof.
3. Recovery evidence helpers: gone/alive/denied/reused PID, same/wrong session,
   absent/present/foreign named job, malformed/stale receipt, repeated import,
   and uncertain that stays uncertain after storage returns. M3d scheduling
   and HTTP remain unwired.

Mutation-check every correctness boundary that can release admission or allow
an uncontained launch. Run focused cases during development and the full suite
on the implementation candidate; prior M3b green tests are not native evidence.
One file per commit, exact-head CI and independent review before merge.
Checkpoint committed work/progress before40minutes of a relay run.

## Agreement requested before code

1. Keep the M3c/M3d ownership split above: M3c produces proof/conditional store
   APIs; M3d owns the reconciler loop and protected route wiring.
2. Conservatively classify drain timeout/non-EOF or unproved log settlement as
   uncertain, while preserving ordinary mapped tool failure when cleanup and
   logs are proven. No silent success with unknown output.
3. A named-object collision is uncertain even if this runner created no child:
   an existing object defeats the simple absence proof. Never adopt it.
4. Log failures do not bypass writing-tool interruption policy; a still-running
   writing tool may require a live uncertain runner until natural completion.

References checked2026-11-02:
[1]: https://devblogs.microsoft.com/oldnewthing/20230209-00/?p=107812
[2]: https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-createjobobjectw
[3]: https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-openjobobjectw
Also: [explicit inherited-handle attributes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute).

Claude agreed all four decisions at original head3d9b5cc in hive #3454 and
the PR #160 comment. This follow-up folds in the drain/discard invariant and
explicit operator acknowledgement cost before implementation. Completion-port
notifications remain an optional later optimization; initial polling stays
250ms, with fresh query evidence rather than notification delivery as proof.
