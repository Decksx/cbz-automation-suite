# Current and Target Architecture

## Deployment

```text
Office PC
  Ryzen 3900X / RTX 3080
  - watcher
  - GUI
  - sanitizer and maintenance workflows
  - operational SQLite database
  - exact and Version 1 perceptual-hash workers
  - future OpenCLIP worker

        SMB / UNC

Unraid NAS
  - comic libraries
  - Komga and Komf integration
  - Plex
  - long-term storage

Pi 5
  - future dashboard and scheduler
  - health monitoring
  - not a primary image-processing worker
```

## Current code relationships

```text
apps/cbz_gui.py
        │
        ▼
scripts/cbz_workflows.py
        ├── cbz_sanitizer.py
        ├── cbz_library_maintenance.py
        └── cbz_compilation_resolver.py

cbz_watcher.py ─────────────┐
cbz_sanitizer.py ───────────┼──► cbz_core.py
cbz_library_maintenance.py ─┘

comic_automation/
        ├── database/       migration and connection policy
        ├── jobs/           persistent queue and workers
        ├── library/        discovery and checkpoints
        ├── archive/        inspection, hashing, candidate generation,
        │                   quarantine, duplicate resolution
        └── service.py      long-running service runner
```

`cbz_core.py` owns shared domain decisions. Watcher behavior, worker scheduling, archive rewriting, file movement, GUI state, logging, and routing remain in their respective tools.

## Target staging-first pipeline

```text
Incoming
  ↓
Source batch discovery
  ↓
Staging
  ↓
Archive inventory and exact hashing
  ↓
Filename / metadata normalization
  ↓
Series identity resolution
  ↓
Page hashing and image analysis
  ↓
Duplicate and quality evaluation
  ├── high confidence: approve or quarantine
  └── uncertain: review case
  ↓
Final library publication
  ↓
Komga scan
  ↓
Komf metadata feedback
```

## State ownership

### Filesystem

Stores CBZ data.

### SQLite

Is authoritative for:

- archive identity and path history
- source batches
- processing runs and stages
- archive inspection
- persistent jobs and classified failures
- archive and page hashes
- decoded page dimensions
- near-duplicate candidates
- quarantine history

It will later add:

- immutable archive revisions and observations
- revision-aware provenance
- canonical series identities and aliases
- review cases and decisions
- quality assessments
- OpenCLIP embeddings
- Komga/Komf identifiers

### JSON

After database integration, JSON remains appropriate for:

- user-editable configuration such as `routing.json`
- exports and imports
- troubleshooting snapshots
- temporary compatibility with existing plan/proposal workflows

## Concurrency

Keep SQLite local to the Office PC. Enable WAL and serialize writes. Do not open the active database over SMB from Unraid or the Pi.
