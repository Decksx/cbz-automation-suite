# Runner failure evidence — 2026-11-13

## Scope and remaining uncertainty

Progress on [issue #171](https://github.com/Decksx/cbz-automation-suite/issues/171).
The original CI failure at `bc9a7e73864a5a2e12919fc72e5508177728f32a`
reported `uncertain` instead of `succeeded` in
`test_stalled_running_commit_cannot_block_descendant_supervision`, without
retaining the runner's reason. Its cause remains unknown. The earlier passing
repetitions did not diagnose it, and the controlled faults below do not claim
to reproduce that historical failure.

The test's final assertion now retains reason, phase, all supervision budgets,
the final ledger row, a bounded event page, cached native milestone times,
ownership flags, log error/offset/completeness, and log-thread liveness.
Diagnostics run only on failure, after the existing supervision witness has
settled. They do not poll native handles that the runner has already closed.
An unavailable event page is reported as an evidence-read error rather than
masking the original assertion.

The passing path, release/ready handshakes, watchdogs, SHORT limits, uncertainty
policy, production code, and fixture cleanup are unchanged. No production
library, backfill, or model operation was performed. All disposable native
children stayed under the inherited Job Object.

## Controlled verification

Code commit: `d687764068c09fc4a7c0ca62c26be389363acce1`, based on master
`fcf19b143cd39d18f4add50f77b24cd4e5a9de54`. A pytest plugin invokes the exact
witness in separate processes and captures its diagnostic object and traceback.
It does not change any witness handshake or budget.

| Case | Deliberate action | Observed result |
| --- | --- | --- |
| Control | None | Test passed; diagnostic helper was never called. |
| Running commit | Raise OSError after the existing release handshake | Intentional assertion failure; `PERSISTENCE_FAILED:running:OSError`, complete log, native termination/zero milestones, and uncertain ledger events retained. |
| Log publication | Raise OSError on the final complete index publication | Intentional assertion failure; `LOG_UNKNOWN_TAIL:OSError`, incomplete durable log, and running/uncertain events retained. |
| Unavailable event page | Running-commit fault plus PermissionError from events | Original assertion failure retained, with `PermissionError: controlled evidence-page refusal` recorded in the event field. |
| Base assertion | Compile the unchanged base witness in memory, with the same running-commit fault | Intentional assertion failure with no diagnostic object or runner reason in its traceback. |

The verification harness asserts the expected outcome of each case. Intentional
pytest failures above are not counted as passing application tests. Both setup
and cleanup passed in all five final cases. The base comparison proves the
observability change matters, without modifying source files on disk.

The new event-read guard was also bypassed in memory. The unavailable-page
case then failed with PermissionError instead of the original AssertionError,
and no diagnostic object was retained. With the guard present, that case
preserves the original assertion and its other evidence. This is the one new
correctness guard; no production guard was added or changed.

Two early harness checks incorrectly expected exception messages where the
runner deliberately retains only exception class names. The expectations were
corrected to the actual cached contract; source code was unchanged. The final
five-case run and the separate guard-bypass run completed successfully.

External scripts and full raw tracebacks are retained in
`C:\Users\david\.codex\tmp\comic171-evidence`:

- `run_probes.py`: `c94a6e4257bd84a8ea9ea5ff394418f20577ff8f1e6d429e61f00a9b8645a5f0`
- `probe171.py`: `1c49aacef2e31ed5f394b266ce648c8d9622a9893b0342cb960a537ad0ce7da1`
- `check_event_guard.py`: `a9a6df4e9ac876888ba16efba77c66e4cf8002b5c0a95b51bcf81ce1ff33452c`

Run each driver with `C:\Python311\python.exe` from the candidate checkout.
`summary.json` records the five outcomes; the per-case JSON and text files
retain full evidence. The in-memory bypass leaves source bytes untouched.
The working-file SHA-256 for `tests/web/test_web_runner.py` is
`7b9b79385c9183664d5e5841219e11d6f890b4be5cfcdbdbeed6a388cca8a068`.

## Suite and delivery

Full suite at code commit `d687764`, Windows/Python 3.11, translation disabled,
`python -m pytest -q -n 4 --dist loadfile`: **6,597 passed / 12 skipped /
0 failed in 442.21 seconds**. Full output is retained as `full-suite.txt` in
the artifact directory above. Collection separately measured **6,609 cases**.
No test IDs were added or removed; the existing witness's failing assertion
is the only collected-test change. Inventory accounting: the measured #187
source inventory was 6,580, and merged #186 added 29, giving the current 6,609.
This is not a fresh full-suite measurement of the base in this investigation.

The source hash was unchanged after the controlled cases, in-memory bypass,
and suite. `git diff --check` is clean; the test file remains index LF /
working CRLF. The later development-log commit has no executable delta.
Review and CI must bind to the final source head. Issue #171 remains open for
a diagnosed failure or a new evidence-backed hypothesis; this slice closes
the missing failure-context gap only.
