"""
CBZ Gap Checker
Scans one or more series folders for missing chapter/issue numbers and
writes a consolidated CSV report.

Usage:
    python cbz_gap_checker.py                          # scans all configured SCAN_FOLDERS
    python cbz_gap_checker.py "C:/path/Series"         # scan a single folder directly

The script treats each immediate subdirectory of a SCAN_FOLDER as a series.
If a path passed on the command line contains .cbz files directly, it is
treated as a single series rather than a parent folder.
"""

import os
import re
import csv
import sys
from pathlib import Path
from datetime import datetime

# ─────────────────────────────────────────────
# CONFIGURATION — edit these as needed
# ─────────────────────────────────────────────
SCAN_FOLDERS = [
    r"\\tower\media\comics\Comix",
    #r"\\tower\media\comics\Manga",
]
OUTPUT_FOLDER = r"C:\git\ComicAutomation"

# Gaps are only reported when the jump between consecutive issue numbers
# exceeds this threshold. Set to 1 to report every single missing integer.
# Set higher (e.g. 2) to ignore half-issues or minor numbering quirks.
GAP_THRESHOLD = 1

# Series with fewer than this many issues are skipped (likely incomplete imports)
MIN_ISSUES_TO_REPORT = 2
# ─────────────────────────────────────────────


# ── Number extraction ─────────────────────────────────────────────────────────
_NUMBER_RE = re.compile(
    r'(?:'
    r'ch(?:ap(?:ter)?)?p?\.?\s*(\d[\d.]*)'   # ch / chap / chapter / chp + number
    r'|issue\s*(\d[\d.]*)'                    # issue + number
    r'|#\s*(\d[\d.]*)'                        # # + number
    r'|[^\d](\d{1,4}(?:\.\d+)?)\s*$'         # bare number at end of stem (≤4 digits)
    r')',
    re.IGNORECASE
)


def extract_number(stem: str) -> float | None:
    """Return the chapter/issue number from a filename stem, or None."""
    m = _NUMBER_RE.search(stem)
    if m:
        val = next(g for g in m.groups() if g is not None)
        return float(val)
    return None


def format_number(n: float) -> str:
    """Display as integer if whole, otherwise as decimal (e.g. 12 or 12.5)."""
    return str(int(n)) if n == int(n) else str(n)


# ── Gap detection ─────────────────────────────────────────────────────────────
def find_gaps(numbers: list[float]) -> list[str]:
    """
    Given a sorted list of issue numbers, return a list of gap descriptions.
    e.g. [1, 2, 4, 7] -> ["3", "5-6"]
    If the series starts above 1, issues 1 through (first-1) are also flagged.
    e.g. [10, 11, 12] -> ["1-9"]
    """
    if not numbers:
        return []

    gaps = []

    # Flag missing issues before the first one found (if series doesn't start at 1)
    first = numbers[0]
    if first > 1:
        missing_before = list(range(1, int(first)))
        if len(missing_before) == 1:
            gaps.append(format_number(missing_before[0]))
        else:
            gaps.append(f"1-{format_number(missing_before[-1])}")

    if len(numbers) < 2:
        return gaps

    for i in range(len(numbers) - 1):
        lo = numbers[i]
        hi = numbers[i + 1]
        diff = hi - lo

        if diff <= GAP_THRESHOLD:
            continue  # no gap (or within threshold for half-issues)

        # Build list of missing integers between lo and hi
        missing = []
        n = lo + 1
        while n < hi:
            missing.append(n)
            n += 1

        if not missing:
            continue

        # Format as range or individual
        if len(missing) == 1:
            gaps.append(format_number(missing[0]))
        else:
            gaps.append(f"{format_number(missing[0])}-{format_number(missing[-1])}")

    return gaps


# ── Series scanning ───────────────────────────────────────────────────────────
def scan_series(series_dir: Path) -> dict | None:
    """
    Scan a single series directory. Returns a result dict or None if
    the series has no numbered issues or too few issues to report.
    """
    cbz_files = sorted(series_dir.glob("*.cbz"))
    if not cbz_files:
        return None

    numbered = {}  # number -> filename
    unnumbered = []

    for f in cbz_files:
        num = extract_number(f.stem)
        if num is not None:
            # On duplicate numbers keep the first seen (alphabetical sort)
            if num not in numbered:
                numbered[num] = f.name
        else:
            unnumbered.append(f.name)

    if len(numbered) < MIN_ISSUES_TO_REPORT:
        return None

    sorted_nums = sorted(numbered.keys())
    gaps = find_gaps(sorted_nums)

    return {
        "series":        series_dir.name,
        "path":          str(series_dir),
        "total_found":   len(numbered),
        "unnumbered":    len(unnumbered),
        "first_issue":   format_number(sorted_nums[0]),
        "last_issue":    format_number(sorted_nums[-1]),
        "gap_count":     len(gaps),
        "missing":       ", ".join(gaps) if gaps else "",
        "has_gaps":      bool(gaps),
    }


def scan_folder(folder: Path) -> list[dict]:
    """Scan all immediate subdirectories of folder as individual series."""
    results = []
    if not folder.exists():
        print(f"  WARNING: Folder not found, skipping: {folder}")
        return results

    subdirs = sorted(d for d in folder.iterdir() if d.is_dir())
    print(f"  Scanning {len(subdirs)} series in: {folder}")

    for series_dir in subdirs:
        result = scan_series(series_dir)
        if result:
            results.append(result)

    return results


# ── Report writing ────────────────────────────────────────────────────────────
def write_report(results: list[dict], output_path: Path) -> None:
    """Write results to CSV, sorted by gap count descending then series name."""
    # Separate series with and without gaps
    with_gaps    = [r for r in results if r["has_gaps"]]
    without_gaps = [r for r in results if not r["has_gaps"]]

    with_gaps.sort(key=lambda r: (-r["gap_count"], r["series"].lower()))
    without_gaps.sort(key=lambda r: r["series"].lower())

    all_rows = with_gaps + without_gaps

    fieldnames = [
        "series", "first_issue", "last_issue", "total_found",
        "gap_count", "missing", "unnumbered", "path",
    ]

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\n  Report written: {output_path}")
    print(f"  Series with gaps  : {len(with_gaps)}")
    print(f"  Series complete   : {len(without_gaps)}")
    print(f"  Total series      : {len(all_rows)}")


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    timestamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = Path(OUTPUT_FOLDER) / f"cbz_gaps_{timestamp}.csv"
    os.makedirs(OUTPUT_FOLDER, exist_ok=True)

    all_results = []

    # If paths passed on command line, use those instead of SCAN_FOLDERS
    targets = sys.argv[1:] if len(sys.argv) > 1 else SCAN_FOLDERS

    for target in targets:
        target_path = Path(target)

        # If the target itself contains .cbz files, treat it as a single series.
        # Otherwise treat it as a parent folder containing series subdirectories.
        # A folder with only subdirs (no direct .cbz files) is always a parent folder.
        has_cbz  = any(target_path.glob("*.cbz"))
        has_dirs = any(d for d in target_path.iterdir() if d.is_dir())

        if has_cbz and not has_dirs:
            # Leaf directory — definitely a single series
            print(f"  Scanning single series: {target_path.name}")
            result = scan_series(target_path)
            if result:
                all_results.append(result)
        elif has_cbz and has_dirs:
            # Mixed — treat as single series (cbz files at root level)
            print(f"  Scanning single series (mixed): {target_path.name}")
            result = scan_series(target_path)
            if result:
                all_results.append(result)
            # Also scan subdirs in case they are sub-series
            all_results.extend(scan_folder(target_path))
        else:
            # No .cbz at root — definitely a parent folder
            all_results.extend(scan_folder(target_path))

    if not all_results:
        print("  No series found with enough numbered issues to report.")
        return

    write_report(all_results, output_path)

    # Print a quick console summary of the worst offenders
    with_gaps = [r for r in all_results if r["has_gaps"]]
    if with_gaps:
        print("\n  Top series by missing issue count:")
        for r in sorted(with_gaps, key=lambda x: -x["gap_count"])[:10]:
            print(f"    {r['series']:40s}  missing: {r['missing']}")


if __name__ == "__main__":
    print("=" * 60)
    print("CBZ Gap Checker")
    print("=" * 60)
    main()
    print("=" * 60)
    print("Done.")
    print("=" * 60)
