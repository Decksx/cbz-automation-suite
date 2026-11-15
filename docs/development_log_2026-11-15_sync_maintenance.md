# Local maintenance folders outside migration payloads (#108)

Claude's hive #5218 identified that canonical-apply journals and quarantine
originals would otherwise be observed and synced like books. Codex took this
follow-up in confirmed #5263, from comic master `63f4c1b` (the #197 merge).

`sync_inventory` now excludes the reserved `.cbz-canonical-naming` and
`.cbz-canonical-quarantine` components at every depth, case-insensitively, on
both sides. This covers maintenance inside a series as well as a library-root
run. The existing walk records the boundary without descending or hashing it.
Similarly prefixed names remain ordinary content. No live ignore file or
library payload was changed by this source work.

The policy grows from 13 to 15 component names. The configuration binder's
existing dynamic policy requires the expanded root/nested/descendant rules
for both names. Missing rules fall back to filesystem-only evidence. Old
observations retain their old policy and digest: an old-policy baseline cannot
verify under the new policy, even with identical payloads. Pin the reviewed
code version for fresh baseline and verification, and refresh the runbook's
operator pin and ignore rules before the long acceptance collection. Do not
edit or reseal old artifacts to make them appear current.

The new test file adds 13 cases: eight root/nested/case boundary witnesses,
two preview/verification witnesses with asymmetric local maintenance state,
two effective-ignore witnesses, and one real old-policy/new-policy comparison.
The boundary witnesses also keep similarly prefixed payload folders visible.
Focused inventory/preview/verify/config/CLI suite: **249 passed in 6.77 s**.

In-memory mutations, without changing source files: control **13 passed**;
remove only the journal exclusion **6 failed / 7 passed**; remove only the
quarantine exclusion **6 failed / 7 passed**; restore the old policy by removing
both **13 failed**. Details remain outside Git at
`C:\Users\david\.codex\tmp\sync108-maintenance-mutants.json`.

Full suite at `bb3b18fbcf1810ec56805ae496cfa1b8d160eb13`:
`python -m pytest -q -n 8` gives **6,952 passed / 12 skipped / 0 failed in
242.43 s**. The **6,964** collected cases reconcile to merged base **6,939
passed / 12 skipped**, plus these **13** new cases. JUnit remains outside Git
at `C:\Users\david\.codex\tmp\sync108-maintenance-full.xml`. Source and test
files remained unchanged through mutations and the full run; this final log
commit changes no executable behavior. The complete diff passes `git diff
--check`.

Independent exact-head review and fresh CI are required before merge. Issue
#108 stays open; Claude owns the runbook update and operational changes. No
live transfer, configuration change, or baseline collection is implied.
