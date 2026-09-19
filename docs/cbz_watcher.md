# CBZ Watcher

`scripts/cbz_watcher.py` monitors an incoming folder with `watchdog`, processes every CBZ in a settled directory, and then moves or merges that directory into a configured destination.

## Configuration

Current code-level defaults include:

```python
WATCH_FOLDER = r"C:\Temp\Mega\Mega Uploads\book2"
POLL_INTERVAL = 2
SETTLE_DELAY = 5
MIN_AGE = 300
ROUTING_FILE = REPO_ROOT / "routing.json"
LOG_FILE = REPO_ROOT / "Logs" / "cbz_watcher.log"
```

These values are machine-specific and should be reviewed before use.

## Processing unit

The immediate comic directory is the batch:

1. Wait for inactivity and minimum-age requirements.
2. Suppress events caused by the watcher's own operations.
3. Clean the top-level directory name.
4. Enumerate and stabilize all CBZ files.
5. Parse each filename with `cbz_core.parse_comic_name()`.
6. Rename archives when needed.
7. Create or update `ComicInfo.xml`.
8. Optionally AI-decensor each archive with Camelia, one book at a time.
9. Resolve the destination through `routing.json`.
10. Move the directory.
11. Merge file-by-file if the destination exists.

## Optional Camelia stage

The CBZ Automation GUI exposes **AI decensor each CBZ before import** on the
CBZ Watcher screen. It is off by default. When enabled, choose one to four
Camelia stages in order, including Aletheia-Lens `mosaic`. Stage 1 defaults to
`black_bars`; stages 2 through 4
default to `None`, preserving the previous single-stage behavior. Each later
stage consumes the images produced by the preceding stage.

The `mosaic` stage uses Camelia's separately installed Aletheia-Lens backend.
Set it up once with `C:\git\camelia\scripts\setup_aletheia.ps1` before enabling
it in the watcher. A missing backend fails that book before it is routed; the
watcher's existing directory rollback handling remains in effect.

Camelia runs only when the watcher's resolved destination contains an exact
`Comix` directory segment. Manga, graphic-novel, and other destinations skip
the AI stage and continue through the normal import instead of failing it.

The watcher invokes Camelia's standalone `scripts/process_cbz.py` interface;
the Camelia web server does not need to be running. Each successfully rebuilt
archive keeps its watcher-normalized filename and internal archive structure.
Before replacement, the untouched source is retained below
`data/ai-decensor-originals/<job-id>/`.
This C: copy remains available throughout the directory import for rollback.
After the entire directory routes successfully, the watcher copies each
original to `F:\ai-decensor-originals\<Series>\<Book>.cbz`, verifies its
SHA-256 digest, and only then removes the C: copy. Same-name backups with
different contents get a readable timestamp suffix; exact duplicates are
deduplicated. If F: is unavailable or verification fails, the C: original
remains and the watcher logs the failed offload. The GUI can choose a different
archive root, or the CLI can use `--ai-decensor-archive-dir`.

This stage fails closed. If Camelia fails on any book, the directory is not
routed. Any earlier AI replacements from that directory pass are restored from
their quarantined originals, preventing partially decensored imports.

Equivalent command-line use:

```powershell
python -m scripts.cbz_watcher --ai-decensor --ai-decensor-model black_bars --ai-decensor-model transparent_black --ai-decensor-model white_bars --ai-decensor-model mosaic
```

The default installation paths are `C:\git\camelia` and
`C:\ProgramData\miniconda3\envs\camelia_env\python.exe`. Override them with
`CBZ_CAMELIA_ROOT` and `CBZ_CAMELIA_PYTHON`, or with `--camelia-root` and
`--camelia-python`. Use `--ai-decensor-backup-dir` to select a different local
quarantine root.

To offload pre-existing UUID-folder backups, preview and then apply:

```powershell
python -m scripts.ai_decensor_backups
python -m scripts.ai_decensor_backups --apply
```

The migration reads `ComicInfo.xml` directly from each CBZ to find its series.
An archive with missing/ambiguous metadata stays on C: for review. The
standalone Camelia Decensor is separate: it preserves its source books and
does not create rollback originals in this C: folder.

## File stability

The watcher uses a rolling size window and tolerates limited SMB metadata jitter. Meaningful growth means a copy is still active. Missing files or exhausted retries are skipped.

## ComicInfo behavior

When an archive lacks `ComicInfo.xml`, the watcher starts from a template containing Komga/Mihon-related namespace fields. Existing metadata is passed through shared `update_comicinfo_xml()` decisions.

Archive rewrites:

- preserve ZIP-entry compression methods;
- use temporary and backup paths;
- retry file-lock errors;
- avoid rewriting when XML is already correct.

## Routing

Copy:

```text
config\routing.example.json
```

to:

```text
routing.json
```

Example:

```json
{
  "destinations": {
    "comix": "\\\\tower\\media\\comics\\Comix",
    "manga": "\\\\tower\\media\\comics\\Manga"
  },
  "default": "comix",
  "rules": [
    {
      "match": "source",
      "pattern": "MangaDex (EN)",
      "dest": "manga"
    }
  ]
}
```

Rules are ordered; first match wins. Unmatched directories use the default destination.

## Running

```powershell
python -m scripts.cbz_watcher
```

Use the watcher for incoming day-to-day processing. Use unified workflows for retrospective library-wide cleanup.

## Logging

```text
Logs\cbz_watcher.log
```

The rotating log is 5 MB with three backups.
