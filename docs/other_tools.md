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
LOG_FILE = r"C:\git\ComicAutomation\cbz_folder_merger.log"
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

Scans all series directories under `SCAN_FOLDERS` for cases where a compilation archive (e.g. `Batman Ch. 1-5.cbz`) overlaps with the matching individual chapter archives. Runs **recursively by default** — every subdirectory containing `.cbz` files directly is treated as a series and checked automatically.

When **all** chapters covered by a compilation are present as individual files:

1. Verifies total page counts match between compilation and individuals.
2. For each page position, selects the higher-quality page:
   - **PNG beats JPEG** regardless of file size
   - Otherwise **larger file size wins**
3. Rewrites the compilation with the best pages from either source.
4. Moves the now-redundant individual archives to `PROCESSED_FOLDER/series_name/`.

Cases where only some individual chapters are present are reported but never acted on automatically.

**Configuration** (`scripts\cbz_compilation_resolver.py`):
```python
SCAN_FOLDERS = [
    r"\\tower\media\comics\Comix",
    r"\\tower\media\comics\Manga",
]
LOG_FILE         = r"C:\git\ComicAutomation\cbz_compilation_resolver.log"
PROCESSED_FOLDER = r"C:\git\ComicAutomation\Processed"
```

**Usage:**
```powershell
python scripts\cbz_compilation_resolver.py                         # scan all SCAN_FOLDERS (recursive)
python scripts\cbz_compilation_resolver.py "C:\Comics\Batman"      # single series
python scripts\cbz_compilation_resolver.py --dry-run               # preview, no changes
python scripts\cbz_compilation_resolver.py "C:\Comics\Batman" --dry-run
```

> Previously this script required an interactive directory prompt or a single path argument. It now reads from `SCAN_FOLDERS` by default, matching the behaviour of the other tools.

---

## cbz_number_tagger.py

Retroactively sets `<Number>` and `<Volume>` tags in `ComicInfo.xml` for files that predate the pipeline. Only updates files where a chapter keyword is present in the filename (`ch`, `chapter`, `chp`, `issue`, `#`) — bare title digits are ignored to avoid false positives. Files with no detectable number are skipped and logged. Scans **recursively** via `rglob`.

**Configuration** (`scripts\cbz_number_tagger.py`):
```python
SCAN_FOLDERS = [
    r"\\tower\media\comics\Comix",
    r"\\tower\media\comics\Manga",
]
LOG_FILE = r"C:\git\ComicAutomation\cbz_number_tagger.log"
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

Runs **recursively** — sibling directories at every nesting level under each `SCAN_FOLDER` are compared against each other. Grouping folders (publisher subdirectories, genre buckets, etc.) are descended into automatically so near-duplicate series nested at any depth are caught.

For each matched pair:
- The directory with **more files** is treated as the canonical name.
- If file counts are equal, the **longer name** wins.
- Pairs at or above `AUTO_RENAME_THRESHOLD` (default `0.90`) are auto-merged.
- Pairs between `REPORT_THRESHOLD` (default `0.80`) and `0.90` are flagged in the log for manual review.
- Each sibling group is labelled with its parent folder name in the log output for easy navigation.

**Configuration** (`scripts\cbz_series_matcher.py`):
```python
SCAN_FOLDERS = [
    r"\\tower\media\comics\Comix",
    r"\\tower\media\comics\Manga",
]
LOG_FILE               = r"C:\git\ComicAutomation\cbz_series_matcher.log"
AUTO_RENAME_THRESHOLD  = 0.90
REPORT_THRESHOLD       = 0.80
```

**Usage:**
```powershell
python scripts\cbz_series_matcher.py             # scan all SCAN_FOLDERS (recursive)
python scripts\cbz_series_matcher.py --dry-run   # preview, no changes
```

---

## cbz_gap_checker.py

Scans library folders for missing chapter numbers per series and writes a consolidated timestamped CSV report. Runs **recursively by default** — directories containing `.cbz` files directly are treated as series; directories containing only subdirectories are descended into further, so you can point it at any level of your library hierarchy.

**Configuration** (`scripts\cbz_gap_checker.py`):
```python
SCAN_FOLDERS = [
    r"\\tower\media\comics\Comix",
    # r"\\tower\media\comics\Manga",   # uncomment to include Manga
]
OUTPUT_FOLDER  = r"C:\git\ComicAutomation"
GAP_THRESHOLD  = 1    # minimum jump to count as a gap
MIN_ISSUES_TO_REPORT = 2  # skip series with fewer numbered issues than this
```

**Usage:**
```powershell
python scripts\cbz_gap_checker.py                                        # scan all SCAN_FOLDERS
python scripts\cbz_gap_checker.py "\\tower\media\comics\Comix\Batman"    # single series
```

Output: `C:\git\ComicAutomation\cbz_gaps_YYYYMMDD_HHMMSS.csv`

No `--dry-run` needed — this script is read-only and never modifies files.

---

## strip_duplicates.py

Removes duplicate number/label tokens from filenames (e.g. `Batman ver. 9 ver.9` → `Batman ver. 9`) and fixes oddly spaced or repeated punctuation (e.g. `! !` → `!!`, `.. .` → `...`). Also corrects asymmetrically spaced hyphens. Runs **recursively by default**; use `--no-recursive` to limit to a single directory.

On any rename collision, the larger file is kept; ties keep the existing file.

**Standalone usage:**
```powershell
python scripts\strip_duplicates.py "C:\Comics\Batman"                 # rename in place (recursive)
python scripts\strip_duplicates.py "C:\Comics\Batman" --dry-run       # preview only
python scripts\strip_duplicates.py "C:\Comics" --no-recursive         # single directory only
python scripts\strip_duplicates.py "C:\Comics" --no-recursive --dry-run
python scripts\strip_duplicates.py --test                             # run built-in self-tests
```

**Library usage:**
```python
from strip_duplicates import clean

print(clean("Batman ver. 9 ver.9 Wow! !"))
# -> "Batman ver. 9 Wow!!"
```

---

## cbz_deduplicator.py

Scans one or more library folders for three classes of duplicate or fixable files and resolves them in a single pass. Runs **recursively by default**; use `--no-recursive` to limit duplicate checks to a single level. Loose-image-folder conversion always targets immediate subdirectories only, regardless of the recursion flag.

**Task 1 — Duplicate .cbz files:** Groups `.cbz` files within each directory by their normalised stem (whitespace, hyphens, underscores, and punctuation stripped). When two or more files normalise to the same key (e.g. `Batman - Ch. 12.cbz` and `Batman Ch.12.cbz`), the largest file is kept and the rest deleted. Ties go to the alphabetically-first name.

**Task 2 — CBR vs CBZ pairs:** When the same normalised stem exists as both `.cbr` and `.cbz`, the `.cbr` is always deleted. The `.cbz` is always kept regardless of file size.

**Task 3 — Loose image folders → CBZ:** Any immediate subdirectory containing only image files (jpg, jpeg, png, gif, webp, avif, bmp, tiff) plus optionally a single `ComicInfo.xml` is packed into a `.cbz` archive placed next to the folder. `ComicInfo.xml` is placed first in the archive; images follow in natural sort order. The source folder is removed after successful packing.

**Configuration** (`scripts\cbz_deduplicator.py`):
```python
SCAN_FOLDERS = [
    r"\\tower\media\comics\Comix",
    r"\\tower\media\comics\Manga",
]
LOG_FILE = r"C:\git\ComicAutomation\cbz_deduplicator.log"
```

**Usage:**
```powershell
python scripts\cbz_deduplicator.py                          # scan all SCAN_FOLDERS (recursive)
python scripts\cbz_deduplicator.py "\\tower\media\Comix"    # one folder (recursive)
python scripts\cbz_deduplicator.py --dry-run                # preview only, no changes
python scripts\cbz_deduplicator.py --no-recursive           # single directory level only
```

**Conflict resolution:** Task 1 keeps the largest file. Task 2 always keeps `.cbz`. Task 3 skips packing if a `.cbz` with the same name already exists next to the folder.
