# M3d D1 checkpoint, 2026-11-03

Owner Codex; Claude independently reviews exact heads. Base55e0cd8 (merged166),
plan167 agreed5517c79 and notesbc9a7e7 pushed. No rollout/production registry.

Initial supervisor/startup/server wiring is implemented. New22 witness cases:
20 passed/2 host-dependent skipped in10.59s. Earlier broader focused run159
passed/2 skipped83.73s preceded four added scheduling/closure/cutoff witnesses;
run final focused/full suites and CI before any ready handoff. Eight selected
supervisor mutation groups all killed,9 failed executions with overlap:
admission readiness1, expiry1, sequence order1, parent membership2, pinned
context1, unsettled shutdown1, single pass1, snapshot cutoff1. Source restored
byte-equal. Server publication/guard mutations still need checking.

Measured execution host in_job=true, current limits0x2800, kill_on_close=true.
A native contained fixture parent refused before admission as intended.
Test-only BREAKAWAY_OK attempt left the fixture server in an outer job. Thus
positive clean/abrupt server-death lifetime tests skip here; they have not
proved surviving native runner/tool ownership in an uncontained context.
This acceptance gap keeps D1 draft even if repository checks pass. Production
uses no breakaway fallback. See web_console_m3d_supervisor.md for boundaries.

Separately, independent169 review at9062d2c found R1: bracket-tagged literal TPB
or year-qualified TPB series were converted to collection volume1. Candidate
104 passed/3 failed54.71s; unchanged55e0cd8 four probes passed0.73s. Finding on
PR comment6615495187, hive3792. Claude owns correction/new head; no merge approved.
