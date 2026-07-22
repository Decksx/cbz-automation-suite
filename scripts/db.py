"""Migration-based SQLite layer for CBZ Automation Suite."""

from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

DEFAULT_DATABASE_PATH = Path(r"C:\git\ComicAutomation\data\comics.db")
DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path


@dataclass(frozen=True)
class DatabaseStatus:
    database_path: Path
    schema_version: int
    available_migrations: int
    pending_migrations: int
    journal_mode: str
    foreign_keys_enabled: bool


def connect(
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    *,
    read_only: bool = False,
    timeout: float = 30.0,
) -> sqlite3.Connection:
    path = Path(database_path)
    if read_only:
        conn = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro",
            uri=True,
            timeout=timeout,
        )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=timeout)

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if not read_only:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def discover_migrations(
    migrations_dir: str | Path = DEFAULT_MIGRATIONS_DIR,
) -> list[Migration]:
    directory = Path(migrations_dir)
    migrations: list[Migration] = []
    if not directory.exists():
        return migrations

    for path in directory.glob("*.sql"):
        prefix, separator, remainder = path.stem.partition("_")
        if separator and prefix.isdigit():
            migrations.append(
                Migration(
                    version=int(prefix),
                    name=remainder.replace("_", " "),
                    path=path,
                )
            )

    versions = [item.version for item in migrations]
    if len(versions) != len(set(versions)):
        raise RuntimeError("Duplicate migration version detected.")
    return sorted(migrations, key=lambda item: item.version)


def ensure_migration_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    ensure_migration_table(conn)
    return {
        int(row["version"])
        for row in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )
    }


def apply_migrations(
    conn: sqlite3.Connection,
    migrations_dir: str | Path = DEFAULT_MIGRATIONS_DIR,
) -> list[Migration]:
    applied = applied_versions(conn)
    completed: list[Migration] = []

    for migration in discover_migrations(migrations_dir):
        if migration.version in applied:
            continue
        sql = migration.path.read_text(encoding="utf-8")
        try:
            with conn:
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                    (migration.version, migration.name),
                )
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(
                f"Migration {migration.version:03d} "
                f"({migration.name}) failed: {exc}"
            ) from exc
        completed.append(migration)
        applied.add(migration.version)

    return completed


def initialize_database(
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    migrations_dir: str | Path = DEFAULT_MIGRATIONS_DIR,
) -> list[Migration]:
    with connect(database_path) as conn:
        return apply_migrations(conn, migrations_dir)


def current_schema_version(conn: sqlite3.Connection) -> int:
    ensure_migration_table(conn)
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
    ).fetchone()
    return int(row["version"])


def get_status(
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    migrations_dir: str | Path = DEFAULT_MIGRATIONS_DIR,
) -> DatabaseStatus:
    migrations = discover_migrations(migrations_dir)
    with connect(database_path) as conn:
        version = current_schema_version(conn)
        applied = applied_versions(conn)
        journal = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
        foreign_keys = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])

    return DatabaseStatus(
        database_path=Path(database_path),
        schema_version=version,
        available_migrations=len(migrations),
        pending_migrations=sum(m.version not in applied for m in migrations),
        journal_mode=journal,
        foreign_keys_enabled=foreign_keys,
    )


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        conn.execute("BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
