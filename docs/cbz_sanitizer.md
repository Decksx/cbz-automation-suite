# cbz_sanitizer.py — Batch Sanitizer

The canonical script for batch sanitizing an existing library. Walks all subdirectories and applies the full filename-cleaning and ComicInfo.xml tagging pipeline **in place** — nothing is moved or transferred.

This is also the **canonical reference implementation** for all shared functions. Other tools sync their shared logic from this script.

---

## Running

From the repo root:

```powershell
python scripts\cbz_sanitizer.py [path] [flags]
```

Or from inside `scripts\`:

```powershell
python cbz_sanitizer.py [path] [flags]
```

---

## Configuration

Edit the constants at the top of `scripts\cbz_sanitizer.py`:

```python
# Default folder(s) to scan when no path is given on the command line.
# Can be a single string or a list.
SCAN_FOLDERS: list = [
    r"\\tower\media\comics\Comix",
    r"\\tower\media\comics\Manga",
]

LOG_FILE      = r"C:\ComicAutomation\cbz_sanitizer.log"
PROGRESS_FILE = r"C:\ComicAutomation\cbz_sanitizer_progress.json"

# Default sort order: newest | oldest | alpha
DEFAULT_SORT  = "newest"
```

---

## CLI Usage

```powershell
# Scan all configured SCAN_FOLDERS, newest-modified dirs first
python scripts\cbz_sanitizer.py

# Scan a specific path (overrides SCAN_FOLDERS)
python scripts\cbz_sanitizer.py "L:\Comix"
python scripts\cbz_sanitizer.py "\\tower\media\comics\Comix"

# Sort order
python scripts\cbz_sanitizer.py --sort=newest   # most recently modified first (default)
python scripts\cbz_sanitizer.py --sort=oldest   # oldest modified first
python scripts\cbz_sanitizer.py --sort=alpha    # alphabetical

# Resume / restart
python scripts\cbz_sanitizer.py --resume        # resume from saved progress
python scripts\cbz_sanitizer.py --restart       # ignore saved progress, start fresh

# Dry run
python scripts\cbz_sanitizer.py --dry-run       # preview all changes without writing

# Combine freely
python scripts\cbz_sanitizer.py "L:\Comix" --sort=oldest --dry-run
```

---

## Sort Modes

| Mode | Order | Use case |
|------|-------|---------|
| `newest` | Most recently modified dirs first | Catch up after a batch import |
| `oldest` | Oldest modified dirs first | Work through the archive chronologically |
| `alpha` | Alphabetical by directory name | Deterministic; easy to audit |

---

## Progress Tracking

Progress is stored in an append-only JSONL file — one line per completed file path.

- **O(1) write cost** — no full rewrite as the processed set grows.
- **Crash-safe** — every file is persisted immediately after processing.
- **Resumable** — `--resume` loads the file and skips already-processed paths.
- The progress file is excluded from git via `.gitignore` (it's machine-local).

---

## Processing Pipeline

For each subdirectory under the scan target:

1. Clean the directory name via `clean_directory_name()`
2. If the cleaned name collides with an existing directory, merge via `_merge_directories()`
3. Pre-compute fallback names for any files whose cleaned stem would be empty
4. For each `.cbz` file:
   - `clean_filename()` — strip junk from the filename
   - `normalize_stem()` — strip series-name prefix, fix generic stems
   - `normalise_number_tokens()` — deduplicate chapter number tokens
   - Rename the file on disk
5. `process_comicinfo()` — read or create `ComicInfo.xml`; set `<Title>`, `<Series>`, `<Number>`, `<Volume>`
6. `detect_and_fix_compilations()` — detect and fix compilation-range chapters within the directory

See [shared_pipeline.md](shared_pipeline.md) for details on the cleaning and ComicInfo logic.
