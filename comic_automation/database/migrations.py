from __future__ import annotations

import sqlite3
from pathlib import Path


def ensure_migration_table(connection: sqlite3.Connection) -> None:
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
            connection.execute("BEGIN IMMEDIATE")

            for statement in statements:
                connection.execute(statement)

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
