# CBZ Automation Suite

Windows-focused automation suite for organizing, cleaning, tagging, deduplicating, and routing `.cbz` comic archives.

Designed for:

- large manga/comic libraries
- automated ingest pipelines
- Komga/Mihon-compatible metadata
- Windows + UNC/network-share environments
- long-running unattended processing

---

# Features

## Shared Core Architecture

The suite now uses a centralized shared core module:

```text
scripts/cbz_core.py
```

This module owns:

- filename normalization
- directory cleanup
- chapter/volume extraction
- mixed-language title shortening
- ComicInfo.xml update logic
- root-aware series inference
- shared regex patterns

Watcher and batch tools import from the shared module instead of maintaining duplicated copies.

---

## Automatic Watcher Pipeline

```text
Incoming Folder
      ↓
cbz_watcher.py
      ↓
normalize filenames
      ↓
update ComicInfo.xml
      ↓
route by source/title
      ↓
move into library
```

The watcher:

- waits for downloads to settle
- prevents partial-file processing
- rewrites ComicInfo.xml safely using ElementTree
- preserves custom titles
- merges duplicate directories automatically
- keeps the larger file on collisions
- routes using external JSON config

---

## Mixed-language Filename Handling

The suite safely handles archives containing both English and original-language titles.

Example:

```text
One Piece / ワンピース Ch.005.cbz
→ One Piece Ch.5.cbz
```

Unlike older sanitizers, the pipeline:

- preserves non-Latin-only titles
- keeps chapter/volume metadata
- avoids destructive Unicode stripping
- removes only Windows-forbidden path characters

---

# Repository Structure

```text
cbz-automation-suite/
├── scripts/
│   ├── __init__.py
│   ├── cbz_core.py
│   ├── cbz_watcher.py
│   ├── cbz_sanitizer.py
│   ├── cbz_library_maintenance.py
│   ├── cbz_compilation_resolver.py
│   ├── cbz_gap_checker.py
│   └── __init__.py
├── tests/
├── docs/
├── config/
├── progress_tracking/
├── pytest.ini
└── README.md
```

---

# Main Tools

| Tool | Purpose |
|------|---------|
| `cbz_core.py` | Shared normalization/parsing/ComicInfo layer |
| `cbz_watcher.py` | Live automated ingest watcher |
| `cbz_sanitizer.py` | Batch filename + ComicInfo cleaner |
| `cbz_library_maintenance.py` | Consolidated archive cleanup, series organization, and metadata repair |
| `cbz_compilation_resolver.py` | Resolve compilation vs chapter overlap |
| `cbz_gap_checker.py` | Missing chapter scanner |

---

# Installation

## Requirements

- Python 3.11+
- watchdog
- pytest (development/testing)

Install dependencies:

```powershell
pip install watchdog
python -m pip install -U pytest
```

Clone the repository:

```powershell
git clone https://github.com/Decksx/cbz-automation-suite.git
cd cbz-automation-suite
```

---

# Running

## Watcher

```powershell
python scripts\cbz_watcher.py
```

## Sanitizer

```powershell
python scripts\cbz_sanitizer.py --dry-run
```

## Tests

```powershell
python -m pytest
```

---

# Configuration

Routing is controlled externally through:

```text
routing.json
```

Example:

```json
{
  "destinations": {
    "comics": "\\\\tower\\media\\comics\\Comix",
    "manga": "\\\\tower\\media\\comics\\Manga"
  },
  "default": "comics",
  "rules": [
    {
      "match": "source",
      "pattern": "MangaDex (EN)",
      "dest": "manga"
    }
  ]
}
```

---

# Testing

Regression tests currently cover:

- filename normalization
- mixed-language shortening
- chapter/volume parsing
- root-aware series inference
- ComicInfo title preservation
- generic-title replacement

Run:

```powershell
python -m pytest
```

Windows GitHub Actions CI is included.

---

# Documentation

See:

- `docs/overview.md`
- `docs/cbz_core.md`
- `docs/shared_pipeline.md`
- `docs/cbz_watcher.md`
- `docs/cbz_sanitizer.md`
- `docs/engineering_decisions.md`

---

# Design Goals

- unattended operation
- deterministic cleanup
- Windows-safe filesystem behavior
- reusable shared normalization logic
- low-maintenance long-term architecture
- safe metadata rewriting
- large-library scalability

---

# License

MIT License
