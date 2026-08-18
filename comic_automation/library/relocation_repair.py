"""Guarded repair of archive locations, keyed on content rather than path.

The problem this solves
-----------------------

Archive identity in this codebase is established by path:
``LibraryRepository.record_archive`` looks a file up by its path, and a file
seen at a path it has never occupied is treated as a brand-new archive. That
is correct for discovery and wrong for repair. When a file moves, a rescan
mints a *second* archive identity for the same bytes and leaves the original
pointing at a path that no longer exists, so the original's evidence -- page
inventory, SHA-256, perceptual hashes -- is stranded rather than carried over.

Measured on 2026-08-17: 60 archives had been reorganised from ``X:\\Manga``
into ``X:\\Graphic Novels`` and were invisible to every gate until a worker
tried to open them; 1,911 ``file_locations`` rows still pointed at
``X:\\Horrorsplat``, a root that no longer exists. The eligibility predicate
could not see any of it, because it compares recorded metadata to recorded
metadata and never stats the filesystem.

What repair means here
----------------------

Two states are repaired, and they are the same problem wearing different
clothes:

``moved``
    The recorded path holds no file, but a file elsewhere under the searched
    roots hashes to this archive's stored SHA-256.
``metadata_drift``
    A file is still at the recorded path and still hashes to the stored
    SHA-256, but its size or mtime no longer match what was recorded -- what a
    restore from backup produces, since a restored copy is a new file object.

Both are repaired only on proof: the candidate's SHA-256 must equal the digest
stored in ``archive_hashes``. Filename and size are used to *narrow* the
search cheaply; they never establish identity.

Why refreshing recorded metadata is safe here, and only here
------------------------------------------------------------

``docs`` and the batch preflight both warn against "fitting fresh metadata
around stale evidence" -- updating ``archive_content_signatures.source_*`` to
agree with a live file asserts a consistency nobody verified. That warning
applies when the file's content is *unknown*. It does not apply here: this
module refreshes recorded size and mtime only after proving, by full-file
SHA-256, that the bytes are the ones the signature was computed from. The
evidence is re-established, not assumed.

Guarding
--------

Report-first, in line with every other recovery path in this repository. The
default run is read-only and prints a plan. ``--confirm`` requires both
``--expected-count`` and ``--plan-digest`` from that plan, so an apply refuses
to run against a library or database that has moved since the plan was
reviewed. Each archive is re-verified immediately before its own write, so a
file that changes between plan and apply is skipped rather than trusted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from comic_automation.database.connection import database_connection

SHA256_ALGORITHM = "sha256"
SHA256_VERSION = "1"
ARCHIVE_SUFFIXES = {".cbz", ".cbr"}

# Read size for hashing. Large enough that network shares are not dominated by
# per-read overhead, small enough not to hold a whole archive in memory.
HASH_CHUNK_BYTES = 8 << 20


@dataclass(frozen=True)
class BrokenLocation:
    """An archive whose recorded current location does not describe reality."""

    archive_id: int
    location_id: int
    recorded_path: str
    recorded_size: int | None
    recorded_mtime_ns: int | None
    stored_sha256: str | None
    state: str  # "missing" | "metadata_drift"


@dataclass(frozen=True)
class Repair:
    """A verified, applicable repair for one archive."""

    archive_id: int
    location_id: int
    old_path: str
    new_path: str
    new_size: int
    new_mtime_ns: int
    sha256: str
    kind: str  # "moved" | "metadata_drift"


@dataclass
class RepairPlan:
    repairs: list[Repair] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)
    ambiguous: list[dict] = field(default_factory=list)

    def digest(self) -> str:
        """Stable digest of the plan's decisions.

        Covers archive id, source and destination path, and verified digest for
        every repair, in archive-id order. Encoding mirrors the batch selection
        digest used elsewhere in this project: UTF-8, LF after every record
        including the last, lowercase hex SHA-256. An apply quoting a different
        digest is describing a different plan and is refused.
        """
        payload = "".join(
            f"{r.archive_id}\t{r.old_path}\t{r.new_path}\t{r.sha256}\n"
            for r in sorted(self.repairs, key=lambda r: r.archive_id)
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sha256_file(path: Path, chunk: int = HASH_CHUNK_BYTES) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def find_broken_locations(connection: sqlite3.Connection) -> list[BrokenLocation]:
    """Return archives whose recorded current location disagrees with the disk.

    Stats every current location, which is precisely what the eligibility
    predicate does not do and why this class of breakage stays invisible.
    """
    rows = connection.execute(
        """
        SELECT
            fl.archive_id       AS archive_id,
            fl.id               AS location_id,
            fl.path             AS path,
            fl.file_size        AS recorded_size,
            fl.modified_time_ns AS recorded_mtime_ns,
            ah.digest           AS stored_sha256
        FROM file_locations AS fl
        LEFT JOIN archive_hashes AS ah
          ON ah.archive_id = fl.archive_id
         AND ah.algorithm = ?
         AND ah.algorithm_version = ?
        WHERE fl.is_current = 1
        ORDER BY fl.archive_id
        """,
        (SHA256_ALGORITHM, SHA256_VERSION),
    ).fetchall()

    broken: list[BrokenLocation] = []
    for row in rows:
        path = str(row["path"])
        try:
            stat = os.stat(path)
        except OSError:
            broken.append(
                BrokenLocation(
                    archive_id=int(row["archive_id"]),
                    location_id=int(row["location_id"]),
                    recorded_path=path,
                    recorded_size=row["recorded_size"],
                    recorded_mtime_ns=row["recorded_mtime_ns"],
                    stored_sha256=row["stored_sha256"],
                    state="missing",
                )
            )
            continue
        if (
            stat.st_size != row["recorded_size"]
            or stat.st_mtime_ns != row["recorded_mtime_ns"]
        ):
            broken.append(
                BrokenLocation(
                    archive_id=int(row["archive_id"]),
                    location_id=int(row["location_id"]),
                    recorded_path=path,
                    recorded_size=row["recorded_size"],
                    recorded_mtime_ns=row["recorded_mtime_ns"],
                    stored_sha256=row["stored_sha256"],
                    state="metadata_drift",
                )
            )
    return broken


def index_roots(roots: Iterable[Path]) -> dict[int, list[Path]]:
    """Index archive files under *roots* by size.

    Size is a cheap, highly selective narrowing filter that costs one stat per
    file. It is never treated as identity -- every candidate it proposes is
    still hashed before use.
    """
    by_size: dict[int, list[Path]] = {}
    for root in roots:
        stack = [str(root)]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(entry.path)
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue
                            if Path(entry.name).suffix.lower() not in ARCHIVE_SUFFIXES:
                                continue
                            by_size.setdefault(entry.stat().st_size, []).append(
                                Path(entry.path)
                            )
                        except OSError:
                            continue
            except OSError:
                continue
    return by_size


def plan_repairs(
    broken: Sequence[BrokenLocation],
    by_size: dict[int, list[Path]],
    *,
    known_paths: set[str] | None = None,
) -> RepairPlan:
    """Verify each broken location against candidate files and build a plan.

    ``known_paths`` are paths already claimed by some current location; a
    candidate among them is not offered as a relocation target, so repair can
    never point two archives at one file.
    """
    known = {p.lower() for p in (known_paths or set())}
    plan = RepairPlan()

    for item in broken:
        if not item.stored_sha256:
            plan.unresolved.append(
                {
                    "archive_id": item.archive_id,
                    "path": item.recorded_path,
                    "state": item.state,
                    "reason": "no stored sha256 to verify against",
                }
            )
            continue

        if item.state == "metadata_drift":
            # The file is still where it should be; only its recorded metadata
            # is stale. Verify content directly rather than searching.
            live = Path(item.recorded_path)
            try:
                actual = sha256_file(live)
                stat = live.stat()
            except OSError as error:
                plan.unresolved.append(
                    {
                        "archive_id": item.archive_id,
                        "path": item.recorded_path,
                        "state": item.state,
                        "reason": f"unreadable: {error}",
                    }
                )
                continue
            if actual != item.stored_sha256:
                plan.unresolved.append(
                    {
                        "archive_id": item.archive_id,
                        "path": item.recorded_path,
                        "state": item.state,
                        "reason": "content changed: sha256 differs from stored",
                    }
                )
                continue
            plan.repairs.append(
                Repair(
                    archive_id=item.archive_id,
                    location_id=item.location_id,
                    old_path=item.recorded_path,
                    new_path=item.recorded_path,
                    new_size=stat.st_size,
                    new_mtime_ns=stat.st_mtime_ns,
                    sha256=actual,
                    kind="metadata_drift",
                )
            )
            continue

        # state == "missing": search for the content elsewhere.
        candidates = [
            candidate
            for candidate in by_size.get(item.recorded_size or -1, [])
            if str(candidate).lower() not in known
        ]
        verified = []
        for candidate in candidates:
            try:
                if sha256_file(candidate) == item.stored_sha256:
                    verified.append(candidate)
            except OSError:
                continue

        if len(verified) == 1:
            target = verified[0]
            stat = target.stat()
            plan.repairs.append(
                Repair(
                    archive_id=item.archive_id,
                    location_id=item.location_id,
                    old_path=item.recorded_path,
                    new_path=str(target),
                    new_size=stat.st_size,
                    new_mtime_ns=stat.st_mtime_ns,
                    sha256=item.stored_sha256,
                    kind="moved",
                )
            )
        elif len(verified) > 1:
            # Identical content at several paths is a duplicate question, not a
            # relocation question, and picking one would silently choose which
            # copy the library considers canonical.
            plan.ambiguous.append(
                {
                    "archive_id": item.archive_id,
                    "path": item.recorded_path,
                    "candidates": [str(p) for p in verified],
                }
            )
        else:
            plan.unresolved.append(
                {
                    "archive_id": item.archive_id,
                    "path": item.recorded_path,
                    "state": item.state,
                    "reason": "no file under the searched roots matches the stored sha256",
                }
            )
    return plan


def apply_repairs(
    connection: sqlite3.Connection,
    repairs: Sequence[Repair],
) -> dict:
    """Apply verified repairs in one transaction.

    Each repair is re-verified against the live file immediately before its own
    write, so a file that moved or changed between plan and apply is skipped
    rather than trusted. A location is retired and replaced rather than
    mutated, matching how quarantine treats a departing location and preserving
    the history that makes a later move traceable.
    """
    applied: list[dict] = []
    skipped: list[dict] = []

    for repair in repairs:
        target = Path(repair.new_path)
        try:
            stat = target.stat()
            actual = sha256_file(target)
        except OSError as error:
            skipped.append(
                {"archive_id": repair.archive_id, "reason": f"unreadable: {error}"}
            )
            continue
        if actual != repair.sha256:
            skipped.append(
                {
                    "archive_id": repair.archive_id,
                    "reason": "content changed between plan and apply",
                }
            )
            continue

        if repair.new_path == repair.old_path:
            # The file never moved; only its recorded metadata was stale. There
            # is no location history to preserve, and file_locations.path is
            # UNIQUE, so retiring and re-inserting the same path would both
            # fabricate a move that did not happen and violate that constraint.
            connection.execute(
                """
                UPDATE file_locations
                SET file_size = ?, modified_time_ns = ?,
                    last_seen_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (stat.st_size, stat.st_mtime_ns, repair.location_id),
            )
        else:
            # A genuine move: retire the stale location rather than editing it,
            # so the path history stays traceable.
            connection.execute(
                """
                UPDATE file_locations
                SET is_current = 0, last_seen_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (repair.location_id,),
            )
            connection.execute(
                """
                INSERT INTO file_locations (
                    archive_id, path, is_current, file_size, modified_time_ns,
                    first_seen_at, last_seen_at
                )
                VALUES (?, ?, 1, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(path) DO UPDATE SET
                    archive_id = excluded.archive_id,
                    is_current = 1,
                    file_size = excluded.file_size,
                    modified_time_ns = excluded.modified_time_ns,
                    last_seen_at = CURRENT_TIMESTAMP
                """,
                (repair.archive_id, repair.new_path, stat.st_size, stat.st_mtime_ns),
            )
        # Re-establish the signature's source metadata. Safe only because the
        # content was just proven identical by full-file SHA-256; without that
        # proof this would assert a consistency nobody verified.
        connection.execute(
            """
            UPDATE archive_content_signatures
            SET source_file_size = ?, source_modified_time_ns = ?
            WHERE archive_id = ?
            """,
            (stat.st_size, stat.st_mtime_ns, repair.archive_id),
        )
        applied.append(
            {
                "archive_id": repair.archive_id,
                "kind": repair.kind,
                "old_path": repair.old_path,
                "new_path": repair.new_path,
            }
        )

    return {"applied": applied, "skipped": skipped}


def current_paths(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT path FROM file_locations WHERE is_current = 1"
        )
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Repair archive locations by content. Finds current locations that "
            "no longer describe the disk and re-points them at the file whose "
            "SHA-256 matches, preserving archive identity and its accumulated "
            "evidence. Read-only without --confirm."
        )
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument(
        "--root",
        type=Path,
        action="append",
        default=[],
        help="Library root to search for moved files. Repeatable.",
    )
    parser.add_argument("--limit", type=int, help="Cap repairs planned.")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Apply the plan. Requires --expected-count and --plan-digest.",
    )
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--plan-digest")
    parser.add_argument("--json-output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    with database_connection(args.database) as connection:
        connection.row_factory = sqlite3.Row
        broken = find_broken_locations(connection)
        known = current_paths(connection)

        missing = [b for b in broken if b.state == "missing"]
        by_size = index_roots(args.root) if (missing and args.root) else {}
        plan = plan_repairs(broken, by_size, known_paths=known)

        if args.limit is not None:
            plan.repairs = plan.repairs[: args.limit]

        digest = plan.digest()
        print("Location repair plan (content-verified).")
        print(f"  broken current locations : {len(broken):,}")
        print(f"    missing from disk      : {len(missing):,}")
        print(f"    metadata drift         : {len(broken) - len(missing):,}")
        print(f"  repairs planned          : {len(plan.repairs):,}")
        print(f"    moved                  : "
              f"{sum(1 for r in plan.repairs if r.kind == 'moved'):,}")
        print(f"    metadata_drift         : "
              f"{sum(1 for r in plan.repairs if r.kind == 'metadata_drift'):,}")
        print(f"  ambiguous (not repaired) : {len(plan.ambiguous):,}")
        print(f"  unresolved               : {len(plan.unresolved):,}")
        print(f"  plan digest              : {digest}")

        result = {
            "broken": len(broken),
            "repairs": [r.__dict__ for r in plan.repairs],
            "ambiguous": plan.ambiguous,
            "unresolved": plan.unresolved,
            "plan_digest": digest,
            "applied": None,
        }

        if args.confirm:
            if args.expected_count != len(plan.repairs):
                print(
                    f"\nREFUSING: --expected-count {args.expected_count} does not "
                    f"match the {len(plan.repairs)} repairs planned now."
                )
                return 1
            if args.plan_digest != digest:
                print(
                    "\nREFUSING: --plan-digest describes a different plan. "
                    "Re-run the read-only plan and review it again."
                )
                return 1
            outcome = apply_repairs(connection, plan.repairs)
            connection.commit()
            result["applied"] = outcome
            print(f"\n  applied  : {len(outcome['applied']):,}")
            print(f"  skipped  : {len(outcome['skipped']):,}")
        else:
            print("\n  Read-only plan. Re-run with --confirm --expected-count "
                  f"{len(plan.repairs)} --plan-digest {digest}")

    if args.json_output:
        args.json_output.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"  json: {args.json_output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
