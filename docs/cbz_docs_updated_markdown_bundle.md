# CBZ Updated Documentation Bundle


---

# File: `docs/overview.md`

# Overview

The CBZ Automation Suite is a collection of Python scripts for monitoring, cleaning, tagging, and routing `.cbz` comic book archives on Windows. Designed to run against a network share or a local drive with minimal manual intervention.

---

## Design Principles

- **Hands-off pipeline** — files dropped into a watch folder are processed and routed automatically.
- **Recursive by default** — all batch tools descend into subdirectories automatically; opt out with `--no-recursive` where supported.
- **Parallel by default** — all batch tools use `min(8, cpu_count)` worker threads automatically; opt down with `--workers 1` for serial behaviour.
- **Resumable** — batch operations track progress in an append-only JSONL file.
- **Non-destructive** — files are renamed in place, never silently deleted; on any collision the larger file wins.
- **Windows-aware** — explicit handling for `FileExistsError` on rename, UNC paths, and watchdog event filtering.
- **Dry-run everywhere** — all batch tools support `--dry-run` for safe previewing.
- **Shared core module** — `scripts/cbz_core.py` owns shared normalization, parsing, and ComicInfo logic. Watcher and batch tools import from the shared module instead of maintaining duplicated regex/helper copies.
- **External config** — routing is driven by `routing.json`, not hardcoded in the script.

---

## Tools at a Glance

| Script | Recursive? | Workers? | Purpose | Doc |
|--------|-----------|----------|---------|-----|
| `scripts/cbz_core.py` | — | — | Shared normalization/parsing/ComicInfo layer used by watcher and batch tools | [cbz_core.md](cbz_core.md) |
| `scripts/cbz_watcher.py` | Always (watchdog) | — | Live watcher — monitors Incoming folder, cleans, tags, and routes files via `routing.json` | [cbz_watcher.md](cbz_watcher.md) |
| `scripts/cbz_sanitizer.py` | Always (`rglob`) | **Yes** | Batch sanitizer — in-place clean/tag built on shared `cbz_core.py` helpers | [cbz_sanitizer.md](cbz_sanitizer.md) |
| `scripts/cbz_folder_merger.py` | Single-level (by design) | **Yes** | Merges colliding series directories; two-phase ComicInfo update | [other_tools.md](other_tools.md#cbz_folder_mergerpy) |
| `scripts/cbz_compilation_resolver.py` | **Yes — default** | **Yes** | Resolves compilation vs individual overlaps | [other_tools.md](other_tools.md#cbz_compilation_resolverpy) |
| `scripts/cbz_number_tagger.py` | Always (`rglob`) | — | Retroactively sets ComicInfo number/volume tags | [other_tools.md](other_tools.md#cbz_number_taggerpy) |
| `scripts/cbz_series_matcher.py` | **Yes — default** | **Yes** | Near-duplicate series name detector | [other_tools.md](other_tools.md#cbz_series_matcherpy) |
| `scripts/cbz_gap_checker.py` | **Yes — default** | **Yes** | Outputs CSV of missing chapter numbers | [other_tools.md](other_tools.md#cbz_gap_checkerpy) |
| `scripts/cbz_deduplicator.py` | **Yes — default** | **Yes** | Removes duplicate cbz/cbr files and packs loose image folders | [other_tools.md](other_tools.md#cbz_deduplicatorpy) |
| `scripts/strip_duplicates.py` | **Yes — default** | **Yes** | Removes duplicate number tokens and fixes spaced punctuation | [other_tools.md](other_tools.md#strip_duplicatespy) |

---

## Shared Core

`scripts/cbz_core.py` centralizes logic that previously lived independently in multiple scripts:

- `sanitize()`
- `clean_filename()`
- `clean_directory_name()`
- `parse_comic_name()`
- `ParsedComicName`
- `update_comicinfo_xml()`
- chapter/volume extraction
- mixed English/original-title shortening
- root-aware series inference

The watcher has been migrated to call `parse_comic_name()` and `update_comicinfo_xml()` directly, eliminating duplicated filename and ComicInfo title-selection logic.

---

## Running Scripts

Run from the repo root:

```powershell
cd "C:\Users\David.Johnson\ComicAutomation"
python scripts\cbz_sanitizer.py --dry-run
python scripts\cbz_watcher.py
```

---

## Repository File Structure

```text
cbz-automation-suite/
├── scripts/
│   ├── __init__.py
│   ├── cbz_core.py
│   ├── cbz_watcher.py
│   ├── cbz_sanitizer.py
│   └── ...
├── tests/
│   ├── test_normalization.py
│   ├── test_series_detection.py
│   └── test_comicinfo.py
├── docs/
│   ├── overview.md
│   ├── cbz_core.md
│   ├── cbz_sanitizer.md
│   ├── cbz_watcher.md
│   ├── other_tools.md
│   ├── shared_pipeline.md
│   └── engineering_decisions.md
├── pytest.ini
├── README.md
└── requirements.txt
```


---

# File: `docs/cbz_core.md`

# cbz_core.py

Shared normalization, parsing, and ComicInfo helper module.

`cbz_core.py` is the suite-wide shared core layer. It prevents watcher, sanitizer, and maintenance scripts from drifting apart by maintaining duplicated regexes and helper functions.

---

## Responsibilities

`cbz_core.py` owns:

- text sanitization
- Windows-safe filename cleanup
- mixed English/original-title shortening
- filename normalization
- directory-name normalization
- root-aware series inference
- chapter and volume extraction
- `ParsedComicName`
- ComicInfo XML update decisions

It intentionally does **not** own:

- file moves
- archive rewrite mechanics
- watcher debounce/settle logic
- routing rules
- logging configuration
- dry-run behavior

---

## Public API

The intended public API is exported through `__all__`:

```python
ALL_RULES
GIBBERISH_RE
IGNORED_SERIES_FOLDERS
NUMBER_PREFIX_RE
TRAILING_JUNK_RE
ParsedComicName
clean_directory_name()
clean_filename()
clean_xml_field()
extract_chapter_number()
extract_volume_number()
infer_series_name()
is_generic()
is_generic_title()
normalise_number_tokens()
normalize_stem()
parse_comic_name()
parse_rules()
sanitize()
shorten_mixed_original_title()
update_comicinfo_xml()
```

---

## ParsedComicName

```python
@dataclass(frozen=True)
class ParsedComicName:
    original_path: Path
    filename: str
    stem: str
    series: str
    chapter: str | None
    volume: str | None
```

This object is the normalized metadata payload used by watcher and ComicInfo update logic.

---

## parse_comic_name()

`parse_comic_name()` is the authoritative normalization pipeline.

It performs:

1. series inference
2. directory-name cleanup
3. filename cleanup
4. leading-number stripping
5. generic stem normalization
6. chapter/volume token normalization
7. trailing-junk stripping
8. chapter extraction
9. volume extraction

Example:

```python
from pathlib import Path
from scripts.cbz_core import parse_comic_name

parsed = parse_comic_name(Path("One Piece/001 - One Piece Ch.005.cbz"))

print(parsed.filename)  # One Piece Ch.5.cbz
print(parsed.series)    # One Piece
print(parsed.chapter)   # 5
```

---

## Mixed-language title shortening

Many source files contain both an English title and the original Japanese/Chinese/Korean title:

```text
One Piece / ワンピース Ch.005.cbz
```

The shared core prefers the English segment when this looks like a duplicate-title pattern, but preserves chapter/volume data from the original-language segment.

```text
One Piece / ワンピース Ch.005.cbz
→ One Piece Ch.5.cbz
```

Non-Latin-only filenames are preserved instead of being erased:

```text
ワンピース Ch.005.cbz
→ ワンピース Ch.5.cbz
```

---

## Root-aware series inference

`infer_series_name()` can skip container folders such as `Issues`, `Chapters`, `Volumes`, `Extras`, and `Specials`.

```python
path = Path(r"\\tower\media\comics\Marvel\Batman\Issues\Batman Ch.5.cbz")
root = Path(r"\\tower\media\comics")

infer_series_name(path, root)
# -> "Batman"
```

---

## ComicInfo XML updates

`update_comicinfo_xml()` accepts existing XML text and a `ParsedComicName`, then returns:

```python
(new_xml, changed)
```

The watcher uses `changed` to avoid unnecessary archive rewrites.

### Title overwrite policy

`<Title>` is replaced only when:

- missing
- blank
- generic
- gibberish
- equal to the series name

Custom titles are preserved.

### Series, Number, Volume

- `<Series>` is always set to the normalized/inferred series.
- `<Number>` is set when a chapter number is detected.
- `<Volume>` is set when a volume number is detected.

---

## Import pattern

Scripts that may run from repo root or directly inside `scripts/` should use the dual import shim:

```python
try:
    from scripts.cbz_core import parse_comic_name, update_comicinfo_xml
except ModuleNotFoundError:
    from cbz_core import parse_comic_name, update_comicinfo_xml
```


---

# File: `docs/shared_pipeline.md`

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


---

# File: `docs/cbz_watcher.md`

# cbz_watcher.py

Live file watcher. Monitors an Incoming folder for `.cbz` files, applies the full cleaning/tagging pipeline, and moves each processed directory to its configured destination. Uses `watchdog` for filesystem event monitoring.

---

## Shared-core integration

`cbz_watcher.py` imports shared helpers directly from `scripts/cbz_core.py`:

- `clean_directory_name()`
- `clean_filename()`
- `parse_comic_name()`
- `update_comicinfo_xml()`

A fallback import shim supports both repo-root execution and direct-script execution from inside `scripts/`.

This migration eliminated duplicated regex sets, manual filename-pipeline reconstruction, and local ComicInfo title-selection logic from the watcher.

---

## Processing Pipeline

For each directory that passes the timers:

1. **Top-level directory rename** — cleans the incoming directory name via `clean_directory_name()`.
2. **Stability check** — verifies each `.cbz` file is stable.
3. **Filename parsing** — `parse_comic_name()` from `cbz_core.py` performs the full shared normalization pipeline and returns a structured `ParsedComicName` object.
4. **Rename** — renames each `.cbz` if the parsed filename differs.
5. **ComicInfo.xml** — delegates metadata decisions to `update_comicinfo_xml()` from `cbz_core.py`. Existing XML is updated using `ElementTree`; custom titles are preserved automatically.
6. **Archive rewrite** — rewrites the archive only if XML changed, preserving original compression.
7. **Route & move** — resolves the destination via `routing.json`.
8. **Merge** — if the destination directory already exists, merges file by file; larger file wins.

See [shared_pipeline.md](shared_pipeline.md) and [cbz_core.md](cbz_core.md) for the full normalization and ComicInfo rules.

---

## Configuration

Edit the constants at the top of `scripts\cbz_watcher.py`:

```python
WATCH_FOLDER  = r"C:\Temp\Mega\Mega Uploads\book2"
LOG_FILE      = r"C:\git\ComicAutomation\cbz_watcher.log"
ROUTING_FILE  = r"C:\git\ComicAutomation\routing.json"
POLL_INTERVAL = 2
SETTLE_DELAY  = 5
MIN_AGE       = 300
```

---

## Running

```powershell
python scripts\cbz_watcher.py
```

---

## Routing

Destination routing is driven by `routing.json`, an external config file. A `routing.example.json` template is provided in `config/`.

---

## Windows Notes

- `_processing_dirs` suppresses watchdog events fired by the watcher's own rename operations.
- `_on_settled()` checks whether a directory or any parent/child is already being processed.
- `_move_cbz_dir()` checks whether the source still exists before moving and falls back to merge if the destination appears mid-move.
- Before calling `Path.rename()`, the watcher checks whether the target exists.

---

## Logging

Rotating log file at `LOG_FILE` plus stdout.


---

# File: `docs/cbz_sanitizer.md`

# cbz_sanitizer.py

Batch sanitizer. Recursively scans a library folder for `.cbz` files and applies the full cleaning and tagging pipeline in-place: filename normalization, directory renaming, and `ComicInfo.xml` creation/repair.

`cbz_sanitizer.py` imports shared normalization and ComicInfo helpers from `scripts/cbz_core.py`, which serves as the suite-wide shared core layer.

---

## Processing Pipeline

For each `.cbz` file found:

1. **Filename parsing** — `parse_comic_name()` runs the shared normalization pipeline and returns structured filename/chapter/volume metadata.
2. **Rename** — renames the `.cbz` file if the parsed filename differs.
3. **ComicInfo.xml** — creates one from the built-in template if absent, or reads the existing one.
4. **Tag update** — delegates metadata decisions to `update_comicinfo_xml()` from `cbz_core.py`, preserving custom titles while normalizing generic metadata.
5. **Archive rewrite** — if any tag or XML changed, rewrites the archive while preserving the original compression type.
6. **Directory rename** — after all files in a subdirectory are processed, renames the directory itself if its cleaned name differs.

See [shared_pipeline.md](shared_pipeline.md) and [cbz_core.md](cbz_core.md).

---

## CLI Usage

```powershell
python scripts\cbz_sanitizer.py
python scripts\cbz_sanitizer.py --dry-run
python scripts\cbz_sanitizer.py --workers 4
python scripts\cbz_sanitizer.py --resume
python scripts\cbz_sanitizer.py --restart
```


---

# File: `docs/engineering_decisions.md`

# Engineering Decisions

A record of non-obvious design choices in the suite and the reasoning behind them.

---

## Shared cbz_core.py normalization layer

**Decision:** Shared normalization, parsing, and ComicInfo logic now lives in `scripts/cbz_core.py`. Watcher and batch tools import helpers from the shared module rather than maintaining duplicated copies.

**Why:** The earlier architecture relied on manually syncing duplicated regexes and helper functions between `cbz_sanitizer.py`, `cbz_watcher.py`, and other tools. This repeatedly caused drift bugs where a fix landed in one script but not another.

Moving the logic into `cbz_core.py` creates:

- one authoritative normalization pipeline
- one authoritative ComicInfo update policy
- one authoritative regex set
- reusable structured parsing via `ParsedComicName`
- safer future migrations for dedupe, indexing, and image-aware processing

---

## parse_comic_name() as the authoritative normalization pipeline

**Decision:** Filename generation and metadata extraction now flow through `parse_comic_name()` instead of each script manually chaining helper functions.

**Why:** The old watcher implementation reconstructed the normalization pipeline manually:

```python
clean_filename()
normalize_stem()
normalise_number_tokens()
```

This recreated the exact drift problem the shared-core migration was meant to eliminate.

---

## Mixed-language title shortening instead of destructive Unicode stripping

**Decision:** The suite no longer aggressively strips all non-Latin text during sanitization.

**Why:** Many incoming archives contain both English and original-language titles, such as:

```text
One Piece / ワンピース Ch.005.cbz
```

The new pipeline preserves non-Latin-only titles, prefers English-heavy segments when duplicate-language titles exist, preserves chapter/volume metadata, and removes only Windows-forbidden path characters.

---

## ElementTree-based ComicInfo updates

**Decision:** ComicInfo updates now use `xml.etree.ElementTree` instead of regex substitution.

**Why:** Regex replacement against XML was fragile around multiline formatting, namespaces, malformed XML, and duplicate tags.

---

## Larger file wins on conflict

**Decision:** When two files collide during a merge or move, the larger file is always kept.

**Why:** File size is a practical proxy for scan quality and avoids human prompts during large library merges.

---

## External routing config

**Decision:** Destination routing is driven by `routing.json`.

**Why:** Routing is machine-specific and easier to maintain outside Python code.

---

## Runtime files kept off the repo

**Decision:** Routing files, logs, and progress JSON contents are excluded from git.

**Why:** They are machine-specific runtime state and create noisy diffs.

---

## Dry-run on all batch tools

**Decision:** Every modifying batch tool supports `--dry-run`.

**Why:** Large-library operations need preview mode before applying changes.


---

# File: `docs/other_tools_update_note.md`

# Other Tools — Shared Core Note

Add this note near the top of `docs/other_tools.md`:

```md
> Shared normalization note: tools should migrate toward importing filename, directory, chapter/volume, and ComicInfo helpers from `scripts/cbz_core.py` instead of carrying local copies of regex and normalization logic. See [cbz_core.md](cbz_core.md) and [shared_pipeline.md](shared_pipeline.md).
```
