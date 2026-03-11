# Other Tools

---

## cbz_folder_merger.py

Scans a library directory for sibling folders whose **cleaned names collide** (i.e. two directories that would normalise to the same name) and merges them. On any file conflict, the larger file is kept.

Also available as `cbz_folder_merger_LDrive.py` — identical logic configured for a local drive path.

```
python cbz_folder_merger.py
python cbz_folder_merger.py "\\tower\media\comics\Comix"
python cbz_folder_merger.py --dry-run
```

---

## cbz_compilation_resolver.py

Detects `.cbz` compilations (e.g. `Batman Ch. 1-5.cbz`) that overlap with individual chapter files in the same directory. For each overlapping page position, it selects the **best available version** using this quality hierarchy:

1. **PNG beats JPEG** — regardless of file size
2. **Larger file size wins** — when format is the same

If at least one page is an upgrade, the compilation is rewritten with the best pages. The individual archives are then moved to `C:\ComicAutomation\Processed\`.

```
python cbz_compilation_resolver.py                    # prompts for directory
python cbz_compilation_resolver.py "C:\Comics\Batman" # specific series
python cbz_compilation_resolver.py --dry-run
```

---

## cbz_number_tagger.py

Retroactively sets `<Number>` (chapter) and `<Volume>` tags in `ComicInfo.xml` across an existing library, using the same extraction logic as the watcher pipeline.

> **Note:** The watcher already tags files on ingest. This tool is for files that predate the watcher pipeline.

```
python cbz_number_tagger.py
python cbz_number_tagger.py "\\tower\media\comics\Comix\Batman"
python cbz_number_tagger.py --dry-run
```

---

## cbz_series_matcher.py

Detects near-duplicate series folder names by normalising names (strips punctuation, lowercases) before comparing. For example, `Batman: Year One` and `Batman Year One` score as identical.

| Similarity | Action |
|------------|--------|
| >= `AUTO_RENAME_THRESHOLD` (default `0.90`) | Auto-merge |
| `0.80` – `0.90` | Flag in log for manual review |
| < `0.80` | Ignored |

```
python cbz_series_matcher.py
python cbz_series_matcher.py --dry-run
```

---

## cbz_gap_checker.py

Scans series directories, parses chapter numbers from filenames, and reports any **missing numbers** in each series. Writes a timestamped CSV to `C:\ComicAutomation\`.

Output filename format: `cbz_gaps_YYYYMMDD_HHMMSS.csv`

```
python cbz_gap_checker.py
python cbz_gap_checker.py "\\tower\media\comics\Comix\Batman"
```

---

## strip_duplicates.py

Removes duplicate number tokens from filenames (e.g. `Batman 12 12.cbz` → `Batman 12.cbz`) and fixes oddly spaced punctuation. Can be used standalone or imported as a library.

```
python strip_duplicates.py "C:\Comics\Batman"
python strip_duplicates.py "C:\Comics" --recursive
python strip_duplicates.py --dry-run
python strip_duplicates.py --test         # run built-in self-tests
```

```python
# As an importable library
from strip_duplicates import clean

clean("Batman 12 12")     # → "Batman 12"
clean("Batman  -  12")    # → "Batman - 12"
```
