# SQLite Database Foundation

## Status

The migration-driven SQLite operational core is implemented through
migration 009 and is in production use for:

- library discovery and checkpoints;
- archive identity and location history;
- archive inspection;
- persistent jobs and failure classification;
- archive-level SHA-256;
- exact page inventory and SHA-256;
- Version 1 dHash/pHash and decoded dimensions;
- review-only near-duplicate candidates;
- guarded quarantine history.

The current production metrics and next architecture work are tracked
in `docs/implementation_roadmap.md`.

## Authoritative files

```text
comic_automation/database/connection.py
comic_automation/database/migrations.py
comic_automation/database/migrations/*.sql
comic_automation/jobs/
comic_automation/library/
comic_automation/archive/
```

The original `scripts/db.py` and `scripts/db_cli.py` foundation remains
for compatibility, but the `comic_automation` package and numbered
migrations are authoritative for the production schema.

## Current migrations

```text
001 operational_foundation
002 discovery_checkpoints
003 archive_inspections
004 refine_inspection_status
005 job_failure_categories
006 archive_hashes
007 archive_page_hashes
008 near_duplicate_candidates
009 archive_quarantine
```

See `docs/database_architecture.md` for the table-level summary.

## Connection settings

Writable package connections enable:

```text
foreign_keys=ON
journal_mode=WAL
synchronous=NORMAL
busy_timeout=30000
```

The package uses explicit transaction control (`isolation_level=None`)
so repository and queue code owns `BEGIN IMMEDIATE`, `COMMIT`, and
`ROLLBACK` boundaries.

The active database belongs on the Office PC. Do not open it for direct
writes over SMB. Future remote readers should use a read-only API or
verified snapshot.

## Migrations

`apply_migrations()`:

- discovers numbered `NNN_*.sql` files;
- skips already applied versions;
- applies each migration in one `BEGIN IMMEDIATE` transaction;
- records the version inside the same transaction;
- rolls back the whole migration on failure.

Production and maintenance commands may call migration application at
startup. A report with no new schema work returns an empty
`applied_migrations` list.

## Backups and integrity checks

Use SQLite's online backup API to create a consistent timestamped
backup. Guarded production work should:

1. verify the source with `PRAGMA quick_check`;
2. create a new backup without overwriting an existing file;
3. run `quick_check` against the backup;
4. record its path, size, and timestamp;
5. protect it from writes during the bounded operation;
6. verify its metadata remains unchanged afterward.

Do not treat a copied `.db` file as verified until SQLite opens and
checks it successfully.

## Current production database

The active backfill documented on 2026-07-29 uses:

```text
G:\ComicAutomation\TestDatabase\inspection-working.db
```

Although the directory retains the historical `TestDatabase` name, the
file is the operational working database for the guarded production
runs documented in `docs/development_log_2026-07-29.md`.

## Next database work

After the Version 1 perceptual-hash backfill and final coverage audit:

1. establish the minimum shared local DAL;
2. introduce immutable archive revisions and observations;
3. migrate derived evidence to revision-aware provenance;
4. add guarded revision retention/pruning;
5. harden jobs with leases, deterministic idempotency keys, and
   explicit retryability.

Do not introduce a network API, remote writer, or database-platform
migration until a measured operational requirement exists.
