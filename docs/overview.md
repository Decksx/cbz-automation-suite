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
| `scripts/cbz_watcher.py` | Live watcher — monitors Incoming folder, cleans, tags, and routes files | [cbz_watcher.md](cbz_watcher.md) |
| `scripts/cbz_sanitizer.py` | Batch sanitizer — in-place clean/tag with `--sort`, `--resume`, `--dry-run` | [cbz_sanitizer.md](cbz_sanitizer.md) |
| `scripts/cbz_folder_merger.py` | Merges colliding series directories; keeps larger file on conflict | [other_tools.md](other_tools.md#cbz_folder_mergerpy) |
| `scripts/cbz_folder_merger_LDrive.py` | Local-drive variant of folder merger | [other_tools.md](other_tools.md#cbz_folder_merger_ldrivepy) |
| `scripts/cbz_compilation_resolver.py` | Resolves compilation vs individual overlaps; rewrites with best pages | [other_tools.md](other_tools.md#cbz_compilation_resolverpy) |
| `scripts/cbz_number_tagger.py` | Retroactively sets `<Number>` and `<Volume>` ComicInfo tags from filenames | [other_tools.md](other_tools.md#cbz_number_taggerpy) |
| `scripts/cbz_series_matcher.py` | Near-duplicate series name detector; auto-merges above threshold | [other_tools.md](other_tools.md#cbz_series_matcherpy) |
| `scripts/cbz_gap_checker.py` | Scans library, outputs timestamped CSV of missing chapter numbers | [other_tools.md](other_tools.md#cbz_gap_checkerpy) |
| `scripts/strip_duplicates.py` | Removes duplicate number tokens and fixes spaced punctuation; importable as library | [other_tools.md](other_tools.md#strip_duplicatespy) |
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

## Repository File Structure

```
cbz-automation-suite/
├── scripts/
│   ├── cbz_watcher.py
│   ├── cbz_sanitizer.py            # Canonical shared-function reference
│   ├── cbz_folder_merger.py
│   ├── cbz_folder_merger_LDrive.py
│   ├── cbz_compilation_resolver.py
│   ├── cbz_number_tagger.py
│   ├── cbz_series_matcher.py
│   ├── cbz_gap_checker.py
│   └── strip_duplicates.py
├── config/
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
├── README.md
└── requirements.txt
```

---

## Logs

All tools write rotating logs (max 5 MB, 3 backups). Configure `LOG_FILE` at the top of each script.

| Log file | Script |
|----------|--------|
| `C:\ComicAutomation\cbz_watcher.log` | cbz_watcher.py |
| `C:\ComicAutomation\cbz_sanitizer.log` | cbz_sanitizer.py |
| `C:\ComicAutomation\cbz_folder_merger.log` | cbz_folder_merger.py |
| `C:\ComicAutomation\cbz_compilation_resolver.log` | cbz_compilation_resolver.py |
| `C:\ComicAutomation\cbz_series_matcher.log` | cbz_series_matcher.py |
| `C:\ComicAutomation\cbz_number_tagger.log` | cbz_number_tagger.py |
| `C:\ComicAutomation\cbz_gap_checker.log` | cbz_gap_checker.py |
| `C:\ComicAutomation\strip_duplicates.log` | strip_duplicates.py |
