# Read-only canonical naming plan

Implements #112 step 8's plan generation. This command emits the
`canonical-naming-plan-v1` document consumed by #128, without importing
that PR's executor or waiting for it to merge. It has no apply mode.

```powershell
python -m scripts.cbz_canonical_plan --library-root C:\test-library `
  --database C:\test-catalog.db --quarantine-date 2026-10-24
```

The JSON goes to stdout. Save it outside the library if needed; the
command does not create an output file. Exit 0 means a complete plan with
no review holds, 1 means a complete plan with review holds, and 2 means
no trustworthy plan was produced (stderr explains why). A plan with review
holds can contain other independently actionable units; the holds are
carried in the digested document and never executed.

No library, database row, schema, lock, owner file, journal or quarantine
directory is written. SQLite may create/touch its WAL/SHM sidecars when
opening read-only; this is the established DAL read contract. An archive
above `--max-held-bytes` (default 512 MiB) is bound through the existing
step-5 private-copy mechanism. `--temp-dir` must resolve outside the
library; copies are cleaned up best-effort by that mechanism.
`--max-copy-bytes` can cap each private copy. A copy failure or budget
refusal holds that archive. This is a full byte/page inventory, potentially
expensive over SMB; it does not use cached hashes as proof.

## Evidence and scope

- Walk one root recursively, excluding `.stfolder`, `.stversions`, and
  canonical naming state/quarantine trees. Never follow child symlinks or
  reparse points. Exclusions are named in provenance. Unreadable walks
  abort; incomplete coverage is not reported as a complete plan.
- Match each archive to its exact current catalog path. An uncatalogued
  file, orphan row, ambiguous path, or archive with multiple current
  locations is held. Drive/UNC aliases are not guessed. Use a root spelling
  consistent with the catalog; the database itself is bound by real path,
  device and file id using #117's `file_identity`.
- Derive full-file SHA256, pages, non-image members and ComicInfo from one
  owned byte source through step 5. Feed those facts to step 4 and its
  existing precedence/conflict rules, then use the pure step-3 formatter.
  The planner owns no tag vocabulary. The tag-strip PR owns that policy;
  until integration, master's marker readings/formatter apply and unknown
  tags go to review. A directory's name is never promoted to resolved
  series authority. Human decision manifests are not yet connected.
- An ordinary rename requires resolved series/number and every offered
  optional field. An unresolved year/version is not silently discarded.
  The exception is a bare `vN` already explicitly classified as version:
  step 4 retains its filename-grade volume candidate; that candidate is
  not a second unresolved volume. Genuine volume evidence still governs.
- Only established, byte-proved strict-prefix families with a unique
  keeper and its own resolved version label become family units. Failed
  families are held as units, never degraded into independent renames.
  Step 4 also emits unmarked candidate groups/singletons; those remain
  ordinary rename candidates. Volume-classified bare `vN` alone does not
  make a version family.
- Decensor markers in the root name, relative paths or resolved series
  are held until a stable pairing adapter exists. A root inside a directory
  named `ai-decensor-originals` is refused before archive derivation.
  No F: originals are scanned, paired, renamed or superseded.
  The emitted pairing lists are empty; this slice does not invent a
  pairing registry or silently update one.
- A keeper rename is part of its family; each dependant gets a proposed
  dated quarantine path containing the family id and archive id. No
  quarantine move happens. `--accept-comicinfo-drop` defaults off; even
  when enabled, step 5's reviewed coverage checks still govern.
- Destination checks use NFC/casefold collision keys across all proposals
  and observed occupants, including directories and uncatalogued files.
  An identical foreign file still blocks. A collision holds every affected
  unit, including the whole family. Case-only ordinary renames are left
  for the executor's two-step protocol. Existing reparse components in
  proposed destinations, including excluded quarantine trees, block.

## Consistency and approval

One outer `read_consistent_snapshot` spans catalog reads, the existing
page-evidence reader, every byte derivation and the final root inventory.
Its `mode=ro`, `query_only`, quick-check and outside-transaction
`data_version` checks refuse any concurrent database commit. Nested page
reads cannot silently combine database generations. The database file
identity is checked before reads and after the complete window; root
device/file id/real path are also checked across the run.

The sorted before/after inventory binds paths, types, sizes, mtimes and
file ids for all observed entries, plus named exclusions. Any difference
refuses the entire plan. This is **not an atomic filesystem snapshot**:
an unobserved change/revert can occur. Every action carries the strong
identity of its evaluated bytes; apply must revalidate it after lock waits
and at its existing guarded boundaries. This planner neither attests
quiescence nor activates an executor. It does not measure SMB locking or
rename semantics; those remain rollout prerequisites.

The SHA256 covers canonical UTF-8 JSON (sorted keys, compact separators,
unescaped Unicode) of every field except `plan_digest`. Counts match the
executor's five counts. Additional provenance, review and unchanged rows
are sealed too. Fixing a review item or regenerating against changed
inventory produces a new document and needs a new digest approval.

David #2646 authorizes a later read-only live plan. Claude owns that Tower
run after #128's journal fixes are reviewed/merged and the tag-strip PR
lands. Codex's implementation and tests use temporary fixtures only.
Batch apply remains gated on David's approval of the exact plan digest,
the verified backup and the executor's independent operational checks.
