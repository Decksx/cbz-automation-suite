# X: migration cutover runbook (#108, #158)

Status: **draft for review**, claude-lead, 2026-11-15. David's go: hive #5120 ("migrate back to X: today"), #5191 (explicit permission for X:, Z:\media\comics, G:, C:\git and the Tower/PiNAS container configs), with option B (sync, then one full comparison pass). Every step lists what it changes, how to check it, and how to undo it.

## Facts this runbook relies on (measured 2026-11-15)

**Library and backup:**
- Pairs: Manga `wcv4q-z7oqx` (`X:\Manga` and Tower `/mnt/user/media/comics/Manga`, `\\TOWER\media\comics\Manga`), and Comix `wwrge-mushw` (`X:\Comix` and `.../comics/Comix`).
- Read-only estimate: Tower has 77,931 files / 2,783 GiB, and X: has 67,205 / 2,489 GiB. Complete, 0 problems.
- X: has 0.99 TiB free. F: has 3.65 TiB free, and its last comics backup is `F:\comics`, dated 2026-08-04.

**Syncthing (current state):**
- OfficePC: both folders send-only, paused. Comix has staggered versioning; Manga has none.
- Tower `binhex-syncthing` (container state Created, not running): both folders receive-only, paused.
- No `.stignore` anywhere. **Tower Comix has no `.stfolder` marker.**

**Komga and writers:**
- Komga runs on Tower and reads `/mnt/user/media/comics`. It needs no change.
- Original flow, confirmed by the paused config: writers on X:, then Syncthing X:→Tower, then Komga reads Tower.
- The watcher routes to `X:\_staging\{Comix,Manga}`. It holds 100 CBZs (1.2 GB), **none of which are on Tower** by name and size. They're outside the Syncthing roots.
- The watcher and Camelia were stopped by David (#5130), confirmed by both leads (#5142, #5163).

## Phase 0: preconditions (re-check immediately before Phase 2)

1. **Writers stopped.**
   - `C:\git\.claude-tools\camelia_procs.py` must print "not running".
   - `watcher_check.py` must show 0 watcher/gui/comic processes.
   - Any running writer: stop it and re-check.
2. **Both Syncthing instances paused,** with roles as listed above. Never unpause in the current roles: Tower receive-only would treat its newer content as local changes, and Revert/Override would destroy them.
3. **Fresh X: backup on F:** (David runs it, because it outlives our sessions):
   ```
   robocopy X:\Manga    F:\x-backup-2026-11-15\Manga    /E /COPY:DAT /DCOPY:DAT /R:1 /W:1 /MT:8 /NP /XD .stversions /LOG:F:\x-backup-2026-11-15-manga.log
   robocopy X:\Comix    F:\x-backup-2026-11-15\Comix    /E /COPY:DAT /DCOPY:DAT /R:1 /W:1 /MT:8 /NP /XD .stversions /LOG:F:\x-backup-2026-11-15-comix.log
   robocopy X:\_staging F:\x-backup-2026-11-15\_staging /E /COPY:DAT /DCOPY:DAT /R:1 /W:1 /MT:8 /NP /LOG:F:\x-backup-2026-11-15-staging.log
   ```
   - **Check:** per-root file count and bytes on F: equal X: (the estimate's per-side counts), and every log's "FAILED" column is 0.
   - It must not touch `F:\comics` or `F:\ai-decensor-originals`.

## Phase 0b: give Syncthing the same scope as the comparison

All four roots (`X:\Manga`, `X:\Comix`, `/mnt/user/media/comics/Manga`, `/mnt/user/media/comics/Comix`) get the **same** `.stignore`, written while everything is paused.
- It lists exactly the observer's 13 built-in exclusions (`sync_inventory._EXCLUDED` at 2e82a32) plus the temporary-name rule.
- Syncthing then skips what the comparison skips. In particular, the four writer-gate and canonical-naming control files can never be carried between the copies.
- `.stignore` itself is never synced.

```
// Same on all four roots: the #108 observer's built-in exclusions (2e82a32)
(?i).stversion
(?i).stversions
(?i).stfolder
(?i).stignore
(?i)@eadir
(?i)$recycle.bin
(?i)system volume information
(?i)ai-decensor-originals
(?i)ai_decensor_originals
(?i).cbz-library-writers.lock
(?i).cbz-library-writers.owner.json
(?i).cbz-canonical-naming.lock
(?i).cbz-canonical-naming.owner.json
(?i).syncthing.*.tmp
```

- **Check:** after Syncthing starts, each folder's "Ignore Patterns" shows these lines on both devices.
- The comparison stays **filesystem-only** evidence: an XML/`.stignore` read is not a two-boundary API capture (#198), and nothing here claims Syncthing's effective scope.
- Undo: delete the four `.stignore` files.

## Phase 1: merge Tower into X: (temporary direction Tower → X:)

Tower is send-only throughout this phase, so **nothing on Tower can change**. Only X: is written, and X: is backed up.

1. **Tower Comix marker:** `mkdir /mnt/user/media/comics/Comix/.stfolder` (SSH `tower.local`). Undo: `rmdir` it.
2. **Versioning on X::** set both X: folders' versioning to **none** for the merge.
   - X: has 0.99 TiB free, and versions of replaced or removed X: files could fill it. The F: backup is the safety net instead.
   - Record the old values (Comix staggered) to restore in Phase 3.
3. **Flip roles** (folder settings in each GUI, or the config files with the instance stopped):
   - Tower: Manga and Comix → **send-only**.
   - OfficePC: Manga and Comix → **receive-only**.
   - Then re-check: the IDs, paths and shared devices are unchanged.
4. **Start and unpause:**
   - Start the `binhex-syncthing` container. Unpause device Office-PC on Tower and device NAS on OfficePC.
   - Unpause the folders: **Tower first**, so it advertises its index, then OfficePC.
5. **Make Tower authoritative:** Tower's consolidation, decensored files and deletions were recorded as receive-only *local* changes, so the global state may still reflect X:'s old index.
   - If Tower's (now send-only) folders show **"Override Changes"**, click it **on Tower**. This publishes Tower's state as global, which is the intended direction.
   - Never click Override or Revert on Tower while it is receive-only.
6. **Let it sync:** wait until both X: folders say "Up to Date". X: then holds Tower's files. Identical files stay; differing ones are fetched (with blocks reused locally where possible).
7. **Revert Local Changes** on each X: folder. This removes or replaces whatever only X: has; per David, X:-only content is pre-consolidation duplicates, and F: holds it anyway.
   - **Check:** both X: folders are "Up to Date" with no local additions, and Tower shows no changes.

**Rollback for Phase 1:**
1. Pause both folders.
2. Restore X: from `F:\x-backup-2026-11-15` (robocopy back with `/MIR` per folder).
3. Put the roles back to the original (X: send-only, Tower receive-only, paused).

Tower was never written.

## Phase 2: prove X: equals Tower (option B's one full pass)

1. **Pause** both folders, so nothing moves during the comparison.
2. **Comparison pass** (David runs it in his own PowerShell; about a day at the ~37 MB/s measured over SMB for Tower, plus the X: side). Use codex-lead's prepared, fixture-tested commands: `C:\Users\david\.codex\tmp\sync108-live-20261115\OPERATOR_COMMANDS.md`, SHA-256 c32c538f1e53…, section "One full baseline; derive preview without another hash pass".
   - They pin the reviewed observer at 2e82a32 in a clean checkout, and check the manifest's SHA (`pairs.json`, 2e44e52a1f08…: folder IDs `wcv4q-z7oqx`/`wwrge-mushw`, Tower UNC roots, X: roots, no prefixes).
   - They refuse to overwrite earlier output, redirect bytes through `cmd` (PowerShell 5 would write UTF-16), and derive `preview.json` from the saved baseline with no second hash pass.
   - In option B this "baseline" is taken **after** the sync, as the acceptance comparison. It can't prove what X: held before the sync; the F: backup covers that. The same artifact later serves as the expected baseline for a `verify` in the restored X:→Tower direction.
3. **Pass criteria** (all required):
   - `complete=true`.
   - Every Tower file is in `keep_in_place`. `to_write`, `to_remove` and `unresolved` are all empty.
   - `counts.tower.keep.files` = the Tower observed files, and the same for target.
   - Any failure means stay paused, investigate, and do not proceed.

## Phase 3: restore the original direction and the writers

1. **Roles back to the original:**
   - OfficePC: Manga and Comix → **send-only**.
   - Tower: Manga and Comix → **receive-only**.
   - Restore X: Comix's staggered versioning. Keep Tower's Comix versioning as it was.
2. **Unpause** both, then check: both sides "Up to Date", Tower shows **no local changes**, and nothing transfers.
3. **File the 100 staged books** from `X:\_staging` into the library. This is the watcher's normal hand-off, done as an explicit, logged move that refuses occupied destinations. They then reach Tower through Syncthing.
4. **Writers:**
   - The watcher already routes to `X:\_staging`. Confirm Camelia's library root (its ComicAutomation hand-off uses `X:\comix`).
   - Restart the watcher and Camelia.
   - **Smoke check:** drop one known test CBZ into the watcher intake. It lands in `X:\_staging\...` and nothing errors.
5. **Standing rule until the writer-gate rollout:** stop the watcher and Camelia before any rename/cleanup apply (#197 keeps `--attest-quiescent` required).

## What this does not cover

- **Canonical-naming state folders:** `.cbz-canonical-naming\` (journals) and `.cbz-canonical-quarantine\` sit inside a library root once an apply runs. They are **not** in the observer's exclusions, so they'd be compared and synced like books. Follow-up for #108 before any apply on X:. Neither exists in today's roots.

- `F:\ai-decensor-originals` and other backups: untouched.
- The canonical-naming apply on X:, and the writer-gate adapters for the watcher and Camelia: later work, unaffected.
- Syncthing's global index is trusted for the transfer. Phase 2's independent byte comparison is the acceptance evidence, not Syncthing's status.
