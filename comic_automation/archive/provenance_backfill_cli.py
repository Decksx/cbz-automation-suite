"""Operator CLI for the read-only provenance backfill planner (slice 3).

Reads. Never writes to the database. The only files it creates are the plan
artifacts, and it refuses to place those beside the database or over an
existing file.

    python -m comic_automation.archive.provenance_backfill_cli \\
        --database G:\\ComicAutomation\\TestDatabase\\inspection-working.db \\
        --json  plans/backfill.json \\
        --csv   plans/backfill.csv
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from comic_automation.archive.provenance_backfill_planner import (
    BackfillPlannerError,
    write_plan_csv,
    write_plan_json,
    plan_backfill,
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
        "",
        "  read guarantee",
        f"    quick_check          {snapshot.quick_check}",
        f"    data_version         {snapshot.data_version_before} -> "
        f"{snapshot.data_version_after}",
        f"    unchanged            {snapshot.data_version_unchanged}",
        "",
        f"  planned rows     {plan.totals['planned_rows']}",
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

    try:
        if args.json_path:
            written = write_plan_json(plan, args.json_path, database=args.database)
            print(f"\n  wrote {written}")

        if args.csv_path:
            written = write_plan_csv(plan, args.csv_path, database=args.database)
            print(f"  wrote {written}")
    except BackfillPlannerError as error:
        print(f"error: {error}", file=sys.stderr)
        return 6

    # A gate failure is reported in the exit code as well as the text, so a
    # wrapper script cannot mistake a failing plan for a passing one.
    return 1 if plan.gate_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
