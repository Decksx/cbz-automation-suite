# Sync preview/verifier observations — 2026-11-13

David requested parallel X-migration development in hive #4888. Codex accepted
#108 through Claude's #4891 handoff, acknowledged in #4894. Claude owns #158 B2
part B and then the B3 coordination design. The current base is master
`23469d166ef2536733291910b2d50159b6dd3d2f`.

## Delivered slice

`comic_automation.library.sync_inventory.observe_pairs` supplies the shared
filesystem evidence for the forthcoming preview and verifier. Explicit folder
IDs bind paired roots and literal exclusions. It validates root identity and
separation before traversal, records coverage problems and exclusions, and
fully hashes each file whose size occurs on both sides once. Handle/path
checks and a second inventory expose observed drift. Output is deterministic,
JSON-compatible and digest-bound. Its scope is explicitly filesystem-only.

The durable API, exclusions, format and non-atomic observation limits are in
`docs/sync_preview_observations.md`. Development uses temporary local trees.
The report classifications, CLI and sanitized Syncthing configuration evidence
remain the next #108 slice. The issue remains open until those are delivered.

## Corrections found during implementation

A root can be replaced after initial binding and before the first inventory.
Comparing only the two later inventory walks missed that transition. The new
root-replacement witness failed before the final root-identity comparison was
restored and then passed. The regression output is retained as
`sync108-root-binding-before.txt`.

Three reserved Windows names (`COM¹.cbz`, `LPT².txt`, `CON .cbz`) exposed an
incomplete initial regex. Those three cases failed before validation was
delegated to the runtime's `PureWindowsPath.is_reserved`, then passed. Output
is retained as `sync108-windows-before.txt`. Failed hash attempts now retain
null byte counts because their read counts are uncertified. Unhashed files
outside the size intersection retain zero bytes read.

## Verification and inventory

The fresh base suite at master 23469d1 passed **6,608 tests / 12 skipped /
0 failed in 438.05 seconds** (6,620 collected cases). The final fixture module
adds **52 cases**, so the expected candidate inventory is **6,672**. Earlier
development full-suite runs preceded the final root/name/count refinements.
The final frozen version is retested below.

Focused final tests passed **52/52**. Code and tests are final at
`d32fc12a99e87cc531ffef8dc757396e0678f95a`; later development-log changes have
no executable delta. The full final suite passed **6,660 tests / 12 skipped /
0 failed in 362.77 seconds**, matching the 6,672-case inventory. Both suites
used Windows/Python 3.11, translation disabled and `-n 4 --dist loadfile`.
The focused run uses one pytest process. Source and test hashes remained
unchanged after the final suite and guard proofs. Diff check is clean.

Twenty guard groups were removed in separate pytest processes using AST
mutation in memory. The unchanged in-memory control passed all 52 cases.
Every group produced failing tests, totaling **56 failing test executions
with overlap**. These deliberate negative results are separate from the
application suite.

| Removed behavior | Failing cases |
| --- | ---: |
| Root overlap/alias refusal | 3 |
| Reparse checks | 3 |
| Physical identity requirement | 1 |
| History/control/protected component exclusions | 5 |
| Syncthing temporary-name exclusion | 1 |
| Declared prefix exclusion | 1 |
| Windows path syntax/reserved-name checks | 18 |
| Case/Unicode normalization | 1 |
| Collision refusal | 1 |
| Hard-link refusal | 1 |
| Cross-side size prefilter | 2 |
| Pre-read handle comparison | 2 |
| Complete byte-count requirement | 1 |
| Post-read handle comparison | 1 |
| Post-read pathname comparison | 1 |
| Final tree comparison | 2 |
| Initial root-binding comparison | 1 |
| Complete topology gate before walking | 5 |
| Root/scope binding in the digest | 2 |
| Unknown byte count after a failed hash | 4 |

The first mutation harness copied module globals, which bypassed the tests'
monkeypatch hooks, and an overly broad AST selector subsequently failed setup.
Those counts were discarded. The corrected harness executes into the module's
own namespace and requires a passing unmodified control before checking
mutants. Early artifacts are retained in the mutation-attempt directories.
The final run verified source bytes stayed identical throughout.

## Artifact locations and next action

Artifacts are under `C:\Users\david\.codex\tmp` and include
`sync108-base-suite.txt`, `sync108-mutation-control.txt`, `sync108-final52-suite.txt`,
`sync108-final52.xml`, `sync108-mutations.json`, the individual
`sync108-mutation-*.txt` logs, `sync108_mutation.py` and
`run_sync108_mutations.py`. All child processes remain in the inherited Job
Object. The implementation and tests use dedicated sync files.

Final working-file SHA-256 values are

- source `983732a2ccbc4ac08dfa0ea8fea474560708a57eabb246ba445edfad594ce323`
- tests `b4e615e4bb7c8acf66c31f5e689c955bee19d8655ed1a507c049b95655c5a53d`

Claude independently reviews the resulting exact source commit. Review and CI
precede a pinned merge. The next implementation consumes these observations
for keep/write/remove/unresolved accounting and post-sync path verification,
then supplies the CLI and configuration-evidence boundary from #108.
