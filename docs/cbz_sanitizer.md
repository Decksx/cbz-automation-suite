# cbz_sanitizer.py

Batch sanitizer. Recursively scans a library folder for `.cbz` files and applies the full cleaning and tagging pipeline in-place: filename normalization, directory renaming, and `ComicInfo.xml` creation/repair.

`cbz_sanitizer.py` is also the **canonical reference** for all shared functions. Other scripts in the suite sync from it.

---

## Configuration

Edit the constants at the top of `scripts\cbz_sanitizer.py`:

```python
SCAN_FOLDER   = r"\\tower\media\comics\Comix"       # folder to scan
LOG_FILE      = r"C:\git\ComicAutomation\Logs\cbz_sanitizer.log"
PROGRESS_FILE = r"C:\git\ComicAutomation\progress_tracking\cbz_sanitizer_progress.json"
DEFAULT_WORKERS = min(8, os.cpu_count() or 4)        # override with --workers N
```

---

## CLI Usage

```powershell
# Run from the repo root:
cd C:\git\ComicAutomation

python scripts\cbz_sanitizer.py                             # scan SCAN_FOLDER, newest-modified dirs first
python scripts\cbz_sanitizer.py --sort=oldest               # oldest-modified dirs first
python scripts\cbz_sanitizer.py --sort=alpha                # alphabetical order
python scripts\cbz_sanitizer.py --resume                    # resume an interrupted run
python scripts\cbz_sanitizer.py --restart                   # clear progress file, start fresh
python scripts\cbz_sanitizer.py --dry-run                   # log all planned changes, write nothing
python scripts\cbz_sanitizer.py --workers 4                 # use 4 parallel worker threads
python scripts\cbz_sanitizer.py --workers 1                 # fully serial (original behaviour)
python scripts\cbz_sanitizer.py --rules=leading_nums,trailing_junk   # run specific rules only
python scripts\cbz_sanitizer.py --rules=comicinfo            # only update ComicInfo.xml metadata
```

All flags can be combined:

```powershell
python scripts\cbz_sanitizer.py --sort=oldest --dry-run
python scripts\cbz_sanitizer.py --sort=alpha
python scripts\cbz_sanitizer.py --workers 8 --sort=newest
python scripts\cbz_sanitizer.py --rules=leading_nums,trailing_junk --sort=alpha
```

---

## Sort Modes

| Mode | Behaviour |
|------|-----------|
| *(default)* | Subdirectories sorted by modification time, **newest first** |
| `--sort=oldest` | Subdirectories sorted by modification time, oldest first |
| `--sort=alpha` | Subdirectories sorted alphabetically |
| `--sort=alpha-reverse` | Subdirectories sorted reverse alphabetically |

Sorting applies at the subdirectory level. Files within each subdirectory are always processed in alphabetical order.

---

## Rule Toggles

By default all cleaning rules are active. Pass `--rules=<comma-separated list>` to run only specific rules — useful for targeted passes over a large library.

| Rule | What it does |
|------|--------------|
| `brackets` | Remove `[bracketed]` and `(parenthesised)` blocks from filenames |
| `comicinfo` | Update `ComicInfo.xml` metadata inside each archive |
| `leading_nums` | Strip leading numeric prefixes (`1 - `, `3761755 v1 `) |
| `non_latin` | Remove non-Latin/non-Greek/non-emoji characters |
| `normalize_stem` | Rewrite generic chapter stems (`Ch.5`, `chapter 5`) using the directory name as context |
| `number_tokens` | Normalise chapter/volume number formatting (`Vol.01` → `Vol.1`) |
| `scan_groups` | Strip scanlation group names (`FooScans`, `BarScanlations`) |
| `trailing_junk` | Strip trailing hyphens, dashes (`–`, `—`), and underscores |
| `url` | Strip URLs and domain-like tokens |

Omitting `--rules` entirely runs all rules (default behaviour).

```powershell
# Only fix the two recently-added junk patterns, skip everything else
python scripts\cbz_sanitizer.py --rules=leading_nums,trailing_junk

# Only update ComicInfo.xml, do not rename any files
python scripts\cbz_sanitizer.py --rules=comicinfo

# Strip brackets and scan group names only
python scripts\cbz_sanitizer.py --rules=brackets,scan_groups
```

---

## Parallel Processing

The sanitizer parallelises at the **series directory** level — each series directory is an independent unit of work dispatched to a `ThreadPoolExecutor`. Files within a series are processed serially to preserve rename/collision safety.

- Default workers: `min(8, cpu_count)`
- `--workers 1`: fully serial, identical to original behaviour
- Progress file writes are protected by a `threading.Lock()` — safe at any worker count
- Counters are aggregated from worker return values — no shared mutable state

Expected speedup on a large library: **2–4×** depending on I/O throughput and series count.

---

## Progress & Resume

The progress file (`cbz_sanitizer_progress.json`) uses **persistent append-only JSONL** — one JSON line is written per completed file, and a session-start marker is appended at the beginning of each run. Progress accumulates across sessions, so stopping and restarting always resumes from the full combined history of all previous runs. This means:

- Interrupting a run (Ctrl-C, power loss, network drop) costs nothing to recover from.
- Resuming skips all already-processed files in O(1) per lookup regardless of library size.
- Each startup appends a `{"session": "..."}` header line and then resumes automatically — no prompt.
- The progress file is excluded from git.

Use `--restart` to wipe the progress file entirely and start clean from scratch.

---

## Processing Pipeline

For each `.cbz` file found:

1. **Filename parsing** — `parse_comic_name()` runs the shared normalization pipeline and returns structured filename/chapter/volume metadata.
2. **Rename** — renames the `.cbz` file if the parsed filename differs.
3. **ComicInfo.xml** — creates one from the built-in template if absent, or reads the existing one.
4. **Tag update** — delegates metadata decisions to `update_comicinfo_xml()` from `cbz_core.py`, preserving custom titles while normalizing generic metadata.
5. **Archive rewrite** — if any tag or XML changed, rewrites the archive while preserving the original compression type.
6. **Directory rename** — after all files in a subdirectory are processed, renames the directory itself if its cleaned name differs.

See [shared_pipeline.md](shared_pipeline.md) and [cbz_core.md](cbz_core.md).

---

## CLI Usage

Rotating log file at `Logs\cbz_sanitizer.log` (5 MB max, 3 backups). Also streams to stdout. Log entries include every rename, tag update, skip, and error with timestamps. Thread-safe — Python's `logging` module uses internal locks.
