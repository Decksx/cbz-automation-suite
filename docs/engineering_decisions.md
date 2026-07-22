# Engineering Decisions

## Shared core is authoritative

Normalization, series inference, title handling, translation helpers, number extraction, and ComicInfo update decisions belong in `cbz_core.py`.

Scripts retain only operation-specific mechanics such as watcher debounce, archive rewrites, worker scheduling, routing, and GUI state.

## Unified workflows are preferred

Use `cbz_workflows.py` for multi-stage maintenance and series operations. Individual commands remain available for targeted work, scheduled jobs, and compatibility.

## Persistent progress currently remains JSONL

The sanitizer automatically resumes from append-only JSONL history. Keep this during database migration until database-backed resumability is proven.

## Dry-run plans are executable artifacts

A reviewed dry run should be replayable without rescanning. Current JSON plans will map to database action-plan records while retaining JSON export.

## Uncertain series matches require review

Fuzzy similarity alone must not silently merge likely matches. Proposals, exclusions, `_Check`, and GUI decisions are first-class concepts.

## Staging precedes final publication

The target model identifies, normalizes, analyzes, and reviews archives before they enter the final Komga library.

## SQLite is operational

The database will control processing state and retain history. It is not merely a report generated after filesystem work.

## SQLite remains local

The active database belongs on the Office PC. Direct concurrent SMB access from Unraid or the Pi is unsupported.

## Image dedupe is progressive

```text
exact archive hash
ordered exact page hashes
pHash / dHash
sequence-aware overlap
quality scoring
OpenCLIP embeddings
```

CLIP refines candidate matching; it is not the first candidate-generation step.

## Deletion is delayed

Use quarantine and review before permanent deletion even though a separate library backup exists.

## External metadata is evidence

Komga and Komf identifiers and titles feed the local series model, but provider and timestamp provenance must be retained.

## Office PC is the worker

CPU-intensive scanning, image decoding, hashing, and GPU embeddings run on the Office PC. The Pi 5 is for dashboards, scheduling, and health checks.
