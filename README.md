# CBZ Automation Suite

A collection of Python scripts for monitoring, cleaning, tagging, and routing `.cbz` comic book archives on Windows. Designed to work against a network share (e.g. `\\tower\media\comics\`) or a local drive.

---

## Tools

| Script | Purpose |
|--------|---------|
| `cbz_watcher.py` | Live watcher — monitors an Incoming folder, cleans filenames, injects ComicInfo.xml metadata, and routes files to the correct destination |
| `cbz_sanitizer.py` | Batch sanitizer — walks an existing network share library and applies the same cleaning/tagging pipeline in-place |
| `Localcbz_sanitizer.py` | Resumable sanitizer — same as above but for local drives; tracks progress to a JSON file so large runs can be interrupted and resumed |
| `Newest1st_cbz_sanitizer.py` | Sanitizer variant — processes files newest-first within each directory |
| `Oldest_firstcbz_sanitizer.py` | Sanitizer variant — processes files oldest-first within each directory |
| `cbz_folder_merger.py` | Merges directories whose cleaned names collide; keeps the larger file on any conflict |
| `cbz_compilation_resolver.py` | Detects compilation/individual chapter overlaps; performs page-by-page quality comparison and rewrites compilations with the best pages |
| `cbz_number_tagger.py` | Sets `<Number>` (chapter) and `<Volume>` tags in ComicInfo.xml based on the filename |
| `cbz_series_matcher.py` | Finds near-duplicate series folder names and auto-merges above a configurable similarity threshold |
| `cbz_gap_checker.py` | Scans library folders and writes a CSV report of missing chapter numbers per series |
| `strip_duplicates.py` | Removes duplicate number tokens and fixes oddly spaced punctuation in filenames |
| `run_watcher.bat` | Windows launcher — installs `watchdog` and starts `cbz_watcher.py` |

---

## Requirements

- Python 3.8+
- [watchdog](https://pypi.org/project/watchdog/) — required by `cbz_watcher.py` only

All other scripts use the Python standard library exclusively (`zipfile`, `re`, `pathlib`, `logging`, `difflib`, `csv`, `json`, etc.).

```bash
pip install watchdog
```

Or use the provided launcher which handles this automatically:

```bat
run_watcher.bat
```

---

## Quick Start

### Live Watcher

Edit the constants at the top of `cbz_watcher.py`:

```python
WATCH_FOLDER = r"C:\Comics\Incoming"
LOG_FILE     = r"C:\ComicAutomation\cbz_watcher.log"
DEFAULT_DEST = r"\\tower\media\comics\Comix"

# Only list sources that should route somewhere OTHER than DEFAULT_DEST
SOURCE_ROUTING = {
    "manga-source": r"\\tower\media\comics\Manga",
}
```

Then run:

```bash
python cbz_watcher.py
# or double-click run_watcher.bat
```

### Batch Sanitize a Network Share

Edit `SCAN_FOLDER` in `cbz_sanitizer.py`, then:

```bash
python cbz_sanitizer.py
```

### Batch Sanitize a Local Drive (Resumable)

Edit `SCAN_FOLDER` in `Localcbz_sanitizer.py` to point at a local path (e.g. `L:\Comix`), then:

```bash
python Localcbz_sanitizer.py           # start or resume
python Localcbz_sanitizer.py --restart # ignore saved progress, start fresh
```

### Tag Chapter / Volume Numbers

```bash
python cbz_number_tagger.py                                      # all configured folders
python cbz_number_tagger.py "\\tower\media\comics\Comix\Batman"  # single series
python cbz_number_tagger.py --dry-run                            # preview only
```

### Find Near-Duplicate Series Names

```bash
python cbz_series_matcher.py           # scan all configured folders
python cbz_series_matcher.py --dry-run # preview merges without writing
```

### Check for Missing Chapters

```bash
python cbz_gap_checker.py                                        # all configured folders
python cbz_gap_checker.py "\\tower\media\comics\Comix\Batman"    # single series
```

Outputs a timestamped CSV to `C:\ComicAutomation\cbz_gaps_YYYYMMDD_HHMMSS.csv`.

### Resolve Compilation / Individual Overlaps

```bash
python cbz_compilation_resolver.py                        # prompts for directory
python cbz_compilation_resolver.py "C:\Comics\Batman"     # single series
python cbz_compilation_resolver.py --dry-run              # preview only
```

### Clean Duplicate Number Tokens in Filenames

```bash
python strip_duplicates.py "C:\Comics\Batman"             # rename in place
python strip_duplicates.py "C:\Comics\Batman" --dry-run   # preview
python strip_duplicates.py "C:\Comics" --recursive        # walk subdirs
python strip_duplicates.py --test                         # run self-tests
```

---

## How It Works

### Filename & Metadata Cleaning

All tools share a common `sanitize()` pipeline that removes:
- Bracketed group/publisher tags: `[GroupName]`, `(Publisher)`
- CJK / full-width characters
- Website patterns: `www.site.com`, `site.net`
- Scanner/group credits: words containing `scans` or `scanners` (e.g. `TheGuildScans`)
- Redundant series-name prefixes in filenames

### ComicInfo.xml Handling

- If a `.cbz` has no `ComicInfo.xml`, one is created from a built-in template.
- `<Title>` is replaced if it is missing, generic (e.g. `Chapter 12`), or matches a configurable overwrite pattern.
- `<Series>`, `<Number>`, and `<Volume>` are set automatically from the folder name and filename.
- Archive rewrites preserve the original compression type of every member — images are never re-compressed.

### Routing (watcher only)

The watcher reads the name of the immediate subfolder inside `WATCH_FOLDER` and looks it up in `SOURCE_ROUTING`. Anything not listed routes to `DEFAULT_DEST`.

```
Incoming/
├── manga-source/          →  \\tower\media\comics\Manga
│   └── Some Series/
│       └── ch01.cbz
└── any-other-source/      →  \\tower\media\comics\Comix  (default)
    └── Another Series/
        └── ch01.cbz
```

### Conflict Resolution

When a destination folder already exists, `_merge_directories()` merges the incoming folder into it. On any file conflict, the **larger** file is kept.

### Compilation Quality Resolution

`cbz_compilation_resolver.py` compares pages between a compilation (e.g. `Batman Ch. 1-5.cbz`) and the matching individual chapter files. For each page position:
- **PNG beats JPEG** regardless of file size
- Otherwise **larger file size wins**

If all individual chapters are present and at least one page is an upgrade, the compilation is rewritten with the best pages and the individual archives are moved to `C:\ComicAutomation\Processed\`.

### Series Deduplication

`cbz_series_matcher.py` normalises folder names (strips punctuation, lowercases) before comparing, so `Batman: Year One` and `Batman Year One` score as identical. Pairs at or above `AUTO_RENAME_THRESHOLD` (default `0.90`) are auto-merged; pairs between `0.80` and `0.90` are flagged in the log for manual review.

---

## Logs

All tools write rotating logs (max 5 MB, 3 backups). Configure `LOG_FILE` in each script.

```
C:\ComicAutomation\cbz_watcher.log
C:\ComicAutomation\cbz_sanitizer.log
C:\ComicAutomation\cbz_compilation_resolver.log
C:\ComicAutomation\cbz_series_matcher.log
C:\ComicAutomation\cbz_number_tagger.log
C:\ComicAutomation\cbz_gap_checker.log
C:\ComicAutomation\strip_duplicates.log
```

---

## File Structure

```
cbz-automation-suite/
├── cbz_watcher.py                  # Live watcher (main tool)
├── cbz_sanitizer.py                # Batch sanitizer — canonical shared-function reference
├── Localcbz_sanitizer.py           # Resumable local-drive sanitizer
├── Newest1st_cbz_sanitizer.py      # Sanitizer — newest files first
├── Oldest_firstcbz_sanitizer.py    # Sanitizer — oldest files first
├── cbz_folder_merger.py            # Folder merge utility
├── cbz_compilation_resolver.py     # Compilation page-quality optimizer
├── cbz_number_tagger.py            # Chapter/volume number tagger
├── cbz_series_matcher.py           # Near-duplicate series name detector
├── cbz_gap_checker.py              # Missing chapter gap reporter
├── strip_duplicates.py             # Duplicate number / spacing cleaner
├── run_watcher.bat                 # Windows convenience launcher
├── requirements.txt                # watchdog only
└── README.md
```

---

## Notes

- **Windows only** — path handling, network shares, and rename behaviour are Windows-specific.
- `cbz_sanitizer.py` is the **canonical reference** for all shared functions. Other tools sync their shared functions from it.
- The progress file used by `Localcbz_sanitizer.py` is append-only — one JSON line per completed file — so interrupting a 10,000-file run costs almost nothing to resume.
- `strip_duplicates.py` can also be used as an importable library: `from strip_duplicates import clean`.
