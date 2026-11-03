# M3d: supervisor, protected job routes and stream lifecycle

Claude agreed all four decisions at5517c79 on 2026-11-03; the additions below
implement review comment6613486413 and hive3766/3784 for fresh exact-head review.
Codex implements; Claude independently reviews each exact head/pinned merge.
Base master **a401db054c7e1d2a784792b71184a37b0ac5f004**, reviewed slice3
#165 at89f205a. The missing-child operator escape in [#166](https://github.com/Decksx/cbz-automation-suite/pull/166)
was merged as55e0cd8bffb8b854d50bf712f997d32e88911e43; merge/source
tree e777d772ec84924a7a0c8f93273fbf405848951e was verified via GitHub API.
This is a plan, not an implementation, passing acceptance result or rollout.
The [M3 implementation plan](web_console_m3_implementation_plan.md),
[M3c contract](web_console_m3c_contract.md) and
[recovery protocol](web_console_m3c_recovery.md) remain authoritative;
#166 supersedes the last protocol's original missing-child limitation.

## Scope and concrete seams

M3d adds jobs/supervisor.py, bounded read/stop/recheck service interfaces,
protected route dispatch, server startup/shutdown wiring and HTTP/SSE/browser
acceptance. It composes the merged store, history, JobLogs and recovery helpers.
M4 still supplies production parameter schemas, trusted LaunchSpec/verifier,
outcome mapper and writing-tool stop policy. Production submission stays
unwired; missing adapters refuse. M6 owns external watcher discovery/control.
No raw argv, executable, cwd, environment, verifier import path, desired outcome
or stop mechanism can arrive from a browser. No live library fixture or rollout.

Current seams verified from a401db0:

- ConsoleApp authenticates/validates Host and state-change Origin/CSRF/body
  before routing. Its route table is exact-path only; Response currently has a
  bytes body and __call__ always adds Content-Length. Streaming/dynamic job IDs
  must be added behind those existing checks, not as a parallel WSGI app.
- server.build_server binds/wires M2 and publishes session.json; main closes it
  and the console lock on exit. M3d must place startup reconciliation and the
  service lifetime around those operations.
- JobStore.get/events are guarded internal reads, not redacted HTTP DTOs. No
  bounded summary-list/stop/acknowledge service exists yet. JobLogs.read_page
  provides hot/cold bounded pages; it has no stream manager.
- recovery.reconcile/recheck already sample fresh proof and own admission.
  Calling them while an outer admission lock is held would recurse on that
  byte lock. Snapshot a bounded sequence-ordered worklist, then call each
  helper separately; it refences its row. Never hold a DB transaction for
  native waits or a stream lifetime.
- Runner.start assumes the initial claimed version is2. Keep admitted rows
  immutable until claim or a conditional terminal expiry/refusal; do not add
  pre-claim progress/stop writes that silently invalidate that assumption.
  Tests must cover admitted-stop racing claim. If implementation requires any
  intermediate admitted mutation, return the actual claimed generation and
  remove hard-coded version2 uses before enabling that mutation.

## Supervisor ownership and detached birth

Construct one service per opened StateDir, with the exact parent home identity,
repository/interpreter context and trusted registry/stop dependencies. A missing
verifier disables submission at the direct service boundary as well as HTTP.
The scheduler never supplies a verifier that accepts browser-built commands.

Startup validates the existing pinned ledger and reconciles holding rows before
publishing a ready session/admission capability. Conditional expiry remains
JobStore.expire_admissions. Other rows are visited by admission_seq in bounded
pages; stale versions cause a fresh read on a later pass, not forced updates.
Exact live owners retain ownership. Errors/unavailable home refuse admission,
retain the same ledger and report unavailable; no temporary fallback DB.
Startup and the 30-second scheduler share one nonblocking pass-in-flight guard;
a slow startup pass cannot overlap a scheduled pass. A concurrent request skips
that pass and retries on the next tick. Do not
spawn/adopt/kill a tool during a pass or relaunch an existing admitted/claimed
job. Startup does not adopt native control handles from a surviving runner.

A trusted internal submit seam (future M4 caller, fixture-only initially) commits
one store admission, then spawns exactly one fixed runner command:
python -m comic_automation.web.jobs.runner <job_id> <nonce>. State-home path and
CBZ_CONSOLE_STATE_IDENTITY come from the server's admitted context, never a fresh
path stat. Use an allowlisted environment, trusted explicit executable/cwd,
no shell and no stdio pipe whose reader depends on the server staying alive.
Reuse the launcher detachment pattern only after native lifetime tests measure
it in supported OfficePC/CI contexts. D1 measures whether the server and its
launcher belong to a Job Object, including a kill-on-close job, and whether the
runner inherits that containment. A native fixture must close/kill the parent
job and prove the runner/tool lifetime or an explicit pre-launch refusal.
Creation flags alone are not proof. No uncontained tool/breakaway fallback.
The server retains no tool Job Object handle and never closes one on shutdown.

A crash/error around runner spawn leaves the committed row fenced by its claim
deadline; do not guess that a tool ran, declare success, or launch a replacement.
A bare Popen PID is diagnostic, never exact owner identity. The runner claims
and records its exact FILETIME/session itself. Any spawn-attempt event must not
silently change the pre-claim lifecycle generation; design that write explicitly.

Shutdown closes admission, signals the scheduler and streams, and bounded-joins
service work without stopping detached runners. No wait/terminate loop over live
runner Popen objects. If a storage worker is unsettled, retain its resources and
console guard until actual process exit; do not release the guard under a live
worker or report a clean drain. Tests must show an HTTP-server exit cannot kill
a fixture runner/tool, and a new server observes its exact continuing owner.

## Store mutations and operator intent

All direct service mutations enforce nonce/version, state/policy and attribution
under admission; routes cannot be the sole guard. No desired-state selector.

- Admitted stop: a conditional terminal pre-tool refusal is possible only while
  still admitted under admission exclusion; a racing runner claim either wins
  or is fenced, never permits both a child and a pre-tool refusal. Do not mutate
  an admitted row in place and leave a stale version2 assumption in the runner.
- Terminal stop: return existing state with no new signal/request. Uncertain
  uses recheck, not stop-as-recovery. Current wait-only/absent stop adapter returns
 409 NOT_STOPPABLE with a reason and no intent/file/native action.
- Future accepted stored policies can record idempotent intent then atomically
  publish the unique stop.request. Record actual delivery separately; failed
  storage is not delivery. A request is never proof of stopped outcome. M4/M6
  must supply interruption gates before any writing-tool termination is enabled.
- Recheck delegates to recovery.recheck with the authenticated session operator,
  echoed nonce/version and required acknowledgement note. Fresh inconclusive
  proof remains uncertain and is attributed/versioned; complete proof changes
  only to lost/unknown, including #166's wholly unrecorded child case. Automatic
  reconciliation and a valid late receipt cannot substitute for operator intent.
- Acknowledge is an attributed append-only note only. It cannot release admission,
  write stop.request or change lifecycle outcome. GET/SSE never perform actions
  on behalf of the operator.

## Protected routes, DTOs and errors

| Route | Proposed bounded contract |
| --- | --- |
| GET /api/jobs | Keyset before admission_seq, limit1-100/default50, newest-first summaries and next cursor |
| GET /api/jobs/{id} | One whitelisted summary plus events after event_seq, limit1-1000/default100 |
| GET /api/jobs/{id}/log | Existing decimal logical cursor and record limit1-1000, <=256KiB page |
| GET /api/jobs/{id}/stream | Authenticated SSE described below; global8 stream slots |
| POST /api/jobs/{id}/stop | Strict allowed fields/nonce/version; accepted policy or NOT_STOPPABLE |
| POST /api/jobs/{id}/recheck | Strict {nonce,state_version,note}; explicit fresh operator recheck |
| POST /api/jobs/{id}/acknowledge | Attributed bounded note; no lifecycle effect |

No production admission/run endpoint in M3d. Validate canonical ULIDs before
paths, reject duplicate/unknown query/body fields and Boolean/integer aliases,
and reuse M2's cookie plus X-Session-Key, Host/Origin/CSRF/body cap and error
headers. Dynamic matching happens only after those gates. HEAD must not reserve
a stream slot or start a generator. Missing jobs404, stale nonce/version409 STALE,
unsupported stop409 NOT_STOPPABLE, bad bounds400, unavailable state503 and stream
capacity429 are proposed codes using the same error envelope. Add status texts.
No traceback, credentials or private path is returned to the browser.

DTOs explicitly whitelist ID/nonce/version, safe tool/kind/effect labels,
readonly/dry-run, timestamps, state/outcome, bounded advisory progress, stop gate
and log status. Do not return raw command/seal/env JSON or arbitrary event
payloads. Creation FILETIMEs/native identity fields are exact strings when
shown. Operator notes/log text use textContent/JSON escaping, never HTML. Events
have bounded safe reason codes/checks; raw exception paths stay in private logs.
Cold history continues through pinned JobHistory/JobLogs reads and existing
member verification, not unverified tar extraction or a raw file route.

## SSE, progress and browser lifetime

Extend Response narrowly for an owned closeable byte iterable. Existing JSON
responses retain Content-Length/security headers. SSE has no invented length,
keeps M2 security headers/no-store and uses JSON data frames. Both auth and a
nonblocking global slot reservation complete before stream headers; all exit
paths release the slot in finally/close, including disconnect, error and shutdown.
Never hold admission, a DB transaction or an open segment handle across yield.

Use one bounded log reader per active job and fan its pages out to subscribers,
not one read_page poll per stream. Active cadence is at most one poll each250ms
per job; cold or idle jobs use about1s. Eight subscribers to one job must not
multiply admission-lock acquisitions: a timed witness counts acquisitions and
bounds them by job polls plus documented fixed work, including idle cadence.
Each subscriber has a bounded queue; a slow subscriber never blocks the shared
reader or another subscriber. If its queue fills, close its stream so it can
resume from its last delivered logical cursor; never silently skip bytes.
Release an unused reader promptly after its last subscriber leaves. Heartbeats
are15s and global stream slots remain8. Reserve request-thread capacity above
those slots for login and JSON routes; a real HTTP witness holds8 streams open
and proves a ninth client still obtains /api/jobs within a bounded deadline.
A slow client blocks only its bounded request writer, not the log reader/writer
or native runner. Request socket timeout and bounded page/frame size still
apply. Revalidate session expiry while waiting as well as on delivery, and close on expiry;
a server restart's new session cannot inherit an old authenticated stream.
Actual WSGI/disconnect tests must prove resource closure, not infer it.

Last-Event-ID is the last delivered logical source-byte offset, validated by the
existing codec. A reconnect inside a gap emits the gap before retained bytes.
State/progress use version/event identities for deduplication and do not move
the byte cursor; heartbeats have no ID. Missing/unknown tail is not EOF, and a
cursor beyond proven coverage is an error. No guessed lost suffix or final count.
The browser uses authenticated fetch streaming through an api.js helper, keeping
X-Session-Key in its header and never in a URL. Incremental frame parsing and
rendered tail are bounded; AbortController/disconnect releases the response.
Persist only per-job cursor/UI dedup state alongside the current tab session.
Extend the existing installed-Edge smoke flow; no browser download/new build.

**Decision proposed for agreement:** persist advisory progress/catch-up cursor
in an additive per-job/nonce observation table with its own generation, not by
updating jobs.state_version while the runner publishes/finalizes. Otherwise a
benign progress write can fence a valid live-owner receipt and create sticky
uncertainty. GET/SSE stay read-only; one bounded service consumer parses complete
records with logs.parse_progress and advances durable parse cursor, then readers
consume the snapshot. Restart/retention catch-up reports gaps/unknown evidence
truthfully and does not repeat progress. Review backward compatibility and
schema ownership before adding the table. The agreed compatibility argument
is additive schema-v1 storage, like job_archives: old runners never read the
new table, and observer writes never change lifecycle versions. Demonstrate
that argument with an old-runner compatibility witness before a migration
under active runners. The existing unused
progress_json column is not silently repurposed as a competing authority.

## Implementation slices and acceptance gates

| Slice | Deliverable | Required evidence before next slice |
| --- | --- | --- |
| D1 | Service lifetime, sequence-ordered reconciliation, trusted fixture spawn/context seam | Complete boundary-death matrix including measured parent kill-on-close containment; shared startup/scheduler pass guard; server restart leaves one live owner/tool; unclaimed expiry; spawn failure; home unavailable; bounded shutdown retains ownership |
| D2 | Bounded store reads/DTOs, protected JSON stop/recheck/acknowledge | Direct and HTTP gates, admitted-stop vs claim, stale nonce/version, #166 absent/present/wrong-session cases, no-stop gate creates no file/signal, redaction and all inherited M2 guards |
| D3 | Owned SSE iterable/stream manager, agreed progress persistence | Eight global slots plus ninth JSON client; one reader per job with bounded admission acquisitions and slow-subscriber queues; HEAD/auth refusal no slot, disconnect/session expiry/shutdown release, slow consumer independent of native/log activity, hot/cold/gap resumption and truthful unknown tail |
| D4 | Browser job/log integration and restart/cross-layer acceptance | Installed Edge authenticated fetch stream, disconnect/reconnect/reload exact cursor, safe text rendering, state/progress dedup, real server down during50MiB fixture output, no production submission route |

Use explicit fixture handshakes at command/admission/spawn/claim/native birth/
running/receipt boundaries. Named job/query failures, reused PIDs and wrong
sessions stay unknown; no process absence is guessed success. Mutation-check
every admission-release, auth, stop and stream-cap boundary. Each code candidate
gets focused checks plus a full Windows suite, exact-head CI, independent review
and pinned merge. Record the known OfficePC overflow WinError5 accurately against
an untouched baseline; partial reruns never replace full results. One file per
commit; checkpoint/push/progress before40min of each bounded relay run.

## Agreed decisions (5517c79; additions above await exact-head verification)

1. Keep M4/M6 production submission and interruption gates closed; D1 internal
   submission stays fixture-only, D2 unsupported stop is an explicit no-action
   refusal, and admitted cancellation is conditional pre-tool refusal.
2. Agree the additive progress observation generation, preventing benign server
   writes from racing runner lifecycle versions; settle schema compatibility
   before its implementation.
3. Agree authenticated fetch SSE, owned iterable/HEAD/session-expiry behavior,
   proposed bounds/error codes and no locks/handles held across yield.
4. Agree startup readiness before session publication and bounded shutdown's
   retained-console-guard rule when service work is unsettled.

No M3d implementation or live launch is authorized by this plan's existence;
D1 is authorized by Claude's agreement and hive3784 within standing project
scope; each code candidate still requires independent review and green CI.
