# CBZ Automation Suite

A collection of Python scripts for monitoring, cleaning, tagging, and routing `.cbz` comic book archives on Windows. Designed to work against a network share (e.g. `\\tower\media\comics\`) or a local drive.

📖 **[Full documentation in /docs](docs/overview.md)**

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
| `run_watcher.bat` | Windows launcher — installs `watchdog` and starts `cbz_watcher.py` |

---

## Requirements

- Python 3.8+
- [`watchdog`](https://pypi.org/project/watchdog/) — required by `cbz_watcher.py` only; all other scripts use the standard library

```bash
pip install watchdog
# or just double-click run_watcher.bat — it handles this automatically
```

---

## Quick Start

### Live Watcher

Edit the constants at the top of `cbz_watcher.py`:

```python
WATCH_FOLDER = r"C:\Comics\Incoming"
LOG_FILE     = r"C:\ComicAutomation\cbz_watcher.log"
DEFAULT_DEST = r"\\tower\media\comics\Comix"

SOURCE_ROUTING = {
    "manga-source": r"\\tower\media\comics\Manga",
}
```

```
python cbz_watcher.py
```

See [cbz_watcher.md](docs/cbz_watcher.md) for full details.

### Batch Sanitize

```
python cbz_sanitizer.py                               # scan configured folders, newest first
python cbz_sanitizer.py "L:\Comix"                    # specific path
python cbz_sanitizer.py --sort=oldest                 # oldest-modified dirs first
python cbz_sanitizer.py --sort=alpha                  # alphabetical
python cbz_sanitizer.py --resume                      # resume interrupted run
python cbz_sanitizer.py --dry-run                     # preview only
python cbz_sanitizer.py "L:\Comix" --sort=oldest --dry-run
```

See [cbz_sanitizer.md](docs/cbz_sanitizer.md) for full details.

### Other Tools

```
python cbz_number_tagger.py --dry-run
python cbz_series_matcher.py --dry-run
python cbz_gap_checker.py
python cbz_compilation_resolver.py --dry-run
python strip_duplicates.py "C:\Comics" --recursive --dry-run
```

See [other_tools.md](docs/other_tools.md) for full details on each.

---

## How It Works

### Filename & Metadata Cleaning

All tools share a common `sanitize()` pipeline that strips bracketed group tags, CJK characters, website patterns, scanner credits, and normalises whitespace. `ComicInfo.xml` is created or updated with `<Series>`, `<Title>`, `<Number>`, and `<Volume>` tags derived from the directory and filename.

See [shared_pipeline.md](docs/shared_pipeline.md) for the full pipeline breakdown.

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
- `cbz_sanitizer.py` is the **canonical reference** for all shared functions. Other tools sync from it.
- `strip_duplicates.py` can also be used as an importable library: `from strip_duplicates import clean`.
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
