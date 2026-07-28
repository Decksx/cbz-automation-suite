# Implementation Roadmap

## Phase 1 — baseline and tests

- keep documentation aligned with code;
- add regression coverage for normalization, proposal generation, action plans, and workflow command construction;
- verify dry-run behavior.

## Phase 2 — operational SQLite core

- migration framework;
- connection policy;
- runs and stages;
- source batches;
- archive identity;
- path history;
- file events;
- watcher/workflow integration.

## Phase 3 — archive audit

- [x] non-mutating crawler;
- [x] archive SHA-256 (run across the full library 2026-07-28: 59,541
      archives hashed, 100% coverage; 2 exact-duplicate groups found,
      both involving the `_extraneous` suspected-duplicate holding
      folder);
- [ ] exact-duplicate group review and resolution (2 groups found;
      resolution tooling scope -- reusable CLI vs. one-time cleanup --
      not yet decided);
- [x] archive metadata inventory (ComicInfo.xml parsing via the
      inspection pipeline);
- [ ] page inventory;
- [ ] exact page hashes (implemented, not yet run at production scale
      -- next planned step);
- [x] resumable database jobs.

## Phase 4 — series identity

- canonical series;
- title observations and aliases;
- language, script, and provenance;
- candidate scoring;
- review cases;
- Komga/Komf IDs.

## Phase 5 — perceptual dedupe

- [x] pHash/dHash workers;
- [x] sampled hash blocking and decoded dimension summaries;
- [x] conservative ordered page comparison;
- [x] persistent review-only Tier C candidates;
- [ ] richer aggregate archive signatures;
- [ ] partial-overlap and compilation detection;
- review UI;
- [x] quarantine workflow (guarded move-to-holding-folder CLI for
      permanently-broken archives; see
      `comic_automation/archive/quarantine_cli.py` and
      `docs/database_architecture.md`).

## Phase 6 — quality scoring

- page and archive metrics;
- model versioning;
- preferred-copy selection;
- operator overrides.

## Phase 7 — OpenCLIP

- GPU environment;
- batched sampled embeddings;
- cosine-similarity refinement;
- full-page embeddings for ambiguous candidates.

## Phase 8 — managed publication

- staging-first watcher;
- approval gates;
- final-library promotion;
- Komga scan coordination;
- Komf feedback ingestion.

## Phase 9 — dashboard

- Pi-hosted status UI;
- queue health;
- run history;
- review counts;
- read-only reports;
- no direct SMB SQLite writes.
