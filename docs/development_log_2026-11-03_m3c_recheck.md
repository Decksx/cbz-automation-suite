# M3c missing-child recheck follow-up, 2026-11-03

Codex implements delivery3754 (ACK confirmed3756; start3759), Claude reviews
and pinned-merges after exact-head CI. Base master
**a401db054c7e1d2a784792b71184a37b0ac5f004**, verified #165 merge with parents
4f9893a+89f205a and treeb71beb2a equal to the approved source. Claude reported
198 checks and three mutants; those are reviewer evidence, not Codex reruns.
Code/test/protocol checkpoint **91b5ac88083a00312d00c91d0c3bc3fc623b1560**.
No rollout, live library operation, process restart or arbitrary PID termination.

## Agreed change

The old conservative missing-child limit is lifted only for explicit attributed
recheck. If both child fields are absent, exact runner gone plus deterministic
same-session named-job absence proves no surviving contained member, using the
same JOB_LIST/no-breakaway/kill-on-close proof as the accepted claimed-pre-child
crash table. Present jobs, including empty objects, unknown/wrong sessions,
denied observations, foreign stored names and partial identities retain
uncertainty. A recheck still accepts expected nonce/version and nonempty actor/
note, and changes only to lost/unknown. Automatic reconcile stays sticky and
receipt import still requires the recorded child. The prior slice3 evidence
record's missing-child limitation is historical; this follow-up supersedes it.

## Focused proof and selected mutations

- New12 cases on unchanged a401db0 implementation: **10 failed /2 passed /
  69 deselected in6.66s**. This includes the missing escape and previously
  suppressed native job observations. Tests alone were added for that replay.
- After the change, recovery module: **81 passed in40.13s**. The population is
  the prior69 recovery cases plus12 new cases, with no existing cases removed.
- Native witnesses: real present live/empty named jobs retain uncertainty, then
  fresh absence permits explicit lost; wrong recorded Windows session remains
  unknown; known disposable fixture-owner death before birth and before child
  identity persistence allows explicit acknowledgement while auto reconcile
  remains unchanged. Only the fixture Popen owner is killed.
- **Nine selected mutations killed /13 failing executions**, with overlap:
  unrecorded-child permission1, derived uncertain job name1, job absence2,
  native same-session check1, partial-identity boundary2, automatic uncertainty
  exclusion1, operator attribution3, nonce1 and version1. Partial-identity and
  automatic-clearing are compound boundary bypasses; not an assertion that
  each redundant check individually releases admission. Mutated files restored
  byte-equal. External replay: recheck_missing_mutants.py/.log under
  Documents/Codex/control, plus before/focused logs.
- git diff --check clean. Full clean candidate suite and final-head CI pending
  at this checkpoint. Earlier full at2436c09 had4971 passed/10 skipped/1 failed
  (untouched overflow WinError5), with all69 slice3 cases passed. a401db0 has
  identical implementation/tests to that candidate; no fresh full baseline run
  is claimed in this follow-up. The prior source/merge tree receipt is separate
  from a new follow-up acceptance result.

M3d planning follows separately; no routes, scheduler or production adapters
are enabled here. M3d must use this explicit operator path, never an age-based
or automatic uncertain release.
