# M3c slice 2, checkpoint A: live-owner store and exit receipts

This checkpoint starts from master **080e39d5a644b67c9df1b2d55d8e222b21995935**.
It implements the store/receipt part of the accepted
[M3c contract](web_console_m3c_contract.md). It is **not the completed runner**.
There is no new command-line entry point, registry adapter, server launch,
HTTP route, recovery importer or production operation. Native fixtures alone
exercise these internal interfaces. Codex owns implementation; Claude reviews
each exact head before a pinned merge.

## Conditional store interfaces

`JobStore.read_command(claim, version)` reads at most 64 KiB plus one byte,
checks the immutable command digest and all command/ledger fields including
the seal digest, and returns the internal document. The real runner must
still reconstruct a trusted LaunchSpec and revalidate interpreter/source/
registry seals immediately before native birth. This API accepts no argv.

`mark_running(claim, version, child)` requires a live matching claim in
`claimed`, records the exact child FILETIME as a decimal string, requires
the same Windows session, and derives the deterministic Local job name.
The caller already owns the contained child. A failed commit never permits
another native creation or means that no child was created.

`refuse_before_create(claim, version, reason=...)` accepts only `claimed`.
It is a low-level trusted-runner interface: its caller must positively know
native birth did not occur. It is forbidden after a possible birth, including
failure to commit running, or a named-object collision. Those paths retain
admission through `hold_uncertain`. No public desired-state interface is added.

`finalize(claim, version, process, pump, outcome_mapper=...)` accepts only
`running`. It reads the durable receipt and re-observes the actual owned
process and log pump. It checks the expected row again inside the write
transaction before updating state and appending the event. A second call is
fenced, not a second terminal event. Recovery import will be a separate API.

All mutations retain OS admission exclusion and a short DAL transaction.
The shared `_claimed_row` predicate validates the open claim, issuing store,
job/nonce, expected state/version, exact runner PID/FILETIME/session, and the
physical identity of the still-named runner-lock file. The predicate and
update run in the same transaction under admission exclusion; there is no
unlocked read/update interval. Events and clock high-water changes roll back
with the transition. There are no native waits, log flushes or fsyncs inside
the write transaction. No schema migration is needed for these interfaces.

## Receipt publication and finality

`jobs.receipts.publish(..., outcome_mapper=...)` requires a live running claim.
The mapper is a trusted internal dependency, required again at finalization;
receipt JSON does not select its own outcome. M4 must bind the mapper to its
reconstructed/sealed registry adapter. Only succeeded/failed are supported
here. Stopped mapping and any recorded stop intent refuse until actual stop
policy/delivery evidence is implemented.

Both publication and finalization require:

- The retained `ContainedProcess` matches the recorded child and named job;
  root exit is directly observed, and a fresh job query reports zero members.
- The supplied `LogPump` owns that process's pipe. Both real threads started
  and settled, no pump error remains, and the reader observed actual EOF.
- The pinned private log index is valid, complete, matches the disk snapshot
  and reader offset, and is bound by its actual byte hash. Finite recorded
  log gaps remain truthful; unknown/non-EOF tails do not qualify.
- The immutable command still matches its ledger digest, fields and seal.

The receipt binds format version, job/nonce, command/seal digests, exact runner
and child identities/session, job name, root exit code, mapped state, end time,
zero-member observation, log settlement/coverage/index hash and no-stop facts.
Canonical bytes reject extra/duplicate fields and Boolean/integer aliases.
Opened regular-file identities, path privacy/reparse checks and bounded reads
guard receipt/index/command access. A receipt from the future cannot commit.

Publication creates an exclusive sibling temporary file, checks the write,
flushes/fsyncs, atomically replaces exit.json and verifies readback. An existing
receipt is never overwritten. A failed fsync/replace leaves the ledger running;
there is no alternate directory or fabricated terminal result. A valid receipt
survives a terminal DB/event failure and may be retried by the **same live
claim**, once. This is not dead-runner recovery import.

**Persistence is synchronous and can stall.** These APIs do not yet provide
runner supervision. The forthcoming runner must perform receipt publication
without blocking its native polling loop, bound cleanup attempts, and retain
claim/handles for unsettled work. A timeout or cancellation request is not
settlement proof. Neither a recovered writer nor a valid late receipt clears
`uncertain`; M3d's explicit fresh-proof recheck remains the only release path.

## Native review follow-ups and remaining work

The environment block now sorts by uppercase names, with a direct CreateProcessW
argument witness. The dedicated runner host must not launch unrelated processes
concurrently while temporary pipe/NUL handles are inheritable: HANDLE_LIST
constrains this launch, not another broad-inheritance call in the same host.
Opening and closing a collided job's query/creation handle can participate in
kill-on-last-close lifetime if its owner exits concurrently. Never modify or
adopt its limits, and never claim closing the collided handle cannot affect it.

Checkpoint B still owes `runner.py`, trusted LaunchSpec reconstruction, claimed
entry/spawn wiring, post-birth commit failure handling, descendant grace and
termination deadlines independent of EOF, stop gates, responsive publication,
bounded cancellation/join, and the failed-writer child that writes beyond the
pipe buffer while the reader continues draining/discarding. Slice 3 still owes
dead-runner recovery evidence/import helpers. M3d orchestration/routes and M4
production adapters remain closed. No rollout or live-library action.

Acceptance results and selected guard mutations are recorded in the dated
development log and the implementation PR, tied to exact code commits.
