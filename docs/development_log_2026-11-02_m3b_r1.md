# M3b R1: bounded production writer checks (2026-11-02)

Claude requested changes on exact 9b8a93873dd77392928912196ecae98f241f56d7
in [review R1](https://github.com/Decksx/cbz-automation-suite/pull/156#issuecomment-6584982033).
The follow-up was authorized by relay #3421 (confirmed ACK #3423).
Claude owns the fresh exact-head review and pinned merges of Comic #156 then
Swarm context #183. No rollout is included.

## Resolution

Source/test/protocol candidate **cc90ef094d15f5f31a76a486b0b306096db1f60e**
adds five one-file follow-ups. JobLogs.create_writer still performs the full
inspect_state_dir under _home/admission at creation, including the config,
containment, fixed privacy scan and ledger identity pin. Each streaming access
instead checks home st_dev/st_ino against the pinned store identity, privacy
of the home and exact path plus its parent, all reparse components below the
home, and the open claim. Streaming does not rescan other job artifacts or
reload config/containment/SQLite. Disk I/O remains outside admission.

watcher_watch_folder caches one parsed source snapshot by bytes, without
importing the watcher. Calls still read the current source; same-size edits
with restored timestamps invalidate the result. This removes repeated AST
parsing without retaining an obsolete watch root.

The protocol now explicitly records a StateDirRefused mid-job as a boundary
failure: the writer stops without finish/EOF and the reader may keep draining.
M3c must classify the job as uncertain, stop/cancel as needed, and retain the
claim until both threads settle. The incomplete index ends at the last durable
offset. A live job row still pages as live; after uncertainty is recorded it
pages as unknown_tail, never eof=true. A deterministic pump witness preserves
6 durable bytes from 11 read bytes and verifies StateDirRefused/unknown_tail.

## Witnesses and measurements

The production create_writer witness persists **8 MiB / 128 x 64 KiB chunks**
with real private paths and fsyncs, verifies exact persisted hash and no gaps,
and observes **one full inspection total**, at creation. It asserts the count,
not elapsed time. Its local call duration was **7.13 s** in the final focused
run. Adapter/config files: **45 passed in 27.50 s**, including nine new cases
(one cache case, eight adapter cases) against the prior population.

Five targeted mutations caused nine named failures:
- Reintroduce a full inspection on each access: inspection-count witness, 1.
- Remove pinned home identity comparison: changed-identity witness, 1.
- Remove selected-path privacy/reparse check: four new ACL cases plus a
  mid-job junction replacement, 5.
- Remove open-claim check: existing closed-claim witness, 1.
- Disable parse caching: repeated-parse witness, 1.

Every mutation was restored before verification. Replay script is outside Git:
Documents/Codex/control/m3b_r1_mutants.py. Its first cache replay exposed an
overly narrow AST spy signature in the test harness; the spy was corrected
to forward positional/keyword arguments and the named failure was verified.

A separate disposable private-home measurement through create_writer:
**20 appends x 1,024 bytes in 0.8697 s**; then **20 x 65,536 bytes in 1.1649 s**
(1,310,720 bytes / 1.1649 s = about 1.125 MB/s). Writer creation and finish
are excluded from those append timings. Claude's prior review measured
20 x 1 KB appends in 17.2 s with 60 full containment scans. These are host
measurements, not a guaranteed tool rate or a controlled cross-run benchmark.
The deterministic count is the regression contract. Script:
Documents/Codex/control/m3b_r1_rate.py.

## Explicit OfficePC environment exception

Per the review's offered alternative, retain and record the exception instead
of adding a retry to a direct LogDisk test. Claude reports that at exact
9b8a938, tests/web was **338 passed / 1 failed** and
test_alternating_overflow_compacts_map_and_physical_files deterministically
raised PermissionError [WinError 5] replacing index.tmp with index.json, alone
and in the suite. This is attributed review evidence, not a new Codex replay
of that exact test at the old head. The earlier Codex zero-write failure was
independently reproduced at unmodified f0b709f; see the previous daily record.
The underlying host access-denial cause remains undiagnosed. Defender,
indexing, ACLs or another process are hypotheses, not established causes.
Production already retries OSError with backoff in LogPump; no production or
test retry changes are made here, and a red local run must be qualified.

## Verification checkpoint

The local full suite and both Windows CI runs were started at cc90ef0.
This evidence-only follow-up leaves source and tests unchanged. Final full
results, exact-head CI receipts and the next owner are recorded in the PR
handoff and maintained Swarm context; pending results are not claimed green.
