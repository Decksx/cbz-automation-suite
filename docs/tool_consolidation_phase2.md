# Phase 2 Tool Consolidation

This phase reduces day-to-day entry points while preserving the existing mature scripts.

The old scripts are not deleted. These new facade scripts delegate to them:

```text
scripts/cbz_archive_cleaner.py
scripts/cbz_library_organizer.py
scripts/cbz_metadata_tools.py
```

## cbz_archive_cleaner.py

Archive/file-level cleanup.

Wraps:

- `cbz_deduplicator.py`
- `strip_duplicates.py`

Examples:

```powershell
python scripts\cbz_archive_cleaner.py dedupe --dry-run
python scripts\cbz_archive_cleaner.py strip "C:\Comics" --dry-run
python scripts\cbz_archive_cleaner.py clean-all "\\tower\media\comics\Comix" --dry-run
```

Use this for duplicate `.cbz` cleanup, `.cbr`/`.cbz` pairs, loose image folder packing, and duplicate filename-token cleanup.

## cbz_library_organizer.py

Series/directory-level cleanup.

Wraps:

- `cbz_folder_merger.py`
- `cbz_series_matcher.py`
- `find_uncensored_dupes.py`

Examples:

```powershell
python scripts\cbz_library_organizer.py match-series --dry-run
python scripts\cbz_library_organizer.py merge-folders "\\tower\media\comics\Comix" --dry-run
python scripts\cbz_library_organizer.py find-uncensored --library "\\tower\media\comics\Comix"
python scripts\cbz_library_organizer.py organize-all --dry-run
```

Use this for fuzzy duplicate series folder detection, split-folder merging, and censored/uncensored pair review.

## cbz_metadata_tools.py

Retroactive metadata repair.

Wraps:

- `cbz_number_tagger.py`

Examples:

```powershell
python scripts\cbz_metadata_tools.py number-tags --dry-run
python scripts\cbz_metadata_tools.py number-tags "\\tower\media\comics\Comix\Batman"
```

## Why keep cbz_compilation_resolver.py separate?

`cbz_compilation_resolver.py` rewrites archive page contents by comparing compilation archives against individual chapters. That is riskier and more specialized than ordinary folder/file consolidation, so it should remain a standalone tool for now.

## Migration plan

1. Add the three facade scripts.
2. Keep the old scripts in place.
3. Update docs to recommend the new facade entry points.
4. Use facades for daily operation.
5. Gradually extract duplicated internals into shared modules.
6. Deprecate old direct entry points only after the facades are proven.

## Suggested commit

```powershell
git add scripts\cbz_archive_cleaner.py scripts\cbz_library_organizer.py scripts\cbz_metadata_tools.py docs\tool_consolidation_phase2.md
git commit -m "refactor(tools): add consolidated facades for archive, library, and metadata workflows"
```
