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
    1   the plan could not be produced (unreadable database, bad manifest,
        a concurrent commit during the read)
    3   the plan was produced but contains unexplained residue

Residue is a failure by default. `unexplained` is the bucket for evidence the
planner could not interpret, so a plan carrying any is one whose reconciliation
is incomplete -- and the production gate for this slice is that there is none.
`--allow-unexplained` downgrades it to a warning for use against development
databases, and says so in the summary rather than falling silent.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from comic_automation.archive.revision_retention import (
    EXECUTION_STATUS,
    PLANNER_VERSION,
    RULE_GRANULARITY,
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

    try:
        policy = RetentionPolicy(keep_previous_generations=args.keep_previous)
        pin_entries = (
            load_pin_manifest(args.pins) if args.pins is not None else []
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

    if args.json_out:
        payload["json_output"] = str(write_json(plan, args.json_out))
    if args.csv_out:
        payload["csv_output"] = str(write_csv(plan, args.csv_out))

    print_summary(payload)

    if plan.totals["unexplained"]:
        message = (
            f"{plan.totals['unexplained']} revision(s) could not be "
            "classified from the available evidence."
        )

        if args.allow_unexplained:
            print(f"\nWARNING: {message} Reported as residue, never as "
                  "prunable. --allow-unexplained suppressed the failure.")
            return EXIT_OK

        print(
            f"\nFAILED: {message} The production gate for this planner is "
            "unexplained = 0; residue is never a prune candidate, but a "
            "plan carrying any is not fully reconciled.",
            file=sys.stderr,
        )
        return EXIT_UNEXPLAINED

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
