# Development log 2026-09-02 -- slice 4A, the protected-migration guard

Branch `slice4/protected-migration-guard`, off `master` at
`4bdc5d90795369600f9fe69c17689c61fc7d31c2`.

Scope is section 4 of `docs/slice4_migration_design.md` and its tests only.
No migration 015, no producer change, no production access.
`G:\ComicAutomation\` was not opened.

## What landed

```text
comic_automation/database/protected_migrations.py   new    the declaration,
                                                           the central guard,
                                                           the execution seam
comic_automation/database/migrations.py             +20    guard wired into
                                                           apply_migrations()
tests/test_protected_migrations.py                  new    36 tests
tests/test_archive_fault_injection.py               +13    version derivation
                                                           re-pointed
docs/engineering_decisions.md                       +64    the decision and
                                                           its stated limits
```

## Test count, reconciled

```text
master 4bdc5d9   2065 passed, 2 skipped   (recorded in the slice 4A handoff)
this branch      2101 passed, 2 skipped
delta            +36 passed, +0 skipped
tests added      36, all in tests/test_protected_migrations.py
```

The skip count is unchanged and that is deliberate. An intermediate revision
of this branch measured `2099 passed, 3 skipped`, because the seam's
set-equality test skipped itself when only one protected version is declared.
That skip is what the bypass sweep below caught; the test now patches the
declaration instead of skipping, so it counts as a pass and the delta is
exactly the number of tests written.

Python 3.11.3, Windows checkout, `python -m pytest -q`, clean tree.

## Guard-bypass evidence

Each guard was disabled **alone**, with every other guard in place, and the
full suite was run against the bypass. Suite baseline for every row:
`2101 passed, 2 skipped`.

```text
B1  apply_migrations() no longer calls the guard          7 failing
      test_apply_migrations_refuses_while_a_protected_migration_is_pending
      test_apply_migrations_refuses_rather_than_skipping_the_protected_one
      test_a_refusal_mutates_neither_schema_nor_ledger
      test_apply_migrations_resumes_once_the_protected_version_is_recorded
      test_entry_point_with_an_argument_directory_fails_closed
      test_entry_point_with_a_module_constant_fails_closed
      test_the_service_fails_closed_at_startup

B2  guard moved AFTER ensure_migration_table()            1 failing
      test_a_refusal_on_a_fresh_database_creates_no_ledger

B3  pending set ignores the ledger                        2 failing
      test_a_recorded_protected_migration_is_no_longer_pending
      test_apply_migrations_resumes_once_the_protected_version_is_recorded

B4  recorded_versions() creates the ledger                2 failing
    (replaced by applied_versions())
      test_a_refusal_on_a_fresh_database_creates_no_ledger
      test_asking_the_question_does_not_create_the_ledger

B5  seam accepts a subset instead of set equality         1 failing
      test_the_seam_refuses_when_a_pending_version_is_unauthorized

B6  authorization accepts unprotected versions            1 failing
      test_an_authorization_may_only_name_protected_versions

B7  authorization accepts an empty version set            1 failing
      test_an_authorization_must_name_at_least_one_version

B8  seam's missing-file check removed                     0 failing
      UNREACHABLE. Not a proven guard. See below.
```

### B2 and B5 failed nothing on the first sweep

Both are recorded because the sweep is the only reason they are tested now,
and because the first counts were 0 and 0.

**B2.** Two tests already asserted that a refusal creates nothing, and neither
could see the ordering. `test_asking_the_question_does_not_create_the_ledger`
calls the guard directly, so `apply_migrations()` is not in the picture at
all. `test_a_refusal_mutates_neither_schema_nor_ledger` refuses against a
database that already has a ledger, where `ensure_migration_table()` is a
no-op either way. Only a **fresh** database separates the two orderings:
refusing after that call leaves a `schema_migrations` table created by a run
that applied nothing. `test_a_refusal_on_a_fresh_database_creates_no_ledger`
was written for exactly that, and B2 now fails it by name.

**B5.** `test_the_seam_refuses_when_a_pending_version_is_unauthorized` was
skipping itself, because expressing pending-but-unauthorized needs two
pending protected versions and the declaration names one. A skipped test
reports green and asserts nothing. It now patches `PROTECTED_MIGRATIONS`
rather than skipping: `is_protected()` reads the module global at call time,
so the pending-set computation and the authorization's own validation both
see the extended policy while the code under test remains the shipped code.

### B8 is unreachable, and is recorded as unreachable

The `missing`-file check in `resolve_protected_execution()` cannot fire: the
set-equality check above it guarantees every authorized version came from the
discovered files. Bypassing it alone fails nothing, and that is reported as
the fact it is rather than papered over with a test that reaches it by
patching. It is kept as an assertion against a future pending-set computation
that stops deriving from the files on disk, and it is written down in
`docs/engineering_decisions.md` as unreachable defensive code.

## Injection-site re-audit

`tests/test_archive_fault_injection.py`'s
`test_a_commit_failure_inside_a_migration_restores_the_tables` failed when the
guard landed. It derives its injected migration's version as one past the real
set -- 14 + 1 = **15**, the first declared protected version -- so
`apply_migrations()` refused the call before `BEGIN IMMEDIATE`, the injected
COMMIT failure never fired, and the test failed for a reason unrelated to
rollback. The derivation now skips protected versions.

The injection **site** did not move. The guard is checked before
`ensure_migration_table()`, well ahead of the `BEGIN IMMEDIATE` and `COMMIT`
that test injects at, so the probe still lands where it did and
`guarded.matches == 1` still holds. No other fault-injection call site in
`tests/fault_injection.py` is touched by this change.

This is the same failure class the test's own comment already records for the
014 collision, one constraint further along: a derived version is only safe
against the constraints it actually derives from.

## Platform-specific measurements

None. Nothing measured here is platform-dependent: the guard is set arithmetic
over migration filenames plus a `sqlite_master` lookup. The suite counts above
are from a Windows checkout on Python 3.11.3, and CI runs the same suite on
`windows-latest` / 3.11, so they are not a cross-platform claim either way.

## Line endings

```text
file                                                index  bare-LF before -> after
comic_automation/database/migrations.py             lf     0 -> 0
tests/test_archive_fault_injection.py               lf     0 -> 0
docs/engineering_decisions.md                       lf     0 -> 0
comic_automation/database/protected_migrations.py   lf     (new file)
tests/test_protected_migrations.py                  lf     (new file)
docs/development_log_2026-09-02.md                  lf     (new file)
```

No edit normalized anything. Both new source files land as `i/lf`, matching
their directories' convention: `comic_automation/database/` is entirely `i/lf`,
and `tests/` is 77 `i/lf` to 3 `i/crlf`.

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
the disjointness of the two roots is now asserted by tests rather than left as
a comment.

One documentation detail was deliberately left alone: the header of
`docs/slice4_migration_design.md` still reads "design under review", which the
exact-SHA approval and the merge of PR #89 have overtaken. Per the standing
handoff it is corrected in a one-file commit the next time that document is
legitimately changed, not in a standalone cleanup PR and not here.

## Not touched

`G:\ComicAutomation\`, `X:\`, `\\tower\`, the production database, protected
backups. Every test writes to `tmp_path`. The untracked `Logs/*.out` files and
`routing.json.bak-2026-08-19` present at branch time were left alone.
