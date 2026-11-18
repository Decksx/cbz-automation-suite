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

## Browser transport mitigation and failure evidence (#207)

PR #206 exact 2596e9f fails integration run 44346736469 twice: a second-tab
navigation reports ERR_NO_BUFFER_SPACE, then the same-origin POST helper
reports Failed to fetch. Causes remain unclassified. There is no third blind
rerun. Upstream Playwright 43545/43605 supplies a concrete Windows TCP-port-
randomization mitigation hypothesis, documented in web_browser_diagnostics.md.

The CI-only pin moves 1.48.0 -> 1.65.1. Effective installed-Edge 157.0.4341.60
launches show the mitigation absent/present respectively, one upstream feature
list and web security enabled. The added real-launch regression fails as an
assertion on 1.48.0. All 21 existing browser/server tests pass with the new
driver, using ordinary C:\Python311 and a private package directory on this
command's PYTHONPATH. A venv-interpreter setup attempt timed out at server
session readiness; it is not a compatibility result. Global Python is unchanged.

The web-local failure hook collects bounded classified request outcomes plus
owned-server liveness and drained-pipe summaries. No raw credentials, URLs,
headers, bodies, commands or pipe text enter this added section. Existing
pytest traces still need private review before publication. All inherited
browser security assertions remain; no retry, OS socket change or live launch.

Twelve added cases (11 helper/hook cases and one real-browser guard) bring
collection from 7,107 at base 887b583 to 7,119. Focused diagnostic/browser:
14 passed. Thirteen in-memory mutations fail 1/1/4/1/2/1/1/1/1/1/1/1/2
assertions, zero test errors; tracked source hashes unchanged. The first private
mutation run exposed a missing known-route assertion; coverage was strengthened
and all 13 rerun. A deliberately failed unchanged sign-in test yields 41
classified events and a live server answering health 401; pipes were empty in
that sample. A separate pipe regression drains 120,000 bytes and catches the
disabled drainer. The private witness-output parser's initial shell quoting
error was corrected in a standalone script; the existing witness output then
validated without another browser run.

Code-final 012f760 plus #206 combines without conflict to tree 3f447b4b513167bbebdbbaac4b9fa94dc32a2fbd.
Thirty-five selected tests pass in four-worker/loadfile mode, 23.11 s; tree
unchanged. This is not a full combined-suite claim. Full mitigation-suite and
CI results belong in the PR receipts. Temporary comparison copies are removed
after tests, inactive and unbound; both active implementation copies remain.
No source or assertion in the Jobs implementation is changed by this branch.
