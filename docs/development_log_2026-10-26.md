# Development log: 2026-10-26

## Web console M1: watcher single-instance ownership

Codex implemented the accepted design's M1 from exact master
`4cf01a81526a06ddc24c95aa6cf6a411a254a0a9` in its own scratch worktree.
Claude owns independent review. Implementation pull request: [#146](https://github.com/Decksx/cbz-automation-suite/pull/146).
The code/test candidate checked below is
`ffd4f1fdd8e7616990d89a98593d15f1a6f15730`; this log is a later documentation-only
commit. Every commit changes one file.

The helper acquires a Windows Global mutex keyed by physical watch-root
device/inode identity. Unknown identity, busy ownership and uncertain kernel
results refuse startup with exit code 3. Local, UNC (Universal Naming
Convention), mapped-drive and junction aliases use the same key. A killed
owner can be acquired with the native abandoned-mutex result.

The watcher owns the mutex before routing, recovery, discovery and observer
startup. Ownership continues through observer shutdown and settle/readiness
callback draining. That cleanup is necessary because the old timers could
outlive the observer. Pending timers are cancelled, late callbacks are fenced,
and already-entered callbacks finish before root ownership is released.
This adds no stop-file API (application programming interface).

### Verification

Windows / C:\Python311 / Python 3.11.3, temporary fixtures only:

- Measured executable/test baseline: **4,295 passed / 10 skipped**, from
  Codex's immediately preceding exact #144 review at `79257a5`. Git tree
  comparisons established that master `4cf01a8` has identical executable/test
  content, plus the separately approved design document. No new baseline
  suite run at 4cf01a8 is claimed.
- New focused file: **32 passed in 8.72 s**. A broader focused run before
  the last three added checks was **98 passed in 10.41 s**; the final full
  suite below includes all final checks.
- Full suite at the code/test candidate: **4,327 passed / 10 skipped in
  216.23 s**, with `CBZ_TRANSLATION_ENABLED=0` and
  `python -m pytest -n 4 --dist loadfile -q`. Exactly **+32 passes**, unchanged
  skips. Timings describe these runs, not a performance guarantee.
- Real-process witnesses passed for local/localhost UNC, temporary `subst`,
  junction, separate source copies, different roots, normal release and a
  crashed holder. A separate witness used two actual Git worktrees at the
  same candidate: second exit 3, then exit 0 after release. The abandoned
  witness retained a non-owning handle so it measured `WAIT_ABANDONED`, not
  creation of a new object.
- OfficePC's executing token was medium integrity, Administrators deny-only,
  with no `SeCreateGlobalPrivilege` listed by `whoami.exe /groups` and `/priv`.
  The native witnesses passed under that token. Cross-account behaviour is
  not measured. No UNC or mapped-drive host skip occurred in this local run.

All **20/20 selected guard mutations** were rejected by named tests:

| Mutation | Failed cases |
|---|---:|
| Replace the physical key with a path hash | 1 |
| Use Local instead of Global namespace | 1 |
| Accept zero identity | 2 |
| Accept a file as the watch directory | 1 |
| Ignore mutex creation failure | 1 |
| Ignore failed/unknown wait results | 3 |
| Reject abandoned ownership | 1 |
| Skip post-acquisition identity recheck | 2 |
| Omit owned-handle release | 4 |
| Omit handle close | 4 |
| Permit recursive embedded watcher ownership | 1 |
| Enter work without root ownership | 1 |
| Discard the script's exit status | 1 |
| Return success for refused startup | 1 |
| Skip tracker cleanup on session exit | 1 |
| Skip active callback draining | 1 |
| Permit a late callback after closure | 1 |
| Permit notify after closure | 1 |
| Permit readiness retries after closure | 1 |
| Permit minimum-age rearming after closure | 1 |

The disposable mutation worktree was restored byte-for-byte after every
case, verified clean and inactive, and removed. Harness and detailed named
failure logs remain outside Git under
`C:\Users\david\Documents\Codex\control\m1-guard-results\`.

The mixed-ending watcher was edited with unchanged line bytes retained:
**2,699 CRLF / 839 bare LF** became **2,758 CRLF / 842 bare LF**. The deltas
are the actual inserted/replaced lines, not whole-file normalization.
The dynamically derived LF-only diff check passes; existing CRLF/mixed
files are excluded as repository policy requires.

### Limits and next owner

`docs/watcher_single_instance.md` records the protocol and rollout limits.
The root and its ancestors must remain stable during a session; M1 does not
pin them against later retargeting. Destination-wide writer exclusion,
cross-account deployment and per-tool interruption safety remain separate.

The running watcher, its settings, live libraries, database, relay and Codex
state were untouched. A pre-M1 process holds no mutex and must be stopped
deliberately before eventual first-live rollout. Claude reviews the final
exact head; merge and runtime activation need their own receipts.

### Final review follow-up: configuration belongs inside ownership

Self-review found that the first M1 candidate still assigned integration
globals before acquiring the mutex. A refused embedded second session could
change the active session's AI/resolver settings. The new regression failed
against that candidate (**1 failed / 33 passed** in the focused run).

Configuration assignments and media-policy save/restore now happen only
inside the owned context. A refusal cannot restore a stale policy snapshot
over another session's initialization. The updated focused run passed
**34/34** (33 new tests plus the existing policy-cleanup case).
The original 20 guard checks are rerun, with a 21st replay of the earlier
configuration ordering. Updated full-suite and exact-head continuous
integration receipts are recorded on #146; the earlier 4,327/10 measurement
above remains bound to its original code/test candidate. The watcher now
has **2,764 CRLF / 842 bare LF**, with the moved configuration block retaining
its original mixed endings.
