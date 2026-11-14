"""Read-only estimates, content observations and logical sync previews.

JSON goes to stdout. Throttled progress goes to stderr. The manifest and
generated observation are operator-supplied artifacts outside the library.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PureWindowsPath
import sys
import time

from comic_automation.library.sync_inventory import ProgressError, RootPair, observe_pairs
from comic_automation.library.sync_preview import build_preview


def read_pairs(path: Path) -> tuple[RootPair, ...]:
    """Load the small declarative root-pair manifest, anchoring relative paths.

    This schema describes filesystem scope. Raw Syncthing configuration and
    credentials are rejected as unknown fields and never copied to output.
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(document, dict) or set(document) != {"contract", "pairs"}
            or document["contract"] != "sync-root-pairs-v1" or not isinstance(document["pairs"], list)):
        raise ValueError("expected sync-root-pairs-v1 with a pairs array")
    pairs = []
    for item in document["pairs"]:
        if (not isinstance(item, dict) or not {"folder_id", "tower", "target"} <= set(item)
                or set(item) - {"folder_id", "tower", "target", "exclude_prefixes"}):
            raise ValueError("pair requires folder_id, tower, target and optional exclude_prefixes")
        if not all(isinstance(item[key], str) and item[key] for key in ("folder_id", "tower", "target")):
            raise ValueError("folder_id and paths must be non-empty strings")
        prefixes = item.get("exclude_prefixes", [])
        if not isinstance(prefixes, list) or not all(isinstance(value, str) for value in prefixes):
            raise ValueError("exclude_prefixes must be a string array")
        base = path.resolve().parent
        if any((PureWindowsPath(item[key]).drive and not PureWindowsPath(item[key]).is_absolute())
               or (PureWindowsPath(item[key]).root and not PureWindowsPath(item[key]).drive)
               for key in ("tower", "target")):
            raise ValueError("root/drive-relative paths are ambiguous; use absolute or manifest-relative paths")
        pairs.append(RootPair(item["folder_id"], base / item["tower"], base / item["target"], tuple(prefixes)))
    return tuple(pairs)


def _renderer(stream):
    previous = float("-inf")
    def render(event):
        nonlocal previous
        now = time.monotonic()
        if event["phase"] in {"hash_start", "hash_read", "hash_result", "inventory_read", "recheck_read"} and now - previous < 1:
            return
        previous = now
        print(json.dumps(event, ensure_ascii=True, sort_keys=True), file=stream, flush=True)
    return render


def _error(message):
    try:
        print(message, file=sys.stderr)
    except (OSError, ValueError):
        pass


def main(argv=None) -> int:
    """Estimate, observe or classify declared roots without sync controls.

    Exit 0 means complete filesystem observation/classification, not Syncthing
    readiness. Exit 2 means invalid input or incomplete coverage; 1 means a
    progress/output consumer failed.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("estimate", "observe", "preview"))
    parser.add_argument("--pairs", required=True, type=Path, help="sync-root-pairs-v1 JSON manifest")
    parser.add_argument("--quiet", action="store_true", help="suppress progress on stderr")
    args = parser.parse_args(argv)
    try:
        pairs = read_pairs(args.pairs)
        report = observe_pairs(pairs, hash_content=args.mode != "estimate",
                               progress=None if args.quiet else _renderer(sys.stderr))
        if args.mode == "preview":
            report = build_preview(report)
    except ProgressError:
        _error("progress output failed")
        return 1
    except (OSError, ValueError) as error:
        _error(f"observation refused: {type(error).__name__}: {error}")
        return 2
    try:
        print(json.dumps(report, sort_keys=True, indent=2, ensure_ascii=True))
    except (OSError, ValueError):
        _error("observation output failed")
        return 1
    return 0 if report["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
