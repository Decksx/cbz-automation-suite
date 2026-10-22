"""Read-only SeriesIndex observations; no routing or activation (#98).

Run directly or with ``python -m scripts.cbz_series_ambiguity_report``.
Only the explicitly supplied routing file and root directory metadata are
read. JSON goes to stdout; no output file or library directory is created.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.cbz_routing import RoutingConfigError, SeriesIndex, parse


def _canonical_bytes(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def _directory_paths(root: Path) -> list[Path]:
    # Same input policy as SeriesIndex's default lister. A missing root or
    # failed enumeration raises; it must never become a successful empty scan.
    return sorted((child for child in root.iterdir() if child.is_dir()),
                  key=lambda child: (child.name.casefold(), child.name))


def build_report(routing_path: Path, scope: list[str]) -> dict:
    """Observe exactly the configured index roots, after scope validation.

    The directory lists supplied to SeriesIndex are also retained in the
    report, binding its fingerprint to non-colliding names as well. Each
    identical configured root spelling is enumerated once; alias labels still
    appear separately, and SeriesIndex retains its own lexical dedup policy.
    This is not an atomic snapshot or an archive-content measurement.
    """
    routing_path = Path(os.path.abspath(routing_path))
    config_bytes = routing_path.read_bytes()
    raw = json.loads(config_bytes)
    if not isinstance(raw, dict):
        raise RoutingConfigError("routing config must be a JSON object")
    cfg = parse(raw)
    keys = cfg.series_index_destinations or tuple(cfg.destinations)
    if len(keys) != len(set(keys)):
        raise RoutingConfigError("configured index destinations contain duplicates")
    if len(scope) != len(set(scope)) or set(scope) != set(keys):
        raise RoutingConfigError(
            "--scope must name every configured index destination exactly once: "
            + " ".join(keys))

    observed: dict[str, tuple[list[Path], str | None]] = {}
    roots = []
    scan_problems = []
    for key in keys:
        root = Path(cfg.destinations[key])
        row = {"dest_key": key, "root": str(root), "status": "not_scanned",
               "directories": []}
        if cfg.series_index_enabled:
            if str(root) not in observed:
                try:
                    observed[str(root)] = (_directory_paths(root), None)
                except OSError as exc:
                    # Discard a failed root's partial enumeration. Other roots
                    # can still provide diagnostics, but the gate stays blocked.
                    observed[str(root)] = ([], f"{type(exc).__name__}: {exc}")
            paths, error = observed[str(root)]
            if error is not None:
                row.update(status="failed", error=error)
                scan_problems.append(f"root scan failed: {key}: {root}: {error}")
            else:
                row.update(status="enumerated", directories=[str(p) for p in paths])
        roots.append(row)

    index = SeriesIndex.build(cfg, lister=lambda root: observed[str(root)][0])
    diagnostics = index.diagnostics()
    diagnostics = replace(
        diagnostics,
        scan_problems=diagnostics.scan_problems + tuple(scan_problems))
    report = {
        "schema_version": 1,
        "config": {"path": str(routing_path),
                   "sha256": hashlib.sha256(config_bytes).hexdigest()},
        "index_enabled": cfg.series_index_enabled,
        "priority": list(keys),
        "scope": roots,
        "counts": {
            "directory_observations": sum(len(row["directories"]) for row in roots),
            "canonical_keys": len(index),
            "collision_keys": len(diagnostics.collisions),
            "empty_key_paths": len(diagnostics.empty_keys),
            "excluded_paths": len(diagnostics.excluded),
            "scan_problems": len(diagnostics.scan_problems),
        },
        "diagnostics": diagnostics.to_dict(),
        "observation": {
            "atomic_filesystem_snapshot": False,
            "archive_contents_read": False,
            "physical_aliases_resolved": False,
            "reviewed_exceptions_evaluated": False,
        },
    }
    report["report_sha256"] = hashlib.sha256(_canonical_bytes(report)).hexdigest()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routing", type=Path, required=True,
                        help="explicit routing JSON; there is no default config")
    parser.add_argument("--scope", nargs="+", required=True, metavar="DEST",
                        help="confirm every configured index destination, once each")
    args = parser.parse_args(argv)
    try:
        report = build_report(args.routing, args.scope)
    except (OSError, ValueError, TypeError) as exc:
        print(f"Report not produced: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False))
    # 0 means a clear observation, never permission to activate the watcher.
    return 0 if report["diagnostics"]["clear"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
