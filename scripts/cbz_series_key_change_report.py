"""Read-only report of which series folders change identity under #111's fix.

``series_key`` used to remove the ``uncensored``/``decensored`` marker
words *before* turning separators into spaces, so a marker glued to an
underscore (``Berserk_Uncensored``) survived the first pass. #111 moved the
removal after the separator rule. That changes the key of exactly those
names, and an index built under the old order has to be rebuilt rather than
topped up. This report says which names change, and which folders the new
rule now treats as one series, before anyone rebuilds anything.

Run directly or with ``python -m scripts.cbz_series_key_change_report``,
naming every root explicitly with ``--root``. Only directory names are read:
no archive is opened, no database is touched, nothing is written. JSON goes
to stdout.

Exit status: 0 when no key changes, 1 when some do (an observation, not an
error), 2 when the report could not be produced.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import unicodedata

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.cbz_routing import NON_SERIES_DIRECTORIES, series_key


# The rule as it stood before #111, kept verbatim so the report compares
# against what an existing index was actually built with. Not for any other
# use: it is the defect.
_LEGACY_MARKER_WORDS_RE = re.compile(r"\b(?:uncensored|decensored)\b", re.IGNORECASE)
_LEGACY_PUNCT_RE = re.compile(r"[^\w\s]|_")
_LEGACY_SPACE_RE = re.compile(r"\s+")


def legacy_series_key(name: str) -> str:
    """``series_key`` before #111: marker removal ran first."""
    name = unicodedata.normalize("NFC", name or "")
    name = _LEGACY_MARKER_WORDS_RE.sub("", name)
    name = _LEGACY_PUNCT_RE.sub(" ", name.lower())
    name = _LEGACY_SPACE_RE.sub(" ", name).strip()
    return unicodedata.normalize("NFC", name)


def _canonical_bytes(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def _directory_names(root: Path) -> list[str]:
    # A missing root or failed enumeration raises: it must never become a
    # successful empty scan that reports "nothing changes".
    return sorted((child.name for child in root.iterdir() if child.is_dir()),
                  key=lambda name: (name.casefold(), name))


def build_report(roots: list[Path]) -> dict:
    """Compare old and new keys for every immediate subdirectory of *roots*.

    * ``changed``: every folder whose key differs under the new rule.
    * ``merges``: new non-empty keys shared by folders whose old keys
      differed. These are series the old index kept apart and a rebuilt one
      will join, so they are what David reviews.
    * ``empty_keys``: folders whose new key is empty (``_decensored``). The
      series index excludes an empty identity rather than indexing it, so
      these are listed on their own and are never counted as a merge.
    * ``splits``: old keys whose folders now differ. Not expected from this
      fix; listed so that an unexpected one cannot pass unseen.
    * ``excluded``: folders the series index never treats as series
      (``NON_SERIES_DIRECTORIES``, e.g. Syncthing's ``.stfolder``). They are
      kept out of every key comparison above, as SeriesIndex keeps them out
      of the index, but listed so nothing is silently dropped.

    Each root's full, sorted list of observed names is part of the report,
    and so of its fingerprint. Without it, renaming a folder the fix does
    not affect would leave the report, and its digest, unchanged.
    """
    absolute = [Path(os.path.abspath(r)) for r in roots]
    if len(absolute) != len(set(absolute)):
        raise ValueError("--root names the same directory more than once")

    rows = []
    excluded = []
    root_rows = []
    for root in absolute:
        names = _directory_names(root)
        root_rows.append({"root": str(root), "directories": len(names),
                          "names": names})
        for name in names:
            if name.casefold() in NON_SERIES_DIRECTORIES:
                excluded.append({"root": str(root), "name": name})
                continue
            rows.append({"root": str(root), "name": name,
                         "legacy_key": legacy_series_key(name),
                         "key": series_key(name)})

    changed = [r for r in rows if r["legacy_key"] != r["key"]]

    by_new: dict[str, list[dict]] = {}
    by_old: dict[str, list[dict]] = {}
    for r in rows:
        if r["key"]:
            by_new.setdefault(r["key"], []).append(r)
        if r["legacy_key"]:
            by_old.setdefault(r["legacy_key"], []).append(r)
    empty_keys = [r for r in rows if not r["key"]]
    merges = [{"key": k, "members": members}
              for k, members in sorted(by_new.items())
              if len({m["legacy_key"] for m in members}) > 1]
    splits = [{"legacy_key": k, "members": members}
              for k, members in sorted(by_old.items())
              if len({m["key"] for m in members}) > 1]

    report = {
        "contract": "series-key-change-report-v1",
        "roots": root_rows,
        "counts": {"directories": len(rows), "changed": len(changed),
                   "merges": len(merges), "empty_keys": len(empty_keys),
                   "splits": len(splits), "excluded": len(excluded)},
        "changed": changed,
        "merges": merges,
        "empty_keys": empty_keys,
        "excluded": excluded,
        "splits": splits,
    }
    report["report_sha256"] = hashlib.sha256(_canonical_bytes(report)).hexdigest()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, action="append", required=True,
                        help="a library root whose series folders are compared; repeat per root")
    args = parser.parse_args(argv)
    try:
        report = build_report(args.root)
    except (OSError, ValueError) as exc:
        print(f"Report not produced: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False))
    return 0 if report["counts"]["changed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
