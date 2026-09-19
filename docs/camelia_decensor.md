# Standalone Camelia Decensor

The CBZ Automation GUI has a **Camelia Decensor** tool separate from CBZ Watcher.
Choose one `.cbz` or a folder (searched recursively, up to 1,000 archives), a
separate output directory containing a `Comix` path segment, and the decensor
methods. The default order is `black_bars`, `transparent_black`, `white_bars`,
then Aletheia-Lens `mosaic`. Uncheck methods you do not need. Dry run is the
default and writes nothing.

The command-line equivalent is:

```powershell
python -m scripts.camelia_decensor "X:\comix\Sample.cbz" `
  --output-dir "C:\git\ComicAutomation\data\decensored\Comix" `
  --model-type black_bars --model-type transparent_black `
  --model-type white_bars --model-type mosaic --dry-run
```

Remove `--dry-run` to process. `CBZ_CAMELIA_ROOT` and `CBZ_CAMELIA_PYTHON`
override the default Camelia checkout (`C:\git\camelia`) and its Python
environment (`C:\ProgramData\miniconda3\envs\camelia_env\python.exe`). The
Camelia model files and separate Aletheia-Lens installation must be available.
The Camelia web server does not need to be running.

Camelia writes `uncensored` plus exact method tags (`camelia:black_bars`,
`camelia:transparent_black`, `camelia:white_bars`, `camelia:mosaic`) into
ComicInfo.xml after successful processing. The tool skips selected methods
already recorded and can apply a different method later to that output CBZ.
Older books with only a generic `uncensored` or `decensored` marker have unknown
method history and are skipped unless **Reprocess** is checked (or `--reprocess`
is passed). That override repeats every selected method. It calls
Camelia's shared multi-stage pipeline for each remaining book. Folder structure
is preserved below the output directory. Successful output is verified to have
an `uncensored` marker. Original CBZs are never changed. A pre-existing output
is verified and skipped, and the crawl continues to later books. When a
method-tagged source has an existing output name, the next result goes under
`_additional_methods\<method-name>\` instead, preserving the earlier copy.
If that alternate name also exists, it is likewise verified and skipped. A
conflicting, corrupt, incomplete, or incorrectly tagged existing output still
stops the batch without being overwritten. Copy installation also fails if
another process creates that filename while a book is being processed.

This is a batch, not a durable queue. Completed copies remain after an
interrupted run. Existing copies are automatically skipped only after checking
their ZIP CRC, member manifest, unchanged non-image members, decodable output
pages, and the selected method tags. The legacy `--resume` option is retained
for command compatibility and cannot be combined with `--reprocess`; use dry
run first to review the remaining book count. Camelia checks each extracted image before model work and
reports the source CBZ member if an image is mislabeled or unreadable. Use
Camelia's library backlog for a durable multi-day crawl. The GUI Stop button ends the
current process tree on Windows, potentially leaving Camelia's temporary work
files, but does not touch source archives or install an incomplete result.
Hard-link support on the output filesystem is required for the atomic,
no-overwrite copy installation; unsupported filesystems fail closed.

## Reconciling Camelia's in-place X: backlog with this database

Camelia's resumable backlog can replace a CBZ at the same `X:\comix` path.
That changes its archive SHA-256 and usually its size/mtime. The watcher above
only watches its incoming folder, so it does not register this X: replacement.
`comic_automation.library.camelia_handoff` consumes Camelia's durable
`job_complete` events instead of rescanning the whole library.
Before model processing, the same module can register a previously untracked
X: book and establish its original SHA-256/current revision, so its later
replacement retains that logical archive ID. The preflight refuses sync
history paths such as `.stversions`, symlinks, and ambiguous same-stat bytes.

Preview (strictly read-only):

```powershell
python -m comic_automation.library.camelia_handoff `
  --camelia-database "C:\git\camelia\camelia-decensor\state\library_backlog.sqlite3" `
  --database "G:\ComicAutomation\TestDatabase\inspection-working.db" `
  --root "X:\comix" --limit 25
```

Preview registration for one untracked book (also read-only):

```powershell
python -m comic_automation.library.camelia_handoff `
  --database "G:\ComicAutomation\TestDatabase\inspection-working.db" `
  --root "X:\comix" --prepare-source "X:\comix\Series\Book.cbz"
```

Camelia uses `--apply` for that one-book preflight only when its queue is
explicitly resumed. No full-library registration or migration is automatic.

Only after reviewing the preview and verifying a suitable schema should an
operator add `--apply`. The consumer never runs migrations itself. It refuses
an untracked path, a missing/non-current location, a symlink, a file that
changed after Camelia marked it complete, or ambiguous same-size/same-mtime
bytes. It also refuses databases lacking the inspection, archive-hash,
page-hash and archive-revision tables. The working database at the example
`G:` path was reviewed read-only on 2026-09-16: migrations 001–014, all
required tables present, `quick_check` returned `ok`, and the handoff preview
found zero completed Camelia events to apply. No migration is required for
this handoff. `G:\ComicAutomation\database\comics.db` is an older, separate
database at migrations 001–002 and must not be substituted for the working
database. Verify the path again before setting Camelia's handoff variable.

Each accepted event is atomic with a consumer cursor in
`application_settings`. A same-path replacement keeps `archive_files.id`,
updates the location and archive SHA-256, records/reuses the current revision,
and enqueues fresh structural inspection and exact page hashing. Page and
perceptual evidence still require their normal workers; this handoff does not
silently claim that those asynchronous stages have finished. A crash before
commit replays the event, and a crash after commit sees the advanced cursor.
Deleting/recreating the Camelia backlog at the same path changes its instance
identifier, so the cursor does not skip events from a new database.

Camelia can call this consumer after each completed book by setting
`CAMELIA_COMIC_AUTOMATION_DATABASE` to the reviewed database path before
starting its server. Its queue pauses if registration or handoff fails, and
scheduled X: processing is blocked unless both are configured. The Camelia backlog
keeps a local rollback copy during replacement, then calls
`scripts.ai_decensor_backups --source-file ... --apply` to copy and SHA-256
verify it into a ComicInfo Series folder on F:. Only after verification is the
C: copy removed. On queue restart, pending UUID job folders are reconciled
first. A missing Series, an unavailable/full F: drive, or a failed copy pauses
the queue with the C: original retained. Camelia requires this offload
integration for backlog processing; the default destination is
`F:\ai-decensor-originals`, with `CAMELIA_BACKUP_ARCHIVE_ROOT` for another F:
folder.
