# Development log — 2026-11-12

## #185: bounded production log-index publication recovery

Owner: codex-lead; independent review belongs to claude-lead. Branch
`fix/185-bounded-index-retry` starts at
`fb17f09c03e92ef8dc8d98dbde05c91350d6d59e`. Work resumed after the Camelia
prune and index fix, with rollout of that separate application left to Claude
and David. No live comic database, library, model, log store, or worker is used.

### Behavior and boundaries

A single recovered index replacement error previously left a claimed pump's
fully durable log incomplete. The chosen fix is issue #185 option 1: recover
inside `LogDisk._publish`, before an error reaches the pump. Only a
PermissionError with WinError 5 and both exact index.tmp/index.json paths from
os.replace is eligible. Five publication attempts are allowed, with four
20 ms waits. Validation, serialization/size checks, private-path/claim checks,
rewrite, flush and fsync repeat before every replacement attempt. The durable
snapshot advances only after replacement succeeds. Filesystem calls themselves
remain subject to their existing latency; 80 ms bounds the deliberate waits.

Exhausted or unrelated errors retain the existing pump failure and uncertainty
policy. No error flag is broadly cleared. Explicit durable gaps retain their
meaning and may coexist with proven EOF; an unrecorded tail is not promoted to
coverage. The runner protocol and source comments describe these boundaries.

The test-only wrapper from #184 is removed. The three existing stress tests now
exercise production recovery directly. Its 11 wrapper tests are replaced with
26 real-publication tests, a net increase of 15 cases. They cover initial,
append and EOF publication, repeated validation/write/fsync, damaged temporary
files, late watermark, exhaustion, unrelated error types/paths, claim/coverage/
size changes during the wait, claimed-pump finality and explicit gap coverage.
The focused file passes: 26 tests in 1.59 seconds.

### Validation and mutation evidence

FULL_SUITE_PENDING: the full Windows suite is running. Two final test cases
were added after its collection; their focused run passed. Final full-suite
coverage and exact inventory will be recorded before the review handoff.
The previously recorded #184 baseline is 6,553 passed / 12 skipped. Its final
web code/tests at c8f2995 match the branch base fb17f09 byte-for-byte; this is
historical baseline evidence, not a newly rerun baseline claim.

The final 26-case file against the base _publish method fails 15 and passes 11.
All mutations are in-memory method replacements in separate fixture processes;
repository sources are not restored or rewritten. Each of these 18 bypasses
fails at least one new test:

| Removed or broadened behavior | Failing tests |
| --- | ---: |
| WinError 5 only | 2 |
| Source path present | 1 |
| Destination path present | 1 |
| Exact source path | 1 |
| Exact destination path | 1 |
| Fifth-attempt refusal | 3 |
| Delay between attempts | 9 |
| Candidate validation on retry | 1 |
| Temporary path validation on retry | 1 |
| Index path validation on retry | 1 |
| Index fsync | 2 |
| Index size bound on retry | 1 |
| PermissionError type only | 1 |
| Late durable watermark | 11 |
| Fresh temporary-file rewrite | 1 |
| Catch only replacement errors | 1 |
| Pump sticky-failure rule | 3 |
| Explicit gap recording | 1 |

Source SHA-256 before/after mutation runs:
`2cc99bf070d66b0e5d663db065fa2feb4115e7551cefba48797ad5e8522555b3`.
External scripts/logs use mutate185_publish.py and comic185-* in Codex scratch.
The 4/6,040 rate from #184 describes initial attempts in a private stress
fixture. The earlier 18% job estimate assumes that rate and independent failures
over 300 publications; production frequency and the Windows actor are unknown.
The fix is justified by the reproduced failure mechanism, not that projection.

Code/tests/protocol are six one-file commits through
`77f54fbf685e00289ee2a345d77ab21fdbe20c24`; this log is a separate commit.
Independent exact-head review and CI are required before merge. The web console
has not been rolled out and no operational job was launched by this work.


## #168: bounded recovery in three direct-writer log stress fixtures

Owner: codex-lead; independent review goes to claude-lead. Base is
`eb507f4db95d49312e191e812bc533c832815d8e`. David asked both leads to resume
open projects in hive4423. This is the previously assigned log-index work,
separate from Claude's watcher naming and documentation changes.

### Observed failure and scope decision

A private temporary-directory reproduction ran20 copies of the alternating
overflow workload,300 appends each plus initial/final index publication.
Of **6,040 initial replacement attempts,4 failed** with PermissionError,
WinError5/errno13, source `index.tmp` and destination `index.json`, all in
`LogDisk._publish`. All4 succeeded on the first retry after approximately
15–16ms. Thus the experiment made **6,044 total replacement attempts**, including
those4 recovery calls. All20 final indexes validated complete coverage through
offset1200, at most4 segments/5 gaps and exactly5 physical files.

This proves transient behavior at the observed operation. It does not identify
Defender, indexing, ACLs or any other Windows actor. Earlier failing receipts
on issue168 remain valid; a green rerun is not substituted for them. Diagnostic
script/results are retained outside Git under Codex scratch worktrees as
`diagnose168_index.py`, `.json` and `.log`; the JSON names its private temp root.

The issue explicitly allows a justified narrowly bounded fixture retry. The
production `LogPump` already retries storage errors, and its sticky-failure
policy deliberately withholds complete EOF after a storage failure. Production
code is unchanged. Retrying inside the product publisher would alter that
contract, so this change instead adds an explicit test-only fixture for the
three direct-writer stress tests whose purpose is retention, zero-write cleanup
or streamed byte coverage:

- `test_alternating_overflow_compacts_map_and_physical_files`
- `test_zero_writes_do_not_leak_empty_segments`
- `test_create_writer_streams_eight_mib_with_one_full_inspection`

### Fixture contract

`tests/web/log_test_support.py::retry_log_index` is opt-in, never autouse.
It retries the same publisher and candidate only after a PermissionError with
WinError5 and both exact expected source/destination paths. There are at most
5 calls and4 sleeps of20ms (80ms scheduled waiting plus I/O time), with a
pytest warning naming the index for every retry. Persistent refusal propagates
the original error; other errors are not retried. Initial publication is also
covered when an opted-in test constructs its writer.

Each attempt still executes the original validation, full write, flush, fsync
and atomic replacement; the in-memory snapshot advances only after replacement.
Ordinary storage-fault tests do not opt in. New tests assert that the default
writer still exposes its first failure and retains the old watermark. No
production retry, byte-coverage, retention, ACL, cancellation or unknown-tail
behavior changes. The Windows actor remains unknown after this mitigation.

### Verification

The first focused log suite passed **83 tests in48.96s** with10 new cases.
One additional missing-destination case was added before the final full suite,
bringing the new test count to11. The final inventory is the base's6,554 plus
11 = **6,565**. Full code-final run atc8f2995 on Windows/Python3.11, translation
disabled, `-n4 --dist loadfile`: **6,553 passed / 12 skipped / 0 failed in444.20s**.
All11 new tests passed. The extra two host skips are the native outer-Job
containment cases. The earlier independently measured base-tree suite was
6,542/12/0, inventory6,554, recorded in the PR182 review; it is not a fresh
baseline full rerun in this investigation.

An external deterministic injection raises the observed index replacement
WinError5 on the third publication in each affected stress test. Unchanged
base: **3 failed**. Candidate: **3 passed / 3 retry warnings in8.58s**. Assertions
inside the tests were not changed. A separate unforced replay of each of those
3 tests10 times passed **30/30 in79.27s**, without retry warnings in that sample.
The repeated green sample does not establish that the OS interference is gone.

Eight guards were bypassed in memory, with sources unchanged:

| Removed condition or behavior | Failing new tests |
| --- | ---: |
| Only WinError5 | 2 |
| Exact temporary source path | 1 |
| Exact index destination path | 1 |
| Source path must be present | 1 |
| Destination path must be present | 1 |
| Re-raise after the fifth attempt | 1 |
| Delay between attempts | 2 |
| Explicit fixture installs the wrapper | 1 |

External probe scripts and logs use `probe168_stress_fault.py`,
`repeat168_stress.py`, `mutate168_support.py` and `comic168-*` in the same
Codex scratch directory. Source SHA-256 before/after those mutation runs:
helper `4dae3bdad5134598c73133fe801ebd4a6722747f450cc324a84b19fe963f0b3f`,
tests `06fe0b893ced2952e3402b432c5e2aebceee1fbd5fcc8f0ab215ba1c7fc09752`.
Code/tests final at `c8f2995716d2ab9d358ff549d8ffa591c9e57365` (four one-file
commits); subsequent development-log commit has no test/code delta.

No production library, log store, runtime process or security setting was
accessed or changed. Processes remained under the inherited Job Object.
