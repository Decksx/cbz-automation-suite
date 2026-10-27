# Development log: 2026-10-27

## Watcher cleanup diagnostics (#147)

Codex implemented the separate follow-up after M1 merged as
fb6ef7dbc8a45d16b15103bff5f5dae8e11f32b6. Claude reviews the exact final PR
head and owns the pinned merge on green continuous integration (CI). No rollout
or running watcher change is part of this work.

A failed mutex release used to replace an active session exception, which main
then classified as startup refusal / exit 3. Cleanup-only failures now have a
separate exception and exit 4. When there is an original failure, it propagates
with the cleanup diagnostic logged and attached as a note. Handle close is
always attempted; uncertain cleanup retains process-local root exclusion.
The same treatment covers failed CloseHandle results, which were unchecked.

Measured baseline at fb6ef7d: **4,328 passed / 10 skipped in 232.40 s**, Windows,
C:\Python311 (3.11.3), CBZ_TRANSLATION_ENABLED=0, four pytest workers with
loadfile distribution. Nine initial regressions failed against the old
implementation, then passed with the fix. Final coverage also includes primary
body errors with every injected cleanup failure, policy restoration, a handled
outer exception, actual interpreter diagnostics and exit status, and embedded
retry refusal. Final suite and named guard results are recorded on the PR.

The watcher was edited byte-exactly; its existing mixed line endings are
retained. The CLI (command-line interface) remains unchanged except for cleanup
error classification. Protocol and accepted limits are in
docs/watcher_single_instance.md. Graceful stop-file support is a separate M1b PR.
