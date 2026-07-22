# CBZ Automation Suite

Windows-first automation for monitoring, sanitizing, organizing, reviewing, and routing CBZ comic archives stored locally or on SMB/UNC libraries.

## Primary components

- `scripts/cbz_core.py` — shared parsing, normalization, translation, and ComicInfo logic
- `scripts/cbz_watcher.py` — live incoming-folder processor and router
- `scripts/cbz_sanitizer.py` — recursive batch sanitizer with persistent progress and incremental scanning
- `scripts/cbz_library_maintenance.py` — consolidated cleanup, organization, proposal, plan, and repair operations
- `scripts/cbz_workflows.py` — preferred orchestration entry point for multi-stage jobs
- `apps/cbz_gui.py` — graphical launcher and series-review workflow
- `scripts/cbz_compilation_resolver.py` — page-level compilation/individual overlap resolver
- `scripts/cbz_gap_checker.py` — missing-chapter report generator

> The SQLite and image-recognition architecture documented under `docs/` is the committed implementation direction. It is not yet fully implemented in the current scripts.

## Requirements

- Windows 10/11
- Python 3.11+
- `watchdog` for the live watcher
- access to the configured local or UNC comic paths

```powershell
cd C:\git\ComicAutomation
python -m pip install -r requirements.txt
```

## Preferred commands

### GUI

```powershell
python apps\cbz_gui.py
```

### Unified maintenance workflow

```powershell
python scripts\cbz_workflows.py maintenance "\\tower\media\comics\Comix" --dry-run
```

Default stages:

```text
sanitize → archive → organize → metadata → names
```

### Unified series workflow

```powershell
python scripts\cbz_workflows.py series "\\tower\media\comics\Comix" `
  --stages=organize,stage,review,compilations `
  --dry-run
```

The default series workflow runs only `organize` unless `--stages` is supplied.

### Live watcher

Copy `config\routing.example.json` to `routing.json`, edit it, then run:

```powershell
python scripts\cbz_watcher.py
```

## Dry-run action plans

```powershell
python scripts\cbz_library_maintenance.py organize-series ROOT `
  --dry-run `
  --plan-out Logs\organize-plan.json

python scripts\cbz_library_maintenance.py apply-plan Logs\organize-plan.json --dry-run
python scripts\cbz_library_maintenance.py apply-plan Logs\organize-plan.json
```

## Documentation

- [Project overview](docs/overview.md)
- [Architecture](docs/architecture.md)
- [Unified workflows](docs/workflows.md)
- [Sanitizer](docs/cbz_sanitizer.md)
- [Watcher](docs/cbz_watcher.md)
- [Library maintenance](docs/library_maintenance.md)
- [Shared pipeline](docs/shared_pipeline.md)
- [Database architecture](docs/database_architecture.md)
- [Image deduplication](docs/image_deduplication.md)
- [Engineering decisions](docs/engineering_decisions.md)
- [Implementation roadmap](docs/implementation_roadmap.md)

## Runtime state

- `routing.json` — local routing configuration
- `Logs/` — rotating logs, reports, plans, proposals, and decisions
- `progress_tracking/` — persistent sanitizer progress
- `_Check/` — manual review staging where applicable

The next major implementation phase moves operational state from path-keyed JSON and implicit filesystem state into SQLite.
