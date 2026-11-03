[RESOLVED 2026-11-03: local full result and additional guard evidence]

Full clean99e89532c679c6779740820dae7ccfe67004b608 passed5004/12 in315.36s
(-n4 --dist loadfile, translation off). Base166 inventory4994 plus22 added
cases equals5016 total; two new native-context skips account for12 versus10.
No baseline failure occurred in that full run. Follow-up41213248c2fefe17dd1e7404befe68141cc61c23
adds only one real server-close witness, no implementation change. It passed
before mutation and after restoration (final1 passed/22 deselected16.33s).
Three additional mutation groups killed/3 failed executions: publishing session
before startup, skipping server wait for unsettled work, and falsifying native
parent-membership observation. Total11 groups killed/12 failed executions with
overlap; source restored byte-equal in an isolated copy. Final broader focused
checks/CI receipts will be recorded on the exact-head PR. The original checkpoint
below remains historical. D1 stays draft pending genuinely uncontained positive
native lifetime proof; no rollout or production registry enabled.

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
