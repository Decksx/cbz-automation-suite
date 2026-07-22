# Library Maintenance

`scripts/cbz_library_maintenance.py` is the consolidated maintenance utility.

It replaces routine use of former standalone scripts for archive deduplication, duplicate-token cleanup, folder merging, fuzzy series matching, uncensored-pair detection, and number tagging.

## Common arguments

Most mutating subcommands accept:

```text
PATH [PATH ...]
--dry-run
--workers N
--verbose
--plan-out FILE
```

`--plan-out` records concrete actions during a dry run. Replay them with `apply-plan`.

## `archive-clean`

```powershell
python scripts\cbz_library_maintenance.py archive-clean ROOT --dry-run
```

Cleans duplicate archives and duplicate filename tokens and can pack loose image folders.

Important options:

```text
--no-recursive
--no-metadata-dedupe
```

Current dedupe uses filenames and optionally ComicInfo Series/Volume/Number metadata. This is not general visual duplicate detection.

## `organize-series`

```powershell
python scripts\cbz_library_maintenance.py organize-series ROOT --dry-run
```

Performs folder merges, fuzzy series matching, optional review staging, post-merge dedupe, metadata repair, and likely compilation-range fixes.

Important options:

```text
--no-dedupe-archives
--no-metadata-dedupe
--uncensored-check
--move-which both|uncensored|censored
--possible-series-check
--recursive-parents
--report-threshold FLOAT
--auto-threshold FLOAT
--series-common-words N
--series-min-group-size N
--interactive
```

`--interactive` prompts before staging possible-series groups when stdin is a real terminal.

## `propose-series`

Creates a structured JSON proposal without modifying the library:

```powershell
python scripts\cbz_library_maintenance.py propose-series ROOT `
  --out Logs\series_proposal.json
```

Proposal groups include:

- group ID
- detector kind
- score when available
- suggested canonical name
- parent path
- member names, paths, and CBZ counts

## `apply-series`

Applies GUI-reviewed decisions:

```powershell
python scripts\cbz_library_maintenance.py apply-series Logs\series_decisions.json `
  --dry-run `
  --plan-out Logs\series-merge-plan.json
```

Decision behavior:

- `yes` — merge members into the chosen or suggested target
- `no` — record the group as an exclusion
- undecided — leave it untouched

Approved merges are followed by dedupe, metadata update, and compilation detection.

## `apply-plan`

```powershell
python scripts\cbz_library_maintenance.py apply-plan Logs\plan.json --dry-run
python scripts\cbz_library_maintenance.py apply-plan Logs\plan.json
```

The current plan executor supports directory creation, file movement, deletes, recursive directory removal, directory moves, and image-folder packing.

## `clear-exclusions`

```powershell
python scripts\cbz_library_maintenance.py clear-exclusions --dry-run
python scripts\cbz_library_maintenance.py clear-exclusions --filter "title fragment"
```

Exclusions are stored in:

```text
Logs\series_exclusions.json
```

## `rename`

```powershell
python scripts\cbz_library_maintenance.py rename ROOT --dry-run
```

Runs shared filename normalization.

## `repair-names`

Repairs mojibake in filenames, directory names, and ComicInfo `Title`, `Series`, `LocalizedSeries`, and `AlternateSeries`.

```powershell
python scripts\cbz_library_maintenance.py repair-names ROOT --dry-run
python scripts\cbz_library_maintenance.py repair-names ROOT --names-only
```

## `metadata`

```powershell
python scripts\cbz_library_maintenance.py metadata ROOT --dry-run
```

Repairs ComicInfo through the shared core.

## `all`

Compatibility preset:

```text
archive-clean → organize-series → metadata
```

Prefer `cbz_workflows.py maintenance` for the complete current workflow, which also includes sanitizer and name-repair stages.
