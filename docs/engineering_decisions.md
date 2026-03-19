# Engineering Decisions

A record of non-obvious design choices in the suite and the reasoning behind them.

---

## Append-only JSONL progress file

**Decision:** The sanitizer progress file stores one JSON line per completed file path, appended immediately after each file is processed.

**Why:** On a library of 10,000+ files, rewriting the entire progress set after each file would be O(n) per write — compounding overhead as the run progresses. Append-only is O(1) regardless of library size, crash-safe (every file is persisted immediately), and trivially resumable.

---

## Pre-compiled regex at module level

**Decision:** All regex patterns used in tight per-file loops are compiled once at module level rather than inside functions.

**Why:** `re.compile()` has measurable overhead when called thousands of times. Pre-compiling at import time means zero per-call cost during the main processing loop.

---

## Larger file wins on conflict

**Decision:** When two files collide during a merge or move, the larger file is always kept.

**Why:** File size is a reliable proxy for scan quality — higher-resolution scans are almost always larger. This heuristic produces the right result without requiring human input on every conflict, which would make large-library merges impractical.

---

## External routing config (routing.json)

**Decision:** Destination routing is driven by `routing.json` — an external JSON file at `C:\git\ComicAutomation\routing.json` — rather than a hardcoded `SOURCE_ROUTING` dict inside the script.

**Why:** The old dict had 55 entries, all mapping to the same destination. Adding a new source required editing Python. The JSON config separates concern cleanly: `destinations` defines named paths once, `rules` reference those names, and wildcard patterns (e.g. `Toonily*`) eliminate the need for one entry per site variant. A `routing.example.json` template in `config/` gives new users the structure without committing real network paths to the repo.

---

## routing.json excluded from git

**Decision:** `routing.json` is listed in `.gitignore`. Only `routing.example.json` (with placeholder paths) is committed.

**Why:** The live `routing.json` contains real UNC paths specific to the host machine. Committing it would expose network share structure and break for anyone cloning the repo on a different machine. The example template gives new users everything they need to create their own.

---

## _processing_dirs loop prevention

**Decision:** A module-level `_processing_dirs` set (protected by a threading lock) tracks directories currently being processed. The watchdog event handler silently drops any event whose path falls inside a currently-processing directory.

**Why:** The watcher renames `.cbz` files as part of cleaning. Each rename fires a watchdog `on_moved` event. Without suppression, that event re-triggers the settle timer, which re-triggers processing — an infinite loop. The set-based guard is O(1) per event and adds no meaningful overhead.

---

## FileExistsError pre-check on rename

**Decision:** All rename operations check whether the target path exists before calling `Path.rename()`.

**Why:** On POSIX, `rename()` silently overwrites the target. On Windows, it raises `FileExistsError`. This commonly occurs when a 2-way cloud sync delivers both a cleaned and an uncleaned copy of the same file. The pre-check produces a clean skip/warn instead of a crash, and keeps the code explicit about what happens on collision.

---

## Top-level directory name cleaned before file loop

**Decision:** The watcher cleans the top-level incoming directory name at the start of `_process_and_move_directory_inner()`, before iterating over any `.cbz` files.

**Why:** When the file structure is two levels deep (e.g. `Source Dir / Comic Dir / Chapter.cbz`), the inner loop only iterates over `Comic Dir` — `Source Dir` never enters the per-comic cleaning step. Without the top-level pre-clean, source directories with Japanese text and G-codes would land at the destination with those artefacts intact.

---

## Path guard in watchdog handler

**Decision:** The watchdog event handler checks whether an incoming path is inside `WATCH_FOLDER` before queuing it.

**Why:** When the watcher moves a processed directory to a destination, watchdog fires a `moved` event for the destination path. Without the guard, the watcher would re-queue the file it just processed, causing an infinite loop.

---

## cbz_sanitizer.py as the canonical reference

**Decision:** `scripts/cbz_sanitizer.py` is the single source of truth for all shared functions (`sanitize()`, `clean_filename()`, `process_comicinfo()`, etc.). Other scripts sync from it rather than maintaining independent copies.

**Why:** Prevents the shared cleaning logic from drifting across scripts. When a bug is fixed or a new edge case is handled, there is exactly one place to update.

---

## Four sanitizer variants collapsed into one script

**Decision:** `Newest1st_cbz_sanitizer.py`, `Oldest_firstcbz_sanitizer.py`, and `Localcbz_sanitizer.py` were merged into `cbz_sanitizer.py` with `--sort` and `--resume`/`--restart` CLI flags.

**Why:** The three variants were identical except for one line (the `sorted()` key/direction) and the `SCAN_FOLDER` value. Maintaining them separately meant any shared-function fix had to be applied four times. The merged script is strictly better: same capabilities, one place to maintain.

---

## Flat root restructured into scripts/ config/ docs/

**Decision:** All Python scripts moved to `scripts/`, launcher and Task Scheduler XML moved to `config/`, documentation moved to `docs/`.

**Why:** The flat root had 20+ files with no visual grouping — scripts, docs, progress JSONs, and config all jumbled together. The three-folder structure makes the purpose of each file immediately clear and keeps the root to just `README.md`, `requirements.txt`, and `.gitignore`.

---

## Runtime files kept off the repo (partially)

**Decision:** `routing.json` and `*.log` live at `C:\git\ComicAutomation\` on the host machine and are fully excluded via `.gitignore`. Progress JSONs live inside `progress_tracking/` in the repo directory — the folder itself is committed (so it always exists on a fresh clone), but the JSON contents are gitignored.

**Why:** Logs and `routing.json` are machine-specific and change-noisy — no value in tracking them. Progress JSONs are also machine-specific runtime state, but keeping them in a dedicated subfolder rather than the repo root or a separate system path makes it easy to find and clear them without hunting through `C:\git\ComicAutomation\`. The committed empty folder means `os.makedirs` calls are never needed for the progress path on a fresh clone.

---

## Dry-run on all batch tools

**Decision:** Every tool that modifies files supports `--dry-run`, which logs all planned operations without writing anything.

**Why:** Running a new tool or an updated script against a 50,000-file library without a way to preview the changes is high risk. Dry-run makes it safe to validate behaviour on real data before committing.

---

## cbz_number_tagger.py kept separate from the watcher

**Decision:** The number tagger is a standalone script rather than a watcher feature.

**Why:** The watcher already tags `<Number>` and `<Volume>` on ingest via `process_comicinfo()`. The tagger exists solely for retroactive tagging of files that predate the pipeline. Merging it into the watcher would conflate two different use cases.

---

## Regex patterns normalised between sanitizer and watcher

**Decision:** All shared compiled regex patterns between `cbz_sanitizer.py` and `cbz_watcher.py` are byte-for-byte identical. The dead `_CJK_RE` pattern (superseded by `_NON_LATIN_RE`) and the redundant raw-string `TITLE_OVERWRITE_PATTERNS` list (superseded by `_TITLE_OVERWRITE_RES`) were removed from both files.

**Why:** Divergent patterns between the two main scripts is a maintenance hazard — a fix applied to one is silently absent from the other. Normalising them ensures the watcher and sanitizer produce identical output for the same input.

---

## Non-Latin removal scope (step 5 of sanitize())

**Decision:** Step 5 uses `_NON_LATIN_RE`, which preserves Basic Latin, Extended Latin (accented characters), Greek, general punctuation, and emoji — stripping everything else.

**Why:** The original `_CJK_RE` only covered CJK unified ideographs and full-width forms. Comic filenames sourced from aggregator sites contain characters from many scripts (Arabic, Cyrillic, Thai, Devanagari, etc.), not just CJK. A single broad allowlist is simpler, more maintainable, and handles all cases correctly. Emoji and Greek are preserved because they appear legitimately in series titles and special characters.

---

## Concurrent thread safety in the watcher

**Decision:** Three separate guards were added to `cbz_watcher.py` to prevent race conditions when many directories are processed simultaneously:

1. **`_processing_dirs` lock update after rename** — the lock previously discarded `dir_path.parent / dir_path.name` (a no-op, since that equals `dir_path`). It now captures `old_dir_path = dir_path` before the rename and discards that, so watchdog events fired against the renamed path are still suppressed.

2. **`_on_settled()` already-processing guard** — before spawning a new processing thread, checks whether the directory (or any ancestor/descendant) is already in `_processing_dirs`. When a rename re-triggers the settle timer on the new path, this prevents duplicate concurrent processing.

3. **`_move_cbz_dir()` source-gone and race guards** — checks whether the source directory still exists before attempting a move (a second thread may have already moved it), and catches the Windows race where `dest_dir` is created by a concurrent thread between the `exists()` check and `shutil.move()`, falling back to merge rather than raising `WinError 183`.

**Why:** All four error classes observed in production (`WinError 183` on move, `WinError 3` source not found, `Permission denied` on file-in-use, repeat processing of already-moved dirs) traced back to a single root cause: the `_processing_dirs` lock was tracking the pre-rename path while watchdog events arrived using the post-rename path, letting concurrent threads process the same directory simultaneously.

---

## cbz_deduplicator.py — three-pass duplicate resolution

**Decision:** `cbz_deduplicator.py` handles three distinct duplicate classes in a single scan: near-identical `.cbz` filenames (differ only by whitespace/punctuation), `.cbr`/`.cbz` format pairs, and loose image folders that should be packed as `.cbz` archives.

**Why:** All three are library hygiene problems that arise from the same ingest sources. Running them as one pass avoids scanning the same directories multiple times. The three tasks are independent (no shared state) so they compose cleanly. The image-folder conversion (Task 3) specifically targets the case where a cloud sync delivers a folder of images instead of a pre-packed archive — previously these would just sit in the library indefinitely.

---

## _CJK_RE removed from cbz_sanitizer.py

**Decision:** The `_CJK_RE` compiled pattern was removed from `cbz_sanitizer.py`. It is no longer referenced anywhere in the file.

**Why:** `_CJK_RE` was superseded by `_NON_LATIN_RE` (which covers CJK and all other non-Latin scripts in a single broader pattern) but was never removed. Leaving a dead compiled regex at module level is misleading — it implies the pattern is in use when it is not. The sanitizer and watcher now have identical regex sets.

---

## Recursive by default across all batch tools

**Decision:** All batch tools that previously required `--recursive` to descend into subdirectories now run recursively by default. Where an opt-out makes sense (`cbz_deduplicator.py`, `strip_duplicates.py`), `--no-recursive` is provided. Tools with no meaningful single-level mode (`cbz_gap_checker.py`, `cbz_series_matcher.py`, `cbz_compilation_resolver.py`) have no opt-out flag.

**Why:** The library is organised as `\\tower\media\comics\Comix\<Series>\<Chapter>.cbz`. Every tool needs to reach the series/chapter level to do meaningful work. Requiring `--recursive` on every invocation is error-prone — it's easy to forget and silently get incomplete results on a large library. Recursive behaviour is the expected default; single-level is the rare exception and should be the opt-in, not the other way around.

---

## cbz_series_matcher.py — sibling-group recursion

**Decision:** Rather than comparing all directories in a flat list against each other, `cbz_series_matcher.py` collects sibling groups at every nesting level via `_collect_dir_groups()` and runs `find_matches()` independently on each group.

**Why:** Comparing across nesting levels would produce meaningless high-similarity scores between series in completely different parts of the library (e.g. `Comix/Batman` vs `Manga/Batman` would look identical after normalisation, but they are intentionally separate). Grouping by siblings ensures only directories that could plausibly be duplicates of each other — those sharing the same parent — are ever compared. Running recursively means near-duplicate detection catches series nested at any depth, not just the top level of each `SCAN_FOLDER`.

---

## cbz_compilation_resolver.py — SCAN_FOLDERS + _iter_series_dirs()

**Decision:** The compilation resolver was changed from requiring an interactive directory prompt to reading from `SCAN_FOLDERS` by default. A new `_iter_series_dirs()` helper recursively walks from a root, collecting only directories that contain `.cbz` files directly (skipping grouping folders that contain only subdirectories).

**Why:** The old interactive prompt made the script unsuitable for scheduled or automated runs. The library has hundreds of series directories — running the resolver against each one manually is not practical. `_iter_series_dirs()` correctly distinguishes between series-level directories (contain `.cbz` files) and organisational grouping folders (contain only subdirectories), so the resolver processes exactly the right set of directories without false positives or missed series.

---

## cbz_gap_checker.py — recursive scan_folder()

**Decision:** `scan_folder()` in `cbz_gap_checker.py` now recurses into subdirectories automatically. Directories containing `.cbz` files directly are treated as series; directories containing only subdirectories are descended into further.

**Why:** The previous implementation treated each immediate subdirectory of a `SCAN_FOLDER` as a series, which worked for a flat `Comix/Batman/` structure but missed series nested one level deeper (e.g. `Comix/Publisher/Batman/`). The recursive approach handles both layouts without requiring any configuration change.

---

## Parallel processing via --workers N across all batch tools

**Decision:** All batch tools (except `cbz_watcher.py` and `cbz_number_tagger.py`) now support a `--workers N` flag (default: `min(8, cpu_count)`). Each tool parallelises at the most independent grain available. `--workers 1` restores fully serial behaviour.

**Why:** The primary bottleneck in all batch tools is I/O — reading and rewriting zip archives, directory traversal, and file operations. These are largely independent between files and directories, making them well-suited to threading. The GIL is not a significant constraint here because most time is spent waiting on disk I/O rather than CPU-bound computation. The `--workers 1` escape hatch ensures no behaviour regression for users who need deterministic serial output or are debugging edge cases.

Parallelisation grain by tool:
- **cbz_sanitizer.py** — series directory level; files within a series stay serial for rename/collision safety
- **cbz_deduplicator.py** — individual directory level for Tasks 1 and 2; Task 3 (image folder packing) stays serial
- **cbz_gap_checker.py** — series directory level; tree walk is serial, all `scan_series()` calls are parallel
- **cbz_series_matcher.py** — sibling group level; each group is independent with no shared state
- **cbz_compilation_resolver.py** — series directory level; each directory is fully isolated
- **strip_duplicates.py** — directory level; files grouped by parent, each directory batch is a worker
- **cbz_folder_merger.py** — merge group level (outer pool) + per-file ComicInfo update level (inner pool)

---

## cbz_folder_merger.py — two-phase ComicInfo update

**Decision:** `update_comicinfo()` in `cbz_folder_merger.py` was split into two phases. Phase 1 opens the zip and reads only `ComicInfo.xml` to check whether any tag needs changing. If nothing changed, the function returns immediately without touching image data. Phase 2 (full read + rewrite) only executes when an actual change is required.

**Why:** The original implementation read all zip entry data — including every image — into memory before building the new zip, even when the only goal was to update a few bytes of XML. For a typical 20–50 MB chapter archive this is significant wasted I/O. On a merged folder of 200 chapters where most already have correct metadata, the old approach read gigabytes of image data unnecessarily. The two-phase approach makes the common case (no change needed) essentially free.

---

## cbz_folder_merger.py — two-level parallel pool

**Decision:** `process_library()` uses a two-level `ThreadPoolExecutor` structure: an outer pool where each merge group is a worker, and an inner pool within each group for parallelising `update_comicinfo()` calls per file. Workers are split evenly between the two levels.

**Why:** A single flat pool of all files across all groups would mix files from different groups in the same thread pool, which is safe but produces interleaved log output that is hard to read and debug. The two-level structure keeps each group's work coherent, allows the outer pool to process many groups concurrently, and still exploits file-level parallelism within each group for the ComicInfo update pass (which is the most time-consuming step after the file moves).
