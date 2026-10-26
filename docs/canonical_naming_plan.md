# Read-only canonical naming plan

Implements #112 step 8's plan generation. This command emits the
`canonical-naming-plan-v1` document consumed by #128, without importing
that PR's executor. It has no apply mode.

```powershell
python -m scripts.cbz_canonical_plan --library-root C:\test-library `
  --database C:\test-catalog.db --quarantine-date 2026-10-24 `
  --output C:\reports\canonical-plan.json
```

`--output` exclusively creates a new UTF-8 file outside the library and
prints a small receipt (path, digest, counts). It refuses an occupied output,
including one appearing during planning; it never overwrites. The parent
directory must already exist. This avoids shell-dependent redirect encoding
(notably Windows PowerShell 5.1's UTF-16 output). Without `--output`, the
JSON goes to stdout. Exit 0 means a complete plan with
no review holds, 1 means a complete plan with review holds, and 2 means
no trustworthy plan was produced (stderr explains why). A plan with review
holds can contain other independently actionable units; the holds are
carried in the digested document and never executed.

No library, database row, schema, lock, owner file, journal or quarantine
directory is written. Both the supplied database access path and its resolved
target must be outside the library. Before the first SQLite open, retain the
lexical/resolved path checks and compare the library root's `(st_dev, st_ino)`
against every ancestor directory of both database paths. This catches local
drive, UNC and mapped-drive aliases even when `resolve()` preserves different
strings. An identity query failure, non-directory ancestor or zero file id
refuses the run; an uncertain identity is not evidence of separation.

An in-library file symlink or outward directory junction is also refused,
even when its target is external: resolving the target must not erase the
supplied access path. A file symlink outside the library targeting an internal
catalog refuses too. Actual file-symlink tests are host-dependent and must
report missing Windows symlink privilege as a skip, not a successful witness.
Directory identity checks observe containment before opening the catalog;
they do not provide an atomic fence against concurrent path retargeting.
SQLite may create/touch WAL/SHM sidecars beside that external catalog
when opening read-only; this is the established DAL read contract. An archive
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
  `ai-decensor-originals` is excluded before descent at **every depth**;
  selecting it (or a descendant) as the root refuses the whole run.
- Match each archive to its exact current catalog path. An uncatalogued
  file, orphan row, ambiguous path, or archive with multiple current
  locations is held. Drive/UNC aliases are not guessed. Use a root spelling
  identical to the catalog; a case-only spelling mismatch is held because
  the executor compares its row preimage exactly. The database is bound by real path,
  device and file id using #117's `file_identity`.
- Derive full-file SHA256, pages, non-image members and ComicInfo from one
  owned byte source through step 5. Feed those facts to step 4 and its
  existing precedence/conflict rules, then use the pure step-3 formatter.
  The planner owns no tag vocabulary. The tag-strip PR owns that policy;
  the shared display-token helper controls edition visibility (including
  dropping English) while resolver family distinctions remain intact.
  Unknown tags go to review. A directory's name is never promoted to resolved
  series authority. Human decision manifests are not yet connected.
- An ordinary rename requires resolved series/number and every offered
  optional field. An unresolved year/version is not silently discarded.
  The exception is a bare `vN` already explicitly classified as version:
  step 4 retains its filename-grade volume candidate; that candidate is
  not a second unresolved volume. Genuine volume evidence still governs.
  Symmetrically, a bare `vN` conclusively classified as volume does not
  retain a phantom unresolved version. Explicit bracketed version markers
  and genuine unresolved version evidence still block.
- Before emitting actions, re-resolve the proposed names together using
  the same ComicInfo and byte-bound page facts. A proposed name that fails
  resolution or formats differently is held as a non-fixpoint. The second
  resolution can only reject an original proposal; its filename candidates
  supply no new authority. A member's failure holds its entire family,
  including quarantine. This catches title text such as `Series Vol. 2`
  gaining unresolved optional-field candidates after formatting.
- **Lone canonical version no-op** (Claude's review disposition, hive
  #2708): when the formatter's output equals the existing filename
  byte-for-byte and the only missing classification is its `[vN]`, report
  unchanged with `unverified_version_label`. Certify no version, family or
  supersession. The diagnostic count is separate from the executor's five
  action counts, under `diagnostics.unverified_version_labels`, and appears
  in the saved-output receipt. It causes no review exit by itself. "Lone"
  means alone in the decided identity candidate group (series key plus
  primary chapter/issue number, or volume when no number was offered), not
  alone in the containing folder. Other chapters or series do not block the
  no-op. A co-member in that group,
  any proposed name change, protected identity, catalog problem or collision
  restores the normal evidence rules. Prior attempt-receipt certification
  can be a later adapter; a filename never becomes version authority here.
- Only established, byte-proved strict-prefix families with a unique
  keeper and its own resolved version label become family units. Failed
  families are held as units, never degraded into independent renames.
  Step 4 also emits unmarked candidate groups/singletons; those remain
  ordinary rename candidates. Volume-classified bare `vN` alone does not
  make a version family.
- Decensor markers in the root name, relative paths or resolved series
  (including underscore/hyphen delimiters, `uncen`, `uncensor` and `decensor`
  stems) are held until a stable pairing adapter exists. Byte-bound
  ComicInfo `Series`, `Title` and `Tags` are checked too: Camelia decensors
  in place, so an unchanged filename can carry only `Tags=uncensored,
  camelia:black_bars` or a title marker. Any Camelia method tag also holds.
  Malformed metadata cannot rule out protected identity and is held.
  No F: originals are scanned, paired, renamed or superseded.
  The emitted pairing lists are empty; this slice does not invent a
  pairing registry or silently update one.
  Source audit: the current watcher keeps `(archive, original)` pairs in
  memory, rekeys them across fallback renames and logs `archive_backup`
  results. `ai_decensor_backups.py` installs originals by series/name; these
  paths write no durable pairing table/manifest the planner could consume.
  Missing/removed markers cannot be reconstructed from that provenance;
  no unmarked archive is claimed proven unrelated to an original.
- A keeper rename is part of its family; each dependant gets a proposed
  dated quarantine path containing the family id and archive id. No
  quarantine move happens. `--accept-comicinfo-drop` defaults off; even
  when enabled, step 5's reviewed coverage checks still govern.
- Destination checks use NFC/casefold collision keys across all proposals
  and observed occupants, including directories and uncatalogued files.
  An identical foreign file still blocks. A collision holds every affected
  unit, including the whole family. Case-only ordinary renames are left
  for the executor's two-step protocol, using Windows path comparison even
  on POSIX hosts; casefold equivalence alone does not grant that exception.
  Existing reparse components in
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

## Media policy (issue #140)

Planner version 2 requires an established number style. Use
`--number-style auto|issue|chapter` (default auto) and a shared
`--number-style-registry <file>`, or set `CBZ_NUMBER_STYLE_REGISTRY` before
launch. The fallback `config/number-styles.json` is intentionally empty;
unknown or ambiguous roots refuse auto. Physical root identities handle UNC,
drive aliases and subfolder scans. An explicit style chooses only the number
syntax and does not bypass field, database, decensor, family or collision guards.

The registry SHA256, style, decision source, scope and matching root identity
are sealed in provenance. Western issues use `#003`, collections use
`v01 (TPB)`, Manga chapters use `ch. 3`, and Manga volumes omit TPB. A separate
collection field preserves independent edition variants. Unnumbered Full and
substantively different folder/ComicInfo titles stay held; normalized-equal
folder spelling wins display without becoming new series authority. The same
policy is used during the proposed-name resolution guard.

See `docs/media_aware_naming.md` for the complete policy, watcher behavior
change, configuration/rollout requirement and the conservative Manga-volume
fixpoint limitation. Existing plans must not be reused for this policy:
generate a fresh read-only plan and obtain approval of its exact digest.
