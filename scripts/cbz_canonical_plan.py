"""Emit a read-only canonical rename/supersession plan as UTF-8 JSON.

No apply mode. --output exclusively creates a UTF-8 file outside the library;
otherwise JSON goes to stdout. Exit 0: complete plan with no review holds;
1: complete plan with review holds; 2: no trustworthy plan (stderr explains).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from comic_automation.library.canonical_plan import build_plan


def _output_target(output: Path, root: Path) -> Path:
    path, library = output.absolute(), root.resolve()
    if path.resolve() == library or library in path.resolve().parents:
        raise ValueError("--output must be outside the library")
    if os.path.lexists(path):
        raise FileExistsError(f"output already exists: {path}")
    return path


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
    parser.add_argument("--output", type=Path, help="exclusively create a UTF-8 plan outside the library")
    args = parser.parse_args(argv)
    try:
        if args.output is not None:
            _output_target(args.output, args.library_root)
        doc = build_plan(args.library_root, database=args.database,
            quarantine_date=args.quarantine_date,
            accept_comicinfo_drop=args.accept_comicinfo_drop,
            temp_dir=args.temp_dir, max_held_bytes=args.max_held_bytes,
            max_copy_bytes=args.max_copy_bytes)
        payload = json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        if args.output is not None:
            target = _output_target(args.output, args.library_root)
            # Exclusive creation is the race-safe refusal of an occupied output.
            with target.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
    except Exception as exc:
        # Exit 1 means only a complete plan with review holds. Unexpected
        # failures also invalidate the whole observation and must mean 2.
        print(f"Plan not produced: {exc}", file=sys.stderr)
        return 2
    if args.output is None:
        print(json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False))
    else:
        print(json.dumps({"output": str(target), "plan_digest": doc["plan_digest"], "counts": doc["counts"]}))
    return 1 if doc["counts"]["review"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
