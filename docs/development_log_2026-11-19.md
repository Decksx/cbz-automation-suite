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
