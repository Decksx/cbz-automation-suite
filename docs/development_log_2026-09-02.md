# Development log 2026-09-02 -- slice 4A, the protected-migration guard

Branch `slice4/protected-migration-guard`, off `master` at
`4bdc5d90795369600f9fe69c17689c61fc7d31c2`.

Scope is section 4 of `docs/slice4_migration_design.md` and its tests only.
No migration 015, no producer change, no production access.
`G:\ComicAutomation\` was not opened.

Two review rounds. Round 1 landed the guard; round 2 fixed a **fail-open**
defect round 1 had shipped, and corrected two durable-evidence claims that
were wrong. Both rounds are recorded, because a log that only shows the
final state cannot be told apart from one where nothing went wrong.

## What landed

```text
comic_automation/database/protected_migrations.py   new    the declaration,
                                                           the snapshot, the
                                                           guard, the apply-set
                                                           invariant, the seam
comic_automation/database/migrations.py             +33    applies from the
                                                           snapshot the guard
                                                           judged
tests/test_protected_migrations.py                  new    46 tests
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

The obvious reading -- "one snapshot closes it" -- is wrong, and the wrong
reading is the dangerous one, because it invites dropping the other two
layers. Three independent things stand between a stale plan and protected SQL:

```text
1  the guard        refuses while a protected version is pending
2  the plan filter  ordinary_apply_plan() excludes protected versions
                    however the snapshot it was built from was obtained
3  the invariant    assert_no_protected_in_apply_set() re-checks the plan
                    itself, immediately before any SQL runs
```

Measured by peeling them apart against the review's own scenario -- 015
arrives the instant the single reading completes -- through the real
`apply_migrations()`:

```text
layers disabled                          outcome
none                                     [1]      015 not applied
re-scan                                  [1]      015 not applied
plan filter                              [1]      015 not applied
plan filter + re-scan                    REFUSED  invariant fired
plan filter + invariant                  [1]      015 not applied
plan filter + invariant + re-scan        [1, 15]  THE REPORTED DEFECT
```

The review's exact result returns only with all three disabled together, and
any one of them standing prevents it.

## Test count, reconciled

```text
master 4bdc5d9   2065 passed, 2 skipped   (recorded in the slice 4A handoff)
this branch      2111 passed, 2 skipped
delta            +46 passed, +0 skipped
tests added      46, all in tests/test_protected_migrations.py
```

The skip count is unchanged, deliberately. An intermediate round-1 revision
measured `2099 passed, 3 skipped`, because the seam's set-equality test
skipped itself when only one protected version is declared. That skip is what
the round-1 bypass sweep caught; the test now patches the declaration instead
of skipping.

Python 3.11.3, Windows checkout, `python -m pytest -q`, clean tree.

## Guard-bypass evidence

Each guard disabled **alone**, with every other guard in place, full suite run
against the bypass. Baseline for every row: `2111 passed, 2 skipped`.

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
       test_a_recorded_protected_version_stays_out_of_the_apply_plan

B10  apply-set invariant removed                           1 failing
       test_a_broken_plan_builder_is_stopped_before_any_sql_runs

B11  snapshot discovery re-scans instead of being reused   0 failing
       DEFENCE IN DEPTH. Not independently observable. See below.

B12  duplicate versions collapse silently again            3 failing
       test_two_files_claiming_one_version_are_refused
       test_a_duplicated_protected_version_is_refused_as_ambiguous
       test_apply_migrations_refuses_a_duplicate_version_directory
```

B8 in round 1 was the seam's missing-file check, which was unreachable and has
since been **deleted** rather than kept -- with the snapshot, every pending
version comes out of `snapshot.discovered` by construction, so nothing is left
for it to catch. B8 is now the non-empty-operator refusal, which review
correctly pointed out was a guard with no bypass row at all.

### B11 fails nothing, and that is reported rather than fixed up

Re-introducing the second scan on its own applies no protected migration,
because layers 2 and 3 above still hold. It was tempting to write a test that
reaches it by disabling something else at the same time; that would be a
bypass of two guards reported as one, which is the thing the injection-site
gate exists to stop. The peeling table above is the honest form of the same
evidence.

An earlier draft of
`test_the_plan_comes_from_the_guarded_snapshot_not_a_fresh_scan`'s docstring
claimed the bypass would fail it. It does not. The docstring now says so
outright -- a comment asserting a bypass result that was never measured is
worse than no comment, because it is read as evidence.

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
`sqlite_master` lookup. The suite counts above are from a Windows checkout on
Python 3.11.3, and CI runs the same suite on `windows-latest` / 3.11, so they
are not a cross-platform claim either way. Review's paired Linux / Python 3.12
run confirmed the same `+46` delta against a baseline carrying 198
platform-dependent failures on both sides.

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
comic_automation/database/protected_migrations.py     new, 494       0
tests/test_protected_migrations.py                    new, 1548      0
tests/test_archive_fault_injection.py                 765 -> 778     0 -> 0
tests/test_archive_revisions.py                      1479 -> 1505    0 -> 0
docs/engineering_decisions.md                         507 -> 635     0 -> 0
docs/development_log_2026-09-02.md                    new            0
```

**CR bytes `0 -> 0` is the claim that matters**: no edit introduced a carriage
return into an LF blob, so nothing was normalized and no file moved toward
`mixed`. The LF-line counts moving is just the size of each diff. Both new
source files land `i/lf`, matching their directories -- `comic_automation/database/`
is entirely `i/lf`, and `tests/` is 77 `i/lf` to 3 `i/crlf`.

`git diff --check` with the git-derived exclusions (45 files) exits 0.

## A claim that was removed rather than repaired

`test_every_entry_point_points_at_the_protected_root()` said in round 1 that a
twelfth `apply_migrations()` call site would fail its count assertion. It would
not have: the assertion counted a dictionary written by hand in the test, which
is length 11 whatever the source tree does. The claim was future-detection
theatre.

It is now backed by `_apply_migrations_call_sites()`, an AST census of
`comic_automation/` and `scripts/` that counts calls in modules importing
`apply_migrations` from `comic_automation.database.migrations` -- which is what
separates them from `scripts/db.py`'s independent implementation of the same
name, and from the definition itself. The census is compared against the
hand-written root map, so a new caller makes the two sets differ. It has its
own fault-injection test rather than being trusted.

Parsed rather than grepped, so a call inside a string or comment cannot
inflate the count and a line-wrapped call cannot escape it. Measured: 11
modules, 11 calls, matching design section 4's list exactly.

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
