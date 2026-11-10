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

## Non-destructive name collisions (PR A of the claim-first plan)

Branch `fix/non-destructive-collisions` from master `0d9eea8`. The first of
two PRs replacing #175 (the second moves arrivals out of the MEGA-synced
watch folder before any change). Decided by the operator on 2026-11-09:
quarantine on C: next to the watch root, configurable; 30-day retention for
quarantine only (the purge itself is not part of this PR).

The legacy conflict branches of `_merge_directories` and `_move_loose_files`
unlinked the copy keep-the-larger did not keep. Both now call
`_settle_by_size`: a byte-identical arrival is removed; otherwise the loser
is moved to `QUARANTINE_ROOT/<date>/<series>/` under a unique name before
anything else moves. A failed quarantine move keeps both copies in place
(and removes a partial cross-volume copy); a failed replacement moves the
quarantined library copy back. Resolver mode already quarantined through
`settle_file_conflict` and is unchanged.

Tests: `tests/test_watcher_non_destructive_collisions.py` (17), both movers;
`tests/conftest.py` points `QUARANTINE_ROOT` at each test's tmp dir. At
`0d9eea8` 15 of the 17 fail (the two byte-identical cases behaved the same
before). Guards proven by bypass (failing tests): only byte-identical copies
are removed 2 (it failed nothing until an equal-size, different-bytes test
was added); the loser is quarantined, not deleted 12; a failed quarantine
leaves no partial copy 2; a failed replacement restores the library copy 2;
quarantine names are collision-safe 1.

Verification at `cc92cd9` (this commit is docs only), Windows /
`C:\Python311` / Python 3.11, temporary fixtures only: full suite **5,071
passed / 10 skipped / 1 failed** (5,082 tests = master `0d9eea8`'s 5,065 +
17). The failure is the known `tests/web` file-lock flake
`test_alternating_overflow_compacts_map_and_physical_files`; the branch
changes nothing under `web` and it passes alone. `scripts/cbz_watcher.py`
stays `mixed`; bare-LF 868 -> 869 (one new line beside an LF `global`).

### PR #176 review round 1 (Codex, at `2b01b79`)

**P1:** quarantine names were chosen (`unique_path`) and used later, so two
admitted directories settling at once could pick the same name, and the
second move overwrote the first copy. A failed move's cleanup could also
delete another placement's completed file (it took "both exist" as proof
of a partial copy). **P2:** `_move_loose_files` counted a failed quarantine
or a rolled-back replacement as a moved file and returned a landing.

Codex's repros `test_review176_quarantine_race.py` (SHA256
`f016d349...29f`) and `test_review176_failed_landing.py` (`139c9e21...332`)
fail 6/6 at `2b01b79`. My tests (`00d9f70`) fail 8/8 there, including a
landing race that base `0d9eea8` has too: two arrivals landing one new
name at once.

Fix: `_reserve` creates the target exclusively (`O_CREAT | O_EXCL`, next
numbered name if taken) before moving into it, for quarantine placements
and, through `_land`, for new library names; a failed placement removes
only the name it created. `_settle_by_size` returns whether it settled, and
`_move_loose_files` counts only settled collisions and completed landings.

Guards proven by bypass (failing tests): reservations are exclusive 6; a
landing owns its name first 2; an unsettled loose collision is not a
landing 2; an unlanded loose file is not a landing 1 (it failed nothing
until a test of a name taken during the landing was added); a failed
replacement restores the library copy 3; a failed quarantine removes only
its own reservation 4.

Round 1 verification at `cfca284` (this commit is docs only): full suite
**5,080 passed / 10 skipped / 1 failed** (5,091 tests = 5,082 + 9). The
failure is the known `tests/web` file-lock flake
`test_alternating_overflow_compacts_map_and_physical_files`, which passes
alone. Codex's two repro files pass 6/6 at this head. Watcher bare-LF still
869.

Correction to the line above: the first isolated rerun of that web test
after the full suite **also failed** (1 failed); three further isolated
runs passed (3/3). It is intermittent on its own too, not only under the
full suite's load. The branch changes nothing under `comic_automation/web`
or `tests/web`.

### PR #176 review round 2 (Codex, at `04cfd9a`)

**P1:** a second collision treated the first import's reservation (empty)
or its partial copy as a library book, quarantined it and landed its own;
the first import then overwrote that archive, or its failure cleanup
deleted it. **P2 (supplemental):** `_merge_directories` ignored a failed
settlement and `_move_cbz_dir` returned a landing even when nothing in the
batch completed. Codex's repros `test_review176_active_reservation.py`
(SHA256 `ed5c8fe4...b29`) fail 8/8 and `test_review176_directory_receipt.py`
(`7e1b5050...cd3`) fail 2/2 at `04cfd9a`. My tests (`7e3b6b2`, `d81d45b`)
fail 14/14 and 2/2 there.

Fix (`3c8a61e`): `_land` creates an exclusive hidden marker
`.<name>.cbzreserve-<token>` before reserving the destination and removes it
only once the archive is complete; both movers' collision paths, legacy and
resolver, defer while a marker exists. A crashed landing's marker keeps the
name waiting until removed by hand: deliberately conservative; a stale
marker never loses a book. Then `_merge_directories` returns how many files
completed and `_move_cbz_dir` returns no landing for a merge in which none
did, on the race-fallback path too.

Guards proven by bypass (failing tests): the merge defers to a landing in
flight 9; the loose mover too 5; the marker exists before the reservation
12; the owner removes its marker 12; a merge with nothing completed is no
landing 2; the race-fallback merge too 1; only a settled collision completes
3; only a landed file completes 1 (it failed nothing until a test of a name
taken during a folder landing was added). A guard that treated an
unreadable folder as "in flight" was unreachable (`glob` does not raise
there) and was removed.

Round 2 verification at `08b29de` (this commit is docs only): full suite
**5,099 passed / 10 skipped / 1 failed** (5,110 tests = 5,091 + 19). The
failure is again `tests/web/test_web_logs.py::test_alternating_overflow_compacts_map_and_physical_files`;
three isolated reruns afterwards: 1 passed,1 passed,1 passed. Codex also reproduced this
WinError 5 on unmodified base. Codex's four PR #176 repro files pass 16/16
at this head. Watcher bare-LF 871 (869 + two new import lines in the LF
import block).

### PR #176 review round 3 (Codex, at `d47b7d8`)

**P1:** the landing marker was observed, not owned. A writer could see a
complete library book, pause, and then settle against another arrival's
reservation that replaced it meanwhile. Codex's
`test_review176_marker_window.py` (SHA256 `bc9ca1e5...102a`) fails 16/16 at
`d47b7d8`. Scope, narrowed by Codex (PR comment 6757585360): the
demonstrated concurrency is two admitted timer threads in one watcher
process, so an in-process lock per physical destination suffices; other
processes writing the library are documented as outside the guarantee.

Fix (`df2c4b5`): `_settle_or_land` is the one path both movers take for
every file. It takes `_destination_lock(dest)` (a `threading.Lock` per
normalized physical path, from a registry that keeps a lock only while it
is used) **before** deciding anything and holds it through quarantine,
landing, rollback and cleanup; a thread that cannot take it defers its
arrival, untouched, to its next pass. The landing marker and its check
(`RESERVE_MARK`, `_landing_in_flight`) are removed: one mechanism, and no
marker files for a crash to leave behind. The exclusive reservation of the
name in `_land` stays.

Two of Codex's earlier probes no longer apply as written, and this is
expected rather than a regression:

- `test_review176_marker_window.py` patches `_landing_in_flight`, which no
  longer exists (AttributeError). Its interleaving also cannot occur now:
  while the paused writer owns the name, the second writer defers instead
  of reserving it, so its wait for that reservation would time out. My
  `test_a_writer_that_saw_a_clear_target_keeps_it_until_it_is_done` covers
  the same scenario (pause after ownership and a clear observation; the
  other writer must defer; every original survives), both movers, legacy
  and resolver.
- `test_review176_quarantine_race.py::test_concurrent_losing_arrivals_keep_both_quarantine_copies`
  settles one library name from two threads at once, which ownership now
  forbids: the second defers and never reaches the probe's barrier
  (BrokenBarrierError after 10 s). My version of that race uses two library
  roots, where two quarantines of one name can still meet.

The other probes pass: active reservation 8/8, directory receipt 2/2,
failed landing 2/2, and the two failure-cleanup cases of the quarantine
race.

Guards proven by bypass (failing tests): a settlement owns its destination
18; one physical file, one lock 1; ownership ends with the settlement even
on error 1 (it failed nothing until the test held a registry reference, as
another thread would); the registry keeps no lock nobody uses 17.
