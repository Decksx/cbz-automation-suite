# Unified Workflows

`scripts/cbz_workflows.py` is the preferred CLI for controlled multi-stage operations. It runs existing scripts as subprocesses from the repository root and stops when a stage fails.

## Maintenance workflow

Valid stages:

```text
sanitize
archive
organize
metadata
names
```

Default:

```text
sanitize,archive,organize,metadata,names
```

```powershell
python scripts\cbz_workflows.py maintenance ROOT --dry-run

python scripts\cbz_workflows.py maintenance ROOT `
  --stages=sanitize,archive,metadata,names
```

Common options:

```text
--workers N
--dry-run
--no-metadata-dedupe
--uncensored-check
--move-which both|uncensored|censored
```

Maintenance options:

```text
--rules=rule1,rule2
--sort=newest|oldest|alpha|alpha-reverse
--full
--restart
--names-only
```

| Stage | Command |
|---|---|
| `sanitize` | `cbz_sanitizer.py --scan=ROOT` |
| `archive` | `cbz_library_maintenance.py archive-clean ROOT` |
| `organize` | `cbz_library_maintenance.py organize-series ROOT` |
| `metadata` | `cbz_library_maintenance.py metadata ROOT` |
| `names` | `cbz_library_maintenance.py repair-names ROOT` |

## Series workflow

Valid stages:

```text
organize
stage
review
compilations
```

Default: `organize`.

```powershell
python scripts\cbz_workflows.py series ROOT `
  --stages=organize,stage,review,compilations `
  --dry-run
```

| Stage | Behavior |
|---|---|
| `organize` | Merge and normalize series folders |
| `stage` | Enable possible-series staging into `_Check` |
| `review` | Generate a structured proposal JSON |
| `compilations` | Run the page-level compilation resolver |

Default proposal:

```text
Logs\series_proposal.json
```

Useful options:

```text
--series-common-words N
--series-min-group-size N
--out FILE
--uncensored-check
--move-which both|uncensored|censored
--no-metadata-dedupe
```

A workflow is not transactional across stages. A later failure does not undo earlier completed work.
