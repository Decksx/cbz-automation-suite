# M3b CI capture-test correction (2026-11-02)

Codex resumed from the clean night checkpoint at
`f0b709f53bc3598d50e12381e45a6ead551b1446` under David's restart instruction
(relay #3394, confirmed ACK #3396 and start status #3397). Claude owns fresh
exact-head review of #156 and the later pinned merges of #156 then context
#183. The previous local verification remains its own measurement; this note
supersedes the old pending-CI state and explains the failed CI result.

## Evidence and cause

Exact-head run [40616463908](https://github.com/Decksx/cbz-automation-suite/actions/runs/40616463908)
failed one test, with **4,703 passed / 3 skipped / 1 failed**. Its 50 MiB
fixture called `LogPump(...).start().join(30)` immediately. At the assertion,
`PumpResult` had `settled=False`, `complete=False`, `error=None`, read offset
**37,879,808**, durable offset **37,355,520**, versus **52,428,800** expected.
The read/durable difference is exactly the fixture's eight-chunk credit window.
The test's call duration was 30.04 s. The successful companion was
[40616450549](https://github.com/Decksx/cbz-automation-suite/actions/runs/40616450549).

`LogPump.join` deliberately cancels on timeout. It is a final-drain/cleanup
operation, not an unlimited workload-completion wait. The test used its maximum
30-second cleanup bound as a requirement to persist an entire 50 MiB workload.
Slower healthy fsync throughput therefore caused the fixture to cancel itself
before EOF. The observed counters and lack of an I/O error match that path;
this is a test-lifecycle error, not evidence that cancellation should be disabled
or that production timeouts should be raised.

Controlled replay patched only `LogDisk.append` in a separate process:

```python
def healthy_slow_append(self, *args):
    time.sleep(0.05)  # 800 chunks imply >=40 seconds of healthy extra work
    return original_append(self, *args)
```

- Original f0b709f witness: **1 failed in 50.51 s**, `error=None`, read
  **30,343,168**, durable **29,818,880**. This reproduces cancellation of a
  still-progressing healthy capture without inducing write failure or overflow.
- Corrected fixture at 3fa4c7a: **1 passed in 102.09 s**, exact 50 MiB persisted-byte
  hash, no gaps, and restart paging checks all retained. Durations include host
  load and harness overhead; this is a causal fault-injection comparison, not a
  storage benchmark or throughput guarantee.

Replay code remains outside Git at Documents/Codex/control/
`m3b_ci_slow_repro.py`; all filesystem targets were temporary fixtures.

## Test-only correction

Single-file commit **3fa4c7aab2e71540522b831c151aca939f0b1fbc** changes
`tests/web/test_web_logs.py`. Its fixture waits for both finite-workload threads
under one independent 180-second test watchdog, then calls the bounded 5-second
cleanup join. The artificial credit wait uses the same test watchdog rather
than imposing an unrelated 5-second healthy-storage deadline. Failure cleanup
releases Python fixture waits, requests cancellation, and uses bounded joins;
it does not claim an unsettled writer succeeded.

The new deterministic witness
`test_capture_fixture_waits_for_workload_before_cleanup_deadline` keeps a writer
blocked until the fixture explicitly waits for it. The cleanup spy refuses an
early pump join. Bypassing only that completion wait produces **1 named failure
in 1.78 s**; source bytes were restored in the isolated scratch worktree. The
existing `test_join_timeout_is_not_a_successful_eof` still proves the product's
timeout is real and cannot manufacture EOF.

Focused new ordering test, existing cancellation test and normal 50 MiB witness:
**3 passed in 16.25 s**. No production source changes: `git diff f0b709f --
comic_automation` is empty. The earlier 21 product-guard campaign remains bound
to its original candidate; this turn does not relabel it as a new campaign.

## Final verification and handoff

At test-only head **3fa4c7aab2e71540522b831c151aca939f0b1fbc**:

- Both complete Windows CI runs passed **4,705 tests / 3 skipped**:
  [41491510758](https://github.com/Decksx/cbz-automation-suite/actions/runs/41491510758),
  **358.69 s**, and
  [41491498875](https://github.com/Decksx/cbz-automation-suite/actions/runs/41491498875),
  **348.01 s**. One new regression adds one case to the original population;
  the seven extra local symlink-privilege skips run successfully in CI.
- Current focused log file: **50 passed in 31.49 s**; this includes the fixed
  50 MiB witness and the unchanged product timeout/unknown-EOF witnesses.
- The local full run completed **4,697 passed / 10 skipped / 1 failed in
  425.51 s**. The corrected 50 MiB case passed in 19.02 s. The unrelated
  `test_zero_writes_do_not_leak_empty_segments` encountered `PermissionError:
  [WinError 5]` at `os.replace(index.tmp, index.json)`, offset31.
- An unmodified f0b709f scratch checkout independently reproduced the same
  zero-write/index-replace access-denial failure, **1 failed in 26.98 s**, at
  offset98. No fault injection was used for that baseline check. The code and
  this test body are byte-identical between the heads. Its focused rerun on
  the corrected head passes within the 50-case result above. The origin of
  the host access denial was not established; no antivirus, ACL, filesystem or
  production retry changes are claimed as a fix for it. No clean local full
  pass is claimed for this turn, and it is not hidden by the CI successes.

The final evidence commit adds only this document after the measured test
head. Updated-head CI/readiness receipts are recorded in the PR handoff.
Both temporary scratch worktrees were clean and inactive before removal.

Both PRs stay draft until the updated exact heads pass CI. No merge, live
archive/data operation, watcher/GUI rollout, production run route, relay
configuration or Codex-state edit is part of this correction.
