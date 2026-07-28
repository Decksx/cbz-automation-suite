# Image Recognition and Deduplication

## Status

**Implementation in progress.**

The first usable system will combine exact hashes, page perceptual hashes, ordered page comparison, and quality scoring. OpenCLIP comes after candidate generation is stable.

Implemented foundations:

- whole-archive SHA-256;
- ordered exact page SHA-256 and archive content signatures;
- Pillow-backed 64-bit dHash and pHash;
- persistent, versioned page hashes in SQLite;
- bounded, resumable queue workers for exact and perceptual hashing.

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

### Tier D

Partial overlap, missing pages, or compilation/individual relationships.

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
