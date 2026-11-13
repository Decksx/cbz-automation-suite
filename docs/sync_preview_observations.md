# Filesystem observations for the Syncthing migration reports

Issue #108 implements the Tower-to-X preview and post-sync verification on
explicit root pairs. The first implementation PR supplies their shared
filesystem observation API in `comic_automation.library.sync_inventory`.
Report classification, the CLI and sanitized Syncthing configuration evidence
follow in the next slice. Issue #108 remains open through those deliveries.

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
   Fully SHA-256 each file in that intersection once. Files of other sizes
   carry a null hash and zero payload bytes read.
4. Check the opened file's identity/size/timestamps against the inventory before
   reading, check byte count and handle observations afterward, then check the
   pathname and its components again. A failed observation supplies a problem
   and a null hash. Reads stop after the observed size plus one byte, so a
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

The API reads directories and files and returns its document in memory. A
later CLI will place generated artifacts outside the compared roots. Actual
sync and cutover remain the separately reviewed operator procedure in #108.
