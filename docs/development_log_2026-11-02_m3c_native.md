# M3c slice 1 native adapter checkpoint (2026-11-02)

Codex acknowledged relay #3454 as confirmed hive #3463 and started from
merged contract f09fcfcb9bfa276ecc99416aa796a743c7dbfc02. Both additions to the
agreed contract were already folded at7536e03: keep draining/discarding after
writer failure, and explicit fresh-proof acknowledgement of log-only
uncertainty. The next deliverable here is native ownership only; no runner
claim, ledger finalization, HTTP submission or rollout is enabled.

## Candidate and own native evidence

Four one-file commits through **d257487836a442eb39e001612973650942ed50d8**
add win32.py, disposable helper processes,31 native test cases and the API/
measurement protocol. The evidence-only follow-up at9fdc159 leaves source/tests
unchanged at that checkpoint. Full-suite and exact-head CI receipts are recorded on the PR and
in maintained Swarm context rather than claimed complete at this checkpoint.

OfficePC: Python3.11.3 AMD64, Windows10.0 build26200, session1;
TokenIsElevated=0 and TokenElevationType=3 (non-elevated filtered token).
This is an observed token, not an inference from the account name.
A disposable job held two Python processes and two conhost.exe processes,
confirmed with JobObjectBasicProcessIdList and query-only image handles.
Tests verify the witnessed identities and whole-job zero/absence instead of
assuming that a two-Python tree has exactly two native members.

Current native file: **31 passed in7.17s**. An earlier overlapping native/log
run passed76cases in22.25s before five additional native cases were added
(the newer owner-death boundary, three CRT/security failure cases and a
creation-denial case). No old count is relabelled as a current whole-file run.

The native witnesses cover:
- membership in the exact named job at the earliest helper action, correct
  Windows quoting/Unicode/empty args, explicit env and NUL stdin;
- exclusion of an intentionally inheritable sentinel file, private job DACL,
  noninheritable job handle and unchanged existing-object limits;
- native name/type collision and denied creation as uncertain, not absence;
- exact FILETIME/session process observations, reused identity versus access
  denial, same-session absent/present/unknown job queries;
- exit0/7/259 via process signalling, closed-owner refusal before NULL-job
  queries, root exit while a grandchild keeps the output pipe open;
-13 deterministic setup/transfer/create/post-birth failure boundaries, with
  direct native checks on every tracked handle and on process/thread handles
  returned just before an injected wrapper exception;
- owner death both after factory return and while paused after CreateProcessW
  before child identity is returned. Explicit ready/release files are atomic;
  the real root and grandchild are proven gone and the job absent. Failing
  mutants have a fixture release/self-expiry path, with no production PID.

We observed that zero job accounting and a process object's signalled exit
need not be observed at the exact same instant. Tests wait for each required
condition independently; there is no fixed sleep treated as proof.
The pre-identity crash handshake is after the native call returns. No
in-kernel interruption of CreateProcessW is claimed.

## Guards proven load-bearing

Eleven selected mutations cause twelve named failures:
1. omit JOB_LIST: exact membership fails (1);
2. omit HANDLE_LIST: the sentinel file leaks to the child (1);
3. omit KILL_ON_JOB_CLOSE: root/grandchild survive owner death at both
   handshakes (2);
4. bypass existing-job refusal: limit-change spy/collision type fails (1);
5. omit possible-birth classification after an exceptional native return (1);
6. make the job handle inheritable (1);
7. omit wrong-object/inaccessible-name collision classification (1);
8. treat process denial as gone (1);
9. treat job denial as absent (1);
10. ignore the session namespace (1);
11. omit recovery of PROCESS_INFORMATION handles after an exceptional return:
    direct GetHandleInformation finds a leaked native handle (1).

Sources were restored after every mutation and before final tests/commits.
Campaign script remains outside Git at Documents/Codex/control/
m3c_native_mutants.py; the additional raw-handle recovery mutation was run
with the same restore-in-finally pattern. The test source does not mutate
production or real library roots.

## Follow-up acceptance and qualified full run

The full local -n4/--dist loadfile run begun at d257487 completed
**4,737 passed /10 skipped /1 failed in495.81s**. All31 then-current native
cases passed. The only failure is the previously recorded OfficePC
test_alternating_overflow_compacts_map_and_physical_files: WinError5 at
os.replace(index.tmp,index.json), candidate offset442. Log implementation and
this test are unchanged from the M3b baseline; its underlying host cause
remains undiagnosed. No clean local full pass or antivirus/ACL explanation is
claimed. That earlier exception is not hidden by native/CI successes.

Test-only follow-ups strengthen the sentinel check with physical samefile
identity (handling path aliases), remove the remaining fixed process-count
assumption, put fixture self-expiry beyond the bounded proof windows, assert
the owner is actually alive before the deliberate crash, and count native
create calls (zero on setup refusal, exactly one at/after native birth).
An additional real missing-executable case proves no PATH fallback and no
child. Current native file: **32 passed in7.53s**. Product source is unchanged
since d257487; these are helper/test refinements plus this evidence update.

After those refinements, HANDLE_LIST and KILL_ON_JOB_CLOSE mutations were
replayed and again caused1 and2 named failures. Both were restored. The
eleven unique selected mutations/twelve named failures above remain their
measured campaign; repeats do not inflate that count. Final exact-head full
Windows CI and readiness receipts follow on the PR and maintained context.

## Boundaries and next action

This adapter is an internal trusted-caller primitive, not an unvalidated
public command route or M4 registry. A post-birth setup failure closes the
contained job and marks possible birth; it cannot be reported as refused.
Later writing-tool log failures must still follow the agreed keep-alive/
drain policy. Same-user deliberate extra job handles or broker-created work
remain outside the ordinary supported-tree guarantee.

Claude independently reviews the exact head after green Windows CI. Slice2
still owes trusted launch reconstruction, claim/store/receipt integration,
the more-than-pipe-buffer failed-writer witness, descendant/drain timing and
ledger crash recovery. M3d still owns scheduling, routes and explicit recheck.
Native probes write only disposable fixtures. No merge or rollout by Codex.
