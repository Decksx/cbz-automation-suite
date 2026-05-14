# Overview

The CBZ Automation Suite is a collection of Python scripts for monitoring, cleaning, tagging, and routing `.cbz` comic book archives on Windows. Designed to run against a network share or a local drive with minimal manual intervention.

---

## Design Principles

- **Hands-off pipeline** — files dropped into a watch folder are processed and routed automatically.
- **Recursive by default** — all batch tools descend into subdirectories automatically; opt out with `--no-recursive` where supported.
- **Parallel by default** — all batch tools use `min(8, cpu_count)` worker threads automatically; opt down with `--workers 1` for serial behaviour.
- **Resumable** — batch operations track progress in an append-only JSONL file.
- **Non-destructive** — files are renamed in place, never silently deleted; on any collision the larger file wins.
- **Windows-aware** — explicit handling for `FileExistsError` on rename, UNC paths, and watchdog event filtering.
- **Dry-run everywhere** — all batch tools support `--dry-run` for safe previewing.
- **Shared core module** — `scripts/cbz_core.py` owns shared normalization, parsing, and ComicInfo logic. Watcher and batch tools import from the shared module instead of maintaining duplicated regex/helper copies.
- **External config** — routing is driven by `routing.json`, not hardcoded in the script.

---

## Tools at a Glance

| Script | Recursive? | Workers? | Purpose | Doc |
|--------|-----------|----------|---------|-----|
| `scripts/cbz_core.py` | — | — | Shared normalization/parsing/ComicInfo layer used by watcher and batch tools | [cbz_core.md](cbz_core.md) |
| `scripts/cbz_watcher.py` | Always (watchdog) | — | Live watcher — monitors Incoming folder, cleans, tags, and routes files via `routing.json` | [cbz_watcher.md](cbz_watcher.md) |
| `scripts/cbz_sanitizer.py` | Always (`rglob`) | **Yes** | Batch sanitizer — in-place clean/tag with `--sort`, `--resume`, `--dry-run`, `--workers`, `--rules` | [cbz_sanitizer.md](cbz_sanitizer.md) |
| `scripts/cbz_library_maintenance.py archive-clean` | Configurable | **Yes** | Removes duplicate archives, strips duplicate filename tokens, and packs loose image folders | [other_tools.md](other_tools.md#cbz_library_maintenancepy) |
| `scripts/cbz_library_maintenance.py organize-series` | Configurable | **Yes** | Merges split chapter folders, matches near-duplicate series, repairs merged ComicInfo, fixes likely compilation ranges, and moves review groups to `_Check/` | [other_tools.md](other_tools.md#cbz_library_maintenancepy) |
| `scripts/cbz_library_maintenance.py metadata` | Always (`rglob`) | **Yes** | Retroactively repairs `<Title>`, `<Series>`, `<Number>`, and `<Volume>` tags from filenames | [other_tools.md](other_tools.md#cbz_library_maintenancepy) |
| `scripts/cbz_compilation_resolver.py` | **Yes — default** | **Yes** | Resolves compilation vs individual overlaps; rewrites with best pages | [other_tools.md](other_tools.md#cbz_compilation_resolverpy) |
| `scripts/cbz_gap_checker.py` | **Yes — default** | **Yes** | Scans library, outputs timestamped CSV of missing chapter numbers to `Logs/` | [other_tools.md](other_tools.md#cbz_gap_checkerpy) |
| `config/routing.example.json` | — | — | Template for `routing.json` — copy to `C:\\git\\ComicAutomation\routing.json` and edit | [cbz_watcher.md](cbz_watcher.md#routing) |
| `config/run_watcher.bat` | — | — | Double-click launcher — installs watchdog and starts the watcher | — |
| `config/CBZWatcher_Task.xml` | — | — | Windows Task Scheduler import — auto-starts watcher on login | — |

---

## Shared Core

`scripts/cbz_core.py` centralizes logic that previously lived independently in multiple scripts:

- `sanitize()`
- `clean_filename()`
- `clean_directory_name()`
- `parse_comic_name()`
- `ParsedComicName`
- `update_comicinfo_xml()`
- chapter/volume extraction
- mixed English/original-title shortening
- root-aware series inference

The watcher has been migrated to call `parse_comic_name()` and `update_comicinfo_xml()` directly, eliminating duplicated filename and ComicInfo title-selection logic.

---

## Running Scripts

Run from the repo root:

```powershell
cd "C:\Users\David.Johnson\ComicAutomation"
python scripts\cbz_sanitizer.py --dry-run
python scripts\cbz_watcher.py
```

---

## Repository File Structure

```text
cbz-automation-suite/
├── scripts/
│   ├── cbz_watcher.py              # Live watcher (main tool)
│   ├── cbz_sanitizer.py            # Canonical shared-function reference
│   ├── cbz_library_maintenance.py
│   ├── cbz_compilation_resolver.py
│   ├── cbz_gap_checker.py
│   └── cbz_core.py
├── config/
│   ├── routing.example.json        # Template — copy to C:\git\ComicAutomation\routing.json
│   ├── run_watcher.bat
│   └── CBZWatcher_Task.xml
├── docs/
│   ├── overview.md                 <- this file
│   ├── cbz_sanitizer.md
│   ├── cbz_watcher.md
│   ├── other_tools.md
│   ├── shared_pipeline.md
│   ├── engineering_decisions.md
│   └── CBZ_Automation_Suite_Documentation.docx
├── Logs/                           # folder committed; contents gitignored
│   └── .gitkeep
├── progress_tracking/              # folder committed; contents gitignored
│   ├── cbz_sanitizer_progress.json
│   ├── Newest1st_cbz_sanitizer_progress.json
│   └── Oldestfirstcbz_sanitizer_progress.json
├── README.md
└── requirements.txt
```

> **Runtime files** — `routing.json` lives at `C:\\git\\ComicAutomation\` and is excluded from git. Logs live in `Logs\` — the folder is committed (via `Logs\.gitkeep`) but the log contents are gitignored. Progress JSONs live in `progress_tracking\` — the folder is committed but the JSON contents are gitignored.

---

## Logs

All tools write rotating logs (max 5 MB, 3 backups) to `C:\git\ComicAutomation\Logs\`. The `Logs\` folder is committed to git (via `.gitkeep`) so it always exists on a fresh clone — no manual creation needed. Log file contents are gitignored. Configure `LOG_FILE` at the top of each script.

| Log file | Script |
|----------|--------|
| `Logs\cbz_watcher.log` | cbz_watcher.py |
| `Logs\cbz_sanitizer.log` | cbz_sanitizer.py |
| `Logs\cbz_library_maintenance.log` | cbz_library_maintenance.py |
| `Logs\cbz_compilation_resolver.log` | cbz_compilation_resolver.py |
| `Logs\cbz_gaps_YYYYMMDD_HHMMSS.csv` | cbz_gap_checker.py (CSV report, not a log) |
