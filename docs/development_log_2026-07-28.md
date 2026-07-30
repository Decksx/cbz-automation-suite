# Comic Automation Suite — Work Completed 2026-07-28

**Repository:** `Decksx/cbz-automation-suite`
**Primary local working copy:** `C:\git\ComicAutomation`
**Desktop processing workspace:** `G:\ComicAutomation`
**Working inspection database:** `G:\ComicAutomation\TestDatabase\inspection-working.db`
**Komga library root:** `X:\`
**Branch:** `feature/archive-inspection`

This continues directly from the CRC continuation and corruption-handling
work completed earlier the same day (see prior commits `bf24b40`,
`621d5aa`, `0d3c73c`, `f465611`, `c997cb4`). Everything below is
read-only with respect to the source library except the two explicitly
approved remediation actions in sections 3 and 5.

## 1. CRC continuation — final state (recap)

The guarded CRC continuation completed successfully before this
session's work began:

```text
Total CRC-verified archives: 58,437
Raw unverified archives:     1  (archive 42988, Homunculus V1-02.cbz,
                                 corrupt_archive, excluded from
                                 automatic reprocessing)
SQLite integrity:            ok
```

Backup and reports from that run:

```text
G:\ComicAutomation\TestDatabase\inspection-working-pre-crc-continuation-20260728-083940.db
G:\ComicAutomation\logs\archive-inspection\crc-continuation-20260728-083940.json
```

## 2. Terminal failure review and export

Queried the working database read-only for every terminally-failed
`inspect_archive` job:

```text
147 terminal failures
  144 corrupt_archive
    3 filesystem_not_found
```

Verified read-only via a local scratch copy (byte-identical size to the
source, copied without I/O errors): `PRAGMA quick_check` → `ok`, and
`page_count * page_size` matched the file size on disk exactly for both
the source and the copy.

Exported the full inventory (archive ID, path, job ID, attempts,
failure category, error text, series/directory) and clustered by
series. Three clusters accounted for 133 of 144 corrupt archives:

```text
105  Manga\Feng Shen Ji III   corrupt_archive
 22  Manga\Superior Day       corrupt_archive
  4  Manga\Blood Lad          corrupt_archive (Blood Lad v6-v9 "tmp" files)
  2  Comix\Stupidemic - UNCENSORED   filesystem_not_found
 14  singletons across Manga/Comix/Horrorsplat
```

The size and uniformity of the `Feng Shen Ji III` cluster (105 files, all
`corrupt_archive`, same series) pointed to a systemic cause (bad
rip/conversion batch) rather than independent bit-rot.

## 3. Manual remediation (user-performed, outside tooling)

Two clusters were handled directly, not through the quarantine tool
below:

- `Manga\Feng Shen Ji III` — entire folder deleted; scheduled for a
  fresh re-download.
- `Manga\Blood Lad` — all four `v6tmp`/`v7tmp`/`v8tmp`/`v9tmp.cbz` files
  deleted individually; scheduled for a fresh re-download.

## 4. Quarantine feature (new)

Built a guarded remediation command to relocate the remaining
permanently-broken archives out of the live library into a designated
holding folder, renamed to be self-identifying, so they're easy to
re-download.

### New files

```text
comic_automation/database/migrations/009_archive_quarantine.sql
comic_automation/archive/quarantine.py
comic_automation/archive/quarantine_cli.py
scripts/comic_archive_quarantine.py
tests/test_archive_quarantine.py
```

### Design

- New `archive_quarantine` table (migration 009) tracks each quarantined
  archive's source/destination path, failure category, originating job,
  and a `pending_redownload` / `resolved` / `abandoned` status. See
  `docs/database_architecture.md` for the full schema note.
- Naming rule: prefix the series name onto the filename only if it
  isn't already present (most archives already have it in-name, e.g.
  `Superior Day Chapter 12.cbz`), so quarantined files stay
  self-identifying without redundant names.
- Only `corrupt_archive` is eligible for quarantine by default;
  `filesystem_not_found` is explicitly rejected (`--category`) since
  there is no file on disk to move.
- Default mode is a dry-run preview; `--confirm` is required to
  actually move files, and requires `--backup-directory`. Confirming
  gates on a clean `PRAGMA quick_check` first.
- Each file is handled independently: a failure on one archive doesn't
  stop the batch. If the database update fails after a file has already
  been physically moved, the file is moved back so the filesystem and
  database never disagree.
- `--exclude-series` skips series being handled separately (used for
  `Feng Shen Ji III` and `Blood Lad`, see section 3).

### Testing

Repo requires Python 3.11+ (`enum.StrEnum`); the CI/dev sandbox used for
this session only had 3.10 by default, so Python 3.12 was installed via
`uv python install 3.12` to run the suite.

```text
tests/test_archive_quarantine.py:  18 passed
Full suite:                        156 passed, 2 failed
```

The 2 failures (`tests/test_series_detection.py`) are pre-existing and
unrelated to this work: they hardcode Windows UNC backslash paths
(`\\tower\media\comics\...`) that only parse correctly under a native
Windows `Path`, not on the Linux sandbox used to run this particular
verification pass.

Adding migration 009 required updating two existing tests that hardcode
the expected applied-migrations list:

```text
tests/test_migrations.py   (8 -> 9)
tests/test_service.py      (8 -> 9)
```

A real cross-platform bug was found and fixed during testing:
`QuarantineRepository.find_candidates` originally parsed the stored
library path with the platform-ambient `pathlib.Path`, which silently
breaks series-name extraction on a non-Windows host (a `X:\Manga\...`
string is treated as a single unsplit component). Fixed by parsing with
`PureWindowsPath` explicitly for the stored-path string, since these
paths are always Windows-style regardless of what host the tool runs on
(`PureWindowsPath` also accepts forward slashes, so it doesn't break
POSIX-style test fixtures either).

### Dry-run verification against the real working database

```text
--exclude-series "Feng Shen Ji III"                  -> 39 candidates
--exclude-series "Feng Shen Ji III" "Blood Lad"       -> 35 candidates
```

Matched the manual failure-inventory analysis from section 2 exactly.

## 5. Quarantine executed

Run by the user directly (this session has no access to `X:\`):

```powershell
python scripts/comic_archive_quarantine.py --database G:\ComicAutomation\TestDatabase\inspection-working.db --quarantine-root X:\_NeedsRedownload --exclude-series "Feng Shen Ji III" --exclude-series "Blood Lad" --confirm --backup-directory G:\ComicAutomation\backups
```

Result:

```text
Candidates found:   35
Moved:              35
Errors:             0
Backup:             G:\ComicAutomation\backups\inspection-working-pre-quarantine-20260728-170428.db
Pending redownload: 35
quick_check before: ok
quick_check after:  ok
Elapsed:            8.77 seconds
```

All 35 archives are now in `X:\_NeedsRedownload`, renamed to show
series and chapter, tracked in `archive_quarantine` with status
`pending_redownload`.

## 6. Library rescan

Ran a full read-only discovery rescan of `X:\` to reconcile state after
the manual deletions (section 3) and the quarantine move (section 5),
explicitly excluding the new holding folder so it isn't rediscovered as
part of the live library:

```powershell
python scripts/comic_library_discovery.py --root X:\ --database G:\ComicAutomation\TestDatabase\inspection-working.db --exclude-directory "_NeedsRedownload"
```

Result:

```text
Scanned:     59,379
New:         1,104
Changed:     16
Unchanged:   58,259
Missing:     271
Jobs queued: 1,120
Errors:      0
Excluded:    5 directories
Elapsed:     57.06 seconds
```

The large new/missing counts are expected: the CBZ watcher script has
continued syncing new material into the library throughout this work,
independent of the CRC/quarantine effort above. `.stversions` and
`.stfolder` (Syncthing) are excluded from every scan by default (see
`comic_automation/library/exclusions.py`;
`DEFAULT_EXCLUDED_DIRECTORIES`), so no extra flag was needed for those.

## 7. filesystem_not_found follow-up

Queried the 3 `filesystem_not_found` archives directly against the live
database after the rescan (indexed lookup, no full copy needed):

```text
24953  X:\Comix\Stupidemic - UNCENSORED\39.cbz   -> still missing, no replacement found
24954  X:\Comix\Stupidemic - UNCENSORED\40.cbz   -> still missing, BUT already
                                                     superseded by "Stupidemic Ch. 40.cbz"
                                                     (status: ok, first seen 2026-07-23)
26024  X:\Comix\Thanatos\Thanatos.cbz            -> still missing, BUT already
                                                     superseded by "Thanatos Ch. 1.cbz" +
                                                     "Thanatos 2.cbz" (both status: ok,
                                                     first seen 2026-07-27)
```

Confirmed via `file_events`/`last_seen_at`: sibling files in the same
two folders picked up fresh `last_seen_at` timestamps from this
session's rescan, but these 3 exact paths were untouched and logged no
`restored` event, confirming they are still genuinely absent (not a
scan artifact).

Net result: only `Stupidemic - UNCENSORED\39.cbz` (chapter 39) is a real
gap needing a fresh download. The other two `filesystem_not_found`
entries are stale references to already-replaced content and need no
further action.

## 8. Repository commits

Three commits were made covering the work in sections 4 and above (the
corruption-handling fix predates this session but was still
uncommitted; everything else is new). The ~34 other files `git status`
shows as modified are line-ending-only noise from this checkout's
`core.autocrlf` (working tree CRLF vs. committed LF, byte-identical
content otherwise) and were deliberately left out of every commit:

```text
2154adf fix: classify zlib.error/EOFError as corrupt_archive during CBZ inspection
23ed81c feat: add guarded archive quarantine workflow for permanently-broken CBZs
5b15843 docs: log 2026-07-28 terminal-failure review and quarantine session
```

Branch `feature/archive-inspection` is 24 commits ahead of
`origin/feature/archive-inspection`; not yet pushed.

## 9. Post-rescan inspection run

Ran the bounded inspection CLI (with `--verify-crc`) against the 1,120
jobs queued by the rescan in section 6, to fully validate the
watcher-synced content before moving on to hashing:

```powershell
python scripts/comic_archive_inspection.py --database G:\ComicAutomation\TestDatabase\inspection-working.db --limit 1200 --verify-crc --progress-every 100 --json-output G:\ComicAutomation\logs\archive-inspection\post-rescan-inspection-20260728.json --failure-csv-output G:\ComicAutomation\logs\archive-inspection\post-rescan-inspection-20260728-failures.csv
```

Result:

```text
Processed:         1,120
Succeeded:         1,119
Terminally failed: 1
Remaining pending: 0
Inspection statuses:  ok: 59,536   no_images: 5
Terminal failures:    148  (145 corrupt_archive, 3 filesystem_not_found)
```

One new corrupt archive turned up, isolated (not a cluster):

```text
X:\_extraneous\Drunk on You\Drunk on You Chapter 51.cbz  (archive 58664)
```

Folded into the same pending-redownload bucket as the rest of section
8's original list rather than tracked separately.

## 10. Archive-level exact hashing (SHA-256)

With the library fully inspected (0 pending `inspect_archive` jobs),
moved to the hashing phase of the roadmap. Archive-level (whole-file)
SHA-256 hashing first:

```powershell
python scripts/comic_archive_hashing.py --database G:\ComicAutomation\TestDatabase\inspection-working.db --enqueue-missing --limit 5000 --progress-every 250 --json-output G:\ComicAutomation\logs\archive-hashing\batch-1-20260728.json
```

Completed in a single batch — most of the library already had hashes
from earlier work; only the 1,119 archives freshly inspected in section
9 needed catching up:

```text
Processed:          1,119
Succeeded:           1,119
Hashes stored:      59,541  (100% of currently-valid archives)
Remaining pending:       0
Duplicate groups:        2
Elapsed:             99.04 seconds
```

Archive-level exact hashing is now complete for the entire library.

### Exact-duplicate groups found

```text
1. X:\Manga\Gundam - Gundam Perfect File\Oneshot.cbz
   ≡ X:\_extraneous\Gundam - Gundam Perfect File\Oneshot.cbz
   (629 MB, byte-identical, sha256 match)

2. X:\Comix\Ai no Katachi 2\Ai no Katachi 2.cbz
   ≡ X:\_extraneous\Ai no Katachi 2\anime Chapter.cbz
   (29,699 bytes, byte-identical despite different filename)
```

Both pairs follow the same shape: one copy already organized into the
live library (`Manga`/`Comix`), and a byte-identical copy sitting in
`X:\_extraneous`.

### `X:\_extraneous` clarified

`_extraneous` is a holding folder for suspected duplicate copies (not
a curated library folder). Confirmed: when an archive in `_extraneous`
has a confirmed exact match already in the organized library, the
`_extraneous` copy can be deleted. This is an explicitly approved,
bounded remediation rule, distinct from the quarantine workflow in
section 4 (that one *moves* permanently-broken files out of the
library and preserves them for re-download; this one *deletes* a
confirmed-redundant copy that already exists elsewhere).

As of this writing, whether to build a small reusable "resolve
confirmed exact duplicates in `_extraneous`" CLI (mirroring the
quarantine tool's guarded pattern: preview by default, `--confirm` +
backup required to actually delete) versus a one-time manual cleanup of
just these 2 pairs is still an open decision — `_extraneous` is fed
continuously by the watcher script, so more matches are expected over
time, which favors the reusable option, but no build work has started
on it yet.

## 11. Current outstanding state

```text
Original 147 terminal failures, now 148 (see section 9):
  105  Feng Shen Ji III        -- deleted by user, pending re-download
    4  Blood Lad tmp files     -- deleted by user, pending re-download
   35  quarantined             -- in X:\_NeedsRedownload, pending re-download
    1  _extraneous corrupt     -- Drunk on You ch.51, pending re-download
    2  filesystem_not_found    -- already superseded, no action needed
    1  filesystem_not_found    -- genuine gap (Stupidemic ch. 39), pending re-download

Exact-duplicate groups: 2, pending a decision on resolution tooling
(see section 10) and then execution -- not yet deleted.
```

## 12. Next steps

Recommended order, updated from the original roadmap based on this
session's findings (dependency-driven: validate before hashing, exact
dedup before perceptual dedup, identity before quality scoring):

1. ~~Run pending inspection jobs~~ -- done, section 9.
2. ~~Exact archive-level hashing~~ -- done, section 10. Page-level
   content hashing (`scripts/comic_page_hashing.py`, same
   `--enqueue-missing`/batched pattern) is still outstanding and is the
   more thorough half of "exact hashing" -- it catches same-content
   archives that were repackaged/recompressed differently and so don't
   share a whole-file SHA-256.
3. Resolve the 2 known exact-duplicate groups (decision pending, see
   section 10) -- and re-run duplicate detection after each future
   hashing batch, since `_extraneous` is fed continuously.
4. Run perceptual/near-duplicate analysis (implemented, not yet run at
   scale) -- review-only, never auto-deletes.
5. Build series and issue normalization.
6. Quality scoring and preferred-copy selection.
7. Komga/Komf publication integration.
8. Productionize service/monitoring/dashboard.

The redownload backlog (105 + 4 + 35 + 1 + 1 = 146 archives across
sections 8-11) is explicitly deferred by the user to be handled
separately later; it does not block the above.
