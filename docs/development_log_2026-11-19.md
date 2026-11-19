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
