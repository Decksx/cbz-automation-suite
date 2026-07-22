"""
cbz_gap_checker.py — CBZ Gap Checker.

Scans each series directory in parallel and writes a timestamped CSV report of
missing chapter numbers. Results are aggregated after worker completion.
"""

from __future__ import annotations

import os
import re
import csv
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
SCAN_FOLDERS = [
    r"\\tower\media\comics\Comix",
]
REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_FOLDER = REPO_ROOT / "Logs"
GAP_THRESHOLD = 1
MIN_ISSUES_TO_REPORT = 2
DEFAULT_WORKERS = min(8, os.cpu_count() or 4)


class ProgressReporter:
    """Emit machine-readable progress lines for GUI launchers."""

    def __init__(self, total: int, label: str = "series") -> None:
        self.total = max(0, total)
        self.label = label
        self.current = 0
        self.enabled = os.environ.get("CBZ_PROGRESS") == "1"
        self._lock = threading.Lock()
        self.emit(0)

    def step(self, amount: int = 1) -> None:
        with self._lock:
            self.current = min(self.total, self.current + amount)
            self.emit(self.current)

    def emit(self, current: int) -> None:
        if not self.enabled:
            return
        percent = 100 if self.total == 0 else int((current / self.total) * 100)
        print(f"CBZ_PROGRESS {current}/{self.total} {percent}% {self.label}", flush=True)

# ─────────────────────────────────────────────
# NUMBER EXTRACTION
# ─────────────────────────────────────────────
_NUMBER_RE = re.compile(
    r'(?:'
    r'ch(?:ap(?:ter)?)?p?\.?\s*(\d[\d.]*)'
    r'|issue\s*(\d[\d.]*)'
    r'|#\s*(\d[\d.]*)'
    r'|[^\d](\d{1,4}(?:\.\d+)?)\s*$'
    r')',
    re.IGNORECASE
)

def extract_number(stem: str) -> float | None:
    m = _NUMBER_RE.search(stem)
    if m:
        val = next(g for g in m.groups() if g is not None)
        return float(val)
    return None

def format_number(n: float) -> str:
    return str(int(n)) if n == int(n) else str(n)


# ─────────────────────────────────────────────
# GAP DETECTION
# ─────────────────────────────────────────────
def find_gaps(numbers: list[float]) -> list[str]:
    if not numbers:
        return []
    gaps = []
    first = numbers[0]
    if first > 1:
        missing_before = list(range(1, int(first)))
        if missing_before:
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
            continue
        missing = []
        n = lo + 1
        while n < hi:
            missing.append(n)
            n += 1
        if not missing:
            continue
        if len(missing) == 1:
            gaps.append(format_number(missing[0]))
        else:
            gaps.append(f"{format_number(missing[0])}-{format_number(missing[-1])}")
    return gaps


# ─────────────────────────────────────────────
# SERIES SCANNING  (single series — worker unit)
# ─────────────────────────────────────────────
def scan_series(series_dir: Path) -> dict | None:
    """Scan a single series directory. Safe to call from threads."""
    cbz_files = sorted(series_dir.glob("*.cbz"))
    if not cbz_files:
        return None

    numbered = {}
    unnumbered = []
    for f in cbz_files:
        num = extract_number(f.stem)
        if num is not None:
            if num not in numbered:
                numbered[num] = f.name
        else:
            unnumbered.append(f.name)

    if len(numbered) < MIN_ISSUES_TO_REPORT:
        return None

    sorted_nums = sorted(numbered.keys())
    gaps = find_gaps(sorted_nums)

    return {
        "series":      series_dir.name,
        "path":        str(series_dir),
        "total_found": len(numbered),
        "unnumbered":  len(unnumbered),
        "first_issue": format_number(sorted_nums[0]),
        "last_issue":  format_number(sorted_nums[-1]),
        "gap_count":   len(gaps),
        "missing":     ", ".join(gaps) if gaps else "",
        "has_gaps":    bool(gaps),
    }


# ─────────────────────────────────────────────
# FOLDER COLLECTION  (unchanged recursive walk)
# ─────────────────────────────────────────────
def _collect_series_dirs(folder: Path) -> list[Path]:
    """
    Recursively collect all series-level directories (dirs that contain .cbz
    files directly).  Returns a flat list suitable for parallel scanning.
    """
    result: list[Path] = []
    if not folder.exists():
        print(f"  WARNING: Folder not found, skipping: {folder}")
        return result
    subdirs = sorted(d for d in folder.iterdir() if d.is_dir())
    for series_dir in subdirs:
        has_cbz = any(series_dir.glob("*.cbz"))
        if has_cbz:
            result.append(series_dir)
        else:
            result.extend(_collect_series_dirs(series_dir))
    return result


# ─────────────────────────────────────────────
# PARALLEL SCAN
# ─────────────────────────────────────────────
def scan_folder_parallel(
    folder: Path,
    workers: int,
    progress: ProgressReporter | None = None,
    series_dirs: list[Path] | None = None,
) -> list[dict]:
    """Collect all series dirs then scan them in parallel."""
    series_dirs = series_dirs if series_dirs is not None else _collect_series_dirs(folder)
    if not series_dirs:
        return []

    print(f"  Scanning {len(series_dirs)} series in: {folder}  ({workers} worker(s))")

    results: list[dict] = []

    if workers == 1:
        for sd in series_dirs:
            r = scan_series(sd)
            if r:
                results.append(r)
            if progress:
                progress.step()
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_dir = {executor.submit(scan_series, sd): sd for sd in series_dirs}
            for future in as_completed(future_to_dir):
                try:
                    r = future.result()
                    if r:
                        results.append(r)
                except Exception as e:
                    sd = future_to_dir[future]
                    print(f"  ERROR scanning '{sd.name}': {e}")
                finally:
                    if progress:
                        progress.step()

    return results


# ─────────────────────────────────────────────
# REPORT WRITING
# ─────────────────────────────────────────────
def write_report(results: list[dict], output_path: Path) -> None:
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


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
def main():
    timestamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = Path(OUTPUT_FOLDER) / f"cbz_gaps_{timestamp}.csv"
    os.makedirs(OUTPUT_FOLDER, exist_ok=True)

    raw_args = sys.argv[1:]
    workers  = DEFAULT_WORKERS
    for i, arg in enumerate(raw_args):
        if arg.startswith("--workers="):
            try:
                workers = max(1, int(arg.split("=", 1)[1]))
            except ValueError:
                pass
        elif arg == "--workers" and i + 1 < len(raw_args):
            try:
                workers = max(1, int(raw_args[i + 1]))
            except ValueError:
                pass

    skip_indices = set()
    for i, arg in enumerate(raw_args):
        if arg == "--workers" and i + 1 < len(raw_args):
            skip_indices.add(i + 1)

    targets = [
        a for i, a in enumerate(raw_args)
        if not a.startswith("--") and i not in skip_indices
    ] or SCAN_FOLDERS

    print("=" * 60)
    print(f"CBZ Gap Checker  (workers={workers})")
    print("=" * 60)

    all_results: list[dict] = []
    scan_jobs: list[tuple[str, Path, list[Path]]] = []

    for target in targets:
        target_path = Path(target)
        if not target_path.exists():
            print(f"  WARNING: Folder not found, skipping: {target_path}")
            continue
        has_cbz  = any(target_path.glob("*.cbz"))
        has_dirs = any(d for d in target_path.iterdir() if d.is_dir())

        if has_cbz and not has_dirs:
            scan_jobs.append(("single", target_path, [target_path]))
        elif has_cbz and has_dirs:
            scan_jobs.append(("mixed-single", target_path, [target_path]))
            scan_jobs.append(("folder", target_path, _collect_series_dirs(target_path)))
        else:
            scan_jobs.append(("folder", target_path, _collect_series_dirs(target_path)))

    progress = ProgressReporter(sum(len(series_dirs) for _, _, series_dirs in scan_jobs), "series")

    for kind, target_path, series_dirs in scan_jobs:
        if kind == "single":
            print(f"  Scanning single series: {target_path.name}")
            r = scan_series(target_path)
            if r:
                all_results.append(r)
            progress.step()
        elif kind == "mixed-single":
            print(f"  Scanning single series (mixed): {target_path.name}")
            r = scan_series(target_path)
            if r:
                all_results.append(r)
            progress.step()
        else:
            all_results.extend(scan_folder_parallel(target_path, workers, progress, series_dirs))

    if not all_results:
        print("  No series found with enough numbered issues to report.")
        return

    write_report(all_results, output_path)

    with_gaps = [r for r in all_results if r["has_gaps"]]
    if with_gaps:
        print("\n  Top series by missing issue count:")
        for r in sorted(with_gaps, key=lambda x: -x["gap_count"])[:10]:
            print(f"    {r['series']:40s}  missing: {r['missing']}")

    print("=" * 60)
    print("Done.")
    print("=" * 60)


if __name__ == "__main__":
    main()
