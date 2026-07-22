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

- non-mutating crawler;
- archive SHA-256;
- archive metadata inventory;
- page inventory;
- exact page hashes;
- resumable database jobs.

## Phase 4 — series identity

- canonical series;
- title observations and aliases;
- language, script, and provenance;
- candidate scoring;
- review cases;
- Komga/Komf IDs.

## Phase 5 — perceptual dedupe

- pHash/dHash workers;
- aggregate archive signatures;
- blocked candidate generation;
- ordered page comparison;
- review UI;
- quarantine workflow.

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
