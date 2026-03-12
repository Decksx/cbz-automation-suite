# Overview

The CBZ Automation Suite is a collection of Python scripts for monitoring, cleaning, tagging, and routing `.cbz` comic book archives on Windows. Designed to run against a network share (e.g. `\\tower\media\comics\`) or a local drive with minimal manual intervention.

---

## Design Principles

- **Hands-off pipeline** — files dropped into a watch folder are processed and routed automatically.
- **Resumable** — batch operations track progress in an append-only JSONL file; interrupting a long run costs nothing to recover from.
- **Non-destructive** — files are renamed in place, never silently deleted; on any collision the larger file wins.
- **Windows-aware** — explicit handling for `FileExistsError` on rename, UNC paths, and watchdog destination-folder event filtering.
- **Dry-run everywhere** — all batch tools support `--dry-run` for safe previewing on large libraries.
- **One canonical reference** — `scripts/cbz_sanitizer.py` owns all shared functions; other scripts sync from it rather than maintaining independent copies.
- **External config** — routing is driven by `routing.json` at `C:\\git\\ComicAutomation\routing.json`, not hardcoded in the script. Add new sources or destinations without touching Python.

---

## Repository

- **GitHub:** https://github.com/Decksx/cbz-automation-suite
- **Local path:** `C:\Users\David.Johnson\ComicAutomation`

---

## Requirements

- Python 3.8+
- [`watchdog`](https://pypi.org/project/watchdog/) >= 3.0.0 — required by `cbz_watcher.py` **only**

All other scripts use the Python standard library exclusively (`zipfile`, `re`, `pathlib`, `logging`, `difflib`, `csv`, `json`, etc.).

```powershell
pip install watchdog
```

---

## Tools at a Glance

| Script | Purpose | Doc |
|--------|---------|-----|
| `scripts/cbz_watcher.py` | Live watcher — monitors Incoming folder, cleans, tags, and routes files via `routing.json` | [cbz_watcher.md](cbz_watcher.md) |
| `scripts/cbz_sanitizer.py` | Batch sanitizer — in-place clean/tag with `--sort`, `--resume`, `--dry-run` | [cbz_sanitizer.md](cbz_sanitizer.md) |
| `scripts/cbz_folder_merger.py` | Merges colliding series directories; interactive path prompt; supports UNC and local drives | [other_tools.md](other_tools.md#cbz_folder_mergerpy) |
| `scripts/cbz_compilation_resolver.py` | Resolves compilation vs individual overlaps; rewrites with best pages | [other_tools.md](other_tools.md#cbz_compilation_resolverpy) |
| `scripts/cbz_number_tagger.py` | Retroactively sets `<Number>` and `<Volume>` ComicInfo tags from filenames | [other_tools.md](other_tools.md#cbz_number_taggerpy) |
| `scripts/cbz_series_matcher.py` | Near-duplicate series name detector; auto-merges above threshold | [other_tools.md](other_tools.md#cbz_series_matcherpy) |
| `scripts/cbz_gap_checker.py` | Scans library, outputs timestamped CSV of missing chapter numbers | [other_tools.md](other_tools.md#cbz_gap_checkerpy) |
| `scripts/strip_duplicates.py` | Removes duplicate number tokens and fixes spaced punctuation; importable as library | [other_tools.md](other_tools.md#strip_duplicatespy) |
| `config/routing.example.json` | Template for `routing.json` — copy to `C:\\git\\ComicAutomation\routing.json` and edit | [cbz_watcher.md](cbz_watcher.md#routing) |
| `config/run_watcher.bat` | Double-click launcher — installs watchdog and starts the watcher | — |
| `config/CBZWatcher_Task.xml` | Windows Task Scheduler import — auto-starts watcher on login | — |

---

## Running Scripts

All scripts live in `scripts/`. Run from the **repo root**:

```powershell
cd C:\Users\David.Johnson\ComicAutomation
python scripts\cbz_sanitizer.py --dry-run
python scripts\cbz_watcher.py
```

---

## First-time Setup

1. Clone the repo to `C:\Users\David.Johnson\ComicAutomation`
2. Copy `config\routing.example.json` to `C:\\git\\ComicAutomation\routing.json`
3. Edit `routing.json` to set your actual destination paths and source rules
4. Edit the `WATCH_FOLDER`, `LOG_FILE`, and `ROUTING_FILE` constants at the top of `scripts\cbz_watcher.py`
5. Run via `config\run_watcher.bat` or import `config\CBZWatcher_Task.xml` into Task Scheduler

---

## Repository File Structure

```
cbz-automation-suite/
├── scripts/
│   ├── cbz_watcher.py              # Live watcher (main tool)
│   ├── cbz_sanitizer.py            # Canonical shared-function reference
│   ├── cbz_folder_merger.py
│   ├── cbz_compilation_resolver.py
│   ├── cbz_number_tagger.py
│   ├── cbz_series_matcher.py
│   ├── cbz_gap_checker.py
│   └── strip_duplicates.py
├── config/
│   ├── routing.example.json        # Template — copy to C:\\git\\ComicAutomation\routing.json
│   ├── run_watcher.bat
│   └── CBZWatcher_Task.xml
├── docs/
│   ├── overview.md                 ← this file
│   ├── cbz_sanitizer.md
│   ├── cbz_watcher.md
│   ├── other_tools.md
│   ├── shared_pipeline.md
│   ├── engineering_decisions.md
│   └── CBZ_Automation_Suite_Documentation.docx
├── progress_tracking/              # folder committed; contents gitignored
│   ├── cbz_sanitizer_progress.json
│   ├── Newest1st_cbz_sanitizer_progress.json
│   └── Oldestfirstcbz_sanitizer_progress.json
├── README.md
└── requirements.txt
```

> **Runtime files** — `routing.json` and `*.log` live at `C:\\git\\ComicAutomation\` on the host machine and are excluded from git. Progress JSONs live in `progress_tracking/` in the repo folder — the folder is committed but the JSON contents are gitignored.

---

## Logs

All tools write rotating logs (max 5 MB, 3 backups). Configure `LOG_FILE` at the top of each script.

| Log file | Script |
|----------|--------|
| `C:\\git\\ComicAutomation\cbz_watcher.log` | cbz_watcher.py |
| `C:\\git\\ComicAutomation\cbz_sanitizer.log` | cbz_sanitizer.py |
| `C:\\git\\ComicAutomation\cbz_folder_merger.log` | cbz_folder_merger.py |
| `C:\\git\\ComicAutomation\cbz_compilation_resolver.log` | cbz_compilation_resolver.py |
| `C:\\git\\ComicAutomation\cbz_series_matcher.log` | cbz_series_matcher.py |
| `C:\\git\\ComicAutomation\cbz_number_tagger.log` | cbz_number_tagger.py |
| `C:\\git\\ComicAutomation\cbz_gap_checker.log` | cbz_gap_checker.py |
| `C:\\git\\ComicAutomation\strip_duplicates.log` | strip_duplicates.py |
