# M3a: console job store and admission

This is the first implementation slice of the approved
[M3 plan](web_console_m3_implementation_plan.md). It builds on M2 merge
8f1f0ffe3443e38e4224964b2a1c5e8564e08973. Codex implements; Claude reviews
each exact head and owns the pinned merge after green checks. No rollout.

## State inspection

`state_dir.inspect_state_dir(path, expected_identity=..., environ=...)`
validates an existing state home while the HTTP server owns `console.lock`.
It shares the server's local-volume, privacy and containment checks, requires
existing configuration and a matching existing identity pin, and uses the
read-only database layer so committed write-ahead log contents remain visible.
It checks physical identity again before returning.

The returned `StateDir` view owns no lock. Closing it cannot release the
server's lock. Inspection never initializes a missing configuration, directory,
ledger or pin. A runner must receive the expected physical identity from its
trusted admission context and revalidate before it trusts command artifacts.

This is validation at a boundary, not a promise that an operator cannot
replace a directory after the call returns. Admission and runner boundaries
must each revalidate; there is no temporary or second-ledger fallback.

## Durable store and admission

`jobs.store.JobStore` creates the console tables under `console_meta`'s
`schema_version = 1`; it refuses unknown versions. It uses the existing
database access layer with write-ahead logging and a console-specific FULL
synchronous policy. It changes neither the operational database nor its queue.
Every service boundary revalidates the pinned home and schema version.

An admission requires an injected trusted verifier returning `VerifiedRequest`.
There is no permissive default, browser run route or direct command field.
M4 supplies the production registry/seal verifier. The test registry returns
an inert fixture seal, not an executable command. Classification and effects
come from that trusted verifier, never from a caller override after verification.

The service obtains the one-byte admission lock before preparing its bounded
immutable command. It flushes, fsyncs, atomically publishes and reads back the
command before beginning the insertion transaction. Unlike the plan's proposed
prepare-before-lock order, this puts preparation under OS exclusion to avoid
racing the shared privacy scan with temporary files. Disk synchronization stays
outside the database transaction. Contenders wait a bounded time for the lock.

Inside `BEGIN IMMEDIATE`, compatibility is checked again and the row and event
commit together. A known conflict is also checked before preparation to avoid
leaving unnecessary artifacts. Failed preparation cannot create a runnable row;
an orphan directory is never reused. Its later reclamation belongs to the
supervisor's retention work. The command includes the physical state identity,
canonical ULID, nonce, immutable seal and trusted classification; the ledger
records the full command digest and the separate seal digest.

Only one task can hold admission. Watchers may coexist with read-only tasks.
Writing tasks and watcher starts need a fresh, known watcher observation;
uncertainty blocks all classes. These rules do not exclude arbitrary external
CLI writers. M6 supplies the production watcher observer.

## Claim and expiry

`claim(job_id, nonce, ProcessIdentity)` first obtains the job's nonblocking
runner lock. It then conditionally commits an admitted-to-claimed update with
the exact nonce, deadline and process identity. Creation time is an integer
Windows FILETIME, stored as text to preserve all bits; session ID is retained.
These primitives validate integer fields, not native process liveness. M3c
collects and verifies the real Windows evidence; tests here inject identities.
The returned `RunnerClaim` keeps the runner lock until close. Closing it does
not finish the job or release the durable admission hold.

The claim checks its deadline itself. `expire_admissions()` can fence only
still-unclaimed rows past that deadline. A stored clock high-water mark and
the preparation/commit ordering check refuse contradictory or rolled-back time.
Expiry never releases a claimed or uncertain job. `hold_uncertain` requires
matching nonce and state version; this slice has no uncertainty-clear API.
Every lifecycle mutation and its append-only event share one transaction.
Database triggers refuse updates and deletes of events.

Native creation, terminal proof, logs, stop, reconciliation and protected job
routes belong to later M3 slices. This store does not launch a runner or tool.
Production submission remains unwired until M4's trusted adapter is present.
M2's privacy scan still refuses more than 2,000 state entries. This slice does
not garbage-collect job history or orphan artifacts; retention needs to account
for that ceiling before production submission is enabled.
The source-change window, live watcher interruption gates and manual UI checks
retain their accepted limits; this slice does not claim those gates passed.
