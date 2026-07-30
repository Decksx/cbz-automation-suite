from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from comic_automation.archive.perceptual_reuse_analysis import (
    analyze_reuse_opportunity,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze, without database writes, how much missing "
            "Version 1 perceptual evidence can be reused from pages "
            "with identical exact SHA-256 evidence."
        )
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    return parser


def run_reuse_analysis(
    *,
    database: Path,
    json_output: Path | None,
) -> dict:
    result = analyze_reuse_opportunity(database)

    if json_output is not None:
        output = json_output.resolve(strict=False)
        database_path = database.resolve(strict=True)

        if output == database_path:
            raise ValueError(
                "--json-output must not be the database path."
            )

        output.parent.mkdir(parents=True, exist_ok=True)
        result["json_output"] = str(output)
        output.write_text(
            json.dumps(result, indent=2) + "\n",
            encoding="utf-8",
        )

    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        result = run_reuse_analysis(
            database=args.database,
            json_output=args.json_output,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Reuse opportunity analysis failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
