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
