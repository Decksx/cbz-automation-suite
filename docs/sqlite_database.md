# SQLite Database Foundation

This phase adds a migration-driven SQLite layer.

## Files

```text
scripts/db.py
scripts/db_cli.py
migrations/001_initial_schema.sql
tests/test_db.py
```

## Initialize

```powershell
python scripts\db_cli.py init
```

For the current workstation path:

```powershell
python scripts\db_cli.py --database "C:\Users\David.Johnson\ComicAutomation\data\comics.db" init
```

## Inspect

```powershell
python scripts\db_cli.py status
python scripts\db_cli.py tables
```

## Connection settings

Writer connections enable:

```text
journal_mode=WAL
synchronous=NORMAL
foreign_keys=ON
busy_timeout=30000
```

Office-PC remains the primary writer. Readers such as a future dashboard can
query through a read-only API or snapshot rather than opening the database over
SMB.

## Initial tables

- `series`
- `series_aliases`
- `archives`
- `pages`
- `processing_runs`
- `archive_series_matches`
- `dedupe_candidates`
- `quality_scores`
- `file_events`
- `routing_log`
- `repair_log`
- `review_queue`
- `application_settings`
- `schema_migrations`

## Next database implementation

The next layer should add repository/service functions for:

1. starting and finishing processing runs
2. upserting archive inventory records
3. recording file and routing events
4. creating canonical series and aliases
5. inserting review-queue candidates
