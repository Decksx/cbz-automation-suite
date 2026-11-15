"""Read exported JSON artifacts and emit sanitized configuration-bound evidence."""
import argparse
import json
from pathlib import Path
import sys

from comic_automation.library.sync_config import bind_configuration


def main(argv=None):
    """Read only named files; stdout is the sole artifact destination.

    Exit 0 requires complete comparison and supported configuration evidence;
    2 is refusal/fallback/differences and 1 is output failure. No HTTP client.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--capture", required=True, type=Path)
    parser.add_argument("--expected", type=Path, help="configuration-bound baseline for verification")
    args = parser.parse_args(argv)
    try:
        def read(path):
            return json.loads(path.read_text(encoding="utf-8"))
        result = bind_configuration(read(args.report), read(args.capture),
                                    expected=read(args.expected) if args.expected else None)
    except (OSError, ValueError, TypeError):
        # Raw exports may contain credentials: no parser values or traces.
        try:
            print("configuration evidence refused", file=sys.stderr)
        except (OSError, ValueError):
            pass
        return 2
    try:
        print(json.dumps(result, sort_keys=True, indent=2, ensure_ascii=True))
    except (OSError, ValueError):
        return 1
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
