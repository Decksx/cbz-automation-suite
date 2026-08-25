"""Operator CLI for the read-only revision-retention planner.

Reports only. There is no `--confirm`, no apply path and no write mode, and
that is a property of this slice rather than a flag that happens to be off:
the module opens the database exclusively through
`revision_retention.plan_from_database`, which goes through the repository's
WAL-aware read guard (`mode=ro` plus `PRAGMA query_only`, one deferred read
transaction, `data_version` sampled either side).

Nothing runs at import. Every path -- including `--help` -- reaches argparse
without touching a database, a library root or the filesystem beyond the
files named on the command line. That is deliberate: this repository has
entry points that perform work during startup, and probing one with `--help`
has already scanned a live share once.

Exit codes
----------

    0   a plan was produced and reconciled
    1   the plan could not be produced or written (colliding output paths, an
        unreadable database, a bad manifest, a concurrent commit during the
        read, a failed write)
    3   the plan was produced but did not pass the reconciliation gate

The gate is three conditions, not one: zero classified residue, zero rows
carrying evidence the planner could not interpret, and zero archives holding
no revision row. Residue alone is insufficient, because a current revision
keeps its `protected` classification even when its archive's evidence is
unreadable -- and production holds one revision per archive, every one of them
current, so an unreadable archive there would produce no residue at all.
`--allow-unexplained` downgrades all three to a warning for development
databases, and says which fired rather than falling silent.

Output paths are refused before anything is opened when they collide with the
database, its WAL/SHM sidecars, the pin manifest, or each other. The read-only
guarantee covers the connection; it does not cover a report writer handed the
database as its destination.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from comic_automation.archive.revision_retention import (
    EXECUTION_STATUS,
    PLANNER_VERSION,
    RULE_GRANULARITY,
    OutputPathError,
    RetentionPolicy,
    load_pin_manifest,
    plan_from_database,
    write_csv,
    write_json,
)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_UNEXPLAINED = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m comic_automation.archive.revision_retention_cli",
        description=(
            "Report which archive revisions may ever be pruned. Read-only: "
            "this tool never deletes a revision, never writes to the "
            "database, and has no apply path."
        ),
    )
    parser.add_argument(
        "--database",
        required=True,
        help="Path to the SQLite database. Opened strictly read-only.",
    )
    parser.add_argument(
        "--pins",
        default=None,
        help=(
            "Optional JSON manifest of operator pins. Every pin is validated "
            "against the database and contributes to the snapshot digest."
        ),
    )
    parser.add_argument(
        "--keep-previous",
        type=int,
        default=1,
        metavar="N",
        help=(
            "Retention window, in generations kept behind the current "
            "revision (default: 1, the roadmap's 'at least the immediately "
            "previous revision'). 0 keeps only the current revision."
        ),
    )
    parser.add_argument(
        "--json-out",
        default=None,
        help="Write the full plan as deterministic JSON to this path.",
    )
    parser.add_argument(
        "--csv-out",
        default=None,
        help="Write one row per revision as deterministic CSV to this path.",
    )
    parser.add_argument(
        "--allow-unexplained",
        action="store_true",
        help=(
            "Report unexplained residue as a warning instead of failing. "
            "For development databases; production runs must not need it."
        ),
    )
    parser.add_argument(
        "--show-rules",
        action="store_true",
        help=(
            "Print each protection rule and the granularity it is evaluated "
            "at, then continue."
        ),
    )
    return parser


def _identity(path: str) -> str:
    """A comparable identity for a path that may not exist yet.

    `realpath` resolves symlinks, junctions and `..` segments; `normcase`
    folds case and separators, which matters because this repository's
    library volumes are case-insensitive and `X:/a` and `x:\\a` are one file.
    Neither requires the path to exist, so an output file can be checked
    before it is created.
    """
    return os.path.normcase(os.path.realpath(path))


def _same_file(left: str, right: str) -> bool:
    """True when two paths reach the same file.

    Two tests, because neither alone is sufficient. The textual comparison
    catches paths that do not exist yet, which is the ordinary case for an
    output file. `os.path.samefile` catches what text cannot: hard links and
    distinct paths onto the same volume that resolve differently but share an
    inode or file index. It needs both files to exist, so it only supplements.
    """
    if _identity(left) == _identity(right):
        return True

    try:
        return os.path.samefile(left, right)
    except OSError:
        # One of them does not exist, or is not stat-able. The textual test
        # above has already had its say; an unreadable path is not evidence
        # of sameness.
        return False


def _refuse_colliding_outputs(
    *,
    database: str,
    pins: str | None,
    json_out: str | None,
    csv_out: str | None,
) -> None:
    """Refuse output paths that would destroy an input, or each other.

    Checked before the database is opened, so a refusal costs nothing and
    nothing has been written or read.

    This exists because the planner's read-only guarantee stops at the
    connection. `mode=ro` and `PRAGMA query_only` make it impossible for the
    *reader* to modify the database, and none of that survives contact with
    `--json-out <database>`: the guarded read closes, and the report writer
    truncates the file through an ordinary `write_text`. The read-only claim
    would still have been true, and the database would still be gone.

    The WAL and SHM sidecars are included. They are not the database file, but
    truncating either one destroys uncommitted state or forces recovery, and
    an operator who typed one of those paths did not mean to.

    Sidecars are derived from **both** the typed path and its fully resolved
    target, and that is not belt-and-braces. `read_guards` opens the database
    through `path.resolve(strict=True)`, so when the typed path is a symlink
    or sits under a junction, SQLite works against the resolved file and puts
    its WAL and SHM beside *that* -- while a sidecar name built by
    concatenating onto the typed path names a different, quite possibly
    nonexistent, file. Protecting only the typed form leaves the real WAL
    unguarded: it matches neither the typed sidecar nor the database itself,
    `samefile` cannot help while it does not yet exist, and the writers'
    SQLite-header check does not recognise a WAL or SHM file either, since
    neither begins with the database magic. Every layer misses it, so the
    resolved names are enumerated here.

    The resulting list is deduplicated by resolved identity -- for the
    ordinary case where nothing is a link, both derivations give the same four
    paths, and reporting a collision twice helps nobody.
    """
    protected: list[tuple[str, str]] = [(database, "the database being read")]

    # os.path.realpath rather than Path.resolve: it does not raise on a
    # missing path, and the database's existence is SQLite's to complain
    # about, with a better message than this function could give.
    for base in (database, os.path.realpath(database)):
        protected.extend(
            (base + suffix, "a database sidecar")
            for suffix in ("-wal", "-shm")
        )

    if pins is not None:
        protected.append((pins, "the pin manifest"))

    seen: set[str] = set()
    deduplicated: list[tuple[str, str]] = []

    for path, description in protected:
        key = _identity(path)

        if key in seen:
            continue

        seen.add(key)
        deduplicated.append((path, description))

    protected = deduplicated

    for flag, output in (("--json-out", json_out), ("--csv-out", csv_out)):
        if output is None:
            continue

        for target, description in protected:
            if _same_file(output, target):
                raise OutputPathError(
                    f"{flag}={output!r} is {description} "
                    f"({os.path.realpath(target)}). Refusing to write a "
                    "report over an input: the database is read through a "
                    "read-only connection, but that guarantee does not "
                    "extend to output paths, and this write would truncate "
                    "the file the plan describes."
                )

    if json_out is not None and csv_out is not None:
        if _same_file(json_out, csv_out):
            raise OutputPathError(
                f"--json-out and --csv-out are the same file ({json_out!r}). "
                "Refusing rather than writing both: the second write would "
                "silently replace the first, leaving one format and no sign "
                "that the other was ever requested."
            )


def _print_rules() -> None:
    print("Protection rules and evidence granularity")
    print("-" * 60)

    for rule, granularity in sorted(RULE_GRANULARITY.items()):
        print(f"  {rule:<28} {granularity}")

    print(
        "\n  archive_proxy means the evidence keys on archive_id and cannot\n"
        "  name a revision, so it conservatively protects every revision of\n"
        "  that archive. Roadmap Step 4 (revision-aware provenance) is what\n"
        "  would make those rules revision-granular.\n"
    )


def print_summary(payload: dict[str, Any]) -> None:
    totals = payload["totals"]

    print("Revision retention plan")
    print("=" * 60)
    print(f"Database:              {payload['database']}")
    print(f"Planner:               {payload['planner_version']}")
    print(f"Execution:             {payload['execution_status']}")
    window = payload["policy"]["keep_previous_generations"]
    print(f"Retention window:      {window} generation(s)")

    pin_source = payload.get("pin_source")
    pin_origin = "" if not pin_source else f" from {pin_source}"
    print(f"Pins:                  {len(payload['pins'])}{pin_origin}")

    print(f"Snapshot digest:       {payload['snapshot_digest']}")
    print("-" * 60)
    print(f"Revisions:             {totals['revisions']}")
    print(f"  archives:            {totals['archives']}")
    print(f"  current:             {totals['current']}")
    print(f"  noncurrent:          {totals['noncurrent']}")
    print(f"    protected:         {totals['protected_noncurrent']}")
    print(f"    candidates:        {totals['candidates']}")
    print(f"    unexplained:       {totals['unexplained']}")
    print("-" * 60)
    print(
        "Rows carrying uninterpretable evidence: "
        f"{totals['rows_with_unknown_evidence']} "
        f"across {totals['archives_with_unknown_evidence']} archive(s)"
    )
    print(
        "Archives with no revision row:          "
        f"{totals['archives_without_revisions']}"
    )
    print("-" * 60)
    print(
        "Candidates executable under schema 014: "
        f"{totals['candidates_feasible_under_schema_014']}"
    )
    print(
        "  Policy eligibility and schema feasibility are separate answers.\n"
        "  Schema 014 refuses every revision delete while its archive "
        "exists,\n  so a candidate here is a policy statement, not a "
        "pending action."
    )
    print("-" * 60)
    print(f"quick_check:           {payload['quick_check']}")
    print(
        "data_version:          "
        f"{payload['data_version_before']} -> {payload['data_version_after']}"
    )
    print(
        "concurrent commit:     "
        f"{payload['concurrent_commit_detected']}"
    )

    if payload.get("json_output"):
        print(f"JSON output:           {payload['json_output']}")
    if payload.get("csv_output"):
        print(f"CSV output:            {payload['csv_output']}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.show_rules:
        _print_rules()

    # Output paths are settled before anything is opened or read. A refusal
    # here costs nothing; discovering the collision after a seven-second read
    # would mean discovering it with the write already underway.
    try:
        _refuse_colliding_outputs(
            database=args.database,
            pins=args.pins,
            json_out=args.json_out,
            csv_out=args.csv_out,
        )
    except OutputPathError as error:
        print(f"Refusing to run: {error}", file=sys.stderr)
        return EXIT_FAILED

    try:
        policy = RetentionPolicy(keep_previous_generations=args.keep_previous)
        # None means no manifest was named; an empty list means one was named
        # and held no pins. The distinction reaches the snapshot digest.
        pin_entries = (
            None if args.pins is None else load_pin_manifest(args.pins)
        )
        snapshot = plan_from_database(
            args.database,
            policy=policy,
            pin_entries=pin_entries,
            pin_source=(
                None if args.pins is None else str(Path(args.pins))
            ),
        )
    except Exception as error:
        print(f"Revision retention planning failed: {error}", file=sys.stderr)
        return EXIT_FAILED

    plan = snapshot.result
    payload: dict[str, Any] = {
        "database": str(snapshot.database),
        "planner_version": PLANNER_VERSION,
        "execution_status": EXECUTION_STATUS,
        **plan.as_dict(),
        **snapshot.report_fields(),
    }

    # Written after the summary is assembled but reported before the gate, so
    # a write failure is never mistaken for a policy outcome.
    try:
        if args.json_out:
            payload["json_output"] = str(write_json(plan, args.json_out))
        if args.csv_out:
            payload["csv_output"] = str(write_csv(plan, args.csv_out))
    except (OutputPathError, OSError) as error:
        print_summary(payload)
        print(f"\nFAILED to write the report: {error}", file=sys.stderr)
        return EXIT_FAILED

    print_summary(payload)

    failures = plan.gate_failures

    if failures:
        message = "; ".join(failures)

        if args.allow_unexplained:
            print(
                f"\nWARNING: {message}. Nothing here is reported as prunable "
                "-- residue and unreadable evidence never become candidates "
                "-- but the plan is not fully reconciled. "
                "--allow-unexplained suppressed the failure."
            )
            return EXIT_OK

        print(
            f"\nFAILED: {message}. The production gate for this planner is "
            "zero residue, zero rows carrying evidence the planner could not "
            "interpret, and zero archives without a revision row. A current "
            "revision stays protected even when its archive's evidence is "
            "unreadable, so residue alone would not have caught this.",
            file=sys.stderr,
        )
        return EXIT_UNEXPLAINED

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
