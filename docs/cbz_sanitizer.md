# cbz_sanitizer.py

Batch sanitizer. Recursively scans a library folder for `.cbz` files and applies the full cleaning and tagging pipeline in-place: filename normalisation, directory renaming, and `ComicInfo.xml` creation/repair. Does **not** move files — use `cbz_watcher.py` for that.

`cbz_sanitizer.py` is also the **canonical reference** for all shared functions. Other scripts in the suite sync from it.

---

## Configuration

Edit the constants at the top of `scripts\cbz_sanitizer.py`:

```python
SCAN_FOLDER   = r"\\tower\media\comics\Comix"       # folder to scan
LOG_FILE      = r"C:\ComicAutomation\cbz_sanitizer.log"
PROGRESS_FILE = r"C:\ComicAutomation\cbz_sanitizer_progress.json"
```

---

## CLI Usage

```powershell
# Run from the repo root:
cd C:\Users\David.Johnson\ComicAutomation

python scripts\cbz_sanitizer.py                  # scan SCAN_FOLDER, newest-modified dirs first
python scripts\cbz_sanitizer.py --sort=oldest    # oldest-modified dirs first
python scripts\cbz_sanitizer.py --sort=alpha     # alphabetical order
python scripts\cbz_sanitizer.py --resume         # resume an interrupted run
python scripts\cbz_sanitizer.py --restart        # ignore saved progress, start fresh
python scripts\cbz_sanitizer.py --dry-run        # log all planned changes, write nothing
```

All flags can be combined:

```powershell
python scripts\cbz_sanitizer.py --sort=oldest --dry-run
python scripts\cbz_sanitizer.py --resume --sort=alpha
```

---

## Sort Modes

| Mode | Behaviour |
|------|-----------|
| *(default)* | Subdirectories sorted by modification time, **newest first** |
| `--sort=oldest` | Subdirectories sorted by modification time, oldest first |
| `--sort=alpha` | Subdirectories sorted alphabetically |

Sorting applies at the subdirectory level. Files within each subdirectory are always processed in alphabetical order.

---

## Progress & Resume

The progress file (`cbz_sanitizer_progress.json`) uses **append-only JSONL** — one JSON line is written per completed file immediately after processing. This means:

- Interrupting a run (Ctrl-C, power loss, network drop) costs nothing to recover from.
- Resuming skips all already-processed files in O(1) per lookup regardless of library size.
- The progress file is excluded from git.

On the next run, if a progress file exists and no flag is passed, the script interactively prompts:

```
  A progress file was found from a previous run.
  [R] Resume from where it left off
  [S] Start over from the beginning
  Choice (R/S):
```

Pass `--resume` or `--restart` to skip the prompt.

---

## Processing Pipeline

For each `.cbz` file found:

1. **Filename cleaning** — applies `sanitize()` + `normalize_stem()` to the file's stem.
2. **Rename** — renames the `.cbz` file if the cleaned name differs (pre-checks for `FileExistsError` before calling `Path.rename()`).
3. **ComicInfo.xml** — creates one from the built-in template if absent, or reads the existing one.
4. **Tag update** — sets `<Title>`, `<Series>`, `<Number>`, and `<Volume>` from the cleaned filename and directory name.
5. **Archive rewrite** — if any tag or the XML itself changed, rewrites the archive, preserving the original compression type of every image member.
6. **Directory rename** — after all files in a subdirectory are processed, renames the directory itself if its cleaned name differs.

See [shared_pipeline.md](shared_pipeline.md) for the full `sanitize()` step breakdown and `ComicInfo.xml` tag logic.

---

## Logging

Rotating log file at `LOG_FILE` (5 MB max, 3 backups). Also streams to stdout. Log entries include every rename, tag update, skip, and error with timestamps.
