# Phase 3 Aggressive Tool Consolidation

This phase replaces several overlapping maintenance scripts with one organized script:

```text
scripts/cbz_library_maintenance.py
```

It consolidates day-to-day behavior from:

- `cbz_deduplicator.py`
- `strip_duplicates.py`
- `cbz_folder_merger.py`
- `cbz_series_matcher.py`
- `find_uncensored_dupes.py`
- `cbz_number_tagger.py`

The specialized tools below stay separate:

- `cbz_watcher.py`
- `cbz_sanitizer.py`
- `cbz_compilation_resolver.py`
- `cbz_gap_checker.py`

---

## New Commands

### Archive cleanup

```powershell
python scripts\cbz_library_maintenance.py archive-clean "\\tower\media\comics\Comix" --dry-run
```

Handles:

- duplicate filename-token cleanup
- duplicate `.cbz` files
- `.cbr` vs `.cbz` pairs
- loose image folder packing

### Series organization

```powershell
python scripts\cbz_library_maintenance.py organize-series "\\tower\media\comics\Comix" --dry-run
```

Handles:

- split chapter-folder merging
- fuzzy duplicate series folder merging
- optional uncensored/decensored pair review

Add uncensored pair handling:

```powershell
python scripts\cbz_library_maintenance.py organize-series "\\tower\media\comics\Comix" --dry-run --uncensored-check
```

### Metadata repair

```powershell
python scripts\cbz_library_maintenance.py metadata "\\tower\media\comics\Comix" --dry-run
```

Handles:

- ComicInfo title/series/number/volume repair using `cbz_core.py`

### Full maintenance

```powershell
python scripts\cbz_library_maintenance.py all "\\tower\media\comics\Comix" --dry-run
```

Runs archive cleanup, series organization, then metadata repair.

---

## GUI Update

`cbz_gui.py` is updated to show fewer, clearer tools:

- CBZ Sanitizer
- CBZ Watcher
- Archive Cleaner
- Series Organizer
- Metadata Repair
- Full Maintenance
- Compilation Resolver
- Gap Checker

The GUI now supports script subcommands, so multiple buttons can launch the same consolidated script with different modes.

---

## Why this is more aggressive than Phase 2

Phase 2 used facade wrappers that delegated to old scripts.

Phase 3 introduces a real consolidated implementation with shared logic and one maintenance entry point. Old scripts can remain temporarily for rollback, but the GUI points users toward the new consolidated workflow.

---

## Recommended validation

```powershell
python scripts\cbz_library_maintenance.py archive-clean "\\tower\media\comics\Comix" --dry-run
python scripts\cbz_library_maintenance.py organize-series "\\tower\media\comics\Comix" --dry-run
python scripts\cbz_library_maintenance.py metadata "\\tower\media\comics\Comix" --dry-run
python scripts\cbz_gui.py
```

---

## Commit

```powershell
git add scripts\cbz_library_maintenance.py cbz_gui.py docs\phase3_aggressive_consolidation.md
git commit -m "refactor(tools): consolidate maintenance workflows and update GUI"
```
