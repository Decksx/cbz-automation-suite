# CBZ Documentation Update Package

## Apply automatically

📖 **[Full documentation in /docs](docs/overview.md)**

---

## Repository Structure

```
cbz-automation-suite/
├── apps/
│   └── cbz_gui.py                  # GUI launcher for the CBZ tools
├── scripts/
│   ├── cbz_watcher.py              # Live watcher — main day-to-day tool
│   ├── cbz_sanitizer.py            # Batch sanitizer — canonical shared-function reference
│   ├── cbz_library_maintenance.py  # Consolidated archive cleanup, organization, metadata repair
│   ├── cbz_workflows.py            # Unified series and library-maintenance workflows
│   ├── cbz_compilation_resolver.py # Resolve compilation vs individual chapter overlaps
│   ├── cbz_gap_checker.py          # Report missing chapter numbers per series
│   └── cbz_core.py                 # Shared filename and ComicInfo helpers
├── config/
│   ├── run_watcher.bat             # Double-click launcher
│   └── CBZWatcher_Task.xml         # Windows Task Scheduler import
├── tools/
│   ├── comics/                     # Standalone PowerShell comic helpers
│   ├── directory_mirroring/        # Mirror/sync utility scripts
│   ├── plex/                       # Plex media organization helpers
│   └── apply_doc_updates.ps1       # Documentation update helper
├── integrations/
│   └── komf/                       # KOMF config and userscript
├── data/
│   └── reports/                    # Historical generated reports worth keeping
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
├── archive/                        # old backups and patch bundles
├── README.md
└── requirements.txt
```

---

## Tools

| Script | Recursive? | Workers? | Purpose |
|--------|-----------|----------|---------|
| `cbz_watcher.py` | Always | — | Live watcher — monitors an Incoming folder, cleans filenames, injects `ComicInfo.xml` metadata, and routes files to the correct destination |
| `cbz_workflows.py maintenance` | Mixed | **Yes** | Unified sanitizer, archive cleanup, organization, metadata repair, and name repair workflow |
| `cbz_workflows.py series` | Mixed | **Yes** | Unified series organizer, similar-series staging/review, and page-level compilation resolver workflow |
| `cbz_gap_checker.py` | **Yes — default** | **Yes** | Scans library folders and writes a timestamped CSV report of missing chapter numbers per series |

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

Run commands from the repo root:

```powershell
cd C:\Users\David.Johnson\Documents\ComicAutomation
```

### Live Watcher

Edit the constants at the top of `scripts\cbz_watcher.py`:

```python
WATCH_FOLDER  = r"C:\Comics\Incoming"
REPO_ROOT     = Path(__file__).resolve().parents[1]
LOG_FILE      = REPO_ROOT / "Logs" / "cbz_watcher.log"
ROUTING_FILE  = REPO_ROOT / "routing.json"
```

Copy `config\routing.example.json` to `routing.json` in the repo root and set your destinations and rules:

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

### GUI Launcher

```powershell
python apps\cbz_gui.py
```

### Routing Setup

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

### Unified Workflows

```powershell
# Complete maintenance pass; remove stages for a targeted run.
python scripts\cbz_workflows.py maintenance "\\tower\media\comics\Comix" --dry-run
python scripts\cbz_workflows.py maintenance "\\tower\media\comics\Comix" --stages=sanitize,metadata,names

# Organize, stage similar series, generate a review proposal, and resolve compilations.
python scripts\cbz_workflows.py series "\\tower\media\comics\Comix" --dry-run --stages=organize,stage,review,compilations
```

The GUI exposes these as **Library Maintenance** and **Series Workflow**.
Existing individual script commands remain available for scheduled jobs and
backward compatibility.

### Library Maintenance

```powershell
python scripts\cbz_library_maintenance.py archive-clean "\\tower\media\comics\Comix" --dry-run
python scripts\cbz_library_maintenance.py organize-series "\\tower\media\comics\Comix" --dry-run
python scripts\cbz_library_maintenance.py organize-series "\\tower\media\comics\Comix" --uncensored-check --move-which both
python scripts\cbz_library_maintenance.py organize-series "\\tower\media\comics\Comix" --possible-series-check
python scripts\cbz_library_maintenance.py metadata "\\tower\media\comics\Comix" --dry-run
python scripts\cbz_library_maintenance.py all "\\tower\media\comics\Comix" --dry-run
```

### Other Tools

```powershell
cd "C:\Users\David.Johnson\ComicAutomation"
powershell -ExecutionPolicy Bypass -File ".\tools\apply_doc_updates.ps1" -RepoRoot "."
```

The script creates a timestamped backup of your current `docs/` folder before overwriting files.

## Or copy manually

Copy the files in `docs/` into your repository's `docs/` folder.

## Review and commit

```powershell
python scripts\cbz_sanitizer.py --workers 8
python scripts\cbz_library_maintenance.py archive-clean "\\tower\media\comics\Comix" --workers 4
python scripts\cbz_gap_checker.py --workers 8
python scripts\cbz_compilation_resolver.py --workers 8
python scripts\cbz_library_maintenance.py organize-series "\\tower\media\comics\Comix" --workers 8
python scripts\cbz_library_maintenance.py metadata "\\tower\media\comics\Comix" --workers 4
```

The default is `min(8, cpu_count)`. Pass `--workers 1` to restore fully serial behaviour. See [docs/engineering_decisions.md](docs/engineering_decisions.md) for the design rationale.

---

## How It Works

### Filename & Metadata Cleaning

All tools share a common `sanitize()` pipeline (defined in `cbz_sanitizer.py`) that strips non-Latin/non-Greek/non-emoji characters (covering CJK, Arabic, Cyrillic, full-width forms, etc.), bracketed group and publisher tags, website patterns, scanner/scanlation credits, trailing G-code suffixes, and normalises whitespace. See [docs/shared_pipeline.md](docs/shared_pipeline.md) for the full step-by-step breakdown.

`ComicInfo.xml` is created or updated with `<Title>`, `<Series>`, `<Number>`, and `<Volume>` tags derived from the filename and directory name.

Japanese, Chinese, and Korean titles are translated to English before filename
and metadata normalization when no separate English title is already present.
Native-script titles are translated automatically; romanized titles are handled
when language detection identifies Japanese, Korean, or Chinese. Existing
original-language values are retained in `<AlternateSeries>` and `<Notes>`.
Set `CBZ_TRANSLATION_ENABLED=0` to disable online translation; native-script
titles are preserved unchanged when translation is unavailable. Set
`CBZ_TRANSLITERATE_FALLBACK=1` to opt into offline transliteration instead.

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
- `scripts\cbz_core.py` contains the shared filename and ComicInfo helpers.
- `scripts\cbz_library_maintenance.py` consolidates the former deduplicator, duplicate-token stripper, folder merger, series matcher, uncensored duplicate finder, and number tagger workflows.
- Progress files (`*_progress.json`) are machine-local and excluded from git via `.gitignore`.
- All log files are written to `Logs\` — the folder is committed (via `Logs\.gitkeep`) so it always exists on a fresh clone. Log contents are gitignored.
- Archive cleanup supports `--no-recursive`; organization supports `--recursive-parents` when nested sibling groups should be considered.
- All batch tools default to `min(8, cpu_count)` workers. Pass `--workers 1` for fully serial behaviour.

---

## Logs

All logs go to `Logs\` under the repo root. The folder is committed to git so it always exists on a fresh clone — no manual creation needed.

| Log file | Script |
|----------|--------|
| `Logs\cbz_watcher.log` | cbz_watcher.py |
| `Logs\cbz_sanitizer.log` | cbz_sanitizer.py |
| `Logs\cbz_library_maintenance.log` | cbz_library_maintenance.py |
| `Logs\cbz_compilation_resolver.log` | cbz_compilation_resolver.py |
| `Logs\cbz_gaps_YYYYMMDD_HHMMSS.csv` | cbz_gap_checker.py (CSV report) |

---

## Documentation

| Doc | Contents |
|-----|---------|
| [docs/overview.md](docs/overview.md) | Design principles, all tools at a glance, repo structure, log paths |
| [docs/cbz_sanitizer.md](docs/cbz_sanitizer.md) | Full CLI reference, sort modes, rule toggles, progress/resume system, parallel processing |
| [docs/cbz_watcher.md](docs/cbz_watcher.md) | Configuration, routing logic, settle/age timers, Task Scheduler setup |
| [docs/other_tools.md](docs/other_tools.md) | consolidated maintenance commands, compilation resolver, and gap checker |
| [docs/shared_pipeline.md](docs/shared_pipeline.md) | sanitize() steps, ComicInfo tag logic, archive rewriting, conflict resolution |
| [docs/engineering_decisions.md](docs/engineering_decisions.md) | Rationale for non-obvious design choices |
