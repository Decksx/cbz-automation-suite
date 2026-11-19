# Development log: 2026-11-19

## D3a source-read failure correction, PR 210

The Codex connector's P2 inline 4619277800 and Claude's follow-up 6996796833
are valid. The original f7e2625 candidate was never merged: its final local
7,252/12 and both exact-head CI events were green, but the merge is held.
The observer caught LogRefused only. ArchiveRefused is a sibling JobRefused,
and selected hot/cold source reads can also raise OSError. Those failures could
leave the previously persisted eof=true unchanged. Earlier approval and tests
did not establish this boundary; they are not reused for the corrected head.

The observer now also catches ArchiveRefused and rethrows it after the normal
unavailable-status commit. The log reader maps OSError to LogRefused only around
the selected pinned-member read and hot-file open/read/close, retaining the
original exception as its cause. Private-path checks, admission/home operations
and database access stay outside those catches. No broad JobRefused/OSError
catch is added to the observer.

Eight new IDs start from a real saved EOF. Positive cases: corrupt cold ZIP,
missing cold ZIP, cold open PermissionError, and a hot segment disappearing
after lexists but before the native shared open. The last witness replaces hot
evidence with a valid extended index after the saved EOF so the next cursor
actually selects segment bytes, then removes that file at the open boundary.
It preserves the saved progress/cursor while clearing EOF. Negative cases:
page admission/home I/O, private-check I/O, StateDirRefused and database errors
propagate without changing the saved observation.

Before the fix, the first seven added cases gave 4 failures / 3 passes, exactly
the four source failures. The private-check I/O case was added subsequently.
The broader observation/log-service/receipt run passed 107 in 49.32 s before
that last negative case was added. Final inventory: original 24 + 8 = 32
observation cases; total 7,272 = base 7,240 + 32. The final full-source run and
fresh exact-head CI results will be posted on the PR.

Five new process-local mutants are caught: remove archive refusal handling
(1 failure), remove cold I/O mapping (2), remove hot I/O mapping (1), catch
OSError broadly in the observer (2), and normalize private-check I/O (1).
The original 15 mutations were rerun and are all caught. No collection/setup
errors, and both source hashes remain unchanged. The exhaustion mutant's
expected failure also had a pytest temporary-directory cleanup warning,
WinError 145; its test failure was the intended unhandled SQLite-int overflow.

The log_service read adapter now changes, so the original claim that its whole
blob is unchanged is superseded. Its create_writer AST remains identical to
f7e2625. Store, receipts, runner, log disk/pump and recovery modules remain
unchanged. The native compatibility fixture still tests the unchanged writer,
live claim and receipt/finalization fence. Documentation now states this scope.
No app/server/SSE wiring or production console/library/catalog/queue/model work.

### Decoder boundary follow-up before merge

The selected ZIP reader also leaked two expected decoder failures: zlib.error
from invalid DEFLATE bytes, and EOFError from a truncated member stream. The
3460b73 head is held and superseded. A real payload witness keeps ZIP metadata
intact but changes the compressed index member's first block to the reserved
DEFLATE type; it fails with bare zlib.error on that intermediate head. A second
case injects EOFError at ZipExtFile.read after a real saved EOF. The pinned-member
codec now normalizes those two expected failures to ArchiveRefused. No broad
Exception catch is added, and source-I/O/private-home boundaries stay as above.

Final inventory is 34 observation cases (24 original + 10 new), total 7,274.
The intermediate 32-case full run finished 7,260 passed / 12 skipped, 614.93 s;
it was superseded by the decoder additions while running and is not the final
gate. The observation/log-service/receipt run through the DEFLATE addition
passed 109 in 128.43 s; the final archive suite plus stream-EOF case separately
passed 18 in 8.32 s. The final full-source run includes all 110 focused cases.

All seven source-boundary mutations on the final implementation are caught:
DEFLATE normalization (1), stream-EOF normalization (1), archive refusal
handling (3), cold I/O mapping (2), hot I/O mapping (1), broad consumer OSError
catch (2), private-check I/O mapping (1). No collection/setup errors and all
three source hashes unchanged. The earlier 15 observer mutations exercised the
same unchanged observation-module bytes; those plus these seven give 22 caught
mutations, with the earlier cleanup warning retained above. Final full-suite,
fresh review and both exact-head CI results belong on the PR. No deployment.

## D3b prerequisite: one-page producer handoff

From merged 83dce07, add consume_page returning ObservedPage(page, observation).
The existing consume_once delegates and keeps its snapshot-only result. The
consumer returns the bounded source page it already parsed only after its
observation commit succeeds. This lets the future shared producer fan out log
data and progress without a second source read. It accepts no arbitrary replay
cursor. The envelope contains caller-owned JSON objects; frozen fields do not
freeze nested data, and the publisher must encode immutable bytes before sharing.

The cursor/parser/error/CAS body is unchanged. This does not add the shared
worker, subscriber queues, slots, scheduling, HTTP/SSE or browser integration.
No schema or lifecycle change. Those remaining D3 gates stay mandatory.

Eleven new cases cover single-read page fidelity, no open handle/admission scope
after return, a controlled pending-commit barrier, restart/legacy compatibility,
partial live lines, cold/gap pages, read/commit/stale failures and UTF-8 source-byte
cursors. The focused observations/page/log-service/receipt run passes 121 in
57.50 s; the last equality/import cleanup is covered by the final full run.
Inventory: base 7,274 + 11 = 7,285. Full Windows suite is running; final results
and fresh CI belong on the PR.

Four process-local mutations fail: duplicate source read (1 failure), return an
uncommitted batch (10), drop page records (7), return a page from the legacy API
(1). No setup/collection errors and the source hash stays unchanged. These prove
the handoff semantics, not future fan-out/HTTP behaviour. A constructor-timing
assertion was replaced before commit with an actual caller/commit barrier, and
page equality permits a safe internal copy; no Python object-identity promise is
part of the API. No production console/library/catalog/queue/model work.

## D3b service backend: shared reader and encoded fan-out

From merged #211 (0c0f1aa), add stream_manager and stream_frames, with no
HTTP/app/server changes. One worker per job/nonce round-robins bounded progress
and replay positions; one committed producer page is projected to safe DTOs,
encoded once and shared as immutable byte tuples. Eight service-global slots
include in-flight opens. Per-subscriber queues hold at most four 512-KiB
packets plus the current delivery; a slow subscriber closes without blocking
others, and reconnect uses its last delivered byte event ID. State/progress
identities stay separate from that ID. Unknown tails never claim EOF.

Session validity is checked before open, after its fixed reads, during waiting
and before buffered delivery. Close works before first iteration. The last
subscriber stops its worker; the registry retains it until source I/O unwinds.
Shutdown also retains blocked open reservations. False close/wait results
require retaining the home/console guard. An interrupted thread start of
unknown lifetime fails closed. No force-release is added.

The dedicated protocol is docs/web_console_shared_log_readers.md, including
the historical-replay observation boundary and future HTTP owner obligations.
No source handles or SQLite transactions cross delivery. At most one poll per
250 ms for active output or one second cold/idle, independent of subscriber
count. The real-ledger timed witness bounds admission work by four scopes per
poll plus two fixed read scopes per subscribe (initialization measured outside
that interval), with eight subscribers.

There are 56 new fixture cases (22 frame and 34 manager), giving inventory
7,341 = base 7,285 + 56. Focused final checks pass 56 in 33.05 s. They include
shared packet identity/read count; real hot/cold/gap replay; cursor isolation;
slow/fast consumers; source failure; session expiry during wait and buffered
delivery; shutdown during source I/O and open reservations; thread-start
failure before and after birth; restart buffers; active/idle cadence; and a
real 15-second heartbeat. An abnormal worker cancellation initially left
subscribers alive: the added test failed, then finally cleanup fixed it.

All 27 final-code process-local guard mutations are caught, without collection
or setup errors: cap/reservation/auth/expiry/queue/cleanup/lifetime/cadence,
duplicate read/encode, nonce/cursor isolation, DTO redaction/escaping/budgets,
gap ordering, truthful EOF and decimal precision. Twenty-six each cause one
failure; the unknown-EOF mutation causes three. The final rerun patches only
disposable child module namespaces, while full-suite workers read unchanged
source bytes. Both source hashes stay unchanged across that rerun. The first
26-case mutation pass used sequential byte restoration before the final
cancellation fix; it is superseded by this 27-case rerun.

The shared-packet witness was then refined to allow a legitimately scheduled
later poll if the test thread is delayed, while still refusing a repeat read
of the published page. Its final single-case run passes in 4.00 s. All three
mutations using that witness (capacity, duplicate encode, duplicate source
read) are re-proven against the revised harness. An intermediate full run was
superseded by that test-only refinement; it is not the final gate.

The full Windows gate uses frozen final source/tests; its exact result, fresh
head review and both CI events are recorded on the PR. This is the service backend,
not all of D3: owned Response/WSGI closure, M2 binding, socket/request capacity,
eight real streams plus a ninth JSON client, and D4 browser acceptance remain.
No deployment or live job/library/catalog/queue/model action.

### D3b review R1: never-claimed terminal cursors

Connector inline 4623967616 found a valid P2 at c041922: admitted jobs refused
nonzero cursors, but pre-claim refused/admission_expired jobs accepted one and
then synthesized only cursor-zero pages forever. The new two-state regression
fails twice on c041922 (DID NOT RAISE). Admission and synthesis now use the
same unobserved-state predicate. Those two cases plus the admitted regression
pass 3/3 in 2.83 s, without log reads, ledger mutation or retained invalid slots.

Inventory is now 7,343 = base 7,285 + 58 (22 frame, 36 manager). Both earlier
full runs are superseded by the code fix, even if green; their results are not
final gates. The first finished 7,329/12 in 481.86 s. A fresh frozen-code/tests
full Windows run uses eight loadfile workers; its result and the fresh guard
rerun, review and CI are recorded on the PR. No production action.


### D3b successor review fixes: retained readers and absent initial indexes

The successor thread passed the relay canary at swarm HEAD 1c71804 (hive
#6224), then resumed comic #212 from 0e7f851. Claude's two P2 probes reproduce
on that head: eight blocked closing workers count as zero slots, and both
claimed-before-writer and failed-before-writer streams close without state.
The new targeted run fails 3 / passes 3 before the fixes.

Capacity now includes one slot for every reader without subscribers until
its worker finalizer releases ownership. A 12-job fixture blocks the first
eight reads, closes their subscribers, then proves four more opens refuse
before identity I/O. Failed starts after known birth retain this capacity too.
The same accounting covers reservations and ordinary subscribers.

The log adapter adds an explicit allow_unobserved option at the missing-index
boundary. The consumer enables it only before any durable observation exists.
Claimed and terminal jobs without a writer emit state/progress/status without
claiming EOF or moving the cursor; later writer creation resumes ordinary logs.
Nonzero replay closes only the invalid subscriber. Missing observed indexes
(including empty indexes), corrupt indexes and missing indexed segments remain
unavailable. Default consumer/log APIs retain their original refusal behavior.
No HTTP/app/server or browser code changes, and M4b remains Claude's.

The active-cadence witness freezes only the manager clock and advances it only
through the worker's wait branch. A source barrier after two pages makes the
250-ms floor independent of slow disk reads. The seven added manager cases
give 43 manager + 22 frame cases, and inventory 7,350 = 7,285 + 65.
Broader focused checks pass 120 in 61.37 s before adding the final two negative
cases; those two pass separately in 1.69 s. Final frozen-source checks and
semantic mutation results are recorded below and on the PR when complete.

The initial full-suite attempt was stopped after discovering the inherited
supervisor lifetime test can use CREATE_BREAKAWAY_FROM_JOB on this host.
The successor rule forbids that launch. The replacement local suite explicitly
deselects its two cases; it is a partial local gate, not an unqualified full
suite. Hive #6226 asks Claude to cover them independently in an appropriate
host. Exact-head CI and independent review remain required. No production
console, data, queue, library or model action.


The fresh process-local rerun catches all 34/34 semantic mutations, with no
setup/collection errors and unchanged hashes for all four source modules.
The active-cadence bypass now deterministically causes one failure. Additional
mutants prove closing-reader capacity, state before writer birth, observed
empty-index loss, invalid missing-index cursors, corruption refusal and default
consumer refusal. The frozen replacement local suite is still pending at this
checkpoint; none of the stopped/old suite results substitute for that gate.


The first contained run finished 7,336 passed / 10 skipped / 2 failed,
7,348 selected (two death/restart cases excluded), in 363.70 s. It is not a
passing gate. One new assertion wrongly assumed append always wins the race
after create_writer; a valid empty-live-index packet may arrive first. The
witness now accepts that ordered EOF-false status before requiring all bytes.
The other failure is the browser-driver default check: the same assertion
fails on unmodified base 0c0f1aa with the global Python environment and installed
Edge 157.0.4341.60. No browser or global Python settings were changed. The final
contained rerun uses the existing isolated Playwright 1.65.1 environment at
.codex/tmp/web207-playwright165; source and tests stay frozen. Only the revised
claim/writer witness needs fresh mutant checks; both affected mutants are rerun.
All earlier source guard results remain bound to identical source bytes.
