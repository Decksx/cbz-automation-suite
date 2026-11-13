# M3c slice 2B: claimed runner supervision

Built from master `36a1b82cb9dbdd3d890e55b95fec6d1159592de7`, after
[slice 2A](web_console_m3c_receipts.md), under the accepted
[M3c contract](web_console_m3c_contract.md). Codex owns implementation and
Claude owns independent exact-head review. No deployment is included.

## Launch authority

`Runner.start(store, job_id, nonce, registry=...)` is an internal interface.
It commits one live claim, reads the immutable command against the ledger,
reconstructs a trusted `LaunchSpec`, and prepares the private log writer. It
then repeats command binding and reconstruction/file checks immediately before
one contained launch. A pre-birth stop intent refuses. No relaunch follows a
failed native call or failed running commit.

The required registry callable is supplied in code; it cannot be selected by
command JSON, argv, import path or HTTP. It receives a copy of the command so
it cannot change the seal against which its result is checked. A `LaunchSpec`
contains explicit absolute executable/cwd, tuple argv, copied/frozen explicit
environment, trusted outcome mapper/id, and a stop policy. Its seal document
binds all those serializable fields plus three SHA256 file seals: interpreter,
source and registry. The interpreter seal must name the executable. Actual
input bytes are streamed through SHA256 before birth. M4 must supply and audit
the production registry, its source-set semantics, environment allowlist,
outcome identities and supported tools. The accepted source check-to-import
window remains; this is not a sandbox or proof about broker-created helpers.

The module entry point accepts **only job ID and nonce**. It resolves the
configured existing home and uses `inspect_state_dir`, including the existing
ledger identity pin, without taking the console lock or initializing a home.
There is deliberately no production registry yet: an eligible CLI claim is
refused before native birth. M3d still owns detached spawning and server routes.

The runner must occupy a dedicated host which launches no unrelated processes.
The native adapter's temporary inherited pipe/NUL handles must not overlap an
unrelated broad-inheritance launch in that host. Store and receipt code now
derive names through the same native `job_name` helper.

## Polling and persistence

`Runner.tick()` makes nonblocking root/job observations. `run()` calls it every
250 ms. Ledger operations, receipt publication/fsync, terminal commit and final
resource closure run through at most one I/O worker. Native supervision keeps
running when that worker stalls, including while the first running commit is
pending. No DB transaction encloses a native wait or log drain.

Root exit starts the 10-second descendant grace even when a descendant holds
the output pipe or has already closed it. At expiry the runner requests whole-
job termination, then allows 30 seconds to prove zero. A query failure is
unknown; a successful termination request is not zero. Uncertainty is sticky,
but the runner continues observations and retains ownership until actual
settlement. There is no age-based release or duplicate launch.

After root exit plus verified zero, the reader gets 5 seconds to reach EOF.
Cleanup has a separate 5-second combined budget. Expiry requests best-effort
thread I/O cancellation through `LogPump.join(0)` and records uncertainty;
it never treats a cancellation receipt as completion. A stalled log thread
keeps claim and handles alive even after the deadline. Native cancellation
may be ignored by a driver, as Microsoft's documentation explicitly states.

Persistence has a 5-second slow-I/O notice threshold, not a proof-invalidating
deadline. Passing it changes the local phase to waiting_for_running/publish/
refresh, while retaining the worker/claim/handles and native supervision.
Latency alone neither cancels the operation nor records durable uncertainty.
When it eventually succeeds, finalization still requires all fresh proof.
Actual I/O errors, unknown native observations and failed log settlement remain
sticky uncertainty; a late successful receipt cannot clear any of those.
After publication, the
terminal store operation repeats 2A's native/log/receipt proof. An in-flight
terminal DB commit cannot be safely cancelled: the runner retains ownership and
continues responding until its outcome is known, without racing it with another
mutation or declaring cancellation. The terminal worker repeats the final
native proof itself; the polling loop does not race that commit with redundant
native queries. Resource release likewise waits for its
worker's actual completion. These are bounded **supervision/cleanup attempts**,
not a promise that storage or process lifetime always completes within a bound.

## Failure and stop semantics

Only positively pre-birth failures become refused. Native name collisions and
possible-birth errors become uncertain. The native adapter closes its owned
failure-path handles; a returned possible-birth error is not proof that all
processes are gone. Durable uncertainty retains admission for slice 3/M3d
recovery. A post-birth running/event failure never becomes a refusal.

After any log error, the live writing root is left running under its stored
policy. The reader continues draining/offering (and dropping when the bounded
queue is full) until actual EOF or final cleanup after verified job zero. It
does not wait for writer recovery. The runner selects `sticky_failure=True`
on its log pump: even recovered I/O cannot publish a complete EOF index after
an error escapes the disk writer. The last durable coverage remains truthful,
with `unknown_tail` and no ordinary terminal receipt. Standalone M3b pumps retain their previous
retry/coverage behavior by default.

The disk writer handles a narrowly bounded publication retry before that sticky
boundary: only a `PermissionError` with `winerror == 5` from replacing its exact
`index.tmp` with `index.json`, with both paths present on the error, is retried.
It makes at most five attempts, separated by four 20 ms waits (80 ms of waiting
in total; filesystem calls can take longer). Every attempt revalidates the
candidate and claim/path boundary, rewrites and fsyncs the temporary index, and
advances the in-memory durable watermark only after a successful atomic replace.
This applies to initial publication, appended coverage, and EOF publication.
Recovered refusals do not reach the pump. Exhaustion, other replacement errors,
and write/fsync/validation failures retain the existing error and uncertainty
policy. This does not add any authority to stop or finalize a writing job.
An explicit, durable dropped/retention gap keeps its existing coverage meaning;
proven pipe EOF can coexist with such a gap. Recovery does not erase the gap or
turn an unrecorded tail into proven coverage.

Only `wait` stop policy is accepted in this slice. A stop intent observed while
the root runs records uncertainty and retains admission; it is **not** authority
to terminate the root. M4 must implement approved writing-tool interruption
gates and actual delivery evidence before stopped outcomes can be enabled.
The root-exit descendant cleanup deadline remains the accepted containment
policy. Log failure, keyboard interruption and receipt failure never provide
an additional writing-tool kill gate. Owner death still has native
kill-on-job-close semantics.

Before releasing a locally successful/failed owner, the runner requires settled
native/log/I/O activity and a committed terminal transition. An uncertain owner
releases only its settled local resources after uncertainty is durably recorded;
the row still blocks admission. If storage is unavailable, the existing row
keeps holding admission and the live owner retains its unsettled resources.
No fallback directory or DB exists. Late valid receipts do not clear uncertain.
Orphan `exit.json.tmp` evidence must be handled explicitly by slice 3, never
automatically erased or used to relaunch.

Slice 3 dead-owner observations/import, M3d reconciliation/operator recheck/
routes, and M4 production adapters/stop gates remain separate. Tests and guard
bypasses are recorded in the dated development log and exact-head PR.

Native references checked 2026-11-03:
[TerminateJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-terminatejobobject),
[cancellation limits](https://learn.microsoft.com/en-us/windows/win32/fileio/canceling-pending-i-o-operations).
