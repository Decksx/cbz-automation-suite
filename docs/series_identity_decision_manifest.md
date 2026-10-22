# Series identity decisions: candidate model and decision manifest (design)

Status: **design only**, for review. Nothing here is implemented, migrated,
or applied. Phase 4 step 5 of `docs/implementation_roadmap.md` ("define a
provider-neutral candidate model and durable human-decision manifest"),
built against the binding rules in "Human-reviewed identity resolution
(#57)" and the decision "A human decision is the only resolution of an
ambiguous identity" in `docs/engineering_decisions.md`.

Author: claude-lead. Reviewer: codex-lead. Written 2026-10-22 at master
`f88d1da`. The companion read-only ambiguity report (Phase 4 step 4
tooling) is a separate slice owned by codex-lead; this design consumes its
output but does not depend on its implementation details beyond what is
named under "Inputs".

## What this is for

The v2 series index may become authoritative only when ambiguity is zero
**or every remaining ambiguous key carries a reviewed exception/identity
manifest** (rule 8). The zero branch is executable today
(`SeriesIndexDiagnostics.require_clear()`). The manifest branch has no
design: "no unchecked key list waives this stricter gate". This document
defines what a reviewed decision is, how it is recorded, how it goes stale,
and how the gate consumes it.

It also gives Phase 4 steps 6-10 (external candidates, the review surface,
plan/apply) a stable shape to build against, so the first provider adapter
does not define the model by accident.

## Non-goals

- No activation of v2 authority, routing, staging, or moves. The gate
  evaluates one in-memory observation; passing it is evidence for a
  separate activation decision, never the decision.
- No automatic resolution. Scores order candidates for the operator; they
  never decide (rules 1-2).
- No schema, migration, or repository code in this slice.
- No ComicInfo or filesystem mutation. Those remain separate
  content-addressed plan/apply operations (rule 7).

## Inputs

1. **An observation**: one `SeriesIndexDiagnostics` snapshot, serialized by
   `to_dict()`, with the report fingerprint the ambiguity-report command
   computes over it (configuration, scope, and every contributing entry).
   Only a *complete* scan is a valid input: a snapshot with
   `scan_problems` cannot be decided against.
2. **Archive identity** for archive-level decisions: `archive_revisions`
   (migration 014) — `archive_id`, revision `id`, and `archive_sha256`.
3. **Candidates** (later steps): local folders now; provider results
   (MangaDex first, rule-6 adapters after) through the model below.

## Subjects: what a decision is about

Ambiguity is a property of a folder, not only of a series (the `ERIKA`
case: 3 adult archives among 9 horror ones). So a decision names one of
three subject kinds, each with the identity it is bound to:

| Subject | Bound to | Example |
|---|---|---|
| `series_key` | the canonical `series_key` **and** the exact set of contributing `(destination, path)` entries observed for it | `berserk` seen under Comix and Manga |
| `folder` | one absolute normalized path **and** the set of archive revisions inside it at observation time | `X:\Comix\ERIKA` |
| `archive` | `archive_id` **and** `archive_sha256` of the revision decided on | one archive inside `ERIKA` |

Paths use the index's own lexical identity (host case policy, absolute
normalized, no symlink/junction/alias resolution), so a decision and the
diagnostics compare paths the same way.

## Decision kinds

From Phase 4 step 9, made explicit:

| Kind | Meaning | Subject |
|---|---|---|
| `assign` | this subject belongs to canonical series S (destination D) | folder or archive |
| `merge` | these colliding paths are one series; canonical home is D/path P | series_key |
| `distinct` | same key, different series; each path gets its own disambiguated key/alias | series_key |
| `alternate_edition` | same work, separate edition folder kept apart deliberately | series_key or folder |
| `split` | a mixed folder's archives are distributed across ≥2 series, archive by archive | folder (+ per-archive `assign`) |
| `not_a_series` | the directory is not a series (control dir, staging leftover, …) | folder |
| `none_of_these` | no offered candidate is right; identity still to be established | any |
| `unresolved` | reviewed and explicitly left open | any |
| `withdraw` | retract an earlier decision without replacing it | the earlier decision |

`none_of_these` and `unresolved` are recorded decisions, not absences: they
make "a person looked and could not decide" durable. **Both count as
unresolved for the gate.**

A `split` is complete only when every archive revision observed in the
folder has its own current `assign`. Any archive without one leaves the
folder — and every key it contributes to — unresolved. This is what lets
`ERIKA` reach a correct outcome that a series-level model cannot express.

## The decision record

Append-only. Never updated, never deleted. Each entry:

```text
decision_id        sha256 over the canonical JSON of every field below
                   except itself (content-addressed: an id cannot be
                   reused for different content)
supersedes         decision_id of the entry this replaces, or null
kind               one of the kinds above
subject            {kind, identity fields, bound observation (see below)}
outcome            kind-specific: target series key/title, destination,
                   canonical path, disambiguated keys, per-archive map
candidates_shown   the candidate ids the operator was shown, with their
                   scores and sources (ordering evidence, never authority)
evidence_refs      report fingerprint; local evidence (cover/ComicInfo
                   digests) and provider candidate snapshots consulted
rationale          free text, required and non-empty
decided_by         operator identity (never a model or rule)
decided_at_utc     ISO-8601 UTC; displays may render local time
tool               name and version of the review surface that wrote it
```

`decided_by` must be a person. A deterministic approved rule that resolves
an unambiguous case (rule 1) is recorded differently — as the rule's
own output with its version — and never with `decided_by`, so the two
authorities can never be confused.

## Freshness: a decision is bound to what it was made against

Every subject records the **bound observation**: the exact entry set (for a
key), or path plus revision set (for a folder), or `archive_sha256` (for an
archive), as observed in the fingerprinted report the operator reviewed.

At gate time a decision is **current** only if:

1. it is not superseded or withdrawn, **and**
2. its bound observation equals the subject's state in the observation
   being evaluated — same entry set, same path and revision set, same
   archive digest.

Otherwise it is **stale**, and a stale decision counts as unresolved. It is
never re-applied silently, never "probably still right". Consequences,
chosen deliberately:

- a new folder joining a decided key makes the key stale: someone must look
  at the newcomer;
- an archive rewritten in place (new revision, new digest) makes its
  `assign` stale;
- a moved archive makes a *folder* decision stale but not its *archive*
  decision, because archive identity is content, not location;
- re-confirming is cheap by design: the review surface offers "unchanged —
  reconfirm" which writes a new entry superseding the stale one, bound to
  the new observation.

Removals require a rebuild before evaluation (as the diagnostics already
require); a decision never clears because its subject disappeared from an
incremental view.

## Where it lives

The records carry live library paths, so they do not go into Git (the
standing rule for reports). Options:

| Option | For | Against |
|---|---|---|
| **A. Table in the production database**, behind a DAL repository; append-only enforced by triggers rejecting UPDATE/DELETE | one transaction owner (DAL); backed up with the database; queryable | needs a migration, which queues behind migration 015 |
| B. Hash-chained JSONL under `G:\ComicAutomation\` | no migration | a second store with its own integrity, locking and backup story |

**Recommendation: A**, as the first migration after 015 is settled, with a
JSONL export/import for review and backup that round-trips byte-exactly.
B is rejected as a primary store: the DAL exists precisely so there is one
transaction owner.

## How the gate consumes it

```text
evaluate_authority(observation, decisions) -> GateReport
  refuse if the observation has scan_problems              (incomplete)
  for each empty_key:  needs a current not_a_series or assign/split
  for each collision key K:
      needs a current merge | distinct | alternate_edition on K,
      or every contributing folder resolved by current folder decisions
      (split requires per-archive assigns for every observed revision)
  verdict per key: resolved | unresolved | stale | undecided
  pass only if every key is resolved
```

The report lists every non-resolved key with the reason, the decision ids
consulted, and the observation fingerprint, so a "fail" is actionable and a
"pass" is reproducible. Passing remains evidence for a separate
activation decision.

## Candidate model (provider-neutral)

```text
candidate_id    stable per (source, source_id)
source          local_folder | provider:<name>
source_id       path for local; provider's own id otherwise
title, aliases  as retrieved, unnormalized, plus series_key(title)
evidence        cover digest, ComicInfo fields, counts — with retrieved_at
                and provider/adapter version
score, reasons  ordering aid only; recorded in candidates_shown
```

The model precedes any adapter (roadmap: "so it is provider-neutral rather
than shaped by whichever API arrived first"). Provider data is advisory
until an operator selects it (rule 6), and selection is a decision record.

## What implementation must prove (for the later slices)

- Append-only: UPDATE and DELETE are refused by the store, by name.
- Content addressing: the same content gives the same id; a one-field
  change gives a different id; a forged id is refused on load.
- Staleness: each consequence listed under "Freshness" has a test, and each
  freshness check, when removed, fails its test.
- Gate: incomplete scan refused; `unresolved`/`none_of_these` never pass;
  an incomplete split fails; stale never passes; superseded or withdrawn
  never count.
- No model or rule can write a `decided_by` record.
- The JSONL export/import round-trips byte-exactly and refuses a broken
  supersedes chain.

## Open questions for review

1. **Canonical series records** do not exist yet (Phase 4's first item).
   Should `outcome` reference a series by `series_key` plus display title
   for now, and gain a foreign key once records exist, or should canonical
   series records come first?
2. **Archive identity**: is `archive_sha256` the right binding for
   archive-level decisions, or the revision `id` (both are recorded; which
   one decides freshness)?
3. **Migration order**: confirm the table follows migration 015 rather than
   being folded into it.
4. **Deterministic rule outputs** (rule 1): same table with a distinct
   `authority = rule:<name>@<version>`, or a separate table?
5. **Report fingerprint**: align the exact fields with the ambiguity-report
   command so a decision's `evidence_refs` and the gate's observation use
   one definition.
