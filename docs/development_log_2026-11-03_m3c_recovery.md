# M3c slice 3 recovery evidence, 2026-11-03

Owner Codex; independent exact-head review/pinned merge Claude. Delivery3742
ACK confirmed3746, start status3747. Base master
4f9893acae3a0fbb8bc7f40a0ac8c207cac2d240, including merged slice2B91cae2c.
Code/test/protocol checkpoint **ec24e64b918969634fa21cc918eae7b52c2d34a6**,
seven one-file commits. This record adds evidence only. No rollout or live
library/process operation. Tests use disposable private state homes/tools.

## Implemented boundary

See [protocol](web_console_m3c_recovery.md). Fresh same-session native evidence
is sampled under admission exclusion, never accepted as serialized authority.
Full row/nonce/version is fenced before transactional state+event commit.
Receipt import binds command/seal, canonical exact identities/outcome, complete
index and existing bounded segments, with another fresh native sample after
file reads. Only an eligible dead-owner running row can import; repeated import
is idempotent. Durable uncertain never automatically clears. Explicit attributed
operator recheck can acknowledge unknown output only as lost with fresh proof.
Missing recorded child in an uncertain row remains conservative/blocked; no
broad override. M3d supplies authentication/scheduling/routes, M4 adapters/gates.

Orphan exit.json.tmp is preserved and specifically classified during publication
and recovery. Pump startup failure retains ownership, records its specific
post-birth reason, and still attempts to save exact child identity. Native poll
remains250ms; ledger refresh is independently1s. Runner main requires the parent
admission identity environment context, not a new stat of the current path.

## Verification before full-suite checkpoint

- Untouched base4f9893a, own inactive review worktree: full -n4 --dist loadfile
  **4,902 passed /10 skipped /1 failed in349.30s**. The failure is
  tests/web/test_web_logs.py::test_alternating_overflow_compacts_map_and_physical_files,
  os.replace(index.tmp,index.json), WinError5. This independently reproduces the
  reviewer-reported intermittent baseline failure. No claim about its OS cause.
- Focused recovery/runner/live-receipt suite: **164 passed in41.82s**, -n4
  --dist loadfile. Then the context/refresh witnesses were hardened against
  missing-path/timing shortcuts: **2 passed in1.61s**. Added complete-index
  witness and strengthened native birth/temp acknowledgement handshakes:
  **6 passed /21 deselected in7.17s**. Final new recovery inventory is69 cases;
  existing tests were not removed, so full inventory should increase by69.
- All **22 selected mutations killed /30 failing executions**, with overlap;
  every mutated source restored byte-equal. First21 selected cases produced29
  failures; the final incomplete-index bypass produced1. The uncertain-import
  exclusion and two future-time checks are compound boundary bypasses, not a
  claim that each redundant individual check alone releases admission.
- Mutations cover runner/child gone, job absence, foreign name, nonce/version,
  full row recheck, sticky uncertainty, operator intent/proof, orphan handling,
  re-observation after reads, canonical receipt, command/seal binding, actual
  index hash, segment presence, stale/future time, complete-index proof, parent
  identity context, ledger refresh rate and pump-start reason.
- Actual fixture owner death handshakes: claim, native birth with observed live
  child, before receipt, fsynced temp before replace, durable receipt before DB
  terminal commit. Temp-only evidence remains uncertain and preserved; explicit
  operator acknowledgement is separately exercised with real fresh native proof.
  The publication fixture initially tried recursive admission access while the
  publisher held that lock. It was corrected to use captured child identity and
  a read-only diagnostic snapshot; recovery mutation still waits for owner death.
- git diff --check clean. Source imports compile. Full clean candidate suite and
  final-head CI results remain pending at this checkpoint, not claimed passed.

Replay logs/scripts live outside Git under Documents/Codex/control:
m3c_recovery_baseline.log, focused_final.log, hardened.log, extra.log,
m3c_recovery_mutants.py/.log and m3c_recovery_complete_mutant.log (the shorter
log labels here share the m3c_recovery_ prefix). These are evidence artifacts,
not production state. Original failed fixture logs remain for provenance.

## Full clean candidate result

Full suite at clean **2436c09246b0167c2f332faf45c126229e4ec082**:
**4,971 passed /10 skipped /1 failed in519.65s**, -n4 --dist loadfile.
The sole failure is the same untouched alternating-overflow WinError5 at
logs.py:176 os.replace(index.tmp,index.json) reproduced on clean base above.
All69 added tests passed; passing count increased by69 with the same skip
and failure counts. This is not a fully green local-suite claim.

One bounded targeted rerun on each clean tree then passed: candidate
**1 passed in3.79s**; base **1 passed in3.80s**. Those are partial reruns,
not replacement full-suite results and not proof of the underlying OS cause.
Logs m3c_recovery_full.log and m3c_recovery_overflow_candidate/base.log retain
the exception and rerun evidence. No production or log-core fix was bundled.

This append is evidence only; code/test/protocol checkpointec24e64 is unchanged.
Final-head independent review and CI remain required before pinned merge.
