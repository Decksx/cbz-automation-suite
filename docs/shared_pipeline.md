# Shared Processing Pipeline

## Authoritative parser

The authoritative normalization path is `cbz_core.parse_comic_name()`.

```text
source path
  ↓
infer series context
  ↓
clean directory name
  ↓
clean filename
  ↓
translate or preserve title variants where configured
  ↓
remove leading identifiers and source noise
  ↓
normalize generic chapter stems
  ↓
normalize chapter / volume / episode / part tokens
  ↓
extract structured metadata
  ↓
return ParsedComicName
```

## Shared responsibilities

`cbz_core.py` owns:

- sanitization
- Windows-safe filename cleanup
- series inference
- chapter, volume, season, episode, and part extraction where supported
- title translation and alternate-title handling
- generic-title detection
- ComicInfo field-selection decisions

It does not own:

- filesystem moves
- archive rewrite mechanics
- watcher debounce and settle logic
- worker-pool management
- routing
- GUI state
- logging configuration

## ComicInfo

`update_comicinfo_xml()` receives XML text plus parsed metadata and returns updated text and a change flag. The caller owns archive I/O.

Depending on available metadata, the shared pipeline may update or preserve fields such as:

```text
Title
Series
LocalizedSeries
AlternateSeries
Number
Volume
Notes
```

Tool-specific templates may preserve Komga/Mihon namespace fields.

## Translation

Shared translation behavior supports Japanese, Chinese, and Korean title text where enabled.

Relevant environment controls include:

```text
CBZ_TRANSLATION_ENABLED=0
CBZ_TRANSLITERATE_FALLBACK=1
```

Original-language values should be retained as alternate metadata when an English title is selected.

## Collision behavior

Current workflows generally retain the larger file when destination paths collide. This is a pragmatic collision rule, not a reliable quality or visual-identity decision.

The future database and image-recognition pipeline will use evidence-based duplicate and quality resolution.

## Dry-run guarantees

Dry-run mode should avoid:

- archive rewrites
- renames
- moves
- deletes
- progress mutation

Maintenance commands may still write an explicitly requested plan file containing proposed actions.
