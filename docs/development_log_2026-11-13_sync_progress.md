# #108 hash estimates and observation progress, 2026-11-13

## Scope and evidence choice

Based on reviewed PR #190 merge
`a4305be47a88555eb3c4792fa14ba3bd5dfe8aad`. Code and tests are final at
`e0d331563757b8741f3fabcf0ed021b15f3c8d2c`; later commits document the change.

Claude's #190 review points out that the size intersection hashes almost
everything when the two libraries are nearly identical. His August catalog
and earlier throughput measurement are his evidence, not a new live inventory
or a guaranteed ETA. This slice retains full-content equality evidence and
exposes candidate logical bytes before payload reads, an estimate-only mode,
and progress during metadata scans and large-file reads. It does not weaken
equality to a path/size presumption or reuse catalog hashes as fresh evidence.
Resume needs an explicit evidence-age/reuse contract first.

The API adds digest-bound `hash_mode` and `hash_plan`. Runtime progress stays
outside the digest. Incomplete initial coverage skips payload hashing. A
consumer failure propagates as `ProgressError`, rather than being mistaken
for an ordinary filesystem error. Events are copied before delivery so a
consumer cannot corrupt the report's nested plan.

The CLI accepts only an explicit `sync-root-pairs-v1` filesystem manifest and
the modes `estimate` and `observe`. It prints JSON to stdout, throttles progress
on stderr, and provides `--quiet`. It has no control or apply operation.
Preview/verifier classification and sanitized configuration evidence remain
later #108 work. A complete estimate supplies no content-equality proof.

## Validation

The predecessor's measured full result was 6,660 passed / 12 skipped, with
6,672 collected cases. This slice adds 27 cases in
`tests/test_sync_observe_cli.py`. Focused execution of that file plus the
52 existing inventory cases passed: **79 passed in 2.08 seconds**.

Full suite on the same code/tests passed **6,687 passed / 12 skipped / 0 failed
in 431.31 seconds** (`-n 4 --dist loadfile`, translation disabled). JUnit
confirms 6,699 cases, 0 errors and 0 failures: exactly 27 more cases than the
predecessor. Both source hashes and the test hash below were unchanged after
the suite. All execution uses temporary local fixtures and inherited
processes; no production library, Syncthing service, backup or configuration
was accessed.

## Mutation checks

An external AST plugin executes each changed function in its original module
dictionary without changing source files. The unmodified control passes all
27 new cases. Each mutation exits with assertion failures, not collection or
harness errors:

| Removed or weakened guard | Failed cases |
| --- | ---: |
| Estimate skips payload hashing | 2 |
| Incomplete initial coverage skips payload hashing | 1 |
| Boolean-only mode flag | 3 |
| Consumer exceptions remain distinct from filesystem failures | 4 |
| Progress dictionaries are independent copies | 1 |
| Within-file byte events | 4 |
| Directory inventory events | 1 |
| Explicit estimate/content mode binding | 3 |
| Correct candidate-byte estimate | 4 |
| Strict top-level manifest schema | 2 |

These are 25 failing executions with overlap, not 25 distinct tests. Scratch
logs and scripts are under `C:\Users\david\.codex\tmp`:
`sync108-progress-mutations.json`, `sync108-progress-mutation-*.txt`,
`sync108_progress_mutation.py` and `run_sync108_progress_mutations.py`.

Source SHA-256 before and after the mutation run:

- `sync_inventory.py`: `40690017e824d8ac952c0e7937e31b73b54f0746e4adaaaa2b2256069e91387f`
- `sync_observe_cli.py`: `58780d06ed9ecccb85d98e63ba39ca9aa1b39ea6b28148656f99c32b48a8d434`
- New test file: `0d667cf6c3e5a3d5a12c572be23ad1c267520213e2a35c6aac02ef6b492c758f`

The mutation driver compares the two source files; the test hash was recorded
separately. The full-suite log and JUnit artifact are
`sync108-progress-suite.txt` and `sync108-progress-suite.xml` in the same scratch
directory. Independent exact-head review and current green CI remain merge
requirements. Issue #108 stays open.
