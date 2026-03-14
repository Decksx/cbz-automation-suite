# cbz_watcher.py

Live file watcher. Monitors an Incoming folder for `.cbz` files, applies the full cleaning/tagging pipeline, and moves each processed directory to its configured destination. Uses [watchdog](https://pypi.org/project/watchdog/) for filesystem event monitoring.

---

## Configuration

Edit the constants at the top of `scripts\cbz_watcher.py`:

```python
WATCH_FOLDER  = r"C:\Temp\Mega\Mega Uploads\book2"   # folder to monitor
LOG_FILE      = r"C:\git\ComicAutomation\cbz_watcher.log"
ROUTING_FILE  = r"C:\git\ComicAutomation\routing.json"
POLL_INTERVAL = 2      # seconds between stability checks
SETTLE_DELAY  = 5      # seconds of inactivity before a directory is processed
MIN_AGE       = 300    # minimum directory age in seconds before processing (5 min)
```

Routing is controlled entirely by `routing.json` — see [Routing](#routing) below.

---

## Running

```powershell
python scripts\cbz_watcher.py
# or double-click config\run_watcher.bat  (installs watchdog automatically)
# or import config\CBZWatcher_Task.xml into Task Scheduler for auto-start on login
```

---

## Routing

Destination routing is driven by `routing.json`, an external config file that lives at `C:\git\ComicAutomation\routing.json` (path set by `ROUTING_FILE`). The watcher loads it at startup. You never need to edit the Python script to add or change a route.

A `routing.example.json` template is provided in `config/` showing the full structure.

### Structure

```json
{
  "destinations": {
    "comics": "\\\\tower\\media\\comics\\Comix",
    "manga":  "\\\\tower\\media\\comics\\Manga"
  },
  "default": "comics",
  "rules": [
    { "match": "source", "pattern": "MangaDex (EN)",  "dest": "manga"  },
    { "match": "source", "pattern": "Toonily*",        "dest": "manga"  },
    { "match": "source", "pattern": "Pururin (EN)",    "dest": "comics" }
  ]
}
```

### Fields

| Field | Description |
|-------|-------------|
| `destinations` | Named aliases for destination paths. Define each path once here; rules reference the alias. |
| `default` | The destination alias used when no rule matches. |
| `rules` | Evaluated top-to-bottom; first match wins. |
| `match` | `"source"` matches the download source folder name inside `WATCH_FOLDER`; `"title"` matches the comic directory name. |
| `pattern` | Case-insensitive glob. Use `*` as a wildcard — e.g. `Toonily*` matches `Toonily.me (EN)`, `Toonily.net (EN)`, etc. |
| `dest` | References a key from `destinations`. |

### Adding a new destination

Add a new entry to `destinations`, then reference it in a rule:

```json
"hentai": "\\\\tower\\media\\comics\\Hentai"
```

### Adding a new source

```json
{ "match": "source", "pattern": "NewSite (EN)", "dest": "manga" }
```

### Folder structure

```
WATCH_FOLDER/
├── MangaDex (EN)/          →  \\tower\media\comics\Manga   (matched by rule)
│   └── Some Series/
│       └── ch01.cbz
└── Any Other Source/       →  \\tower\media\comics\Comix   (default fallback)
    └── Batman/
        └── ch01.cbz
```

---

## Settle & Age Timers

A directory is only processed when **both** conditions are met:

| Timer | Setting | Purpose |
|-------|---------|---------|
| Settle delay | `SETTLE_DELAY = 5s` | No new file events for this duration — ensures downloads are complete |
| Minimum age | `MIN_AGE = 300s` | Directory must be at least this old — prevents processing partially-synced folders from a 2-way cloud sync |

If a directory has settled but hasn't reached `MIN_AGE`, the watcher logs the remaining wait time and reschedules itself for the exact remaining duration.

---

## Processing Pipeline

For each directory that passes the timers:

1. **Top-level directory rename** — cleans the incoming directory name via `sanitize()` before anything else runs. If the cleaned name already exists, files are merged into the existing directory instead of crashing.
2. **Stability check** — verifies each `.cbz` file is stable (no size change between checks).
3. **Filename cleaning** — applies `sanitize()` + `normalize_stem()` to each file's stem.
4. **Rename** — renames each `.cbz` if the cleaned name differs. If the target already exists, the rename is skipped and the original filename is kept — no crash.
5. **ComicInfo.xml** — creates or updates `<Title>`, `<Series>`, `<Number>`, and `<Volume>` tags.
6. **Archive rewrite** — rewrites the archive if XML changed, preserving original compression.
7. **Route & move** — resolves the destination via `routing.json` and moves the directory immediately after processing.
8. **Merge** — if the destination directory already exists, merges file by file (larger file wins on collision).

See [shared_pipeline.md](shared_pipeline.md) for the full `sanitize()` step breakdown and `ComicInfo.xml` tag logic.

---

## Windows Task Scheduler (Auto-start on Login)

1. Open Task Scheduler → **Import Task**
2. Select `config\CBZWatcher_Task.xml`
3. Update the **Action** paths if your Python or repo location differs:
   - **Program:** path to your `python.exe`
   - **Arguments:** `scripts\cbz_watcher.py`
   - **Start in:** `C:\git\ComicAutomation`
4. Under **Triggers**, confirm the trigger is set to **At log on**.

Alternatively, just double-click `config\run_watcher.bat` for a manual session — it handles `watchdog` installation automatically.

---

## Windows Notes

- **Loop prevention** — a `_processing_dirs` set (with a threading lock) suppresses watchdog events fired by the watcher's own rename operations. The lock is updated to track the new path immediately after any top-level rename, so events referencing the renamed path are also suppressed correctly.
- **Concurrent thread guard** — `_on_settled()` checks whether a directory (or any parent/child) is already being processed before spawning a new thread. Prevents duplicate processing when a rename re-triggers the settle timer.
- **Race-condition safe move** — `_move_cbz_dir()` checks whether the source still exists before attempting a move (guards against two threads racing to move the same directory), and catches the Windows race where the destination is created by a concurrent thread between the existence check and `shutil.move()`, falling back to merge in that case.
- **FileExistsError safety** — before calling `Path.rename()`, the watcher checks whether the target exists. On POSIX, rename silently overwrites; on Windows it raises `FileExistsError`. The pre-check produces a clean skip/merge instead of a crash.
- **Destination pre-creation** — all destination directories from `routing.json` are created at startup to avoid first-move delays on UNC shares.

---

## Logging

Rotating log file at `LOG_FILE` (5 MB max, 3 backups). Also streams to stdout. Log entries cover every event, stability check, settle/age wait, rename, tag update, route decision, move, and merge conflict.
