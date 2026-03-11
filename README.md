# CBZ Automation Suite

A collection of Python scripts for monitoring, cleaning, tagging, and routing `.cbz` comic book archives on Windows. Designed to work against a network share (e.g. `\\tower\media\comics\`) or a local drive.

📖 **[Full documentation in /docs](docs/overview.md)**

---

## Repository Structure

```
cbz-automation-suite/
├── scripts/                        # All Python scripts — run from here
│   ├── cbz_watcher.py
│   ├── cbz_sanitizer.py
│   ├── cbz_folder_merger.py
│   ├── cbz_folder_merger_LDrive.py
│   ├── cbz_compilation_resolver.py
│   ├── cbz_number_tagger.py
│   ├── cbz_series_matcher.py
│   ├── cbz_gap_checker.py
│   └── strip_duplicates.py
├── config/
│   ├── run_watcher.bat             # Double-click launcher
│   └── CBZWatcher_Task.xml        # Windows Task Scheduler import
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
| `cbz_watcher.py` | Live watcher — monitors an Incoming folder, cleans filenames, injects ComicInfo.xml metadata, and routes files to the correct destination |
| `cbz_sanitizer.py` | Batch sanitizer — walks a library folder and applies the full cleaning/tagging pipeline in-place; supports sort order, multiple scan targets, resume, and dry-run via CLI flags |
| `cbz_folder_merger.py` | Merges directories whose cleaned names collide; keeps the larger file on any conflict |
| `cbz_compilation_resolver.py` | Detects compilation/individual chapter overlaps; performs page-by-page quality comparison and rewrites compilations with the best pages |
| `cbz_number_tagger.py` | Sets `<Number>` (chapter) and `<Volume>` tags in ComicInfo.xml based on the filename |
| `cbz_series_matcher.py` | Finds near-duplicate series folder names and auto-merges above a configurable similarity threshold |
| `cbz_gap_checker.py` | Scans library folders and writes a CSV report of missing chapter numbers per series |
| `strip_duplicates.py` | Removes duplicate number tokens and fixes oddly spaced punctuation in filenames |

---

## Requirements

- Python 3.8+
- [`watchdog`](https://pypi.org/project/watchdog/) — required by `cbz_watcher.py` only; all other scripts use the standard library

```bash
pip install watchdog
# or just double-click config\run_watcher.bat — it handles this automatically
```

---

## Quick Start

All scripts live in `scripts/`. Run them from the **repo root**:

```powershell
cd C:\Users\David.Johnson\ComicAutomation
```

### Live Watcher

Edit the constants at the top of `scripts\cbz_watcher.py`:

```python
WATCH_FOLDER = r"C:\Comics\Incoming"
LOG_FILE     = r"C:\ComicAutomation\cbz_watcher.log"
DEFAULT_DEST = r"\\tower\media\comics\Comix"

SOURCE_ROUTING = {
    "manga-source": r"\\tower\media\comics\Manga",
}
```

```powershell
python scripts\cbz_watcher.py
# or double-click config\run_watcher.bat
# or import config\CBZWatcher_Task.xml into Task Scheduler for auto-start on login
```

### Batch Sanitize

```powershell
python scripts\cbz_sanitizer.py                               # scan configured folders, newest first
python scripts\cbz_sanitizer.py "L:\Comix"                    # specific path
python scripts\cbz_sanitizer.py --sort=oldest                 # oldest-modified dirs first
python scripts\cbz_sanitizer.py --sort=alpha                  # alphabetical
python scripts\cbz_sanitizer.py --resume                      # resume interrupted run
python scripts\cbz_sanitizer.py --dry-run                     # preview only
python scripts\cbz_sanitizer.py "L:\Comix" --sort=oldest --dry-run
```

### Other Tools

```powershell
python scripts\cbz_number_tagger.py --dry-run
python scripts\cbz_series_matcher.py --dry-run
python scripts\cbz_gap_checker.py
python scripts\cbz_compilation_resolver.py --dry-run
python scripts\strip_duplicates.py "C:\Comics" --recursive --dry-run
```

See [docs/other_tools.md](docs/other_tools.md) for full details on each.

---

## How It Works

All tools share a common `sanitize()` pipeline that strips bracketed group tags, CJK characters, website patterns, scanner credits, and normalises whitespace. `ComicInfo.xml` is created or updated with `<Series>`, `<Title>`, `<Number>`, and `<Volume>` tags derived from the directory and filename.

See [docs/shared_pipeline.md](docs/shared_pipeline.md) for the full pipeline breakdown.

### Routing (watcher only)

```
WATCH_FOLDER/
├── manga-source/     →  \\tower\media\comics\Manga
└── anything-else/    →  \\tower\media\comics\Comix  (default)
```

### Conflict Resolution

On any filename collision during a merge, **the larger file is kept**.

---

## Notes

- **Windows only** — path handling, network shares, and rename behaviour are Windows-specific.
- `scripts\cbz_sanitizer.py` is the **canonical reference** for all shared functions. Other tools sync from it.
- `scripts\strip_duplicates.py` can also be used as an importable library: `from scripts.strip_duplicates import clean` (or `cd scripts` first).
- Progress files (`*_progress.json`) are machine-local and excluded from git.

---

## Documentation

| Doc | Contents |
|-----|---------|
| [docs/overview.md](docs/overview.md) | Design principles, tools at a glance, repo structure, logs |
| [docs/cbz_sanitizer.md](docs/cbz_sanitizer.md) | Full CLI reference, sort modes, progress tracking, processing pipeline |
| [docs/cbz_watcher.md](docs/cbz_watcher.md) | Configuration, routing logic, processing pipeline, Windows notes |
| [docs/other_tools.md](docs/other_tools.md) | merger, compilation resolver, number tagger, series matcher, gap checker, strip_duplicates |
| [docs/shared_pipeline.md](docs/shared_pipeline.md) | sanitize() steps, ComicInfo tag logic, conflict resolution |
| [docs/engineering_decisions.md](docs/engineering_decisions.md) | Rationale for non-obvious design choices |
