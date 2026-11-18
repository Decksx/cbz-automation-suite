# Development log — 2026-11-18

## Web console: bounded authenticated job history

David's hive #5979 moves focus back to the comic web GUI after the X: move.
Codex owns the M3d D2 read boundary and first Jobs panel, from master c9e8527;
Claude owns independent review and parallel M4a schema/argv groundwork.

Added bounded redacted summaries/events, protected dynamic job routes, console
operation lifetime guards and a Jobs panel with manual refresh and keyset
pages. No tool submission, stop, recheck or acknowledgement is exposed.
`web_console_jobs_read_view.md` specifies the exact response/compatibility scope.

Focused checks: **115 passed**, including two installed-Edge browser tests.
The browser fixture's initial StateDir context-manager assumption and route
glob were corrected; the narrow-screen test also exposed real layout overflow,
fixed with shrinkable grid content and wrapping table cells. The final browser
checks cover real authenticated ledger reads, keyset/event paging, keyboard
focus, 390px layout, untrusted text, error/retry and busy request exclusion.
The 33 new tests plus one newly parameterized static-JS check add 34 to the
base inventory. Twelve in-memory guard bypasses each fail the intended tests:
list limit 2, event limit 1, before cursor 2, WAL change detection 1, event/tool/
outcome redaction 1 each, authentication 1, Host 1, blank query 1, supervisor
lifetime 1 and UI busy reentry 1. No test errors; source hashes unchanged. The
private harness's first substring anchor and UTF-8 output handling were fixed
before the successful complete run. A later table-scroll-only visual polish
passes all **10** browser/static checks. The full local run began before that
last HTML/CSS polish; exact final-head full CI/results are recorded on the PR.
All work uses private disposable state and inherited process
containment, with no live console, library, queue or process-control changes.

## Classify a non-current Camelia source without pausing unrelated books

David's hive #5943 reports a queue stop. Own stable read at 11:53 UTC confirms
book 46467 failed source registration before replacement: its catalog location
was non-current. Claude owns the backed-up repair (#5953); no live data changes
by this implementation.

`SourceLocationRefused` is a specific `HandoffRefused` subclass, raised only at
the existing non-current-source check before any registration writes. The CLI
returns exit 3 and the documented versioned JSON/path contract, retaining stderr
for people. Everything else still fails closed with exit 1. The companion
Camelia consumer must verify that contract before continuing its queue.

Five new CLI cases: preview/apply both retain database and archive bytes; missing
root, missing database and ambiguous same-stat hash remain global failures.
Targeted handoff suite: **13 passed**, base 8 + 5 new.

In-memory guard bypasses execute the permanent CLI assertions against altered
modules, with no tracked-source edits: replacing the typed refusal fails 2;
wrong exit fails 2; wrong schema fails 2; wrong path fails 2; changing ordinary
failures to exit 3 fails 3. All are assertion failures, no test errors. The
first private harness run hit a SQLite fixture cleanup lock; garbage collection
before temporary-folder cleanup fixed that harness, and all five were rerun.
Source hashes stayed unchanged. Full-suite/CI results belong in the PR receipt.

No catalog auto-repair, quarantine, schema migration, archive modification or
runtime launch is introduced. Older consumers pause on exit 3; newer consumers
pause with an older producer. Live rollout stays with the operational owner.
