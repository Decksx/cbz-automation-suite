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

## New SQLite access converges on a local DAL

New and touched database code should use a shared local data-access
layer for connections, pragmas, transactions, migrations, backups, and
repository queries. Existing standalone scripts migrate incrementally
rather than through a disruptive all-at-once rewrite.

A network-facing API is deferred until a real remote client or worker
requires one. Local high-throughput page/hash writes should not be
routed through HTTP.

## Archive revisions represent unique byte states

A logical archive may have multiple immutable byte-level content
revisions. A revision is not an observation event: if bytes previously
seen for an archive reappear, reuse that revision and record a new
filesystem observation.

`archive_files.current_revision_id` will be the sole current-revision
authority. The database must prevent an archive from pointing to a
revision owned by another archive.

Byte-identical archives remain distinct archive identities during the
initial revision migration. Their revisions may share the same
SHA-256; duplicate resolution remains an independent guarded action.

## Immutable revisions use guarded retention

Immutable does not mean unbounded. Keep current, recently previous,
referenced, unresolved, quarantined, and operator-pinned revisions.
Classify older unreferenced revisions as prunable, then remove them
only through a separate reviewed plan/apply operation.

Avoid broad cascading deletion for revision evidence.

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

## Version 1 perceptual hashes are frozen

During the production Version 1 backfill, do not change JPEG decoding,
resize behavior, DCT implementation, floating-point accumulation
order, or stored digest semantics.

Output-preserving optimizations require exact digest regression tests.
Any change that alters stored hash bits uses a new algorithm version.

## Performance optimization is measure-first

Before decoding more pages, measure exact-SHA reuse opportunity and
distinguish archives that can be fully satisfied from those with only
partial page reuse.

Optional timing belongs inside the perceptual worker and repository
save path, aggregated per archive/batch without per-page telemetry
writes. Static operation counts identify hypotheses; recorded phase
timings establish actual bottlenecks.

## Deletion is delayed

Use quarantine and review before permanent deletion even though a separate library backup exists.

## External metadata is evidence

Komga and Komf identifiers and titles feed the local series model, but provider and timestamp provenance must be retained.

## Office PC is the worker

CPU-intensive scanning, image decoding, hashing, and GPU embeddings run on the Office PC. The Pi 5 is for dashboards, scheduling, and health checks.
