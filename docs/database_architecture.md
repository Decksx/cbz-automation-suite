# Database Architecture

## Status

**Committed design direction; implementation pending.**

SQLite will become the authoritative operational state layer. CBZ files remain on the filesystem.

Recommended local path:

```text
C:\git\ComicAutomation\data\comics.db
```

Do not place the active SQLite database on the Unraid SMB share.

## Why it is required

The database must answer:

- Is this renamed file the same archive seen under another path?
- Which source introduced this title?
- Which review approved a series merge?
- Have page hashes already been generated?
- Which copy is preferred in a duplicate group?
- Which algorithm and version produced a similarity score?
- Which filesystem actions were planned and attempted?
- Which Komga/Komf object maps to the local canonical series?

## Core identity rule

Archive identity and file location are separate:

```text
archive_files
  logical/content record

file_locations
  current and historical physical paths

file_events
  movement and mutation history
```

A rename or move must not create a new archive identity.

## Planned migrations

### Migration 001 — operational core

```text
schema_migrations
application_settings
processing_runs
processing_stages
processing_items
source_batches
archive_files
file_locations
file_events
```

### Migration 002 — archive inventory

```text
archive_metadata
archive_pages
page_hashes
archive_signatures
archive_relationships
```

### Migration 003 — series identity

```text
series
series_titles
series_external_ids
series_relationships
archive_series_candidates
archive_series_assignments
```

### Migration 004 — review and plans

```text
review_cases
review_case_members
review_decisions
action_plans
plan_actions
action_executions
```

### Migration 005 — image recognition and dedupe

```text
image_analysis_jobs
similarity_models
image_embeddings
dedupe_cases
dedupe_case_members
dedupe_comparisons
dedupe_resolutions
```

### Migration 006 — quality analysis

```text
quality_models
quality_assessments
archive_quality_summaries
```

### Migration 007 — routing and external systems

```text
routing_decisions
external_library_items
external_sync_runs
external_metadata_snapshots
```

## Connection policy

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 30000;
```

Use one primary writer or a serialized write service.

## Location roles

```text
incoming
staging
review
library
quarantine
backup
missing
```

## Series title provenance

Every title observation should preserve:

```text
title
normalized_title
language
script
title_type
source
confidence
is_preferred
created_at
```

Title types:

```text
canonical
english
romaji
native
translated
folder
filename
comicinfo
source
manual
alternate
```

Automatic translations must not silently replace manually approved canonical titles.

## Transition from JSON

During migration:

- retain JSONL progress until database-backed resume is stable;
- import proposal/decision JSON into review tables;
- import action-plan JSON into plan tables;
- retain JSON export for troubleshooting.

## Backup

The separate library backup protects CBZ files but does not protect operational history. Back up SQLite independently, preferably through SQLite's online backup API or after cleanly closing writers and checkpointing WAL.

## Actual implemented schema (as of migration 009)

The "Planned migrations" section above describes the original committed
design direction. The schema actually implemented in
`comic_automation/database/migrations/` has since diverged from those
exact table names as the service, job queue, and inspection pipeline
were built out. The authoritative source for current schema is that
migrations directory; this section tracks it at a summary level so this
document doesn't go stale again.

```text
001 operational_foundation      application_settings, processing_runs,
                                 processing_stages, processing_items,
                                 source_batches, archive_files,
                                 file_locations, file_events, jobs
002 discovery_checkpoints       discovery_checkpoints
003 archive_inspections         archive_inspections
004 refine_inspection_status    (status refinements)
005 job_failure_categories      jobs.failure_category
006 archive_hashes              archive_hashes
007 archive_page_hashes         archive_pages, page_hashes
008 near_duplicate_candidates   archive_content_signatures,
                                 near_duplicate_candidates
009 archive_quarantine          archive_quarantine
```

### archive_quarantine (migration 009)

Tracks permanently-broken archives (currently `corrupt_archive`; never
`filesystem_not_found`, since there's no file to move) that have been
explicitly approved for remediation and physically relocated out of the
live library into a designated holding folder, pending manual
re-download. This is deliberately separate from `file_locations`, which
only tracks where an archive lives *within* the library --
a quarantined archive isn't part of the library at all anymore.

```text
archive_quarantine
  id
  archive_id            (unique; one row per archive ever quarantined)
  source_path            original library path
  quarantine_path        path inside the holding folder
  failure_category
  job_id                 the terminal inspect_archive job that triggered this
  status                 pending_redownload | resolved | abandoned
  quarantined_at
  resolved_at
  notes
```

The move itself is additionally logged as a `quarantined` event in
`file_events` (`source_path` / `destination_path`), consistent with how
every other archive relocation is audited. The archive's prior
`file_locations` row is marked `is_current = 0` rather than replaced --
a quarantine holding folder is intentionally excluded from library
discovery scans (`--exclude-directory`), so it should never be treated
as a live library path.

See `comic_automation/archive/quarantine_cli.py` for the guarded CLI
(preview by default; `--confirm` + `--backup-directory` required to
actually move files) and `comic_automation/archive/quarantine.py` for
the underlying naming rule and repository logic.
