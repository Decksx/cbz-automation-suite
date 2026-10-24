# Komga rename measurement — 2026-10-24

Owner: Codex; independent reviewer: Claude. Implements design
[§5.6 / step 6](canonical_naming_and_version_supersession.md#56-other-interactions)
on base `3d68ccca42a92453828bff7a9da9e94c23086c08`.
All times below are **UTC-6**.

## Result and disposition

On this Komga **1.29.1** instance, plain, case-only and series-folder renames
created **new book IDs**, but retained the tested read position, completion
state, read date and locked title/summary. A folder rename also changed the
series ID. Quarantine out of the library, a scan while absent, then return
and another scan lost the tested progress and custom metadata with
`emptyTrashAfterScan=true`.

No progress-carrying implementation is warranted for the first rename slice:
the measured ordinary renames already carry state, and David accepted resets.
The later operator plan should disclose the quarantine/reset cost. It must not
assume Komga IDs survive, or use an old ID as durable archive identity. This is
a measurement, not permission to run the live rename plan or change settings.

## Isolation and configuration

- Tower container image `gotson/komga:1.29.1`; `/actuator/info` independently
  reported version `1.29.1`, Git commit `1d29728`.
- Existing mount: `/mnt/user/media/comics` -> `/comics`. Live library roots
  were `/comics/Comix`, `/comics/Manga`, `/comics/Graphic Novels`.
- Test run host root: `/mnt/user/media/comics/_cbz_step6_20261024T212208Z`.
  Its `library` child was the new Komga library, and `quarantine` was outside
  that library. Both were disjoint from every live root, in host and container
  coordinates. No mount or live-library configuration was changed.
- Configured Syncthing folders covered only Manga/Comix, were paused on both
  machines, and the Tower Syncthing container was stopped (`Created`). The
  import watcher pointed at `C:\Temp\Mega\Mega Uploads\book2`. Inspected
  Tower User Scripts contained no comics sync/backup job.
- Four distinct source CBZs were copied, **25,291,859 bytes** total. Original
  paths were never renamed or moved. SHA-256, size, mtime, device and inode
  were checked before/after; every copy was checked against its source hash.
- Valid test-library ID: `0S12TRJRBPZKQ`. File/page hashing and automatic trash
  emptying were **on**, matching all three live libraries. Test periodic and
  startup scans were **off**; only CBZ scanning was on. Extension repair,
  archive conversion, collection/read-list import and local artwork import
  were off. ComicInfo book/series import and dimension analysis were on.
- Existing Komf-configured automation authentication was reused in memory,
  approved by David in chat (Claude's hive **#2530**). Credentials were never
  printed, saved, changed or included in the report/evidence.

## Method and observations

Initial creation scans automatically. My first setup also requested a scan,
which produced duplicate test URL records. That registration
(`0S12TEMJFPW36`) was removed at **15:29:53**, before any progress/metadata
seeding or measurement. Re-creation used only its automatic initial scan and
settled to four unique books. Its baseline was seeded at **15:30:26–27**.

Each target received a locked `STEP6 <label>` title and summary. Rename and
folder targets were at page **2**, incomplete; the one-page quarantine target
was completed. The untouched 23-page control was completed. This measures
the automation account's progress, not other users' progress.

For each operation: save the preceding snapshot; move only the owned test
copy; observe a **6-second quiet interval**; request one deep scan of the
test library only; poll until the expected unique paths are READY, have file
hashes and remain stable for at least 8 seconds (at least 12 seconds total).
All quiet snapshots retained the pre-move IDs/paths. No live-library scan,
book/series delete or explicit empty-trash endpoint was called.

| Operation and settled time | Book ID before -> after | Series ID | Read state and seeded metadata |
|---|---|---|---|
| `Plain.cbz` -> `Renamed.cbz`, **15:31:23** | `0S12TRJTZPMT1` -> `0S12V1Z9VPKMM` | Unchanged, `0S12TRJTVPVDX` | Page 2, incomplete, original read date, locked title/summary retained |
| `Renamed.cbz` -> `renamed.cbz` via an unscanned temporary hop, **15:31:56** | `0S12V1Z9VPKMM` -> `0S12V5ZAVPT9Z` | Unchanged | Same state retained |
| `FolderTarget` -> `FolderTargetRenamed`, **15:32:35** | `0S12TRJV3PK4B` -> `0S12VAR7FPG11` | `0S12TRJTZPMT2` -> `0S12VAR7BPG86` | Same page-2 state and seeded metadata retained |
| Quarantine out, scan, **15:33:00** | `0S12TRJV7PM8B` -> absent from library results | Removed with its only book | No active book at that path |
| Return identical quarantine bytes, scan, **15:33:31** | New `0S12VHK6QPXRS` | New `0S12VHK6QPXRR` | `readProgress=null`; seeded title/summary and their locks lost; embedded metadata imported |
| Untouched control throughout | `0S12TRJTVPVDW` throughout | `0S12TRJTQPGNT` throughout | Full read-progress object, title/summary and locks unchanged |

For the renamed books, the progress row's `created`/`lastModified` changed
when state was carried to the new ID; page/completed/readDate stayed equal.
Metadata timestamps can change on refresh, including for the control. The
claim is preservation of the tested state, not byte-identical API objects.
Komga's file hash and our SHA-256 checks were unchanged across each move.

## Limits and next action

This is a small controlled measurement of one version, one account, identical
CBZ bytes, hashing enabled and automatic trash emptying enabled. It does not
measure changed archive contents, cross-library moves, other trash/hash
settings, multi-user progress, or client caches/bookmarks. **Series-level
locked titles/summaries, Komf-sourced fields, posters/thumbnails, collections
and read-list membership were not seeded or tracked: not measured.** A new
series ID does not establish whether those values survived. The metadata
claims in the table concern the books' seeded title/summary only; a broader
metadata-carrying decision needs separate evidence before live folder renames.
The files were renamed on Tower via SSH, not through Windows SMB. No scan
occurred between the two case-only hops. No event-driven watcher configuration
was found in exposed settings; a scan during a hop or an externally triggered
watcher case was not measured. Do not generalize the quiet-interval result to
those cases. The live libraries use six-hour scheduled scans.

The official [scan guide](https://komga.org/docs/guides/scan-analysis-refresh/)
describes scans after renames/moves, and the
[library guide](https://komga.org/docs/guides/libraries/) documents hashing,
scan cadence and library removal. These explain the controls; the table is
the observed result. Continue step 7 against temporary fixtures. Keep scan
coordination and changed Komga IDs visible in the later live-plan review.

## Cleanup and evidence

At **15:35:18**, the valid test registration was removed through the library
registration endpoint. Only this run's manifest-verified copied files were
unlinked, then its empty directories removed. The run root is absent. Both
test registrations are gone; the final library list/settings exactly match
the original three. All four original SHA-256/size/mtime/device/inode values
remain unchanged. No live progress or metadata mutation endpoint was used.

Local evidence is retained at
`C:\Users\david\Documents\Codex\control\komga-step6-20261024T212208Z`:
phase snapshots, quiet snapshots, request/move audit, source/copy manifest
and cleanup receipts. Sanitized `result.json` SHA-256:
`8ba05b0f3469cbb291918880182de0638b94a0e9fbe81fc459809610795cac38`.
Request/move audit `events.jsonl` SHA-256:
`189e3ed8e660975cad36198a7c1c941f161055ddbbed6eba8f678f781d147345`.
The before/after library JSON files both hash to
`f6920f33c5a88dfa93803d61acf4219e589165330e8e42188d255b60f4e3fcc0`.
Cleanup receipt SHA-256:
`b293b6b2636e4a5b9f8de00ca711b152764a19f4e50befdaea9f79abf100b284`.

No runtime code changed for this report; local validation was the measured
matrix, source/copy/cleanup checks and documentation diff-check.
