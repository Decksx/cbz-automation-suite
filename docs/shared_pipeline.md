# Shared Cleaning Pipeline

All tools share a common `sanitize()` function defined in `scripts/cbz_sanitizer.py` (the canonical reference). The watcher and all batch tools use byte-for-byte identical compiled regex patterns. The pipeline applies the following transformations **in order**:

| Step | What it removes / fixes |
|------|------------------------|
| 1. HTML/XML entity decode | Converts `&amp;`, `&#039;`, `&apos;`, `&lt;`, etc. to plain characters |
| 2. URL stripping | Removes `http://...`, `www.site.com`, and bare `site.com/net/org/io/...` patterns |
| 3. Scanner credit stripping | Words containing `scans` / `scanners` / `scanlations` (e.g. `TheGuildScans`, `FooScanlations`) |
| 4. Trailing slash / G-code | Trailing slashes and trailing G-code suffixes (e.g. `Batman G1234` → `Batman`) |
| 5. Non-Latin/non-Greek/non-emoji character removal | Strips everything outside Basic Latin, Extended Latin, Greek, general punctuation, and emoji — covers CJK, Arabic, Cyrillic, Thai, full-width forms, and all other non-Latin scripts |
| 6. Bracket group removal | `[GroupName]` and `(Publisher)` tags removed in a single pass |
| 7. Stray bracket cleanup | Orphaned `[` `]` `(` `)` characters left behind after step 6 |
| 8. Underscore replacement | Underscores replaced with spaces |
| 9. Whitespace normalisation | Collapses multiple consecutive spaces, strips leading/trailing whitespace |

---

## Filename Normalisation

After `sanitize()`, filenames go through additional steps:

- **`normalise_number_tokens()`** — normalises chapter/volume number formatting (e.g. strips leading zeros, standardises separators).
- **`normalize_stem()`** — strips the series name prefix from the filename stem (e.g. `Batman - Chapter 12` → `Chapter 12` when inside a `Batman` directory), and resolves generic stems like `Chapter 12`, `Unknown Chapter 5`, or `# Chapter 3` by prepending the directory name as context.

---

## Directory Name Cleaning

Directory names go through `sanitize()` plus extra steps:

- Strip leading hashtag characters (`# Batman` → `Batman`)
- Strip trailing hashtag characters (`Batman #` → `Batman`)
- Strip trailing dangling tokens with no following number (`Batman - ch` → `Batman`, `Series v` → `Series`)

---

## ComicInfo.xml Handling

### Tag logic

| Tag | Source | Overwrite condition |
|-----|--------|-------------------|
| `<Title>` | Cleaned filename stem | If missing, blank, or matches a generic pattern (see below) |
| `<Series>` | Cleaned parent directory name | Always set |
| `<Number>` | Chapter number extracted from filename | Set if a chapter keyword (`ch`, `chapter`, `chp`, `issue`, `#`) is present |
| `<Volume>` | Volume number extracted from filename or directory name | Set if found |

### Generic title patterns (overwrite triggers)

These patterns in `<Title>` are treated as placeholder values and replaced with the cleaned filename stem:

- `manga_chapter`
- `# English` / `# Chapter`
- `Chapter N` (bare)
- `Part N` (bare)
- `doujinshi chapter`
- `Unknown chapter`

### Archive rewriting

When `ComicInfo.xml` needs to be created or updated, the archive is rewritten **preserving the original compression type of every member**. Images are never re-compressed.

### Template

If no `ComicInfo.xml` exists, one is injected using this built-in template:

```xml
<ComicInfo
  xmlns:xsd="http://www.w3.org/2001/XMLSchema"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Title></Title>
  <Series></Series>
  <Number></Number>
  <Summary></Summary>
  <Writer></Writer>
  <Penciller></Penciller>
  <Genre></Genre>
  <Web></Web>
  <ty:PublishingStatusTachiyomi xmlns:ty="http://www.w3.org/2001/XMLSchema"></ty:PublishingStatusTachiyomi>
  <ty:Categories xmlns:ty="http://www.w3.org/2001/XMLSchema"></ty:Categories>
  <mh:SourceMihon xmlns:mh="http://www.w3.org/2001/XMLSchema">Komga</mh:SourceMihon>
</ComicInfo>
```

Note: the template only affects **newly created** `ComicInfo.xml` files. Existing files are updated in place — only the tags listed in the tag logic table above are modified.

---

## Conflict Resolution

When a destination directory already exists (during a move or merge), `_merge_directories()` merges the incoming folder into it file by file. On any filename collision, **the larger file is kept**. This policy is consistent across all tools that perform merges or moves.
