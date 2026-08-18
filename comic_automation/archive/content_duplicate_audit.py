"""Read-only audit of exact content duplicates across the whole library.

This module never deletes, moves, quarantines, or enqueues anything. It
groups archives that share an identical ordered-page content signature
and reports them, so duplicate control no longer waits on the perceptual
backfill.

Why this exists as a separate audit
-----------------------------------

Two other things in this codebase look like they already cover it and do
not:

* ``duplicate_resolution_cli`` resolves exact duplicates, but only for
  copies already staged under an ``_extraneous`` holding folder. It
  cannot see duplicates sitting in the live library, which is where
  these are.
* ``near_duplicate.compare_archive_fingerprints`` short-circuits when two
  archives share a content signature, but it operates pairwise on
  perceptual fingerprints and therefore needs the perceptual backfill.
  Exact duplicates need none of that: ``archive_content_signatures``
  already holds the evidence.

The distinction that matters operationally: an *exact* duplicate is
decidable now, from stored data, with no judgement call. A *near*
duplicate is a similarity question that genuinely needs perceptual
hashes. Conflating the two is what made duplicate control look blocked
on a backfill it never depended on.

Signature semantics
-------------------

The grouping key is ``archive_content_signatures.digest`` --
``ordered-page-sha256``, the SHA-256 of the archive's page content in
page order. Two archives sharing it hold the same pages in the same
order, regardless of container bytes, compression level, page filenames,
or ComicInfo. That is deliberately *stronger* evidence than the ComicInfo
Series/Volume/Number triple that caused the 2026-08-17 incident, and
deliberately *weaker* than requiring byte-identical files, which would
miss recompressed copies. Byte identity is reported alongside as
``archive_sha256`` where available, so a reviewer can see which groups
are also byte-identical.

Recorded state is not live state
--------------------------------

Only locations with ``is_current = 1`` are counted, but that is a claim about
the row, not about the disk: 3,578 current locations were measured on
2026-08-17 that no longer described the file at their path. This audit does not
stat the filesystem -- it is safe to point at a protected backup, which
statting would break -- so it reports the staleness it *can* see from stored
data: a member whose ``archive_content_signatures.source_*`` no longer matches
its ``file_locations`` size and mtime is flagged ``signature_is_stale``, and
any group containing one is reported as not ``actionable``.

Such groups are still listed rather than hidden. The evidence is real; it is
just not safe to resolve until the location is repaired or re-verified against
the filesystem. **A caller resolving duplicates must re-verify against live
bytes regardless** -- this report is grounds for review, never for deletion on
its own.

Reads go through ``read_guards.read_consistent_snapshot``: one deferred
read transaction bracketed by ``PRAGMA data_version`` readings taken
outside it, which is the only guard that holds under WAL. File size and
mtime are diagnostics, never the concurrency gate.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Sequence

from comic_automation.database.read_guards import (
    DatabaseMutatedError,
    fingerprint_database,
    fingerprint_database_files,
    fingerprint_report_fields,
    quick_check,
    read_consistent_snapshot,
)

# Archives sharing this signature hold identical pages in identical order.
SIGNATURE_ALGORITHM = "ordered-page-sha256"
SIGNATURE_VERSION = "1"


def collect_duplicate_groups(connection: sqlite3.Connection) -> list[dict]:
    """Return every group of >=2 currently-located archives sharing a signature.

    Each group is returned with its members ordered largest-page-count first,
    then largest file, then lowest archive id -- a stable order so two runs
    over unchanged data produce identical output, and so the member a reviewer
    sees first is the most complete copy rather than an arbitrary one.

    Retention is NOT decided here. The audit reports; choosing a keeper is a
    separate, guarded step.
    """
    rows = connection.execute(
        """
        SELECT
            acs.digest            AS signature,
            acs.archive_id        AS archive_id,
            acs.page_count        AS page_count,
            acs.image_bytes       AS image_bytes,
            af.file_size          AS file_size,
            fl.path               AS path,
            fl.file_size          AS location_size,
            fl.modified_time_ns   AS location_mtime_ns,
            acs.source_file_size        AS signature_source_size,
            acs.source_modified_time_ns AS signature_source_mtime_ns,
            ah.digest             AS archive_sha256
        FROM archive_content_signatures AS acs
        JOIN file_locations AS fl
          ON fl.archive_id = acs.archive_id AND fl.is_current = 1
        JOIN archive_files AS af
          ON af.id = acs.archive_id
        LEFT JOIN archive_hashes AS ah
          ON ah.archive_id = acs.archive_id
         AND ah.algorithm = 'sha256'
        WHERE acs.algorithm = ?
          AND acs.algorithm_version = ?
          AND acs.digest IS NOT NULL
          AND acs.page_count > 0
        ORDER BY acs.digest, acs.page_count DESC, af.file_size DESC, acs.archive_id
        """,
        (SIGNATURE_ALGORITHM, SIGNATURE_VERSION),
    ).fetchall()

    by_signature: dict[str, list[dict]] = {}
    for row in rows:
        # A signature describes the bytes observed when it was computed. If the
        # location has moved on since, the signature no longer describes what
        # is at that path -- 3,578 such locations were measured on 2026-08-17.
        # is_current = 1 is a claim about the row, not about the disk.
        stale = (
            row["signature_source_size"] != row["location_size"]
            or row["signature_source_mtime_ns"] != row["location_mtime_ns"]
        )
        by_signature.setdefault(str(row["signature"]), []).append(
            {
                "archive_id": int(row["archive_id"]),
                "path": str(row["path"]),
                "page_count": int(row["page_count"]),
                "file_size": int(row["file_size"] or 0),
                "image_bytes": int(row["image_bytes"] or 0),
                "archive_sha256": row["archive_sha256"],
                "signature_is_stale": bool(stale),
            }
        )

    groups = []
    for signature, members in by_signature.items():
        if len(members) < 2:
            continue
        hashes = [m["archive_sha256"] for m in members]
        # Every member must have a hash AND they must all agree. An earlier
        # revision filtered out the missing ones first, so a group where one
        # member had no hash and the rest agreed was reported byte-identical on
        # the strength of evidence that did not exist for every member.
        byte_identical = all(hashes) and len(set(hashes)) == 1
        stale_members = [m for m in members if m["signature_is_stale"]]
        fresh_members = [m for m in members if not m["signature_is_stale"]]
        # Everything past the first copy is redundant. Sizes can differ between
        # members (different compression of identical pages), so the reclaimable
        # figure is measured against the largest, which is the copy most likely
        # to be kept.
        largest = max(m["file_size"] for m in members)
        groups.append(
            {
                "signature": signature,
                "member_count": len(members),
                "redundant_count": len(members) - 1,
                "page_count": members[0]["page_count"],
                "byte_identical": byte_identical,
                "stale_member_count": len(stale_members),
                # Only a group whose every member's signature still describes
                # its recorded location is safe to act on without re-verifying
                # against the filesystem first.
                "actionable": not stale_members and len(fresh_members) >= 2,
                "reclaimable_bytes": sum(m["file_size"] for m in members) - largest,
                "members": members,
            }
        )

    # Largest reclaim first: a reviewer working top-down frees the most space
    # per decision made.
    groups.sort(key=lambda g: (-g["reclaimable_bytes"], g["signature"]))
    return groups


def summarize(groups: list[dict]) -> dict:
    """Aggregate counts a reviewer needs before opening the detail rows."""
    actionable = [g for g in groups if g["actionable"]]
    return {
        "group_count": len(groups),
        "redundant_copies": sum(g["redundant_count"] for g in groups),
        "reclaimable_bytes": sum(g["reclaimable_bytes"] for g in groups),
        "byte_identical_groups": sum(1 for g in groups if g["byte_identical"]),
        "largest_group": max((g["member_count"] for g in groups), default=0),
        # Reported separately rather than filtered out: a group containing a
        # stale member is still evidence worth seeing, it is just not safe to
        # resolve until the location is repaired or re-verified.
        "actionable_groups": len(actionable),
        "actionable_redundant_copies": sum(g["redundant_count"] for g in actionable),
        "actionable_reclaimable_bytes": sum(
            g["reclaimable_bytes"] for g in actionable
        ),
        "groups_with_stale_members": sum(
            1 for g in groups if g["stale_member_count"]
        ),
    }


def _write_json(path: Path, payload: object) -> Path:
    path = Path(path).resolve(strict=False)
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite existing report: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _write_csv(path: Path, groups: list[dict]) -> Path:
    """One row per member, so a group's members sort together in a spreadsheet."""
    path = Path(path).resolve(strict=False)
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite existing report: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "signature",
                "member_count",
                "actionable",
                "byte_identical",
                "signature_is_stale",
                "archive_id",
                "page_count",
                "file_size",
                "archive_sha256",
                "path",
            ]
        )
        for group in groups:
            for member in group["members"]:
                writer.writerow(
                    [
                        group["signature"],
                        group["member_count"],
                        "yes" if group["actionable"] else "no",
                        "yes" if group["byte_identical"] else "no",
                        "yes" if member["signature_is_stale"] else "",
                        member["archive_id"],
                        member["page_count"],
                        member["file_size"],
                        member["archive_sha256"] or "",
                        member["path"],
                    ]
                )
    return path


def run_audit(
    *,
    database: Path,
    json_output: Path | None = None,
    csv_output: Path | None = None,
) -> dict:
    """Produce the read-only exact-duplicate report.

    Raises `FileNotFoundError` if the database does not exist,
    `DatabaseIntegrityError` if `PRAGMA quick_check` fails,
    `DatabaseChangedError` if another connection committed during the run,
    and `DatabaseMutatedError` if this process's own reads somehow touched
    the file.
    """
    database = Path(database).resolve(strict=False)
    if not database.is_file():
        raise FileNotFoundError(f"Database does not exist: {database}")

    started = time.perf_counter()
    fingerprint_before = fingerprint_database(database)
    files_before = fingerprint_database_files(database)

    def read(connection: sqlite3.Connection) -> list[dict]:
        return collect_duplicate_groups(connection)

    snapshot = read_consistent_snapshot(
        database,
        read,
        context="content duplicate audit",
        integrity_check=quick_check,
    )
    groups = snapshot.result

    # Re-stat after the connection closes: mode=ro plus query_only should make
    # this impossible, and this is the guarantee the audit actually promises.
    fingerprint_after = fingerprint_database(database)
    files_after = fingerprint_database_files(database)
    if fingerprint_after != fingerprint_before:
        raise DatabaseMutatedError(
            "Database changed during a read-only audit run: "
            f"before={fingerprint_before} after={fingerprint_after}. "
            "This audit must never modify the database it inspects."
        )

    output = {
        "database": str(database),
        "signature_algorithm": SIGNATURE_ALGORITHM,
        "signature_version": SIGNATURE_VERSION,
        "quick_check": snapshot.quick_check,
        "data_version_before": snapshot.data_version_before,
        "data_version_after": snapshot.data_version_after,
        # Shared provenance block, so this report's fingerprint fields carry
        # the same keys and the same diagnostic-only labelling as every other
        # audit in this family.
        **fingerprint_report_fields(
            fingerprint_before=fingerprint_before,
            fingerprint_after=fingerprint_after,
            files_before=files_before,
            files_after=files_after,
        ),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "summary": summarize(groups),
        "groups": groups,
    }

    if json_output is not None:
        output["json_output"] = str(_write_json(json_output, output))
    if csv_output is not None:
        output["csv_output"] = str(_write_csv(csv_output, groups))
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only audit of exact content duplicates (identical "
            "ordered-page signature) across the whole library. Never "
            "deletes, moves, or enqueues anything."
        )
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--csv-output", type=Path)
    parser.add_argument(
        "--top",
        type=int,
        default=15,
        help="How many groups to print in the summary (default 15).",
    )
    return parser


def print_summary(output: dict, top: int = 15) -> None:
    summary = output["summary"]
    print("Exact content-duplicate audit (read-only).")
    print(f"Database:            {output['database']}")
    print(f"quick_check:         {output['quick_check']}")
    print(
        "data_version:        "
        f"{output['data_version_before']} -> {output['data_version_after']}"
    )
    print(f"Duplicate groups:    {summary['group_count']:,}")
    print(f"Redundant copies:    {summary['redundant_copies']:,}")
    print(f"  byte-identical:    {summary['byte_identical_groups']:,} groups")
    print(f"  largest group:     {summary['largest_group']:,} members")
    print(f"Reclaimable:         {summary['reclaimable_bytes'] / (1024 ** 3):,.2f} GiB")
    print("")
    print("Actionable now (every member's signature still describes its location):")
    print(f"  groups:            {summary['actionable_groups']:,}")
    print(f"  redundant copies:  {summary['actionable_redundant_copies']:,}")
    print(
        "  reclaimable:       "
        f"{summary['actionable_reclaimable_bytes'] / (1024 ** 3):,.2f} GiB"
    )
    print(
        f"  held back:         {summary['groups_with_stale_members']:,} groups "
        "contain a member whose recorded location has drifted"
    )

    if output["groups"]:
        print(f"\nLargest reclaims (top {top}):")
        for group in output["groups"][:top]:
            print(
                f"  {group['reclaimable_bytes'] / (1024 ** 2):>9,.1f} MiB  "
                f"{group['member_count']} copies, {group['page_count']} pages"
                f"{'  [byte-identical]' if group['byte_identical'] else ''}"
            )
            for member in group["members"]:
                print(f"      {member['archive_id']:>7}  {member['path']}")

    for key in ("json_output", "csv_output"):
        if output.get(key):
            print(f"{key}: {output[key]}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = run_audit(
        database=args.database,
        json_output=args.json_output,
        csv_output=args.csv_output,
    )
    print_summary(output, top=args.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
