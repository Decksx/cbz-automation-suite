# Image Recognition and Deduplication

## Status

**Implementation and production backfill in progress.**

The progressive system combines exact hashes, page perceptual hashes,
ordered page comparison, and later quality scoring. OpenCLIP comes
after candidate generation is stable.

Implemented foundations:

- whole-archive SHA-256;
- ordered exact page SHA-256 and archive content signatures;
- Pillow-backed 64-bit dHash and pHash;
- persistent, versioned page hashes in SQLite;
- decoded page dimensions and image formats;
- bounded, resumable queue workers for exact and perceptual hashing;
- conservative Tier C candidate blocking and ordered comparison;
- persistent review-only near-duplicate candidates.

Last reconciled production state on 2026-07-29:

```text
page SHA-256 rows:            2,955,304
perceptual job rows:             20,600
completed:                       20,531
terminally failed:                   69
pending / claimed / running:          0
dHash Version 1 rows:          1,025,682
pHash Version 1 rows:          1,025,682
eligible archives remaining:      37,654
near-duplicate candidates:             0
```

The two latest guarded batches processed 10,000 archives with 9,991
successes, 9 legitimate terminal image-decoding failures, no retries,
and exact pre/post reconciliation.

## Safety policy

Duplicate detection is not duplicate deletion.

Initial resolutions:

```text
keep
quarantine
manual_review
ignore
```

Automated permanent deletion remains disabled until real-library validation is complete.

## Identity layers

### Physical file identity

SHA-256 of the entire CBZ.

### Archive content identity

Signature derived from ordered page content while excluding ZIP metadata and ComicInfo differences.

### Visual identity

Perceptual hashes and embeddings that detect resized, recompressed, cropped, color-adjusted, or watermarked variants.

## Page inventory

Store:

```text
archive_id
page_index
entry_name
source-byte SHA-256
decoded-image SHA-256 where useful
width
height
format
file_size
```

## Versioned perceptual hashes

```text
page_id
algorithm
algorithm_version
hash_bits
parameters_json
hash_value
created_at
```

Initial algorithms:

```text
pHash
dHash
wHash (optional)
```

The current perceptual hashing worker stores `dhash` and `phash` version
`1`. It only enqueues archives whose exact page inventory is current and
skips archives that already have both perceptual hashes for every page.

Run a bounded batch:

```powershell
python scripts/comic_perceptual_hashing.py `
  --database G:\ComicAutomation\db\comics.db `
  --limit 10 `
  --progress-every 1 `
  --enqueue-missing
```

Use `--report-only` to inspect queue and stored-hash counts without
processing jobs. No CBZ content is rewritten.

The perceptual worker also stores decoded width, height, and image
format. Archives hashed before that schema addition are automatically
eligible for a bounded perceptual-hash pass to backfill those values.

## Version 1 performance optimization

Version 1 hash semantics are frozen during the active production
backfill. Do not change JPEG decoding, resizing, DCT arithmetic,
floating-point accumulation order, or stored digest format.

After the active guarded batch:

1. Run a read-only exact-page-SHA reuse analysis.
2. Freeze exact Version 1 dHash/pHash regression vectors.
3. Cache immutable pHash cosine/normalization constants by
   `(hash_size, high_frequency_factor)`.
4. Add optional aggregate phase timing inside the worker and
   repository.
5. Implement version-aware bulk exact-hash reuse if the measured
   opportunity is material.
6. Evaluate selective missing-page hashing if partial reuse is common.
7. Resume guarded 5,000-archive batches after regression and
   database-copy validation.

The reuse report must distinguish:

```text
reusable_pages
fully_satisfied_archives
partially_satisfied_archives
pages_still_requiring_decode
archives_still_requiring_processing
```

This distinction matters because the current worker processes every
image page in an eligible archive. Fully satisfied archives avoid the
worker entirely; partial reuse does not avoid decoding unless the
worker gains a selective missing-page path.

Cached and uncached Version 1 results must satisfy exact digest
equality, not merely zero Hamming distance.

Optional profiling uses `time.perf_counter()` inside the worker and
repository save path and aggregates, without per-page telemetry writes:

```text
zip_open_and_inventory_seconds
zip_entry_read_seconds
image_open_and_decode_seconds
dhash_seconds
phash_seconds
database_lookup_seconds
database_save_seconds
```

See `docs/implementation_roadmap.md` Step 1A for the full requirements,
acceptance criteria, guarded rollout, and deferred Version 2 research.

### Exact-SHA reuse result

The read-only production analysis completed on 2026-07-29:

```text
eligible archives:                         37,654
eligible pages:                         1,917,928
reusable pages:                            16,163  (0.84%)
fully satisfied archives:                     333  (0.88%)
partially satisfied archives:                 407
pages avoided by full-archive reuse:        11,539  (0.60%)
additional pages avoided selectively:       4,624  (0.24%)
ambiguous source SHA-256 digests:                0
```

The measured opportunity is not material enough to justify production
bulk-copy writes or a selective worker path during the current Version
1 backfill. Retain the read-only analysis command for future
reassessment and proceed to frozen Version 1 regression vectors and
output-preserving pHash constant caching.

### Version 1 constant-cache result

Eight exact regression vectors now freeze Version 1 dHash and pHash
outputs across the supported image formats, modes, dimensions, and
non-default hash parameters. The pHash cosine and normalization
constants are cached as immutable tuples by
`(hash_size, high_frequency_factor)` without changing resize behavior,
coefficient order, floating-point accumulation order, digest format, or
algorithm version.

The reproducible paired benchmark is:

```powershell
python scripts\benchmark_perceptual_hash_constants.py `
  --calls-per-round 250 `
  --rounds 7
```

On Python 3.11.3 and Pillow 12.3.0, the median result increased from
122.00 to 123.37 hashes per second, an approximately 1.13% throughput
gain. The result confirms that constant construction was removable
overhead but not the dominant share of the pure-Python DCT.

### Optional phase-timing result

The bounded perceptual-hash runner accepts `--profile`. When enabled,
the worker and repository aggregate these phases in memory:

```text
zip_open_and_inventory_seconds
zip_entry_read_seconds
image_open_and_decode_seconds
dhash_seconds
phash_seconds
database_lookup_seconds
database_save_seconds
```

The normal path does not call the timing clock inside page phases.
Profiling adds no schema and writes no per-page telemetry.

The reproducible local benchmark is:

```powershell
python scripts\benchmark_perceptual_hash_profiling.py `
  --archives 50 `
  --pages-per-archive 4 `
  --rounds 3
```

Three alternating profiled/unprofiled rounds covered all five supported
formats and found no measurable profiling overhead (`-0.24%`, within
noise). pHash accounted for 88.51% of timed work across 600 profiled
pages, followed by image open/decode at 5.35% and dHash at 3.07%.

This establishes pHash as the measured local CPU hotspot. A guarded
production batch with `--profile` is still required to quantify SMB
read behavior.

## Aggregate archive signatures

Precompute:

```text
cover pHash
first-content-page pHash
middle-page pHash
last-content-page pHash
sampled page sequence
full exact page sequence
page count
dimension summary
```

## Candidate tiers

### Tier A

Same archive SHA-256.

### Tier B

Same ordered exact page-hash sequence despite packaging or metadata differences.

### Tier C

Similar page count and strongly matching ordered pHash sequence.

The first Tier C implementation uses both dHash and pHash. It:

- blocks on 16-bit bands from the first, middle, and last page hashes;
- ignores overly broad blocking buckets;
- allows a page-count difference of 5%, with a minimum allowance of one
  page;
- tries small sequence offsets to account for an added or removed
  leading/trailing page;
- requires at least 90% of the larger archive's pages to match;
- requires both 64-bit hashes on each matched page to have Hamming
  distance 6 or less;
- records aspect-ratio agreement and median pixel area for review;
- excludes archives already represented by the same ordered exact page
  signature.

Generate a bounded set of review candidates:

```powershell
python scripts/comic_near_duplicate_candidates.py `
  --database G:\ComicAutomation\db\comics.db `
  --limit 100
```

Use `--report-only` to inspect readiness and current review counts
without generating candidates. New records use `pending_review`.
Previously reviewed decisions are never overwritten. The command does
not delete, move, rename, replace, or rewrite archive files.

### Review UI (design, not yet built)

Design agreed 2026-07-28, ahead of any actual frontend/API work.
Comparison is at the **archive level**, not the individual page level --
a `near_duplicate_candidates` row is two whole archives, and pages
aren't removable from a CBZ without breaking its sequence.

Each review card shows the **cover page** of both archives side by
side, with metadata under each:

```text
filename
series (once series/issue normalization exists; falls back to folder
        name until then)
chapter/volume, if available
size on disk
page count
resolution (of the cover, or a dimension summary)
```

Three actions per card:

- click either cover -> that archive is the preferred/kept copy
- a box between the two covers -> keep both

The queue advances automatically to the next `pending_review` row,
ordered by `similarity_score` descending (the existing
`idx_near_duplicate_review` index already supports this ordering, no
schema change needed there).

Resolved: `review_status` has a fourth state, `rejected` (the matcher
was wrong, these aren't actually the same release), with no button
mapped to it above. Decided 2026-07-28 that it doesn't need one --
"false positive, keep both" and "true duplicate, keep both anyway" both
end in the same action (delete nothing, advance to the next pair), so
the three-button design is sufficient. `rejected` stays a valid value
in the schema, just unreached by this particular interface; the only
cost is losing data on the matcher's false-positive rate, which only
matters for tuning Tier C's thresholds later, not for the review
workflow itself.

Still open before this can be built:

- `confirmed_duplicate` records that one side was preferred, but the
  table has no column yet for *which* archive was chosen. A small
  migration (e.g. `preferred_archive_id`) is needed before this can
  actually be written to, not just designed.

Every decision made through this UI is also a labeled preference
signal (`archive A chosen over B`) that the quality-scoring phase could
train against later, rather than starting quality scoring from
nothing.

### Tier D

Partial overlap, missing pages, or compilation/individual relationships.

Tier D remains future work. The conservative Tier C implementation does
not claim to detect inserted interior pages, compilations, or substantial
partial overlap.

## Sequence-aware requirements

The system must distinguish:

- one added credit page;
- advertisements removed;
- missing first or last pages;
- shifted page numbering;
- compilation containing individual chapters;
- identical cover with different interior;
- censored and uncensored editions;
- translated and untranslated editions.

Cover-only matching is insufficient.

## OpenCLIP

The RTX 3080 will generate embeddings in batches.

Initial samples:

```text
cover
first content page
25% position
middle
75% position
last content page
```

Generate full-page embeddings only for ambiguous candidates.

## Quality scoring

Candidate metrics:

```text
pixel area
sharpness
blur
compression artifacts
upscaling likelihood
page completeness
duplicate-page ratio
blank-page ratio
corrupt-page count
color depth
archive size
```

The preferred copy must not be selected by file size alone.

## Scaling

Do not compare every archive against every other archive. Candidate blocking should use series/chapter identity, page-count range, exact hashes, cover-hash neighborhoods, and sampled signatures before expensive sequence or CLIP comparison.
