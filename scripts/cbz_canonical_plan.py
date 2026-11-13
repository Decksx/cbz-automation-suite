"""Emit a read-only canonical rename/supersession plan as UTF-8 JSON.

No apply mode. --output exclusively creates a UTF-8 file outside the library;
otherwise JSON goes to stdout. Exit 0: complete plan with no review holds;
1: plan with review holds or, in scoped mode, skipped series folders;
2: no trustworthy plan (stderr explains).

By default any change under the library root during planning refuses the
whole plan. --scoped (or one or more --scope NAME) compares each series
folder on its own and lists a changed folder under skipped_scopes instead.

--changed-in-batch N / --changed-since-batch N (#158 B2) choose the scopes
from catalog events inside a completed discovery batch's bounds (that
batch only, or that batch through the newest event). The selection is
sealed into planning_scope. A changed folder the selection could not plan
(unreadable, or changed root-level books) also makes the exit 1. If no
folder needs planning, only the selection report is printed (no plan and
no --output file): exit 0, or 1 when something was omitted.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from comic_automation.library.canonical_plan import _INTERNAL, build_plan
from comic_automation.library.changed_scopes import changed_scopes
from comic_automation.library.number_style import DEFAULT_REGISTRY


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
    parser.add_argument("--number-style", choices=("auto", "issue", "chapter"), default="auto")
    parser.add_argument("--number-style-registry", type=Path,
                        default=DEFAULT_REGISTRY)
    parser.add_argument("--series-folder-root", action="store_true",
        help="Confirm that the scan root itself is the operator-chosen series folder.")
    parser.add_argument("--scoped", action="store_true",
        help="Opt in to scoped planning: a series folder that changes during planning is "
             "skipped and reported instead of refusing the whole plan.")
    parser.add_argument("--scope", action="append", metavar="NAME",
        help="Plan only this series folder directly under the root (exact spelling); "
             "repeatable; implies --scoped.")
    changed = parser.add_mutually_exclusive_group()
    changed.add_argument("--changed-in-batch", type=int, metavar="N",
        help="Plan the series folders with catalog events inside discovery batch N.")
    changed.add_argument("--changed-since-batch", type=int, metavar="N",
        help="Plan the series folders with catalog events from batch N's start to now.")
    args = parser.parse_args(argv)
    batch = args.changed_in_batch if args.changed_in_batch is not None else args.changed_since_batch
    if batch is not None and args.scope:
        parser.error("--scope cannot be combined with a changed-series selection")
    chosen = None
    try:
        if args.output is not None:
            _output_target(args.output, args.library_root)
        if batch is not None:
            chosen = changed_scopes(args.database, args.library_root, batch_id=batch,
                                    since=args.changed_since_batch is not None, internal=_INTERNAL)
            if not chosen.planned:
                # Nothing to plan; build_plan would refuse an empty scope list.
                report = {"plan": None, "selection": chosen.as_dict()}
                print(json.dumps(report, sort_keys=True, indent=2, ensure_ascii=True))
                return 1 if chosen.omitted else 0
        doc = build_plan(args.library_root, database=args.database,
            quarantine_date=args.quarantine_date,
            accept_comicinfo_drop=args.accept_comicinfo_drop,
            temp_dir=args.temp_dir, max_held_bytes=args.max_held_bytes,
            max_copy_bytes=args.max_copy_bytes, number_style=args.number_style,
            number_style_registry=args.number_style_registry, series_folder_root=args.series_folder_root,
            scoped=args.scoped, scopes=chosen.planned if chosen else args.scope,
            selection=chosen.as_dict() if chosen else None)
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
        print(json.dumps({"output": str(target), "plan_digest": doc["plan_digest"],
                          "counts": doc["counts"], "diagnostics": doc["diagnostics"]}))
    # A skipped folder was not planned at all, so the plan is not complete.
    return 1 if doc["counts"]["review"] or doc.get("skipped_scopes") or (chosen and chosen.omitted) else 0


if __name__ == "__main__":
    raise SystemExit(main())
