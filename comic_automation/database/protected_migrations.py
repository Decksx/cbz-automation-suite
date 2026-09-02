"""Fail-closed protection for migrations that must never run automatically.

Every migration in `comic_automation/database/migrations/` is applied by
`apply_migrations()` on startup: eleven CLI and service entry points call
it, and none of them has any notion of approval, backup or postflight.
That is correct for a migration whose whole content is "add a column",
and it is *wrong* for migration 015.

Why 015 is different
--------------------

Slice 4 (`docs/slice4_migration_design.md`) rebuilds four evidence
tables -- ``archive_hashes``, ``archive_content_signatures``,
``archive_inspections`` and ``near_duplicate_candidates`` -- and binds
every one of 180,519 field projections to a revision taken from an
approved plan artifact. It drops and recreates tables. It cannot be
re-run, and a partial application is not a state any code in this
repository knows how to read.

The design requires (sections 8 and 12) verified writer quiescence, a
protected backup created and verified while quiescent, plan and digest
revalidation *after* the write lock is obtained, statement-by-statement
execution, in-transaction reconciliation, and a restore-from-backup path
for a post-commit failure. None of that can be supplied by a CLI that
happens to open the database on a Tuesday.

So the ordinary path must refuse, and refusing is the whole point
----------------------------------------------------------------

R6 of the design: ordinary commands **fail closed** while a protected
migration is pending. `apply_migrations()` aborts. It specifically does
**not** skip 015 and carry on applying anything else, because "carry on"
means every one of those eleven entry points would then be running
producer code against schema 014 while the operator believes the
migration is merely queued -- the exact split brain section 12.1
describes.

Refusal is also required to be *inert*. `assert_no_pending_protected()`
reads the ledger without creating it (see `recorded_versions`), so a
refusal leaves no schema and no ledger row behind, on a fresh database
as much as on a populated one.

What this module is not
-----------------------

It is not the protected executor. `resolve_protected_execution()` is the
seam that executor will enter through, and it applies nothing at all --
see its docstring for the section 12 obligations that remain
unimplemented and which are therefore *not* enforced by anything in this
module yet.

Protection is declared by version number, in code
-------------------------------------------------

`PROTECTED_MIGRATIONS` is the authority, not a filename convention and
not a marker inside the .sql file. A guard keyed on file content could
be defeated by a file that fails to contain the marker -- which is the
failure direction that matters, since a missing marker reads as "not
protected" and silently permits exactly the run this module exists to
prevent. A version number in source cannot go missing by accident.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from comic_automation.database.migrations import (
    discover_migrations,
    migration_version,
)


# The declaration. A version listed here may never be applied by
# apply_migrations(), whatever its filename is and whether or not the
# file exists yet.
PROTECTED_MIGRATIONS: frozenset[int] = frozenset({15})


# Why each protected version is protected, in operator-facing words: a
# refusal that says only "refused" tells whoever hit it nothing about
# what to do next, and the answer ("ask the operator to run the
# protected executor") is not guessable from the error alone.
PROTECTED_MIGRATION_REASONS: Mapping[int, str] = MappingProxyType(
    {
        15: (
            "Slice 4 rebuilds four evidence tables and binds every row "
            "to a revision from an approved plan artifact. It requires "
            "verified writer quiescence, a verified protected backup, "
            "plan and digest revalidation under the write lock, "
            "statement-by-statement execution, and in-transaction "
            "reconciliation. See docs/slice4_migration_design.md "
            "sections 8 and 12."
        ),
    }
)


# The one directory a protected migration is permitted to live in.
#
# Declared, rather than derived from whatever directory a caller passed,
# because the guard can only protect the root it is pointed at: a
# protected .sql file sitting under some *other* migrations root would be
# discovered and applied by that root's runner with no guard in sight.
# `scripts/db.py` has exactly such an independent root (design section
# 4.1), which is why the disjointness of the two is a test and not a
# comment.
PROTECTED_MIGRATION_ROOT: Path = Path(__file__).resolve().parent / "migrations"


class ProtectedMigrationError(RuntimeError):
    """A protected migration was pending, or the seam was misused.

    Raised on the ordinary path when a protected migration has not been
    applied yet, and by the protected-execution seam when the
    authorization it was given does not match the database's actual
    state. Both are refusals; neither leaves the database changed.
    """


def is_protected(version: int) -> bool:
    """Whether `version` may only be applied by the protected executor."""
    return version in PROTECTED_MIGRATIONS


def recorded_versions(connection: sqlite3.Connection) -> set[int]:
    """Migration versions recorded in the ledger, creating nothing.

    Deliberately *not* `migrations.applied_versions()`, which calls
    `ensure_migration_table()` and so would create ``schema_migrations``
    as a side effect of asking a question. The guard has to be able to
    refuse having mutated nothing whatsoever, including on a database
    that has no ledger yet -- otherwise "a refusal makes no schema
    mutation" would be false in precisely the case where the database is
    most obviously untouched.

    An absent ledger reads as "nothing applied", which is the truth.
    Presence is checked against ``sqlite_master`` rather than by catching
    `sqlite3.OperationalError` around the SELECT, because that except
    clause would also swallow a genuinely broken ledger -- a corrupted or
    renamed table -- and report it as an empty one, turning a database
    that should stop the run into one that looks freshly initialised.
    """
    exists = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'schema_migrations'
        """
    ).fetchone()

    if exists is None:
        return set()

    rows = connection.execute(
        "SELECT version FROM schema_migrations"
    ).fetchall()

    return {int(row[0]) for row in rows}


def discovered_versions(directory: str | Path) -> dict[int, Path]:
    """Every numbered migration file under `directory`, keyed by version.

    A mapping rather than a list because both callers below need to go
    from a version number back to the file that declares it.
    """
    return {
        migration_version(path): path
        for path in discover_migrations(directory)
    }


def pending_protected_versions(
    connection: sqlite3.Connection,
    directory: str | Path,
) -> list[int]:
    """Protected migrations that exist on disk and are not yet recorded.

    Sorted, so a refusal message and a set comparison both read the same
    way every time.

    A protected version with no file is *not* pending: there is nothing
    to apply. That is the state this repository is in today -- 015 is
    declared protected and does not exist -- and it is why wiring the
    guard in changes no existing behaviour.
    """
    discovered = discovered_versions(directory)
    recorded = recorded_versions(connection)

    return sorted(
        version
        for version in discovered
        if is_protected(version) and version not in recorded
    )


def describe_pending_protected(versions: Iterable[int]) -> str:
    """The operator-facing refusal text for `versions`."""
    lines = [
        "Refusing to apply migrations: a protected migration is pending "
        "and may only be applied by the operator through the protected "
        "executor.",
    ]

    for version in sorted(versions):
        reason = PROTECTED_MIGRATION_REASONS.get(
            version, "No reason recorded for this protected version."
        )
        lines.append(f"  migration {version:03d}: {reason}")

    lines.append(
        "Nothing was applied and the database was not modified. Read-only "
        "access is unaffected: use comic_automation.database.read_guards."
        "readonly_database_connection()."
    )

    return "\n".join(lines)


def assert_no_pending_protected(
    connection: sqlite3.Connection,
    directory: str | Path,
) -> None:
    """The central guard. Raise if any protected migration is pending.

    Called by `apply_migrations()` before it applies anything at all, so
    a pending protected migration aborts the whole run rather than
    letting the unprotected migrations below it land first. Applying
    those and stopping would leave the schema somewhere between two
    releases with no ledger row saying so.

    Reads only, and creates nothing (`recorded_versions`), so the refusal
    path is inert.
    """
    pending = pending_protected_versions(connection, directory)

    if pending:
        raise ProtectedMigrationError(describe_pending_protected(pending))


@dataclass(frozen=True)
class ProtectedExecutionAuthorization:
    """Names the exact protected versions one protected execution covers.

    An explicit object rather than a boolean flag: `allow_protected=True`
    would authorize whatever happened to be pending, so a second
    protected migration arriving in a later release would be swept along
    by an approval that never mentioned it. Naming the versions makes
    that impossible to express.

    `operator` and `reason` are recorded because the postflight artifact
    has to say on whose authority the run happened; they are carried, not
    validated, since this module cannot check either claim.
    """

    versions: frozenset[int]
    operator: str
    reason: str

    def __post_init__(self) -> None:
        # Normalized so a caller passing a set, list or tuple still gets
        # the hashable frozen field this frozen dataclass advertises.
        object.__setattr__(self, "versions", frozenset(self.versions))

        if not self.versions:
            raise ProtectedMigrationError(
                "A protected execution authorization must name at least "
                "one version."
            )

        unprotected = sorted(
            version for version in self.versions if not is_protected(version)
        )

        if unprotected:
            raise ProtectedMigrationError(
                "A protected execution authorization may only name "
                f"protected versions; {unprotected} are not in "
                "PROTECTED_MIGRATIONS. An ordinary migration is applied "
                "by apply_migrations(), not through this seam."
            )

        if not self.operator.strip():
            raise ProtectedMigrationError(
                "A protected execution authorization must name the "
                "operator who granted it."
            )


def resolve_protected_execution(
    connection: sqlite3.Connection,
    directory: str | Path,
    authorization: ProtectedExecutionAuthorization,
) -> tuple[Path, ...]:
    """Resolve the migration files an authorized protected run covers.

    **This is the seam, not the executor.** It opens no transaction,
    executes no migration statement, writes no ledger row and changes
    nothing. It answers one question -- "which files does this
    authorization entitle the caller to apply, and does the database
    agree that is exactly what is pending?" -- and hands back the paths
    in version order.

    Refuses unless the pending protected set is *exactly* the authorized
    set. Both directions matter: an authorization naming a version that
    is not pending is stale or wrong, and a pending version the
    authorization does not name is a migration nobody approved.

    Deliberately **not** implemented here, and therefore not enforced by
    anything yet (design sections 8 and 12): verified writer quiescence,
    the protected backup, plan-digest and expected-count revalidation,
    the ledger preconditions of section 12.1 steps 3-4 (recorded against
    discovered set equality, and the next-applicable test),
    statement-by-statement application, the in-transaction ledger row,
    and the section 12.2 reconciliation. A caller holding these paths has
    been authorized and has satisfied none of those obligations. They
    belong to the executor that is built in a later slice.
    """
    pending = set(pending_protected_versions(connection, directory))

    if pending != authorization.versions:
        raise ProtectedMigrationError(
            "Protected execution refused: the authorization names "
            f"{sorted(authorization.versions)} but the pending protected "
            f"set is {sorted(pending)}. These must match exactly."
        )

    discovered = discovered_versions(directory)

    # Cannot fire given the equality above -- every pending version came
    # from `discovered` -- and asserted anyway, because it is the check
    # that would catch a future pending-set computation that stopped
    # deriving from the files on disk.
    missing = sorted(
        version
        for version in authorization.versions
        if version not in discovered
    )

    if missing:
        raise ProtectedMigrationError(
            "Protected execution refused: no migration file found for "
            f"{missing} under {Path(directory)}."
        )

    return tuple(
        discovered[version] for version in sorted(authorization.versions)
    )
