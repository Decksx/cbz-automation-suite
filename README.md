# CBZ Documentation Update Package

## Apply automatically

📖 **[Full documentation in /docs](docs/overview.md)**

---

## Repository Structure

```
cbz-automation-suite/
├── scripts/
│   ├── cbz_watcher.py              # Live watcher — main day-to-day tool
│   ├── cbz_sanitizer.py            # Batch sanitizer — canonical shared-function reference
│   ├── cbz_folder_merger.py        # Merge colliding series folders
│   ├── cbz_compilation_resolver.py # Resolve compilation vs individual chapter overlaps
│   ├── cbz_deduplicator.py         # Remove duplicate cbz/cbr files; pack loose image folders
│   ├── cbz_number_tagger.py        # Retroactively set <Number>/<Volume> tags
│   ├── cbz_series_matcher.py       # Detect and merge near-duplicate series folders
│   ├── cbz_gap_checker.py          # Report missing chapter numbers per series
│   ├── strip_duplicates.py         # Remove duplicate number tokens from filenames
│   └── find_uncensored_dupes.py    # Quarantine censored/uncensored duplicate folder pairs
├── config/
│   ├── run_watcher.bat             # Double-click launcher
│   └── CBZWatcher_Task.xml         # Windows Task Scheduler import
├── docs/
│   ├── overview.md
│   ├── cbz_sanitizer.md
│   ├── cbz_watcher.md
│   ├── other_tools.md
│   ├── shared_pipeline.md
│   ├── engineering_decisions.md
│   └── CBZ_Automation_Suite_Documentation.docx
├── Logs/                           # committed folder; contents gitignored
│   └── .gitkeep
├── README.md
└── requirements.txt
```

---

## Tools

| Script | Recursive? | Workers? | Purpose |
|--------|-----------|----------|---------|
| `cbz_watcher.py` | Always | — | Live watcher — monitors an Incoming folder, cleans filenames, injects `ComicInfo.xml` metadata, and routes files to the correct destination |
| `cbz_sanitizer.py` | Always | **Yes** | Batch sanitizer — walks a library folder and applies the full cleaning/tagging pipeline in-place; supports `--sort`, `--restart`, `--dry-run`, `--workers`, and `--rules` |
| `cbz_folder_merger.py` | Single-level (by design) | **Yes** | Merges sibling directories whose cleaned names collide; two-phase ComicInfo update; interactive path prompt; supports UNC and local drives |
| `cbz_compilation_resolver.py` | **Yes — default** | **Yes** | Detects compilation/individual chapter overlaps; performs page-by-page quality comparison and rewrites compilations with the best pages |
| `cbz_deduplicator.py` | **Yes — default** | **Yes** | Finds and removes duplicate `.cbz`/`.cbr` files; packs loose image folders into archives. Use `--no-recursive` for single-level |
| `cbz_number_tagger.py` | Always | — | Sets `<Number>` and `<Volume>` tags in `ComicInfo.xml` from the filename — retroactive library tool |
| `cbz_series_matcher.py` | **Yes — default** | **Yes** | Finds near-duplicate series folder names at every nesting level and auto-merges above a configurable similarity threshold |
| `cbz_gap_checker.py` | **Yes — default** | **Yes** | Scans library folders and writes a timestamped CSV report of missing chapter numbers per series |
| `strip_duplicates.py` | **Yes — default** | **Yes** | Removes duplicate number tokens and fixes oddly spaced punctuation in filenames; also importable as a library. Use `--no-recursive` for single-level |
| `find_uncensored_dupes.py` | Single-level (by design) | — | Scans a library for folder pairs that are censored/uncensored duplicates and moves them to `_Check/` for manual review |

---

## Requirements

- Python 3.11+
- [`watchdog`](https://pypi.org/project/watchdog/) >= 3.0.0 — required by `cbz_watcher.py` **only**; all other scripts use the standard library exclusively

```powershell
pip install watchdog
# or double-click config\run_watcher.bat — it installs watchdog and starts the watcher automatically
```

---

## Quick Start

All scripts live in `scripts/`. Run them from the repo root:

```powershell
cd C:\git\ComicAutomation
```

### Live Watcher

Edit the constants at the top of `scripts\cbz_watcher.py`:

```python
WATCH_FOLDER  = r"C:\Comics\Incoming"
LOG_FILE      = r"C:\git\ComicAutomation\Logs\cbz_watcher.log"
ROUTING_FILE  = r"C:\git\ComicAutomation\routing.json"
```

Copy `config\routing.example.json` to `C:\git\ComicAutomation\routing.json` and set your destinations and rules:

```json
{
  "destinations": {
    "comix": "\\\\tower\\media\\comics\\Comix",
    "manga": "\\\\tower\\media\\comics\\Manga"
  },
  "default": "comix",
  "rules": [
    { "match": "source", "pattern": "MangaDex (EN)", "dest": "manga" }
  ]
}
```

```powershell
python scripts\cbz_watcher.py
# or double-click config\run_watcher.bat
# or import config\CBZWatcher_Task.xml into Task Scheduler for auto-start on login
```

### Batch Sanitize

```powershell
python scripts\cbz_sanitizer.py                               # scan SCAN_FOLDER, newest dirs first
python scripts\cbz_sanitizer.py --sort=oldest                 # oldest-modified dirs first
python scripts\cbz_sanitizer.py --sort=alpha                  # alphabetical
python scripts\cbz_sanitizer.py --restart                     # clear progress, start fresh
python scripts\cbz_sanitizer.py --dry-run                     # preview only, no changes written
python scripts\cbz_sanitizer.py --workers 4                   # use 4 parallel workers
python scripts\cbz_sanitizer.py --rules=leading_nums,trailing_junk  # run specific rules only
python scripts\cbz_sanitizer.py --rules=comicinfo             # only update ComicInfo.xml
```

### Uncensored Dupe Finder

```powershell
python scripts\find_uncensored_dupes.py                       # dry run — shows pairs, no changes
python scripts\find_uncensored_dupes.py --live                # move both folders to _Check
python scripts\find_uncensored_dupes.py --live --move uncensored  # move only the uncensored one
python scripts\find_uncensored_dupes.py --library "\\tower\media\comics\Comix"
```

### Other Tools

```powershell
cd "C:\Users\David.Johnson\ComicAutomation"
powershell -ExecutionPolicy Bypass -File "<unzipped-package>\tools\apply_doc_updates.ps1" -RepoRoot "."
```

The script creates a timestamped backup of your current `docs/` folder before overwriting files.

## Or copy manually

Copy the files in `docs/` into your repository's `docs/` folder.

## Review and commit

```powershell
python scripts\cbz_sanitizer.py --workers 8
python scripts\cbz_deduplicator.py --workers 4
python scripts\cbz_gap_checker.py --workers 8
python scripts\cbz_series_matcher.py --workers 4
python scripts\cbz_compilation_resolver.py --workers 8
python scripts\cbz_folder_merger.py --workers 8
python scripts\strip_duplicates.py "C:\Comics" --workers 4
```

The default is `min(8, cpu_count)`. Pass `--workers 1` to restore fully serial behaviour. See [docs/engineering_decisions.md](docs/engineering_decisions.md) for the design rationale.

---

## How It Works

### Filename & Metadata Cleaning

All tools share a common `sanitize()` pipeline (defined in `cbz_sanitizer.py`) that strips non-Latin/non-Greek/non-emoji characters (covering CJK, Arabic, Cyrillic, full-width forms, etc.), bracketed group and publisher tags, website patterns, scanner/scanlation credits, trailing G-code suffixes, and normalises whitespace. See [docs/shared_pipeline.md](docs/shared_pipeline.md) for the full step-by-step breakdown.

`ComicInfo.xml` is created or updated with `<Title>`, `<Series>`, `<Number>`, and `<Volume>` tags derived from the filename and directory name.

The sanitizer also supports `--rules=<list>` to run only specific cleaning rules — useful for targeted passes:

| Rule | What it does |
|------|-------------|
| `brackets` | Remove `[bracketed]` / `(parenthesised)` blocks |
| `comicinfo` | Update ComicInfo.xml metadata only |
| `leading_nums` | Strip leading numeric prefixes (`1 - `, `3761755 v1 `) |
| `non_latin` | Remove non-Latin characters |
| `normalize_stem` | Rewrite generic chapter stems |
| `number_tokens` | Normalise `Vol.01` → `Vol.1` etc. |
| `scan_groups` | Strip scanlation group names |
| `trailing_junk` | Strip trailing hyphens/dashes/underscores |
| `url` | Strip URLs and domain-like tokens |

### Routing (watcher only)

Routing is driven by `routing.json` (path set by `ROUTING_FILE`). Rules are evaluated top-to-bottom; first match wins. Unmatched directories fall back to the `default` destination.

```
WATCH_FOLDER/
├── MangaDex (EN)/    →  \\tower\media\comics\Manga   (rule match)
└── anything-else/    →  \\tower\media\comics\Comix   (default fallback)
```

### Conflict Resolution

On any filename collision during a merge or move, **the larger file is always kept**.

---

## Notes

- **Windows only** — path handling, UNC share access, and rename behaviour are Windows-specific throughout.
- `scripts\cbz_sanitizer.py` is the **canonical reference** for all shared functions. Other tools sync from it.
- `scripts\strip_duplicates.py` is also importable as a library: `from strip_duplicates import clean`.
- Progress files (`*_progress.json`) are machine-local and excluded from git via `.gitignore`.
- All log files are written to `Logs\` — the folder is committed (via `Logs\.gitkeep`) so it always exists on a fresh clone. Log contents are gitignored.
- All batch tools are **recursive by default**. Use `--no-recursive` on `cbz_deduplicator.py` and `strip_duplicates.py` to limit to a single directory level.
- All batch tools default to `min(8, cpu_count)` workers. Pass `--workers 1` for fully serial behaviour.

---

## Logs

All logs go to `C:\git\ComicAutomation\Logs\`. The folder is committed to git so it always exists on a fresh clone — no manual creation needed.

| Log file | Script |
|----------|--------|
| `Logs\cbz_watcher.log` | cbz_watcher.py |
| `Logs\cbz_sanitizer.log` | cbz_sanitizer.py |
| `Logs\cbz_folder_merger.log` | cbz_folder_merger.py |
| `Logs\cbz_compilation_resolver.log` | cbz_compilation_resolver.py |
| `Logs\cbz_series_matcher.log` | cbz_series_matcher.py |
| `Logs\cbz_number_tagger.log` | cbz_number_tagger.py |
| `Logs\cbz_deduplicator.log` | cbz_deduplicator.py |
| `Logs\strip_duplicates.log` | strip_duplicates.py |
| `Logs\cbz_gaps_YYYYMMDD_HHMMSS.csv` | cbz_gap_checker.py (CSV report) |

`find_uncensored_dupes.py` logs to stdout only — no persistent log file.

---

## Documentation

| Doc | Contents |
|-----|---------|
| [docs/overview.md](docs/overview.md) | Design principles, all tools at a glance, repo structure, log paths |
| [docs/cbz_sanitizer.md](docs/cbz_sanitizer.md) | Full CLI reference, sort modes, rule toggles, progress/resume system, parallel processing |
| [docs/cbz_watcher.md](docs/cbz_watcher.md) | Configuration, routing logic, settle/age timers, Task Scheduler setup |
| [docs/other_tools.md](docs/other_tools.md) | folder merger, compilation resolver, deduplicator, number tagger, series matcher, gap checker, strip_duplicates, find_uncensored_dupes |
| [docs/shared_pipeline.md](docs/shared_pipeline.md) | sanitize() steps, ComicInfo tag logic, archive rewriting, conflict resolution |
| [docs/engineering_decisions.md](docs/engineering_decisions.md) | Rationale for non-obvious design choices |
