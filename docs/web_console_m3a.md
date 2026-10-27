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

## Remaining work in this slice

The durable store, serialized admission and claim/expiry primitives follow
this state-validation chunk. The completed slice must include the plan's
20-process contention, claim-versus-expiry, duplicate/late nonce and immutable
event witnesses before handoff. Production submission stays unwired until M4
supplies the trusted schema/seal adapter. Native tool creation, log pumping,
reconciliation and HTTP job routes belong to the later M3 slices.
