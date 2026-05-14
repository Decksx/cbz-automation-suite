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
