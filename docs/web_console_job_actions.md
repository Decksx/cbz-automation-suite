# Protected operator intent (M3d D2)

These JSON actions compose the existing job store and recovery rules. They do
not add production submission, runner interruption or browser action buttons.
The read-only Jobs screen remains the browser's current interface; D4 owns its
action integration. M4/M6 must define an accepted interruption adapter and
writing-tool gates before a running tool can be stopped.

| POST route | Exact JSON fields |
| --- | --- |
| `/api/jobs/{id}/stop` | `nonce`, `state_version` |
| `/api/jobs/{id}/recheck` | `nonce`, `state_version`, `note` |
| `/api/jobs/{id}/acknowledge` | `nonce`, `state_version`, `note` |

No query fields are accepted. IDs must be canonical ULIDs; nonces are 32 lower-
case hexadecimal characters. Versions are positive integers below 2^63; JSON
booleans, floats and numeric strings are refused. Notes must be nonblank UTF-8
text, at most 4,096 characters, without NUL. The server supplies the actor from
the authenticated session, never a JSON field. Trusted direct service callers
must supply an attributed actor (nonblank UTF-8, at most 256 characters, no NUL).
Direct service validation precedes filesystem/storage access.

Every request passes the existing Host, cookie plus browser-key, Origin,
Fetch-Site, CSRF, JSON content-type, body-size and command-field checks before
dynamic routing. Common JSON parsing now also refuses duplicate object keys,
including nested duplicates. Unknown/missing action fields refuse; actor,
desired outcome, process identity, native evidence and stop mechanism are not
browser-selected parameters. GET, HEAD and OPTIONS return 405 for these known
action routes and never perform the action.

## State and ownership

**Stop:** the direct service takes admission exclusion and a short write
transaction, then reads and fences the row. An admitted job becomes terminal
`refused`, increments its version and records an attributed `operator_stop`
event with internal outcome `OPERATOR_STOP_BEFORE_TOOL`. It never becomes
`stopped`, and no `stop.request` is written. Runner claim owns the same admission
lock: either the claim wins and fences the stop, or the refusal wins and the
runner can no longer claim it. The runner's version-2 assumption is preserved.

A terminal job with the current nonce/version returns unchanged, with no new
event or intent. A replay of an older version is stale and must refresh first.
Claimed/running jobs currently return `409 NOT_STOPPABLE`, reason `WAIT_ONLY`;
uncertain jobs return the same code with `RECHECK_REQUIRED`. These refusals do
not record intent, query/signal a native process or create a stop file.

**Recheck:** delegates to `recovery.recheck`, which acquires its own admission
lock and freshly samples native proof. The action layer must not nest that
lock. Only an uncertain job is eligible. Complete same-session absence proof
can produce `lost` with unknown outcome; inconclusive proof stays uncertain.
Either valid attempt records attribution/note and increments the version.
Wrong-session absence remains unknown. A stale replay cannot sample new proof
or release admission. Caller-supplied evidence is never accepted.

**Acknowledge:** fences under admission exclusion and appends a private audit
note in one transaction. It changes no job field, version, lifecycle, admission
or stop-request state. It works for retained cold-history rows without restoring
their job directories. Repeating a valid note may append another audit event;
this is not a receipt or recovery decision.

All actions use the supervisor's operation-lifetime guard, so closing the
console cannot release ownership while a mutation or native recheck is active.
Failed event persistence rolls back the entire action. Native recovery queries
are outside SQLite write transactions. Conditional stop SQL and its row-count
check retain a second fence in addition to the admission-locked row decision;
those SQL predicates are not claimed as independently proven guards.

## Responses and follow-ups

Successful responses contain only `{"job": <whitelisted summary>}`. Commands,
seals, actor, notes, native identity and recovery evidence remain private. Event
pages recognise `operator_stop` and `acknowledged` as fixed codes without
exposing their raw details. Kind and state labels now also use fixed sets;
unexpected values display `unknown` even if the underlying schema is corrupt.

Missing jobs return 404; invalid fields/bounds/IDs return 400; stale nonce or
version returns `409 STALE`; an ineligible recheck returns `409 INVALID_STATE`.
Unavailable storage/ownership returns a redacted 503. Original errors stay in
private console logs. The blank `/login?code=` regression pins 401 while keeping
an outstanding valid link usable. These last two coverage/DTO changes resolve
the nonblocking review notes from #206; they do not rewrite existing ledger rows.
