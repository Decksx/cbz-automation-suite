# X: migration cutover runbook (#108, #158)

Status: **draft for review, revision 3**, claude-lead, 2026-11-15.
- **Authority:** David's #5120 ("migrate back to X: today") and #5191 (explicit permission for X:, Z:\media\comics, G:, C:\git, C:\temp and the Tower/PiNAS container configs).
- **Path:** option B (sync, then one full comparison pass).
- **No backup of X: (David #5278/#5281):** X:'s content isn't needed, so anything only X: holds is discarded by design. There is no restore-from-backup rollback.
- **This revision** answers codex-lead's #199 review: index authority and step order, Camelia's old-root queue, space, and no absolute claims about Tower.

## Facts this runbook relies on (measured 2026-11-15)

**Library:**
- Pairs: Manga `wcv4q-z7oqx` (`X:\Manga` and Tower `/mnt/user/media/comics/Manga`, `\\TOWER\media\comics\Manga`), and Comix `wwrge-mushw` (`X:\Comix` and `.../comics/Comix`).
- Read-only estimate: Tower has 77,931 files / 2,783 GiB, and X: has 67,205 / 2,489 GiB. X: has 0.99 TiB free.
- codex-lead's space budget: 26,017 Tower paths absent on X: (0.552 TiB), and an illustrative peak of 0.671 TiB with replacement temporaries.

**Syncthing (current state):**
- OfficePC: both folders send-only, paused. Comix has staggered versioning; Manga has none.
- Tower `binhex-syncthing` (container state Created, not running): both folders receive-only, paused.
- No `.stignore` anywhere. **Tower Comix has no `.stfolder` marker.**
- None of the control files or maintenance folders exist in the four roots.

**Komga and writers:**
- Komga runs on Tower and reads `/mnt/user/media/comics`. It needs no change.
- Original flow: writers on X:, then Syncthing X:→Tower, then Komga reads Tower.
- The watcher routes to `X:\_staging\{Comix,Manga}`: 100 CBZs (1.2 GB), none of them on Tower. They're outside the Syncthing roots.
- **Camelia's backlog** (codex-lead, read-only, 20:02:55 UTC): 13,621 queued rows, all with `root_path=Z:\comics\Comix` and a `Z:` `source_path`. All 39,140 rows use that root. Processing is `output_mode='replace'`.
- The watcher and Camelia were stopped by David (#5130), confirmed by both leads (#5142, #5163).

## Phase 0: preconditions (re-check immediately before Phase 1, and again before Phase 2)

1. **Writers stopped.**
   - `C:\git\.claude-tools\camelia_procs.py` must print "not running".
   - `watcher_check.py` must show 0 watcher/gui/comic processes.
   - Camelia and the watcher stay stopped, with no autostart, until Phase 3 step 4.
2. **Both Syncthing instances paused,** with roles as listed above. Record each instance's running version.
3. **Tools pinned to merged master b9cfa58** (#197, #198 and #200).
   - The observer's exclusion policy then includes both maintenance folders. The operator commands' pin (codex-lead's `OPERATOR_COMMANDS.md`, currently 2e82a32) must be refreshed to b9cfa58 before Phase 2.
   - The `.stignore` below must equal that policy.

## Phase 0b: give Syncthing the same scope as the comparison

All four roots get the **same** `.stignore`, written while everything is paused.
- It lists exactly the observer's built-in exclusions at b9cfa58 (`sync_inventory._EXCLUDED`: 15 names) plus the temporary-name rule.
- Syncthing then skips what the comparison skips. The writer-gate and canonical-naming control files and the apply journal and quarantine folders can never be carried between the copies.

```
// Same on all four roots: the #108 observer's built-in exclusions (b9cfa58)
(?i).stversion
(?i).stversions
(?i).stfolder
(?i).stignore
(?i)@eadir
(?i)$recycle.bin
(?i)system volume information
(?i)ai-decensor-originals
(?i)ai_decensor_originals
(?i).cbz-canonical-naming
(?i).cbz-canonical-quarantine
(?i).cbz-library-writers.lock
(?i).cbz-library-writers.owner.json
(?i).cbz-canonical-naming.lock
(?i).cbz-canonical-naming.owner.json
(?i).syncthing.*.tmp
```

- **Check:** each folder's "Ignore Patterns" shows these lines on both devices.
- The evidence stays **filesystem-only**: no two-boundary API capture (#198) has been made, so nothing here claims Syncthing's effective scope.
- Undo: delete the four `.stignore` files.

## Phase 1: make Tower authoritative, then let X: receive

Tower is the source of truth throughout this phase.
- Send-only means Tower never *pulls* remote changes. Its own scans still update its index, and anything writing to Tower locally (none should be running) would still change it.
- X: only receives once Tower's state is published and confirmed.

1. **Tower Comix marker:** `mkdir /mnt/user/media/comics/Comix/.stfolder` (SSH `tower.local`). Undo: `rmdir` it.
2. **Space guard on X::**
   - Set both X: folders' **versioning to none** for the merge. The record of the old values (Comix staggered, Manga none) goes in this runbook's run log, for restoration in Phase 3.
   - X:'s replaced or removed files are not needed (David #5278), and versions would consume the space the merge needs.
   - Confirm `minDiskFree` on X: (default 1%, about 37 GiB). Keep at least 250 GiB of headroom over the 0.67 TiB illustrative peak.
   - If X: free space falls below 150 GiB during Phase 1: **pause** the X: folders, report, and resolve before continuing.
3. **Tower → send-only, X: stays send-only.**
   - Change Tower's two folders to send-only. Leave OfficePC's as send-only.
   - Start the `binhex-syncthing` container, unpause the devices, and unpause the folders on both sides.
   - Neither side can now apply the other's changes.
4. **Fresh scans and index exchange on both sides.**
   - Trigger a full scan of each folder on each device (GUI "Rescan", or `POST /rest/db/scan?folder=<id>`).
   - Wait until each folder's state is idle, with no scan or pull errors.
   - Save the `GET /rest/db/status` output for every folder on both devices as witnesses: `globalFiles`, `localFiles`, `needFiles`, `errors`.
   - Both devices must report each other as connected and up to date on index exchange.
5. **Publish Tower's state as global.**
   - On **Tower only**, if a folder shows "Override Changes", click it. Override enforces Tower's *local index* (just refreshed by step 4) over the cluster's.
   - Don't infer anything from a missing button. Re-read `db/status` and confirm Tower's `localFiles`/`localBytes` equal its `globalFiles`/`globalBytes`, and that Tower `needFiles` is 0.
   - **Never** Override or Revert on X: in this step.
6. **Confirm the global state matches Tower.**
   - Compare Tower's global counts with the read-only estimate (77,931 files, minus anything excluded by the `.stignore`), allowing for any differences the scan surfaced, and record them.
   - Spot-check a few known Tower-only additions and a few known X:-only paths. The former must be in the global index; the latter must not be in Tower's local state.
7. **X: → receive-only.** Change OfficePC's two folders to receive-only. X: now pulls everything it needs from the global (Tower) state. Watch free space per step 2. Wait until both X: folders say "Up to Date", with no errors.
8. **Revert on X: only.**
   - X:'s remaining differences are local additions only it has: pre-consolidation duplicates, not needed (David #5278).
   - Click "Revert Local Changes" on each **X:** folder. Afterwards `needFiles` is 0 on both sides, and X: shows no local additions.

**Abort or rollback in Phase 1:**
- Pause the X: folders. Tower is unaffected by X:'s state.
- Restarting the phase from step 3 is safe.
- There is no backup to restore X: from, by David's decision.

## Phase 2: prove X: equals Tower (option B's one full pass)

1. **Pause** both folders, so nothing moves during the comparison. Re-run the Phase 0 writer check.
2. **Comparison pass.** David runs it in his own PowerShell; it takes about a day at the ~37 MB/s measured over SMB for Tower, plus the X: side.
   - Use codex-lead's prepared commands, `C:\Users\david\.codex\tmp\sync108-live-20261115\OPERATOR_COMMANDS.md`, section "One full baseline; derive preview without another hash pass", **re-pinned to b9cfa58** (Phase 0 step 3), with the actual-ID `pairs.json`.
   - They check the code pin and the manifest SHA, refuse to overwrite earlier output, redirect bytes through `cmd`, and derive `preview.json` from the saved baseline without a second hash pass.
   - In option B this baseline is taken **after** the sync, as the acceptance comparison. It doesn't prove what X: held before, which by David's decision isn't needed. It serves later as the expected baseline for a `verify` in the restored direction.
3. **Pass criteria** (all required):
   - `complete=true`.
   - Every Tower file is in `keep_in_place`. `to_write`, `to_remove` and `unresolved` are empty.
   - `counts.tower.keep.files` equals Tower's observed files, and the same for target.
   - Any failure means stay paused, investigate, and do not proceed.

## Phase 3: restore the original direction and the writers

1. **Roles back to the original:**
   - OfficePC: Manga and Comix → **send-only**. Restore X: Comix's staggered versioning from the run log.
   - Tower: Manga and Comix → **receive-only**, with Tower Comix keeping its staggered versioning.
2. **Unpause.** Scan both sides. Check: `needFiles` is 0 everywhere, Tower shows **no local changes**, and nothing transfers.
3. **File the 100 staged books** from `X:\_staging` into the library. This is the watcher's normal hand-off, done as an explicit, logged move that refuses occupied destinations. They then reach Tower through Syncthing.
4. **Camelia's old-root queue, before Camelia runs again.**
   - With autostart disabled, identify the runtime backlog DB that Camelia will actually use, read-only and WAL-aware.
   - List its queued rows whose `root_path` or `source_path` is on `Z:`. Codex's snapshot found 13,621.
   - **Hold** them via the supported `BacklogStore.edit_queue_books(ids, "hold")`, in batches of ≤100. Read back each batch's status.
   - Only then start Camelia, with its library root set to `X:\Comix`.
   - Confirm a newly claimed job's input and output are under `X:`, never `Z:`. Writing on Tower would now create receive-only local changes there.
   - Migrating the held jobs to X: paths is follow-up work, not part of this cutover.
5. **Watcher:**
   - It already routes to `X:\_staging`. Restart it.
   - **Smoke check:** drop one known test CBZ into the intake. It lands in `X:\_staging\...`, nothing errors, and Tower is untouched until Syncthing carries it.
6. **Standing rule until the writer-gate adapters ship:** stop the watcher and Camelia before any rename/cleanup apply (#197 keeps `--attest-quiescent` required).

## What this does not cover

- **Writer gate:** the gate adapters for the watcher and Camelia (B3 slices 3-5).
- **Held Camelia jobs:** migrating them to X: paths.
- **`F:\ai-decensor-originals`** and other backups: untouched.
- **Syncthing's own status** is a progress indicator. Phase 2's independent byte comparison is the acceptance evidence.
