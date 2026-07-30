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
| `comic_automation/` package | SQLite-backed service foundation: persistent job queue (`jobs/`), library discovery (`library/`), archive inspection/hashing/quarantine/near-duplicate detection (`archive/`), long-running service runner (`service.py`) -- see `docs/implementation_roadmap.md` for current status |

## Current limitations

- Legacy sanitizer and maintenance workflows still retain some
  path-based and JSON/JSONL state.
- Review proposals, decisions, and dry-run action plans are not yet
  consolidated into the SQLite operational model.
- General image-aware duplicate detection (perceptual pHash/dHash) is
  implemented and running at production scale as of 2026-07-29, but
  is review-only so far -- see `docs/implementation_roadmap.md` Phase
  5 for current coverage and remaining gaps (aggregate signatures,
  partial-overlap detection, review UI).
- The Version 1 perceptual-hash backfill is incomplete. The last
  reconciled state contains 1,279,247 dHash rows and 1,279,247 pHash
  rows, with 32,554 further archives eligible for processing.
- Komga/Komf metadata is not yet fed back into a canonical local series identity database.

## Current production status

As of the last reconciled guarded batch on 2026-07-30:

| Metric | Value |
| --- | ---: |
| Archives with archive SHA-256 | 59,541 |
| Page SHA-256 rows | 2,955,304 |
| Perceptual jobs completed | 25,623 |
| Perceptual jobs failed | 77 |
| Pending/claimed/running perceptual jobs | 0 |
| dHash rows, Version 1 | 1,279,247 |
| pHash rows, Version 1 | 1,279,247 |
| Eligible archives remaining | 32,554 |
| Near-duplicate candidates | 0 |

The latest optimized guarded batch processed 5,000 archives with 4,991
successes, 8 legitimate terminal image-decoding failures, one scheduled
retry, and exact pre/post reconciliation. It completed with
`PRAGMA quick_check = ok`; the protected backup retained its pre-batch
state.

Across 4,991 successfully profiled archives and 250,423 pages, image
opening and decoding consumed 51.26% of timed work, versus 29.80% for
pHash, 13.97% for dHash, 3.97% for ZIP entry reads, and 0.57% for
database lookup and save combined.

The read-only terminal-failure audit classifies all 77 failures as 40
corrupt archives and 37 corrupt images. The separate source-drift case
for archive `23258` was recovered through the guarded refresh command;
its original perceptual job completed successfully on attempt 2.

See `docs/development_log_2026-07-30.md` for the latest operational
record and
`docs/implementation_roadmap.md` for the next optimization and
architecture work.

## Committed direction

The target architecture adds:

- [x] local SQLite operational state
- [x] archive identity separate from file location
- [x] guarded exact-duplicate resolution tooling
- canonical series and title aliases
- review cases and decisions
- [x] per-page exact and perceptual hashes (running at production
      scale, see `docs/implementation_roadmap.md`)
- quality scoring
- OpenCLIP embeddings on the RTX 3080
- [x] quarantine-first duplicate resolution
- staging before publication to Komga
