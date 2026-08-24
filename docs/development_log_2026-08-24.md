# Development log — 2026-08-24

Four pull requests merged and migration 014 applied to production inside a
reviewed maintenance window.

## Merged

| PR | Merge commit | Subject |
| --- | --- | --- |
| #80 | `42fd727` | Minimum local DAL: connection policy, transactions, repositories |
| #81 | `c98d000` | Stop an interrupted ComicInfo rewrite from losing the archive |
| #83 | `79138bd` | Immutable revision model: migration 014, lineage, current pointer |

Issue #82 was opened for the `test_classification_staging.py` Windows race
and remains outstanding.

Each merge was pinned to an approved head SHA, and parentage plus an empty
tree diff against that head was verified afterwards.

## Migration 014 — production application

**Target.** `G:\ComicAutomation\TestDatabase\inspection-working.db`. Resolved
from `docs/development_log_2026-07-28.md` and three later logs rather than
assumed: `G:\ComicAutomation\database\comics.db` also exists, is 41 MB, and
was last written 2026-07-23. It is not the working database despite the
directory name suggesting the reverse for both files.

**Window.** Watcher stopped (PID 20408) after confirming two hours of
inactivity and a clean leftover scan. Process census showed no watcher, no
GUI, and no other database writer. Restarted afterwards as PID 556.

### Preflight — read-only, 2026-08-24

Opened with `mode=ro` plus `PRAGMA query_only`, never through an entry point
that calls `apply_migrations()`. WAL-aware: `data_version` 2 before and 2
after, one deferred read transaction between them.

| Check | Result |
| --- | --- |
| migrations | `[1…13]` |
| migration-014 tables, columns, indexes, triggers | none |
| `quick_check` | ok |
| active jobs | 0 |
| source size / mtime across the read | unchanged |

Baseline counts, all matching the 2026-08-21 reconciliation:

```text
archive identities        59,688
archive_hashes (sha256)   59,541
content signatures        58,437
current file locations    59,377
archive pages          2,955,391
page_hashes            8,821,073
retirements                    1
supersessions                  0
```

### Protected backup

```text
G:\ComicAutomation\backups\inspection-working.2026-08-24.pre-migration-014.db
size    2,378,436,608 bytes
sha256  4c0654b7b4c88cebc3cbcfda3af72e50ac85dac7179def5d7be679b83486c17a
report  G:\ComicAutomation\logs\backup-verification-2026-08-24-pre-migration-014.json
```

Written by `comic_automation/database/backup_cli.py`: SQLite online backup
API, read-only source, 22 tables and 71 schema objects compared verbatim,
source fingerprint and `data_version` stable either side.

**That digest is identical to the 2026-08-21 protected backup's.** The two
are genuinely separate files — distinct NTFS file IDs, creation times three
days apart — holding byte-identical content, which independently confirms
production was not written to between 2026-08-21 12:24 and this migration.
The 2026-08-21 backup was re-hashed during this work and still matches its
recorded `4c0654b7…86c17a`; it was not rewritten, replaced or refreshed.

### Application and reconciliation

`apply_migrations` returned `[14]`. Preconditions were re-checked
immediately before acting and were unchanged from the preflight.

| Measure | Value | Expected |
| --- | ---: | ---: |
| archive identities | 59,688 | 59,688 |
| initial revisions (ordinal 1) | 59,688 | 59,688 |
| established | 59,541 | 59,541 |
| provisional | 147 | 147 |
| NULL current pointers | 0 | 0 |
| pointers not owned by their archive | 0 | 0 |

Database grew 2,378,436,608 → 2,409,934,848 bytes.

### Post-migration proofs — read-only

| Proof | Result |
| --- | --- |
| `integrity_check` (full) | ok |
| migrations | `[1…14]` |
| 21 pre-014 tables | row counts unchanged |
| `schema_migrations` | grew by exactly one row |
| `archive_files.sha256` | NULL for all 59,688 |
| hashed archives whose current digest equals `archive_hashes.digest` | 59,541 of 59,541 |
| disagreeing | 0 |
| unhashed archives pointing at a provisional revision with NULL digest | 147 |
| broken lineage links | 0 |
| `foreign_key_check` | clean |
| revisions not at ordinal 1, or carrying a predecessor | 0 |
| revisions not marked `migration_backfill`, or with blank evidence | 0 |
| observations created by the migration | 0 |

### Objects created by 014

```text
tables (2)    archive_revisions
              archive_revision_observations

indexes (6)   idx_archive_revisions_sha256
              idx_archive_revisions_archive
              idx_archive_revisions_one_provisional
              idx_archive_files_current_revision
              idx_revision_observations_revision
              idx_revision_observations_location

triggers (7)  trg_archive_revisions_immutable
              trg_archive_revisions_not_deletable
              trg_archive_revisions_lineage_is_sequential
              trg_current_revision_owned_on_insert
              trg_current_revision_owned_on_update
              trg_current_revision_not_cleared
              trg_archive_files_initial_revision

columns (1)   archive_files.current_revision_id
```

## A reporting error worth recording

The watcher was restarted after PR #81 merged, at 13:49:52, and verified at
the time. It was then described as "stopped, awaiting your restart
instruction" in several subsequent reports. It was running throughout, and
had processed a 21-archive batch at 14:12:50. The error was in the reporting
only — no operation was performed on a wrong assumption — but the lead
planned the next maintenance window believing intake was already halted.

The general lesson is the one this repository already applies to
measurements: process state is re-measured before it is asserted, not
carried forward from an earlier turn.

## Open

- **Issue #82** — nondeterministic `WinError 5` on a directory rename in
  `test_classification_staging.py`; local Windows only, CI green.
- **Unsafe archive-member names** are preserved rather than rejected;
  deferred to resource hardening.
- **Step 3** — revision retention and guarded pruning. The lineage foreign
  key was deliberately left `NO ACTION DEFERRABLE INITIALLY DEFERRED` for
  this: when the delete guard is relaxed for reviewed pruning, deleting an
  older revision must not erase its successors.
