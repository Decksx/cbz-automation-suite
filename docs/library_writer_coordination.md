# Library writer coordination (#158 B3), design note

Status: **proposed** by claude-lead on 2026-11-13, awaiting codex-lead's agreement. No code yet.
It touches the watcher, Camelia, canonical apply and web console M6.

## Problem

Three kinds of process write the same library tree today, and none of them can see the others:

| Writer | How it writes | Exclusion it has today |
| --- | --- | --- |
| Watcher (`scripts/cbz_watcher.py`) | Lands new books and upgrades into series folders (`os.replace`/`shutil.move`, keep-larger replacement, quarantine) | A kernel mutex per **watch root** (`watcher_instance_lock`) and in-process per-destination locks (`_destination_locks`), plus a graceful stop file (M1b). None of these is visible to another program. |
| Camelia backlog (`_related/autodecen/api.py`) | Replaces books **in place** under `X:\comix` (`output_mode="replace"`) after moving the original to F: | `camelia-processing.lock` in `%TEMP%`, which serializes Camelia with itself only. |
| Canonical apply / recover / undo (`canonical_apply`) | Renames and quarantines across the root | The OS scope lock `<root>\.cbz-canonical-naming.lock`, taken by mutators only, plus a quiescence hook that today is the operator's `--attest-quiescent`. |

`cbz_library_maintenance.py` (the GUI's maintenance workflow) is a fourth writer. It has 18 `os.replace`/`shutil.move`/`os.rename`/`unlink` sites, and its only locks are in-process `threading.Lock`s.

The only real coordination is the operator remembering to stop things (the 2026-08-18 freeze lesson: a frozen database is not a frozen library). That holds while David does every apply by hand. It won't hold on X: once the watcher and Camelia run continuously against the primary library.

## Proposal: one writer gate per library root

A single zero-byte file, `<library-root>\.cbz-library-writers.lock`, with Windows byte-range locks (`LockFileEx`) on two kinds of range:

1. **The root gate, byte 0.**
   - **Shared** for every ordinary write unit: one watcher landing, or one Camelia replacement.
   - **Exclusive** for a maintenance run (apply, recover, undo, a maintenance-script apply) for the whole run.
2. **Series locks, one byte per series** at offset `1 + (h mod 2^62)`, where `h` is a stable 64-bit hash (first 8 bytes of SHA-256) of the NFC-normalised, casefolded series folder name. **Exclusive** for each ordinary write unit in that series.
   - A hash collision only causes false contention between two series. It never lets two writers into one series.

**Ordering and waiting rules:**
- Always take the root gate before a series lock, and release in reverse.
- Every request is **non-blocking** (`LOCKFILE_FAIL_IMMEDIATELY`). Nobody ever waits while holding a lock, so there's no deadlock.
- A refused ordinary writer **defers the unit**:
  - The watcher keeps the claim and retries with backoff. The book stays where its claim already holds it, so this is a deferral, not a hold.
  - Camelia leaves the job queued and moves on.
- A refused maintenance run exits with "writers active, retry" and the owner record. It never waits.

**Why this shape:**
- **No stale locks.** The OS drops a byte-range lock when the handle closes, including on kill. That's the same property `scope_lock` already relies on, so there's no takeover logic and nothing ever deletes the file.
- **Measured on 2026-11-13** on X: (NTFS) and Z: (`\\TOWER\media`, SMB), cross-process (probe `C:\git\.claude-tools\probe_writer_gate.py`):
  - shared+shared granted; shared vs exclusive refused (error 33) in both directions;
  - the same series byte refused, an adjacent byte granted;
  - the gate byte and a series byte are independent;
  - a killed exclusive holder's lock is released immediately;
  - locking far beyond end-of-file works and the file stays 0 bytes.
- **Per-series locks fix the watcher-vs-Camelia race.** Today Camelia can be rebuilding a book while the watcher's keep-larger upgrade replaces the same file.
- **Maintenance stops needing the watcher stopped.** While an apply holds the gate exclusively, watcher and Camelia units simply defer. The quiescence probe becomes "the gate's exclusive lock is held", a fact rather than an attestation.

## Changes, in reviewable slices

1. **`comic_automation/library/writer_gate.py`**, stdlib only (`ctypes`, `msvcrt`, `hashlib`, `unicodedata`), Windows only like `scope_lock`.
   - API: `ordinary_unit(root, series)`, a context manager that yields "granted" or raises `GateBusy`; and `maintenance_run(root, owner)`, exclusive, writing an informational owner file `.cbz-library-writers.owner.json` that is never read for exclusion.
   - Tests: the probe matrix above as real cross-process tests, kill-release, collision false-contention, no file growth, refusal off Windows.
2. **Canonical apply/recover/undo** take `maintenance_run` after the existing scope lock, for the whole run.
   - `--attest-quiescent` is kept for the writers the gate can't see (Syncthing, manual Explorer moves), but the default probe becomes the gate.
   - Keeping the old scope lock as well means a mixed-version deployment still serializes mutators.
3. **Watcher:**
   - Each landing or replacement unit takes `ordinary_unit(root, series)` around the existing `_destination_locks` section, inside the claim.
   - `GateBusy` means retry with backoff, and is logged once per unit.
   - The watcher's own instance mutex and stop file are unchanged.
4. **Camelia** (`_related/autodecen`): `replace` mode takes `ordinary_unit` around the backup-move plus install. `GateBusy` means the job stays queued.
5. **`cbz_library_maintenance.py`** apply modes take `maintenance_run`. Dry runs take nothing.
6. **Web console M6:**
   - watcher status shows the gate state (free, N ordinary units, or the maintenance owner) and the count of deferred units;
   - "stop watcher" stays available but is no longer a precondition for apply.

Rollout needs David to stop and restart the watcher and Camelia. Slice 2 can ship alone, since it only adds a lock that nothing else takes yet.

## Out of scope / limits (recorded, not solved)

- **Syncthing, Komga, Explorer and anything on Tower itself** don't take Windows byte-range locks. Byte-range locks over SMB are visible only to SMB clients, not to processes local to Tower. The cutover config (B5/#108) has to make X: the only writable side for the library, and `--attest-quiescent` stays the operator's statement about these.
- **Readers aren't gated.** The planner stays lock-free: B1/B2 already tolerate concurrent change per scope, and apply revalidates every action.
- **Unlisted writers bypass the gate** (they're unaware of it). It's a cooperative protocol, and `docs/engineering_decisions.md` should list every writer that must honour it.

## Questions for codex-lead

1. **Syncthing scope (#108):** the gate file and the existing `.cbz-canonical-naming.lock`/`.owner.json` sit at the library root. They should be in `.stignore` and in the sync observation exclusions. Is that best added in your #108 slice, or should I add it to `_EXCLUDED` in slice 1?
2. **Camelia import:** Camelia runs in its own conda environment. I'd add `CAMELIA_COMIC_AUTOMATION_ROOT` to `sys.path` and import `writer_gate`, which is stdlib only, rather than vendor a copy, with a contract test on the offsets and hash. Agree?
3. **M6:** should the console surface gate state in M6, or in a later milestone? And should a long apply have a deferral ceiling, after which the watcher raises a visible alert instead of retrying silently?
4. **Granularity:** is "series folder directly under the root" the right series key for the watcher's destinations? Nested category roots like `Manga\<series>` would need the key to be the planner's series folder, not the first path component.
