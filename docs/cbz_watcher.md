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
3. **Claim** it: move it out of the watch folder into the processing root
   (see below). Nothing in the watch folder is renamed or rewritten.
4. Re-check, inside the claim, that every CBZ is complete.
5. Clean the series folder name.
6. Parse each filename with `cbz_core.parse_comic_name()`.
7. Rename archives when needed.
8. Create or update `ComicInfo.xml`.
9. Optionally AI-decensor each archive with Camelia, one book at a time.
10. Use `routing.json` to obtain a fallback category for a new series.
11. Optionally resolve the canonical series directory across both libraries.
12. Move the directory, or merge it file by file if the destination exists.
13. Send what cannot land to review or quarantine, and retire the claim.

## Claim-first processing

The watch folder is synced by MEGA: every rename or ComicInfo write there is
uploaded, and an archive held in place was re-processed on every pass.
Remembering what had been decided about held archives was what lost books in
issue #174. Since 2026-11-09 an arrival leaves the watch folder before
anything changes it, and **the folder an archive is in is its state**:

| Root | Default | Meaning | Kept |
| --- | --- | --- | --- |
| processing | `C:\Temp\cbz_processing` | claimed, work not finished | until finished |
| review | `C:\Temp\cbz_held\review` | needs a decision | indefinitely |
| quarantine | `C:\Temp\cbz_held\quarantine` | not kept | 30 days |
| library | routed destination | done | |

Set them with `--processing-root`, `--review-root` and `--quarantine-root`
(or `CBZ_PROCESSING_ROOT`, `CBZ_REVIEW_ROOT`, `CBZ_QUARANTINE_ROOT`).

**Claiming.** Each comic directory becomes a claim folder
`<processing>\<YYYYMMDD-HHMMSS-id>\` holding `claim.json` and the payload.
The record (watch root, source folder, relative path, routing) is written
first; then the series folder moves in with one same-volume rename, the first
change ever made to the arrival. A failed record write or rename leaves the
arrival untouched for the next pass. The loose archives of a mixed drop point
are claimed one by one (its subfolders are other series and are claimed on
their own). A series folder that also holds subfolders, such as Syncthing's
`.stversions`, has only its files claimed; the subfolders stay, and folders
left empty are removed.

**Startup.** The three roots must be outside the watch root and every library
root, must not overlap one another, and must be on the watch root's volume
(physical paths, junctions resolved), so every claim and disposition is one
atomic rename. Otherwise the watcher refuses to start (exit 3) rather than
copy and delete inside the synced folder. It then purges quarantine, removes
stale `.tmp.cbz`/`.bak.cbz` rewrite files in the processing root (never in
the watch folder) and finishes unfinished claims before discovering new
arrivals.

**Resume.** Each claim has one owner at a time (`owner.lock`, released by the
OS if its process dies). An unfinished claim is resumed before new arrivals;
every archive in it is treated as already processed for collision purposes,
and a crashed landing (an empty reservation or a hidden `.<name>.cbz-landing`
partial copy in the library) is cleared first. A claim folder whose record is
missing or unreadable is moved whole to `<review>\_unrecovered_claims\`
instead of being guessed at. A claim recorded for another watch root is left
to its watcher. A source path recreated after a claim is a new arrival.

**Dispositions.** Decisions are written to the record before anything moves,
so a crash in between is finished on the next run:

- to **review** (`<review>\<YYYY-MM-DD>\<series>\<file>`): an archive media
  or fallback naming refuses (a `REVIEW:` line gives the reason), an archive
  that would collide with a different library book only because a tag was
  stripped (issue #131), and an archive whose AI rewrite state is unknown;
- to **quarantine**: a second copy of a chapter from another scanlation group
  (`_duplicate_chapter_copies`: the copy whose group has the most archives in
  the directory lands, then the larger file; a refused preferred copy never
  holds a valid one back) and the losing copy of a name collision (below);
- **stay in processing** and retry next pass: anything transient, such as a
  locked file, a failed move, a number-style registry refusal, an unsettled
  series identity or an incomplete archive.

A failed move to review or quarantine keeps that archive in processing. Their
siblings land as usual; when every archive is held, no series folder is
created. Review is never purged: move or delete what is there by hand.
Quarantine is purged by placement date after 30 days; only the dated
`YYYY-MM-DD` folders the watcher creates are removed, so hand-placed items
there are left alone.

## Name collisions never delete a different copy

When an arriving archive lands on a name the destination already holds, the
watcher keeps one copy under that name and **moves the other to the
quarantine**; it never deletes a copy that differs from the one it keeps.

- A byte-identical arrival (same size and SHA-256) is an exact duplicate and
  is removed: nothing is lost.
- Otherwise the larger copy is kept, as before (an arrival must be larger by
  `REPLACEMENT_MIN_GAIN_BYTES` to replace the library copy). The other goes
  to `<quarantine>\<YYYY-MM-DD>\<series folder>\<file>`, renamed
  `<file> (2).cbz` and so on if that name is taken. The dated folder is the
  day it was quarantined, which a retention purge can use.
- The loser is moved to quarantine before anything else changes. If that
  fails, both copies stay where they were and the arrival is retried on the
  next pass. If the arrival then cannot take the library copy's place, the
  quarantined library copy is moved back.
- Every placement owns its target first: the quarantine name, and a new
  library name, are created exclusively (`O_CREAT | O_EXCL`) before the
  move, so two imports running at once can never pick the same name and
  overwrite one another. A failed placement removes only the name it
  created. A library name taken by another arrival in the meantime is
  left alone; the arrival stays in its claim and is settled as a collision
  next pass.
- One settlement owns a library name at a time. Before anything about the
  name is decided, the watcher takes that name's lock (one per normalized
  physical path, so names differing only in case share it) and holds it
  through the decision, the quarantine, the landing, any rollback and any
  cleanup. Another arrival for the same name meanwhile does not wait: it
  stays in its claim and is settled on its next pass. This covers the
  threads of one watcher process (one watcher per watch root); another
  process writing the same library names is not coordinated by it.
- A whole series folder moves into the library only as one rename, which
  is atomic and possible only on the same volume when the series folder is
  not there yet. Otherwise (the library on Tower is another volume) it is
  merged file by file, each file under its own lock as above; a folder is
  never copied straight into the library, where another arrival could
  settle against a half-copied book. Across volumes each file is copied to
  a hidden `.<name>.cbz-landing` partial, flushed, and renamed over the
  name's reservation, so the library name only ever holds an empty
  reservation or the complete book; the claimed copy is removed after.
- Only a settled collision or a completed landing counts as landed; a
  failed one is not reported as a landing. A folder merge in which no file
  completed returns no landing either.

The quarantine is `C:\Temp\cbz_held\quarantine` by default; set it with
`--quarantine-root` or `CBZ_QUARANTINE_ROOT`. Resolver mode keeps its own
quarantine root (`--resolver-quarantine-root`), which already moved losers
rather than deleting them.

## Canonical series resolver

The GUI enables **Resolve series names and import directly to Z:** by default.
The source/category rules in `routing.json` provide the fallback category only
for a genuinely new series. Existing series aliases are checked in both Comix
and Manga first, so a mixed download source cannot override an established
library location. The current `X:\_staging` paths remain review fallbacks.
Confirmed imports go to:

```text
Z:\comics\Comix\<canonical series>
Z:\comics\Manga\<canonical series>
```

The trailing `\<canonical series>` is a placeholder, not a literal `$`
directory. The roots can be changed in the GUI or with CLI arguments when the
future X: migration is ready; no X: final-library destination is hard-coded.

Each root stores `_series_registry.json` and `_series_aliases.json`. A missing
registry is built automatically at resolver-enabled startup. Enable **Rebuild
series registries before watching** after manually moving or renaming series.
That scan teaches the resolver the corrected Comix/Manga location on the next
watcher start. If a folder was moved without being renamed, the watcher also
recognizes its new root immediately by validating the old registry path against
both libraries. The refresh remains important because it removes the stale old
record and makes the correction durable. Otherwise the watcher loads the
existing registry and records newly observed aliases after a successful import.
Registry and alias writes are atomic.

New series names always normalize curly, modifier, full-width, and other
apostrophe variants to the straight ASCII apostrophe. To repair existing names,
enable **Normalize apostrophes and merge duplicate series names** for one
watcher start, or add `--resolver-normalize-apostrophes`. The repair runs before
registry loading across folders and files. If normalized names collide, exact
duplicates are removed; non-identical files keep the larger copy and move the
smaller copy below the external resolver quarantine. The registry is then
rebuilt, preventing `Debra's Story` and `Debra’s Story` from remaining
ambiguous aliases.

Resolution uses, in order:

1. exact normalized aliases;
2. one unambiguous fuzzy alias at or above 95%;
3. a safe new title, using the metadata pipeline's English translation when available.

Normalization ignores case, accents, whitespace, underscores, hyphens, and
punctuation. Alias sources include directory names, `_series_aliases.json`,
`series.json`, representative `ComicInfo.xml`, and compatible cleanup history.
The watcher's existing metadata translation runs before lookup, so native
foreign-script and confidently detected romanized titles can use their English
ComicInfo series name. Original-language and romanized aliases remain usable
for later imports.

An ambiguous exact or fuzzy result is never guessed. The same applies when a
title has equally strong matches in both libraries. It stays in the legacy
`X:\_staging\Comix` or `X:\_staging\Manga` path and is logged with its
candidates and candidate categories for review.

When a destination filename already exists, resolver mode hashes equal-size
files. Byte-identical incoming duplicates are removed. For non-identical
conflicts the larger file is retained and the smaller file is moved outside
Komga to:

```text
Z:\comics\_resolver_quarantine\Comix\<series>\...
Z:\comics\_resolver_quarantine\Manga\<series>\...
```

Plain CLI launches retain the legacy staging behavior. Enable direct resolving
explicitly with:

```powershell
python -m scripts.cbz_watcher --series-resolver
```

Relevant overrides are `--resolver-comix-root`, `--resolver-manga-root`,
`--resolver-quarantine-root`, `--resolver-threshold`, and
`--resolver-refresh`. Resolver-enabled startup fails if a configured Z: root is
unavailable, rather than silently accumulating imports in the old staging
workflow.

After correcting a location manually, stop the watcher and launch once with
`--series-resolver --resolver-refresh`, or select **Rebuild series registries
before watching** in the GUI before starting it again. The refresh is performed
at startup; it is not a background rescan of a running watcher.

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
their quarantined originals, preventing partially decensored imports; the
claim stays in processing for a retry.

The claim record keeps custody of each rewrite: it is marked started before
Camelia runs (if that cannot be written, nothing is rewritten) and done, with
its backup path, afterwards. A resumed claim never runs Camelia again on a
book recorded done, and sends a book recorded started but not done to review,
since its state is unknown.

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

The rotating log is 5 MB with three backups. Held directories are recorded
beside it in `Logs\cbz_watcher_held.json` (see Held archives).
