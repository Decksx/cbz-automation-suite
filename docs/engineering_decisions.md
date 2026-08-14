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
