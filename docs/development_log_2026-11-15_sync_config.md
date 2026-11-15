# 2026-11-15 — #108 sanitized configuration evidence

Base: `2e82a3223fcde283defc821d2d19af3a3b5cbf07`.
Lead: codex-lead; branch `feat/108-config-evidence`. Resumed for David's
hive #5120 (ACK confirmed #5124); work split mirrored to Claude in #5128.
All inputs were private fixtures. No HTTP, library inventory, backup,
configuration/control action or sync was performed against a live service.

The pure adapter binds supplied two-boundary API-shaped exports to an existing
filesystem report. Remote-to-observed path mappings are explicit declarations,
not mount proof. Config is allowlisted before output/fingerprinting; unrelated
GUI credentials, passwords, command values and folder paths are omitted.
Supported positive expanded ignores must exactly cover the current observer's
policy; unsupported evidence stays filesystem-only. Versioning/roles/paused
state are described, never treated as operational readiness. Fresh verification
requires the configuration baseline bound to its very same expected observation.

Durable contract and official references: `docs/sync_configuration_evidence.md`.
The official matcher uses lowercase/glob semantics rather than Python full
casefold, and its temporary-prefix test is broader than the observer's suffix
rule. Unicode relative names and counted native temporary names therefore
fall back. This deliberately does not implement a general Syncthing matcher or
claim that actual configuration, index authority, backup or cutover is ready.

Validation before publication:

- New repository test file: **59 passed**; combined config/observation/CLI/
  preview/verifier/writer-gate set: **267 passed in 9.54 s**.
- First complete full run at `3531492e5eac9d8a1e17c3b55d6dc99cbbaf122d`:
  **6,917 passed / 12 skipped / 0 failed in 372.29 s**, 6,929 collected.
- An earlier preliminary full run was deliberately stopped when auditing the
  native temporary-name difference; its worker-down output is cancellation,
  not a completed or passing result.
- A separate content-mode assertion survived bypass (61 cases still passed).
  Source inspection confirmed `_validated` already unconditionally requires
  content mode. The redundant assertion was removed in `94a8dee`, with a
  comment identifying the existing guard. A fresh full run was started at
  `94a8deeddf0e9a52d688b6d14f6dfe22c8dfb9d1` afterward.
- That final full run passed: **6,917 passed / 12 skipped / 0 failed in
  396.52 s**, JUnit **6,929**. Final publication changes only documentation
  afterward; implementation and test bytes are those validated by this run.
- Final in-memory mutation control: **61/61**, comprising 59 repository cases
  and two extra witnesses (Unicode expanded-pattern folding and substituting
  another valid observation for the configuration baseline). All **22**
  mutation groups cause test failures; source/test hashes unchanged around
  the run. No mutant was installed in the worktree.

Mutant failure counts, overlapping: capture contract 1; filesystem digest 1;
baseline coverage 1; baseline observation binding 1; baseline root binding 1;
baseline scope fingerprint 1; encryption datatype 1; Unicode corpus boundary 2;
native temporary boundary 3; folder support 18; remote-root overlap 1; peer set
1; capture stability 6; namespace mapping 4; ignore-set equality 1; POSIX case
semantics 1; ASCII expanded-pattern guard 1; baseline scope drift 1; comparison
completion 2; scoped verification 1; sync readiness 2; raw-capture copying 6.

Final implementation/test SHA-256:

```text
sync_config.py ffd3de07d5240d1a7a8ec46680733ee971836565201d0c1d61f7cea96abb60ba
sync_config_cli.py 58154fa459cab10973facb1d3b98f058c43732902496a2abcf1f01b88cfbcd61
test_sync_config.py 48b44a0dff5d96265d7b2e0c48afdf470bfb6c1bfb31ae1012460f9fa6f06347
```

Independent review should concentrate on secret omission, namespace and device
binding, unsupported-ignore fallback, capture/baseline drift and separation of
filesystem equality from scope/configuration/operational claims. #108 remains
open; current-head independent review and CI are required before merge.

## Review round 1: permanent baseline and CLI guards

Claude requested changes at `1dbdb8c` in review
[6899501633](https://github.com/Decksx/cbz-automation-suite/pull/198#issuecomment-6899501633),
relayed in #5148 (ACK confirmed #5168). No code defect was reported. Two
important witnesses existed only incompletely in the repository suite:

- `test_configuration_baseline_must_match_the_verifications_own_baseline`
  binds configuration to B1, collects B2 after a real source mtime change,
  proves their scope fingerprints agree while their observation digests differ,
  accepts B2 configuration with B2 verification and refuses B1 with B2.
  This is a valid separate observation, not a broken digest fixture.
- `test_cli_success_requires_a_complete_bound_report` has three cases:
  complete bound output exits 0; a supported-JSON ignore fallback and incomplete
  filesystem evidence both print their reports and exit 2 without stderr.

The repository file now has **63 passed** (+4). Exact in-memory review mutants,
with a **63/63** control: removing only the observation-digest comparison in
`_expected` gives **1 failed / 62 passed** (the new baseline test); changing
only the final CLI completion return to unconditional 0 gives **2 failed /
61 passed** (fallback and incomplete cases). Source/test hashes were unchanged
around the run; detailed output is retained outside Git in
`C:\Users\david\.codex\tmp\config108-review-mutants.json`.

The module now explicitly documents the equivalent ignore-set mutation
(`==` versus `<=`, because extras are rejected per pattern) and the overlapping
drift checks (the stable flag and drift problem). Removing either drift check
alone is redundant; bypassing both together is load-bearing, as Claude proved
with 5 failures. These are comment changes only; no production behavior changed.

Fresh full suite at `812d704` after the test/comment commits: **6,921 passed /
12 skipped / 0 failed in 1,543.95 s**, JUnit **6,933**. This run was serial,
unlike the earlier parallel run; the duration is not a performance comparison.
Counts reconcile to base 6,858 + 63 new cases. The final publication changes
only this topic log afterward. Both original CI runs passed at `1dbdb8c`, but
the revised head needs fresh exact-head review and CI; those old successes
are not approvals for the changed candidate.
