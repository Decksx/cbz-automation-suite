"""Operator CLI for the read-only provenance backfill planner (slice 3).

Reads. Never writes to the database. The only files it creates are the plan
artifacts, and it refuses to place those beside the database or over an
existing file.

    python -m comic_automation.archive.provenance_backfill_cli \\
        --database G:\\ComicAutomation\\TestDatabase\\inspection-working.db \\
        --json  plans/backfill.json \\
        --csv   plans/backfill.csv

Exit codes, because a wrapper script decides on these rather than on the text:

    0  plan written, gates passed
    1  plan written, one or more gates failed
    2  argparse usage error, or the database file does not exist
    3  database integrity failure
    4  the database changed under the read, so no plan was emitted
    5  planning failed
    6  the output paths were refused, or the write failed; either way the
       plan did NOT commit
    7  every requested artifact COMMITTED and is complete, but a staging
       `.partial` survived and needs clearing by hand
  130  the writer had nothing additional to report, so the original interrupt
       arrived unchanged; the message, not the code, says whether it
       committed

These are chosen by the state the writer left behind, never by what went
wrong, and an interrupt is not tied to any one of them:

  * an interrupt that leaves nothing to act on arrives unconverted and exits
    130. That is either every requested artifact committing cleanly -- a
    CSV-only or envelope-only write as much as a pair -- or nothing
    committing and every staging file being removed. The message, not the
    code, says which, and for a pair it says so by the envelope;
  * an interrupt after the CSV committed but before the envelope did leaves a
    committed artifact that will not be removed, which the interrupt itself
    cannot say, so the writer substitutes an `OutputPathError` and this exits
    6 -- correctly, because that plan did not commit;
  * an interrupt that leaves staging residue behind a committed plan is a
    `StagingResidueError` and exits 7.

6, 7 and 130 are therefore distinct states, not three kinds of cause. 7 is not
a failed write: everything asked for is on disk, so a consumer will read the
plan as committed and this command says the same thing rather than the
opposite. For a pair that consumer reads the envelope; a CSV-only write can
also exit 7, and has no envelope because none was requested.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from comic_automation.archive.provenance_backfill_planner import (
    BackfillPlannerError,
    StagingResidueError,
    plan_backfill,
    preflight_output_paths,
    write_plan_artifacts,
)
from comic_automation.database.read_guards import (
    DatabaseChangedError,
    DatabaseIntegrityError,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="provenance-backfill-plan",
        description=(
            "Plan the revision-aware provenance backfill. Read-only: this "
            "command classifies evidence rows and writes plan artifacts, and "
            "applies nothing."
        ),
    )
    parser.add_argument("--database", required=True, help="path to the database")
    parser.add_argument("--json", dest="json_path", help="write the plan envelope here")
    parser.add_argument("--csv", dest="csv_path", help="write per-row bindings here")
    return parser


def _render_summary(plan, snapshot) -> str:
    lines = [
        "provenance backfill plan (nothing applied)",
        "",
        f"  planner          {plan.planner_version}",
        f"  snapshot digest  {plan.snapshot_digest}",
        f"  plan digest      {plan.plan_digest}",
        "",
        "  read guarantee",
        f"    quick_check          {snapshot.quick_check}",
        f"    data_version         {snapshot.data_version_before} -> "
        f"{snapshot.data_version_after}",
        f"    unchanged            {snapshot.data_version_unchanged}",
        "",
        f"  planned rows     {plan.totals['planned_rows']}",
        f"  planned sides    {plan.totals['planned_sides']}",
        f"    bound          {plan.totals['bound']}",
        f"    unresolved     {plan.totals['unresolved']}",
        "",
        "  per table",
    ]

    for table, bucket in plan.totals["per_table"].items():
        lines.append(
            f"    {table:<28} {bucket['rows']:>9}  "
            f"bound {bucket['bound']:>9}  unresolved {bucket['unresolved']:>7}"
        )

    lines.extend(["", "  per basis"])

    for basis, count in plan.totals["per_basis"].items():
        lines.append(f"    {basis:<32} {count:>9}")

    gates = plan.gates
    lines.extend(
        [
            "",
            "  archive-level gates (no receiving row exists for these)",
            f"    provisional archives          {len(gates.provisional_archives):>9}",
            f"    archives without a revision   {len(gates.archives_without_revision):>9}",
            f"    drift archives                {len(gates.drift_archives):>9}",
            "",
            f"  quarantine rows excluded        {plan.quarantine_rows:>9}",
        ]
    )

    if plan.gate_failures:
        lines.extend(["", "  GATE FAILURES"])
        lines.extend(f"    - {failure}" for failure in plan.gate_failures)
    else:
        lines.extend(["", "  gate: no failures"])

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Both artifact paths are checked BEFORE the database is read, so an
    # unusable output path costs nothing and cannot leave a half-written pair.
    try:
        preflight_output_paths(
            json_path=args.json_path, csv_path=args.csv_path,
            database=args.database,
        )
    except BackfillPlannerError as error:
        print(f"error: {error}", file=sys.stderr)
        return 6

    try:
        snapshot = plan_backfill(args.database)
    except FileNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except DatabaseIntegrityError as error:
        print(f"error: {error}", file=sys.stderr)
        return 3
    except DatabaseChangedError as error:
        # The read is not trustworthy, so no plan is emitted at all rather
        # than one that mixes pre- and post-change observations.
        print(f"error: {error}", file=sys.stderr)
        return 4
    except BackfillPlannerError as error:
        print(f"error: {error}", file=sys.stderr)
        return 5

    plan = snapshot.result
    print(_render_summary(plan, snapshot))

    residue: list[Path] = []

    try:
        written = write_plan_artifacts(
            plan,
            json_path=args.json_path,
            csv_path=args.csv_path,
            database=args.database,
        )
    except StagingResidueError as error:
        # Caught BEFORE its parent, because the two mean opposite things. The
        # plan committed -- every artifact asked for reached its final name
        # and the envelope attests to the bindings -- and only a `.partial`
        # could not be cleared. Reporting it as a failed write would have this
        # command call incomplete the very plan that slice 4, reading the
        # envelope, will correctly treat as committed.
        print(f"warning: {error}", file=sys.stderr)
        written = error.committed
        residue = error.residue
    except BackfillPlannerError as error:
        print(f"error: {error}", file=sys.stderr)
        return 6
    except KeyboardInterrupt:
        # The writer substitutes its own error only when it has something to
        # report, so an interrupt arriving here unconverted means it had
        # nothing additional: either every requested artifact committed
        # cleanly -- which includes a CSV-only write, not just a pair -- or
        # nothing committed and every staging file was removed. An interrupt
        # before promotion whose cleanup then FAILED does not reach here; it
        # is converted, because the surviving staging file must be named.
        #
        # Those states are told apart the way slice 4 tells them apart -- on
        # the envelope, where one was requested -- so this command cannot
        # contradict its own consumer on this path either.
        if args.json_path is None:
            print(
                "interrupted; no envelope was requested, so there is no "
                f"commit marker to read -- check {args.csv_path} by hand",
                file=sys.stderr,
            )
        elif Path(args.json_path).exists():
            print(
                f"interrupted, but the envelope is present: {args.json_path}"
                " -- the plan COMMITTED, is complete, and must NOT be removed",
                file=sys.stderr,
            )
        else:
            print(
                "interrupted before the envelope was committed; no plan was "
                "written",
                file=sys.stderr,
            )

        # 130 is the conventional SIGINT code. Deliberately not 6, which would
        # claim a failed write for a plan that may be complete, and not 1,
        # which already means the gates failed.
        return 130

    if written:
        print("")

    for label in ("json", "csv"):
        if label in written:
            print(f"  wrote {written[label]}")

    if residue:
        for path in residue:
            print(f"  staging file left behind, remove by hand: {path}")

        # Distinct from success and from a failed write alike: the artifacts
        # are committed and usable, and a human still has to clear the
        # residue. It takes precedence over the gate code deliberately -- a
        # gate failure is a property of the plan's contents, readable in the
        # summary above, while residue is filesystem state nothing else will
        # clean up.
        return 7

    # A gate failure is reported in the exit code as well as the text, so a
    # wrapper script cannot mistake a failing plan for a passing one.
    return 1 if plan.gate_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
