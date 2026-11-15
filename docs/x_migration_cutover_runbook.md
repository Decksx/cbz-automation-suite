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
2. **Comparison pass** (David runs it; about a day at the ~37 MB/s measured over SMB for Tower, plus the X: side). Run it from a master checkout that includes #195/#196:
   ```
   python -m comic_automation.library.sync_observe_cli observe --pairs C:\git\.claude-tools\sync_live\pairs-live.json > F:\x-backup-2026-11-15-observe.json
   ```
   Then derive the preview **from that saved observation** (no second hashing pass):
   ```
   python -c "import json,sys; from comic_automation.library.sync_preview import build_preview; print(json.dumps(build_preview(json.load(open(sys.argv[1],encoding='utf-8'))),indent=2,sort_keys=True))" F:\x-backup-2026-11-15-observe.json > F:\x-backup-2026-11-15-preview.json
   ```
   - The manifest uses the real folder IDs `wcv4q-z7oqx` and `wwrge-mushw`, `tower` = the `\\TOWER\media\comics\...` paths, and `target` = the `X:\...` paths. codex-lead confirms it against the configuration evidence (#198) before use.
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

- `F:\ai-decensor-originals` and other backups: untouched.
- The canonical-naming apply on X:, and the writer-gate adapters for the watcher and Camelia: later work, unaffected.
- Syncthing's global index is trusted for the transfer. Phase 2's independent byte comparison is the acceptance evidence, not Syncthing's status.
