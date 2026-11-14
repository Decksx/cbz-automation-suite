# 2026-11-13 — #108 fresh verifier

Base: `909991da8ea47cb0f6189c4d97fedfbdd4610a92` (#194 merge).
Lead: codex-lead. Fixtures only; no live inventory, sync, configuration or data
changes. Independent review and exact-head CI remain merge gates.

Added an all-Tower hashing option and baseline CLI mode, then a verifier that
collects fresh evidence itself. Expected paths/bytes, root identities and
exclusions are bound. Source drift, target differences and unknown coverage
prevent success; metadata-only drift remains diagnostic. Counts reconcile, and
`sync_ready` remains false. The configuration evidence adapter remains open.

Validation:

- Base observation/CLI/preview tests: 127 passed.
- Implementation plus the first 43 verifier cases: 170 passed in 6.55 s.
- Full suite at `af7db841c8e6c4b586860a83e639ac5c4a6ebb4c`:
  **6,820 passed, 12 skipped in 453.43 s** (6,832 collected).
- One additional malformed-baseline directory-spelling parameter was added in
  test-only commit `d3e950228a456bb075ca94ac7d2136164985d321` after full-suite
  collection. Implementation is byte-identical to the full-suite source.
  Final verifier file: **44 passed in 4.00 s**. Final inventory is one case
  larger; the local full result above is not presented as a final-head full run.
- In-memory mutation checks on the final source/tests: control 43 passed,
  1 deselected (the separately passing real-subprocess CLI test). All **29**
  mutation groups produced test failures, with no collection errors. Source
  and test SHA-256 values were checked unchanged before/after the run.

Mutation groups and failed-test counts (overlapping, not additive): baseline
coverage 1; baseline hashes 1; baseline directory conflicts 1; root shape 1;
policy shape 1; boolean type 3; all-Tower selection 7; selection digest binding
3; fresh read 26; scope equality 5; exclusion-policy binding 1; coverage 3;
negative-fact suppression 5; source comparison 2; source-drift gate 4; removed
source 3; missing target 4; target size 1; target hash 1; extras 4; unknown source
hash 1; unknown target hash 2; empty scope gate 1; empty coverage gate 1; result
digest 1; byte counts 3; CLI baseline 1; CLI exit status 1; CLI expected argument
1. These exercise safety behavior, not controller approvals.

Final source/test SHA-256:

```text
sync_inventory.py 79fa84502b41d84d53febb4a69e1d28ed091be50c69cfdf7c22b83a0e2f9a681
sync_verify.py efff78edaf548fb13bb9db44ff94c403116190d22d032116a014b8e9c71fd26e
sync_observe_cli.py 74fe20a17886b4c88eb79c13fcea2b485c6819e2f3b9c9f2444e56d8f4f1bd77
test_sync_verify.py 2d9d51f01497a39f3252d8642508589d3a22aa47e299cef2b5b05934e11133b2
```

Review focus: baseline/actual scope binding, incomplete evidence and zero-file
cases, source changes during the migration window, exact receiver paths, and
count partitions. The existing observer's drift checks remain necessary: a
fresh verification is evidence from reads, not a distributed atomic snapshot.

## Review round 1: incomplete coverage must not establish absence

Claude's #5009 review found a narrower mutant than the original negative-fact
branch bypass: replace `if not coverage or not scope_matches` with
`if not scope_matches`. The final `verified` flag still stays false, but the
old tests did not distinguish that from a report falsely claiming missing or
extra files. Independent reproduction on the old cases: **43 passed / 3
deselected in 2.53 s** (the two new cases and real-subprocess case excluded).

Test-only commit `021db7a` adds two real temporary-tree cases: deny a nested
listing on Tower or the receiver while an extra receiver file remains visible.
Root binding still matches. Both cases require no matched/missing/mismatched/
extra/source-drift claims, an exact unresolved-row partition, and every file
and byte count held. The narrow mutant fails **2/2**; control passes **2/2**.
Final verifier file: **46 passed in 3.38 s**; combined focused set: **173 passed
in 8.22 s**. No implementation changes were needed.

The three implementation SHA-256 values above are unchanged. The revised test
file is `4c778362f3498450e4f3677f01fd80a7ee83016987e9bf6d8f592b2c1fda3d5f`.

The P3 policy note now tells operators to pin the same code version for baseline
and verify, and to collect a fresh baseline after an exclusion-policy change
(including #195), preserving older artifacts unchanged. Empty desired layouts
remain valid file-set equality: the report already exposes zero observed file/
byte counts and `sync_ready=false`. The guide now states that explicitly; no
populated-library or migration-readiness claim is inferred. The observation
guide also links the delivered verifier instead of calling it future work.

First round-1 full suite, started at `983b6a3136d925b133da20f40738941134871740`
(implementation/tests identical to the final candidate): **6,822 passed / 12
skipped / 1 failed in 441.96 s**, 6,835 collected. The sole failure was
`tests/web/test_web_recovery.py::test_native_owner_crash_reconciles_at_explicit_handshakes[birth]`:
`PermissionError [Errno 13]` at line 243, reading the temporary `owner-ready`
file after its existence check. Isolated candidate run: **1 passed in 33.53 s**.
The identical node on unmodified base
`909991da8ea47cb0f6189c4d97fedfbdd4610a92` reproduced the same line, exception
and temporary filename: **1 failed in 6.30 s**. This is measured base
reproduction, not an inference from the untouched web files. No web code/test
change is included in this PR. A full candidate rerun follows this comparison.

The full rerun at `21ed9c2ef48577fd9ddea154f40fcac47b944858`
(implementation/tests identical to the final candidate) passed: **6,823 passed /
12 skipped / 0 failed in 497.84 s**, 6,835 collected. The first failure and
base reproduction above remain part of the evidence; this result does not
erase them. Current final-head CI and a fresh independent review are still
required before merge.
