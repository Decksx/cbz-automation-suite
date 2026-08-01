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
- `comic_automation/` — SQLite-backed discovery, archive inspection,
  exact/perceptual hashing, guarded quarantine/duplicate resolution,
  persistent jobs, and service foundation

> The SQLite operational core and Version 1 exact/perceptual hashing
> pipeline are implemented. The perceptual-hash library backfill is
> running in guarded batches; series identity, quality scoring,
> semantic embeddings, and the review dashboard remain roadmap work.

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

### Project

- [Project overview](docs/overview.md)
- [Architecture](docs/architecture.md)
- [Implementation roadmap](docs/implementation_roadmap.md)
- [Engineering decisions](docs/engineering_decisions.md)
- [Assistant working agreement](CLAUDE.md)
- [Session protocol](docs/session_protocol.md)

### Components

- [Unified workflows](docs/workflows.md)
- [Shared core](docs/cbz_core.md)
- [Shared pipeline](docs/shared_pipeline.md)
- [Sanitizer](docs/cbz_sanitizer.md)
- [Watcher](docs/cbz_watcher.md)
- [Library maintenance](docs/library_maintenance.md)
- [Other tools](docs/other_tools.md)

### Database and hashing

- [Database architecture](docs/database_architecture.md)
- [SQLite foundation](docs/sqlite_database.md)
- [Image deduplication](docs/image_deduplication.md)

### Current state

- [Latest production handoff](docs/production_handoff_2026-07-31.md)
- [Latest development log](docs/development_log_2026-07-31.md)

### Audits

- [Archive I/O and resource limits](docs/archive_io_resource_audit.md)
- [Job enqueue idempotency](docs/job_enqueue_idempotency_audit.md)
- [Jobs worker retry](docs/jobs_worker_retry_audit.md)

Earlier development logs and production handoffs remain in `docs/`
alongside the current ones. Phase-era packaging notes — `PHASE1_APPLY_NOTES.md`,
the `README_PHASE*` files, `cbz_docs_updated_markdown_bundle.md`, and
similar — are retained as historical delivery records rather than
current documentation, and are deliberately not indexed here.

## Runtime state

- `routing.json` — local routing configuration
- `Logs/` — rotating logs, reports, plans, proposals, and decisions
- `progress_tracking/` — persistent sanitizer progress
- `_Check/` — manual review staging where applicable

The immediate work is completing and auditing the Version 1
perceptual-hash backfill, then introducing immutable archive revisions,
revision-aware provenance, a consolidated local SQLite data-access
layer, and job-queue lease/idempotency hardening. See the
[implementation roadmap](docs/implementation_roadmap.md) for the
ordered plan.
