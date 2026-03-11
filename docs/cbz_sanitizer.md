# cbz_sanitizer.py — Batch Sanitizer

The canonical script for batch sanitizing an existing library. Walks all subdirectories and applies the full filename-cleaning and ComicInfo.xml tagging pipeline **in place** — nothing is moved or transferred.

This is also the **canonical reference implementation** for all shared functions. Other tools sync their shared logic from this script.

---

## Configuration

Edit the constants at the top of the file:

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

```
python cbz_sanitizer.py                              # scan SCAN_FOLDERS, newest dirs first
python cbz_sanitizer.py "L:\Comix"                   # scan a specific path
python cbz_sanitizer.py "\\tower\media\comics\Comix"  # scan a UNC path

python cbz_sanitizer.py --sort=newest                # most recently modified first (default)
python cbz_sanitizer.py --sort=oldest                # oldest modified first
python cbz_sanitizer.py --sort=alpha                 # alphabetical

python cbz_sanitizer.py --resume                     # resume from saved progress
python cbz_sanitizer.py --restart                    # ignore saved progress, start fresh
python cbz_sanitizer.py --dry-run                    # preview changes without writing

# Flags combine freely
python cbz_sanitizer.py "L:\Comix" --sort=oldest --dry-run
```

### Positional path argument

Passing a path overrides `SCAN_FOLDERS` entirely for that run. Useful for targeting a single series or a local drive without editing the config.

---

## Sort Modes

| Mode | Order | Use case |
|------|-------|---------|
| `newest` | Most recently modified dirs first | Catch up after a batch import — processes new additions before the backlog |
| `oldest` | Oldest modified dirs first | Work through the archive chronologically |
| `alpha` | Alphabetical by directory name | Deterministic; easy to audit or compare runs |

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

See [Shared Pipeline](shared_pipeline.md) for details on the cleaning and ComicInfo logic.
