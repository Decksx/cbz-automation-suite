# CBZ Automation Suite

A collection of Python scripts for monitoring, cleaning, tagging, and routing `.cbz` comic book archives on Windows. Designed to work against a network share (e.g. `\\tower\media\comics\`) or a local drive.

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
│   ├── cbz_number_tagger.py        # Retroactively set <Number>/<Volume> tags
│   ├── cbz_series_matcher.py       # Detect and merge near-duplicate series folders
│   ├── cbz_gap_checker.py          # Report missing chapter numbers per series
│   └── strip_duplicates.py         # Remove duplicate number tokens from filenames
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
├── README.md
└── requirements.txt
```

---

## Tools

| Script | Purpose |
|--------|---------|
| `cbz_watcher.py` | Live watcher — monitors an Incoming folder, cleans filenames, injects `ComicInfo.xml` metadata, and routes files to the correct destination |
| `cbz_sanitizer.py` | Batch sanitizer — walks a library folder and applies the full cleaning/tagging pipeline in-place; supports `--sort`, `--resume`, `--restart`, and `--dry-run` |
| `cbz_folder_merger.py` | Merges sibling directories whose cleaned names collide; keeps the larger file on any conflict |
| `cbz_compilation_resolver.py` | Detects compilation/individual chapter overlaps; performs page-by-page quality comparison and rewrites compilations with the best pages |
| `cbz_number_tagger.py` | Sets `<Number>` and `<Volume>` tags in `ComicInfo.xml` from the filename — retroactive library tool |
| `cbz_series_matcher.py` | Finds near-duplicate series folder names and auto-merges above a configurable similarity threshold |
| `cbz_gap_checker.py` | Scans library folders and writes a timestamped CSV report of missing chapter numbers per series |
| `strip_duplicates.py` | Removes duplicate number tokens and fixes oddly spaced punctuation in filenames; also importable as a library |

---

## Requirements

- Python 3.8+
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
LOG_FILE      = r"C:\git\ComicAutomation\cbz_watcher.log"
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
python scripts\cbz_sanitizer.py --resume                      # resume an interrupted run
python scripts\cbz_sanitizer.py --restart                     # ignore saved progress, start fresh
python scripts\cbz_sanitizer.py --dry-run                     # preview only, no changes written
```

### Other Tools

```powershell
python scripts\cbz_number_tagger.py --dry-run
python scripts\cbz_series_matcher.py --dry-run
python scripts\cbz_gap_checker.py
python scripts\cbz_compilation_resolver.py --dry-run
python scripts\cbz_folder_merger.py --dry-run
python scripts\strip_duplicates.py "C:\Comics" --recursive --dry-run
```

See [docs/other_tools.md](docs/other_tools.md) for full details on each.

---

## How It Works

### Filename & Metadata Cleaning

All tools share a common `sanitize()` pipeline (defined in `cbz_sanitizer.py`) that strips non-Latin/non-Greek/non-emoji characters (covering CJK, Arabic, Cyrillic, full-width forms, etc.), bracketed group and publisher tags, website patterns, scanner/scanlation credits, trailing G-code suffixes, and normalises whitespace. See [docs/shared_pipeline.md](docs/shared_pipeline.md) for the full step-by-step breakdown.

`ComicInfo.xml` is created or updated with `<Title>`, `<Series>`, `<Number>`, and `<Volume>` tags derived from the filename and directory name.

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

---

## Documentation

| Doc | Contents |
|-----|---------|
| [docs/overview.md](docs/overview.md) | Design principles, all tools at a glance, repo structure, log paths |
| [docs/cbz_sanitizer.md](docs/cbz_sanitizer.md) | Full CLI reference, sort modes, progress/resume system |
| [docs/cbz_watcher.md](docs/cbz_watcher.md) | Configuration, routing logic, settle/age timers, Task Scheduler setup |
| [docs/other_tools.md](docs/other_tools.md) | folder merger, compilation resolver, number tagger, series matcher, gap checker, strip_duplicates |
| [docs/shared_pipeline.md](docs/shared_pipeline.md) | sanitize() steps, ComicInfo tag logic, archive rewriting, conflict resolution |
| [docs/engineering_decisions.md](docs/engineering_decisions.md) | Rationale for non-obvious design choices |
