# Project Overview

## Purpose

The CBZ Automation Suite cleans, tags, organizes, reviews, and routes comic archives before or during ingestion into a Komga-based library.

## Current pipeline

```text
Incoming
  ↓
Watcher or batch workflow
  ↓
Filename and ComicInfo normalization
  ↓
Series organization / review staging
  ↓
Final library
  ↓
Komga / Komf
```

## Current tools

| Component | Role |
|---|---|
| `apps/cbz_gui.py` | GUI launcher and interactive series review |
| `scripts/cbz_core.py` | Shared normalization, parsing, translation, and ComicInfo decisions |
| `scripts/cbz_watcher.py` | Watches incoming directories, waits for stability, processes CBZ files, and routes directories |
| `scripts/cbz_sanitizer.py` | Recursive batch sanitizer with persistent progress and incremental scanning |
| `scripts/cbz_library_maintenance.py` | Consolidated cleanup, organization, proposal, plan, metadata, and repair commands |
| `scripts/cbz_workflows.py` | Multi-stage orchestration |
| `scripts/cbz_compilation_resolver.py` | Page-level compilation overlap resolver |
| `scripts/cbz_gap_checker.py` | Missing chapter CSV report |

## Current limitations

- Most state remains path-based.
- Sanitizer progress is JSONL.
- Review proposals and decisions are JSON.
- Dry-run action plans are JSON.
- General image-aware duplicate detection is not implemented.
- Komga/Komf metadata is not yet fed back into a canonical local series identity database.

## Committed direction

The target architecture adds:

- local SQLite operational state
- archive identity separate from file location
- canonical series and title aliases
- review cases and decisions
- per-page exact and perceptual hashes
- quality scoring
- OpenCLIP embeddings on the RTX 3080
- quarantine-first duplicate resolution
- staging before publication to Komga
