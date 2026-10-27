# Web console M3 implementation plan

Status: proposed contract for Claude's review, 2026-10-27. Codex implements
after this plan is approved and M2 lands; Claude owns independent review.
This document schedules work. It does not report an implementation, passing
acceptance tests, production admission, or a rollout.

Baseline: master `2a95b25080da2bd743acd1a902646c2132e8eef6`, including M1b.
The accepted [v1 design](web_console_v1_design.md), especially sections 5.1-5.5,
8.1, 9, 10 and 12, remains authoritative. Its blob at this base is
`df98a7eeb3bbdbc07f9852cb89f67a84fcf9e92a`. The
[M1b stop-file protocol](watcher_graceful_stop.md) is already merged. This plan
was requested in hive #3227; the M2 package is still Claude's workstream.

## 1. Deliverable and boundaries

M3 supplies durable console jobs, serialized admission, a fenced runner claim,
atomic Windows containment, bounded log streaming, restart reconciliation and
gated stop/recheck operations. Jobs survive an HTTP (Hypertext Transfer Protocol) server restart. A server
restart never launches a replacement tool for an existing claimed job.

The console state database (DB) is separate from the comic operational job queue under
`comic_automation/jobs/`. M3 does not enqueue perceptual work, migrate
`comics.db`, expose canonical apply, replace the Tk launcher, change the
watcher, or enable a writing tool's interruption gate. All planned execution fixtures
use disposable roots and fake tools; no live library or watcher is exercised.

M4 still owns production tool schemas, parameter validation, command building,
preview/run routes and per-tool policy. M6 owns watcher discovery/start/stop
routes, external-watcher detection and its forced-interruption acceptance gate.
M3 must provide these application programming interfaces (APIs) without exposing a raw-command route.

## 2. M2 and M4 integration seams

Concrete M2 names are provisional until its exact-head PR is reviewed. Bind to
the public interfaces that land, rather than editing its uncommitted package.

| Provider | Required input or service | M3 responsibility |
|---|---|---|
| M2 config/state guard | Effective state home, physical identity, repository/interpreter context; refusal when unavailable or replaced | Use the same home and identity on server and runner; no second DB or temporary fallback |
| M2 app/security | Authenticated request context, operator/session attribution, JSON (JavaScript Object Notation) validation/errors, redaction and route registration | Register only protected job routes; reuse Host/Origin and Cross-Site Request Forgery (CSRF) checks and response headers |
| M2 server lifecycle | Startup hook and bounded shutdown hook | Reconcile before admitting work; run the 30-second reconciler; disconnect streams without stopping detached runners |
| M4 tool registry | Trusted, server-built launch specification and fresh seal verifier | Recheck the seal before launch; refuse if the adapter or tool policy is missing |
| M6 watcher policy | External-watcher observation and verified stop capabilities | Include observation in compatibility decisions; keep writing submissions disabled when the required observation is unavailable |

The runner entry point remains `python -m comic_automation.web.jobs.runner
<job_id> <nonce>`. It resolves state through the same M2 configuration contract,
with temporary state only through M2's explicit test facility. No browser
supplies a filesystem location, interpreter, environment, argv, stop mode or
claim identity. Job IDs are canonical [ULIDs][ulid] (Universally Unique Lexicographically Sortable Identifiers),
validated before constructing any path. The 128-bit random nonce fences a
claim; it does not replace session authentication.

**Proposed pre-M4 gate:** install the M3 services and read/stop/recheck routes,
but leave production submission unwired until M4 supplies the trusted adapter.
Test fixtures inject a fixed fake-tool registry. A missing verifier refuses;
there is no production verifier that returns success unconditionally. M3 can
implement hash/seal plumbing, but only M4 can reconstruct a production command
from schema-validated parameters. This dependency needs contract approval.

## 3. Store, admission and durable transitions

`web/jobs/store.py` owns the console tables, explicit transactions and
compare-and-swap updates. Use M2's existing database/schema-version mechanism;
do not introduce a competing `user_version` or migration owner. Schema upgrades
must be additive and runner-compatible, or refuse while old runners remain.

Keep the design's job identity, sealed request, runner/tool identity, timestamps,
outcome, progress and stop fields. Add `state_version` for every row change,
the persisted claim deadline, and the runner's Windows session ID so a later
process can prove it queried the correct `Local` namespace. Store creation
times as exact Windows FILETIME integers (64-bit process-creation timestamps), serialized as strings where JSON
number precision would lose bits. A PID (process identifier) alone is never
identity evidence. Use an increasing admission sequence for reconciliation.

State changes and their `job_events` append happen in the same transaction.
Triggers reject event updates/deletes. Runner writes require matching job ID,
nonce, runner PID and creation time, and an allowed source state; zero matched
rows cannot be treated as success. Server actions also use the expected
nonce/version and append attribution. Ordinary progress or log errors cannot
manufacture a terminal lifecycle state.

| Transition | Evidence required | Holds admission afterward? |
|---|---|---|
| New -> `admitted` | Durable sealed command, compatible snapshot, committed row/nonce | yes |
| `admitted` -> `claimed` | Runner lock, correct nonce, unexpired deadline, exactly one committed conditional update | yes |
| `claimed` -> `running` | Atomic job-contained birth; child identity committed under the claim | yes |
| `admitted` -> `admission_expired` | Deadline passed and the row still unclaimed in the transaction | no |
| Pre-tool -> `refused` | Positive evidence that no tool was created, such as seal/native-create refusal | no |
| Claimed/running -> `succeeded`, `failed` or `stopped` | Verified empty job, bounded drain handled, valid matching exit evidence | no |
| Claimed/running -> `lost` | Matching runner dead and the correct job namespace proves object absent; outcome unknown | no |
| Active -> `uncertain` | Identity, containment, artifact or termination evidence inconclusive | yes |
| `uncertain` -> `lost` | Explicit recheck with matching nonce/version and fresh termination proof | no |

An `admitted` row can exist before a runner is spawned. `refused` means no
*tool* ran; a runner may already have committed its claim. A tool that actually
starts and returns an ordinary error is finalized as `failed`, with its outcome
mapping, after descendant verification. Do not rewrite such a failure into a
pre-launch refusal. M4 owns mappings such as planner success-with-holds.

Admission order:

1. Prepare a unique private `jobs/<job_id>/command.json` using temp/write,
   flush/fsync and atomic replace; read back and match its digest. No existing
   job directory is reused. An artifact without an admitted DB row cannot run.
2. Revalidate the state root; acquire `locks/admission.lock` with a one-byte
   nonblocking operating-system (OS) lock and bounded retry. Seek to the same byte in all paths.
3. `BEGIN IMMEDIATE`; examine every admission-holding row and the watcher
   observation, enforce design 5.4, insert `admitted` and its event, commit.
4. Release the lock, then spawn the detached runner with no console window.
   The server does not capture the tool pipe or own its containment handle.
5. Report the durable job ID. A known spawn failure may conditionally refuse
   an unclaimed row; an ambiguous spawn result is retained for claim/expiry
   reconciliation, never retried by launching another tool.

The runner takes its private OS lock and commits the claim before any tool
creation. Expiry and claim race through conditional DB transactions: exactly
one wins. Check the 60-second deadline in the claim itself, not only on a later
reconciliation tick. Inject time in tests; a contradictory/rolled-back clock
must not be used as positive evidence to release admission. No active claimed
job expires merely because a heartbeat, server session or HTTP request ended.

Compatibility comes from design 5.4: one task at a time; a watcher may coexist
with a read-only task; writing tasks exclude both other tasks and watchers.
Every `uncertain` row blocks admission. This is console admission plus detected
watchers, not shared exclusion against arbitrary external CLI writers. Until
M6 provides its observation, reject production writing submissions rather than
assuming that no external watcher exists.

## 4. Native containment and runner finalization

Add a narrow `web/jobs/win32.py` adapter with typed pointer-width ctypes
bindings. All tool creation uses it; the server's detached runner spawn stays
in the supervisor.
Its ownership record lists every process, thread, pipe, NUL, attribute-list
and Job Object handle, with deterministic cleanup on each failure path.

1. Revalidate state identity, immutable command versus DB seal, interpreter
   content hash, argv reconstruction, source fingerprint and registry hash.
   Missing adapters and changes refuse before native creation. The accepted
   check-to-import source-change window remains documented, not claimed closed.
2. Create `Local\cbz-console-<job_id>` with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. Detect an
   already-existing object/name collision before modifying its limits; close
   that handle and retain admission as uncertain, without adopting the object.
   Neither breakaway flag is set.
3. Create the pipe and NUL stdin. The job handle is noninheritable. Build
   `STARTUPINFOEX` with both `JOB_LIST` and the explicit `HANDLE_LIST` containing
   only the child's write handle and NUL. Use explicit executable/cwd and
   Windows argv quoting, never a shell. Any unsupported/failed attribute or
   creation call refuses; there is no suspended-start or uncontained fallback.
4. Close the runner's copy of the write end immediately. Start the reader/writer
   pipeline, retain the job/process handles, and commit `running` under the claim.
   Failure to persist after creation must not release admission or launch again.
5. On root exit, start the **10-second descendant grace immediately**, while
   the pipe remains independently drained. Poll `ActiveProcesses` every 250 ms.
   At the deadline terminate the job; verify zero, with the further **30-second**
   bound. Inconclusive/denied queries become `uncertain`, never success.
6. With verified zero members, finish the read drain within **5 seconds**; if
   it cannot finish, close/cancel the read operation and record the drain gap
   to the extent durable evidence permits. Writer cleanup is also bounded as
   described in section 6. Do not wait for pipe end of file (EOF) before starting step 5.
7. Write `exit.json` atomically, containing the claim and child identities,
   exit code, stop facts, end time and descendant verification; then commit
   the terminal transition/event under the claim. If only the DB commit fails,
   reconciliation can import the matching receipt. Neither write succeeding
   alone proves arbitrary storage failure was harmless.

The runner owns the only *retained* job handle. Reconciliation query handles
are opened for minimum rights and closed immediately; retaining one could delay
kill-on-last-close. The server shutting down cannot close a runner-owned handle.
Native tests must measure OfficePC's standard-user token/OS support and Windows
continuous integration (CI), rather than infer success from M1's different mutex measurement.

Containment applies to the supported tools' ordinary process trees. A Job
Object is not a sandbox for untrusted programs or a guarantee about helpers
created through an out-of-process broker. Audit such tool behavior before
enabling it; do not claim that disabling breakaway contains every possible
Windows launch mechanism. See the [Windows Job Objects reference][jobs].

## 5. Reconciliation and explicit recheck

At server startup, and every 30 seconds, reconcile in admission-sequence order
under the admission OS lock, with conditional state updates in short DB
transactions. Capture native evidence while admission is excluded, then recheck
the row/version before committing; never hold a DB transaction during a long
wait or log drain. Reconciliation does not spawn, adopt control of, or kill a
tool by PID. It observes a live matching runner and otherwise follows 5.2.

| Evidence | Action |
|---|---|
| Valid matching exit receipt and termination proof | Import the terminal result idempotently; duplicate reconcile adds no duplicate finalization |
| Foreign/tampered/truncated exit receipt | `uncertain`, append the mismatch; never trust its exit code |
| Unclaimed row past deadline | Conditional expiry; a delayed runner cannot claim it afterward |
| Matching runner alive | Leave ownership intact; report its durable state |
| Runner dead, job absent in its recorded session namespace | `lost`; no invented successful outcome |
| Process query denied, wrong/unknown session namespace, object present or other inconclusive result | `uncertain`, keep admission |

Do not blindly import `descendants_verified: true`: validate the receipt's
job/nonce, runner and child identities and the immutable request; inconclusive
native evidence stays uncertain. Querying an identically named `Local` object
from another Windows session cannot prove the original object absent.

`POST /api/jobs/{id}/recheck` remains the only way out of `uncertain`. Under
admission exclusion, require the expected nonce/state_version; freshly prove
matching runner and child gone and the correct named job absent. A reused PID
with a different exact creation time is not the old owner; access denied is
not proof of death. Only complete proof changes the row to `lost`. Append the
operator note and each check, including inconclusive rechecks. Never offer a
desired-outcome selector or auto-clear uncertainty after a timeout.

Crash fixtures stop the server/runner at explicit handshakes, not sleep guesses:

| Boundary | Required observation after restart |
|---|---|
| Command prepared, before admission commit | No runnable row and no tool |
| Admission committed, before runner spawn | Unclaimed row fences at deadline |
| Runner spawned, before claim commit | No tool; late/duplicate runners fenced |
| Claim committed, before native create | No replacement launch; dead-runner proof -> lost |
| Native create returned, before child row update | Runner death kills the tree; no orphan outside the job |
| Running; root exit; before exit receipt | Surviving runner continues, or dead runner reconciles without guessed success |
| Receipt durable, before terminal DB commit | Matching receipt reconciles exactly once |

## 6. Logs, offsets and storage-failure truthfulness

`web/jobs/logs.py` supplies the pipe reader, bounded writer queue, segment map,
cursor codec and paging. It uses design 5.3's limits:

| Limit | Value |
|---|---|
| Reader chunk / partial raw line | 64 KiB |
| Queue | 256 chunks, at most 16 MiB payload |
| Segment | 8 MiB, named by starting logical byte offset |
| Service retention | 4 segments / 32 MiB |
| Task retention | 256 MiB |
| Stream poll / heartbeat / concurrent streams | 250 ms / 15 seconds / 8 |

The reader only reads/frames/enqueues without waiting on disk or DB. It advances
raw-byte offsets even on overflow. Gap accounting is bounded too: coalesce
adjacent loss, cap pending metadata, and conservatively discard further
undurable data until the writer can publish a bounded gap. An output flood
must not create an unbounded list of tiny gap records or progress messages.
The writer retries with backoff without holding the reader's lock during input/output (I/O).

After a gap, close the old segment and start at the next retained byte's actual
offset. A short/partial write must preserve only its verified prefix, with the
remainder accounted for before a later segment is published. Readers see a
nonoverlapping map of segments and half-open gap intervals. Commit a retention
gap before deleting a closed segment; never rename an open log file. Bound and
compact gap metadata while preserving the old-prefix retention interval, so a
long-running service does not fill the disk with `gaps.jsonl` alone.

Offsets count source bytes, including newline bytes, not decoded characters,
JSON escapes or a rendered `[line continued]` marker. Bound raw partial lines;
incrementally decode UTF-8 (Unicode's 8-bit text encoding), visibly replace malformed bytes, and include a
continuation flag. A 1 MiB line cannot allocate a 1 MiB partial-line buffer.
Progress parsing on the server consumes only bounded complete/continued log
records; validate finite/count bounds. Progress is advisory and cannot finish
a job. Persist the parse cursor so restart catch-up does not repeat events.

**Review clarification: unknown tail.** If G: cannot accept writes and the
runner dies, it is impossible to reconstruct the exact lost suffix length from
memory afterward. Persist durable log coverage/watermarks when storage works; when
evidence is missing, expose `log_status: unknown_tail` with the last proven
offset. Do not invent an exact finite gap, a final byte count, or `eof: true`.
The existing finite reasons remain dropped/retention/drain_timeout. The new
status is a proposed refinement for review, not a previously approved promise.

**Review clarification: bounded cleanup.** A blocked writer cannot hold the
supervisor's native polling loop hostage. At the final drain deadline, request
I/O cancellation, perform bounded joins and classify inconclusive cleanup as
uncertain while retaining admission. Cancellation is an attempt, not proof
that an I/O request completed. Do not publish successful/log-complete finality
while a writer's outcome is unverified. If the DB also cannot persist this
observation, reject new admissions and let later reconciliation report the
evidence honestly; no fallback state directory. A recovered writer cannot
automatically clear an already-durable uncertain state.

## 7. Stop, HTTP and SSE contracts

The protected stop endpoint validates the job's *stored* policy and current
state under admission exclusion; the client cannot choose a stop mechanism.
For a terminal job, return its existing state without sending another signal.
For `mode: none`, return `409 NOT_STOPPABLE` with the gate reason and make no
stop-file or native termination call. An uncertain job uses recheck, not a
hidden stop-as-recovery action. Record idempotent stop intent and delivery
evidence; never report successful delivery if G: rejected the request write.

Write `jobs/<job_id>/stop.request` using a sibling temp file and atomic rename.
Its parent exists before tool launch, it is job-unique, and it is never cleared
for reuse. The runner observes this request independently of the HTTP server.
Read-only policies may terminate their job immediately, then follow verified
descendant/drain finalization. Only a policy with a proven acceptance gate may
terminate a writing run. A writing stopped result retains the warning that the
library may be partly changed. Do not relabel a prior natural tool failure as
a successful stop just because a request raced with root exit.

Watcher integration passes the absolute M1b stop path from the trusted builder.
M1b's active-directory log and final-stop log provide observable progress.
Graceful latency is unbounded while admitted operations finish. M3 supports the
mechanism but does not open the production watcher stop/start routes or enable
its terminate fallback; M6 must first pass the archive interruption/recovery
gate. Root-exit descendant cleanup is separate from an operator-requested
mid-run forced stop. No external watcher is stopped by this endpoint.

The application programming interface (API) follows design section 10:

| Route | M3 behavior |
|---|---|
| `GET /api/jobs`, `GET /api/jobs/{id}` | Bounded/paged summaries, events, progress and redacted display command |
| `GET /api/jobs/{id}/log?cursor=&limit=` | Logical-byte cursor, bounded lines/gaps, next cursor and truthful EOF/state |
| `GET /api/jobs/{id}/stream` | Server-Sent Events (SSE): line/gap/state/progress, resumable by Last-Event-ID |
| `POST /api/jobs/{id}/stop` | Policy-gated, idempotent request/delivery record; no client command fields |
| `POST /api/jobs/{id}/recheck` | Nonce/version-bound fresh proof; 409 STALE on mismatch |
| `POST /api/jobs/{id}/acknowledge` | Attribution/note only; no lifecycle or admission release |

Sessions, Cross-Site Request Forgery (CSRF), Host/Origin checks, body limits,
unknown-field rejection, redaction and error envelopes stay in M2. Streaming
must pass the same session/Host checks. Disconnects release the global stream
slot in `finally`; a slow browser cannot backpressure the runner. Propose
`limit` as 1-1000 display records with a separate 256 KiB raw-byte page cap;
malformed/negative/overflowing cursors refuse, and a cursor beyond the proven
tail is not treated as EOF. Confirm pagination/error details with M2.

A gap precedes the first retained byte after a reconnect inside a missing
range. SSE IDs are the last delivered logical line-end offset; state/progress
events do not advance that byte cursor. Use state_version/event identity for
their deduplication. Heartbeats carry no ID. Escape SSE data as JSON, including
embedded newlines. Eight streams is a server-wide cap, not per job. Extend the
one browser smoke flow to disconnect/reconnect/reload and verify exact cursor
resumption. M2 remains responsible for bootstrap and session refresh.

## 8. Implementation sequence and acceptance evidence

Each sub-PR branches from reviewed master after its dependencies land. Every
commit changes one file. Each sub-PR stays safe with production submission
disabled and includes its protocol updates, focused tests, named guard
bypasses, full Windows suite and fresh exact-head review/CI before merge.

| Sub-PR | Files/concern | Gate to the next step |
|---|---|---|
| M3a | `jobs/store.py`, admission/claim primitives and events; fake clock/process evidence | 20 concurrent request processes admit exactly one; claim-vs-expiry race; duplicate/late nonce fencing; event immutability |
| M3b | `jobs/logs.py`, bounded reader/writer, segments/gaps and cursor codec | Stalled writer leaves reader draining; alternating overflow bounds metadata; short writes, rotation, retention and reconnect offsets exact |
| M3c | `jobs/win32.py`, `runner.py`; integrate claims, seal checks, containment and logs | Immediate grandchild contained; breakaway refused; native create failure starts nothing; killed runner before child-record commit leaves no survivor; verified-zero finalization |
| M3d | `jobs/supervisor.py`, reconciliation, stop and protected job routes; M2 app hook | Full boundary-death matrix, restart adoption, strict uncertain recheck, stop gates, API/SSE/browser integration; trusted M4 seam remains closed until supplied |

Representative tests to name on each implementation handoff:

- Direct service entry as well as HTTP: compatibility and seal checks cannot
  depend only on a route wrapper, as M1b's direct-admission review demonstrated.
- Two real runners with the same job/nonce, wrong nonce, and one after expiry;
  every losing runner proves no tool-side marker was written.
- `JOB_LIST`/handle-list failure, retained/inherited handle mistakes, first-line
  grandchild, pre-existing named job collision, breakaway request, and killing the runner at an explicit
  before-record handshake. Native fixtures report identities, not just PIDs.
- Grandchild that keeps the pipe open: grace measured from root exit, native
  zero before terminal state, bounded drain and prompt query-handle closure.
- Denied liveness query, PID reuse, wrong session namespace, wrong exit nonce,
  live-member uncertain recheck, stale state_version and a recheck racing new
  admission. No side effect can release admission before the proof commits.
- Server down while 50 MiB of fixture output is produced; 1 MiB line; invalid
  UTF-8; blocked disk writer, partial write and disk error; cursors before,
  inside and after a gap; rotated/deleted segments and an unknown suffix.
- Stop arriving before launch, during a fake read-only run, during a gated
  writing run, after natural exit and after terminal commit. A disabled gate
  creates no request and cannot terminate a tool. No live tool is used.
- Unauthenticated streaming, all inherited M2 request guards, command-field
  rejection, global eight-stream limit, cleanup after disconnect, browser
  Last-Event-ID resumption and a slow consumer independent from the log pump.

Bypass each narrow invariant itself: claim-before-create, nonce and deadline
predicates, atomic containment, explicit handle list, descendant-zero check,
gap/segment boundary, nonblocking enqueue, uncertain admission hold, recheck
proof and stop gate. Record named failures; a surviving narrower mutant needs
a direct witness or an explicitly documented redundant guard. Fault injection
uses events/barriers and injected operations, not call-count timing.

## 9. Decisions requested from Claude before code

1. Confirm the four-sub-PR sequence and the closed production-submit seam
   until M4 supplies a real schema/seal adapter. No raw-command shortcut.
2. Confirm M2's effective-state, DB migration, app lifecycle/route and redaction
   interfaces after its exact-head review. M3 will adapt names, not fork them.
3. Approve the unknown-log-tail refinement and bounded-cleanup failure policy:
   no fabricated byte range, successful EOF, or automatic uncertainty clear
   after a runner/storage failure. Real uncancellable I/O can require operator
   intervention; the plan does not promise arbitrary drivers obey cancellation.
4. Confirm added session/precise-identity evidence, in-claim deadline check,
   page bounds and idempotent stop-delivery reporting. These tighten proof or
   define missing interface details; they do not enable a new operator action.

M3 code starts only after that contract review and M2's merge. This plan does
not ask David to repeat his standing development authorization. Any future
live batch apply retains its own backup and exact-digest approval requirements.

## 10. Technical references checked for this plan

The linked approved design supplies the product contract. These primary
Windows references were checked on 2026-10-27 for the native implementation:

- [Job Objects][jobs]: last-handle termination, inherited child membership and
  its limits. Reconciliation query handles must be short-lived.
- [UpdateProcThreadAttribute][attributes]: JOB_LIST and HANDLE_LIST; the latter
  requires inheritable listed handles and `bInheritHandles=TRUE`. Attribute
  backing storage stays alive through process creation and list destruction.
- [GetProcessTimes][processtimes]: exact creation-time evidence instead of PID
  alone or a floating-point timestamp.
- [CreateJobObjectW][createjob]: existing-object detection and the distinction
  between Local/session and Global names. An existing object is not a new job.
- [Canceling pending I/O][cancelio]: cancellation can fail or be unsupported;
  a request is not evidence of completed cancellation.

[jobs]: https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
[attributes]: https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute
[createjob]: https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-createjobobjectw
[cancelio]: https://learn.microsoft.com/en-us/windows/win32/fileio/canceling-pending-i-o-operations

[processtimes]: https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes
[ulid]: https://github.com/ulid/spec
