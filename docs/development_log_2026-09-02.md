# Development log 2026-09-02 -- slice 4A, the protected-migration guard

Branch `slice4/protected-migration-guard`, off `master` at
`4bdc5d90795369600f9fe69c17689c61fc7d31c2`.

Scope is section 4 of `docs/slice4_migration_design.md` and its tests only.
No migration 015, no producer change, no production access.
`G:\ComicAutomation\` was not opened.

Three review rounds, all recorded. Round 1 landed the guard. Round 2 fixed a
**fail-open** defect round 1 had shipped and corrected two wrong
durable-evidence claims. Round 3 showed that round 2's own bypass evidence was
wrong -- a guard reported as unobservable was observable under a stronger
scenario -- and that the call-site census was both too narrow and untested.

Each round found the previous round's evidence weaker than it claimed. That is
the point of writing all three down: a log that shows only the final state
cannot be told apart from one where nothing went wrong.

## What landed

```text
comic_automation/database/protected_migrations.py   new    the declaration,
                                                           the snapshot, the
                                                           guard, the apply-set
                                                           invariant, the seam
comic_automation/database/migrations.py             +33    applies from the
                                                           snapshot the guard
                                                           judged
tests/test_protected_migrations.py                  new    54 tests
tests/test_archive_fault_injection.py               +13    version derivation
                                                           re-pointed
tests/test_archive_revisions.py                     +26    stale patch target
                                                           re-pointed
docs/engineering_decisions.md                      +128    the decisions and
                                                           their stated limits
```

## The round-2 blocker: the guard was fail-open

Round 1 read the migrations directory **twice** -- once in the guard, once in
`apply_migrations()` to build its apply list. Review injected a file between
the two scans:

```text
guard scans      {1}          -> nothing protected, proceed
015 arrives on disk
applier scans    {1, 15}      -> applies 1 AND 15
result           [1, 15]
ledger           [(1, ...), (15, '015_arrived_late.sql')]
```

Migration 015's schema object was created by an ordinary command. The seam had
the same shape: it computed the pending set from one scan and mapped versions
back to paths from another, so a protected 016 arriving in between let an
authorization for `{15}` succeed while the real pending set was `{15, 16}`.

`MigrationSnapshot` is now one frozen reading of the directory and the ledger.
The guard, the apply plan and the seam all derive from it. The incoherent
shape is unrepresentable rather than discouraged: no function in the module
takes a connection and a directory and answers a question about them.

### The snapshot is not the fix by itself

Three independent things stand between a stale plan and protected SQL:

```text
1  the guard        refuses while a protected version is pending
2  the plan filter  ordinary_apply_plan() excludes protected versions
                    however the snapshot it was built from was obtained
3  the invariant    assert_no_protected_in_apply_set() re-checks the plan
                    itself, immediately before any SQL runs
```

Measured by peeling them apart against the real `apply_migrations()`, with a
protected `015` **and** an unprotected `016` arriving the instant the single
reading completes:

```text
layers disabled                       outcome
none                                  [1]          015 and 016 excluded
re-scan                               [1, 16]      R6 VIOLATED
plan filter                           [1]
invariant                             [1]
plan filter + re-scan                 REFUSED      invariant fired
plan filter + invariant + re-scan     [1, 15, 16]  protected SQL ran
```

Every layer is load-bearing on its own. None is sufficient: protected SQL
actually executing still needs all three gone.

### Round 2 got this table wrong, and how

Round 2 ran the same peeling with **only** `015` arriving, and recorded that
reintroducing the second scan alone was unobservable -- filing it as defence in
depth rather than a guard. With only the protected file arriving, the plan
filter drops it, the invariant finds nothing protected, and the run applies
`[1]` either way, so the bypass genuinely produced no failure.

Review supplied the stronger scenario. With `016` sitting above the protected
version, the same bypass gives:

```text
result [1, 16]
ledger [(1, ...), (16, '016_after_protected.sql')]
tables [..., 'ordinary_016_ran', ...]
```

No protected SQL executed, and R6 was still violated: the run **skipped a
protected migration and carried on past it**, which design section 4.2 forbids
in those words.

The generalisable finding, which is the reason this is in the log rather than
only in the diff: **"no protected SQL ran" is a weaker property than "refuse
rather than skip and continue", and a test asserting only the weaker one reads
exactly like a test asserting both.** That is how a real defect spent a review
round wearing the label "defence in depth". The round-2 docstring that recorded
the bypass as unobservable was itself the artifact of the too-weak scenario.

## Test count, reconciled

```text
master 4bdc5d9   2065 passed, 2 skipped   (recorded in the slice 4A handoff)
this branch      2119 passed, 2 skipped
delta            +54 passed, +0 skipped
tests added      54, all in tests/test_protected_migrations.py
```

The skip count is unchanged, deliberately. An intermediate round-1 revision
measured `2099 passed, 3 skipped`, because the seam's set-equality test
skipped itself when only one protected version is declared. That skip is what
the round-1 bypass sweep caught; the test now patches the declaration instead
of skipping.

Python 3.11.3, Windows checkout, `python -m pytest -q`, clean tree.

## Guard-bypass evidence

Each guard disabled **alone**, with every other guard in place, full suite run
against the bypass. Baseline for every row: `2119 passed, 2 skipped`.

```text
B1   apply_migrations() no longer calls the guard          9 failing
       test_apply_migrations_refuses_while_a_protected_migration_is_pending
       test_apply_migrations_refuses_rather_than_skipping_the_protected_one
       test_a_protected_file_arriving_after_the_guard_is_not_applied
       test_a_refusal_mutates_neither_schema_nor_ledger
       test_a_refusal_on_a_fresh_database_creates_no_ledger
       test_apply_migrations_resumes_once_the_protected_version_is_recorded
       test_entry_point_with_an_argument_directory_fails_closed
       test_entry_point_with_a_module_constant_fails_closed
       test_the_service_fails_closed_at_startup

B2   guard moved AFTER ensure_migration_table()            1 failing
       test_a_refusal_on_a_fresh_database_creates_no_ledger

B3   pending set ignores the ledger                       54 failing
       every migration-idempotency test in the suite, across
       test_service, test_archive_revisions, test_archive_supersession,
       test_enqueue_missing_stages, test_migrations and eleven other files

B4   recorded_versions() creates the ledger                2 failing
       test_a_refusal_on_a_fresh_database_creates_no_ledger
       test_asking_the_question_does_not_create_the_ledger

B5   seam accepts a subset instead of set equality         2 failing
       test_the_seam_refuses_when_a_pending_version_is_unauthorized
       test_the_seam_cannot_be_split_by_a_second_protected_file

B6   authorization accepts unprotected versions            1 failing
       test_an_authorization_may_only_name_protected_versions

B7   authorization accepts an empty version set            1 failing
       test_an_authorization_must_name_at_least_one_version

B8   authorization accepts an unnamed operator             1 failing
       test_an_authorization_must_name_its_operator

B9   apply plan stops filtering protected versions         1 failing
       test_a_pending_protected_version_stays_out_of_the_apply_plan

B10  apply-set invariant removed                           1 failing
       test_a_broken_plan_builder_is_stopped_before_any_sql_runs

B11  snapshot discovery re-scans instead of being reused   1 failing
       test_the_plan_comes_from_the_guarded_snapshot_not_a_fresh_scan
       (0 failing in round 2 -- see "Round 2 got this table wrong")

B12  duplicate versions collapse silently again            3 failing
       test_two_files_claiming_one_version_are_refused
       test_a_duplicated_protected_version_is_refused_as_ambiguous
       test_apply_migrations_refuses_a_duplicate_version_directory

B13  census source discovery replaced by a fixed answer    9 failing
       test_the_census_recognizes_every_import_form
       test_the_census_scans_every_production_directory
       test_the_census_skips_the_tests_tree
       test_the_census_ignores_an_independent_implementation
       test_the_census_ignores_strings_and_comments
       test_the_census_rejects_a_form_it_cannot_classify
       test_the_census_rejects_a_bare_call_it_cannot_source
       test_the_census_refuses_a_file_it_cannot_parse
       test_the_census_detects_a_caller_the_root_map_does_not_list
```

Round 1's B8 was the seam's missing-file check, which was unreachable and has
since been **deleted** rather than kept -- with the snapshot, every pending
version comes out of `snapshot.discovered` by construction, so nothing is left
for it to catch. B8 is now the non-empty-operator refusal, which round 2
review correctly pointed out was a guard with no bypass row at all.

**Every guard now fails at least one named test when disabled alone.** No row
is filed as unreachable or as defence in depth; the two that were, in rounds 1
and 2, are recorded below as the mistakes they were.

### Round 3's finding: B11 was a real guard reported as a cushion

Covered above under "Round 2 got this table wrong". Recorded here as well
because it belongs in the bypass record and not only in the design narrative:
B11 moved from `0 failing` to `1 failing` with no change to the guard at all.
Only the test's scenario changed, from planting `015` alone to planting `015`
and an unprotected `016`.

A bypass row of `0 failing` means one of two things, and they are not
distinguishable by looking at the number: the guard is genuinely unreachable,
or the tests are too weak to reach it. Round 2 assumed the first and wrote it
down. The second was true.

### Round 1's two zero-failure findings

Both are kept in the record because the sweep is the only reason they are
tested at all, and because their first counts were 0 and 0.

**The guard's position** relative to `ensure_migration_table()` broke no test.
The two existing "creates nothing" tests could not see it: one calls the guard
directly, and the other refuses against a database that already has a ledger,
where `ensure_migration_table()` is a no-op. A fresh database is the only
state that separates the orderings.

**The seam's set equality** broke no test, because
`test_the_seam_refuses_when_a_pending_version_is_unauthorized` was skipping
itself -- expressing pending-but-unauthorized needs two pending protected
versions and the declaration names one. A skipped test reports green and
asserts nothing. It now patches `PROTECTED_MIGRATIONS`: `is_protected()` reads
the module global at call time, so the pending-set computation and the
authorization's own validation both see the extended policy while the code
under test stays the shipped code.

## Injection-site re-audits

Two, both caught by the suite rather than by inspection.

**`tests/test_archive_fault_injection.py`** derives its injected migration's
version as one past the real set -- 14 + 1 = **15**, the first declared
protected version -- so `apply_migrations()` refused the call before
`BEGIN IMMEDIATE`, the injected COMMIT failure never fired, and the test
failed for a reason unrelated to rollback. The derivation now skips protected
versions. The injection *site* did not move: the guard is checked well ahead
of the `BEGIN IMMEDIATE` and `COMMIT` the probe targets, and
`guarded.matches == 1` still holds.

**`tests/test_archive_revisions.py::_apply_through`** patched
`migrations.discover_migrations` to stop the migration set at a chosen
version. Discovery moved into `take_migration_snapshot()`, and
`protected_migrations` binds that name at import time, so patching the module
that *defines* it stopped reaching the call. It failed in the worst available
way: the helper silently applied every migration instead of stopping at 13, so
six tests that exist to exercise the 13 -> 14 backfill were handed a database
already at 14. The patch now targets the binding that actually runs, and the
helper asserts the schema it produced, so the next move of that call site
fails inside the helper by name rather than as six unrelated backfill
failures.

No other fault-injection call site in `tests/fault_injection.py` is touched by
this change.

## Platform-specific measurements

None. The guard is set arithmetic over migration filenames plus a
`sqlite_master` lookup, and an AST walk over source files. The suite counts
above are from a Windows checkout on Python 3.11.3, and CI runs the same suite
on `windows-latest` / 3.11, so they are not a cross-platform claim either way.
Review's paired Linux / Python 3.12 runs confirmed the branch delta on that
platform too, against a baseline carrying 198 platform-dependent failures on
both sides.

## Line endings

The round-1 version of this section was **wrong**, and is corrected here
rather than quietly rewritten. It printed a column headed "bare-LF" showing
`0 -> 0` for every file. Those were CRLF counts of the *working-tree*
checkouts, which are `w/crlf` under `core.autocrlf=true` and whose bare-LF
count is trivially zero. They were not measurements of the blobs, and the
label made them read as if they were.

Every changed blob is `i/lf`. Measured with `git show <rev>:<path>`:

```text
file                                                  LF lines      CR bytes
comic_automation/database/migrations.py               125 -> 158     0 -> 0
comic_automation/database/protected_migrations.py     new, 506       0
tests/test_protected_migrations.py                    new, 1934      0
tests/test_archive_fault_injection.py                 765 -> 778     0 -> 0
tests/test_archive_revisions.py                      1479 -> 1505    0 -> 0
docs/engineering_decisions.md                         507 -> 662     0 -> 0
docs/development_log_2026-09-02.md                    new            0
```

(This file's own LF count is not listed: it is the file being written, so
any figure here would be measuring a draft. Its CR count is 0, which is the
part that is checkable and the part that matters.)

**CR bytes `0 -> 0` is the claim that matters**: no edit introduced a carriage
return into an LF blob, so nothing was normalized and no file moved toward
`mixed`. The LF-line counts moving is just the size of each diff. Both new
source files land `i/lf`, matching their directories -- `comic_automation/database/`
is entirely `i/lf`, and `tests/` is 77 `i/lf` to 3 `i/crlf`.

`git diff --check` with the git-derived exclusions (45 files) exits 0.

## The call-site census, corrected twice

Round 1 claimed that a twelfth `apply_migrations()` call site would fail
`test_every_entry_point_points_at_the_protected_root`'s count assertion. It
would not have: the assertion counted a dictionary written by hand in the
test, which is length 11 whatever the source tree does. Future-detection
theatre.

Round 2 replaced it with an AST census. Round 3 found that census had three
defects, all of the same kind -- it answered a narrower question than its
callers believed, and its own test could not tell.

**Scope.** It walked `comic_automation/` and `scripts/` only. `apps/` exists
in this repository and was outside the scan entirely, so an entry point there
was invisible. It now walks the repository and removes only a skip list
(`tests/`, caches, vendored trees), so a caller added under a directory
invented later is inside the census by default rather than by amendment.

**Import forms.** Only the bare call was counted. Measured by review:

```text
aliased direct import   imports_it=True    counted_calls=0
module import           imports_it=False   counted_calls=0
```

All six forms that reach this function are now recognized -- bare, aliased,
`from ...database import migrations`, that with an alias, `import ... as`, and
the fully dotted call. A module defining its own `apply_migrations` and calling
it bare still counts zero: that is `scripts/db.py`, out of scope by design
section 4.1.

Anything else naming `apply_migrations` in call position now raises
`CensusError`, as does a file that cannot be read or parsed. Counting zero is
the dangerous answer for a census whose entire job is to notice a caller
nobody told it about, and an unparseable file is not a file with no callers.

**The test.** `test_the_call_site_census_detects_a_new_caller` re-implemented
the parsing predicates inline instead of calling the helper, so it passed while
exercising none of the helper's code. Review proved it by replacing the helper
with a fixed eleven-entry dictionary:

```text
2 passed
```

`_apply_migrations_call_sites()` now takes a root, and all eight census tests
point the **real** function at an injected temporary source tree. The same
fixed-dictionary substitution is bypass row B13 above and fails nine tests by
name.

Measured against the tree: 11 modules, 11 calls, matching design section 4's
list exactly.

## A docstring that described a state its own fixture ruled out

`ordinary_apply_plan()` said its protected filter covered an "already
recorded" protected version. It cannot: `pending()` subtracts the recorded set
before the filter runs, so everything reaching it is pending and the guard
would have refused the whole run over it.

`test_a_recorded_protected_version_stays_out_of_the_apply_plan` was named for
the same impossible state while its fixture set `recorded=frozenset()` --
both versions pending. The test was always constructing the pending case; only
its name and its explanation described the other one. Renamed to
`test_a_pending_protected_version_stays_out_of_the_apply_plan`, and the source
docstring now says what the filter actually is: a layer behind the guard for
when the plan is not built from the snapshot the guard judged, which is
necessary and not sufficient.

Worth recording as a pattern rather than a typo. A comment describing a
fixture state the code makes unreachable is not caught by any test, because
the test passes for the real reason while its name advertises the imaginary
one.

## Deliberately not done

Scoped out of slice 4A by the task, not judged unnecessary:

```text
migration 015 SQL                        slice 4 execution phase
the protected executor                   section 12, a later PR
section 12.1 ledger preconditions 3-4    the executor's; the seam checks
                                         only that authorized == pending
section 12.2 applied projection          the executor's
producer cutover (section 7)             slice 4, a later PR
page_inventory / archive_pages           slice 4p
candidate parameters, parameters_basis   slice 6
```

`scripts/db.py` is untouched by design (section 4.1). Its independent
`apply_migrations` over `<repo root>/migrations` will never discover 015, and
the disjointness of the two roots is asserted by tests rather than left as a
comment.

One behavioural change is **not** scoped out and is called out because it
would otherwise be a quiet exception to "ordinary migrations are unaffected":
a duplicate-version directory used to half-apply and now refuses outright.
`schema_migrations.version` is an INTEGER PRIMARY KEY, so two files claiming
one version describe a state the ledger cannot hold.

One documentation detail was deliberately left alone: the header of
`docs/slice4_migration_design.md` still reads "design under review", which the
exact-SHA approval and the merge of PR #89 have overtaken. Per the standing
handoff it is corrected in a one-file commit the next time that document is
legitimately changed, not in a standalone cleanup PR and not here.

## Not touched

`G:\ComicAutomation\`, `X:\`, `\\tower\`, the production database, protected
backups. Every test writes to `tmp_path`. The untracked `Logs/*.out` files and
`routing.json.bak-2026-08-19` present at branch time were left alone.
