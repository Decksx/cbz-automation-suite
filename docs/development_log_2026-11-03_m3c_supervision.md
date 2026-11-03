# M3c slice 2B supervision checkpoint — 2026-11-03

## Follow-up: delayed persistence is not failed proof

This section supersedes the age-only receipt-timeout behavior described in the
initial checkpoint below. Exact250f135 push CI41674738395 failed **2 /4,879
passed /3 skipped**: the normal runner fixture ended uncertain, and an untouched
backup collision/dedup fixture failed. The assertion did not print the runner's
reason, so its actual CI cause is unknown; no timing/WinError diagnosis is
claimed from that output. Claude requested diagnostics and a decision on
age-only persistence in PR comment6609818293.

A deterministic slow-publish witness reproduced the policy concern separately:
holding a successful publisher past the5-second observation threshold caused
PERSISTENCE_TIMEOUT:publish and sticky uncertainty in the old implementation.
The revised runner retains ownership and native supervision, reports waiting,
and waits for the operation's actual result. Successful delayed publication can
finalize only after fresh native/log/receipt proof. A second witness injects
real native uncertainty while publication waits and proves that a late valid
receipt still cannot clear it. Errors, log/drain failures and descendant
deadlines retain their prior conservative rules. No production deadline widened.

Ordinary runner fixtures now use production drain/cleanup budgets rather than
the shortened deadline-test budgets. Only explicit deadline witnesses select
SHORT. Success assertions include reason, phase, log error, budgets and events.
The original CI uncertainty remains undiagnosed unless further output identifies
it. Full/load/guard follow-up results will be appended after verification.

The backup fixture's separate clock-boundary probe passes with fixed identical
ZIP metadata and deliberately reproduces its assertion failure when the third
archive's ZIP entry timestamps cross a2-second boundary. This was measured on
unchanged base20ebcc2; its SHA256-based backup behavior is untouched. It
establishes a fixture sensitivity, not the original CI byte-level cause.

Follow-up verification before the new full run: focused runner **43 passed
in37.32s**. Eight-worker concurrent load selection (runner plus50MiB log capture
and8MiB inspection-count witness) **45 passed in35.32s**. Twenty selected guard
mutants all killed, **23 failing executions with overlap**, including the new
age-is-not-failure witness and a real-native-unknown-during-publication witness.
The latter keeps a late valid receipt from clearing actual uncertainty. Source
restored byte-equal after every mutant. Follow-up replay/output are outside Git:
m3c_supervision_mutants_followup.py/.log and m3c_supervision_load.log in the
same control directory. Backup fixture issue is tracked separately as
[comic #164](https://github.com/Decksx/cbz-automation-suite/issues/164).

The clean follow-up checkpoint **01f522db2583ca817e4ed95cec2ab9b67ca03f97**
passed the complete local suite: **4,875 passed /10 skipped in359.14s**,
Python3.11 with -n4 --dist loadfile -q. The +1 over the original2B run is the
native-uncertainty-during-publication witness; the age-only publication witness
was replaced with the successful-wait expectation. No source/test/protocol
change follows this checkpoint, only this evidence append. Final-head CI and
fresh independent review are still required. Output is retained outside Git as
m3c_supervision_followup_full.log. Neither historical CI failure reproduced in
this run; their original causes are not established by a later pass.

Owner: Codex. Independent reviewer/next owner: Claude. Base master
`36a1b82cb9dbdd3d890e55b95fec6d1159592de7` (merged slice 2A #162).
Delivery #3636 acknowledged as confirmed hive #3639; plan confirmed #3641.
Implementation/test/protocol checkpoint:
**a18ae548266259e02918658c31364385bd10490b**, eight one-file commits on
`feat/web-m3c-supervision`. Pushed checkpoint confirmed in hive #3654 before
the per-run deadline. No deployment, production submission or live-library use.

The [protocol](web_console_m3c_runner.md) describes the final interfaces,
budgets, deferred production adapter/stop gates and unsettled ownership rules.
It implements one live claimed launch and fresh interpreter/source/registry
seals, native polling independent of ledger/receipt persistence, root-exit
descendant deadlines independent of EOF, bounded cancellation attempts and
sticky uncertainty. Both stored and receipt job names use the native helper.
Production reconstruction remains absent; the CLI accepts only job ID/nonce
and refuses an eligible claim before birth without a trusted adapter.

## Focused and guard evidence

`C:\Python311\python.exe -m pytest tests/web/test_web_runner.py -q`:
**42 passed in 26.64 seconds**, on the final checkpoint's product/test code.
Fixtures include real private state homes, actual Windows contained Python
children and real reader/writer threads. Explicit ready/release handshakes bind
writer failure followed by 4 MiB of output, stalled I/O, live writing-tool stop
intent and owner death after claim, after birth/before running commit, and
after durable receipt/before terminal commit. Fake monotonic time only advances
deadline witnesses; it is never evidence that a native process has exited.

Nineteen selected single-guard mutants were all killed by the named focused
witnesses, **22 failing executions with overlapping populations**. They cover:

- reconstructed seal, actual input-byte hashes, registry command copying,
  second fresh reconstruction, and frozen environment;
- possible-birth and collision classification;
- fresh empty-job proof, root-exit grace, refusing an EOF-gated grace and
  the termination-proof deadline;
- not aborting the reader on writer error, retaining unsettled cleanup,
  sticky receipt-timeout and recovered-writer EOF policy;
- pre-birth and live stop intent, and avoiding native observation races during
  and after terminal commit.

All mutated files were restored byte-equal after each subprocess; checkout was
clean before the final full suite. Replay runner/output remain outside Git at
`C:\Users\david\Documents\Codex\control\m3c_supervision_mutants.py` and
`m3c_supervision_mutants_final.log`. These are selected guard bypasses, not a
claim of exhaustive mutation coverage.

During development, new witnesses exposed a shared read-refresh/uncertainty-
retry timer and redundant observations after terminal commit. The final code
separates those timers and serializes terminal proof/commit/resource release.
The recovered-writer witness now holds its child until recovery is observed,
so a short cleanup budget is not mistaken for a recovery handshake.

## Known Windows baseline failure

An earlier related focused run stopped with **142 passed /1 failed** on
`test_alternating_overflow_compacts_map_and_physical_files`: WinError 5 from
`os.replace(index.tmp, index.json)`. The replacement code is unchanged here.
The clean old receipt worktree at `e07b94dd60eb5ebecb03826e62eb8e215a2ce869`
has the **same tree** as base master36a1b82, and its logs.py diff against that
base was empty. One initial isolated baseline pass was followed by a bounded
repeat: three passes, then the same WinError 5 on run four. This establishes
pre-existence, not its cause; no Defender/ACL/sharing diagnosis is claimed.
The exception was recorded rather than adding a speculative retry to production.

## Full suite and remaining scope

The full suite was started on the clean checkpoint with
`C:\Python311\python.exe -m pytest -n 4 --dist loadfile -q`; its result will be
appended after completion. Slice 2A's local measurement at `7c853b7` was
4,832 passed /10 skipped (its product/test code is unchanged in base36a1b82);
this checkpoint adds 42 cases (expected 4,874 /10
when all pass). Windows CI uses a different skip population; record its actual
count and tested commit separately.

Slice 3 still owns dead-runner observation/import and orphan exit.json.tmp
handling. M3d owns detached server spawning, recovery scheduling and explicit
operator recheck/routes. M4 owns trusted production builders, outcome identities
and accepted writing-tool interruption gates. No automatic uncertain release,
fake production registry, HTTP execution route or rollout is included.

## Completed verification

On clean checkpoint **a18ae548266259e02918658c31364385bd10490b**, the full local
suite completed: **4,874 passed /10 skipped in 376.92 seconds**. This is the
previous product/test baseline plus exactly 42 new runner cases. The previously
reproduced WinError 5 did not occur in this run; it is not diagnosed or fixed.
Full output remains outside Git at
`C:\Users\david\Documents\Codex\control\m3c_supervision_full.log`.

Push CI [41673108329](https://github.com/Decksx/cbz-automation-suite/actions/runs/41673108329)
on exact **a18ae548266259e02918658c31364385bd10490b** is SUCCESS:
**4,881 passed /3 skipped in 417.04 seconds**. This CI host executes seven cases
skipped locally. This evidence record is the only subsequent file change;
product, fixture, test and protocol code remain unchanged. The final candidate
still needs its own exact-head CI and Claude's independent review before merge.
