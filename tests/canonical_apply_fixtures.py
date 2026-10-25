"""Shared tmp-only fixtures for the canonical-naming apply tests (step 7).

Every library is a ``tmp_path`` directory and every database a migrated tmp
SQLite file with the step-7 marker table installed. Nothing here can name a
live path.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import pytest

from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.library.canonical_apply import executor
from comic_automation.library.canonical_apply import journal as jn
from comic_automation.library.canonical_apply.markers import (
    PairingColumn,
    PairingRef,
    install_schema,
)
from comic_automation.library.canonical_apply.plan import (
    DependantSpec,
    FamilySpec,
    MemberSpec,
    RenameSpec,
    build_plan,
    load_plan,
)

MIGRATIONS = (
    Path(__file__).resolve().parents[1] / "comic_automation" / "database" / "migrations"
)

P1, P2, P3, P4, P5 = (f"page-{n}".encode() for n in range(1, 6))
DATE = "2026-10-24"


class SimulatedCrash(BaseException):
    """The process 'dies' here. BaseException so no ``except Exception``
    in the code under test can swallow it and keep going."""


def crash_at(
    point: str, *, action_id: str | None = None, step: int | None = None, occurrence: int = 1
) -> executor.Fault:
    seen = {"n": 0}

    def fault(name: str, context: Mapping[str, Any]) -> None:
        if name != point:
            return
        if action_id is not None and context.get("action_id") != action_id:
            return
        if step is not None and context.get("step") != step:
            return
        seen["n"] += 1
        if seen["n"] == occurrence:
            raise SimulatedCrash(f"{name} {dict(context)}")

    return fault


def quiet(root: Path) -> None:
    return None


def book(folder: Path, name: str, pages: Sequence[bytes], extra: Mapping[str, bytes] | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for index, data in enumerate(pages, start=1):
            archive.writestr(f"{index:03d}.png", data)
        for member, data in (extra or {}).items():
            archive.writestr(member, data)
    return path


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@dataclass
class Env:
    root: Path
    series: Path
    database: Path
    tmp: Path
    plans: int = 0

    @property
    def quarantine_root(self) -> str:
        return str(self.root / ".cbz-canonical-quarantine" / DATE)

    def register(self, path: Path) -> tuple[int, int]:
        """An archive_files row and a current file_locations row for ``path``."""
        with database_connection(self.database) as connection:
            archive_id = int(connection.execute(
                "INSERT INTO archive_files (file_size) VALUES (?)", (path.stat().st_size,)
            ).lastrowid)
            location_id = int(connection.execute(
                "INSERT INTO file_locations (archive_id, path, is_current) VALUES (?, ?, 1)",
                (archive_id, str(path)),
            ).lastrowid)
        return archive_id, location_id

    def execute(self, sql: str, params: tuple = ()) -> None:
        with database_connection(self.database) as connection:
            connection.execute(sql, params)

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        with database_connection(self.database) as connection:
            return [tuple(row) for row in connection.execute(sql, params)]

    def row_path(self, location_id: int) -> str:
        return self.query("SELECT path FROM file_locations WHERE id = ?", (location_id,))[0][0]

    def markers(self, attempt_id: str | None = None) -> list[tuple]:
        sql = (
            "SELECT attempt_id, action_id, step, kind, preimage_path, postimage_path "
            "FROM rename_attempt_steps"
        )
        if attempt_id is None:
            return self.query(sql + " ORDER BY rowid")
        return self.query(sql + " WHERE attempt_id = ? ORDER BY rowid", (attempt_id,))

    def member(self, path: Path, pairings: tuple[PairingRef, ...] = ()) -> MemberSpec:
        archive_id, location_id = self.register(path)
        return MemberSpec(archive_id, location_id, str(path), sha(path), pairings)

    def rename_spec(self, source: Path, destination: Path, action_id: str, pairings=()) -> RenameSpec:
        member = self.member(source)
        return RenameSpec(
            action_id, member.archive_id, member.location_id, str(source), str(destination),
            member.sha256, {"series": "fixture"}, tuple(pairings),
        )

    def family_spec(
        self,
        family_id: str,
        keeper: Path,
        dependants: Sequence[tuple[Path, int]],
        *,
        rename_to: Path | None = None,
    ) -> FamilySpec:
        """``dependants`` are (path, page count): each a strict prefix of the keeper."""
        deps = tuple(
            DependantSpec(
                member=self.member(path),
                action_id=f"{family_id}:quarantine:{index}",
                quarantine_to=str(Path(self.quarantine_root) / family_id / path.name),
                index_map=tuple(range(count)),
            )
            for index, (path, count) in enumerate(dependants, start=1)
        )
        return FamilySpec(
            family_id=family_id,
            keeper=self.member(keeper),
            dependants=deps,
            keeper_rename_to=None if rename_to is None else str(rename_to),
            keeper_action_id=None if rename_to is None else f"{family_id}:keeper",
        )

    def plan(self, renames=(), families=(), review=(), database=None) -> dict[str, Any]:
        return build_plan(
            library_root=str(self.root),
            quarantine_root=self.quarantine_root,
            database=self.database if database is None else database,
            renames=renames,
            families=families,
            review=review,
        )

    def write_plan(self, document: Mapping[str, Any]) -> Path:
        self.plans += 1
        path = self.tmp / f"plan-{self.plans}.json"
        path.write_text(json.dumps(document, indent=1), encoding="utf-8")
        return path

    def load(self, document: Mapping[str, Any]):
        return load_plan(
            self.write_plan(document),
            library_root=self.root,
            expect_digest=document["plan_digest"],
            expect_renames=document["counts"]["renames"],
            expect_families=document["counts"]["families"],
        )

    def apply(self, document, **kwargs) -> executor.RunReport:
        kwargs.setdefault("quiescence", quiet)
        return executor.apply(
            self.load(document), library_root=self.root, database=self.database, **kwargs
        )

    def recover(self, document, attempt_id: str, **kwargs) -> executor.RunReport:
        kwargs.setdefault("quiescence", quiet)
        return executor.recover(
            self.load(document), library_root=self.root, database=self.database,
            attempt_id=attempt_id, **kwargs,
        )

    def undo(self, document, forward_attempt: str, **kwargs) -> executor.RunReport:
        kwargs.setdefault("quiescence", quiet)
        return executor.undo(
            self.load(document), library_root=self.root, database=self.database,
            forward_attempt=forward_attempt, **kwargs,
        )

    def attempts(self) -> tuple[str, ...]:
        return jn.list_attempts(self.root)

    def only_attempt(self) -> str:
        (attempt,) = self.attempts()
        return attempt

    def journal(self, attempt_id: str) -> list[tuple]:
        """(kind, action_id, step) for every record after the header."""
        return [
            (r.kind, r.action_id, r.step) if r.kind != jn.KIND_NOTE else (r.kind, r.data.get("note"), None)
            for r in jn.read_journal(self.root, attempt_id)[1:]
        ]

    def step_journal(self, attempt_id: str) -> list[tuple]:
        return [entry for entry in self.journal(attempt_id) if entry[0] != jn.KIND_NOTE]

    def tree(self) -> dict[str, str]:
        """Every file under the root except canonical-naming state, by sha."""
        return {
            str(p.relative_to(self.root)): sha(p)
            for p in sorted(self.root.rglob("*"))
            if p.is_file() and ".cbz-canonical-naming" not in p.parts
            and p.name != ".cbz-canonical-naming.lock"
            and p.name != ".cbz-canonical-naming.owner.json"
        }


def statuses(report: executor.RunReport) -> dict[str, str]:
    return {r["action_id"]: r["status"] for r in report.resolutions}


def unit_status(report: executor.RunReport, unit_id: str) -> str:
    return report.unit(unit_id)["status"]


def exact_names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


@pytest.fixture()
def env(tmp_path: Path) -> Env:
    root = tmp_path / "library"
    series = root / "Series"
    series.mkdir(parents=True)
    database = tmp_path / "db" / "test.db"
    database.parent.mkdir()
    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATIONS)
    install_schema(database)
    work = tmp_path / "work"
    work.mkdir()
    return Env(root, series, database, work)


PAIRS = PairingColumn(table="decensor_pairs", path_column="decensored_path")


def create_pairing_table(env: Env) -> None:
    env.execute(
        "CREATE TABLE decensor_pairs (id INTEGER PRIMARY KEY, decensored_path TEXT NOT NULL)"
    )


Fault = Callable[[str, Mapping[str, Any]], None]
