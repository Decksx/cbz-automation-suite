# Development log: 2026-10-27, watcher graceful stop

Codex implements M1b after #147. Claude approved the stop-file integration
contract in hive #3204 and owns independent exact-head review and the pinned
merge on green continuous integration (CI). Base master is
740a7324f40bfc4201ad9e5aff513c29a317c5a3, the #148 merge. Its tree equals
Codex-tested f50df64 (c2d2e3ff14300c15ffc453066d82736dc14ba920): **4,340 passed /
10 skipped in 242.91 s** on Windows/Python 3.11.3, translation disabled, four
workers and loadfile distribution. No redundant new baseline run is claimed.

The optional stop file closes directory admission rather than interrupting a
writing unit. Startup and timer entry paths share the stop latch; pending
timers are cancelled and late callbacks are fenced. Both callback and direct
unit counters drain under M1 ownership. A real observer subprocess stops with
exit 0 after an atomic request. The watcher never removes the request. Polling
storage errors warn once and continue, while a missing startup parent refuses.

Seventeen new tests cover those properties. Focused new file: **17 passed in
3.34 s**. A broader run before the last direct-admission case passed **129**;
the final full suite includes all 17. Protocol and latency/rollout limits are
in docs/watcher_graceful_stop.md. Final suite, named guard mutations and exact
candidate receipts belong on the implementation PR.

Watcher edits are byte-exact: **2,767 CRLF / 842 bare LF** to **2,842 / 865**.
The added/replaced regions account for both deltas; unchanged line bytes and
existing mixed endings are retained. Each commit changes one file. No live
watcher, launcher, intake, library, database, relay or Codex state was changed.
M6 still owns forced-interruption safety; the web package remains Claude's M2.

## Independent review follow-up: two narrow test gaps

Claude's review 6450347316 at 807a69e approved the code and found two surviving
test bypasses. A new direct-entry witness creates the stop request, then calls
the directory gate without any prior timer or explicit stop poll. It proves
the admission poll itself is needed. A second witness uses a real incomplete
ZIP, an expired readiness budget, the real holding rename and sidecar, then
latches stop between that hold and its ready-sibling pass. The ready sibling
must still be processed within the already-admitted unit.

The updated stop-file tests pass **19/19 in 3.85 s**. Both exact reviewer
mutations now fail their named new test (one failure each): admission changed
to admitted = not self._closed, and the hold path re-enters
_process_directory_when_ready. The preceding 15 guard checks are rerun
alongside those two in the separate scratch copy. Final full-suite and
exact-head CI receipts are on PR #149. The earlier 17-case counts above remain
historical measurements. Watcher source and its mixed bytes are unchanged;
the follow-up changes tests and this log only. No rollout.
