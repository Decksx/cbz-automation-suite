"""Emit a read-only canonical rename/supersession plan as JSON on stdout.

No apply mode. Exit 0: complete plan with no review holds; 1: complete plan
with review holds; 2: no trustworthy plan (errors go to stderr). Redirect
stdout to an operator-selected path outside the library when saving a plan.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from comic_automation.library.canonical_plan import build_plan


def main(argv: list[str] | None = None) -> int:
    """Plan only; never install schema, acquire apply locks or change books."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library-root", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--quarantine-date", required=True, help="YYYY-MM-DD; explicit for reproducible plans")
    parser.add_argument("--accept-comicinfo-drop", action="store_true")
    parser.add_argument("--temp-dir", type=Path)
    parser.add_argument("--max-held-bytes", type=int, default=512 * 1024 * 1024)
    parser.add_argument("--max-copy-bytes", type=int)
    args = parser.parse_args(argv)
    try:
        doc = build_plan(args.library_root, database=args.database,
            quarantine_date=args.quarantine_date,
            accept_comicinfo_drop=args.accept_comicinfo_drop,
            temp_dir=args.temp_dir, max_held_bytes=args.max_held_bytes,
            max_copy_bytes=args.max_copy_bytes)
    except Exception as exc:
        # Exit 1 means only a complete plan with review holds. Unexpected
        # failures also invalidate the whole observation and must mean 2.
        print(f"Plan not produced: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False))
    return 1 if doc["counts"]["review"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
