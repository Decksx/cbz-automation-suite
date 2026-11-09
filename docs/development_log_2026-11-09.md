# Development log: 2026-11-09

## Watcher: hold only the archives that need review

Branch `fix/watcher-partial-deferral` from master `ab5eb50`. Every commit
changes one file. Claude implemented this at the operator's request ("yes,
please fix those issues", 2026-11-09).

### Incident

The live watcher (`C:\git\ComicAutomation` at `5755290`) received a
710-archive `Usogui` folder (`MangaDex (ALL)`). Measured from
`Logs\cbz_watcher.log`:

- the 09:24 pass refused 54/710 archives with `conflicting arrival number
  tokens` and deferred the whole directory, so 0/710 moved;
- the 11:03 restart began re-processing all 710 at about 4 s per archive
  (roughly 45 minutes) toward the same result;
- the folder held 710 archives covering 588 distinct chapter numbers; 112
  chapters had two or more copies (122 extra archives), mostly two
  scanlation groups (`Team Duwang`, 528 archives; `Easy Going`, 103).

### Changes

1. **Per-archive holds** (`scripts/cbz_watcher.py`). A refusal now holds
   only the refused archive. `_move_cbz_dir` and `_merge_directories` take
   `held`; with any held, the directory is merged rather than moved
   wholesale, so held archives never leave the import folder. A refused
   placeholder therefore still cannot reach the series folder (the PR #121
   reason the whole directory used to wait). Fallback naming now runs for
   the archives media naming accepted, since they now move. When every
   archive is held nothing moves and no series folder is created.
2. **Unchanged held directories are skipped.** `Logs\cbz_watcher_held.json`
   records a fingerprint (file names, sizes, modification times, watcher
   source hash, number-style registry hash) of a directory left holding only
   deliberately held archives. Later passes skip it while it matches.
   Refusals that can clear without a file changing (an OSError such as a
   locked archive, or a registry style refusal) are not deliberate holds, so
   such a directory is retried as before. So is any directory where a move
   failed. Delete an entry or the file to force a retry.
3. **Equal repeated numbers are one claim**
   (`comic_automation/library/media_naming.py`, `distinct_numbers`).
   `Inglorious Vol.44 Ch.483.5 - Volume 44 Side Story` states volume 44
   twice and was refused as conflicting. Of the 54 names refused on
   2026-11-09, 47/54 now name (`Usogui v44 ch. 483.5.cbz`); 7/54 are still
   refused and now held one by one: six `... v2` names (a trailing `v2` may
   be a revision, a different value from the volume) and `Ch.323 - Chapter
   1, Fin` (two chapter numbers).
4. **Second translations are held** (`_duplicate_chapter_copies`). The copy
   whose group has the most archives in the directory lands, then the
   larger file; the others are held, never deleted. Rules that keep it from
   firing on different books: same single number, volume, specials,
   editions, collection and version markers; different prefixes; both
   groups with two or more archives; and a prefix that is the series name
   plus more words (`Batman Annual` in `Batman`) is another title. The last
   rule exists because `Annual` is not special vocabulary in
   `field_resolution`, so nothing else would separate `Batman Annual #1`
   from `Batman #1`.

### Deliberately not done

- The `auto number style needs exactly one registered root` refusal for
  directories the resolver sends to `X:\_staging` is a registry/
  configuration gap, out of scope here. Those directories are still
  retried every pass (they are small), because a registry refusal is
  treated as transient.
- No change to how the operator resolves a held second translation: it is
  reported in the log, and the choice is manual.

### Tests

`tests/test_watcher_held_archives.py` (new),
`tests/test_media_naming.py` (+5), `tests/conftest.py` (new: every test
gets its own held-state file, so no test writes the checkout's `Logs`).
`tests/test_watcher_canonical_convergence.py`:
`test_a_refused_directory_offloads_nothing` described the old
whole-directory hold and is replaced by
`test_a_refused_archive_keeps_its_original_while_its_sibling_routes`.
`tests/test_watcher_stripped_collision.py` drives the retry of a held
arrival on purpose, so its fixture turns the skip off.

### Verification

Windows / `C:\Python311` / Python 3.11, temporary fixtures only, both runs
in parallel on the same machine:

- master `c9bba0f`: **5,020 passed / 10 skipped / 1 failed** (5,031 tests).
- this branch on `c9bba0f`: **5,041 passed / 10 skipped / 2 failed**
  (5,053 tests). +22 = 17 in `test_watcher_held_archives.py` + 5 in
  `test_media_naming.py`; the replaced convergence test is one for one.
- Every failure in both runs is in `tests/web` (Windows file-lock
  `PermissionError`, a different test each run). The branch changes nothing
  there, and the two that failed on the branch pass alone (6 passed).
- Line endings: `scripts/cbz_watcher.py` stays `mixed`; bare-LF 865 -> 868
  (+6 new lines written into LF regions, -3 LF lines whose text changed).

Guards proven load-bearing by bypassing each and running the held-archive,
canonical-convergence and series-year test files (failing tests per guard):

| Guard | Fails when bypassed |
| --- | --- |
| `_merge_directories` skips held | 10 |
| `_move_cbz_dir` never moves a directory wholesale with holds | 9 |
| all-held directory creates no series folder | 1 |
| only deliberate holds are recorded | 4 |
| unchanged held directory is skipped | 1 |
| series-plus-words prefix is another title | 1 |
| one-archive prefixes are not groups | 1 |
| the kept copy's own group is never held | 1 |
| watcher source hash in the fingerprint | 1 |
| OSError refusal is transient | 2 |
| registry refusal is transient | 1 |
| transient refusals left out of deliberate holds | 3 |

A first draft also skipped duplicate groups with a single group before
ranking. Bypassing it failed nothing (the per-copy group check already
covers it), so it was removed as unreachable.
