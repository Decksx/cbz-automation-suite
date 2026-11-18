# Development log — 2026-11-18

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
