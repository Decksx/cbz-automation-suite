# Archive I/O & Resource-Limit Audit

**Status:** evidence-only audit. No production code was changed as part of this
document. Findings reference function names, not line numbers, since line
numbers drift with every edit; every claim below was verified by reading the
actual source referenced.

**Scope:** six components involved in reading, hashing, or rewriting CBZ
(zip) archives:

- `comic_automation/archive/inspection.py`
- `comic_automation/archive/handlers.py`
- `comic_automation/archive/page_hashing.py`
- `comic_automation/archive/perceptual_hashing.py`
- `scripts/cbz_sanitizer.py`
- `scripts/cbz_library_maintenance.py`

**Out of scope (see Deferred work):** `scripts/cbz_compilation_resolver.py`,
`comic_automation/jobs/worker.py`'s retry/backoff mechanics, and any
end-to-end tracing of encrypted-archive handling.

For each component: whether it streams or materializes archive content,
whether it extracts to disk, existing size/entry/pixel limits, zip-slip/path
validation, memory behavior for large pages and omnibus archives, error
classification and retry, and mutation/replacement safety across SMB.

---

## 1. `comic_automation/archive/inspection.py`

Entry points: `inspect_archive`, `inspect_cbz`, `_read_comic_info`.

**Streaming vs. materialization.** Page/image entries are never opened or
read at all — `inspect_cbz` only calls `archive.infolist()` and classifies
entries by filename suffix (`IMAGE_EXTENSIONS`). The only entry ever read is
`ComicInfo.xml`, via `_read_comic_info`, using a single `archive.read(entry)`
call (whole-member materialization) — but that member is capped in size (see
below), so this is the lowest-risk read in the whole audit.

**Disk extraction.** None. No `.extract()`/`.extractall()` calls anywhere in
the file.

**Size limits.** `MAX_COMIC_INFO_BYTES = 1_048_576` (1 MiB), enforced twice in
`_read_comic_info`: once against the declared `entry.file_size` before
reading, and again against `len(payload)` after reading. The second check
prevents an oversized payload from being accepted if its declared metadata
was inaccurate, but it occurs after `archive.read(entry)` and therefore is
not itself a guarantee against a transient oversized allocation from
maliciously inconsistent ZIP metadata. No size limit exists for page/image
entries, but none are ever read by this module in the first place.

**Entry-count / decoded-pixel limits.** No cap on entry count — `infolist()`
is iterated in full — but this is classification-only (filename suffix
checks), not decoding, so cost is O(entries) with no decompression. No
image decoding happens in this file at all, so no pixel limit applies here.

**ZIP-slip / path validation.** Not applicable — nothing is ever extracted
to a filesystem path.

**Memory behavior for large pages/omnibus archives.** Minimal in ordinary
operation: the only payload ever materialized is `ComicInfo.xml`, which is
rejected when its declared or actual size exceeds 1 MiB. The post-read check
has the allocation caveat described above. Page images are inspected by
metadata only, never decoded. This is the lightest-weight component in the
audit for large/omnibus archives.

**Additional safeguard — XXE mitigation.** Before parsing, `_read_comic_info`
lowercases the raw bytes and rejects the payload if `b"<!doctype"` or
`b"<!entity"` appears, before calling `ElementTree.fromstring`. This blocks
DTD/external-entity-based XML attacks independent of the size cap.

**Error classification and retry.** `zipfile.BadZipFile`, `zlib.error`, and
`EOFError` are caught around the whole `ZipFile` block and re-raised as
`CorruptArchiveError`. An oversized or DTD-bearing `ComicInfo.xml` raises
`UnsafeComicInfoError`, but only sets `comic_info_valid = False` /
`comic_info_error` on the result — it does not abort inspection of the rest
of the archive. There is no retry loop in this module; retry policy is a
job-queue concern applied by the caller (see `handlers.py`).

**Mutation/replacement safety.** Not applicable — this module never writes
to an archive or the filesystem; it only reads.

---

## 2. `comic_automation/archive/handlers.py`

Entry point: `InspectArchiveHandler.__call__`.

This is a thin job-handler wrapper around `inspection.py` — it performs no
zip I/O of its own. It looks up the archive's current location via
`ArchiveInspectionRepository.current_location`, calls `inspect_archive`, and
translates exceptions into the job queue's categorized-error vocabulary:

- `CorruptArchiveError` → `PermanentJobError(category="corrupt_archive")`
- `UnsupportedArchiveFormatError` → `PermanentJobError(category="unsupported_archive_format")`
- `ArchiveInspectionError` (base class, catch-all for the two above) → `PermanentJobError(category="archive_inspection_error")`
- `FileNotFoundError` → `CategorizedJobError(category="filesystem_not_found")`
- `PermissionError` → `CategorizedJobError(category="filesystem_permission")`
- `OSError` → `CategorizedJobError(category="filesystem_io")`

**Streaming/materialization, disk extraction, size/entry/pixel limits,
zip-slip, memory behavior:** entirely inherited from `inspection.py` — see
Section 1; `handlers.py` adds none of its own.

**Error classification and retry.** The categorization above is what feeds
the job queue's retry/permanent-failure decision (`PermanentJobError` vs.
`CategorizedJobError`), but the actual retry/backoff loop that consumes
these categories lives in `comic_automation/jobs/worker.py`, which is out of
scope for this audit (see Deferred work) — this file only produces the
category, it doesn't decide what happens next.

**Mutation/replacement safety.** The only write this handler performs is
`self.repository.save(...)`, writing structured inspection results (status,
counts, parsed metadata) to the database. It never mutates the archive file
itself.

---

## 3. `comic_automation/archive/page_hashing.py`

Entry points: `calculate_page_hashes`, `ArchivePageHashRepository`,
`HashArchivePagesHandler`.

**Streaming vs. materialization.** This is the one component in the audit
that genuinely streams page content: `calculate_page_hashes` opens each
image entry with `archive.open(entry, mode="r")` and reads it in a bounded
chunk loop:

```python
with archive.open(entry, mode="r") as stream:
    while chunk := stream.read(chunk_size):
        digest.update(chunk)
        bytes_read += len(chunk)
```

`chunk_size` defaults to `DEFAULT_CHUNK_SIZE = 1024 * 1024` (1 MiB). The
application-level payload buffer for any single page read is bounded by
`chunk_size`, regardless of the page's actual decompressed size. `zipfile`
and the digest implementation may retain their own small internal buffers,
so this is not a claim that total process memory is exactly 1 MiB per page.

**Disk extraction.** None.

**Size limits.** No explicit pre-read check on `entry.file_size` or
`compress_size` before opening an entry — but because the read itself is
streamed in fixed-size chunks, an unbounded/large page cannot cause an
unbounded single-allocation memory spike the way a `.read()`-based
materialization would; the streaming design substitutes for an explicit cap
here, though a cap on total bytes read per archive is still absent.

**Entry-count / decoded-pixel limits.** No cap on the number of image
entries hashed per archive. No image decoding occurs in this module (it
hashes raw bytes, not decoded pixels), so no pixel limit applies.

**ZIP-slip / path validation.** Not applicable — nothing is extracted to
disk.

**Memory behavior for large pages/omnibus archives.** Bounded per-page by
`chunk_size` as above; pages are processed and their digests finalized one
at a time (the `pages` list accumulates only small `PageContentHash`
dataclass records — page index, name, sizes, CRC, hex digest — never raw
page bytes), so this scales to omnibus archives without a memory footprint
proportional to total page-image bytes.

**Error classification and retry.** `zipfile.BadZipFile`, `zlib.error`,
`EOFError` → `PermanentJobError(category="archive_corrupt")`; `RuntimeError`
(zipfile's own internal-consistency errors) →
`PermanentJobError(category="archive_unreadable")`. The handler layer
(`HashArchivePagesHandler.__call__`) adds the same
`FileNotFoundError`/`PermissionError`/`OSError` → `CategorizedJobError`
ladder as `handlers.py`. No retry-with-sleep loop exists in this module —
retry policy is again a job-queue concern.

**Mutation/replacement safety, especially across SMB.** `calculate_page_hashes`
snapshots `archive_path.stat()` before opening the archive and again after
finishing all page reads, and raises a plain `OSError` if size or mtime
changed in between:

```python
if (
    before.st_size != after.st_size
    or before.st_mtime_ns != after.st_mtime_ns
):
    raise OSError(
        f"Archive changed while hashing pages: {archive_path}"
    )
```

This is a working concurrent-modification detector — it prevents committing
hashes computed from a torn/mid-write read on an SMB share — but note that
this specific `OSError` is not given its own category; at the handler layer
it falls into the generic `filesystem_io` bucket, indistinguishable from an
unrelated I/O error in job diagnostics. The module itself never writes to
the archive; `ArchivePageHashRepository.save` writes only to the database,
wrapped in an explicit `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK` transaction
with the delete-then-reinsert pattern for `archive_pages` rows. No
application-level file lock is used. The archive is never written by this
module, and the before/after stat comparison detects common source drift,
but it does not provide exclusive-read semantics.

---

## 4. `comic_automation/archive/perceptual_hashing.py`

Entry points: `calculate_perceptual_hashes`, `ArchivePerceptualHashRepository`,
`HashArchivePagesPerceptualHandler`.

**Streaming vs. materialization.** Unlike `page_hashing.py`, this module
materializes each page's full decompressed bytes in one call:

```python
payload = archive.read(entry)
...
with Image.open(BytesIO(payload)) as image:
    image.load()
```

This is materialization per-page (not the whole archive simultaneously —
only one page's bytes are held at a time, released before the next
iteration), but it is not chunked/streamed the way `page_hashing.py`'s
`archive.open()`/chunk-loop is.

**Disk extraction.** None.

**Size limits.** No explicit check on `entry.file_size` or `compress_size`
before `archive.read(entry)`. This is a real gap relative to both
`inspection.py`'s double-checked 1 MiB `ComicInfo.xml` cap and
`page_hashing.py`'s bounded-chunk streaming: a page entry with an
arbitrarily large declared or actual size is read into memory in one call
with no pre-flight guard.

**Entry-count / decoded-pixel limits.** No cap exists on the number of pages
processed per archive. Pixel-count protection relies entirely on Pillow's
built-in default `Image.MAX_IMAGE_PIXELS` — no override of this constant
exists anywhere in the codebase (confirmed by search). In the installed
Pillow implementation, that value is the warning threshold; images above
twice the value raise `Image.DecompressionBombError`. Images between the
warning and error thresholds continue processing after emitting
`Image.DecompressionBombWarning`. The error is caught and converted to a
permanent, categorized failure rather than propagating as a crash:

```python
except (
    Image.DecompressionBombError,
    UnidentifiedImageError,
    OSError,
    SyntaxError,
    ValueError,
) as exc:
    raise PermanentJobError(
        "Invalid or unsupported image page "
        f"{entry.filename!r} in {archive_path}: {exc}",
        category="page_image_corrupt",
    ) from exc
```

This exception tuple is scoped tightly around the `Image.open`/`.load()`/
hash calls only (nested inside the archive-level `try`), so a genuine
zip-level `OSError` is not mis-categorized as an image-decode error. The
effective warning and rejection thresholds, however, are derived from
whichever `Image.MAX_IMAGE_PIXELS` value ships with the installed Pillow
version — not values this codebase deliberately chose, documented, or
pinned.

**ZIP-slip / path validation.** Not applicable — nothing is extracted to
disk.

**Memory behavior for large pages/omnibus archives.** One page's full
decompressed bytes plus its decoded Pillow image buffer are held in memory
at a time (not the whole archive), which is more memory-efficient than
`cbz_sanitizer.py`/`cbz_library_maintenance.py`'s whole-archive
materialization pattern (Sections 5–6), but is still a real per-page spike
for an unusually large single page, since there is no size gate before the
read (see Size limits above). Separately, `perceptual_hash()`'s DCT
computation is a pure-Python nested loop over `hash_size × hash_size ×
sample_size × sample_size` terms with no vectorization (e.g. no numpy) —
this is a CPU-cost/architecture observation, not a memory concern, and is
noted here as evidence only; no speed claim or multiplier is asserted (see
Section 4 of Prioritized Findings for why any change here needs
benchmarking and algorithm versioning).

**Error classification and retry.** Same archive-level triad as
`page_hashing.py` (`BadZipFile`/`zlib.error`/`EOFError` → `archive_corrupt`;
`RuntimeError` → `archive_unreadable`), plus the page-level image-decode
categorization above. The handler layer adds the same
`FileNotFoundError`/`PermissionError`/`OSError` → `CategorizedJobError`
ladder. No retry-with-sleep loop exists in this module.

**Mutation/replacement safety, especially across SMB.** Same before/after
`stat()` concurrent-modification guard as `page_hashing.py` (raises
`OSError` if size/mtime drifted during processing). `ArchivePerceptualHashRepository.save`
adds a further consistency check not present in `page_hashing.py`: it
reloads the stored `archive_pages` inventory (page index + entry name) and
compares it against the freshly computed result before writing, raising
`OSError` on any mismatch:

```python
if expected != actual:
    raise OSError(
        "Stored page inventory does not match the current "
        f"archive for archive_id={archive_id}."
    )
```

Database writes are wrapped in `BEGIN IMMEDIATE`/`COMMIT`/`ROLLBACK`. No
application-level file lock is used. The module never writes to the archive,
and its before/after stat guard detects common source drift, but it does not
provide exclusive-read semantics.

---

## 5. `scripts/cbz_sanitizer.py`

Relevant functions: `_write_cbz_with_comicinfo`, `process_comicinfo`,
`_process_comicinfo_with_core`, `process_cbz_file`, `_patch_comicinfo_for_range`.

**Streaming vs. materialization.** Every `ComicInfo.xml`-only read
(`process_comicinfo`, `_process_comicinfo_with_core`, the prefetch block in
`process_cbz_file`, `_patch_comicinfo_for_range`) uses a single
`zf.read(real_name)` call — small-entry materialization, low risk. The
archive-rewrite path is different and higher-risk: `_write_cbz_with_comicinfo`
materializes **every** entry in the archive before writing anything back:

```python
zip_entries: list[tuple] = []
with zipfile.ZipFile(cbz_path, "r") as zin:
    for item in zin.infolist():
        zip_entries.append((item, zin.read(item.filename)))
```

**Disk extraction.** None — no `.extract()`/`.extractall()` calls exist in
this file. The rewrite path (`_write_cbz_with_comicinfo`) writes a new zip
via `writestr`, using the original `ZipInfo` objects' `item.filename` values
unchanged. Because this is a zip-to-zip copy (never a filesystem write keyed
by entry name), there is no direct filesystem zip-slip triggered by this
file, but entry names are also never sanitized before being carried into the
output archive (see ZIP-slip below).

**Size limits.** None for zip-bomb/resource-safety purposes. The only size
checks present are a zero-byte skip heuristic (`process_cbz_file`,
`_process_series_dir`) and whole-file size comparisons used purely to pick a
winner between duplicate candidates — neither is a resource cap.

**Entry-count / decoded-pixel limits.** No cap on entry count anywhere. No
PIL/Pillow usage exists in this file at all (confirmed by search) — it never
decodes image pixel data, only manipulates `ComicInfo.xml` text and raw
entry bytes/filenames, so no pixel-bomb exposure exists in this file
specifically.

**ZIP-slip / path validation.** `_write_cbz_with_comicinfo` passes the
original archive's `item.filename` straight into `zout.writestr(item, data,
...)` with no `".."` check, no `os.path.normpath`, and no path-containment
validation. This file itself never extracts to a real filesystem path, so it
does not itself trigger path traversal — but a maliciously-named entry in
the source archive would be silently carried forward, unexamined, into the
"sanitized" output, and inherited by any downstream tool that later performs
a naive extraction.

**Memory behavior for large pages/omnibus archives.** `_write_cbz_with_comicinfo`
is invoked on essentially every processed CBZ where the ComicInfo rule is
active — it is the normal code path, not an edge case — so for an
omnibus-sized archive, the full set of decompressed page images is held in
the `zip_entries` list simultaneously for the entire rewrite duration, purely
to change one small XML entry.

**Error classification and retry.** No retry loop exists in
`_process_comicinfo_with_core`, the `process_cbz_file` prefetch block, or
`_patch_comicinfo_for_range` — each catches `(zipfile.BadZipFile, OSError)`
(or just passes silently, in the prefetch case) and gives up immediately.
Two functions do retry, but at different, undocumented-as-different
intervals for what is conceptually the same "file locked" condition:

- `_write_cbz_with_comicinfo`: `except OSError as e:` → retries **5 attempts,
  0.5s sleep**, logging `"File locked (attempt {n}/5), retrying in 0.5s..."`.
- `process_comicinfo`: `except OSError:` → retries **5 attempts, 5s sleep**,
  logging `"File locked reading zip (attempt {n}/5), retrying in 5s..."`.

`zipfile.BadZipFile` is treated as permanent (no retry) in both.

**Mutation/replacement safety, especially across SMB.** `_write_cbz_with_comicinfo`
replaces the original file via a three-step sequence, not a single atomic
replace:

```python
tmp_path = cbz_path.with_suffix(".tmp.cbz")
...
bak_path = cbz_path.with_suffix(".bak.cbz")
cbz_path.rename(bak_path)
tmp_path.rename(cbz_path)
bak_path.unlink(missing_ok=True)
```

An individual same-volume rename is commonly atomic on local NTFS, but the
three-step sequence as a whole is not; there is a window between the first
and second rename where no file exists at the original path. This audit did
not independently establish the relevant SMB server/client guarantees. No
file locking of any kind is used. There is no verification that the source
file's size/mtime is unchanged immediately before the rename versus what was
originally read at the top of the function — a concurrent writer on the same
share during the read-and-rebuild window would be silently overwritten with
no detection. No explicit recovery step exists if the rename sequence fails
partway (e.g. after the original has been renamed to `.bak.cbz` but before
`tmp_path` is renamed into place).

---

## 6. `scripts/cbz_library_maintenance.py`

Relevant functions: `read_comicinfo`, `write_comicinfo`, `pack_image_folder`,
`archive_clean_worker`, `detect_and_fix_compilations`,
`patch_comicinfo_range`, `run_repair_names`.

**Streaming vs. materialization.** `read_comicinfo` materializes one small
entry (`zf.read(real)`), same low-risk pattern as `cbz_sanitizer.py`'s
ComicInfo-only reads. `write_comicinfo` has the same whole-archive
materialize-then-rewrite pattern as `cbz_sanitizer.py`'s
`_write_cbz_with_comicinfo`:

```python
entries: list[tuple[zipfile.ZipInfo, bytes]] = []
with zipfile.ZipFile(cbz_path, "r") as zin:
    for info in zin.infolist():
        entries.append((info, zin.read(info.filename)))
```

`pack_image_folder` writes a new zip from loose files already on disk
(`zf.write(item, arcname=item.name)`) — this is the reverse direction
(filesystem → zip) and does not read any compressed archive members at all,
so it carries none of the read-side risk discussed here.

**Disk extraction.** None anywhere in the file (confirmed by search) — no
`.extract()`/`.extractall()` calls exist.

**Size limits.** None for archive/member content. The only numeric limit in
the file is an unrelated 5 MiB log-rotation cap
(`RotatingFileHandler(..., maxBytes=5 * 1024 * 1024, ...)`). Whole-file size
comparisons exist in `pack_image_folder` and `larger_file_wins` but are
collision-resolution heuristics ("keep the bigger file"), not resource
safety.

**Entry-count / decoded-pixel limits.** No cap on entry count in
`write_comicinfo` or `read_comicinfo`. No PIL/Pillow usage anywhere in this
file (confirmed by search) — no pixel-decode exposure exists here.

**ZIP-slip / path validation.** No extraction occurs anywhere in this file,
so there is no active zip-slip vector. `write_comicinfo` round-trips the
original archive's own `ZipInfo.filename` values, unsanitized, into the
rewritten archive — the same latent carry-through observation as
`cbz_sanitizer.py`. `pack_image_folder`'s `arcname` values come from trusted
local `Path.name` basenames (files already on disk in a known folder), not
from any archive entry, so that specific path is clean.

**Memory behavior for large pages/omnibus archives.** `write_comicinfo` is
the shared implementation behind every ComicInfo update, every
compilation-range patch, and every mojibake-title repair in this tool
(`metadata_worker`, `patch_comicinfo_range`, and `run_repair_names` all funnel
through it) — meaning the whole-archive-materialize pattern is a frequently
and repeatedly triggered code path across multiple distinct maintenance
operations, not a rare edge case. Despite "compilation"/"merge" terminology
elsewhere in the file, no function here actually combines two archives'
page-level content into a third archive: `detect_and_fix_compilations` and
related functions only detect and rename files whose chapter number looks
like a concatenated range, and patch the `<Number>` tag via
`patch_comicinfo_range` → `write_comicinfo`. All "merge" operations
(`merge_dir_contents`, `merge_series_dir`, `merge_chapter_folders`) move whole
`.cbz` files between folders with `shutil.move`, never touching zip-entry
content — true archive-content merging is implemented in
`cbz_compilation_resolver.py` (out of scope; see Deferred work).

**Error classification and retry.** Broad but inconsistent-with-`cbz_sanitizer.py`
exception handling: `read_comicinfo` catches `(zipfile.BadZipFile, OSError)`;
`write_comicinfo` and `pack_image_folder` both catch generic `Exception`;
various worker/plan functions catch `OSError`, `(OSError, shutil.Error)`, or
generic `Exception` depending on the call site. **No retry-with-sleep logic
exists anywhere in this file** (confirmed by search for `sleep(`) — every
operation is attempted exactly once; failures are logged and counted into
`stats.errors`, never retried. `archive_clean_worker` explicitly documents
its broad `OSError` catch as covering "a transient network/filesystem error
... (e.g. an SMB share blip, WinError 59)" so a single bad folder doesn't
abort the whole run — but the failed operation itself is still not retried,
only tolerated at the batch level.

**Mutation/replacement safety, especially across SMB.** Two rewrite
mechanisms, both multi-step and non-atomic-as-a-whole, neither with file
locking or a pre-rename staleness check:

`write_comicinfo`:
```python
tmp_path = cbz_path.with_suffix(".tmp.cbz")
bak_path = cbz_path.with_suffix(".bak.cbz")
...
bak_path.unlink(missing_ok=True)
cbz_path.rename(bak_path)
tmp_path.rename(cbz_path)
bak_path.unlink(missing_ok=True)
```
The same three-step bak/tmp/original dance as `cbz_sanitizer.py`'s
`_write_cbz_with_comicinfo` — a crash between the two renames leaves no file
at the original name until manual recovery.

`pack_image_folder`:
```python
if cbz_path.exists():
    if tmp_path.stat().st_size > cbz_path.stat().st_size:
        cbz_path.unlink()
        tmp_path.rename(cbz_path)
```
A delete-then-rename two-step, with the same brief window where no file
exists at `cbz_path`.

Neither function verifies the target file's size/mtime immediately before
its final rename against what was read at the start of the operation — the
same gap flagged in `cbz_sanitizer.py`, and the same gap that
`page_hashing.py`/`perceptual_hashing.py` (Sections 3–4) already close for
the read-only hashing path via their before/after `stat()` comparison. All
other "replacement" operations in this file (`larger_file_wins`,
`merge_dir_contents`, `move_possible_same_series_to_check`,
`find_uncensored_pairs`, `execute_plan`'s `movedir`) are plain
`shutil.move()`/`Path.rename()` calls with no temp staging, no locking, and
no atomicity guarantee beyond whatever the underlying filesystem driver
provides; `larger_file_wins`'s delete-then-move is likewise two non-atomic
steps.

---

## Prioritized Findings

### 1. Confirmed safeguards already present

- `inspection.py` enforces a double-checked 1 MiB acceptance limit on
  `ComicInfo.xml` (`entry.file_size` pre-check and `len(payload)`
  post-check), plus a DTD/`ENTITY` substring rejection before
  `ElementTree.fromstring` (XXE mitigation). The post-check rejects
  inconsistent oversized content but happens after allocation, as noted in
  Section 1. This remains the most defensively written component in the
  audit.
- None of the six audited components ever call `.extract()`/`.extractall()`
  or write archive-member bytes to an arbitrary filesystem path derived from
  `entry.filename` — the "read into memory or copy zip-to-zip, never extract
  loose to disk" design eliminates zip-slip-via-extraction as a live vector
  across the entire audited surface.
- `page_hashing.py` streams page content via `archive.open(entry)` in fixed
  1 MiB chunks — the only component in the audit that bounds its
  application-level page payload buffer independently of the page's actual
  size.
- `page_hashing.py` and `perceptual_hashing.py` both snapshot the source
  archive's size/mtime before and after processing and abort with an
  `OSError` if either changed — a working concurrent-modification detector
  for the read/hash path on SMB shares.
- `perceptual_hashing.py`'s repository save step additionally re-validates
  the stored page inventory (index + entry name) against the freshly
  computed result before writing, catching drift between exact-hash time and
  perceptual-hash time.
- A consistent exception-categorization ladder
  (`FileNotFoundError`→`filesystem_not_found`,
  `PermissionError`→`filesystem_permission`, `OSError`→`filesystem_io`) is
  applied uniformly across `handlers.py`, `page_hashing.py`, and
  `perceptual_hashing.py`, giving the job queue a stable vocabulary for
  retry/permanent-failure decisions.
- Both `scripts/` files use a temp-file-plus-rename pattern for every
  archive rewrite rather than modifying a CBZ in place, avoiding a
  half-written zip at the original filename if the write step itself fails.

### 2. Confirmed risks in current code

- **Whole-archive materialization on every metadata write.**
  `cbz_sanitizer.py`'s `_write_cbz_with_comicinfo` and
  `cbz_library_maintenance.py`'s `write_comicinfo` both decompress and hold
  every page of the archive in memory simultaneously to rewrite one small
  XML entry. This is the default code path for any ComicInfo update,
  compilation-range patch, or title repair — not an edge case — and has no
  size guard.
- **No pre-read size check in `perceptual_hashing.py`.**
  `calculate_perceptual_hashes` calls `archive.read(entry)` for each page
  with no check on `entry.file_size`/`compress_size` beforehand, unlike
  `inspection.py`'s double-checked cap or `page_hashing.py`'s bounded
  streaming.
- **Implicit, unpinned pixel thresholds.** No `Image.MAX_IMAGE_PIXELS`
  override exists anywhere in the codebase. Pillow uses that value as a
  warning threshold and raises `DecompressionBombError` only above twice
  that value, so `perceptual_hashing.py`'s warning/rejection behavior depends
  on whatever default ships with the installed Pillow version.
- **Non-atomic, multi-step file replacement on every rewrite path in both
  `scripts/` files.** `_write_cbz_with_comicinfo`, `write_comicinfo`
  (original→`.bak.cbz`, tmp→original, delete `.bak.cbz`), and
  `pack_image_folder` (delete-then-rename) each use multiple separate
  `Path.rename()`/`unlink()` calls rather than one atomic replace, with a
  window where no file exists at the target path if interrupted.
- **No pre-rename staleness check in any `scripts/` rewrite function.**
  Unlike `page_hashing.py`/`perceptual_hashing.py`'s before/after `stat()`
  guard, none of `_write_cbz_with_comicinfo`, `write_comicinfo`, or
  `pack_image_folder` re-verify the target's size/mtime immediately before
  the final rename against what was originally read — a concurrent SMB
  writer during the read-rebuild window would be silently overwritten with
  no detection.
- **No file locking anywhere** across either `scripts/` file for archive
  rewrites; the only protection against a locked/in-use file is
  retry-after-`OSError` where retry exists at all.
- **Unsanitized entry-name pass-through.** Both `scripts/` files copy
  original `ZipInfo.filename` values unchanged into rewritten archives, with
  no `".."`/traversal validation. Not an active vulnerability in these two
  files (neither extracts to disk), but a maliciously-crafted entry name
  would silently survive "sanitization" and be reintroduced into the corpus.
- **Inconsistent retry behavior for the identical failure condition.**
  `cbz_sanitizer.py` retries a "file locked" `OSError` in 2 of 5 read/write
  call sites, at two different intervals (0.5s and 5s, both ×5 attempts),
  and has no retry in the other 3. `cbz_library_maintenance.py` has zero
  retry logic anywhere in the file. The same transient SMB condition
  produces different outcomes depending on which script and which function
  happens to encounter it.

### 3. Small, low-risk improvements

- Add an explicit, documented pixel policy in `perceptual_hashing.py`
  instead of relying on Pillow's implicit, version-dependent default:
  configure `Image.MAX_IMAGE_PIXELS`, document that the hard-error threshold
  is twice that value, and decide explicitly whether warnings below the hard
  threshold should remain non-terminal.
- Add a pre-read `entry.file_size`/`compress_size` sanity check in
  `calculate_perceptual_hashes` before `archive.read(entry)`, mirroring
  `inspection.py`'s existing double-check pattern, to bound worst-case
  per-page memory before decoding.
- Normalize the retry interval/attempt-count for "file locked" conditions
  across `cbz_sanitizer.py`'s own functions (currently 0.5s vs. 5s for the
  same failure class) so behavior is predictable regardless of which code
  path a given CBZ happens to go through.
- Add a size/mtime staleness re-check immediately before the final rename in
  `_write_cbz_with_comicinfo`, `write_comicinfo`, and `pack_image_folder` —
  a direct reuse of the before/after `stat()` pattern already proven in
  `page_hashing.py`/`perceptual_hashing.py`, not new design.
- Add an in-code comment noting that entry names are passed through
  unsanitized in the two `scripts/` rewrite paths, so a future contributor
  adding an extraction feature doesn't assume names are already safe.

### 4. Changes requiring benchmarks or algorithm versioning

- Moving `perceptual_hashing.py` from `archive.read(entry)` to a chunked
  `archive.open(entry)` stream needs benchmarking against the existing
  `PerceptualHashProfile`/`phase_timings` instrumentation already built into
  this module, since it changes the exact read-phase cost that profiling
  was built to measure.
- Changing `_write_cbz_with_comicinfo`/`write_comicinfo` to stream entries
  through a temp zip one at a time (instead of materializing the full
  entries list) is a real behavior change to the rewrite path used by every
  metadata update in both `scripts/` files; it should be benchmarked for
  wall-clock impact on large omnibus archives and needs test coverage for
  partial-failure/rollback semantics under the new approach before adoption.
- Any change to `perceptual_hash()`'s pure-Python nested-loop DCT computation
  (e.g. vectorizing) is an algorithm-level change that could alter the
  existing dhash/phash digest determinism relied on by
  `PHASH_ALGORITHM_VERSION = "1"`; it requires an explicit new algorithm
  version rather than being treated as a drop-in optimization, and must not
  be described as a speed improvement without a benchmark.
- Collapsing the current multi-step temp/backup/rename sequences (across all
  four rewrite functions in both `scripts/` files) into a single atomic
  replace is a correctness-sensitive change to the mutation path itself and
  should be validated specifically against SMB rename semantics (not just
  local NTFS) before rollout, given that SMB atomicity guarantees can differ
  from local filesystems.

### 5. Deferred work

- `scripts/cbz_compilation_resolver.py` was not part of this audit's file
  list but is exactly where true archive-content merging happens (combining
  multiple CBZs' page entries into one) — the pattern most likely to
  concentrate memory and zip-slip risk — and should get its own I/O/
  resource-limit pass.
- `comic_automation/jobs/worker.py`'s retry/backoff policy (how
  `CategorizedJobError` vs. `PermanentJobError` actually translate into
  requeue timing and max-attempts) determines the real-world blast radius of
  every "no retry"/"inconsistent retry" finding above, but it lives outside
  the six audited files and was not reviewed here.
- No dedicated review was done of encrypted-archive handling beyond
  `inspection.py`'s `encrypted` detection flag (`flag_bits & 0x1`) — whether
  any downstream component ever attempts to read pages from an encrypted CBZ,
  and what happens if so, was not traced end-to-end.
- SMB-specific behavior claims in this audit (rename atomicity, sharing-
  violation exception types) are based on the exception-handling code
  already written for that environment, not on independently reproduced SMB
  fault-injection testing; a dedicated SMB-fault test harness would be
  needed to confirm these behaviors empirically rather than by code
  inspection alone.
