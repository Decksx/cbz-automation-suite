# GUI Architecture & Implementation Roadmap

## Status

**Future workstream — architectural North Star**

This document defines the target architecture and phased implementation plan for the
Comic Server Automation Suite GUI.

It does **not** activate a network-facing control plane today and does not supersede
the current implementation roadmap's deliberate deferral of FastAPI, remote control,
or a web frontend until the backend milestones and operational need justify them.

The purpose of this document is to freeze the safety invariants and sequencing now so
that future frontend work cannot outpace, duplicate, or weaken the backend architecture.

The preferred future implementation direction is a lightweight Python web control plane,
such as **FastAPI + server-rendered HTML + HTMX**, rather than a large single-page
application.

---

## 1. Architectural North Star

The GUI is an operational lens and guarded control surface for the automation suite.
It is **not** a second automation engine.

The system should escalate trust in this order:

```text
Observe
   ↓
Understand
   ↓
Decide
   ↓
Plan
   ↓
Approve
   ↓
Mutate
   ↓
Verify
   ↓
Recover
```

### Golden rule

> **Every GUI mutation must be possible without the GUI.**

The GUI may expose, validate, plan, invoke, and report on backend capabilities, but it
must never contain the only implementation of a routing rule, recovery action,
normalization rule, duplicate decision, archive rewrite, or other domain behavior.

A useful design test for every future GUI feature is:

> **If the browser closes at this exact moment, is the backend still in a valid,
> explainable, and recoverable state?**

If the answer is no, the workflow boundary is wrong.

---

## 2. Immutable Architectural Principles

### 2.1 API-first / no GUI-only logic

The frontend does not implement:

- archive parsing;
- hashing;
- series identity;
- filename normalization;
- routing;
- duplicate classification;
- metadata semantics;
- recovery rules;
- path-selection rules;
- lock semantics.

Existing and future Python domain services remain authoritative.

HTTP handlers should be thin adapters over reusable service/domain boundaries so the
same operation can be invoked by CLI, tests, worker code, or another future control
surface.

### 2.2 Dependency-scoped plans

Plans are pinned to the exact entities and state they depend on.

A plan is invalidated by relevant drift, such as:

- a source archive obtaining a new current revision;
- a source hash changing;
- an expected destination appearing or disappearing;
- configuration affecting the operation changing;
- a conflicting operator decision being recorded;
- a required entity becoming owned by another mutation operation.

Unrelated database activity must not invalidate the plan.

Do **not** introduce a coarse global `database_generation` solely for GUI freshness.

### 2.3 Revision-pinned mutations

Filesystem mutations are planned against specific logical archives and
`archive_revisions`, not merely path strings.

Paths are observations and destinations; they are not archive identity.

Where an operation depends on archive content, the plan must record at minimum:

- logical archive ID;
- expected current revision ID;
- expected archive SHA-256 where applicable;
- expected path/destination state;
- any configuration fingerprint that materially affects execution.

### 2.4 Authoritative entity-level locking

Mutation requires the narrowest authoritative exclusion mechanism suitable for the
operation.

Examples may include:

- per-archive operation ownership;
- per-series operation locking;
- database transaction ownership;
- recovery-case exclusivity;
- other durable cross-process coordination.

The implementation is intentionally not frozen to "locks in SQLite." The invariant is:

> Two independent processes must not concurrently perform incompatible mutations against
> the same archive or series.

The GUI never implements lock semantics itself. It only requests operations and surfaces
backend lock/ownership state.

### 2.5 Append-only decisions

The review model separates:

```text
Evidence
    immutable machine observation

Recommendation
    machine interpretation of evidence

Operator Decision
    human adjudication

Execution Plan
    proposed mutation

Execution Result
    what actually happened
```

A later decision supersedes an earlier decision. Historical records are not overwritten.

This allows recommendation algorithms to evolve without rewriting the history of what
the operator saw and chose at the time.

### 2.6 Secure by default

Before mutation endpoints exist, the GUI must have:

- authenticated operator identity;
- authorization;
- secure session handling;
- CSRF protection where applicable;
- auditable operator attribution.

For the earliest read-only phase, bind to localhost by default and do not expose an
unauthenticated remote administrative API.

### 2.7 Backend truth survives browser loss

Durable operational state belongs in backend storage, not browser memory.

If the browser closes during:

- plan validation;
- approval;
- execution;
- verification;
- recovery;

reopening the GUI must reconstruct the authoritative state from the backend.

---

## 3. Plan State Machine

All mutating workflows use one authoritative plan lifecycle.

### 3.1 Primary state flow

```text
DRAFT
  ↓
GENERATED
  ↓
VALIDATED
  ↓
AWAITING_APPROVAL
  ↓
APPROVED
  ↓
EXECUTING
  ↓
VERIFYING
  ↓
COMPLETED
```

### 3.2 Alternate terminal or refusal states

```text
STALE
VALIDATION_FAILED
EXECUTION_FAILED
RECOVERY_REQUIRED
SUPERSEDED
CANCELLED
```

Validation findings should be structured data, not a competing second lifecycle.

### 3.3 Approval is version-specific

Approval binds to the exact plan manifest the operator reviewed.

Recommended approval fields:

```text
approved_plan_id
approved_plan_version
approved_at
approved_by
approved_manifest_digest
```

If anything changes that alters the approved manifest, the previous approval is invalid.

The backend must never silently regenerate a plan after approval and then execute the
new version under the old approval.

---

## 4. Plan Data Contract

The exact table/schema design remains provisional until implementation, but the
behavioral contract is not.

Example:

```json
{
  "plan_id": "pln_01J5V...",
  "plan_version": 1,
  "created_at": "2026-08-13T14:22:00Z",
  "operation_type": "MERGE_ARCHIVE_CONTENT",
  "state": "AWAITING_APPROVAL",
  "prerequisites": {
    "archives": [
      {
        "archive_id": 9921,
        "expected_current_revision_id": 38114,
        "expected_sha256": "e3b0c..."
      },
      {
        "archive_id": 8812,
        "expected_current_revision_id": 37702,
        "expected_sha256": "4ac9e..."
      }
    ],
    "destinations": [
      {
        "path": "\\\\server\\library\\Series\\Issue.cbz",
        "expected_state": "ABSENT"
      }
    ],
    "configuration_fingerprint": "a1b2c3d4..."
  },
  "operations": [],
  "approval_record": {
    "approved_by": "operator_1",
    "approved_at": "2026-08-13T14:25:00Z",
    "approved_manifest_digest": "f4c9..."
  },
  "recovery_strategy": "RETAIN_SOURCE_UNTIL_VERIFIED"
}
```

### 4.1 Execute-time revalidation

`execute` must re-evaluate all prerequisites immediately before mutation.

If an assumption has drifted, execution is refused and the plan becomes stale or
otherwise non-executable.

A structured `409 Conflict` response should identify the exact reason.

Example:

```json
{
  "error": "PLAN_STALE",
  "reason": "SOURCE_REVISION_CHANGED",
  "archive_id": 9921,
  "expected_revision_id": 38114,
  "current_revision_id": 38119
}
```

Other machine-readable conflict reasons may include:

```text
DESTINATION_APPEARED
DESTINATION_DISAPPEARED
CONFIGURATION_CHANGED
CONFLICTING_DECISION
ENTITY_BUSY
PLAN_SUPERSEDED
SOURCE_HASH_CHANGED
```

The GUI displays these results. It does not independently determine them.

---

## 5. GUI Workspaces

### 5.1 Persistent Global Status Bar

Compact system state only:

- pending jobs;
- active jobs;
- active workers;
- estimated active processing time;
- archives/hour;
- terminal-failure rate;
- SQLite/WAL health summary;
- watcher/routing mode;
- staging mode.

Detailed diagnostics belong in the Dashboard.

### 5.2 Dashboard / Operations

Read-oriented operational control center:

- queue state by job type;
- worker inventory and heartbeat;
- throughput;
- retry rates;
- terminal failures;
- stale/orphaned job indicators;
- recent operational events;
- hashing/inspection coverage;
- links into Recovery where intervention is needed.

### 5.3 Library & Routing

Expose effective configuration rather than merely editing files.

For each setting, the GUI should be able to show:

```text
effective value
source
editable?
validation result
restart/reload required?
```

Example:

```json
{
  "routing_mode": {
    "value": "off",
    "source": "environment",
    "editable": false
  }
}
```

Routing-v2 activation controls remain unavailable until backend gates permit them.

### 5.4 Review & Deduplication

Primary evidence-review workspace.

Display:

- logical archive ID;
- revision ID;
- current/historical state;
- observed timestamp;
- SHA-256;
- path;
- page count;
- exact page overlaps;
- dHash/pHash differences;
- dimensions;
- missing/reordered pages;
- metadata differences.

The workspace must separate:

```text
Evidence
Recommendation
Human Decision
Plan
Execution Result
```

#### Evidence strength

Do not display heuristic scores as percentages unless they are genuinely calibrated
probabilities.

Prefer:

```text
Very High
High
Moderate
Low
```

with the underlying evidence shown directly.

Example:

```text
Exact overlap: 22 / 24 pages
Perceptual differences: 2 pages
Metadata disagreement: 1 field group
```

### 5.5 Side-by-side image comparison

Use an image-preview service boundary.

Do not repeatedly send raw full-resolution source pages from SMB archives for normal
canvas display.

Preferred boundary:

```text
revision + page
    ↓
authoritative archive reader
    ↓
bounded decode
    ↓
preview derivative
    ↓
browser
```

Future implementations may add full-resolution tile/source access for explicit deep zoom.

### 5.6 Sanitization

The GUI may select backend-supported rules and scopes, but normalization and rewrite
logic remain in authoritative domain services.

All destructive changes flow through plans.

### 5.7 Metadata

Support targeted/bulk `ComicInfo.xml` changes through plan generation.

Show:

- old metadata;
- proposed metadata;
- field-level diffs;
- validation errors;
- affected archive/revision identities.

Archive rewrites must use guarded stage-and-swap behavior and create/record the resulting
revision after verification.

### 5.8 Plans & Execution

Expose durable plan state:

- generated plans;
- validation status;
- staleness;
- approvals;
- manifest digest;
- execution progress;
- verification result;
- recovery-required state.

The GUI must not treat a plan as indefinitely valid merely because the operator viewed
it earlier.

### 5.9 Jobs & Workers

Initially read-only.

Expose:

- jobs by type/state;
- current claims;
- worker identity;
- heartbeats;
- retries;
- terminal failures;
- archive job history.

Do not expose a generic "cancel job" button until each relevant job type has an explicit,
proven-safe cancellation contract.

### 5.10 Recovery

Recovery is a first-class workflow, not a generic error page.

Expose:

- recovery case;
- assessment/classification;
- current evidence;
- source/destination state;
- rollback availability;
- quarantine state;
- recommended action;
- operator decision;
- refusal/guard reason.

Do not expose generic state surgery such as "clear stale lease" unless that exact action
has an authoritative backend safety contract.

### 5.11 Configuration / Administration

Later-phase workspace for:

- effective configuration;
- routing roots;
- resource limits;
- worker settings;
- review thresholds;
- retention/pruning settings where supported;
- feature/mode status.

Mutating configuration uses the same guarded principles as other state changes.

---

## 6. Append-Only Decision Ledger

The exact schema remains provisional, but the logical record should support:

```text
decision_id
review_case_id / candidate_id
archive_revision_ids
recommendation_version
recommendation_snapshot
decision
operator
decided_at
notes
supersedes_decision_id
```

A changed decision creates a new record referencing the prior one.

The raw candidate/evidence record remains intact.

---

## 7. Service Layer Before HTTP Layer

Define reusable backend services before the HTTP surface.

Illustrative service boundaries:

```text
TelemetryService
ReviewService
PlanService
ExecutionService
RecoveryService
ConfigurationService
PreviewService
```

FastAPI handlers should primarily:

1. authenticate/authorize;
2. validate transport-level input;
3. invoke the domain/service operation;
4. translate the result to HTTP;
5. return structured errors.

This preserves the golden rule that all mutations remain usable outside the GUI.

---

## 8. Resource-Oriented API Direction

Endpoint names remain provisional, but the resource boundaries should follow this shape.

### Telemetry / jobs

```text
GET /api/telemetry/summary
GET /api/jobs
GET /api/jobs/{job_id}
GET /api/workers
```

### Evidence / archives

```text
GET /api/candidates
GET /api/candidates/{candidate_id}
GET /api/archives/{archive_id}
GET /api/archives/{archive_id}/revisions
GET /api/revisions/{revision_id}/pages/{page_number}
```

### Decisions

```text
POST /api/operator-decisions
```

### Plans

```text
POST /api/plans
GET  /api/plans/{plan_id}
POST /api/plans/{plan_id}/validate
POST /api/plans/{plan_id}/approve
POST /api/plans/{plan_id}/execute
```

### Recovery

```text
GET  /api/recovery/cases
GET  /api/recovery/cases/{case_id}
POST /api/recovery/cases/{case_id}/actions
```

Critical state transitions use explicit operations rather than ambiguous generic updates.

---

## 9. SQLite Access Model

Do not require a traditional connection pool as part of the architecture.

Prefer a shared GUI database access/service layer with:

- short-lived connections where appropriate;
- WAL-aware reads;
- bounded busy timeouts;
- explicit writer serialization where required;
- authoritative transaction boundaries;
- no assumption that HTTP is required for local high-throughput work.

SQLite remains the operational database while the project remains single-host and
single-operator unless measured need justifies a change.

---

## 10. Locking and Concurrency

Do not create a global "lock the job queue" execution model.

Unrelated work such as:

- hashing;
- archive inspection;
- watcher ingestion;
- review of another archive;

should continue when safe.

Use the narrowest backend exclusion required for the mutation.

The UI may display that an operation is waiting for an entity, but must not own the lock
implementation.

---

## 11. Audit and Observability

Stable IDs should be designed early and used consistently across:

- database rows;
- API responses;
- logs;
- UI links;
- recovery cases.

Important IDs are likely to include:

```text
archive_id
revision_id
candidate_id / review_case_id
decision_id
plan_id
execution_id
recovery_case_id
job_id
processing_run_id
```

Important operator events include:

- recommendation created;
- decision recorded;
- plan generated;
- validation passed/failed;
- plan approved;
- execution started;
- execution completed/failed;
- verification completed/failed;
- recovery case created;
- recovery action selected;
- quarantine action;
- configuration change.

Raw technical logs and higher-level operator activity should remain distinguishable.

---

## 12. Technology Direction

Preferred future stack:

- FastAPI;
- server-rendered HTML;
- HTMX for incremental updates;
- existing Python domain modules as authority;
- SQLite as the operational store;
- existing persistent jobs/workers for heavy background work.

Long archive-processing work must not run synchronously inside HTTP request handlers.

A full SPA framework is not required unless later interaction complexity demonstrates a
measured need.

---

## 13. Phased Implementation Sequence

The GUI earns increasing authority over time.

### Phase 1 — Read-Only Control Plane

Prerequisites:

- current SQLite operational model;
- no destructive API.

Deliver:

- localhost-secure FastAPI shell;
- authentication foundation;
- system health;
- queue telemetry;
- job state;
- worker state;
- operational logs/activity.

Goal:

Gain useful visibility without changing production behavior.

### Phase 2 — Evidence & Revision Visualization

Prerequisites:

- immutable archive revisions available;
- provenance/evidence relationships sufficient for review.

Deliver:

- candidate queue;
- evidence ledger;
- archive/revision views;
- bounded image previews;
- side-by-side visual comparison.

Goal:

Allow human review of exact/perceptual evidence with no filesystem mutation.

### Phase 3 — Durable Human Review

No filesystem mutation.

Deliver:

- append-only operator decisions;
- mark distinct;
- defer;
- preferred-revision decisions;
- quarantine recommendation/decision records;
- recommendation snapshots/versioning.

Goal:

Capture durable human adjudication before trusting the GUI with execution.

### Phase 4 — Plan & Validation Framework

No execution.

Deliver:

- plan generation;
- dependency snapshots;
- structured validation;
- staleness detection;
- manifest diff;
- plan versioning;
- approval records;
- manifest digest.

Goal:

Prove the full decision-to-plan safety path without mutating files.

### Phase 5 — Guarded Execution

Prerequisites:

- authoritative cross-process entity exclusion;
- proven plan revalidation;
- durable execution/recovery linkage.

Deliver:

- quarantine execution;
- metadata mutation;
- local stage-and-swap;
- archive replacement/merge operations;
- verification;
- resulting archive-revision recording;
- recovery handoff on failure.

Goal:

Permit destructive actions only after the read, review, plan, validation, and approval
layers have proven stable.

### Phase 6 — Recovery & Operational Governance

Deliver:

- recovery assessment;
- rollback visibility;
- guarded recovery actions;
- interrupted-operation handling;
- terminal-failure operational workflows.

Goal:

Make exception handling a structured administrative workflow.

### Phase 7 — Configuration / Administration

Deliver:

- effective configuration model;
- configuration provenance/source;
- editability;
- validation;
- required reload/restart visibility;
- safe administrative controls.

### Phase 8 — Future Routing-v2 Controls

Only after backend gates permit v2-active routing.

The GUI must never become a mechanism for bypassing routing-v2 readiness gates.

---

## 14. Initial Non-Goals

The initial GUI must not:

- duplicate domain rules in frontend code;
- activate watcher v2 prematurely;
- activate staging prematurely;
- introduce another series-identity implementation;
- execute stale plans;
- treat paths as archive identity;
- present uncalibrated heuristic scores as probabilities;
- hold global locks where narrow exclusion is sufficient;
- perform long archive work synchronously in HTTP requests;
- expose unsafe generic job cancellation;
- expose unsafe generic lease manipulation;
- hide recovery-required states behind generic errors;
- depend on browser memory for durable operational truth;
- force a traditional SPA or distributed architecture without measured need.

---

## 15. Relationship to the Main Implementation Roadmap

The main implementation roadmap currently and intentionally defers a network-facing
FastAPI control plane and full frontend work while core backend work remains active.

This document does not reverse that decision.

Instead, it defines the architecture that should be followed **when the GUI workstream is
activated**.

Activation should be tied to backend readiness rather than calendar date.

In particular:

- immutable archive revision semantics must be sufficiently established before
  revision-aware review;
- evidence/provenance must be sufficiently stable before the review UI depends on it;
- durable decisions must precede mutation;
- plans/validation must precede execution;
- authoritative cross-process exclusion must precede destructive GUI operations;
- recovery support must mature alongside execution;
- watcher-v2 activation remains separately gated.

---

## 16. Frozen vs. Provisional Decisions

### Frozen architectural invariants

Treat these as the North Star:

- no GUI-only business logic;
- every GUI mutation is possible without the GUI;
- dependency-scoped freshness;
- revision-pinned mutation;
- version-specific approval;
- execute-time revalidation;
- narrow authoritative locking;
- append-only evidence/recommendation/decision history;
- secure mutation endpoints;
- durable backend truth;
- mandatory verification;
- first-class recovery;
- phased escalation of trust.

### Provisional implementation details

These may change as backend work matures:

- exact FastAPI endpoint names;
- exact SQL table names;
- exact identifier encoding;
- precise plan JSON field names;
- exact authentication provider;
- exact preview cache implementation;
- exact entity-lock persistence mechanism;
- exact configuration storage representation.

Changing a provisional detail must not weaken a frozen invariant.

---

## 17. Summary

The GUI should become a safe administrative control plane over the Comic Server
Automation Suite, not a parallel automation implementation.

The defining properties are:

- evidence is preserved;
- recommendations are distinguishable from decisions;
- decisions are append-only;
- plans are durable and dependency-pinned;
- approvals bind to exact manifests;
- execution revalidates;
- locks are narrow and authoritative;
- mutation is verified;
- failures become structured recovery states;
- backend services remain canonical;
- browser state never substitutes for authoritative backend state.

The implementation should proceed only as fast as the backend architecture can support
these guarantees.
