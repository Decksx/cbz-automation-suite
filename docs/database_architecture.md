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
