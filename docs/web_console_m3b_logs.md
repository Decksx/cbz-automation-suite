# M3b: bounded logs and byte cursors

Codex implements this slice from merged master
`20ebcc26989ebabd8f65b6fe6363edabeec64470`; Claude independently reviews the
exact head and owns pinned merge on green CI. The accepted contract is
[the M3 plan](web_console_m3_implementation_plan.md), with Claude's
[M2 seams](https://github.com/Decksx/cbz-automation-suite/pull/150#issuecomment-6450997809)
and [history review notes](https://github.com/Decksx/cbz-automation-suite/pull/155#issuecomment-6457388532).

## Interfaces and ownership

- `JobLogs(store, job_id).create_writer(claim)` checks the live matching claim
  and returns a `LogDisk`. The claim stays held until every log thread settles.
  The writer revalidates the pinned/private home before file access, without
  holding admission during disk writes. A closed claim refuses further writes.
- `LogPump(source, disk).start()` creates daemon reader/writer threads. It owns
  neither the pipe nor the claim. `join(timeout)` returns `PumpResult` with
  settled/complete proof, read and durable offsets, and any observed error.
  A timeout requests cancellation and returns unsettled even if a later driver
  call completes. M3c must classify unproved cleanup as uncertain and retain
  admission; these primitives cannot clear or finalize any lifecycle state.
- `JobLogs.read_page(cursor="0", limit=1000)` returns ordered `records` of
  type `line` or `gap`, decimal `next_cursor`, `proven_offset`, `log_status`
  and `eof`. Limits are 1–1000 records and 256 KiB of JSON including escaping.
  M3d adapts this to protected polling/SSE, state/progress events and the
  fetch-stream browser transport. No production HTTP/run route is added here.
- `parse_progress(record)` accepts only bounded complete nonfragment progress
  records with finite counts/percent. It returns advisory fields and a cursor,
  never a lifecycle state. M3d owns durable parse-cursor advancement and
  idempotent event persistence, before its route integration is accepted.

## Queue and storage

Reader chunks and partial records are at most 64 KiB. The queue holds at most
256 chunks (16 MiB), in addition to one reader and one writer chunk. The reader
only reads and offers raw bytes under a short condition lock: line decoding
belongs to the bounded pager. It never performs disk/DB work or waits for a
queue slot. Its monotonic byte counter advances even when a chunk is dropped.
The writer detects loss from successive offsets and the final counter; there
is no pending list of alternating gaps that can grow without a bound.

Segments are `log/seg-<start:016x>.log`, at most 8 MiB and always contiguous.
After a loss interval, a new segment begins at the next actual source offset.
Services keep four segments (32 MiB), tasks 32 (256 MiB). Closed segments are
deleted only after a durable retention gap covers them. Failed deletion is
retried before accepting more storage work, so orphan deletion metadata stays
bounded. No open segment is renamed. Lower segment/retention limits exist for
fixtures; callers cannot increase the production limits.

**Format refinement for review:** a compact atomic `log/index.json` replaces
the design sketch's directory-size map plus `gaps.jsonl`. It pins job/nonce,
segments, gaps, source-byte watermark and proven EOF. The map covers exactly
`[0, offset)` without overlap or holes. It holds at most 32 segments and 33
gaps (fewer for services) and is limited to 64 KiB. Removing old segments also
compacts all previous loss intervals into one old-prefix retention gap. This
preserves cursor semantics while bounding gap metadata indefinitely.

Each append fsyncs data before atomically replacing the fsynced index. An index
short write refuses publication. Partial segment writes retain their confirmed
prefix and explicitly gap the rest; later data starts a new segment. After an
I/O error the same writer retries with backoff, truncating any uncommitted file
suffix to the last published watermark before replay. A reader trusts only
the index's coverage; uncommitted bytes never become output by file-size guess.
An existing log directory is never reopened for writing by a new runner.

The index is a mutable operational checkpoint, not an append-only audit ledger.
No log thread alters job rows or job events. A process/storage crash without
proven EOF leaves `unknown_tail` and the last proven offset. It cannot supply
an exact lost suffix length or `eof: true`. Native writer/reader cancellation
can be unsupported or race completion; thread exit must still be proved.
The finite gap reasons remain dropped/retention/drain_timeout; a truncated
unknown tail cannot be fabricated into a finite drain-timeout gap.

## Reading, UTF-8 and archives

All cursors and record bounds count original bytes including newline bytes;
the public codec uses canonical decimal strings up to `2**63-1`. It refuses
negative, padded, fractional, newline-terminated, boolean or future cursors.
Continued records remain <=64 KiB raw and carry `continued`/`fragment` flags.
M5 renders continuation indicators. UTF-8 decoder state is accounted for at
fragment boundaries and across contiguous segment rotations; malformed bytes
are visibly replaced. A gap breaks line-continuation proof. Partial live lines
wait for newline or the 64 KiB bound. Text/JSON escaping cannot change cursors.

`log_io.shared_read` uses pointer-width `CreateFileW` bindings with
READ/WRITE/DELETE sharing and a noninheritable read handle, transferred once
to a CRT descriptor. Each bounded page owns and closes its readers. Tests prove
that the segment can be appended/unlinked while that native read handle lives.

**Cold-read refinement for review:** verified archival additionally records
each member's size/SHA-256 in immutable `job_archive_members` rows, in the same
transaction as its archive pointer and event. `read_pinned_member` authenticates
one member against that independent ledger evidence. It bounds ZIP directory
metadata before parsing and hashes at most one 8 MiB member rather than the
whole 512 MiB batch. A page caches at most two members (16 MiB), so bounded
lookahead across rotation does not repeatedly re-hash adjacent segments. This API intentionally trusts a pinned
member, not an unverified manifest or the contents of unrelated members.
The existing command/cleanup APIs retain full-batch seal verification.

Privacy/reparse guards cover the selected hot paths and archive batch. During
post-commit cleanup, both copies must match the pinned member digest; a corrupt
cold copy cannot fall back to hot, and a corrupt hot copy cannot be hidden by
cold. The new table is additive under schema version 1. Old batches without
member rows refuse fast log paging; no evidence is backfilled or invented.
Their original whole-batch command reader still works. No live archive was
created or converted by this implementation.

M3d still needs a byte/time budget for idle archiving, since full archive writes
and cleanup verification hold admission. This paging change does not remove
that requirement or enable production submissions, native launch, stop, routes,
or live maintenance. Test and guard receipts belong in the daily log and PR.

Native references checked for this implementation:
[CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)
and [CancelSynchronousIo](https://learn.microsoft.com/en-us/windows/win32/api/ioapiset/nf-ioapiset-cancelsynchronousio).
Cancellation receipts are explicitly distinct from I/O completion proof.
