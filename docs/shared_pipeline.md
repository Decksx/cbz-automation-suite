# Shared Cleaning Pipeline

All tools share a common `sanitize()` function defined in `cbz_sanitizer.py` (the canonical reference). The pipeline applies the following transformations **in order**:

| Step | What it removes / fixes |
|------|------------------------|
| 1. HTML/XML entity decode | Converts `&amp;`, `&#039;`, etc. to plain characters |
| 2. URL stripping | Removes `www.site.com`, `site.net` patterns |
| 3. Scanner credit stripping | Words containing `scans` / `scanners` / `scanlations` (e.g. `TheGuildScans`) |
| 4. Trailing slash / G-code | Trailing slash and G-code suffixes |
| 5. Non-Latin character removal | CJK, full-width, and other non-Latin/Greek characters |
| 6. Bracket group removal | `[GroupName]` and `(Publisher)` tags |
| 7. Stray bracket cleanup | Orphaned `[` `]` `(` `)` characters |
| 8. Underscore replacement | Underscores replaced with spaces |
| 9. Whitespace normalisation | Collapses multiple spaces, trims |

---

## Filename Normalisation

After `sanitize()`, filenames go through additional steps:

- **`normalize_stem()`** — strips the series name prefix from the filename stem (e.g. `Batman - Chapter 12` → `Chapter 12` when inside a `Batman` directory), and resolves generic stems like `Chapter 12` or `Unknown Chapter 5` using the directory name as context.
- **`normalise_number_tokens()`** — deduplicates chapter number tokens (e.g. `Batman 12 12` → `Batman 12`).

---

## ComicInfo.xml Handling

### Tag logic

| Tag | Source | Overwrite condition |
|-----|--------|-------------------|
| `<Title>` | Cleaned filename stem | If missing, blank, or matches a generic pattern (e.g. `Chapter 12`, `# English`) |
| `<Series>` | Cleaned parent directory name | Always set |
| `<Number>` | Chapter number extracted from filename | Always set if found |
| `<Volume>` | Volume number extracted from filename or directory name | Always set if found |

### Generic title patterns (overwrite triggers)

These patterns in `<Title>` are treated as placeholder values and replaced:

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
</ComicInfo>
```

---

## Conflict Resolution

When a destination directory already exists (during a move or merge), `_merge_directories()` merges the incoming folder into it file by file. On any filename collision, **the larger file is kept**. This policy is consistent across all tools.
