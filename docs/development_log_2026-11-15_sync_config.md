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
