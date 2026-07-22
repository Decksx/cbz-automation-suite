# Current and Target Architecture

## Deployment

```text
Office PC
  Ryzen 3900X / RTX 3080
  - watcher
  - GUI
  - sanitizer and maintenance workflows
  - future SQLite database
  - future pHash / CLIP workers

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

Will become authoritative for:

- archive identity and path history
- source batches
- processing runs and stages
- metadata observations
- series identities and aliases
- review cases
- action plans and execution results
- page hashes and embeddings
- quality assessments
- duplicate candidates and resolutions
- Komga/Komf identifiers

### JSON

After database integration, JSON remains appropriate for:

- user-editable configuration such as `routing.json`
- exports and imports
- troubleshooting snapshots
- temporary compatibility with existing plan/proposal workflows

## Concurrency

Keep SQLite local to the Office PC. Enable WAL and serialize writes. Do not open the active database over SMB from Unraid or the Pi.
