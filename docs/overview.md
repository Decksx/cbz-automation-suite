# Project Overview

## Mission

The CBZ Automation Suite is a Windows-first, local-first system for safely
ingesting, identifying, normalizing, comparing, organizing, maintaining, and
publishing a large digital-comic collection assembled from multiple sources.
Archives enter through a dedicated incoming area, are stabilized and staged,
inventoried and hashed, sanitized, and given normalized ComicInfo metadata.
The system then resolves series and release identity from filenames,
ComicInfo, filesystem context, aliases, Japanese/Chinese/Korean title
handling, external-provider observations, and image evidence; identifies
exact, near, partial-overlap, compilation, and semantically related
candidates; compares candidate editions with versioned quality evidence; and
publishes reviewed results into the correct library and series location for
Komga/Komf.

Two goals are stated precisely here because their loose forms are misleading:

- **"No duplicates"** means no unreviewed redundant copy remains without an
  explicit disposition. It does not mean forcing every similar pair down to
  one file. Translations, censored and uncensored editions, alternate scans,
  compilations, bonus-content editions, and materially different page
  sequences are meaningful variants, not redundancy.
- **"Highest quality"** means the preferred revision among identity-compatible
  candidates, supported by explainable evidence and an authorised decision. It
  is not automatic deletion by score, and file size alone never selects the
  keeper.

## Success criteria

These are the standing conditions the system is built to satisfy. They do not
change with a measurement; see `docs/implementation_roadmap.md` for progress
against them and `docs/engineering_decisions.md` for the binding policy behind
them.

- **Reliable intake.** Archives from every source move through one
  staging-first pipeline with stability checks, resumable jobs, explicit
  failure classification, and no hidden filesystem mutation.
- **Canonical identity.** A logical archive is separate from its path and its
  byte revision, and a canonical series retains its English, romanized,
  original-language, provider, and filesystem aliases with provenance.
- **Evidence, then decision, then action.** Hashing, metadata, and provider
  lookup produce evidence; an approved deterministic rule or a human review
  produces a decision; a separately approved plan performs the mutation. The
  three are never collapsed into one opaque command.
- **Content proof for destruction.** Metadata may propose that two archives
  are duplicates; only identical page content may authorise removing one.
- **Live state over recorded state.** Anything acting on a file verifies that
  file on disk rather than trusting a database row that claims it is current.
- **Candidate-relative quality.** Quality is compared only after identity and
  release compatibility are established, using versioned component evidence
  rather than a single opaque score.
- **Recoverable maintenance.** Moves, metadata rewrites, quarantine, and
  deletion are quarantine-first, content-addressed, revalidated at execution,
  auditable, and reversible.
- **Operator-visible state.** Coverage, queue health, failures, ambiguity,
  candidate backlogs, decisions, revision lineage, and publication state are
  understandable without reconstructing raw tables or prior session
  transcripts.
- **Separate activation.** A capability may be built, tested, and run in
  shadow or read-only mode before it becomes authoritative. Routing v2,
  automated identity, quality preference, quarantine, and publication each
  require their own named activation gate.

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
- The Version 1 perceptual-hash backfill is incomplete. Its coverage,
  eligible-archive backlog, and terminal failures are tracked in
  `docs/implementation_roadmap.md` and deliberately not restated here: a
  count copied into a second document goes stale in one of them.
- Komga/Komf metadata is not yet fed back into a canonical local series identity database.

## Current production status

Production counts are volatile, so they live in exactly one place:
`docs/implementation_roadmap.md`, under "Current production metrics",
where each value carries the date and the guarded run it was reconciled
against. They are not duplicated here.

What is durable from the profiling work is the shape of the cost. Measured
over the 2026-07-30 guarded batch, across 4,991 successfully profiled
archives and 250,423 pages, image opening and decoding consumed 51.26% of
timed work, versus 29.80% for pHash, 13.97% for dHash, 3.97% for ZIP entry
reads, and 0.57% for database lookup and save combined. A future
optimization should be argued against that distribution rather than
against intuition.

See `docs/development_log_2026-07-30.md` for that batch's operational
record, and `docs/implementation_roadmap.md` for current status, the
2026-08-17 guarded batch and what it found, and the next optimization and
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
