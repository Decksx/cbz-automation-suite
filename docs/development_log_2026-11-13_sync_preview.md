# #108 logical filesystem preview, 2026-11-13

## Review round 1: directory spelling conflicts

Claude's review at initial exact source
`7595ef625d524bd4432b9a29af828a61c48804c7` found a P2: comparing whole file
paths missed `Series/new.cbz` versus `series/old.cbz`. It also requested a
direct self-group-filter witness and explicit directory/type-change limits.
[Review](https://github.com/Decksx/cbz-automation-suite/pull/194#issuecomment-6868565765).

The fix collects every parent prefix, grouped by folder ID and NFC/casefolded
path. Multiple spellings hold all files under that prefix, including otherwise
matching keeps, with the conflicting directory named in the reason. Component
boundaries leave `SeriesExtra` unaffected by a `Series` conflict. Prefix depth,
Unicode equivalence, within-side spelling variants and folder-ID separation
are covered. The same-path-only keep now has an explicit no-elsewhere-group
assertion. Report limits and protocol text list unclassified empty directories
and file/directory transitions whose operation ordering is not modeled.

Seven cases were added; the existing cross-folder-ID case now includes
differently cased parents. Before the implementation fix, **5 failed / 122
passed in 5.05 s**; all five failures were P2 assertions. With the fix,
**127 passed in 4.31 s**. Code/tests are final at
`8a655a5b5c8cbd33c0d540d71be84e762fc4b413`. New commits each change one file.

The round-1 mutation control passes all **48 cases**. All **40 groups** are
caught, with **94 failing executions including overlap**. The new proof
groups have the following results; the prior 30 groups are rerun as well:

| Removed or weakened behavior | Failed cases |
| --- | ---: |
| Directory-prefix protection | 5 |
| Case folding of directory keys | 4 |
| NFC directory normalization | 1 |
| Folder-ID namespace | 1 |
| Nested-prefix traversal | 1 |
| Multiple-spellings threshold | 4 |
| Target-only subtree holds | 4 |
| Tower subtree holds | 5 |
| Corresponding-target hold | 1 |
| Same-path-only group filter | 1 |

Source/CLI/test SHA-256 before and after this proof:

- `sync_preview.py`: `464e26c28a3d150820b7a239adaddde47a97554db25df0d399c731c8b343a23f`
- `sync_observe_cli.py`: `08ca71fa1ed1147739eb540bd507b7d4fc29788f797d63d16a8f8c5d59dce0c9`
- `test_sync_preview.py`: `fcab2c4522dfdc30c0b621e67754f7c7235f7deba3f02474b2d1e957edb5c267`

Round-1 full suite: **6,777 passed / 12 skipped / 0 failed in 458.63 s**,
`-n 4 --dist loadfile`, translation disabled. JUnit confirms **6,789 cases**,
0 errors and 0 failures: seven more than the initial #194 source and 48 more
than master 32006c6. Source, CLI and test hashes above remain unchanged after
the suite. Evidence is
kept separate from the initial proof: `sync108-preview-r1-suite.txt` / `.xml`,
`sync108-preview-r1-mutations.json`, `sync108-preview-r1-mutation-*.txt`,
`sync108_preview_r1_mutation.py` and `run_sync108_preview_r1_mutations.py` in
Codex tmp. All checks use disposable local fixtures, with no live inventory
or configuration operation.

## Initial delivery record

### Delivered scope

Based on merged master `32006c63b7a0c3b35afdcf39b420db488761bf8b`.
Code/tests are final at `5c5d0aff74bd6660c7ad1740e5973a98b4fc3b14`;
later commits document the same behavior. Commits each touch one file.

The new `sync_preview.build_preview` classifies content observations into
same-path keeps, new/replacement writes, obsolete-path removals and unresolved
records. Every observed Tower and target file gets its own logical count and
byte accounting. Replacements retain their old target and are not counted
again as removals. Size inequality proves a replacement without reads; equal
sizes require full-content evidence. Incomplete observations yield unresolved
files and never absence certainty. A purportedly complete artifact missing a
required shared-size hash is refused, even for relocated paths.

Global size/SHA-256 groups expose all Tower/target matches, including across
folder IDs, without expanding their Cartesian product or changing placement
counts. Cross-side case/NFC spelling conflicts are unresolved instead of
proposing write/removal actions on one Windows path. Mtime-only differences
are reported separately from byte equality.

The report embeds independent observation evidence, reconciles counts, and
seals all fields into a deterministic digest. It remains filesystem-only,
with `sync_ready=false`. The read-only CLI gains `preview`; there is no apply
or Syncthing control operation. This also adds the two #191 follow-ups:
per-pair unknown-field coverage and refusal of Windows root-relative paths.

The post-sync verifier and sanitized configuration/effective-ignore adapter
remain subsequent #108 deliveries. This preview neither closes that issue
nor authorizes live inventory, backup, configuration, sync or cutover.

### Validation and accounting

Before edits, the existing observation/CLI tests passed: **79 in 4.19 s**.
The isolated parent checkout collected **6,741 cases** (29.57 s). The first
collection invocation stayed in the feature directory and is not used as a
parent measurement; `sync108-preview-parent-collection.txt` is the correct
parent artifact. The final slice adds **41 cases** in `test_sync_preview.py`.
Final focused run: **120 passed in 4.45 s**.

The initial implementation's full run passed **6,768 / 12 skipped in 508.97 s**,
before the final evidence guard and test strengthening. An interim rerun was
stopped for that source change; no completed result is claimed for it. Only
that private pytest master and its four workers were stopped. All runs and
children stayed inside the inherited Job Object.

Final full suite on the final code/tests: **6,770 passed / 12 skipped / 0 failed
in 466.72 s**, `-n 4 --dist loadfile`, translation disabled. JUnit confirms
**6,782 cases**, 0 errors and 0 failures: 6,741 parent cases + 41 additions.
Source, CLI and test hashes below remain unchanged after the final suite.
A separate real-module CLI smoke returned exit 0 for a relocated-byte fixture,
with one new path, one obsolete path and one advisory group, `sync_ready=false`,
and every fixture name/byte unchanged.

### Mutation checks

The external plugin mutates functions in memory in their original module
dictionary. It never replaces candidate sources. A first collision-guard
removal survived because the malformed fixture also had inconsistent counts;
the fixture now preserves counts and independently exercises exact and
case-fold collisions. The artifact-tamper witness changes mtime, avoiding a
second hash-length check masking the digest guard. The evidence-copy witness
also checks that later input changes cannot alter the sealed report.

Invalid-manifest tests intercept observation before it starts, so even a
removed root-path guard never inventories an unapproved host path. The final
control passes all 41 new cases. All **30 mutation groups** fail tests, with
**56 failing executions including overlap** and no collection/harness errors.
Source, CLI and test bytes are identical before and after the final proof.

| Removed or weakened behavior | Failed cases |
| --- | ---: |
| Content-mode/producer contract | 2 |
| Observation digest | 1 |
| Collection schema | 1 |
| Root schema | 1 |
| Paired root bindings | 1 |
| Unique root bindings | 1 |
| File/path schema and binding | 6 |
| Full hash format | 1 |
| Certified read length | 2 |
| Unique portable paths | 2 |
| Observed file counts | 1 |
| Coverage flag | 1 |
| Reported gaps | 1 |
| Cross-side path spelling | 3 |
| Size inequality | 1 |
| Required candidate hashes | 2 |
| Full hash equality | 1 |
| New-path classification | 3 |
| Replacement accounting | 2 |
| Obsolete-path classification | 5 |
| Advisory match groups | 2 |
| Separate metadata differences | 1 |
| Logical byte accounting | 4 |
| No claimed sync readiness | 2 |
| Output digest | 1 |
| Independent evidence copy | 1 |
| CLI classification | 2 |
| CLI full-content mode | 2 |
| Per-pair unknown fields | 1 |
| Root-relative manifest refusal | 2 |

Working SHA-256 values:

- `sync_preview.py`: `31ea242250b2d34420c500c4c386c5cada290526b7c1812a3045cfa82d63d8d8`
- `sync_observe_cli.py`: `08ca71fa1ed1147739eb540bd507b7d4fc29788f797d63d16a8f8c5d59dce0c9`
- `test_sync_preview.py`: `1378e194de91a43aa5cc142c7d860ec70ab1b860cb1ebab2be34036d23651602`

Scratch evidence under `C:\Users\david\.codex\tmp`:

- `sync108-preview-focused-final.txt`
- `sync108-preview-parent-collection.txt`
- `sync108-preview-final-v2-suite.txt` and matching JUnit XML
- `sync108-preview-mutations.json` and `sync108-preview-mutation-*.txt`
- `sync108_preview_mutation.py` and `run_sync108_preview_mutations.py`

All payloads were disposable fixtures; no production library, Syncthing
service or configuration was accessed. Independent exact-head review and
green current-source CI remain required before a pinned merge.
