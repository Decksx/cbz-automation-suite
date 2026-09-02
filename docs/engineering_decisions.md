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

## Resource limits are explicit and pinned

Decode and read limits belong in this codebase, not in whatever default
a dependency happens to ship. `Image.MAX_IMAGE_PIXELS` is pinned
explicitly in `perceptual_hashing.py` so a Pillow upgrade cannot
silently move the decompression-bomb threshold, and a declared-size cap
(`MAX_PAGE_UNCOMPRESSED_BYTES`) bounds the per-page `archive.read()`
allocation before decoding.

The two limits are independent and both are needed: a small compressed
file can still decode to a huge image, and a page can pass the pixel
check while its declared size is implausible. Caps are safety ceilings
set well above any legitimate library page, not operational tuning.

Where a limit's enforcement behavior belongs to the dependency rather
than to us — Pillow warns above the pinned value but only errors above
twice it — say so in the comment rather than implying we chose it.

## Staleness is re-checked before every destructive step

Any operation that reads a file, builds a replacement, and then
overwrites the original must snapshot size/mtime before the read and
re-verify immediately before the destructive step. This is the same
before/after `stat()` pattern the read-only hashing path already used;
it now also guards `_write_cbz_with_comicinfo`, `write_comicinfo`, and
`pack_image_folder`.

Detection behavior follows each module's existing error model rather
than imposing a new one: the sanitizer routes drift into its retry
loop, while library maintenance — which has no retry logic anywhere —
abandons the operation and counts an error. Consistency of *detection*
matters; uniformity of *recovery* does not.

This detects drift. It does not make replacement atomic. Collapsing the
multi-step backup/rename/unlink sequences into a single `os.replace()`
stays deferred until SMB rename semantics are validated directly, not
inferred from local NTFS behavior.

## Retry policy for one failure class is uniform within a module

A "file locked" `OSError` is one condition and gets one policy. The
sanitizer's two retry loops previously used 0.5s and 5s for the same
transient SMB condition; both now share module-level constants. Longer
backoff was preferred: a held lock is likelier to clear after seconds
than milliseconds, and repeatedly hammering a network share is worse
than waiting.

Modules may still differ from each other where that difference is
deliberate.

## Audits record evidence; fixes record resolution

Audit documents are preserved unedited as the evidence record of the
code at audit time. When a finding is closed, annotate it inline and
add a resolution log at the top — never rewrite the original finding to
describe current code, which would destroy the record of what was
actually observed and why.

A resolution log states what landed, what was deliberately not done and
on what authority, and what remains open.

## Deletion is delayed

Use quarantine and review before permanent deletion even though a separate library backup exists.

## External metadata is evidence

Komga and Komf identifiers and titles feed the local series model, but provider and timestamp provenance must be retained.

## A human decision is the only resolution of an ambiguous identity

Extends "Uncertain series matches require review" from fuzzy local
matching to external providers, and strengthens "External metadata is
evidence" into a rule about who may act on it. Recorded 2026-08-14 on
issue #57, because until then this gate existed only as an operator
ruling carried between sessions — and a limit that lives only in a
transcript is invisible to whoever later decides whether to rely on the
capability.

Programmatic matching may retrieve, score, rank, and explain candidates,
and may resolve an identity automatically only when an approved,
deterministic, tested rule finds exactly one unambiguous result with no
material contradictory evidence. A confidence score or threshold alone
does not make an ambiguous identity authoritative. Multiple plausible
candidates, conflicting identity evidence, mixed folders, and merge or
split decisions whose identity remains uncertain require explicit human
review. A materially ambiguous identity is never resolved automatically
by score, provider order, popularity, current placement, or index
priority. A reviewed human decision overrides conflicting programmatic
proposals, and stands until another reviewed decision supersedes it.

The distinction is between confidence and authority. A high-scoring
candidate is stronger evidence than a low-scoring one, and neither is by
itself a decision. Authority comes from the rule that consumed the
evidence — approved, deterministic, and tested — or from a person; never
from a score crossing a line. Raising a threshold changes how much
evidence is demanded before acting, but it cannot turn a contested
identity into an uncontested one, because the rival candidate is still
there and the rule is now merely more confident about ignoring it.

So the question a rule must answer is not "how good is the best
candidate" but "is there exactly one". Ranking earns its place either
way: it makes the operator's choice cheap where review is required, and
makes the single unambiguous case cheap to recognise where it is not.

**Current placement is not evidence of correct placement.** Deriving an
identity from where a series currently sits infers the answer from the
thing under question — the same circularity that excludes
`SourceMihon=komga` from the adult signal, which means "re-imported from
the Komga library". Index priority resolves a routing decision at
runtime so archives are not stranded; it does not adjudicate what a
series is.

**Ambiguity is a property of a folder, not only of a series.** Mixed
folders must support archive-by-archive assignment and splitting. `ERIKA`
is the worked example: 3 confirmed adult archives among 9 horror ones,
adjudicated a true positive at the series level while remaining
unresolved pending an archive-level split. A series-level model cannot
express its correct outcome, so a resolution model that only assigns
whole series is not sufficient.

External metadata and covers stay advisory until the operator selects
them. Candidate search is read-only; changing ComicInfo is a separate
content-addressed plan/apply operation carrying source-revision
revalidation, backup, and audit history — the same shape as every other
guarded mutation here, and for the same reason: a plan reviewed against
one source state must refuse to apply to another.

### The v2 index authority gate

Before the v2 series index becomes authoritative, ambiguity must equal
zero, or every remaining ambiguous key must carry a reviewed
exception/identity manifest.

Logging `ambiguous_series=True` while continuing to route does **not**
satisfy this. The flag records that the library disagrees with itself; it
resolves nothing. Enabling index authority over unresolved duplicates
would encode that disagreement as an authoritative prior decision, and
the first production index build would make it one — after which the
evidence that it was ever ambiguous is a log line.

## Office PC is the worker

CPU-intensive scanning, image decoding, hashing, and GPU embeddings run on the Office PC. The Pi 5 is for dashboards, scheduling, and health checks.

## Library volume filesystem and access path are architectural

The archive-rewrite guards compare `(st_size, st_mtime_ns)` before and
after the read-rebuild window. How well that works is a property of the
volume, not of the code, and it was measured on 2026-08-02:

| Volume | Same-size concurrent replacement detected |
| --- | --- |
| `X:\` as exFAT (until 2026-08-02), measured **2-second** `st_mtime_ns` quantum | 5/16 |
| `X:\` as NTFS 4K (reformatted 2026-08-02, re-verified 2026-08-03) | 10/10 |
| Local NTFS, timestamp resolution finer than 1.5 ms | 16/16 |
| `\\tower\media` — SMB, writer on the server | 0/6 within ~10 s (**every** change type, including size) |

Effectiveness depends on **access path and filesystem together**, not on
either alone. Both have to be right; a fine-grained filesystem reached over
a caching network path is no better than a coarse local one, and worse.

**The filesystem half.** exFAT exposes the raw DOS 2-second timestamp; the
10 ms increment field does not surface through Windows. Any same-size
change landing in the same 2-second bucket as the file's previous write was
invisible, leaving size as the only reliable change signal. `X:\` was
reformatted to NTFS (4 KB clusters) on 2026-08-02 — new volume serial
`0x66895a31`, library copied out to `D:` and back — and re-measured on
2026-08-03: 400/400 distinct timestamps, and same-size replacement detected
10/10 where exFAT was 0/20. That enabler is gone, eliminated by the format
rather than by code.

**The access-path half.** SMB is worse than exFAT was, not better: the
Windows client caches attributes *and* file data, so for roughly ten
seconds a remote change is invisible regardless of type, and content-based
checks are blind alongside metadata ones. A guard is only as good as its
locality — it must run local to the filesystem it guards. Serving the
library to Komga on Tower over SMB was considered and **rejected**, for
unnecessary network traffic and extraneous I/O on Tower's HDDs rather than
for this; the measurement is recorded because it independently rules the
configuration out. It also bounds the planned option to source from or
store to network storage: that is supported, but the rewrite guards degrade
from a narrow race window to a ten-second blind window, and that tradeoff
belongs in front of the user choosing it.

The durable lesson is not the 2-second number, which no longer applies. It
is that nobody was tracking which filesystem the live library sat on, and
every conclusion about concurrency safety silently depended on it.

## Environment claims get measured, never inferred

Three environment claims were asserted from plausible reasoning during the
2026-08-02 guard validation and all three were overturned by measurement:
that the library volume was SMB (it was locally attached), that it was NTFS
(it was exFAT at the time), and that a share-mode open would detect the
concurrent writer (0/16 — the writer has already released by check time).

Filesystem, volume, and access-path behavior is measured on the actual
target before it is written down or designed against. First-principles
reasoning about these is evidence of a hypothesis worth testing, not a
finding.

## Unknown byte identity is a state, not a null

Revision identity is `archive_revisions.archive_sha256`, and it is
nullable. Reconciled 2026-08-21 against the protected pre-revision backup,
147 of 59,688 archives have no archive-level SHA-256, and 311 archives have
no current file location at all -- so some of those bytes are unreachable
and can never acquire a digest.

A `NOT NULL` column left two options and both were wrong. Aborting the
migration would block Step 2 permanently on archives whose files no longer
exist. Backfilling only the hashed ones would leave 147 archives with no
revision and a NULL `current_revision_id`, breaking the roadmap's criterion
that every archive has exactly one deterministic current revision -- and
leaving a silent NULL that every later query has to remember.

So `identity_state` is `'established'` or `'provisional'`, tied to the
presence of the digest by a CHECK in both directions, and capped at one
provisional row per archive by a partial unique index. 59,541 established
+ 147 provisional accounts for every archive. The gap stays queryable
instead of becoming folklore, which is the same reason retirement evidence
is NOT NULL: a missing fact that looks like an ordinary empty value stops
being findable.

When the bytes are finally hashed, the established revision is **appended
after** the provisional one and the current pointer moves to it. The
provisional row is kept as noncurrent history and the schema refuses to
delete it while its archive exists. Replacing it in place was the first
design and it was wrong twice over: it destroyed the record that the
identity existed with unknown bytes between two dates, and it cascaded
away every observation recorded against it during that period.

## Every archive has a current revision, enforced by the schema

`archive_files.current_revision_id` is nullable and always will be: an
archive's first revision cannot exist before the archive row it
references, so no `NOT NULL` column can be satisfied on INSERT. Enforcing
the invariant only in the migration's backfill would cover the archives
that existed the day 014 ran and nothing discovered afterwards, because
every later archive arrives through an INSERT the backfill never sees.

It is closed at both ends instead. An `AFTER INSERT` trigger gives every
new archive an initial provisional revision and points it there, so the
NULL window shuts inside the same statement and applies to raw SQL exactly
as it does to the DAL. A `BEFORE UPDATE` trigger refuses to clear a live
archive's pointer back to NULL. Without both, a transaction could commit
an archive with no current revision at all, and nothing would ever revisit
it.

The initial revision is provisional because that is the truth at that
instant: the identity row has just been created and nothing has hashed its
bytes.

## Revisions and supersession are different relationships

Supersession (migration 013) relates two archive *identities*: the work
continues under a different `archive_id`. A revision (migration 014)
relates two byte *states of one identity*. Archive 37704 needs both --
three byte generations recorded across two identities -- and it shares a
historical digest with archive 58201, which is a supersession case.

Merging them on that shared digest is the specific failure the model
prevents, and it is prevented structurally rather than by convention:
revision lineage carries `archive_id` into its foreign key so a chain
cannot cross identities, and `archive_supersessions` holds no digest
column at all so it cannot express a byte generation. Neither table can
be used to say the other's thing.

`archive_sha256` is indexed but deliberately not globally unique: 888
exact-duplicate groups were measured on 2026-08-21, and byte-identical
archives must stay separately addressable. Canonical-copy selection is a
later guarded resolution action, not a schema constraint.

## The envelope is the plan's commit marker, including when the writer fails

The backfill planner writes two artifacts through a staged commit: bindings
to `plan.csv`, then the envelope to `plan.json`. The envelope is promoted
last and carries the CSV's SHA-256, so its presence attests that the bindings
finished writing and proves they are the bindings it approved.

**A consumer decides whether a plan committed on the envelope alone.** It
never decides on the absence of a `.partial` staging file. Residue is a
cleanup outcome, not a commit outcome: an operator clearing a leftover
staging file does not change whether the plan committed, so a rule keyed on
residue returns two different answers for one unchanged plan. Migration 015
and slice 4 consume on this rule.

The decision was forced by a defect found in review on 2026-08-28. When the
CSV promoted, the envelope promoted, and only the envelope's staging name
could not be unlinked, the writer raised and called the plan "incomplete...
should be removed by hand" -- while both artifacts were complete and the
envelope's digest matched the CSV beside it. An operator following that text
would have deleted a valid committed plan, and a consumer reading the
envelope would have kept it. One state, opposite conclusions.

The alternative was considered and rejected: treating such a pair as
uncommitted would have required every consumer to check both staging paths
before trusting an envelope, which is the weaker predicate above and
contradicts the commit ordering the writer already pays for.

So the writer classifies instead. `StagingResidueError` (a subclass of
`OutputPathError`) means the plan committed and only cleanup failed; a plain
`OutputPathError` means it did not commit. The classification is "every
requested artifact reached its final name", which in pair mode is
*equivalent* to the envelope existing -- a CSV-side residue failure raises
before the envelope is promoted -- so the writer and its consumer cannot
disagree. That equivalence is asserted in both directions by
`test_the_committed_class_and_the_envelope_marker_cannot_disagree`.

It remains a raise rather than a successful return, because a successful
return has to keep meaning that no staging file is left anywhere. Callers
separate the cases on the exception type and its `committed` / `residue`
attributes, never by parsing the message. The operator CLI exits 7 for this
case: not 0, because a human must still clear the residue, and not 6, which
means the plan did not commit.

A second round found the same contradiction on a path with no residue at
all. An interrupt arriving between the envelope's promotion and the
writer's return left a complete, committed pair -- and was rewrapped as
`OutputPathError`, the type whose meaning is that the plan did not
commit, around prose that correctly called the pair complete. The CLI
read the type and reported a failed write.

The rule that resolves it: **the writer substitutes its own error type
only when it has something to tell the caller that the original error
does not.** Stated exactly, because a narrower version of it was written
here first: the original interruption propagates unchanged when the
writer has nothing additional to report -- either **every requested
artifact committed cleanly**, or **nothing committed and all staging
files were removed**. So a `KeyboardInterrupt` stays a
`KeyboardInterrupt` rather than being downgraded into something
`except Exception` can swallow.

Neither half may be narrowed to the pair. A CSV-only or envelope-only
write that promotes its single artifact and clears its staging name
qualifies for the first. And the cleanup clause in the second is
load-bearing: an interrupt arriving before any promotion whose staging
cleanup then fails leaves a file that must be named, so it is converted
to `OutputPathError` and does not propagate unchanged. When the CSV
committed and the envelope did not, there *is* something to say, so the
substitution happens; that asymmetry is the rule working, not an
exception to it.

A general committed-plan exception was considered and rejected. After the
final promotion the only remaining statement is the return, so its
non-interrupt case would have been unreachable defensive code, and its
interrupt case would have required converting an interrupt.

The operator CLI's exit codes follow the state the writer left behind, not
the cause. An interrupt is not tied to one code: it exits 130 when it
arrives unconverted, which happens when there was nothing additional to
report -- every requested artifact committed cleanly, or nothing
committed and all staging files were removed -- and the message, not the
code, tells those apart, by the envelope where one was requested. It
exits 6 when the writer
substituted an `OutputPathError` because a committed CSV was being left
behind with no envelope, which is correct, since that plan did not
commit. It exits 7 when everything requested committed and staging
residue survived. 130 is separate from 1, which already means the gates
failed.

One consequence is worth stating plainly, because two contracts stated it
wrongly before it was noticed: **an envelope is present only when one was
requested.** The condition for a committed plan is that every *requested*
artifact reached its final name. For a pair that is equivalent to the
envelope existing, which is why the envelope is the marker a consumer
reads. A CSV-only write commits with no envelope at all, and raises the
same `StagingResidueError` and exits the same 7, so neither the exception
type nor the exit code may be read as evidence that an envelope exists.

The rollback's ownership proof has a stated limit, recorded here because a
limit that lives only at the call site is invisible to anyone deciding
whether to rely on it. `_discard` holds the descriptor it created the
staging file with, which keeps POSIX from recycling the inode, so a
device/inode match cannot be satisfied by a later file that inherited the
id. That is the accident this rollback actually hit and it is closed.

It does **not** make the check and the removal atomic. `os.lstat` and
`os.unlink` are two calls against a pathname, and a process coordinating
with this one could re-point that name in between, in which case the
unlink removes the replacement. The descriptor protects the inode, not
the name.

The reason no mechanism is planned is an **operational assumption, not a
property of the paths**: artifact generation requires one cooperating
writer per requested final and staging namespace. The implementation
refuses concurrent *creation* with `O_EXCL`, and that is all it does. It
does not defend against another process deliberately removing or
replacing a staging pathname during rollback.

The staging names give no help here and it is worth being exact about
why, because the first version of this entry claimed they did. They are
deterministic siblings -- `plan.csv` stages through `plan.csv.partial` --
so two processes aimed at the same output compute the same staging path,
and `O_EXCL` makes the second one lose the create rather than making the
name unguessable. Anyone relying on this rollback under concurrency needs
the assumption above to hold, or needs a coordination mechanism that does
not exist yet.

The docstring claimed protection outright until 2026-08-28 -- that "the
name cannot be swapped" -- which is the kind of claim that stops someone
adding the mechanism that would.

## A migration too dangerous to run automatically is refused, not skipped

Every migration under `comic_automation/database/migrations/` is applied
on startup by `apply_migrations()`, which eleven CLI and service entry
points call and which has no notion of approval, backup or postflight.
That is right for a migration that adds a column and wrong for migration
015, which rebuilds four evidence tables, binds 180,519 field projections
from an approved plan artifact, cannot be re-run, and leaves no state any
code here can read if it lands partially
(`docs/slice4_migration_design.md` sections 5 and 8).

`comic_automation/database/protected_migrations.py` declares such versions
in `PROTECTED_MIGRATIONS`, and `apply_migrations()` **aborts** while any of
them is pending. Three parts of that are decisions rather than mechanics.

**Protection is a version number in source, not a filename convention and
not a marker inside the .sql file.** A content marker fails in the
dangerous direction: a file that omits it reads as unprotected and is
applied silently. A missing entry in a frozenset is a diff.

**It aborts the whole run rather than skipping the protected file.**
Skipping and continuing would apply the unprotected migrations queued
around it and report success, leaving the schema between two releases with
no ledger row saying so, and leaving every entry point running producer
code against a schema the operator believes is still pending. Refusal is
also inert: the guard reads the ledger through its own
`recorded_versions()` rather than `applied_versions()`, because the latter
calls `ensure_migration_table()` and would make a refusing run create a
table as a side effect of asking a question.

**Failing closed is not failing blind.** The strictly read-only path
(`database/read_guards.py`) is unaffected and never migrates, so an
operator facing a pending 015 can still read the database -- which is most
of what they will want to do. The refusal message names that path.

### One reading, and three layers over it

The first implementation asked twice. The guard scanned the migrations
directory, returned, and `apply_migrations()` scanned again to build its
apply list; a `015_*.sql` created between the two was invisible to the
first and visible to the second, so an ordinary command applied a
protected migration and wrote its ledger row. The protected-execution
seam had the same shape, comparing a pending set from one scan against
paths from another.

`MigrationSnapshot` is one frozen reading of the directory and the
ledger, and the guard, the apply plan and the seam all derive from it.
The incoherent shape is unrepresentable rather than merely discouraged:
no function here takes a connection and a directory and answers a
question about them.

Coherence is **not** the only thing standing between a stale plan and
protected SQL, and it is worth being exact about that, because the
obvious reading -- "the snapshot is the fix" -- is wrong. Three layers
are independent:

```text
1  the guard        refuses while a protected version is pending
2  the plan filter  ordinary_apply_plan() excludes protected versions
                    however the snapshot it was built from was obtained
3  the invariant    assert_no_protected_in_apply_set() re-checks the
                    plan itself, immediately before any SQL runs
```

Measured by peeling them apart. The first measurement used a scenario
where only a protected `015` arrives after the guarded snapshot, and it
produced a **wrong conclusion** that was written down here: that
reintroducing the second scan on its own was unobservable, and therefore
defence in depth rather than a guard in its own right.

It is observable, and the scenario was too weak to see it. Adding an
unprotected `016` above the protected version:

```text
layers disabled                       outcome
none                                  [1]          015 and 016 excluded
re-scan                               [1, 16]      R6 VIOLATED
plan filter                           [1]
invariant                             [1]
plan filter + re-scan                 REFUSED      invariant fired
plan filter + invariant + re-scan     [1, 15, 16]  protected SQL ran
```

With only the second scan reintroduced, the fresh scan sees
`{1, 15, 16}`, the plan filter silently drops `015`, the invariant finds
nothing protected, and `016` runs. No protected SQL executed and R6 was
still violated: the run **skipped a protected migration and carried on
past it**, which section 4.2 forbids in those words.

The lesson is not about this guard. "No protected SQL ran" is a weaker
property than "refuse rather than skip", and a test asserting only the
weaker one reads exactly like a test asserting both. That is what made a
real defect look like defence in depth for a whole review round.

So all three layers are load-bearing, each failing a named test when
disabled alone. What remains true is that no single one of them is
sufficient: the deepest failure -- protected SQL actually executing --
still needs all three gone.

The invariant is deliberately not inside `ordinary_apply_plan()`. An
invariant enforced only by the function that establishes it is that
function checking its own output, and a rewrite of the builder carries
the check away with it.

### Two files may not claim one version

`schema_migrations.version` is an INTEGER PRIMARY KEY, so `015_a.sql`
and `015_b.sql` describe a state the ledger cannot represent: one of the
two could never be recorded. The version-to-path mapping used to be a
dictionary comprehension, which kept whichever file sorted last and
dropped the other in silence -- so which of two migrations ran was
decided by filename order, and the loser was left permanently unapplied
*and* unrecorded.

`take_migration_snapshot()` refuses instead, with `AmbiguousMigrationError`.
This does change ordinary behaviour: a duplicate-version directory used
to half-apply and now refuses outright. That is a fix, not a regression,
and it is written down here because "ordinary migrations are unaffected"
is otherwise a claim with a quiet exception. The error is a separate type
from `ProtectedMigrationError` because a malformed directory is a
different condition, and a duplicated *protected* id must not be reported
as "a protected migration is pending" -- that would send an operator to
the protected executor to run a file that cannot be identified.

### The limits, which are the part worth writing down

`resolve_protected_execution()` is a seam, not an executor. It resolves
which files an authorization covers and refuses unless the pending
protected set equals the authorized set exactly. It enforces **none** of
the rest of design sections 8 and 12: writer quiescence, the protected
backup, plan-digest and expected-count revalidation, the ledger
preconditions of section 12.1 steps 3-4, statement-by-statement
application, the in-transaction ledger row, or the section 12.2
reconciliation. A caller holding the resolved paths has been authorized and
has satisfied none of those obligations. Recorded here, and not only in the
docstring, because a limit visible only at the call site is invisible to
anyone deciding whether to rely on the capability.

The guard protects **one root**. It computes its pending set from the
directory it is handed, so an entry point aimed at a different migrations
directory would find no protected file, see an empty pending set and fail
*open*, in silence. `scripts/db.py` has exactly such an independent root
(`<repo root>/migrations`, applied with `executescript()` and no guard).
The two roots are disjoint today and that is asserted by tests rather than
assumed -- including that all eleven call sites resolve to the guarded root
and that no protected id sits under the unguarded one.

The `missing`-file check that used to sit inside
`resolve_protected_execution()` is **gone**. It was unreachable given the
set equality above it, and bypassing it alone failed nothing. Once the
seam took a snapshot, every pending version came out of
`snapshot.discovered` by construction, so there was no longer even a
hypothetical path for it to catch -- and unreachable code kept "just in
case" reads to the next person as a guard that works.
