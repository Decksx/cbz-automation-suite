# CBZ Sanitizer

`scripts/cbz_sanitizer.py` recursively sanitizes CBZ archives in place. It processes one direct CBZ-containing series directory per worker while keeping files within a series serial for deterministic rename and collision handling.

## Current defaults

```python
SCAN_FOLDER = r"\\tower\media\comics\manga"
LOG_FILE = REPO_ROOT / "Logs" / "cbz_sanitizer.log"
PROGRESS_FILE = REPO_ROOT / "progress_tracking" / "cbz_sanitizer_progress.json"
DEFAULT_WORKERS = min(20, os.cpu_count() or 12)
```

Prefer command-line `--scan` rather than editing `SCAN_FOLDER` for each run.

## Usage

```powershell
python scripts\cbz_sanitizer.py --scan "\\tower\media\comics\Manga"
python scripts\cbz_sanitizer.py --scan ROOT --dry-run
python scripts\cbz_sanitizer.py --scan ROOT --workers 8
python scripts\cbz_sanitizer.py --scan ROOT --sort=alpha
python scripts\cbz_sanitizer.py --scan ROOT --rules=leading_nums,trailing_junk
```

Accepted forms:

```text
--scan=PATH
--scan PATH
--workers=N
--workers N
```

Forward-slash UNC paths from GUI pickers are normalized to Windows UNC paths.

## Sort modes

```text
newest
oldest
alpha
alpha-reverse
```

Default: `newest`.

## Persistent progress

The progress file is append-only JSONL.

- Session records contain `session` and a format version.
- Completed-file records contain `p`.
- Prior sessions are always loaded.
- `--resume` remains a compatibility no-op because normal runs already resume.
- `--restart` removes the progress file and starts with an empty processed set.
- Dry runs do not modify progress.

## Incremental scanning

Incremental mode restricts work to files modified after a cutoff.

It is enabled when:

- `--incremental` is supplied;
- `--since=...` is supplied; or
- sort mode is `newest`, unless `--full` or `--restart` overrides it.

Cutoff behavior:

- explicit `--since` uses the supplied value;
- otherwise the most recent prior session timestamp is used;
- when no prior session exists, the run falls back to a full scan.

Accepted `--since` values:

```text
7d
24h
30m
2026-05-20
2026-05-20 14:30:00
2026-05-20T14:30:00
epoch seconds
```

Force a complete scan:

```powershell
python scripts\cbz_sanitizer.py --scan ROOT --full
```

## Rule selection

Omitting `--rules` runs all rules.

| Rule | Purpose |
|---|---|
| `brackets` | Remove bracketed and parenthesized source fragments |
| `comicinfo` | Update ComicInfo metadata |
| `leading_nums` | Remove leading source IDs and numeric prefixes |
| `non_latin` | Apply configured non-Latin cleanup |
| `normalize_stem` | Normalize generic chapter stems |
| `number_tokens` | Normalize chapter and volume tokens |
| `scan_groups` | Remove scanlation-group tokens |
| `trailing_junk` | Remove trailing separators |
| `url` | Remove URLs and domain-like tokens |

CJK translation and title-selection behavior are supplied by `cbz_core.py` when available.

## Per-series processing

1. Clean or merge the directory name.
2. Build fallback filenames when cleaning produces an empty archive stem.
3. Process each CBZ serially.
4. Rename and update ComicInfo.
5. Detect likely concatenated compilation chapter numbers.
6. Append persistent progress.
7. Report processed, renamed, skipped, and compilation-fix counts.

## Logging

```text
Logs\cbz_sanitizer.log
```

The rotating log is 5 MB with three backups and also streams to the console.
