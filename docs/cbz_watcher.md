# cbz_watcher.py — Live Watcher

Monitors a watch folder for new `.cbz` directories using [watchdog](https://pypi.org/project/watchdog/). When a directory has been stable for `MIN_AGE` seconds, it is processed: filenames are cleaned, `ComicInfo.xml` is created or updated, and the directory is moved to the correct destination share.

---

## Running

```powershell
# From the repo root
python scripts\cbz_watcher.py

# Or double-click the launcher (installs watchdog automatically)
config\run_watcher.bat

# Or import into Windows Task Scheduler for auto-start on login
# Import: config\CBZWatcher_Task.xml
```

---

## Configuration

Edit the constants at the top of `scripts\cbz_watcher.py`:

```python
WATCH_FOLDER = r"C:\Temp\Mega\Mega Uploads\book2"   # Folder to monitor
LOG_FILE     = r"C:\ComicAutomation\cbz_watcher.log"
DEFAULT_DEST = r"\\tower\media\comics\Comix"         # Default destination

SETTLE_DELAY = 5    # Seconds to wait after last file activity before processing
MIN_AGE      = 300  # Minimum directory age (seconds) before processing

# Only list sources that should route somewhere OTHER than DEFAULT_DEST
SOURCE_ROUTING = {
    "1Manga.co (EN)": r"\\tower\media\comics\Manga",
    "MangaDex":       r"\\tower\media\comics\Manga",
    # ... 40+ manga sources pre-configured
}
```

---

## Routing Logic

The watcher reads the **immediate subfolder name** inside `WATCH_FOLDER` and looks it up in `SOURCE_ROUTING`. Anything not listed routes to `DEFAULT_DEST`.

```
WATCH_FOLDER/
├── 1Manga.co (EN)/          →  \\tower\media\comics\Manga
│   └── Some Series/
│       └── ch01.cbz
└── anything-else/           →  \\tower\media\comics\Comix  (default)
    └── Another Series/
        └── ch01.cbz
```

---

## Processing Pipeline

For each incoming directory, `process_and_move_directory()` runs:

1. Clean the top-level directory name
2. Group `.cbz` files by immediate parent directory
3. For each subdirectory: clean name → rename on disk
4. Pre-compute fallback names for files with empty cleaned stems
5. For each `.cbz`: `clean_filename()` → `normalize_stem()` → `normalise_number_tokens()` → rename
6. `process_comicinfo()` — read or create `ComicInfo.xml`; set `<Title>`, `<Series>`, `<Number>`, `<Volume>`
7. `_move_cbz_dir()` → `_merge_directories()` on destination conflict (larger file wins)

See [shared_pipeline.md](shared_pipeline.md) for details on the cleaning and ComicInfo logic.

---

## Number Tagging Note

The watcher already performs `<Number>` and `<Volume>` tagging inside `process_comicinfo()` as part of its normal pipeline. [`cbz_number_tagger.py`](other_tools.md#cbz_number_taggerpy) is a **separate retroactive tool** for files that were added before the watcher was running — it is not a duplicate.

---

## Windows Task Scheduler

To auto-start the watcher on login, import `config\CBZWatcher_Task.xml` into Task Scheduler:

```powershell
schtasks /create /xml "config\CBZWatcher_Task.xml" /tn "CBZWatcher"
```

The task runs `python scripts\cbz_watcher.py` with `C:\Users\David.Johnson\ComicAutomation` as the working directory.

---

## Windows-Specific Behaviour

- Watchdog fires events for files **moved to the destination folder** as well as files arriving in the watch folder. A path guard in the event handler filters destination-folder events to prevent re-queuing already-processed files.
- `Path.rename()` raises `FileExistsError` on Windows when the target exists. All rename sites include a pre-check and skip/merge logic.
