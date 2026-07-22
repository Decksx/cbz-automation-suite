# Other Tools

## Unified Workflows

Use `cbz_workflows.py` as the primary entry point for related operations:

```powershell
python scripts\cbz_workflows.py maintenance ROOT --dry-run
python scripts\cbz_workflows.py maintenance ROOT --stages=sanitize,archive,metadata,names
python scripts\cbz_workflows.py series ROOT --dry-run --stages=organize,stage,review,compilations
```

`maintenance` consolidates CBZ Sanitizer, Archive Cleaner, Series Organizer,
Metadata Repair, Repair Names, and the former Full Maintenance preset.

`series` consolidates Series Organizer, Stage Similar Series, Series Review,
and Compilation Resolver. The review stage writes `Logs/series_proposal.json`,
which the GUI opens in its existing interactive review window.

The older subcommands below remain compatibility entry points.

Secondary and library-wide workflows now live in the consolidated maintenance entrypoint:

```powershell
python scripts\cbz_library_maintenance.py --help
```

The former standalone maintenance scripts were folded into this tool and removed:

- `cbz_deduplicator.py`
- `strip_duplicates.py`
- `cbz_folder_merger.py`
- `cbz_series_matcher.py`
- `find_uncensored_dupes.py`
- `cbz_number_tagger.py`

## cbz_library_maintenance.py

### Archive Cleanup

Removes duplicate `.cbz`/`.cbr` archives, strips duplicate number tokens from filenames, and packs loose image folders into `.cbz` archives.

```powershell
python scripts\cbz_library_maintenance.py archive-clean "\\tower\media\comics\Comix" --dry-run
python scripts\cbz_library_maintenance.py archive-clean "\\tower\media\comics\Comix" --workers 8
python scripts\cbz_library_maintenance.py archive-clean "\\tower\media\comics\Comix" --no-recursive
```

### Series Organization

Merges split chapter folders, auto-merges near-duplicate series names, renames generic archive names during merges, updates merged ComicInfo metadata, fixes likely compilation number ranges, and can move review candidates into `_Check/`.

```powershell
python scripts\cbz_library_maintenance.py organize-series "\\tower\media\comics\Comix" --dry-run
python scripts\cbz_library_maintenance.py organize-series "\\tower\media\comics\Comix" --uncensored-check --move-which both
python scripts\cbz_library_maintenance.py organize-series "\\tower\media\comics\Comix" --possible-series-check
python scripts\cbz_library_maintenance.py organize-series "\\tower\media\comics\Comix" --recursive-parents
```

Useful options:

- `--report-threshold`: minimum similarity to report a possible series match.
- `--auto-threshold`: minimum similarity to auto-merge a series match.
- `--uncensored-check`: move censored/uncensored duplicate folder pairs to `_Check/`.
- `--move-which censored|uncensored|both`: choose which side of an uncensored pair to move.
- `--possible-series-check`: group likely same-series folders for manual review.
- `--series-common-words`: minimum fuzzy common-prefix words for possible-series groups.
- `--series-min-group-size`: minimum folders required to create a possible-series group.

### Metadata Repair

Repairs `ComicInfo.xml` title, series, number, and volume tags from the archive filename and containing folder.

```powershell
python scripts\cbz_library_maintenance.py metadata "\\tower\media\comics\Comix" --dry-run
python scripts\cbz_library_maintenance.py metadata "\\tower\media\comics\Comix" --workers 8
```

### Full Maintenance

Runs archive cleanup, series organization, and metadata repair in sequence.

```powershell
python scripts\cbz_library_maintenance.py all "\\tower\media\comics\Comix" --dry-run
python scripts\cbz_library_maintenance.py all "\\tower\media\comics\Comix" --workers 8
```

## cbz_compilation_resolver.py

The compilation resolver remains separate because it does page-level image comparison and rewrites compilation archives with the best available pages.

```powershell
python scripts\cbz_compilation_resolver.py "\\tower\media\comics\Comix" --dry-run
python scripts\cbz_compilation_resolver.py "\\tower\media\comics\Comix" --workers 8
```

## cbz_gap_checker.py

The gap checker remains separate because it is report-only and writes a timestamped CSV to `Logs/`.

```powershell
python scripts\cbz_gap_checker.py "\\tower\media\comics\Comix" --workers 8
```
