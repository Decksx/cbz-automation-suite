# CBZ Automation Suite

A collection of Python scripts for monitoring, cleaning, tagging, and routing `.cbz` comic book archives on Windows. Designed to run against a network share (e.g. `\\tower\media\comics\`).

---

## Tools

| Script | Purpose |
|--------|---------|
| `cbz_watcher.py` | Live file system watcher — monitors an Incoming folder, cleans filenames, injects ComicInfo.xml metadata, and routes files to the correct destination |
| `cbz_sanitizer.py` | Batch sanitizer — walks an existing library folder and applies the same cleaning/tagging pipeline without moving files |
| `cbz_folder_merger.py` | Merges directories whose cleaned names collide; keeps the larger file on conflict |
| `cbz_compilation_resolver.py` | Detects multi-chapter compilation files and tags them with correct `<Number>` ranges in ComicInfo.xml |
| `cbz_number_tagger.py` | Lightweight tagger — sets `<Number>` (chapter) and `<Volume>` in ComicInfo.xml based on the filename |

---

## Requirements

- Python 3.8+
- [watchdog](https://pypi.org/project/watchdog/) (watcher only)

```bash
pip install watchdog
```

Or use the provided convenience launcher:

```bash
# Windows — installs dependencies and starts the watcher
start_watcher.bat
```

---

## Quick Start

### 1. Configure the watcher

Edit the constants at the top of `cbz_watcher.py`:

```python
WATCH_FOLDER = r"C:\Comics\Incoming"
LOG_FILE     = r"C:\ComicAutomation\cbz_watcher.log"
DEFAULT_DEST = r"\\tower\media\comics\Comix"

# Only list sources that should route somewhere other than DEFAULT_DEST
SOURCE_ROUTING = {
    "manga-source": r"\\tower\media\comics\Manga",
}
```

### 2. Run the watcher

```bash
python cbz_watcher.py
```

The watcher will:
1. Create all destination folders on startup
2. Clean any stale `.tmp.cbz` / `.bak.cbz` files left by interrupted runs
3. Monitor `WATCH_FOLDER` continuously — processing any `.cbz` that lands there

### 3. Batch-sanitize an existing library

Edit `SCAN_FOLDER` in `cbz_sanitizer.py`, then:

```bash
python cbz_sanitizer.py
```

### 4. Tag chapter/volume numbers across a library

```bash
# Scan all configured folders
python cbz_number_tagger.py

# Scan a specific series folder
python cbz_number_tagger.py "\\tower\media\comics\Comix\Batman"

# Preview without writing
python cbz_number_tagger.py --dry-run
```

---

## How It Works

### Filename & Metadata Cleaning

All tools share a common `sanitize()` pipeline that removes:
- Bracketed group/publisher tags: `[GroupName]`, `(Publisher)`
- CJK / full-width characters
- Website patterns: `www.site.com`, `site.net`
- Scanner credits: words containing `scans`, `scanners` (e.g. `TheGuildScans`)
- Redundant series-name prefixes in filenames

### ComicInfo.xml Handling

- If a `.cbz` has no `ComicInfo.xml`, one is created from a built-in template.
- If a `<Title>` is missing, generic (e.g. `Chapter 12`), or matches a configurable overwrite pattern, it is replaced with a title derived from the cleaned filename.
- `<Series>`, `<Number>`, and `<Volume>` are set automatically from the folder name and filename.
- Archive rewrites preserve the original compression type of every member file — images are never re-compressed.

### Routing (watcher only)

The watcher reads the name of the immediate subfolder inside `WATCH_FOLDER` (the "source folder") and looks it up in `SOURCE_ROUTING`. Anything not listed routes to `DEFAULT_DEST`.

```
Incoming/
├── manga-source/          →  \\tower\media\comics\Manga
│   └── Some Series/
│       └── ch01.cbz
└── unknown-source/        →  \\tower\media\comics\Comix  (default)
    └── Another Series/
        └── ch01.cbz
```

### Conflict Resolution

When a destination folder already exists, `_merge_directories()` merges the incoming folder into it. On any file conflict, the **larger** file is kept.

---

## Logs

All tools write rotating logs (max 5 MB, 2 backups). Configure `LOG_FILE` in each script.

```
C:\ComicAutomation\cbz_watcher.log
C:\ComicAutomation\cbz_sanitizer.log
...
```

---

## File Structure

```
cbz-automation-suite/
├── cbz_watcher.py              # Live watcher (main tool)
├── cbz_sanitizer.py            # Batch sanitizer (canonical shared-function reference)
├── cbz_folder_merger.py        # Folder merge utility
├── cbz_compilation_resolver.py # Compilation range tagger
├── cbz_number_tagger.py        # Chapter/volume number tagger
├── requirements.txt
└── README.md
```

---

## Notes

- **Windows only** — path handling, network shares, and rename behaviour are Windows-specific.
- `cbz_sanitizer.py` is the **canonical reference** for all shared functions. The other tools sync their shared functions from it.
- The `Newest1st_cbz_sanitizer.py` and `Oldest_firstcbz_sanitizer.py` variants process files in reverse-chronological or chronological order respectively — useful for controlling which duplicate is kept.
