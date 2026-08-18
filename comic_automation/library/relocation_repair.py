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

Why refreshing recorded metadata needs two proofs, not one
----------------------------------------------------------

``docs`` and the batch preflight both warn against "fitting fresh metadata
around stale evidence" -- updating ``archive_content_signatures.source_*`` to
agree with a live file asserts a consistency nobody verified.

An earlier revision of this module answered that warning with a single check:
the live file's SHA-256 equals ``archive_hashes.digest``. Review rejected that
reasoning and was right to. That check proves which bytes the *archive hash
row* describes. It proves nothing about ``archive_content_signatures``, which
is written by a different stage: if a file changed and only the archive hash
was recomputed, the hash describes the new bytes while the page signature still
describes the old ones, and refreshing ``source_*`` would launder that stale
page evidence into looking current.

Two conditions are therefore required before any refresh:

1. the live file's SHA-256 equals the stored archive digest, and
2. ``archive_hashes`` and ``archive_content_signatures`` record the *same*
   observed file size and mtime -- see ``BrokenLocation.evidence_is_coherent``.

The second is what makes the two rows descriptions of one file state rather
than two unrelated observations. An archive failing it is reported as needing
reinspection and is never repaired: recomputing page evidence is a different
operation with a different cost, and it is not this module's job to fake it.

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
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from comic_automation.database.connection import database_connection

log = logging.getLogger(__name__)

SHA256_ALGORITHM = "sha256"
SHA256_VERSION = "1"
ARCHIVE_SUFFIXES = {".cbz", ".cbr"}

# Read size for hashing. Large enough that network shares are not dominated by
# per-read overhead, small enough not to hold a whole archive in memory.
HASH_CHUNK_BYTES = 8 << 20


class LocationRepairError(RuntimeError):
    """A repair could not be completed without corrupting location state.

    Raised rather than collected into `skipped`, because the conditions that
    produce it mean an invariant this module relies on is already broken. The
    caller's transaction must be rolled back, not partially committed.
    """


@dataclass(frozen=True)
class PathClaim:
    """One `file_locations` row's claim on a canonical path.

    Carries `location_id` because apply must be able to revive *this* row
    rather than depend on SQLite matching a path string: the recorded spelling
    and the repair's target spelling can differ while naming one file.
    """

    location_id: int
    archive_id: int
    is_current: bool
    recorded_path: str


@dataclass(frozen=True)
class BrokenLocation:
    """An archive whose recorded current location does not describe reality."""

    archive_id: int
    location_id: int
    recorded_path: str
    recorded_size: int | None
    recorded_mtime_ns: int | None
    stored_sha256: str | None
    state: str  # "missing" | "metadata_drift" | "unreadable"
    # Provenance: the file state each evidence row was computed from. Repair
    # may only refresh the page signature when these agree, because agreeing
    # is what makes them descriptions of the same bytes rather than two
    # unrelated observations.
    hash_file_size: int | None = None
    hash_mtime_ns: int | None = None
    signature_source_size: int | None = None
    signature_source_mtime_ns: int | None = None

    @property
    def evidence_is_coherent(self) -> bool:
        """True when the archive hash and the page signature describe one state.

        `archive_hashes` and `archive_content_signatures` are written by
        separate stages and can drift apart: if a file changed and only the
        archive hash was recomputed, the hash describes the new bytes while the
        page signature still describes the old ones. Verifying a live file
        against the hash digest then proves nothing about the signature.

        Requiring both rows to record the same observed size and mtime is what
        turns "these bytes match the hash row" into "these bytes are what the
        page evidence was computed from".
        """
        return (
            self.hash_file_size is not None
            and self.signature_source_size is not None
            and self.hash_file_size == self.signature_source_size
            and self.hash_mtime_ns == self.signature_source_mtime_ns
        )


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


def canonical_path(path: str | Path) -> str:
    """The comparison form for a filesystem path.

    ``os.path.normcase`` folds case and separator style on Windows and is a
    no-op on POSIX, so this is the one place that knows how paths compare on
    the host. Planning, same-run reservation and apply-time ownership all use
    it, because a check that compares paths differently from the check beside
    it is not a check.

    The library lives on a case-insensitive volume, where ``X:\\A.cbz`` and
    ``x:\\a.cbz`` are one file. SQLite's UNIQUE index is case-sensitive and
    would happily hold both spellings as separate rows, so an ownership query
    using exact equality could miss a row that already claims the same file.
    """
    return os.path.normcase(os.path.normpath(str(path)))


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
            ah.digest           AS stored_sha256,
            ah.file_size        AS hash_file_size,
            ah.modified_time_ns AS hash_mtime_ns,
            acs.source_file_size        AS signature_source_size,
            acs.source_modified_time_ns AS signature_source_mtime_ns
        FROM file_locations AS fl
        LEFT JOIN archive_hashes AS ah
          ON ah.archive_id = fl.archive_id
         AND ah.algorithm = ?
         AND ah.algorithm_version = ?
        LEFT JOIN archive_content_signatures AS acs
          ON acs.archive_id = fl.archive_id
        WHERE fl.is_current = 1
        ORDER BY fl.archive_id
        """,
        (SHA256_ALGORITHM, SHA256_VERSION),
    ).fetchall()

    def _broken(row, state: str) -> BrokenLocation:
        return BrokenLocation(
            archive_id=int(row["archive_id"]),
            location_id=int(row["location_id"]),
            recorded_path=str(row["path"]),
            recorded_size=row["recorded_size"],
            recorded_mtime_ns=row["recorded_mtime_ns"],
            stored_sha256=row["stored_sha256"],
            state=state,
            hash_file_size=row["hash_file_size"],
            hash_mtime_ns=row["hash_mtime_ns"],
            signature_source_size=row["signature_source_size"],
            signature_source_mtime_ns=row["signature_source_mtime_ns"],
        )

    broken: list[BrokenLocation] = []
    for row in rows:
        path = str(row["path"])
        try:
            stat = os.stat(path)
        except FileNotFoundError:
            # Genuinely absent: the only state a relocation search should act
            # on.
            broken.append(_broken(row, "missing"))
            continue
        except OSError as error:
            # A permission failure or a transient I/O error says nothing about
            # whether the file is there. Treating it as "missing" would send
            # repair hunting for a replacement for a file that exists and is
            # merely unreadable right now, and could re-point the archive at a
            # different copy. Classified separately so it is reported and
            # skipped rather than acted on.
            broken.append(_broken(row, "unreadable"))
            log.debug("Cannot stat %s: %s", path, error)
            continue
        if (
            stat.st_size != row["recorded_size"]
            or stat.st_mtime_ns != row["recorded_mtime_ns"]
        ):
            broken.append(_broken(row, "metadata_drift"))
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
    owners: dict[str, list[PathClaim]] | None = None,
) -> RepairPlan:
    """Verify each broken location against candidate files and build a plan.

    ``owners`` maps canonical paths to the archive holding them, from
    `path_owners`. A candidate held by a *different* archive is never offered
    as a relocation target, whether that archive's row is current or retired,
    so repair can never point two archives at one file and never plans a
    target the apply stage would refuse.
    """
    owned = dict(owners or {})
    # Targets claimed by earlier repairs in this same plan. Without this, two
    # archives that share a stored digest -- which is exactly what a duplicate
    # pair is, and there are 886 such groups in production -- can both select
    # the same unclaimed file, and the apply would hand it to whichever ran
    # last.
    reserved: set[str] = set()
    plan = RepairPlan()

    for item in broken:
        if item.state == "unreadable":
            plan.unresolved.append(
                {
                    "archive_id": item.archive_id,
                    "path": item.recorded_path,
                    "state": item.state,
                    "reason": "path could not be read (permission or I/O), "
                    "which is not evidence of absence",
                }
            )
            continue

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

        if not item.evidence_is_coherent:
            # The archive hash and the page signature were computed from
            # different observed file states, so verifying against the hash
            # proves nothing about the page evidence. Refreshing source_* here
            # would launder stale page evidence into looking current.
            plan.unresolved.append(
                {
                    "archive_id": item.archive_id,
                    "path": item.recorded_path,
                    "state": item.state,
                    "reason": "archive hash and page signature describe "
                    "different file states; needs reinspection, not repair",
                    "hash_state": [item.hash_file_size, item.hash_mtime_ns],
                    "signature_state": [
                        item.signature_source_size,
                        item.signature_source_mtime_ns,
                    ],
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
        candidates = []
        blocked_by_owner: list[tuple[Path, str]] = []
        for candidate in by_size.get(item.recorded_size or -1, []):
            key = canonical_path(candidate)
            if key in reserved:
                continue
            refusal = ownership_refusal(owned.get(key, []), item.archive_id)
            if refusal is not None:
                # Held by another archive, or ambiguously held by several rows.
                # Recorded rather than silently dropped, so a candidate that
                # matches by content but cannot be claimed is explained instead
                # of reported as "no match".
                blocked_by_owner.append((candidate, refusal))
                continue
            candidates.append(candidate)

        verified = []
        for candidate in candidates:
            try:
                if sha256_file(candidate) == item.stored_sha256:
                    verified.append(candidate)
            except OSError:
                continue

        if not verified and blocked_by_owner:
            blocked_matches = []
            for candidate, refusal in blocked_by_owner:
                try:
                    if sha256_file(candidate) == item.stored_sha256:
                        blocked_matches.append((str(candidate), refusal))
                except OSError:
                    continue
            if blocked_matches:
                path_text, refusal = blocked_matches[0]
                plan.unresolved.append(
                    {
                        "archive_id": item.archive_id,
                        "path": item.recorded_path,
                        "state": item.state,
                        "reason": (
                            f"content found at an unclaimable path: {refusal}; "
                            "needs duplicate resolution, not relocation"
                        ),
                        "blocked_path": path_text,
                    }
                )
                continue

        if len(verified) == 1:
            target = verified[0]
            stat = target.stat()
            # Claim it, so no later archive in this plan can select it too.
            reserved.add(canonical_path(target))
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
    """Apply verified repairs atomically, or apply none of them.

    This function owns its transaction. `connect_database` opens connections
    with ``isolation_level=None``, so sqlite3 starts no implicit transaction
    and every statement would otherwise commit on execution. An earlier
    revision relied on a trailing ``connection.commit()`` and on raising
    `LocationRepairError` to undo a half-finished repair; neither did anything.
    A repair could retire an archive's old location, fail to claim the new one,
    raise -- and leave the archive with no current location committed to disk.
    The same gap meant a failure on repair 500 left repairs 1-499 committed
    while this docstring claimed one transaction.

    ``BEGIN IMMEDIATE`` rather than ``BEGIN``: it takes the write lock up
    front, so the ownership snapshot taken next cannot be invalidated by
    another writer claiming a canonical path midway through the batch. A
    deferred transaction would only acquire that lock at the first write, which
    is after `path_owners` has already been read.

    Each repair is additionally re-verified against the live file immediately
    before its own write, so a file that moved or changed between plan and
    apply is skipped rather than trusted. A genuine move retires the old
    location and revives or inserts the new one, matching how quarantine
    treats a departing location and preserving traceable history.

    Raises on any failure that must not be partially applied; the transaction
    is rolled back first, so the caller sees the database as it was.
    """
    applied: list[dict] = []
    skipped: list[dict] = []
    applied_paths: set[str] = set()

    # Write lock first, then the ownership snapshot, so no other writer can
    # claim a canonical path between reading ownership and acting on it.
    connection.execute("BEGIN IMMEDIATE")
    try:
        # Canonical ownership as it stands right now, kept current as repairs
        # land so a later repair in the same run sees an earlier one's claim.
        owners = path_owners(connection)
        _apply_within_transaction(
            connection, repairs, owners, applied, skipped, applied_paths
        )
    except BaseException:
        # Includes LocationRepairError, whose whole purpose is to abandon a
        # half-finished repair. Rolling back is what makes that purpose real.
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:  # pragma: no cover - rollback of a dead txn
            pass
        raise
    connection.execute("COMMIT")

    return {"applied": applied, "skipped": skipped}


def _apply_within_transaction(
    connection: sqlite3.Connection,
    repairs: Sequence[Repair],
    owners: dict[str, list[PathClaim]],
    applied: list[dict],
    skipped: list[dict],
    applied_paths: set[str],
) -> None:
    """The per-repair work, run inside the caller's open transaction.

    Split out so that `apply_repairs` reads as begin / do / commit-or-rollback
    and the transaction boundary cannot drift away from the work it protects.
    """
    for repair in repairs:
        target = Path(repair.new_path)

        # A path may be claimed by only one archive. Two archives that share a
        # stored digest can both have selected this file, and a path can also
        # be claimed by a scan that ran after the plan was made. Refuse rather
        # than transfer: silently re-pointing a live location at a different
        # archive merges two identities and strands one archive's evidence.
        if canonical_path(repair.new_path) in applied_paths:
            skipped.append(
                {
                    "archive_id": repair.archive_id,
                    "reason": "another repair in this run already claimed this path",
                }
            )
            continue
        # Ownership is checked across EVERY row for this path, not only current
        # ones. file_locations.path is UNIQUE, so a retired row belonging to
        # another archive still occupies it. An earlier revision filtered on
        # is_current = 1 and missed exactly that: the archive's old location was
        # retired, the conditional upsert then matched nothing because the
        # archive ids differed, and the archive was left with NO current
        # location at all -- while the signature was still refreshed and the
        # repair reported as applied.
        # Ownership is resolved against a canonical map of every row, never a
        # SQL prefilter. An earlier revision narrowed with
        # `WHERE path = ? COLLATE NOCASE` before applying canonical_path, which
        # folds case but NOT separators -- so a recorded "X:/Manga/A.cbz" never
        # reached the canonical comparison when the repair targeted
        # "X:\Manga\A.cbz", and SQLite would then hold both spellings for what
        # Windows resolves to one file.
        target_key = canonical_path(repair.new_path)
        refusal = ownership_refusal(owners.get(target_key, []), repair.archive_id)
        if refusal is not None:
            skipped.append({"archive_id": repair.archive_id, "reason": refusal})
            continue

        # The location row must still be the one that was planned against. If
        # it is no longer current, something else repaired, quarantined or
        # retired this archive in the meantime and this plan is describing a
        # state that has moved.
        location = connection.execute(
            "SELECT path, is_current FROM file_locations WHERE id = ?",
            (repair.location_id,),
        ).fetchone()
        if location is None or not int(location[1]) or str(location[0]) != repair.old_path:
            skipped.append(
                {
                    "archive_id": repair.archive_id,
                    "reason": "original location changed since planning",
                }
            )
            continue

        try:
            stat_before = target.stat()
            actual = sha256_file(target)
            stat_after = target.stat()
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
        if (
            stat_before.st_size,
            stat_before.st_mtime_ns,
        ) != (stat_after.st_size, stat_after.st_mtime_ns):
            # The file changed while it was being read, so the digest just
            # computed describes neither the before nor the after state.
            skipped.append(
                {
                    "archive_id": repair.archive_id,
                    "reason": "file changed while it was being hashed",
                }
            )
            continue
        stat = stat_after

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
            # ownership_refusal above proved this canonical path is either
            # unclaimed or held by exactly one row belonging to this archive.
            existing = (owners.get(target_key) or [None])[0]
            if existing is not None:
                # Revive THAT row by id, and normalise its recorded spelling to
                # the path actually verified on disk. An earlier revision used
                # `ON CONFLICT(path) DO UPDATE`, which matches on the exact
                # string: a retired row recorded as "X:/Manga/A.cbz" did not
                # conflict with a target of "X:\Manga\A.cbz", so the insert
                # ADDED a second row and left two rows resolving to one file --
                # manufacturing the collision the next run would refuse, and
                # producing two current rows if the existing one was current.
                # UNIQUE(path) cannot be violated here because a second row for
                # this canonical path would have been a collision refusal.
                cursor = connection.execute(
                    """
                    UPDATE file_locations
                    SET path = ?, is_current = 1, file_size = ?,
                        modified_time_ns = ?, last_seen_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        repair.new_path,
                        stat.st_size,
                        stat.st_mtime_ns,
                        existing.location_id,
                    ),
                )
            else:
                try:
                    cursor = connection.execute(
                        """
                        INSERT INTO file_locations (
                            archive_id, path, is_current, file_size,
                            modified_time_ns, first_seen_at, last_seen_at
                        )
                        VALUES (?, ?, 1, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        """,
                        (
                            repair.archive_id,
                            repair.new_path,
                            stat.st_size,
                            stat.st_mtime_ns,
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    # A row appeared for this path that the ownership map did
                    # not have. The map is built inside this transaction, so
                    # this should be unreachable; reaching it means the map is
                    # wrong and nothing in this run may be trusted.
                    raise LocationRepairError(
                        f"Claiming {repair.new_path!r} for archive "
                        f"{repair.archive_id} violated UNIQUE(path): {error}"
                    ) from error
            # A silent no-op here is the failure mode that leaves an archive
            # with its old location retired and no new one, so it is fatal
            # rather than collected.
            if cursor.rowcount != 1:
                raise LocationRepairError(
                    "Refusing to leave archive "
                    f"{repair.archive_id} without a current location: claiming "
                    f"{repair.new_path!r} affected {cursor.rowcount} rows."
                )
        # Re-establish the signature's source metadata. Two things had to be
        # true before reaching here, and neither alone is sufficient: the live
        # bytes match the stored archive digest, AND the archive hash and the
        # page signature were computed from the same observed file state
        # (BrokenLocation.evidence_is_coherent). Without the second, matching
        # the hash would prove only which bytes the hash row describes, and
        # this UPDATE would launder a stale page signature into looking
        # current.
        connection.execute(
            """
            UPDATE archive_content_signatures
            SET source_file_size = ?, source_modified_time_ns = ?
            WHERE archive_id = ?
            """,
            (stat.st_size, stat.st_mtime_ns, repair.archive_id),
        )
        applied_paths.add(target_key)
        # Replace rather than append: the write either revived the single
        # existing row or inserted the only one, so exactly one claim now holds
        # this canonical path. Appending would fabricate a collision that the
        # next repair in this run would refuse.
        claimed = owners.get(target_key) or []
        owners[target_key] = [
            PathClaim(
                location_id=(
                    claimed[0].location_id if claimed else int(cursor.lastrowid or 0)
                ),
                archive_id=repair.archive_id,
                is_current=True,
                recorded_path=repair.new_path,
            )
        ]
        applied.append(
            {
                "archive_id": repair.archive_id,
                "kind": repair.kind,
                "old_path": repair.old_path,
                "new_path": repair.new_path,
            }
        )


def path_owners(connection: sqlite3.Connection) -> dict[str, list[PathClaim]]:
    """Map every canonical path to *all* recorded rows that resolve to it.

    Deliberately includes retired rows. ``file_locations.path`` is UNIQUE, so a
    retired row still occupies its path and a repair cannot claim it. Planning
    that only knew about *current* paths would propose a repair the apply stage
    is already guaranteed to refuse, and that proposal would be counted in the
    plan's expected count and digest -- an operator would review, approve and
    apply a plan containing targets known in advance to be unclaimable.

    A *list* rather than one owner per path, because SQLite's uniqueness is
    case- and separator-sensitive while the filesystem's is not: ``X:/Manga/A.cbz``
    and ``X:\\Manga\\A.cbz`` are two rows and one file. An earlier revision
    stored a single tuple per canonical path and silently kept whichever row the
    query happened to yield last. If the survivor belonged to the archive being
    repaired, another archive's claim on the same file simply disappeared from
    view. Collisions are now represented and refused rather than resolved by
    iteration order.
    """
    owners: dict[str, list[PathClaim]] = {}
    for location_id, path, archive_id, is_current in connection.execute(
        "SELECT id, path, archive_id, is_current FROM file_locations"
    ):
        owners.setdefault(canonical_path(path), []).append(
            PathClaim(
                location_id=int(location_id),
                archive_id=int(archive_id),
                is_current=bool(is_current),
                recorded_path=str(path),
            )
        )
    return owners


def ownership_refusal(claims: Sequence[PathClaim], archive_id: int) -> str | None:
    """Why *archive_id* may not claim a path, or None if it may.

    Used by planning and by apply, so the two cannot disagree about what
    ownership means.
    """
    if not claims:
        return None
    if len(claims) > 1:
        spellings = ", ".join(sorted(repr(c.recorded_path) for c in claims))
        holders = sorted({c.archive_id for c in claims})
        # Two rows resolving to one file is already an inconsistent database
        # state. Which row to revive is not this module's guess to make, even
        # when every row belongs to the archive being repaired.
        return (
            f"{len(claims)} recorded rows resolve to this path "
            f"(archives {holders}): {spellings}"
        )
    claim = claims[0]
    if claim.archive_id != archive_id:
        state = "current" if claim.is_current else "retired"
        return f"path is the {state} location of archive {claim.archive_id}"
    return None


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
        owners = path_owners(connection)

        missing = [b for b in broken if b.state == "missing"]
        drifted = [b for b in broken if b.state == "metadata_drift"]
        unreadable = [b for b in broken if b.state == "unreadable"]
        by_size = index_roots(args.root) if (missing and args.root) else {}
        plan = plan_repairs(broken, by_size, owners=owners)

        if args.limit is not None:
            plan.repairs = plan.repairs[: args.limit]

        digest = plan.digest()
        print("Location repair plan (content-verified).")
        print(f"  broken current locations : {len(broken):,}")
        print(f"    missing from disk      : {len(missing):,}")
        print(f"    metadata drift         : {len(drifted):,}")
        print(f"    unreadable             : {len(unreadable):,}")
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
            # apply_repairs owns its transaction and has already committed.
            outcome = apply_repairs(connection, plan.repairs)
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
