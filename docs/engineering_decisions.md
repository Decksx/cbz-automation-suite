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

**Decision:** When two files collide during a merge, the larger file is always kept.

**Why:** File size is a reliable proxy for scan quality — higher-resolution scans are almost always larger. This heuristic produces the right result without requiring human input on every conflict, which would make large-library merges impractical.

---

## Path guard in watchdog handler

**Decision:** The watchdog event handler checks whether an incoming path is inside `WATCH_FOLDER` before queuing it.

**Why:** When the watcher moves a processed directory to `DEFAULT_DEST`, watchdog fires a `moved` event for the destination path. Without the guard, the watcher would re-queue the file it just processed, causing an infinite loop.

---

## FileExistsError pre-check on rename

**Decision:** All rename operations check whether the target path exists before calling `Path.rename()`.

**Why:** On POSIX, `rename()` silently overwrites the target. On Windows, it raises `FileExistsError`. The pre-check enables clean skip/merge behaviour instead of a crash, and keeps the code explicit about what happens on collision.

---

## cbz_sanitizer.py as the canonical reference

**Decision:** `scripts/cbz_sanitizer.py` is the single source of truth for all shared functions (`sanitize()`, `clean_filename()`, `process_comicinfo()`, etc.). Other scripts sync from it rather than maintaining independent copies.

**Why:** Prevents the shared cleaning logic from drifting across scripts. When a bug is fixed or a new edge case is handled, there is exactly one place to update.

---

## Four sanitizer variants collapsed into one script

**Decision:** `Newest1st_cbz_sanitizer.py`, `Oldest_firstcbz_sanitizer.py`, and `Localcbz_sanitizer.py` were merged into `cbz_sanitizer.py` with `--sort` and path CLI flags.

**Why:** The three variants were identical except for one line (the `sorted()` key/direction) and the `SCAN_FOLDER` value. Maintaining them separately meant any shared-function fix had to be applied four times. The merged script is strictly better: same capabilities, one place to maintain.

---

## Flat root restructured into scripts/ config/ docs/

**Decision:** All Python scripts moved to `scripts/`, launcher and Task Scheduler XML moved to `config/`, documentation moved to `docs/`.

**Why:** The flat root had 20+ files with no visual grouping — scripts, docs, progress JSONs, and config all jumbled together. The three-folder structure makes the purpose of each file immediately clear and keeps the root to just `README.md`, `requirements.txt`, and `.gitignore`.

---

## Dry-run on all batch tools

**Decision:** Every tool that modifies files supports `--dry-run`, which logs all planned operations without writing anything.

**Why:** Running a new tool or an updated script against a 50,000-file library without a way to preview the changes is high risk. Dry-run makes it safe to validate behaviour on real data before committing.

---

## cbz_number_tagger.py kept separate from the watcher

**Decision:** The number tagger is a standalone script rather than a watcher feature.

**Why:** The watcher already tags `<Number>` and `<Volume>` on ingest via `process_comicinfo()`. The tagger exists solely for retroactive tagging of files that predate the pipeline. Merging it into the watcher would conflate two different use cases.
