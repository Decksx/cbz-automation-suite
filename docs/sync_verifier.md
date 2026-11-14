# Fresh filesystem verifier (#108)

The verifier compares a saved, fully hashed Tower baseline with fresh reads of
both declared sides. It checks bytes at the expected paths and refuses success
if Tower changed after the baseline. It does not control Syncthing or authorize
a migration, configuration change, or cutover.

Use the existing `sync-root-pairs-v1` manifest described in
[sync observation](sync_observation.md). Keep manifests and output artifacts
outside the compared roots. For example:

```powershell
python -m comic_automation.library.sync_observe_cli estimate --all-tower --pairs pairs.json
python -m comic_automation.library.sync_observe_cli baseline --pairs pairs.json > baseline.json
python -m comic_automation.library.sync_observe_cli verify --pairs pairs.json --expected baseline.json > verification.json
```

`baseline` hashes every Tower file, including sizes absent from the receiver.
It also hashes receiver files whose sizes occur on Tower. The same selection is
budgeted by `estimate --all-tower`, without reading payloads. `observe` and
`preview` accept `--all-tower` too. Baseline collection can read the entire
source library; the estimate reports logical candidate bytes, not elapsed time
or network traffic. Progress goes to stderr; `--quiet` suppresses it.

The Python API is `sync_verify.verify_pairs(expected, pairs, progress=None)`.
It takes the fresh observation itself. A caller cannot supply a saved actual
observation instead. A default content observation is acceptable as a baseline
if it happens to contain complete SHA-256 evidence for every Tower file.

Verification requires the same folder IDs, root paths and physical identities,
literal excluded prefixes, and built-in exclusion policy. Malformed, incomplete,
unhashed, digest-invalid, or internally conflicting baselines are refused before
actual payload reads. If current coverage or scope is uncertain, rows are
unresolved; the report does not assert that inaccessible files are absent.

With complete coverage, the report lists matched, missing, mismatched and extra
receiver files, plus source additions, removals and byte changes. Equal bytes at
a different path, including a different case spelling, do not pass. Fresh source
drift blocks success even when the receiver still matches the old baseline.
Metadata changes with equal fresh bytes are diagnostics and do not fail equality.
Observed file and logical-byte partitions are reconciled separately for the
expected source, current source and receiver. Both observations and the result
are digest-bound; an unkeyed digest detects accidental modification, not a
maliciously resealed document.

Exit 0 in `verify` means `verified=true`. Exit 2 means invalid input, incomplete
evidence or differences; exit 1 means output/progress delivery failed. Other
modes retain their complete-observation/classification exit contract.

The observations are not an atomic snapshot across machines. Empty directories,
transfer order and file-to-directory transitions are outside this file-level
comparison. Excluded boundaries remain outside its claims. Every report keeps
`sync_ready=false`: sanitized configuration/index/role/versioning evidence and
the separately authorized backup and operational checks are still required.
