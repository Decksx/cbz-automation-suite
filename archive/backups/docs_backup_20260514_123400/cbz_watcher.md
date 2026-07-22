# cbz_watcher.py

Live file watcher. Monitors an Incoming folder for `.cbz` files, applies the full cleaning/tagging pipeline, and moves each processed directory to its configured destination. Uses `watchdog` for filesystem event monitoring.

---

## Shared-core integration

`cbz_watcher.py` imports shared helpers directly from `scripts/cbz_core.py`:

- `clean_directory_name()`
- `clean_filename()`
- `parse_comic_name()`
- `update_comicinfo_xml()`

A fallback import shim supports both repo-root execution and direct-script execution from inside `scripts/`.

This migration eliminated duplicated regex sets, manual filename-pipeline reconstruction, and local ComicInfo title-selection logic from the watcher.

---

## Processing Pipeline

For each directory that passes the timers:

1. **Top-level directory rename** — cleans the incoming directory name via `clean_directory_name()`.
2. **Stability check** — verifies each `.cbz` file is stable.
3. **Filename parsing** — `parse_comic_name()` from `cbz_core.py` performs the full shared normalization pipeline and returns a structured `ParsedComicName` object.
4. **Rename** — renames each `.cbz` if the parsed filename differs.
5. **ComicInfo.xml** — delegates metadata decisions to `update_comicinfo_xml()` from `cbz_core.py`. Existing XML is updated using `ElementTree`; custom titles are preserved automatically.
6. **Archive rewrite** — rewrites the archive only if XML changed, preserving original compression.
7. **Route & move** — resolves the destination via `routing.json`.
8. **Merge** — if the destination directory already exists, merges file by file; larger file wins.

See [shared_pipeline.md](shared_pipeline.md) and [cbz_core.md](cbz_core.md) for the full normalization and ComicInfo rules.

---

## Configuration

Edit the constants at the top of `scripts\cbz_watcher.py`:

```python
WATCH_FOLDER  = r"C:\Temp\Mega\Mega Uploads\book2"
LOG_FILE      = r"C:\git\ComicAutomation\cbz_watcher.log"
ROUTING_FILE  = r"C:\git\ComicAutomation\routing.json"
POLL_INTERVAL = 2
SETTLE_DELAY  = 5
MIN_AGE       = 300
```

---

## Running

```powershell
python scripts\cbz_watcher.py
```

---

## Routing

Destination routing is driven by `routing.json`, an external config file. A `routing.example.json` template is provided in `config/`.

---

## Windows Notes

- `_processing_dirs` suppresses watchdog events fired by the watcher's own rename operations.
- `_on_settled()` checks whether a directory or any parent/child is already being processed.
- `_move_cbz_dir()` checks whether the source still exists before moving and falls back to merge if the destination appears mid-move.
- Before calling `Path.rename()`, the watcher checks whether the target exists.

---

## Logging

Rotating log file at `LOG_FILE` plus stdout.
