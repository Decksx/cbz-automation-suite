# Shared log readers: service backend

This is the backend portion of M3d D3b, following #210's durable observations
and #211's committed page handoff. It does not expose an HTTP route, start a
console, submit a job, or provide browser integration. The accepted HTTP/WSGI,
eight-stream-plus-JSON and D4 browser gates in `web_console_m3d_contract.md`
remain required before this becomes a usable streaming feature.

## Ownership and admission

The service creates one `SharedLogReaders(store)` and calls `initialize()`
before publishing readiness. Initialization is the explicit additive schema
operation. A request authenticates through M2 before calling `subscribe`,
passing the exact job ID, nonce, canonical source-byte cursor and a trusted,
thread-safe, bounded in-memory session-validity predicate. The predicate must
return exactly `True`; exceptions refuse it. The HTTP adapter must bind this
predicate to its own session generation and expiry, never a client value.

There are eight global slots per manager, counting both subscriptions and
opens still doing their fixed identity/snapshot reads. Capacity is reserved
without waiting before those reads. Invalid identity/auth, failed opens and
failed thread creation release their reservations. A subscription is itself
the closeable byte iterator: `close()` releases it even before `next()` has
ever run. A future Response adapter must own this close on every exit, including
HEAD/auth refusal, pre-header errors, disconnect and WSGI cancellation.

Only one worker exists for each exact `(job_id, nonce)`. The last subscriber
leaving stops it promptly; its registry entry remains until its current source
operation unwinds. An open during that closing window refuses `READER_CLOSING`
rather than starting a second reader. Shutdown cancels subscriptions and wakes
workers. `close(timeout)` returns false while any worker or reserved open is
unsettled. The service must retain its state home and console guard until
`wait_closed()` succeeds or the process exits; a false result is not a clean
drain. Worker cancellation runs the same subscriber cleanup in `finally`.

A thread-start exception after known thread birth retains ownership until the
worker settles. An interrupted start with unknown birth also fails closed;
it can leave ownership retained until process exit. This backend does not
provide a force-release operation for an unproved thread lifetime.

## One reader, bounded fan-out

Workers round-robin over at most nine distinct positions: the persisted
observation cursor and eight subscribers' queued cursors. A persisted-position
turn uses `consume_page()` and publishes that very page after its progress
commit; it does not read the page again. Historical replay is a bounded,
read-only `JobLogs.read_page`, without rewinding or recounting progress.
Clients at one position share one immutable encoded packet. No source or
admission handle, or SQLite transaction, survives into that packet.

There is at most one poll attempt per 250 ms for an active job with output;
cold or idle jobs use one second. Joining or waking a subscriber does not
reset the deadline. Each poll uses at most four admission scopes (job identity
plus the observation's snapshot/read/commit); a replay turn uses two. Each
subscribe has two fixed read scopes, and explicit initialization has one.
The timed fixture witness measures those acquisitions for eight subscribers,
including the idle cadence. Subscribers do not each poll the ledger.

Each subscriber has at most four queued packets, plus the packet currently
being delivered. Each packet is capped at 512 KiB including JSON/SSE overhead,
and at 1,000 source records. Packets are shared immutable tuples of bytes, not
mutable page dictionaries. Queue overflow closes that subscriber with
`SLOW_SUBSCRIBER`, discarding its queued remainder; it never blocks another
subscriber or silently skips bytes. Reconnect starts from the last fully
delivered source event ID, not the worker's queued cursor. A socket writer may
still retain one bounded frame; its deadline belongs to the future HTTP layer.

State/progress versions deduplicate unchanged packets. Their JSON identities
remain separate from the byte cursor. Complete jobs continue polling at the
idle cadence until the client closes; EOF is a proven log boundary, not a
server command to mutate lifecycle state.

## Frames and refusal

`stream_frames.encode_page()` projects explicit DTO fields before encoding.
Commands, seals, process identities and unknown nested keys are omitted. Log
text and progress labels remain display data and must be rendered with
`textContent` by D4. JSON escaping prevents newlines from creating injected
SSE fields. Decimal strings preserve 64-bit source cursors and generations.

Only `log` and `gap` frames carry SSE IDs, equal to their source-byte end.
Gap frames precede retained bytes, including reconnect from inside a gap.
`state`, `progress`, `log_status`, errors and 15-second heartbeats have no ID.
State/progress carry versions for browser-side deduplication. Unknown or
unavailable tails cannot report EOF. A cursor beyond proven coverage closes
only subscribers at that invalid cursor. Jobs still admitted, or refused/expired
without a runner, have only the synthetic zero cursor; a nonzero subscription
is refused before a worker or slot can remain attached.

Source/storage/encoding failures close that job's subscribers with a fixed
public error; details go to the private logger. No queued stale success frame
is delivered after this closure. The persisted-position consumer retains
#210's unavailable commit semantics. A failed *historical replay* closes the
stream without using that older page to change durable progress. Its prior
observation remains a historical snapshot, not a new integrity attestation.

Session validity is checked during waiting and before each delivery, including
already buffered frames. Expiry discards buffers and releases the slot. A
closed manager cannot reopen; a new manager cannot inherit an old iterator's
buffer. Real M2 session binding, server-restart/WSGI closure, socket deadlines,
HTTP thread reserve and installed-Edge tests remain the integration slice.
