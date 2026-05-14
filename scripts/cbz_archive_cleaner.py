"""
cbz_archive_cleaner.py

Consolidated facade for archive-level cleanup.

Subcommands:
  dedupe     -> cbz_deduplicator.py
  strip      -> strip_duplicates.py
  clean-all  -> strip_duplicates.py, then cbz_deduplicator.py
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
    parser = argparse.ArgumentParser(description="Archive-level CBZ cleanup facade.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("dedupe", help="Run cbz_deduplicator.py")
    p.add_argument("args", nargs=argparse.REMAINDER)

    p = sub.add_parser("strip", help="Run strip_duplicates.py")
    p.add_argument("args", nargs=argparse.REMAINDER)

    p = sub.add_parser("clean-all", help="Run strip_duplicates.py, then cbz_deduplicator.py")
    p.add_argument("args", nargs=argparse.REMAINDER)

    args = parser.parse_args()
    passthrough = list(args.args or [])

    if args.command == "dedupe":
        return run_script("cbz_deduplicator.py", passthrough)
    if args.command == "strip":
        return run_script("strip_duplicates.py", passthrough)
    if args.command == "clean-all":
        rc = run_script("strip_duplicates.py", passthrough)
        if rc != 0:
            return rc
        return run_script("cbz_deduplicator.py", passthrough)

    parser.error("unknown command")
    return 2

if __name__ == "__main__":
    raise SystemExit(main())
