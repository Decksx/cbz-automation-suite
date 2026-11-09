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

## Review round 1 (Codex, at `c2805816`)

Codex reproduced four findings as five failing probe cases; the connector
left a P1. Each became a test first (`8225eb7`, all six failing at
`c2805816`), then a fix:

1. **A failed deferred-name move was cached as a hold.** Every
   tag-stripped arrival was passed as deliberately held, so a failed move
   was skipped for good. Tag-stripped arrivals are no longer passed at all:
   a deferred rename is a plan, and a collision hold depends on the library
   copy, which the directory fingerprint cannot see. Those directories are
   retried every pass, as before this PR.
2. **Year and season variants were held as second translations.** The
   duplicate key now compares bracketed tags (`(2019)`, `(s2)`) and `Part`
   markers. A separate key on parsed years was tried and removed: the
   parser reads only parenthesised years, which the bracket tags already
   cover, and bypassing it failed nothing.
3. **A same-size, same-mtime replacement inherited the skip.** The
   fingerprint now includes each file's SHA-256 (it is computed only for
   directories holding nothing but held archives).
4. **A refused preferred copy held back the valid one.** `_second_copies`
   keeps the best copy naming did not refuse; a copy freed that way is
   named in a further batch (`_name_batch`) until every cluster settles.
5. **P1: held second copies were released by a new arrival.** Once the
   kept copy has landed, the intake alone no longer shows the held copy as
   a second translation, so it was named onto the kept copy's library name
   and settled by keep-the-larger. Second copies are now remembered by name
   and SHA-256 (`second_copies` in the state entry) and stay held while
   unchanged, whatever else arrives.

Two more cases in Codex's probe file were not reported as findings but
failed at `c2805816` and pass on base, so they are fixed too:

- `Part A` / `Part B` of one chapter from two prefixes (item 2);
- a held archive whose ComicInfo the pass itself rewrote after judging it.
  Its hold was remembered from facts that no longer hold, so the next pass
  never judged the rewritten archive. An archive the pass rewrote
  (size or mtime changed since the pass began) is no longer remembered; the
  next pass judges it again and, if it is then left unchanged, remembers it.
  This costs one extra pass for such an archive.

Found while verifying: `_name_batch` called `_apply_media_naming` without
the `_media_naming_enabled()` guard the inline code had, which
`test_empty_registry_runs_real_legacy_watcher_import_without_stalling`
caught. The guard is restored.

The rewrite rule masked the transient-refusal guards: their existing tests
rewrite the archive in the same pass, so bypassing a transient guard failed
nothing. New tests run the refusing pass twice (the second leaves the
archive unchanged), so each transient guard is again load-bearing. A guard
that a missing fingerprint never matches was unreachable and was removed.

### Round 1 verification

At `eb98c51` (code and tests unchanged since; this commit is documentation
only), Windows / `C:\Python311` / Python 3.11, temporary fixtures only:

- full suite: **5,055 passed / 10 skipped / 0 failed** (5,065 tests) =
  master `c9bba0f`'s 5,031 + 34 (29 in `test_watcher_held_archives.py`, 5 in
  `test_media_naming.py`);
- Codex's probe file `test_review173_probes.py` (SHA256
  `ba26ca90...8aed7`): **8/8 passed** at this head; **0/8** at `c2805816`;
- `scripts/cbz_watcher.py` stored bare-LF count still 868.

Every guard added or changed in this round fails named tests when bypassed:

| Guard | Fails |
| --- | --- |
| tag-stripped arrivals not cached | 1 |
| bracket tags in the duplicate key | 2 |
| `Part` markers in the duplicate key | 1 |
| content hash in the fingerprint | 1 |
| a refused copy cannot be kept | 2 |
| copies freed by a refusal are named | 1 |
| earlier second copies stay held | 1 |
| a carried copy must be unchanged | 1 |
| rewritten archives are judged again | 1 |
| empty registry never enters media naming | 1 |
| kept copy's own group is not held | 1 |
| OSError refusal is transient | 1 |
| registry refusal is transient | 1 |
| transient refusals left out of deliberate holds | 2 |

## Issue #174: held copies discarded after AI preprocessing

PR #173 merged as `0d9eea8`. Codex then withdrew its approval: with AI
decensoring reprocessing held archives, a remembered second copy was
rewritten by the pass *before* `_carried_second_copies` hashed it. The hash
no longer matched, so a new arrival released both held Beta copies; they
were named onto the library's chapter names and discarded by
keep-the-larger. Codex's repro `test_review173_r2_preprocessing.py`
(SHA256 `de49c3d1...8bced`) fails its `rewrite` case at `0d9eea8`. The live
watcher was rolled back to `5755290` and is untouched by this work.

Branch `fix/watcher-held-preprocessing` from `0d9eea8`. Tests first
(`99c1324`, four failing at `0d9eea8`), then the fix:

1. **Check order.** The carry is judged on the bytes the pass finds, before
   ComicInfo or AI rewrites them (`memory` in
   `_process_and_move_directory_inner`). The recorded hash is of the bytes a
   pass leaves behind, which is exactly what the next pass finds unless
   someone else changed the file.
2. **No path settles a held or second copy by keep-the-larger.** A
   protected arrival that meets a different existing file is held
   (`_hold_protected_collision`, in both `_merge_directories` and
   `_move_loose_files`); an exact duplicate is still removed. Protected:
   every copy in a duplicate cluster that is not already held (the fourth
   test showed keep-the-larger *discarding* an incoming kept copy whose gain
   was below the replacement threshold), a remembered copy whose bytes
   changed, and, when the state file is unreadable
   (`_load_held_state` returns None, distinct from "no file"), every
   archive in the pass.
3. **Docs.** Force a retry by touching a file, not by deleting the state
   entry: deleting it makes the watcher forget the held copies, after which
   a forgotten copy outside any duplicate cluster is an ordinary arrival.
   That operator action is the one remaining way to give up the protection,
   and it is documented as such.

Guards proven by bypass (held-archive tests, failing per guard): carry
judged before preprocessing 1; merge holds a protected collision 4; loose
move holds a protected collision 1; exact duplicate is not held 2; changed
remembered copy protected 1; unreadable state protects everything 1;
cluster copies protected 1; unreadable state is not empty state 2.

Verification at `98f0719` (code and tests; later commits are docs only),
Windows / `C:\Python311` / Python 3.11, temporary fixtures only:

- full suite: **5,060 passed / 10 skipped / 1 failed** (5,071 tests) =
  master `0d9eea8`'s 5,065 + 6. The failure is
  `tests/web/test_web_logs.py::test_alternating_overflow_compacts_map_and_physical_files`,
  the same Windows file-lock flake that failed on master `c9bba0f` in this
  log's first baseline; the branch changes nothing under `web`, and it
  passes alone;
- Codex's files at this head: `test_review173_r2_preprocessing.py` 2/2,
  `test_review173_probes.py` 8/8, `test_review173_r2_guards.py` 4/4;
- `scripts/cbz_watcher.py` stored bare-LF count still 868.
