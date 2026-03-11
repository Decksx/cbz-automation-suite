# cbz_watcher.py

Live file watcher. Monitors an Incoming folder for `.cbz` files, applies the full cleaning/tagging pipeline, and moves each processed directory to its configured destination. Uses [watchdog](https://pypi.org/project/watchdog/) for filesystem event monitoring.

---

## Configuration

Edit the constants at the top of `scripts\cbz_watcher.py`:

```python
WATCH_FOLDER  = r"C:\Temp\Mega\Mega Uploads\book2"   # folder to monitor
LOG_FILE      = r"C:\ComicAutomation\cbz_watcher.log"
POLL_INTERVAL = 2      # seconds between stability checks
SETTLE_DELAY  = 5      # seconds of inactivity before a directory is processed
MIN_AGE       = 300    # minimum directory age in seconds before processing (5 min)

DEFAULT_DEST  = r"\\tower\media\comics\Comix"

# Only list sources that need a NON-default destination.
# Keys are matched case-insensitively. Anything not listed goes to DEFAULT_DEST.
SOURCE_ROUTING = {
    "manga-source": r"\\tower\media\comics\Manga",
}
```

---

## Running

```powershell
python scripts\cbz_watcher.py
# or double-click config\run_watcher.bat  (installs watchdog automatically)
# or import config\CBZWatcher_Task.xml into Task Scheduler for auto-start on login
```

---

## Routing Logic

The watcher reads the **immediate subdirectory name** inside `WATCH_FOLDER` and looks it up (case-insensitively) in `SOURCE_ROUTING`. Anything not matched routes to `DEFAULT_DEST`.

```
WATCH_FOLDER/
├── MangaDex (EN)/          →  \\tower\media\comics\Manga   (SOURCE_ROUTING match)
│   └── Some Series/
│       └── ch01.cbz
└── Any Other Source/       →  \\tower\media\comics\Comix   (DEFAULT_DEST fallback)
    └── Batman/
        └── ch01.cbz
```

The routing table currently lists 40+ manga sources, all pointing to `\\tower\media\comics\Manga`.

---

## Settle & Age Timers

A directory is only processed when **both** conditions are met:

| Timer | Setting | Purpose |
|-------|---------|---------|
| Settle delay | `SETTLE_DELAY = 5s` | No new file events for this duration — ensures downloads are complete |
| Minimum age | `MIN_AGE = 300s` | Directory must be at least this old — prevents processing partially-synced folders |

If a directory has settled but hasn't reached `MIN_AGE`, the watcher logs the remaining wait time and checks again on the next `POLL_INTERVAL` tick.

---

## Processing Pipeline

For each directory that passes the timers:

1. **Stability check** — verifies each `.cbz` file is stable (no size change between checks).
2. **Filename cleaning** — applies `sanitize()` + `normalize_stem()` to each file's stem.
3. **Rename** — renames each `.cbz` if the cleaned name differs.
4. **ComicInfo.xml** — creates or updates `<Title>`, `<Series>`, `<Number>`, and `<Volume>` tags.
5. **Archive rewrite** — rewrites the archive if XML changed, preserving original compression.
6. **Directory rename** — renames the parent directory using the cleaned series name.
7. **Route & move** — resolves the destination via `SOURCE_ROUTING` / `DEFAULT_DEST` and moves the directory.
8. **Merge** — if the destination directory already exists, merges file by file (larger file wins on collision).

See [shared_pipeline.md](shared_pipeline.md) for the full `sanitize()` step breakdown and `ComicInfo.xml` tag logic.

---

## Windows Task Scheduler (Auto-start on Login)

1. Open Task Scheduler → **Import Task**
2. Select `config\CBZWatcher_Task.xml`
3. Update the **Action** paths if your Python or repo location differs:
   - **Program:** path to your `python.exe`
   - **Arguments:** `scripts\cbz_watcher.py`
   - **Start in:** `C:\Users\David.Johnson\ComicAutomation`
4. Under **Triggers**, confirm the trigger is set to **At log on**.

Alternatively, just double-click `config\run_watcher.bat` for a manual session — it handles `watchdog` installation automatically.

---

## Windows Notes

- The watchdog handler filters out events fired for files being **moved to** the destination folder. Without this guard, every processed file would re-trigger the settle timer in a loop.
- Destination directories are pre-created at startup to avoid first-move delays on UNC shares.
- The `_processing_dirs` set (with a threading lock) prevents re-queuing directories that are mid-process.

---

## Logging

Rotating log file at `LOG_FILE` (5 MB max, 3 backups). Also streams to stdout. Log entries cover every event, stability check, settle/age wait, rename, tag update, route decision, move, and merge conflict.
