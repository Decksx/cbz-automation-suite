"""
cbz_library_organizer.py

Consolidated facade for series/directory organization.

Subcommands:
  merge-folders    -> cbz_folder_merger.py
  match-series     -> cbz_series_matcher.py
  find-uncensored  -> find_uncensored_dupes.py
  organize-all     -> cbz_series_matcher.py, then cbz_folder_merger.py
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

def run_script(script_name: str, extra_args: list[str]) -> int:
    script_path = SCRIPT_DIR / script_name
    if not script_path.exists():
        print(f"ERROR: required script not found: {script_path}", file=sys.stderr)
        return 2
    cmd = [sys.executable, str(script_path), *extra_args]
    print("")
    print("=" * 72)
    print("Running:", " ".join(f'"{c}"' if " " in c else c for c in cmd))
    print("=" * 72)
    return subprocess.run(cmd).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Library organization facade.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("merge-folders", help="Run cbz_folder_merger.py")
    p.add_argument("args", nargs=argparse.REMAINDER)

    p = sub.add_parser("match-series", help="Run cbz_series_matcher.py")
    p.add_argument("args", nargs=argparse.REMAINDER)

    p = sub.add_parser("find-uncensored", help="Run find_uncensored_dupes.py")
    p.add_argument("args", nargs=argparse.REMAINDER)

    p = sub.add_parser("organize-all", help="Run series matcher, then folder merger")
    p.add_argument("args", nargs=argparse.REMAINDER)

    args = parser.parse_args()
    passthrough = list(args.args or [])

    if args.command == "merge-folders":
        return run_script("cbz_folder_merger.py", passthrough)
    if args.command == "match-series":
        return run_script("cbz_series_matcher.py", passthrough)
    if args.command == "find-uncensored":
        return run_script("find_uncensored_dupes.py", passthrough)
    if args.command == "organize-all":
        rc = run_script("cbz_series_matcher.py", passthrough)
        if rc != 0:
            return rc
        return run_script("cbz_folder_merger.py", passthrough)

    parser.error("unknown command")
    return 2

if __name__ == "__main__":
    raise SystemExit(main())
