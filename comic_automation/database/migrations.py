from __future__ import annotations

import sqlite3
from pathlib import Path


def ensure_migration_table(connection: sqlite3.Connection) -> None:
    # Tracks which numbered migration files have already been applied
    # to this database, so apply_migrations() can be called repeatedly
    # (e.g. on every service/CLI startup) and only run new migrations.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def applied_versions(connection: sqlite3.Connection) -> set[int]:
    ensure_migration_table(connection)

    # Every version number that has already been recorded as applied.
    rows = connection.execute(
        "SELECT version FROM schema_migrations"
    ).fetchall()

    return {int(row["version"]) for row in rows}


def discover_migrations(directory: str | Path) -> list[Path]:
    migration_dir = Path(directory)

    if not migration_dir.exists():
        raise FileNotFoundError(
            f"Migration directory not found: {migration_dir}"
        )

    return sorted(migration_dir.glob("[0-9][0-9][0-9]_*.sql"))


def migration_version(path: Path) -> int:
    prefix = path.stem.split("_", 1)[0]
    return int(prefix)


def iter_sql_statements(sql: str) -> list[str]:
    """
    Split a migration script into complete SQLite statements.

    sqlite3.complete_statement() understands quoted strings and comments,
    making it safer than splitting directly on semicolons.
    """
    statements: list[str] = []
    buffer: list[str] = []

    for line in sql.splitlines():
        buffer.append(line)
        candidate = "\n".join(buffer).strip()

        if candidate and sqlite3.complete_statement(candidate):
            statements.append(candidate)
            buffer.clear()

    remainder = "\n".join(buffer).strip()
    if remainder:
        raise ValueError(
            "Migration contains an incomplete SQL statement."
        )

    return statements


def apply_migrations(
    connection: sqlite3.Connection,
    directory: str | Path,
) -> list[int]:
    # Checked before ensure_migration_table(), and before any
    # migration is applied, so a refusal creates no ledger row, no
    # ledger table and no schema change -- and applies nothing at
    # all, not even the unprotected migrations queued below the
    # protected one. Applying those and stopping would leave the
    # schema between two releases with no ledger row saying so.
    #
    # Imported inside the function rather than at module scope:
    # protected_migrations reuses this module's discovery and
    # version-parsing primitives, so importing it from here at
    # module scope is a cycle. The reuse is deliberate -- a guard
    # carrying its own copy of the filename parser could disagree
    # with the applier about which version a file declares, and a
    # disagreement in the 'not protected' direction is silent.
    from comic_automation.database.protected_migrations import (
        assert_no_pending_protected,
    )

    assert_no_pending_protected(connection, directory)

    ensure_migration_table(connection)
    already_applied = applied_versions(connection)
    newly_applied: list[int] = []

    for path in discover_migrations(directory):
        version = migration_version(path)

        if version in already_applied:
            continue

        sql = path.read_text(encoding="utf-8-sig")
        statements = iter_sql_statements(sql)

        try:
            # Each migration file is applied as a single transaction:
            # either every statement in it succeeds and the version is
            # recorded, or none of it takes effect. BEGIN IMMEDIATE
            # (rather than a bare BEGIN) acquires the write lock
            # up front so a concurrent writer can't interleave with a
            # migration mid-way through.
            connection.execute("BEGIN IMMEDIATE")

            for statement in statements:
                connection.execute(statement)

            # Record this version as applied inside the same
            # transaction as the schema change itself, so a crash
            # between the two is impossible to observe.
            connection.execute(
                """
                INSERT INTO schema_migrations (version, name)
                VALUES (?, ?)
                """,
                (version, path.name),
            )

            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

        newly_applied.append(version)
        already_applied.add(version)

    return newly_applied
