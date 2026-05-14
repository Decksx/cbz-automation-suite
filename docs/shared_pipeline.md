# Shared Cleaning Pipeline

All tools share a common normalization/parsing layer implemented in `scripts/cbz_core.py`. The watcher and batch tools import shared helpers directly from the core module rather than maintaining duplicated regex and helper copies.

---

## Shared Core Functions

`cbz_core.py` owns the suite-wide helpers for:

- `sanitize()`
- `clean_filename()`
- `clean_directory_name()`
- `clean_xml_field()`
- `parse_comic_name()`
- `normalise_number_tokens()`
- `normalize_stem()`
- `extract_chapter_number()`
- `extract_volume_number()`
- `infer_series_name()`
- `update_comicinfo_xml()`

---

## Sanitization Pipeline

| Step | What it removes / fixes |
|------|------------------------|
| 1. HTML/XML entity decode | Converts entities to plain characters |
| 2. URL stripping | Removes URLs and bare domain-like tokens |
| 3. Scanner credit stripping | Removes scanner/scanlation credit tokens |
| 4. Trailing slash / G-code | Removes trailing slashes and G-code suffixes |
| 5. Bracket group removal | Removes `[GroupName]` and `(Publisher)` blocks |
| 6. Mixed-language title shortening | Shortens English/original-language duplicate titles without erasing non-Latin-only titles |
| 7. Underscore replacement | Underscores become spaces |
| 8. Windows-safe cleanup | Removes Windows-forbidden path characters |
| 9. Whitespace normalization | Collapses repeated spaces and strips leading/trailing whitespace |

---

## Mixed-language title handling

Older versions aggressively stripped non-Latin text entirely. The shared `cbz_core.py` pipeline now:

- preserves non-Latin-only titles
- prefers English segments when a filename contains both English and original-language titles
- preserves chapter and volume suffixes during shortening
- removes only Windows-forbidden path characters

Examples:

```text
One Piece / ワンピース Ch.005.cbz
→ One Piece Ch.5.cbz

Batman — バットマン Vol.01 Ch.005.cbz
→ Batman Vol.1 Ch.5.cbz

ワンピース Ch.005.cbz
→ ワンピース Ch.5.cbz
```

---

## Filename Normalization

After `sanitize()`, filenames go through additional steps inside `parse_comic_name()`:

- strip leading numeric prefixes
- normalize generic stems with `normalize_stem()`
- normalize number tokens with `normalise_number_tokens()`
- strip trailing junk
- extract chapter and volume numbers

`parse_comic_name()` is now the single authoritative filename-normalization pipeline.

---

## Directory Name Cleaning

Directory names go through `sanitize()` plus extra steps:

- Strip leading hashtag characters
- Strip trailing hashtag characters
- Strip trailing dangling tokens with no following number

---

## Root-aware Series Inference

`infer_series_name()` avoids blindly treating `path.parent.name` as the series and can skip generic container folders such as `Issues`, `Chapters`, `Volumes`, `Extras`, and `Specials`.

---

## ComicInfo.xml Handling

| Tag | Source | Overwrite condition |
|-----|--------|-------------------|
| `<Title>` | Cleaned filename stem | If missing, blank, generic, gibberish, or equal to series |
| `<Series>` | Cleaned/inferred series name | Always set |
| `<Number>` | Chapter number extracted from filename | Set if chapter number is found |
| `<Volume>` | Volume number extracted from filename or directory name | Set if found |

Existing files are updated using `xml.etree.ElementTree` rather than regex substitution. Custom titles are preserved automatically.
