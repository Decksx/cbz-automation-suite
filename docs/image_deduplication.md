# Image Recognition and Deduplication

## Status

**Implementation in progress.**

The first usable system will combine exact hashes, page perceptual hashes, ordered page comparison, and quality scoring. OpenCLIP comes after candidate generation is stable.

Implemented foundations:

- whole-archive SHA-256;
- ordered exact page SHA-256 and archive content signatures;
- Pillow-backed 64-bit dHash and pHash;
- persistent, versioned page hashes in SQLite;
- decoded page dimensions and image formats;
- bounded, resumable queue workers for exact and perceptual hashing;
- conservative Tier C candidate blocking and ordered comparison;
- persistent review-only near-duplicate candidates.

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
