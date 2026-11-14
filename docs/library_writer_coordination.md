# Library writer coordination (#158 B3), design note

Status: **proposed, revision 3** (claude-lead, 2026-11-13).
- Revision 1 was 181da1e.
- Revision 2 (f266f17) answered codex-lead's R1–R4 and adopted the answers to the four questions. codex-lead accepted R1, R3 and the registered-root part of R4, and cleared slice 1 to proceed on fixtures.
- Revision 3 makes Camelia's private-staging boundary explicit (R2) and pins directory-only series keys and the four control filenames. No code yet. It touches the watcher, Camelia, canonical apply and web console M6.

## Problem

Three kinds of process write the same library tree today, and none of them can see the others:

| Writer | How it writes | Exclusion it has today |
| --- | --- | --- |
| Watcher (`scripts/cbz_watcher.py`) | Lands new books and upgrades into series folders: per-file `_settle_or_land`, plus whole-folder `os.rename` in `_move_cbz_dir`, `_backfill_chapter_one`, `_land_deferred_names`, collision quarantine and rollback | A kernel mutex per **watch root** (`watcher_instance_lock`) and in-process per-destination locks (`_destination_locks`) around `_settle_or_land` only, plus a graceful stop file (M1b). None of these is visible to another program. |
| Camelia backlog (`_related/autodecen/api.py`) | Reads and extracts the source, runs inference (minutes), rebuilds to a temp file, **copies** the original to the backup folder, then `os.replace`s the temp over the source in place under `X:\comix` | `camelia-processing.lock` in `%TEMP%`, which serializes Camelia with itself only. `assert_overwrite_allowed` enforces processed-marker policy, not exclusion. |
| Canonical apply / recover / undo (`canonical_apply`) | Renames and quarantines across the root | The OS scope lock `<root>\.cbz-canonical-naming.lock`, taken by mutators only, plus a quiescence hook that today is the operator's `--attest-quiescent`. |

`cbz_library_maintenance.py` (the GUI's maintenance workflow) is a fourth writer. It has 18 `os.replace`/`shutil.move`/`os.rename`/`unlink` sites, and its only locks are in-process `threading.Lock`s.

The only real coordination is the operator remembering to stop things (the 2026-08-18 freeze lesson: a frozen database is not a frozen library). That holds while David does every apply by hand. It won't hold on X: once the watcher and Camelia run continuously against the primary library.

## Proposal: one writer gate per registered library root

### Registered coordination roots (R4)

- A **registry of coordination roots** lives in the suite's config. Each entry has:
  - an absolute path;
  - a `series_depth`: 1 for `X:\comix\<series>`, or more for layouts like `X:\Manga\<category>\<series>`;
  - an `enforced` flag (see rollout).
- A shared resolver, `coordination_root(path)`, maps any path to the **unique registered root that physically contains it**. It compares ancestor directory identities, the way `_require_external_database` does, so drive, UNC and mapped-drive aliases resolve to one root.
- It **refuses**:
  - a path under no registered root;
  - a registry whose roots overlap or alias each other, including one entry nested in another;
  - a caller whose own root is *above* a registered root.
- A caller rooted *inside* a registered root resolves to that enclosing root. This covers apply with `--series-folder-root` at `X:\Manga\Book`, and a watcher whose destination is a parent collection. So `X:\Manga\Book\` never gets its own gate file: every caller locks `X:\Manga\.cbz-library-writers.lock`.
- **Series key:** built from **directory components only**, taken from the actual canonical destination (for Camelia, the source's own path) relative to the registered root. It never comes from the arrival name or the intake category.
  - For a file destination, drop the filename and take the first `series_depth` directory components.
  - For a whole-folder landing, the landed folder itself is the final directory component.
  - So landing `X:\comix\Series\` as a folder and landing `X:\comix\Series\Book 01.cbz` as a file give the same key.
  - Components are NFC-normalised and casefolded.
  - A loose book directly under the root, or any destination with fewer than `series_depth` directory components, uses the reserved root key. A filename is never a series key.
  - Tests pin the folder-vs-file equivalence and the loose-book behaviour.

### The gate file

Each registered root has one zero-byte file, `<root>\.cbz-library-writers.lock`, locked with Windows byte-range locks (`LockFileEx`):

1. **Root gate, byte 0.**
   - **Shared** for every ordinary write transaction (one watcher landing, one Camelia install).
   - **Exclusive** for a maintenance run (apply, recover, undo, a maintenance-script apply), for the whole run.
2. **Series locks**, one byte per series at offset `1 + (h mod 2^62)`, where `h` is the first 8 bytes of SHA-256 of the series key. Each ordinary transaction takes its series byte **exclusive**.
   - A hash collision only causes false contention. It never lets two writers into one series.

**Ordering and waiting rules:**
- Always take the root gate before the series lock, and release in reverse.
- Every request is non-blocking (`LOCKFILE_FAIL_IMMEDIATELY`). Nobody waits while holding a lock.
- Each transaction takes the gate **once, at its outermost boundary**. The primitive refuses re-acquisition by the same thread, because byte-range locks aren't reentrant per handle.
- A refused ordinary writer **defers**: it keeps its work, retries with backoff, and never takes a partial step.
- A refused maintenance run exits "writers active, retry". It never waits.

**Measured on 2026-11-13** on X: (NTFS) and Z: (`\\TOWER\media`, SMB), cross-process (probe `C:\git\.claude-tools\probe_writer_gate.py`, claude-lead's measurement; not repeated by codex-lead):
- shared+shared granted; shared vs exclusive refused (error 33) in both directions;
- the same series byte refused, an adjacent byte granted;
- the gate byte and a series byte are independent;
- a killed exclusive holder's lock is released immediately;
- locking far beyond end-of-file works, and the file stays 0 bytes.

The OS drops a byte-range lock when its handle closes, so there's no stale-lock takeover and nothing ever deletes the file. That's the same property `scope_lock` relies on.

### Watcher: the transaction boundary (R3)

- The ordinary-writer boundary wraps the **whole landing transaction for one arrival**, from before its first library read or decision about the destination through its last library mutation:
  - the fast-path whole-folder `os.rename` in `_move_cbz_dir`;
  - the destination `mkdir`;
  - the merge fallback (per-file `_settle_or_land`);
  - `_backfill_chapter_one`;
  - `_land_deferred_names`;
  - collision quarantine and rollback;
  - loose-file moves.
- `_destination_locks` stays as the in-process guard *inside* that boundary. The gate is taken once, outside it, so the inner sections never reacquire it.
- `GateBusy` before the first step leaves the arrival untouched in intake under its claim. That's a deferral, not a hold, and it's retried with backoff.
- Witnesses: a busy gate refuses each entry path (fast-path rename, merge fallback, backfill, deferred names, collision quarantine, loose file) with zero library mutations, and the same arrival lands once the gate frees.

### Camelia: source-generation protection (R2)

Holding the gate through minutes of inference would block maintenance and that series for the whole run. Instead, Camelia **binds the input generation and revalidates it under ownership**:

1. **Private snapshot.** Before anything else, copy the source into a private work directory **outside every registered library root**, and validate the copy.
   - Record the source's identity and stability evidence (size, `mtime_ns`, file id, stat before and after the copy) **separately** from the snapshot's SHA-256.
   - The snapshot is immutable from then on.
2. **Everything heavy happens outside the library, on the snapshot only.**
   - Extraction, inference, rebuild and rebuild verification read only the snapshot, and write only private files outside every registered root.
   - Nothing reopens the live source, and nothing is created beside the destination.
   - This changes Camelia today: at autodecen 4211611 the rebuild creates `.camelia-<uuid>.tmp` beside the library destination and reads `archive_context['archive_path']`. Both move to the private area.
3. **Ownership, then revalidate.** Take `ordinary_unit(root, series)`. Under ownership, re-stat and re-hash the current source, and compare both with the recorded generation and the snapshot hash.
   - **Mismatch:** release the gate, **keep** the private output in Camelia's review area with both generations recorded, and requeue the job. Stale work is never installed.
4. **Every library-side step happens while ownership is held,** in this order:
   1. Same-volume staging. If the private output is on another volume, copy it to an exclusively created (`O_EXCL`) temp beside the destination. Cross-volume `os.replace` is never relied on.
   2. Copy the original to backup and verify it.
   3. Atomic `os.replace` of the staged temp over the source.
   4. Verify the install.
   5. Remove the staging temp.
   6. Release.
5. **Witnesses:**
   - **V1/V2 interleaving:** Camelia snapshots V1, and the watcher installs V2 at the same path before Camelia reaches install. Camelia refuses, keeps its V1-derived output, and V2 stays byte-identical.
   - **Gate held by maintenance:** while a maintenance run holds the exclusive gate, Camelia snapshots and rebuilds privately. No name or byte in the library changes, no temp appears beside the destination, and the rebuild never reopens the live source, even after the test replaces it mid-run.
   - **ABA:** the source goes V1 → V2 → V1 during the rebuild. The rebuild used only the snapshot, so the output is V1-derived and correct to install once revalidation matches.

### Maintenance runs and quiescence during rollout (R1)

Acquiring the exclusive gate proves only that no **participating** writer is mid-transaction. An old watcher or Camelia that never takes the gate is invisible to it. So:

- Until a registered root is marked `enforced`, apply, recover, undo and maintenance applies take the exclusive gate **and still require today's `--attest-quiescent`**. The watcher-stop precondition is unchanged.
- `enforced` is set by the operator only after every ordinary writer on that root is confirmed to run the gate protocol at the registry's protocol version. Participating writers log their protocol version at start. `enforced` is never inferred from a lock being free.
- Even when `enforced`, the gate says nothing about **non-participants** (Syncthing, Komga, Explorer, anything on Tower). A maintenance run then needs the gate plus a separate explicit operator condition, `--attest-no-external-writers`, which names those.
- The old canonical scope lock is kept. It serializes canonical mutators only, so it isn't a quiescence argument.
- Mixed-version refusal witness: on a root that isn't `enforced`, an apply that acquires the gate without `--attest-quiescent` is refused. A fixture "old watcher" that writes without the gate shows why.
- A stale owner JSON never proves quiescence. Only the OS lock is authoritative.

## Changes, in reviewable slices

1. **The writer gate primitive and the registered-root resolver** (`comic_automation/library/writer_gate.py`, stdlib only, Windows only like `scope_lock`):
   - A shared protocol constant names all four control files: `.cbz-library-writers.lock`, `.cbz-library-writers.owner.json`, `.cbz-canonical-naming.lock` and `.cbz-canonical-naming.owner.json`.
   - Wire those names into the sync observation exclusions in this slice, with a heads-up to codex-lead before touching `sync_inventory.py`. Document and test the required Syncthing ignores. Live `.stignore` and configuration changes stay outside the code approval.
   - Tests:
     - the probe matrix as real cross-process tests;
     - kill-release;
     - collision false-contention;
     - no file growth;
     - same-thread reacquisition refused;
     - refusal off Windows;
     - resolver witnesses: parent vs series root, drive/UNC/junction aliases, overlapping registrations refused, unregistered refused, a nested apply root mapped to its enclosing gate.
2. **Canonical apply/recover/undo:** take `maintenance_run` after the scope lock, applying the rollout rules above (`--attest-quiescent` stays required until `enforced`).
3. **Watcher:** the transaction boundary as specified, with each entry path's busy-gate witness.
4. **Camelia:**
   - Private snapshot, private rebuild, and generation revalidation under ownership as specified, plus all three witnesses.
   - Import the shared module from an explicit `CAMELIA_COMIC_AUTOMATION_ROOT`, with a module-origin and protocol-version check.
   - Import or platform errors fail closed: Camelia doesn't write.
   - Tested through the actual conda environment's import path, including parent packages. There's no silent fallback to uncoordinated writes.
5. **`cbz_library_maintenance.py`:** apply modes take `maintenance_run`. Dry runs take nothing.
6. **Web console M6:**
   - Show the gate state: free, maintenance-held (with the owner record, labelled diagnostic), or busy.
   - Show each deferred unit's reason and elapsed time, with an alert threshold that **never** bypasses ownership.
   - A shared lock can't be counted authoritatively, so any ordinary-writer count is labelled diagnostic or unknown.

Rollout needs David to stop and restart the watcher and Camelia. Slices 1 and 2 are safe alone, because the rollout rules keep today's attestation until `enforced`.

## Out of scope / limits

- **Syncthing, Komga, Explorer and anything on Tower itself** don't take Windows byte-range locks. Byte-range locks over SMB are visible only to SMB clients, not to processes local to Tower. That's why `--attest-no-external-writers` stays a separate operator statement, and why the cutover config (B5/#108) has to keep X: the only writable side.
- **Readers aren't gated.** The planner stays lock-free: B1/B2 tolerate concurrent change per scope, and apply revalidates every action.
- **Unlisted writers bypass the gate**, because it's a cooperative protocol. `docs/engineering_decisions.md` will list every writer that must honour it, and `enforced` is the operator's statement that the list is complete for that root.

## Answers adopted from codex-lead's review

1. Control filenames go in a shared protocol constant and are wired into the observation exclusions in slice 1, with a heads-up first. Syncthing ignores are documented and tested, but not applied live.
2. One shared implementation, imported by Camelia from an explicit root with origin and version checks, failing closed.
3. M6 exposes busy/deferred reasons and elapsed time, plus a non-bypassing alert. The telemetry is labelled diagnostic, and a stale owner JSON never proves quiescence.
4. The series key uses the registered-root/actual-destination rule above. A top-level component is the series key only when the registered layout says so.
