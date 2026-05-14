"""
cbz_metadata_tools.py

Consolidated facade for retroactive metadata tools.

Subcommands:
  number-tags -> cbz_number_tagger.py
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
    parser = argparse.ArgumentParser(description="CBZ metadata repair facade.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("number-tags", help="Run cbz_number_tagger.py")
    p.add_argument("args", nargs=argparse.REMAINDER)

    args = parser.parse_args()

    if args.command == "number-tags":
        return run_script("cbz_number_tagger.py", list(args.args or []))

    parser.error("unknown command")
    return 2

if __name__ == "__main__":
    raise SystemExit(main())
