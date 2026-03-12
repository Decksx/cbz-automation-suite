# Other Tools

Secondary scripts for library maintenance. All support `--dry-run` unless noted.

---

## cbz_folder_merger.py

Scans a library root for sibling directories representing the same series split across chapter-numbered folders (e.g. `Batman ch. 1`, `batman ch2`, `Batman Chapter 7`). Renames generically-named files inside each source folder to use the directory name before merging, then consolidates everything into a single clean folder and updates `ComicInfo.xml` tags post-merge.

Works with UNC network shares, local drives, or any valid Windows path — pass the path on the command line or select it interactively at runtime.

**Configuration** (`scripts\cbz_folder_merger.py`):
```python
SCAN_FOLDERS = [
    r"\\tower\media\comics\Comix",
    r"\\tower\media\comics\Manga",
]
LOG_FILE = r"C:\ComicAutomation\cbz_folder_merger.log"
```

**Usage:**
```powershell
python scripts\cbz_folder_merger.py                        # interactive prompt (choose from SCAN_FOLDERS or enter custom path)
python scripts\cbz_folder_merger.py "\\tower\media\Comix"  # UNC network path
python scripts\cbz_folder_merger.py "L:\Comix"             # local drive path
python scripts\cbz_folder_merger.py "C:\Comics\Batman"     # single series directory
python scripts\cbz_folder_merger.py --dry-run              # preview, no changes
python scripts\cbz_folder_merger.py "L:\Comix" --dry-run   # local drive, preview only
```

**Interactive prompt** (when run with no path argument):
```
  No path given. Choose an option:
  [1] \\tower\media\comics\Comix
  [2] \\tower\media\comics\Manga
  [A] All of the above (2 folder(s))
  [C] Enter a custom path (local drive or UNC share)
  Choice:
```

**Conflict resolution:** On any filename collision, the larger file is kept. Applies to both file merges and directory renames.

---

## cbz_compilation_resolver.py

Scans a directory for series where a compilation archive (e.g. `Batman Ch. 1-5.cbz`) overlaps with the matching individual chapter archives. When **all** chapters covered by a compilation are present as individual files:

1. Verifies total page counts match between compilation and individuals.
2. For each page position, selects the higher-quality page:
   - **PNG beats JPEG** regardless of file size
   - Otherwise **larger file size wins**
3. Rewrites the compilation with the best pages from either source.
4. Moves the now-redundant individual archives to `PROCESSED_FOLDER/series_name/`.

Cases where only some individual chapters are present are reported but never acted on automatically.

**Configuration** (`scripts\cbz_compilation_resolver.py`):
```python
LOG_FILE         = r"C:\ComicAutomation\cbz_compilation_resolver.log"
PROCESSED_FOLDER = r"C:\ComicAutomation\Processed"
```

**Usage:**
```powershell
python scripts\cbz_compilation_resolver.py                         # prompts for directory
python scripts\cbz_compilation_resolver.py "C:\Comics\Batman"      # single series
python scripts\cbz_compilation_resolver.py --dry-run               # preview, no changes
python scripts\cbz_compilation_resolver.py "C:\Comics\Batman" --dry-run
```

---

## cbz_number_tagger.py

Retroactively sets `<Number>` and `<Volume>` tags in `ComicInfo.xml` for files that predate the pipeline. Only updates files where a chapter keyword is present in the filename (`ch`, `chapter`, `chp`, `issue`, `#`) — bare title digits are ignored to avoid false positives. Files with no detectable number are skipped and logged.

**Configuration** (`scripts\cbz_number_tagger.py`):
```python
SCAN_FOLDERS = [
    r"\\tower\media\comics\Comix",
    r"\\tower\media\comics\Manga",
]
LOG_FILE = r"C:\ComicAutomation\cbz_number_tagger.log"
```

**Usage:**
```powershell
python scripts\cbz_number_tagger.py                                   # scan all SCAN_FOLDERS
python scripts\cbz_number_tagger.py "\\tower\media\comics\Comix\Batman"  # single series
python scripts\cbz_number_tagger.py --dry-run                         # preview, no changes
```

---

## cbz_series_matcher.py

Detects near-duplicate series folder names caused by punctuation differences, spacing, or romanisation variants. Normalises names (strips punctuation, lowercases) before comparing, so `Batman: Year One` and `Batman Year One` score as identical.

For each matched pair:
- The directory with **more files** is treated as the canonical name.
- If file counts are equal, the **longer name** wins.
- Pairs at or above `AUTO_RENAME_THRESHOLD` (default `0.90`) are auto-merged.
- Pairs between `REPORT_THRESHOLD` (default `0.80`) and `0.90` are flagged in the log for manual review.

**Configuration** (`scripts\cbz_series_matcher.py`):
```python
SCAN_FOLDERS = [
    r"\\tower\media\comics\Comix",
    r"\\tower\media\comics\Manga",
]
LOG_FILE               = r"C:\ComicAutomation\cbz_series_matcher.log"
AUTO_RENAME_THRESHOLD  = 0.90
REPORT_THRESHOLD       = 0.80
```

**Usage:**
```powershell
python scripts\cbz_series_matcher.py             # scan all SCAN_FOLDERS
python scripts\cbz_series_matcher.py --dry-run   # preview, no changes
```

---

## cbz_gap_checker.py

Scans library folders for missing chapter numbers per series and writes a consolidated timestamped CSV report. Treats each immediate subdirectory of a `SCAN_FOLDER` as a series. If a path passed on the command line contains `.cbz` files directly, it is treated as a single series rather than a parent folder.

**Configuration** (`scripts\cbz_gap_checker.py`):
```python
SCAN_FOLDERS = [
    r"\\tower\media\comics\Comix",
    # r"\\tower\media\comics\Manga",   # uncomment to include Manga
]
OUTPUT_FOLDER = r"C:\ComicAutomation"
```

**Usage:**
```powershell
python scripts\cbz_gap_checker.py                                        # scan all SCAN_FOLDERS
python scripts\cbz_gap_checker.py "\\tower\media\comics\Comix\Batman"    # single series
```

Output: `C:\ComicAutomation\cbz_gaps_YYYYMMDD_HHMMSS.csv`

No `--dry-run` needed — this script is read-only and never modifies files.

---

## strip_duplicates.py

Removes duplicate number/label tokens from filenames (e.g. `Batman ver. 9 ver.9` → `Batman ver. 9`) and fixes oddly spaced or repeated punctuation (e.g. `! !` → `!!`, `.. .` → `...`). Also corrects asymmetrically spaced hyphens.

On any rename collision, the larger file is kept; ties keep the existing file.

**Standalone usage:**
```powershell
python scripts\strip_duplicates.py "C:\Comics\Batman"              # rename in place
python scripts\strip_duplicates.py "C:\Comics\Batman" --dry-run    # preview only
python scripts\strip_duplicates.py "C:\Comics" --recursive         # walk all subdirs
python scripts\strip_duplicates.py "C:\Comics" --recursive --dry-run
python scripts\strip_duplicates.py --test                          # run built-in self-tests
```

**Library usage:**
```python
from strip_duplicates import clean

print(clean("Batman ver. 9 ver.9 Wow! !"))
# -> "Batman ver. 9 Wow!!"
```
