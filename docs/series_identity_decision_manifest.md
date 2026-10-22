# Series identity decisions: candidate model and decision manifest (design)

Status: **design only**, for review. Nothing here is implemented, migrated,
or applied. Phase 4 step 5 of `docs/implementation_roadmap.md` ("define a
provider-neutral candidate model and durable human-decision manifest"),
built against the binding rules in "Human-reviewed identity resolution
(#57)" and the decision "A human decision is the only resolution of an
ambiguous identity" in `docs/engineering_decisions.md`.

Author: claude-lead. Reviewer: codex-lead. First draft `60a4b74`
(2026-10-22); this revision answers codex-lead's review of that head
(PR #99): observation enrichment, trusted approval provenance, supersession
conflicts, and the line between a recorded decision and usable routing
authority.

## What this is for

The v2 series index may become authoritative only when ambiguity is zero
**or every remaining ambiguous key carries a reviewed exception/identity
manifest** (rule 8). The zero branch is executable today
(`SeriesIndexDiagnostics.require_clear()`). The manifest branch has no
design: "no unchecked key list waives this stricter gate". This document
defines what a reviewed decision is, who may make one and how that is
proven, how decisions supersede each other without forks, how they go
stale, and — separately — when a decision has actually become something the
index may consume.

## Non-goals

- No activation of v2 authority, routing, staging, or moves. The gate
  evaluates one observation; passing it is evidence for a separate
  activation decision, never the decision.
- No automatic resolution. Scores order candidates for the operator; they
  never decide (rules 1-2).
- No schema, migration, or repository code in this slice.
- No ComicInfo or filesystem mutation. Those remain separate
  content-addressed plan/apply operations (rule 7).

## 1. The observation a decision is made against

The directory report (#98/#101, `docs/series_ambiguity_report.md`) is
**directory-level only**: configuration bytes and path, the confirmed
roots, the directory names it enumerated, and the `SeriesIndex`
diagnostics, under a schema-versioned `report_sha256`. It reads no archive
and no database, by design. It therefore cannot carry folder revision sets
or archive digests, and this design does not pretend it does.

A decision is made against an **observation envelope**, versioned on its
own, with two components:

```text
envelope_schema      1
directory_report     {schema_version, report_sha256}        (from #101)
revision_inventory   {schema, coverage, acquired, rows, inventory_sha256}
envelope_sha256      sha256 over the canonical JSON of the above
```

The **revision inventory** is a separate read-only producer (not built yet)
that, for exactly the folders named in the directory report, lists each
archive's logical identity and current revision from the production
database:

```text
row: folder_path, archive_id, current_revision_id,
     archive_sha256 (or null), identity_state ('established'|'provisional')
coverage: every folder in the directory report's collisions and empty keys,
          listed explicitly; a folder not covered is not observed
acquired: WAL-aware read — PRAGMA data_version before, one deferred read
          transaction, data_version after; a changed pair rejects the
          inventory (the read-only audit rule in CLAUDE.md)
```

**Until that inventory exists, folder- and archive-level dispositions
cannot pass the gate.** Key-level dispositions bound only to the directory
report can be recorded, but are still subject to section 4.

### Archive freshness rules (open question 2, answered)

An archive-level decision binds `archive_id + revision_id + archive_sha256`
and is current only when the inventory shows that archive's
`archive_files.current_revision_id` equal to the bound `revision_id`, with
the same established digest.

- **A provisional revision (null digest) is never a match.** `archive_sha256`
  is null exactly when the bytes were never hashed (147 archives at
  migration 014). Two nulls are not evidence of the same bytes, so a
  decision cannot be bound to a provisional revision at all, and a bound
  decision whose archive is now provisional is stale.
- **Reappearance of an older byte state.** Revisions are unique per
  `(archive_id, archive_sha256)`, so an archive that returns to bytes it
  had before points back at that earlier revision. A decision bound to that
  revision becomes current again. This is deliberate: the decision was
  about those exact bytes, and they are back. It is reported, not hidden —
  the gate lists "current again after intervening revision N" — and any
  decision made against the intervening revision remains in the history.
- A **folder**-level binding is the folder path plus the exact set of
  `(archive_id, current_revision_id)` observed in it. A new archive, a
  removed archive, or any archive's revision change makes it stale.

## 2. Subjects and decisions

Ambiguity is a property of a folder, not only of a series (the `ERIKA`
case: 3 adult archives among 9 horror ones). A decision names one subject.
Each subject has a **stable `subject_id`**, independent of any observation,
and a **bound observation**, which changes as evidence changes:

| Subject | `subject_id` | Bound observation |
|---|---|---|
| `series_key` | `key:<canonical series_key>` | its exact `(destination, path)` entry set in the directory report |
| `folder` | `folder:<lexical path identity>` | path plus `(archive_id, current_revision_id)` set from the inventory |
| `archive` | `archive:<archive_id>` | `revision_id + archive_sha256` |

Paths use the index's own lexical identity (host case policy, absolute
normalized, no symlink/junction/alias resolution).

Decision kinds (from Phase 4 step 9, made explicit):

| Kind | Meaning | Subject |
|---|---|---|
| `assign` | belongs to canonical series S | folder or archive |
| `merge` | the colliding paths are one series; one canonical home | series_key |
| `distinct` | same key, different series; each path maps to its own series | series_key |
| `alternate_edition` | same work, edition kept apart deliberately | series_key or folder |
| `split` | a mixed folder's archives belong to ≥2 series | folder |
| `not_a_series` | not a series at all | folder |
| `none_of_these` | no offered candidate is right | any |
| `unresolved` | reviewed and explicitly left open | any |
| `withdraw` | retract the current decision without replacing it | any |

Outcomes reference **canonical series by a stable local id**
(`series_id`), with titles and aliases as attributes; a mutable
`series_key` alone cannot identify distinct outcomes (codex-lead's answer to
open question 1, adopted). That makes **canonical series records a
prerequisite** of implementation: a minimal append-only series table
(`series_id`, created-by receipt, display title, aliases) comes first.

`none_of_these` and `unresolved` are recorded decisions, not absences; both
count as unresolved for the gate.

## 3. The decision record and who may write it

Append-only; never updated, never deleted.

```text
decision_id      sha256 over the canonical JSON of every field below except
                 itself (record integrity, not authorship)
subject_id       stable subject identity (section 2)
supersedes       decision_id of the subject's current head, or null for the
                 subject's first decision
kind, outcome    as above; outcome uses series_id
bound            the bound observation and the envelope_sha256 it came from
candidates_shown candidate ids shown, with scores and sources
rationale        required, non-empty
authority        typed: {type: operator_receipt, receipt_id}
                     | {type: rule, name, version}
```

### Approval provenance (trusted ingestion boundary)

A content hash proves the record was not altered; it does not prove who
approved it. A self-asserted operator name is not provenance. So:

- **One component may admit operator decisions**: the operator review
  command (CLI first, GUI later). No library function, script, job handler,
  or import path can create an `operator_receipt` authority.
- **The receipt is anchored in an authenticated operator event.** The review
  command shows the operator the decision and its `decision_id`, and admits
  it only after an approval posted **by the hub's authenticated `admin`
  identity** that names that exact `decision_id` (for example a hive
  message `APPROVE <decision_id>`). The hub records sender identity from the
  credential that authenticated the post, not from message content. The
  receipt stores: hub message id, sender as recorded by the hub, the
  `decision_id`, and the envelope fingerprint the decision was shown with.
- **Admission verifies the receipt at write time** (message exists, sender
  is `admin`, it names this `decision_id`) inside the same transaction that
  appends the decision; a decision without a verified receipt is refused.
- **Rule outputs** (rule 1: exactly one unambiguous result under an approved,
  deterministic, tested rule) use `authority.type = rule` with name and
  version. They can never carry an operator receipt, and the two are
  distinguishable by type, not by convention (open question 4, answered).
- **Import** (JSONL, for backup and review) re-verifies every record: hash
  consistency, then the receipt against the hub's message record. A record
  whose receipt cannot be verified is refused, never admitted inert-but-
  present; a forged `decided_by`-style field has no effect because no field
  confers authority — only a verified receipt does.

**Threat model, stated honestly.** This boundary excludes model output,
rule code, scripts, and forged or edited imports from creating human
authority. It does not defend against an attacker who controls the
operator's own account and the admin credential: on this single-user
machine the leads and the operator share an OS account, so the protection
is that no code path mints authority without a hub-authenticated admin
event, plus the standing rule that agents never post as admin. The hub
record is the audit trail if that rule were broken.

## 4. Supersession without forks

Append-only stores can accept two children of one head, or two "first"
decisions for one subject, each looking current. That is ruled out
structurally, not by convention:

- **Admission is compare-and-set on the subject's head**, inside one
  `BEGIN IMMEDIATE` transaction: read the current head for `subject_id`; the
  new decision's `supersedes` must equal it (or be null only when the
  subject has no decisions). Anything else is refused.
- **The store enforces linear chains**: `UNIQUE(supersedes)` where not null
  (no two children of one parent) and a partial `UNIQUE(subject_id)` where
  `supersedes` is null (one root per subject).
- **Cross-subject supersession is refused**: the superseded decision must
  have the same `subject_id`.
- **`withdraw`** supersedes the head; the subject is then undecided.
- **Import** must present parents before children; a missing parent, a
  second root, or a second child of one parent refuses the whole import.
  A fork that arrives anyway (for example from an older export) marks the
  subject **conflicted**, which blocks the gate until an operator writes a
  decision superseding a chosen head. Clock order never picks a winner.

## 5. Decision, reconciliation, and routing authority

A recorded decision is intent. The index can only consume a **routing
mapping**. The gate keeps three things separate:

1. **Recorded**: a current, non-stale, verified decision exists.
2. **Compatible**: the dispositions on one key agree. Two current `assign`s
   placing folders under the same key into different `series_id`s conflict
   unless a key-level `distinct` covers that key and maps each path. A
   `merge` and a `distinct` on the same key conflict. Conflicts block.
3. **Reconciled**: anything the outcome requires has actually happened, and
   the routing mapping the index consumes encodes it:
   - `merge` needs the non-canonical paths empty or moved (an executed,
     verified move plan) — the directory report must show one home;
   - `split` needs the per-archive moves executed — the folder must no
     longer be mixed in a fresh observation; a complete set of per-archive
     `assign`s alone is a *planned* split and does not pass;
   - `distinct` and `alternate_edition` need the routing configuration to
     carry the mapping (alias/override entries that route each path to its
     own series), visible in the config bytes the directory report hashed;
   - `not_a_series` needs the routing policy to exclude that path.

**Destination priority never overrides a human outcome.** If, for a resolved
key, the routing configuration would send new arrivals somewhere other than
the decided canonical home, the key is blocked, not "resolved by priority".

```text
evaluate_authority(envelope, decisions, routing_config) -> GateReport
  refuse if the directory report has scan_problems        (incomplete)
  refuse folder/archive dispositions without a revision inventory
  per key: recorded? compatible? reconciled? -> resolved | unresolved |
           stale | conflicted | planned_not_applied | undecided
  pass only if every key is resolved
```

The report names every non-resolved key, the reason, the decisions
consulted, and the envelope fingerprint, so a fail is actionable and a pass
reproducible. A pass is evidence for a separate activation decision.

## 6. Candidate model (provider-neutral)

```text
candidate_id    stable per (source, source_id)
source          local_folder | provider:<name>
source_id       path for local; provider's own id otherwise
title, aliases  as retrieved, plus series_key(title)
evidence        cover digest, ComicInfo fields, counts, retrieved_at,
                adapter version
score, reasons  ordering aid only; recorded in candidates_shown
```

The model precedes any adapter so it is not shaped by the first API.
Provider data stays advisory until an operator selects it (rule 6), and a
selection is a decision with a verified receipt.

## 7. Storage

Records carry live library paths, so they never go into Git. **An
append-only table in the production database**, behind a DAL repository,
with triggers that refuse UPDATE and DELETE, the uniqueness constraints in
section 4, and a byte-exact JSONL export/import that re-verifies receipts.
It is **not** folded into migration 015, whose table rebuild is already
scoped; it takes a later migration number once 015's dependency is
settled (open question 3, answered). The canonical series table (section 2)
lands with or before it.

## 8. Negative tests the implementation must have (named)

- `test_two_conflicting_folder_assigns_under_one_key_block_the_key`
- `test_merge_and_distinct_on_one_key_conflict`
- `test_complete_per_archive_assigns_are_a_planned_split_not_a_pass`
- `test_unapplied_merge_still_showing_two_homes_blocks`
- `test_distinct_without_routing_aliases_blocks`
- `test_destination_priority_disagreeing_with_outcome_blocks`
- `test_provisional_null_digest_never_matches`
- `test_reappeared_revision_is_current_and_reported`
- `test_new_archive_in_a_decided_folder_makes_it_stale`
- `test_second_child_of_one_parent_is_refused`
- `test_second_root_for_a_subject_is_refused`
- `test_cross_subject_supersession_is_refused`
- `test_forked_import_marks_the_subject_conflicted`
- `test_decision_without_verified_admin_receipt_is_refused`
- `test_import_with_unverifiable_receipt_is_refused`
- `test_rule_authority_cannot_carry_an_operator_receipt`
- `test_folder_decision_without_revision_inventory_cannot_pass`
- `test_incomplete_directory_report_is_refused`
- `test_update_and_delete_are_refused_by_the_store`

Each guard must be shown load-bearing: removing it fails its named test.

## 9. Open questions (remaining)

1. **Receipt channel**: is a hub `admin` message the right authenticated
   operator event, or should the review GUI hold its own operator
   credential? (The hub is the only operator-authenticated channel that
   exists today.)
2. **Alias representation**: `distinct`/`alternate_edition` reconcile
   through routing configuration today (`series_overrides`); should the
   canonical series table replace that list before this gate is used?
3. **Inventory producer ownership** and whether it runs as a standalone
   read-only command alongside the directory report or inside it under a
   second schema.
