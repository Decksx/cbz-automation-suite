# Filesystem observations for the Syncthing migration reports

Issue #108 implements the Tower-to-X preview and post-sync verification on
explicit root pairs. The first implementation PR supplies their shared
filesystem observation API in `comic_automation.library.sync_inventory`.
The CLI exposes estimates, observations and the filesystem preview described
below. The fresh post-sync verifier and sanitized Syncthing configuration
evidence remain subsequent deliveries. Issue #108 stays open.

## API and scope

```python
from pathlib import Path
from comic_automation.library.sync_inventory import RootPair, observe_pairs

observation = observe_pairs((
    RootPair("manga", Path("fixture/tower"), Path("fixture/receiver"),
             exclude_prefixes=("private",)),
))
```

Each pair identifies one Syncthing folder ID, a desired Tower tree and a
receiver tree. Only those explicitly declared roots are observed. Other
library roots, backups and original collections outside them remain outside
the observation. Callers supply local temporary roots during development.

`exclude_prefixes` contains literal relative paths, matched by complete path
components after NFC normalization and case folding. Both sides share the
same prefixes. Input rejects absolute paths, parent traversal, empty components
and Windows-unrepresentable names. Syncthing ignore-file syntax needs a separate
adapter to establish effective configuration scope.

The policy includes the existing shared directory exclusions, `.stignore`,
`.syncthing.*.tmp` temporary names and protected original-directory names
`ai-decensor-originals` / `ai_decensor_originals`, at any depth. An excluded
subtree is recorded at its boundary and is never descended or hashed. A root
inside a protected original-directory path is refused. The output binds the
literal prefixes and built-in exclusion policy into its digest.

## Observation process

1. Establish every root and ancestor as a plain directory with a usable
   filesystem identity. Compare normalized roots and physical ancestor
   identities to reject aliases or nested roots, including across pairs.
2. Walk the declared trees without following links. Retain every regular
   file's relative path, size, identity and nanosecond mtime/ctime observations.
   Record explicit exclusions and errors. Hard-linked payloads, reparse points,
   unsupported entry types and Windows path collisions make coverage incomplete.
3. Intersect the file-size sets across both sides and all declared folder IDs.
   Report the candidate file count and summed logical bytes before opening any
   payload. Fully SHA-256 each file in that intersection once, provided initial
   inventory coverage is complete. Files of other sizes
   carry a null hash and zero payload bytes read.
4. Check the opened file's identity/size/timestamps against the inventory before
   reading, check byte count and handle observations afterward, then check the
   pathname and its components again. A failed observation supplies a problem
   and a null hash/byte count, since its payload-read count is uncertified.
   Reads stop after the observed size plus one byte, so a
   continuously growing source cannot make the read unbounded.
5. Re-establish each root and repeat the tree walk after hashing. An observed
   addition, removal, replacement, metadata change or coverage error makes
   `complete` false.

`complete` describes the filesystem observation's coverage. A caller making
absence or negative content assertions must require it. The API's `scope` is
always `filesystem-only`. Syncthing folder roles, effective ignore rules,
versioning, peers and index authority require their own bound evidence before
the future report can claim an exact Syncthing scope.

## Output and limits

The JSON-compatible document has contract `sync-filesystem-observation-v1`,
root bindings, file rows, exclusion rows, problems, observed file counts and an
`observation_digest`. The digest is SHA-256 over UTF-8 JSON of the other fields,
with sorted keys, compact separators, Unicode retained and non-finite numbers
refused. Entries are sorted by folder ID, side and relative path. Repeating an
observation of unchanged fixture trees produces the same document and digest.

Counts cover inventoried regular files. Error records describe gaps whose
population may be unknown. Excluded subtree records identify boundaries and do
not claim descendant file counts. Many-to-many hash matches and path-based
keep/write/remove accounting belong to the report layer.

The walk and hashing observe live files without locking the library. The
before/after checks expose observed drift, while the report retains the
non-atomic scan limitation. A file can change after its final observation.
Timestamp/file-ID checks alone cannot establish immutable bytes. The preview
and verifier must bind fresh observations and report later drift.

## Estimates and progress

`observe_pairs(pairs, hash_content=False)` inventories and rechecks both trees
without opening payload files. Its `hash_mode` is `estimate`; all file hashes
remain null and payload bytes read remain zero. `hash_plan` gives candidate
files and bytes, including separate `tower` and `target` totals. The default
`hash_content=True` uses mode `content`. Mode and plan are digest-bound.

Candidate bytes count the logical file content that a complete content pass
would read across both sides. They are neither predicted SMB traffic nor an ETA:
cache hits, storage throughput, metadata operations and failures affect elapsed
time. Nearly identical trees can share almost every file size and therefore
require hashing almost everything. An estimate supplies no content-equality
evidence, even when `complete` is true.

An optional `progress=callback` receives independent dictionaries for inventory,
directory counts, the hash plan, file starts/results, bytes read within a file,
the final tree recheck and completion. Progress timing is outside the document
and digest. Attempted read bytes can include reads later rejected by drift
checks; file-row `bytes_read` remains the certified value or null. A consumer
exception aborts with `ProgressError`; it is not turned into a filesystem gap.

## Read-only CLI

Keep the manifest and redirected artifacts outside the compared trees. A
manifest declares filesystem scope only; it does not accept raw Syncthing
configuration or credentials. Relative paths resolve against its directory:

```json
{
  "contract": "sync-root-pairs-v1",
  "pairs": [
    {"folder_id": "manga", "tower": "fixture/tower",
     "target": "fixture/receiver", "exclude_prefixes": ["private"]}
  ]
}
```

```powershell
python -m comic_automation.library.sync_observe_cli estimate --pairs pairs.json
python -m comic_automation.library.sync_observe_cli observe --pairs pairs.json
python -m comic_automation.library.sync_observe_cli preview --pairs pairs.json
```

All commands print JSON to stdout and throttled JSON progress to stderr.
`--quiet` suppresses progress. Exit 0 means complete observation, 2 means invalid
input or incomplete coverage, and 1 means an output/progress consumer failed.
An incomplete observation still prints its document. A complete estimate remains
an estimate, not a successful sync verification.

The CLI reads directories and files and prints its document; it has no sync,
apply, rename or configuration-control mode. It does not resume a prior hash
pass or promote cached stat-matching hashes to fresh evidence. Reuse needs an
explicit evidence-age contract first. Actual sync and cutover remain the
separately reviewed operator procedure in #108.

## Logical filesystem preview

`comic_automation.library.sync_preview.build_preview(observation)` derives a
`sync-filesystem-preview-v1` document. It accepts content observations and
checks their digest, file/root bindings, portable path collisions, certified
hash read lengths and observed file counts. A supposedly complete artifact
missing a shared-size candidate's hash is refused, including when the match
would be at another path. An estimate is refused. The
preview embeds an independent copy of the full observation and binds it and
every derived field into `preview_digest`, using the same canonical JSON
encoding as the observation digest. Digests bind artifacts; the producer's
filesystem stability and coverage checks remain prerequisites.

| Set | Meaning |
| --- | --- |
| `keep_in_place` | Exact corresponding folder ID/path, equal size and verified SHA-256. |
| `to_write`, `new_path` | Desired Tower path absent from the complete target inventory. |
| `to_write`, `replacement` | Corresponding path has unequal size or unequal verified bytes; retains `old_target`. |
| `to_remove` | In-scope target path absent from the desired Tower layout; replacements are not counted here. |
| `unresolved` | Coverage/stability is incomplete, needed full hashes are absent, or cross-side Windows path spellings conflict. |

Different sizes prove inequality without payload reads. Equal sizes need full
hashes before a keep or replacement is asserted. Incomplete coverage makes
every observed file unresolved, since absence cannot be inferred from gaps.
An incomplete observation with zero files is still incomplete. Case-only or
NFC-equivalent spelling differences across sides are held as unresolved.
Every directory prefix represented by the files is checked within each
folder ID, including conflicting spellings within one side. All files under
a conflicting prefix are held, even when their child names differ or a child
would otherwise be a same-path keep. Reasons name the conflicting folder
spellings. Prefix components keep `Series` separate from `SeriesExtra`.
Proposing writes and removals under spelling variants could address the same
Windows directory or file; the report does not resolve that policy itself.

`counts.tower` partitions observed files/bytes into keep, write and unresolved;
`counts.target` partitions them into keep, replaced, remove and unresolved.
`reconciled` checks every observed file has a disposition. Excluded boundaries
remain in the embedded observation; their unknown descendant populations are
not counted as observed library files. `metadata_differences` records mtime
differences for equal-content keeps. Neither keeps nor logical byte totals
predict Syncthing block transfers, metadata updates or physical disk writes.

`exact_matches_elsewhere` contains advisory groups keyed by `(size, sha256)`
with the full Tower and target path lists, including matches across folder
IDs. It includes only groups with a cross-path alternative. A row's size/hash
joins it to the matching group. The lists represent many-to-many alternatives
without expanding a Cartesian product or inflating file counts. An identical
file at another target path remains a new-path write at the desired location
and a removal at the obsolete location; this is not an automatic rename or a
semantic loss/supersession judgment.

`complete` means the filesystem classification is complete and reconciled.
`sync_ready` remains **false**: this slice has no bound Syncthing configuration
or effective-ignore evidence. CLI exit 0 is a complete filesystem comparison,
not permission or readiness to sync; exit 2 includes unresolved comparisons.
The preview command has no control/apply operation. The post-sync verifier
must still use fresh path-and-content evidence and detect source drift; it
is not implemented by this preview slice.

Directories themselves are not classified: empty-directory creation/removal
is outside this file accounting. A file-to-directory or directory-to-file
transition is a logical file delta, not an ordered execution plan; required
directory operations and their ordering are not modeled. These limits also
appear in each preview document. `complete` refers to observed files.
